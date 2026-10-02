"""狙击配置的匹配与完全覆盖语义测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.plugins.riven_sniper import criteria as criteria_module  # noqa: E402
from src.plugins.riven_sniper.criteria import ANY_ATTRIBUTE, groups_match  # noqa: E402
from src.plugins.riven_sniper.grading import GradeResult, StatGrade  # noqa: E402
from src.plugins.riven_sniper.matcher import (  # noqa: E402
    config_subsumes,
    match_channel_card,
    match_config,
)


CC = "critical_chance"
CD = "critical_damage"
MS = "multishot"
FR = "fire_rate_/_attack_speed"
ZOOM = "zoom"
RECOIL = "recoil"
STATUS = "status_chance"
COMBO_GAIN = "chance_to_gain_combo_count"


def _auction(weapon="torid", pos=(CC, MS), neg=(), re_rolls=0):
    item = {
        "type": "riven",
        "weapon_url_name": weapon,
        "mod_rank": 8,
        "re_rolls": re_rolls,
        "attributes": (
            [{"url_name": slug, "positive": True, "value": 100.0} for slug in pos]
            + [{"url_name": slug, "positive": False, "value": -50.0} for slug in neg]
        ),
    }
    return {"id": "auction", "item": item}


def _config(**overrides):
    config = {
        "id": 1,
        "enabled": 1,
        "weapon": None,
        "wildcard": "all",
        "positives": [[CC], [MS]],
        "negatives": [],
        "zero_rerolls": False,
    }
    config.update(overrides)
    return config


def _grade_result(pos=(), neg=()):
    return GradeResult(stats=[
        *(StatGrade(slug, 0.0, True, None, grade) for slug, grade in pos),
        *(StatGrade(slug, 0.0, False, None, grade) for slug, grade in neg),
    ])


def test_exact_weapon_and_type_wildcard_scope():
    assert match_config(_config(weapon="torid", wildcard=None), _auction())
    assert not match_config(_config(weapon="rubico", wildcard=None), _auction())

    assert match_config(_config(wildcard="rifle"), _auction("torid"))
    assert not match_config(_config(wildcard="melee"), _auction("torid"))
    # WFM 将大型枪械标成 rifle，本地 group 仍必须优先判为空战。
    assert not match_config(_config(wildcard="rifle"), _auction("imperator"))
    assert match_config(_config(wildcard="archgun"), _auction("imperator"))


def test_positive_positions_are_and_and_each_group_is_or():
    config = _config(positives=[[CC, MS], [FR]])

    assert match_config(config, _auction(pos=(CC, FR)))
    assert match_config(config, _auction(pos=(MS, FR)))
    assert not match_config(config, _auction(pos=(CC, MS)))
    assert not match_config(config, _auction(pos=(CD, FR)))


def test_assignment_backtracks_instead_of_greedily_consuming_a_stat():
    # 若先让 CC/MS 消耗 CC，第二个 CC 位置将失败；正确分配是 MS + CC。
    groups = [[CC, MS], [CC]]
    assert groups_match(groups, (CC, MS))
    assert match_config(_config(positives=groups), _auction(pos=(CC, MS)))


def test_any_requires_a_real_unconsumed_stat():
    groups = [[ANY_ATTRIBUTE], [CC]]

    assert groups_match(groups, (CC, MS))
    assert not groups_match(groups, (CC,))
    assert not groups_match(groups, (MS, CD))
    assert groups_match([[ANY_ATTRIBUTE], [ANY_ATTRIBUTE]], (CC, MS))
    assert not groups_match([[ANY_ATTRIBUTE], [ANY_ATTRIBUTE]], (CC,))


def test_two_positive_positions_reject_a_three_positive_riven():
    two = _config(positives=[[CC], [MS]])
    three = _config(positives=[[CC], [MS], [CD]])

    assert match_config(two, _auction(pos=(CC, MS)))
    assert not match_config(two, _auction(pos=(CC, MS, CD)))
    assert match_config(three, _auction(pos=(CC, MS, CD)))
    assert not match_config(three, _auction(pos=(CC, MS)))


def test_no_negative_condition_means_the_riven_must_have_no_negative():
    config = _config(negatives=[])

    assert match_config(config, _auction(neg=()))
    assert not match_config(config, _auction(neg=(ZOOM,)))


def test_negative_any_and_or_require_one_matching_negative():
    any_negative = _config(negatives=[[ANY_ATTRIBUTE]])
    selected_negative = _config(negatives=[[ZOOM, RECOIL]])

    assert match_config(any_negative, _auction(neg=(STATUS,)))
    assert not match_config(any_negative, _auction(neg=()))
    assert not match_config(any_negative, _auction(neg=(ZOOM, RECOIL)))
    assert match_config(selected_negative, _auction(neg=(ZOOM,)))
    assert match_config(selected_negative, _auction(neg=(RECOIL,)))
    assert not match_config(selected_negative, _auction(neg=(STATUS,)))
    assert not match_config(selected_negative, _auction(neg=()))


def test_restored_combo_count_curse_matches_wfm_negative_attribute():
    config = _config(
        weapon="skana", wildcard=None, negatives=[[COMBO_GAIN]],
    )

    assert match_config(config, _auction("skana", neg=(COMBO_GAIN,)))
    assert not match_config(config, _auction("skana", neg=(ZOOM,)))


def test_zero_wash_is_strict_and_unrestricted_config_is_broader():
    zero_only = _config(zero_rerolls=True)
    unrestricted = _config(zero_rerolls=False)

    assert match_config(zero_only, _auction(re_rolls=0))
    assert not match_config(zero_only, _auction(re_rolls=1))
    missing = _auction()
    del missing["item"]["re_rolls"]
    assert not match_config(zero_only, missing)
    assert match_config(unrestricted, _auction(re_rolls=99))
    assert match_config(unrestricted, missing)


def test_disabled_non_riven_and_missing_attribute_slugs_do_not_match():
    assert not match_config(_config(enabled=0), _auction())

    non_riven = _auction()
    non_riven["item"]["type"] = "lich"
    assert not match_config(_config(), non_riven)

    malformed = _auction()
    malformed["item"]["attributes"][0]["url_name"] = None
    assert not match_config(_config(), malformed)


def test_channel_card_uses_the_same_config_matching_semantics():
    card = {
        "weapon_slug": "torid",
        "rerolls": 0,
        "stats": [
            {"ref": "WeaponCritChanceMod", "is_curse": False},
            {"ref": "WeaponFireIterationsMod", "is_curse": False},
            {"ref": "WeaponZoomFovMod", "is_curse": True},
        ],
    }
    config = _config(
        weapon="torid", wildcard=None, negatives=[[ZOOM]], zero_rerolls=True)

    assert match_channel_card(config, card)
    assert not match_channel_card(
        {**config, "weapon": "rubico"}, card)
    assert not match_channel_card(
        {**config, "positives": [[CC], [CD]]}, card)
    assert not match_channel_card(
        config, {**card, "rerolls": 1})


def test_positive_or_and_negative_minimum_ratings_are_bound_per_attribute():
    config = _config(
        positives=[[CC, MS], [FR]],
        positive_ratings=[{CC: "A", MS: "B+"}, {}],
        negatives=[[ZOOM]],
        negative_ratings=[{ZOOM: "A"}],
    )

    assert match_config(
        config,
        _auction(pos=(CC, FR), neg=(ZOOM,)),
        _grade_result(pos=((CC, "A"), (FR, "F")), neg=((ZOOM, "A"),)),
    )
    assert not match_config(
        config,
        _auction(pos=(CC, FR), neg=(ZOOM,)),
        _grade_result(pos=((CC, "B+"), (FR, "S")), neg=((ZOOM, "A+"),)),
    )
    assert match_config(
        config,
        _auction(pos=(MS, FR), neg=(ZOOM,)),
        _grade_result(pos=((MS, "B+"), (FR, "C-")), neg=((ZOOM, "S"),)),
    )
    assert not match_config(
        config,
        _auction(pos=(MS, FR), neg=(ZOOM,)),
        _grade_result(pos=((MS, "A"), (FR, "A")), neg=((ZOOM, "A-"),)),
    )


def test_unscorable_grades_fail_only_when_that_alternative_has_a_minimum():
    rated = _config(positive_ratings=[{CC: "F"}, {}])
    legacy = _config()
    result = _grade_result(pos=((CC, "X"), (MS, "?")))

    assert not match_config(rated, _auction(), result)
    assert match_config(legacy, _auction(), result)


def test_any_minimum_rating_applies_to_the_real_stat_it_consumes():
    config = _config(
        positives=[[ANY_ATTRIBUTE], [CC]],
        positive_ratings=[{ANY_ATTRIBUTE: "A"}, {}],
        negatives=[[ANY_ATTRIBUTE]],
        negative_ratings=[{ANY_ATTRIBUTE: "B+"}],
    )
    auction = _auction(pos=(CC, MS), neg=(ZOOM,))
    channel = {
        "weapon_slug": "torid",
        "rerolls": 0,
        "stats": [
            {"ref": "WeaponCritChanceMod", "is_curse": False, "grade": "C"},
            {"ref": "WeaponFireIterationsMod", "is_curse": False,
             "grade": "A"},
            {"ref": "WeaponZoomFovMod", "is_curse": True, "grade": "B+"},
        ],
    }

    assert match_config(
        config,
        auction,
        _grade_result(pos=((CC, "C"), (MS, "A")), neg=((ZOOM, "B+"),)),
    )
    assert match_channel_card(config, channel)
    assert not match_config(
        config,
        auction,
        _grade_result(pos=((CC, "S"), (MS, "A-")), neg=((ZOOM, "B+"),)),
    )
    assert not match_config(
        config,
        auction,
        _grade_result(pos=((CC, "C"), (MS, "A")), neg=((ZOOM, "B"),)),
    )
    channel["stats"][1]["grade"] = "A-"
    assert not match_channel_card(config, channel)


def test_multiple_rated_any_positions_backtrack_by_grade():
    config = _config(
        positives=[[ANY_ATTRIBUTE], [ANY_ATTRIBUTE]],
        positive_ratings=[{ANY_ATTRIBUTE: "A"}, {ANY_ATTRIBUTE: "B"}],
    )

    assert match_config(
        config,
        _auction(pos=(CC, MS)),
        _grade_result(pos=((CC, "B"), (MS, "A"))),
    )
    assert not match_config(
        config,
        _auction(pos=(CC, MS)),
        _grade_result(pos=((CC, "B"), (MS, "B"))),
    )


def test_wfm_and_channel_use_identical_positive_and_negative_rating_semantics():
    config = _config(
        weapon="torid", wildcard=None,
        positive_ratings=[{CC: "A"}, {MS: "B+"}],
        negatives=[[ZOOM]], negative_ratings=[{ZOOM: "A"}],
    )
    auction = _auction(pos=(CC, MS), neg=(ZOOM,))
    channel = {
        "weapon_slug": "torid",
        "rerolls": 0,
        "stats": [
            {"ref": "WeaponCritChanceMod", "is_curse": False, "grade": "A"},
            {"ref": "WeaponFireIterationsMod", "is_curse": False, "grade": "B+"},
            {"ref": "WeaponZoomFovMod", "is_curse": True, "grade": "A"},
        ],
    }
    result = _grade_result(
        pos=((CC, "A"), (MS, "B+")), neg=((ZOOM, "A"),))

    assert match_config(config, auction, result)
    assert match_channel_card(config, channel)

    result.stats[-1].grade = "A-"
    channel["stats"][-1]["grade"] = "A-"
    assert not match_config(config, auction, result)
    assert not match_channel_card(config, channel)


# ---- 配置完全覆盖（新配置的所有结果都必须已在旧配置范围内） ----


def test_type_wildcard_covers_specific_weapon_but_not_the_reverse():
    rifle = _config(wildcard="rifle")
    torid = _config(weapon="torid", wildcard=None)
    melee = _config(weapon="nikana", wildcard=None)

    assert config_subsumes(rifle, torid)
    assert not config_subsumes(torid, rifle)
    assert not config_subsumes(rifle, melee)


def test_all_scope_covers_types_and_exact_scopes():
    all_weapons = _config(wildcard="all")
    rifle = _config(wildcard="rifle")
    torid = _config(weapon="torid", wildcard=None)

    assert config_subsumes(all_weapons, rifle)
    assert config_subsumes(all_weapons, torid)
    assert not config_subsumes(rifle, all_weapons)


def test_positive_or_and_any_coverage_directions():
    exact = _config(positives=[[CC], [FR]])
    either = _config(positives=[[CC, MS], [FR]])
    any_stat = _config(positives=[[ANY_ATTRIBUTE], [FR]])

    assert config_subsumes(either, exact)
    assert not config_subsumes(exact, either)
    assert config_subsumes(any_stat, either)
    assert not config_subsumes(either, any_stat)


def test_position_count_is_part_of_coverage():
    two = _config(positives=[[CC], [MS]])
    three = _config(positives=[[CC], [MS], [CD]])

    assert not config_subsumes(two, three)
    assert not config_subsumes(three, two)


def test_negative_coverage_uses_the_same_or_and_any_semantics():
    no_negative = _config(negatives=[])
    zoom = _config(negatives=[[ZOOM]])
    selected = _config(negatives=[[ZOOM, RECOIL]])
    any_negative = _config(negatives=[[ANY_ATTRIBUTE]])

    assert config_subsumes(any_negative, selected)
    assert config_subsumes(selected, zoom)
    assert not config_subsumes(zoom, selected)
    assert not config_subsumes(no_negative, zoom)
    assert not config_subsumes(any_negative, no_negative)


def test_unrestricted_wash_covers_zero_wash_but_not_reverse():
    unrestricted = _config(zero_rerolls=False)
    zero_only = _config(zero_rerolls=True)

    assert config_subsumes(unrestricted, zero_only)
    assert not config_subsumes(zero_only, unrestricted)


def test_minimum_rating_coverage_keeps_unrestricted_and_threshold_directions():
    unrestricted = _config()
    minimum_f = _config(positive_ratings=[{CC: "F"}, {}])
    minimum_b = _config(positive_ratings=[{CC: "B"}, {}])
    minimum_a = _config(positive_ratings=[{CC: "A"}, {}])

    assert config_subsumes(unrestricted, minimum_b)
    # @F 仍排除 X/?，因此不能覆盖完全不限制评级的旧配置。
    assert not config_subsumes(minimum_f, unrestricted)
    assert not config_subsumes(minimum_b, unrestricted)
    assert config_subsumes(minimum_b, minimum_a)
    assert not config_subsumes(minimum_a, minimum_b)


def test_minimum_rating_coverage_respects_each_or_alternative():
    broader = _config(
        positives=[[CC, MS], [FR]],
        positive_ratings=[{CC: "B", MS: "A"}, {}],
    )
    narrower = _config(
        positives=[[CC, MS], [FR]],
        positive_ratings=[{CC: "A", MS: "A+"}, {}],
    )
    crossed = _config(
        positives=[[CC, MS], [FR]],
        positive_ratings=[{CC: "A", MS: "B"}, {}],
    )

    assert config_subsumes(broader, narrower)
    assert not config_subsumes(narrower, broader)
    assert not config_subsumes(broader, crossed)
    assert not config_subsumes(crossed, broader)


def test_any_minimum_rating_coverage_respects_scope_and_threshold():
    unrestricted_any = _config(
        positives=[[ANY_ATTRIBUTE], [FR]],
        positive_ratings=[],
    )
    any_b = _config(
        positives=[[ANY_ATTRIBUTE], [FR]],
        positive_ratings=[{ANY_ATTRIBUTE: "B"}, {}],
    )
    any_a = _config(
        positives=[[ANY_ATTRIBUTE], [FR]],
        positive_ratings=[{ANY_ATTRIBUTE: "A"}, {}],
    )
    exact_cc_a = _config(
        positives=[[CC], [FR]],
        positive_ratings=[{CC: "A"}, {}],
    )

    assert config_subsumes(unrestricted_any, any_b)
    assert not config_subsumes(any_b, unrestricted_any)
    assert config_subsumes(any_b, any_a)
    assert not config_subsumes(any_a, any_b)
    assert config_subsumes(any_a, exact_cc_a)
    assert not config_subsumes(exact_cc_a, any_a)


def test_minimum_rating_coverage_prunes_irrelevant_grade_cartesian(monkeypatch):
    original = criteria_module._rated_groups_match_normalized
    calls = 0

    def counting_match(groups, actual):
        nonlocal calls
        calls += 1
        return original(groups, actual)

    criteria_module._rated_groups_cover_cached.cache_clear()
    monkeypatch.setattr(
        criteria_module, "_rated_groups_match_normalized", counting_match)
    alternatives = [f"attribute_{index}" for index in range(25)]
    try:
        assert criteria_module.rated_groups_cover(
            [[ANY_ATTRIBUTE], [ANY_ATTRIBUTE], [ANY_ATTRIBUTE]],
            [],
            [alternatives, ["fixed_y"], ["fixed_z"]],
            [{slug: "A" for slug in alternatives}, {}, {}],
        )
        # 旧实现会对全部词条组合固定枚举 12^3 个评级状态，调用数超过
        # 七百万；现在只检查 b 可匹配的组合和真正不同的评级等价类。
        assert calls < 10_000
    finally:
        criteria_module._rated_groups_cover_cached.cache_clear()


def test_all_non_scope_conditions_must_also_cover_the_new_config():
    existing = _config(
        wildcard="rifle",
        positives=[[CC, MS], [FR]],
        negatives=[[ZOOM, RECOIL]],
        zero_rerolls=False,
    )
    new = _config(
        weapon="torid",
        wildcard=None,
        positives=[[CC], [FR]],
        negatives=[[ZOOM]],
        zero_rerolls=True,
    )

    assert config_subsumes(existing, new)
    assert not config_subsumes(new, existing)

    assert not config_subsumes(
        _config(wildcard="rifle", positives=[[CC], [FR]], negatives=[[ZOOM]],
                zero_rerolls=True),
        _config(weapon="torid", wildcard=None, positives=[[CC, MS], [FR]],
                negatives=[[ZOOM]], zero_rerolls=False),
    )
