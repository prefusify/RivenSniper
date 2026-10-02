"""WFM 轮询 + 匹配 + 限速推送队列。"""

from __future__ import annotations

import asyncio
import heapq
import secrets
import time
from collections import Counter, deque
from dataclasses import dataclass, field

import httpx
import nonebot
from nonebot import logger
from nonebot.adapters.discord.exception import (
    ActionFailed as DiscordActionFailed,
    RateLimitException as DiscordRateLimitException,
)
from nonebot.exception import NetworkError
from src.snowluma_adapter import SnowLumaOutcomeUnknown

from . import bargain, matcher, shared
from .delivery import DeliveryItem, DeliverySource
from .channel_push import (
    ChannelPushPayload, build_channel_discord_rich, build_channel_qq,
)
from .formatter import (
    DISCORD_MESSAGE_MAX_EMBEDS,
    DISCORD_MESSAGE_MAX_EMBED_CHARACTERS,
    build_bargain_item_discord_rich,
    build_bargain_riven_discord_rich,
    build_hit_discord_rich,
    build_hit_discord_rich_batch,
    build_hit_qq_batch,
    build_hit_push,
    format_hit,
    hit_discord_embed_character_count,
)
from .grading import GradeResult, grade_auction_item
from .stats import STATS
from .runtime_info import runtime_identity
from .store import Store
from .wfm import WfmClient
from .wfm_fast import FastRivenPoller, subscription_token

_PRUNE_INTERVAL = 6 * 3600


def _send_delay(interval: float) -> float:
    """返回发送成功或失败后的可选固定间隔；0/负数表示不额外等待。"""
    return max(0.0, float(interval or 0.0))


@dataclass
class _HitCard:
    """待渲染的命中卡：渲染推迟到预渲染线程，避免 Pillow 阻塞事件循环。"""
    config: dict
    auction: dict
    extra_count: int = 0  # 同挂单额外命中的配置数（标题标注"等N条"）
    locale: str = "zh"
    grade_result: GradeResult | None = field(
        default=None, repr=False, compare=False)
    # 发送失败后保留成品，既避免重渲染，也让重试时仍可从 auction 复核卖家黑名单。
    rendered: object | None = field(default=None, repr=False, compare=False)


@dataclass
class _HitBatch:
    """同一轮、同一真实目标的多条 WM 命中。"""
    cards: tuple[_HitCard, ...]
    locale: str = "zh"
    rendered: object | None = field(default=None, repr=False, compare=False)
    nonce: str = field(
        default_factory=lambda: secrets.token_hex(12), repr=False,
        compare=False)


@dataclass
class _PreparedPush:
    """已完成预渲染、等待按目标串行发送的队列项。"""
    token: int
    item: DeliveryItem
    message: object | None
    render_failed: bool = False


@dataclass(frozen=True, slots=True)
class _DeferredDiscordPush:
    """等待当前 Discord Gateway 会话恢复的未发送消息。"""
    sequence: int
    deadline: float
    item: DeliveryItem


class _ScheduledDeliveryQueue(asyncio.Queue[DeliveryItem]):
    """按固定投递优先级出队，同时保留 asyncio.Queue 的容量与 join 语义。"""

    def __init__(self, maxsize: int, priority_key):
        self._priority_key = priority_key
        self._next_sequence = 0
        super().__init__(maxsize=maxsize)

    def _init(self, _maxsize: int) -> None:
        self._queue = []

    def _put(self, item: DeliveryItem) -> None:
        sequence = self._next_sequence
        self._next_sequence += 1
        key = (
            self._priority_key(item, sequence)
            if isinstance(item, DeliveryItem)
            else (float("inf"), float("inf"), sequence)
        )
        heapq.heappush(self._queue, (key, item))

    def _get(self) -> DeliveryItem:
        return heapq.heappop(self._queue)[1]

    def snapshot(self) -> tuple[DeliveryItem, ...]:
        return tuple(item for _key, item in sorted(self._queue))

    def items(self) -> tuple[DeliveryItem, ...]:
        """返回无顺序快照，供只统计来源和年龄的状态路径使用。"""
        return tuple(item for _key, item in self._queue)

    def drain_in_arrival_order_nowait(self) -> tuple[DeliveryItem, ...]:
        """供满队列清理使用；重排后仍保持原始入队先后。"""
        items = tuple(
            item for _key, item in sorted(
                self._queue, key=lambda entry: entry[0][2])
        )
        self._queue.clear()
        for _item in items:
            self._wakeup_next(self._putters)
        return items

    def pop_oldest_nowait(self) -> DeliveryItem:
        """队列溢出时仍淘汰最早入队项，不受来源调度优势影响。"""
        if self.empty():
            raise asyncio.QueueEmpty
        index = min(
            range(len(self._queue)),
            key=lambda value: self._queue[value][0][2],
        )
        _key, item = self._queue[index]
        last = self._queue.pop()
        if index < len(self._queue):
            self._queue[index] = last
            heapq.heapify(self._queue)
        self._wakeup_next(self._putters)
        return item


class SniperPoller:
    # WFM 连续失败达到该次数（及其倍数）时重建 HTTP 客户端，
    # 自愈网络切换/DNS 变化/连接池损坏等长跑后出现的持续失败
    _CLIENT_RESET_AFTER = 5
    # 心跳判定账号离线后，多久内仍信任该判定（略大于 30s 心跳间隔，容忍偶发丢跳）。
    # 心跳持续离线时每跳都会刷新时间戳，故僵死态期间始终判为不可发送
    _HEARTBEAT_STALE = 90.0
    # 主发送流水线异常退出时指数退避重启；稳定运行一段时间后重置退避。
    _SEND_RESTART_INITIAL_SECONDS = 1.0
    _SEND_RESTART_MAX_SECONDS = 30.0
    _SEND_RESTART_STABLE_SECONDS = 60.0
    _DISCORD_OFFLINE_GRACE_SECONDS = 60.0
    # 发送与渲染解耦：最多提前处理 16 条，4 个工作线程并行生成卡图。
    # 同一目标按就绪顺序串行；流水线默认不额外限制不同目标并发。
    # QQ action 通过 SnowLuma HTTP API 独立发送，不共享反向 WS 写缓冲。
    _RENDER_AHEAD = 16
    _RENDER_WORKERS = 4
    # 当前卡图序列化后约 200 KiB；每 action 四张可控制在常见 1 MiB HTTP
    # 请求体附近，并让四个渲染 worker 同时准备后续批次。更大的单 action
    # 会放大结果未知时的影响范围，也会形成新的渲染和上传长尾。
    _QQ_WM_BATCH_MAX_CARDS = 4
    # 时效更强的频道消息可在同目标的短期 WM 突发中前移；使用有限时间优势，
    # 较老的 WM 仍会先发送，避免持续频道流量导致其它来源饥饿。
    _SOURCE_SCHEDULE_ADVANCE = {
        DeliverySource.IRC: 2.0,
        DeliverySource.BARGAIN: 1.0,
        DeliverySource.SYSTEM: 2.0,
        DeliverySource.WM: 0.0,
    }
    _DISCORD_UNKNOWN_CHANNEL = 10003

    def __init__(self, store: Store, config):
        self.store = store
        self.config = config
        self.wfm = WfmClient()
        self.fast = FastRivenPoller(store, config, self._on_fast_auctions)
        # 来源有限时间优势在入队时进入优先堆，出队为 O(log n)，避免每条消息
        # 全量扫描积压队列。容量满时仍按实际入队顺序淘汰最旧消息。
        self.queue: asyncio.Queue[DeliveryItem] = _ScheduledDeliveryQueue(
            int(getattr(config, "send_queue_maxsize", 1000)),
            self._delivery_schedule_key,
        )
        self._first_pass = True
        self._last_prune = 0.0
        self._fail_streak = 0
        self._tasks: list[asyncio.Task] = []
        self._retry_tasks: set[asyncio.Task] = set()
        self._retry_items: dict[asyncio.Task, DeliveryItem] = {}
        self._pipeline_inflight: dict[int, int] = {}
        self._inflight_deliveries: dict[int, DeliveryItem] = {}
        self._delivery_counters: dict[str, Counter[str]] = {}
        self._source_generations: dict[DeliverySource, str] = {}
        self._source_accepting: set[DeliverySource] = {
            DeliverySource.WM, DeliverySource.BARGAIN, DeliverySource.SYSTEM,
        }
        self._discord_dm_bot = None
        self._discord_dm_channels: dict[int, int] = {}
        self._discord_dm_locks: dict[int, asyncio.Lock] = {}
        self._discord_ready_event = asyncio.Event()
        self._discord_offline_since: float | None = (
            time.time()
            if getattr(config, "discord_dm_enabled", False)
            else None
        )
        self._discord_deferred: deque[_DeferredDiscordPush] = deque()
        self._discord_deferred_sequence = 0
        self._discord_deferred_task: asyncio.Task | None = None
        self._discord_prewarm_task: asyncio.Task | None = None
        self._discord_global_rate_limit_until = 0.0
        self._discord_target_rate_limit_until: dict[
            tuple[str, str, str], float
        ] = {}
        self._discord_route_rate_limit_until: dict[str, float] = {}
        self._send_limit_condition = asyncio.Condition()
        self._active_send_count = 0
        self._runtime_settings_condition = asyncio.Condition()
        self._runtime_settings_version = 0
        self._poll_now_event = asyncio.Event()
        self._sender_state = "stopped"
        self._sender_restart_count = 0
        self._sender_last_error: str | None = None
        self._sender_last_error_at: float | None = None
        self._sender_last_started_at: float | None = None
        self._sender_last_success_at: float | None = None
        self._runtime_identity = runtime_identity()

    @property
    def queue_depth(self) -> int:
        """入口、流水线在途和等待重试的消息总数。"""
        return (self.queue.qsize() + len(self._inflight_deliveries)
                + len(self._retry_items) + len(self._discord_deferred))

    @property
    def queued_deliveries(self) -> tuple[DeliveryItem, ...]:
        if isinstance(self.queue, _ScheduledDeliveryQueue):
            return self.queue.snapshot()
        return tuple(getattr(self.queue, "_queue", ()))

    @property
    def delivery_status(self) -> dict[str, object]:
        """按来源暴露当前积压、最老年龄和累计处理结果。"""
        now = time.time()
        queued: Counter[str] = Counter()
        inflight: Counter[str] = Counter()
        oldest: dict[str, float] = {}
        queue_items = (
            self.queue.items()
            if isinstance(self.queue, _ScheduledDeliveryQueue)
            else tuple(getattr(self.queue, "_queue", ()))
        )
        current_items = (
            *queue_items,
            *self._inflight_deliveries.values(),
            *self._retry_items.values(),
            *(entry.item for entry in self._discord_deferred),
        )
        for item in queue_items:
            queued[item.source.value] += 1
        for item in (
                *self._inflight_deliveries.values(),
                *self._retry_items.values(),
                *(entry.item for entry in self._discord_deferred)):
            inflight[item.source.value] += 1
        for item in current_items:
            age = max(0.0, now - item.enqueued_at)
            oldest[item.source.value] = max(
                oldest.get(item.source.value, 0.0), age)
        sources = {source.value for source in DeliverySource}
        counters = {
            source: dict(self._delivery_counters.get(source, {}))
            for source in sorted(sources)
        }
        return {
            "total": self.queue_depth,
            "queued_by_source": {
                source: queued.get(source, 0) for source in sorted(sources)
            },
            "inflight_by_source": {
                source: inflight.get(source, 0) for source in sorted(sources)
            },
            "oldest_age_by_source": {
                source: round(oldest.get(source, 0.0), 3)
                for source in sorted(sources)
            },
            "counters_by_source": counters,
            "active_generations": {
                source.value: generation
                for source, generation in self._source_generations.items()
            },
            "accepting_sources": sorted(
                source.value for source in self._source_accepting),
        }

    @property
    def sender_health(self) -> dict[str, object]:
        """主发送链路的可观测状态，供 WebUI 和外部监控读取。"""
        return {
            "state": self._sender_state,
            "running": self._sender_state == "running",
            "restart_count": self._sender_restart_count,
            "last_error": self._sender_last_error,
            "last_error_at": self._sender_last_error_at,
            "last_started_at": self._sender_last_started_at,
            "last_success_at": self._sender_last_success_at,
            "runtime": dict(self._runtime_identity),
        }

    @property
    def discord_ready(self) -> bool:
        """Discord Gateway 已完成 Ready/Resumed，允许实际发送。"""
        return (self._discord_ready_event.is_set()
                and self._discord_dm_bot is not None)

    def _record_delivery(self, item: DeliveryItem, outcome: str) -> None:
        counters = self._delivery_counters.setdefault(
            item.source.value, Counter())
        counters[outcome] += 1
        counters[f"{outcome}_items"] += self._payload_item_count(item.payload)

    @staticmethod
    def _payload_item_count(payload) -> int:
        return len(payload.cards) if isinstance(payload, _HitBatch) else 1

    def new_delivery(
        self,
        source: DeliverySource,
        target: object,
        payload: object,
        *,
        generation: str | None = None,
        observed_at: float | None = None,
        now: float | None = None,
    ) -> DeliveryItem:
        created_at = time.time() if now is None else now
        ttl = float(getattr(
            self.config, "trade_message_ttl_seconds", 60.0))
        return DeliveryItem(
            source=source,
            target=target,
            payload=payload,
            attempts=0,
            enqueued_at=created_at,
            expires_at=created_at + ttl,
            generation=generation,
            observed_at=observed_at,
        )

    def _delivery_is_current(self, item: DeliveryItem) -> bool:
        if item.source not in self._source_accepting:
            return False
        generation = self._source_generations.get(item.source)
        return generation is None or generation == item.generation

    def _prune_stale_queued_deliveries(self, now: float) -> None:
        """在队列容量紧张时清理已经过期或被撤销的入口消息。"""
        retained: list[DeliveryItem] = []
        if isinstance(self.queue, _ScheduledDeliveryQueue):
            queued_items = self.queue.drain_in_arrival_order_nowait()
        else:
            queued_items = []
            while not self.queue.empty():
                queued_items.append(self.queue.get_nowait())
        for queued in queued_items:
            self.queue.task_done()
            if queued.expired(now):
                self._record_delivery(queued, "expired")
            elif not self._delivery_is_current(queued):
                self._record_delivery(queued, "cancelled")
            else:
                retained.append(queued)
        for queued in retained:
            self.queue.put_nowait(queued)

    def enqueue_delivery(
            self, item: DeliveryItem, *, count_logical: bool = True,
            evict_oldest: bool = True) -> bool:
        """非阻塞入队；默认在满队列中淘汰入口 FIFO 最旧的一条。"""
        now = time.time()
        if item.expired(now):
            self._record_delivery(item, "expired")
            return False
        if not self._delivery_is_current(item):
            self._record_delivery(item, "cancelled")
            return False

        if self.queue.full():
            self._prune_stale_queued_deliveries(now)

        if self.queue.full():
            if not evict_oldest:
                self._record_delivery(item, "queue_full")
                return False
            oldest = (
                self.queue.pop_oldest_nowait()
                if isinstance(self.queue, _ScheduledDeliveryQueue)
                else self.queue.get_nowait()
            )
            self.queue.task_done()
            self._record_delivery(oldest, "evicted_oldest")
            logger.warning(
                "发送队列已满，淘汰最旧消息 source={} age={:.1f}s target={}",
                oldest.source.value,
                max(0.0, now - oldest.enqueued_at),
                self._describe_target(oldest.target),
            )

        self.queue.put_nowait(item)
        self._record_delivery(item, "enqueued")
        if item.attempts == 0 and count_logical:
            counters = self._delivery_counters.setdefault(
                item.source.value, Counter())
            item_count = self._payload_item_count(item.payload)
            counters["logical_items"] += item_count
            if item_count > 1:
                counters["batches"] += 1
                counters["batched_items"] += item_count
                counters["api_requests_saved"] += item_count - 1
        return True

    def activate_source(self, source: DeliverySource, generation: str) -> None:
        self._source_generations[source] = generation
        self._source_accepting.add(source)

    def cancel_source(
        self, source: DeliverySource, generation: str | None = None,
    ) -> dict[str, int]:
        """撤销某来源当前代次的入口、重试和在途消息。"""
        active_generation = self._source_generations.get(source)
        if generation is not None and active_generation != generation:
            return {"queued": 0, "retrying": 0, "inflight": 0}
        self._source_accepting.discard(source)

        # 先同步摘除并取消等待重试的任务，再清理入口队列。这样即使某个重试
        # 恰好已完成入队但 done callback 尚未执行，也会被下面的队列扫描移除。
        retrying_removed = 0
        for task, retry_item in tuple(self._retry_items.items()):
            matches = retry_item.source == source and (
                generation is None or retry_item.generation == generation)
            if not matches:
                continue
            self._retry_items.pop(task, None)
            self._retry_tasks.discard(task)
            if not task.done():
                retrying_removed += 1
                self._record_delivery(retry_item, "cancelled")
                task.cancel()

        retained_deferred: deque[_DeferredDiscordPush] = deque()
        while self._discord_deferred:
            entry = self._discord_deferred.popleft()
            retry_item = entry.item
            matches = retry_item.source == source and (
                generation is None or retry_item.generation == generation)
            if matches:
                retrying_removed += 1
                self._record_delivery(retry_item, "cancelled")
            else:
                retained_deferred.append(entry)
        self._discord_deferred = retained_deferred

        queued_removed = 0
        retained: list[DeliveryItem] = []
        while True:
            try:
                queued = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self.queue.task_done()
            matches = queued.source == source and (
                generation is None or queued.generation == generation)
            if matches:
                queued_removed += 1
                self._record_delivery(queued, "cancelled")
            else:
                retained.append(queued)
        for queued in retained:
            self.queue.put_nowait(queued)

        inflight = sum(
            item.source == source
            and (generation is None or item.generation == generation)
            for item in self._inflight_deliveries.values()
        )
        return {
            "queued": queued_removed,
            "retrying": retrying_removed,
            "inflight": inflight,
        }

    def start(self):
        self._tasks.append(asyncio.create_task(self._poll_loop()))
        self._tasks.append(asyncio.create_task(self.fast.run()))
        # 狙击/捡漏发送通道
        self._sender_state = "starting"
        self._tasks.append(asyncio.create_task(
            self._supervise_send_loop(
                self.queue, lambda: self.config.sniper_send_interval)))
        if getattr(self.config, "discord_dm_enabled", False):
            discord_bot = self._adapter_bot("Discord")
            if discord_bot is not None:
                self.discord_bot_connected(discord_bot)
        logger.info("riven sniper poller started (dry_run={})", self.config.sniper_dry_run)

    async def stop(self):
        tasks = [*self._tasks, *self._retry_tasks]
        if self._discord_deferred_task is not None:
            tasks.append(self._discord_deferred_task)
        if self._discord_prewarm_task is not None:
            tasks.append(self._discord_prewarm_task)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._retry_tasks.clear()
        self._retry_items.clear()
        self._discord_deferred.clear()
        self._discord_deferred_task = None
        self._discord_prewarm_task = None
        self._discord_dm_bot = None
        self._discord_ready_event.clear()
        self._discord_offline_since = None
        self._discord_dm_channels.clear()
        self._discord_dm_locks.clear()
        self._discord_global_rate_limit_until = 0.0
        self._discord_target_rate_limit_until.clear()
        self._discord_route_rate_limit_until.clear()
        self._sender_state = "stopped"
        await self.wfm.close()

    async def _on_poll_failure(self):
        """失败连击计数；到阈值就重建 WFM 客户端（长跑自愈）。"""
        self._fail_streak += 1
        if self._fail_streak % self._CLIENT_RESET_AFTER == 0:
            await self.wfm.reset()
            logger.warning("WFM 连续失败 {} 次，已重建 HTTP 客户端", self._fail_streak)

    async def _poll_loop(self):
        while True:
            rate_limit_delay: float | None = None
            try:
                await self._poll_once()
                self._fail_streak = 0
            except httpx.HTTPStatusError as e:
                STATS.record_poll_failure()
                if e.response.status_code == 429:
                    # 被限流：尊重 Retry-After，至少等 2 个轮询间隔
                    retry_after = e.response.headers.get("Retry-After", "")
                    extra = float(retry_after) if retry_after.isdigit() else 0.0
                    rate_limit_delay = min(300.0, max(
                        float(self.config.sniper_poll_interval) * 2, extra))
                    logger.warning("WFM 限流(429)，{:.0f}s 后重试",
                                   rate_limit_delay)
                else:
                    logger.warning("WFM 轮询失败: {}", e)
                await self._on_poll_failure()
            except httpx.HTTPError as e:
                STATS.record_poll_failure()
                logger.warning("WFM 轮询失败: {}", e)
                await self._on_poll_failure()
            except Exception:
                STATS.record_poll_failure()
                logger.exception("狙击轮询异常")
                await self._on_poll_failure()
            completed_at = asyncio.get_running_loop().time()
            if rate_limit_delay is None:
                delay_getter = lambda: self.config.sniper_poll_interval
            else:
                # 热更新不能绕过服务端已给出的本轮退避下限。
                delay_getter = lambda: max(
                    rate_limit_delay, self.config.sniper_poll_interval)
            if rate_limit_delay is None:
                await self._wait_for_poll_delay(completed_at, delay_getter)
            else:
                await self._wait_for_runtime_delay(completed_at, delay_getter)
                # 退避结束后的下一轮已经会读取最新群组，无需紧接着再轮询一次。
                self._poll_now_event.clear()

    @staticmethod
    def _pack_discord_hit_cards(
            cards: list[_HitCard]) -> tuple[tuple[_HitCard, ...], ...]:
        """按 Discord Embed 硬限制拆分同目标同轮 WM 命中。"""
        batches: list[tuple[_HitCard, ...]] = []
        current: list[_HitCard] = []
        current_characters = 0
        for card in cards:
            try:
                if card.grade_result is None:
                    card.grade_result = grade_auction_item(card.auction["item"])
                characters = max(
                    hit_discord_embed_character_count(
                        card.config, card.auction, card.extra_count, locale,
                        card.grade_result)
                    for locale in ("zh", "en")
                )
            except Exception as error:
                # 合并预检复用了完整 Embed 构建；单卡数据异常只能让该卡
                # 降级到既有单条渲染路径，不能中断整轮或连带其它目标。
                if current:
                    batches.append(tuple(current))
                    current = []
                    current_characters = 0
                batches.append((card,))
                logger.warning(
                    "Discord WM 合并预检失败，已隔离为单条: auction={} error={}",
                    card.auction.get("id"), error)
                continue
            if current and (
                    len(current) >= DISCORD_MESSAGE_MAX_EMBEDS
                    or current_characters + characters
                    > DISCORD_MESSAGE_MAX_EMBED_CHARACTERS):
                batches.append(tuple(current))
                current = []
                current_characters = 0
            current.append(card)
            current_characters += characters
        if current:
            batches.append(tuple(current))
        return tuple(batches)

    @classmethod
    def _pack_qq_hit_cards(
            cls, cards: list[_HitCard]) -> tuple[tuple[_HitCard, ...], ...]:
        return tuple(
            tuple(cards[offset:offset + cls._QQ_WM_BATCH_MAX_CARDS])
            for offset in range(0, len(cards), cls._QQ_WM_BATCH_MAX_CARDS)
        )

    async def _poll_once(self):
        auctions = await self.wfm.recent_auctions()
        candidates = [
            a for a in auctions
            if (a.get("item") or {}).get("type") == "riven"
            and not a.get("closed")
            and not a.get("private")
            and a.get("visible", True)
        ]
        fresh_ids = set(self.store.mark_seen([a["id"] for a in candidates]))
        new_auctions = [a for a in candidates if a["id"] in fresh_ids]

        # 启动后的第一轮仍不运行紫卡狙击，避免刷屏历史听单；但停机期间
        # 新建且仍在一小时窗口内的订单必须交给紫卡捡漏，由其 created 与
        # 永久挂单 ID 规则自行筛选，不能在这里永久吞掉。
        if self._first_pass:
            self._first_pass = False
            logger.info("首轮标记 {} 条现存拍卖", len(fresh_ids))
            await self._check_bargain_auctions(new_auctions)
            STATS.record_poll(len(candidates), 0, 0)
            return

        if time.time() - self._last_prune > _PRUNE_INTERVAL:
            self.store.prune_seen()
            self._last_prune = time.time()

        if not new_auctions:
            return

        enabled_platforms = (
            ("discord",)
            if getattr(self.config, "discord_dm_enabled", False) else ())
        active_scopes = self.store.active_delivery_scope_ids(
            platforms=enabled_platforms)
        configs = [
            config for config in self.store.list_configs()
            if config["group_id"] in active_scopes
        ]
        hits = await self._process_wm_auctions(new_auctions, configs)
        await self._check_bargain_auctions(new_auctions)
        STATS.record_poll(len(candidates), len(new_auctions), hits)

    async def _on_fast_auctions(self, auctions: list[dict], configs: list[dict]) -> None:
        await self._process_wm_auctions(auctions, configs, fast=True)

    async def _process_wm_auctions(
            self, auctions: list[dict], configs: list[dict], *, fast: bool = False) -> int:
        # 入队前的目标/挂单认领没有 await；耗时匹配可并发，不阻塞另一条获取链路。
        return await self._enqueue_wm_hits(auctions, configs, fast=fast)

    async def _enqueue_wm_hits(
            self, new_auctions: list[dict], configs: list[dict], *, fast: bool) -> int:
        blacklisted = []
        for auction in new_auctions:
            seller = (auction.get("owner") or {}).get("ingame_name", "")
            blacklisted.append(self.store.blacklisted_scope_ids(seller))

        def aggregate():
            # 真实规则量达到数百条时，匹配交叉积也必须离开事件循环。
            agg: dict[tuple[int, str], dict] = {}
            match_count = 0
            for auction, blocked in zip(new_auctions, blacklisted, strict=True):
                grade = grade_auction_item(auction["item"]) if configs else None
                for cfg in configs:
                    gid = cfg["group_id"]
                    if gid in blocked or not matcher.match_config(cfg, auction, grade):
                        continue
                    match_count += 1
                    e = agg.setdefault((gid, auction["id"]),
                                       {"auction": auction, "configs": [], "grade_result": grade})
                    e["configs"].append(cfg)
            return agg, match_count

        agg, match_count = await asyncio.to_thread(aggregate)

        grouped_hits: dict[int, list[_HitCard]] = {}
        queued_pushes = 0
        hits = 0
        # 同一目标在一轮里可能命中多条挂单，偏好每轮每目标只查一次。
        locales: dict[int, str] = {}
        for (gid, _aid), e in agg.items():
            cfg0 = e["configs"][0]
            extra = len(e["configs"]) - 1
            locale = locales.get(gid)
            if locale is None:
                locale = self.store.get_target_preferences(gid)["locale"]
                locales[gid] = locale
            # 卡图渲染推迟到发送前的有界预渲染流水线；dry-run 直接
            # 入队可读文本。
            if self.config.sniper_dry_run:
                payload = format_hit(
                    cfg0, e["auction"], e["grade_result"],
                    extra_count=extra, locale=locale)
            else:
                payload = _HitCard(
                    cfg0, e["auction"], extra, locale,
                    grade_result=e["grade_result"])
                route = self.store.get_target(gid)
                if route and route["platform"] in {"qq", "discord"}:
                    grouped_hits.setdefault(gid, []).append(payload)
                    continue
            accepted = self._enqueue_wm_payload(
                gid, payload, {e["auction"]["id"]: e["configs"]}, fast=fast)
            queued_pushes += bool(accepted)
            hits += accepted

        if grouped_hits:
            discord_targets = {
                gid for gid in grouped_hits
                if self.store.get_target(gid)["platform"] == "discord"
            }
            packed = await asyncio.to_thread(lambda: {
                gid: (
                    self._pack_discord_hit_cards(cards)
                    if gid in discord_targets
                    else self._pack_qq_hit_cards(cards)
                )
                for gid, cards in grouped_hits.items()
            })
            for gid, batches in packed.items():
                for cards in batches:
                    payload = (
                        cards[0] if len(cards) == 1
                        else _HitBatch(cards, cards[0].locale)
                    )
                    accepted = self._enqueue_wm_payload(
                        gid, payload, {card.auction["id"]: agg[(gid, card.auction["id"])]["configs"]
                                       for card in cards},
                        fast=fast)
                    queued_pushes += bool(accepted)
                    hits += accepted
        if hits:
            logger.info(
                "本轮 {} 条新听单，命中 {} 处，逻辑推送 {} 条，入队消息 {} 条",
                len(new_auctions), match_count, hits, queued_pushes)

        return hits

    def _enqueue_wm_payload(
            self, gid: int, payload, matches: dict[str, list[dict]],
            *, fast: bool) -> int:
        if not self._target_is_active(gid):
            return 0
        if fast:
            current = {subscription_token(config) for config in self.store.wm_fast_configs(
                discord_enabled=getattr(self.config, "discord_dm_enabled", False))}
            # 打包在线程内完成，期间规则可能改变；逐张复核，不能让同批其他规则放行旧命中。
            matches = {aid: [cfg for cfg in configs if subscription_token(cfg) in current]
                       for aid, configs in matches.items()}
            matches = {aid: configs for aid, configs in matches.items() if configs}
            cards = payload.cards if isinstance(payload, _HitBatch) else (payload,)
            for card in cards:
                if isinstance(card, _HitCard) and card.auction["id"] in matches:
                    configs = matches[card.auction["id"]]
                    card.config, card.extra_count = configs[0], len(configs) - 1
        claimed = self.store.claim_wm_notifications(gid, list(matches))
        if not claimed:
            return 0
        if isinstance(payload, _HitBatch):
            cards = tuple(card for card in payload.cards if card.auction["id"] in claimed)
            payload = cards[0] if len(cards) == 1 else _HitBatch(cards, payload.locale)
        if not self.enqueue_delivery(self.new_delivery(DeliverySource.WM, gid, payload)):
            self.store.release_wm_notifications(gid, list(claimed))
            return 0
        return len(claimed)

    @staticmethod
    async def _check_bargain_auctions(auctions: list[dict]) -> None:
        bargain_poller = shared.get_bargain()
        if bargain_poller is None:
            return
        try:
            await bargain_poller.on_fresh_riven_auctions(auctions)
        except Exception:
            logger.exception("紫卡捡漏检查异常")

    async def _requeue_after(self, item: DeliveryItem, delay: float) -> None:
        """延迟后重新入队；独立任务等待，不阻塞发送循环处理后续消息。"""
        await asyncio.sleep(delay)
        self.enqueue_delivery(item)

    def _schedule_requeue(
            self, item: DeliveryItem, delay: float) -> None:
        task = asyncio.create_task(self._requeue_after(item, delay))
        self._retry_tasks.add(task)
        self._retry_items[task] = item

        def finished(done: asyncio.Task) -> None:
            self._retry_tasks.discard(done)
            self._retry_items.pop(done, None)

        task.add_done_callback(finished)

    def _schedule_retry(self, item: DeliveryItem) -> None:
        self._schedule_requeue(
            item, float(getattr(
                self.config, "send_retry_delay_seconds", 2.0)))

    async def _render_payload(self, payload):
        """把待渲染的命中负载在工作线程里生成消息；其它类型原样返回。

        渲染放到线程池：不阻塞事件循环（轮询/其它发送通道继续跑）。
        渲染结果缓存在原始负载中，失败重试不重复渲染，同时保留
        拍卖卖家供黑名单复核。
        """
        if isinstance(payload, _HitBatch):
            if payload.rendered is None:
                missing = [
                    card for card in payload.cards
                    if card.grade_result is None
                ]
                if missing:
                    results = await asyncio.to_thread(
                        lambda: [grade_auction_item(card.auction["item"])
                                 for card in missing]
                    )
                    for card, result in zip(missing, results, strict=True):
                        card.grade_result = result
                entries = tuple(
                    (card.config, card.auction, card.extra_count,
                     card.grade_result)
                    for card in payload.cards
                )
                scope_id = int(payload.cards[0].config.get("group_id", 0))
                target = self.store.get_target(scope_id)
                builder = (
                    build_hit_discord_rich_batch
                    if target and target["platform"] == "discord"
                    else build_hit_qq_batch
                )
                message, _ = await asyncio.to_thread(
                    builder, entries, payload.locale)
                payload.rendered = message
            return payload.rendered
        if isinstance(payload, _HitCard):
            if payload.rendered is None:
                if payload.grade_result is None:
                    payload.grade_result = await asyncio.to_thread(
                        grade_auction_item, payload.auction["item"])
                scope_id = int(payload.config.get("group_id", 0))
                target = self.store.get_target(scope_id)
                builder = (build_hit_discord_rich
                           if target and target["platform"] == "discord"
                           else build_hit_push)
                args = (payload.config, payload.auction, payload.extra_count,
                        payload.locale, payload.grade_result)
                message, _ = await asyncio.to_thread(
                    builder, *args)
                payload.rendered = message
            return payload.rendered
        if isinstance(payload, bargain.BargainItemPushPayload):
            if payload.rendered is None:
                route = self.store.get_target(payload.target_scope)
                if route and route["platform"] == "discord":
                    message, _ = await asyncio.to_thread(
                        build_bargain_item_discord_rich,
                        payload.slug,
                        payload.order,
                        payload.hit,
                        payload.locale,
                    )
                else:
                    message = bargain.build_item_push_text(
                        payload.slug,
                        payload.order,
                        payload.hit,
                        locale=payload.locale,
                    )
                payload.rendered = message
            return payload.rendered
        if isinstance(payload, bargain.BargainRivenPushPayload):
            if payload.rendered is None:
                route = self.store.get_target(payload.target_scope)
                if route and route["platform"] == "discord":
                    message, _ = await asyncio.to_thread(
                        build_bargain_riven_discord_rich,
                        payload.weapon_slug,
                        payload.auction,
                        payload.hit,
                        payload.locale,
                    )
                else:
                    message = bargain.build_riven_push_text(
                        payload.weapon_slug,
                        payload.auction,
                        payload.hit,
                        locale=payload.locale,
                    )
                payload.rendered = message
            return payload.rendered
        if isinstance(payload, ChannelPushPayload):
            if payload.rendered is None:
                route = self.store.get_target(
                    payload.target_scope)
                builder = (build_channel_discord_rich
                           if route and route["platform"] == "discord"
                           else build_channel_qq)
                message, _ = await asyncio.to_thread(builder, payload)
                payload.rendered = message
            return payload.rendered
        return payload

    def _sync_payload_locale(self, target, payload) -> bool:
        """把延迟渲染推送同步到目标当前语言；发生变化时清除旧成品。"""
        if not isinstance(payload, (
                _HitCard, _HitBatch, bargain.BargainItemPushPayload,
                bargain.BargainRivenPushPayload, ChannelPushPayload)):
            return False
        if not isinstance(target, int):
            return False
        locale = self.store.get_target_preferences(target)["locale"]
        if payload.locale == locale:
            return False
        payload.locale = locale
        payload.rendered = None
        return True

    async def _render_payload_for_target(
            self, target, payload, prepared=None):
        """按发送时的当前语言渲染，切换发生在渲染期间时重新生成。"""
        localized = isinstance(payload, (
            _HitCard, _HitBatch, bargain.BargainItemPushPayload,
            bargain.BargainRivenPushPayload, ChannelPushPayload))
        changed = self._sync_payload_locale(target, payload)
        if prepared is not None and (
                not localized
                or (not changed and (
                    not isinstance(payload, _HitBatch)
                    or payload.rendered is prepared))):
            return prepared
        while True:
            message = await self._render_payload(payload)
            if not self._sync_payload_locale(target, payload):
                return message

    @staticmethod
    def _adapter_name(bot) -> str:
        return bot.adapter.get_name()

    @classmethod
    def _adapter_bot(cls, adapter_name: str):
        try:
            bots = list(nonebot.get_bots().values())
        except ValueError:
            bots = []
        for bot in bots:
            if cls._adapter_name(bot) == adapter_name:
                return bot
        return None

    def _bot_connected(self) -> bool:
        """能否真正把消息发出去。

        反向 WS 连着只是必要条件：账号被踢下线后 SnowLuma 仍保持 WS，但发送会
        持续超时。此时心跳会上报 status.online=false——据此把僵死态判为不可
        发送，在调用发送 API 前直接丢弃时效消息。
        """
        try:
            bots = list(nonebot.get_bots().values())
            if not any(self._adapter_name(bot) == "OneBot V11"
                       for bot in bots):
                return False
        except ValueError:
            return False
        online, ts = shared.bot_online_state()
        if online is False and (time.time() - ts) < self._HEARTBEAT_STALE:
            return False
        return True

    def _target_bot_connected(self, target) -> bool:
        route = (self.store.get_target(target)
                 if isinstance(target, int) and target < 0 else None)
        if route:
            return (getattr(self.config, "discord_dm_enabled", False)
                    and route["active"] and route["platform"] == "discord"
                    and self._discord_ready_event.is_set()
                    and self._discord_dm_bot is not None
                    and self._adapter_bot("Discord")
                    is self._discord_dm_bot)
        return self._bot_connected()

    def _target_is_active(self, target) -> bool:
        if isinstance(target, tuple):
            kind, target_id = target
            if kind != "group":
                return True
            target = int(target_id)
        if not isinstance(target, int):
            return True
        route = self.store.get_target(target)
        if not route or not route["active"]:
            return False
        return (route["platform"] != "discord"
                or getattr(self.config, "discord_dm_enabled", False))

    def _channel_payload_is_allowed(self, target, payload) -> bool:
        """频道开关或狙击配置可能在排队期间变化，发送前必须重新核验。"""
        if not isinstance(payload, ChannelPushPayload):
            return True
        if (not isinstance(target, int)
                or not self.store.get_target_preferences(target)["channel_enabled"]
                or not payload.cards):
            return False
        configs = self.store.list_configs(target)
        return all(
            any(matcher.match_channel_card(config, card.decoded)
                for config in configs)
            for card in payload.cards
        )

    @staticmethod
    def _describe_target(target) -> str:
        """把 target 渲染成日志可读的中文（群/好友）。"""
        if isinstance(target, tuple):
            kind, tid = target
            return f"好友{tid}" if kind == "private" else f"群{tid}"
        if isinstance(target, int) and target < 0:
            return f"平台私聊作用域{-target}"
        return f"群{target}"

    def _blocked_hit_seller(self, target, payload) -> str | None:
        """返回已被目标拉黑的 WM 或频道狙击卖家。"""
        if not isinstance(target, int):
            return None
        if isinstance(payload, _HitCard):
            seller = str(
                (payload.auction.get("owner") or {}).get("ingame_name") or ""
            ).strip()
            scope = "wm"
        elif isinstance(payload, ChannelPushPayload):
            seller = payload.seller.strip()
            scope = "channel"
        else:
            return None
        blocked = (
            self.store.is_blacklisted(target, seller)
            if scope == "wm"
            else self.store.is_blacklisted(target, seller, scope=scope)
        ) if seller else False
        if blocked:
            return seller
        return None

    def _filter_blocked_hit_batch(self, target, payload) -> int:
        """移除合并 WM 中排队期间被拉黑的卖家，保留其余命中。"""
        if not isinstance(target, int) or not isinstance(payload, _HitBatch):
            return 0
        cards = tuple(
            card for card in payload.cards
            if not self.store.is_blacklisted(
                target,
                str((card.auction.get("owner") or {}).get(
                    "ingame_name") or "").strip(),
            )
        )
        removed = len(payload.cards) - len(cards)
        if removed:
            payload.cards = cards
            payload.rendered = None
        return removed

    def _target_send_key(self, target) -> tuple[str, str, str]:
        """主通道的真实发送目标；相同键共用一个串行锁。"""
        if isinstance(target, tuple):
            kind, target_id = target
            return "qq", str(kind), str(target_id)
        if isinstance(target, int) and target < 0:
            route = self.store.get_target(target)
            if route:
                return (str(route["platform"]), "private",
                        str(route["external_id"]))
            return "scope", "private", str(target)
        return "qq", "group", str(target)

    def _send_concurrency_limit(self) -> int:
        return max(0, int(getattr(
            self.config, "sniper_send_concurrency", 0) or 0))

    async def _acquire_global_send_slot(self) -> None:
        async with self._send_limit_condition:
            await self._send_limit_condition.wait_for(
                lambda: self._send_concurrency_limit() == 0
                or self._active_send_count < self._send_concurrency_limit())
            self._active_send_count += 1

    async def _release_global_send_slot(self) -> None:
        async with self._send_limit_condition:
            self._active_send_count -= 1
            self._send_limit_condition.notify_all()

    async def _wait_for_runtime_delay(self, since: float, delay_getter) -> None:
        """等待动态间隔；配置通知会立即按新值重新计算剩余时间。"""
        loop = asyncio.get_running_loop()
        async with self._runtime_settings_condition:
            observed_version = self._runtime_settings_version
            while True:
                remaining = since + _send_delay(delay_getter()) - loop.time()
                if remaining <= 0:
                    return
                try:
                    await asyncio.wait_for(
                        self._runtime_settings_condition.wait_for(
                            lambda: self._runtime_settings_version
                            != observed_version),
                        timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    return
                observed_version = self._runtime_settings_version

    async def _wait_for_poll_delay(self, since: float, delay_getter) -> None:
        """等待下一轮计划时间，也允许群组热更新要求立即轮询。"""
        delay_task = asyncio.create_task(
            self._wait_for_runtime_delay(since, delay_getter))
        poll_now_task = asyncio.create_task(self._poll_now_event.wait())
        try:
            done, _ = await asyncio.wait(
                (delay_task, poll_now_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if poll_now_task in done:
                self._poll_now_event.clear()
            if delay_task in done:
                await delay_task
        finally:
            delay_task.cancel()
            poll_now_task.cancel()
            await asyncio.gather(
                delay_task, poll_now_task, return_exceptions=True)

    async def notify_runtime_settings_changed(
            self, *, poll_now: bool = False) -> None:
        """广播热配置变更，唤醒轮询间隔、目标间隔和并发许可等待。"""
        if poll_now:
            self._poll_now_event.set()
        async with self._runtime_settings_condition:
            self._runtime_settings_version += 1
            self._runtime_settings_condition.notify_all()
        async with self._send_limit_condition:
            self._send_limit_condition.notify_all()

    def _activate_discord_dm_cache(self, bot) -> None:
        """Discord 连接对象变化时丢弃旧 channel id，避免跨会话复用。"""
        if bot is self._discord_dm_bot:
            return
        self._discord_dm_bot = bot
        self._discord_dm_channels.clear()
        self._discord_dm_locks.clear()
        self._discord_global_rate_limit_until = 0.0
        self._discord_target_rate_limit_until.clear()
        self._discord_route_rate_limit_until.clear()

    def _discord_retry_delay(
            self, error: DiscordRateLimitException, attempts: int) -> float:
        retry_after = float(getattr(error, "retry_after", 0.0) or 0.0)
        if retry_after > 0:
            return retry_after
        base = float(getattr(
            self.config, "send_retry_delay_seconds", 2.0) or 2.0)
        return base * (2 ** min(attempts, 4))

    def _note_discord_rate_limit(
            self, target, error: DiscordRateLimitException,
            delay: float) -> None:
        deadline = asyncio.get_running_loop().time() + delay
        if bool(getattr(error, "global_rate_limit", False)):
            self._discord_global_rate_limit_until = max(
                self._discord_global_rate_limit_until, deadline)
            return
        key = self._target_send_key(target)
        self._discord_target_rate_limit_until[key] = max(
            self._discord_target_rate_limit_until.get(key, 0.0), deadline)
        route_key = getattr(error, "route_key", None)
        if route_key:
            self._discord_route_rate_limit_until[route_key] = max(
                self._discord_route_rate_limit_until.get(route_key, 0.0),
                deadline)

    async def _wait_for_discord_route(self, route_suffix: str) -> None:
        loop = asyncio.get_running_loop()
        while True:
            matching = [
                deadline
                for route_key, deadline
                in self._discord_route_rate_limit_until.items()
                if route_key.endswith(route_suffix)
            ]
            deadline = max(
                [self._discord_global_rate_limit_until, *matching])
            remaining = deadline - loop.time()
            if remaining <= 0:
                for route_key in tuple(self._discord_route_rate_limit_until):
                    if (route_key.endswith(route_suffix)
                            and self._discord_route_rate_limit_until[route_key]
                            <= loop.time()):
                        self._discord_route_rate_limit_until.pop(
                            route_key, None)
                return
            await asyncio.sleep(remaining)

    async def _wait_for_discord_rate_limit(self, target) -> None:
        key = self._target_send_key(target)
        if key[0] != "discord":
            return
        loop = asyncio.get_running_loop()
        while True:
            deadline = max(
                self._discord_global_rate_limit_until,
                self._discord_target_rate_limit_until.get(key, 0.0),
            )
            remaining = deadline - loop.time()
            if remaining <= 0:
                self._discord_target_rate_limit_until.pop(key, None)
                return
            await asyncio.sleep(remaining)

    async def _discord_dm_channel(self, bot, recipient_id: int) -> int:
        """解析并缓存私信 channel；同一接收方并发首次解析只发一个请求。"""
        self._activate_discord_dm_cache(bot)
        cached = self._discord_dm_channels.get(recipient_id)
        if cached is not None:
            return cached
        lock = self._discord_dm_locks.setdefault(recipient_id, asyncio.Lock())
        async with lock:
            cached = self._discord_dm_channels.get(recipient_id)
            if cached is not None:
                return cached
            await self._wait_for_discord_route("/users/@me/channels")
            channel = await bot.create_DM(recipient_id=recipient_id)
            channel_id = int(channel.id)
            self._discord_dm_channels[recipient_id] = channel_id
            return channel_id

    async def _prewarm_discord_dms(self, bot) -> None:
        """连接建立后顺序预热已启用目标，降低重启后的首批投递延迟。"""
        self._activate_discord_dm_cache(bot)
        targets = self.store.list_targets(
            "discord", active_only=True)
        warmed = 0
        for target in targets:
            recipient_id = int(target["external_id"])
            while (bot is self._discord_dm_bot
                   and self._discord_ready_event.is_set()):
                await self._wait_for_discord_rate_limit(target["scope_id"])
                try:
                    await self._discord_dm_channel(bot, recipient_id)
                    warmed += 1
                    break
                except DiscordRateLimitException as error:
                    delay = self._discord_retry_delay(error, 0)
                    self._note_discord_rate_limit(
                        target["scope_id"], error, delay)
                    logger.warning(
                        "Discord DM 预热触发限流，按服务端要求等待 {:.2f}s",
                        delay)
                except Exception as error:
                    logger.warning(
                        "Discord DM 预热部分失败，已完成 {}/{}: {}",
                        warmed, len(targets), error)
                    return
            if (bot is not self._discord_dm_bot
                    or not self._discord_ready_event.is_set()):
                return
            # 预热是后台优化，不与实时发送争抢瞬时 API 配额。
            await asyncio.sleep(0.1)
        if warmed:
            logger.info("Discord DM 通道预热完成: {} 个目标", warmed)

    def _ensure_discord_deferred_worker(self) -> None:
        if (self._discord_deferred_task is not None
                and not self._discord_deferred_task.done()):
            return
        self._discord_deferred_task = asyncio.create_task(
            self._discord_deferred_loop())

    async def _discord_deferred_loop(self) -> None:
        """等待当前 Gateway 会话就绪，并在原 TTL/断线窗口内恢复入队。"""
        try:
            while self._discord_deferred:
                now = time.time()
                retained: deque[_DeferredDiscordPush] = deque()
                while self._discord_deferred:
                    entry = self._discord_deferred.popleft()
                    if entry.deadline <= now or entry.item.expired(now):
                        self._record_delivery(entry.item, "expired")
                        self._record_delivery(entry.item, "offline_expired")
                        logger.warning(
                            "Discord 断线暂存已过期，丢弃 source={} target={}",
                            entry.item.source.value,
                            self._describe_target(entry.item.target),
                        )
                    else:
                        retained.append(entry)
                self._discord_deferred = retained
                if not retained:
                    return

                if self._discord_ready_event.is_set():
                    recovered = sorted(
                        retained,
                        key=lambda entry: (
                            entry.item.enqueued_at, entry.sequence),
                    )
                    self._discord_deferred.clear()
                    # 恢复是批量入队；先清掉失效入口项，避免它们占着容量导致
                    # 本来可以保留的断线消息被误判为队列满。
                    self._prune_stale_queued_deliveries(now)
                    available = max(0, self.queue.maxsize - self.queue.qsize())
                    overflow = max(0, len(recovered) - available)
                    for entry in recovered[:overflow]:
                        self._record_delivery(
                            entry.item, "offline_recovery_queue_full")
                    recovered = recovered[overflow:]
                    recovered_count = 0
                    for entry in recovered:
                        if self.enqueue_delivery(
                                entry.item, count_logical=False,
                                evict_oldest=False):
                            self._record_delivery(
                                entry.item, "offline_recovered")
                            recovered_count += 1
                    logger.info(
                        "Discord Gateway 已就绪，恢复 {} 条断线暂存消息，"
                        "队列容量不足丢弃 {} 条",
                        recovered_count,
                        overflow,
                    )
                    return

                next_deadline = min(
                    entry.deadline for entry in retained)
                timeout = max(0.0, next_deadline - time.time())
                try:
                    await asyncio.wait_for(
                        self._discord_ready_event.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    pass
        finally:
            if self._discord_deferred_task is asyncio.current_task():
                self._discord_deferred_task = None

    def _defer_discord_delivery(self, item: DeliveryItem) -> bool:
        """暂存尚未调用 API 的 Discord 消息；非 Discord 目标返回 False。"""
        if (not getattr(self.config, "discord_dm_enabled", False)
                or self._target_send_key(item.target)[0] != "discord"):
            return False

        now = time.time()
        if self._discord_ready_event.is_set():
            # 连接表与就绪状态发生短暂竞态时，以实际不可发送为准进入新断线周期。
            self._discord_ready_event.clear()
        if self._discord_offline_since is None:
            self._discord_offline_since = now
        deadline = min(
            item.expires_at,
            self._discord_offline_since
            + self._DISCORD_OFFLINE_GRACE_SECONDS,
        )
        if deadline <= now:
            self._record_delivery(item, "expired")
            self._record_delivery(item, "offline_expired")
            return True

        maxsize = self.queue.maxsize
        if len(self._discord_deferred) >= maxsize:
            oldest = self._discord_deferred.popleft().item
            self._record_delivery(oldest, "offline_evicted_oldest")
            logger.warning(
                "Discord 断线暂存已满，淘汰最旧消息 source={} target={}",
                oldest.source.value, self._describe_target(oldest.target),
            )
        entry = _DeferredDiscordPush(
            self._discord_deferred_sequence, deadline, item)
        self._discord_deferred_sequence += 1
        self._discord_deferred.append(entry)
        self._record_delivery(item, "offline_deferred")
        self._ensure_discord_deferred_worker()
        logger.warning(
            "Discord 尚未就绪，消息暂存至断线窗口/TTL 截止 "
            "source={} target={} remaining={:.1f}s",
            item.source.value,
            self._describe_target(item.target),
            max(0.0, deadline - now),
        )
        return True

    def discord_bot_connected(self, bot) -> None:
        """记录 Gateway 连接候选；等待 Ready/Resumed 后才允许发送。"""
        self._activate_discord_dm_cache(bot)
        self._discord_ready_event.clear()
        if self._discord_offline_since is None:
            self._discord_offline_since = time.time()
        if (self._discord_prewarm_task is not None
                and not self._discord_prewarm_task.done()):
            self._discord_prewarm_task.cancel()
        self._discord_prewarm_task = None

    def discord_session_ready(self, bot) -> None:
        """收到 Ready/Resumed 后释放暂存并开始 DM 通道预热。"""
        if bot is not self._discord_dm_bot:
            return
        offline_since = self._discord_offline_since
        self._discord_offline_since = None
        self._discord_ready_event.set()
        self._ensure_discord_deferred_worker()
        if (self._discord_prewarm_task is not None
                and not self._discord_prewarm_task.done()):
            self._discord_prewarm_task.cancel()
        self._discord_prewarm_task = asyncio.create_task(
            self._prewarm_discord_dms(bot))
        if offline_since is not None:
            logger.info(
                "Discord Gateway 会话已就绪，断线持续 {:.1f}s",
                max(0.0, time.time() - offline_since),
            )

    def discord_bot_disconnected(self, bot) -> None:
        if bot is not self._discord_dm_bot:
            return
        self._discord_ready_event.clear()
        if self._discord_offline_since is None:
            self._discord_offline_since = time.time()
        if (self._discord_prewarm_task is not None
                and not self._discord_prewarm_task.done()):
            self._discord_prewarm_task.cancel()
        self._discord_prewarm_task = None
        self._discord_dm_bot = None
        self._discord_dm_channels.clear()
        self._discord_dm_locks.clear()
        self._discord_global_rate_limit_until = 0.0
        self._discord_target_rate_limit_until.clear()
        self._discord_route_rate_limit_until.clear()

    @staticmethod
    async def _send_discord_message(
            bot, channel_id: int, message, nonce: str | None = None) -> None:
        if nonce is None:
            await bot.send_to(channel_id, message)
            return
        from nonebot.adapters.discord import Message, MessageSegment
        from nonebot.adapters.discord.message import parse_message

        sendable = (
            MessageSegment.text(message) if isinstance(message, str)
            else message
        )
        sendable = sendable if isinstance(sendable, Message) else Message(
            sendable)
        await bot.create_message(
            channel_id=channel_id,
            nonce=nonce,
            enforce_nonce=True,
            **parse_message(sendable.sendable()),
        )

    async def _send_to(
            self, target, message, *, nonce: str | None = None) -> None:
        """按 target 分派：群号 int / ('group',gid) -> 群发；('private',qq) -> 私聊。"""
        if isinstance(target, int) and target < 0:
            route = self.store.get_target(target)
            if not route or not route["active"]:
                raise RuntimeError("Discord 私聊目标未启用")
            if route["platform"] != "discord":
                raise RuntimeError(f"暂不支持平台 {route['platform']}")
            bot = SniperPoller._adapter_bot("Discord")
            if bot is None:
                raise RuntimeError("Discord Bot 未连接")
            recipient_id = int(route["external_id"])
            channel_id = await self._discord_dm_channel(bot, recipient_id)
            await self._wait_for_discord_route(
                f"/channels/{channel_id}/messages")
            try:
                await self._send_discord_message(
                    bot, channel_id, message, nonce)
            except DiscordActionFailed as error:
                if error.code != self._DISCORD_UNKNOWN_CHANNEL:
                    raise
                # 仅 Unknown Channel 能确定消息未送达，可安全重建并重发一次。
                self._discord_dm_channels.pop(recipient_id, None)
                channel_id = await self._discord_dm_channel(bot, recipient_id)
                await self._wait_for_discord_route(
                    f"/channels/{channel_id}/messages")
                await self._send_discord_message(
                    bot, channel_id, message, nonce)
            return

        bot = SniperPoller._adapter_bot("OneBot V11")
        if bot is None:
            raise RuntimeError("OneBot 未连接")
        if isinstance(target, tuple):
            kind, tid = target
            if kind == "private":
                await bot.send_private_msg(user_id=tid, message=message)
                return
            target = tid  # ('group', gid)
        await bot.send_group_msg(group_id=target, message=message)

    async def _queue_item_is_ready(
            self, queue: asyncio.Queue, item: DeliveryItem,
            *, after_render: bool) -> bool:
        """在渲染前和真正发送前执行目标状态、开关与黑名单检查。"""
        target, payload = item.target, item.payload

        if item.expired(time.time()):
            self._record_delivery(item, "expired")
            logger.info(
                "消息已过 TTL，取消 source={} target={}",
                item.source.value, self._describe_target(target))
            return False
        if not self._delivery_is_current(item):
            self._record_delivery(item, "cancelled")
            logger.info(
                "消息来源代次已撤销，取消 source={} target={}",
                item.source.value, self._describe_target(target))
            return False

        if not self._target_is_active(target):
            self._record_delivery(item, "target_inactive")
            stage = "在渲染期间" if after_render else ""
            logger.info("目标状态{}失效，取消推送到{}",
                        stage, self._describe_target(target))
            return False

        if not self._channel_payload_is_allowed(target, payload):
            self._record_delivery(item, "channel_disabled_or_unmatched")
            stage = "在渲染期间" if after_render else ""
            logger.info("目标频道消息开关{}已关闭或频道紫卡不再命中狙击配置，取消推送到{}",
                        stage, self._describe_target(target))
            return False

        removed = self._filter_blocked_hit_batch(target, payload)
        if removed:
            counters = self._delivery_counters.setdefault(
                item.source.value, Counter())
            counters["blacklisted_items"] += removed
            logger.info(
                "目标 {} 的 WM 合并消息移除 {} 条新近拉黑卖家命中",
                target, removed)
            if not payload.cards:
                return False

        blocked_seller = self._blocked_hit_seller(target, payload)
        if blocked_seller:
            self._record_delivery(item, "blacklisted")
            stage = "已渲染的" if after_render else "队列中的"
            source = "频道" if isinstance(payload, ChannelPushPayload) else "WM"
            logger.info("卖家 {} 已在目标 {} 的 {} 黑名单，取消{}狙击推送",
                        blocked_seller, target, source, stage)
            return False

        return True

    def _pipeline_acquire(self, queue: asyncio.Queue) -> None:
        key = id(queue)
        self._pipeline_inflight[key] = self._pipeline_inflight.get(key, 0) + 1

    def _pipeline_release(
            self, queue: asyncio.Queue, slots: asyncio.Semaphore) -> None:
        key = id(queue)
        remaining = self._pipeline_inflight.get(key, 1) - 1
        if remaining > 0:
            self._pipeline_inflight[key] = remaining
        else:
            self._pipeline_inflight.pop(key, None)
        slots.release()

    async def _get_scheduled_delivery(
            self, source: asyncio.Queue) -> object:
        """从入口队列按有限时间优势取下一条，同时保持 Queue 记账语义。"""
        if isinstance(source, _ScheduledDeliveryQueue):
            return await source.get()
        first = await source.get()
        if not isinstance(first, DeliveryItem):
            return first
        queued = getattr(source, "_queue", None)
        if not queued:
            return first
        best = first
        best_index = -1
        best_key = self._delivery_schedule_key(first, 0)
        for index, candidate in enumerate(queued):
            if not isinstance(candidate, DeliveryItem):
                continue
            candidate_key = self._delivery_schedule_key(candidate, index + 1)
            if candidate_key < best_key:
                best = candidate
                best_index = index
                best_key = candidate_key
        if best_index >= 0:
            del queued[best_index]
            queued.appendleft(first)
        return best

    async def _render_feed_loop(
            self, source: asyncio.Queue, render_queue: asyncio.Queue,
            slots: asyncio.Semaphore,
            inflight_items: dict[int, DeliveryItem]) -> None:
        sequence_by_stream: dict[
            tuple[tuple[str, str, str], DeliverySource], int
        ] = {}
        next_token = 0
        while True:
            await slots.acquire()
            tracked = False
            try:
                item = await self._get_scheduled_delivery(source)
                if not isinstance(item, DeliveryItem):
                    source.task_done()
                    raise TypeError(
                        f"发送队列只接受 DeliveryItem，实际为 {type(item).__name__}")
                token = next_token
                next_token += 1
                inflight_items[token] = item
                self._inflight_deliveries[id(item)] = item
                if not await self._queue_item_is_ready(
                        source, item, after_render=False):
                    inflight_items.pop(token, None)
                    self._inflight_deliveries.pop(id(item), None)
                    source.task_done()
                    slots.release()
                    continue
                self._pipeline_acquire(source)
                tracked = True
                target_key = self._target_send_key(item.target)
                stream_key = (target_key, item.source)
                sequence = sequence_by_stream.get(stream_key, 0)
                sequence_by_stream[stream_key] = sequence + 1
                await render_queue.put((stream_key, sequence, token, item))
            except BaseException:
                if tracked:
                    self._pipeline_release(source, slots)
                else:
                    slots.release()
                raise

    async def _render_worker_loop(
            self, render_queue: asyncio.Queue,
            rendered_queue: asyncio.Queue) -> None:
        while True:
            stream_key, sequence, token, item = await render_queue.get()
            payload = item.payload
            try:
                message = await self._render_payload_for_target(
                    item.target, payload)
                prepared = _PreparedPush(token, item, message)
            except Exception:
                logger.exception("命中卡渲染失败，丢弃该条消息")
                prepared = _PreparedPush(
                    token, item, None, render_failed=True)
            await rendered_queue.put((stream_key, sequence, prepared))

    @staticmethod
    async def _render_order_loop(
            rendered_queue: asyncio.Queue, ready_queue: asyncio.Queue) -> None:
        next_sequence: dict[object, int] = {}
        pending: dict[object, dict[int, _PreparedPush]] = {}
        while True:
            stream_key, sequence, prepared = await rendered_queue.get()
            target_pending = pending.setdefault(stream_key, {})
            target_pending[sequence] = prepared
            expected = next_sequence.get(stream_key, 0)
            while expected in target_pending:
                await ready_queue.put(target_pending.pop(expected))
                expected += 1
            next_sequence[stream_key] = expected
            if not target_pending:
                pending.pop(stream_key, None)

    @classmethod
    def _delivery_schedule_key(
            cls, item: DeliveryItem, token: int) -> tuple[float, float, int]:
        advance = cls._SOURCE_SCHEDULE_ADVANCE.get(item.source, 0.0)
        return item.enqueued_at - advance, item.enqueued_at, token

    def _schedule_send_retry(
            self, item: DeliveryItem, payload, message) -> bool:
        max_retries = int(getattr(self.config, "send_max_retries", 1))
        if item.attempts >= max_retries:
            return False
        retry_payload = payload if isinstance(
            payload, (
                _HitCard, _HitBatch, bargain.BargainItemPushPayload,
                bargain.BargainRivenPushPayload, ChannelPushPayload,
            )) else message
        retry_item = DeliveryItem(
            source=item.source,
            target=item.target,
            payload=retry_payload,
            attempts=item.attempts + 1,
            enqueued_at=item.enqueued_at,
            expires_at=item.expires_at,
            generation=item.generation,
            observed_at=item.observed_at,
        )
        self._record_delivery(item, "retry_scheduled")
        self._schedule_retry(retry_item)
        return True

    def _schedule_rate_limit_retry(
            self, item: DeliveryItem, payload, message, delay: float) -> bool:
        if time.time() + delay >= item.expires_at:
            self._record_delivery(item, "expired")
            return False
        retry_payload = payload if isinstance(
            payload, (
                _HitCard, _HitBatch, bargain.BargainItemPushPayload,
                bargain.BargainRivenPushPayload, ChannelPushPayload,
            )) else message
        retry_item = DeliveryItem(
            source=item.source,
            target=item.target,
            payload=retry_payload,
            attempts=item.attempts + 1,
            enqueued_at=item.enqueued_at,
            expires_at=item.expires_at,
            generation=item.generation,
            observed_at=item.observed_at,
        )
        self._record_delivery(item, "retry_scheduled")
        self._schedule_requeue(retry_item, delay)
        return True

    async def _send_prepared_job(
            self, source: asyncio.Queue, slots: asyncio.Semaphore,
            interval_getter, prepared: _PreparedPush,
            inflight_items: dict[int, DeliveryItem]) -> None:
        slot_released = False
        global_slot_acquired = False
        resolved = False
        try:
            if prepared.render_failed:
                self._record_delivery(prepared.item, "render_failed")
                resolved = True
                return
            item = prepared.item
            target = item.target
            payload = item.payload
            message = prepared.message
            if not await self._queue_item_is_ready(
                    source, item, after_render=True):
                resolved = True
                return

            if (not self.config.sniper_dry_run
                    and not self._target_bot_connected(target)):
                if not self._defer_discord_delivery(item):
                    self._record_delivery(item, "bot_offline")
                    logger.warning(
                        "Bot 不可发送，直接丢弃时效消息 source={} target={}",
                        item.source.value, self._describe_target(target))
                resolved = True
                return

            message = await self._render_payload_for_target(
                target, payload, message)
            if not self.config.sniper_dry_run:
                await self._wait_for_discord_rate_limit(target)
                await self._acquire_global_send_slot()
                global_slot_acquired = True
                # 等待并发许可期间 TTL、来源代次或目标状态可能已经变化。
                if not await self._queue_item_is_ready(
                        source, item, after_render=True):
                    resolved = True
                    return
                if not self._target_bot_connected(target):
                    if not self._defer_discord_delivery(item):
                        self._record_delivery(item, "bot_offline")
                        logger.warning(
                            "Bot 在发送许可等待期间离线，丢弃 "
                            "source={} target={}",
                            item.source.value,
                            self._describe_target(target),
                        )
                    resolved = True
                    return
                # 全局发送许可等待期间也可能切换语言；实际投递前再同步一次。
                message = await self._render_payload_for_target(
                    target, payload, message)

            # 已经开始当前目标的发送，不再占用预渲染窗口。因此默认模式下
            # 不同目标数量不受 _RENDER_AHEAD 限制。
            if not slot_released:
                self._pipeline_release(source, slots)
                slot_released = True
            # 从这里开始已经调用实际投递路径。若任务在 API 调用期间被取消，
            # 不能把结果未知的消息重新收回队列，否则会形成非发送失败触发的重试。
            resolved = True
            api_started_at = time.time()
            api_started_loop = asyncio.get_running_loop().time()
            attempt_outcome = "sent"
            try:
                if self.config.sniper_dry_run:
                    logger.info("[DRY-RUN] -> {}:\n{}",
                                self._describe_target(target), message)
                elif isinstance(payload, _HitBatch):
                    await self._send_to(
                        target, message, nonce=payload.nonce)
                else:
                    await self._send_to(target, message)
                STATS.record_push(
                    True, items=self._payload_item_count(payload))
                self._record_delivery(item, "sent")
                self._sender_last_success_at = time.time()
            except asyncio.CancelledError:
                attempt_outcome = "send_cancelled"
                self._record_delivery(item, "send_cancelled")
                raise
            except DiscordRateLimitException as error:
                self._record_delivery(item, "rate_limited")
                delay = self._discord_retry_delay(error, item.attempts)
                self._note_discord_rate_limit(target, error, delay)
                if self._schedule_rate_limit_retry(
                        item, payload, message, delay):
                    attempt_outcome = "rate_limited_retry"
                    logger.warning(
                        "推送到{}触发 Discord 限流，按服务端要求 {:.2f}s 后重试",
                        self._describe_target(target), delay)
                else:
                    attempt_outcome = "rate_limited_expired"
                    STATS.record_push(
                        False, items=self._payload_item_count(payload))
                    logger.warning(
                        "推送到{}触发 Discord 限流，等待 {:.2f}s 后将超过 TTL，"
                        "按既有时效规则放弃",
                        self._describe_target(target), delay)
            except SnowLumaOutcomeUnknown as error:
                attempt_outcome = "outcome_unknown"
                STATS.record_push(
                    False, items=self._payload_item_count(payload))
                self._record_delivery(item, "outcome_unknown")
                logger.error(
                    "推送到{}的 SnowLuma HTTP action 结果未知；"
                    "为避免重复消息不自动重试: {}",
                    self._describe_target(target), error,
                )
            except NetworkError as error:
                max_retries = int(getattr(
                    self.config, "send_max_retries", 1))
                if self._schedule_send_retry(item, payload, message):
                    attempt_outcome = "network_retry"
                    logger.warning("推送到{}失败（第 {}/{} 次），稍后重试: {}",
                                   self._describe_target(target),
                                   item.attempts + 1, max_retries, error)
                else:
                    attempt_outcome = "network_failed"
                    STATS.record_push(
                        False, items=self._payload_item_count(payload))
                    self._record_delivery(item, "send_failed")
                    logger.error("推送到{}重试 {} 次后仍失败，放弃该条消息: {}",
                                 self._describe_target(target),
                                 max_retries, error)
            except Exception as error:
                attempt_outcome = "send_failed"
                STATS.record_push(
                    False, items=self._payload_item_count(payload))
                self._record_delivery(item, "send_failed")
                logger.opt(exception=error).error(
                    "推送到{}发生不可重试错误，放弃该条消息: {}: {!r}",
                    self._describe_target(target),
                    type(error).__name__,
                    error,
                )
            finally:
                if not self.config.sniper_dry_run:
                    api_finished_at = time.time()
                    STATS.record_delivery_timing(
                        item.source.value,
                        platform=self._target_send_key(target)[0],
                        outcome=attempt_outcome,
                        queue_seconds=api_started_at - item.enqueued_at,
                        api_seconds=(asyncio.get_running_loop().time()
                                     - api_started_loop),
                        source_seconds=(
                            None if item.observed_at is None
                            else api_started_at - item.observed_at),
                        source_end_seconds=(
                            None if item.observed_at is None
                            else api_finished_at - item.observed_at),
                    )
            # 投递结果已经明确（成功、重试已排程或永久失败），后续仅剩
            # 当前目标的节流等待；此时发生流水线重启不应重复回收本消息。
            if global_slot_acquired:
                await self._release_global_send_slot()
                global_slot_acquired = False

            # 间隔只约束当前目标，其它目标仍可继续发送。
            await self._wait_for_runtime_delay(
                asyncio.get_running_loop().time(), interval_getter)
        finally:
            if resolved:
                inflight_items.pop(prepared.token, None)
                self._inflight_deliveries.pop(id(prepared.item), None)
                source.task_done()
            if global_slot_acquired:
                await self._release_global_send_slot()
            if not slot_released:
                self._pipeline_release(source, slots)

    async def _target_prepared_send_loop(
            self, source: asyncio.Queue, target_queue: asyncio.PriorityQueue,
            slots: asyncio.Semaphore, interval_getter,
            inflight_items: dict[int, DeliveryItem]) -> None:
        while True:
            _schedule_key, prepared = await target_queue.get()
            try:
                await self._send_prepared_job(
                    source, slots, interval_getter, prepared,
                    inflight_items)
            finally:
                target_queue.task_done()

    async def _prepared_send_loop(
            self, source: asyncio.Queue, ready_queue: asyncio.Queue,
            slots: asyncio.Semaphore, interval_getter,
            inflight_items: dict[int, DeliveryItem]) -> None:
        # 每个真实目标只有一个发送 worker；准备完成的不同来源可按有限时间
        # 优势重新排序，避免大量任务先到先占住目标锁造成队首阻塞。
        target_queues: dict[
            tuple[str, str, str], asyncio.PriorityQueue
        ] = {}
        async with asyncio.TaskGroup() as jobs:
            while True:
                prepared = await ready_queue.get()
                target_key = self._target_send_key(prepared.item.target)
                target_queue = target_queues.get(target_key)
                if target_queue is None:
                    target_queue = asyncio.PriorityQueue()
                    target_queues[target_key] = target_queue
                    jobs.create_task(self._target_prepared_send_loop(
                        source, target_queue, slots, interval_getter,
                        inflight_items))
                target_queue.put_nowait((self._delivery_schedule_key(
                    prepared.item, prepared.token), prepared))

    async def _send_loop(self, queue: asyncio.Queue, interval_getter):
        """主通道的有界两级发送流水线。

        并行预渲染后按目标独立串行。信号量确保最多只有
        ``_RENDER_AHEAD`` 条已取出入口队列，避免成品图无界积压。
        """
        render_queue: asyncio.Queue = asyncio.Queue(maxsize=self._RENDER_AHEAD)
        rendered_queue: asyncio.Queue = asyncio.Queue(maxsize=self._RENDER_AHEAD)
        ready_queue: asyncio.Queue = asyncio.Queue(maxsize=self._RENDER_AHEAD)
        slots = asyncio.Semaphore(self._RENDER_AHEAD)
        inflight_items: dict[int, DeliveryItem] = {}
        tasks = [
            asyncio.create_task(
                self._render_feed_loop(
                    queue, render_queue, slots, inflight_items)),
            *(asyncio.create_task(
                self._render_worker_loop(render_queue, rendered_queue))
              for _ in range(self._RENDER_WORKERS)),
            asyncio.create_task(
                self._render_order_loop(rendered_queue, ready_queue)),
            asyncio.create_task(
                self._prepared_send_loop(
                    queue, ready_queue, slots, interval_getter,
                    inflight_items)),
        ]
        try:
            done, _ = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if not task.cancelled() and task.exception() is not None:
                    raise task.exception()
            raise RuntimeError("主发送流水线阶段意外结束")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            recovered = [
                inflight_items[token] for token in sorted(inflight_items)
            ]
            if recovered:
                # asyncio.Queue 没有队首插入接口。同步排空再重建可确保故障前
                # 已取出的旧消息排在故障期间新入队消息之前，且不会与消费者竞态。
                queued = []
                while True:
                    try:
                        queued.append(queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                for _ in range(len(recovered) + len(queued)):
                    queue.task_done()
                for item in (*recovered, *queued):
                    self._inflight_deliveries.pop(id(item), None)
                    self.enqueue_delivery(item)
                logger.warning("主发送流水线已按原顺序回收 {} 条在途消息",
                               len(recovered))
            inflight_items.clear()
            for item in recovered:
                self._inflight_deliveries.pop(id(item), None)
            self._pipeline_inflight.pop(id(queue), None)

    @staticmethod
    def _sender_error_summary(exc: BaseException) -> str:
        """展开 TaskGroup 异常外壳，保留真正导致重启的错误摘要。"""
        if isinstance(exc, BaseExceptionGroup):
            parts = [
                SniperPoller._sender_error_summary(child)
                for child in exc.exceptions
            ]
            return " | ".join(dict.fromkeys(parts))
        return f"{type(exc).__name__}: {exc}"

    async def _supervise_send_loop(self, queue: asyncio.Queue,
                                   interval_getter) -> None:
        """监督主发送流水线，异常时回收在途消息并指数退避重启。"""
        restart_delay = self._SEND_RESTART_INITIAL_SECONDS
        loop = asyncio.get_running_loop()
        try:
            while True:
                started_at = loop.time()
                self._sender_state = "running"
                self._sender_last_started_at = time.time()
                try:
                    await self._send_loop(queue, interval_getter)
                    raise RuntimeError("主发送流水线意外退出")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if (loop.time() - started_at
                            >= self._SEND_RESTART_STABLE_SECONDS):
                        restart_delay = self._SEND_RESTART_INITIAL_SECONDS
                    self._sender_restart_count += 1
                    self._sender_last_error = self._sender_error_summary(exc)
                    self._sender_last_error_at = time.time()
                    self._sender_state = "restarting"
                    logger.exception(
                        "主发送流水线异常退出，{:.1f}s 后自动重启",
                        restart_delay)
                    await asyncio.sleep(restart_delay)
                    restart_delay = min(
                        self._SEND_RESTART_MAX_SECONDS,
                        max(self._SEND_RESTART_INITIAL_SECONDS,
                            restart_delay * 2),
                    )
        finally:
            self._sender_state = "stopped"
