"""Warframe IRC 昵称中的平台身份标记。"""

from __future__ import annotations

import re
import unicodedata


PLATFORM_UNKNOWN = "unknown"
PLATFORM_ORDER = (
    "windows",
    "xbox",
    "playstation",
    "switch",
    "ios",
    "android",
    "switch2",
    PLATFORM_UNKNOWN,
)

PLATFORM_BY_PRIVATE_CHAR = {
    "\ue000": "windows",
    "\ue001": "xbox",
    "\ue002": "playstation",
    "\ue003": "switch",
    "\ue004": "ios",
    "\ue005": "android",
    "\ue006": "switch2",
}

_PRIVATE_PLATFORM_RE = re.compile("[\ue000-\ue006]")
_IRC_IDENTITY_RE = re.compile(
    r"![0-9a-fA-F]{24}_([^@\s]*)@",
)
_PLATFORM_BY_IRC_FAMILY = {
    "0": "windows",
    "1": "xbox",
    "2": "playstation",
    "3": "switch",
    "4": "ios",
    "5": "android",
}
_IRC_PREFIX_REQUIRED = frozenset("0123456789-")


def normalize_player_nick(value: object) -> str:
    """统一玩家昵称中视觉等价的 Unicode 空格分隔符。"""
    return "".join(
        " " if unicodedata.category(character) == "Zs" else character
        for character in str(value or "")
    ).strip()


def player_nick_lookup_variants(value: object) -> tuple[str, ...]:
    """返回规范昵称及 IRC 历史库使用过的不换行空格形态。"""
    raw = str(value or "").strip()
    canonical = normalize_player_nick(raw)
    variants = (canonical, raw, canonical.replace(" ", "\u00a0"))
    return tuple(dict.fromkeys(item for item in variants if item))


def game_nick_to_irc(nick: str) -> str:
    """把游戏昵称编码为 Warframe IRC 使用的 wire nickname。"""
    wire = (
        normalize_player_nick(nick)
        .replace(" ", "\u00a0")
        .replace(".", "|")
    )
    if wire and wire[0] in _IRC_PREFIX_REQUIRED:
        wire = "`" + wire
    return wire


def irc_nick_to_game(nick: str) -> str:
    """把 Warframe IRC wire nickname 还原为游戏昵称。"""
    if len(nick) >= 2 and nick[0] == "`" and nick[1] in _IRC_PREFIX_REQUIRED:
        nick = nick[1:]
    return normalize_player_nick(nick.replace("|", "."))


def normalize_platform(value: object) -> str:
    platform = str(value or "").strip().casefold()
    return platform if platform in PLATFORM_ORDER else PLATFORM_UNKNOWN


def platform_name(platform: object, locale: str = "zh") -> str:
    """返回游戏账号平台的用户可见名称。"""
    names = {
        "windows": "PC",
        "xbox": "Xbox",
        "playstation": "PlayStation",
        "switch": "Nintendo Switch",
        "ios": "iOS",
        "android": "Android",
        "switch2": "Nintendo Switch 2",
        PLATFORM_UNKNOWN: "Unknown platform" if locale == "en" else "未识别平台",
    }
    return names[normalize_platform(platform)]


def split_platform_nick(value: object) -> tuple[str, str]:
    """返回移除平台字形后的昵称及字形所表示的平台。"""
    nick = str(value or "").strip()
    platform = next(
        (PLATFORM_BY_PRIVATE_CHAR[character] for character in reversed(nick)
         if character in PLATFORM_BY_PRIVATE_CHAR),
        PLATFORM_UNKNOWN,
    )
    return normalize_player_nick(_PRIVATE_PLATFORM_RE.sub("", nick)), platform


def platform_from_irc_raw(raw: object) -> str:
    """从 IRC hostmask 的平台家族字段中提取平台。"""
    match = _IRC_IDENTITY_RE.search(str(raw or ""))
    if match is None:
        return PLATFORM_UNKNOWN
    marker = match.group(1).upper()
    if marker.startswith("3X"):
        return "switch2"
    return _PLATFORM_BY_IRC_FAMILY.get(marker[:1], PLATFORM_UNKNOWN)


def resolve_player_identity(
    nick: object, *, explicit_platform: object = "", raw: object = "",
) -> tuple[str, str]:
    """优先使用昵称字形，其次使用显式字段及 IRC hostmask。"""
    clean_nick, glyph_platform = split_platform_nick(nick)
    clean_nick = irc_nick_to_game(clean_nick)
    if glyph_platform != PLATFORM_UNKNOWN:
        return clean_nick, glyph_platform
    explicit = normalize_platform(explicit_platform)
    if explicit != PLATFORM_UNKNOWN:
        return clean_nick, explicit
    return clean_nick, platform_from_irc_raw(raw)


__all__ = [
    "PLATFORM_BY_PRIVATE_CHAR",
    "PLATFORM_ORDER",
    "PLATFORM_UNKNOWN",
    "game_nick_to_irc",
    "irc_nick_to_game",
    "normalize_player_nick",
    "normalize_platform",
    "player_nick_lookup_variants",
    "platform_name",
    "platform_from_irc_raw",
    "resolve_player_identity",
    "split_platform_nick",
]
