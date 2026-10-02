"""生产采集中的 Delayed-WHOX 频道成员快照。"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import ALL_SLOTS, PresenceSnapshotPolicy
from .protocol import (
    Presence,
    delayjoin_whox_command,
    is_who_unsupported,
    parse_who_end,
    parse_whox_member,
    parse_whox_query_type,
)
from .runtime import atomic_write_json, read_json


_CHANNEL = re.compile(r"^#[A-Z0-9_-]{1,63}$")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class SnapshotRequest:
    request_id: str
    slot: str
    channels: tuple[str, ...]
    armed_at: str
    status: str = "armed"

    @classmethod
    def create(
        cls,
        *,
        slot: str,
        channels: Iterable[str],
    ) -> "SnapshotRequest":
        normalized_slot = str(slot).strip().upper()
        if normalized_slot not in ALL_SLOTS:
            raise ValueError("快照 slot 必须是 A-Q")
        normalized_channels = tuple(dict.fromkeys(
            str(channel).strip().upper() for channel in channels
        ))
        if not normalized_channels:
            raise ValueError("手动快照至少需要一个频道")
        if len(normalized_channels) > 20:
            raise ValueError("手动快照频道不能超过服务器 CHANLIMIT=20")
        if any(_CHANNEL.fullmatch(channel) is None for channel in normalized_channels):
            raise ValueError("手动快照包含无效 IRC 频道名")
        return cls(
            request_id=uuid.uuid4().hex,
            slot=normalized_slot,
            channels=normalized_channels,
            armed_at=_utc(),
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SnapshotRequest":
        request = cls(
            request_id=str(value.get("request_id") or "").strip(),
            slot=str(value.get("slot") or "").strip().upper(),
            channels=tuple(dict.fromkeys(
                str(channel).strip().upper()
                for channel in value.get("channels") or ()
            )),
            armed_at=str(value.get("armed_at") or "").strip(),
            status=str(value.get("status") or "armed").strip(),
        )
        if not request.request_id or request.slot not in ALL_SLOTS:
            raise ValueError("手动快照请求缺少有效 request_id 或 slot")
        if not request.armed_at:
            raise ValueError("手动快照请求缺少 armed_at")
        if (
            not request.channels
            or len(request.channels) > 20
            or any(_CHANNEL.fullmatch(channel) is None for channel in request.channels)
        ):
            raise ValueError("手动快照请求包含无效频道")
        if request.status not in {
            "armed", "running", "complete", "failed", "cancelled",
        }:
            raise ValueError(f"未知手动快照状态: {request.status!r}")
        return request

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def arm_snapshot_request(path: str | Path, request: SnapshotRequest) -> None:
    current = read_json(path)
    if current and current.get("status") in {"armed", "running"}:
        raise RuntimeError("已有尚未完成的手动快照请求")
    atomic_write_json(path, request.to_dict())


def load_snapshot_request(path: str | Path) -> SnapshotRequest | None:
    value = read_json(path)
    return SnapshotRequest.from_dict(value) if value else None


def cancel_snapshot_request(
    path: str | Path,
    request: SnapshotRequest,
    *,
    expected_status: str,
    reason: str,
) -> None:
    if expected_status not in {"armed", "running"}:
        raise ValueError(f"无效的手动快照活动状态: {expected_status}")
    current = read_json(path)
    if not current or current.get("request_id") != request.request_id:
        raise RuntimeError("手动快照请求已被替换或不存在")
    if current.get("status") != expected_status:
        raise RuntimeError("手动快照请求状态已发生变化")
    atomic_write_json(path, {
        **current,
        "status": "cancelled",
        "updated_at": _utc(),
        "completed_at": _utc(),
        "error": reason,
        "result": {},
    })


def update_snapshot_request(
    path: str | Path,
    request: SnapshotRequest,
    *,
    status: str,
    **details: Any,
) -> None:
    if status not in {"running", "complete", "failed", "cancelled"}:
        raise ValueError(f"无效的手动快照状态: {status}")
    current = read_json(path)
    if not current or current.get("request_id") != request.request_id:
        raise RuntimeError("手动快照请求已被替换或不存在")
    current_status = str(current.get("status") or "")
    if status == "running" and current_status != "armed":
        raise RuntimeError("手动快照请求已被其他 worker 认领")
    if status != "running" and current_status != "running":
        raise RuntimeError("手动快照请求不在运行中")
    atomic_write_json(path, {
        **current,
        "status": status,
        "updated_at": _utc(),
        **details,
    })


@dataclass
class _PendingSnapshot:
    channel: str
    query_type: str
    started_at: float
    members: dict[str, str] = field(default_factory=dict)
    membership_overrides: dict[str, str | None] = field(default_factory=dict)
    nick_overrides: dict[str, str] = field(default_factory=dict)
    matching_reply_seen: bool = False
    query_type_mismatch: bool = False


class PresenceSnapshotScheduler:
    """单槽单在途 WHOX 调度器；仅完整回复可以生成快照。"""

    def __init__(
        self,
        channels: Iterable[str],
        *,
        slot: str,
        collector_account_id: str,
        policy: PresenceSnapshotPolicy,
        started_at: float | None = None,
        stagger_by_slot: bool = True,
        expire_unjoined: bool = False,
    ) -> None:
        self.channels = tuple(dict.fromkeys(
            str(channel).strip().upper() for channel in channels
        ))
        self.slot = str(slot).strip().upper()
        self.collector_account_id = str(collector_account_id).strip().lower()
        self.policy = policy
        now = time.monotonic() if started_at is None else float(started_at)
        slot_offset = (
            max(0, ord(self.slot or "A") - ord("A"))
            if stagger_by_slot else 0
        )
        first_at = now + policy.initial_delay_seconds + slot_offset
        self._due = {
            channel: first_at + index * policy.request_gap_seconds
            for index, channel in enumerate(self.channels)
        }
        self._next_send_at = first_at
        self._query_sequence = 100
        self._pending: _PendingSnapshot | None = None
        self._disabled_reason = "" if policy.enabled else "disabled"
        self._expire_unjoined = bool(expire_unjoined)
        self._failed_channels: dict[str, str] = {}
        self.completed_count = 0
        self.failed_count = 0
        self.last_member_count = 0
        self.last_success_at: float | None = None
        self.last_error = ""

    def _next_query_type(self) -> str:
        self._query_sequence += 1
        if self._query_sequence > 999:
            self._query_sequence = 100
        return str(self._query_sequence)

    def _finish(self, now: float, *, error: str = "") -> None:
        pending = self._pending
        if pending is None:
            return
        self._due.pop(pending.channel, None)
        self._next_send_at = now + self.policy.request_gap_seconds
        if error:
            self.failed_count += 1
            self.last_error = error
            self._failed_channels[pending.channel] = error
        self._pending = None

    def next_command(
        self, now: float, joined_channels: Iterable[str],
    ) -> str | None:
        if self._disabled_reason:
            return None
        joined = {str(channel).strip().upper() for channel in joined_channels}
        if self._expire_unjoined:
            for channel, due_at in tuple(self._due.items()):
                if (
                    self._pending is not None
                    and channel == self._pending.channel
                ):
                    continue
                if channel not in joined and now - due_at >= self.policy.timeout_seconds:
                    self._due.pop(channel, None)
                    self.failed_count += 1
                    self.last_error = "channel_not_joined"
                    self._failed_channels[channel] = "channel_not_joined"
        if self._pending is not None:
            if self._pending.channel not in joined:
                self._finish(now, error="channel_not_joined")
            elif now - self._pending.started_at >= self.policy.timeout_seconds:
                self._finish(now, error="timeout")
            return None
        if now < self._next_send_at:
            return None
        due = sorted(
            (
                (when, channel) for channel, when in self._due.items()
                if channel in joined and when <= now
            ),
            key=lambda item: (item[0], item[1]),
        )
        if not due:
            return None
        channel = due[0][1]
        query_type = self._next_query_type()
        self._pending = _PendingSnapshot(channel, query_type, now)
        return delayjoin_whox_command(channel, query_type)

    def observe_presence(self, presence: Presence) -> None:
        pending = self._pending
        if pending is None:
            return
        account_id = presence.sender_id
        if not account_id or account_id == self.collector_account_id:
            return
        if presence.event_type == "join" and presence.chan.upper() == pending.channel:
            pending.membership_overrides[account_id] = presence.nick
        elif presence.event_type == "part" and presence.chan.upper() == pending.channel:
            pending.membership_overrides[account_id] = None
        elif presence.event_type == "quit":
            pending.membership_overrides[account_id] = None
        elif presence.event_type == "nick" and presence.new_nick:
            pending.nick_overrides[account_id] = presence.new_nick

    def observe_line(
        self,
        line: str,
        *,
        now: float,
        joined_channels: Iterable[str],
    ) -> dict[str, Any] | None:
        pending = self._pending
        if pending is None:
            return None
        if is_who_unsupported(line):
            self._disabled_reason = "who_unsupported"
            self._finish(now, error="who_unsupported")
            return None
        reply_query_type = parse_whox_query_type(line)
        if (reply_query_type is not None
                and reply_query_type != pending.query_type):
            pending.query_type_mismatch = True
            return None
        member = parse_whox_member(line, query_type=pending.query_type)
        if member is not None:
            pending.matching_reply_seen = True
            if member.account_id != self.collector_account_id:
                pending.members[member.account_id] = member.irc_nick
            return None
        ended_channel = parse_who_end(line)
        if ended_channel != pending.channel:
            return None
        joined = {str(channel).strip().upper() for channel in joined_channels}
        if pending.channel not in joined:
            self._finish(now, error="channel_not_joined")
            return None
        if pending.query_type_mismatch:
            self._finish(now, error="query_type_mismatch")
            return None
        if not pending.matching_reply_seen:
            self._finish(now, error="no_matching_reply")
            return None
        for account_id, irc_nick in pending.nick_overrides.items():
            if account_id in pending.members:
                pending.members[account_id] = irc_nick
        for account_id, irc_nick in pending.membership_overrides.items():
            if irc_nick is None:
                pending.members.pop(account_id, None)
            else:
                pending.members[account_id] = pending.nick_overrides.get(
                    account_id, irc_nick,
                )
        members = [
            [account_id, irc_nick]
            for account_id, irc_nick in sorted(pending.members.items())
        ]
        event = {
            "type": "channel_snapshot",
            "snapshot_id": uuid.uuid4().hex,
            "chan": pending.channel,
            "members": members,
        }
        size = len(json.dumps(
            event, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8"))
        if size > self.policy.max_record_bytes - 1024:
            self._finish(now, error="record_size")
            return None
        self.completed_count += 1
        self.last_member_count = len(members)
        self.last_success_at = time.time()
        self._finish(now)
        return event

    def health(self) -> dict[str, Any]:
        if self._disabled_reason == "disabled":
            coverage = "passive"
        elif self._disabled_reason or self._failed_channels:
            coverage = "degraded"
        elif not self._due and self._pending is None:
            coverage = "complete"
        else:
            coverage = "syncing"
        return {
            "snapshot_coverage": coverage,
            "snapshot_pending_channel": (
                self._pending.channel if self._pending is not None else None
            ),
            "snapshot_completed_count": self.completed_count,
            "snapshot_failed_count": self.failed_count,
            "snapshot_failed_channels": sorted(self._failed_channels),
            "snapshot_last_member_count": self.last_member_count,
            "snapshot_last_success_at": self.last_success_at,
            "snapshot_last_error": self.last_error,
        }

    @property
    def finished(self) -> bool:
        return bool(self._disabled_reason) or (
            not self._due and self._pending is None
        )


__all__ = [
    "PresenceSnapshotScheduler",
    "SnapshotRequest",
    "arm_snapshot_request",
    "cancel_snapshot_request",
    "load_snapshot_request",
    "update_snapshot_request",
]
