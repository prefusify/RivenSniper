"""紫卡词条数值高低分析。

公式移植自 calamity-inc/warframe-riven-info 的 RivenParser.js（自游戏客户端）：

    显示值 = 基准值 × 1.5 × 倾向 × 10 × 1.25^负词条数 × lerp(0.9,1.1,浮动)
             × 词条数系数[正词条数] × (等级+1)                      —— 正词条
    显示值 = -基准值 × 1.5 × 倾向 × 10 × lerp(0.9,1.1,浮动)
             × 负系数[正词条数] × 词条数系数[负词条数] × (等级+1)    —— 负词条

浮动系数 roll 的合法区间为 [0,1]（0=最低roll 1=最高roll）；数值超限时
保留按同一公式外推的 roll，用于展示真实偏差百分比。

紫卡可升级：r 级数值 = 满级数值 × (r+1)/9。现实中 WFM 卖家填数值的口径不一：
未升级的卡有人填 0 级数值有人填满级数值（无论听单标注的 mod_rank 是多少）、
衍生武器（Prime 等）按各自倾向显示。因此评分做多候选拟合：
  (标注等级 → 满级 → 0级) × (基础武器倾向 → 各变体倾向)
选第一个能让全部词条落在可能区间内的候选；都不行则按基础口径标 X。

评分展示复用 RivenSniper bot 的方式：S/A+/A/A-/B+/B/B-/C+/C/C-/F 十一档 +
偏离中值百分比（±10%）。负词条评分方向与 RivenSniper 一致：负面幅度越温和
（显示值越接近弱端）评分越高，StatGrade.roll 存储的是按 1-物理roll
反转后的评分方向浮动。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import rivendata

NUM_BUFFS_ATTEN = [0, 1, 0.66000003, 0.5, 0.40000001, 0.34999999]
NUM_BUFFS_CURSE_ATTEN = [0, 1, 0.33000001, 0.5, 1.25, 1.5]

# 对阵营伤害：WFM API 使用总乘数（如 1.54 / 0.72），
# 游戏公式使用相对 1.0 的增量（+0.54 / -0.28）。
_FACTION_REFS = {
    "WeaponFactionDamageGrineer",
    "WeaponFactionDamageCorpus",
    "WeaponFactionDamageInfested",
    "WeaponMeleeFactionDamageGrineer",
    "WeaponMeleeFactionDamageCorpus",
    "WeaponMeleeFactionDamageInfested",
}

# 这些词条的 WFM 数值就是原始值（非百分比 /100）。
# 阵营伤害是带 1.0 原点的乘数，必须走下方的独立双向转换。
_RAW_VALUE_REFS = {
    "WeaponMeleeComboInitialBonusMod",
    "ComboDurationMod",
    "WeaponMeleeRangeIncMod",
    "WeaponPunctureDepthMod",  # WFM 穿透存原始值(如 2.7)，实测确认
}

# 通常 curse 会把基准值取反；此项本身就是 negativeOnly 且基准值为负，
# WFM 实盘卡面仍显示负数。不能按“基准值为负”泛化，因为后坐力 curse 需要取反为正。
_CURSE_PRESERVE_BASE_SIGN_REFS = {
    "WeaponMeleeComboPointsOnHitMod",
}

# RivenParser floatToGrade 的 X 界限（±11.5 -> roll ±0.075）
_ROLL_TOLERANCE = 0.075


def roll_to_grade(roll: float) -> str:
    """浮动系数 [0,1] -> 字母评分。与 RivenParser.floatToGrade 一致。"""
    v = -10 + 20 * roll
    if v < -11.5 or v > 11.5:
        return "X"
    for threshold, grade in [
        (9.5, "S"), (7.5, "A+"), (5.5, "A"), (3.5, "A-"), (1.5, "B+"),
        (-1.5, "B"), (-3.5, "B-"), (-5.5, "C+"), (-7.5, "C"), (-9.5, "C-"),
    ]:
        if v >= threshold:
            return grade
    return "F"


@dataclass
class StatGrade:
    slug: str
    value: float          # WFM 显示值；阵营 curse 为小于 1 的正乘数
    positive: bool
    roll: float | None    # 评分方向浮动；超限可在 0~1 外，None=无法评分
    grade: str            # S/A+/.../F，X=出界，?=无数据
    min_display: float | None = None  # 该词条在此卡上的可能最小/最大显示值
    max_display: float | None = None

    @property
    def deviation(self) -> float | None:
        """偏离中值百分比（RivenSniper 口径：-10 ~ +10）"""
        return None if self.roll is None else round(-10 + 20 * self.roll, 2)


@dataclass
class GradeResult:
    stats: list[StatGrade] = field(default_factory=list)
    variant: str | None = None      # 按哪个变体倾向拟合成功（非基础武器时标注）
    assumed_rank: int | None = None  # 按哪个等级拟合成功（与听单标注不同时标注，0或8）
    fitted: bool = False            # 是否有候选完全在界内


def _display_to_value(game_ref: str, display: float) -> float:
    if game_ref in _FACTION_REFS:
        return display - 1.0
    return display if game_ref in _RAW_VALUE_REFS else display / 100.0


def _value_to_display(game_ref: str, value: float) -> float:
    if game_ref in _FACTION_REFS:
        return round((1.0 + value) * 100) / 100
    if game_ref in _RAW_VALUE_REFS:
        return round(value, 2)
    return round(value * 1000) / 10


def is_faction_slug(slug: str) -> bool:
    """词条 slug 是否使用阵营伤害总乘数。"""
    return (rivendata.attributes().get(slug) or {}).get("game_ref") in _FACTION_REFS


def format_faction_multiplier(value: float) -> str:
    """按 WFM/游戏卡面口径格式化阵营伤害乘数。"""
    number = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"x{number}"


def _curse_base_value(game_ref: str, base: float) -> float:
    """返回词条作为 curse 时参与公式的有符号基准值。"""
    return base if game_ref in _CURSE_PRESERVE_BASE_SIGN_REFS else -base


def _grade_with(attrs: list[dict], category: str, disposition: float,
                lvl: int) -> tuple[list[StatGrade], bool]:
    """按给定倾向/等级评分。返回 (结果, 已知词条是否全部在界内)。"""
    buffs = [a for a in attrs if a.get("positive")]
    curses = [a for a in attrs if not a.get("positive")]
    n_buffs, n_curses = len(buffs), len(curses)

    results: list[StatGrade] = []
    all_in = True
    for a in attrs:
        slug = a.get("url_name", "")
        display = a.get("value") or 0.0
        positive = bool(a.get("positive"))
        meta = rivendata.attributes().get(slug)
        game_ref = meta.get("game_ref") if meta else None
        base = rivendata.base_value(category, game_ref) if game_ref else None

        if base is None:
            results.append(StatGrade(slug, display, positive, None, "?"))
            continue

        atten = 1.5 * disposition * 10 * (lvl + 1)
        if positive:
            mid = base * atten * (1.25 ** n_curses) \
                * NUM_BUFFS_ATTEN[min(n_buffs, len(NUM_BUFFS_ATTEN) - 1)]
        else:
            mid = _curse_base_value(game_ref, base) * atten \
                * NUM_BUFFS_CURSE_ATTEN[min(n_buffs, len(NUM_BUFFS_CURSE_ATTEN) - 1)] \
                * NUM_BUFFS_ATTEN[min(n_curses, len(NUM_BUFFS_ATTEN) - 1)]

        if not mid:
            results.append(StatGrade(slug, display, positive, None, "?"))
            continue

        raw = _display_to_value(game_ref, display)
        roll = (raw / mid - 0.9) / 0.2
        if not positive:
            # 负词条方向对齐 RivenSniper：负面越温和评分越高。
            # 判定区间对称，反转不影响下方的 X 越界判定。
            roll = 1.0 - roll
        roll_lo = roll_hi = roll
        if game_ref in _FACTION_REFS:
            # WFM 阵营乘数只保留两位小数。0 级词条幅度很小，
            # 用舍入后的中心值直接反推会把合法值误判为 X；因此用
            # 整个 0.01 显示桶判定是否与可能 roll 区间相交。
            bucket_rolls = [
                ((bucket_raw / mid - 0.9) / 0.2)
                for bucket_raw in (raw - 0.005, raw + 0.005)
            ]
            if not positive:
                bucket_rolls = [1.0 - candidate
                                for candidate in bucket_rolls]
            roll_lo, roll_hi = min(bucket_rolls), max(bucket_rolls)
        lo = _value_to_display(game_ref, mid * 0.9)
        hi = _value_to_display(game_ref, mid * 1.1)
        lo, hi = min(lo, hi), max(lo, hi)  # 负词条 mid 为负，区间反向
        if (roll_lo <= 1 + _ROLL_TOLERANCE
                and roll_hi >= -_ROLL_TOLERANCE):
            score_roll = min(max(roll, 0.0), 1.0)
            results.append(StatGrade(slug, display, positive,
                                     score_roll,
                                     roll_to_grade(score_roll), lo, hi))
        else:
            # 超限仍保留外推 roll，供卡图与回退文字展示真实偏差百分比。
            results.append(StatGrade(slug, display, positive, roll, "X", lo, hi))
            all_in = False
    return results, all_in


def _ordered(stats: list[StatGrade]) -> list[StatGrade]:
    """正词条在前、负词条在后（稳定排序）——保证卡图/文字里负词条始终置底。"""
    return sorted(stats, key=lambda g: not g.positive)


def grade_auction_item(item: dict) -> GradeResult:
    """对 WFM 拍卖的 item(riven) 做词条数值高低分析（多候选拟合）。"""
    weapon_slug = item.get("weapon_url_name", "")
    category = rivendata.weapon_category(weapon_slug)
    weapon = rivendata.weapons().get(weapon_slug)
    attrs = item.get("attributes") or []
    lvl = item.get("mod_rank") or 0

    if not weapon or not category or not attrs:
        return GradeResult(
            stats=_ordered([StatGrade(a.get("url_name", ""), a.get("value") or 0.0,
                                      bool(a.get("positive")), None, "?")
                            for a in attrs]))

    base_disp = weapon["disposition"]
    variant_disps: list[tuple[str | None, float]] = [(None, base_disp)]
    for name, disp in (weapon.get("variant_dispositions") or {}).items():
        if abs(disp - base_disp) > 1e-6:
            variant_disps.append((name, disp))

    # 候选顺序：标注等级优先、基础武器优先；
    # 无论标注等级是多少，都兼容满级和 0 级两种填写口径
    ranks: list[tuple[int, int | None]] = [(lvl, None)]
    for r in (8, 0):
        if r != lvl:
            ranks.append((r, r))
    candidates = [(v, d, r, assumed) for r, assumed in ranks for v, d in variant_disps]

    first_result: list[StatGrade] | None = None
    for variant, disp, rank, assumed in candidates:
        stats, all_in = _grade_with(attrs, category, disp, rank)
        if first_result is None:
            first_result = stats
        if all_in:
            return GradeResult(stats=_ordered(stats), variant=variant,
                               assumed_rank=assumed, fitted=True)

    return GradeResult(stats=_ordered(first_result or []), fitted=False)
