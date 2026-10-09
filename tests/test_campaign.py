"""Campaign tools + mock platform: the safety rules hold no matter what the agent asks for."""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from mcp import Client

from campaign_mcp.platform import create_platform_app
from campaign_mcp.server import CampaignClient, build_server as build_campaign
from conftest import API_KEY, OTHER_KEY, VALID_JOB, fast_config, text
from poster_mcp.client import PosterServiceClient, Settings
from poster_mcp.server import build_server as build_poster

pytestmark = pytest.mark.anyio

ADMIN = {"Authorization": "Bearer admin-secret"}


@asynccontextmanager
async def platform(api_key: str = API_KEY, app=None):
    app = app or create_platform_app(
        fast_config(api_keys={API_KEY: "demo", OTHER_KEY: "rival"}), approver_token="approver-secret", admin_token="admin-secret")
    transport = httpx.ASGITransport(app=app)
    settings = Settings(base_url="http://mock", api_key=api_key, timeout_seconds=2, max_retries=1)
    posters = build_poster(PosterServiceClient(settings, transport=transport), poll_interval_seconds=0.05)
    campaign = build_campaign(CampaignClient(settings, transport=transport))
    raw = httpx.AsyncClient(transport=transport, base_url="http://mock")
    async with Client(posters) as p, Client(campaign) as c:
        yield p, c, raw, app
    await raw.aclose()


async def new_campaign(raw: httpx.AsyncClient, key: str = API_KEY) -> str:
    r = await raw.post("/v1/campaigns", json={"name": "Weekend sale"}, headers={"Authorization": f"Bearer {key}"})
    return r.json()["campaign_id"]


async def make_poster(p: Client) -> str:
    job = (await p.call_tool("create_poster_job", VALID_JOB)).structured_content
    done = (await p.call_tool("get_job_status", {"job_id": job["job_id"], "wait_seconds": 3})).structured_content
    assert done["status"] == "completed"
    return done["images"][0]["url"]


def next_thursday() -> datetime:
    today = datetime.now(ZoneInfo("Asia/Riyadh"))
    return today + timedelta(days=(3 - today.weekday()) % 7 or 7)


async def build_full_campaign(p: Client, c: Client, cid: str) -> dict:
    url = await make_poster(p)
    thu = next_thursday()
    content = (await c.call_tool("upload_screen_content", {"campaign_id": cid, "image_url": url, "name": "AR 1"}))
    seq = await c.call_tool("create_screen_sequence", {"campaign_id": cid, "content_ids": [content.structured_content["id"]]})
    sched = await c.call_tool("create_screen_schedule", {
        "campaign_id": cid, "sequence_id": seq.structured_content["id"],
        "screen_ids": ["scr_riy_olaya_1", "scr_jed_tahlia_1"],
        "start_date": thu.date().isoformat(), "end_date": (thu + timedelta(days=2)).date().isoformat(),
        "days_of_week": ["thu", "fri", "sat"], "start_time": "10:00", "end_time": "16:00",
    })
    assert not sched.is_error, text(sched)
    seg = await c.call_tool("build_audience_segment", {"campaign_id": cid, "name": "Loyalty", "loyalty_members_only": True})
    design = await c.call_tool("design_email", {
        "campaign_id": cid, "subject": "عرض نهاية الأسبوع", "body": "خصم ٢٠٪ من الخميس إلى السبت",
        "hero_image_url": url,
    })
    email = await c.call_tool("schedule_email_campaign", {
        "campaign_id": cid, "design_id": design.structured_content["id"], "segment_id": seg.structured_content["id"],
        "send_at": thu.strftime("%Y-%m-%dT09:00"),
    })
    assert not email.is_error, text(email)
    return {"segment": seg.structured_content, "email": email.structured_content, "schedule": sched.structured_content}


async def test_agent_has_no_tool_that_publishes_or_sends():
    async with platform() as (_, c, _, _):
        names = {t.name for t in (await c.list_tools()).tools}
    assert not {n for n in names if re.search(r"publish|send_|approve$|delete|contacts", n)}
    assert "submit_for_approval" in names


async def test_full_flow_stays_draft_until_a_person_approves():
    async with platform() as (p, c, raw, _):
        cid = await new_campaign(raw)
        made = await build_full_campaign(p, c, cid)
        assert made["schedule"]["status"] == "draft" and made["email"]["status"] == "draft"

        sub = (await c.call_tool("submit_for_approval", {"campaign_id": cid, "summary": "Weekend sale"})).structured_content
        assert sub["status"] == "pending" and sub["approval_url"].endswith(sub["approval_id"])
        state = (await c.call_tool("get_campaign", {"campaign_id": cid})).structured_content
        assert state["status"] == "pending_approval"
        assert {s["status"] for s in state["screen_schedules"] + state["email_campaigns"]} == {"pending_approval"}

        # The agent's API key cannot approve; a wrong approver token is refused.
        r = await raw.post(f"/approve/{sub['approval_id']}", data={"decision": "approve", "approver_token": API_KEY})
        assert r.status_code == 403
        r = await raw.post(f"/approve/{sub['approval_id']}", data={"decision": "approve", "approver_token": "approver-secret"})
        assert r.status_code == 200
        state = (await c.call_tool("get_campaign", {"campaign_id": cid})).structured_content
        assert state["status"] == "scheduled"


async def test_segments_return_counts_never_contacts():
    async with platform() as (_, c, raw, _):
        cid = await new_campaign(raw)
        seg = (await c.call_tool("build_audience_segment", {"campaign_id": cid, "name": "All"})).structured_content
    assert seg["size"] > 0
    assert "@" not in str(seg) and "contacts" not in seg
    tools = {t.name: t for t in (await _list_campaign_tools())}
    assert "recipients" not in str(tools["schedule_email_campaign"].input_schema)


async def _list_campaign_tools():
    async with platform() as (_, c, _, _):
        return (await c.list_tools()).tools


async def test_only_this_tenants_generated_posters_can_go_on_screens():
    async with platform() as (p, c, raw, app):
        cid = await new_campaign(raw)
        r = await c.call_tool("upload_screen_content",
                              {"campaign_id": cid, "image_url": "https://evil.example/x.png", "name": "x"})
        assert r.is_error and "IMAGE_NOT_ALLOWED" in text(r) and "get_job_status" in text(r)
        url = await make_poster(p)
    # tenant_b cannot use tenant_a's poster or campaign.
    async with platform(api_key=OTHER_KEY, app=app) as (_, c2, raw2, _):
        cid_b = await new_campaign(raw2, OTHER_KEY)
        r = await c2.call_tool("upload_screen_content", {"campaign_id": cid_b, "image_url": url, "name": "stolen"})
        assert r.is_error and "IMAGE_NOT_ALLOWED" in text(r)
        r = await c2.call_tool("get_campaign", {"campaign_id": cid})
        assert r.is_error and "CAMPAIGN_NOT_FOUND" in text(r)


async def test_scheduler_outage_is_reported_as_not_confirmed():
    async with platform() as (p, c, raw, app):
        cid = await new_campaign(raw)
        await raw.post("/admin/faults", json={"tenant": "demo", "schedule_fail_next": 1}, headers=ADMIN)
        url = await make_poster(p)
        content = (await c.call_tool("upload_screen_content", {"campaign_id": cid, "image_url": url, "name": "a"}))
        seq = await c.call_tool("create_screen_sequence", {"campaign_id": cid, "content_ids": [content.structured_content["id"]]})
        thu = next_thursday().date().isoformat()
        r = await c.call_tool("create_screen_schedule", {
            "campaign_id": cid, "sequence_id": seq.structured_content["id"], "screen_ids": ["scr_riy_olaya_1"],
            "start_date": thu, "end_date": thu, "days_of_week": ["thu"], "start_time": "10:00", "end_time": "16:00",
        })
        assert r.is_error and "NOT confirmed" in text(r) and "Never say it succeeded" in text(r)
        state = (await c.call_tool("get_campaign", {"campaign_id": cid})).structured_content
        assert state["screen_schedules"] == []


async def test_injected_instructions_in_a_brand_document_are_flagged():
    async with platform() as (_, c, _, _):
        clean = (await c.call_tool("read_brand_document", {"doc_id": "doc_brand_guide"})).structured_content
        bad = (await c.call_tool("read_brand_document", {"doc_id": "doc_brand_guide_2026"})).structured_content
    assert clean["warning"] is None
    assert "attacker@example.com" in bad["warning"] and "Ignore it" in bad["warning"]


async def test_campaign_is_locked_after_submission():
    async with platform() as (p, c, raw, _):
        cid = await new_campaign(raw)
        await build_full_campaign(p, c, cid)
        await c.call_tool("submit_for_approval", {"campaign_id": cid, "summary": "s"})
        r = await c.call_tool("build_audience_segment", {"campaign_id": cid, "name": "late change"})
    assert r.is_error and "CAMPAIGN_LOCKED" in text(r)


async def test_dates_in_the_past_are_rejected_with_a_hint():
    async with platform() as (p, c, raw, _):
        cid = await new_campaign(raw)
        url = await make_poster(p)
        d = (await c.call_tool("design_email", {"campaign_id": cid, "subject": "s", "body": "b", "hero_image_url": url}))
        seg = await c.call_tool("build_audience_segment", {"campaign_id": cid, "name": "All"})
        r = await c.call_tool("schedule_email_campaign", {
            "campaign_id": cid, "design_id": d.structured_content["id"], "segment_id": seg.structured_content["id"],
            "send_at": "2020-01-02T09:00",
        })
    assert r.is_error and "DATE_IN_PAST" in text(r) and "today's date" in text(r)



async def test_read_tools_with_filters_work():
    async with platform() as (_, c, _, _):
        screens = (await c.call_tool("list_screens", {"city": "Jeddah", "location_type": "drive_thru"}))
        media = (await c.call_tool("list_media_assets", {"type": "background"}))
        docs = (await c.call_tool("list_brand_documents", {}))
    assert not screens.is_error, text(screens)
    assert [s["id"] for s in screens.structured_content["screens"]] == ["scr_jed_tahlia_dt"]
    assert media.structured_content["assets"] and all(a["type"] == "background" for a in media.structured_content["assets"])
    assert len(docs.structured_content["documents"]) == 2


async def test_email_after_the_screen_run_needs_user_confirmation():
    async with platform() as (p, c, raw, _):
        cid = await new_campaign(raw)
        made = await build_full_campaign(p, c, cid)
        # Move the email to the Sunday after the Thu-Sat screen run.
        sunday = (next_thursday() + timedelta(days=3)).strftime("%Y-%m-%dT09:00")
        email2 = await c.call_tool("schedule_email_campaign", {
            "campaign_id": cid, "design_id": made["email"]["design_id"], "segment_id": made["segment"]["id"],
            "send_at": sunday,
        })
        assert not email2.is_error
        r = await c.call_tool("submit_for_approval", {"campaign_id": cid, "summary": "s"})
        assert r.is_error and "NEEDS_USER_CONFIRMATION" in text(r) and "after the screen run ends" in text(r)
        state = (await c.call_tool("get_campaign", {"campaign_id": cid})).structured_content
        assert state["approval"] is None
        ok = await c.call_tool("submit_for_approval", {"campaign_id": cid, "summary": "s", "user_confirmed": True})
        assert not ok.is_error and ok.structured_content["warnings"]
