"""命中紫卡图片渲染（Pillow）。

左侧按紫卡当前等级选择 ``data/assets/rank0.png`` ~ ``rank8.png``；模板包含
对应的底部等级灯。右侧采用固定、高可读性的游戏风格布局。

评分数值颜色按偏离中值 -10%~+10% 平滑渐变，字母档锚点：
F=红、C=橙、B=黄、A=浅绿、S=紫。X（越界）为白色，?（无基准）为灰色。
"""

from __future__ import annotations

import colorsys
import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import rivendata
from .grading import (
    GradeResult,
    StatGrade,
    format_faction_multiplier,
    is_faction_slug,
)

ASSETS_DIR = rivendata.DATA_DIR / "assets"
TEMPLATE_NAMES = {rank: f"rank{rank}.png" for rank in range(9)}
PLATINUM_ASSET = "Platinum.png"

# 用原布局 75% 的画布、字体和素材直接绘制，避免先生成大图再缩放。
# 原布局尺寸为 1298×742，左侧卡面为 560×742。
OUTPUT_SCALE = 0.75


def _px(value: float) -> int:
    return round(value * OUTPUT_SCALE)


CARD_W, CARD_H = _px(560), _px(742)
CARD_X = _px(10)
PANEL_X = _px(596)
PANEL_W = _px(690)
IMG_W, IMG_H = _px(1298), CARD_H

BG_TOP = (9, 12, 25, 255)
BG_BOTTOM = (4, 4, 10, 255)
PANEL_BG = (5, 5, 9, 255)
CARD_TEXT = (193, 139, 231, 255)
CARD_STAT = (205, 160, 238, 255)
PANEL_TITLE = (249, 246, 255, 255)
PANEL_SUB = (205, 180, 250, 255)
PANEL_STAT = (225, 195, 255, 255)
PANEL_EXTRA = (232, 219, 255, 255)

# 经项目词条表核对的最宽合法组合。左右两侧分别从这些探针反推统一字号，
# 短词条不会比最长词条使用更大的字体。
CARD_STAT_PROBES = (
    "+999.9% 投射物飞行速度",
    "+999.9% 滑行攻击暴击率",
    "+999.9% 额外连击数获取",
    "+999.9% 连击数获取几率",
    "+999.9% 射速/攻击速度",
    "x9.99 对Infested伤害",
)
PANEL_STAT_PROBES = tuple(
    value.replace("% ", "%  ") for value in CARD_STAT_PROBES
)

# 高对比度评分渐变：偏离中值(-10..+10) → (色相°, 饱和, 明度)。
_COLOR_STOPS = [
    (-9.75, (0.0, 0.76, 1.00)),
    (-6.5, (28.0, 0.82, 1.00)),
    (0.0, (52.0, 0.78, 1.00)),
    (6.5, (108.0, 0.62, 0.95)),
    (9.75, (272.0, 0.55, 1.00)),
]
X_COLOR = (245, 245, 245, 255)
NA_COLOR = (157, 157, 157, 255)

# 非百分比词条的单位（与 formatter._NON_PERCENT_SLUGS 对应）。
_UNITS = {
    "range": "m",
    "punch_through": "m",
    "combo_duration": "s",
    "channeling_damage": "",
    "damage_vs_corpus": "",
    "damage_vs_grineer": "",
    "damage_vs_infested": "",
}

_POLARITY_ASSET = {
    "madurai": "pol_madurai.png",
    "vazarin": "pol_vazarin.png",
    "naramon": "pol_naramon.png",
    "zenurik": "pol_zenurik.png",
}

_FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyhbd.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)
_SYMBOL_FONT_CANDIDATES = (
    "C:/Windows/Fonts/seguisym.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/System/Library/Fonts/Apple Symbols.ttf",
)


def grade_color(
    deviation: float | None,
    grade: str = "",
) -> tuple[int, int, int, int]:
    """偏离中值 → 平滑渐变色；X/? 使用独立颜色。"""
    if grade == "X":
        return X_COLOR
    if deviation is None:
        return NA_COLOR
    value = max(_COLOR_STOPS[0][0], min(_COLOR_STOPS[-1][0], deviation))
    for (d0, hsv0), (d1, hsv1) in zip(_COLOR_STOPS, _COLOR_STOPS[1:]):
        if value <= d1:
            ratio = 0.0 if d1 == d0 else (value - d0) / (d1 - d0)
            h, s, v = (a + (b - a) * ratio for a, b in zip(hsv0, hsv1))
            red, green, blue = colorsys.hsv_to_rgb(h / 360.0, s, v)
            return (
                round(red * 255),
                round(green * 255),
                round(blue * 255),
                255,
            )
    return X_COLOR


@lru_cache(maxsize=1)
def _font_path() -> str | None:
    for path in _FONT_CANDIDATES:
        if Path(path).is_file():
            return path
    return None


@lru_cache(maxsize=1)
def _symbol_font_path() -> str | None:
    for path in _SYMBOL_FONT_CANDIDATES:
        if Path(path).is_file():
            return path
    return _font_path()


@lru_cache(maxsize=256)
def _font_cached(path: str | None, size: int):
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _font(size: int):
    return _font_cached(_font_path(), _px(size))


def _symbol_font(size: int):
    return _font_cached(_symbol_font_path(), _px(size))


@lru_cache(maxsize=None)
def _asset(name: str) -> Image.Image:
    return Image.open(ASSETS_DIR / name).convert("RGBA")


@lru_cache(maxsize=9)
def _template(rank: int) -> Image.Image:
    if rank not in TEMPLATE_NAMES:
        raise ValueError(f"紫卡等级必须在 0~8，收到 {rank}")
    template_name = TEMPLATE_NAMES[rank]
    # 原图解码后每张约 5.3 MiB；这里只缓存最终输出尺寸，避免九个等级
    # 都出现后仍长期持有整套原始像素。
    with Image.open(ASSETS_DIR / template_name) as opened:
        source = opened.convert("RGBA")
    alpha_box = source.getchannel("A").getbbox()
    if not alpha_box:
        raise ValueError(f"{template_name} 没有可见内容")
    return source.crop(alpha_box).resize(
        (CARD_W, CARD_H), Image.Resampling.LANCZOS
    )


@lru_cache(maxsize=8)
def _platinum_icon(height: int) -> Image.Image:
    source = _asset(PLATINUM_ASSET)
    alpha_box = source.getchannel("A").getbbox()
    if not alpha_box:
        raise ValueError(f"{PLATINUM_ASSET} 没有可见内容")
    source = source.crop(alpha_box)
    width = round(source.width * height / source.height)
    return source.resize((width, height), Image.Resampling.LANCZOS)


@lru_cache(maxsize=16)
def _polarity_icon(name: str, size: int) -> Image.Image:
    source = _asset(_POLARITY_ASSET[name]).resize(
        (size, size), Image.Resampling.LANCZOS
    )
    tinted = Image.new("RGBA", source.size, CARD_TEXT)
    tinted.putalpha(source.getchannel("A"))
    return tinted


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> float:
    return draw.textlength(text, font=font)


def _fit_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: int,
    start_size: int,
    *,
    min_size: int,
):
    for size in range(start_size, min_size - 1, -1):
        candidate = _font(size)
        if _text_width(draw, text, candidate) <= max_width:
            return candidate
    return _font(min_size)


def _largest_common_font(
    draw: ImageDraw.ImageDraw,
    probes: tuple[str, ...],
    max_width: int,
    max_size: int,
    *,
    min_size: int,
):
    for size in range(max_size, min_size - 1, -1):
        candidate = _font(size)
        if all(_text_width(draw, value, candidate) <= max_width for value in probes):
            return candidate
    return _font(min_size)


def _draw_mixed(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    segments: list[tuple[str, object]],
    *,
    fill: tuple[int, int, int, int],
    anchor: str = "lm",
    baseline: bool = False,
    gap: float = 0,
) -> None:
    widths = [_text_width(draw, value, font) for value, font in segments]
    total_width = sum(widths) + gap * max(len(segments) - 1, 0)
    x, y = xy
    if anchor.startswith("r"):
        x -= total_width
    elif anchor.startswith("m"):
        x -= total_width / 2
    vertical_anchor = "ls" if baseline else "lm"
    for index, ((value, font), width) in enumerate(zip(segments, widths)):
        draw.text(
            (x, y),
            value,
            font=font,
            fill=fill,
            anchor=vertical_anchor,
        )
        x += width
        if index < len(segments) - 1:
            x += gap


def _gradient_background() -> Image.Image:
    image = Image.new("RGBA", (IMG_W, IMG_H))
    draw = ImageDraw.Draw(image)
    for y in range(IMG_H):
        ratio = y / max(IMG_H - 1, 1)
        color = tuple(
            round(start + (end - start) * ratio)
            for start, end in zip(BG_TOP, BG_BOTTOM)
        )
        draw.line((0, y, IMG_W, y), fill=color)
    draw.rectangle((PANEL_X, 0, IMG_W, IMG_H), fill=PANEL_BG)
    return image


def format_stat_text(grade: StatGrade, locale: str = "zh") -> str:
    """卡面词条：``+17.4% 暴击伤害``。"""
    value, name = _stat_parts(grade, locale)
    return f"{value} {name}"


def _stat_parts(grade: StatGrade, locale: str = "zh") -> tuple[str, str]:
    name = rivendata.attribute_card_name(grade.slug, locale)
    faction = is_faction_slug(grade.slug)
    if not faction and not grade.positive and grade.value >= 0:
        name += " (NEG)" if locale == "en" else "(负)"
    if faction:
        value = format_faction_multiplier(grade.value)
    else:
        sign = "+" if grade.value >= 0 else ""
        unit = _UNITS.get(grade.slug, "%")
        value = f"{sign}{grade.value}{unit}"
    return value, name


def _weapon_names(slug: str) -> tuple[str, str]:
    weapon = rivendata.weapons().get(slug) or {}
    english = weapon.get("name_en") or slug.replace("_", " ").title()
    chinese = weapon.get("name_zh") or english
    return chinese, english


def _localized_weapon_names(slug: str, locale: str) -> tuple[str, str]:
    """中文卡保留英文对照，英文卡只显示英文武器名。"""
    chinese, english = _weapon_names(slug)
    return (english, "") if locale == "en" else (chinese, english)


def _item_rank(item: dict) -> int:
    rank = int(item.get("mod_rank") or 0)
    if rank not in TEMPLATE_NAMES:
        raise ValueError(f"紫卡等级必须在 0~8，收到 {rank}")
    return rank


def _draw_card(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    item: dict,
    result: GradeResult,
    locale: str,
) -> None:
    rank = _item_rank(item)
    weapon_name = rivendata.weapon_name(item.get("weapon_url_name", ""), locale)
    riven_name = str(item.get("name") or "").capitalize()
    image.alpha_composite(_template(rank), (CARD_X, 0))

    capacity = str(10 + rank)
    capacity_font = _font(36)
    polarity = item.get("polarity") or ""
    icon = _polarity_icon(polarity, _px(32)) \
        if polarity in _POLARITY_ASSET else None
    number_width = _text_width(draw, capacity, capacity_font)
    icon_width = _px(8) + icon.width if icon else 0
    group_width = number_width + icon_width
    # 容量框的实测中心：(473, 68)，坐标相对卡面。
    group_x = CARD_X + _px(473) - group_width / 2
    group_y = _px(68)
    draw.text(
        (group_x, group_y),
        capacity,
        font=capacity_font,
        fill=CARD_TEXT,
        anchor="lm",
    )
    if icon:
        number_box = draw.textbbox(
            (group_x, group_y), capacity, font=capacity_font, anchor="lm"
        )
        icon_y = round((number_box[1] + number_box[3] - icon.height) / 2)
        image.alpha_composite(
            icon,
            (round(group_x + number_width + _px(8)), icon_y),
        )

    title_width = _px(444)
    weapon_font = _fit_font(draw, weapon_name, title_width, 50, min_size=34)
    riven_font = _fit_font(draw, riven_name, title_width, 47, min_size=34)
    draw.text(
        (CARD_X + CARD_W / 2, _px(331)),
        weapon_name,
        font=weapon_font,
        fill=CARD_TEXT,
        anchor="mm",
    )
    draw.text(
        (CARD_X + CARD_W / 2, _px(381)),
        riven_name,
        font=riven_font,
        fill=CARD_TEXT,
        anchor="mm",
    )

    stat_font = _largest_common_font(
        draw, CARD_STAT_PROBES, title_width, 44, min_size=34
    )
    y = _px(430)
    for grade in result.stats[:4]:
        draw.text(
            (CARD_X + CARD_W / 2, y),
            format_stat_text(grade, locale),
            font=stat_font,
            fill=CARD_STAT,
            anchor="mm",
        )
        y += _px(46)

    plate_y = _px(650)
    plate_font = _font(31)
    _draw_mixed(
        draw,
        (CARD_X + _px(101), plate_y),
        [(("MR " if locale == "en" else "段位"), plate_font),
         (str(item.get("mastery_level", "?")), plate_font)],
        fill=CARD_STAT,
        baseline=True,
        gap=_px(3),
    )
    _draw_mixed(
        draw,
        (CARD_X + _px(461), plate_y),
        [("↺", _symbol_font(40)), (str(item.get("re_rolls", 0)), _font(31))],
        fill=CARD_STAT,
        anchor="rm",
        baseline=True,
        gap=_px(3),
    )


def _used_disposition(item: dict, result: GradeResult) -> float | None:
    """评分实际采用的倾向：有拟合变体时使用变体倾向。"""
    weapon = rivendata.weapons().get(item.get("weapon_url_name", "")) or {}
    disposition = weapon.get("disposition")
    if result.variant:
        disposition = (weapon.get("variant_dispositions") or {}).get(
            result.variant, disposition
        )
    return disposition


def _disposition_text(disposition: float | None, locale: str) -> str:
    label = "Disp." if locale == "en" else "倾向"
    return (f"{label} {disposition:g}"
            if disposition is not None else f"{label} —")


def _price_lines(
    auction: dict | None, locale: str = "zh",
) -> list[tuple[str, object]]:
    if not auction:
        return []
    buyout = auction.get("buyout_price")
    starting = auction.get("starting_price")
    if auction.get("is_direct_sell"):
        label = "Buyout" if locale == "en" else "一口价"
        return [(label, buyout)] if buyout is not None else []
    lines: list[tuple[str, object]] = []
    if starting is not None:
        lines.append(("Starting", starting) if locale == "en" else ("起拍价", starting))
    if buyout is not None:
        lines.append(("Buyout", buyout) if locale == "en" else ("一口价", buyout))
    return lines


def _score_text(grade: StatGrade, locale: str = "zh") -> str:
    if grade.roll is not None:
        deviation = grade.deviation
        sign = "+" if deviation >= 0 else ""
        if locale == "en":
            limit = " out of range" if grade.grade == "X" else ""
            return f"Grade: {grade.grade} ({sign}{deviation}%{limit})"
        limit = " 数值超限" if grade.grade == "X" else ""
        return f"评分：{grade.grade}（{sign}{deviation}%{limit}）"
    if grade.grade == "X":
        return ("Grade: out of range (value does not fit the possible range)"
                if locale == "en" else "评分：越界*（数值与可能区间不符）")
    return "Grade: ? (no baseline data)" if locale == "en" else "评分：?（缺少基准数据）"


def _draw_panel_stat(
    draw: ImageDraw.ImageDraw,
    grade: StatGrade,
    y: int,
    locale: str,
) -> None:
    x = PANEL_X + _px(30)
    max_width = PANEL_W - _px(60)
    stat_font = _largest_common_font(
        draw, PANEL_STAT_PROBES, max_width, 60, min_size=39
    )
    value, name = _stat_parts(grade, locale)
    color = grade_color(grade.deviation, grade.grade)
    draw.text((x, y), value, font=stat_font, fill=color, anchor="la")
    value_width = _text_width(draw, value + "  ", stat_font)
    draw.text(
        (x + value_width, y),
        name,
        font=stat_font,
        fill=PANEL_STAT,
        anchor="la",
    )

    score = _score_text(grade, locale)
    score_font = _fit_font(
        draw, score, max_width - _px(14), 40, min_size=26
    )
    draw.text(
        (x + _px(14), y + _px(58)),
        score,
        font=score_font,
        fill=color,
        anchor="la",
    )


def _display_price(value: object) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _draw_price(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    label: str,
    amount: object,
    y: int,
) -> None:
    font = _font(42)
    icon = _platinum_icon(_px(36))
    text = f"{label}  {_display_price(amount)}"
    text_width = _text_width(draw, text, font)
    total_width = text_width + _px(12) + icon.width
    start_x = PANEL_X + PANEL_W - _px(28) - total_width
    draw.text((start_x, y), text, font=font, fill=PANEL_EXTRA, anchor="lm")
    text_box = draw.textbbox((start_x, y), text, font=font, anchor="lm")
    icon_y = round((text_box[1] + text_box[3] - icon.height) / 2)
    image.alpha_composite(
        icon, (round(start_x + text_width + _px(12)), icon_y)
    )


def _draw_panel(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    item: dict,
    result: GradeResult,
    auction: dict | None,
    locale: str,
    source_label: str | None,
) -> None:
    weapon_name, translated_weapon_name = _localized_weapon_names(
        item.get("weapon_url_name", ""), locale,
    )
    rank = _item_rank(item)
    x0 = PANEL_X + _px(30)
    x1 = PANEL_X + PANEL_W - _px(28)

    disposition = _used_disposition(item, result)
    disposition_text = _disposition_text(disposition, locale)
    disposition_font = _font(40)
    draw.text(
        (x1, _px(17)),
        disposition_text,
        font=disposition_font,
        fill=PANEL_EXTRA,
        anchor="ra",
    )

    title_font = _fit_font(
        draw, weapon_name, PANEL_W - _px(60) - _px(210), 52, min_size=36
    )
    draw.text(
        (x0, _px(14)), weapon_name, font=title_font,
        fill=PANEL_TITLE, anchor="la",
    )
    translation_font = _fit_font(
        draw, translated_weapon_name, PANEL_W - _px(60), 36, min_size=28
    )
    draw.text(
        (x0, _px(78)), translated_weapon_name, font=translation_font,
        fill=PANEL_SUB, anchor="la",
    )

    y = _px(132)
    for grade in result.stats[:4]:
        _draw_panel_stat(draw, grade, y, locale)
        y += _px(109)

    _draw_mixed(
        draw,
        (x0, _px(666)),
        [(("Rank " if locale == "en" else "等级 "), _font(36)),
         (f"{rank}/8", _font(42))],
        fill=PANEL_EXTRA,
    )

    if source_label:
        source_font = _fit_font(draw, source_label, PANEL_W - _px(60), 46, min_size=30)
        draw.text((x1, _px(670)), source_label, font=source_font,
                  fill=PANEL_EXTRA, anchor="rm")
        return

    prices = _price_lines(auction, locale)
    if len(prices) == 1:
        label, amount = prices[0]
        _draw_price(image, draw, label, amount, _px(670))
    elif prices:
        price_ys = (_px(623), _px(681))
        for (label, amount), price_y in zip(prices[:2], price_ys):
            _draw_price(image, draw, label, amount, price_y)


def render_hit_card(
    item: dict,
    result: GradeResult,
    auction: dict | None = None,
    *,
    locale: str = "zh",
    source_label: str | None = None,
) -> bytes:
    """渲染命中紫卡图并返回 JPEG bytes。

    素材、字体或数据异常时抛出，由调用方回退为完整文字推送。
    """
    image = _gradient_background()
    draw = ImageDraw.Draw(image)
    _draw_card(image, draw, item, result, locale)
    _draw_panel(image, draw, item, result, auction, locale, source_label)
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "JPEG", quality=90)
    return buffer.getvalue()
