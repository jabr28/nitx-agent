"""HTTP client for the poster service, plus secret-safe logging.

The client owns three concerns the MCP layer should not care about:
authentication, retries/timeouts, and turning HTTP failures into a typed
`ServiceError` the tool layer can explain to an agent.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import anyio
import httpx

log = logging.getLogger("poster_mcp")


class RedactingFilter(logging.Filter):
    """Replaces known secret values in every log record before it is emitted."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self._secrets:
            message = message.replace(secret, "***REDACTED***")
        record.msg, record.args = message, None
        return True


def install_redaction(secret: str) -> None:
    """Attach the redaction filter to every handler on the root logger."""
    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(RedactingFilter([secret]))


@dataclass
class ServiceError(Exception):
    """A failure talking to the poster service, already classified."""

    kind: str  # timeout | unavailable | auth | validation | not_found | conflict | rate_limited | unknown
    code: str
    message: str
    status: int | None = None
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        return f"{self.kind}/{self.code}: {self.message}"


@dataclass
class Settings:
    base_url: str
    api_key: str
    timeout_seconds: float = 10.0
    max_retries: int = 2

    @classmethod
    def from_env(cls) -> "Settings":
        api_key = os.environ.get("POSTER_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "POSTER_API_KEY is not set. Put it in the environment (see .env.example); "
                "it is never accepted as a tool argument."
            )
        return cls(
            base_url=os.environ.get("POSTER_API_BASE_URL", "http://127.0.0.1:8080"),
            api_key=api_key,
            timeout_seconds=float(os.environ.get("POSTER_API_TIMEOUT_SECONDS", 10)),
            max_retries=int(os.environ.get("POSTER_API_MAX_RETRIES", 2)),
        )


_KIND_BY_STATUS = {
    400: "validation",
    401: "auth",
    403: "auth",
    404: "not_found",
    409: "conflict",
    422: "validation",
    429: "rate_limited",
}


class PosterServiceClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self._settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=settings.timeout_seconds,
            transport=transport,
            headers={"Authorization": f"Bearer {settings.api_key}"},
        )

    @property
    def timeout_seconds(self) -> float:
        return self._settings.timeout_seconds

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, *, retry: bool, **kwargs: Any) -> dict[str, Any]:
        """Send a request. Retries only network failures and 5xx, and only when `retry` is set.

        Callers pass retry=True for reads and for writes protected by an idempotency key.
        """
        attempts = 1 + (self._settings.max_retries if retry else 0)
        last: ServiceError | None = None
        for attempt in range(attempts):
            if attempt:
                await anyio.sleep(min(0.5 * 2 ** (attempt - 1), 4))
            try:
                resp = await self._http.request(method, path, **kwargs)
            except httpx.TimeoutException:
                last = ServiceError(
                    "timeout", "TIMEOUT", f"No response within {self._settings.timeout_seconds:.0f}s."
                )
                log.warning("%s %s timed out (attempt %d/%d)", method, path, attempt + 1, attempts)
                continue
            except httpx.TransportError as exc:
                last = ServiceError("unavailable", "CONNECTION_FAILED", type(exc).__name__)
                log.warning("%s %s connection failed (attempt %d/%d)", method, path, attempt + 1, attempts)
                continue

            if resp.status_code >= 500:
                last = ServiceError("unavailable", "SERVER_ERROR", f"HTTP {resp.status_code}", resp.status_code)
                log.warning("%s %s -> %d (attempt %d/%d)", method, path, resp.status_code, attempt + 1, attempts)
                continue

            try:
                data = resp.json()
            except ValueError:
                data = {}
            if resp.is_success:
                log.info("%s %s -> %d", method, path, resp.status_code)
                return data

            err = data.get("error", {}) if isinstance(data, dict) else {}
            log.info("%s %s -> %d %s", method, path, resp.status_code, err.get("code"))
            raise ServiceError(
                kind=_KIND_BY_STATUS.get(resp.status_code, "unknown"),
                code=err.get("code", f"HTTP_{resp.status_code}"),
                message=err.get("message", resp.reason_phrase),
                status=resp.status_code,
                details={k: v for k, v in err.items() if k not in ("code", "message")},
            )
        assert last is not None
        raise last

    async def list_templates(self, category: str | None, search: str | None) -> dict[str, Any]:
        params = {k: v for k, v in {"category": category, "search": search}.items() if v}
        return await self._request("GET", "/v1/templates", params=params, retry=True)

    async def create_job(self, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        # Safe to retry because the backend deduplicates on the idempotency key.
        return await self._request(
            "POST", "/v1/poster-jobs", json=payload, headers={"Idempotency-Key": idempotency_key}, retry=True
        )

    async def get_job(self, job_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/poster-jobs/{job_id}", retry=True)

    async def cancel_job(self, job_id: str) -> dict[str, Any]:
        # Cancelling twice is harmless (the backend returns the cancelled job), so retry is safe.
        return await self._request("POST", f"/v1/poster-jobs/{job_id}/cancel", retry=True)
