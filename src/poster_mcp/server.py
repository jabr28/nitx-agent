"""MCP server exposing the poster-generation service to AI agents.

Design rules for every tool:
* descriptions say *when* to use the tool and what to call next;
* errors say what went wrong, whether anything was created, and the exact next step;
* nothing secret (API key, auth headers) ever appears in output or logs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from collections import deque
from typing import Annotated, Any, Literal

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from .backend import CATEGORIES, IMAGE_COUNT_MAX, PROMPT_MAX_CHARS
from .client import PosterServiceClient, ServiceError, Settings, install_redaction

log = logging.getLogger("poster_mcp")

INSTRUCTIONS = """\
Generates advertising posters (food, beverage, retail, seasonal) from a product photo, a background photo and a prompt.

Typical flow:
1. list_templates (optional) to pick a template_id.
2. create_poster_job -> returns job_id with status "processing". Generation takes 5-20 seconds.
3. get_job_status(job_id, wait_seconds=20) until status is "completed", "failed" or "cancelled".
4. If "failed" and error.retryable is true, call create_poster_job again with a NEW idempotency_key.

Never report a poster as ready unless get_job_status returned status "completed" with images.
"""

Category = Literal[tuple(CATEGORIES)]  # type: ignore[valid-type]


class Budget:
    """Sliding one-hour cap on images this server may request, to bound spend."""

    def __init__(self, max_images_per_hour: int, clock=time.monotonic):
        self.max = max_images_per_hour
        self._clock = clock
        self._events: deque[tuple[float, int]] = deque()
        # Idempotency keys already paid for: replaying them is free.
        self._paid_keys: set[str] = set()

    def _used(self) -> int:
        cutoff = self._clock() - 3600
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()
        return sum(n for _, n in self._events)

    def check(self, images: int, key: str) -> None:
        if key in self._paid_keys:
            return
        used = self._used()
        if used + images > self.max:
            retry_in = int(self._events[0][0] + 3600 - self._clock()) + 1 if self._events else 3600
            raise ToolError(
                f"BUDGET_EXCEEDED: this server may request at most {self.max} images per hour; "
                f"{used} already requested, this call needs {images}. No job was created. "
                f"Next step: reuse images you already generated, lower image_count, or wait ~{retry_in}s. "
                "If more images are genuinely needed, ask a human to raise POSTER_MAX_IMAGES_PER_HOUR."
            )

    def record(self, images: int, key: str) -> None:
        self._paid_keys.add(key)
        self._events.append((self._clock(), images))


def _explain(err: ServiceError, *, action: str, idempotency_key: str | None = None) -> ToolError:
    """Turn a ServiceError into a message an agent can act on."""
    d = err.details or {}
    if err.kind == "timeout" or err.kind == "unavailable":
        if action == "create":
            msg = (
                f"SERVICE_UNREACHABLE ({err.code}): the poster service did not answer after several attempts. "
                "The job MAY have been created. Next step: call create_poster_job again with the SAME arguments "
                f'and idempotency_key="{idempotency_key}" - it returns the existing job instead of creating a '
                "duplicate. If it keeps failing, stop and tell the user the poster service is unavailable."
            )
        else:
            msg = (
                f"SERVICE_UNREACHABLE ({err.code}): the poster service did not answer after several attempts. "
                "Nothing was changed. Next step: wait ~30 seconds and try again; if it keeps failing, tell the "
                "user the poster service is unavailable. Do not claim the job finished."
            )
    elif err.kind == "auth":
        msg = (
            "AUTH_FAILED: the server's poster-service credentials were rejected. This is a configuration "
            "problem the agent cannot fix. Next step: stop and tell a human to check POSTER_API_KEY."
        )
    elif err.code == "PROMPT_TOO_LONG":
        msg = (
            f"PROMPT_TOO_LONG: prompt is {d.get('actual')} characters; the maximum is {PROMPT_MAX_CHARS}. "
            "No job was created. Next step: shorten the prompt (keep product, scene and any headline text; "
            "drop filler adjectives) and call create_poster_job again."
        )
    elif err.code == "TEMPLATE_NOT_FOUND":
        msg = (
            f"TEMPLATE_NOT_FOUND: {err.message} No job was created. Next step: call list_templates to get a "
            "valid template_id, or omit template_id."
        )
    elif err.code == "JOB_NOT_FOUND":
        msg = (
            f"JOB_NOT_FOUND: {err.message} Next step: use the job_id exactly as returned by create_poster_job. "
            "Do not invent job ids."
        )
    elif err.code == "JOB_ALREADY_FINISHED":
        msg = (
            f"JOB_ALREADY_FINISHED: the job already ended with status '{d.get('job_status')}', so it cannot be "
            "cancelled. Next step: call get_job_status to read its result; nothing else is needed."
        )
    elif err.code == "IDEMPOTENCY_KEY_REUSED":
        msg = (
            "IDEMPOTENCY_KEY_REUSED: this idempotency_key was already used with different arguments "
            f"(existing job {d.get('existing_job_id')}). No new job was created. Next step: if you meant to "
            "start a new, different job, call create_poster_job again with a new idempotency_key."
        )
    elif err.kind == "rate_limited":
        msg = (
            f"RATE_LIMITED ({err.code}): {err.message} No job was created. Next step: wait "
            f"{d.get('retry_after_seconds', 10)} seconds (or let running jobs finish), then retry."
        )
    elif err.kind == "validation":
        field = d.get("field")
        msg = (
            f"INVALID_INPUT ({err.code}): {err.message} No job was created. Next step: fix "
            f"{field or 'the input'} and call the tool again."
        )
    else:
        msg = f"UNEXPECTED_ERROR ({err.code}): {err.message} Next step: tell the user the request failed."
    return ToolError(msg)


def _next_step_for(job: dict[str, Any]) -> str:
    status = job.get("status")
    if status == "processing":
        return (
            "Still generating. Call get_job_status again with wait_seconds=20. "
            "If it is still processing after ~2 minutes, call cancel_job and tell the user."
        )
    if status == "completed":
        return "Done. Show the image URLs to the user. URLs expire in about an hour; do not store them long-term."
    if status == "failed":
        if job.get("error", {}).get("retryable"):
            return (
                "Generation failed for a temporary reason. Retry once by calling create_poster_job with the same "
                "inputs and a NEW idempotency_key. If it fails again, tell the user."
            )
        return (
            "Generation failed because of the input (see error.message). Do not retry unchanged; ask the user for "
            "a different product_image or adjust the inputs."
        )
    if status == "cancelled":
        return "The job was cancelled; no images will be produced."
    return ""


def build_server(
    client: PosterServiceClient,
    *,
    max_images_per_hour: int = 40,
    poll_interval_seconds: float = 1.0,
) -> MCPServer:
    mcp = MCPServer(name="nitx-poster", instructions=INSTRUCTIONS, version="1.0.0")
    budget = Budget(max_images_per_hour)

    @mcp.tool(
        title="List poster templates",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    )
    async def list_templates(
        category: Annotated[
            Category | None, Field(description="Only return templates in this category. Omit for all.")
        ] = None,
        search: Annotated[
            str | None,
            Field(description="Case-insensitive text to match in the template name, e.g. 'coffee'.", max_length=100),
        ] = None,
    ) -> dict[str, Any]:
        """Browse ready-made poster templates. Use it to find a template_id for create_poster_job.

        Using a template is optional. Returns {"templates": [{"id", "name", "category"}]}.
        An empty list means nothing matched: retry with a broader search or no filters.
        """
        try:
            return await client.list_templates(category, search)
        except ServiceError as err:
            raise _explain(err, action="read") from None

    @mcp.tool(
        title="Start a poster generation job",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
    )
    async def create_poster_job(
        product_image: Annotated[
            str, Field(description="https URL of the product photo (e.g. the coffee cup) to place in the poster.")
        ],
        background_image: Annotated[str, Field(description="https URL of the background/scene photo.")],
        prompt: Annotated[
            str,
            Field(
                description=(
                    f"What the poster should show, at most {PROMPT_MAX_CHARS} characters. Include any headline "
                    "text exactly as it must appear, in Arabic or English."
                ),
                # Advertised to the model but enforced below/by the backend, so the agent
                # gets an actionable PROMPT_TOO_LONG message instead of a raw validation dump.
                json_schema_extra={"minLength": 1, "maxLength": PROMPT_MAX_CHARS},
            ),
        ],
        image_count: Annotated[
            int, Field(
                description="How many variations to generate, 1-4. Each one costs money; default 1.",
                json_schema_extra={"minimum": 1, "maximum": IMAGE_COUNT_MAX},
            ),
        ] = 1,
        resolution: Annotated[
            Literal["1k", "2k"], Field(description='"1k" (1024px, default, for screens/drafts) or "2k" (2048px, print).')
        ] = "1k",
        template_id: Annotated[
            str | None, Field(description="Optional template id from list_templates.")
        ] = None,
        idempotency_key: Annotated[
            str | None,
            Field(
                description=(
                    "Optional. Calls with the same key and same inputs return the SAME job instead of creating a "
                    "duplicate, so retries after a timeout are safe. If omitted, a key is derived from the inputs, "
                    "so an identical request returns the existing job. Pass a NEW key to deliberately generate "
                    "another variation (e.g. after a failed job)."
                ),
                max_length=128,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Start generating poster images. Returns immediately with job_id and status "processing".

        Generation takes about 5-20 seconds. Then call get_job_status(job_id, wait_seconds=20) until the
        status is "completed" (images ready), "failed" or "cancelled". Do not tell the user the poster is
        ready until get_job_status says "completed".
        """
        payload = {
            "template_id": template_id,
            "product_image": product_image,
            "background_image": background_image,
            "prompt": prompt,
            "image_count": image_count,
            "resolution": resolution,
        }
        key = idempotency_key or "auto-" + hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()[:32]

        if not 1 <= image_count <= IMAGE_COUNT_MAX:
            raise ToolError(
                f"INVALID_IMAGE_COUNT: image_count must be 1-{IMAGE_COUNT_MAX}; got {image_count}. No job was "
                f"created. Next step: call create_poster_job again with image_count between 1 and {IMAGE_COUNT_MAX} "
                "(use 1 unless the user asked for variations)."
            )
        budget.check(image_count, key)
        try:
            job = await client.create_job(payload, key)
        except ServiceError as err:
            raise _explain(err, action="create", idempotency_key=key) from None
        if not job.get("idempotent_replay"):
            budget.record(image_count, key)
        job["idempotency_key"] = key
        job["next_step"] = (
            "Existing job returned (same idempotency_key); no duplicate was created. " if job.get("idempotent_replay") else ""
        ) + _next_step_for(job)
        return job

    @mcp.tool(
        title="Check a poster job",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    )
    async def get_job_status(
        job_id: Annotated[str, Field(description="The job_id returned by create_poster_job.", max_length=64)],
        wait_seconds: Annotated[
            int,
            Field(
                description=(
                    "Wait up to this many seconds for the job to finish before answering (0-30). "
                    "Use 20 to avoid polling in a tight loop."
                ),
                ge=0,
                le=30,
            ),
        ] = 0,
    ) -> dict[str, Any]:
        """Get the status of a poster job: "processing", "completed", "failed" or "cancelled".

        When "completed", `images` holds the poster URLs. When "failed", `error` has a code, a message and
        `retryable`. Every response includes `next_step` telling you what to do.
        """
        deadline = anyio.current_time() + wait_seconds
        try:
            while True:
                job = await client.get_job(job_id)
                if job.get("status") != "processing" or anyio.current_time() >= deadline:
                    break
                await anyio.sleep(min(poll_interval_seconds, max(deadline - anyio.current_time(), 0)))
        except ServiceError as err:
            raise _explain(err, action="read") from None
        job["next_step"] = _next_step_for(job)
        return job

    @mcp.tool(
        title="Cancel a poster job",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True),
    )
    async def cancel_job(
        job_id: Annotated[str, Field(description="The job_id to stop.", max_length=64)],
    ) -> dict[str, Any]:
        """Stop a job that is still processing. Returns status "cancelled".

        Fails with JOB_ALREADY_FINISHED if the job already completed or failed; in that case read the
        result with get_job_status instead.
        """
        try:
            job = await client.cancel_job(job_id)
        except ServiceError as err:
            raise _explain(err, action="cancel") from None
        job["next_step"] = _next_step_for(job)
        return job

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Nitx poster MCP server")
    parser.add_argument("--transport", choices=["stdio", "http"], default=os.environ.get("MCP_TRANSPORT", "stdio"))
    parser.add_argument("--host", default=os.environ.get("MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", 8000)))
    args = parser.parse_args()

    # Logs go to stderr: stdout is the protocol channel for the stdio transport.
    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        settings = Settings.from_env()
    except RuntimeError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        sys.exit(2)
    install_redaction(settings.api_key)
    # httpx logs every request line at INFO; ours are enough.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    client = PosterServiceClient(settings)
    server = build_server(
        client, max_images_per_hour=int(os.environ.get("POSTER_MAX_IMAGES_PER_HOUR", 40))
    )
    if args.transport == "stdio":
        server.run("stdio")
    else:
        from mcp.server.transport_security import TransportSecuritySettings

        allowed = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
        allowed += [f"127.0.0.1:{args.port}", f"localhost:{args.port}"]
        server.run(
            "streamable-http",
            host=args.host,
            port=args.port,
            transport_security=TransportSecuritySettings(allowed_hosts=allowed),
        )


if __name__ == "__main__":
    main()
