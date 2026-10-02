"""游戏频道采集文件的轻量健康状态。"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .chat_collector.config import FOUR_SLOTS, topology_for_mode
from .chat_collector.runtime import pid_matches


DEFAULT_IRC_FEED_STALE_SECONDS = 180.0
_DAILY_SLOT_FILE = re.compile(
    r"^privmsg_\d{4}-\d{2}-\d{2}_([A-Q])\.jsonl$", re.I
)
_READY_COLLECTOR_STATUSES = {"listening", "degraded"}
_STARTING_COLLECTOR_STATUSES = {"authenticating", "joining", "reconnecting"}


def _collector_state_health(
        feed_path: Path, *, stale_seconds: float, now: float) -> dict | None:
    """分槽严格过滤时，用 worker 状态心跳区分“安静”和“停更”。"""
    match = _DAILY_SLOT_FILE.fullmatch(feed_path.name)
    if not match:
        return None
    state_path = feed_path.parent.parent / "states" / f"{match.group(1).upper()}.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    worker_status = str(state.get("status") or "")
    value = str(state.get("updated_at") or "").strip()
    try:
        updated_at = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    age = max(0.0, now - updated_at)
    alive = pid_matches(
        int(state.get("pid") or 0), str(state.get("process_identity") or ""))
    fresh = age < stale_seconds
    joined = tuple(state.get("joined") or ())
    ready = alive and fresh and bool(joined) and (
        worker_status in _READY_COLLECTOR_STATUSES)
    if ready and worker_status == "listening":
        status = "live"
    elif ready:
        status = "degraded"
    elif alive and fresh and worker_status in _STARTING_COLLECTOR_STATUSES:
        status = "starting"
    elif not alive or worker_status in {
        "stopped", "stop_requested", "not_started", "needs_ticket",
    }:
        status = "stopped"
    else:
        status = "stale"
    return {
        "status": status,
        "last_event_at": updated_at,
        "age_seconds": age,
        "source": "collector_state",
        "process_alive": alive,
        "feed_ready": ready,
    }


def irc_feed_file_health(
        path: str | Path, *, stale_seconds: float = DEFAULT_IRC_FEED_STALE_SECONDS,
        now: float | None = None) -> dict:
    """优先读取分槽 worker 心跳；旧单文件模式仍使用文件修改时间。"""
    if not path:
        return {"status": "missing", "last_event_at": None, "age_seconds": None}
    feed_path = Path(path)
    current = time.time() if now is None else now
    state_health = _collector_state_health(
        feed_path, stale_seconds=stale_seconds, now=current
    )
    if state_health is not None:
        return state_health
    try:
        stat = feed_path.stat()
    except OSError:
        return {"status": "missing", "last_event_at": None, "age_seconds": None}
    age = max(0.0, current - stat.st_mtime)
    return {
        "status": "stale" if age >= stale_seconds else "live",
        "last_event_at": stat.st_mtime,
        "age_seconds": age,
    }


def irc_feed_paths(
        *, feed_dir: str | Path = "", feed_path: str | Path = "",
        now: float | None = None) -> tuple[Path, ...]:
    """按当前采集模式返回 UTC 日输入，兼容旧单文件路径。"""
    if feed_dir:
        timestamp = time.time() if now is None else now
        day = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
        root = Path(feed_dir)
        slots = irc_feed_expected_slots(root)
        return tuple(root / f"privmsg_{day}_{slot}.jsonl" for slot in slots)
    return (Path(feed_path),) if feed_path else ()


def irc_feed_expected_slots(feed_dir: str | Path) -> tuple[str, ...]:
    root = Path(feed_dir)
    runtime_root = root.parent
    try:
        control = json.loads(
            (runtime_root / "collector_control.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        control = {}
    raw_slots = control.get("expected_slots") if isinstance(control, dict) else None
    if (
        isinstance(raw_slots, list)
        and control.get("status") in {"running", "stopping"}
    ):
        slots = tuple(str(slot).strip().upper() for slot in raw_slots)
        if slots and len(slots) == len(set(slots)) and all(
            len(slot) == 1 and "A" <= slot <= "Q" for slot in slots
        ):
            return slots
    try:
        mode = json.loads(
            (runtime_root / "collector_mode.json").read_text(encoding="utf-8")
        ).get("mode")
        return topology_for_mode(mode).slots
    except (AttributeError, OSError, ValueError, json.JSONDecodeError):
        return FOUR_SLOTS


def irc_feed_sources_health(
        paths: Iterable[str | Path], *,
        stale_seconds: float = DEFAULT_IRC_FEED_STALE_SECONDS,
        now: float | None = None) -> dict:
    """聚合多个分槽文件；任何分槽缺失或停更都会显式降级。"""
    details = {
        str(Path(path)): irc_feed_file_health(
            path, stale_seconds=stale_seconds, now=now)
        for path in paths
    }
    expected_count = len(details)
    ready_count = sum(
        health["status"] in {"live", "degraded"}
        for health in details.values()
    )
    existing = [health for health in details.values() if health["status"] != "missing"]
    if not existing:
        return {
            "status": "missing", "last_event_at": None,
            "age_seconds": None, "sources": details,
            "live_source_count": 0,
            "expected_source_count": expected_count,
        }
    latest = max(float(health["last_event_at"]) for health in existing)
    current = time.time() if now is None else now
    if (ready_count == expected_count and all(
            health["status"] == "live" for health in details.values())):
        status = "live"
    elif ready_count:
        status = "degraded"
    elif any(health["status"] == "stale" for health in existing):
        status = "stale"
    elif any(health["status"] == "starting" for health in existing):
        status = "starting"
    else:
        status = "stopped"
    return {
        "status": status,
        "last_event_at": latest,
        "age_seconds": max(0.0, current - latest),
        "sources": details,
        "live_source_count": ready_count,
        "expected_source_count": expected_count,
    }
