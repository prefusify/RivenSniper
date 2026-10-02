"""Warframe IRC 的纯协议函数。"""

from __future__ import annotations

import binascii
import hashlib
import re
from dataclasses import dataclass
from typing import NamedTuple


_WF_PRIVMSG = re.compile(
    r"^:([^!\r\n]+)!([0-9a-fA-F]{24})_[^@\s]*@\S+\s+PRIVMSG\s+(#[^\s]+)\s+:(.*)$",
    re.DOTALL,
)
_LOOSE_PRIVMSG = re.compile(
    r"^:([^!\r\n]+)!\S+\s+PRIVMSG\s+(#[^\s]+)\s+:(.*)$", re.DOTALL
)
_NUMERIC = re.compile(r"^(?::\S+\s+)?(\d{3})\s+\S+(.*)$")
_JOIN = re.compile(r"^:([^!\s]+)(?:!\S+)?\s+JOIN\s+:?(#[^\s,]+)", re.I)
_PART = re.compile(r"^:([^!\s]+)(?:!\S+)?\s+PART\s+(#[^\s,]+)", re.I)
_KICK = re.compile(r"^:\S+\s+KICK\s+(#[^\s,]+)\s+([^\s]+)(?:\s+:(.*))?", re.I)
_CHANNEL = re.compile(r"(?:^|\s):?(#[^\s,]+)")
_AUTH = re.compile(r"NOTICE\s+\*\s+:Auth\s+([0-9A-Fa-f]+):")
_ACCOUNT_ID = re.compile(r"^[0-9a-f]{24}$")
_ACCOUNT_USER = re.compile(r"^([0-9a-fA-F]{24})_[^\s@]*$")
JOIN_CONFIRMATION_CODES = frozenset({353, 366})
JOIN_REJECTION_CODES = frozenset({403, 404, 405, 471, 473, 474, 475})


class Privmsg(NamedTuple):
    nick: str
    sender_id: str
    chan: str
    text: str


class Presence(NamedTuple):
    event_type: str
    nick: str
    sender_id: str
    chan: str
    new_nick: str


class WhoxMember(NamedTuple):
    account_id: str
    irc_nick: str


def parse_privmsg(line: str) -> Privmsg | None:
    normalized = (line or "").rstrip("\r\n")
    match = _WF_PRIVMSG.match(normalized)
    if match:
        return Privmsg(match.group(1), match.group(2).lower(), match.group(3), match.group(4))
    match = _LOOSE_PRIVMSG.match(normalized)
    if match:
        return Privmsg(match.group(1), "", match.group(2), match.group(3))
    return None


_PRESENCE_PREFIX = re.compile(
    r"^:([^!\r\n]+)!([0-9a-fA-F]{24})_[^@\s]*@\S+\s+"
    r"(JOIN|PART|QUIT|NICK)\b(?:\s+(.*))?$",
    re.IGNORECASE,
)


def parse_presence(line: str) -> Presence | None:
    """解析带稳定账号 ID 的成员事件；缺少 ID 时明确返回 ``None``。"""
    match = _PRESENCE_PREFIX.match((line or "").rstrip("\r\n"))
    if match is None:
        return None
    nick, account_id, command, raw_argument = match.groups()
    event_type = command.lower()
    argument = str(raw_argument or "").strip()
    if argument.startswith(":"):
        argument = argument[1:]
    channel = (
        argument.split(maxsplit=1)[0].split(",", 1)[0]
        if event_type in {"join", "part"} and argument
        else ""
    )
    new_nick = argument if event_type == "nick" else ""
    return Presence(event_type, nick, account_id.lower(), channel, new_nick)


def is_account_id(value: str) -> bool:
    return bool(_ACCOUNT_ID.fullmatch((value or "").strip().lower()))


def delayjoin_whox_command(channel: str, query_type: str) -> str:
    normalized_channel = str(channel or "").strip().upper()
    normalized_query_type = str(query_type or "").strip()
    if not normalized_channel.startswith("#"):
        raise ValueError("WHOX 频道必须以 # 开头")
    if (not normalized_query_type.isascii()
            or not normalized_query_type.isdigit()
            or len(normalized_query_type) > 3):
        raise ValueError("WHOX query type 必须是 1 到 3 位 ASCII 数字")
    return f"WHO {normalized_channel} d%tnu,{normalized_query_type}"


def parse_whox_member(
    line: str, *, query_type: str,
) -> WhoxMember | None:
    """解析 ``d%tnu`` 的 354 回复，只接受指定 query type。"""
    numeric = _NUMERIC.match((line or "").rstrip("\r\n"))
    if numeric is None or int(numeric.group(1)) != 354:
        return None
    tokens = numeric.group(2).split(maxsplit=2)
    if len(tokens) != 3 or tokens[0] != str(query_type):
        return None
    account = _ACCOUNT_USER.fullmatch(tokens[1])
    irc_nick = tokens[2].lstrip(":").strip()
    if account is None or not irc_nick:
        return None
    return WhoxMember(account.group(1).lower(), irc_nick)


def parse_whox_query_type(line: str) -> str | None:
    numeric = _NUMERIC.match((line or "").rstrip("\r\n"))
    if numeric is None or int(numeric.group(1)) != 354:
        return None
    tokens = numeric.group(2).split(maxsplit=1)
    return tokens[0] if tokens else None


def parse_who_end(line: str) -> str | None:
    numeric = _NUMERIC.match((line or "").rstrip("\r\n"))
    if numeric is None or int(numeric.group(1)) != 315:
        return None
    tokens = numeric.group(2).split()
    if not tokens:
        return None
    channel = tokens[0].lstrip(":").upper()
    return channel if channel.startswith("#") else None


def is_who_unsupported(line: str) -> bool:
    numeric = _NUMERIC.match((line or "").rstrip("\r\n"))
    if numeric is None or int(numeric.group(1)) != 421:
        return False
    tokens = numeric.group(2).split()
    return bool(tokens) and tokens[0].upper() == "WHO"


def parse_auth_challenge(line: str) -> int | None:
    match = _AUTH.search(line or "")
    return int(match.group(1), 16) & 0x7FFFFFFF if match else None


def irc_password(psk: bytes, timestamp: int, auth_code: int, nonce: str) -> str:
    """已由现有样本验证的 17 轮双 MD5 + XOR token。"""
    if len(psk) < 64:
        raise ValueError("PSK 至少需要 64 字节")
    modulus = len(psk)
    ts = int(timestamp) & 0x7FFFFFFF
    auth = int(auth_code) & 0x7FFFFFFF
    timestamp_offset = ts % modulus
    auth_offset = auth % modulus
    inputs = (
        psk,
        psk[auth_offset:],
        nonce.encode("ascii"),
        psk,
        psk[timestamp_offset:],
    )
    lengths = (
        auth_offset,
        modulus - auth_offset,
        len(nonce),
        timestamp_offset,
        modulus - timestamp_offset,
    )
    digests = (hashlib.md5(), hashlib.md5())
    for index in range(17):
        part = index % 5
        digests[index & 1].update(inputs[part][: lengths[part]])
    mixed = bytes(left ^ right for left, right in zip(digests[0].digest(), digests[1].digest()))
    return binascii.hexlify(ts.to_bytes(4, "big") + mixed).decode("ascii")


@dataclass(frozen=True)
class JoinReport:
    status: str
    joined: tuple[str, ...]
    missing: tuple[str, ...]
    rejected: dict[str, str]
    reason: str = ""


class JoinTracker:
    """精确跟踪每个目标频道，不把多数成功冒充为完整健康。"""

    def __init__(self, channels: list[str] | tuple[str, ...], *, own_nick: str):
        self.targets = frozenset(channels)
        self.own_nick = own_nick.casefold()
        self.confirmed: set[str] = set()
        self.rejected: dict[str, str] = {}

    def observe(self, line: str) -> None:
        normalized = (line or "").rstrip("\r\n")
        join = _JOIN.match(normalized)
        if join:
            nick, channel = join.group(1), join.group(2)
            if nick.casefold() == self.own_nick and channel in self.targets:
                self.confirmed.add(channel)
                self.rejected.pop(channel, None)
            return
        part = _PART.match(normalized)
        if part:
            if part.group(1).casefold() == self.own_nick:
                self.confirmed.discard(part.group(2))
            return
        kick = _KICK.match(normalized)
        if kick:
            channel, target, detail = kick.groups()
            if target.casefold() == self.own_nick and channel in self.targets:
                self.confirmed.discard(channel)
                self.rejected[channel] = f"KICK {detail or ''}".rstrip()
            return

        numeric = _NUMERIC.match(normalized)
        if not numeric:
            return
        code = int(numeric.group(1))
        channel_match = _CHANNEL.search(numeric.group(2))
        if not channel_match:
            return
        channel = channel_match.group(1)
        if channel not in self.targets:
            return
        if code in JOIN_CONFIRMATION_CODES:
            self.confirmed.add(channel)
            self.rejected.pop(channel, None)
        elif code in JOIN_REJECTION_CODES:
            detail = numeric.group(2)[channel_match.end():].strip().lstrip(":")
            self.rejected[channel] = f"{code} {detail}".rstrip()

    def report(self, *, settled: bool) -> JoinReport:
        joined = tuple(sorted(self.confirmed))
        missing = tuple(sorted(self.targets - self.confirmed))
        if not missing:
            status = "healthy"
        elif not settled and not self.rejected:
            status = "joining"
        elif joined:
            status = "degraded"
        else:
            status = "failed" if settled else "joining"
        return JoinReport(status, joined, missing, dict(sorted(self.rejected.items())))


class Heartbeat:
    """客户端 PING/PONG 截止期；普通流量不会掩盖未回复的探针。"""

    def __init__(self, *, interval: float = 90.0, timeout: float = 20.0):
        self.interval = float(interval)
        self.timeout = float(timeout)
        self._last_ping_at: float | None = None
        self._pending_token: str | None = None
        self._pending_at: float | None = None

    def next_ping(self, now: float, slot: str) -> str | None:
        if self._last_ping_at is None:
            self._last_ping_at = now
            return None
        if self._pending_token is not None or now - self._last_ping_at < self.interval:
            return None
        token = f"collector-{slot}-{int(now * 1000)}"
        self._last_ping_at = now
        self._pending_token = token
        self._pending_at = now
        return token

    def observe(self, line: str) -> bool:
        if self._pending_token is None or " PONG " not in f" {line} ":
            return False
        received = line.rstrip("\r\n").rsplit(":", 1)[-1].split()[-1]
        if received != self._pending_token:
            return False
        self._pending_token = None
        self._pending_at = None
        return True

    def expired(self, now: float) -> bool:
        return self._pending_at is not None and now - self._pending_at >= self.timeout
