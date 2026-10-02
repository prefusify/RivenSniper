"""Warframe 游戏内紫卡链接（OMG）离线解码 → 词条/评分档/洗数/造词名。

数值公式、评分档阈值对齐 calamity-inc/warframe-riven-info 的 RivenParser.js；
链接位布局由游戏链接与 Discord 解码结果逐字段配对验证（见 tests/test_riven_link.py）。

链接形如 ``[OMG-<类别>:<base64>]``，<类别> 即 riven_values.json 的键
（LotusRifleRandomModRare 等）。base64 解码后按 MSB 位流解析：

  bits[0:4]  头： 0b11 | (numBuffs-2)<<1 | numCurses    （nb∈{2,3}, nc∈{0,1}）
  stats 区   从 bit4 开始，每词条 36bit，buff 在前、curse 在后：
             [0:5] 词条索引 · [5:36] 正 float32 的低 31bit（符号位省略）
  meta 区    stats 后为 8bit 不透明字段、1bit 武器选择器、类别定宽的武器索引
             （Archgun/Zaw/Kitgun/手枪/步枪/霰弹/近战分别为 5/4/3/8/8/6/9bit）、
             5bit 最低 MR、2bit 未知、2bit 极性码、4bit 当前等级、2bit 未知、
             10bit 洗数；尾部剩余位尚不解释。

词条种类索引 = 该类别在 riven_values.json 中的 list 顺序。数值/评分档复用
grading.py（同源 RivenParser）。武器索引走客户端原生 Riven 类型表，并只映射到
data/weapons.json 中的 canonical slug；查不到则只显示大类。
"""
from __future__ import annotations

import base64
import binascii
import json
import struct

from . import grading, rivendata
from .chat_message import RIVEN_LINK_RE

# 保留原有公开名称；采集器和解码器共享同一个严格链接规则。
OMG_RE = RIVEN_LINK_RE

_BLOCK = 36

# 客户端原生武器表索引的位宽。此前所谓“类别定宽 meta 前导 + 11bit 武器/MR16
# 组合码”实际跨字段取位：类别定宽部分就是索引宽度，11bit 又混入选择器、索引和
# MR 高位。2026-07-13 从游戏解析器及运行时类型表还原出真实边界。
_WEAPON_INDEX_BITS = {
    "LotusArchgunRandomModRare": 5,
    "LotusModularMeleeRandomModRare": 4,
    "LotusModularPistolRandomModRare": 3,
    "LotusPistolRandomModRare": 8,
    "LotusRifleRandomModRare": 8,
    "LotusShotgunRandomModRare": 6,
    "PlayerMeleeWeaponRandomModRare": 9,
}

# RivenParser.valueToDisplayValue 的两个特殊取整组
_FACTION_REFS = grading._FACTION_REFS
_COMBO_RANGE_REFS = {
    "WeaponPunctureDepthMod",
    "WeaponMeleeComboInitialBonusMod",
    "ComboDurationMod",
    "WeaponMeleeRangeIncMod",
}

# 游戏内部 2bit 枚举。紫卡只会分配 Madurai/Vazarin/Naramon；0 保留为未知，
# 不能套用普通 Mod 可用的 Zenurik。1/2/3 由 2026-07-13 样本锁定为 V/D/横杠。
_POLARITY_BY_CODE = {
    0: (None, "?"),
    1: ("madurai", "V"),
    2: ("vazarin", "D"),
    3: ("naramon", "—"),
}

# ---- 位流基元 ----
def _bits(b64: str) -> list[int]:
    raw = base64.b64decode(b64 + "=" * ((-len(b64)) % 4))
    return [(byte >> i) & 1 for byte in raw for i in range(7, -1, -1)]


def _gb(bits: list[int], start: int, width: int) -> int:
    v = 0
    for i in range(width):
        v = (v << 1) | bits[start + i]
    return v


# ---- 静态映射（随 rivendata 缓存失效）----
def _category_zh(category: str) -> str:
    """紫卡类别键 -> 大类中文（步枪/手枪/…）。"""
    for rtype, cat in rivendata.RIVEN_CATEGORY.items():
        if cat == category:
            return rivendata.WILDCARD_CATEGORY_ZH.get(rtype, rtype)
    return "紫卡"


def _ref_names(game_ref: str) -> tuple[str, str]:
    """词条 game_ref -> (中文, 英文)。查不到回退 game_ref 本身。"""
    slug = rivendata.attribute_slug_from_ref(game_ref)
    if slug is not None:
        return (
            rivendata.attribute_name(slug, "zh"),
            rivendata.attribute_name(slug, "en"),
        )
    return (game_ref, game_ref)


def _to_display(game_ref: str, value: float) -> float:
    """RivenParser.valueToDisplayValue：物理值 -> 卡面显示值。"""
    if game_ref in _FACTION_REFS:
        return round(value * 100) / 100          # 乘数，如 1.56 / 0.63
    if game_ref in _COMBO_RANGE_REFS:
        return round(value * 10) / 10
    return round(value * 1000) / 10              # 百分比，截到 0.1


def _abs_value(category, game_ref, roll, lvl, nb, nc, is_curse, dispo):
    """正算卡面显示值（= RivenParser.parseRiven）。dispo/base 缺失时返回 None。"""
    base = rivendata.base_value(category, game_ref)
    if base is None or dispo is None:
        return None
    atten = 1.5 * dispo * 10
    lerp = 0.9 + 0.2 * roll
    if not is_curse:
        v = (base * atten * (1.25 ** nc) * lerp
             * grading.NUM_BUFFS_ATTEN[min(nb, 5)] * (lvl + 1))
    else:
        v = (grading._curse_base_value(game_ref, base) * atten * lerp
             * grading.NUM_BUFFS_CURSE_ATTEN[min(nb, 5)]
             * grading.NUM_BUFFS_ATTEN[min(nc, 5)] * (lvl + 1))
    return _to_display(game_ref, v)


# ---- 客户端武器索引 -> canonical slug ----
_weapon_index_cache: tuple[float, dict] | None = None


def _weapon_table() -> dict:
    """{category: [{path, slug}, ...]}，随文件 mtime 热重载。"""
    global _weapon_index_cache
    path = rivendata.DATA_DIR / "riven_weapon_indices.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if _weapon_index_cache is None or _weapon_index_cache[0] != mtime:
        try:
            _weapon_index_cache = (
                mtime, json.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError):
            _weapon_index_cache = (mtime, {})
    return _weapon_index_cache[1]


def _weapon_record(category: str, index: int | None) -> dict:
    if index is None:
        return {}
    table = _weapon_table().get(category, [])
    if not 0 <= index < len(table):
        return {}
    record = table[index]
    return record if isinstance(record, dict) else {}


def weapon_slug(category: str, index: int | None) -> str | None:
    if index is None:
        return None
    return _weapon_record(category, index).get("slug")


def weapon_path(category: str, index: int | None) -> str | None:
    if index is None:
        return None
    return _weapon_record(category, index).get("path")


# ---- 造词名（= RivenParser.parseRiven 命名规则；仅用 buff）----
def riven_name(category: str, stats: list[dict]) -> str:
    """由稳定词条数据生成游戏内紫卡名。"""
    entries = rivendata.riven_values().get(category, [])
    by_tag = {str(entry.get("tag") or ""): entry for entry in entries}
    buffs = []
    for stat in stats:
        if stat.get("is_curse"):
            continue
        entry = stat.get("_entry") or by_tag.get(
            str(stat.get("ref") or stat.get("tag") or ""), {})
        buffs.append({
            "roll": float(stat.get("roll") or 0.0),
            "_entry": entry,
            "_base": float(stat.get("_base") or entry.get("value") or 0.0),
        })
    order = sorted(buffs, key=lambda stat: (-stat["roll"], stat["_base"]))
    name = ""
    n = len(order)
    for i, s in enumerate(order):
        e = s["_entry"]
        if i == n - 1:
            name += e.get("suffix") or ""
        elif i == 0:
            p = e.get("prefix") or ""
            name += (p[:1].upper() + p[1:]) if p else ""
        else:
            name += "-" + (e.get("prefix") or "")
    return name


def decode_link(category: str, b64: str) -> dict | None:
    """解码单条 OMG 链接。无法解析（类别未知/位数不足/头部异常）返回 None。"""
    entries = rivendata.riven_values().get(category)
    if not entries:
        return None
    try:
        bits = _bits(b64)
    except binascii.Error:
        return None
    if len(bits) < 48:
        return None

    header = _gb(bits, 0, 4)
    if (header >> 2) != 0b11:
        return None  # 头部标记异常，布局不认识
    n_curses = header & 1
    n_buffs = 2 + ((header >> 1) & 1)
    n = n_buffs + n_curses
    stats_end = 4 + n * _BLOCK
    index_bits = _WEAPON_INDEX_BITS.get(category)
    if index_bits is None:
        return None
    index_start = stats_end + 9  # 8bit opaque field + 1bit selector
    meta_end = stats_end + 34 + index_bits
    if len(bits) < meta_end:
        return None

    stats = []
    for j in range(n):
        stat_start = 4 + j * _BLOCK
        idx = _gb(bits, stat_start, 5)
        float_bits = _gb(bits, stat_start + 5, 31)
        roll = struct.unpack(">f", struct.pack(">I", float_bits))[0]
        if not 0.0 <= roll <= 1.0:
            return None
        is_curse = j >= n_buffs
        entry = entries[idx] if idx < len(entries) else None
        ref = entry["tag"] if entry else f"?{idx}"
        zh, en = _ref_names(ref) if entry else (ref, ref)
        grade = grading.roll_to_grade(1 - roll if is_curse else roll)
        stats.append({
            "ref": ref, "zh": zh, "en": en, "roll": roll,
            "grade": grade, "is_curse": is_curse,
            "_entry": entry or {}, "_base": (entry or {}).get("value", 0.0),
            # 追踪库用原始位身份构造稳定内容哈希；下划线字段不会进入
            # 面向用户的公开序列化。
            "_index": idx, "_float_bits": float_bits,
        })

    weapon_selector = _gb(bits, stats_end + 8, 1)
    if weapon_selector != 1:
        return None
    weapon_index = _gb(bits, index_start, index_bits)
    lvl_req = _gb(bits, index_start + index_bits, 5)
    polarity_code = _gb(bits, index_start + index_bits + 7, 2)
    lvl = _gb(bits, index_start + index_bits + 9, 4)
    rerolls = _gb(bits, index_start + index_bits + 15, 10)
    if not 8 <= lvl_req <= 16 or lvl > 8:
        return None
    slug = weapon_slug(category, weapon_index)
    weapon = rivendata.weapons().get(slug, {}) if slug else {}
    dispo = weapon.get("disposition")
    variant_dispositions = weapon.get("variant_dispositions") or {}
    if not variant_dispositions and dispo is not None:
        variant_dispositions = {weapon.get("name_en") or slug: dispo}
    for s in stats:
        s["display"] = _abs_value(category, s["ref"], s["roll"], lvl,
                                  n_buffs, n_curses, s["is_curse"], dispo)
        s["display_variants"] = {
            variant: _abs_value(
                category, s["ref"], s["roll"], lvl,
                n_buffs, n_curses, s["is_curse"], variant_dispo,
            )
            for variant, variant_dispo in variant_dispositions.items()
        }

    polarity, polarity_mark = _POLARITY_BY_CODE[polarity_code]

    return {
        "category": category,
        "cat_zh": _category_zh(category),
        "weapon_index": weapon_index,
        "weapon_path": weapon_path(category, weapon_index),
        "weapon_slug": slug,
        "weapon_name": rivendata.weapon_display_name(slug) if slug else None,
        "lvl_req": lvl_req,
        "polarity_code": polarity_code,
        "polarity": polarity,
        "polarity_mark": polarity_mark,
        "lvl": lvl,
        "rerolls": rerolls,
        "n_buffs": n_buffs,
        "n_curses": n_curses,
        "riven_name": riven_name(category, stats),
        "stats": stats,
    }
