"""游戏公开频道的规范标识、双语来源名与私聊语言路由。"""

from __future__ import annotations

import re
from dataclasses import dataclass


IN_GAME_SWITCHABLE_REGIONS = frozenset({
    "EN_NA", "EN_EU", "EN_AS", "EN_SA", "EN_RU",
})
_REGION_SUFFIX_PATTERN = "|".join(sorted(
    key.removeprefix("EN_") for key in IN_GAME_SWITCHABLE_REGIONS
))
_CHANNEL_RE = re.compile(
    r"^#?(?P<kind>[GQRT])_(?P<language>ZH|EN|FR|DE|ES|PT|RU|JA|KO|TC|IT|PL|UK)"
    rf"(?:_(?P<region>{_REGION_SUFFIX_PATTERN}))?$",
    re.IGNORECASE,
)

_KIND_ZH = {"G": "区域", "Q": "问答", "R": "招募", "T": "交易"}
_KIND_EN = {"G": "Region", "Q": "Q&A", "R": "Recruit", "T": "Trade"}
_LANGUAGE_ZH = {
    "ZH": "简体中文", "EN": "英语", "FR": "法语", "DE": "德语",
    "ES": "西语", "PT": "葡语", "RU": "俄语", "JA": "日语",
    "KO": "韩语", "TC": "繁体中文", "IT": "意大利语", "PL": "波兰语",
    "UK": "乌克兰语",
}
_REGION_ZH = {
    "NA": "北美", "EU": "欧洲", "SA": "南美", "RU": "俄区英文",
    "AS": "亚洲",
}


@dataclass(frozen=True, slots=True)
class ChannelIdentity:
    kind: str
    language: str
    region: str | None = None


def parse_channel(channel: str | None) -> ChannelIdentity | None:
    value = str(channel or "").strip()
    match = _CHANNEL_RE.fullmatch(value)
    if match is None:
        return None
    kind = match.group("kind").upper()
    language = match.group("language").upper()
    region = match.group("region")
    region = region.upper() if region else None
    # 只有这五个英文地区可在游戏内直接切换；其他语言/地区组合无效。
    if region and f"{language}_{region}" not in IN_GAME_SWITCHABLE_REGIONS:
        return None
    return ChannelIdentity(kind, language, region)


def presence_region_key(channel: str | None) -> str | None:
    """返回忽略 G/Q/R/T 类型的在线提醒地区键。"""
    identity = parse_channel(channel)
    if identity is None:
        return None
    if identity.region:
        return f"{identity.language}_{identity.region}"
    return identity.language


def presence_region_label(channel: str | None, locale: str = "zh") -> str:
    """返回不含 G/Q/R/T 类型的上线提醒地区标签。"""
    identity = parse_channel(channel)
    if identity is None:
        raw = str(channel or "?").strip().lstrip("#").replace("_", "-") or "?"
        return f"[{raw}]"
    if locale == "en":
        return f"[{identity.region or identity.language}]"
    suffix = (_REGION_ZH[identity.region] if identity.region
              else _LANGUAGE_ZH[identity.language])
    return f"[{suffix}]"


def source_label(channel: str | None, locale: str = "zh") -> str:
    """返回带方括号的用户可见来源，例如 ``[交易-北美]``。"""
    identity = parse_channel(channel)
    if identity is None:
        raw = str(channel or "?").strip().lstrip("#").replace("_", "-") or "?"
        return f"[{raw}]"
    if locale == "en":
        suffix = identity.region or identity.language
        return f"[{_KIND_EN[identity.kind]}-{suffix}]"
    suffix = (_REGION_ZH[identity.region] if identity.region
              else _LANGUAGE_ZH[identity.language])
    return f"[{_KIND_ZH[identity.kind]}-{suffix}]"


_WHISPER_PHRASES = {
    "EN": "wtb {weapon} riven",
    "ZH": "收{weapon}紫卡",
    "TC": "收{weapon}紫卡",
    "FR": "achète riven {weapon}",
    "DE": "suche {weapon} Riven",
    "ES": "compro riven de {weapon}",
    "PT": "compro riven de {weapon}",
    "RU": "куплю ривен на {weapon}",
    "RU_LATIN": "kuplyu riven na {weapon}",
    "IT": "cerco riven per {weapon}",
    "PL": "kupię riven {weapon}",
}


def whisper_language(channel: str | None, target_locale: str) -> str:
    """按产品规则选择频道私聊固定文案使用的语言。"""
    identity = parse_channel(channel)
    if identity is None or identity.region:
        return "EN"
    language = identity.language
    if language == "UK":
        return "EN"
    if target_locale == "zh":
        if language == "RU":
            return "RU_LATIN"
        if language in {"JA", "KO", "PL"}:
            return "EN"
    if target_locale == "en" and language in {"JA", "KO", "ZH", "TC"}:
        return "EN"
    return language if language in _WHISPER_PHRASES else "EN"


def channel_whisper(
    seller: str, weapon_name: str, channel: str | None, target_locale: str,
) -> str:
    language = whisper_language(channel, target_locale)
    phrase = _WHISPER_PHRASES[language].format(weapon=weapon_name)
    return f'/w "{seller}" {phrase}'


def unrolled_label(locale: str) -> str:
    return "Unrolled" if locale == "en" else "0洗"


__all__ = [
    "ChannelIdentity", "IN_GAME_SWITCHABLE_REGIONS", "channel_whisper",
    "parse_channel", "presence_region_key", "presence_region_label",
    "source_label", "unrolled_label", "whisper_language",
]
