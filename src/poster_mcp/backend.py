"""Mock poster-generation backend.

Stands in for the real (unavailable) Nitx creative service. It behaves like an
asynchronous job API:

* a job takes MOCK_MIN_SECONDS..MOCK_MAX_SECONDS (default 5-20s) to finish,
* about MOCK_FAIL_RATE (default 10%) of jobs fail at random,
* invalid input is rejected with a 422 and a machine-readable error,
* every request needs a bearer API key, and each key belongs to one tenant;
  a tenant can never see another tenant's jobs,
* the Idempotency-Key header makes job creation safe to retry.

Job state is computed lazily from timestamps, so there are no background
workers and tests can drive time with an injected clock.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

PROMPT_MAX_CHARS = 500
IMAGE_COUNT_MIN, IMAGE_COUNT_MAX = 1, 4
RESOLUTIONS = {"1k": 1024, "2k": 2048}
IDEMPOTENCY_TTL_SECONDS = 24 * 3600

TEMPLATES: list[dict[str, str]] = [
    {"id": "tpl_coffee_minimal", "name": "Minimal Coffee Cup", "category": "beverage"},
    {"id": "tpl_coffee_weekend", "name": "Weekend Coffee Sale", "category": "promotion"},
    {"id": "tpl_juice_splash", "name": "Fresh Juice Splash", "category": "beverage"},
    {"id": "tpl_burger_bold", "name": "Bold Burger Close-up", "category": "food"},
    {"id": "tpl_dessert_pastel", "name": "Pastel Dessert Table", "category": "food"},
    {"id": "tpl_ramadan_lantern", "name": "Ramadan Lantern Night", "category": "seasonal"},
    {"id": "tpl_national_day", "name": "Saudi National Day Green", "category": "seasonal"},
    {"id": "tpl_flash_sale", "name": "Flash Sale Countdown", "category": "promotion"},
    {"id": "tpl_retail_shelf", "name": "Retail Shelf Spotlight", "category": "retail"},
    {"id": "tpl_retail_bogo", "name": "Buy One Get One", "category": "retail"},
]
CATEGORIES = sorted({t["category"] for t in TEMPLATES})

# (code, message, retryable)
FAILURE_REASONS = [
    ("GPU_TIMEOUT", "The image model timed out while rendering.", True),
    ("RENDER_ERROR", "The renderer crashed while compositing the poster.", True),
    (
        "PRODUCT_SEGMENTATION_FAILED",
        "The product could not be cut out of product_image (busy background or product too small).",
        False,
    ),
]


@dataclass
class MockConfig:
    min_seconds: float = 5.0
    max_seconds: float = 20.0
    fail_rate: float = 0.1
    # Force the first N jobs to fail, for reproducible demos and evals.
    fail_first_n: int = 0
    # Artificial latency added to every request (to exercise client timeouts).
    latency_seconds: float = 0.0
    max_active_jobs_per_tenant: int = 5
    seed: int | None = None
    # api_key -> tenant_id
    api_keys: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "MockConfig":
        keys: dict[str, str] = {}
        # Format: "tenant_a:key_a,tenant_b:key_b"
        for pair in filter(None, os.environ.get("MOCK_API_KEYS", "").split(",")):
            tenant, _, key = pair.strip().partition(":")
            if tenant and key:
                keys[key] = tenant
        seed = os.environ.get("MOCK_SEED")
        return cls(
            min_seconds=float(os.environ.get("MOCK_MIN_SECONDS", 5)),
            max_seconds=float(os.environ.get("MOCK_MAX_SECONDS", 20)),
            fail_rate=float(os.environ.get("MOCK_FAIL_RATE", 0.1)),
            fail_first_n=int(os.environ.get("MOCK_FAIL_FIRST_N", 0)),
            latency_seconds=float(os.environ.get("MOCK_LATENCY_SECONDS", 0)),
            max_active_jobs_per_tenant=int(os.environ.get("MOCK_MAX_ACTIVE_JOBS", 5)),
            seed=int(seed) if seed else None,
            api_keys=keys,
        )


@dataclass
class Job:
    id: str
    tenant: str
    request: dict[str, Any]
    created_at: float
    duration: float
    failure: tuple[str, str, bool] | None
    cancelled_at: float | None = None

    def status(self, now: float) -> str:
        if self.cancelled_at is not None:
            return "cancelled"
        if now < self.created_at + self.duration:
            return "processing"
        return "failed" if self.failure else "completed"

    def to_dict(self, now: float) -> dict[str, Any]:
        status = self.status(now)
        out: dict[str, Any] = {
            "job_id": self.id,
            "status": status,
            "created_at": round(self.created_at, 3),
            "elapsed_seconds": round(min(now, self.created_at + self.duration) - self.created_at, 1)
            if status != "cancelled"
            else round((self.cancelled_at or now) - self.created_at, 1),
        }
        if status == "processing":
            out["estimated_seconds_remaining"] = round(self.created_at + self.duration - now, 1)
        elif status == "completed":
            size = RESOLUTIONS[self.request["resolution"]]
            expires = int(time.time()) + 3600
            out["images"] = [
                {
                    "url": f"https://cdn.mock-nitx.local/{self.tenant}/{self.id}/{i + 1}.png"
                    f"?expires={expires}&sig={secrets.token_hex(8)}",
                    "width": size,
                    "height": size,
                    "expires_in_seconds": 3600,
                }
                for i in range(self.request["image_count"])
            ]
        elif status == "failed":
            code, message, retryable = self.failure  # type: ignore[misc]
            out["error"] = {"code": code, "message": message, "retryable": retryable}
        return out


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any):
        self.status, self.code, self.message, self.extra = status, code, message, extra


def _validate_create(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ApiError(422, "INVALID_BODY", "Request body must be a JSON object.")

    def url_field(name: str) -> str:
        value = body.get(name)
        if not isinstance(value, str) or not value.startswith(("https://", "http://")):
            raise ApiError(422, "INVALID_FIELD", f"{name} must be an http(s) URL to an image.", field=name)
        if len(value) > 2048:
            raise ApiError(422, "INVALID_FIELD", f"{name} is longer than 2048 characters.", field=name)
        return value

    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ApiError(422, "INVALID_FIELD", "prompt is required and must be non-empty text.", field="prompt")
    if len(prompt) > PROMPT_MAX_CHARS:
        raise ApiError(
            422,
            "PROMPT_TOO_LONG",
            f"prompt is {len(prompt)} characters; the maximum is {PROMPT_MAX_CHARS}.",
            field="prompt",
            max=PROMPT_MAX_CHARS,
            actual=len(prompt),
        )

    image_count = body.get("image_count", 1)
    if isinstance(image_count, bool) or not isinstance(image_count, int) or not (
        IMAGE_COUNT_MIN <= image_count <= IMAGE_COUNT_MAX
    ):
        raise ApiError(
            422,
            "INVALID_IMAGE_COUNT",
            f"image_count must be an integer from {IMAGE_COUNT_MIN} to {IMAGE_COUNT_MAX}; got {image_count!r}.",
            field="image_count",
        )

    resolution = body.get("resolution", "1k")
    if resolution not in RESOLUTIONS:
        raise ApiError(
            422,
            "INVALID_RESOLUTION",
            f'resolution must be "1k" or "2k"; got {resolution!r}.',
            field="resolution",
        )

    template_id = body.get("template_id")
    if template_id is not None and template_id not in {t["id"] for t in TEMPLATES}:
        raise ApiError(404, "TEMPLATE_NOT_FOUND", f"No template with id {template_id!r}.", field="template_id")

    return {
        "template_id": template_id,
        "product_image": url_field("product_image"),
        "background_image": url_field("background_image"),
        "prompt": prompt,
        "image_count": image_count,
        "resolution": resolution,
    }


def create_app(config: MockConfig | None = None, clock: Callable[[], float] = time.time) -> FastAPI:
    config = config or MockConfig.from_env()
    rng = random.Random(config.seed)
    jobs: dict[str, Job] = {}
    # (tenant, idempotency_key) -> (payload_hash, job_id, created_at)
    idempotency: dict[tuple[str, str], tuple[str, str, float]] = {}
    counter = {"created": 0}

    app = FastAPI(title="Mock Poster Service", version="1.0")
    app.state.config = config
    app.state.jobs = jobs

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        headers = {}
        if "retry_after_seconds" in exc.extra:
            headers["Retry-After"] = str(exc.extra["retry_after_seconds"])
        return JSONResponse(
            status_code=exc.status,
            content={"error": {"code": exc.code, "message": exc.message, **exc.extra}},
            headers=headers,
        )

    @app.middleware("http")
    async def _latency(request: Request, call_next):
        if config.latency_seconds:
            import anyio

            await anyio.sleep(config.latency_seconds)
        return await call_next(request)

    def tenant_for(authorization: str | None) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise ApiError(401, "UNAUTHORIZED", "Missing bearer token.")
        tenant = config.api_keys.get(authorization.removeprefix("Bearer ").strip())
        if tenant is None:
            raise ApiError(401, "UNAUTHORIZED", "Invalid API key.")
        return tenant

    def get_owned_job(tenant: str, job_id: str) -> Job:
        job = jobs.get(job_id)
        # Another tenant's job is reported as "not found" so ids cannot be probed.
        if job is None or job.tenant != tenant:
            raise ApiError(404, "JOB_NOT_FOUND", f"No job with id {job_id!r}.")
        return job

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/templates")
    async def list_templates(
        category: str | None = None,
        search: str | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        tenant_for(authorization)
        if category is not None and category not in CATEGORIES:
            raise ApiError(422, "INVALID_CATEGORY", f"Unknown category {category!r}.", allowed=CATEGORIES)
        items = [
            t
            for t in TEMPLATES
            if (category is None or t["category"] == category)
            and (not search or search.lower() in t["name"].lower())
        ]
        return {"templates": items}

    @app.post("/v1/poster-jobs", status_code=202)
    async def create_job(
        request: Request,
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None),
    ) -> JSONResponse:
        tenant = tenant_for(authorization)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            raise ApiError(400, "INVALID_JSON", "Request body is not valid JSON.")
        payload = _validate_create(body)
        payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        now = clock()

        if idempotency_key:
            hit = idempotency.get((tenant, idempotency_key))
            if hit and now - hit[2] < IDEMPOTENCY_TTL_SECONDS:
                if hit[0] != payload_hash:
                    raise ApiError(
                        409,
                        "IDEMPOTENCY_KEY_REUSED",
                        "This idempotency key was already used for a different request.",
                        existing_job_id=hit[1],
                    )
                job = jobs[hit[1]]
                return JSONResponse(
                    status_code=200,
                    content={**job.to_dict(now), "idempotent_replay": True},
                )

        active = sum(1 for j in jobs.values() if j.tenant == tenant and j.status(now) == "processing")
        if active >= config.max_active_jobs_per_tenant:
            raise ApiError(
                429,
                "TOO_MANY_ACTIVE_JOBS",
                f"This account already has {active} jobs processing (limit {config.max_active_jobs_per_tenant}).",
                retry_after_seconds=10,
            )

        counter["created"] += 1
        if counter["created"] <= config.fail_first_n:
            failure = FAILURE_REASONS[0]  # forced failures are always retryable
        elif rng.random() < config.fail_rate:
            failure = rng.choice(FAILURE_REASONS)
        else:
            failure = None
        job = Job(
            id=f"job_{secrets.token_hex(6)}",
            tenant=tenant,
            request=payload,
            created_at=now,
            duration=rng.uniform(config.min_seconds, config.max_seconds),
            failure=failure,
        )
        jobs[job.id] = job
        if idempotency_key:
            idempotency[(tenant, idempotency_key)] = (payload_hash, job.id, now)
        return JSONResponse(status_code=202, content={**job.to_dict(now), "idempotent_replay": False})

    @app.get("/v1/poster-jobs/{job_id}")
    async def get_job(job_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        tenant = tenant_for(authorization)
        return get_owned_job(tenant, job_id).to_dict(clock())

    @app.post("/v1/poster-jobs/{job_id}/cancel")
    async def cancel_job(job_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        tenant = tenant_for(authorization)
        job = get_owned_job(tenant, job_id)
        now = clock()
        status = job.status(now)
        if status == "cancelled":
            return job.to_dict(now)
        if status != "processing":
            raise ApiError(
                409,
                "JOB_ALREADY_FINISHED",
                f"Job {job_id} already finished with status {status!r} and cannot be cancelled.",
                job_status=status,
            )
        job.cancelled_at = now
        return job.to_dict(now)

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(
        create_app(),
        host=os.environ.get("MOCK_HOST", "127.0.0.1"),
        port=int(os.environ.get("MOCK_PORT", 8080)),
        log_level="info",
    )


if __name__ == "__main__":
    main()
