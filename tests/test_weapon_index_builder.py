import json
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import build_riven_weapon_indices as builder  # noqa: E402


def test_every_unmapped_runtime_object_has_an_explicit_ignore_reason():
    table = json.loads(
        (ROOT / "data" / "riven_weapon_indices.json").read_text(encoding="utf-8")
    )
    counts = Counter(
        builder.ignored_path_reason(entry["path"])
        for category in builder.CATEGORY_BY_COUNT.values()
        for entry in table[category]
        if entry["slug"] is None
    )
    assert None not in counts
    assert dict(counts) == table["_meta"]["ignored_entries"]


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("/Lotus/Powersuits/Example/ExampleAbilityWeapon", "skill_weapon"),
        ("/Lotus/Types/Example/ExaltedExampleWeapon", "skill_weapon"),
        ("/Lotus/Weapons/Example/ConclaveExampleWeapon", "event_object"),
        ("/Lotus/Weapons/Tenno/Pistol/AutoPistolVariant", "internal_class"),
        ("/Lotus/Weapons/Example/FutureLoanerWeapon", "internal_class"),
    ],
)
def test_future_non_riven_object_patterns_stay_ignored(path, reason):
    assert builder.ignored_path_reason(path) == reason


@pytest.mark.parametrize(
    "path",
    [
        "/Lotus/Weapons/Sentients/NewActualWeapon/NewActualWeapon",
        "/Lotus/Weapons/Example/FutureWeaponBase",
        "/Lotus/Weapons/Example/FutureWeaponVariant",
    ],
)
def test_new_real_weapon_candidate_cannot_be_silently_ignored(path):
    tables = {category: [] for category in builder.CATEGORY_BY_COUNT.values()}
    tables["LotusRifleRandomModRare"] = [path]
    with pytest.raises(RuntimeError, match="禁止静默忽略"):
        builder.build_table("test-build", tables, {}, {})


def test_dark_dagger_family_base_is_mapped_instead_of_ignored():
    path = "/Lotus/Weapons/Tenno/Melee/Dagger/DarkDaggerBase"
    assert builder.PATH_OVERRIDES[path] == "dark_dagger"
    assert builder.ignored_path_reason(path) is None
