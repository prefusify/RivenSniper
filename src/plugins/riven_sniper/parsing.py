"""狙击添加参数解析。

当前标准语法以空格分隔词条位置（AND），以 ``/`` 分隔同一位置的备选
词条（OR），每个备选可用 ``@评级`` 设置最低评级；任意位置的下限应用于
它最终占用的真实词条。``+`` 仅作为空格的输入别名；评级中的 ``A+`` /
``B+`` / ``C+`` 不参与分词。
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from . import rivendata, texts
from .command_meta import command_example_name
from .criteria import (ANY_ATTRIBUTE, groups_satisfiable, normalize_groups,
                       rated_groups_as_lists)
from .ratings import RATINGS_DESCENDING, normalize_minimum_rating


class ParseError(Exception):
    """解析失败，message 面向用户。"""


@dataclass
class AddArgs:
    weapon: str | None = None
    wildcard: str | None = None
    # 外层位置之间为 AND，内层备选之间为 OR。
    positives: list[list[str]] = field(default_factory=list)
    positive_ratings: list[dict[str, str]] = field(default_factory=list)
    negatives: list[list[str]] = field(default_factory=list)
    negative_ratings: list[dict[str, str]] = field(default_factory=list)
    zero_rerolls: bool = False


_NORMALIZE = str.maketrans({
    "＋": "+", "➕": "+",
    "－": "-", "–": "-", "—": "-", "−": "-",
    "：": ":", "＝": "=", "，": " ", "；": " ", "。": " ", "？": " ",
    "！": "!",
    "、": "/", "｜": "/", "|": "/", "／": "/",
    "＜": "<", "≤": "<", "≦": "<",
    "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
    "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
})

_ANY_WORDS = {"任意", "any"}
_DELETED_SHAPE_RE = re.compile(r"(?:2\s*[+-]\s*1|2正1负|两正一负)", re.I)
_QUOTED_RE = re.compile(r'"([^"\r\n]*)"|“([^”\r\n]*)”')
_PROTECTED_SPACE = "\ue000"
_PROTECTED_SLASH = "\ue001"
_PROTECTED_PLUS = "\ue002"


def normalize(text: str) -> str:
    return text.translate(_NORMALIZE).strip()


def _pick(locale: str, zh: str, en: str) -> str:
    return en if locale == "en" else zh


def _suggest(word: str, candidates: list[str], locale: str) -> str:
    if locale == "en":
        candidates = [candidate for candidate in candidates if candidate.isascii()]
    close = difflib.get_close_matches(word, candidates, n=1, cutoff=0.5)
    if not close:
        return ""
    return _pick(
        locale,
        f"，是否想输入「{close[0]}」？",
        f'; did you mean "{close[0]}"?',
    )


def _resolve_attr(word: str, kind: str, locale: str) -> str:
    word = word.strip()
    slug = rivendata.resolve_attribute(word)
    if slug:
        return slug
    candidates = list(rivendata.aliases()["attributes"].keys())
    kind_en = "positive attribute" if kind == "正词条" else "negative attribute"
    command = command_example_name("attribute.list")
    hint = (f"发送 {command} 查看词条最简写" if locale == "zh"
            else f"Use {command} to view stat shortcuts.") if command else \
        texts.render("通用.缺少英文简写", locale=locale)
    raise ParseError(_pick(
        locale,
        f"不认识{kind}「{word}」{_suggest(word, candidates, locale)}\n"
        f"{hint}",
        f'Unknown {kind_en} "{word}"{_suggest(word, candidates, locale)}\n'
        f"{hint}",
    ))


def _split_weapon(text: str, locale: str) -> tuple[str | None, str | None, str]:
    """从命令头解析武器/类型，并优先选择最长的可解析武器前缀。"""
    boundaries = {m.start() for m in re.finditer(
        r"\s|[+!@]|(?<=\S)-|价格|洗练|极性|买断|未洗|已洗|排除|艾特|"
        r"0洗|2\s*[+-]\s*1|2正1负|两正一负", text)}
    boundaries.add(len(text))
    matches: list[tuple[int, str | None, str | None]] = []
    tried: list[str] = []
    for pos in sorted(boundaries):
        if pos == 0:
            continue
        candidate = text[:pos].strip().strip(":=")
        if not candidate:
            continue
        if candidate not in tried:
            tried.append(candidate)
        wildcard = rivendata.resolve_wildcard(candidate)
        if wildcard:
            matches.append((pos, None, wildcard))
        weapon = rivendata.resolve_weapon(candidate)
        if weapon:
            matches.append((pos, weapon, None))
    if matches:
        pos, weapon, wildcard = max(matches, key=lambda value: value[0])
        return weapon, wildcard, text[pos:]

    first = tried[0] if tried else text
    names = list(rivendata._weapon_name_index().keys())
    raise ParseError(_pick(
        locale,
        f"不认识武器「{first}」{_suggest(first, names, locale)}\n"
        "请用游戏内武器名，或类型：all / rifle / shotgun / pistol / melee / archgun / kitgun / zaw",
        f'Unknown weapon or type "{first}"{_suggest(first, names, locale)}\n'
        "Use an exact in-game Chinese or English weapon name, or one of: "
        "all, rifle, shotgun, pistol, melee, archgun, kitgun, zaw.",
    ))


def _deleted_syntax_error(token: str, locale: str) -> str | None:
    lower = token.lower()
    if token.startswith("排除") or token.startswith("!"):
        return _pick(locale,
                     "已删除 排除/! 语法；负词条请直接写成 -词条，多个备选用 /",
                     "Exclusion syntax was removed. Use -z/r for negative-stat alternatives.")
    if lower in {"-无", "-none", "-有"}:
        return _pick(locale,
                     "已删除旧的负词条语法；不写负词条即要求无负词条，需要任意负词条请写 -any",
                     "Legacy negative-stat syntax was removed. Omit a negative to require none, or use -any.")
    if token.startswith("价格") or token.startswith("<"):
        return _pick(locale, "已删除价格条件，狙击配置不再按价格过滤",
                     "Price conditions were removed; sniper rules no longer filter by price.")
    if token.startswith("极性"):
        return _pick(locale, "已删除极性条件，狙击配置不再按极性过滤",
                     "Polarity conditions were removed; sniper rules no longer filter by polarity.")
    if lower in {"买断", "buyout"}:
        return _pick(locale, "已删除买断条件，狙击配置不再按买断状态过滤",
                     "Buyout conditions were removed; sniper rules no longer filter by sale type.")
    if token in {"未洗", "已洗"} or token.startswith("洗练"):
        return _pick(locale, "洗练次数仅支持 unrolled（0 洗）；旧洗练语法已删除",
                     "Reroll filtering only supports unrolled; legacy reroll syntax was removed.")
    if re.fullmatch(r"\d+洗", token) and token != "0洗":
        return _pick(locale, "洗练次数仅支持 unrolled（0 洗）",
                     "Reroll filtering only supports unrolled.")
    return None


def _resolve_group(
    token: str, kind: str, locale: str,
) -> tuple[list[str], dict[str, str]]:
    if not token:
        raise ParseError(_pick(
            locale, f"{kind}不能为空",
            f"The {'positive' if kind == '正词条' else 'negative'} attribute cannot be empty.",
        ))
    raw_alternatives = token.split("/")
    if any(not alternative for alternative in raw_alternatives):
        raise ParseError(_pick(
            locale, f"「{token}」的 / 两侧都必须填写词条",
            f'Both sides of / in "{token}" must contain an attribute.',
        ))

    alternatives: list[str] = []
    ratings: dict[str, str] = {}
    valid_ratings = "/".join(RATINGS_DESCENDING)
    for raw_alternative in raw_alternatives:
        alternative = raw_alternative.replace(_PROTECTED_SLASH, "/")
        if alternative.startswith(("+", "-")):
            raise ParseError(_pick(
                locale,
                f"「{token}」格式错误：正负号只写在整个位置前，如 -z/r",
                f'Invalid group "{token}": put the sign before the whole position, such as -z/r.',
            ))

        attribute_text, marker, rating_text = alternative.rpartition("@")
        minimum: str | None = None
        if marker:
            if not attribute_text or not rating_text or "@" in attribute_text:
                raise ParseError(_pick(
                    locale,
                    f"「{alternative}」格式错误；最低评级写在词条后，如 cc@A 或 any@A",
                    f'Invalid minimum grade syntax "{alternative}"; use cc@A or any@A.',
                ))
            try:
                minimum = normalize_minimum_rating(rating_text)
            except ValueError:
                raise ParseError(_pick(
                    locale,
                    f"最低评级「{rating_text}」无效；可用 {valid_ratings}",
                    f'Invalid minimum rating "{rating_text}"; use {valid_ratings}.',
                )) from None
            alternative = attribute_text

        if alternative.lower() in _ANY_WORDS:
            slug = ANY_ATTRIBUTE
        else:
            slug = _resolve_attr(alternative, kind, locale)
        if slug in alternatives:
            raise ParseError(_pick(
                locale,
                f"「{token}」格式错误：同一 OR 位置内的备选必须解析为不同词条，"
                "不同别名指向同一词条也算重复",
                f'Invalid group "{token}": alternatives within one OR position '
                "must resolve to different attributes; aliases for the same "
                "attribute are duplicates.",
            ))
        alternatives.append(slug)
        if minimum is not None:
            ratings[slug] = minimum
    if ANY_ATTRIBUTE in alternatives and len(alternatives) > 1:
        raise ParseError(_pick(
            locale,
            f"任意必须单独占一个{kind}位置，不能与其他词条组成 OR",
            "any must occupy a position by itself and cannot be combined with another OR alternative.",
        ))
    groups, normalized_ratings = rated_groups_as_lists(
        [alternatives], [ratings])
    return groups[0], (normalized_ratings[0] if normalized_ratings else {})


def _tokenize(rest: str, locale: str) -> list[str]:
    """按空格/加号切分 AND 位置，同时保留位置内的斜杠 OR。"""
    rest = rest.strip()
    if not rest:
        return []
    # 先在原文中拒绝旧形态语法，避免把其中的 + 当作 AND 分隔符吞掉。
    if _DELETED_SHAPE_RE.search(rest):
        raise ParseError(_pick(
            locale,
            "已删除 2+1/2-1 语法；正词条位置数量本身决定匹配两正或三正",
            "The 2+1/2-1 syntax was removed; the number of positive positions selects a two- or three-positive riven.",
        ))
    def protect(match: re.Match[str]) -> str:
        value = next(group for group in match.groups() if group is not None)
        return (value.replace(" ", _PROTECTED_SPACE)
                .replace("/", _PROTECTED_SLASH)
                .replace("+", _PROTECTED_PLUS))

    # 先保护引号内的数据库名称，再清理 OR 分隔符两侧的空白。否则
    # ``"Fire Rate / Attack Speed"`` 会被改写，无法按英文标准名解析。
    protected = _QUOTED_RE.sub(protect, rest)
    protected = re.sub(r"\s*/\s*", "/", protected)
    # 最低评级中的 + 属于评级本身；其后的第二个 + 仍可作为 AND 别名，
    # 因而 ``暴击@A++多重@B+`` 仍能按两个位置解析。
    protected = re.sub(
        r"(?i)(@[a-z])\+",
        lambda match: match.group(1) + _PROTECTED_PLUS,
        protected,
    )
    if '"' in protected or "“" in protected or "”" in protected:
        raise ParseError(_pick(locale, "引号未闭合", "A quoted name is not closed."))
    return [
        token.replace(_PROTECTED_SPACE, " ").replace(_PROTECTED_PLUS, "+")
        for token in re.split(r"(?:\s+|\+)", protected) if token
    ]


def parse_add(text: str, *, locale: str = "zh") -> AddArgs:
    locale = "en" if locale == "en" else "zh"
    text = normalize(text)
    if not text:
        command = command_example_name("sniper.add")
        if command is None:
            raise ParseError(texts.render("通用.缺少英文简写", locale=locale))
        raise ParseError(_pick(
            locale,
            f"示例：\n{command} Torid cc ms -any\n"
            "填写 2 或 3 个正词条；-any 表示任意负词条，不写则要求无负词条\n"
            f"评级与备选示例：\n{command} Torid cc@A/ms@B cd -z/r unrolled\n"
            "空格表示同时匹配，/ 表示任选一个；unrolled 表示 0 洗",
            f"Example:\n{command} Torid cc ms -any\n"
            "Use 2 or 3 positive stats. -any requires a negative stat; omit it to require none.\n"
            f"Grades and alternatives:\n{command} Torid cc@A/ms@B cd -z/r unrolled\n"
            "Spaces require separate stats; / separates alternatives. unrolled means zero rerolls.",
        ))

    args = AddArgs()
    args.weapon, args.wildcard, rest = _split_weapon(text, locale)

    for token in _tokenize(rest, locale):
        deleted_error = _deleted_syntax_error(token, locale)
        if deleted_error:
            raise ParseError(deleted_error)
        if token == "0洗" or token.casefold() == "unrolled":
            args.zero_rerolls = True
        elif token.startswith("-"):
            group, ratings = _resolve_group(token[1:], "负词条", locale)
            args.negatives.append(group)
            args.negative_ratings.append(ratings)
        elif token.isdigit():
            raise ParseError(_pick(
                locale,
                f"不认识参数「{token}」；狙击配置已不支持价格条件",
                f'Unknown parameter "{token}"; sniper rules no longer support price conditions.',
            ))
        else:
            group, ratings = _resolve_group(token, "正词条", locale)
            args.positives.append(group)
            args.positive_ratings.append(ratings)

    args.positives, args.positive_ratings = rated_groups_as_lists(
        args.positives, args.positive_ratings)
    args.negatives, args.negative_ratings = rated_groups_as_lists(
        args.negatives, args.negative_ratings)
    _validate(args, locale)
    return args


def _validate(args: AddArgs, locale: str) -> None:
    positive_count = len(args.positives)
    if positive_count not in (2, 3):
        raise ParseError(_pick(
            locale,
            f"每条配置必须填写 2 至 3 个正词条位置（当前 {positive_count} 个）；"
            "空格表示 AND，/ 表示同一位置的 OR",
            f"Each rule needs two or three positive positions (currently {positive_count}); "
            "spaces mean AND and / means OR within one position.",
        ))
    if len(args.negatives) > 1:
        raise ParseError(_pick(
            locale,
            "紫卡最多设置 1 个负词条位置；多个可接受负词条请写在同一位置并用 / 连接，"
            "如 -z/r",
            "A rule can contain at most one negative position. Join acceptable alternatives "
            "with / in that position, such as -z/r.",
        ))
    if not groups_satisfiable(normalize_groups(args.positives)):
        raise ParseError(_pick(
            locale,
            "正词条位置互相冲突，无法分别匹配到不同的实际词条",
            "The positive positions conflict and cannot match distinct actual attributes.",
        ))
