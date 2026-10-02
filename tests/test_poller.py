"""轮询与统一内存发送流水线。"""

import asyncio
import sys
import time
import types
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.plugins.riven_sniper.poller as poller_mod  # noqa: E402
from src.plugins.riven_sniper import bargain  # noqa: E402
from src.plugins.riven_sniper.channel_push import (  # noqa: E402
    ChannelCard,
    ChannelPushPayload,
)
from src.plugins.riven_sniper.chat_tracking import TrackingStore  # noqa: E402
from src.plugins.riven_sniper.delivery import (  # noqa: E402
    DeliveryItem,
    DeliverySource,
)
from src.plugins.riven_sniper.poller import (  # noqa: E402
    SniperPoller,
    _HitBatch,
    _HitCard,
    _send_delay,
)
from src.plugins.riven_sniper.criteria import ANY_ATTRIBUTE  # noqa: E402
from src.plugins.riven_sniper.stats import STATS  # noqa: E402
from src.plugins.riven_sniper.store import Store  # noqa: E402

GROUP = 302875968

NAMI_ITEM = {
    "weapon_url_name": "nami_solo", "name": "visi-loctitis", "type": "riven",
    "mod_rank": 8, "re_rolls": 38, "mastery_level": 14, "polarity": "vazarin",
    "attributes": [
        {"url_name": "base_damage_/_melee_damage", "value": 237.0, "positive": True},
        {"url_name": "critical_damage", "value": 109.4, "positive": True},
        {"url_name": "critical_chance_on_slide_attack", "value": -116.2, "positive": False},
    ],
}

AUCTION = {
    "id": "auc-1", "item": NAMI_ITEM,
    "owner": {"id": "u1", "ingame_name": "Seller", "status": "ingame"},
    "starting_price": 100, "buyout_price": 150, "is_direct_sell": False,
    "closed": False, "private": False, "visible": True, "platform": "pc",
}


class _OneBotAdapter:
    @staticmethod
    def get_name():
        return "OneBot V11"


def _onebot_stub():
    return types.SimpleNamespace(adapter=_OneBotAdapter())


def _config(dry_run=False):
    return types.SimpleNamespace(
        sniper_poll_interval=15.0, sniper_dry_run=dry_run,
        sniper_max_configs_per_group=20,
        sniper_send_interval=0.0, sniper_send_concurrency=0,
        send_queue_maxsize=1000, trade_message_ttl_seconds=60.0,
        send_max_retries=1, send_retry_delay_seconds=2.0,
        discord_dm_enabled=True)


def _poller(store, dry_run=False):
    if store.get_target(GROUP) is None:
        store.upsert_qq_target(GROUP, 10000, enabled=True)
    p = SniperPoller(store, _config(dry_run))
    p._first_pass = False   # 跳过首轮只标记
    async def _fake_recent():
        return [AUCTION]
    p.wfm.recent_auctions = _fake_recent
    return p


def _enqueue(
    p, target, payload, *, attempts=0, now=None, expires_at=None,
    source=DeliverySource.SYSTEM, generation=None,
):
    if target > 0 and p.store.get_target(target) is None:
        p.store.upsert_qq_target(target, 10000, enabled=True)
    item = p.new_delivery(
        source, target, payload, generation=generation,
        now=time.time() if now is None else now)
    if attempts or expires_at is not None:
        item = DeliveryItem(
            source=item.source,
            target=item.target,
            payload=item.payload,
            attempts=attempts,
            enqueued_at=item.enqueued_at,
            expires_at=item.expires_at if expires_at is None else expires_at,
            generation=item.generation,
        )
    assert p.enqueue_delivery(item)
    return item


def test_send_interval_is_fixed_and_never_negative():
    assert _send_delay(0) == 0.0
    assert _send_delay(-1) == 0.0
    assert _send_delay(2.0) == 2.0


@pytest.mark.parametrize("payload", [
    _HitCard({"group_id": GROUP}, AUCTION, locale="zh"),
    bargain.BargainItemPushPayload(
        "arcane_grace", {}, bargain.Hit(
            Decimal("1"), Decimal("2"), Decimal("0.5")), "zh", GROUP),
    bargain.BargainRivenPushPayload(
        "nami_solo", {}, bargain.Hit(
            Decimal("1"), Decimal("2"), Decimal("0.5")), "zh", GROUP),
    ChannelPushPayload(
        seller="Seller", channel="#T_ZH", cards=(), locale="zh",
        target_scope=GROUP),
])
async def test_deferred_card_uses_current_target_language_before_send(
        tmp_path, monkeypatch, payload):
    """入队后切换语言时，预渲染和最终发送都不能沿用旧语言成品。"""
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=False)
    rendered_locales = []

    async def render(current):
        rendered_locales.append(current.locale)
        current.rendered = f"rendered-{current.locale}"
        return current.rendered

    monkeypatch.setattr(p, "_render_payload", render)
    store.set_target_locale(GROUP, "en")
    first = await p._render_payload_for_target(GROUP, payload)
    assert first == "rendered-en"

    store.set_target_locale(GROUP, "zh")
    second = await p._render_payload_for_target(GROUP, payload, first)
    assert second == "rendered-zh"
    assert rendered_locales == ["en", "zh"]

    await p.wfm.close()
    store.close()


def test_full_delivery_queue_evicts_global_oldest(tmp_path):
    store = _tmp_store(tmp_path)
    config = _config(dry_run=True)
    config.send_queue_maxsize = 2
    p = SniperPoller(store, config)
    p.activate_source(DeliverySource.IRC, "run-a")

    now = time.time()
    _enqueue(p, GROUP, "oldest", now=now, source=DeliverySource.WM)
    _enqueue(
        p, GROUP, "middle", now=now + 1, source=DeliverySource.IRC,
        generation="run-a")
    _enqueue(
        p, GROUP, "newest", now=now + 2,
        source=DeliverySource.BARGAIN)

    assert [item.payload for item in p.queued_deliveries] == [
        "middle", "newest"]
    assert [item.source for item in p.queued_deliveries] == [
        DeliverySource.IRC, DeliverySource.BARGAIN]
    counters = p.delivery_status["counters_by_source"]["wm"]
    assert counters["evicted_oldest"] == 1
    store.close()


def test_delivery_ttl_uses_runtime_configuration(tmp_path):
    store = _tmp_store(tmp_path)
    config = _config(dry_run=True)
    config.trade_message_ttl_seconds = 12.5
    p = SniperPoller(store, config)

    item = p.new_delivery(DeliverySource.WM, GROUP, "hit", now=100)

    assert item.expires_at == 112.5
    store.close()


async def test_cancel_irc_generation_removes_queued_and_inflight(
        tmp_path, monkeypatch):
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=False)
    p.activate_source(DeliverySource.IRC, "run-a")
    monkeypatch.setattr(p, "_target_bot_connected", lambda _target: True)

    render_started = asyncio.Event()
    release_render = asyncio.Event()
    sent = []

    async def render(payload):
        render_started.set()
        await release_render.wait()
        return payload

    async def send(_target, payload):
        sent.append(payload)

    monkeypatch.setattr(p, "_render_payload", render)
    monkeypatch.setattr(p, "_send_to", send)
    irc = p.new_delivery(
        DeliverySource.IRC, GROUP, "irc", generation="run-a")
    assert p.enqueue_delivery(irc)
    task = asyncio.create_task(p._send_loop(p.queue, lambda: 0.0))
    await asyncio.wait_for(render_started.wait(), 2)

    cancelled = p.cancel_source(DeliverySource.IRC, "run-a")
    assert cancelled == {"queued": 0, "retrying": 0, "inflight": 1}
    release_render.set()
    for _ in range(20):
        if p.queue_depth == 0:
            break
        await asyncio.sleep(0)
    assert sent == []
    assert p.queue_depth == 0

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await p.wfm.close()
    store.close()


async def test_cancel_irc_generation_removes_retry_tracking_immediately(
        tmp_path):
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=False)
    p.activate_source(DeliverySource.IRC, "run-a")
    irc = p.new_delivery(
        DeliverySource.IRC, GROUP, "retry", generation="run-a")
    p._schedule_requeue(irc, 60.0)

    assert p.delivery_status["inflight_by_source"]["irc"] == 1
    cancelled = p.cancel_source(DeliverySource.IRC, "run-a")

    assert cancelled == {"queued": 0, "retrying": 1, "inflight": 0}
    assert p.queue_depth == 0
    assert p.delivery_status["inflight_by_source"]["irc"] == 0
    assert not p._retry_tasks
    assert not p._retry_items
    await asyncio.sleep(0)
    await p.wfm.close()
    store.close()


async def test_poll_interval_hot_update_interrupts_existing_wait(
        tmp_path, monkeypatch):
    """轮询正在等待旧长间隔时，热更新应立即按新间隔重新调度。"""
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=True)
    p.config.sniper_poll_interval = 60.0
    first_poll = asyncio.Event()
    second_poll = asyncio.Event()
    calls = 0

    async def poll_once():
        nonlocal calls
        calls += 1
        (first_poll if calls == 1 else second_poll).set()

    monkeypatch.setattr(p, "_poll_once", poll_once)
    task = asyncio.create_task(p._poll_loop())
    await asyncio.wait_for(first_poll.wait(), 2)

    p.config.sniper_poll_interval = 0.01
    await p.notify_runtime_settings_changed()
    await asyncio.wait_for(second_poll.wait(), 1)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await p.wfm.close()
    store.close()


async def test_group_hot_update_forces_immediate_poll(
        tmp_path, monkeypatch):
    """只修改目标群时也必须打断旧轮询等待，不能等到原截止时间。"""
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=True)
    p.config.sniper_poll_interval = 60.0
    first_poll = asyncio.Event()
    second_poll = asyncio.Event()
    calls = 0

    async def poll_once():
        nonlocal calls
        calls += 1
        (first_poll if calls == 1 else second_poll).set()

    monkeypatch.setattr(p, "_poll_once", poll_once)
    task = asyncio.create_task(p._poll_loop())
    await asyncio.wait_for(first_poll.wait(), 2)

    await p.notify_runtime_settings_changed(poll_now=True)
    await asyncio.wait_for(second_poll.wait(), 1)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await p.wfm.close()
    store.close()


def _tmp_store(tmp_path):
    store = Store(tmp_path / "t.db")
    store.upsert_qq_target(GROUP, 10000, enabled=True)
    store.add_config(
        GROUP, weapon=None, wildcard="all",
        positives=[["base_damage_/_melee_damage"], ["critical_damage"]],
        negatives=[[ANY_ATTRIBUTE]],
    )
    return store


async def test_poll_enqueues_deferred_card_not_rendered_image(tmp_path):
    """正常模式：轮询循环只入队 _HitCard 描述，不在此处渲染图片。"""
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=False)
    await p._poll_once()
    assert p.queue.qsize() == 1
    item = p.queue.get_nowait()
    group_id, payload = item.target, item.payload
    attempts, enqueued_at = item.attempts, item.enqueued_at
    assert group_id == GROUP and attempts == 0
    assert abs(time.time() - enqueued_at) < 5
    assert isinstance(payload, _HitCard)
    assert payload.auction["id"] == "auc-1"
    await p.wfm.close()
    store.close()


async def test_wfm_scores_each_auction_once_across_configs_and_preflight(
        tmp_path, monkeypatch):
    store = Store(tmp_path / "single-grade.db")
    store.upsert_qq_target(GROUP, 10000, enabled=True)
    common = {
        "positives": [
            ["base_damage_/_melee_damage"], ["critical_damage"],
        ],
        "negatives": [[ANY_ATTRIBUTE]],
    }
    store.add_config(
        GROUP, weapon=None, wildcard="all",
        positive_ratings=[{"base_damage_/_melee_damage": "F"}, {}],
        **common,
    )
    store.add_config(
        GROUP, weapon="nami_solo", wildcard=None, **common)
    p = _poller(store, dry_run=False)
    original = poller_mod.grade_auction_item
    calls = 0

    def counting_grade(item):
        nonlocal calls
        calls += 1
        return original(item)

    monkeypatch.setattr(poller_mod, "grade_auction_item", counting_grade)

    await p._poll_once()

    assert calls == 1
    payload = p.queue.get_nowait().payload
    assert isinstance(payload, _HitCard)
    assert payload.grade_result is not None
    SniperPoller._pack_discord_hit_cards([payload])
    assert calls == 1
    await p.wfm.close()
    store.close()


async def test_poll_accepts_crossplay_auction_from_other_platform(tmp_path):
    store = _tmp_store(tmp_path)
    p = SniperPoller(store, _config(dry_run=False))
    p._first_pass = False
    auction = {
        **AUCTION,
        "id": "auc-xbox",
        "platform": "xbox",
        "owner": {**AUCTION["owner"], "crossplay": True},
    }

    async def _fake_recent():
        return [auction]

    p.wfm.recent_auctions = _fake_recent
    await p._poll_once()

    assert p.queue.qsize() == 1
    payload = p.queue.get_nowait().payload
    assert isinstance(payload, _HitCard)
    assert payload.auction["id"] == "auc-xbox"
    await p.wfm.close()
    store.close()


async def test_first_pass_still_hands_unseen_auctions_to_riven_bargain(
        tmp_path, monkeypatch):
    store = Store(tmp_path / "first-pass.db")
    p = SniperPoller(store, _config(dry_run=False))
    auction = {
        **AUCTION,
        "id": "created-during-downtime",
        "created": "2026-07-21T00:00:00Z",
        "is_direct_sell": True,
    }

    async def _fake_recent():
        return [auction]

    received = []

    class BargainSpy:
        async def on_fresh_riven_auctions(self, auctions):
            received.extend(auctions)

    p.wfm.recent_auctions = _fake_recent
    monkeypatch.setattr(
        poller_mod.shared, "get_bargain", lambda: BargainSpy())
    await p._poll_once()

    assert p._first_pass is False
    assert [row["id"] for row in received] == ["created-during-downtime"]
    assert p.queue.empty()  # 首轮狙击依然不推送。
    await p.wfm.close()
    store.close()


async def test_dry_run_enqueues_text(tmp_path):
    """dry-run：入队纯文本（不渲染），供控制台/脚本直接打印。"""
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=True)
    await p._poll_once()
    payload = p.queue.get_nowait().payload
    assert isinstance(payload, str)
    assert "WARFRAME.MARKET · RIVEN" in payload and "海波单剑" in payload
    await p.wfm.close()
    store.close()


async def test_poll_does_not_enqueue_blacklisted_seller(tmp_path):
    """WM 卖家黑名单在命中阶段生效，且玩家名大小写不影响匹配。"""
    store = _tmp_store(tmp_path)
    store.add_blacklist(GROUP, "seller")
    p = _poller(store, dry_run=False)

    await p._poll_once()

    assert p.queue.empty()
    await p.wfm.close()
    store.close()


async def test_wm_blacklist_matches_unicode_space_variant(tmp_path):
    store = _tmp_store(tmp_path)
    store.add_blacklist(GROUP, "Example o")
    p = _poller(store, dry_run=False)
    auction = {
        **AUCTION,
        "owner": {**AUCTION["owner"], "ingame_name": "Example\u00a0o"},
    }

    async def recent_auctions():
        return [auction]

    p.wfm.recent_auctions = recent_auctions
    await p._poll_once()

    assert p.queue.empty()
    await p.wfm.close()
    store.close()


async def test_poll_dedups_same_auction_across_matching_configs(tmp_path):
    """同一挂单被多配置命中时只入队一次，并记录额外命中数。"""
    store = Store(tmp_path / "t.db")
    store.upsert_qq_target(GROUP, 10000, enabled=True)
    conditions = {
        "positives": [["base_damage_/_melee_damage"], ["critical_damage"]],
        "negatives": [[ANY_ATTRIBUTE]],
    }
    store.add_config(
        GROUP, weapon=None, wildcard="all", **conditions,
    )
    store.add_config(
        GROUP, weapon="nami_solo", wildcard=None,
        **conditions,
    )
    p = SniperPoller(store, _config(dry_run=False))
    p._first_pass = False

    async def _fake():
        return [AUCTION]
    p.wfm.recent_auctions = _fake
    await p._poll_once()

    assert p.queue.qsize() == 1
    payload = p.queue.get_nowait().payload
    assert isinstance(payload, _HitCard)
    assert payload.extra_count == 1
    await p.wfm.close()
    store.close()


async def test_render_payload_renders_card_and_passes_through(tmp_path):
    """发送线路：_HitCard 渲染成图片消息；非卡片负载原样返回（重试不重渲染）。"""
    store = _tmp_store(tmp_path)
    p = _poller(store)
    card = _HitCard({"id": 1, "group_id": GROUP,
        "weapon": None, "wildcard": "melee",
        "positives": [["base_damage_/_melee_damage"], ["critical_damage"]],
        "negatives": [[ANY_ATTRIBUTE]], "enabled": 1}, AUCTION)
    msg = await p._render_payload(card)
    assert any(seg.type == "image" for seg in msg)
    assert await p._render_payload(card) is msg  # 重试复用成品，同时保留卖家元数据
    assert await p._render_payload("原样文本") == "原样文本"
    await p.wfm.close()
    store.close()


async def test_send_pipeline_renders_ahead_while_first_send_is_waiting(
        tmp_path, monkeypatch):
    """QQ 上传上一条时，后一条应在有界流水线中提前渲染。"""
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=False)
    monkeypatch.setattr(p, "_bot_connected", lambda: True)

    first_sending = asyncio.Event()
    second_rendered = asyncio.Event()
    release_first_send = asyncio.Event()
    second_sent = asyncio.Event()
    sent = []

    async def render(payload):
        if payload == "second":
            second_rendered.set()
        return f"rendered-{payload}"

    async def send(_target, message):
        sent.append(message)
        if message == "rendered-first":
            first_sending.set()
            await release_first_send.wait()
        else:
            second_sent.set()

    monkeypatch.setattr(p, "_render_payload", render)
    monkeypatch.setattr(p, "_send_to", send)
    now = time.time()
    _enqueue(p, GROUP, "first", now=now)
    _enqueue(p, GROUP, "second", now=now)
    task = asyncio.create_task(p._send_loop(p.queue, lambda: 0.0))

    await asyncio.wait_for(first_sending.wait(), 2)
    await asyncio.wait_for(second_rendered.wait(), 2)
    assert sent == ["rendered-first"]
    assert p.queue.qsize() == 0
    assert p.queue_depth == 2

    release_first_send.set()
    await asyncio.wait_for(second_sent.wait(), 2)
    assert sent == ["rendered-first", "rendered-second"]

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await p.wfm.close()
    store.close()


async def test_send_interval_hot_update_releases_same_target_waiter(
        tmp_path, monkeypatch):
    """同目标正在等待旧发送间隔时，热更新为 0 应立即放行下一条。"""
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=False)
    p.config.sniper_send_interval = 60.0
    monkeypatch.setattr(p, "_bot_connected", lambda: True)

    first_sent = asyncio.Event()
    second_sent = asyncio.Event()
    sent = []

    async def send(_target, message):
        sent.append(message)
        (first_sent if len(sent) == 1 else second_sent).set()

    monkeypatch.setattr(p, "_send_to", send)
    now = time.time()
    _enqueue(p, GROUP, "first", now=now)
    _enqueue(p, GROUP, "second", now=now)
    task = asyncio.create_task(p._send_loop(
        p.queue, lambda: p.config.sniper_send_interval))

    await asyncio.wait_for(first_sent.wait(), 2)
    for _ in range(10):
        await asyncio.sleep(0)
    assert not second_sent.is_set()

    p.config.sniper_send_interval = 0.0
    await p.notify_runtime_settings_changed()
    await asyncio.wait_for(second_sent.wait(), 1)
    assert sent == ["first", "second"]

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await p.wfm.close()
    store.close()


async def test_sender_supervisor_recovers_inflight_in_original_order(
        tmp_path, monkeypatch):
    """流水线阶段崩溃后，在途消息按入口顺序回收并由重启实例继续发送。"""
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=False)
    p._SEND_RESTART_INITIAL_SECONDS = 0.0
    p._SEND_RESTART_MAX_SECONDS = 0.0
    monkeypatch.setattr(p, "_bot_connected", lambda: True)

    original_order_loop = p._render_order_loop
    order_loop_starts = 0

    async def flaky_order_loop(rendered_queue, ready_queue):
        nonlocal order_loop_starts
        order_loop_starts += 1
        if order_loop_starts == 1:
            await rendered_queue.get()
            raise RuntimeError("forced order-stage failure")
        await original_order_loop(rendered_queue, ready_queue)

    sent = []
    all_sent = asyncio.Event()

    async def send(_target, message):
        sent.append(message)
        if len(sent) == 3:
            all_sent.set()

    monkeypatch.setattr(p, "_render_order_loop", flaky_order_loop)
    monkeypatch.setattr(p, "_send_to", send)
    now = time.time()
    for payload in ("first", "second", "third"):
        _enqueue(p, GROUP, payload, now=now)

    task = asyncio.create_task(p._supervise_send_loop(
        p.queue, lambda: p.config.sniper_send_interval))
    await asyncio.wait_for(all_sent.wait(), 2)

    assert sent == ["first", "second", "third"]
    assert p.sender_health["state"] == "running"
    assert p.sender_health["restart_count"] == 1
    assert "forced order-stage failure" in p.sender_health["last_error"]
    assert p.sender_health["last_success_at"] is not None

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert p.sender_health["state"] == "stopped"
    await p.wfm.close()
    store.close()


async def test_sender_supervisor_observes_send_job_failure(
        tmp_path, monkeypatch):
    """发送子任务的调度异常不能被静默吞掉，应触发整条流水线恢复。"""
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=False)
    p._SEND_RESTART_INITIAL_SECONDS = 0.0
    p._SEND_RESTART_MAX_SECONDS = 0.0
    monkeypatch.setattr(p, "_bot_connected", lambda: True)

    original_ready = p._queue_item_is_ready
    failed_once = False

    async def flaky_ready(queue, item, *, after_render):
        nonlocal failed_once
        if after_render and not failed_once:
            failed_once = True
            raise RuntimeError("forced send-job failure")
        return await original_ready(queue, item, after_render=after_render)

    sent = asyncio.Event()

    async def send(_target, _message):
        sent.set()

    monkeypatch.setattr(p, "_queue_item_is_ready", flaky_ready)
    monkeypatch.setattr(p, "_send_to", send)
    _enqueue(p, GROUP, "message")

    task = asyncio.create_task(p._supervise_send_loop(
        p.queue, lambda: p.config.sniper_send_interval))
    await asyncio.wait_for(sent.wait(), 2)

    assert p.sender_health["state"] == "running"
    assert p.sender_health["restart_count"] == 1
    assert "forced send-job failure" in p.sender_health["last_error"]

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert p.sender_health["state"] == "stopped"
    await p.wfm.close()
    store.close()


async def test_main_channel_default_concurrency_is_not_capped_by_render_window(
        tmp_path, monkeypatch):
    """默认不设上限：活跃目标可超过 8 条预渲染窗口同时在途。"""
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=False)
    monkeypatch.setattr(p, "_bot_connected", lambda: True)

    target_count = p._RENDER_AHEAD + 4
    all_started = asyncio.Event()
    release_sends = asyncio.Event()
    all_finished = asyncio.Event()
    active = 0
    finished = 0
    peak = 0

    async def send(_target, _message):
        nonlocal active, finished, peak
        active += 1
        peak = max(peak, active)
        if active == target_count:
            all_started.set()
        await release_sends.wait()
        active -= 1
        finished += 1
        if finished == target_count:
            all_finished.set()

    monkeypatch.setattr(p, "_send_to", send)
    now = time.time()
    for index in range(target_count):
        _enqueue(p, GROUP + index, f"group-{index}", now=now)
    task = asyncio.create_task(p._send_loop(p.queue, lambda: 0.0))

    await asyncio.wait_for(all_started.wait(), 2)
    assert peak == target_count
    assert p._active_send_count == target_count
    assert p.queue_depth == target_count
    release_sends.set()
    await asyncio.wait_for(all_finished.wait(), 2)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert p._active_send_count == 0
    await p.wfm.close()
    store.close()


async def test_main_channel_concurrency_limit_hot_update_unblocks_waiters(
        tmp_path, monkeypatch):
    """正数上限约束流水线；热更新为 0 后取消额外全局限制。"""
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=False)
    p.config.sniper_send_concurrency = 1
    monkeypatch.setattr(p, "_bot_connected", lambda: True)

    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_sends = asyncio.Event()
    started = 0

    async def send(_target, _message):
        nonlocal started
        started += 1
        (first_started if started == 1 else second_started).set()
        await release_sends.wait()

    monkeypatch.setattr(p, "_send_to", send)
    now = time.time()
    _enqueue(p, GROUP, "group-a", now=now)
    _enqueue(p, GROUP + 1, "group-b", now=now)
    task = asyncio.create_task(p._send_loop(p.queue, lambda: 0.0))

    await asyncio.wait_for(first_started.wait(), 2)
    for _ in range(20):
        await asyncio.sleep(0)
    assert not second_started.is_set()
    assert p._active_send_count == 1

    p.config.sniper_send_concurrency = 0
    await p.notify_runtime_settings_changed()
    await asyncio.wait_for(second_started.wait(), 2)
    assert p._active_send_count == 2
    release_sends.set()

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert p._active_send_count == 0
    await p.wfm.close()
    store.close()


async def test_main_channel_rechecks_blacklist_after_concurrency_wait(
        tmp_path, monkeypatch):
    """等待并发许可期间新增的黑名单必须在实际发送前生效。"""
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=False)
    p.config.sniper_send_concurrency = 1
    monkeypatch.setattr(p, "_bot_connected", lambda: True)

    first_started = asyncio.Event()
    second_waiting = asyncio.Event()
    second_final_check = asyncio.Event()
    release_first = asyncio.Event()
    sent = []
    second_checks = 0

    async def render(payload):
        return f"message-{payload.config['id']}"

    async def send(target, _message):
        sent.append(target)
        if target == GROUP:
            first_started.set()
            await release_first.wait()

    original_ready = p._queue_item_is_ready

    async def tracked_ready(queue, item, *, after_render):
        nonlocal second_checks
        result = await original_ready(
            queue, item, after_render=after_render)
        if after_render and item.target == GROUP + 1:
            second_checks += 1
            if second_checks == 1 and result:
                second_waiting.set()
            elif second_checks == 2:
                second_final_check.set()
        return result

    monkeypatch.setattr(p, "_render_payload", render)
    monkeypatch.setattr(p, "_send_to", send)
    monkeypatch.setattr(p, "_queue_item_is_ready", tracked_ready)
    now = time.time()
    _enqueue(p, GROUP, _HitCard({"id": 1}, AUCTION), now=now)
    _enqueue(p, GROUP + 1, _HitCard({"id": 2}, AUCTION), now=now)
    task = asyncio.create_task(p._send_loop(p.queue, lambda: 0.0))

    await asyncio.wait_for(first_started.wait(), 2)
    await asyncio.wait_for(second_waiting.wait(), 2)
    store.add_blacklist(GROUP + 1, "Seller")
    release_first.set()
    await asyncio.wait_for(second_final_check.wait(), 2)
    await asyncio.sleep(0)

    assert sent == [GROUP]
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert p._active_send_count == 0
    await p.wfm.close()
    store.close()


async def test_send_pipeline_preserves_order_when_second_render_finishes_first(
        tmp_path, monkeypatch):
    """并行渲染可乱序完成，但同一目标仍必须按入队顺序发送。"""
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=False)
    monkeypatch.setattr(p, "_bot_connected", lambda: True)

    release_first_render = asyncio.Event()
    second_rendered = asyncio.Event()
    both_sent = asyncio.Event()
    sent = []

    async def render(payload):
        if payload == "first":
            await release_first_render.wait()
        else:
            second_rendered.set()
        return f"rendered-{payload}"

    async def send(_target, message):
        sent.append(message)
        if len(sent) == 2:
            both_sent.set()

    monkeypatch.setattr(p, "_render_payload", render)
    monkeypatch.setattr(p, "_send_to", send)
    now = time.time()
    _enqueue(p, GROUP, "first", now=now)
    _enqueue(p, GROUP, "second", now=now)
    task = asyncio.create_task(p._send_loop(p.queue, lambda: 0.0))

    await asyncio.wait_for(second_rendered.wait(), 2)
    await asyncio.sleep(0)
    assert sent == []
    release_first_render.set()
    await asyncio.wait_for(both_sent.wait(), 2)
    assert sent == ["rendered-first", "rendered-second"]

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await p.wfm.close()
    store.close()


async def test_channel_delivery_can_jump_recent_wm_burst_for_same_target(
        tmp_path, monkeypatch):
    """同目标首条已发送时，频道消息应越过刚排队的 WM，但不打断在途调用。"""
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=False)
    p.activate_source(DeliverySource.IRC, "run-a")
    monkeypatch.setattr(p, "_bot_connected", lambda: True)

    first_started = asyncio.Event()
    irc_rendered = asyncio.Event()
    release_first = asyncio.Event()
    all_sent = asyncio.Event()
    sent = []

    async def render(payload):
        if payload == "irc":
            irc_rendered.set()
        return payload

    async def send(_target, message):
        sent.append(message)
        if message == "wm-first":
            first_started.set()
            await release_first.wait()
        if len(sent) == 3:
            all_sent.set()

    monkeypatch.setattr(p, "_render_payload", render)
    monkeypatch.setattr(p, "_send_to", send)
    now = time.time()
    _enqueue(p, GROUP, "wm-first", now=now, source=DeliverySource.WM)
    task = asyncio.create_task(p._send_loop(p.queue, lambda: 0.0))
    await asyncio.wait_for(first_started.wait(), 2)

    _enqueue(p, GROUP, "wm-second", now=now + 0.01,
             source=DeliverySource.WM)
    _enqueue(p, GROUP, "irc", now=now + 0.02,
             source=DeliverySource.IRC, generation="run-a")
    await asyncio.wait_for(irc_rendered.wait(), 2)
    release_first.set()
    await asyncio.wait_for(all_sent.wait(), 2)
    assert sent == ["wm-first", "irc", "wm-second"]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await p.wfm.close()
    store.close()


def test_source_scheduling_advantage_does_not_starve_old_wm(tmp_path):
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=False)
    now = time.time()
    old_wm = p.new_delivery(
        DeliverySource.WM, GROUP, "wm", now=now - 3)
    recent_irc = p.new_delivery(
        DeliverySource.IRC, GROUP, "irc", now=now)

    assert p._delivery_schedule_key(old_wm, 1) < p._delivery_schedule_key(
        recent_irc, 2)
    store.close()


async def test_source_scheduling_applies_before_render_window(tmp_path):
    """近期 WM 突发不能在入口 FIFO 中挡住更时效的频道消息。"""
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=False)
    p.activate_source(DeliverySource.IRC, "run-a")
    now = time.time()
    for index in range(20):
        _enqueue(
            p, GROUP, f"wm-{index}", now=now + index * 0.001,
            source=DeliverySource.WM)
    irc = _enqueue(
        p, GROUP, "irc", now=now + 0.1, source=DeliverySource.IRC,
        generation="run-a")

    selected = await p._get_scheduled_delivery(p.queue)

    assert selected is irc
    assert p.queue.qsize() == 20
    p.queue.task_done()
    remaining = [
        await p._get_scheduled_delivery(p.queue) for _ in range(20)
    ]
    assert [item.payload for item in remaining] == [
        f"wm-{index}" for index in range(20)]
    for _item in remaining:
        p.queue.task_done()
    await p.queue.join()
    await p.wfm.close()
    store.close()


async def test_slow_render_does_not_block_a_different_target(
        tmp_path, monkeypatch):
    """不同目标只受各自顺序约束，后入队目标可先完成发送。"""
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=False)
    monkeypatch.setattr(p, "_bot_connected", lambda: True)

    release_first_render = asyncio.Event()
    second_rendered = asyncio.Event()
    first_sent = asyncio.Event()
    second_sent = asyncio.Event()
    sent = []

    async def render(payload):
        if payload == "slow-first-target":
            await release_first_render.wait()
        else:
            second_rendered.set()
        return payload

    async def send(target, message):
        sent.append((target, message))
        (first_sent if target == GROUP else second_sent).set()

    monkeypatch.setattr(p, "_render_payload", render)
    monkeypatch.setattr(p, "_send_to", send)
    now = time.time()
    _enqueue(p, GROUP, "slow-first-target", now=now)
    _enqueue(p, GROUP + 1, "fast-second-target", now=now)
    task = asyncio.create_task(p._send_loop(p.queue, lambda: 0.0))

    await asyncio.wait_for(second_rendered.wait(), 2)
    await asyncio.wait_for(second_sent.wait(), 2)
    assert sent == [(GROUP + 1, "fast-second-target")]

    release_first_render.set()
    await asyncio.wait_for(first_sent.wait(), 2)
    assert sent == [
        (GROUP + 1, "fast-second-target"),
        (GROUP, "slow-first-target"),
    ]

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await p.wfm.close()
    store.close()


async def test_send_pipeline_render_ahead_window_is_strictly_bounded(
        tmp_path, monkeypatch):
    """发送堵塞时只允许固定数量的消息离开入口队列。"""
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=False)
    p._RENDER_AHEAD = 3
    p._RENDER_WORKERS = 2
    monkeypatch.setattr(p, "_bot_connected", lambda: True)

    render_started = asyncio.Event()
    release_render = asyncio.Event()
    active_renders = 0

    async def render(payload):
        nonlocal active_renders
        active_renders += 1
        if active_renders == p._RENDER_WORKERS:
            render_started.set()
        await release_render.wait()
        return payload

    monkeypatch.setattr(p, "_render_payload", render)
    now = time.time()
    for index in range(10):
        _enqueue(p, GROUP, f"message-{index}", now=now)
    task = asyncio.create_task(p._send_loop(p.queue, lambda: 0.0))

    await asyncio.wait_for(render_started.wait(), 2)
    for _ in range(20):
        if p._pipeline_inflight.get(id(p.queue)) == p._RENDER_AHEAD:
            break
        await asyncio.sleep(0)
    assert p._pipeline_inflight[id(p.queue)] == 3
    assert p.queue.qsize() == 7
    assert p.queue_depth == 10

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await p.wfm.close()
    store.close()


async def test_send_loop_rechecks_blacklist_added_during_render(
        tmp_path, monkeypatch):
    """命中入队后才拉黑卖家，也不能越过最终发送点。"""
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=False)
    monkeypatch.setattr(p, "_bot_connected", lambda: True)

    render_started = asyncio.Event()
    release_render = asyncio.Event()
    second_blacklist_check = asyncio.Event()
    sent = []

    async def delayed_render(_payload):
        render_started.set()
        await release_render.wait()
        return "rendered hit"

    async def record_send(target, message):
        sent.append((target, message))

    original_is_blacklisted = store.is_blacklisted
    checks = 0

    def tracked_is_blacklisted(group_id, seller):
        nonlocal checks
        checks += 1
        result = original_is_blacklisted(group_id, seller)
        if checks >= 2:
            second_blacklist_check.set()
        return result

    monkeypatch.setattr(p, "_render_payload", delayed_render)
    monkeypatch.setattr(p, "_send_to", record_send)
    monkeypatch.setattr(store, "is_blacklisted", tracked_is_blacklisted)

    _enqueue(p, GROUP, _HitCard({"id": 1}, AUCTION))
    task = asyncio.create_task(p._send_loop(p.queue, lambda: 0.0))
    await asyncio.wait_for(render_started.wait(), 2)
    store.add_blacklist(GROUP, "SELLER")
    release_render.set()
    await asyncio.wait_for(second_blacklist_check.wait(), 2)

    assert sent == []
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await p.wfm.close()
    store.close()


async def test_send_loop_rechecks_channel_blacklist_added_during_render(
        tmp_path, monkeypatch):
    store = _tmp_store(tmp_path)
    store.set_target_channel_enabled(GROUP, True)
    store.add_config(
        GROUP, weapon="torid", wildcard=None,
        positives=[["critical_damage"], ["multishot"]], negatives=[])
    p = _poller(store, dry_run=False)
    monkeypatch.setattr(p, "_bot_connected", lambda: True)
    render_started = asyncio.Event()
    release_render = asyncio.Event()
    sent = []

    async def delayed_render(_payload):
        render_started.set()
        await release_render.wait()
        return "rendered channel hit"

    async def record_send(target, message):
        sent.append((target, message))

    payload = ChannelPushPayload(
        seller="ChannelSeller", channel="#T_ZH", cards=(ChannelCard({
            "weapon_slug": "torid", "rerolls": 1,
            "stats": [
                {"ref": "WeaponCritDamageMod", "is_curse": False},
                {"ref": "WeaponFireIterationsMod", "is_curse": False},
            ],
        }, 1),), locale="zh", target_scope=GROUP)
    monkeypatch.setattr(p, "_render_payload", delayed_render)
    monkeypatch.setattr(p, "_send_to", record_send)
    _enqueue(p, GROUP, payload)
    task = asyncio.create_task(p._send_loop(p.queue, lambda: 0.0))

    await asyncio.wait_for(render_started.wait(), 2)
    store.add_blacklist(
        GROUP, "channelseller", scope="channel")
    release_render.set()
    await asyncio.wait_for(p.queue.join(), 2)

    assert sent == []
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await p.wfm.close()
    store.close()


async def test_send_retry_keeps_hit_metadata_for_blacklist(
        tmp_path, monkeypatch):
    """首次发送失败后的成品重试仍保留卖家，期间新增黑名单会取消重试。"""
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=False)
    monkeypatch.setattr(p, "_bot_connected", lambda: True)

    card = _HitCard({"id": 1}, AUCTION)
    rendered_payloads = []
    send_attempts = 0
    blacklist_observed = asyncio.Event()
    blacklist_added = False

    async def fake_render(payload):
        rendered_payloads.append(payload)
        return "rendered hit"

    async def fail_send(_target, _message):
        nonlocal send_attempts
        send_attempts += 1
        raise poller_mod.NetworkError("temporary failure")

    async def add_blacklist_during_retry(item, delay):
        nonlocal blacklist_added
        assert delay == 2.0
        if not blacklist_added:
            blacklist_added = True
            store.add_blacklist(GROUP, "Seller")
        p.enqueue_delivery(item)

    original_is_blacklisted = store.is_blacklisted

    def tracked_is_blacklisted(group_id, seller):
        result = original_is_blacklisted(group_id, seller)
        if result:
            blacklist_observed.set()
        return result

    monkeypatch.setattr(p, "_render_payload", fake_render)
    monkeypatch.setattr(p, "_send_to", fail_send)
    monkeypatch.setattr(p, "_requeue_after", add_blacklist_during_retry)
    monkeypatch.setattr(store, "is_blacklisted", tracked_is_blacklisted)

    _enqueue(p, GROUP, card)
    task = asyncio.create_task(p._send_loop(p.queue, lambda: 0.0))
    await asyncio.wait_for(blacklist_observed.wait(), 2)

    assert send_attempts == 1
    assert rendered_payloads == [card]
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await p.wfm.close()
    store.close()


async def test_send_retry_wait_does_not_block_later_messages(
        tmp_path, monkeypatch):
    """网络错误的延迟重试独立等待，发送循环可继续处理后续消息。"""
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=False)
    monkeypatch.setattr(p, "_bot_connected", lambda: True)

    retry_waiting = asyncio.Event()
    release_retry = asyncio.Event()
    second_sent = asyncio.Event()
    first_retried = asyncio.Event()
    attempts: list[str] = []

    async def controlled_requeue(item, delay):
        assert delay == 2.0
        retry_waiting.set()
        await release_retry.wait()
        p.enqueue_delivery(item)

    async def send(_target, message):
        attempts.append(message)
        if message == "first" and attempts.count("first") == 1:
            raise poller_mod.NetworkError("temporary failure")
        if message == "second":
            second_sent.set()
        elif message == "first":
            first_retried.set()

    monkeypatch.setattr(p, "_requeue_after", controlled_requeue)
    monkeypatch.setattr(p, "_send_to", send)
    now = time.time()
    _enqueue(p, GROUP, "first", now=now)
    _enqueue(p, GROUP, "second", now=now)
    task = asyncio.create_task(p._send_loop(p.queue, lambda: 0.0))

    await asyncio.wait_for(retry_waiting.wait(), 2)
    await asyncio.wait_for(second_sent.wait(), 2)
    assert attempts == ["first", "second"]
    release_retry.set()
    await asyncio.wait_for(first_retried.wait(), 2)
    assert attempts == ["first", "second", "first"]

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await p.wfm.close()
    store.close()


async def test_send_loop_does_not_retry_permanent_errors(tmp_path, monkeypatch):
    """非网络异常立即记失败，避免重复发送确定无法成功的消息。"""
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=False)
    monkeypatch.setattr(p, "_bot_connected", lambda: True)
    attempted = asyncio.Event()
    attempts = 0

    async def fail_permanently(_target, _message):
        nonlocal attempts
        attempts += 1
        attempted.set()
        raise RuntimeError("permanent failure")

    monkeypatch.setattr(p, "_send_to", fail_permanently)
    failures_before = STATS.push_failures_today
    _enqueue(p, GROUP, "bad message")
    task = asyncio.create_task(p._send_loop(p.queue, lambda: 0.0))
    await asyncio.wait_for(attempted.wait(), 2)
    await asyncio.sleep(0)

    assert attempts == 1
    assert not p._retry_tasks
    assert STATS.push_failures_today == failures_before + 1

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await p.wfm.close()
    store.close()


async def test_send_loop_does_not_retry_unknown_snowluma_outcome(
        tmp_path, monkeypatch):
    """HTTP action 可能已送达时不补发，避免制造重复 QQ 消息。"""
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=False)
    monkeypatch.setattr(p, "_bot_connected", lambda: True)
    attempted = asyncio.Event()
    attempts = 0

    async def unknown(_target, _message):
        nonlocal attempts
        attempts += 1
        attempted.set()
        raise poller_mod.SnowLumaOutcomeUnknown("response lost")

    monkeypatch.setattr(p, "_send_to", unknown)
    _enqueue(p, GROUP, "maybe sent")
    task = asyncio.create_task(p._send_loop(p.queue, lambda: 0.0))
    await asyncio.wait_for(attempted.wait(), 2)
    await asyncio.sleep(0)

    assert attempts == 1
    assert not p._retry_tasks
    counters = p.delivery_status["counters_by_source"]["system"]
    assert counters["outcome_unknown"] == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await p.wfm.close()
    store.close()


async def test_send_loop_retries_discord_rate_limit(tmp_path, monkeypatch):
    """Discord 429 属于可重试结果，不能按永久错误立即丢弃。"""
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=False)
    p.config.send_max_retries = 0
    monkeypatch.setattr(p, "_bot_connected", lambda: True)

    class FakeRateLimit(Exception):
        retry_after = 3.5
        global_rate_limit = False

    attempted = asyncio.Event()
    retry_waiting = asyncio.Event()
    retry_delays = []

    async def controlled_requeue(_item, delay):
        retry_delays.append(delay)
        retry_waiting.set()
        await asyncio.Event().wait()

    async def rate_limited(_target, _message):
        attempted.set()
        raise FakeRateLimit()

    monkeypatch.setattr(
        poller_mod, "DiscordRateLimitException", FakeRateLimit)
    monkeypatch.setattr(p, "_send_to", rate_limited)
    monkeypatch.setattr(p, "_requeue_after", controlled_requeue)
    _enqueue(p, GROUP, "limited")
    task = asyncio.create_task(p._send_loop(p.queue, lambda: 0.0))
    await asyncio.wait_for(attempted.wait(), 2)
    await asyncio.wait_for(retry_waiting.wait(), 2)

    counters = p.delivery_status["counters_by_source"]["system"]
    assert counters["rate_limited"] == 1
    assert counters["retry_scheduled"] == 1
    assert len(p._retry_tasks) == 1
    assert retry_delays == [3.5]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await p.stop()
    store.close()


async def test_global_discord_rate_limit_delays_other_targets(tmp_path):
    store = _tmp_store(tmp_path)
    first = store.upsert_discord_target("2001")
    second = store.upsert_discord_target("2002")
    p = _poller(store, dry_run=False)

    class GlobalRateLimit(Exception):
        global_rate_limit = True

    loop = asyncio.get_running_loop()
    p._note_discord_rate_limit(
        first["scope_id"], GlobalRateLimit(), 0.03)
    started = loop.time()

    await p._wait_for_discord_rate_limit(second["scope_id"])

    assert loop.time() - started >= 0.02
    await p.wfm.close()
    store.close()


def test_rate_limit_retry_does_not_outlive_message_ttl(tmp_path):
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=False)
    now = time.time()
    item = DeliveryItem(
        DeliverySource.WM, GROUP, "message", 0,
        now, now + 0.1)

    assert p._schedule_rate_limit_retry(
        item, item.payload, item.payload, 0.2) is False
    assert not p._retry_tasks
    assert p.delivery_status["counters_by_source"]["wm"]["expired"] == 1
    store.close()


async def test_cancel_during_send_does_not_requeue_unknown_result(
        tmp_path, monkeypatch):
    """API 调用开始后的取消结果未知，不得作为流水线故障自动重试。"""
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=False)
    monkeypatch.setattr(p, "_target_bot_connected", lambda _target: True)
    send_started = asyncio.Event()

    async def send(_target, _message):
        send_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(p, "_send_to", send)
    _enqueue(p, GROUP, "unknown result")
    task = asyncio.create_task(p._send_loop(p.queue, lambda: 0.0))
    await asyncio.wait_for(send_started.wait(), 2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert p.queue_depth == 0
    assert not p._retry_tasks
    counters = p.delivery_status["counters_by_source"]["system"]
    assert counters["send_cancelled"] == 1
    await p.wfm.close()
    store.close()


async def test_send_loop_drops_messages_while_bot_offline(tmp_path, monkeypatch):
    """Bot 掉线不是发送失败：消息立即丢弃且不安排重试。"""
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=False)
    offline_waiting = asyncio.Event()
    rendered = asyncio.Event()

    def disconnected(_target):
        offline_waiting.set()
        return False

    async def render(_payload):
        rendered.set()
        return "rendered"

    monkeypatch.setattr(p, "_target_bot_connected", disconnected)
    monkeypatch.setattr(p, "_render_payload", render)

    card = _HitCard({"id": 1}, AUCTION)
    _enqueue(p, GROUP, card, attempts=2)
    task = asyncio.create_task(p._send_loop(p.queue, lambda: 0.0))
    await asyncio.wait_for(offline_waiting.wait(), 2)
    assert rendered.is_set()
    for _ in range(20):
        if p.queue_depth == 0:
            break
        await asyncio.sleep(0)
    assert p.queue_depth == 0
    assert p.delivery_status["counters_by_source"]["system"]["bot_offline"] == 1

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await p.wfm.close()
    store.close()


async def test_offline_target_does_not_block_other_main_targets(
        tmp_path, monkeypatch):
    """离线目标等待恢复时，在线 QQ 目标仍应立即通过流水线。"""
    store = _tmp_store(tmp_path)
    discord = store.upsert_discord_target("1001")
    offline_scope = discord["scope_id"]
    p = _poller(store, dry_run=False)

    offline_waiting = asyncio.Event()
    group_sent = asyncio.Event()
    sent = []

    def target_connected(target):
        if target == offline_scope:
            offline_waiting.set()
            return False
        return True

    async def send(target, message):
        sent.append((target, message))
        group_sent.set()

    monkeypatch.setattr(p, "_target_bot_connected", target_connected)
    monkeypatch.setattr(p, "_send_to", send)
    now = time.time()
    _enqueue(p, offline_scope, "offline", now=now)
    _enqueue(p, GROUP, "online", now=now)
    task = asyncio.create_task(p._send_loop(p.queue, lambda: 0.0))

    await asyncio.wait_for(offline_waiting.wait(), 2)
    await asyncio.wait_for(group_sent.wait(), 2)
    assert sent == [(GROUP, "online")]

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await p.stop()
    store.close()


async def test_discord_offline_message_waits_for_ready_or_resumed(
        tmp_path, monkeypatch):
    """on_bot_connect 不提前放行；Gateway 就绪后恢复原消息。"""
    store = _tmp_store(tmp_path)
    target = store.upsert_discord_target("1001")
    p = _poller(store, dry_run=False)
    bot = _DiscordBot()
    monkeypatch.setattr(
        poller_mod.nonebot, "get_bots", lambda: {"discord": bot},
        raising=False,
    )

    async def skip_prewarm(_bot):
        return None

    monkeypatch.setattr(p, "_prewarm_discord_dms", skip_prewarm)
    sent = []
    sent_event = asyncio.Event()

    async def send(target_id, message):
        sent.append((target_id, message))
        sent_event.set()

    monkeypatch.setattr(p, "_send_to", send)
    p.discord_bot_connected(bot)
    _enqueue(p, target["scope_id"], "held message")
    task = asyncio.create_task(p._send_loop(p.queue, lambda: 0.0))

    for _ in range(100):
        counters = p.delivery_status["counters_by_source"]["system"]
        if counters.get("offline_deferred") == 1:
            break
        await asyncio.sleep(0)
    assert sent == []
    assert len(p._discord_deferred) == 1
    assert not p._discord_ready_event.is_set()

    p.discord_session_ready(bot)
    await asyncio.wait_for(sent_event.wait(), 2)

    assert sent == [(target["scope_id"], "held message")]
    counters = p.delivery_status["counters_by_source"]["system"]
    assert counters["offline_deferred"] == 1
    assert counters["offline_recovered"] == 1
    assert counters.get("bot_offline", 0) == 0

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await p.stop()
    store.close()


async def test_discord_offline_grace_does_not_extend_past_sixty_seconds(
        tmp_path):
    store = _tmp_store(tmp_path)
    target = store.upsert_discord_target("1001")
    p = _poller(store, dry_run=False)
    p._discord_offline_since = time.time() - 61.0
    item = p.new_delivery(
        DeliverySource.SYSTEM, target["scope_id"], "too late")

    assert p._defer_discord_delivery(item) is True
    assert not p._discord_deferred
    counters = p.delivery_status["counters_by_source"]["system"]
    assert counters["offline_expired"] == 1
    assert counters["expired"] == 1

    await p.stop()
    store.close()


async def test_discord_offline_buffer_is_bounded(tmp_path):
    store = _tmp_store(tmp_path)
    target = store.upsert_discord_target("1001")
    p = _poller(store, dry_run=False)
    p.config.send_queue_maxsize = 2
    p.queue = asyncio.Queue(maxsize=2)

    for index in range(3):
        item = p.new_delivery(
            DeliverySource.SYSTEM,
            target["scope_id"],
            f"held-{index}",
        )
        assert p._defer_discord_delivery(item) is True

    assert [entry.item.payload for entry in p._discord_deferred] == [
        "held-1", "held-2"]
    counters = p.delivery_status["counters_by_source"]["system"]
    assert counters["offline_deferred"] == 3
    assert counters["offline_evicted_oldest"] == 1

    await p.stop()
    store.close()


async def test_discord_recovery_keeps_fresh_queue_and_newest_held_message(
        tmp_path):
    """断线恢复不能用旧暂存消息淘汰已经排队的新消息。"""
    store = _tmp_store(tmp_path)
    target = store.upsert_discord_target("1001")
    p = _poller(store, dry_run=False)
    p.config.send_queue_maxsize = 3
    p.queue = asyncio.Queue(maxsize=3)

    for payload in ("held-old", "held-middle", "held-new"):
        item = p.new_delivery(
            DeliverySource.SYSTEM, target["scope_id"], payload)
        assert p._defer_discord_delivery(item) is True
    stale = p.new_delivery(
        DeliverySource.SYSTEM, GROUP, "stale", now=time.time() - 120)
    p.queue.put_nowait(stale)
    live = p.new_delivery(DeliverySource.SYSTEM, GROUP, "live")
    assert p.enqueue_delivery(live)

    p._discord_ready_event.set()
    await asyncio.wait_for(p._discord_deferred_task, 2)

    queued = [p.queue.get_nowait() for _ in range(p.queue.qsize())]
    assert [item.payload for item in queued] == [
        "live", "held-middle", "held-new"]
    for _ in queued:
        p.queue.task_done()
    counters = p.delivery_status["counters_by_source"]["system"]
    assert counters["offline_recovered"] == 2
    assert counters["offline_recovery_queue_full"] == 1
    assert counters["expired"] == 1
    assert counters.get("evicted_oldest", 0) == 0

    await p.stop()
    store.close()


async def test_offline_target_does_not_replay_after_reconnect(
        tmp_path, monkeypatch):
    """离线期间丢弃的消息在目标重连后不得补发。"""
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=False)
    online = False
    offline_waiting = asyncio.Event()
    sent = []

    def target_connected(_target):
        if not online:
            offline_waiting.set()
        return online

    async def send(_target, message):
        sent.append(message)

    monkeypatch.setattr(p, "_target_bot_connected", target_connected)
    monkeypatch.setattr(p, "_send_to", send)
    now = time.time()
    _enqueue(p, GROUP, "first", now=now)
    _enqueue(p, GROUP, "second", now=now)
    task = asyncio.create_task(p._send_loop(p.queue, lambda: 0.0))

    await asyncio.wait_for(offline_waiting.wait(), 2)
    for _ in range(20):
        if p.queue_depth == 0:
            break
        await asyncio.sleep(0)
    online = True
    await asyncio.sleep(0.02)
    assert sent == []

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await p.wfm.close()
    store.close()


def test_grading_orders_negatives_last():
    """评分结果正词条在前、负词条在后（保证卡图/文字负词条置底）。"""
    from src.plugins.riven_sniper.grading import grade_auction_item
    item = {"weapon_url_name": "nami_solo", "attributes": [
        {"url_name": "critical_chance_on_slide_attack", "value": -100.0, "positive": False},
        {"url_name": "critical_damage", "value": 100.0, "positive": True},
        {"url_name": "base_damage_/_melee_damage", "value": 100.0, "positive": True},
    ]}
    stats = grade_auction_item(item).stats
    flags = [s.positive for s in stats]
    assert flags == sorted(flags, reverse=True)  # 正(True)在前、负(False)在后
    assert stats[-1].positive is False


async def test_bot_connected_reflects_account_online_heartbeat(tmp_path, monkeypatch):
    """WS 连着≠可发送：心跳 online=false 时判为不可发送，陈旧离线判定回落连接态。"""
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=False)
    monkeypatch.setattr(poller_mod.nonebot, "get_bots",
                        lambda: {"1": _onebot_stub()}, raising=False)

    # 尚无心跳：按连接态处理（可发送）
    monkeypatch.setattr(poller_mod.shared, "_bot_online", None)
    monkeypatch.setattr(poller_mod.shared, "_bot_online_ts", 0.0)
    assert p._bot_connected() is True

    # 心跳在线
    monkeypatch.setattr(poller_mod.shared, "_bot_online", True)
    monkeypatch.setattr(poller_mod.shared, "_bot_online_ts", time.time())
    assert p._bot_connected() is True

    # 心跳离线且新鲜：判为不可发送
    monkeypatch.setattr(poller_mod.shared, "_bot_online", False)
    monkeypatch.setattr(poller_mod.shared, "_bot_online_ts", time.time())
    assert p._bot_connected() is False

    # 心跳离线但陈旧（超过 _HEARTBEAT_STALE，如 SnowLuma 停止发心跳）：回落连接态
    monkeypatch.setattr(poller_mod.shared, "_bot_online_ts",
                        time.time() - p._HEARTBEAT_STALE - 1)
    assert p._bot_connected() is True

    # WS 断开：直接不可发送
    monkeypatch.setattr(poller_mod.nonebot, "get_bots", lambda: {}, raising=False)
    assert p._bot_connected() is False
    await p.wfm.close()
    store.close()


async def test_send_loop_drops_when_account_offline(tmp_path, monkeypatch):
    """账号被踢下线时直接丢弃，不把未调用 API 计为重试。"""
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=False)
    monkeypatch.setattr(poller_mod.nonebot, "get_bots",
                        lambda: {"1": _onebot_stub()}, raising=False)
    monkeypatch.setattr(poller_mod.shared, "_bot_online", False)
    monkeypatch.setattr(poller_mod.shared, "_bot_online_ts", time.time())

    offline_waiting = asyncio.Event()
    original_connected = p._target_bot_connected

    def tracked_connected(target):
        connected = original_connected(target)
        if not connected:
            offline_waiting.set()
        return connected

    monkeypatch.setattr(p, "_target_bot_connected", tracked_connected)

    card = _HitCard({"id": 1}, AUCTION)
    _enqueue(p, GROUP, card, attempts=1)
    task = asyncio.create_task(p._send_loop(p.queue, lambda: 0.0))
    await asyncio.wait_for(offline_waiting.wait(), 2)
    for _ in range(20):
        if p.queue_depth == 0:
            break
        await asyncio.sleep(0)
    assert p.queue_depth == 0

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await p.wfm.close()
    store.close()


async def test_enqueue_rejects_expired_message(tmp_path, monkeypatch):
    """TTL 已过的消息在进入发送流水线前直接丢弃。"""
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=False)
    now = time.time()
    item = DeliveryItem(
        DeliverySource.SYSTEM, GROUP, "过期消息", 0,
        now - 10, now - 1)
    assert p.enqueue_delivery(item) is False
    assert p.queue.empty()
    assert p.delivery_status["counters_by_source"]["system"]["expired"] == 1
    await p.wfm.close()
    store.close()


class _DiscordAdapter:
    @staticmethod
    def get_name():
        return "Discord"


class _DiscordBot:
    adapter = _DiscordAdapter()

    def __init__(self):
        self.calls = []

    async def create_DM(self, recipient_id):
        self.calls.append(("dm", recipient_id))
        return types.SimpleNamespace(id=700)

    async def send_to(self, channel_id, message):
        self.calls.append(("send", channel_id, message))

    async def create_message(self, **params):
        self.calls.append(("create", params))


async def test_authorized_discord_scope_is_polled_and_routed(tmp_path, monkeypatch):
    store = Store(tmp_path / "discord.db")
    target = store.upsert_discord_target("1001")
    store.add_config(
        target["scope_id"], weapon=None, wildcard="all",
        positives=[["base_damage_/_melee_damage"], ["critical_damage"]],
        negatives=[[ANY_ATTRIBUTE]],
    )
    p = _poller(store, dry_run=False)
    await p._poll_once()
    item = p.queue.get_nowait()
    scope_id, payload = item.target, item.payload
    assert scope_id == target["scope_id"]
    assert isinstance(payload, _HitCard)

    message = await p._render_payload(payload)
    segments = list(message)
    assert [segment.type for segment in segments] == ["embed"]
    embed = segments[0].data["embed"]
    assert embed.author.name == "WARFRAME.MARKET · RIVEN"
    assert embed.title == "海波单剑 Visi-loctitis · 150p"

    bot = _DiscordBot()
    monkeypatch.setattr(
        poller_mod.nonebot, "get_bots", lambda: {"discord": bot},
        raising=False)
    await p._send_to(scope_id, message)
    await p._send_to(scope_id, message)
    assert bot.calls[0] == ("dm", 1001)
    assert bot.calls[1][0:2] == ("send", 700)
    assert bot.calls[2][0:2] == ("send", 700)
    assert [call[0] for call in bot.calls].count("dm") == 1

    p.discord_bot_disconnected(bot)
    await p._send_to(scope_id, message)
    assert [call[0] for call in bot.calls].count("dm") == 2
    await p.wfm.close()
    store.close()


async def test_discord_wm_hits_from_same_poll_are_sent_as_one_message(
        tmp_path, monkeypatch):
    store = Store(tmp_path / "discord-batch.db")
    target = store.upsert_discord_target("1010")
    store.add_config(
        target["scope_id"], weapon=None, wildcard="all",
        positives=[["base_damage_/_melee_damage"], ["critical_damage"]],
        negatives=[[ANY_ATTRIBUTE]],
    )
    auctions = [
        {**AUCTION, "id": "batch-1", "buyout_price": 150},
        {**AUCTION, "id": "batch-2", "buyout_price": 175},
    ]
    p = _poller(store, dry_run=False)

    async def recent():
        return auctions

    p.wfm.recent_auctions = recent
    await p._poll_once()

    assert p.queue.qsize() == 1
    item = p.queue.get_nowait()
    assert item.target == target["scope_id"]
    assert isinstance(item.payload, _HitBatch)
    assert [card.auction["id"] for card in item.payload.cards] == [
        "batch-1", "batch-2"]
    counters = p.delivery_status["counters_by_source"]["wm"]
    assert counters["logical_items"] == 2
    assert counters["batches"] == 1
    assert counters["api_requests_saved"] == 1

    message = await p._render_payload(item.payload)
    assert [segment.type for segment in message] == ["embed", "embed"]
    assert [segment.data["embed"].title for segment in message] == [
        "海波单剑 Visi-loctitis · 150p",
        "海波单剑 Visi-loctitis · 175p",
    ]

    bot = _DiscordBot()
    monkeypatch.setattr(
        poller_mod.nonebot, "get_bots", lambda: {"discord": bot},
        raising=False)
    await p._send_to(
        item.target, message, nonce=item.payload.nonce)
    assert [call[0] for call in bot.calls] == ["dm", "create"]
    request = bot.calls[1][1]
    assert request["nonce"] == item.payload.nonce
    assert request["enforce_nonce"] is True
    assert len(request["embeds"]) == 2
    await p.wfm.close()
    store.close()


def test_discord_wm_batch_packing_obeys_embed_limits(monkeypatch):
    cards = [
        _HitCard(
            {"group_id": -1}, {**AUCTION, "id": f"batch-{index}"})
        for index in range(11)
    ]
    monkeypatch.setattr(
        poller_mod, "hit_discord_embed_character_count",
        lambda *_args, **_kwargs: 600)

    batches = SniperPoller._pack_discord_hit_cards(cards)

    assert [len(batch) for batch in batches] == [10, 1]


async def test_discord_wm_batch_preflight_failure_is_isolated(
        tmp_path, monkeypatch):
    store = Store(tmp_path / "discord-batch-isolation.db")
    target = store.upsert_discord_target("1012")
    store.add_config(
        target["scope_id"], weapon=None, wildcard="all",
        positives=[["base_damage_/_melee_damage"], ["critical_damage"]],
        negatives=[[ANY_ATTRIBUTE]],
    )
    auctions = [
        {**AUCTION, "id": "good-before"},
        {**AUCTION, "id": "bad"},
        {**AUCTION, "id": "good-after"},
    ]
    p = _poller(store, dry_run=False)

    async def recent():
        return auctions

    def character_count(_config, auction, *_args, **_kwargs):
        if auction["id"] == "bad":
            raise ValueError("injected preflight failure")
        return 600

    p.wfm.recent_auctions = recent
    monkeypatch.setattr(
        poller_mod, "hit_discord_embed_character_count", character_count)

    await p._poll_once()

    queued = p.queued_deliveries
    assert [item.payload.auction["id"] for item in queued] == [
        "good-before", "bad", "good-after"]
    assert all(isinstance(item.payload, _HitCard) for item in queued)
    assert store.mark_seen([auction["id"] for auction in auctions]) == []
    await p.wfm.close()
    store.close()


def test_discord_wm_batch_packing_is_valid_in_both_locales():
    cards = [
        _HitCard(
            {"group_id": -1}, {**AUCTION, "id": f"actual-{index}"})
        for index in range(11)
    ]

    batches = SniperPoller._pack_discord_hit_cards(cards)

    assert sum(map(len, batches)) == len(cards)
    for locale in ("zh", "en"):
        for batch in batches:
            entries = tuple(
                (card.config, card.auction, card.extra_count)
                for card in batch
            )
            message, _ = poller_mod.build_hit_discord_rich_batch(
                entries, locale)
            assert len(message["embed"]) == len(batch)


async def test_qq_wm_hits_from_same_poll_share_one_action(tmp_path):
    store = _tmp_store(tmp_path)
    p = _poller(store, dry_run=False)

    async def recent():
        return [
            {**AUCTION, "id": f"qq-{index}"}
            for index in range(1, 6)
        ]

    p.wfm.recent_auctions = recent
    await p._poll_once()

    assert p.queue.qsize() == 2
    items = p.queued_deliveries
    assert isinstance(items[0].payload, _HitBatch)
    assert [card.auction["id"] for card in items[0].payload.cards] == [
        "qq-1", "qq-2", "qq-3", "qq-4"]
    assert isinstance(items[1].payload, _HitCard)
    assert items[1].payload.auction["id"] == "qq-5"
    message = await p._render_payload(items[0].payload)
    assert sum(segment.type == "image" for segment in message) == 4
    counters = p.delivery_status["counters_by_source"]["wm"]
    assert counters["logical_items"] == 5
    assert counters["api_requests_saved"] == 3
    await p.wfm.close()
    store.close()


async def test_discord_wm_batch_removes_newly_blacklisted_entry(tmp_path):
    store = Store(tmp_path / "discord-batch-blacklist.db")
    target = store.upsert_discord_target("1011")
    p = _poller(store, dry_run=False)
    batch = _HitBatch((
        _HitCard(
            {"group_id": target["scope_id"]},
            {**AUCTION, "id": "blocked"}),
        _HitCard(
            {"group_id": target["scope_id"]},
            {
                **AUCTION,
                "id": "allowed",
                "owner": {
                    **AUCTION["owner"], "ingame_name": "OtherSeller"},
            }),
    ))
    item = p.new_delivery(DeliverySource.WM, target["scope_id"], batch)
    await p._render_payload(batch)
    assert batch.rendered is not None
    store.add_blacklist(target["scope_id"], "Seller")

    assert await p._queue_item_is_ready(
        p.queue, item, after_render=True) is True
    assert [card.auction["id"] for card in batch.cards] == ["allowed"]
    assert batch.rendered is None
    await p.wfm.close()
    store.close()


async def test_stale_discord_dm_cache_recreates_unknown_channel(
        tmp_path, monkeypatch):
    store = Store(tmp_path / "discord-stale-channel.db")
    target = store.upsert_discord_target("1005")
    p = _poller(store, dry_run=False)

    class UnknownChannel(Exception):
        code = p._DISCORD_UNKNOWN_CHANNEL

    class StaleChannelBot(_DiscordBot):
        async def create_DM(self, recipient_id):
            channel_id = 700 + sum(call[0] == "dm" for call in self.calls)
            self.calls.append(("dm", recipient_id))
            return types.SimpleNamespace(id=channel_id)

        async def send_to(self, channel_id, message):
            self.calls.append(("send", channel_id, message))
            if channel_id == 700:
                raise UnknownChannel()

    bot = StaleChannelBot()
    monkeypatch.setattr(poller_mod, "DiscordActionFailed", UnknownChannel)
    monkeypatch.setattr(
        poller_mod.nonebot, "get_bots", lambda: {"discord": bot},
        raising=False)

    await p._send_to(target["scope_id"], "message")

    assert bot.calls == [
        ("dm", 1005),
        ("send", 700, "message"),
        ("dm", 1005),
        ("send", 701, "message"),
    ]
    await p.wfm.close()
    store.close()


async def test_authorized_discord_channel_uses_rich_embed(
        tmp_path, monkeypatch):
    store = Store(tmp_path / "discord-channel.db")
    target = store.upsert_discord_target("1001")
    p = _poller(store, dry_run=False)
    decoded = {
        "weapon_slug": "torid",
        "riven_name": "toxi-acrican",
        "lvl_req": 15,
        "lvl": 8,
        "rerolls": 1,
        "polarity": "madurai",
        "polarity_mark": "V",
        "stats": [{
            "ref": "WeaponCritDamageMod",
            "display": 155.1,
            "roll": 0.8,
            "grade": "A",
            "is_curse": False,
        }],
    }
    payload = ChannelPushPayload(
        seller="Seller",
        channel="#T_ZH",
        cards=(ChannelCard(decoded, 1234),),
        locale="zh",
        target_scope=target["scope_id"],
    )

    def unexpected_tracking_query(*_args, **_kwargs):
        raise AssertionError("频道 Rich Embed 渲染/发送不应查询追踪库")

    async def unexpected_market_query(*_args, **_kwargs):
        raise AssertionError("频道 Rich Embed 渲染/发送不应请求 WM")

    monkeypatch.setattr(
        TrackingStore, "player_report", unexpected_tracking_query)
    monkeypatch.setattr(
        TrackingStore, "_count_player_rivens", unexpected_tracking_query)
    monkeypatch.setattr(p.wfm, "riven_search", unexpected_market_query)

    message = await p._render_payload(payload)

    segments = list(message)
    assert [segment.type for segment in segments] == ["embed"]
    embed = segments[0].data["embed"]
    assert not embed.title
    assert not embed.author
    assert "**#1234 [托里德 Toxi-acrican](" in embed.description
    assert "历史紫卡数量未知" in embed.description
    assert "[#1234 托里德 Toxi-acrican]" not in embed.description
    assert "[托里德 Toxi-acrican]" in embed.description
    bot = _DiscordBot()
    monkeypatch.setattr(
        poller_mod.nonebot, "get_bots", lambda: {"discord": bot},
        raising=False,
    )
    await p._send_to(target["scope_id"], message)
    assert [call[0] for call in bot.calls] == ["dm", "send"]
    await p.wfm.close()
    store.close()


async def test_only_discord_bargain_payloads_use_rich_embed(tmp_path):
    store = Store(tmp_path / "discord-bargain.db")
    target = store.upsert_discord_target("1001")
    store.upsert_qq_target(GROUP, 42, enabled=True)
    p = _poller(store, dry_run=False)
    hit = bargain.Hit(
        Decimal("60"), Decimal("100"), Decimal("0.4"), samples=3)

    discord_payload = bargain.BargainRivenPushPayload(
        weapon_slug="nami_solo",
        auction=AUCTION,
        hit=hit,
        locale="zh",
        target_scope=target["scope_id"],
    )
    discord_message = await p._render_payload(discord_payload)
    discord_embed = list(discord_message)[0].data["embed"]
    assert discord_embed.color == 0xED4245
    assert "低于参考价: 40%" in discord_embed.description

    qq_payload = bargain.BargainRivenPushPayload(
        weapon_slug="nami_solo",
        auction=AUCTION,
        hit=hit,
        locale="zh",
        target_scope=GROUP,
    )
    qq_message = await p._render_payload(qq_payload)
    assert isinstance(qq_message, str)
    assert qq_message.startswith("紫卡低价挂单\n")
    assert "低于参考价：40%" in qq_message

    order = {
        "id": "order-1",
        "platinum": 45,
        "quantity": 2,
        "rank": 5,
        "user": {"ingameName": "Seller", "status": "ingame"},
    }
    discord_item_payload = bargain.BargainItemPushPayload(
        slug="arcane_grace",
        order=order,
        hit=hit,
        locale="zh",
        target_scope=target["scope_id"],
    )
    discord_item_message = await p._render_payload(discord_item_payload)
    discord_item_embed = list(discord_item_message)[0].data["embed"]
    assert discord_item_embed.color == 0xED4245
    assert "url" not in discord_item_embed.model_dump(
        exclude_none=True, exclude_unset=True)
    assert "低于参考价: 40%" in discord_item_embed.description

    qq_item_payload = bargain.BargainItemPushPayload(
        slug="arcane_grace",
        order=order,
        hit=hit,
        locale="zh",
        target_scope=GROUP,
    )
    qq_item_message = await p._render_payload(qq_item_payload)
    assert isinstance(qq_item_message, str)
    assert qq_item_message.startswith("低价挂单\n")
    assert "低于参考价：40%" in qq_item_message

    await p.wfm.close()
    store.close()


async def test_revoked_discord_scope_is_not_polled(tmp_path):
    store = Store(tmp_path / "discord-revoked.db")
    target = store.upsert_discord_target("1002")
    store.add_config(
        target["scope_id"], weapon=None, wildcard="all",
        positives=[["base_damage_/_melee_damage"], ["critical_damage"]],
        negatives=[[ANY_ATTRIBUTE]],
    )
    store.set_target_enabled(target["scope_id"], False)
    p = _poller(store, dry_run=False)
    await p._poll_once()
    assert p.queue.empty()
    await p.wfm.close()
    store.close()


async def test_discord_scope_is_not_polled_when_feature_disabled(tmp_path):
    store = Store(tmp_path / "discord-disabled.db")
    target = store.upsert_discord_target("1003")
    store.add_config(
        target["scope_id"], weapon=None, wildcard="all",
        positives=[["base_damage_/_melee_damage"], ["critical_damage"]],
        negatives=[[ANY_ATTRIBUTE]],
    )
    p = _poller(store, dry_run=False)
    p.config.discord_dm_enabled = False
    await p._poll_once()
    assert p.queue.empty()
    await p.wfm.close()
    store.close()


def test_queued_target_validity_tracks_database_state_and_platform_switch(tmp_path):
    store = Store(tmp_path / "target-validity.db")
    target = store.upsert_discord_target("1004")
    p = _poller(store, dry_run=False)

    assert p._target_is_active(GROUP) is True
    assert p._target_is_active(("group", GROUP)) is True
    assert p._target_is_active(("private", 999)) is True
    assert p._target_is_active(target["scope_id"]) is True

    store.set_target_enabled(GROUP, False)
    p.config.discord_dm_enabled = False
    assert p._target_is_active(GROUP) is False
    assert p._target_is_active(("group", GROUP)) is False
    assert p._target_is_active(target["scope_id"]) is False
    store.close()


async def test_queued_channel_delivery_rechecks_target_controls(tmp_path):
    store = Store(tmp_path / "channel-permission.db")
    p = _poller(store, dry_run=False)
    p.activate_source(DeliverySource.IRC, "run-a")
    store.set_target_channel_enabled(GROUP, True)
    store.upsert_qq_target(GROUP, 42, enabled=True)
    config_id = store.add_config(
        GROUP, weapon="torid", wildcard=None,
        positives=[["critical_damage"], ["multishot"]], negatives=[],
    )
    payload = ChannelPushPayload(
        seller="Seller", channel="#T_ZH", cards=(ChannelCard({
            "weapon_slug": "torid", "rerolls": 1,
            "stats": [
                {"ref": "WeaponCritDamageMod", "is_curse": False},
                {"ref": "WeaponFireIterationsMod", "is_curse": False},
            ],
        }, 1),), locale="zh",
        target_scope=GROUP,
    )
    item = p.new_delivery(
        DeliverySource.IRC, GROUP, payload, generation="run-a")

    assert await p._queue_item_is_ready(
        p.queue, item, after_render=False) is True
    store.add_blacklist(GROUP, "Seller", scope="wm")
    assert await p._queue_item_is_ready(
        p.queue, item, after_render=True) is True
    store.add_blacklist(GROUP, "Seller", scope="channel")
    assert await p._queue_item_is_ready(
        p.queue, item, after_render=True) is False
    store.remove_blacklist(GROUP, "Seller", scope="channel")

    store.set_target_channel_enabled(GROUP, False)
    assert await p._queue_item_is_ready(
        p.queue, item, after_render=True) is False

    store.set_target_channel_enabled(GROUP, True)
    assert store.delete_config(config_id, GROUP) is True
    mismatch_id = store.add_config(
        GROUP, weapon="rubico", wildcard=None,
        positives=[["critical_damage"], ["multishot"]], negatives=[],
    )
    assert await p._queue_item_is_ready(
        p.queue, item, after_render=True) is False

    assert store.delete_config(mismatch_id, GROUP) is True
    assert await p._queue_item_is_ready(
        p.queue, item, after_render=True) is False

    await p.wfm.close()
    store.close()
