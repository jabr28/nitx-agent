"""End-to-end tests: MCP client -> MCP server -> HTTP client -> mock backend (in-process)."""

from __future__ import annotations

import logging

import anyio
import httpx
import pytest

from conftest import API_KEY, OTHER_KEY, VALID_JOB, fast_config, harness, text
from poster_mcp.client import RedactingFilter, Settings

pytestmark = pytest.mark.anyio


# --- discovery -----------------------------------------------------------------------------------


async def test_tools_are_listed_with_constraints_an_llm_can_read(h):
    tools = {t.name: t for t in (await h.mcp.list_tools()).tools}
    assert set(tools) == {"list_templates", "create_poster_job", "get_job_status", "cancel_job"}
    props = tools["create_poster_job"].input_schema["properties"]
    assert props["prompt"]["maxLength"] == 500
    assert (props["image_count"]["minimum"], props["image_count"]["maximum"]) == (1, 4)
    assert props["resolution"]["enum"] == ["1k", "2k"]
    assert set(tools["create_poster_job"].input_schema["required"]) == {
        "product_image",
        "background_image",
        "prompt",
    }
    # Every tool explains itself.
    assert all(len(t.description or "") > 60 for t in tools.values())


async def test_list_templates_filters_by_category_and_search(h):
    r = await h.call("list_templates", {"category": "beverage"})
    assert not r.is_error
    assert {t["category"] for t in r.structured_content["templates"]} == {"beverage"}

    r = await h.call("list_templates", {"search": "COFFEE"})
    names = [t["name"] for t in r.structured_content["templates"]]
    assert names and all("coffee" in n.lower() for n in names)


# --- happy path ----------------------------------------------------------------------------------


async def test_job_runs_from_processing_to_completed_with_images(h):
    r = await h.call("create_poster_job", {**VALID_JOB, "image_count": 3, "resolution": "2k"})
    assert not r.is_error
    job = r.structured_content
    assert job["status"] == "processing"
    assert "get_job_status" in job["next_step"]

    r = await h.call("get_job_status", {"job_id": job["job_id"], "wait_seconds": 3})
    done = r.structured_content
    assert done["status"] == "completed"
    assert len(done["images"]) == 3
    assert all(img["url"].startswith("https://") and img["width"] == 2048 for img in done["images"])


# --- input validation ----------------------------------------------------------------------------


async def test_prompt_over_500_chars_is_rejected_with_a_fix(h):
    r = await h.call("create_poster_job", {**VALID_JOB, "prompt": "x" * 501})
    assert r.is_error
    msg = text(r)
    assert "PROMPT_TOO_LONG" in msg and "501" in msg and "500" in msg
    assert "No job was created" in msg and "shorten" in msg
    assert h.app.state.jobs == {}


@pytest.mark.parametrize("count", [0, 5, -1])
async def test_image_count_outside_1_to_4_is_rejected(h, count):
    r = await h.call("create_poster_job", {**VALID_JOB, "image_count": count})
    assert r.is_error
    assert "INVALID_IMAGE_COUNT" in text(r) and "between 1 and 4" in text(r)
    assert h.app.state.jobs == {}


async def test_unknown_template_points_agent_to_list_templates(h):
    r = await h.call("create_poster_job", {**VALID_JOB, "template_id": "tpl_does_not_exist"})
    assert r.is_error
    assert "TEMPLATE_NOT_FOUND" in text(r) and "list_templates" in text(r)


# --- idempotency ---------------------------------------------------------------------------------


async def test_retry_with_same_idempotency_key_returns_the_same_job(h):
    args = {**VALID_JOB, "idempotency_key": "brief-42-poster-1"}
    first = (await h.call("create_poster_job", args)).structured_content
    second = (await h.call("create_poster_job", args)).structured_content
    assert first["job_id"] == second["job_id"]
    assert second["idempotent_replay"] is True
    assert len(h.app.state.jobs) == 1


async def test_identical_request_without_key_is_also_deduplicated(h):
    a = (await h.call("create_poster_job", VALID_JOB)).structured_content
    b = (await h.call("create_poster_job", VALID_JOB)).structured_content
    assert a["job_id"] == b["job_id"] and len(h.app.state.jobs) == 1


async def test_reusing_a_key_for_different_inputs_is_refused(h):
    await h.call("create_poster_job", {**VALID_JOB, "idempotency_key": "k1"})
    r = await h.call("create_poster_job", {**VALID_JOB, "prompt": "something else", "idempotency_key": "k1"})
    assert r.is_error and "IDEMPOTENCY_KEY_REUSED" in text(r)
    assert len(h.app.state.jobs) == 1


async def test_timeout_after_server_created_job_does_not_create_a_duplicate():
    """The first create succeeds on the server but the response is lost; the client's retry
    (same idempotency key) must get the original job back, not a second one."""
    class LoseFirstCreateResponse(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner = None
            self.lost = False

        async def handle_async_request(self, request):
            response = await self.inner.handle_async_request(request)
            if request.method == "POST" and request.url.path == "/v1/poster-jobs" and not self.lost:
                self.lost = True
                raise httpx.ReadTimeout("simulated lost response", request=request)
            return response

    transport = LoseFirstCreateResponse()
    async with harness(transport=transport) as h:
        transport.inner = httpx.ASGITransport(app=h.app)
        r = await h.call("create_poster_job", VALID_JOB)
        assert not r.is_error
        assert r.structured_content["idempotent_replay"] is True
        assert len(h.app.state.jobs) == 1


# --- slow and failing service --------------------------------------------------------------------


async def test_service_timeout_tells_agent_to_retry_with_the_same_key():
    calls = {"n": 0}

    def always_timeout(request):
        calls["n"] += 1
        raise httpx.ReadTimeout("simulated", request=request)

    async with harness(transport=httpx.MockTransport(always_timeout)) as h:
        r = await h.call("create_poster_job", {**VALID_JOB, "idempotency_key": "abc-123"})
    assert r.is_error
    msg = text(r)
    assert "MAY have been created" in msg and 'idempotency_key="abc-123"' in msg
    assert calls["n"] == 3  # one try + two retries


async def test_failed_job_reports_reason_and_how_to_recover():
    async with harness(fast_config(fail_first_n=1)) as h:
        job = (await h.call("create_poster_job", VALID_JOB)).structured_content
        r = await h.call("get_job_status", {"job_id": job["job_id"], "wait_seconds": 3})
    st = r.structured_content
    assert st["status"] == "failed"
    assert st["error"]["retryable"] is True and st["error"]["code"]
    assert "NEW idempotency_key" in st["next_step"]
    assert "images" not in st


async def test_long_running_job_is_reported_as_processing_with_guidance():
    async with harness(fast_config(min_seconds=30, max_seconds=30)) as h:
        job = (await h.call("create_poster_job", VALID_JOB)).structured_content
        r = await h.call("get_job_status", {"job_id": job["job_id"], "wait_seconds": 1})
    st = r.structured_content
    assert st["status"] == "processing" and st["estimated_seconds_remaining"] > 0
    assert "cancel_job" in st["next_step"]


# --- cancel --------------------------------------------------------------------------------------


async def test_cancel_running_job(h):
    job = (await h.call("create_poster_job", VALID_JOB)).structured_content
    r = await h.call("cancel_job", {"job_id": job["job_id"]})
    assert r.structured_content["status"] == "cancelled"
    await anyio.sleep(0.4)  # past the job's natural finish time
    r = await h.call("get_job_status", {"job_id": job["job_id"]})
    assert r.structured_content["status"] == "cancelled" and "images" not in r.structured_content


async def test_cancel_finished_job_returns_an_explanatory_error(h):
    job = (await h.call("create_poster_job", VALID_JOB)).structured_content
    await h.call("get_job_status", {"job_id": job["job_id"], "wait_seconds": 3})
    r = await h.call("cancel_job", {"job_id": job["job_id"]})
    assert r.is_error
    assert "JOB_ALREADY_FINISHED" in text(r) and "completed" in text(r)


# --- isolation, secrets, limits ------------------------------------------------------------------


async def test_unknown_job_id_tells_agent_not_to_invent_ids(h):
    r = await h.call("get_job_status", {"job_id": "job_made_up"})
    assert r.is_error and "JOB_NOT_FOUND" in text(r) and "Do not invent" in text(r)


async def test_tenant_cannot_see_or_cancel_another_tenants_job():
    async with harness() as a:
        job = (await a.call("create_poster_job", VALID_JOB)).structured_content
        app = a.app
    # Same backend, different API key (tenant_b).
    async with harness(api_key=OTHER_KEY, transport=httpx.ASGITransport(app=app)) as b:
        for tool in ("get_job_status", "cancel_job"):
            r = await b.call(tool, {"job_id": job["job_id"]})
            assert r.is_error and "JOB_NOT_FOUND" in text(r)
    assert app.state.jobs[job["job_id"]].cancelled_at is None


async def test_api_key_never_appears_in_tool_output_or_logs(caplog):
    caplog.handler.addFilter(RedactingFilter([API_KEY]))
    caplog.set_level(logging.DEBUG)
    logging.getLogger("poster_mcp").info("debug dump: Authorization: Bearer %s", API_KEY)

    async with harness() as h:
        outputs = [
            await h.call("list_templates", {}),
            await h.call("create_poster_job", {**VALID_JOB, "prompt": "y" * 900}),
            await h.call("create_poster_job", VALID_JOB),
            await h.call("get_job_status", {"job_id": "nope"}),
        ]
    # Wrong key: auth error must not echo any key either.
    async with harness(api_key="sk-wrong-key-55555") as bad:
        r = await bad.call("list_templates", {})
        assert r.is_error and "AUTH_FAILED" in text(r) and "sk-wrong-key" not in text(r)

    blob = "\n".join(text(o) for o in outputs) + caplog.text
    assert API_KEY not in blob
    assert "***REDACTED***" in caplog.text


async def test_hourly_image_budget_blocks_runaway_generation():
    async with harness(max_images_per_hour=5) as h:
        r1 = await h.call("create_poster_job", {**VALID_JOB, "image_count": 4, "idempotency_key": "a"})
        r2 = await h.call("create_poster_job", {**VALID_JOB, "image_count": 2, "idempotency_key": "b"})
        # A replay of an existing job costs nothing and is still allowed.
        r3 = await h.call("create_poster_job", {**VALID_JOB, "image_count": 4, "idempotency_key": "a"})
    assert not r1.is_error
    assert r2.is_error and "BUDGET_EXCEEDED" in text(r2)
    assert not r3.is_error and r3.structured_content["idempotent_replay"] is True


async def test_too_many_concurrent_jobs_is_rate_limited():
    async with harness(fast_config(min_seconds=30, max_seconds=30, max_active_jobs_per_tenant=2)) as h:
        for i in range(2):
            assert not (await h.call("create_poster_job", {**VALID_JOB, "idempotency_key": f"k{i}"})).is_error
        r = await h.call("create_poster_job", {**VALID_JOB, "idempotency_key": "k3"})
    assert r.is_error and "RATE_LIMITED" in text(r) and "wait" in text(r)


def test_missing_api_key_fails_fast_with_a_clear_message(monkeypatch):
    monkeypatch.delenv("POSTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="POSTER_API_KEY is not set"):
        Settings.from_env()
