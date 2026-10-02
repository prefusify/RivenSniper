"""紫卡 OMG 链接的字段、数值和跨类别解码回归。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.plugins.riven_sniper import grading, riven_link  # noqa: E402

CAT = "LotusRifleRandomModRare"

# (b64, 造词名, 武器索引, rerolls, numBuffs, numCurses)
CARDS = [
    ("9b51zSsL1EkVt78mCp1r4/fpQ+1jxghk", "Sati-toxicron", 172, 537, 3, 1),
    ("9T8McYyz9yhr576saJWD3AFq8PhaTgAS", "Pura-hexacan", 11, 4, 3, 1),
    ("8b9zUfez7xpj0L9xurQr5DTdgEq7hAAI", "Vexi-critaada", 87, 2, 3, 1),
    ("0T2/6bl79GUSWT6/tFPFx7BgAMA=", "Satitis", 143, 3, 2, 1),
    ("8j8EGqA76BRW0z6ZfDlT28queD0iiABU", "Igni-geliton", 164, 21, 3, 1),
    ("8T7sNaB78i0NQL89F5Vj4sVZ5u0jzgQk", "Crita-satitis", 164, 265, 3, 1),
    ("xT8M8q0L4NEKnpt8BgAg", "Hexacron", 111, 8, 2, 0),
    ("8L99TP8r8SRLlr6nEOdz8O7VL1mDzAAc", "Crita-cronitin", 48, 7, 3, 1),
    ("9j45Vssz6iInUT9yYY47x8m/pKzzygAI", "Acri-gelibin", 158, 2, 3, 1),
    ("5b2zk+FD54eEmT9gDRG2izygAkA=", "Zeti-decitox", 22, 9, 3, 0),
    ("8j8KtCkz7T0u0b5Nn+i79+MdxayrTAAA", "Igni-gelitio", 149, 0, 3, 1),
    ("979cK7dD6bGV0L8+7laj42SsR11DRgAg", "Sati-critades", 168, 8, 3, 1),
]

EXACT_VALUE_CARDS = [
    (
        "LotusPistolRandomModRare",
        "8T5FIDIL9VI5Fz4lT8W70lSTGcubjgCI",
        "ballistica",
        115,
        34,
        [
            ("WeaponCritDamageMod", 0.1925056278705597, "C", 99.0),
            ("WeaponCritChanceMod", 0.8325739502906799, "A", 187.5),
            ("WeaponDamageAmountMod", 0.16143710911273956, "C", 239.9),
            ("WeaponZoomFovMod", 0.04035300388932228, "A+", -68.2),
        ],
    ),
    (
        "LotusRifleRandomModRare",
        "8T9NiKpb9TjuN70Tfedj9RLDSiibxgUk",
        "torid",
        19,
        329,
        [
            ("WeaponCritDamageMod", 0.8028665781021118, "A", 155.1),
            ("WeaponToxinDamageMod", 0.826399028301239, "A", 116.8),
            ("WeaponFireIterationsMod", 0.036008741706609726, "C-", 99.5),
            ("WeaponAmmoMaxMod", 0.8170807361602783, "C", -51.8),
        ],
    ),
    (
        "LotusRifleRandomModRare",
        "8L4MZ+kT3HKL178qrABL8Q/IKB0jxgAE",
        "burston",
        164,
        1,
        [
            ("WeaponCritChanceMod", 0.13711513578891754, "C", 189.1),
            ("WeaponCritDamageMod", 0.09724567085504532, "C-", 150.0),
            ("WeaponFireIterationsMod", 0.66668701171875, "B+", 126.4),
            ("WeaponSlashDamageMod", 0.5663529634475708, "B", -132.2),
        ],
    ),
]

LIVE_SCREENSHOT_CARDS = [
    (
        "S01", "LotusPistolRandomModRare",
        "+L9lITWT4h1Vur9J1iSb6KOhozx6TcA4", "akarius", "Lexi-argimag",
        143, 9, 7, 14, 3, "naramon",
        [1.6, -45.4, 1.26, -20.7],
    ),
    (
        "S02", "LotusRifleRandomModRare",
        "9b920isL9+xUQT8u3Mpj3+J4gr0UDgAA", "boltor", "Crita-toxitis",
        162, 16, 8, 0, 3, "naramon",
        [110.6, 185.5, 139.9, -41.6],
    ),
    (
        "S03", "LotusArchgunRandomModRare",
        "5r5339Vj4BM8mD3mVRtAmcLAGA==", "dual_decurion", "Arma-ampinok",
        6, 14, 6, 12, 1, "madurai",
        [41.7, 67.4, 1.8],
    ),
    (
        "S04", "LotusRifleRandomModRare",
        "974wvcwr9hJORz4dY7KD8OD5T2hyCUAC", "stinger", "Croni-satiata",
        14, 8, 5, 0, 2, "vazarin",
        [69.1, 53.1, 126.2, -59.8],
    ),
    (
        "S05", "LotusPistolRandomModRare",
        "8T88rCEL1dfF273bF5ED8t8N7OjrzQAw", "ocucor", "Acri-heracron",
        29, 15, 4, 12, 3, "naramon",
        [58.9, 85.4, 46.1, -62.1],
    ),
    (
        "S06", "PlayerMeleeWeaponRandomModRare",
        "9z3gFohT23Zbs791uyej8fu9u6jJpGAK", "lecta", "Igni-cronides",
        50, 13, 3, 5, 2, "vazarin",
        [26.4, 47.8, 51.2, -31.4],
    ),
    (
        "S07", "PlayerMeleeWeaponRandomModRare",
        "8D8pGOKT87kRJj7OE6Cb2axn+suxZEAI", "broken_war", "Locti-visicon",
        236, 11, 2, 4, 2, "vazarin",
        [61.1, 0.7, 1.16, -31.5],
    ),
    (
        "S08", "LotusShotgunRandomModRare",
        "wz8TtDU79Ea0EHwMIQCQ", "sobek", "Magnado",
        32, 12, 1, 9, 2, "vazarin",
        [16.1, 22.2],
    ),
    (
        "S09", "LotusRifleRandomModRare",
        "8L7Eqlhbwq/oQT9ZSaBj75cG4MmajAAA", "penta", "Acri-critatox",
        51, 10, 0, 0, 3, "naramon",
        [20.6, 11.4, 18.1, -5.6],
    ),
    (
        "S10", "LotusShotgunRandomModRare",
        "8L9TE8gT9ZDXl78QDc1j8bhXunvpGAAA", "steflos", "Acri-critacan",
        31, 9, 8, 0, 1, "madurai",
        [112.3, 112.8, 142.0, -86.2],
    ),
]


def test_decode_all_ground_truth():
    for b64, name, code, rerolls, nb, nc in CARDS:
        d = riven_link.decode_link(CAT, b64)
        assert d is not None, f"{name} 解码失败"
        assert d["riven_name"] == name, f"{name}: 造词名={d['riven_name']}"
        assert d["weapon_index"] == code, f"{name}: index={d['weapon_index']}"
        assert d["rerolls"] == rerolls, f"{name}: rerolls={d['rerolls']}"
        assert (d["n_buffs"], d["n_curses"]) == (nb, nc), f"{name}: nb/nc"
        assert len(d["stats"]) == nb + nc
        # buff 在前、curse 在后
        assert [s["is_curse"] for s in d["stats"]] == [False] * nb + [True] * nc
        # 评分档接线正确：buff 用 roll、curse 用 1-roll
        for s in d["stats"]:
            r = 1 - s["roll"] if s["is_curse"] else s["roll"]
            assert s["grade"] == grading.roll_to_grade(r)
            assert 0.0 <= s["roll"] <= 1.0


@pytest.mark.parametrize(
    "category,b64,slug,code,rerolls,expected", EXACT_VALUE_CARDS
)
def test_float32_roll_and_exact_dc_values(
    monkeypatch, category, b64, slug, code, rerolls, expected
):
    """31bit 正 float32 必须逐项还原 DC 卡面值；旧 22bit 截断算法过不了此测试。"""
    monkeypatch.setattr(riven_link, "weapon_slug", lambda _category, _code: slug)
    decoded = riven_link.decode_link(category, b64)
    assert decoded is not None
    assert (decoded["weapon_index"], decoded["rerolls"]) == (code, rerolls)
    assert len(decoded["stats"]) == len(expected)
    for stat, (ref, roll, grade, display) in zip(decoded["stats"], expected):
        assert stat["ref"] == ref
        assert stat["roll"] == pytest.approx(roll, abs=1e-12)
        assert stat["grade"] == grade
        assert stat["display"] == display


@pytest.mark.parametrize(
    "sample,category,b64,slug,name,index,mr,rank,rerolls,pol_code,polarity,"
    "expected_values",
    LIVE_SCREENSHOT_CARDS,
)
def test_live_screenshot_meta_polarity_and_variant_values(
    sample, category, b64, slug, name, index, mr, rank, rerolls,
    pol_code, polarity, expected_values,
):
    decoded = riven_link.decode_link(category, b64)
    assert decoded is not None, sample
    assert (
        decoded["weapon_slug"], decoded["riven_name"],
        decoded["weapon_index"],
        decoded["lvl_req"], decoded["lvl"], decoded["rerolls"],
        decoded["polarity_code"], decoded["polarity"],
    ) == (slug, name, index, mr, rank, rerolls, pol_code, polarity)
    for stat, expected in zip(decoded["stats"], expected_values):
        values = list(stat["display_variants"].values())
        if stat["ref"] in riven_link._FACTION_REFS:
            values = [value + 1 for value in values]
        assert expected in values, (sample, stat["ref"], values)


def test_weapon_index_widths_cover_every_supported_riven_category():
    assert riven_link._WEAPON_INDEX_BITS == {
        "LotusArchgunRandomModRare": 5,
        "LotusModularMeleeRandomModRare": 4,
        "LotusModularPistolRandomModRare": 3,
        "LotusPistolRandomModRare": 8,
        "LotusRifleRandomModRare": 8,
        "LotusShotgunRandomModRare": 6,
        "PlayerMeleeWeaponRandomModRare": 9,
    }


def test_native_weapon_tables_cover_every_canonical_weapon():
    table = riven_link._weapon_table()
    assert table["_meta"] == {
        "game_build": "2b7a678-9c20078e6c8bb2e3",
        "canonical_database": "weapons.json",
        "source": "Warframe runtime Riven type tables",
        "mapped_entries": 630,
        "canonical_weapons": 418,
        "ignored_entries": {
            "skill_weapon": 37,
            "event_object": 5,
            "internal_class": 24,
        },
    }
    assert {
        category: len(table[category])
        for category in riven_link._WEAPON_INDEX_BITS
    } == {
        "LotusArchgunRandomModRare": 21,
        "LotusModularMeleeRandomModRare": 11,
        "LotusModularPistolRandomModRare": 6,
        "LotusPistolRandomModRare": 173,
        "LotusRifleRandomModRare": 178,
        "LotusShotgunRandomModRare": 40,
        "PlayerMeleeWeaponRandomModRare": 267,
    }
    mapped_slugs = {
        entry["slug"]
        for category in riven_link._WEAPON_INDEX_BITS
        for entry in table[category]
        if entry["slug"] is not None
    }
    assert mapped_slugs == set(riven_link.rivendata.weapons())


def test_latest_real_weapon_paths_are_mapped_to_canonical_names():
    assert riven_link.weapon_slug("LotusRifleRandomModRare", 95) == "haalvu"
    assert riven_link.weapon_path("LotusRifleRandomModRare", 95) == (
        "/Lotus/Weapons/Sentients/SentOctaMiniGun/SentOctaMiniGun"
    )
    assert riven_link.rivendata.weapons()["haalvu"] == {
        "name_zh": "哈尔武",
        "name_en": "Haalvu",
        "group": "primary",
        "riven_type": "rifle",
        "disposition": 0.5,
        "mr": 8,
        "variant_dispositions": {"Haalvu": 0.5},
    }
    assert riven_link.weapon_slug("PlayerMeleeWeaponRandomModRare", 229) == (
        "pangolin_sword"
    )
    assert (
        riven_link.rivendata.weapons()["pangolin_sword"]["variant_dispositions"]
        ["Pangolin Prime"]
        == 1.05
    )
    assert riven_link.weapon_slug("PlayerMeleeWeaponRandomModRare", 125) == (
        "dark_dagger"
    )
    assert riven_link.weapon_path("PlayerMeleeWeaponRandomModRare", 125) == (
        "/Lotus/Weapons/Tenno/Melee/Dagger/DarkDaggerBase"
    )


@pytest.mark.parametrize(
    "internal_index,weapon_index,slug",
    [
        (27, 28, "sun_and_moon"),
        (149, 150, "sampotes"),
        (165, 166, "edun"),
        (204, 205, "syam"),
        (206, 207, "azothane"),
        (250, 251, "argo_and_vel"),
    ],
)
def test_duviri_drifter_objects_are_ignored_but_warframe_weapons_are_mapped(
    internal_index, weapon_index, slug,
):
    category = "PlayerMeleeWeaponRandomModRare"
    assert riven_link.weapon_slug(category, internal_index) is None
    assert riven_link.weapon_slug(category, weapon_index) == slug


def test_ghoulsaw_and_dark_split_sword_use_their_real_weapon_entries():
    category = "PlayerMeleeWeaponRandomModRare"
    assert riven_link.weapon_slug(category, 52) == "ghoulsaw"
    assert riven_link.weapon_slug(category, 69) == "ghoulsaw"
    assert riven_link.weapon_slug(category, 72) is None
    assert riven_link.weapon_slug(category, 201) is None
    assert riven_link.weapon_slug(category, 202) == "dark_split_sword_(dual_swords)"
    assert riven_link.weapon_slug(category, 203) is None


def test_hek_base_class_path_from_french_live_link_is_not_ignored():
    decoded = riven_link.decode_link(
        "LotusShotgunRandomModRare", "4b48fZFz8A0Mcj8PC6e+yKOAAAA="
    )
    assert decoded is not None
    assert (
        decoded["weapon_index"], decoded["weapon_path"],
        decoded["weapon_slug"], decoded["weapon_name"],
        decoded["lvl_req"], decoded["lvl"], decoded["rerolls"],
        decoded["riven_name"],
    ) == (
        36, "/Lotus/Weapons/Tenno/Shotgun/QuadShotgunBase",
        "hek", "海克 (Hek)", 10, 8, 0, "Igni-visitio",
    )


def test_client_path_ground_truth_for_latest_vectis_sample():
    decoded = riven_link.decode_link(
        CAT, "8T78G9oL883/Z78oejFj86nm7l1jjgB8"
    )
    assert decoded is not None
    assert (
        decoded["weapon_index"], decoded["weapon_path"],
        decoded["weapon_slug"], decoded["lvl_req"], decoded["lvl"],
        decoded["rerolls"], decoded["polarity"], decoded["riven_name"],
    ) == (
        172, "/Lotus/Weapons/Tenno/Rifle/TennoSniperRifle",
        "vectis", 14, 8, 31, "naramon", "Crita-satitis",
    )


@pytest.mark.parametrize(
    "b64,code,polarity,mark",
    [
        ("9b51zSsL1EkVt78mCp1r4/fpQ+1jxghk", 1, "madurai", "V"),
        ("8j8EGqA76BRW0z6ZfDlT28queD0iiABU", 2, "vazarin", "D"),
        ("8T7sNaB78i0NQL89F5Vj4sVZ5u0jzgQk", 3, "naramon", "—"),
    ],
)
def test_polarity_code_mapping(b64, code, polarity, mark):
    decoded = riven_link.decode_link(CAT, b64)
    assert (decoded["polarity_code"], decoded["polarity"], decoded["polarity_mark"]) == (
        code, polarity, mark,
    )


def test_tag_decoding_locked():
    """锁定词条种类解码（block[4:9]）——守望者四词条含 1 个 curse。"""
    d = riven_link.decode_link(CAT, "9b51zSsL1EkVt78mCp1r4/fpQ+1jxghk")
    refs = [(s["ref"], s["is_curse"]) for s in d["stats"]]
    assert refs == [
        ("WeaponToxinDamageMod", False),
        ("WeaponCritChanceMod", False),
        ("WeaponFireIterationsMod", False),
        ("WeaponClipMaxMod", True),
    ]


@pytest.mark.parametrize(
    "b64,expected_lvl",
    [
        ("8L99TP8r8SRLlr6nEOdz8O7VL1mDzAAc", 0),  # 4 词条 0级量子切割器
        ("8j8KtCkz7T0u0b5Nn+i79+MdxayrTAAA", 0),  # 4 词条 0级圣英
        ("0T2/6bl79GUSWT6/tFPFx7BgAMA=", 8),    # 3 词条满级诸葛
        ("xT8M8q0L4NEKnpt8BgAg", 8),            # 2 词条满级帕里斯
    ],
)
def test_current_rank_from_meta_for_all_layouts(b64, expected_lvl):
    decoded = riven_link.decode_link(CAT, b64)
    assert decoded is not None
    assert decoded["lvl"] == expected_lvl
    assert decoded["polarity_code"] in {1, 2, 3}


def test_mr16_is_a_direct_five_bit_field_after_weapon_index():
    decoded = riven_link.decode_link(CAT, "xT8M8q0L4NEKnpt8BgAg")
    assert decoded is not None
    assert decoded["weapon_index"] == 111
    assert decoded["weapon_slug"] == "paris"
    assert decoded["lvl_req"] == 16


def test_weapon_name_from_native_index_table():
    d = riven_link.decode_link(CAT, "8T7sNaB78i0NQL89F5Vj4sVZ5u0jzgQk")  # 伯斯顿
    assert d["weapon_slug"] == "burston"
    assert d["weapon_name"] and "Burston" in d["weapon_name"]


def test_recoil_curse_direction():
    """诸葛 Satitis：Recoil 显示为正(+加后坐力)但属 curse（base 为负）——靠头部 nb/nc
    正确切分，不能靠显示符号。"""
    d = riven_link.decode_link(CAT, "0T2/6bl79GUSWT6/tFPFx7BgAMA=")
    assert d["n_buffs"] == 2 and d["n_curses"] == 1
    curse = d["stats"][-1]
    assert curse["ref"] == "WeaponRecoilReductionMod" and curse["is_curse"]


def test_restored_combo_count_ref_has_name_and_negative_curse_value():
    ref = "WeaponMeleeComboPointsOnHitMod"
    assert riven_link._ref_names(ref) == (
        "连击数获取几率", "Chance to Gain Combo Count"
    )
    assert riven_link.rivendata.attribute_slug_from_ref(ref) == (
        "chance_to_gain_combo_count"
    )
    display = riven_link._abs_value(
        "PlayerMeleeWeaponRandomModRare", ref, 0.5, 8, 3, 1, True, 1.0,
    )
    assert display is not None and display < 0
