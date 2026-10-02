"""Discord Rich Embed 共用的 ANSI 紫卡文本格式。"""

from __future__ import annotations

import re

from . import rivendata
from .grading import StatGrade, format_faction_multiplier, is_faction_slug

ANSI_RESET = "\x1b[0m"
ANSI_BLUE = "\x1b[1;34m"
ANSI_YELLOW = "\x1b[1;33m"

_GRADE_ANSI = {
    "F": 31,
    "C": 33,
    "B": 36,
    "A": 32,
    "S": 35,
}

_NON_PERCENT_SLUGS = {
    "combo_duration", "range", "channeling_damage", "punch_through",
}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    """移除 Discord ANSI 着色，供不支持该格式的平台复用同一文案。"""
    return _ANSI_RE.sub("", text)


def ansi_block(text: str) -> str:
    """把已经包含 SGR 控制码的文本包装为 Discord ANSI 代码块。"""
    return f"```ansi\n{text}{ANSI_RESET}\n```"


def plain_code_block(text: str) -> str:
    """生成用于一键复制私聊短语的普通代码块。"""
    return f"```\n{text}\n```"


def score_ansi(_deviation: float | None, grade: str = "") -> str:
    """按评分等级返回 Discord 支持的固定 ANSI 前景色。"""
    if grade == "X":
        return ANSI_RESET
    color = _GRADE_ANSI.get(grade[:1].upper())
    if color is not None:
        return f"\x1b[1;{color}m"
    return ANSI_RESET


def stat_value_text(stat: StatGrade) -> str:
    """生成固定符号与小数位的词条值。"""
    if is_faction_slug(stat.slug):
        return format_faction_multiplier(stat.value)
    suffix = "" if stat.slug in _NON_PERCENT_SLUGS else "%"
    return f"{stat.value:+.1f}{suffix}"


def stat_rows_ansi(
    stats: list[StatGrade],
    locale: str,
    *,
    title: str | None = None,
    right_align: bool = False,
) -> str:
    """以评分、偏移、数值、词条的固定列顺序生成 ANSI 文本。"""
    rows = [
        (
            stat,
            "—" if stat.deviation is None else f"{stat.deviation:+.2f}%",
            stat_value_text(stat),
            rivendata.attribute_name(stat.slug, locale),
        )
        for stat in stats
    ]
    deviation_width = max(
        10, *(len(deviation) for _stat, deviation, _value, _name in rows))
    value_width = max(
        10, *(len(value) for _stat, _deviation, value, _name in rows))

    lines: list[str] = []
    if title:
        lines.append(f"{ANSI_BLUE}[{title}]{ANSI_RESET}")
    for stat, deviation, value, name in rows:
        if right_align:
            line = (
                f"{stat.grade:<5}{deviation:>{deviation_width}}  "
                f"{value:>{value_width}}  {name}"
            )
        else:
            line = (
                f"{stat.grade:<5}{deviation:<{deviation_width}}"
                f"{value:<{value_width}}{name}"
            )
        lines.append(
            f"{score_ansi(stat.deviation, stat.grade)}{line}{ANSI_RESET}"
        )
    return "\n".join(lines)


def riven_block_ansi(
    stats: list[StatGrade],
    locale: str,
    *,
    title: str,
    mastery_level: int | str,
    rank: int | str,
    rolls: int | str,
    polarity: str,
    right_align: bool = False,
) -> str:
    """生成频道与 WM 共用的完整紫卡词条数据块。"""
    rows = stat_rows_ansi(
        stats,
        locale,
        title=title,
        right_align=right_align,
    )
    meta = (f"MR: {mastery_level}   Rank: {rank}   Rerolls: {rolls}   "
            f"Polarity: {polarity}" if locale == "en" else
            f"段位：{mastery_level}   等级：{rank}   洗练：{rolls}   极性：{polarity}")
    return f"{rows}\n{ANSI_RESET}{meta}"


__all__ = [
    "ANSI_BLUE", "ANSI_RESET", "ANSI_YELLOW", "ansi_block",
    "plain_code_block", "riven_block_ansi", "score_ansi",
    "stat_rows_ansi", "stat_value_text", "strip_ansi",
]
