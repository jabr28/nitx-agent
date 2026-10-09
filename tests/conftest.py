from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from mcp import Client

from poster_mcp.backend import MockConfig, create_app
from poster_mcp.client import PosterServiceClient, Settings
from poster_mcp.server import build_server

API_KEY = "sk-test-tenant-a-0123456789"
OTHER_KEY = "sk-test-tenant-b-9876543210"

VALID_JOB = {
    "product_image": "https://assets.example.com/latte.png",
    "background_image": "https://assets.example.com/cafe.jpg",
    "prompt": "A latte on a wooden table, headline: عرض نهاية الأسبوع",
}


@dataclass
class Harness:
    mcp: Client
    app: Any  # the mock backend, to inspect stored jobs

    async def call(self, tool: str, args: dict[str, Any]):
        return await self.mcp.call_tool(tool, args)


def fast_config(**overrides: Any) -> MockConfig:
    cfg = MockConfig(
        min_seconds=0.3,
        max_seconds=0.3,
        fail_rate=0.0,
        seed=7,
        api_keys={API_KEY: "tenant_a", OTHER_KEY: "tenant_b"},
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


@asynccontextmanager
async def harness(
    config: MockConfig | None = None,
    *,
    api_key: str = API_KEY,
    transport: httpx.AsyncBaseTransport | None = None,
    max_images_per_hour: int = 40,
):
    app = create_app(config or fast_config())
    settings = Settings(base_url="http://mock", api_key=api_key, timeout_seconds=1, max_retries=2)
    service = PosterServiceClient(settings, transport=transport or httpx.ASGITransport(app=app))
    server = build_server(service, max_images_per_hour=max_images_per_hour, poll_interval_seconds=0.05)
    async with Client(server) as mcp:
        yield Harness(mcp=mcp, app=app)
    await service.aclose()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def h(anyio_backend):
    async with harness() as hh:
        yield hh


def text(result) -> str:
    return result.content[0].text
