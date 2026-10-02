"""SnowLuma OneBot action 的 HTTP 路由与并发。"""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx
from nonebot.adapters.onebot.v11.exception import ActionFailed, NetworkError
from nonebot.config import Config, Env
from nonebot.drivers import Response
from nonebot.drivers.httpx import Driver

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.snowluma_adapter import SnowLumaAdapter, SnowLumaOutcomeUnknown


def _adapter(monkeypatch, request, *, roots=None):
    driver = Driver(Env(), Config(driver="~httpx"))
    monkeypatch.setattr(type(driver), "request", request)
    adapter = object.__new__(SnowLumaAdapter)
    adapter.driver = driver
    adapter.onebot_config = SimpleNamespace(
        onebot_api_roots=(
            {"*": "http://127.0.0.1:3000/"}
            if roots is None else roots
        ),
        onebot_access_token="test-token",
    )
    return adapter


async def test_actions_use_snowluma_http_with_wildcard_root(monkeypatch):
    requests = []

    async def request(_driver, setup):
        requests.append(setup)
        return Response(
            200,
            content=json.dumps({
                "status": "ok", "retcode": 0,
                "data": {"message_id": 17},
            }),
            request=setup,
        )

    adapter = _adapter(monkeypatch, request)
    result = await adapter._call_api(
        SimpleNamespace(self_id="12345"),
        "send_group_msg",
        group_id=67890,
        message="hello",
    )

    assert result == {"message_id": 17}
    assert len(requests) == 1
    sent = requests[0]
    assert str(sent.url) == "http://127.0.0.1:3000/send_group_msg"
    assert sent.headers["Authorization"] == "Bearer test-token"
    assert json.loads(sent.content) == {
        "group_id": 67890, "message": "hello"}


async def test_snowluma_http_actions_can_wait_for_responses_concurrently(
        monkeypatch):
    both_started = asyncio.Event()
    release = asyncio.Event()
    active = 0
    peak = 0

    async def request(_driver, setup):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            both_started.set()
        await release.wait()
        active -= 1
        body = json.loads(setup.content)
        return Response(
            200,
            content=json.dumps({
                "status": "ok", "retcode": 0,
                "data": {"group_id": body["group_id"]},
            }),
            request=setup,
        )

    adapter = _adapter(monkeypatch, request)
    bot = SimpleNamespace(self_id="12345")
    calls = [
        asyncio.create_task(adapter._call_api(
            bot, "send_group_msg", group_id=1, message="first")),
        asyncio.create_task(adapter._call_api(
            bot, "send_group_msg", group_id=2, message="second")),
    ]
    await asyncio.wait_for(both_started.wait(), 2)
    release.set()

    assert await asyncio.gather(*calls) == [
        {"group_id": 1}, {"group_id": 2}]
    assert peak == 2


async def test_missing_snowluma_http_root_never_falls_back_to_websocket(
        monkeypatch):
    async def request(_driver, _setup):  # pragma: no cover
        raise AssertionError("不应发起 HTTP 请求")

    adapter = _adapter(monkeypatch, request, roots={})

    with pytest.raises(NetworkError, match="ONEBOT_API_ROOTS"):
        await adapter._call_api(
            SimpleNamespace(self_id="12345"), "get_status")


async def test_connect_failure_is_safe_to_retry(monkeypatch):
    async def request(_driver, _setup):
        raise httpx.ConnectError(
            "refused", request=httpx.Request("POST", "http://127.0.0.1"))

    adapter = _adapter(monkeypatch, request)

    with pytest.raises(NetworkError) as raised:
        await adapter._call_api(
            SimpleNamespace(self_id="12345"), "send_group_msg")
    assert not isinstance(raised.value, SnowLumaOutcomeUnknown)


async def test_response_timeout_is_not_safe_to_retry(monkeypatch):
    async def request(_driver, _setup):
        raise httpx.ReadTimeout(
            "late", request=httpx.Request("POST", "http://127.0.0.1"))

    adapter = _adapter(monkeypatch, request)

    with pytest.raises(SnowLumaOutcomeUnknown):
        await adapter._call_api(
            SimpleNamespace(self_id="12345"), "send_group_msg")


@pytest.mark.parametrize(
    ("status_code", "content"),
    [
        (503, b"temporarily unavailable"),
        (200, b"{broken"),
        (200, b""),
    ],
)
async def test_http_or_response_failure_has_unknown_delivery_outcome(
        monkeypatch, status_code, content):
    async def request(_driver, setup):
        return Response(
            status_code, content=content, request=setup)

    adapter = _adapter(monkeypatch, request)

    with pytest.raises(SnowLumaOutcomeUnknown):
        await adapter._call_api(
            SimpleNamespace(self_id="12345"), "send_group_msg")


async def test_onebot_business_failure_remains_action_failed(monkeypatch):
    async def request(_driver, setup):
        return Response(
            200,
            content=json.dumps({
                "status": "failed",
                "retcode": 100,
                "data": None,
                "message": "rejected",
            }),
            request=setup,
        )

    adapter = _adapter(monkeypatch, request)

    with pytest.raises(ActionFailed) as raised:
        await adapter._call_api(
            SimpleNamespace(self_id="12345"), "send_group_msg")
    assert not isinstance(raised.value, SnowLumaOutcomeUnknown)
