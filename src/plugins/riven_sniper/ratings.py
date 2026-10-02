"""紫卡最低评级的统一顺序与比较语义。"""

from __future__ import annotations


RATING_ORDER = (
    "F", "C-", "C", "C+", "B-", "B", "B+", "A-", "A", "A+", "S",
)
RATINGS_DESCENDING = tuple(reversed(RATING_ORDER))
VALID_RATINGS = frozenset(RATING_ORDER)
_RATING_RANK = {rating: index for index, rating in enumerate(RATING_ORDER)}


def normalize_minimum_rating(value: object | None) -> str | None:
    """规范化配置里的最低评级；空值表示不限制。"""
    if value is None or not str(value).strip():
        return None
    rating = str(value).strip().upper()
    if rating not in VALID_RATINGS:
        raise ValueError(f"无效的最低评级: {value}")
    return rating


def rating_meets_minimum(
    actual: object | None,
    minimum: object | None,
) -> bool:
    """实际评级是否达到下限；X/? 等不可评分结果不满足有限制的条件。"""
    required = normalize_minimum_rating(minimum)
    if required is None:
        return True
    actual_rating = str(actual or "").strip().upper()
    if actual_rating not in VALID_RATINGS:
        return False
    return _RATING_RANK[actual_rating] >= _RATING_RANK[required]
