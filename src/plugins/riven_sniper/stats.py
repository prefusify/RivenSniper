"""运行状态埋点：轮询统计 + 环形日志缓冲（WebUI 数据源）。"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

_DELIVERY_WINDOW_SECONDS = 1800


@dataclass
class PollerStats:
    started_at: float = field(default_factory=time.time)
    last_poll_at: float | None = None
    last_poll_candidates: int = 0
    last_poll_new: int = 0
    last_poll_hits: int = 0
    consecutive_failures: int = 0
    polls_total: int = 0
    hits_today: int = 0
    pushes_today: int = 0
    push_failures_today: int = 0
    push_items_today: int = 0
    push_item_failures_today: int = 0
    _day: str = field(default_factory=lambda: date.today().isoformat())
    _delivery_timings: deque[
        tuple[
            float, str, str, str, float, float, float | None, float | None,
        ]
    ] = field(default_factory=deque, repr=False)

    def _roll_day(self):
        today = date.today().isoformat()
        if today != self._day:
            self._day = today
            self.hits_today = self.pushes_today = self.push_failures_today = 0
            self.push_items_today = self.push_item_failures_today = 0

    def record_poll(self, candidates: int, new: int, hits: int):
        self._roll_day()
        self.last_poll_at = time.time()
        self.last_poll_candidates = candidates
        self.last_poll_new = new
        self.last_poll_hits = hits
        self.polls_total += 1
        self.consecutive_failures = 0
        self.hits_today += hits

    def record_poll_failure(self):
        self.consecutive_failures += 1

    def record_push(self, ok: bool, *, items: int = 1):
        self._roll_day()
        if ok:
            self.pushes_today += 1
            self.push_items_today += items
        else:
            self.push_failures_today += 1
            self.push_item_failures_today += items

    def record_delivery_timing(
        self,
        source: str,
        *,
        platform: str,
        outcome: str,
        queue_seconds: float,
        api_seconds: float,
        source_seconds: float | None = None,
        source_end_seconds: float | None = None,
    ) -> None:
        """记录最近投递尝试的阶段耗时；只保留来源、结果与耗时。"""
        now = time.time()
        self._delivery_timings.append((
            now, source, platform, outcome, max(0.0, queue_seconds),
            max(0.0, api_seconds),
            None if source_seconds is None else max(0.0, source_seconds),
            (None if source_end_seconds is None
             else max(0.0, source_end_seconds)),
        ))
        self._prune_delivery_timings(now)

    def _prune_delivery_timings(self, now: float) -> None:
        cutoff = now - _DELIVERY_WINDOW_SECONDS
        while (self._delivery_timings
               and self._delivery_timings[0][0] < cutoff):
            self._delivery_timings.popleft()

    @staticmethod
    def _timing_summary(values: list[float]) -> dict[str, float | int]:
        if not values:
            return {"samples": 0}
        ordered = sorted(value * 1000 for value in values)

        def percentile(ratio: float) -> float:
            position = (len(ordered) - 1) * ratio
            lower = int(position)
            upper = min(lower + 1, len(ordered) - 1)
            fraction = position - lower
            value = ordered[lower] + (
                ordered[upper] - ordered[lower]) * fraction
            return round(value, 1)

        return {
            "samples": len(ordered),
            "p50_ms": percentile(0.5),
            "p95_ms": percentile(0.95),
            "max_ms": round(ordered[-1], 1),
        }

    def _delivery_timing_snapshot(self) -> dict[str, object]:
        now = time.time()
        self._prune_delivery_timings(now)
        recent = list(self._delivery_timings)

        def summarize(samples) -> dict[str, object]:
            return {
                "queue_to_api_start": self._timing_summary(
                    [sample[4] for sample in samples]),
                "discord_or_qq_api": self._timing_summary(
                    [sample[5] for sample in samples]),
                "source_to_api_start": self._timing_summary(
                    [sample[6] for sample in samples
                     if sample[6] is not None]),
                "source_to_api_end": self._timing_summary(
                    [sample[7] for sample in samples
                     if sample[7] is not None]),
            }

        sources = sorted({sample[1] for sample in recent})
        platforms = sorted({sample[2] for sample in recent})
        outcomes = sorted({sample[3] for sample in recent})
        return {
            "window_seconds": _DELIVERY_WINDOW_SECONDS,
            "covered_seconds": (
                round(min(_DELIVERY_WINDOW_SECONDS,
                          max(0.0, now - recent[0][0])), 3)
                if recent else 0.0),
            "all": summarize(recent),
            "by_source": {
                source: summarize([
                    sample for sample in recent if sample[1] == source
                ])
                for source in sources
            },
            "by_platform": {
                platform: summarize([
                    sample for sample in recent if sample[2] == platform
                ])
                for platform in platforms
            },
            "by_outcome": {
                outcome: summarize([
                    sample for sample in recent if sample[3] == outcome
                ])
                for outcome in outcomes
            },
        }

    def snapshot(self) -> dict:
        self._roll_day()
        return {
            "started_at": self.started_at,
            "uptime_seconds": int(time.time() - self.started_at),
            "last_poll_at": self.last_poll_at,
            "last_poll": {
                "candidates": self.last_poll_candidates,
                "new": self.last_poll_new,
                "hits": self.last_poll_hits,
            },
            "consecutive_failures": self.consecutive_failures,
            "polls_total": self.polls_total,
            "today": {
                "hits": self.hits_today,
                "pushes": self.pushes_today,
                "push_failures": self.push_failures_today,
                "push_items": self.push_items_today,
                "push_item_failures": self.push_item_failures_today,
            },
            "delivery_latency": self._delivery_timing_snapshot(),
        }


class LogBuffer:
    """loguru sink：环形缓冲 + SSE 订阅广播。全部在单事件循环内使用。"""

    _LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}

    def __init__(self, maxlen: int = 500):
        self._buf: deque[dict] = deque(maxlen=maxlen)
        self._subs: set[asyncio.Queue] = set()

    def sink(self, message):  # loguru sink 接口
        r = message.record
        entry = {
            "ts": r["time"].strftime("%H:%M:%S"),
            "iso": r["time"].isoformat(),
            "level": r["level"].name,
            "msg": r["message"],
            "name": r["name"] or "",
        }
        self._buf.append(entry)
        for q in list(self._subs):
            try:
                q.put_nowait(entry)
            except asyncio.QueueFull:
                pass

    def recent(self, n: int = 200, min_level: str = "INFO") -> list[dict]:
        floor = self._LEVELS.get(min_level.upper(), 20)
        out = [e for e in self._buf if self._LEVELS.get(e["level"], 20) >= floor]
        return out[-n:]

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._subs.discard(q)


STATS = PollerStats()
LOGS = LogBuffer()
_sink_installed = False
_RUNTIME_LOG_PATH = (
    Path(__file__).resolve().parents[3] / ".runtime" / "logs" / "bot.log"
)


def install_log_sink():
    global _sink_installed
    if _sink_installed:
        return
    from nonebot import logger

    def log_filter(record):
        return not record["name"].startswith("uvicorn")

    logger.add(LOGS.sink, level="INFO", filter=log_filter)
    try:
        _RUNTIME_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            str(_RUNTIME_LOG_PATH),
            level="INFO",
            encoding="utf-8",
            rotation="20 MB",
            retention="14 days",
            enqueue=True,
            backtrace=False,
            diagnose=False,
            filter=log_filter,
        )
    except OSError as error:
        logger.warning("BOT 持久日志不可用，继续使用内存日志: {}", error)
    _sink_installed = True
