"""捡漏监控运行时。

普通道具只消费 WFM WebSocket 的新建订单，并与已缓存的 WFM 90 天最新
日桶均价比较；连接断开时只重连，不通过 HTTP 回放历史订单。紫卡继续复用
狙击轮询得到的新拍卖，同时独立按小时采集同武器直售单形成滚动均价。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Iterable, Mapping

import httpx
from nonebot import logger

from . import bargain, marketdata
from .delivery import DeliverySource
from .store import Store
from .wfm import WfmClient


class BargainPoller:
    _ITEM_STATS_REFRESH_SECONDS = 3600
    _ITEM_STATS_VALID_SECONDS = 2 * _ITEM_STATS_REFRESH_SECONDS
    _CONFIG_SCAN_SECONDS = 5
    _CATALOG_REFRESH_SECONDS = 24 * 3600
    _BARO_CALIBRATION_SECONDS = 6 * 3600
    _BARO_NO_SCHEDULE_SECONDS = 3600
    _BARO_INCOMPLETE_RETRY_SECONDS = 60
    _BARO_BOUNDARY_GRACE_SECONDS = 5
    _BARO_FAILURE_INITIAL_SECONDS = 60
    _BARO_FAILURE_MAX_SECONDS = 3600
    _WS_RECONNECT_MAX_SECONDS = 60
    _PENDING_ITEM_MAX_AGE_SECONDS = 300
    _PENDING_ITEM_MAX_ORDERS = 1000

    def __init__(self, store: Store, config, delivery_poller):
        self.store = store
        self.config = config
        self.wfm = WfmClient()
        self._delivery_poller = delivery_poller
        self._tasks: list[asyncio.Task] = []
        self._item_stats_attempted_at: dict[str, float] = {}
        self._item_stats_retry_after: dict[str, float] = {}
        self._item_stats_signatures: dict[str, frozenset[str | None]] = {}
        self._item_stats_verified_at: dict[tuple[str, str], float] = {}
        self._pending_item_orders: dict[str, tuple[float, str, dict]] = {}
        self._known_riven_configs: set[tuple[str, int]] | None = None
        self._requested_riven_samples: set[str] = set()
        self._riven_retry_after: dict[str, float] = {}
        self._sample_wakeup = asyncio.Event()
        self._baro_ready = asyncio.Event()
        self._baro_stock: tuple[tuple[str, str, int], ...] = ()
        self._next_baro_activation_ts: int | None = None
        self._baro_next_delay_seconds = self._BARO_NO_SCHEDULE_SECONDS

    def start(self):
        self._tasks.extend([
            asyncio.create_task(self._order_stream_loop()),
            asyncio.create_task(self._item_statistics_loop()),
            asyncio.create_task(self._riven_sample_loop()),
            asyncio.create_task(self._catalog_loop()),
            asyncio.create_task(self._baro_loop()),
        ])
        logger.info(
            "bargain core started (ordinary=WebSocket only, riven={}h/{} samples)",
            bargain.params()["riven_rolling_hours"],
            bargain.params()["riven_min_valid_samples"])

    async def stop(self):
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.wfm.close()

    # ---- 监控配置 ----

    def _active_delivery_scopes(self) -> set[int]:
        enabled_platforms = (
            ("discord",)
            if getattr(self.config, "discord_dm_enabled", False) else ())
        return self.store.active_delivery_scope_ids(platforms=enabled_platforms)

    def _configured_group_items(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        allowed_groups = self._active_delivery_scopes()
        for row in self.store.list_bargain_items():
            if row["group_id"] in allowed_groups:
                out.setdefault(row["slug"], []).append(row)
        return out

    def _watched_group_items(self) -> dict[str, list[dict]]:
        return self._configured_group_items()

    def _configured_item_rows(self) -> dict[str, list[dict]]:
        return self._configured_group_items()

    def _watched_group_rivens(self) -> dict[str, list[dict]]:
        allowed_groups = self._active_delivery_scopes()
        out: dict[str, list[dict]] = {}
        for row in self.store.list_bargain_riven_items():
            gid = row["group_id"]
            if gid in allowed_groups:
                out.setdefault(row["weapon_slug"], []).append(row)
        return out

    def _configured_riven_rows(self) -> list[tuple[tuple[str, int], str]]:
        rows: list[tuple[tuple[str, int], str]] = []
        allowed_groups = self._active_delivery_scopes()
        for row in self.store.list_bargain_riven_items():
            if row["group_id"] in allowed_groups:
                rows.append((("qq", int(row["id"])), row["weapon_slug"]))
        return rows

    # ---- 普通道具：全市场新建订单 WebSocket ----

    async def _order_stream_loop(self):
        delay = 1.0
        while True:
            try:
                async for order in self.wfm.new_order_events():
                    delay = 1.0
                    try:
                        await self.handle_new_item_order(order)
                    except Exception:
                        logger.exception("普通道具 WebSocket 单条订单处理失败")
                logger.warning("普通道具 WebSocket 已结束，准备重连（不补历史单）")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "普通道具 WebSocket 断开: {}；{:.0f}s 后重连（不补历史单）",
                    exc, delay)
            await asyncio.sleep(delay)
            delay = min(self._WS_RECONNECT_MAX_SECONDS, delay * 2)

    @staticmethod
    def _unwrap_order(payload: Mapping) -> Mapping:
        nested = payload.get("order")
        return nested if isinstance(nested, Mapping) else payload

    def _expire_pending_item_orders(self, now: float) -> None:
        cutoff = now - self._PENDING_ITEM_MAX_AGE_SECONDS
        expired = [
            order_id for order_id, (received_at, _slug, _payload)
            in self._pending_item_orders.items()
            if received_at < cutoff
        ]
        for order_id in expired:
            self._pending_item_orders.pop(order_id, None)
        if expired:
            self.store.mark_bargain_item_orders_seen(expired, int(now))

    def _defer_item_order(self, order_id: str, slug: str, payload: Mapping,
                          *, received_at: float) -> None:
        self._expire_pending_item_orders(time.time())
        if order_id in self._pending_item_orders:
            return
        overflow: list[str] = []
        while len(self._pending_item_orders) >= self._PENDING_ITEM_MAX_ORDERS:
            oldest = next(iter(self._pending_item_orders))
            self._pending_item_orders.pop(oldest, None)
            overflow.append(oldest)
        if overflow:
            self.store.mark_bargain_item_orders_seen(overflow)
        self._pending_item_orders[order_id] = (
            received_at, slug, dict(payload))

    async def _flush_pending_item_orders(self, slug: str | None = None) -> int:
        now = time.time()
        self._expire_pending_item_orders(now)
        delivered = 0
        pending = list(self._pending_item_orders.items())
        for order_id, (received_at, item_slug, payload) in pending:
            if slug is not None and item_slug != slug:
                continue
            current = self._pending_item_orders.get(order_id)
            if current is None or current[0] != received_at:
                continue
            self._pending_item_orders.pop(order_id, None)
            delivered += await self.handle_new_item_order(
                payload, _received_at=received_at)
        return delivered

    async def handle_new_item_order(
            self, payload: Mapping, *, _received_at: float | None = None) -> int:
        """只对一条 WS 新建事件判断一次，返回成功投递目标数。"""
        order = self._unwrap_order(payload)
        if order.get("type") != "sell" or not order.get("visible", True):
            return 0
        order_id = str(order.get("id") or "")
        if not order_id:
            return 0
        item_id = str(order.get("itemId") or order.get("item_id") or "")
        slug = marketdata.id_to_slug(item_id)
        if not slug:
            return 0
        group_rows = self._watched_group_items().get(slug, [])
        if not group_rows:
            return 0
        if self.store.is_bargain_item_order_seen(order_id):
            return 0
        now = time.time()
        received_at = now if _received_at is None else _received_at
        # 同一 WS 事件在初始化队列中只保留首次收到的内容；后续同 ID 更新
        # 仍不被当成新订单。
        if (_received_at is None
                and order_id in self._pending_item_orders):
            return 0
        if (not self._baro_ready.is_set()
                or (self._next_baro_activation_ts is not None
                    and now >= self._next_baro_activation_ts)):
            self._defer_item_order(
                order_id, slug, payload, received_at=received_at)
            return 0

        max_rank = marketdata.item_max_rank(slug)
        bucket = bargain.item_bucket_of(order, max_rank)
        baseline = self.store.get_bargain_item_daily_baseline(slug, bucket)
        verified_at = self._item_stats_verified_at.get((slug, bucket))
        if (baseline is None or verified_at is None):
            if slug not in self._item_stats_attempted_at:
                self._defer_item_order(
                    order_id, slug, payload, received_at=received_at)
                return 0
            # 本进程已成功读取该道具统计但目标等级/规格仍不存在：本次新单
            # 按规则忽略，不能等未来日桶出现后把它当作新单补推。
            self.store.mark_bargain_item_order_seen(order_id)
            return 0
        if now - verified_at > self._ITEM_STATS_VALID_SECONDS:
            self._defer_item_order(
                order_id, slug, payload, received_at=received_at)
            return 0
        # 前置状态已经就绪，至此才永久消费事件；初始化期间收到的订单会由
        # 内存队列恰好重试一次，而断线历史仍绝不通过 HTTP 回补。
        if not self.store.mark_bargain_item_order_seen(order_id):
            return 0
        activation_ts = self._baro_activation_for_slug(slug)
        if (activation_ts is not None
                and int(baseline["source_ts"]) <= activation_ts):
            return 0
        if activation_ts is not None:
            self.store.resume_bargain_baro_pauses_for_baseline(
                slug, bucket, int(baseline["source_ts"]))
        if (self.store.is_bargain_item_paused(slug, bucket)
                or self.store.is_bargain_item_paused(slug, "*")):
            return 0
        price = bargain.as_decimal(order.get("platinum"))
        if price is None or price <= 0:
            return 0
        seller = str((order.get("user") or {}).get("ingameName") or "")
        delivered = 0

        for row in group_rows:
            if not bargain.level_allowed(order.get("rank"), row.get("level"),
                                         max_rank):
                continue
            hit = bargain.evaluate_price(
                price, baseline["price"], bargain.item_threshold_of(row),
                samples=int(baseline.get("volume") or 0))
            if hit is None:
                continue
            locale = self.store.get_target_preferences(row["group_id"])["locale"]
            delivery_payload = bargain.BargainItemPushPayload(
                slug=slug,
                order=dict(order),
                hit=hit,
                locale=locale,
                target_scope=row["group_id"],
            )
            if await self._enqueue(row["group_id"], delivery_payload):
                delivered += 1

        return delivered

    async def _enqueue(self, group_id: int, payload: object) -> bool:
        return self._delivery_poller.enqueue_delivery(
            self._delivery_poller.new_delivery(
                DeliverySource.BARGAIN, group_id, payload))

    # ---- 普通道具：WFM 90days 最新日桶 ----

    async def _refresh_item_statistics(self, slug: str,
                                       config_rows: Iterable[Mapping]) -> int:
        rows = await self.wfm.item_statistics(slug)
        verified_at = time.time()
        for key in [key for key in self._item_stats_verified_at
                    if key[0] == slug]:
            self._item_stats_verified_at.pop(key, None)
        max_rank = marketdata.item_max_rank(slug)
        levels = {row.get("level") for row in config_rows}
        if max_rank <= 0:
            levels = {None}
        else:
            levels &= {"0", "max"}
        subtypes = {None}
        subtypes.update(str(row["subtype"]) for row in rows
                        if row.get("subtype") not in (None, ""))
        written = 0
        for level in levels:
            target_rank = (None if max_rank <= 0 else
                           0 if level == "0" else max_rank)
            for subtype in subtypes:
                daily = bargain.latest_item_daily_average(
                    rows, target_rank=target_rank, max_rank=max_rank,
                    subtype=subtype)
                if daily is None:
                    continue
                self.store.upsert_bargain_item_daily_baseline(
                    slug, daily.bucket, daily.price,
                    source_id=daily.stat_id,
                    source_datetime=daily.stat_datetime,
                    source_ts=int(daily.stat_timestamp),
                    volume=daily.volume)
                self._item_stats_verified_at[(slug, daily.bucket)] = verified_at
                # 每个等级/规格独立恢复，不能因另一个桶先更新而提前解禁。
                self.store.resume_bargain_baro_pauses_for_baseline(
                    slug, daily.bucket, int(daily.stat_timestamp))
                written += 1
        self._item_stats_attempted_at[slug] = verified_at
        await self._flush_pending_item_orders(slug)
        return written

    async def _item_statistics_tick(self) -> int:
        now = time.time()
        configured = self._configured_item_rows()
        for removed in set(self._item_stats_signatures) - set(configured):
            self._item_stats_signatures.pop(removed, None)
            self._item_stats_attempted_at.pop(removed, None)
            self._item_stats_retry_after.pop(removed, None)
            for key in [key for key in self._item_stats_verified_at
                        if key[0] == removed]:
                self._item_stats_verified_at.pop(key, None)
        refreshed = 0
        for slug, config_rows in configured.items():
            signature = frozenset(row.get("level") for row in config_rows)
            changed = self._item_stats_signatures.get(slug) != signature
            self._item_stats_signatures[slug] = signature
            if now < self._item_stats_retry_after.get(slug, 0):
                continue
            if (not changed
                    and now - self._item_stats_attempted_at.get(slug, 0)
                    < self._ITEM_STATS_REFRESH_SECONDS):
                continue
            try:
                await self._refresh_item_statistics(slug, config_rows)
                self._item_stats_retry_after.pop(slug, None)
                refreshed += 1
            except (httpx.HTTPError, ValueError) as exc:
                retry_after = 0.0
                if isinstance(exc, httpx.HTTPStatusError):
                    raw = exc.response.headers.get("Retry-After", "")
                    try:
                        retry_after = float(raw)
                    except ValueError:
                        pass
                self._item_stats_retry_after[slug] = now + max(
                    30.0, retry_after)
                logger.warning("WM 道具日桶刷新失败 {}: {}", slug, exc)
            except Exception:
                self._item_stats_retry_after[slug] = now + 30.0
                logger.exception("WM 道具日桶刷新异常: {}", slug)
        self._expire_pending_item_orders(time.time())
        return refreshed

    async def _item_statistics_loop(self):
        while True:
            await self._item_statistics_tick()
            await asyncio.sleep(self._CONFIG_SCAN_SECONDS)

    # ---- 紫卡：小时样本与滚动均价 ----

    def request_riven_sample(self, weapon_slug: str) -> None:
        """配置新增后唤醒采样器；已有本小时有效样本会直接复用。"""
        if weapon_slug:
            self._requested_riven_samples.add(weapon_slug)
            self._sample_wakeup.set()

    async def _sample_riven_weapon(self, weapon_slug: str, *,
                                   immediate: bool = False,
                                   now: float | None = None) -> bool:
        now = time.time() if now is None else float(now)
        sample_hour = self.store.bargain_utc_hour(now)
        existing = self.store.list_bargain_riven_samples(
            weapon_slug, since_ts=sample_hour, until_ts=int(now))
        if immediate and existing:
            return False
        attempt = self.store.get_bargain_riven_sample_attempt(weapon_slug)
        if (not immediate and attempt is not None
                and int(attempt["attempt_hour"]) >= sample_hour):
            return False
        auctions = await self.wfm.riven_search(weapon_slug)
        # 只有请求成功后才封闭本小时；网络失败由上层退避后重试。
        self.store.claim_bargain_riven_sample_hour(
            weapon_slug, sample_hour=sample_hour, attempted_at=int(now))
        sample = bargain.build_riven_hour_sample(auctions)
        if sample is None:
            return False
        return self.store.add_bargain_riven_sample(
            weapon_slug, sample.price, sample.order_count, sample.spread,
            sampled_at=int(now), sample_hour=sample_hour)

    async def _riven_sample_tick(self) -> int:
        configured = self._configured_riven_rows()
        current_keys = {key for key, _weapon in configured}
        new_keys = (set() if self._known_riven_configs is None else
                    current_keys - self._known_riven_configs)
        self._known_riven_configs = current_keys
        immediate_weapons = {
            weapon for key, weapon in configured if key in new_keys}
        immediate_weapons |= self._requested_riven_samples
        all_weapons = {weapon for _key, weapon in configured}
        pending = deque(sorted(all_weapons))
        sampled = 0
        while pending or self._requested_riven_samples:
            # 控制台/命令在长扫描期间新增的武器插到下一位，避免受保守的
            # 合约搜索限速影响而排在整条小时队列末尾。
            requested = sorted(self._requested_riven_samples)
            self._requested_riven_samples.clear()
            for weapon in reversed(requested):
                immediate_weapons.add(weapon)
                try:
                    pending.remove(weapon)
                except ValueError:
                    pass
                pending.appendleft(weapon)
            if not pending:
                break
            weapon = pending.popleft()
            if time.time() < self._riven_retry_after.get(weapon, 0):
                continue
            try:
                if await self._sample_riven_weapon(
                        weapon, immediate=weapon in immediate_weapons):
                    sampled += 1
                self._riven_retry_after.pop(weapon, None)
            except httpx.HTTPError as exc:
                retry_after = 0.0
                if isinstance(exc, httpx.HTTPStatusError):
                    raw = exc.response.headers.get("Retry-After", "")
                    try:
                        retry_after = float(raw)
                    except ValueError:
                        pass
                self._riven_retry_after[weapon] = time.time() + max(
                    30.0, retry_after)
                logger.warning("紫卡小时样本采集失败 {}: {}", weapon, exc)
            except Exception:
                self._riven_retry_after[weapon] = time.time() + 30.0
                logger.exception("紫卡小时样本采集异常: {}", weapon)
        return sampled

    async def _riven_sample_loop(self):
        while True:
            await self._riven_sample_tick()
            self._sample_wakeup.clear()
            try:
                await asyncio.wait_for(
                    self._sample_wakeup.wait(), self._CONFIG_SCAN_SECONDS)
            except asyncio.TimeoutError:
                pass

    def _riven_rolling_baseline(
            self, weapon_slug: str, now: float) -> tuple[object | None, int]:
        current = bargain.params()
        hours = int(current["riven_rolling_hours"])
        minimum = int(current["riven_min_valid_samples"])
        since_ts = max(0, int(now) - hours * 3600)
        samples = self.store.list_bargain_riven_samples(
            weapon_slug, since_ts=since_ts)
        return bargain.rolling_riven_average(
            samples, now=now, hours=hours, min_samples=minimum)

    async def on_fresh_riven_auctions(self, auctions: list[dict]):
        """检查狙击轮询发现的新紫卡；与狙击命中、黑名单完全独立。"""
        group_rows = self._watched_group_rivens()
        watched = set(group_rows)
        if not watched:
            return 0
        now = time.time()
        delivered_total = 0
        for auction in auctions:
            item = auction.get("item") or {}
            weapon = str(item.get("weapon_url_name") or "")
            auction_id = str(auction.get("id") or "")
            if not weapon or weapon not in watched or not auction_id:
                continue
            if self.store.is_bargain_riven_notified(auction_id):
                continue
            price = bargain.direct_buyout_price(auction)
            if price is None or not bargain.is_fresh_riven_listing(
                    auction, now=now):
                continue
            baseline, sample_count = self._riven_rolling_baseline(weapon, now)
            if baseline is None:
                continue
            seller = str((auction.get("owner") or {}).get("ingame_name") or "")
            delivered = 0
            for row in group_rows.get(weapon, []):
                hit = bargain.evaluate_price(
                    price, baseline, bargain.riven_threshold_of(row),
                    samples=sample_count)
                if hit is None:
                    continue
                locale = self.store.get_target_preferences(
                    row["group_id"])["locale"]
                payload = bargain.BargainRivenPushPayload(
                    weapon_slug=weapon,
                    auction=dict(auction),
                    hit=hit,
                    locale=locale,
                    target_scope=row["group_id"],
                )
                if await self._enqueue(row["group_id"], payload):
                    delivered += 1

            if delivered:
                self.store.mark_bargain_riven_notified(auction_id, int(now))
                delivered_total += delivered
        return delivered_total

    # ---- 本地目录：启动立即检查、之后每日检查 ----

    def _reconcile_item_config_levels(self) -> int:
        """目录等级语义变化后保持所有配置可解释、可操作。"""
        if not marketdata.available():
            return 0
        known = marketdata.items()
        changed = 0
        for row in self.store.list_bargain_items():
            if row["slug"] not in known:
                continue
            max_rank = marketdata.item_max_rank(row["slug"])
            level = row.get("level")
            if max_rank <= 0 and level is not None:
                self.store.set_bargain_item_level(
                    row["id"], row["group_id"], None)
                changed += 1
            elif max_rank > 0 and level not in {"0", "max"}:
                # Bot 目标的普通捡漏条目不再有停用状态；目录后来补充等级
                # 语义时，固定归入满级桶，避免保留下来却永远不参与轮询。
                self.store.set_bargain_item_level(
                    row["id"], row["group_id"], "max")
                changed += 1
        if changed:
            logger.warning("道具目录等级语义变化，已规范化 {} 条捡漏配置", changed)
        return changed

    async def _catalog_loop(self):
        while True:
            self._reconcile_item_config_levels()
            try:
                if await marketdata.refresh_catalog():
                    logger.info("WFM 道具目录刷新成功（含 maxRank/gameRef）")
                    self._reconcile_item_config_levels()
                    # 新增/更正等级后让对应统计在下一轮立刻刷新。
                    self._item_stats_attempted_at.clear()
                    self._item_stats_verified_at.clear()
                else:
                    logger.warning("WFM 道具目录更新失败，继续使用本地旧版本")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("WFM 道具目录更新异常，继续使用本地旧版本")
            await asyncio.sleep(self._CATALOG_REFRESH_SECONDS)

    # ---- 虚空商人：命中库存后暂停到下一日桶 ----

    @staticmethod
    def _world_state_millis(value) -> int | None:
        try:
            if isinstance(value, Mapping):
                value = value.get("$date", value)
            if isinstance(value, Mapping):
                value = value.get("$numberLong", value)
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _world_state_id(value) -> str:
        if isinstance(value, Mapping):
            value = value.get("$oid") or value.get("id")
        return str(value or "")

    def _active_baro_stock(self, world_state: Mapping,
                           now: float | None = None) -> list[tuple[str, str, int]]:
        now_ms = int((time.time() if now is None else now) * 1000)
        traders = world_state.get("VoidTraders") or []
        if isinstance(traders, Mapping):
            traders = [traders]
        stock: list[tuple[str, str, int]] = []
        for trader in traders:
            activation = self._world_state_millis(trader.get("Activation"))
            expiry = self._world_state_millis(trader.get("Expiry"))
            if (activation is None or expiry is None
                    or not activation <= now_ms < expiry):
                continue
            event_id = self._world_state_id(trader.get("_id"))
            if not event_id:
                continue
            manifest = trader.get("Manifest") or []
            for entry in manifest:
                game_ref = str(entry.get("ItemType") or "")
                if game_ref:
                    stock.append((event_id, game_ref, activation // 1000))
        return stock

    def _next_baro_activation(self, world_state: Mapping,
                              now: float | None = None) -> int | None:
        now_ms = int((time.time() if now is None else now) * 1000)
        traders = world_state.get("VoidTraders") or []
        if isinstance(traders, Mapping):
            traders = [traders]
        future: list[int] = []
        for trader in traders:
            activation = self._world_state_millis(trader.get("Activation"))
            if activation is not None and activation > now_ms:
                future.append(activation // 1000)
        return min(future) if future else None

    def _baro_delay_from_state(self, world_state: Mapping, *, now: float) -> float:
        """按已知事件边界决定下次查询时间。

        距商人较远时每 6 小时校准一次；到达或离开边界会在 5 秒后复查。
        商人已到但事件 ID/商品清单尚未就绪时，才临时按分钟查询。
        """
        now_ms = int(now * 1000)
        traders = world_state.get("VoidTraders") or []
        if isinstance(traders, Mapping):
            traders = [traders]
        boundaries: list[float] = []
        for trader in traders:
            activation = self._world_state_millis(trader.get("Activation"))
            expiry = self._world_state_millis(trader.get("Expiry"))
            if activation is None or expiry is None:
                continue
            if activation <= now_ms < expiry:
                manifest = trader.get("Manifest") or []
                has_item = any(
                    isinstance(entry, Mapping) and entry.get("ItemType")
                    for entry in manifest)
                if not self._world_state_id(trader.get("_id")) or not has_item:
                    return float(self._BARO_INCOMPLETE_RETRY_SECONDS)
                boundaries.append(expiry / 1000)
            elif activation > now_ms:
                boundaries.append(activation / 1000)
        if not boundaries:
            return float(self._BARO_NO_SCHEDULE_SECONDS)
        boundary_delay = (
            min(boundaries) - now + self._BARO_BOUNDARY_GRACE_SECONDS)
        return max(1.0, min(
            float(self._BARO_CALIBRATION_SECONDS), boundary_delay))

    def _baro_activation_for_slug(self, slug: str) -> int | None:
        activations = [
            activation_ts
            for _event_id, game_ref, activation_ts in self._baro_stock
            if slug in marketdata.game_ref_to_slugs(game_ref)
        ]
        return max(activations) if activations else None

    async def _baro_once(self) -> int:
        state = await self.wfm.world_state()
        now = time.time()
        stock = tuple(self._active_baro_stock(state, now=now))
        self._baro_stock = stock
        self._next_baro_activation_ts = self._next_baro_activation(
            state, now=now)
        self._baro_next_delay_seconds = self._baro_delay_from_state(
            state, now=now)
        configured = self._configured_item_rows()
        paused = 0
        for event_id, game_ref, activation_ts in stock:
            for slug in marketdata.game_ref_to_slugs(game_ref):
                if slug not in configured:
                    continue
                baselines = self.store.list_bargain_item_daily_baselines(slug)
                baseline_by_bucket = {row["bucket"]: row for row in baselines}
                max_rank = marketdata.item_max_rank(slug)
                buckets = {
                    bargain.bucket_for_level(row.get("level"), max_rank)
                    for row in configured[slug]
                }
                buckets.update(baseline_by_bucket)
                for bucket in buckets:
                    row = baseline_by_bucket.get(bucket)
                    cached_ts = int(row["source_ts"]) if row else 0
                    # 已经拿到商人激活后的日桶时，本轮事件的等待条件已经满足。
                    # 主动清掉可能由更早同步留下的暂停，并且不能在商人仍驻留时
                    # 被后续校准重新添加，否则会被迫再等下一天。
                    if cached_ts > activation_ts:
                        self.store.resume_bargain_baro_pauses_for_baseline(
                            slug, bucket, cached_ts)
                        continue
                    # 以激活时间为门槛，确保激活前但本地尚未同步的旧日桶不能
                    # 提前解除暂停；没有旧基准的新道具也等待首个后续日桶。
                    if self.store.add_bargain_baro_pause(
                            event_id, slug, bucket,
                            previous_source_ts=activation_ts):
                        paused += 1
                        logger.info(
                            "虚空商人商品已暂停捡漏: {} {}", slug, bucket)
        self._baro_ready.set()
        await self._flush_pending_item_orders()
        return paused

    async def _baro_loop(self):
        failure_delay = float(self._BARO_FAILURE_INITIAL_SECONDS)
        while True:
            try:
                await self._baro_once()
            except asyncio.CancelledError:
                raise
            except httpx.HTTPError as exc:
                next_delay = failure_delay
                failure_delay = min(
                    float(self._BARO_FAILURE_MAX_SECONDS), failure_delay * 2)
                logger.warning(
                    "虚空商人状态刷新失败: {}；{:.0f}秒后重试",
                    exc, next_delay)
            except Exception:
                next_delay = failure_delay
                failure_delay = min(
                    float(self._BARO_FAILURE_MAX_SECONDS), failure_delay * 2)
                logger.exception(
                    "虚空商人状态处理异常；{:.0f}秒后重试", next_delay)
            else:
                next_delay = self._baro_next_delay_seconds
                failure_delay = float(self._BARO_FAILURE_INITIAL_SECONDS)
                logger.debug(
                    "虚空商人状态已同步，下次校准在 {:.0f} 秒后", next_delay)
            await asyncio.sleep(next_delay)
