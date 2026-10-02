"""Discord Rich Embed 与 ANSI 紫卡预览格式回归。"""

from __future__ import annotations

import re
import sys
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.plugins.riven_sniper import (
    bargain,
    channel_push as channel_push_module,
    discord_rich,
    riven_link,
)
from src.plugins.riven_sniper.channel_push import (
    ChannelCard,
    ChannelPushPayload,
    build_channel_discord_rich,
    build_channel_qq,
)
from src.plugins.riven_sniper.formatter import (
    build_bargain_item_discord_rich,
    build_bargain_riven_discord_rich,
    build_hit_discord_rich,
)
from src.plugins.riven_sniper.grading import StatGrade

VECTIS_LINK = (
    "[OMG-LotusRifleRandomModRare:"
    "9b51zSsL1EkVt78mCp1r4/fpQ+1jxghk]"
)
TORID_LINK = (
    "[OMG-LotusRifleRandomModRare:"
    "8T9NiKpb9TjuN70Tfedj9RLDSiibxgUk]"
)
INVALID_LINK = "[OMG-PlayerMeleeWeaponRandomModRare:OwAAEAAAAA==]"

NAMI_ITEM = {
    "weapon_url_name": "nami_solo",
    "name": "visi-loctitis",
    "type": "riven",
    "mod_rank": 8,
    "re_rolls": 38,
    "mastery_level": 14,
    "polarity": "vazarin",
    "attributes": [
        {
            "url_name": "base_damage_/_melee_damage",
            "value": 237.0,
            "positive": True,
        },
        {"url_name": "range", "value": 2.6, "positive": True},
        {"url_name": "critical_damage", "value": 109.4, "positive": True},
        {
            "url_name": "critical_chance_on_slide_attack",
            "value": -116.2,
            "positive": False,
        },
    ],
}

WM_AUCTION = {
    "id": "test-auction-1",
    "item": NAMI_ITEM,
    "owner": {"ingame_name": "TestSeller", "status": "ingame"},
    "starting_price": 100,
    "buyout_price": 150,
    "is_direct_sell": False,
}

ITEM_ORDER = {
    "id": "test-order-1",
    "type": "sell",
    "visible": True,
    "platinum": "45",
    "quantity": 2,
    "rank": 5,
    "user": {"ingameName": "TestSeller", "status": "ingame"},
}


def _decode(link: str) -> dict:
    match = riven_link.OMG_RE.search(link)
    return riven_link.decode_link(match.group(1), match.group(2))


def _embed(message):
    segments = list(message)
    assert len(segments) == 1
    assert segments[0].type == "embed"
    return segments[0].data["embed"]


def _without_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def test_score_ansi_uses_fixed_grade_color_scale():
    expected = {
        "F": 31,
        "C-": 33,
        "C": 33,
        "C+": 33,
        "B-": 36,
        "B": 36,
        "B+": 36,
        "A-": 32,
        "A": 32,
        "A+": 32,
        "S": 35,
    }

    assert {
        grade: discord_rich.score_ansi(0.0, grade)
        for grade in expected
    } == {
        grade: f"\x1b[1;{color}m" for grade, color in expected.items()
    }
    assert discord_rich.score_ansi(12.0, "X") == discord_rich.ANSI_RESET
    assert discord_rich.score_ansi(None, "?") == discord_rich.ANSI_RESET


def test_stat_rows_use_requested_order_and_left_alignment_without_header():
    stats = [
        StatGrade("base_damage_/_melee_damage", 237.0, True, 0.9, "A+"),
        StatGrade("range", 2.6, True, 0.5, "B"),
        StatGrade("critical_damage", -99.5, False, 0.1, "C-"),
    ]

    rendered = _without_ansi(discord_rich.stat_rows_ansi(stats, "zh"))
    lines = rendered.splitlines()

    assert len(lines) == 3
    assert [line[:5].rstrip() for line in lines] == ["A+", "B", "C-"]
    assert [line[5:15].rstrip() for line in lines] == [
        "+8.00%", "+0.00%", "-8.00%",
    ]
    assert [line[15:25].rstrip() for line in lines] == [
        "+237.0%", "+2.6", "-99.5%",
    ]
    assert "评分" not in rendered and "数值" not in rendered


def test_channel_rich_embed_shows_and_colors_only_current_hits():
    torid = _decode(TORID_LINK)
    vectis = _decode(VECTIS_LINK)
    payload = ChannelPushPayload(
        seller="Seller",
        channel="#T_ZH",
        cards=(ChannelCard(torid, 1, card_index=0),),
        locale="zh",
        seller_platform="windows",
        seller_riven_count=123,
        raw_text=(
            f"800出{TORID_LINK}，重复卡{VECTIS_LINK}，"
            f"非命中卡{VECTIS_LINK}接受报价"
        ),
        message_cards=(
            ChannelCard(torid, 1, card_index=0),
            ChannelCard(vectis, 2, duplicate=True, card_index=1),
            ChannelCard(vectis, 3, card_index=2),
        ),
        display_cards=(
            ChannelCard(torid, 1, card_index=0),
            ChannelCard(vectis, 2, duplicate=True, card_index=1),
        ),
    )

    message, log = build_channel_discord_rich(payload)
    embed = _embed(message)
    description = embed.description

    assert not embed.title
    assert not embed.author
    assert (
        "**[Seller](https://warframe.market/profile/Seller) · "
        "历史记录：123 张不同紫卡**" in description
    )
    assert "**#1 [托里德 Toxi-acrican](" in description
    assert "#2 守望者 Sati-toxicron" not in description
    assert "重复发送" not in description
    assert "~~" not in description
    assert TORID_LINK not in description and VECTIS_LINK not in description
    assert (
        f"{discord_rich.ANSI_BLUE}[托里德 Toxi-acrican]"
        f"{discord_rich.ANSI_RESET}"
        in description
    )
    assert (
        f"重复卡[守望者 Sati-toxicron]，"
        f"非命中卡[守望者 Sati-toxicron]接受报价"
        in _without_ansi(description)
    )
    original = description.split("```ansi", 2)[1]
    assert original.count(discord_rich.ANSI_BLUE) == 1
    assert (
        f"{discord_rich.ANSI_YELLOW}[交易-简体中文] [PC] Seller"
        f"{discord_rich.ANSI_RESET} :"
        in original
    )
    assert "\x1b[1;37m" not in description
    assert "\x1b[1;30m" not in description
    assert "[#1 托里德 Toxi-acrican]" not in description
    assert "[托里德 Toxi-acrican]" in description
    assert "[#2 守望者 Sati-toxicron]" not in description
    assert "评分" not in description
    assert description.count('/w "Seller"') == 2
    assert '```\n/w "Seller" hi\n```' in description
    assert '```\n/inv "Seller"\n```' in description
    assert '```\n/join "Seller"\n```' in description
    assert '/w "Seller" hi\n/inv "Seller"' not in description
    assert "Discord Rich Embed" in log
    assert "预览" not in log


def test_channel_header_preserves_seller_underscores_and_links_each_card_once():
    torid = _decode(TORID_LINK)
    vectis = _decode(VECTIS_LINK)
    seller = "__Seller /,[]__"
    payload = ChannelPushPayload(
        seller=seller,
        channel="#T_ZH",
        cards=(
            ChannelCard(torid, 142449, card_index=0),
            ChannelCard(vectis, 142450, card_index=1),
        ),
        locale="zh",
        seller_riven_count=123,
    )

    message, _log = build_channel_discord_rich(payload)
    description = _embed(message).description
    header = description.splitlines()[:3]

    assert header[0] == (
        "**[__Seller /,\\[\\]__]"
        "(https://warframe.market/profile/"
        "__Seller%20%2F%2C%5B%5D__) · 历史记录：123 张不同紫卡**"
    )
    assert "\\_" not in header[0]
    assert description.count("https://warframe.market/profile/") == 1
    search_urls = re.findall(
        r"\((https://warframe\.market/auctions/search\?[^)]+)\)",
        "\n".join(header[1:]),
    )
    assert len(search_urls) == 2
    torid_params = parse_qs(urlsplit(search_urls[0]).query)
    assert torid_params == {
        "type": ["riven"],
        "weapon_url_name": ["torid"],
        "positive_stats": ["critical_damage,toxin_damage,multishot"],
        "negative_stats": ["ammo_maximum"],
        "buyout_policy": ["direct"],
        "sort_by": ["price_asc"],
    }


def test_channel_search_url_encodes_slash_and_stat_separator():
    card = {
        "weapon_slug": "test_weapon",
        "stats": [
            {"ref": "WeaponDamageAmountMod", "is_curse": False},
            {"ref": "WeaponCritDamageMod", "is_curse": False},
        ],
    }

    url = channel_push_module._wm_riven_search_url(card)

    assert (
        "positive_stats=base_damage_%2F_melee_damage%2Ccritical_damage"
        in url
    )


def test_channel_search_url_omits_negative_stats_when_card_has_no_curse():
    card = deepcopy(_decode(TORID_LINK))
    card["stats"] = [
        stat for stat in card["stats"] if not stat.get("is_curse")
    ]
    payload = ChannelPushPayload(
        seller="Seller",
        channel="#T_ZH",
        cards=(ChannelCard(card, 7),),
        locale="zh",
    )

    message, _log = build_channel_discord_rich(payload)
    description = _embed(message).description
    search_url = re.search(
        r"\((https://warframe\.market/auctions/search\?[^)]+)\)",
        description,
    ).group(1)
    params = parse_qs(urlsplit(search_url).query)

    assert "negative_stats" not in params
    assert "negative_stats=" not in search_url
    assert "历史紫卡数量未知" in description


def test_channel_original_message_keeps_names_aligned_after_invalid_link():
    vectis = _decode(VECTIS_LINK)
    card = ChannelCard(vectis, 7, card_index=0)
    payload = ChannelPushPayload(
        seller="Mixed",
        channel="#T_ZH",
        cards=(card,),
        locale="zh",
        raw_text=f"200出{INVALID_LINK}，另一个{VECTIS_LINK}",
        message_cards=(card,),
        display_cards=(card,),
    )

    message, _log = build_channel_discord_rich(payload)
    description = _embed(message).description

    assert (
        f"200出[紫卡]，另一个{discord_rich.ANSI_BLUE}"
        "[守望者 Sati-toxicron]"
        in description
    )


def test_qq_channel_message_matches_discord_content_without_rich_formatting():
    torid = _decode(TORID_LINK)
    payload = ChannelPushPayload(
        seller="Seller",
        channel="#T_ZH",
        cards=(ChannelCard(torid, 1, card_index=0),),
        locale="zh",
        seller_platform="windows",
        raw_text=f"800出{TORID_LINK}接受报价",
        message_cards=(ChannelCard(torid, 1, card_index=0),),
    )

    message, log = build_channel_qq(payload)
    segments = list(message)

    assert len(segments) == 1 and segments[0].type == "text"
    text = segments[0].data["text"]
    assert text == log
    assert "#1 托里德 Toxi-acrican" in text
    assert "[交易-简体中文] [PC] Seller : 800出[托里德 Toxi-acrican]" in text
    assert "段位：" in text and "等级：" in text and "洗练：" in text
    assert '/w "Seller" 收托里德紫卡' in text
    assert '/w "Seller" hi\n/inv "Seller"\n/join "Seller"' in text
    assert TORID_LINK not in text
    assert "warframe.market" not in text
    assert "历史记录" not in text
    assert "\x1b[" not in text
    assert "```" not in text and "**" not in text
    assert all(segment.type != "image" for segment in segments)


def test_wm_scheme_one_uses_channel_block_and_right_aligned_stat_columns():
    message, log = build_hit_discord_rich(
        {"display_number": 1}, WM_AUCTION, locale="zh",
    )
    embed = _embed(message)
    fields = {field.name: field for field in embed.fields}
    plain_description = _without_ansi(embed.description)
    stat_lines = [
        line for line in plain_description.splitlines()
        if any(value in line for value in ("+237.0%", "+2.6", "+109.4%"))
    ]

    assert embed.author.name == "WARFRAME.MARKET · RIVEN"
    assert embed.title == "海波单剑 Visi-loctitis · 150p"
    assert embed.url == (
        "https://warframe.market/auction/test-auction-1"
    )
    assert "[海波单剑 Visi-loctitis]" in plain_description
    assert "段位：14   等级：8   洗练：38   极性：D槽" in plain_description
    assert fields["卖家"].value == (
        "[TestSeller](https://warframe.market/profile/TestSeller) · 游戏中"
    )
    assert fields["价格"].value == "起拍 100p / 一口价 150p"
    assert fields["\u200b"].value.startswith("```\n/w TestSeller ")
    assert '```\n/w "TestSeller" hi\n```' in fields["\u200b"].value
    assert '```\n/inv "TestSeller"\n```' in fields["\u200b"].value
    assert '```\n/join "TestSeller"\n```' in fields["\u200b"].value
    assert '/w "TestSeller" hi\n/inv "TestSeller"' not in fields["\u200b"].value
    assert "游戏内联系" not in fields["\u200b"].value
    assert "快捷私聊" not in fields
    assert "评分" not in plain_description and "数值" not in plain_description
    assert [line[17:27] for line in stat_lines] == [
        "   +237.0%", "      +2.6", "   +109.4%",
    ]
    assert "Discord Rich Embed" in log
    assert "预览" not in log


def test_wm_seller_link_uses_game_name_without_escaped_underscores():
    auction = {
        **WM_AUCTION,
        "owner": {"ingame_name": "__Test_Seller__", "status": "online"},
    }

    message, _log = build_hit_discord_rich(
        {"display_number": 1}, auction, locale="zh",
    )
    embed = _embed(message)
    seller = next(field for field in embed.fields if field.name == "卖家")

    assert seller.value == (
        "[__Test_Seller__]"
        "(https://warframe.market/profile/__Test_Seller__) · 在线"
    )


def test_bargain_riven_uses_red_wm_layout_without_riven_stats():
    hit = bargain.Hit(
        price=Decimal("150"),
        baseline=Decimal("390"),
        discount=Decimal("0.615"),
        samples=12,
    )

    message, log = build_bargain_riven_discord_rich(
        "nami_solo", WM_AUCTION, hit, locale="zh")
    embed = _embed(message)
    fields = {field.name: field for field in embed.fields}
    plain_description = _without_ansi(embed.description)

    assert embed.author.name == "WARFRAME.MARKET · RIVEN"
    assert embed.title == "海波单剑 Visi-loctitis · 150p"
    assert embed.url == "https://warframe.market/auction/test-auction-1"
    assert embed.color == 0xED4245
    assert "低于参考价: 62%\n低价样本均价: 390p" in plain_description
    assert all(text not in plain_description for text in (
        "基础伤害", "攻击范围", "+237.0%", "+2.6",
        "MR:", "Rank:", "Rolls:", "Polarity:",
    ))
    assert fields["卖家"].value == (
        "[TestSeller](https://warframe.market/profile/TestSeller) · 游戏中"
    )
    assert fields["价格"].value == "起拍 100p / 一口价 150p"
    assert fields["\u200b"].value.startswith("```\n/w TestSeller ")
    assert '/w "TestSeller" hi' not in fields["\u200b"].value
    assert '```\n/inv "TestSeller"\n```' in fields["\u200b"].value
    assert "Discord Rich Embed" in log


@pytest.mark.parametrize(
    ("locale", "suffix"),
    (("zh", "0洗"), ("en", "Unrolled")),
)
def test_bargain_riven_title_marks_unrolled_card(locale, suffix):
    hit = bargain.Hit(
        price=Decimal("150"),
        baseline=Decimal("390"),
        discount=Decimal("0.615"),
        samples=12,
    )
    auction = deepcopy(WM_AUCTION)
    auction["item"]["re_rolls"] = 0
    auction["owner"]["ingame_name"] = "Entropy_Calm"

    message, _log = build_bargain_riven_discord_rich(
        "nami_solo", auction, hit, locale=locale)
    embed = _embed(message)
    seller = next(field for field in embed.fields if field.name in {"卖家", "Seller"})

    assert embed.title.endswith(f" · {suffix}")
    assert "[Entropy_Calm](https://warframe.market/profile/Entropy_Calm)" in seller.value
    assert "\\_" not in seller.value


def test_bargain_item_uses_red_layout_without_listing_link():
    hit = bargain.Hit(
        price=Decimal("45"),
        baseline=Decimal("98"),
        discount=Decimal("0.54"),
        samples=12,
    )

    message, log = build_bargain_item_discord_rich(
        "arcane_grace", ITEM_ORDER, hit, locale="zh")
    embed = _embed(message)
    fields = {field.name: field for field in embed.fields}
    plain_description = _without_ansi(embed.description)

    assert embed.author.name == "WARFRAME.MARKET · ITEM"
    assert embed.title == "赋能·优雅 R5 · 45p"
    assert "url" not in embed.model_dump(
        exclude_none=True, exclude_unset=True)
    assert embed.color == 0xED4245
    assert "低于参考价: 54%\n参考日均价: 98p" in plain_description
    assert fields["卖家"].value == (
        "[TestSeller](https://warframe.market/profile/TestSeller) · 游戏中"
    )
    assert fields["价格"].value == "45p · 数量 2"
    assert fields["\u200b"].value == (
        '```\n/w TestSeller Hi! I want to buy: "Arcane Grace (rank 5)" '
        "for 45 platinum. (warframe.market)\n```\n"
        '```\n/inv "TestSeller"\n```'
    )
    assert "test-order-1" not in str(embed)
    assert "Discord Rich Embed" in log
