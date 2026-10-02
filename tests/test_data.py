import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import fetch_data  # noqa: E402
from src.plugins.riven_sniper import marketdata, rivendata  # noqa: E402
from src.plugins.riven_sniper.criteria import ANY_ATTRIBUTE  # noqa: E402


def test_attribute_aliases_point_to_known_standard_stats():
    attrs = json.loads((ROOT / "data" / "attributes.json").read_text(encoding="utf-8"))
    aliases = json.loads((ROOT / "data" / "aliases.json").read_text(encoding="utf-8"))

    known_targets = set(attrs) | {ANY_ATTRIBUTE}
    unknown_alias_targets = sorted(
        set(aliases["attributes"].values()) - known_targets)
    unknown_display_targets = sorted(
        set(aliases["attribute_display"]) - known_targets)

    assert unknown_alias_targets == []
    assert unknown_display_targets == []


def test_combo_count_stats_are_distinct_and_survive_data_refresh():
    attrs = json.loads((ROOT / "data" / "attributes.json").read_text(encoding="utf-8"))

    combo = attrs["chance_to_gain_combo_count"]
    extra = attrs["chance_to_gain_extra_combo_count"]
    assert combo == {
        "game_ref": "WeaponMeleeComboPointsOnHitMod",
        "name_zh": "连击数获取几率",
        "name_en": "Chance to Gain Combo Count",
        "group": "melee",
        "prefix": "",
        "suffix": "",
    }
    assert combo["game_ref"] != extra["game_ref"]

    # 上游简中当前是残句；刷新器必须恢复项目采用的完整正名，而不是再次删掉词条。
    upstream = {
        "slug": "chance_to_gain_combo_count",
        "gameRef": "WeaponMeleeComboPointsOnHitMod",
        "i18n": {
            "zh-hans": {"name": "的几率来获得连击数"},
            "en": {"name": "Chance to Gain Combo Count"},
        },
        "group": "melee",
        "prefix": "",
        "suffix": "",
    }
    assert fetch_data._attribute_record(upstream) == combo


def test_pluralized_weapon_variant_stays_in_same_family():
    base = fetch_data._tokens("Dual Decurion")
    variant = fetch_data._tokens("Prisma Dual Decurions")

    assert base <= variant
    assert variant - base == {"prisma"}


def test_telos_variant_dispositions_survive_data_refresh():
    weapons = rivendata.weapons()

    for slug, expected in (
            fetch_data.VARIANT_DISPOSITION_OVERRIDES_BY_SLUG.items()):
        variants = weapons[slug]["variant_dispositions"]
        assert expected.items() <= variants.items()


def test_name_sources_are_complete_and_localized():
    """受控词库中的武器、词条和道具均有中英文名称。"""
    assert all(w.get("name_zh") and w.get("name_en")
               for w in rivendata.weapons().values())
    assert all(a.get("name_zh") and a.get("name_en")
               for a in rivendata.attributes().values())
    assert all(item.get("zh") and item.get("en")
               for item in marketdata.items().values())

    assert rivendata.weapon_name("torid", "zh") == "托里德"
    assert rivendata.weapon_name("torid", "en") == "Torid"
    assert rivendata.attribute_name("critical_chance", "zh") == "暴击率"
    assert rivendata.attribute_name("critical_chance", "en") == "Critical Chance"
    assert rivendata.attribute_name(ANY_ATTRIBUTE, "zh") == "任意"
    assert rivendata.attribute_name(ANY_ATTRIBUTE, "en") == "Any"
    assert rivendata.attribute_syntax_name(ANY_ATTRIBUTE, "zh") == "任意"
    assert rivendata.attribute_syntax_name(ANY_ATTRIBUTE, "en") == "Any"


def test_channel_attribute_refs_reuse_standard_attribute_catalog():
    values = {
        entry["tag"]
        for entries in rivendata.riven_values().values()
        for entry in entries
    }
    unresolved = {ref for ref in values if rivendata.attribute_slug_from_ref(ref) is None}
    assert unresolved == set()
