"""warframe.market API 与官方新订单 WebSocket 客户端。"""

from __future__ import annotations

import asyncio
import json
import uuid
import weakref
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
from websockets.asyncio.client import connect as websocket_connect

from .version import VERSION

BASE_V1 = "https://api.warframe.market/v1"
BASE_V2 = "https://api.warframe.market/v2"
WS_URL = "wss://ws.warframe.market/socket"
WORLD_STATE_URL = "https://api.warframe.com/cdn/worldState.php"
NEW_ORDER_ROUTE = "@wfm|event/subscriptions/newOrder"
HEADERS = {
    "User-Agent": f"rivensniper/{VERSION}",
    "Platform": "pc",
    # 产品约束：所有 WFM 市场查询始终使用跨平台市场，不提供关闭开关。
    "Crossplay": "true",
    "Language": "en",
}


@dataclass
class _GateState:
    lock: asyncio.Lock
    next_at: float = 0.0


class _RateGate:
    """同一进程内按事件循环共享的平滑请求门。

    WFM 的限制按客户端出口计算，因此 SniperPoller 与 BargainPoller 的多个
    ``WfmClient`` 实例必须共用节奏。按事件循环保存状态可避免测试套件在不同
    loop 间复用 ``asyncio.Lock``。
    """

    def __init__(self, interval_seconds: float):
        self.interval = float(interval_seconds)
        self._states: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

    def _state(self) -> tuple[asyncio.AbstractEventLoop, _GateState]:
        loop = asyncio.get_running_loop()
        state = self._states.get(loop)
        if state is None:
            state = _GateState(asyncio.Lock())
            self._states[loop] = state
        return loop, state

    async def wait(self) -> None:
        loop, state = self._state()
        async with state.lock:
            while True:
                delay = state.next_at - loop.time()
                if delay <= 0:
                    break
                await asyncio.sleep(delay)
            state.next_at = loop.time() + self.interval

    def defer(self, seconds: float) -> None:
        loop, state = self._state()
        state.next_at = max(state.next_at, loop.time() + max(0.0, seconds))


# 官方通用上限为 3 req/s；0.35s 留出少量调度余量。拍卖合约搜索使用
# 更保守的 10 req/min，避免小时采样在整点形成突发。
_PUBLIC_REQUEST_GATE = _RateGate(0.35)
_CONTRACT_SEARCH_GATE = _RateGate(6.1)


def _retry_after_seconds(response: httpx.Response) -> float:
    raw = response.headers.get("Retry-After", "")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 30.0


async def wait_for_public_request() -> None:
    """等待 WFM 通用 HTTP 请求槽；供目录刷新等旁路复用。"""
    await _PUBLIC_REQUEST_GATE.wait()


async def wait_for_contract_request() -> None:
    """同时预留通用与合约搜索请求槽。

    两个节奏必须以最终实际放行时刻为起点一起更新。若先预留合约槽、再被
    通用端点的 ``Retry-After`` 阻塞，合约间隔会在等待期间提前耗尽，下一次
    搜索便可能紧跟着发出。
    """
    public_loop, public = _PUBLIC_REQUEST_GATE._state()
    contract_loop, contract = _CONTRACT_SEARCH_GATE._state()
    if public_loop is not contract_loop:
        raise RuntimeError("WFM request gates belong to different event loops")
    async with public.lock:
        async with contract.lock:
            while True:
                delay = max(public.next_at, contract.next_at) - public_loop.time()
                if delay <= 0:
                    break
                await asyncio.sleep(delay)
            now = public_loop.time()
            public.next_at = now + _PUBLIC_REQUEST_GATE.interval
            contract.next_at = now + _CONTRACT_SEARCH_GATE.interval


def observe_public_response(response: httpx.Response) -> None:
    """把通用端点的 429 退避同步给进程内全部 WFM 客户端。"""
    if response.status_code == 429:
        _PUBLIC_REQUEST_GATE.defer(_retry_after_seconds(response))


class WfmClient:
    def __init__(self):
        self._client = self._new_client()

    @staticmethod
    def _new_client() -> httpx.AsyncClient:
        # keepalive 60s > 轮询间隔：复用 TLS 连接，避免长期运行时每轮重新握手
        return httpx.AsyncClient(headers=HEADERS, timeout=20,
                                 limits=httpx.Limits(max_connections=10,
                                                     max_keepalive_connections=5,
                                                     keepalive_expiry=60))

    async def _get(self, url: str, *, contract_search: bool = False,
                   **kwargs) -> httpx.Response:
        if contract_search:
            await wait_for_contract_request()
        else:
            await wait_for_public_request()
        response = await self._client.get(url, **kwargs)
        if response.status_code == 429:
            retry_after = _retry_after_seconds(response)
            _PUBLIC_REQUEST_GATE.defer(retry_after)
            if contract_search:
                _CONTRACT_SEARCH_GATE.defer(retry_after)
        response.raise_for_status()
        return response

    async def recent_auctions(self) -> list[dict]:
        """最新挂出的拍卖（含紫卡与赤毒/信条武器，调用方自行过滤 type）。"""
        r = await self._get(f"{BASE_V1}/auctions")
        return r.json()["payload"]["auctions"]

    async def item_statistics(self, slug: str) -> list[dict]:
        """返回道具已完成交易统计中的 90 天日桶。"""
        r = await self._get(f"{BASE_V1}/items/{slug}/statistics")
        payload = r.json().get("payload") or {}
        closed = payload.get("statistics_closed") or {}
        rows = closed.get("90days") or []
        return rows if isinstance(rows, list) else []

    async def riven_search(self, weapon_slug: str) -> list[dict]:
        """按武器搜紫卡拍卖（v1，直购、价格升序），武器地板价数据源。"""
        r = await self._get(
            f"{BASE_V1}/auctions/search",
            params={"type": "riven", "weapon_url_name": weapon_slug,
                    "buyout_policy": "direct", "sort_by": "price_asc"},
            contract_search=True)
        return r.json()["payload"]["auctions"]

    async def new_order_events(self) -> AsyncIterator[dict]:
        """订阅并逐条产出全跨平台 PC 市场的新建订单。

        连接中断会直接结束或抛出异常，由调用方决定重连策略。这里刻意不调用
        HTTP recent-orders 接口补洞，以免把历史订单误当作新订单。
        """
        request_id = str(uuid.uuid4())
        subscription = {
            "route": "@wfm|cmd/subscribe/newOrders",
            "id": request_id,
            "payload": {"platform": "pc", "crossplay": True},
        }
        async with websocket_connect(
            WS_URL,
            subprotocols=["wfm"],
            user_agent_header=HEADERS["User-Agent"],
            ping_interval=20,
            ping_timeout=20,
            close_timeout=10,
        ) as socket:
            await socket.send(json.dumps(subscription, separators=(",", ":")))
            async for raw in socket:
                message = json.loads(raw)
                route = message.get("route")
                if route == NEW_ORDER_ROUTE:
                    payload = message.get("payload")
                    if isinstance(payload, dict):
                        yield payload
                elif message.get("id") == request_id and route and route.endswith(":error"):
                    raise RuntimeError(
                        f"WFM newOrders subscription failed: {message.get('payload')!r}")

    async def world_state(self) -> dict:
        """读取 Digital Extremes 官方 PC World State 原始数据。"""
        # 动态调度可能让两次 World State 请求相隔数小时，远端通常会先
        # 关闭这条空闲连接。禁止该请求进入长连接池，并只对“尚未收到响应”
        # 的传输断开立即重试一次；HTTP 错误和无效 JSON 仍原样上抛。
        async def request() -> dict:
            response = await self._client.get(
                WORLD_STATE_URL, headers={"Connection": "close"})
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {}

        try:
            return await request()
        except httpx.TransportError:
            await asyncio.sleep(0)
            return await request()

    async def reset(self):
        """长跑自愈：连续失败后重建客户端，丢掉可能已损坏的连接池状态。"""
        old, self._client = self._client, self._new_client()
        try:
            await old.aclose()
        except Exception:
            pass

    async def close(self):
        await self._client.aclose()


def auction_url(auction_id: str) -> str:
    return f"https://warframe.market/auction/{auction_id}"
