"""Discord 适配器的 429 响应保留与传播。"""

import json
import sys
from pathlib import Path

import pytest
from nonebot.adapters.discord import Adapter as BaseDiscordAdapter
from nonebot.drivers import Request, Response

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.discord_adapter import DiscordAdapter, DiscordRateLimitError


def test_rate_limit_error_prefers_retry_after_header():
    error = DiscordRateLimitError(Response(
        429,
        headers={"Retry-After": "1.25"},
        content=json.dumps({"retry_after": 9.0, "global": True}),
    ))

    assert error.retry_after == 1.25
    assert error.global_rate_limit is True


def test_rate_limit_error_uses_response_body_fallback():
    error = DiscordRateLimitError(Response(
        429, content=json.dumps({"retry_after": 2.5, "global": False})))

    assert error.retry_after == 2.5
    assert error.global_rate_limit is False


async def test_adapter_raises_enriched_rate_limit_before_generated_api(
        monkeypatch):
    api_request = Request(
        "POST", "https://discord.com/api/v10/users/@me/channels")
    response = Response(
        429,
        headers={"Retry-After": "3.5"},
        content=json.dumps({"retry_after": 3.5}),
        request=api_request,
    )

    async def request(_self, _setup):
        return response

    monkeypatch.setattr(BaseDiscordAdapter, "request", request)
    adapter = object.__new__(DiscordAdapter)

    with pytest.raises(DiscordRateLimitError) as raised:
        await adapter.request(api_request)
    assert raised.value.retry_after == 3.5
    assert raised.value.route_key == "POST /api/v10/users/@me/channels"
