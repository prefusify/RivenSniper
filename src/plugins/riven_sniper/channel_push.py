"""将频道紫卡转换为各平台消息。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

from . import discord_rich, riven_link, rivendata
from .channel_locale import (
    channel_whisper,
    source_label,
    unrolled_label,
    whisper_language,
)
from .game_commands import seller_shortcut_commands, seller_shortcuts
from .grading import GradeResult, StatGrade
from .platform_identity import PLATFORM_UNKNOWN, platform_name
from .privacy import redact_hidden_identifiers


@dataclass(frozen=True, slots=True)
class ChannelCard:
    decoded: dict[str, Any]
    riven_no: int
    duplicate: bool = False
    card_index: int = 0


@dataclass(slots=True)
class ChannelPushPayload:
    seller: str
    channel: str
    cards: tuple[ChannelCard, ...]
    locale: str
    target_scope: int = 0
    raw_text: str = ""
    message_cards: tuple[ChannelCard, ...] = ()
    display_cards: tuple[ChannelCard, ...] = ()
    rendered: object | None = None
    seller_platform: str = PLATFORM_UNKNOWN
    seller_riven_count: int | None = None


_WM_PROFILE_BASE = "https://warframe.market/profile/"
_WM_RIVEN_SEARCH_BASE = "https://warframe.market/auctions/search"


def _item_and_grade(card: dict[str, Any]) -> tuple[dict[str, Any], GradeResult]:
    weapon_slug = str(card.get("weapon_slug") or "")
    if weapon_slug not in rivendata.weapons():
        raise ValueError("频道紫卡武器索引无法映射到数据库")
    grades: list[StatGrade] = []
    attributes: list[dict[str, Any]] = []
    for stat in card.get("stats") or []:
        slug = rivendata.attribute_slug_from_ref(str(stat.get("ref") or ""))
        if slug is None:
            raise ValueError(f"频道紫卡词条无法映射到数据库: {stat.get('ref')}")
        positive = not bool(stat.get("is_curse"))
        values = [
            float(value) for value in (stat.get("display_variants") or {}).values()
            if value is not None
        ]
        display = stat.get("display")
        value = float(display) if display is not None else (
            sum(values) / len(values) if values else 0.0)
        score_roll = (1.0 - float(stat["roll"])) if not positive else float(stat["roll"])
        grades.append(StatGrade(
            slug=slug,
            value=value,
            positive=positive,
            roll=score_roll,
            grade=str(stat.get("grade") or "?"),
        ))
        attributes.append({"url_name": slug, "value": value, "positive": positive})
    item = {
        "weapon_url_name": weapon_slug,
        "name": str(card.get("riven_name") or ""),
        "mod_rank": int(card.get("lvl") or 0),
        "mastery_level": int(card.get("lvl_req") or 0),
        "polarity": card.get("polarity"),
        "re_rolls": int(card.get("rerolls") or 0),
        "attributes": attributes,
    }
    return item, GradeResult(stats=sorted(grades, key=lambda grade: not grade.positive))


def _localized_card_name(card: dict[str, Any], locale: str) -> str:
    weapon = rivendata.weapon_name(str(card.get("weapon_slug") or ""), locale)
    riven_name = str(card.get("riven_name") or "").capitalize()
    return f"{weapon} {riven_name}".strip()


def _whisper_weapon_name(
    card: dict[str, Any], channel: str, target_locale: str,
) -> str:
    phrase_language = whisper_language(channel, target_locale)
    name_locale = "zh" if phrase_language in {"ZH", "TC"} else "en"
    return rivendata.weapon_name(str(card.get("weapon_slug") or ""), name_locale)


def card_heading(card: ChannelCard, locale: str) -> str:
    decoded = card.decoded
    weapon = rivendata.weapon_name(str(decoded.get("weapon_slug") or ""), locale)
    riven_name = str(decoded.get("riven_name") or "").capitalize()
    grades = " ".join(
        str(stat.get("grade") or "?") for stat in decoded.get("stats") or []
        if not stat.get("is_curse")
    )
    heading = f"#{card.riven_no} {weapon} {riven_name} ({grades})"
    if int(decoded.get("rerolls") or 0) == 0:
        heading += f" {unrolled_label(locale)}"
    return heading


def format_channel_push(payload: ChannelPushPayload) -> str:
    source = source_label(payload.channel, payload.locale)
    platform = platform_name(payload.seller_platform, payload.locale)
    seller = redact_hidden_identifiers(payload.seller)
    lines: list[str] = []
    for card in payload.cards:
        lines.append(card_heading(card, payload.locale))
        lines.append(f"{source} [{platform}] {seller}")
        lines.append(channel_whisper(
            seller,
            _whisper_weapon_name(card.decoded, payload.channel, payload.locale),
            payload.channel,
            payload.locale,
        ))
    if payload.cards:
        lines.append(seller_shortcuts(seller))
    return "\n".join(lines)


def build_channel_qq(payload: ChannelPushPayload):
    from nonebot.adapters.onebot.v11 import Message, MessageSegment

    text = format_channel_qq_text(payload)
    return Message(MessageSegment.text(text)), text


def format_channel_qq_text(payload: ChannelPushPayload) -> str:
    """尽量还原 Discord 频道文案，但不包含 QQ 不支持的格式或卡图。"""
    source = source_label(payload.channel, payload.locale)
    platform = platform_name(payload.seller_platform, payload.locale)
    seller = redact_hidden_identifiers(payload.seller)
    detailed_cards = tuple(card for card in payload.cards if not card.duplicate)

    sections = [
        "\n".join(
            _discord_card_summary(card, payload.locale)
            for card in detailed_cards
        ),
        (
            f"{source} [{platform}] {seller} : "
            f"{discord_rich.strip_ansi(_original_message_ansi(payload))}"
        ),
    ]
    sections.extend(
        discord_rich.strip_ansi(_discord_card_ansi(card, payload))
        for card in detailed_cards
    )
    sections.extend(
        channel_whisper(
            seller,
            _whisper_weapon_name(card.decoded, payload.channel, payload.locale),
            payload.channel,
            payload.locale,
        )
        for card in detailed_cards
    )
    if detailed_cards:
        sections.append(seller_shortcuts(seller))
    return "\n\n".join(section for section in sections if section)


def _discord_card_suffix(card: ChannelCard, locale: str) -> str:
    decoded = card.decoded
    grades = " ".join(
        str(stat.get("grade") or "?")
        for stat in decoded.get("stats") or []
        if not stat.get("is_curse")
    )
    suffix = f" ({grades})"
    curses = []
    for stat in decoded.get("stats") or []:
        if not stat.get("is_curse"):
            continue
        slug = rivendata.attribute_slug_from_ref(str(stat.get("ref") or ""))
        if slug:
            curses.append(rivendata.attribute_name(slug, locale))
    if curses:
        suffix += f" (-{'/'.join(curses)})"
    if int(decoded.get("rerolls") or 0) == 0:
        suffix += f" · {unrolled_label(locale)}"
    return suffix


def _discord_card_summary(card: ChannelCard, locale: str) -> str:
    name = _localized_card_name(card.decoded, locale)
    return f"#{card.riven_no} {name}{_discord_card_suffix(card, locale)}"


def _wm_profile_url(seller: str) -> str:
    return f"{_WM_PROFILE_BASE}{quote(seller, safe='')}"


def _discord_link_label(text: str) -> str:
    """转义链接结构字符，但保持游戏昵称中的下划线原样。"""
    return "".join(
        f"\\{character}" if character in "\\`*[]~" else character
        for character in text
    )


def _wm_riven_search_url(card: dict[str, Any]) -> str:
    positive_stats: list[str] = []
    negative_stats: list[str] = []
    for stat in card.get("stats") or []:
        slug = rivendata.attribute_slug_from_ref(str(stat.get("ref") or ""))
        if slug is None:
            raise ValueError(f"频道紫卡词条无法映射到数据库: {stat.get('ref')}")
        target = negative_stats if stat.get("is_curse") else positive_stats
        target.append(slug)
    params = [
        ("type", "riven"),
        ("weapon_url_name", str(card.get("weapon_slug") or "")),
        ("positive_stats", ",".join(positive_stats)),
    ]
    if negative_stats:
        params.append(("negative_stats", ",".join(negative_stats)))
    params.extend((
        ("buyout_policy", "direct"),
        ("sort_by", "price_asc"),
    ))
    return f"{_WM_RIVEN_SEARCH_BASE}?{urlencode(params)}"


def _seller_riven_count_label(count: int | None, locale: str) -> str:
    if locale == "en":
        return ("Historical Riven count unavailable" if count is None
                else f"{count} distinct Rivens recorded")
    return ("历史紫卡数量未知" if count is None
            else f"历史记录：{count} 张不同紫卡")


def _discord_linked_card_summary(card: ChannelCard, locale: str) -> str:
    name = _localized_card_name(card.decoded, locale)
    return (
        f"**#{card.riven_no} [{_discord_link_label(name)}]"
        f"({_wm_riven_search_url(card.decoded)})"
        f"{_discord_card_suffix(card, locale)}**"
    )


def _original_message_ansi(payload: ChannelPushPayload) -> str:
    cards = payload.message_cards or payload.cards
    by_index = {card.card_index: card for card in cards}
    highlighted_indexes = {
        card.card_index for card in payload.cards if not card.duplicate
    }
    parts: list[str] = []
    cursor = 0
    decoded_index = 0
    for match in riven_link.OMG_RE.finditer(payload.raw_text):
        parts.append(payload.raw_text[cursor:match.start()])
        decoded = riven_link.decode_link(match.group(1), match.group(2))
        card = None
        if decoded is not None:
            card = by_index.get(decoded_index)
            decoded_index += 1
        if card is None:
            parts.append("[Riven]" if payload.locale == "en" else "[紫卡]")
        else:
            name = _localized_card_name(card.decoded, payload.locale)
            rendered_name = f"[{name}]"
            if card.card_index in highlighted_indexes:
                rendered_name = (
                    f"{discord_rich.ANSI_BLUE}{rendered_name}"
                    f"{discord_rich.ANSI_RESET}"
                )
            parts.append(rendered_name)
        cursor = match.end()
    parts.append(payload.raw_text[cursor:])
    return "".join(parts)


def _discord_card_ansi(card: ChannelCard, payload: ChannelPushPayload) -> str:
    _, result = _item_and_grade(card.decoded)
    title = _localized_card_name(card.decoded, payload.locale)
    decoded = card.decoded
    polarity = str(
        decoded.get("polarity_mark") or decoded.get("polarity") or "?"
    )
    return discord_rich.riven_block_ansi(
        result.stats,
        payload.locale,
        title=title,
        mastery_level=int(decoded.get("lvl_req") or 0),
        rank=int(decoded.get("lvl") or 0),
        rolls=int(decoded.get("rerolls") or 0),
        polarity=polarity,
    )


def build_channel_discord_rich(payload: ChannelPushPayload):
    """构建 Discord 频道来源紫卡 Rich Embed。"""
    from nonebot.adapters.discord import Message, MessageSegment
    from nonebot.adapters.discord.api.model import Embed

    source = source_label(payload.channel, payload.locale)
    platform = platform_name(payload.seller_platform, payload.locale)
    seller = redact_hidden_identifiers(payload.seller)
    detailed_cards = tuple(card for card in payload.cards if not card.duplicate)

    seller_header = (
        f"**[{_discord_link_label(seller)}]"
        f"({_wm_profile_url(seller)}) · "
        f"{_seller_riven_count_label(payload.seller_riven_count, payload.locale)}**"
    )
    summaries = [
        _discord_linked_card_summary(card, payload.locale)
        for card in detailed_cards
    ]

    identity = f"{source} [{platform}] {seller}"
    original = (
        f"{discord_rich.ANSI_YELLOW}{identity}"
        f"{discord_rich.ANSI_RESET} : {_original_message_ansi(payload)}"
    )
    sections = [
        "\n".join((seller_header, *summaries)),
        discord_rich.ansi_block(original),
    ]

    sections.extend(
        discord_rich.ansi_block(_discord_card_ansi(card, payload))
        for card in detailed_cards
    )
    sections.extend(
        discord_rich.plain_code_block(channel_whisper(
            seller,
            _whisper_weapon_name(card.decoded, payload.channel, payload.locale),
            payload.channel,
            payload.locale,
        ))
        for card in detailed_cards
    )
    if detailed_cards:
        sections.extend(
            discord_rich.plain_code_block(command)
            for command in seller_shortcut_commands(seller)
        )

    embed = Embed(
        description="\n".join(section for section in sections if section),
        color=0x23A559,
    )
    message = Message([MessageSegment.embed(embed)])
    return message, format_channel_push(payload) + "\n[Discord Rich Embed]"


__all__ = [
    "ChannelCard", "ChannelPushPayload", "build_channel_discord_rich",
    "build_channel_qq", "card_heading",
    "format_channel_push", "format_channel_qq_text",
]
