"""独立采集器的连接身份与频道分片配置。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


FOUR_SLOT_MODE = "4"
SEVENTEEN_SLOT_MODE = "17"
FOUR_SLOTS = tuple("ABCD")
SEVENTEEN_SLOTS = tuple("ABCDEFGHIJKLMNOPQ")
ALL_SLOTS = SEVENTEEN_SLOTS
# 保留给现有导入方的四槽兼容别名；新代码按 CollectorTopology 取槽位。
SLOTS = FOUR_SLOTS
CHANNEL_KINDS = ("G", "Q", "R", "T")
NON_ENGLISH_LANGUAGES = (
    "ZH", "FR", "DE", "ES", "PT", "RU", "JA", "KO", "TC", "IT", "PL", "UK",
)
ENGLISH_REGIONS = ("NA", "EU", "SA", "RU", "AS")
BARE_EN_CHANNELS = frozenset(f"#{kind}_EN" for kind in CHANNEL_KINDS)
EXPECTED_CHANNELS = frozenset(
    [
        f"#{kind}_{language}"
        for kind in CHANNEL_KINDS
        for language in NON_ENGLISH_LANGUAGES
    ]
    + [
        f"#{kind}_EN_{region}"
        for kind in CHANNEL_KINDS
        for region in ENGLISH_REGIONS
    ]
)
REGIONAL_SLOT_LOCALES = {
    "A": "ZH",
    "B": "FR",
    "C": "DE",
    "D": "ES",
    "E": "PT",
    "F": "RU",
    "G": "JA",
    "H": "KO",
    "I": "TC",
    "J": "IT",
    "K": "PL",
    "L": "UK",
    "M": "EN_NA",
    "N": "EN_EU",
    "O": "EN_SA",
    "P": "EN_RU",
    "Q": "EN_AS",
}


class CollectorConfigError(ValueError):
    """采集配置无法安全执行。"""


@dataclass(frozen=True)
class CollectorTopology:
    mode: str
    slots: tuple[str, ...]
    accounts_filename: str
    shards_filename: str
    summary_title: str


FOUR_SLOT_TOPOLOGY = CollectorTopology(
    mode=FOUR_SLOT_MODE,
    slots=FOUR_SLOTS,
    accounts_filename="accounts.json",
    shards_filename="chat_collector_shards.json",
    summary_title="四槽概览",
)
SEVENTEEN_SLOT_TOPOLOGY = CollectorTopology(
    mode=SEVENTEEN_SLOT_MODE,
    slots=SEVENTEEN_SLOTS,
    accounts_filename="accounts_17.json",
    shards_filename="chat_collector_shards_17.json",
    summary_title="十七槽概览",
)


def normalize_collector_mode(value: object) -> str:
    mode = str(value or FOUR_SLOT_MODE).strip()
    if mode not in {FOUR_SLOT_MODE, SEVENTEEN_SLOT_MODE}:
        raise CollectorConfigError("采集模式必须是 4 或 17")
    return mode


def topology_for_mode(value: object) -> CollectorTopology:
    mode = normalize_collector_mode(value)
    return (
        FOUR_SLOT_TOPOLOGY
        if mode == FOUR_SLOT_MODE
        else SEVENTEEN_SLOT_TOPOLOGY
    )


@dataclass(frozen=True)
class PresenceSnapshotPolicy:
    enabled: bool = True
    manual_enabled: bool = True
    initial_delay_seconds: float = 5.0
    request_gap_seconds: float = 4.0
    timeout_seconds: float = 30.0
    max_record_bytes: int = 768 * 1024


@dataclass(frozen=True)
class ReconnectPolicy:
    enabled: bool = True
    backoff_seconds: tuple[float, ...] = (15.0, 30.0, 60.0, 120.0, 300.0)
    max_attempts: int = 12
    reuse_ttl_hours: float = 6.0


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as error:
        raise CollectorConfigError(f"配置不存在: {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise CollectorConfigError(f"无法读取配置 {path}: {error}") from error
    if not isinstance(value, dict):
        raise CollectorConfigError(f"配置根节点必须是对象: {path}")
    return value


def load_shards(
    path: str | Path,
    *,
    topology: CollectorTopology = FOUR_SLOT_TOPOLOGY,
) -> dict[str, tuple[str, ...]]:
    """读取并严格验证当前拓扑的 68 频道产品分片。"""
    source = Path(path)
    document = _read_object(source)
    raw_shards = document.get("shards")
    if not isinstance(raw_shards, dict) or set(raw_shards) != set(topology.slots):
        expected = "、".join(topology.slots)
        raise CollectorConfigError(f"{topology.mode} 槽模式分片必须且只能包含 {expected}")

    shards: dict[str, tuple[str, ...]] = {}
    assigned: list[str] = []
    for slot in topology.slots:
        raw = raw_shards[slot]
        channels = raw.get("channels") if isinstance(raw, dict) else raw
        if not isinstance(channels, list) or not channels:
            raise CollectorConfigError(f"槽 {slot} 的 channels 必须是非空数组")
        normalized = tuple(str(channel).strip() for channel in channels)
        if any(not channel.startswith("#") for channel in normalized):
            raise CollectorConfigError(f"槽 {slot} 存在非法频道名")
        if len(normalized) > 20:
            raise CollectorConfigError(f"槽 {slot} 有 {len(normalized)} 个频道，超过上限 20")
        if topology.mode == SEVENTEEN_SLOT_MODE:
            locale = REGIONAL_SLOT_LOCALES[slot]
            expected_channels = {
                f"#{kind}_{locale}" for kind in CHANNEL_KINDS
            }
            if len(normalized) != 4 or set(normalized) != expected_channels:
                raise CollectorConfigError(
                    f"17 槽模式的槽 {slot} 必须只包含地区 {locale} 的 G/Q/R/T 四频道"
                )
        shards[slot] = normalized
        assigned.extend(normalized)

    if len(assigned) != len(set(assigned)):
        raise CollectorConfigError("分片频道存在重复")
    actual = set(assigned)
    if actual != EXPECTED_CHANNELS:
        missing = sorted(EXPECTED_CHANNELS - actual)
        extra = sorted(actual - EXPECTED_CHANNELS)
        raise CollectorConfigError(f"分片矩阵不完整，缺少={missing}，多出={extra}")
    if actual & BARE_EN_CHANNELS:
        raise CollectorConfigError("产品分片不能包含无区域后缀的英文频道")
    return shards


def load_presence_snapshot_policy(
    path: str | Path,
) -> PresenceSnapshotPolicy:
    document = _read_object(Path(path))
    raw = document.get("presence_snapshot", {})
    if not isinstance(raw, dict):
        raise CollectorConfigError("presence_snapshot 必须是对象")

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise CollectorConfigError("presence_snapshot.enabled 必须是布尔值")
    manual_enabled = raw.get("manual_enabled", True)
    if not isinstance(manual_enabled, bool):
        raise CollectorConfigError("presence_snapshot.manual_enabled 必须是布尔值")

    def number(
        key: str, default: float, *, minimum: float, maximum: float,
    ) -> float:
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CollectorConfigError(f"presence_snapshot.{key} 必须是数字")
        result = float(value)
        if not minimum <= result <= maximum:
            raise CollectorConfigError(
                f"presence_snapshot.{key} 必须在 {minimum} 到 {maximum} 之间"
            )
        return result

    def integer(
        key: str, default: int, *, minimum: int, maximum: int,
    ) -> int:
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise CollectorConfigError(f"presence_snapshot.{key} 必须是整数")
        if not minimum <= value <= maximum:
            raise CollectorConfigError(
                f"presence_snapshot.{key} 必须在 {minimum} 到 {maximum} 之间"
            )
        return value

    return PresenceSnapshotPolicy(
        enabled=enabled,
        manual_enabled=manual_enabled,
        initial_delay_seconds=number(
            "initial_delay_seconds", 5.0, minimum=0.0, maximum=300.0,
        ),
        request_gap_seconds=number(
            "request_gap_seconds", 4.0, minimum=0.1, maximum=60.0,
        ),
        timeout_seconds=number(
            "timeout_seconds", 30.0, minimum=5.0, maximum=300.0,
        ),
        max_record_bytes=integer(
            "max_record_bytes", 768 * 1024,
            minimum=64 * 1024, maximum=900 * 1024,
        ),
    )


def load_reconnect_policy(path: str | Path) -> ReconnectPolicy:
    document = _read_object(Path(path))
    raw = document.get("reconnect", {})
    if not isinstance(raw, dict):
        raise CollectorConfigError("reconnect 必须是对象")

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise CollectorConfigError("reconnect.enabled 必须是布尔值")

    raw_backoff = raw.get("backoff_seconds", [15, 30, 60, 120, 300])
    if not isinstance(raw_backoff, list) or not 1 <= len(raw_backoff) <= 10:
        raise CollectorConfigError("reconnect.backoff_seconds 必须含 1 到 10 个数字")
    backoff: list[float] = []
    for value in raw_backoff:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CollectorConfigError("reconnect.backoff_seconds 必须全部是数字")
        delay = float(value)
        if not 0.1 <= delay <= 900.0:
            raise CollectorConfigError(
                "reconnect.backoff_seconds 必须全部在 0.1 到 900 秒之间"
            )
        backoff.append(delay)

    max_attempts = raw.get("max_attempts", 12)
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or not 1 <= max_attempts <= 100
    ):
        raise CollectorConfigError("reconnect.max_attempts 必须是 1 到 100 的整数")

    reuse_ttl_hours = raw.get("reuse_ttl_hours", 6)
    if (
        isinstance(reuse_ttl_hours, bool)
        or not isinstance(reuse_ttl_hours, (int, float))
        or not 0.1 <= float(reuse_ttl_hours) <= 168.0
    ):
        raise CollectorConfigError(
            "reconnect.reuse_ttl_hours 必须在 0.1 到 168 小时之间"
        )
    return ReconnectPolicy(
        enabled=enabled,
        backoff_seconds=tuple(backoff),
        max_attempts=max_attempts,
        reuse_ttl_hours=float(reuse_ttl_hours),
    )


def load_accounts(
    path: str | Path,
    *,
    topology: CollectorTopology = FOUR_SLOT_TOPOLOGY,
) -> dict[str, str]:
    """读取 ``{"A": {"nick": "..."}}`` 或 ``{"A": "..."}``。"""
    document = _read_object(Path(path))
    accounts: dict[str, str] = {}
    for slot in topology.slots:
        raw = document.get(slot)
        nick = raw.get("nick") if isinstance(raw, dict) else raw
        nick = str(nick or "").strip()
        if not nick:
            raise CollectorConfigError(f"账号配置缺少槽 {slot} 的 nick")
        if any(character.isspace() for character in nick):
            raise CollectorConfigError(f"槽 {slot} 的 nick 不能含空白字符")
        accounts[slot] = nick
    if (
        topology.mode == SEVENTEEN_SLOT_MODE
        and len({nick.casefold() for nick in accounts.values()}) != len(accounts)
    ):
        raise CollectorConfigError("17 槽模式必须使用 17 个不同账号")
    return accounts


def validate_psk(path: str | Path) -> Path:
    source = Path(path)
    try:
        size = source.stat().st_size
    except OSError as error:
        raise CollectorConfigError(f"PSK 不可用: {source}") from error
    if size < 64:
        raise CollectorConfigError(f"PSK 长度异常（{size} 字节）: {source}")
    return source
