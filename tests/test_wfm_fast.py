"""快速搜索的查询边界、订阅基线、共享出口与普通轮询共存契约。"""

import asyncio
from datetime import datetime, timezone
import json
import sys
import threading
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.plugins.riven_sniper.config import Config
from src.plugins.riven_sniper.criteria import ANY_ATTRIBUTE
from src.plugins.riven_sniper.poller import SniperPoller, _HitBatch
from src.plugins.riven_sniper.store import Store
from src.plugins.riven_sniper.wfm_fast import (
    FastRivenPoller, ProxyPool, QueryKey, query_keys,
)
from src.plugins.riven_sniper.wm_proxy import SshTunnel, proxy_routes

CC, CD, MS = "critical_chance", "critical_damage", "multishot"
KEY = QueryKey("torid", (CC, CD, MS))


def rule(scope=101, cid=1, **changes):
    return {"id": cid, "display_number": cid, "group_id": scope,
            "weapon": "torid", "wildcard": None,
            "positives": [[CC], [CD], [MS]], "negatives": [["zoom"]],
            **changes}


def auction(aid="a", created=101):
    return {"id": aid, "created": datetime.fromtimestamp(created, timezone.utc).isoformat(),
            "visible": True, "private": False, "closed": False,
            "owner": {"ingame_name": "Seller"}, "starting_price": 100,
            "buyout_price": 100, "is_direct_sell": True,
            "item": {"type": "riven", "weapon_url_name": "torid", "name": "test",
                     "mod_rank": 8, "re_rolls": 0, "mastery_level": 8, "polarity": "madurai",
                     "attributes": [
                         {"url_name": s, "positive": True, "value": 100}
                         for s in (CC, CD, MS)] + [
                         {"url_name": "zoom", "positive": False, "value": -20}]}}


@pytest.mark.parametrize("changes", [
    {"weapon": None, "wildcard": "all"},
    {"weapon": None, "wildcard": "rifle"},
    {"positives": [[CC], [CD]]},
    {"positives": [[CC], [CD], [ANY_ATTRIBUTE]]},
    {"positives": [[CC, ANY_ATTRIBUTE], [CD], [MS]]},
    {"enabled": False},
])
def test_only_specific_weapon_and_three_explicit_positive_positions(changes):
    assert query_keys(rule(**changes)) == ()


def test_or_expands_distinct_triples_and_local_conditions_share_query():
    assert query_keys(rule(positives=[[CC, CD], [CC, CD], [MS]])) == (KEY,)
    expanded = query_keys(rule(positives=[[CC, "toxin_damage"], [CD], [MS]]))
    assert len(expanded) == 2 and KEY in expanded
    assert query_keys(rule(negatives=[[ANY_ATTRIBUTE]], zero_rerolls=True,
                           positive_ratings=[{CC: "A"}, {}, {}])) == (KEY,)
    assert set(KEY.params()) == {"type", "weapon_url_name", "positive_stats", "sort_by"}


def test_proxy_routes_bound_each_windows_tunnel_and_preserve_egress_identity():
    urls = [f"http://wm{i}:password@127.0.0.1:23990" for i in range(257)]
    routed, group = proxy_routes({"proxies": urls, "ssh": {"local_port": 23990}})
    assert len(group.tunnels) == 3
    assert routed[0] == urls[0]
    assert routed[127] == "http://wm127:password@127.0.0.1:23991"
    assert routed[128] == "http://wm128:password@127.0.0.1:23992"
    assert routed[256] == "http://wm256:password@127.0.0.1:23991"
    assert not group.ready
    with pytest.raises(ValueError, match="重复"):
        proxy_routes({"proxies": urls + [urls[0]], "ssh": {"local_port": 23990}})


def test_each_subscription_gets_own_baseline_without_resetting_existing_target():
    fast = FastRivenPoller(None, Config(), None)
    first, second = rule(), rule(102, 2)
    fast.sync([first], now=100)
    state = fast.queries[KEY]
    assert state.observe([auction("old", 90)], now=101) == []
    fast.sync([first, second], now=110)
    emitted = state.observe([auction("old", 90), auction("new", 111)], now=112)
    assert emitted == [([auction("new", 111)], [first])]
    assert state.observe([auction("new", 111)], now=113) == []
    # 刚加入的目标也已完成基线；缓存后来出现的旧单仍不能成为新挂单。
    emitted = state.observe([auction("late-old", 99), auction("next", 114)], now=115)
    assert emitted == [([auction("next", 114)], [first, second])]
    second = {**second, "wm_fast_generation": 2}
    fast.sync([first, second], now=120)
    assert state.observe([auction("after-reenable", 121)], now=122) == [
        ([auction("after-reenable", 121)], [first])]
    assert len(fast.queries) == 1


def test_result_cap_disables_fast_output_and_recovery_rebaselines():
    fast = FastRivenPoller(None, Config(), None)
    cfg = rule()
    fast.sync([cfg], now=100)
    state = fast.queries[KEY]
    state.observe([], now=101)
    assert state.observe([auction(str(i)) for i in range(500)], now=102) == []
    assert state.state == "truncated"
    assert state.observe([auction("recovered", 103)], now=104) == []
    assert state.state == "running"
    assert state.observe([auction("recovered", 103), auction("fresh", 105)], now=106) == [
        ([auction("fresh", 105)], [cfg])]


def test_invisible_or_malformed_timestamp_orders_are_not_fresh_pushes():
    fast = FastRivenPoller(None, Config(), None)
    fast.sync([rule()], now=100)
    state = fast.queries[KEY]
    state.observe([], now=101)
    rows = [auction(str(i)) for i in range(5)]
    rows[0]["closed"] = True
    rows[1]["private"] = True
    rows[2]["visible"] = False
    rows[3]["created"] = "bad date"
    rows[4]["created"] = "2026-09-08T10:00:00"  # 缺少时区
    assert state.observe(rows, now=102) == []


async def test_pool_rotates_shared_exits_and_paces_from_actual_dispatch():
    calls = []
    async def handler(url, request):
        calls.append((url, asyncio.get_running_loop().time()))
        return httpx.Response(200, json={"payload": {"auctions": []}})
    pool = ProxyPool(["http://one", "http://two"], interval=.06,
                     client_factory=lambda url: httpx.AsyncClient(
                         transport=httpx.MockTransport(lambda r: handler(url, r))))
    try:
        await pool.search(KEY)
        await pool.search(KEY)
        await pool.search(KEY)
        assert [url for url, _ in calls] == ["http://one", "http://two", "http://one"]
        assert calls[2][1] - calls[0][1] >= .055
    finally:
        await pool.close()


async def test_one_429_cools_only_that_exit_and_immediately_uses_another():
    calls = []
    async def handler(url, request):
        calls.append(url)
        if url.endswith("one"):
            return httpx.Response(429, headers={"Retry-After": "60"})
        return httpx.Response(200, json={"payload": {"auctions": [auction()]}})
    pool = ProxyPool(["http://secret@one", "http://secret@two", "http://secret@three"],
                     client_factory=lambda url: httpx.AsyncClient(
                         transport=httpx.MockTransport(lambda r: handler(url, r))))
    try:
        assert await asyncio.wait_for(pool.search(KEY), 1) == [auction()]
        assert calls == ["http://secret@one", "http://secret@two"]
        assert pool.exits[0].ready_at > asyncio.get_running_loop().time() + 59
        assert pool.snapshot()["pool_backoff_seconds"] == 0
        assert "secret" not in json.dumps(pool.snapshot())
    finally:
        await pool.close()


async def test_correlated_429s_pause_whole_pool_and_cancel_releases_waiter():
    third_limit = asyncio.Event()
    calls = []
    async def handler(url, request):
        calls.append(url)
        if len(calls) % 2:
            if len(calls) == 5:
                third_limit.set()
            return httpx.Response(429, headers={"Retry-After": "60"})
        return httpx.Response(200, json={"payload": {"auctions": []}})
    pool = ProxyPool([f"http://exit-{i}" for i in range(6)],
                     client_factory=lambda url: httpx.AsyncClient(
                         transport=httpx.MockTransport(lambda r: handler(url, r))))
    pending = None
    try:
        await pool.search(KEY)
        await pool.search(KEY)
        pending = asyncio.create_task(pool.search(KEY))
        await asyncio.wait_for(third_limit.wait(), 1)
        assert pool.snapshot()["pool_backoff_seconds"] > 59
        assert len(calls) == 5 and not pending.done()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        assert pool.snapshot()["busy"] == 0
    finally:
        if pending:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await pool.close()


async def test_cancelled_request_releases_exit():
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"payload": {"auctions": []}})
    pool = ProxyPool(["http://one"], interval=0, client_factory=lambda _: httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))
    task = asyncio.create_task(pool.search(KEY))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert pool.snapshot()["busy"] == 0
        release.set()
        assert await pool.search(KEY) == []
    finally:
        await pool.close()


async def test_cleanup_failure_still_closes_ssh_tunnels():
    class EmptyStore:
        def wm_fast_configs(self, **kwargs):
            return []
    class BrokenPool:
        async def close(self):
            raise RuntimeError("close failed")
    class Tunnel:
        ready = True
        closed = False
        async def close(self):
            self.closed = True
    fast = FastRivenPoller(EmptyStore(), Config(), None)
    fast.pool, fast.tunnel = BrokenPool(), Tunnel()
    task = asyncio.create_task(fast.run())
    await asyncio.sleep(0)
    task.cancel()
    result = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result[0], RuntimeError)
    assert fast.tunnel.closed and fast.state == "stopped"


@pytest.mark.parametrize("phase", ["waiting", "in-flight"])
async def test_shorter_hot_interval_applies_to_waiting_and_inflight_queries(phase):
    entered, release, dispatched = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Rules:
        def wm_fast_configs(self, **kwargs):
            return [rule()]
    class Pool:
        calls = 0
        async def search(self, key, on_start):
            self.calls += 1
            if self.calls == 1:
                # 模拟已耗时两秒的请求，无须依赖真实计时等待。
                on_start(asyncio.get_running_loop().time() - 2)
                entered.set()
                await release.wait()
                return []
            on_start(asyncio.get_running_loop().time())
            dispatched.set()
            await asyncio.Event().wait()
        async def close(self):
            pass
    config = Config(wm_fast_interval=60)
    fast = FastRivenPoller(Rules(), config, None)
    fast.pool = Pool()
    task = asyncio.create_task(fast.run())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if phase == "waiting":
            first_request = fast.queries[KEY].task
            release.set()
            await first_request
        config.wm_fast_interval = 1
        fast.wakeup()
        release.set()
        await asyncio.wait_for(dispatched.wait(), 1)
        assert fast.pool.calls == 2
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.fixture
def store():
    value = Store(":memory:")
    for scope in (101, 102, 103):
        value.upsert_qq_target(scope, 1, enabled=True)
        value.set_target_wm_fast_enabled(scope, scope != 103)
        value.add_config(scope, weapon="torid", wildcard=None,
                         positives=[[CC], [CD], [MS]], negatives=[["zoom"]])
    yield value
    value.close()


@pytest.mark.parametrize("order", ["fast-first", "ordinary-first", "concurrent"])
async def test_fast_and_ordinary_dedupe_per_target_but_off_target_still_polls(store, order):
    p = SniperPoller(store, Config())
    p._first_pass = False
    async def recent():
        return [auction()]
    p.wfm.recent_auctions = recent
    async def fast():
        await p._on_fast_auctions([auction()], store.wm_fast_configs())
    try:
        if order == "concurrent":
            await asyncio.gather(fast(), p._poll_once())
        elif order == "fast-first":
            await fast()
            assert {item.target for item in p.queued_deliveries} == {101, 102}
            await p._poll_once()
        else:
            await p._poll_once()
            await fast()
        assert sorted(item.target for item in p.queued_deliveries) == [101, 102, 103]
    finally:
        await p.wfm.close()


async def test_rejected_fast_enqueue_does_not_consume_ordinary_notification(store, monkeypatch):
    p = SniperPoller(store, Config())
    p._first_pass = False
    enqueue = p.enqueue_delivery
    monkeypatch.setattr(p, "enqueue_delivery", lambda item: False)
    try:
        await p._on_fast_auctions([auction()], store.wm_fast_configs())
        monkeypatch.setattr(p, "enqueue_delivery", enqueue)
        async def recent():
            return [auction()]
        p.wfm.recent_auctions = recent
        await p._poll_once()
        assert sorted(item.target for item in p.queued_deliveries) == [101, 102, 103]
    finally:
        await p.wfm.close()


async def test_local_negative_reroll_rating_and_blacklist_filters_are_preserved(store):
    p = SniperPoller(store, Config())
    cfg = store.wm_fast_configs()[0]
    rows = [auction("valid"), auction("negative"), auction("rerolled"), auction("blocked")]
    rows[1]["item"]["attributes"][-1]["url_name"] = "reload_speed"
    rows[2]["item"]["re_rolls"] = 1
    rows[3]["owner"]["ingame_name"] = "BlockedSeller"
    store.add_blacklist(101, "BlockedSeller")
    # 用真实存储规则更新约束，后续 token 核对仍走生产路径。
    store.delete_config(cfg["id"], 101)
    store.add_config(101, weapon="torid", wildcard=None,
                     positives=[[CC], [CD], [MS]], negatives=[["zoom"]], zero_rerolls=True)
    try:
        valid_cfg = next(c for c in store.wm_fast_configs() if c["group_id"] == 101)
        await p._on_fast_auctions(rows, [valid_cfg])
        assert len(p.queued_deliveries) == 1
        assert p.queued_deliveries[0].payload.auction["id"] == "valid"
        store.delete_config(valid_cfg["id"], 101)
        store.add_config(101, weapon="torid", wildcard=None,
                         positives=[[CC], [CD], [MS]], negatives=[["zoom"]],
                         positive_ratings=[{CC: "S"}, {CD: "S"}, {MS: "S"}])
        rated_cfg = next(c for c in store.wm_fast_configs() if c["group_id"] == 101)
        await p._on_fast_auctions([auction("below-rating")], [rated_cfg])
        assert len(p.queued_deliveries) == 1
    finally:
        await p.wfm.close()


async def test_rule_removed_during_pack_cannot_hitchhike_on_other_valid_rule(store, monkeypatch):
    store.set_target_wm_fast_enabled(102, False)
    original_cfg = store.wm_fast_configs()[0]
    zero_id = store.add_config(101, weapon="torid", wildcard=None,
                               positives=[[CC], [CD], [MS]], negatives=[["zoom"]],
                               zero_rerolls=True)
    p = SniperPoller(store, Config())
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original_pack = p._pack_qq_hit_cards
    def pack(cards):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(3)
        return original_pack(cards)
    monkeypatch.setattr(p, "_pack_qq_hit_cards", pack)
    zero, rolled = auction("zero"), auction("rolled")
    rolled["item"]["re_rolls"] = 3
    task = asyncio.create_task(p._on_fast_auctions([zero, rolled], store.wm_fast_configs()))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        store.delete_config(original_cfg["id"], 101)
        release.set()
        await asyncio.wait_for(task, 3)
        assert len(p.queued_deliveries) == 1
        payload = p.queued_deliveries[0].payload
        assert not isinstance(payload, _HitBatch)
        assert payload.auction["id"] == "zero"
        assert payload.config["id"] == zero_id and payload.extra_count == 0
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await p.wfm.close()


async def test_switch_off_during_search_discards_results_and_normal_route_continues(store):
    p = SniperPoller(store, Config())
    p._first_pass = False
    store.set_target_wm_fast_enabled(102, False)
    p.fast.sync(store.wm_fast_configs(), now=100)
    state = p.fast.queries[KEY]
    state.observe([], now=100)
    entered, release = asyncio.Event(), asyncio.Event()
    class Pool:
        async def search(self, key, on_start):
            on_start(asyncio.get_running_loop().time())
            entered.set()
            await release.wait()
            return [auction()]
    p.fast.pool = Pool()
    task = asyncio.create_task(p.fast._request(state))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        store.set_target_wm_fast_enabled(101, False)
        release.set()
        await task
        assert p.queued_deliveries == ()
        async def recent():
            return [auction()]
        p.wfm.recent_auctions = recent
        await p._poll_once()
        assert len(p.queued_deliveries) == 3
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await p.wfm.close()


async def test_tunnel_cancel_during_thread_launch_reclaims_spawned_process(monkeypatch):
    tunnel = SshTunnel({})
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    class Process:
        terminated = False
        def poll(self):
            return 0 if self.terminated else None
        def terminate(self):
            self.terminated = True
        def wait(self, timeout=None):
            assert self.terminated
    process = Process()
    def launch():
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(3)
        return process
    monkeypatch.setattr(tunnel, "_launch", launch)
    tunnel.start()
    try:
        await asyncio.wait_for(entered.wait(), 1)
        closing = asyncio.create_task(tunnel.close())
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(closing, 2)
        assert process.terminated and not tunnel.ready
    finally:
        release.set()
        await tunnel.close()
