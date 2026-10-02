"""消费游戏 IRC JSONL，并将有效紫卡消息送入统一投递队列。

主要输入是多槽独立采集器写入的消息与 presence 文件；单文件输入仅用于兼容研究工具。
本模块只增量读取文件，不操作游戏进程。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from nonebot import logger

from . import matcher, rivendata, texts
from .channel_locale import (
    presence_region_key,
    presence_region_label,
)
from .channel_push import ChannelCard, ChannelPushPayload, format_channel_push
from .chat_message import contains_riven_link
from .chat_collector.protocol import is_account_id
from .chat_collector.runtime import atomic_write_json, pid_matches, read_json
from .chat_tracking import (
    CHANNEL_RIVEN_BASE_DEDUPE_SECONDS,
    ObservedRiven,
    TrackingStore,
)
from .delivery import DeliverySource
from .feed_cursor import JsonlCheckpointReader
from .feed_health import irc_feed_paths, irc_feed_sources_health
from .platform_identity import (
    PLATFORM_UNKNOWN,
    resolve_player_identity,
)
from .privacy import redact_hidden_identifiers

# ---- 消息清洗 ----
_PUA_RE = re.compile(r"[\ue000-\uf8ff\u200b-\u200f\u202a-\u202e\ufeff]")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_PRIVMSG_RE = re.compile(
    r"^:([^!\r\n]+?)(?:!([^\s]*))?\s+PRIVMSG\s+(#[^\s]+)\s+:(.*)$",
    re.DOTALL,
)
_OUT_PRIVMSG_RE = re.compile(r"^PRIVMSG\s+(#[^\s]+)\s+:(.*)$", re.DOTALL)

_DAILY_FEED_FILE = re.compile(
    r"^privmsg_(\d{4}-\d{2}-\d{2})_([A-Q])\.jsonl$", re.I
)
_DAILY_PRESENCE_FILE = re.compile(
    r"^presence_(\d{4}-\d{2}-\d{2})_([A-Q])\.jsonl$", re.I
)
_DELIVERY_CURSOR_NAMESPACE = "channel-delivery-20260729-platform-identities"
_TRACKING_CURSOR_NAMESPACE = "channel-tracking-20260729-platform-identities"
_PRESENCE_CURSOR_NAMESPACE = "channel-presence-20260729-platform-identities"
_READ_BATCH_BYTES = 1024 * 1024
_TRACKING_TRANSACTION_MESSAGES = 100
_PRESENCE_TRANSACTION_EVENTS = 500
_HEALTH_INTERVAL_SECONDS = 5.0
_MAINTENANCE_INTERVAL_SECONDS = 3600.0
_MAINTENANCE_BACKLOG_INTERVAL_SECONDS = 60.0


def _target_dedupe_allows(item: ObservedRiven, hours: int) -> bool:
    """在全局一小时基础去重后应用目标自己的静默窗口。"""
    if not item.push_allowed:
        return False
    elapsed = item.dedupe_elapsed_seconds
    return elapsed is None or elapsed >= int(hours) * 3600


def _clean_text(s: str | None) -> str:
    if not s:
        return ""
    s = _PUA_RE.sub("", s)
    s = _CTRL_RE.sub("", s)
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


def _clean_nick(s: str | None) -> str:
    """清除昵称控制字符，同时保留合法的内部空格。"""
    if not s:
        return ""
    return _CTRL_RE.sub("", _PUA_RE.sub("", s)).strip()


def _parse_raw(raw: str) -> dict[str, str] | None:
    raw = (raw or "").replace("\r", "").strip()
    m = _PRIVMSG_RE.match(raw)
    if m:
        identity = m.group(2) or ""
        sender_match = re.match(r"([0-9a-fA-F]{24})_", identity)
        irc_nick = m.group(1)
        nick, platform = resolve_player_identity(irc_nick, raw=raw)
        return {
            "irc_nick": irc_nick,
            "nick": _clean_nick(nick),
            "platform": platform,
            "sender_id": sender_match.group(1).lower() if sender_match else "",
            "chan": m.group(3),
            "text": _clean_text(m.group(4)),
        }
    m = _OUT_PRIVMSG_RE.match(raw)
    if m:
        return {
            "irc_nick": "",
            "nick": "",
            "platform": PLATFORM_UNKNOWN,
            "chan": m.group(1),
            "text": _clean_text(m.group(2)),
        }
    return None


def normalize_record(obj: dict) -> dict | None:
    if not isinstance(obj, dict):
        return None
    direction = obj.get("dir") or "in"
    if direction != "in":
        return None
    raw = obj.get("raw") or ""
    irc_nick = str(obj.get("irc_nick") or obj.get("nick") or "")
    irc_nick = irc_nick.replace("\r", "").replace("\n", "").strip()
    nick, platform = resolve_player_identity(
        irc_nick,
        explicit_platform=obj.get("platform"),
        raw=raw,
    )
    nick = _clean_nick(nick)
    chan = (obj.get("chan") or "").strip()
    text = _clean_text(obj.get("text"))
    p = _parse_raw(raw) if raw else None
    if p:
        irc_nick = irc_nick or p["irc_nick"]
        nick = nick or p["nick"]
        if platform == PLATFORM_UNKNOWN:
            platform = p["platform"]
        chan = chan or p["chan"]
        text = text or p["text"]
    if not text or not chan.startswith("#"):
        return None
    if re.fullmatch(r"\d+", text):
        return None
    timestamp = _record_timestamp(obj.get("t", obj.get("ts")))
    sender_id = str(obj.get("sender_id") or "").strip().lower()
    if not sender_id and raw and p:
        sender_id = p.get("sender_id", "")
    if not is_account_id(sender_id):
        sender_id = ""
    slot = str(obj.get("slot") or "").strip().upper()
    key = str(obj.get("event_key") or "").strip()
    if not key:
        key = "|".join((
            str(timestamp), slot, sender_id, platform, chan,
            irc_nick or nick or "?", text,
        ))
    return {
        "ts": timestamp,
        "t": obj.get("t", obj.get("ts", timestamp)),
        "nick": nick or "?",
        "irc_nick": irc_nick or nick or "?",
        "platform": platform,
        "sender_id": sender_id,
        "chan": chan,
        "text": text,
        "slot": slot,
        "account": str(obj.get("account") or "").strip(),
        "key": key,
    }


def _record_timestamp(value) -> float:
    if isinstance(value, (int, float)):
        timestamp = float(value)
        return timestamp / 1000 if timestamp > 10_000_000_000 else timestamp
    text = str(value or "").strip()
    if text:
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            try:
                return float(text)
            except ValueError:
                pass
    return 0.0


def is_riven_hit(text: str) -> bool:
    """只接受实际携带完整 OMG 紫卡链接的消息。"""
    return contains_riven_link(text)


class IrcChatFeed:
    """异步 tail JSONL，按采集 run_id 投递可解码紫卡。"""

    def __init__(self, config, poller):
        self.config = config
        self.poller = poller
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._reader: JsonlCheckpointReader | None = None
        self._delivery_needs_initialization: bool | None = None
        self._tracker: TrackingStore | None = None
        self._history_tracker: TrackingStore | None = None
        self._presence_tracker: TrackingStore | None = None
        self._tracking_reader: JsonlCheckpointReader | None = None
        self._tracking_needs_initialization: bool | None = None
        self._tracking_initialized = False
        self._presence_reader: JsonlCheckpointReader | None = None
        self._presence_needs_initialization: bool | None = None
        self._next_maintenance_at = 0.0
        self._next_health_at = 0.0
        self._health_status: str | None = None
        control_path = self._control_path()
        control = read_json(control_path) if control_path is not None else None
        known_generation = str((control or {}).get("run_id") or "")
        # 目录采集模式若已有监督器代次，先继承代次但保持拒收，直到
        # _sync_control 验证监督器进程仍存活；旧单文件模式继续使用 legacy。
        self._active_generation: str | None = known_generation or "legacy"
        self._accepting = not known_generation
        self._last_cancelled: dict[str, int] = {}
        if self._accepting:
            self.poller.activate_source(DeliverySource.IRC, "legacy")

    def start(self):
        paths = self._source_paths()
        if not paths:
            logger.warning("IRC feed 已启用但未配置 irc_feed_dir/irc_feed_path")
            return
        try:
            self._ensure_reader(paths[0])
        except (OSError, ValueError):
            logger.exception("IRC feed 检查点不可用，拒绝启动以避免跳过未读记录")
            return
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "IRC 游戏聊天喂送已启动 sources={} filter=decodable_riven",
            [str(path) for path in paths],
        )

    async def stop(self):
        self._stop.set()
        if self._task:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._cancel_active_generation("bot_shutdown")
        trackers = {
            id(tracker): tracker
            for tracker in (
                self._tracker, self._history_tracker, self._presence_tracker,
            )
            if tracker is not None
        }
        for tracker in trackers.values():
            tracker.close()
        self._tracker = None
        self._history_tracker = None
        self._presence_tracker = None

    def _runtime_root(self) -> Path | None:
        feed_dir = str(getattr(self.config, "irc_feed_dir", "") or "")
        return Path(feed_dir).parent if feed_dir else None

    def _control_path(self) -> Path | None:
        root = self._runtime_root()
        return root / "collector_control.json" if root is not None else None

    def _delivery_state_path(self) -> Path | None:
        root = self._runtime_root()
        return root / "irc_delivery_state.json" if root is not None else None

    def _write_delivery_state(
        self, *, reason: str, cancelled: dict[str, int] | None = None,
    ) -> None:
        path = self._delivery_state_path()
        if path is None:
            return
        if cancelled is not None:
            self._last_cancelled = dict(cancelled)
        status = self.poller.delivery_status
        try:
            atomic_write_json(path, {
                "run_id": self._active_generation,
                "accepting": self._accepting,
                "reason": reason,
                "cancelled": dict(self._last_cancelled),
                "irc_queued": status["queued_by_source"].get("irc", 0),
                "irc_inflight": status["inflight_by_source"].get("irc", 0),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
        except OSError as error:
            logger.warning("IRC 撤销确认状态写入失败: {}", error)

    def _cancel_active_generation(self, reason: str) -> dict[str, int]:
        generation = self._active_generation
        self._accepting = False
        if generation is None:
            return {"queued": 0, "retrying": 0, "inflight": 0}
        cancelled = self.poller.cancel_source(
            DeliverySource.IRC, generation)
        self._write_delivery_state(reason=reason, cancelled=cancelled)
        return cancelled

    def _sync_control(self) -> bool:
        """同步外部监督器状态；旧单文件模式不要求控制文件。"""
        path = self._control_path()
        if path is None:
            if not self._accepting:
                self._active_generation = "legacy"
                self.poller.activate_source(DeliverySource.IRC, "legacy")
                self._accepting = True
            return True

        control = read_json(path) or {}
        run_id = str(control.get("run_id") or "")
        control_alive = pid_matches(
            int(control.get("pid") or 0),
            str(control.get("process_identity") or ""),
        )
        if control.get("status") == "running" and run_id and control_alive:
            if self._active_generation != run_id or not self._accepting:
                if self._accepting:
                    self._cancel_active_generation("generation_changed")
                self._active_generation = run_id
                self.poller.activate_source(DeliverySource.IRC, run_id)
                self._accepting = True
                self._last_cancelled = {}
                self._write_delivery_state(reason="running")
            return True

        reason = str(control.get("status") or "collector_not_running")
        if self._accepting:
            self._cancel_active_generation(reason)
        elif self._active_generation is not None:
            # 取消时可能仍有已经开始渲染/调用 API 的项目；它们完成或因代次
            # 失效被丢弃后，持续刷新确认文件，避免监督器永久停在旧的在途数。
            self._write_delivery_state(reason=reason)
        return False

    def _enabled_platforms(self) -> tuple[str, ...]:
        return (("discord",)
                if getattr(self.config, "discord_dm_enabled", False) else ())

    def _targets(self) -> list[int]:
        return self.poller.store.channel_delivery_scope_ids(
            platforms=self._enabled_platforms(),
        )

    def _source_paths(self) -> tuple[Path, ...]:
        return irc_feed_paths(
            feed_dir=getattr(self.config, "irc_feed_dir", "") or "",
            feed_path=getattr(self.config, "irc_feed_path", "") or "",
        )

    def _checkpoint_path(self, fallback: Path | None = None) -> Path:
        configured = getattr(self.config, "irc_feed_checkpoint_path", "") or ""
        if configured:
            return Path(configured)
        feed_dir = getattr(self.config, "irc_feed_dir", "") or ""
        if feed_dir:
            return Path(feed_dir).parent / "feed_cursor.json"
        source = fallback or Path(getattr(self.config, "irc_feed_path", "") or ".")
        return source.with_name(source.name + ".cursor.json")

    def _ensure_reader(self, fallback: Path | None = None) -> JsonlCheckpointReader:
        if self._reader is None:
            self._reader = JsonlCheckpointReader(
                self._checkpoint_path(fallback),
                namespace=_DELIVERY_CURSOR_NAMESPACE,
            )
            self._delivery_needs_initialization = not self._reader.is_initialized
        return self._reader

    def _tracking_checkpoint_path(self, fallback: Path | None = None) -> Path:
        database = getattr(self.config, "irc_track_db_path", "") or ""
        if database:
            path = Path(database)
            return path.with_name(path.name + ".cursor.json")
        feed_dir = getattr(self.config, "irc_feed_dir", "") or ""
        if feed_dir:
            return Path(feed_dir).parent / "track_history_cursor.json"
        source = fallback or Path(getattr(self.config, "irc_feed_path", "") or ".")
        return source.parent / "track_history_cursor.json"

    def _ensure_tracking_reader(
        self, fallback: Path | None = None
    ) -> JsonlCheckpointReader:
        if self._tracking_reader is None:
            self._tracking_reader = JsonlCheckpointReader(
                self._tracking_checkpoint_path(fallback),
                namespace=_TRACKING_CURSOR_NAMESPACE,
            )
            self._tracking_needs_initialization = (
                not self._tracking_reader.is_initialized
            )
        return self._tracking_reader

    def _ensure_tracker(self, fallback: Path | None = None) -> None:
        if (self._tracker is not None
                and self._history_tracker is not None
                and self._presence_tracker is not None):
            return
        configured = getattr(self.config, "irc_track_db_path", "") or ""
        if configured:
            path = Path(configured)
        else:
            feed_dir = getattr(self.config, "irc_feed_dir", "") or ""
            source = fallback or Path(
                getattr(self.config, "irc_feed_path", "") or ".")
            path = Path(feed_dir).parent / "track.db" if feed_dir else source.parent / "track.db"
        # 实时投递、历史追踪与 presence 各用独立 WAL 连接。SQLite 仍负责
        # 写事务串行化，但后台线程不再共享同一个 connection 对象，也不会
        # 因同连接跨线程调用而阻塞或破坏实时投递事务。
        self._tracker = self._tracker or TrackingStore(path)
        self._history_tracker = self._history_tracker or TrackingStore(path)
        self._presence_tracker = self._presence_tracker or TrackingStore(path)

    def _prepare_tracking(self, fallback: Path | None = None) -> bool:
        reader = self._ensure_tracking_reader(fallback)
        if self._tracking_initialized:
            return self._tracker is not None
        # 首次启用只从当前文件末尾开始。必须先持久化这个边界，再尝试打开
        # 数据库；否则数据库故障期间新增的记录会在重试初始化时被一起跳过。
        if self._tracking_needs_initialization:
            if not self._initialize_existing_sources(reader, label="追踪"):
                return False
            self._tracking_needs_initialization = False
        self._ensure_tracker(fallback)
        self._tracking_initialized = True
        return self._tracker is not None

    def _presence_checkpoint_path(self) -> Path:
        database = getattr(self.config, "irc_track_db_path", "") or ""
        if database:
            path = Path(database)
            return path.with_name(path.name + ".presence_cursor.json")
        root = self._runtime_root()
        return ((root / "presence_cursor.json") if root is not None
                else self._checkpoint_path().with_name("presence_cursor.json"))

    def _ensure_presence_reader(self) -> JsonlCheckpointReader:
        if self._presence_reader is None:
            self._presence_reader = JsonlCheckpointReader(
                self._presence_checkpoint_path(),
                namespace=_PRESENCE_CURSOR_NAMESPACE,
            )
            self._presence_needs_initialization = (
                not self._presence_reader.is_initialized
            )
        return self._presence_reader

    def _presence_paths(self) -> tuple[Path, ...]:
        feed_dir = str(getattr(self.config, "irc_feed_dir", "") or "")
        if not feed_dir:
            return ()
        try:
            return tuple(
                path for path in sorted(
                    Path(feed_dir).glob("presence_????-??-??_?.jsonl"))
                if _DAILY_PRESENCE_FILE.fullmatch(path.name)
            )
        except OSError:
            return ()

    def _initialize_existing_sources(
        self, reader: JsonlCheckpointReader, *, label: str = "投递",
    ) -> bool:
        """首次启用时跳过全部现存历史；之后出现的新日文件从头恢复。"""
        feed_dir = getattr(self.config, "irc_feed_dir", "") or ""
        if feed_dir:
            root = Path(feed_dir)
            try:
                existing = sorted(root.glob("privmsg_????-??-??_?.jsonl"))
            except OSError:
                existing = []
        else:
            existing = list(self._source_paths())
        initialized = True
        for source in existing:
            if not _DAILY_FEED_FILE.fullmatch(source.name) and feed_dir:
                continue
            try:
                reader.initialize_at_end(source)
            except OSError as error:
                initialized = False
                logger.warning(
                    "IRC feed {}首次游标初始化失败 {}: {}", label, source, error)
                continue
        if initialized:
            try:
                reader.mark_initialized()
            except OSError as error:
                logger.warning("IRC feed {}首次游标标记失败: {}", label, error)
                initialized = False
        return initialized

    def _initialize_existing_presence_sources(
        self, reader: JsonlCheckpointReader,
    ) -> bool:
        """首次启用上线事件读取时跳过已有记录，避免历史 JOIN 误报。"""
        initialized = True
        for source in self._presence_paths():
            try:
                reader.initialize_at_end(source)
            except OSError as error:
                initialized = False
                logger.warning(
                    "IRC feed 上线首次游标初始化失败 {}: {}", source, error)
        if initialized:
            try:
                reader.mark_initialized()
            except OSError as error:
                logger.warning("IRC feed 上线首次游标标记失败: {}", error)
                initialized = False
        return initialized

    def _prepare_presence_reader(self) -> JsonlCheckpointReader | None:
        """固定首次启用边界；数据库故障期间的新事件随后仍可恢复。"""
        reader = self._ensure_presence_reader()
        if self._presence_needs_initialization:
            if not self._initialize_existing_presence_sources(reader):
                return None
            self._presence_needs_initialization = False
        return reader

    def _poll_source_paths(
        self, reader: JsonlCheckpointReader, *, include_all: bool = False,
    ) -> tuple[Path, ...]:
        current = list(self._source_paths())
        feed_dir = getattr(self.config, "irc_feed_dir", "") or ""
        if not feed_dir:
            return tuple(current)

        root = Path(feed_dir)
        root_key = reader.path_key(root)
        tracked_days = [
            match.group(1)
            for path in reader.tracked_paths()
            if reader.path_key(path.parent) == root_key
            if (match := _DAILY_FEED_FILE.fullmatch(path.name))
        ]
        latest_tracked_day = max(tracked_days, default="")
        selected: dict[str, Path] = {}
        try:
            archived = sorted(root.glob("privmsg_????-??-??_?.jsonl"))
        except OSError:
            archived = []
        for path in archived:
            match = _DAILY_FEED_FILE.fullmatch(path.name)
            if not match:
                continue
            day = match.group(1)
            should_poll = (
                include_all
                or (latest_tracked_day and day >= latest_tracked_day)
                or (reader.has_source(path) and not reader.is_caught_up(path))
            )
            if should_poll:
                selected.setdefault(reader.path_key(path), path)
        for path in current:
            selected.setdefault(reader.path_key(path), path)
        return tuple(selected.values())

    def _poll_presence_paths(
        self, reader: JsonlCheckpointReader,
    ) -> tuple[Path, ...]:
        """读取已跟踪文件的增量及跟踪日之后出现的新 presence 日文件。"""
        paths = self._presence_paths()
        if not paths:
            return ()
        tracked_days = [
            match.group(1)
            for path in reader.tracked_paths()
            if (match := _DAILY_PRESENCE_FILE.fullmatch(path.name))
        ]
        latest_tracked_day = max(tracked_days, default="")
        return tuple(
            path for path in paths
            if (not latest_tracked_day
                or (latest_tracked_day and
                    _DAILY_PRESENCE_FILE.fullmatch(path.name).group(1)
                    >= latest_tracked_day)
                or (reader.has_source(path) and not reader.is_caught_up(path)))
        )

    def _report_health(self, health: dict, paths) -> None:
        status = health["status"]
        if status == self._health_status:
            return
        previous = self._health_status
        self._health_status = status
        display = [str(path) for path in paths]
        if status == "missing":
            logger.warning("IRC 频道采集输入不可用: {}", display)
        elif status == "degraded":
            logger.warning(
                "IRC 频道采集输入部分可用 {}/{}: {}",
                health.get("live_source_count", 0),
                health.get("expected_source_count", len(display)),
                display,
            )
        elif status == "stale":
            logger.warning(
                "IRC 频道采集输入已停更 {:.0f}s: {}",
                health["age_seconds"], display)
        elif status == "starting":
            logger.info("IRC 频道采集正在认证或加入频道: {}", display)
        elif status == "stopped":
            logger.warning("IRC 频道采集已停止: {}", display)
        elif previous in {
            "missing", "stale", "degraded", "starting", "stopped",
        }:
            logger.info("IRC 频道采集输入已恢复: {}", display)

    @staticmethod
    def _prune_tracking_database(
        path: Path, before_ts: int,
    ) -> dict[str, int | bool]:
        with TrackingStore(path) as store:
            return store.prune_presence_history(before_ts)

    def _prune_consumed_feed_files(self, cutoff_day: date) -> dict[str, int]:
        feed_dir = str(getattr(self.config, "irc_feed_dir", "") or "")
        if not feed_dir:
            return {"files": 0, "bytes": 0}
        root = Path(feed_dir).resolve()
        readers_by_pattern = (
            (_DAILY_FEED_FILE, tuple(
                reader for reader in (self._reader, self._tracking_reader)
                if reader is not None
            )),
            (_DAILY_PRESENCE_FILE, tuple(
                reader for reader in (self._presence_reader,)
                if reader is not None
            )),
        )
        removed_files = 0
        removed_bytes = 0
        for pattern, readers in readers_by_pattern:
            if not readers or any(not reader.is_initialized for reader in readers):
                continue
            try:
                candidates = sorted(root.glob("*.jsonl"))
            except OSError:
                continue
            for path in candidates:
                match = pattern.fullmatch(path.name)
                if match is None or date.fromisoformat(match.group(1)) >= cutoff_day:
                    continue
                if not all(reader.is_caught_up(
                    path, allow_trailing_partial=True,
                ) for reader in readers):
                    continue
                try:
                    size = path.stat().st_size
                    path.unlink()
                except OSError as error:
                    logger.warning("IRC 已消费归档删除失败 {}: {}", path, error)
                    continue
                removed_files += 1
                removed_bytes += size
                for reader in readers:
                    try:
                        reader.forget(path)
                    except OSError as error:
                        logger.warning("IRC 已删除归档的游标回收失败 {}: {}", path, error)
        return {"files": removed_files, "bytes": removed_bytes}

    def _presence_history_prune_safe(self, cutoff_day: date) -> bool:
        reader = self._presence_reader
        if reader is None or not reader.is_initialized:
            return False
        for path in self._presence_paths():
            match = _DAILY_PRESENCE_FILE.fullmatch(path.name)
            if (match is not None
                    and date.fromisoformat(match.group(1)) <= cutoff_day
                    and not reader.is_caught_up(
                        path, allow_trailing_partial=True)):
                return False
        return True

    async def _maybe_maintain(self) -> None:
        now = time.monotonic()
        if now < self._next_maintenance_at:
            return
        self._next_maintenance_at = now + _MAINTENANCE_BACKLOG_INTERVAL_SECONDS
        backlog = False
        try:
            presence_days = int(getattr(
                self.config, "irc_presence_retention_days", 7))
            if self._tracker is not None:
                cutoff_time = datetime.now(timezone.utc) - timedelta(
                    days=presence_days)
                if self._presence_history_prune_safe(cutoff_time.date()):
                    result = await asyncio.to_thread(
                        self._prune_tracking_database,
                        self._tracker.path,
                        int(cutoff_time.timestamp()),
                    )
                    backlog = bool(result["remaining"])
                    deleted = sum(int(result[key]) for key in (
                        "events", "snapshots", "sessions", "memberships",
                        "alerts",
                    ))
                    if deleted:
                        logger.info(
                            "IRC presence 历史清理完成 events={} snapshots={} "
                            "sessions={} memberships={} alerts={} backlog={}",
                            result["events"], result["snapshots"],
                            result["sessions"],
                            result["memberships"], result["alerts"], backlog,
                        )
                else:
                    backlog = True

            feed_days = int(getattr(
                self.config, "irc_feed_retention_days", 7))
            if feed_days > 0:
                files = await asyncio.to_thread(
                    self._prune_consumed_feed_files,
                    datetime.now(timezone.utc).date()
                    - timedelta(days=feed_days),
                )
                if files["files"]:
                    logger.info(
                        "IRC 已消费 JSONL 清理完成 files={} bytes={}",
                        files["files"], files["bytes"],
                    )
        except Exception:
            logger.exception("IRC 历史维护失败；稍后重试")
            backlog = True
        self._next_maintenance_at = time.monotonic() + (
            _MAINTENANCE_BACKLOG_INTERVAL_SECONDS
            if backlog else _MAINTENANCE_INTERVAL_SECONDS
        )

    async def _wait_for_next_tick(self, interval: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

    async def _delivery_loop(
        self, interval: float, dry: bool, reader: JsonlCheckpointReader,
    ) -> None:
        """只处理控制状态和时效投递，不等待历史或 presence 积压。"""
        while not self._stop.is_set():
            try:
                if self._sync_control():
                    now = time.monotonic()
                    if now >= self._next_health_at:
                        current_paths = self._source_paths()
                        health = irc_feed_sources_health(
                            current_paths,
                            stale_seconds=float(getattr(
                                self.config, "irc_feed_stale_seconds", 180.0)),
                        )
                        self._report_health(health, current_paths)
                        self._next_health_at = now + _HEALTH_INTERVAL_SECONDS
                    if self._delivery_needs_initialization:
                        if self._initialize_existing_sources(reader):
                            self._delivery_needs_initialization = False
                    if not self._delivery_needs_initialization:
                        for path in self._poll_source_paths(reader):
                            await self._tick(path, dry=dry)
            except Exception:
                logger.exception("IRC 实时投递 tick 异常")
            await self._wait_for_next_tick(interval)

    async def _tracking_loop(self, interval: float) -> None:
        """在独立循环恢复完整紫卡历史，不阻塞新的时效投递。"""
        while not self._stop.is_set():
            try:
                if self._accepting and self._prepare_tracking():
                    reader = self._ensure_tracking_reader()
                    for path in self._poll_source_paths(reader):
                        await self._track_tick(path)
            except Exception:
                logger.exception("IRC 历史追踪 tick 异常；稍后重试")
            await self._wait_for_next_tick(interval)

    async def _presence_loop(self, interval: float, dry: bool) -> None:
        """在独立循环处理上线状态，避免 presence 积压拖慢紫卡投递。"""
        while not self._stop.is_set():
            try:
                reader = (
                    self._prepare_presence_reader()
                    if self._accepting else None
                )
                if reader is not None and self._prepare_tracking():
                    await self._presence_ticks(
                        self._poll_presence_paths(reader), reader, dry=dry,
                    )
            except Exception:
                logger.exception("IRC 上线事件 tick 异常；稍后重试")
            await self._wait_for_next_tick(interval)

    async def _maintenance_loop(self) -> None:
        while not self._stop.is_set():
            await self._maybe_maintain()
            await self._wait_for_next_tick(1.0)

    async def _loop(self):
        interval = float(getattr(self.config, "irc_feed_interval", 0.2) or 0.2)
        dry = bool(getattr(self.config, "sniper_dry_run", False))
        reader = self._ensure_reader()

        try:
            self._prepare_presence_reader()
            self._prepare_tracking()
        except Exception:
            logger.exception("IRC 追踪管线初始化失败；在数据库恢复前暂停频道投递")

        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self._delivery_loop(interval, dry, reader))
            tasks.create_task(self._tracking_loop(interval))
            tasks.create_task(self._presence_loop(interval, dry))
            tasks.create_task(self._maintenance_loop())
            await self._stop.wait()

    async def _tick(
        self,
        path: Path,
        dry: bool = False,
    ):
        if not path.is_file():
            return
        try:
            reader = self._ensure_reader(path)
            batch = reader.read_complete(path, max_bytes=_READ_BATCH_BYTES)
        except OSError:
            return
        if not batch.lines:
            return
        if self._tracker is None:
            try:
                self._ensure_tracker(path)
            except Exception:
                logger.exception("IRC 追踪库不可用，投递游标保持不动")
                return
        for raw_line in batch.lines:
            line = raw_line.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            record_generation = str(
                obj.get("collector_run_id") or "legacy")
            if record_generation != self._active_generation:
                continue
            msg = normalize_record(obj)
            if not msg:
                continue
            if not is_riven_hit(msg["text"]):
                continue
            try:
                observed, seller_riven_count = await asyncio.to_thread(
                    self._tracker.ingest_with_player_riven_count,
                    msg,
                    dedupe_seconds=CHANNEL_RIVEN_BASE_DEDUPE_SECONDS,
                )
            except Exception:
                logger.exception("IRC 追踪/去重写入失败；投递游标保持不动")
                return
            allowed = [item for item in observed if item.push_allowed]
            if not allowed:
                continue
            renderable_context = [item for item in observed if (
                item.card.get("weapon_slug") in rivendata.weapons()
                and all(rivendata.attribute_slug_from_ref(
                    str(stat.get("ref") or "")) is not None
                        for stat in item.card.get("stats") or [])
            )]
            if not any(item.push_allowed for item in renderable_context):
                logger.warning(
                    "跳过无法按数据库名称渲染的频道紫卡 channel={} seller={}",
                    msg["chan"],
                    redact_hidden_identifiers(msg["nick"]),
                )
                continue
            targets = self._targets()
            if not targets:
                continue
            blacklisted_scopes = self.poller.store.blacklisted_scope_ids(
                msg["nick"], scope="channel")
            for target in targets:
                if target in blacklisted_scopes:
                    continue
                configs = self.poller.store.list_configs(target)

                def matches_target(item: ObservedRiven) -> bool:
                    return any(
                        matcher.match_channel_card(config, item.card)
                        for config in configs
                    )

                globally_matching_items = tuple(
                    item for item in renderable_context
                    if item.push_allowed and matches_target(item)
                )
                if not globally_matching_items:
                    continue
                preference = self.poller.store.get_target_preferences(target)
                dedupe_hours = int(preference["channel_dedupe_hours"])
                matching_items = tuple(
                    item for item in globally_matching_items
                    if _target_dedupe_allows(item, dedupe_hours)
                )
                if not matching_items:
                    continue
                matching_cards = tuple(
                    ChannelCard(
                        item.card, item.riven_no, card_index=item.card_index,
                    )
                    for item in matching_items
                )
                message_cards = tuple(
                    ChannelCard(
                        item.card,
                        item.riven_no,
                        duplicate=not _target_dedupe_allows(
                            item, dedupe_hours),
                        card_index=item.card_index,
                    )
                    for item in renderable_context
                )
                display_cards = tuple(
                    card for card, item in zip(
                        message_cards, renderable_context, strict=True)
                    if matches_target(item)
                )
                payload = ChannelPushPayload(
                    seller=redact_hidden_identifiers(msg["nick"]),
                    channel=msg["chan"],
                    cards=matching_cards, locale=preference["locale"],
                    target_scope=target,
                    seller_platform=msg["platform"],
                    seller_riven_count=seller_riven_count,
                    raw_text=msg["text"], message_cards=message_cards,
                    display_cards=display_cards,
                )
                if dry:
                    logger.info(
                        "[IRC dry_run] {}",
                        format_channel_push(payload).replace("\n", " | "))
                    continue
                self.poller.enqueue_delivery(self.poller.new_delivery(
                    DeliverySource.IRC,
                    target,
                    payload,
                    generation=self._active_generation,
                    observed_at=msg["ts"] or None,
                ))
        # 本地追踪与去重写入成功后才推进文件游标。
        reader.commit(batch)

    async def _track_tick(self, path: Path) -> None:
        tracker = self._history_tracker or self._tracker
        if tracker is None or not path.is_file():
            return
        try:
            reader = self._ensure_tracking_reader(path)
            batch = reader.read_complete(path, max_bytes=_READ_BATCH_BYTES)
        except OSError:
            return
        if not batch.lines:
            return
        messages = []
        for raw_line in batch.lines:
            line = raw_line.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            message = normalize_record(obj)
            if message is None:
                continue
            messages.append(message)
        for offset in range(0, len(messages), _TRACKING_TRANSACTION_MESSAGES):
            try:
                await asyncio.to_thread(
                    tracker.ingest_historical_batch,
                    messages[offset:offset + _TRACKING_TRANSACTION_MESSAGES],
                    dedupe_seconds=CHANNEL_RIVEN_BASE_DEDUPE_SECONDS,
                )
            except Exception:
                logger.exception("IRC 追踪库写入失败；追踪游标保持不动")
                return
            # 让实时投递线程有机会在两个短写事务之间取得 WAL 写锁。
            await asyncio.sleep(0)
        reader.commit(batch)

    async def _presence_ticks(
        self, paths, reader: JsonlCheckpointReader, *, dry: bool,
    ) -> None:
        """合并所有槽的未读事件并按原始时间排序后更新在线会话。"""
        tracker = self._presence_tracker or self._tracker
        if tracker is None:
            return
        batches = []
        pending: list[tuple[float, str, int, dict]] = []
        events_by_path: dict[str, list[tuple[float, int]]] = {}
        batch_path_keys: dict[Path, str] = {}
        for path in paths:
            if not path.is_file():
                continue
            try:
                batch = reader.read_complete(
                    path, max_bytes=_READ_BATCH_BYTES)
            except OSError:
                continue
            if not batch.lines:
                continue
            batches.append(batch)
            path_key = reader.path_key(path)
            batch_path_keys[batch.path] = path_key
            for line_index, raw_line in enumerate(batch.lines):
                line = raw_line.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                timestamp = _record_timestamp(event.get("t", event.get("ts")))
                pending.append((timestamp, path_key, line_index, event))
                events_by_path.setdefault(path_key, []).append(
                    (timestamp, line_index))
        if not batches:
            return
        frontiers: list[float] = []
        process_batches = []
        for batch in batches:
            path_key = batch_path_keys[batch.path]
            path_events = events_by_path.get(path_key, [])
            if not path_events:
                reader.commit(batch)
                continue
            process_batches.append(batch)
            try:
                has_unread_bytes = batch.offset < batch.path.stat().st_size
            except OSError:
                has_unread_bytes = True
            if has_unread_bytes:
                frontiers.append(path_events[-1][0])
        if not process_batches:
            return
        frontier = min(frontiers) if frontiers else None
        ordered = sorted(
            (item for item in pending
             if frontier is None or item[0] <= frontier),
            key=lambda item: item[:3],
        )
        chunks: list[list[tuple[float, str, int, dict]]] = []
        regular_chunk: list[tuple[float, str, int, dict]] = []
        for item in ordered:
            if str(item[3].get("type") or "").lower() == "channel_snapshot":
                if regular_chunk:
                    chunks.append(regular_chunk)
                    regular_chunk = []
                chunks.append([item])
                continue
            regular_chunk.append(item)
            if len(regular_chunk) == _PRESENCE_TRANSACTION_EVENTS:
                chunks.append(regular_chunk)
                regular_chunk = []
        if regular_chunk:
            chunks.append(regular_chunk)

        for chunk in chunks:
            try:
                results = await asyncio.to_thread(
                    tracker.observe_presence_batch,
                    tuple(item[3] for item in chunk),
                )
            except Exception:
                logger.exception("IRC 在线会话写入失败；presence 游标保持不动")
                return
            for item, result in zip(chunk, results, strict=True):
                event = item[3]
                try:
                    await self._handle_presence_result(event, result, dry=dry)
                except Exception:
                    # 状态批次已经提交；单条提醒失败不能阻断同批其余事件，
                    # 否则重试时整批都会被 presence_events 去重跳过。
                    logger.exception(
                        "IRC 单条在线提醒处理失败；继续处理同批后续事件")
        selected_by_path: dict[str, set[int]] = {}
        for _timestamp, path_key, line_index, _event in ordered:
            selected_by_path.setdefault(path_key, set()).add(line_index)
        for batch in process_batches:
            path_key = batch_path_keys[batch.path]
            selected = selected_by_path.get(path_key, set())
            valid_indices = {
                line_index for _timestamp, line_index
                in events_by_path[path_key]
            }
            if selected == valid_indices:
                reader.commit(batch)
            elif selected:
                first_unselected = min(valid_indices - selected)
                reader.commit(reader.prefix(batch, first_unselected))

    async def _handle_presence_result(
        self, event: dict, result: dict, *, dry: bool,
    ) -> None:
        tracker = self._presence_tracker or self._tracker
        if tracker is None:
            return
        display_nick = str(result.get("nick") or "").strip()
        if result.get("left_all_channels"):
            if not display_nick:
                logger.warning(
                    "跳过缺少玩家昵称的离开提醒 session_id={}",
                    result["ended_session_id"],
                )
            else:
                scopes = self.poller.store.tracker_scope_ids(
                    result["account_id"],
                    platforms=self._enabled_platforms(),
                )
                scopes = await asyncio.to_thread(
                    tracker.claimed_presence_scope_ids,
                    scopes,
                    int(result["ended_session_id"]),
                )
                for scope_id in scopes:
                    locale = self.poller.store.get_target_preferences(
                        scope_id,
                    )["locale"]
                    message = texts.render(
                        "上线提醒.离开", locale=locale,
                        name=display_nick,
                    )
                    message = redact_hidden_identifiers(message)
                    if dry:
                        logger.info("[IRC presence dry_run] {}", message)
                    else:
                        self.poller.enqueue_delivery(
                            self.poller.new_delivery(
                                DeliverySource.IRC, scope_id, message,
                                generation=self._active_generation,
                                observed_at=(
                                    _record_timestamp(event.get("t")) or None),
                            )
                        )
        if not result.get("session_id"):
            return
        region_key = presence_region_key(event.get("chan"))
        if region_key is None:
            return
        if not display_nick:
            logger.warning(
                "跳过缺少玩家昵称的上线提醒 session_id={}",
                result["session_id"],
            )
            return
        scopes = self.poller.store.tracker_scope_ids(
            result["account_id"],
            platforms=self._enabled_platforms(),
            nick=display_nick,
        )
        claimed = await asyncio.to_thread(
            tracker.claim_presence_alert,
            scopes,
            int(result["session_id"]),
            region_key,
            alerted_at=_record_timestamp(event.get("t")) or time.time(),
        )
        for scope_id in claimed:
            locale = self.poller.store.get_target_preferences(scope_id)["locale"]
            message = texts.render(
                "上线提醒.上线", locale=locale,
                name=display_nick,
                channel=presence_region_label(event.get("chan"), locale),
            )
            message = redact_hidden_identifiers(message)
            if dry:
                logger.info("[IRC presence dry_run] {}", message)
            else:
                self.poller.enqueue_delivery(self.poller.new_delivery(
                    DeliverySource.IRC, scope_id, message,
                    generation=self._active_generation,
                    observed_at=_record_timestamp(event.get("t")) or None,
                ))
