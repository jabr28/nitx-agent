"""The mock backend itself honours the brief: 5-20s jobs, ~10% random failures, input rejection."""

from __future__ import annotations

from fastapi.testclient import TestClient

from poster_mcp.backend import MockConfig, create_app

KEY = {"Authorization": "Bearer k"}
BODY = {
    "product_image": "https://a/p.png",
    "background_image": "https://a/b.png",
    "prompt": "latte",
}


class FakeClock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def make(**overrides):
    clock = FakeClock()
    cfg = MockConfig(seed=1, max_active_jobs_per_tenant=10_000, api_keys={"k": "t"})
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return TestClient(create_app(cfg, clock=clock)), clock


def test_default_jobs_take_5_to_20_seconds():
    client, clock = make(fail_rate=0)
    ids = [client.post("/v1/poster-jobs", json=BODY, headers=KEY).json()["job_id"] for _ in range(50)]

    clock.now += 4.99
    assert all(client.get(f"/v1/poster-jobs/{i}", headers=KEY).json()["status"] == "processing" for i in ids)
    clock.now += 15.02  # 20.01s after creation
    assert all(client.get(f"/v1/poster-jobs/{i}", headers=KEY).json()["status"] == "completed" for i in ids)


def test_about_one_in_ten_jobs_fails():
    client, clock = make(min_seconds=0, max_seconds=0)
    ids = [client.post("/v1/poster-jobs", json=BODY, headers=KEY).json()["job_id"] for _ in range(1000)]
    clock.now += 1
    failed = sum(client.get(f"/v1/poster-jobs/{i}", headers=KEY).json()["status"] == "failed" for i in ids)
    assert 70 <= failed <= 130, failed


def test_invalid_inputs_are_rejected_with_codes():
    client, _ = make()
    cases = [
        ({**BODY, "prompt": "x" * 501}, "PROMPT_TOO_LONG"),
        ({**BODY, "image_count": 5}, "INVALID_IMAGE_COUNT"),
        ({**BODY, "image_count": 0}, "INVALID_IMAGE_COUNT"),
        ({**BODY, "resolution": "4k"}, "INVALID_RESOLUTION"),
        ({**BODY, "product_image": "file:///etc/passwd"}, "INVALID_FIELD"),
    ]
    for body, code in cases:
        r = client.post("/v1/poster-jobs", json=body, headers=KEY)
        assert r.status_code == 422 and r.json()["error"]["code"] == code, (body, r.json())


def test_requests_without_a_valid_key_are_refused():
    client, _ = make()
    assert client.get("/v1/templates").status_code == 401
    assert client.get("/v1/templates", headers={"Authorization": "Bearer nope"}).status_code == 401
