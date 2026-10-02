"""从游戏 Riven 类型表生成完整武器索引表。

输入：
- ``RIVEN_TABLE_PATH`` 采集器输出的 JSONL（每类记录含 ``count`` 与 ``paths``）；
- 官方英文 ``ExportWeapons``；
- 项目 ``data/weapons.json`` canonical 武器库。

只用 ``weapons.json`` 的 ``name_en`` / ``variant_dispositions`` 归一到 canonical slug，
不读取 ``aliases.json``。默认 dry-run；加 ``--write`` 才更新目标文件。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CATEGORY_BY_COUNT = {
    21: "LotusArchgunRandomModRare",
    6: "LotusModularPistolRandomModRare",
    267: "PlayerMeleeWeaponRandomModRare",
    173: "LotusPistolRandomModRare",
    178: "LotusRifleRandomModRare",
    40: "LotusShotgunRandomModRare",
    11: "LotusModularMeleeRandomModRare",
}
PATH_OVERRIDES = {
    # 官方 ExportWeapons 暂未列出 Vinquibus 的近战模式；它与同目录下已命名的
    # TnBayonetRifleWeapon 是同一把组合武器的两个独立 Riven 家族。
    "/Lotus/Weapons/Tenno/Bayonet/TnBayonetMeleeWeapon": "vinquibus_melee",
    # 2026-07-13 法语交易频道实盘链接使用 QuadShotgunBase，而非已命名的
    # QuadShotgun；两者都是 Hek Riven 家族。不能仅因类名带 Base 就忽略。
    "/Lotus/Weapons/Tenno/Shotgun/QuadShotgunBase": "hek",
    # 暗黑匕首与绯红·暗黑匕首都继承该共同 Riven 家族基类。运行时表同时
    # 收录两个成品对象和这个 Base 对象；任何一个索引出现在链接中都应正名。
    "/Lotus/Weapons/Tenno/Melee/Dagger/DarkDaggerBase": "dark_dagger",
}
IGNORE_REASONS = ("skill_weapon", "event_object", "internal_class")
INTERNAL_CLASS_NAMES = {
    "AutoPistolVariant",
    "DarkSwordDaggerDuals",
    "DarkSwordDaggerSingle",
    "DuviriDrifterPistolWeapon",
    "GrnChemstrikeNoxRiflePlayer",
    "GrnMiniSawMeleeWeapon",
    "HandShotGunVariant",
    "HeavyPistolVariant",
    "LightGlaiveWeaponVariant",
    "LotusPistol",
    "PistolVariant",
    "VariantKatana",
}


def ignored_path_reason(path: str) -> str | None:
    """返回不可能对应正常 Riven 武器的运行时对象类别。"""
    leaf = path.rsplit("/", 1)[-1]
    if "/Lotus/Powersuits/" in path or leaf.startswith("Exalted"):
        return "skill_weapon"
    if (
        "Conclave" in path
        or leaf in {"VariantSnowBalls", "VariantTennoStars"}
        or "Xmas" in leaf
    ):
        return "event_object"
    if (
        "Loaner" in leaf
        or "/Friendly/PlayerControllable/" in path
        or "/Tenno/Gear/MiningLaser" in path
        or leaf == "LotusHandCompass"
        or ("/DaxDuviri" in path and leaf.endswith("PlayerWeapon"))
        or leaf in INTERNAL_CLASS_NAMES
    ):
        return "internal_class"
    return None


def load_runtime_tables(path: Path) -> tuple[str, dict[str, list[str]]]:
    build: str | None = None
    tables: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        count = record.get("count")
        category = CATEGORY_BY_COUNT.get(count)
        paths = record.get("paths")
        if category is None or not isinstance(paths, list) or len(paths) != count:
            continue
        record_build = record.get("build")
        if build is not None and record_build != build:
            raise RuntimeError(f"类型表混入多个游戏 build: {build}, {record_build}")
        build = record_build
        tables[category] = paths
    missing = set(CATEGORY_BY_COUNT.values()) - tables.keys()
    if missing:
        raise RuntimeError(f"缺少类型表: {', '.join(sorted(missing))}")
    if not build:
        raise RuntimeError("类型表没有游戏 build")
    return build, tables


def load_official_names(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    entries = payload.get("ExportWeapons") if isinstance(payload, dict) else payload
    return {
        entry["uniqueName"]: entry["name"]
        for entry in entries
        if entry.get("uniqueName") and entry.get("name")
    }


def build_table(
    build: str,
    runtime_tables: dict[str, list[str]],
    official_names: dict[str, str],
    weapons: dict[str, dict],
) -> dict[str, object]:
    exact_names: dict[str, set[str]] = defaultdict(set)
    variant_names: dict[str, set[str]] = defaultdict(set)
    for slug, weapon in weapons.items():
        exact_names[weapon["name_en"].casefold()].add(slug)
        for name in weapon.get("variant_dispositions", {}):
            variant_names[name.casefold()].add(slug)

    output: dict[str, object] = {
        "_meta": {
            "game_build": build,
            "canonical_database": "weapons.json",
            "source": "Warframe runtime Riven type tables",
        }
    }
    covered: set[str] = set()
    mapped_entries = 0
    ignored_entries = {reason: 0 for reason in IGNORE_REASONS}
    for category in CATEGORY_BY_COUNT.values():
        records = []
        for path in runtime_tables[category]:
            slug = PATH_OVERRIDES.get(path)
            if slug is None and (name := official_names.get(path)):
                candidates = exact_names.get(name.casefold(), set()) or variant_names.get(
                    name.casefold(), set()
                )
                if len(candidates) > 1:
                    raise RuntimeError(
                        f"正名不唯一 {path}: {', '.join(sorted(candidates))}"
                    )
                slug = next(iter(candidates), None)
            if slug is None:
                reason = ignored_path_reason(path)
                if reason is None:
                    raise RuntimeError(f"发现未分类的真实武器候选，禁止静默忽略: {path}")
                ignored_entries[reason] += 1
            if slug is not None:
                if slug not in weapons:
                    raise RuntimeError(f"{path} 映射到不存在的 canonical slug: {slug}")
                covered.add(slug)
                mapped_entries += 1
            records.append({"path": path, "slug": slug})
        output[category] = records

    expected = set(weapons)
    if covered != expected:
        raise RuntimeError(
            f"正名覆盖不完整: missing={sorted(expected - covered)}, "
            f"extra={sorted(covered - expected)}"
        )
    output["_meta"]["mapped_entries"] = mapped_entries
    output["_meta"]["canonical_weapons"] = len(covered)
    output["_meta"]["ignored_entries"] = ignored_entries
    return output


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tables", type=Path, required=True)
    parser.add_argument("--export-weapons", type=Path, required=True)
    parser.add_argument("--weapons", type=Path, default=ROOT / "data/weapons.json")
    parser.add_argument(
        "--out", type=Path, default=ROOT / "data/riven_weapon_indices.json"
    )
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    build, runtime_tables = load_runtime_tables(args.tables)
    official_names = load_official_names(args.export_weapons)
    weapons = json.loads(args.weapons.read_text(encoding="utf-8"))
    output = build_table(build, runtime_tables, official_names, weapons)
    serialized = json.dumps(output, ensure_ascii=False, indent=1) + "\n"
    current = args.out.read_text(encoding="utf-8") if args.out.is_file() else None
    status = "一致" if current == serialized else "需更新"
    meta = output["_meta"]
    print(
        f"build={build} 表项={sum(len(v) for k, v in output.items() if k != '_meta')} "
        f"已映射={meta['mapped_entries']} 正名={meta['canonical_weapons']} ({status})"
    )
    if args.write:
        args.out.write_text(serialized, encoding="utf-8", newline="\n")
        print(f"已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
