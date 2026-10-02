"""狙击配置跨群复制：规范语义幂等、覆盖和上限。

所有测试使用临时或内存 Store，绝不碰生产 ``sniper.db``。
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.plugins.riven_sniper import configops  # noqa: E402
from src.plugins.riven_sniper.criteria import ANY_ATTRIBUTE  # noqa: E402
from src.plugins.riven_sniper.store import Store  # noqa: E402


def _cfg(maxn=20):
    return types.SimpleNamespace(sniper_max_configs_per_group=maxn)


def _add(store, gid, weapon="torid", *, wildcard=None,
         positives=None, positive_ratings=None, negatives=None,
         negative_ratings=None, zero_rerolls=False):
    return store.add_config(
        gid,
        weapon=weapon,
        wildcard=wildcard,
        positives=positives or [["critical_chance"], ["multishot"]],
        positive_ratings=positive_ratings,
        negatives=negatives,
        negative_ratings=negative_ratings,
        zero_rerolls=zero_rerolls,
    )


def test_copy_configs_basic_and_idempotent(tmp_path):
    s = Store(tmp_path / "t.db")
    _add(s, 100, "torid")
    _add(s, 100, "nami_solo")
    assert configops.copy_configs(s, _cfg(), 100, 200) == (2, 0, 0, 2)
    assert len(s.list_configs(200)) == 2

    copied, skipped, capped, total = configops.copy_configs(s, _cfg(), 100, 200)
    assert (copied, skipped, capped, total) == (0, 2, 0, 2)
    assert len(s.list_configs(200)) == 2
    s.close()


def test_copy_normalizes_or_group_and_position_order(tmp_path):
    s = Store(tmp_path / "t.db")
    _add(
        s,
        100,
        positives=[
            ["multishot", "critical_chance"],
            ["fire_rate_/_attack_speed"],
        ],
    )
    _add(
        s,
        200,
        positives=[
            ["fire_rate_/_attack_speed"],
            ["critical_chance", "multishot"],
        ],
    )

    assert configops.copy_configs(s, _cfg(), 100, 200) == (0, 1, 0, 1)
    assert len(s.list_configs(200)) == 1
    s.close()


def test_copy_configs_respects_cap(tmp_path):
    s = Store(tmp_path / "t.db")
    for weapon in ("torid", "nami_solo", "boltor"):
        _add(s, 100, weapon)
    copied, skipped, capped, total = configops.copy_configs(
        s, _cfg(maxn=2), 100, 200
    )
    assert (copied, skipped, capped, total) == (2, 0, 1, 3)
    assert len(s.list_configs(200)) == 2
    s.close()


def test_copy_configs_zero_cap_means_unlimited(tmp_path):
    s = Store(tmp_path / "t.db")
    for weapon in ("torid", "nami_solo", "boltor"):
        _add(s, 100, weapon)

    assert configops.copy_configs(
        s, _cfg(maxn=0), 100, 200) == (3, 0, 0, 3)
    assert len(s.list_configs(200)) == 3
    s.close()


def test_copy_empty_source(tmp_path):
    s = Store(tmp_path / "t.db")
    assert configops.copy_configs(s, _cfg(), 100, 200) == (0, 0, 0, 0)
    s.close()


def test_copy_preserves_nested_groups_without_owner_metadata(tmp_path):
    s = Store(tmp_path / "t.db")
    positives = [
        ["critical_chance", "multishot"],
        [ANY_ATTRIBUTE],
    ]
    negatives = [["zoom", "recoil"]]
    _add(
        s,
        100,
        positives=positives,
        negatives=negatives,
        zero_rerolls=True,
    )
    assert configops.copy_configs(s, _cfg(), 100, 200) == (1, 0, 0, 1)

    copied = s.list_configs(200)[0]
    source = s.list_configs(100)[0]
    for field in (
        "positives", "positive_ratings", "negatives", "negative_ratings",
        "zero_rerolls",
    ):
        assert copied[field] == source[field]
    assert "creator_qq" not in copied
    s.close()


def test_copy_dedupes_semantics_and_keeps_existing_rules(tmp_path):
    s = Store(tmp_path / "t.db")
    _add(s, 100)
    _add(
        s, 100,
        positives=[["critical_damage"], ["fire_rate_/_attack_speed"]],
    )
    _add(s, 200)

    assert configops.copy_configs(s, _cfg(), 100, 200) == (1, 1, 0, 2)
    assert len(s.list_configs(200)) == 2
    s.close()


def test_copy_normal_config_dedupes_without_creator_domain(tmp_path):
    s = Store(tmp_path / "t.db")
    _add(s, 100)
    _add(s, 200)

    assert configops.copy_configs(s, _cfg(), 100, 200) == (0, 1, 0, 1)
    assert "creator_qq" not in s.list_configs(200)[0]
    s.close()


def test_copy_skips_exact_weapon_covered_by_target_type_wildcard(tmp_path):
    s = Store(tmp_path / "t.db")
    _add(s, 100, "torid", zero_rerolls=True)
    _add(
        s,
        200,
        weapon=None,
        wildcard="rifle",
        positives=[[ANY_ATTRIBUTE], [ANY_ATTRIBUTE]],
    )

    assert configops.copy_configs(s, _cfg(), 100, 200) == (0, 1, 0, 1)
    assert s.list_configs(200)[0]["wildcard"] == "rifle"
    s.close()


def test_copy_specific_weapon_does_not_cover_type_wildcard(tmp_path):
    s = Store(tmp_path / "t.db")
    _add(s, 100, weapon=None, wildcard="rifle")
    _add(s, 200, weapon="torid")

    assert configops.copy_configs(s, _cfg(), 100, 200) == (1, 0, 0, 1)
    assert len(s.list_configs(200)) == 2
    s.close()


def test_copy_merges_without_clobbering_existing(tmp_path):
    s = Store(tmp_path / "t.db")
    _add(s, 100, "torid")
    _add(s, 100, "nami_solo")
    existing_id = _add(s, 200, "nami_solo")

    copied, skipped, capped, total = configops.copy_configs(s, _cfg(), 100, 200)
    assert (copied, skipped, capped, total) == (1, 1, 0, 2)
    configs = s.list_configs(200)
    assert sorted(config["weapon"] for config in configs) == ["nami_solo", "torid"]
    assert s.get_config(existing_id, 200)["id"] == existing_id
    s.close()


def test_add_rejects_same_criteria_even_when_minimum_ratings_differ(tmp_path):
    s = Store(tmp_path / "t.db")
    _add(s, 100)

    ok, message, config_id = configops.add_config_checked(
        s, _cfg(), 100, "托里德 暴击率@A 多重射击@B+")

    assert not ok and config_id is None
    assert "配置已存在" in message
    assert len(s.list_configs(100)) == 1

    _add(
        s, 200,
        positive_ratings=[{"critical_chance": "A"}, {"multishot": "B+"}],
    )
    ok, message, config_id = configops.add_config_checked(
        s, _cfg(), 200, "托里德 暴击率 多重射击")
    assert not ok and config_id is None
    assert "配置已存在" in message
    assert len(s.list_configs(200)) == 1
    s.close()


def test_copy_preserves_ratings_but_never_replaces_same_criteria(tmp_path):
    s = Store(tmp_path / "t.db")
    source_ratings = [
        {"critical_chance": "A"}, {"multishot": "B+"},
    ]
    _add(s, 100, positive_ratings=source_ratings)

    assert configops.copy_configs(s, _cfg(), 100, 200) == (1, 0, 0, 1)
    assert s.list_configs(200)[0]["positive_ratings"] == source_ratings

    _add(
        s, 300,
        positive_ratings=[{"critical_chance": "S"}, {"multishot": "A"}],
    )
    existing_id = s.list_configs(300)[0]["id"]
    assert configops.copy_configs(s, _cfg(), 100, 300) == (0, 1, 0, 1)
    existing = s.get_config(existing_id, 300)
    assert existing["positive_ratings"] == [
        {"critical_chance": "S"}, {"multishot": "A"},
    ]
    s.close()
