"""命中紫卡图渲染 + 图文推送消息组装测试。"""

import sys
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from src.plugins.riven_sniper import formatter, hitcard  # noqa: E402
from src.plugins.riven_sniper.grading import (  # noqa: E402
    GradeResult,
    StatGrade,
    grade_auction_item,
)

NAMI_ITEM = {
    "weapon_url_name": "nami_solo",
    "name": "visi-loctitis",
    "type": "riven",
    "mod_rank": 8,
    "re_rolls": 38,
    "mastery_level": 14,
    "polarity": "vazarin",
    "attributes": [
        {"url_name": "base_damage_/_melee_damage", "value": 237.0,
         "positive": True},
        {"url_name": "range", "value": 2.6, "positive": True},
        {"url_name": "critical_damage", "value": 109.4, "positive": True},
        {"url_name": "critical_chance_on_slide_attack", "value": -116.2,
         "positive": False},
    ],
}

CONFIG = {
    "id": 1, "display_number": 1, "group_id": 302875968,
    "weapon": None, "wildcard": "melee",
    "positives": [
        ["base_damage_/_melee_damage"], ["range"], ["critical_damage"],
    ],
    "negatives": [["critical_chance_on_slide_attack"]],
    "zero_rerolls": False, "enabled": 1,
}

EXPECTED_CARD_ABBREVIATIONS = {
    "punch_through": "PT",
    "slash_damage": "SLASH",
    "impact_damage": "IMP",
    "toxin_damage": "TOX",
    "status_duration": "SD",
    "ammo_maximum": "AM",
    "recoil": "REC",
    "zoom": "Z",
    "channeling_damage": "IC",
    "channeling_efficiency": "EFF",
    "critical_chance": "CC",
    "critical_damage": "CD",
    "base_damage_/_melee_damage": "DMG",
    "heat_damage": "HEAT",
    "multishot": "MS",
    "reload_speed": "REL",
    "range": "RANGE",
    "damage_vs_corpus": "DTC",
    "damage_vs_grineer": "DTG",
    "puncture_damage": "PUNC",
    "damage_vs_infested": "DTI",
    "electric_damage": "ELEC",
    "finisher_damage": "FIN",
    "fire_rate_/_attack_speed": "FR / AS",
    "projectile_speed": "PS",
    "magazine_capacity": "MAG",
    "status_chance": "SC",
    "cold_damage": "COLD",
    "combo_duration": "COMBO",
    "critical_chance_on_slide_attack": "SLIDE",
    "chance_to_gain_extra_combo_count": "ACCC",
    "chance_to_gain_combo_count": "CGC",
}


def _auction(item=NAMI_ITEM):
    return {
        "id": "test-auction-1",
        "item": item,
        "owner": {"ingame_name": "TestSeller", "status": "ingame"},
        "starting_price": 100,
        "buyout_price": 150,
        "is_direct_sell": False,
    }


def test_render_returns_jpeg():
    result = grade_auction_item(NAMI_ITEM)
    img = hitcard.render_hit_card(NAMI_ITEM, result)
    assert img[:3] == b"\xff\xd8\xff"   # JPEG 魔数
    assert len(img) > 10_000


def test_render_with_auction_uses_fixed_layout():
    """带价格信息时使用固定的大字版布局，输出尺寸保持稳定。"""
    result = grade_auction_item(NAMI_ITEM)
    img = hitcard.render_hit_card(NAMI_ITEM, result, _auction())
    assert img[:3] == b"\xff\xd8\xff"
    assert len(img) > 10_000
    with Image.open(BytesIO(img)) as rendered:
        assert rendered.size == (hitcard.IMG_W, hitcard.IMG_H)
        assert rendered.size == (974, 556)


def test_english_card_uses_reference_abbreviations_at_original_font_sizes():
    """英文卡统一使用参考表简写，并保持 1.19.0 的固定字号。"""
    assert set(hitcard.rivendata.attribute_abbreviations()) == set(
        hitcard.rivendata.attributes()
    )
    assert {
        slug: abbreviation
        for slug, abbreviation in hitcard.rivendata.attribute_abbreviations().items()
    } == EXPECTED_CARD_ABBREVIATIONS

    canvas = Image.new("RGBA", (hitcard.IMG_W, hitcard.IMG_H))
    draw = ImageDraw.Draw(canvas)
    card_font = hitcard._largest_common_font(
        draw, hitcard.CARD_STAT_PROBES, hitcard._px(444), 44, min_size=34,
    )
    panel_font = hitcard._largest_common_font(
        draw, hitcard.PANEL_STAT_PROBES,
        hitcard.PANEL_W - hitcard._px(60), 60, min_size=39,
    )

    grades = [
        StatGrade(slug, 999.9, positive, 0.5, "B")
        for slug in EXPECTED_CARD_ABBREVIATIONS
        for positive in (True, False)
    ]
    assert all(
        draw.textlength(hitcard.format_stat_text(grade, "en"), font=card_font)
        <= hitcard._px(444)
        for grade in grades
    )
    assert all(
        draw.textlength("  ".join(hitcard._stat_parts(grade, "en")),
                        font=panel_font)
        <= hitcard.PANEL_W - hitcard._px(60)
        for grade in grades
    )


def test_panel_header_uses_short_disposition_and_original_title_sizes():
    """英文倾向简写放入原预留区，武器标题恢复原字号范围。"""
    canvas = Image.new("RGBA", (hitcard.IMG_W, hitcard.IMG_H))
    draw = ImageDraw.Draw(canvas)
    disposition = hitcard._disposition_text(1.55, "en")
    disposition_font = hitcard._font(40)
    assert disposition == "Disp. 1.55"
    assert draw.textlength(disposition, font=disposition_font) <= hitcard._px(210)

    available = (
        hitcard.PANEL_W - hitcard._px(60) - hitcard._px(210)
    )
    for slug in hitcard.rivendata.weapons():
        name = hitcard.rivendata.weapon_name(slug, "en")
        font = hitcard._fit_font(draw, name, available, 52, min_size=36)
        assert draw.textlength(name, font=font) <= available


def test_english_panel_does_not_include_a_chinese_subtitle():
    assert hitcard._localized_weapon_names("vectis", "zh") == ("守望者", "Vectis")
    assert hitcard._localized_weapon_names("vectis", "en") == ("Vectis", "")


def test_rank_templates_and_platinum_asset_are_available():
    """0~8 级分别使用对应模板，价格使用白金图标。"""
    assert set(hitcard.TEMPLATE_NAMES) == set(range(9))
    assert all(hitcard._template(rank).size == (hitcard.CARD_W, hitcard.CARD_H)
               for rank in range(9))
    assert hitcard._platinum_icon(36).height == 36
    with pytest.raises(ValueError, match="0~8"):
        hitcard._template(9)


def test_disposition_uses_variant_value_without_variant_description():
    # 评分用的倾向：海波单剑（nami_solo）在 weapons.json 里有基础倾向
    result = grade_auction_item(NAMI_ITEM)
    disp = hitcard._used_disposition(NAMI_ITEM, result)
    assert disp is not None and 0.5 <= disp <= 1.6
    # 未知武器：倾向未知，不应报错
    assert hitcard._used_disposition({"weapon_url_name": "no_such"},
                                     GradeResult()) is None


def test_grade_colors_high_contrast_gradient():
    """档位锚点 F红 C橙 B黄 A浅绿 S紫，按数值高低平滑过渡、无跳变。"""
    r, g, b, _ = hitcard.grade_color(-9.75)   # F 红
    assert r > 230 and g < 110 and b < 110
    r, g, b, _ = hitcard.grade_color(-6.5)    # C 橙
    assert r > 230 and 110 < g < 200 and b < 100
    r, g, b, _ = hitcard.grade_color(0.0)     # B 黄
    assert r > 230 and g > 200 and b < 120
    r, g, b, _ = hitcard.grade_color(6.5)     # A 浅绿
    assert g > 210 and g > r and b < 150
    r, g, b, _ = hitcard.grade_color(9.75)    # S 紫
    assert b > 230 and r > 150 and g < 150
    # 端点外钳制
    assert hitcard.grade_color(-10.0) == hitcard.grade_color(-9.75)
    assert hitcard.grade_color(10.0) == hitcard.grade_color(9.75)
    # 平滑过渡：相邻 0.25% 取样点色差有限
    prev, d = hitcard.grade_color(-9.75), -9.75
    while d < 9.75:
        d = min(d + 0.25, 9.75)
        cur = hitcard.grade_color(d)
        assert max(abs(a - b) for a, b in zip(cur[:3], prev[:3])) < 36, f"dev={d}"
        prev = cur
    # X/? 用独立色，渲染不会因缺档报错
    assert hitcard.grade_color(None, "X") == hitcard.X_COLOR
    assert hitcard.grade_color(None, "?") == hitcard.NA_COLOR


def test_out_of_range_score_keeps_percentage_and_appends_limit_label():
    grade = StatGrade(
        "critical_chance", 220.0, True, 1.2, "X", 173.7, 212.3
    )

    assert grade.deviation == 14.0
    assert hitcard._score_text(grade) == "评分：X（+14.0% 数值超限）"
    assert "评分：X，偏差 +14.0%，数值超限" in formatter._format_stat(
        grade, "zh")


def test_render_unknown_weapon_and_attribute():
    """未知武器/词条（评分 ?）不能让渲染崩溃。"""
    item = {
        "weapon_url_name": "no_such_weapon",
        "name": "acri-test",
        "mod_rank": 0,
        "re_rolls": 0,
        "mastery_level": 8,
        "polarity": "naramon",
        "attributes": [
            {"url_name": "no_such_attr", "value": 55.5, "positive": True},
            {"url_name": "zoom", "value": -3.0, "positive": False},
        ],
    }
    result = grade_auction_item(item)
    assert all(g.grade == "?" for g in result.stats)
    img = hitcard.render_hit_card(item, result)
    assert img[:3] == b"\xff\xd8\xff"   # JPEG 魔数


def test_stat_text_official_names_and_units():
    """卡图词条行：官方中文名 + 非百分比单位。"""
    g = StatGrade("range", 2.6, True, 0.5, "B")
    assert hitcard.format_stat_text(g) == "+2.6m 攻击范围"
    g = StatGrade("critical_damage", 109.4, True, 0.1, "C-")
    assert hitcard.format_stat_text(g) == "+109.4% 暴击伤害"
    # 负词条但数值为正 → 标注（负），与文字推送一致
    g = StatGrade("zoom", 5.9, False, 0.5, "B")
    assert hitcard.format_stat_text(g) == "+5.9% 变焦(负)"
    g = StatGrade("damage_vs_corpus", 1.54, True, 0.62, "B+")
    assert hitcard.format_stat_text(g) == "x1.54 对Corpus伤害"
    g = StatGrade("damage_vs_infested", 0.72, False, 0.84, "A")
    assert hitcard.format_stat_text(g) == "x0.72 对Infested伤害"
    assert hitcard.format_stat_text(g, "en") == "x0.72 DTI"
    g = StatGrade("critical_damage", 109.4, True, 0.1, "C-")
    assert hitcard.format_stat_text(g, "en") == "+109.4% CD"


def test_faction_bot_push_text_and_card_are_multiplier_formatted():
    """真实 WFM 乘数样本走完 Bot 文字和卡图推送管线。"""
    item = {
        "weapon_url_name": "dual_decurion", "name": "sample",
        "type": "riven", "mod_rank": 8, "re_rolls": 0,
        "mastery_level": 10, "polarity": "madurai",
        "attributes": [
            {"value": 85.0, "positive": True,
             "url_name": "critical_damage"},
            {"value": 127.1, "positive": True,
             "url_name": "ammo_maximum"},
            {"value": 1.54, "positive": True,
             "url_name": "damage_vs_corpus"},
            {"value": -54.7, "positive": False, "url_name": "zoom"},
        ],
    }
    auction = _auction(item)

    result = grade_auction_item(item)
    text = formatter.format_hit(CONFIG, auction, result)
    message, log_text = formatter.build_hit_push(CONFIG, auction)

    assert "x1.54 对Corpus伤害" in text
    assert "参考区间 x1.47 至 x1.58" in text
    assert "评分越界" not in text
    assert "x1.54 对Corpus伤害" in log_text
    assert any(segment.type == "image" for segment in message)


def test_build_hit_push_image_message():
    message, log_text = formatter.build_hit_push(CONFIG, _auction())
    segs = list(message)
    assert segs[0].type == "text" and "WARFRAME.MARKET · RIVEN" in segs[0].data["text"]
    assert segs[1].type == "image"
    assert segs[1].data["file"].startswith("base64://")
    tail = segs[2].data["text"]
    # 价格、卖家和购买私聊文案在文字部分
    assert "一口价 150p" in tail and "TestSeller" in tail
    assert (
        '/w TestSeller hi wtb your "Nami Solo Visi-loctitis" riven for 150 platinum.'
        in tail
    )
    assert (
        '/w "TestSeller" hi\n/inv "TestSeller"\n/join "TestSeller"'
        in tail
    )
    # 词条评分明细只进图片，不再出现在文字部分
    assert "暴击伤害" not in tail and "评分" not in tail and "[A" not in tail
    assert "已生成命中卡图" in log_text


def test_build_hit_push_falls_back_to_text(monkeypatch):
    def boom(item, result, auction):
        raise RuntimeError("no assets")

    monkeypatch.setattr(hitcard, "render_hit_card", boom)
    message, log_text = formatter.build_hit_push(CONFIG, _auction())
    assert all(seg.type == "text" for seg in message)
    text = str(message)
    assert "WARFRAME.MARKET · RIVEN" in text and "暴击伤害" in text  # 回退为完整文字
    assert "回退文字" in log_text


def test_format_hit_text_uses_plain_readable_shape():
    """回退文本使用纯文本格式，并保留词条明细与评分。"""
    text = formatter.format_hit(CONFIG, _auction())
    assert "WARFRAME.MARKET · RIVEN" in text
    assert "海波单剑" in text
    assert "暴击伤害" in text and "评分：" in text
    assert '/w TestSeller hi wtb your "Nami Solo Visi-loctitis"' in text
    assert not set("◆▎※🎯💰[]|#").intersection(text)
