"""频道来源、本地化私聊与跨平台卡图消息回归。"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.plugins.riven_sniper import riven_link
from src.plugins.riven_sniper.channel_locale import (
    IN_GAME_SWITCHABLE_REGIONS,
    channel_whisper,
    presence_region_key,
    presence_region_label,
    source_label,
    unrolled_label,
    whisper_language,
)
from src.plugins.riven_sniper.channel_push import (
    ChannelCard,
    ChannelPushPayload,
    build_channel_discord_rich,
    build_channel_qq,
    card_heading,
    format_channel_push,
)


RIVEN_TEXT = (
    "[OMG-LotusRifleRandomModRare:"
    "9b51zSsL1EkVt78mCp1r4/fpQ+1jxghk]"
)


def _decoded() -> dict:
    match = riven_link.OMG_RE.search(RIVEN_TEXT)
    return riven_link.decode_link(match.group(1), match.group(2))


def test_presence_region_key_ignores_channel_kind():
    cases = [
        ("#G_EN_NA", "EN_NA"),
        ("#Q_EN_NA", "EN_NA"),
        ("#R_EN_EU", "EN_EU"),
        ("#T_EN_SA", "EN_SA"),
        ("#G_EN_RU", "EN_RU"),
        ("#Q_EN_AS", "EN_AS"),
        ("#G_ZH", "ZH"),
        ("#T_RU", "RU"),
    ]
    assert [presence_region_key(channel) for channel, _ in cases] == [
        expected for _, expected in cases]


def test_only_five_english_regions_are_switchable_in_game():
    assert IN_GAME_SWITCHABLE_REGIONS == {
        "EN_NA", "EN_EU", "EN_AS", "EN_SA", "EN_RU",
    }


def test_presence_region_label_omits_channel_kind():
    cases = [
        ("#G_EN_NA", "zh", "[北美]"),
        ("#R_EN_EU", "zh", "[欧洲]"),
        ("#Q_ZH", "zh", "[简体中文]"),
        ("#T_EN_AS", "en", "[AS]"),
        ("#G_ZH", "en", "[ZH]"),
    ]
    assert [presence_region_label(channel, locale)
            for channel, locale, _ in cases] == [
        expected for _, _, expected in cases]


def test_source_and_unrolled_labels_follow_target_language():
    assert source_label("#T_EN_NA", "zh") == "[交易-北美]"
    assert source_label("#T_EN_NA", "en") == "[Trade-NA]"
    assert source_label("#R_ZH", "zh") == "[招募-简体中文]"
    assert source_label("#R_ZH", "en") == "[Recruit-ZH]"
    assert unrolled_label("zh") == "0洗"
    assert unrolled_label("en") == "Unrolled"


def test_whisper_language_matrix():
    cases = [
        ("zh", "#T_ZH", "ZH"),
        ("zh", "#T_TC", "TC"),
        ("zh", "#T_FR", "FR"),
        ("zh", "#T_RU", "RU_LATIN"),
        ("zh", "#T_UK", "EN"),
        ("zh", "#T_PL", "EN"),
        ("zh", "#T_JA", "EN"),
        ("zh", "#T_KO", "EN"),
        ("en", "#T_RU", "RU"),
        ("en", "#T_UK", "EN"),
        ("en", "#T_PL", "PL"),
        ("en", "#T_FR", "FR"),
        ("en", "#T_DE", "DE"),
        ("en", "#T_ES", "ES"),
        ("en", "#T_PT", "PT"),
        ("en", "#T_IT", "IT"),
        ("en", "#T_ZH", "EN"),
        ("en", "#T_TC", "EN"),
        ("en", "#T_JA", "EN"),
        ("en", "#T_KO", "EN"),
        ("zh", "#T_EN_NA", "EN"),
        ("en", "#T_EN_NA", "EN"),
    ]
    assert [whisper_language(channel, locale)
            for locale, channel, _ in cases] == [
        expected for _, _, expected in cases]


def test_channel_whisper_has_short_localized_weapon_template():
    cases = [
        ("#T_EN", "zh", "Latron", 'wtb Latron riven'),
        ("#T_ZH", "zh", "拉特昂", "收拉特昂紫卡"),
        ("#T_TC", "zh", "拉特昂", "收拉特昂紫卡"),
        ("#T_FR", "zh", "Latron", "achète riven Latron"),
        ("#T_DE", "zh", "Latron", "suche Latron Riven"),
        ("#T_ES", "zh", "Latron", "compro riven de Latron"),
        ("#T_PT", "zh", "Latron", "compro riven de Latron"),
        ("#T_RU", "en", "Latron", "куплю ривен на Latron"),
        ("#T_RU", "zh", "Latron", "kuplyu riven na Latron"),
        ("#T_IT", "zh", "Latron", "cerco riven per Latron"),
        ("#T_PL", "en", "Latron", "kupię riven Latron"),
        ("#T_PL", "zh", "Latron", "wtb Latron riven"),
        ("#T_UK", "en", "Latron", "wtb Latron riven"),
        ("#T_UK", "zh", "Latron", "wtb Latron riven"),
        ("#T_JA", "zh", "Latron", "wtb Latron riven"),
        ("#T_KO", "en", "Latron", "wtb Latron riven"),
    ]
    assert [channel_whisper("Seller Name", weapon, channel, locale)
            for channel, locale, weapon, _ in cases] == [
        f'/w "Seller Name" {expected}'
        for _, _, _, expected in cases
    ]


def test_channel_text_uses_strict_database_weapon_name_and_source():
    decoded = _decoded()
    decoded["rerolls"] = 0
    card = ChannelCard(decoded, 7)
    zh = ChannelPushPayload(
        "Xbox Seller", "#T_EN_NA", (card,), "zh", 111,
        seller_platform="xbox",
    )
    en = ChannelPushPayload(
        "PS Seller", "#T_EN_NA", (card,), "en", 111,
        seller_platform="playstation",
    )

    assert card_heading(card, "zh").startswith("#7 守望者 Sati-toxicron")
    assert card_heading(card, "zh").endswith("0洗")
    assert card_heading(card, "en").startswith("#7 Vectis Sati-toxicron")
    assert card_heading(card, "en").endswith("Unrolled")
    assert "[交易-北美] [Xbox] Xbox Seller" in format_channel_push(zh)
    assert '/w "Xbox Seller"' in format_channel_push(zh)
    assert "[Trade-NA] [PlayStation] PS Seller" in format_channel_push(en)
    discord_message, _log = build_channel_discord_rich(en)
    description = list(discord_message)[0].data["embed"].description
    assert "Historical Riven count unavailable" in description
    assert "[Vectis Sati-toxicron](" in description
    assert '```\n/w "PS Seller" wtb Vectis riven\n```' in description


def test_channel_push_builders_redact_internal_identifier_seller():
    decoded = _decoded()
    account_id = "0123456789abcdef01234567"
    payload = ChannelPushPayload(
        account_id, "#T_EN_NA", (ChannelCard(decoded, 7),), "zh", 111)
    text = format_channel_push(payload)
    _qq_message, qq_log = build_channel_qq(payload)
    _discord_message, discord_log = build_channel_discord_rich(payload)

    assert account_id not in text
    assert account_id not in qq_log
    assert account_id not in discord_log
    assert "—" in text


@pytest.mark.parametrize(("channel", "phrase"), [
    ("#T_EN_NA", "wtb Vectis riven"),
    ("#T_EN_EU", "wtb Vectis riven"),
    ("#T_EN_AS", "wtb Vectis riven"),
    ("#T_EN_SA", "wtb Vectis riven"),
    ("#T_EN_RU", "wtb Vectis riven"),
    ("#T_ZH", "wtb Vectis riven"),
    ("#T_TC", "wtb Vectis riven"),
    ("#T_JA", "wtb Vectis riven"),
    ("#T_KO", "wtb Vectis riven"),
    ("#T_UK", "wtb Vectis riven"),
    ("#T_FR", "achète riven Vectis"),
    ("#T_DE", "suche Vectis Riven"),
    ("#T_ES", "compro riven de Vectis"),
    ("#T_PT", "compro riven de Vectis"),
    ("#T_RU", "куплю ривен на Vectis"),
    ("#T_IT", "cerco riven per Vectis"),
    ("#T_PL", "kupię riven Vectis"),
])
def test_english_channel_copy_adapts_whisper_and_keeps_original_content(channel, phrase):
    raw = "出售紫卡"
    seller = "中文玩家"
    payload = ChannelPushPayload(
        seller, channel, (ChannelCard(_decoded(), 7),), "en", 111,
        raw_text=raw, seller_platform="windows",
    )
    qq, _log = build_channel_qq(payload)
    discord, _log = build_channel_discord_rich(payload)
    discord_text = "\n".join(
        segment.data["embed"].model_dump_json(exclude_unset=True)
        for segment in discord
    )
    whisper = f'/w "{seller}" {phrase}'
    assert whisper in format_channel_push(payload).splitlines()
    for reply in (str(qq), discord_text.replace('\\"', '"')):
        assert raw in reply and seller in reply
        generated = reply.replace(raw, "").replace(seller, "")
        assert not re.search(r"[\u3400-\u9fff]", generated), generated
        assert whisper in reply
