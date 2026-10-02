import sys
import time
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.plugins.riven_sniper import wfm as wfm_mod  # noqa: E402
from src.plugins.riven_sniper.wfm import HEADERS, WfmClient  # noqa: E402


async def test_crossplay_is_always_enabled():
    assert HEADERS["Crossplay"] == "true"

    client = WfmClient()
    try:
        assert client._client.headers["Crossplay"] == "true"
    finally:
        await client.close()


class _GateSpy:
    def __init__(self):
        self.waits = 0
        self.deferrals = []

    async def wait(self):
        self.waits += 1

    def defer(self, seconds):
        self.deferrals.append(seconds)


async def test_riven_search_uses_public_and_contract_rate_gates(monkeypatch):
    calls = []

    async def contract_wait():
        calls.append("contract")

    async def public_wait():
        calls.append("public")

    monkeypatch.setattr(wfm_mod, "wait_for_contract_request", contract_wait)
    monkeypatch.setattr(wfm_mod, "wait_for_public_request", public_wait)
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, request=request, json={"payload": {"auctions": []}}))
    client = WfmClient()
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        transport=transport, headers=HEADERS)
    try:
        assert await client.riven_search("torid") == []
        assert calls == ["contract"]
    finally:
        await client.close()


async def test_riven_429_defers_both_rate_gates(monkeypatch):
    public = _GateSpy()
    contract = _GateSpy()
    monkeypatch.setattr(wfm_mod, "_PUBLIC_REQUEST_GATE", public)
    monkeypatch.setattr(wfm_mod, "_CONTRACT_SEARCH_GATE", contract)
    monkeypatch.setattr(wfm_mod, "wait_for_contract_request", _GateSpy().wait)
    transport = httpx.MockTransport(lambda request: httpx.Response(
        429, request=request, headers={"Retry-After": "17"}))
    client = WfmClient()
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        transport=transport, headers=HEADERS)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.riven_search("torid")
        assert contract.deferrals == [17.0]
        assert public.deferrals == [17.0]
    finally:
        await client.close()


async def test_contract_interval_starts_after_public_backoff(monkeypatch):
    public = wfm_mod._RateGate(0.001)
    contract = wfm_mod._RateGate(0.03)
    monkeypatch.setattr(wfm_mod, "_PUBLIC_REQUEST_GATE", public)
    monkeypatch.setattr(wfm_mod, "_CONTRACT_SEARCH_GATE", contract)
    public.defer(0.04)
    sent_at = []

    def response(request):
        sent_at.append(time.perf_counter())
        return httpx.Response(
            200, request=request, json={"payload": {"auctions": []}})

    client = WfmClient()
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(response), headers=HEADERS)
    try:
        await client.riven_search("torid")
        await client.riven_search("latron")
        assert sent_at[1] - sent_at[0] >= 0.025
    finally:
        await client.close()


async def test_world_state_retries_one_transport_disconnect(monkeypatch):
    calls = []

    async def get(_url, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise httpx.RemoteProtocolError(
                "Server disconnected without sending a response.")
        return httpx.Response(
            200, request=httpx.Request("GET", wfm_mod.WORLD_STATE_URL),
            json={"VoidTraders": []})

    client = WfmClient()
    monkeypatch.setattr(client._client, "get", get)
    try:
        assert await client.world_state() == {"VoidTraders": []}
        assert len(calls) == 2
        assert all(call["headers"] == {"Connection": "close"}
                   for call in calls)
    finally:
        await client.close()


async def test_world_state_does_not_retry_http_status(monkeypatch):
    calls = 0

    async def get(_url, **_kwargs):
        nonlocal calls
        calls += 1
        return httpx.Response(
            503, request=httpx.Request("GET", wfm_mod.WORLD_STATE_URL))

    client = WfmClient()
    monkeypatch.setattr(client._client, "get", get)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.world_state()
        assert calls == 1
    finally:
        await client.close()


async def test_world_state_raises_after_second_transport_disconnect(monkeypatch):
    calls = 0

    async def get(_url, **_kwargs):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError(
            "connection failed",
            request=httpx.Request("GET", wfm_mod.WORLD_STATE_URL))

    client = WfmClient()
    monkeypatch.setattr(client._client, "get", get)
    try:
        with pytest.raises(httpx.ConnectError):
            await client.world_state()
        assert calls == 2
    finally:
        await client.close()
