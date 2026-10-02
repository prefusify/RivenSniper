"""紫卡归属查询的共享解析与展示格式。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import rivendata, riven_link
from .channel_locale import source_label
from .chat_tracking import TRACKING_PAGE_SIZE
from .platform_identity import PLATFORM_UNKNOWN, platform_name


@dataclass(frozen=True, slots=True)
class TrackingRequest:
    kind: str
    target: str | int
    page: int = 1
    export_all: bool = False


def parse_tracking_request(
    raw: str,
    *,
    kind: str | None = None,
    riven_tokens: set[str] | frozenset[str] | None = None,
) -> TrackingRequest | None:
    """解析玩家或紫卡查询及末尾的“页 N”“全部”浏览参数。"""
    value = str(raw or "").strip()
    if not value:
        return None
    export_all = False
    page = 1
    parts = value.rsplit(maxsplit=1)
    if len(parts) == 2 and parts[1].casefold() in {"全部", "all"}:
        value, export_all = parts[0].strip(), True
    else:
        match = re.search(
            r"\s+(?:页|page)\s*([0-9]+)\s*$", value, re.IGNORECASE,
        )
        if match:
            page = int(match.group(1))
            value = value[:match.start()].strip()
    if not value or page < 1:
        return None
    if kind == "riven":
        if not value.lstrip("#").isdigit():
            return None
        return TrackingRequest(
            "riven", int(value.lstrip("#")), page, export_all,
        )
    if kind == "player":
        return TrackingRequest("player", value, page, export_all)
    target_parts = value.split(maxsplit=1)
    tokens = riven_tokens if riven_tokens is not None else {"紫卡", "riven"}
    if target_parts[0].casefold() in {token.casefold() for token in tokens}:
        if len(target_parts) != 2 or not target_parts[1].lstrip("#").isdigit():
            return None
        return TrackingRequest(
            "riven", int(target_parts[1].lstrip("#")), page, export_all,
        )
    return TrackingRequest("player", value, page, export_all)


def format_tracking_time(timestamp: int) -> str:
    value = datetime.fromtimestamp(int(timestamp), timezone.utc).astimezone()
    offset = value.strftime("%z")
    return (f"{value.year}/{value.month}/{value.day} {value.hour}:{value.minute:02d} "
            f"UTC{offset[:3]}:{offset[3:]}")


def _stat_token(stat: dict[str, Any]) -> str:
    slug = rivendata.attribute_slug_from_ref(str(stat.get("tag") or ""))
    abbreviation = (rivendata.attribute_abbreviations().get(slug or "")
                    or slug or "?")
    roll = float(stat.get("roll") or 0.0)
    score_roll = 1.0 - roll if stat.get("is_curse") else roll
    deviation = -10.0 + 20.0 * score_roll
    return f"{abbreviation}{deviation:+.2f}%"


def format_stat_signature(record: dict[str, Any]) -> str:
    positive = [
        _stat_token(stat) for stat in record.get("stats") or []
        if not stat.get("is_curse")
    ]
    negative = [
        _stat_token(stat) for stat in record.get("stats") or []
        if stat.get("is_curse")
    ]
    text = " ".join(positive)
    if negative:
        text += "|" + " ".join(negative)
    return text


def format_riven_summary(record: dict[str, Any], locale: str = "zh") -> str:
    slug = riven_link.weapon_slug(
        str(record.get("category") or ""), int(record.get("weapon_index") or 0),
    )
    weapon = rivendata.weapon_name(slug or "", locale)
    name = riven_link.riven_name(
        str(record.get("category") or ""), list(record.get("stats") or []),
    ).capitalize()
    label = f"{weapon} {name}".strip()
    mr = int(record.get("lvl_req") or 0)
    return (
        f"#{int(record['riven_no'])} | [{label}] "
        f"({format_stat_signature(record)}) MR:{mr} "
        f"{format_tracking_time(int(record['first_seen']))}"
    )


def format_ownership(record: dict[str, Any], locale: str = "zh") -> str:
    timestamp = format_tracking_time(int(record["observed_at"]))
    holder = str(record["to_nick"])
    platform = platform_name(
        str(record.get("to_platform") or PLATFORM_UNKNOWN), locale,
    )
    source = source_label(str(record.get("channel") or "?"), locale)
    previous = record.get("from_nick")
    if previous:
        return f"{timestamp} | {previous} -> {holder} [{platform}] | {source}"
    label = "First recorded holder" if locale == "en" else "首次记录的持有者"
    return f"{timestamp} | {label}: {holder} [{platform}] | {source}"


def pagination_label(pagination: dict[str, Any], locale: str = "zh") -> str:
    total = int(pagination["total"])
    page = int(pagination["page"])
    pages = int(pagination["pages"])
    start = int(pagination["start"])
    end = int(pagination["end"])
    if locale == "en":
        return f"{start}-{end} of {total} | page {page}/{max(1, pages)}"
    return f"{start}-{end}/{total} | 第 {page}/{max(1, pages)} 页"


__all__ = [
    "TRACKING_PAGE_SIZE", "TrackingRequest", "format_ownership",
    "format_riven_summary", "format_stat_signature", "format_tracking_time",
    "pagination_label", "parse_tracking_request", "platform_name",
]
