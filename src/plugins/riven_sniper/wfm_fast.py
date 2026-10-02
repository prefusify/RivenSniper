"""Windows 端快速紫卡搜索：明确三正查询、共享出口池、订阅基线。

VPS 只转发 TCP。此模块不使用普通 WFM 客户端的单出口请求门，也不写普通
轮询的 seen_auctions；每个代理出口分别计时，查询键可以使用任意可用出口。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import ssl
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import httpx
from nonebot import logger

from .criteria import ANY_ATTRIBUTE, normalized_config
from .wfm import BASE_V1, HEADERS, _retry_after_seconds
from .wm_proxy import TunnelGroup, proxy_routes


@dataclass(frozen=True, order=True)
class QueryKey:
    weapon: str
    positives: tuple[str, str, str]

    def params(self) -> dict[str, str]:
        return {"type": "riven", "weapon_url_name": self.weapon,
                "positive_stats": ",".join(self.positives), "sort_by": "price_asc"}


@lru_cache(maxsize=2048)
def _expand(weapon: str, groups: tuple[tuple[str, ...], ...]) -> tuple[QueryKey, ...]:
    return tuple(sorted({
        QueryKey(weapon, tuple(sorted(values)))
        for values in itertools.product(*groups) if len(set(values)) == 3
    }))


def query_keys(config: dict) -> tuple[QueryKey, ...]:
    """仅具体武器 + 三个完全明确的正词条位置；OR 按一对一语义展开。"""
    cfg = normalized_config(config)
    groups = tuple(tuple(group) for group in cfg["positives"])
    if (not config.get("enabled", True) or not cfg["weapon"] or cfg["wildcard"]
            or len(groups) != 3
            or any(not group or ANY_ATTRIBUTE in group for group in groups)):
        return ()
    return _expand(str(cfg["weapon"]), groups)


def subscription_token(config: dict) -> tuple:
    return (config["group_id"], config["id"], config.get("wm_fast_generation", 0),
            json.dumps(normalized_config(config), sort_keys=True))


def created_timestamp(auction: dict) -> float | None:
    try:
        value = datetime.fromisoformat(str(auction["created"]).replace("Z", "+00:00"))
        return value.timestamp() if value.tzinfo is not None else None
    except (KeyError, ValueError, TypeError, OverflowError):
        return None


@dataclass
class _Exit:
    url: str = field(repr=False)
    ready_at: float = 0.0
    busy: bool = False
    client: httpx.AsyncClient | None = field(default=None, repr=False)


class ProxyPool:
    """每个出口同一时间最多一个请求；出错退避不会卡住其他出口。"""

    def __init__(self, urls: list[str], *, interval: float = 6.5,
                 client_factory=None):
        if not urls or len(set(urls)) != len(urls):
            raise ValueError("代理池需要至少一个出口，且出口不能重复")
        self.exits = [_Exit(url) for url in urls]
        self.interval = interval
        self._condition = asyncio.Condition()
        self._cursor = 0
        self._factory = client_factory
        self._ssl_context = None
        self._context_lock = asyncio.Lock()
        self._closed = False
        self._blocked_until = 0.0
        self._recent_limits: dict[int, float] = {}
        self.counts: Counter = Counter()
        self.latencies: deque[float] = deque(maxlen=2048)
        self.last_success: float | None = None

    async def _acquire(self) -> tuple[int, _Exit]:
        loop = asyncio.get_running_loop()
        async with self._condition:
            while not self._closed:
                now = loop.time()
                for offset in range(len(self.exits)):
                    index = (self._cursor + offset) % len(self.exits)
                    entry = self.exits[index]
                    if (not entry.busy and entry.ready_at <= now
                            and self._blocked_until <= now):
                        entry.busy = True
                        self._cursor = (index + 1) % len(self.exits)
                        return index, entry
                deadlines = [max(e.ready_at, self._blocked_until)
                             for e in self.exits if not e.busy]
                delay = max(0.01, min(deadlines) - now) if deadlines else 1.0
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=delay)
                except TimeoutError:
                    pass
        raise RuntimeError("代理池已关闭")

    async def search(self, key: QueryKey, *, on_start=None) -> list[dict]:
        # SSL 信任链只加载一次，所有出口复用；避免数百个客户端重复占用内存。
        if self._ssl_context is None and self._factory is None:
            async with self._context_lock:
                if self._ssl_context is None:
                    self._ssl_context = await asyncio.to_thread(ssl.create_default_context)
        for attempt in range(2):
            index, entry = await self._acquire()
            started = asyncio.get_running_loop().time()
            try:
                if entry.client is None:
                    entry.client = (self._factory(entry.url) if self._factory else
                                    httpx.AsyncClient(
                                        proxy=entry.url, headers=HEADERS,
                                        verify=self._ssl_context, trust_env=False,
                                        timeout=10, limits=httpx.Limits(
                                            max_connections=1,
                                            max_keepalive_connections=1,
                                            keepalive_expiry=120)))
                started = asyncio.get_running_loop().time()
                if on_start is not None:
                    on_start(started)
                entry.ready_at = started + self.interval
                self.counts["requests"] += 1
                response = await entry.client.get(
                    f"{BASE_V1}/auctions/search", params=key.params())
                if response.status_code == 429:
                    self.counts["rate_limited"] += 1
                    now = asyncio.get_running_loop().time()
                    entry.ready_at = max(entry.ready_at, now + max(
                        self.interval, _retry_after_seconds(response)))
                    self._recent_limits[index] = now
                    self._recent_limits = {
                        i: at for i, at in self._recent_limits.items() if now - at < 10}
                    # 多个出口同时被拒绝时，不能假定额度彼此独立而持续放大请求。
                    if len(self._recent_limits) >= min(3, len(self.exits)):
                        self._blocked_until = max(self._blocked_until, entry.ready_at)
                    if attempt == 0:
                        continue
                response.raise_for_status()
                data = await asyncio.to_thread(response.json)
                auctions = data["payload"]["auctions"]
                if not isinstance(auctions, list):
                    raise ValueError("WM 搜索结果不是列表")
                self.counts["success"] += 1
                self.counts["downloaded_bytes"] += response.num_bytes_downloaded
                self.last_success = time.time()
                self.latencies.append(asyncio.get_running_loop().time() - started)
                return auctions
            except httpx.HTTPStatusError as error:
                self.counts["errors"] += 1
                if error.response.status_code != 429:
                    entry.ready_at = max(entry.ready_at,
                                         asyncio.get_running_loop().time() + 30)
                raise
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                self.counts["errors"] += 1
                entry.ready_at = max(entry.ready_at,
                                     asyncio.get_running_loop().time() + 10)
                if entry.client is not None:
                    await entry.client.aclose()
                    entry.client = None
                raise
            finally:
                async with self._condition:
                    entry.busy = False
                    self._condition.notify_all()
        raise RuntimeError("搜索未完成")

    def snapshot(self) -> dict:
        now = asyncio.get_running_loop().time()
        values = sorted(self.latencies)
        return {**self.counts, "exits": len(self.exits),
                "busy": sum(e.busy for e in self.exits),
                "cooling": sum(e.ready_at > now for e in self.exits),
                "pool_backoff_seconds": round(max(0, self._blocked_until - now), 1),
                "last_success": self.last_success,
                "response_p95_seconds": values[int((len(values) - 1) * .95)]
                if values else None}

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._condition.notify_all()
        await asyncio.gather(*(e.client.aclose() for e in self.exits if e.client))


@dataclass
class Subscription:
    config: dict
    since: float
    baselined: bool = False


@dataclass
class QueryState:
    key: QueryKey
    subscriptions: dict[tuple, Subscription] = field(default_factory=dict)
    seen: dict[str, float] = field(default_factory=dict)
    task: asyncio.Task | None = None
    state: str = "baseline"
    count: int = 0
    last_success: float | None = None
    next_at: float = 0.0
    last_started: float | None = None
    intervals: deque[float] = field(default_factory=lambda: deque(maxlen=120))
    last_prune: float = 0.0

    def observe(self, auctions: list[dict], *, now: float) -> list[tuple[list[dict], list[dict]]]:
        """新订阅单独建立基线；已有订阅不因别的目标加入而丢掉新单。"""
        self.count = len(auctions)
        self.last_success = now
        if len(auctions) >= 500:
            self.state = "truncated"
            for subscription in self.subscriptions.values():
                subscription.baselined = False
            return []
        self.state = "running"
        fresh = [a for a in auctions if a["id"] not in self.seen
                 and (a.get("item") or {}).get("type") == "riven"
                 and not a.get("closed") and not a.get("private")
                 and a.get("visible", True)]
        self.seen.update((a["id"], now) for a in auctions)
        if now - self.last_prune > 3600:
            self.seen = {aid: at for aid, at in self.seen.items() if now - at < 30 * 86400}
            self.last_prune = now
        output: dict[tuple[str, ...], tuple[list[dict], list[dict]]] = {}
        for subscription in self.subscriptions.values():
            if subscription.baselined:
                new = [a for a in fresh if (created_timestamp(a) or 0) >= subscription.since]
                if new:
                    ids = tuple(a["id"] for a in new)
                    output.setdefault(ids, (new, []))[1].append(subscription.config)
            subscription.baselined = True
        return list(output.values())


class FastRivenPoller:
    def __init__(self, store, config, on_auctions):
        self.store, self.config, self.on_auctions = store, config, on_auctions
        self.queries: dict[QueryKey, QueryState] = {}
        self.pool: ProxyPool | None = None
        self.tunnel: TunnelGroup | None = None
        self.state = "stopped"
        self._wakeup = asyncio.Event()
        self._retry_config_at = 0.0

    def wakeup(self) -> None:
        interval = float(getattr(self.config, "wm_fast_interval", 2))
        for state in self.queries.values():
            # 设置热更新重排普通查询；错误退避和结果上限的复查时间保持独立。
            if state.state == "running" and state.last_started is not None:
                state.next_at = state.last_started + interval
        self._wakeup.set()

    def sync(self, configs: list[dict], *, now: float) -> None:
        desired: dict[QueryKey, dict[tuple, dict]] = {}
        for config in configs:
            token = subscription_token(config)
            for key in query_keys(config):
                desired.setdefault(key, {})[token] = config
        for key in set(self.queries) - desired.keys():
            state = self.queries[key]
            if state.task is not None:
                state.task.cancel()
            # 尚在取消的任务由 run() 收集，避免异常逃逸。
            state.subscriptions.clear()
        for key, subscriptions in desired.items():
            state = self.queries.setdefault(key, QueryState(key))
            state.subscriptions = {
                token: state.subscriptions.get(token, Subscription(config, now))
                for token, config in subscriptions.items()}

    async def _request(self, state: QueryState) -> None:
        loop = asyncio.get_running_loop()
        started = loop.time()
        def on_start(at):
            nonlocal started
            started = at
            if state.last_started is not None:
                state.intervals.append(at - state.last_started)
            state.last_started = at
        try:
            auctions = await self.pool.search(state.key, on_start=on_start)
            # 主循环同步订阅，统一入队入口再次核对当前规则与开关。
            for fresh, configs in state.observe(auctions, now=time.time()):
                await self.on_auctions(fresh, configs)
            interval = float(getattr(self.config, "wm_fast_interval", 2))
            state.next_at = started + (60 if state.state == "truncated" else interval)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if state.state != "error":
                logger.warning("WM 快速搜索失败: {} {}", state.key.weapon, type(error).__name__)
            state.state = "error"
            interval = float(getattr(self.config, "wm_fast_interval", 2))
            state.next_at = loop.time() + max(2, interval)

    async def _configure(self) -> None:
        path = str(getattr(self.config, "wm_fast_proxy_config", ""))
        if not path:
            self.state = "not_configured"
            return
        try:
            data = await asyncio.to_thread(
                lambda: json.loads(Path(path).read_text(encoding="utf-8")))
        except FileNotFoundError:
            self.state = "not_configured"
            return
        urls, tunnel = proxy_routes(data)
        self.pool = ProxyPool(urls)
        self.tunnel = tunnel
        if self.tunnel:
            self.tunnel.start()

    async def run(self) -> None:
        try:
            while True:
                self.sync(self.store.wm_fast_configs(discord_enabled=getattr(
                    self.config, "discord_dm_enabled", False)), now=time.time())
                for key, state in list(self.queries.items()):
                    if state.task is not None and state.task.done():
                        await asyncio.gather(state.task, return_exceptions=True)
                        state.task = None
                    if not state.subscriptions and state.task is None:
                        self.queries.pop(key)
                active = any(s.subscriptions for s in self.queries.values())
                now = asyncio.get_running_loop().time()
                if active and self.pool is None and now >= self._retry_config_at:
                    try:
                        await self._configure()
                    except Exception as error:
                        self.state = "configuration_error"
                        logger.warning("WM 快速代理配置未就绪: {}", type(error).__name__)
                    self._retry_config_at = now + 10
                ready = self.pool is not None and (self.tunnel is None or self.tunnel.ready)
                if not active:
                    self.state = "idle"
                elif self.pool is not None:
                    self.state = "running" if ready else "connecting"
                if ready:
                    for state in self.queries.values():
                        if state.subscriptions and state.task is None and state.next_at <= now:
                            state.task = asyncio.create_task(self._request(state))
                self._wakeup.clear()
                try:
                    await asyncio.wait_for(self._wakeup.wait(), timeout=.1)
                except TimeoutError:
                    pass
        finally:
            tasks = [s.task for s in self.queries.values() if s.task is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                if self.pool is not None:
                    await self.pool.close()
            finally:
                try:
                    if self.tunnel is not None:
                        await self.tunnel.close()
                finally:
                    self.state = "stopped"

    def snapshot(self, scope_id: int | None = None) -> dict:
        queries = [state for state in self.queries.values() if any(
            scope_id is None or sub.config["group_id"] == scope_id
            for sub in state.subscriptions.values())]
        intervals = sorted(x for s in queries for x in s.intervals)
        return {"state": self.state, "interval": getattr(self.config, "wm_fast_interval", 2),
                "query_count": len(queries), "pool": self.pool.snapshot() if self.pool else None,
                "actual_interval_p95": intervals[int((len(intervals)-1)*.95)] if intervals else None,
                "queries": [{"weapon": s.key.weapon, "positives": s.key.positives,
                             "state": s.state, "count": s.count,
                             "baseline_rules": [sub.config["display_number"]
                                                for sub in s.subscriptions.values()
                                                if not sub.baselined and scope_id is not None
                                                and sub.config["group_id"] == scope_id],
                             "last_success": s.last_success} for s in queries]}
