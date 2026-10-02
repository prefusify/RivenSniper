"""狙击配置的统一规范化语义。

词条条件按“位置”存储：外层列表是 AND，内层列表是同一位置的 OR。
``ANY_ATTRIBUTE`` 表示该位置可匹配任意一个尚未被其他位置占用的真实词条。
最低评级绑定位置内的 OR 备选；``ANY_ATTRIBUTE`` 的最低评级应用于该位置
最终占用的真实词条。解析、存储、匹配、查重、覆盖检测和展示都必须先经过
本模块规范化。
"""

from __future__ import annotations

from functools import lru_cache
from itertools import combinations, product
from typing import Iterable, Sequence

from .ratings import RATING_ORDER, normalize_minimum_rating, rating_meets_minimum


ANY_ATTRIBUTE = "__any_attribute__"

Group = tuple[str, ...]
Groups = tuple[Group, ...]
RatingMap = tuple[tuple[str, str], ...]
RatedGroup = tuple[Group, RatingMap]
RatedGroups = tuple[RatedGroup, ...]


def _normalize_group(item: object) -> Group:
    values = item if isinstance(item, (list, tuple, set)) else (item,)
    cleaned = {
        str(value).strip()
        for value in values
        if value is not None and str(value).strip()
    }
    if not cleaned:
        return ()
    return (ANY_ATTRIBUTE,) if ANY_ATTRIBUTE in cleaned else tuple(sorted(cleaned))


def normalize_groups(raw: Iterable[object] | None) -> Groups:
    """把词条位置规范化为可比较的 tuple，保留重复位置。

    一维旧数据 ``["cc", "ms"]`` 会被解释为两个单选位置；新数据使用
    ``[["cc", "ms"], ["fire_rate"]]`` 表示 ``cc/ms fire_rate``。
    位置内出现“任意”时，其余备选没有额外意义，统一折叠为单独的任意位置。
    """
    groups: list[Group] = []
    for item in raw or ():
        group = _normalize_group(item)
        if not group:
            continue
        groups.append(group)
    # 位置没有先后语义；排序得到稳定的查重键和标准展示。重复位置不能删除，
    # 因为“任意 任意”明确要求占用两个不同的真实词条。
    return tuple(sorted(groups))


def groups_as_lists(groups: Iterable[object] | None) -> list[list[str]]:
    """返回适合 JSON 持久化的规范化位置列表。"""
    return [list(group) for group in normalize_groups(groups)]


def _normalize_rating_map(group: Group, raw: object | None) -> RatingMap:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        if raw:
            raise ValueError("词条评级映射必须是对象")
        return ()
    ratings: dict[str, str] = {}
    for key, value in raw.items():
        slug = str(key).strip()
        rating = normalize_minimum_rating(value)
        if rating is None:
            continue
        if slug not in group:
            raise ValueError(f"最低评级未绑定当前 OR 位置中的词条: {slug}")
        ratings[slug] = rating
    return tuple(sorted(ratings.items()))


def normalize_rated_groups(
    raw_groups: Iterable[object] | None,
    raw_ratings: Iterable[object] | None = None,
) -> RatedGroups:
    """规范化词条位置及每个 OR 备选绑定的最低评级。"""
    group_items = list(raw_groups or ())
    rating_items = list(raw_ratings or ())
    if any(rating_items[len(group_items):]):
        raise ValueError("最低评级没有对应的词条位置")

    specs: list[RatedGroup] = []
    for index, item in enumerate(group_items):
        group = _normalize_group(item)
        raw_rating = rating_items[index] if index < len(rating_items) else None
        if not group:
            if raw_rating:
                raise ValueError("最低评级没有对应的词条位置")
            continue
        specs.append((group, _normalize_rating_map(group, raw_rating)))
    return tuple(sorted(specs))


def rated_groups_as_lists(
    groups: Iterable[object] | None,
    ratings: Iterable[object] | None = None,
) -> tuple[list[list[str]], list[dict[str, str]]]:
    """返回适合 JSON 持久化的词条位置及评级映射。"""
    normalized = normalize_rated_groups(groups, ratings)
    group_lists = [list(group) for group, _ in normalized]
    rating_lists = [dict(rating_map) for _, rating_map in normalized]
    while rating_lists and not rating_lists[-1]:
        rating_lists.pop()
    return group_lists, rating_lists


def normalized_config(config: dict) -> dict:
    """提取一条配置当前仍生效的规范字段。"""
    positives, positive_ratings = rated_groups_as_lists(
        config.get("positives"), config.get("positive_ratings"))
    negatives, negative_ratings = rated_groups_as_lists(
        config.get("negatives"), config.get("negative_ratings"))
    return {
        "weapon": config.get("weapon"),
        "wildcard": config.get("wildcard"),
        "positives": positives,
        "positive_ratings": positive_ratings,
        "negatives": negatives,
        "negative_ratings": negative_ratings,
        "zero_rerolls": bool(config.get("zero_rerolls")),
    }


def criteria_key(config: dict) -> tuple:
    """忽略最低评级的配置身份键；用于禁止同词条规则重复添加。"""
    normalized = normalized_config(config)
    return (
        normalized["weapon"],
        normalized["wildcard"],
        normalize_groups(normalized["positives"]),
        normalize_groups(normalized["negatives"]),
        normalized["zero_rerolls"],
    )


def groups_match(groups: Iterable[object] | None,
                 actual: Sequence[str]) -> bool:
    """判断每个位置能否一对一匹配一个真实词条。

    位置数量必须与真实词条数量一致，因此两正位置不会匹配三正结果；任意位置
    也不能匹配为空，且只能消费一个尚未被其他位置消费的真实词条。
    """
    normalized = normalize_groups(groups)
    values = tuple(
        str(value) for value in actual
        if value is not None and str(value)
    )
    if len(normalized) != len(values):
        return False

    candidates: list[tuple[int, ...]] = []
    for group in normalized:
        indexes = tuple(
            index for index, value in enumerate(values)
            if ANY_ATTRIBUTE in group or value in group
        )
        if not indexes:
            return False
        candidates.append(indexes)

    # 候选最少的位置先分配，避免 OR/任意位置抢走更受限位置唯一能用的词条。
    order = sorted(range(len(normalized)), key=lambda index: len(candidates[index]))

    def assign(position: int, used: set[int]) -> bool:
        if position == len(order):
            return True
        group_index = order[position]
        for actual_index in candidates[group_index]:
            if actual_index in used:
                continue
            used.add(actual_index)
            if assign(position + 1, used):
                return True
            used.remove(actual_index)
        return False

    return assign(0, set())


def _rated_groups_match_normalized(
    groups: RatedGroups,
    actual: Sequence[tuple[str, object | None]],
) -> bool:
    if len(groups) != len(actual):
        return False

    candidates: list[tuple[int, ...]] = []
    for group, rating_map in groups:
        minimums = dict(rating_map)
        is_any = ANY_ATTRIBUTE in group
        indexes = tuple(
            index for index, (slug, rating) in enumerate(actual)
            if (is_any or slug in group)
            and rating_meets_minimum(
                rating, minimums.get(ANY_ATTRIBUTE if is_any else slug))
        )
        if not indexes:
            return False
        candidates.append(indexes)

    order = sorted(range(len(groups)), key=lambda index: len(candidates[index]))

    def assign(position: int, used: set[int]) -> bool:
        if position == len(order):
            return True
        group_index = order[position]
        for actual_index in candidates[group_index]:
            if actual_index in used:
                continue
            used.add(actual_index)
            if assign(position + 1, used):
                return True
            used.remove(actual_index)
        return False

    return assign(0, set())


def rated_groups_match(
    groups: Iterable[object] | None,
    ratings: Iterable[object] | None,
    actual: Sequence[tuple[str, object | None]],
) -> bool:
    """按同一一对一 OR/AND 语义匹配词条及其最低评级。"""
    normalized_actual = tuple(
        (str(slug), rating) for slug, rating in actual
        if slug is not None and str(slug)
    )
    return _rated_groups_match_normalized(
        normalize_rated_groups(groups, ratings), normalized_actual)


def groups_satisfiable(groups: Iterable[object] | None) -> bool:
    """判断一组位置是否至少存在一种可一对一匹配的真实词条组合。"""
    normalized = normalize_groups(groups)
    if not normalized:
        return True
    explicit = sorted({
        slug for group in normalized for slug in group if slug != ANY_ATTRIBUTE
    })
    universe = explicit + [f"__other_{index}__" for index in range(len(normalized))]
    return any(groups_match(normalized, values)
               for values in combinations(universe, len(normalized)))


@lru_cache(maxsize=512)
def _groups_cover_cached(a: Groups, b: Groups) -> bool:
    if len(a) != len(b):
        return False
    if not a:
        return True

    # 显式词条之外的值对规则而言彼此等价；加入 n 个不同的 OTHER 哨兵，
    # 即可完整覆盖任意位置可能消费的所有未命名真实词条组合。
    explicit = sorted({
        slug for groups in (a, b) for group in groups for slug in group
        if slug != ANY_ATTRIBUTE
    })
    universe = explicit + [f"__other_{index}__" for index in range(len(a))]
    for values in combinations(universe, len(a)):
        if groups_match(b, values) and not groups_match(a, values):
            return False
    return True


def groups_cover(a: Iterable[object] | None,
                 b: Iterable[object] | None) -> bool:
    """a 的全部词条匹配结果是否覆盖 b 的全部匹配结果。"""
    return _groups_cover_cached(normalize_groups(a), normalize_groups(b))


@lru_cache(maxsize=512)
def _rated_groups_cover_cached(a: RatedGroups, b: RatedGroups) -> bool:
    if len(a) != len(b):
        return False
    if not a:
        return True
    if not any(ratings for _, ratings in a + b):
        return _groups_cover_cached(
            tuple(group for group, _ in a),
            tuple(group for group, _ in b),
        )

    explicit = sorted({
        slug for groups in (a, b) for group, _ in groups for slug in group
        if slug != ANY_ATTRIBUTE
    })
    universe = explicit + [f"__other_{index}__" for index in range(len(a))]
    all_groups = a + b
    rating_domains: dict[str, tuple[object | None, ...]] = {}
    for slug in universe:
        # 对固定 slug，评级只通过“是否达到相关下限”影响候选边；保留每种
        # 布尔签名的一个代表即可。None 同时代表 X/? 等不可评分结果。
        minimums = {
            minimum
            for _group, rating_map in all_groups
            for rated_slug, minimum in rating_map
            if rated_slug in {slug, ANY_ATTRIBUTE}
        }
        representatives: dict[tuple[bool, ...], object | None] = {}
        ordered_minimums = tuple(
            rating for rating in RATING_ORDER if rating in minimums)
        for actual in (*RATING_ORDER, None):
            signature = tuple(
                rating_meets_minimum(actual, minimum)
                for minimum in ordered_minimums
            )
            representatives.setdefault(signature, actual)
        rating_domains[slug] = tuple(representatives.values())

    for slugs in combinations(universe, len(a)):
        # 评级门槛都是单调下限；若全部取 S 仍无法匹配 b，这组词条在任何
        # 评级下都不是 b 的结果，无需枚举评级。其余情况只枚举会改变 a/b
        # 任一门槛判定的等价类，而不是固定展开 12^位置数 个状态。
        best_actual = tuple((slug, "S") for slug in slugs)
        if not _rated_groups_match_normalized(b, best_actual):
            continue
        for actual_ratings in product(*(rating_domains[slug] for slug in slugs)):
            actual = tuple(zip(slugs, actual_ratings, strict=True))
            if (_rated_groups_match_normalized(b, actual)
                    and not _rated_groups_match_normalized(a, actual)):
                return False
    return True


def rated_groups_cover(
    a_groups: Iterable[object] | None,
    a_ratings: Iterable[object] | None,
    b_groups: Iterable[object] | None,
    b_ratings: Iterable[object] | None,
) -> bool:
    """a 的词条与最低评级范围是否完整覆盖 b。"""
    return _rated_groups_cover_cached(
        normalize_rated_groups(a_groups, a_ratings),
        normalize_rated_groups(b_groups, b_ratings),
    )
