"""构建 data/ 目录：从 WFM v2 和 RivenParser 拉取武器/词条/基准值数据。

用法: uv run python scripts/fetch_data.py
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.plugins.riven_sniper.version import VERSION  # noqa: E402

WFM = "https://api.warframe.market/v2"
RIVEN_TAGS_URL = (
    "https://raw.githubusercontent.com/calamity-inc/warframe-riven-info/senpai/riven_tags.json"
)
WFCD_BASE = "https://raw.githubusercontent.com/WFCD/warframe-items/master/data/json"
WFCD_CATEGORIES = ["Primary", "Secondary", "Melee", "Arch-Gun", "SentinelWeapons"]
# 变体前缀/后缀词：变体名 = 基础名 + 这些词的组合
VARIANT_AFFIXES = {
    "prime", "wraith", "vandal", "prisma", "kuva", "tenet", "coda", "mara", "dex",
    "telos", "secura", "vaykor", "rakta", "sancti", "synoid", "carmine", "ceti", "mk1",
}
# 正式变体名没有完整包含基础武器名，不能用通用 token 规则识别的少数家族。
EXTRA_VARIANTS_BY_SLUG = {
    "pangolin_sword": {"Pangolin Prime"},
}
# WFCD 当前条目缺少这 3 把集团变体的 omegaAttenuation；它们仍与基础武器
# 共用紫卡，刷新数据时必须保留各自倾向，不能静默回退到基础武器。
VARIANT_DISPOSITION_OVERRIDES_BY_SLUG = {
    "akbolto": {"Telos Akbolto": 1.25},
    "boltace": {"Telos Boltace": 1.1},
    "boltor": {"Telos Boltor": 1.2},
}
ATTRIBUTE_ZH_OVERRIDES = {
    # WFM 当前简中名称是缺少主语的“的几率来获得连击数”。这个负面词条与
    # positiveOnly 的 Additional Combo Count Chance 是两个不同的 gameRef。
    "chance_to_gain_combo_count": "连击数获取几率",
}


def _tokens(name: str) -> frozenset[str]:
    # WFCD 个别变体把基础武器名复数化（Dual Decurion → Prisma Dual
    # Decurions）。仅用于家族匹配，统一去掉词尾 s，避免刷新数据时漏掉该变体。
    return frozenset(
        token[:-1] if token.endswith("s") else token
        for token in name.lower().replace("mk1-", "mk1 ").split()
    )


def _attribute_record(attribute: dict) -> dict:
    """把 WFM 词条投影为项目的稳定双语标准词条记录。"""
    i18n = attribute.get("i18n", {})
    slug = attribute["slug"]
    return {
        "game_ref": attribute.get("gameRef"),
        "name_zh": ATTRIBUTE_ZH_OVERRIDES.get(
            slug, (i18n.get("zh-hans") or {}).get("name")
        ),
        "name_en": (i18n.get("en") or {}).get("name"),
        "group": attribute.get("group"),
        "prefix": attribute.get("prefix"),
        "suffix": attribute.get("suffix"),
    }


HEADERS = {
    "User-Agent": f"rivensniper/{VERSION}",
    "Language": "zh-hans",
}


def fetch_json(client: httpx.Client, url: str):
    r = client.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def _write_json(path: Path, value) -> None:
    """完整写入同目录临时文件后原子替换目标文件。"""
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", delete=False,
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
        ) as stream:
            temp_path = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=1)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def main() -> int:
    DATA.mkdir(exist_ok=True)
    with httpx.Client() as client:
        weapons_raw = fetch_json(client, f"{WFM}/riven/weapons")["data"]
        attrs_raw = fetch_json(client, f"{WFM}/riven/attributes")["data"]
        items_raw = fetch_json(client, f"{WFM}/items")["data"]
        riven_tags = fetch_json(client, RIVEN_TAGS_URL)
        wfcd_items = []
        for cat in WFCD_CATEGORIES:
            wfcd_items += fetch_json(client, f"{WFCD_BASE}/{cat}.json")

    # WFCD 名字 -> 倾向（omegaAttenuation）
    wfcd_disp = {
        x["name"]: x["omegaAttenuation"]
        for x in wfcd_items
        if x.get("name") and x.get("omegaAttenuation")
    }

    weapons = {}
    for w in weapons_raw:
        i18n = w.get("i18n", {})
        zh = (i18n.get("zh-hans") or {}).get("name")
        en = (i18n.get("en") or {}).get("name")
        # 找同家族变体：变体名 = 基础名 token + 若干变体词
        variants = {}
        if en:
            base_tokens = _tokens(en)
            extra_variants = EXTRA_VARIANTS_BY_SLUG.get(w["slug"], set())
            for name, disp in wfcd_disp.items():
                vt = _tokens(name)
                if (
                    base_tokens <= vt and (vt - base_tokens) <= VARIANT_AFFIXES
                ) or name in extra_variants:
                    variants[name] = disp
            variants.update(
                VARIANT_DISPOSITION_OVERRIDES_BY_SLUG.get(w["slug"], {})
            )
        weapons[w["slug"]] = {
            "name_zh": zh,
            "name_en": en,
            "group": w.get("group"),
            "riven_type": w.get("rivenType"),
            "disposition": w.get("disposition"),
            "mr": w.get("reqMasteryRank"),
            "variant_dispositions": variants,
        }

    attributes = {}
    for a in attrs_raw:
        attributes[a["slug"]] = _attribute_record(a)

    # 全市场道具目录（捡漏功能：名称解析 + orders/recent 的 itemId 映射）
    market_items = {}
    for it in items_raw:
        i18n = it.get("i18n", {})
        market_items[it["slug"]] = {
            "id": it.get("id"),
            "zh": (i18n.get("zh-hans") or {}).get("name"),
            "en": (i18n.get("en") or {}).get("name"),
            "tags": it.get("tags") or [],
            "max_rank": max(0, int(it.get("maxRank") or 0)),
            "game_ref": it.get("gameRef"),
        }

    _write_json(DATA / "weapons.json", weapons)
    _write_json(DATA / "attributes.json", attributes)
    _write_json(DATA / "riven_values.json", riven_tags)
    _write_json(DATA / "market_items.json", {
        "fetched_at": int(time.time()), "items": market_items,
    })

    # ---- 校验 ----
    print(f"weapons: {len(weapons)}  attributes: {len(attributes)}")
    print(f"market items: {len(market_items)}")
    items_no_zh = [s for s, v in market_items.items() if not v["zh"]]
    if items_no_zh:
        print(f"WARN: {len(items_no_zh)} market items missing zh name, "
              f"e.g. {items_no_zh[:5]}")
    print(f"riven_tags categories: {list(riven_tags.keys())}")
    riven_types = sorted({w["riven_type"] for w in weapons.values() if w["riven_type"]})
    print(f"WFM rivenTypes: {riven_types}")

    no_zh = [s for s, w in weapons.items() if not w["name_zh"]]
    if no_zh:
        print(f"WARN: {len(no_zh)} weapons missing zh name, e.g. {no_zh[:5]}")

    # gameRef 必须能在至少一个 riven_tags 分类中找到，否则该词条无法评分
    all_tags = {t["tag"] for cat in riven_tags.values() for t in cat}
    missing = {s: a["game_ref"] for s, a in attributes.items() if a["game_ref"] not in all_tags}
    if missing:
        print(f"WARN: attributes with gameRef not in riven_tags: {missing}")
    else:
        print("OK: all attribute gameRefs covered by riven_tags")

    print("\nattribute slugs (for aliases.json):")
    for s, a in sorted(attributes.items()):
        print(f"  {s:45s} {a['game_ref'] or '-':40s} {a['name_zh'] or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
