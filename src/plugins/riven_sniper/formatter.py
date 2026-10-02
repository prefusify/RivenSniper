"""推送消息渲染（文本 + 命中紫卡图）。"""

from __future__ import annotations

import base64
from urllib.parse import quote

from nonebot import logger

from . import bargain, discord_rich, hitcard, marketdata, rivendata, texts
from .channel_locale import unrolled_label
from .command_meta import command_example_name
from .criteria import ANY_ATTRIBUTE, normalized_config
from .game_commands import (
    seller_invite_command,
    seller_shortcut_commands,
    seller_shortcuts,
)
from .grading import (
    GradeResult,
    StatGrade,
    format_faction_multiplier,
    grade_auction_item,
    is_faction_slug,
)

_STATUS_ZH = {"ingame": "游戏中", "online": "在线", "offline": "离线",
              "invisible": "隐身"}
_STATUS_EN = {"ingame": "in game", "online": "online", "offline": "offline",
              "invisible": "invisible"}
# 极性/武器种类中文名统一取自 rivendata 的权威映射，避免与词库编辑口径漂移
_POLARITY_ZH = rivendata.POLARITY_CATEGORY_ZH


def _format_group(
    group: list[str], ratings: dict[str, str], locale: str,
) -> str:
    return "/".join(
        ("any" if slug == ANY_ATTRIBUTE
         else rivendata.attribute_short_name(slug))
        + (f"@{ratings[slug]}" if slug in ratings else "")
        for slug in group
    )


def format_config_command(config: dict, locale: str | None = None) -> str:
    """生成可直接复制回群内执行的完整命令。"""
    normalized = normalized_config(config)
    locale = locale or texts.current_locale()
    if normalized["weapon"]:
        scope = rivendata.weapon_short_name(normalized["weapon"])
    else:
        scope = rivendata.wildcard_short_name(normalized["wildcard"] or "all")

    command = command_example_name("sniper.add")
    if command is None:
        return texts.render("通用.缺少英文简写", locale=locale)
    parts = [command, scope]
    positive_ratings = normalized["positive_ratings"]
    negative_ratings = normalized["negative_ratings"]
    parts.extend(
        _format_group(
            group,
            positive_ratings[index] if index < len(positive_ratings) else {},
            locale,
        )
        for index, group in enumerate(normalized["positives"])
    )
    parts.extend(
        "-" + _format_group(
            group,
            negative_ratings[index] if index < len(negative_ratings) else {},
            locale,
        )
        for index, group in enumerate(normalized["negatives"])
    )
    if normalized["zero_rerolls"]:
        parts.append("unrolled")
    return " ".join(parts)


def describe_config(config: dict, locale: str | None = None) -> str:
    """配置编号/状态与当前命令分行，保证命令整行可直接复制执行。"""
    number = config.get("display_number")
    locale = locale or texts.current_locale()
    labels = [f"Rule {number}" if locale == "en" else f"编号 {number}"] \
        if number is not None else []
    if not config.get("enabled", 1):
        labels.append("disabled" if locale == "en" else "已停用")
    command = format_config_command(config, locale)
    return f"{' '.join(labels)}\n{command}" if labels else command


_NON_PERCENT_SLUGS = {
    "combo_duration", "range", "channeling_damage", "punch_through",
    "damage_vs_corpus", "damage_vs_grineer", "damage_vs_infested",
}


def _format_stat(g: StatGrade, locale: str) -> str:
    name = rivendata.attribute_name(g.slug, locale)
    faction = is_faction_slug(g.slug)
    # 负词条数值为正时（如 +后坐力）显式标注，避免误读为正词条
    if not faction and not g.positive and g.value >= 0:
        name += " (negative)" if locale == "en" else "(负)"
    if faction:
        value = format_faction_multiplier(g.value)
    else:
        sign = "+" if g.value >= 0 else ""
        unit = "" if g.slug in _NON_PERCENT_SLUGS else "%"
        value = f"{sign}{g.value}{unit}"
    line = f"  {value} {name}"
    if g.roll is not None:
        dev = g.deviation
        limit = "，数值超限" if g.grade == "X" else ""
        if locale == "en":
            limit = ", value out of range" if g.grade == "X" else ""
            line += f"  Grade: {g.grade}, deviation {'+' if dev >= 0 else ''}{dev}%{limit}"
        else:
            line += f"  评分：{g.grade}，偏差 {'+' if dev >= 0 else ''}{dev}%{limit}"
        if g.min_display is not None:
            lo = (format_faction_multiplier(g.min_display)
                  if faction else str(g.min_display))
            hi = (format_faction_multiplier(g.max_display)
                  if faction else str(g.max_display))
            line += ((f", reference range {lo} to {hi}") if locale == "en"
                     else f"，参考区间 {lo} 至 {hi}")
    elif g.grade == "X":
        line += "  Grade: out of range" if locale == "en" else "  评分：数值超限"
    return line


def _price_line(auction: dict, locale: str) -> str:
    buyout = auction.get("buyout_price")
    starting = auction.get("starting_price")
    if auction.get("is_direct_sell"):
        return f"Buyout {buyout}p" if locale == "en" else f"一口价 {buyout}p"
    elif buyout:
        return ((f"Starting {starting}p / buyout {buyout}p")
                if locale == "en"
                else f"起拍 {starting}p / 一口价 {buyout}p")
    return ((f"Starting {starting}p (no buyout)") if locale == "en"
            else f"起拍 {starting}p（无一口价）")


def _seller_status(owner: dict, locale: str) -> str:
    statuses = _STATUS_EN if locale == "en" else _STATUS_ZH
    return statuses.get(owner.get("status", ""), owner.get("status", "?"))


def _wm_whisper(auction: dict, locale: str) -> str:
    item = auction["item"]
    seller = (auction.get("owner") or {}).get("ingame_name", "?")
    weapon_en = (rivendata.weapons().get(item.get("weapon_url_name", "")) or {}) \
        .get("name_en") or item.get("weapon_url_name", "")
    riven = (item.get("name") or "").capitalize()
    price = auction.get("buyout_price") or auction.get("starting_price")
    return texts.render(
        "狙击推送.私聊", locale=locale, seller=seller,
        weapon_en=weapon_en, riven=riven, price=price,
    )


def _wm_whisper_command(auction: dict, locale: str) -> str:
    """Rich Embed 已有字段标题，只保留可复制的 /w 私聊短语。"""
    return _wm_whisper(auction, locale).split("\n", 1)[-1]


def _discord_link_label(text: str) -> str:
    """转义链接结构字符，但保持游戏昵称中的下划线原样。"""
    return "".join(
        f"\\{character}" if character in "\\`*[]~" else character
        for character in text
    )


def _hit_tail_lines(auction: dict, result: GradeResult, locale: str) -> list[str]:
    """命中推送中不进卡图的部分：价格/卖家/链接/评分口径说明/私聊文案。"""
    owner = auction.get("owner") or {}
    seller = owner.get("ingame_name", "?")
    status = _seller_status(owner, locale)
    price_line = _price_line(auction, locale)

    # 不再附听单链接：可点击 URL 会触发 QQ 风控；买家改用下方 /w 私聊话术
    lines = [
        texts.render("狙击推送.价格行", locale=locale, price_line=price_line,
                     seller=seller, status=status),
    ]
    notes = []
    if result.variant:
        notes.append((f"graded for {result.variant}") if locale == "en"
                     else f"按 {result.variant} 倾向评分")
    if result.assumed_rank == 8:
        notes.append("values fitted and graded at max rank"
                     if locale == "en" else "数值按满级拟合评分")
    elif result.assumed_rank == 0:
        notes.append("values fitted and graded at rank 0"
                     if locale == "en" else "数值按未升级（0 级）拟合评分")
    if notes:
        lines.append(("Grade note: " + "; ".join(notes)) if locale == "en"
                     else "评分说明：" + "；".join(notes))
    if any(g.grade == "X" for g in result.stats):
        lines.append("Value note: a value is outside the possible range and may be a seller entry error."
                     if locale == "en" else
                     "数值说明：当前数值超出该卡的可能区间，多为卖家填写错误，仅供参考")
    lines.append(_wm_whisper(auction, locale))
    lines.append(seller_shortcuts(seller))
    return lines


def _header_config(config: dict, extra_count: int = 0, locale: str = "zh") -> str:
    """命中标题里的配置描述；同挂单多配置命中时标注"等N条"。"""
    desc = describe_config(config, locale)
    if extra_count <= 0:
        return desc
    lines = desc.splitlines()
    if len(lines) == 1:
        return ((f"({extra_count} additional matching rules)\n{lines[0]}")
                if locale == "en" else f"（另命中 {extra_count} 条配置）\n{lines[0]}")
    lines[0] += ((f" ({extra_count} additional matches)") if locale == "en"
                 else f"（另命中 {extra_count} 条配置）")
    return "\n".join(lines)


def format_hit(config: dict, auction: dict,
               result: GradeResult | None = None, extra_count: int = 0,
               locale: str = "zh") -> str:
    """命中通知纯文本（卡图渲染失败的回退形态 / dry-run 日志）。"""
    item = auction["item"]
    weapon = rivendata.weapon_name(item.get("weapon_url_name", "?"), locale)
    riven_name = item.get("name", "")
    if result is None:
        result = grade_auction_item(item)
    stat_lines = "\n".join(_format_stat(g, locale) for g in result.stats)

    lines = [
        texts.render("狙击推送.标题", locale=locale,
                     config=_header_config(config, extra_count, locale)),
        f"{weapon} {riven_name}",
        ((f"Rerolls: {item.get('re_rolls', 0)}") if locale == "en" else f"洗练次数：{item.get('re_rolls', 0)}"),
        ((f"Mastery: {item.get('mastery_level', '?')}") if locale == "en" else f"段位要求：{item.get('mastery_level', '?')}"),
        ((f"Polarity: {item.get('polarity', '?')}") if locale == "en" else f"极性：{_POLARITY_ZH.get(item.get('polarity', ''), item.get('polarity', '?'))}"),
        ((f"Rank: {item.get('mod_rank', 0)}") if locale == "en" else f"卡片等级：{item.get('mod_rank', 0)}"),
        stat_lines,
    ]
    return "\n".join(lines + _hit_tail_lines(auction, result, locale))


def build_hit_push(config: dict, auction: dict, extra_count: int = 0,
                   locale: str = "zh", result: GradeResult | None = None):
    """命中推送消息：紫卡图 + 文字（价格/卖家等，无链接）。

    返回 (message, log_text)：message 为 OneBot Message（图片渲染失败时
    回退为纯文本），log_text 供 dry-run 日志使用。
    """
    from nonebot.adapters.onebot.v11 import Message, MessageSegment

    item = auction["item"]
    if result is None:
        result = grade_auction_item(item)
    full_text = format_hit(
        config, auction, result, extra_count=extra_count, locale=locale)
    try:
        card = hitcard.render_hit_card(item, result, auction, locale=locale)
    except Exception as e:
        logger.warning("命中卡图渲染失败，回退文字推送: {}", e)
        return Message(full_text), full_text + "\n[卡图渲染失败，已回退文字]"

    header = texts.render("狙击推送.标题", locale=locale,
                          config=_header_config(config, extra_count, locale))
    tail = "\n".join(_hit_tail_lines(auction, result, locale))
    message = (MessageSegment.text(header + "\n")
               + MessageSegment.image("base64://"
                                      + base64.b64encode(card).decode())
               + MessageSegment.text("\n" + tail))
    return message, full_text + f"\n[已生成命中卡图 {len(card) // 1024}KB]"


def build_hit_qq_batch(entries: tuple[tuple, ...], locale: str = "zh"):
    """把同目标同轮的多个 WM 命中合并成一次 OneBot action。"""
    from nonebot.adapters.onebot.v11 import Message, MessageSegment

    if not entries:
        raise ValueError("QQ WM 合并消息不能为空")
    segments = []
    logs = []
    for index, entry in enumerate(entries):
        config, auction, extra_count = entry[:3]
        result = entry[3] if len(entry) > 3 else None
        message, log = build_hit_push(
            config, auction, extra_count, locale, result)
        if index:
            segments.append(MessageSegment.text("\n\n"))
        segments.extend(message)
        logs.append(log)
    return Message(segments), "\n\n".join(logs)


DISCORD_MESSAGE_MAX_EMBEDS = 10
DISCORD_MESSAGE_MAX_EMBED_CHARACTERS = 6000


def _build_hit_discord_embed(
        config: dict, auction: dict, extra_count: int = 0,
        locale: str = "zh", result: GradeResult | None = None):
    """构建单个 WM Embed，并返回对应的审计文本。"""
    from nonebot.adapters.discord.api.model import Embed, EmbedAuthor, EmbedField

    item = auction["item"]
    owner = auction.get("owner") or {}
    seller = str(owner.get("ingame_name") or "?")
    status = _seller_status(owner, locale)
    if result is None:
        result = grade_auction_item(item)

    weapon = rivendata.weapon_name(
        str(item.get("weapon_url_name") or "?"), locale,
    )
    riven_name = str(item.get("name") or "").capitalize()
    amount = auction.get("buyout_price") or auction.get("starting_price")
    card_title = f"{weapon} {riven_name}"
    card_price_title = f"{card_title} · {amount}p"

    auction_id = str(auction.get("id") or "")
    auction_url = (
        f"https://warframe.market/auction/{quote(auction_id, safe='')}"
        if auction_id else None
    )
    seller_url = (
        f"https://warframe.market/profile/{quote(seller, safe='')}"
        if seller != "?" else None
    )

    polarity = str(item.get("polarity") or "?")
    if locale == "zh":
        polarity = _POLARITY_ZH.get(polarity, polarity)
    block = discord_rich.riven_block_ansi(
        result.stats,
        locale,
        title=card_title,
        mastery_level=item.get("mastery_level", "?"),
        rank=item.get("mod_rank", 0),
        rolls=item.get("re_rolls", 0),
        polarity=polarity,
        right_align=True,
    )
    description = discord_rich.ansi_block(block)

    seller_value = (
        f"[{_discord_link_label(seller)}]({seller_url}) · {status}"
        if seller_url else f"`{seller}` · {status}"
    )
    fields = [
        EmbedField(
            name="Seller" if locale == "en" else "卖家",
            value=seller_value,
            inline=True,
        ),
        EmbedField(
            name="Price" if locale == "en" else "价格",
            value=_price_line(auction, locale),
            inline=True,
        ),
        EmbedField(
            name="\u200b",
            value="\n".join(
                discord_rich.plain_code_block(command)
                for command in (
                    _wm_whisper_command(auction, locale),
                    *seller_shortcut_commands(seller),
                )
            ),
            inline=False,
        ),
    ]
    embed_options = {
        "title": card_price_title,
        "url": auction_url,
        "description": description,
        "color": 0x8B5CF6,
        "fields": fields,
        "author": EmbedAuthor(name="WARFRAME.MARKET · RIVEN"),
    }
    embed = Embed(**embed_options)
    full_text = format_hit(
        config, auction, result, extra_count=extra_count, locale=locale,
    )
    return embed, full_text + "\n[Discord Rich Embed]"


def discord_embed_character_count(embed) -> int:
    """按 Discord 的 6000 字符规则计算单个 Embed 的计费字符。"""
    data = embed.model_dump(exclude_none=True, exclude_unset=True)
    total = len(str(data.get("title") or ""))
    total += len(str(data.get("description") or ""))
    total += sum(
        len(str(field.get("name") or ""))
        + len(str(field.get("value") or ""))
        for field in data.get("fields") or []
    )
    total += len(str((data.get("footer") or {}).get("text") or ""))
    total += len(str((data.get("author") or {}).get("name") or ""))
    return total


def hit_discord_embed_character_count(
        config: dict, auction: dict, extra_count: int = 0,
        locale: str = "zh", result: GradeResult | None = None) -> int:
    embed, _ = _build_hit_discord_embed(
        config, auction, extra_count, locale, result)
    return discord_embed_character_count(embed)


def build_hit_discord_rich_batch(
        entries: tuple[tuple, ...], locale: str = "zh"):
    """把同目标同轮的多个 WM 命中合并成一条 Discord 消息。"""
    from nonebot.adapters.discord import Message, MessageSegment

    if not entries:
        raise ValueError("Discord WM 合并消息不能为空")
    embeds = []
    logs = []
    for entry in entries:
        config, auction, extra_count = entry[:3]
        result = entry[3] if len(entry) > 3 else None
        embed, log = _build_hit_discord_embed(
            config, auction, extra_count, locale, result)
        embeds.append(embed)
        logs.append(log)
    characters = sum(map(discord_embed_character_count, embeds))
    if len(embeds) > DISCORD_MESSAGE_MAX_EMBEDS:
        raise ValueError("Discord WM 合并消息超过 10 个 Embed")
    if characters > DISCORD_MESSAGE_MAX_EMBED_CHARACTERS:
        raise ValueError("Discord WM 合并消息超过 6000 个 Embed 字符")
    message = Message([MessageSegment.embed(embed) for embed in embeds])
    return message, "\n\n".join(logs)


def build_hit_discord_rich(config: dict, auction: dict, extra_count: int = 0,
                           locale: str = "zh",
                           result: GradeResult | None = None):
    """构建 Discord WM 来源紫卡 Rich Embed。"""
    return build_hit_discord_rich_batch(
        ((config, auction, extra_count, result),), locale)


def _build_bargain_discord_message(
        *, title: str, seller: str, status: str, price_line: str,
        whisper: str, hit: bargain.Hit, locale: str, author: str,
        baseline_label: str,
        url: str | None = None):
    """构建普通道具与紫卡捡漏共用的红色 Discord Embed。"""
    from nonebot.adapters.discord import Message, MessageSegment
    from nonebot.adapters.discord.api.model import Embed, EmbedAuthor, EmbedField

    seller_url = (
        f"https://warframe.market/profile/{quote(seller, safe='')}"
        if seller != "?" else None
    )
    seller_value = (
        f"[{_discord_link_label(seller)}]({seller_url}) · {status}"
        if seller_url else f"`{seller}` · {status}"
    )
    discount_label = "Below reference" if locale == "en" else "低于参考价"
    summary = (
        f"{discount_label}: {round(float(hit.discount) * 100)}%\n"
        f"{baseline_label}: {bargain.display_price(hit.baseline)}p"
    )
    embed_options = {
        "title": title,
        "description": discord_rich.ansi_block(summary),
        "color": 0xED4245,
        "fields": [
            EmbedField(
                name="Seller" if locale == "en" else "卖家",
                value=seller_value,
                inline=True,
            ),
            EmbedField(
                name="Price" if locale == "en" else "价格",
                value=price_line,
                inline=True,
            ),
            EmbedField(
                name="\u200b",
                value="\n".join((
                    discord_rich.plain_code_block(whisper),
                    discord_rich.plain_code_block(
                        seller_invite_command(seller)),
                )),
                inline=False,
            ),
        ],
        "author": EmbedAuthor(name=author),
    }
    if url is not None:
        embed_options["url"] = url
    embed = Embed(**embed_options)
    return Message([MessageSegment.embed(embed)])


def _item_whisper_command(slug: str, order: dict) -> str:
    _, name_en = marketdata.item_names(slug)
    rank = order.get("rank")
    max_rank = marketdata.item_max_rank(slug)
    item = ((name_en or slug) +
            (f" (rank {rank})"
             if max_rank > 0 and rank is not None else ""))
    seller = str((order.get("user") or {}).get("ingameName") or "?")
    price = bargain.format_price(order.get("platinum"))
    return (f'/w {seller} Hi! I want to buy: "{item}" for {price} '
            "platinum. (warframe.market)")


def build_bargain_item_discord_rich(
        slug: str, order: dict, hit: bargain.Hit, locale: str = "zh"):
    """构建无挂单超链接的 Discord 普通道具捡漏 Rich Embed。"""
    user = order.get("user") or {}
    seller = str(user.get("ingameName") or "?")
    statuses = _STATUS_EN if locale == "en" else _STATUS_ZH
    status = statuses.get(
        user.get("status"),
        user.get("status") or ("Unknown" if locale == "en" else "未知状态"),
    )
    max_rank = marketdata.item_max_rank(slug)
    bucket = bargain.plain_bucket_display(
        bargain.item_bucket_of(order, max_rank), locale)
    price = bargain.format_price(order.get("platinum"))
    quantity = order.get("quantity") or 1
    quantity_label = "Quantity" if locale == "en" else "数量"
    message = _build_bargain_discord_message(
        title=f"{marketdata.item_name(slug, locale)}{bucket} · {price}p",
        seller=seller,
        status=status,
        price_line=f"{price}p · {quantity_label} {quantity}",
        whisper=_item_whisper_command(slug, order),
        hit=hit,
        locale=locale,
        author="WARFRAME.MARKET · ITEM",
        baseline_label="Reference daily average" if locale == "en" else "参考日均价",
    )
    log = bargain.build_item_push_text(slug, order, hit, locale=locale)
    return message, log + "\n[Discord Rich Embed]"


def build_bargain_riven_discord_rich(
        weapon_slug: str, auction: dict, hit: bargain.Hit,
        locale: str = "zh"):
    """构建 Discord 紫卡捡漏 Rich Embed，不展示紫卡词条。"""
    item = auction.get("item") or {}
    owner = auction.get("owner") or {}
    seller = str(owner.get("ingame_name") or "?")
    status = _seller_status(owner, locale)
    weapon = rivendata.weapon_name(weapon_slug, locale)
    riven_name = str(item.get("name") or "").capitalize()
    card_title = f"{weapon} {riven_name}".strip()
    amount = auction.get("buyout_price") or auction.get("starting_price")
    unrolled_suffix = (
        f" · {unrolled_label(locale)}"
        if int(item.get("re_rolls") or 0) == 0 else ""
    )

    auction_id = str(auction.get("id") or "")
    auction_url = (
        f"https://warframe.market/auction/{quote(auction_id, safe='')}"
        if auction_id else None
    )
    message = _build_bargain_discord_message(
        title=f"{card_title} · {amount}p{unrolled_suffix}",
        url=auction_url,
        seller=seller,
        status=status,
        price_line=_price_line(auction, locale),
        whisper=_wm_whisper_command(auction, locale),
        hit=hit,
        locale=locale,
        author="WARFRAME.MARKET · RIVEN",
        baseline_label="Rolling low-price average" if locale == "en" else "低价样本均价",
    )
    log = bargain.build_riven_push_text(
        weapon_slug, auction, hit, locale=locale)
    return message, log + "\n[Discord Rich Embed]"
