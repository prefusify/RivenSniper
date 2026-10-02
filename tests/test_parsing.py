"""狙击添加命令的当前语法测试。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.plugins.riven_sniper import rivendata  # noqa: E402
from src.plugins.riven_sniper.criteria import (  # noqa: E402
    ANY_ATTRIBUTE,
    normalized_config,
)
from src.plugins.riven_sniper.formatter import format_config_command  # noqa: E402
from src.plugins.riven_sniper.parsing import ParseError, parse_add  # noqa: E402


def test_spaces_are_and_and_slash_is_or():
    parsed = parse_add("托里德 暴击/多重 射速")

    assert parsed.weapon == "torid"
    assert parsed.wildcard is None
    assert parsed.positives == [
        ["critical_chance", "multishot"],
        ["fire_rate_/_attack_speed"],
    ]
    assert parsed.negatives == []


def test_plus_is_only_an_alias_for_space():
    with_spaces = parse_add("托里德 暴击/多重 射速 -变焦/后坐 0洗")
    with_pluses = parse_add("托里德+暴击/多重+射速+-变焦/后坐+0洗")

    assert with_pluses == with_spaces
    assert with_spaces.negatives == [["recoil", "zoom"]]
    assert with_spaces.zero_rerolls is True


def test_minimum_ratings_bind_each_or_alternative_and_roundtrip():
    parsed = parse_add(
        "托里德 暴击率@A+/多重射击@B+ 射速@C+ -变焦@A 0洗")

    assert parsed.positive_ratings == [
        {"critical_chance": "A+", "multishot": "B+"},
        {"fire_rate_/_attack_speed": "C+"},
    ]
    assert parsed.negative_ratings == [{"zoom": "A"}]

    command = format_config_command(normalized_config(vars(parsed)))
    reparsed = parse_add(command.split(" ", 1)[1])
    assert normalized_config(vars(reparsed)) == normalized_config(vars(parsed))


def test_rating_plus_is_not_consumed_as_the_existing_and_separator():
    spaced = parse_add("托里德 暴击率@A+ 多重射击@B+ -变焦@C+")
    plus_separated = parse_add(
        "托里德+暴击率@A++多重射击@B++-变焦@C+")

    assert plus_separated == spaced
    assert spaced.positive_ratings == [
        {"critical_chance": "A+"}, {"multishot": "B+"},
    ]
    assert spaced.negative_ratings == [{"zoom": "C+"}]


@pytest.mark.parametrize(
    "rating",
    ["S", "A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "F"],
)
def test_every_supported_minimum_rating_parses(rating):
    parsed = parse_add(f"托里德 暴击率@{rating} 多重射击")
    assert parsed.positive_ratings[0] == {"critical_chance": rating}


@pytest.mark.parametrize("rating", ["X", "?", "D", "S+"])
def test_unsupported_minimum_ratings_are_rejected(rating):
    with pytest.raises(ParseError, match="最低评级"):
        parse_add(f"托里德 暴击率@{rating} 多重射击")


def test_any_position_minimum_rating_roundtrips_on_both_sides():
    parsed = parse_add("托里德 任意@A 暴击率 -任意@B+")

    assert parsed.positive_ratings == [{ANY_ATTRIBUTE: "A"}]
    assert parsed.negative_ratings == [{ANY_ATTRIBUTE: "B+"}]

    command = format_config_command(normalized_config(vars(parsed)))
    assert "any@A" in command
    assert "-any@B+" in command
    reparsed = parse_add(command.split(" ", 1)[1])
    assert normalized_config(vars(reparsed)) == normalized_config(vars(parsed))

    english = parse_add("torid any@A cc -any@B+")
    assert normalized_config(vars(english)) == normalized_config(vars(parsed))


@pytest.mark.parametrize(
    "duplicate_group",
    [
        "暴击率@A/暴击率@B",
        "暴击率@A/暴击率",
        "暴击率@A/cc@B",
    ],
)
def test_each_or_position_rejects_alternatives_resolving_to_same_slug(
        duplicate_group):
    with pytest.raises(ParseError, match="同一 OR 位置.*同一词条"):
        parse_add(f"托里德 {duplicate_group} 多重射击")


def test_same_slug_can_appear_in_different_satisfiable_or_positions():
    parsed = parse_add("托里德 暴击率/多重射击 暴击率/射速")

    assert len(parsed.positives) == 2
    assert sum("critical_chance" in group for group in parsed.positives) == 2


def test_restored_combo_count_curse_parses_by_canonical_name_and_alias():
    canonical = parse_add("skana 暴击率 基础伤害 -连击数获取几率")
    alias = parse_add("skana 暴击率 基础伤害 -cgc")

    assert canonical.negatives == [["chance_to_gain_combo_count"]]
    assert alias == canonical


def test_fullwidth_and_chinese_or_separators_are_input_aliases():
    parsed = parse_add("托里德 ＋暴击、多重 ＋射速 －变焦／后坐 ０洗")

    assert parsed.positives == [
        ["critical_chance", "multishot"],
        ["fire_rate_/_attack_speed"],
    ]
    assert parsed.negatives == [["recoil", "zoom"]]
    assert parsed.zero_rerolls is True


def test_any_is_a_real_independent_position_on_both_sides():
    parsed = parse_add("托里德 任意 任意 -任意")

    assert parsed.positives == [[ANY_ATTRIBUTE], [ANY_ATTRIBUTE]]
    assert parsed.negatives == [[ANY_ATTRIBUTE]]

    english_alias = parse_add("托里德 any 暴击 -any")
    assert english_alias.positives == [[ANY_ATTRIBUTE], ["critical_chance"]]
    assert english_alias.negatives == [[ANY_ATTRIBUTE]]

    with pytest.raises(ParseError, match="任意必须单独占"):
        parse_add("托里德 任意/暴击 多重")


@pytest.mark.parametrize(
    ("text", "count"),
    [
        ("托里德 暴击 多重", 2),
        ("托里德 暴击/多重 射速", 2),
        ("托里德 任意 暴击 多重", 3),
    ],
)
def test_exactly_two_or_three_positive_positions_are_accepted(text, count):
    assert len(parse_add(text).positives) == count


@pytest.mark.parametrize(
    "text",
    [
        "托里德 暴击",
        "托里德 暴击 多重 射速 伤害",
    ],
)
def test_one_or_four_positive_positions_are_rejected(text):
    with pytest.raises(ParseError, match="2 至 3"):
        parse_add(text)


def test_or_counts_as_one_position_and_multiple_negative_positions_are_rejected():
    assert len(parse_add("托里德 暴击/多重 射速").positives) == 2

    with pytest.raises(ParseError, match="最多设置 1 个负词条位置"):
        parse_add("托里德 暴击 多重 -变焦 -后坐")


def test_slash_must_have_an_attribute_on_each_side():
    # 分隔符两侧的空格允许作为容错，但真正的空备选不允许。
    assert len(parse_add("托里德 暴击 / 多重 射速").positives) == 2
    for text in ("托里德 /暴击 多重", "托里德 暴击 多重 -变焦/",
                 "托里德 暴击//多重 射速"):
        with pytest.raises(ParseError, match="/ 两侧"):
            parse_add(text)


def test_conflicting_positive_positions_are_rejected():
    with pytest.raises(ParseError, match="互相冲突"):
        parse_add("托里德 暴击 暴击")

    # OR 位置仍有可行的一对一分配：第一位可选多重，第二位选暴击。
    assert parse_add("托里德 暴击/多重 暴击").positives == [
        ["critical_chance"],
        ["critical_chance", "multishot"],
    ]


def test_only_zero_wash_is_supported():
    parsed = parse_add("托里德 暴击 多重 0洗")
    assert parsed.zero_rerolls is True
    assert parse_add('Torid "Critical Chance" Multishot unrolled').zero_rerolls is True
    assert parse_add("托里德 暴击 多重").zero_rerolls is False


@pytest.mark.parametrize(
    "deleted",
    [
        "-无",
        "-有",
        "排除触发",
        "!触发",
        "价格500",
        "<500",
        "极性v",
        "买断",
        "buyout",
        "未洗",
        "已洗",
        "洗练0-0",
        "1洗",
        "2+1",
        "2-1",
        "2正1负",
        "两正一负",
        "@我",
    ],
)
def test_deleted_syntax_always_enters_parse_error_flow(deleted):
    with pytest.raises(ParseError):
        parse_add(f"托里德 暴击 多重 {deleted}")


def test_bare_number_is_not_a_hidden_price_alias():
    with pytest.raises(ParseError, match="不支持价格"):
        parse_add("托里德 暴击 多重 500")


def test_weapon_scope_and_multiword_weapon_name():
    exact = parse_add("dual toxocyst 暴击 多重")
    wildcard = parse_add("步枪 暴击 多重")

    assert (exact.weapon, exact.wildcard) == ("dual_toxocyst", None)
    assert (wildcard.weapon, wildcard.wildcard) == (None, "rifle")


def test_unknown_weapon_and_attribute_offer_useful_errors():
    with pytest.raises(ParseError) as weapon_error:
        parse_add("托里得 暴击 多重")
    assert "托里得" in str(weapon_error.value)
    assert "托里德" in str(weapon_error.value)

    with pytest.raises(ParseError, match="不认识正词条"):
        parse_add("托里德 暴机 多重")


@pytest.mark.parametrize("language", ["zh", "en"])
def test_every_catalog_name_formats_to_a_reusable_standard_command(language):
    """全部武器、类型和词条都必须能从展示命令无损解析回来。"""
    attributes = list(rivendata.attributes())
    base_groups = [[attributes[0]], [attributes[1]]]

    for weapon in rivendata.weapons():
        config = {
            "weapon": weapon, "wildcard": None, "positives": base_groups,
            "negatives": [], "zero_rerolls": False,
        }
        command = format_config_command(config, language)
        assert command.isascii(), command
        parsed = parse_add(command.split(" ", 1)[1])
        assert parsed.weapon == weapon, command

    for wildcard in ("all", "rifle", "shotgun", "pistol", "melee",
                     "archgun", "kitgun", "zaw"):
        config = {
            "weapon": None, "wildcard": wildcard, "positives": base_groups,
            "negatives": [], "zero_rerolls": False,
        }
        command = format_config_command(config, language)
        assert command.isascii(), command
        parsed = parse_add(command.split(" ", 1)[1])
        assert parsed.wildcard == wildcard, command

    for attribute in attributes:
        other = attributes[1] if attribute == attributes[0] else attributes[0]
        config = {
            "weapon": "torid", "wildcard": None,
            "positives": [[attribute], [other]],
            "negatives": [[attribute]], "zero_rerolls": True,
        }
        command = format_config_command(config, language)
        assert command.isascii(), command
        parsed = parse_add(command.split(" ", 1)[1])
        assert normalized_config(vars(parsed)) == normalized_config(config), command


def test_standard_display_does_not_depend_on_editable_aliases(monkeypatch):
    empty_aliases = {
        "attributes": {}, "wildcards": {}, "weapons": {},
    }
    monkeypatch.setattr(rivendata, "aliases", lambda: empty_aliases)

    for wildcard in ("all", "rifle", "shotgun", "pistol", "melee",
                     "archgun", "kitgun", "zaw"):
        config = {
            "weapon": None, "wildcard": wildcard,
            "positives": [
                ["base_damage_/_melee_damage"],
                ["fire_rate_/_attack_speed"],
            ],
            "negatives": [], "zero_rerolls": False,
        }
        command = format_config_command(config)
        parsed = parse_add(command.split(" ", 1)[1])
        assert normalized_config(vars(parsed)) == normalized_config(config), command


def test_config_command_uses_shortest_registered_english_alias(monkeypatch):
    from src.plugins.riven_sniper.command_meta import ACTIVE_ALIASES, ACTIVE_COMMAND_NAMES

    monkeypatch.setitem(ACTIVE_COMMAND_NAMES, "sniper.add", "监听添加")
    monkeypatch.setitem(ACTIVE_ALIASES, "sniper.add", ["sniper", "ss", "加监听"])
    command = format_config_command({
        "weapon": "torid", "wildcard": None,
        "positives": [["critical_chance"], ["multishot"]],
        "negatives": [], "zero_rerolls": False,
    })

    assert command == "ss Torid cc ms"
    with pytest.raises(ParseError, match="ss Torid"):
        parse_add("")


@pytest.mark.parametrize("language", ["zh", "en"])
def test_sniper_help_examples_are_short_and_executable(language):
    with pytest.raises(ParseError) as error:
        parse_add("", locale=language)
    examples = [line for line in str(error.value).splitlines() if line.startswith("s ")]
    assert len(examples) == 2
    for example in examples:
        assert example.isascii()
        parsed = parse_add(example.split(" ", 1)[1], locale=language)
        assert parsed.weapon == "torid" and len(parsed.positives) == 2
    assert "cc@A/ms@B" in examples[1]
