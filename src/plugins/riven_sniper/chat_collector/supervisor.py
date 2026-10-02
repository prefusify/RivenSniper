"""多拓扑采集 worker 的前台监督器与状态查询。"""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..chat_message import contains_riven_link
from ..runtime_info import runtime_identity
from .config import (
    FOUR_SLOT_MODE,
    FOUR_SLOT_TOPOLOGY,
    REGIONAL_SLOT_LOCALES,
    SEVENTEEN_SLOT_MODE,
    SEVENTEEN_SLOT_TOPOLOGY,
    CollectorTopology,
    CollectorConfigError,
    ReconnectPolicy,
    load_accounts,
    load_presence_snapshot_policy,
    load_reconnect_policy,
    load_shards,
    topology_for_mode,
    validate_psk,
)
from .runtime import (
    append_jsonl,
    atomic_write_json,
    pid_identity,
    pid_matches,
    read_json,
    resume_process,
)
from .session import IrcSession, MessageSinkError, Ticket
from .protocol_probe import (
    ProtocolProbeRequest,
    load_protocol_probe,
    update_protocol_probe,
)


REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_RUNTIME_ROOT = REPO_ROOT / ".runtime" / "chat_collector"
DEFAULT_SHARDS_PATH = REPO_ROOT / "configs" / "chat_collector_shards.json"
DEFAULT_SHARDS_17_PATH = REPO_ROOT / "configs" / "chat_collector_shards_17.json"
ACCOUNTS_EXAMPLE = REPO_ROOT / "configs" / "chat_collector_accounts.example.json"
ACCOUNTS_17_EXAMPLE = (
    REPO_ROOT / "configs" / "chat_collector_accounts_17.example.json"
)

_RETRYABLE_AUTH_STATUSES = {
    "auth_timeout",
    "eof",
    "server_error",
}
_RETRYABLE_DISCONNECT_REASONS = {"eof", "pong_timeout"}
_RECONNECT_RESET_SECONDS = 60.0


def _auth_can_retry(status: str) -> bool:
    return (
        status in _RETRYABLE_AUTH_STATUSES
        or status.startswith("connect_error:")
        or status.startswith("session_error:")
    )


def _disconnect_can_retry(reason: str) -> bool:
    return (
        reason in _RETRYABLE_DISCONNECT_REASONS
        or reason.startswith("socket_error:")
        or reason in {"join_eof", "join_timeout"}
        or reason.startswith("join_socket_error:")
    )


def _reconnect_delay(
    policy: ReconnectPolicy,
    attempt: int,
    slot: str,
) -> float:
    base = policy.backoff_seconds[min(attempt - 1, len(policy.backoff_seconds) - 1)]
    slot_offset = max(0, ord(slot) - ord("A")) * 3.0
    return base + slot_offset + random.uniform(0.0, 5.0)


def default_shards_path(topology: CollectorTopology) -> Path:
    return (
        DEFAULT_SHARDS_PATH
        if topology.mode == FOUR_SLOT_MODE
        else DEFAULT_SHARDS_17_PATH
    )


def accounts_example_path(topology: CollectorTopology) -> Path:
    return (
        ACCOUNTS_EXAMPLE
        if topology.mode == FOUR_SLOT_MODE
        else ACCOUNTS_17_EXAMPLE
    )


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class CollectorLayout:
    root: Path = DEFAULT_RUNTIME_ROOT

    @property
    def accounts_path(self) -> Path:
        return self.root / "accounts.json"

    @property
    def mode_path(self) -> Path:
        return self.root / "collector_mode.json"

    def accounts_path_for(self, topology: CollectorTopology) -> Path:
        return self.root / topology.accounts_filename

    @property
    def psk_path(self) -> Path:
        return self.root / "psk_current.bin"

    @property
    def feed_dir(self) -> Path:
        return self.root / "feed"

    @property
    def tickets_dir(self) -> Path:
        return self.root / "tickets"

    @property
    def states_dir(self) -> Path:
        return self.root / "states"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def probes_dir(self) -> Path:
        return self.root / "probes"

    @property
    def protocol_probe_path(self) -> Path:
        return self.root / "protocol_probe.json"

    @property
    def snapshot_request_path(self) -> Path:
        return self.root / "snapshot_request.json"

    @property
    def supervisor_log_path(self) -> Path:
        return self.logs_dir / "supervisor.log"

    @property
    def supervisor_lock(self) -> Path:
        return self.root / "supervisor.lock.json"

    @property
    def control_path(self) -> Path:
        return self.root / "collector_control.json"

    @property
    def delivery_state_path(self) -> Path:
        return self.root / "irc_delivery_state.json"

    @property
    def stop_flag(self) -> Path:
        return self.root / "collector.stop"

    def ticket_path(self, slot: str) -> Path:
        return self.tickets_dir / f"{slot.upper()}.json"

    def state_path(self, slot: str) -> Path:
        return self.states_dir / f"{slot.upper()}.json"

    def slot_stop_path(self, slot: str) -> Path:
        return self.states_dir / f"{slot.upper()}.stop"

    def log_path(self, slot: str) -> Path:
        return self.logs_dir / f"slot_{slot.upper()}.log"

    def ensure(self) -> None:
        for path in (
            self.root,
            self.feed_dir,
            self.tickets_dir,
            self.states_dir,
            self.logs_dir,
            self.probes_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


def collector_topology(layout: CollectorLayout) -> CollectorTopology:
    value = read_json(layout.mode_path) or {}
    return topology_for_mode(value.get("mode"))


def _ensure_accounts_template(
    layout: CollectorLayout, topology: CollectorTopology,
) -> str:
    destination = layout.accounts_path_for(topology)
    if destination.exists():
        return ""
    shutil.copyfile(accounts_example_path(topology), destination)
    return str(destination)


def set_collector_mode(
    layout: CollectorLayout, mode: object,
) -> CollectorTopology:
    layout.ensure()
    lock = read_json(layout.supervisor_lock) or {}
    if pid_matches(
        int(lock.get("pid") or 0), str(lock.get("process_identity") or ""),
    ):
        raise RuntimeError("采集监督器运行中，必须先正常停止后再切换模式")
    live_slots = []
    for slot in SEVENTEEN_SLOT_TOPOLOGY.slots:
        state = read_json(layout.state_path(slot)) or {}
        if pid_matches(
            int(state.get("pid") or 0),
            str(state.get("process_identity") or ""),
        ):
            live_slots.append(slot)
    if live_slots:
        raise RuntimeError(
            f"槽 {'、'.join(live_slots)} 的 worker 仍在运行，"
            "必须先正常停止全部 worker 后再切换模式"
        )
    topology = topology_for_mode(mode)
    _ensure_accounts_template(layout, topology)
    atomic_write_json(layout.mode_path, {
        "mode": topology.mode,
        "updated_at": _utc(),
    })
    return topology


class TicketStore:
    def __init__(self, layout: CollectorLayout, slot: str):
        self.layout = layout
        self.slot = slot.upper()
        self.path = layout.ticket_path(self.slot)

    def load_available(self) -> Ticket | None:
        value = read_json(self.path)
        if not value or value.get("used"):
            return None
        return Ticket.from_dict(value, expected_slot=self.slot)

    def mark_used(self, ticket: Ticket, *, outcome: str) -> None:
        current = read_json(self.path)
        if not current or current.get("used"):
            raise RuntimeError("票据已被消费或已不存在")
        loaded = Ticket.from_dict(current, expected_slot=self.slot)
        if loaded.ticket_id != ticket.ticket_id:
            raise RuntimeError("票据在认证期间已被替换")
        current["ticket_id"] = ticket.ticket_id
        current["used"] = True
        current["used_at"] = _utc()
        current["outcome"] = outcome
        current.pop("nonce", None)
        current.pop("nonce_str", None)
        atomic_write_json(self.path, current)


class DailyFeedSink:
    def __init__(
        self, layout: CollectorLayout, slot: str, run_id: str = "standalone",
    ):
        self.layout = layout
        self.slot = slot.upper()
        self.run_id = run_id

    def path(self) -> Path:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.layout.feed_dir / f"privmsg_{day}_{self.slot}.jsonl"

    def __call__(self, entry: dict[str, Any]) -> None:
        append_jsonl(self.path(), {
            **entry,
            "slot": str(entry.get("slot") or self.slot),
            "collector_run_id": self.run_id,
        })


class DailyPresenceSink:
    def __init__(
        self,
        layout: CollectorLayout,
        slot: str,
        run_id: str = "standalone",
        *,
        max_snapshot_record_bytes: int = 768 * 1024,
    ):
        self.layout = layout
        self.slot = slot.upper()
        self.run_id = run_id
        self.max_snapshot_record_bytes = int(max_snapshot_record_bytes)

    def path(self) -> Path:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.layout.feed_dir / f"presence_{day}_{self.slot}.jsonl"

    def __call__(self, entry: dict[str, Any]) -> None:
        value = {
            **entry,
            "slot": str(entry.get("slot") or self.slot),
            "collector_run_id": self.run_id,
            "observer_key": f"{self.run_id}:{self.slot}",
        }
        if entry.get("type") == "channel_snapshot":
            encoded_size = len(json.dumps(
                value, ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")) + 1
            if encoded_size > self.max_snapshot_record_bytes:
                raise OSError(
                    f"频道快照记录超过 {self.max_snapshot_record_bytes} 字节"
                )
        append_jsonl(self.path(), value)


def _state(
    layout: CollectorLayout,
    slot: str,
    status: str,
    **details: Any,
) -> None:
    value = {
        "slot": slot,
        "status": status,
        "updated_at": _utc(),
        "pid": os.getpid(),
        "process_identity": pid_identity(os.getpid()),
        **details,
    }
    atomic_write_json(layout.state_path(slot), value)


def _try_state(
    layout: CollectorLayout,
    slot: str,
    status: str,
    **details: Any,
) -> bool:
    """状态文件属于观测面；写失败不能牺牲一次性 IRC 会话。"""
    try:
        _state(layout, slot, status, **details)
    except OSError as error:
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        ticket_id = str(details.get("ticket_id") or "-")
        print(
            f"[{stamp}] slot={slot} ticket={ticket_id} "
            f"状态写入失败 status={status}: {error}",
            file=sys.stderr,
            flush=True,
        )
        return False
    return True


_SUPERVISOR_STATUS_LABELS = {
    "not_started": "等待票据",
    "needs_ticket": "等待票据",
    "authenticating": "认证中",
    "reconnecting": "重连中",
    "probing": "协议探测中",
    "probe_complete": "协议探测完成",
    "joining": "加入频道中",
    "listening": "采集中",
    "degraded": "部分频道可用",
    "stopped": "等待新票据",
    "stop_requested": "停止中",
    "auth_failed": "认证失败",
    "ticket_error": "票据异常",
    "config_error": "配置错误",
    "storage_error": "存储错误",
    "internal_error": "内部错误",
    "disconnected": "连接断开",
    "failed": "加入频道失败",
}


def _supervisor_summary(
    layout: CollectorLayout,
    slots: tuple[str, ...] = FOUR_SLOT_TOPOLOGY.slots,
) -> str:
    parts: list[str] = []
    for slot in slots:
        state = read_json(layout.state_path(slot)) or {
            "status": "not_started",
        }
        status = str(state.get("status") or "not_started")
        label = _SUPERVISOR_STATUS_LABELS.get(status, status)
        alive = pid_matches(
            int(state.get("pid") or 0),
            str(state.get("process_identity") or ""),
        )
        if alive and status in {"joining", "listening", "degraded", "reconnecting"}:
            saved = int(state.get("privmsg_count") or 0)
            filtered = int(state.get("filtered_privmsg_count") or 0)
            snapshots = int(state.get("snapshot_count") or 0)
            joined = len(state.get("joined") or ())
            parts.append(
                f"{slot}:{label} 频道{joined} 快照{snapshots}次 "
                f"紫卡{saved}条 过滤{filtered}条"
            )
        else:
            parts.append(f"{slot}:{label}")
    return " | ".join(parts)


def _terminal_width(value: str) -> int:
    return sum(
        2 if unicodedata.east_asian_width(character) in {"F", "W"} else 1
        for character in value
    )


def _pad_terminal(value: str, width: int, *, right: bool = False) -> str:
    padding = " " * max(0, width - _terminal_width(value))
    return f"{padding}{value}" if right else f"{value}{padding}"


def _supervisor_overview_lines(
    layout: CollectorLayout,
    slots: tuple[str, ...] = FOUR_SLOT_TOPOLOGY.slots,
) -> list[str]:
    rows: list[tuple[str, str, str, str, str, str]] = []
    for slot in slots:
        state = read_json(layout.state_path(slot)) or {
            "status": "not_started",
        }
        status = str(state.get("status") or "not_started")
        label = _SUPERVISOR_STATUS_LABELS.get(status, status)
        alive = pid_matches(
            int(state.get("pid") or 0),
            str(state.get("process_identity") or ""),
        )
        if alive and status in {"joining", "listening", "degraded", "reconnecting"}:
            counts = (
                f"{len(state.get('joined') or ()):,}",
                f"{int(state.get('snapshot_count') or 0):,}",
                f"{int(state.get('privmsg_count') or 0):,}",
                f"{int(state.get('filtered_privmsg_count') or 0):,}",
            )
        else:
            counts = ("—", "—", "—", "—")
        rows.append((slot, label, *counts))

    headers = ("槽", "状态", "频道", "快照", "紫卡", "过滤")
    widths = [
        max(_terminal_width(value) for value in (header, *(row[index] for row in rows)))
        for index, header in enumerate(headers)
    ]

    def render(row: tuple[str, str, str, str, str, str]) -> str:
        return "  ".join(
            _pad_terminal(value, widths[index], right=index >= 2)
            for index, value in enumerate(row)
        )

    return [render(headers), *(render(row) for row in rows)]


def _supervisor_channel_lines(
    layout: CollectorLayout,
    shards: dict[str, tuple[str, ...]],
    *,
    channels_per_line: int = 3,
    slots: tuple[str, ...] = FOUR_SLOT_TOPOLOGY.slots,
) -> list[str]:
    channel_width = max(
        len(channel)
        for channels in shards.values()
        for channel in channels
    )
    count_width = 1
    slot_counts: dict[str, list[tuple[str, int, int]]] = {}
    for slot in slots:
        state = read_json(layout.state_path(slot)) or {}
        saved = state.get("privmsg_by_channel") or {}
        filtered = state.get("filtered_privmsg_by_channel") or {}
        counts = []
        for channel in shards[slot]:
            saved_count = int(saved.get(channel) or 0)
            total_count = saved_count + int(filtered.get(channel) or 0)
            counts.append((channel, saved_count, total_count))
            count_width = max(
                count_width,
                len(f"{saved_count:,}"),
                len(f"{total_count:,}"),
            )
        slot_counts[slot] = counts

    lines: list[str] = []
    for slot in slots:
        counts = slot_counts[slot]
        lines.append(f"  槽 {slot} · {len(counts)} 个频道")
        cells = [
            (
                f"{channel:<{channel_width}} "
                f"{saved_count:>{count_width},} / {total_count:>{count_width},}"
            )
            for channel, saved_count, total_count in counts
        ]
        for offset in range(0, len(cells), channels_per_line):
            chunk = cells[offset:offset + channels_per_line]
            lines.append(f"    {'  │  '.join(chunk)}")
    return lines


def _supervisor_report(
    layout: CollectorLayout,
    shards: dict[str, tuple[str, ...]],
    topology: CollectorTopology = FOUR_SLOT_TOPOLOGY,
) -> str:
    return "\n".join((
        topology.summary_title,
        *(_supervisor_overview_lines(layout, topology.slots)),
        "",
        "逐频道（紫卡 / 总消息）",
        *(_supervisor_channel_lines(layout, shards, slots=topology.slots)),
    ))


def _rotate_log(path: Path, *, max_bytes: int = 10 * 1024 * 1024) -> None:
    try:
        if path.stat().st_size < max_bytes:
            return
    except FileNotFoundError:
        return
    oldest = path.with_suffix(path.suffix + ".3")
    try:
        oldest.unlink()
    except FileNotFoundError:
        pass
    for index in (2, 1):
        source = path.with_suffix(path.suffix + f".{index}")
        if source.exists():
            source.replace(path.with_suffix(path.suffix + f".{index + 1}"))
    path.replace(path.with_suffix(path.suffix + ".1"))


def _print_supervisor(message: str, *, log_path: Path | None = None) -> None:
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    prefix = f"[{stamp}] "
    continuation = "  "
    lines = message.splitlines() or [""]
    rendered_lines = [f"{prefix}{lines[0]}"]
    rendered_lines.extend(
        f"{continuation}{line}" if line else ""
        for line in lines[1:]
    )
    rendered = "\n".join(rendered_lines)
    print(rendered, flush=True)
    if log_path is not None:
        try:
            _rotate_log(log_path)
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write(rendered + "\n")
        except OSError as error:
            print(f"监督器日志写入失败: {error}", file=sys.stderr, flush=True)


def _slot_log(slot: str, ticket_id: str, message: str) -> None:
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    print(
        f"[{stamp}] slot={slot} ticket={ticket_id} {message}",
        flush=True,
    )


def initialize_layout(layout: CollectorLayout) -> dict[str, str]:
    layout.ensure()
    if not layout.mode_path.exists():
        atomic_write_json(layout.mode_path, {
            "mode": FOUR_SLOT_MODE,
            "updated_at": _utc(),
        })
    topology = collector_topology(layout)
    created = _ensure_accounts_template(layout, topology)
    return {
        "runtime_root": str(layout.root),
        "collector_mode": topology.mode,
        "accounts_created": created,
    }


def validate_layout(
    layout: CollectorLayout,
    *,
    shards_path: Path | None = None,
    topology: CollectorTopology | None = None,
) -> list[str]:
    selected = topology or collector_topology(layout)
    selected_shards = shards_path or default_shards_path(selected)
    problems: list[str] = []
    try:
        load_accounts(
            layout.accounts_path_for(selected), topology=selected,
        )
    except CollectorConfigError as error:
        problems.append(str(error))
    try:
        load_shards(selected_shards, topology=selected)
        snapshot_policy = load_presence_snapshot_policy(selected_shards)
        load_reconnect_policy(selected_shards)
        if selected.mode == SEVENTEEN_SLOT_MODE and snapshot_policy.enabled:
            problems.append("17 槽模式必须关闭主动 presence 快照")
    except CollectorConfigError as error:
        problems.append(str(error))
    try:
        validate_psk(layout.psk_path)
    except CollectorConfigError as error:
        problems.append(str(error))
    return problems


def run_slot(
    slot: str,
    layout: CollectorLayout,
    *,
    shards_path: Path | None = None,
    topology: CollectorTopology | None = None,
    plain: bool = False,
    run_id: str = "standalone",
) -> int:
    slot = slot.upper()
    selected = topology or collector_topology(layout)
    selected_shards = shards_path or default_shards_path(selected)
    layout.ensure()
    if slot not in selected.slots:
        _try_state(
            layout,
            slot,
            "config_error",
            error=f"槽 {slot} 不属于当前 {selected.mode} 槽模式",
        )
        return 2
    if layout.stop_flag.exists() or layout.slot_stop_path(slot).exists():
        _try_state(layout, slot, "stop_requested")
        return 3
    try:
        accounts = load_accounts(
            layout.accounts_path_for(selected), topology=selected,
        )
        shards = load_shards(selected_shards, topology=selected)
        snapshot_policy = load_presence_snapshot_policy(selected_shards)
        reconnect_policy = load_reconnect_policy(selected_shards)
        if selected.mode == SEVENTEEN_SLOT_MODE and snapshot_policy.enabled:
            raise CollectorConfigError("17 槽模式必须关闭主动 presence 快照")
        psk_path = validate_psk(layout.psk_path)
        loaded_probe = load_protocol_probe(layout.protocol_probe_path)
        probe_request: ProtocolProbeRequest | None = (
            loaded_probe
            if loaded_probe is not None
            and loaded_probe.slot == slot
            and loaded_probe.status == "armed"
            else None
        )
        ticket_store = TicketStore(layout, slot)
        ticket = ticket_store.load_available()
    except (CollectorConfigError, ValueError) as error:
        _try_state(layout, slot, "config_error", error=str(error))
        return 2
    if ticket is None:
        _try_state(layout, slot, "needs_ticket")
        return 3
    if ticket.nick.casefold() != accounts[slot].casefold():
        _try_state(
            layout,
            slot,
            "config_error",
            error=f"票据 nick={ticket.nick!r} 与账号配置不一致",
        )
        return 2

    try:
        psk = psk_path.read_bytes()
    except OSError as error:
        _try_state(layout, slot, "storage_error", reason=f"PSK 读取失败: {error}")
        _slot_log(slot, ticket.ticket_id, f"worker 结束 status=storage_error reason=PSK 读取失败: {error}")
        return 6
    sink = DailyFeedSink(layout, slot, run_id)
    presence_sink = DailyPresenceSink(
        layout,
        slot,
        run_id,
        max_snapshot_record_bytes=snapshot_policy.max_record_bytes,
    )
    assigned_channels = tuple(shards[slot])
    counters: dict[str, Any] = {
        "privmsg_count": 0,
        "filtered_privmsg_count": 0,
        "privmsg_by_channel": {channel: 0 for channel in assigned_channels},
        "filtered_privmsg_by_channel": {
            channel: 0 for channel in assigned_channels
        },
        "last_event_at": None,
        "snapshot_count": 0,
        "snapshot_member_count": 0,
        "snapshot_last_at": None,
    }
    last_health: dict[str, Any] = {
        "joined": [],
        "missing": list(assigned_channels),
        "rejected": {},
    }
    resumed = False

    def should_stop() -> bool:
        return layout.stop_flag.exists() or layout.slot_stop_path(slot).exists()

    def resume_game() -> bool:
        nonlocal resumed
        if resumed or ticket.game_pid is None:
            return resumed
        resumed = resume_process(ticket.game_pid, ticket.game_identity)
        return resumed

    def on_privmsg(entry: dict[str, Any]) -> None:
        channel = str(entry.get("chan") or "")
        if not contains_riven_link(str(entry.get("text") or "")):
            counters["filtered_privmsg_count"] = (
                int(counters["filtered_privmsg_count"] or 0) + 1
            )
            filtered_by_channel = counters["filtered_privmsg_by_channel"]
            filtered_by_channel[channel] = (
                int(filtered_by_channel.get(channel) or 0) + 1
            )
            return
        sink(entry)
        counters["privmsg_count"] = int(counters["privmsg_count"] or 0) + 1
        by_channel = counters["privmsg_by_channel"]
        by_channel[channel] = int(by_channel.get(channel) or 0) + 1
        counters["last_event_at"] = entry["t"]

    def on_presence(entry: dict[str, Any]) -> None:
        presence_sink(entry)
        if entry.get("type") == "channel_snapshot":
            counters["snapshot_count"] = int(counters["snapshot_count"] or 0) + 1
            counters["snapshot_member_count"] = (
                int(counters["snapshot_member_count"] or 0)
                + len(entry.get("members") or ())
            )
            counters["snapshot_last_at"] = entry.get("t")

    session = IrcSession(ticket, psk, plain=plain)
    session.on_presence = on_presence
    observer_active = False
    first_authenticated_at = 0.0
    retry_started_at = time.time()
    reconnect_attempts = 0
    _slot_log(slot, ticket.ticket_id, f"worker 启动 pid={os.getpid()}")

    def terminal(status: str, reason: str, exit_code: int) -> int:
        _slot_log(
            slot, ticket.ticket_id,
            f"worker 结束 status={status} reason={reason}")
        return exit_code

    def state_details() -> dict[str, Any]:
        return {
            **last_health,
            **counters,
            "ticket_id": ticket.ticket_id,
            "run_id": run_id,
            "game_resumed": resumed,
        }

    def wait_for_reconnect(reason: str) -> str:
        nonlocal reconnect_attempts
        if should_stop():
            return "stop"
        reference = (
            ticket.created_at_timestamp()
            or first_authenticated_at
            or retry_started_at
        )
        ttl_seconds = reconnect_policy.reuse_ttl_hours * 3600.0
        age_seconds = max(0.0, time.time() - reference)
        if not reconnect_policy.enabled:
            terminal_reason = "reconnect_disabled"
        elif reconnect_attempts >= reconnect_policy.max_attempts:
            terminal_reason = "reconnect_attempts_exhausted"
        elif age_seconds >= ttl_seconds:
            terminal_reason = "reuse_ttl_expired"
        else:
            terminal_reason = ""
        if terminal_reason:
            _try_state(
                layout,
                slot,
                "needs_ticket",
                reason=terminal_reason,
                last_disconnect_reason=reason,
                reconnect_attempt=reconnect_attempts,
                **state_details(),
            )
            return "terminal"

        attempt = reconnect_attempts + 1
        delay = _reconnect_delay(reconnect_policy, attempt, slot)
        if age_seconds + delay >= ttl_seconds:
            _try_state(
                layout,
                slot,
                "needs_ticket",
                reason="reuse_ttl_expired",
                last_disconnect_reason=reason,
                reconnect_attempt=reconnect_attempts,
                **state_details(),
            )
            return "terminal"
        reconnect_attempts = attempt
        deadline = time.monotonic() + delay
        next_attempt_at = datetime.fromtimestamp(
            time.time() + delay,
            timezone.utc,
        ).isoformat()
        reconnect_health = {
            **state_details(),
            "joined": [],
            "missing": list(assigned_channels),
            "rejected": {},
            "reconnect_attempt": attempt,
            "next_attempt_at": next_attempt_at,
            "last_disconnect_reason": reason,
        }
        _try_state(layout, slot, "reconnecting", **reconnect_health)
        _slot_log(
            slot,
            ticket.ticket_id,
            f"准备第 {attempt} 次重连，等待 {delay:.1f}s，原因={reason}",
        )
        next_heartbeat = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if should_stop():
                return "stop"
            now = time.monotonic()
            if now >= next_heartbeat:
                _try_state(layout, slot, "reconnecting", **reconnect_health)
                next_heartbeat = now + 30.0
            time.sleep(min(0.25, max(0.0, deadline - now)))
        if should_stop():
            return "stop"
        return "retry"

    _try_state(
        layout, slot, "authenticating",
        ticket_id=ticket.ticket_id, run_id=run_id)
    try:
        while True:
            auth = session.connect_and_auth()
            _slot_log(
                slot,
                ticket.ticket_id,
                f"认证结果={auth.status} attempt={reconnect_attempts} "
                f"diagnostics={json.dumps(auth.diagnostics, ensure_ascii=False)}",
            )
            if auth.authenticated:
                break
            session.close()
            if not _auth_can_retry(auth.status):
                ticket_consumed = False
                if auth.response_sent:
                    try:
                        ticket_store.mark_used(ticket, outcome="auth_failed")
                        ticket_consumed = True
                    except (OSError, RuntimeError) as error:
                        _try_state(
                            layout,
                            slot,
                            "storage_error",
                            reason=f"失败认证的票据状态写入失败: {error}",
                            outcome=auth.status,
                            response_sent=True,
                            auth_diagnostics=auth.diagnostics,
                            ticket_id=ticket.ticket_id,
                            game_resumed=resume_game(),
                        )
                        return terminal(
                            "storage_error",
                            f"失败认证的票据状态写入失败: {error}", 6)
                _try_state(
                    layout,
                    slot,
                    "auth_failed",
                    outcome=auth.status,
                    response_sent=auth.response_sent,
                    ticket_consumed=ticket_consumed,
                    auth_diagnostics=auth.diagnostics,
                    ticket_id=ticket.ticket_id,
                    game_resumed=resume_game(),
                )
                return terminal("auth_failed", auth.status, 4)

            if auth.response_sent:
                current_ticket = read_json(ticket_store.path) or {}
                if not current_ticket.get("used"):
                    try:
                        ticket_store.mark_used(ticket, outcome="auth_retrying")
                    except (OSError, RuntimeError) as error:
                        _try_state(
                            layout,
                            slot,
                            "storage_error",
                            reason=f"重试认证的票据状态写入失败: {error}",
                            outcome=auth.status,
                            response_sent=True,
                            auth_diagnostics=auth.diagnostics,
                            ticket_id=ticket.ticket_id,
                            game_resumed=resume_game(),
                        )
                        return terminal(
                            "storage_error",
                            f"重试认证的票据状态写入失败: {error}", 6)
                elif current_ticket.get("ticket_id") != ticket.ticket_id:
                    detail = "票据在重试认证期间已被替换"
                    _try_state(
                        layout,
                        slot,
                        "storage_error",
                        reason=detail,
                        ticket_id=ticket.ticket_id,
                        game_resumed=resume_game(),
                    )
                    return terminal("storage_error", detail, 6)

            reconnect_reason = f"auth:{auth.status}"
            decision = wait_for_reconnect(reconnect_reason)
            if decision == "stop":
                _try_state(
                    layout, slot, "stopped", reason="stop", **state_details()
                )
                return terminal("stopped", "stop", 0)
            if decision == "terminal":
                return terminal("needs_ticket", reconnect_reason, 6)
            session = IrcSession(ticket, psk, plain=plain)
            session.on_presence = on_presence
            _try_state(
                layout,
                slot,
                "authenticating",
                reconnect_attempt=reconnect_attempts,
                last_disconnect_reason=reconnect_reason,
                **state_details(),
            )
        current_ticket = read_json(ticket_store.path) or {}
        if not current_ticket.get("used"):
            try:
                ticket_store.mark_used(ticket, outcome="authenticated")
            except (OSError, RuntimeError) as error:
                _try_state(
                    layout,
                    slot,
                    "storage_error",
                    reason=f"票据消费状态写入失败: {error}",
                    ticket_id=ticket.ticket_id,
                    game_resumed=resume_game(),
                )
                return terminal(
                    "storage_error", f"票据消费状态写入失败: {error}", 6)
        elif current_ticket.get("ticket_id") != ticket.ticket_id:
            detail = "票据在重试认证期间已被替换"
            _try_state(
                layout,
                slot,
                "storage_error",
                reason=detail,
                ticket_id=ticket.ticket_id,
                game_resumed=resume_game(),
            )
            return terminal("storage_error", detail, 6)
        first_authenticated_at = time.time()
        resume_game()
        if probe_request is not None:
            event_path = (
                layout.probes_dir / f"protocol_probe_{probe_request.request_id}.jsonl"
            )
            summary_path = (
                layout.probes_dir / f"protocol_probe_{probe_request.request_id}.json"
            )
            try:
                update_protocol_probe(
                    layout.protocol_probe_path,
                    probe_request,
                    status="running",
                    started_at=_utc(),
                    ticket_id=ticket.ticket_id,
                    event_path=str(event_path),
                    summary_path=str(summary_path),
                )
            except (OSError, RuntimeError) as error:
                _try_state(
                    layout,
                    slot,
                    "storage_error",
                    reason=f"探测请求状态写入失败: {error}",
                    ticket_id=ticket.ticket_id,
                    game_resumed=resumed,
                )
                return terminal(
                    "storage_error", f"探测请求状态写入失败: {error}", 6
                )

            _try_state(
                layout,
                slot,
                "probing",
                ticket_id=ticket.ticket_id,
                run_id=run_id,
                probe_request_id=probe_request.request_id,
                probe_target_nick=probe_request.target_nick,
                probe_target_platform=probe_request.target_platform,
                probe_target_irc_nick=probe_request.target_irc_nick,
                probe_target_account_id=probe_request.target_account_id,
                probe_channels=list(probe_request.probe_channels),
                probe_event_path=str(event_path),
                game_resumed=resumed,
            )

            def on_probe_event(direction: str, line: str) -> None:
                append_jsonl(event_path, {
                    "t": _utc(),
                    "direction": direction,
                    "line": line,
                })

            try:
                probe_result = session.probe_online_status(
                    probe_request.target_irc_nick,
                    target_account_id=probe_request.target_account_id,
                    probe_channels=probe_request.probe_channels,
                    duration_seconds=probe_request.duration_seconds,
                    query_interval_seconds=probe_request.query_interval_seconds,
                    on_event=on_probe_event,
                    should_stop=should_stop,
                )
                reason = str(probe_result["reason"])
                if reason == "complete":
                    request_status = "complete"
                    state_status = "probe_complete"
                    exit_code = 0
                elif reason == "stopped":
                    request_status = "stopped"
                    state_status = "stopped"
                    exit_code = 0
                else:
                    request_status = "failed"
                    state_status = "disconnected"
                    exit_code = 6
                summary = {
                    **probe_request.to_dict(),
                    "status": request_status,
                    "completed_at": _utc(),
                    "ticket_id": ticket.ticket_id,
                    "event_path": str(event_path),
                    **probe_result,
                }
                atomic_write_json(summary_path, summary)
                update_protocol_probe(
                    layout.protocol_probe_path,
                    probe_request,
                    status=request_status,
                    completed_at=summary["completed_at"],
                    result=probe_result,
                    event_path=str(event_path),
                    summary_path=str(summary_path),
                )
                _try_state(
                    layout,
                    slot,
                    state_status,
                    reason=f"protocol_probe:{reason}",
                    ticket_id=ticket.ticket_id,
                    run_id=run_id,
                    probe_request_id=probe_request.request_id,
                    probe_summary_path=str(summary_path),
                    probe_result=probe_result,
                    game_resumed=resumed,
                )
                return terminal(state_status, f"protocol_probe:{reason}", exit_code)
            except MessageSinkError as error:
                detail = f"探测证据落盘失败: {error}"
            except OSError as error:
                detail = f"探测 socket/落盘失败: {error}"
            except (RuntimeError, ValueError) as error:
                detail = f"探测失败: {error}"
            try:
                update_protocol_probe(
                    layout.protocol_probe_path,
                    probe_request,
                    status="failed",
                    completed_at=_utc(),
                    error=detail,
                    event_path=str(event_path),
                )
            except (OSError, RuntimeError):
                pass
            _try_state(
                layout,
                slot,
                "storage_error" if "落盘" in detail else "disconnected",
                reason=detail,
                ticket_id=ticket.ticket_id,
                run_id=run_id,
                probe_request_id=probe_request.request_id,
                game_resumed=resumed,
            )
            return terminal("failed", detail, 6)

        def start_observer() -> None:
            nonlocal observer_active
            on_presence({
                "t": _utc(), "slot": slot, "type": "observer_start",
                "nick": ticket.nick, "sender_id": ticket.account_id, "chan": "",
            })
            observer_active = True

        def stop_observer() -> str:
            nonlocal observer_active
            if not observer_active:
                return ""
            try:
                on_presence({
                    "t": _utc(), "slot": slot, "type": "observer_stop",
                    "nick": ticket.nick, "sender_id": ticket.account_id, "chan": "",
                })
            except OSError as error:
                return f"presence 结束边界写入失败: {error}"
            observer_active = False
            return ""

        def run_connected(
            active_session: IrcSession,
        ) -> tuple[str, str, float]:
            """运行一次 JOIN/listen，返回状态、原因与稳定监听秒数。"""
            try:
                start_observer()
            except OSError as error:
                return "storage_error", f"presence 开始边界写入失败: {error}", 0.0
            _try_state(
                layout,
                slot,
                "joining",
                reconnect_attempt=reconnect_attempts,
                **state_details(),
            )
            try:
                tracker, report = active_session.join_channels(
                    assigned_channels,
                    on_privmsg=on_privmsg,
                    should_stop=should_stop,
                )
            except MessageSinkError as error:
                return "storage_error", f"JOIN 阶段消息落盘失败: {error}", 0.0
            except OSError as error:
                return "disconnected", f"join_socket_error:{error}", 0.0
            except Exception as error:
                return "internal_error", f"join_internal_error:{error}", 0.0

            last_health.clear()
            last_health.update({
                "joined": list(report.joined),
                "missing": list(report.missing),
                "rejected": report.rejected,
            })
            current_status = (
                "listening" if report.status == "healthy" else report.status
            )
            _try_state(
                layout,
                slot,
                current_status,
                reconnect_attempt=reconnect_attempts,
                **state_details(),
            )
            if report.status == "stopped":
                return "stopped", "join_stopped", 0.0
            if report.reason in {"eof", "timeout"}:
                return "disconnected", f"join_{report.reason}", 0.0
            if report.status == "failed":
                return "failed", "join_failed", 0.0

            def write_health(health: dict[str, Any]) -> None:
                details = {**health, **counters}
                health_status = str(details.pop("status"))
                last_health.clear()
                last_health.update({
                    key: value
                    for key, value in details.items()
                    if key not in counters
                })
                _try_state(
                    layout,
                    slot,
                    health_status,
                    reconnect_attempt=reconnect_attempts,
                    ticket_id=ticket.ticket_id,
                    run_id=run_id,
                    game_resumed=resumed,
                    **details,
                )

            listen_started = time.monotonic()
            reason = active_session.listen(
                tracker,
                on_privmsg=on_privmsg,
                should_stop=should_stop,
                health_callback=write_health,
                presence_snapshot_policy=snapshot_policy,
                snapshot_request_path=layout.snapshot_request_path,
            )
            listened = max(0.0, time.monotonic() - listen_started)
            if reason == "stop":
                return "stopped", reason, listened
            if reason.startswith("storage_error:"):
                return "storage_error", reason, listened
            if reason.startswith("internal_error:"):
                return "internal_error", reason, listened
            return "disconnected", reason, listened

        while True:
            final_status, reason, listened_seconds = run_connected(session)
            boundary_error = stop_observer()
            session.close()
            if boundary_error:
                final_status = "storage_error"
                reason = boundary_error
            if final_status == "stopped":
                _try_state(
                    layout, slot, "stopped", reason=reason, **state_details()
                )
                return terminal("stopped", reason, 0)
            if final_status in {"storage_error", "internal_error", "failed"}:
                _try_state(
                    layout, slot, final_status, reason=reason, **state_details()
                )
                return terminal(final_status, reason, 6 if final_status != "failed" else 5)
            if not _disconnect_can_retry(reason):
                _try_state(
                    layout, slot, "disconnected", reason=reason, **state_details()
                )
                return terminal("disconnected", reason, 6)
            if listened_seconds >= _RECONNECT_RESET_SECONDS:
                reconnect_attempts = 0

            reconnect_reason = reason
            while True:
                decision = wait_for_reconnect(reconnect_reason)
                if decision == "stop":
                    _try_state(
                        layout, slot, "stopped", reason="stop", **state_details()
                    )
                    return terminal("stopped", "stop", 0)
                if decision == "terminal":
                    return terminal("needs_ticket", reconnect_reason, 6)

                session = IrcSession(ticket, psk, plain=plain)
                session.on_presence = on_presence
                _try_state(
                    layout,
                    slot,
                    "authenticating",
                    reconnect_attempt=reconnect_attempts,
                    last_disconnect_reason=reconnect_reason,
                    **state_details(),
                )
                auth = session.connect_and_auth()
                _slot_log(
                    slot,
                    ticket.ticket_id,
                    f"重连认证结果={auth.status} attempt={reconnect_attempts} "
                    f"diagnostics={json.dumps(auth.diagnostics, ensure_ascii=False)}",
                )
                if auth.authenticated:
                    break
                session.close()
                if not _auth_can_retry(auth.status):
                    _try_state(
                        layout,
                        slot,
                        "needs_ticket",
                        reason=f"reconnect_auth:{auth.status}",
                        reconnect_attempt=reconnect_attempts,
                        auth_diagnostics=auth.diagnostics,
                        **state_details(),
                    )
                    return terminal("needs_ticket", auth.status, 6)
                reconnect_reason = f"auth:{auth.status}"
    finally:
        if observer_active:
            try:
                on_presence({
                    "t": _utc(), "slot": slot, "type": "observer_stop",
                    "nick": ticket.nick, "sender_id": ticket.account_id, "chan": "",
                })
            except OSError as error:
                _slot_log(slot, ticket.ticket_id, f"presence 结束边界写入失败: {error}")
        session.close()
        resume_game()


class SupervisorLock:
    def __init__(self, path: Path):
        self.path = path
        self.acquired = False
        self._stream: Any = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(8192)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as error:
                    existing = read_json(self.path) or {}
                    raise RuntimeError(
                        f"采集监督器已运行，pid={existing.get('pid', '?')}"
                    ) from error
            else:
                import fcntl

                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    raise RuntimeError("采集监督器已运行") from error
            payload = json.dumps(
                {
                    "pid": os.getpid(),
                    "process_identity": pid_identity(os.getpid()),
                    "started_at": _utc(),
                },
                ensure_ascii=False,
            ).encode("utf-8")
            stream.seek(0)
            stream.truncate()
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        except Exception:
            stream.close()
            raise
        self._stream = stream
        self.acquired = True

    def release(self) -> None:
        if not self.acquired:
            return
        stream = self._stream
        if stream is not None:
            try:
                stream.seek(0)
                stream.truncate()
                stream.write(json.dumps({
                    "pid": 0,
                    "process_identity": "",
                    "released_at": _utc(),
                }).encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
                if os.name == "nt":
                    import msvcrt

                    stream.seek(8192)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()
        self.acquired = False
        self._stream = None

    def __enter__(self) -> "SupervisorLock":
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


@dataclass
class _Worker:
    process: subprocess.Popen
    log: Any
    ticket_id: str


def run_supervisor(
    layout: CollectorLayout,
    *,
    script_path: Path,
    shards_path: Path | None = None,
    topology: CollectorTopology | None = None,
    plain: bool = False,
) -> int:
    layout.ensure()
    selected = topology or collector_topology(layout)
    selected_shards = shards_path or default_shards_path(selected)
    problems = validate_layout(
        layout, shards_path=selected_shards, topology=selected,
    )
    if problems:
        raise CollectorConfigError("；".join(problems))
    shards = load_shards(selected_shards, topology=selected)
    run_id = uuid.uuid4().hex
    build_identity = runtime_identity()
    started_at = _utc()

    def write_control(status: str) -> None:
        atomic_write_json(layout.control_path, {
            "run_id": run_id,
            "status": status,
            "pid": os.getpid(),
            "process_identity": pid_identity(os.getpid()),
            "started_at": started_at,
            "updated_at": _utc(),
            "git_commit": build_identity["git_commit"],
            "source_mtime": build_identity["source_mtime"],
            "source_mtime_utc": build_identity["source_mtime_utc"],
            "collector_mode": selected.mode,
            "expected_slots": list(selected.slots),
        })

    def report(message: str) -> None:
        _print_supervisor(message, log_path=layout.supervisor_log_path)

    workers: dict[str, _Worker] = {}
    attempted: dict[str, str] = {}
    creation_flags = 0x08000000 if os.name == "nt" else 0

    def reap() -> None:
        for slot, worker in tuple(workers.items()):
            exit_code = worker.process.poll()
            if exit_code is None:
                continue
            worker.log.close()
            workers.pop(slot)
            report(f"槽 {slot} worker 已退出（退出码 {exit_code}）")

    def start_ready_workers() -> None:
        for slot in selected.slots:
            if slot in workers:
                continue
            if layout.slot_stop_path(slot).exists():
                continue
            try:
                ticket = TicketStore(layout, slot).load_available()
            except ValueError as error:
                _try_state(layout, slot, "ticket_error", error=str(error))
                continue
            if ticket is None or attempted.get(slot) == ticket.ticket_id:
                continue
            attempted[slot] = ticket.ticket_id
            log_path = layout.log_path(slot)
            _rotate_log(log_path)
            log = log_path.open("a", encoding="utf-8", buffering=1)
            stamp = datetime.now().astimezone().isoformat(timespec="seconds")
            log.write(
                f"\n[{stamp}] ===== 新运行 slot={slot} "
                f"ticket={ticket.ticket_id} =====\n"
            )
            command = [
                sys.executable,
                str(script_path),
                "slot",
                "--slot",
                slot,
                "--runtime-root",
                str(layout.root),
                "--run-id",
                run_id,
                "--mode",
                selected.mode,
                "--shards",
                str(selected_shards),
            ]
            if plain:
                command.append("--plain")
            try:
                process = subprocess.Popen(
                    command,
                    cwd=REPO_ROOT,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=creation_flags,
                )
            except Exception:
                log.close()
                raise
            workers[slot] = _Worker(process, log, ticket.ticket_id)
            report(
                f"槽 {slot} worker 已启动（PID {process.pid}，"
                f"ticket={ticket.ticket_id}）"
            )

    with SupervisorLock(layout.supervisor_lock):
        _rotate_log(layout.supervisor_log_path)
        for flag in (
            layout.stop_flag,
            *(layout.slot_stop_path(slot) for slot in SEVENTEEN_SLOT_TOPOLOGY.slots),
        ):
            try:
                flag.unlink()
            except FileNotFoundError:
                pass
        write_control("running")
        report(
            f"监督器已启动（PID {os.getpid()}）；"
            f"每 30 秒刷新{'四槽' if selected.mode == FOUR_SLOT_MODE else '十七槽'}"
            "与逐频道采集统计"
        )
        next_periodic_at = 0.0
        try:
            while not layout.stop_flag.exists():
                reap()
                start_ready_workers()
                now = time.monotonic()
                if now >= next_periodic_at:
                    report(_supervisor_report(layout, shards, selected))
                    next_periodic_at = now + 30.0
                time.sleep(1.0)
        except KeyboardInterrupt:
            atomic_write_json(layout.stop_flag, {"requested_at": _utc(), "reason": "keyboard"})
        finally:
            write_control("stopping")
            report(
                "收到停止请求，正在等待"
                f"{'四槽' if selected.mode == FOUR_SLOT_MODE else '十七槽'} worker 退出"
            )
            deadline = time.monotonic() + 10.0
            while workers and time.monotonic() < deadline:
                reap()
                if workers:
                    time.sleep(0.2)
            for worker in tuple(workers.values()):
                worker.process.terminate()
            terminate_deadline = time.monotonic() + 3.0
            while workers and time.monotonic() < terminate_deadline:
                reap()
                if workers:
                    time.sleep(0.1)
            for slot, worker in tuple(workers.items()):
                worker.process.kill()
                worker.log.close()
                workers.pop(slot)
            write_control("stopped")
            report("监督器与全部采集 worker 已停止")
    return 0


def request_stop(layout: CollectorLayout) -> None:
    layout.ensure()
    control = read_json(layout.control_path) or {}
    if control.get("run_id"):
        atomic_write_json(layout.control_path, {
            **control,
            "status": "stopping",
            "updated_at": _utc(),
        })
    atomic_write_json(layout.stop_flag, {"requested_at": _utc(), "reason": "operator"})


def collector_status(
    layout: CollectorLayout,
    topology: CollectorTopology | None = None,
) -> dict[str, Any]:
    lock = read_json(layout.supervisor_lock) or {}
    supervisor_pid = int(lock.get("pid") or 0)
    running = pid_matches(supervisor_pid, str(lock.get("process_identity") or ""))
    control = read_json(layout.control_path) or {}
    selected = topology or collector_topology(layout)
    if running and control.get("collector_mode"):
        selected = topology_for_mode(control["collector_mode"])
    slots: dict[str, Any] = {}
    for slot in selected.slots:
        state = read_json(layout.state_path(slot)) or {"slot": slot, "status": "not_started"}
        state_pid = int(state.get("pid") or 0)
        state["process_alive"] = pid_matches(
            state_pid, str(state.get("process_identity") or "")
        )
        state["stop_requested"] = (
            layout.stop_flag.exists() or layout.slot_stop_path(slot).exists()
        )
        state["collector_mode"] = selected.mode
        if selected.mode == SEVENTEEN_SLOT_MODE:
            state["region"] = REGIONAL_SLOT_LOCALES[slot]
        slots[slot] = state
    return {
        "runtime_root": str(layout.root),
        "collector_mode": selected.mode,
        "expected_slots": list(selected.slots),
        "accounts_path": str(layout.accounts_path_for(selected)),
        "supervisor_running": running,
        "supervisor_pid": supervisor_pid if running else None,
        "stop_requested": layout.stop_flag.exists(),
        "control": control,
        "delivery": read_json(layout.delivery_state_path) or {},
        "slots": slots,
    }
