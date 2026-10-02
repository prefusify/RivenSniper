"""评分引擎测试：用公式正向构造已知 roll 的词条值，评分必须能反解回来。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from src.plugins.riven_sniper import grading, rivendata  # noqa: E402
from src.plugins.riven_sniper.grading import (  # noqa: E402
    NUM_BUFFS_ATTEN,
    NUM_BUFFS_CURSE_ATTEN,
    grade_auction_item,
    roll_to_grade,
)


def _make_display(weapon_slug: str, attr_slug: str, roll: float, *,
                  positive: bool, n_buffs: int, n_curses: int, lvl: int) -> float:
    """按 RivenParser 公式正向计算 WFM 显示值。"""
    category = rivendata.weapon_category(weapon_slug)
    game_ref = rivendata.attributes()[attr_slug]["game_ref"]
    base = rivendata.base_value(category, game_ref)
    disp = rivendata.weapons()[weapon_slug]["disposition"]
    factor = 0.9 + 0.2 * roll
    atten = 1.5 * disp * 10 * (lvl + 1)
    if positive:
        value = base * atten * (1.25 ** n_curses) * factor * NUM_BUFFS_ATTEN[min(n_buffs, 5)]
    else:
        value = -base * atten * factor \
            * NUM_BUFFS_CURSE_ATTEN[min(n_buffs, 5)] * NUM_BUFFS_ATTEN[min(n_curses, 5)]
    faction_refs = {
        "WeaponFactionDamageGrineer", "WeaponFactionDamageCorpus",
        "WeaponFactionDamageInfested", "WeaponMeleeFactionDamageGrineer",
        "WeaponMeleeFactionDamageCorpus", "WeaponMeleeFactionDamageInfested",
    }
    if game_ref in faction_refs:
        return round((1 + value) * 100) / 100
    raw_refs = {
        "WeaponMeleeComboInitialBonusMod", "ComboDurationMod",
        "WeaponMeleeRangeIncMod",
    }
    if game_ref in raw_refs:
        return round(value, 2)
    return round(value * 1000) / 10


def _item(weapon, attrs, lvl=8, re_rolls=0):
    return {"weapon_url_name": weapon, "type": "riven", "mod_rank": lvl,
            "re_rolls": re_rolls, "polarity": "madurai",
            "attributes": attrs}


def test_roundtrip_2p1n_rifle():
    """2正1负步枪：正词条 roll 精确往返；负词条存 1-物理roll（RivenSniper 方向）。"""
    rolls = {"critical_chance": 0.75, "critical_damage": 0.25, "zoom": 0.8}
    attrs = [
        {"url_name": "critical_chance", "positive": True,
         "value": _make_display("torid", "critical_chance", 0.75,
                                positive=True, n_buffs=2, n_curses=1, lvl=8)},
        {"url_name": "critical_damage", "positive": True,
         "value": _make_display("torid", "critical_damage", 0.25,
                                positive=True, n_buffs=2, n_curses=1, lvl=8)},
        {"url_name": "zoom", "positive": False,
         "value": _make_display("torid", "zoom", 0.2,  # 物理roll 0.2 → 评分方向 0.8
                                positive=False, n_buffs=2, n_curses=1, lvl=8)},
    ]
    result = grade_auction_item(_item("torid", attrs))
    assert result.fitted and result.variant is None and result.assumed_rank is None
    for g in result.stats:
        assert g.roll is not None, f"{g.slug} 未能评分"
        assert abs(g.roll - rolls[g.slug]) < 0.02, f"{g.slug}: roll {g.roll} != {rolls[g.slug]}"


def test_roundtrip_3p0n_melee_rank0():
    """3正0负近战、0级卡：低数值也能正确反解。"""
    attrs = [
        {"url_name": s, "positive": True,
         "value": _make_display("skana", s, r, positive=True, n_buffs=3, n_curses=0, lvl=0)}
        for s, r in [("critical_chance", 1.0), ("range", 0.0),
                     ("fire_rate_/_attack_speed", 0.5)]
    ]
    grades = {g.slug: g for g in grade_auction_item(_item("skana", attrs, lvl=0)).stats}
    assert grades["critical_chance"].grade == "S"
    assert grades["range"].grade == "F"
    assert grades["fire_rate_/_attack_speed"].grade == "B"


def test_negative_direction_matches_rivensniper():
    """负词条评分方向与 RivenSniper 一致：负面越温和（物理roll越低）评分越高。

    对照示例：Nami Solo -116.2% 滑行暴击（弱端）RivenSniper 给 S +9.713%。
    """
    def curse_grades(phys_roll):
        attrs = [
            {"url_name": "critical_chance", "positive": True,
             "value": _make_display("torid", "critical_chance", 0.5,
                                    positive=True, n_buffs=2, n_curses=1, lvl=8)},
            {"url_name": "critical_damage", "positive": True,
             "value": _make_display("torid", "critical_damage", 0.5,
                                    positive=True, n_buffs=2, n_curses=1, lvl=8)},
            {"url_name": "zoom", "positive": False,
             "value": _make_display("torid", "zoom", phys_roll,
                                    positive=False, n_buffs=2, n_curses=1, lvl=8)},
        ]
        return {g.slug: g for g in grade_auction_item(_item("torid", attrs)).stats}

    mild = curse_grades(0.0)["zoom"]   # 最温和的负词条 → 最高分
    harsh = curse_grades(1.0)["zoom"]  # 最狠的负词条 → 最低分
    assert mild.grade == "S" and abs(mild.deviation - 10.0) < 0.3
    assert harsh.grade == "F" and abs(harsh.deviation + 10.0) < 0.3


@pytest.mark.parametrize("slug", [
    "damage_vs_corpus", "damage_vs_grineer", "damage_vs_infested",
])
def test_faction_buff_wfm_multiplier_roundtrip(slug):
    """WFM 正阵营词条传总乘数，评分前必须减 1。"""
    attrs = [
        {
            "url_name": "critical_damage", "positive": True,
            "value": _make_display(
                "torid", "critical_damage", 0.5, positive=True,
                n_buffs=2, n_curses=0, lvl=8,
            ),
        },
        {
            "url_name": slug, "positive": True,
            "value": _make_display(
                "torid", slug, 0.5, positive=True,
                n_buffs=2, n_curses=0, lvl=8,
            ),
        },
    ]

    result = grade_auction_item(_item("torid", attrs))
    faction = next(g for g in result.stats if g.slug == slug)

    assert faction.value > 1
    assert faction.grade == "B"
    assert faction.roll == pytest.approx(0.5, abs=0.06)
    assert 1 < faction.min_display < faction.value < faction.max_display


@pytest.mark.parametrize("slug", [
    "damage_vs_corpus", "damage_vs_grineer", "damage_vs_infested",
])
def test_faction_curse_wfm_multiplier_roundtrip(slug):
    """WFM 负阵营词条传小于 1 的总乘数，不是正增量。"""
    attrs = [
        {
            "url_name": stat_slug, "positive": True,
            "value": _make_display(
                "torid", stat_slug, 0.5, positive=True,
                n_buffs=2, n_curses=1, lvl=8,
            ),
        }
        for stat_slug in ("critical_chance", "critical_damage")
    ]
    attrs.append({
        "url_name": slug, "positive": False,
        "value": _make_display(
            "torid", slug, 0.5, positive=False,
            n_buffs=2, n_curses=1, lvl=8,
        ),
    })

    result = grade_auction_item(_item("torid", attrs))
    faction = next(g for g in result.stats if g.slug == slug)

    assert 0 < faction.value < 1
    assert faction.grade == "B"
    assert faction.roll == pytest.approx(0.5, abs=0.06)
    assert faction.min_display < faction.value < faction.max_display < 1


def test_faction_conversion_covers_rifle_and_melee_refs():
    """枪械/近战的六个 gameRef 共用同一乘数原点。"""
    for game_ref in grading._FACTION_REFS:
        assert grading._display_to_value(game_ref, 1.54) \
            == pytest.approx(0.54)
        assert grading._display_to_value(game_ref, 0.72) \
            == pytest.approx(-0.28)
        assert grading._value_to_display(game_ref, 0.54) == 1.54
        assert grading._value_to_display(game_ref, -0.28) == 0.72


def test_rank0_faction_multiplier_uses_its_two_decimal_display_bucket():
    """0 级 x1.05 的舍入区间与合法 roll 相交时不应误判 X。"""
    item = _item("alternox", [
        {"url_name": "projectile_speed", "value": 10.3,
         "positive": True},
        {"url_name": "damage_vs_corpus", "value": 1.05,
         "positive": True},
    ], lvl=0)

    result = grade_auction_item(item)
    faction = next(g for g in result.stats
                   if g.slug == "damage_vs_corpus")

    assert result.fitted
    assert faction.grade != "X"
    assert faction.roll is not None
    assert (faction.min_display, faction.max_display) == (1.05, 1.06)


def test_faction_multipliers_participate_in_variant_fit():
    """阵营词条可独立识别变体倾向，不得绕过整卡拟合。"""
    base_disposition = 0.95
    prime_disposition = 0.7
    attrs = []
    for slug in ("damage_vs_corpus", "damage_vs_grineer"):
        base_multiplier = _make_display(
            "rubico", slug, 0.5, positive=True,
            n_buffs=2, n_curses=0, lvl=8,
        )
        prime_multiplier = 1 + (base_multiplier - 1) \
            * prime_disposition / base_disposition
        attrs.append({
            "url_name": slug, "positive": True,
            "value": round(prime_multiplier, 2),
        })

    result = grade_auction_item(_item("rubico", attrs, lvl=8))

    assert result.fitted
    assert result.variant == "Rubico Prime"
    assert all(g.grade != "X" for g in result.stats)


def test_negative_only_combo_count_uses_its_negative_wfm_display_sign():
    """该词条基准值已为负，作为 curse 时不能像后坐力一样再次取反。"""
    weapon = "war"
    lvl, n_buffs, n_curses, physical_roll = 8, 3, 1, 0.4
    attrs = [
        {
            "url_name": slug,
            "positive": True,
            "value": _make_display(
                weapon, slug, roll, positive=True,
                n_buffs=n_buffs, n_curses=n_curses, lvl=lvl,
            ),
        }
        for slug, roll in [
            ("critical_chance", 0.5),
            ("critical_damage", 0.5),
            ("base_damage_/_melee_damage", 0.5),
        ]
    ]
    category = rivendata.weapon_category(weapon)
    ref = "WeaponMeleeComboPointsOnHitMod"
    base = rivendata.base_value(category, ref)
    disposition = rivendata.weapons()[weapon]["disposition"]
    mid = base * 1.5 * disposition * 10 * (lvl + 1) \
        * NUM_BUFFS_CURSE_ATTEN[n_buffs] * NUM_BUFFS_ATTEN[n_curses]
    curse_value = round(mid * (0.9 + 0.2 * physical_roll) * 1000) / 10
    attrs.append({
        "url_name": "chance_to_gain_combo_count",
        "positive": False,
        "value": curse_value,
    })

    result = grade_auction_item(_item(weapon, attrs, lvl=lvl))
    curse = next(g for g in result.stats
                 if g.slug == "chance_to_gain_combo_count")
    assert result.fitted
    assert curse.value < 0
    assert curse.roll is not None
    assert abs(curse.roll - (1 - physical_roll)) < 0.02
    assert curse.grade == roll_to_grade(1 - physical_roll)
    # 同为负基准值的后坐力 curse 仍应显示为正数。
    assert grading._curse_base_value("WeaponRecoilReductionMod", -0.01) == 0.01


def test_archgun_uses_archgun_table():
    """大型枪械必须用 archgun 基准表（WFM 把它标成 rifle 的坑）。"""
    assert rivendata.weapon_category("imperator") == "LotusArchgunRandomModRare"
    assert rivendata.weapon_category("torid") == "LotusRifleRandomModRare"


def test_out_of_range_keeps_calculated_deviation_and_marks_x():
    """严重出界仍外推偏差百分比，同时标 X 且不参与候选拟合。"""
    attrs = [
        {"url_name": "critical_chance", "positive": True, "value": 9999.0},
        {"url_name": "critical_damage", "positive": True, "value": 1.0},
    ]
    result = grade_auction_item(_item("torid", attrs))
    assert not result.fitted
    grades = {g.slug: g for g in result.stats}
    assert grades["critical_chance"].grade == "X"
    assert grades["critical_damage"].grade == "X"
    assert all(g.roll is not None for g in grades.values())
    assert all(abs(g.deviation) > 10 for g in grades.values())


def test_unknown_attribute_graceful():
    attrs = [{"url_name": "not_a_real_stat", "positive": True, "value": 50.0}]
    g = grade_auction_item(_item("torid", attrs)).stats[0]
    assert g.grade == "?"
    assert g.roll is None


def test_unranked_filled_with_max_rank_values():
    """0级卡但卖家填满级数值：应按满级拟合成功并标注。"""
    attrs = [
        {"url_name": s, "positive": True,
         "value": _make_display("torid", s, 0.5, positive=True, n_buffs=2, n_curses=0, lvl=8)}
        for s in ("critical_chance", "critical_damage")
    ]
    result = grade_auction_item(_item("torid", attrs, lvl=0))
    assert result.fitted and result.assumed_rank == 8
    for g in result.stats:
        assert g.grade == "B"


def test_max_rank_listing_filled_with_rank0_values():
    """听单标注满级但卖家填 0 级数值时，应按 0 级拟合成功并标注。"""
    attrs = [
        {"url_name": s, "positive": True,
         "value": _make_display("torid", s, 0.5, positive=True, n_buffs=2, n_curses=0, lvl=0)}
        for s in ("critical_chance", "critical_damage")
    ]
    result = grade_auction_item(_item("torid", attrs, lvl=8))
    assert result.fitted and result.assumed_rank == 0
    for g in result.stats:
        assert g.grade == "B"


def test_rank0_value_is_ninth_of_max():
    """0 级数值 = 满级数值的 1/9。"""
    v0 = _make_display("torid", "critical_chance", 0.5,
                       positive=True, n_buffs=2, n_curses=0, lvl=0)
    v8 = _make_display("torid", "critical_chance", 0.5,
                       positive=True, n_buffs=2, n_curses=0, lvl=8)
    assert abs(v8 / v0 - 9.0) < 0.05


def test_deviation_rivensniper_style():
    """偏离中值展示为 RivenSniper 口径 ±10%。"""
    attrs = [{"url_name": "critical_chance", "positive": True,
              "value": _make_display("torid", "critical_chance", 1.0,
                                     positive=True, n_buffs=2, n_curses=0, lvl=8)},
             {"url_name": "critical_damage", "positive": True,
              "value": _make_display("torid", "critical_damage", 0.5,
                                     positive=True, n_buffs=2, n_curses=0, lvl=8)}]
    grades = {g.slug: g for g in grade_auction_item(_item("torid", attrs)).stats}
    assert abs(grades["critical_chance"].deviation - 10.0) < 0.2
    assert abs(grades["critical_damage"].deviation) < 0.2


def test_variant_disposition_fit():
    """按 Rubico Prime 倾向(0.7)填的数值：变体拟合成功并标注。"""
    base_disp = 0.95
    prime_disp = 0.7
    attrs = []
    for s in ("critical_chance", "critical_damage"):
        v = _make_display("rubico", s, 0.5, positive=True, n_buffs=2, n_curses=0, lvl=8)
        attrs.append({"url_name": s, "positive": True,
                      "value": round(v * prime_disp / base_disp, 1)})
    result = grade_auction_item(_item("rubico", attrs, lvl=8))
    assert result.fitted
    assert result.variant == "Rubico Prime"


def test_melee_damage_uses_melee_tag():
    """近战的基础伤害要映射到 WeaponMeleeDamageMod 基准值。"""
    assert rivendata.base_value("PlayerMeleeWeaponRandomModRare",
                                "WeaponDamageAmountMod") is not None
    attrs = [
        {"url_name": s, "positive": True,
         "value": _make_display("skana", s, 0.5, positive=True, n_buffs=2, n_curses=0, lvl=8)}
        for s in ("base_damage_/_melee_damage", "critical_chance")
    ]
    grades = {g.slug: g for g in grade_auction_item(_item("skana", attrs)).stats}
    assert grades["base_damage_/_melee_damage"].grade == "B"


def test_grade_boundaries():
    assert roll_to_grade(1.0) == "S"
    assert roll_to_grade(0.0) == "F"
    assert roll_to_grade(0.5) == "B"
    assert roll_to_grade(0.999) == "S"
