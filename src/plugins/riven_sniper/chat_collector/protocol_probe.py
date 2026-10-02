"""IRC 在线状态查询的可复现实验配置与响应归纳。"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..platform_identity import (
    PLATFORM_BY_PRIVATE_CHAR,
    PLATFORM_UNKNOWN,
    game_nick_to_irc,
    normalize_platform,
    split_platform_nick,
)
from .config import ALL_SLOTS
from .runtime import atomic_write_json, read_json


PROBE_PLATFORMS = tuple(dict.fromkeys(PLATFORM_BY_PRIVATE_CHAR.values()))
_PLATFORM_GLYPH = {
    platform: glyph for glyph, platform in PLATFORM_BY_PRIVATE_CHAR.items()
}
_NUMERIC = re.compile(r"^(?:@\S+\s+)?(?::\S+\s+)?(\d{3})\s+(.*)$")
_ACCOUNT_ID = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{24})_[^\s@]*")
_ACCOUNT_ID_FULL = re.compile(r"^[0-9a-f]{24}$")
_PROBE_CHANNEL = re.compile(r"^#[A-Z0-9_-]{1,63}$")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def probe_wire_nick(nick: str, platform: str) -> tuple[str, str, str]:
    """返回规范游戏昵称、平台和带 Warframe 平台字形的 IRC 昵称。"""
    clean_nick, glyph_platform = split_platform_nick(nick)
    normalized_platform = normalize_platform(platform)
    if normalized_platform == PLATFORM_UNKNOWN:
        raise ValueError(f"不支持的探测平台: {platform!r}")
    if glyph_platform != PLATFORM_UNKNOWN and glyph_platform != normalized_platform:
        raise ValueError(
            f"昵称平台字形={glyph_platform} 与 --platform={normalized_platform} 不一致"
        )
    if not clean_nick or any(character.isspace() for character in clean_nick):
        raise ValueError("探测昵称不能为空或包含空白字符")
    if any(character in clean_nick for character in "\r\n,"):
        raise ValueError("探测昵称不能包含换行或逗号")
    wire_nick = game_nick_to_irc(clean_nick) + _PLATFORM_GLYPH[normalized_platform]
    return clean_nick, normalized_platform, wire_nick


def normalize_probe_channels(channels: Iterable[str]) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(
        str(channel).strip().upper() for channel in channels
    ))
    if len(normalized) > 20:
        raise ValueError("探测频道不能超过服务器 CHANLIMIT=20")
    if any(_PROBE_CHANNEL.fullmatch(channel) is None for channel in normalized):
        raise ValueError("探测频道必须是有效的 IRC #频道名")
    return normalized


@dataclass(frozen=True)
class ProtocolProbeRequest:
    request_id: str
    slot: str
    target_nick: str
    target_platform: str
    target_irc_nick: str
    duration_seconds: float
    query_interval_seconds: float
    armed_at: str
    target_account_id: str = ""
    probe_channels: tuple[str, ...] = ()
    status: str = "armed"

    @classmethod
    def create(
        cls,
        *,
        slot: str,
        target_nick: str,
        target_platform: str,
        target_account_id: str = "",
        probe_channels: Iterable[str] = (),
        duration_seconds: float = 600.0,
        query_interval_seconds: float = 10.0,
    ) -> "ProtocolProbeRequest":
        normalized_slot = str(slot).strip().upper()
        if normalized_slot not in ALL_SLOTS:
            raise ValueError("探测 slot 必须是 A-Q")
        duration = float(duration_seconds)
        interval = float(query_interval_seconds)
        if duration <= 0 or interval <= 0:
            raise ValueError("探测时长和查询间隔必须大于 0 秒")
        nick, platform, wire_nick = probe_wire_nick(
            target_nick, target_platform
        )
        account_id = str(target_account_id or "").strip().lower()
        if account_id and _ACCOUNT_ID_FULL.fullmatch(account_id) is None:
            raise ValueError("探测 target_account_id 必须是 24 位十六进制")
        channels = normalize_probe_channels(probe_channels)
        if channels and not account_id:
            raise ValueError("频道 A/B 探测必须提供稳定账号 ID")
        return cls(
            request_id=uuid.uuid4().hex,
            slot=normalized_slot,
            target_nick=nick,
            target_platform=platform,
            target_irc_nick=wire_nick,
            duration_seconds=duration,
            query_interval_seconds=interval,
            armed_at=_utc(),
            target_account_id=account_id,
            probe_channels=channels,
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ProtocolProbeRequest":
        nick, platform, wire_nick = probe_wire_nick(
            str(value.get("target_nick") or ""),
            str(value.get("target_platform") or ""),
        )
        request = cls(
            request_id=str(value.get("request_id") or "").strip(),
            slot=str(value.get("slot") or "").strip().upper(),
            target_nick=nick,
            target_platform=platform,
            target_irc_nick=str(value.get("target_irc_nick") or wire_nick),
            duration_seconds=float(value.get("duration_seconds") or 0),
            query_interval_seconds=float(value.get("query_interval_seconds") or 0),
            armed_at=str(value.get("armed_at") or "").strip(),
            target_account_id=str(value.get("target_account_id") or "").strip().lower(),
            probe_channels=normalize_probe_channels(value.get("probe_channels") or ()),
            status=str(value.get("status") or "armed").strip(),
        )
        if not request.request_id or request.slot not in ALL_SLOTS:
            raise ValueError("探测请求缺少有效 request_id 或 slot")
        if request.target_irc_nick != wire_nick:
            raise ValueError("探测请求中的 target_irc_nick 与昵称/平台不一致")
        if request.duration_seconds <= 0 or request.query_interval_seconds <= 0:
            raise ValueError("探测时长和查询间隔必须大于 0 秒")
        if (
            request.target_account_id
            and _ACCOUNT_ID_FULL.fullmatch(request.target_account_id) is None
        ):
            raise ValueError("探测 target_account_id 必须是 24 位十六进制")
        if request.status not in {"armed", "running", "complete", "failed", "stopped"}:
            raise ValueError(f"未知探测状态: {request.status!r}")
        return request

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def arm_protocol_probe(path: str | Path, request: ProtocolProbeRequest) -> None:
    atomic_write_json(path, request.to_dict())


def load_protocol_probe(path: str | Path) -> ProtocolProbeRequest | None:
    value = read_json(path)
    return ProtocolProbeRequest.from_dict(value) if value else None


def update_protocol_probe(
    path: str | Path,
    request: ProtocolProbeRequest,
    *,
    status: str,
    **details: Any,
) -> None:
    current = read_json(path)
    if not current or current.get("request_id") != request.request_id:
        raise RuntimeError("探测请求已被替换或不存在")
    atomic_write_json(path, {
        **current,
        "status": status,
        "updated_at": _utc(),
        **details,
    })


def protocol_probe_commands(
    target_irc_nick: str,
    target_account_id: str = "",
    *,
    include_delayjoined: bool = False,
) -> tuple[str, ...]:
    """返回一次组合探测的初始命令；前五条保留既有基准顺序。"""
    commands = [
        "CAP LS 302",
        f"MONITOR + {target_irc_nick}",
        "MONITOR S",
        f"ISON {target_irc_nick}",
        f"WHOIS {target_irc_nick}",
    ]
    if target_account_id:
        who_flags = "du" if include_delayjoined else "u"
        commands.extend((
            f"WATCH + {target_irc_nick}",
            f"USERHOST {target_irc_nick}",
            f"WHO {target_account_id}_0 {who_flags}%tnu,42",
            f"WHOWAS {target_irc_nick} 1",
            "MODULES",
            "STATS m",
        ))
    return tuple(commands)


def channel_probe_commands(
    channels: Iterable[str],
) -> tuple[tuple[str, str, str, str], ...]:
    """返回 ``(频道, 变体, query type, 命令)`` 的 WHOX A/B 查询。"""
    commands: list[tuple[str, str, str, str]] = []
    for index, channel in enumerate(normalize_probe_channels(channels), start=1):
        plain_query_type = str(100 + index)
        delayed_query_type = str(200 + index)
        commands.extend((
            (
                channel,
                "plain",
                plain_query_type,
                f"WHO {channel} %tnu,{plain_query_type}",
            ),
            (
                channel,
                "delayjoin",
                delayed_query_type,
                f"WHO {channel} d%tnu,{delayed_query_type}",
            ),
        ))
    return tuple(commands)


@dataclass
class ProtocolProbeAnalyzer:
    """把原始响应归纳为支持性、在线事件和稳定账号 ID 证据。"""

    target_irc_nick: str
    target_account_id: str = ""
    probe_channels: tuple[str, ...] = ()
    numeric_codes: list[int] = field(default_factory=list)
    isupport_tokens: list[str] = field(default_factory=list)
    cap_tokens: list[str] = field(default_factory=list)
    account_ids: set[str] = field(default_factory=set)
    cap: str = "unknown"
    monitor: str = "unknown"
    ison: str = "unknown"
    whois: str = "unknown"
    watch: str = "unknown"
    userhost: str = "unknown"
    whox: str = "unknown"
    whowas: str = "unknown"
    modules: str = "unknown"
    stats: str = "unknown"
    monitor_online_seen: bool = False
    monitor_offline_seen: bool = False
    ison_online_seen: bool = False
    ison_current_online: bool = False
    whois_online_seen: bool = False
    watch_online_seen: bool = False
    watch_offline_seen: bool = False
    userhost_online_seen: bool = False
    whox_online_seen: bool = False
    whowas_seen: bool = False
    whox_nicks: set[str] = field(default_factory=set)
    module_lines: list[str] = field(default_factory=list)
    stats_lines: list[str] = field(default_factory=list)
    identity_current_online: bool | None = None
    identity_observations: list[dict[str, Any]] = field(default_factory=list)
    channel_probe_observations: list[dict[str, Any]] = field(default_factory=list)
    probe_joined_channels: set[str] = field(default_factory=set)
    probe_rejected_channels: dict[str, str] = field(default_factory=dict)
    _identity_queries: list[dict[str, Any]] = field(
        default_factory=list, init=False, repr=False
    )
    _channel_queries_by_tag: dict[str, dict[str, Any]] = field(
        default_factory=dict, init=False, repr=False
    )
    _channel_query_order: dict[str, list[str]] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        self.probe_channels = normalize_probe_channels(self.probe_channels)

    def begin_identity_query(self) -> None:
        self._identity_queries.append({
            "started": time.monotonic(),
            "target_seen": False,
        })

    def begin_channel_query(
        self,
        channel: str,
        variant: str,
        query_type: str,
        sweep: int,
    ) -> None:
        query = {
            "channel": channel,
            "variant": variant,
            "query_type": query_type,
            "sweep": sweep,
            "started": time.monotonic(),
            "result_count": 0,
            "target_seen": False,
        }
        self._channel_queries_by_tag[query_type] = query
        self._channel_query_order.setdefault(channel, []).append(query_type)

    def channel_queries_pending(self) -> bool:
        return bool(self._channel_queries_by_tag)

    def identity_queries_pending(self) -> bool:
        return bool(self._identity_queries)

    def _parameters_contain_target(self, parameters: str) -> bool:
        folded = parameters.casefold()
        return (
            self.target_irc_nick.casefold() in folded
            or bool(
                self.target_account_id
                and f"{self.target_account_id}_" in folded
            )
        )

    def _finish_who_query(self, parameters: str) -> None:
        tokens = parameters.split()
        pattern = tokens[1].lstrip(":").upper() if len(tokens) > 1 else ""
        identity_pattern = (
            f"{self.target_account_id}_0".upper()
            if self.target_account_id else ""
        )
        if identity_pattern and pattern == identity_pattern and self._identity_queries:
            query = self._identity_queries.pop(0)
            online = bool(query["target_seen"])
            self.identity_current_online = online
            self.identity_observations.append({
                "observed_at": _utc(),
                "online": online,
                "duration_seconds": round(time.monotonic() - query["started"], 3),
            })

        order = self._channel_query_order.get(pattern)
        if not order:
            return
        query_type = order.pop(0)
        if not order:
            self._channel_query_order.pop(pattern, None)
        query = self._channel_queries_by_tag.pop(query_type, None)
        if query is None:
            return
        self.channel_probe_observations.append({
            "observed_at": _utc(),
            "channel": query["channel"],
            "variant": query["variant"],
            "query_type": query["query_type"],
            "sweep": query["sweep"],
            "target_seen": query["target_seen"],
            "result_count": query["result_count"],
            "duration_seconds": round(time.monotonic() - query["started"], 3),
        })

    def observe(self, line: str) -> None:
        normalized = (line or "").rstrip("\r\n")
        cap_match = re.match(
            r"^(?:@\S+\s+)?(?::\S+\s+)?CAP\s+\S+\s+(?:\*\s+)?(?:LS|LIST)\s+:?(.*)$",
            normalized,
            re.IGNORECASE,
        )
        if cap_match:
            self.cap = "supported"
            self.cap_tokens.extend(
                token for token in cap_match.group(1).split()
                if token not in self.cap_tokens
            )

        numeric = _NUMERIC.match(normalized)
        if numeric is None:
            return
        code = int(numeric.group(1))
        parameters = numeric.group(2)
        if code not in self.numeric_codes:
            self.numeric_codes.append(code)
        for account_id in _ACCOUNT_ID.findall(parameters):
            normalized_account_id = account_id.lower()
            if (
                not self.target_account_id
                or normalized_account_id == self.target_account_id
            ):
                self.account_ids.add(normalized_account_id)

        if code == 5:
            tokens = parameters.split()
            for raw_token in tokens[1:]:
                if raw_token.startswith(":"):
                    break
                token = raw_token
                if token not in self.isupport_tokens:
                    self.isupport_tokens.append(token)
                if token.upper().startswith("MONITOR"):
                    self.monitor = "supported"
                if token.upper().startswith("WATCH"):
                    self.watch = "supported"
                if token.upper() == "WHOX":
                    self.whox = "supported"
        elif code in {730, 731, 732, 733, 734}:
            self.monitor = "supported"
            self.monitor_online_seen |= code == 730
            self.monitor_offline_seen |= code == 731
        elif code == 303:
            self.ison = "supported"
            online = parameters.split(" :", 1)[-1].lstrip(":").split()
            self.ison_current_online = any(
                nick.casefold() == self.target_irc_nick.casefold()
                for nick in online
            )
            self.ison_online_seen |= self.ison_current_online
        elif code in {301, 307, 311, 312, 313, 317, 318, 319, 330, 335, 338, 401}:
            self.whois = "supported"
            self.whois_online_seen |= code == 311
        elif 600 <= code <= 607:
            self.watch = "supported"
            self.watch_online_seen |= code in {600, 604}
            self.watch_offline_seen |= code in {601, 605}
        elif code == 302:
            self.userhost = "supported"
            folded = parameters.casefold()
            self.userhost_online_seen |= (
                self.target_irc_nick.casefold() in folded
                or bool(
                    self.target_account_id
                    and f"{self.target_account_id}_" in folded
                )
            )
        elif code in {352, 354}:
            self.whox = "supported"
            tokens = parameters.split()
            target_seen = self._parameters_contain_target(parameters)
            self.whox_online_seen |= target_seen
            for token in tokens:
                nick = token.lstrip(":")
                if nick.casefold() == self.target_irc_nick.casefold():
                    self.whox_nicks.add(nick)
            if code == 354 and len(tokens) > 1:
                query_type = tokens[1]
                channel_query = self._channel_queries_by_tag.get(query_type)
                if channel_query is not None:
                    channel_query["result_count"] += 1
                    channel_query["target_seen"] |= target_seen
                elif query_type == "42" and self._identity_queries:
                    self._identity_queries[0]["target_seen"] |= target_seen
        elif code == 315:
            self.whox = "supported"
            self._finish_who_query(parameters)
        elif code == 366:
            tokens = parameters.split()
            if len(tokens) > 1:
                channel = tokens[1].lstrip(":").upper()
                if channel in self.probe_channels:
                    self.probe_joined_channels.add(channel)
        elif code in {403, 405, 471, 473, 474, 475}:
            tokens = parameters.split()
            if len(tokens) > 1:
                channel = tokens[1].lstrip(":").upper()
                if channel in self.probe_channels:
                    self.probe_rejected_channels[channel] = parameters
        elif code in {314, 369, 406}:
            self.whowas = "supported"
            self.whowas_seen |= code == 314
        elif code in {702, 703}:
            self.modules = "supported"
            if code == 702 and len(self.module_lines) < 200:
                self.module_lines.append(parameters)
        elif code in {212, 219}:
            self.stats = "supported"
            if code == 212 and len(self.stats_lines) < 200:
                self.stats_lines.append(parameters)
        elif code == 421:
            upper_parameters = parameters.upper()
            if re.search(r"(?:^|\s)CAP(?:\s|:|$)", upper_parameters):
                self.cap = "unsupported"
            if re.search(r"(?:^|\s)MONITOR(?:\s|:|$)", upper_parameters):
                self.monitor = "unsupported"
            if re.search(r"(?:^|\s)ISON(?:\s|:|$)", upper_parameters):
                self.ison = "unsupported"
            if re.search(r"(?:^|\s)WHOIS(?:\s|:|$)", upper_parameters):
                self.whois = "unsupported"
            if re.search(r"(?:^|\s)WATCH(?:\s|:|$)", upper_parameters):
                self.watch = "unsupported"
            if re.search(r"(?:^|\s)USERHOST(?:\s|:|$)", upper_parameters):
                self.userhost = "unsupported"
            if re.search(r"(?:^|\s)WHO(?:\s|:|$)", upper_parameters):
                self.whox = "unsupported"
            if re.search(r"(?:^|\s)WHOWAS(?:\s|:|$)", upper_parameters):
                self.whowas = "unsupported"
            if re.search(r"(?:^|\s)MODULES(?:\s|:|$)", upper_parameters):
                self.modules = "unsupported"
            if re.search(r"(?:^|\s)STATS(?:\s|:|$)", upper_parameters):
                self.stats = "unsupported"
        elif code == 481:
            # 命令存在但当前普通用户权限不足；仍可排除 421 的“命令不存在”。
            upper_parameters = parameters.upper()
            if "MODULE" in upper_parameters:
                self.modules = "restricted"
            elif "STAT" in upper_parameters:
                self.stats = "restricted"

    def summary(self) -> dict[str, Any]:
        paired: dict[tuple[int, str], dict[str, bool]] = {}
        for observation in self.channel_probe_observations:
            key = (int(observation["sweep"]), str(observation["channel"]))
            paired.setdefault(key, {})[str(observation["variant"])] = bool(
                observation["target_seen"]
            )
        confirmed = sorted({
            channel
            for (_sweep, channel), variants in paired.items()
            if variants.get("plain") is False
            and variants.get("delayjoin") is True
        })
        return {
            "target_irc_nick": self.target_irc_nick,
            "cap": self.cap,
            "monitor": self.monitor,
            "ison": self.ison,
            "whois": self.whois,
            "watch": self.watch,
            "userhost": self.userhost,
            "whox": self.whox,
            "whowas": self.whowas,
            "modules": self.modules,
            "stats": self.stats,
            "monitor_online_seen": self.monitor_online_seen,
            "monitor_offline_seen": self.monitor_offline_seen,
            "ison_online_seen": self.ison_online_seen,
            "ison_current_online": self.ison_current_online,
            "whois_online_seen": self.whois_online_seen,
            "watch_online_seen": self.watch_online_seen,
            "watch_offline_seen": self.watch_offline_seen,
            "userhost_online_seen": self.userhost_online_seen,
            "whox_online_seen": self.whox_online_seen,
            "whowas_seen": self.whowas_seen,
            "whox_nicks": sorted(self.whox_nicks),
            "account_ids": sorted(self.account_ids),
            "numeric_codes": self.numeric_codes,
            "isupport_tokens": self.isupport_tokens,
            "cap_tokens": self.cap_tokens,
            "module_lines": self.module_lines,
            "stats_lines": self.stats_lines,
            "identity_current_online": self.identity_current_online,
            "identity_observations": self.identity_observations,
            "probe_channels": list(self.probe_channels),
            "probe_joined_channels": sorted(self.probe_joined_channels),
            "probe_rejected_channels": self.probe_rejected_channels,
            "channel_probe_observations": self.channel_probe_observations,
            "channel_delayjoin_confirmed": bool(confirmed),
            "channel_delayjoin_confirmed_channels": confirmed,
        }


__all__ = [
    "PROBE_PLATFORMS",
    "ProtocolProbeAnalyzer",
    "ProtocolProbeRequest",
    "arm_protocol_probe",
    "channel_probe_commands",
    "load_protocol_probe",
    "normalize_probe_channels",
    "probe_wire_nick",
    "protocol_probe_commands",
    "update_protocol_probe",
]
