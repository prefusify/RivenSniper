"""狙击规则匹配与配置覆盖检测。

正、负词条统一使用“位置 AND、位置内 OR”的规范结构，并通过一对一分配
匹配实际词条及其最低评级。配置覆盖检测复用完全相同的组语义。
"""

from __future__ import annotations

from . import rivendata
from .criteria import normalized_config, rated_groups_cover, rated_groups_match
from .grading import GradeResult, grade_auction_item


def _match_normalized_item(
    normalized: dict,
    item: dict,
    grade_by_attribute: dict[tuple[str, bool], object | None] | None = None,
) -> bool:
    weapon_slug = item.get("weapon_url_name", "")
    if normalized["weapon"]:
        if weapon_slug != normalized["weapon"]:
            return False
    elif normalized["wildcard"]:
        if not rivendata.weapon_matches_wildcard(
                weapon_slug, normalized["wildcard"]):
            return False

    grade_by_attribute = grade_by_attribute or {}
    attrs = item.get("attributes") or []
    listing_pos = [
        (str(attr["url_name"]), grade_by_attribute.get((
            str(attr["url_name"]), True)))
        for attr in attrs
        if attr.get("positive") and attr.get("url_name")
    ]
    listing_neg = [
        (str(attr["url_name"]), grade_by_attribute.get((
            str(attr["url_name"]), False)))
        for attr in attrs
        if not attr.get("positive") and attr.get("url_name")
    ]
    if not rated_groups_match(
            normalized["positives"], normalized["positive_ratings"],
            listing_pos):
        return False
    # 空负词条位置是明确的“必须无负词条”；一个负位置则必须消费一个真实负词条。
    if not rated_groups_match(
            normalized["negatives"], normalized["negative_ratings"],
            listing_neg):
        return False

    if normalized["zero_rerolls"] and item.get("re_rolls") != 0:
        return False
    return True


def match_config(
    config: dict,
    auction: dict,
    result: GradeResult | None = None,
) -> bool:
    """判断一条 WFM 拍卖是否命中一条狙击配置。不含卖家黑名单。"""
    if not config.get("enabled", 1):
        return False

    item = auction.get("item") or {}
    if item.get("type") != "riven":
        return False

    normalized = normalized_config(config)
    has_minimum = any(normalized["positive_ratings"]) \
        or any(normalized["negative_ratings"])
    if has_minimum and result is None:
        result = grade_auction_item(item)
    grades = {
        (stat.slug, stat.positive): stat.grade
        for stat in (result.stats if result is not None else ())
    }
    return _match_normalized_item(normalized, item, grades)


def match_channel_card(config: dict, card: dict) -> bool:
    """判断游戏频道解码出的紫卡是否命中一条狙击配置。"""
    if not config.get("enabled", 1):
        return False
    attributes = []
    grades: dict[tuple[str, bool], object | None] = {}
    for stat in card.get("stats") or []:
        slug = rivendata.attribute_slug_from_ref(str(stat.get("ref") or ""))
        if slug is None:
            return False
        positive = not bool(stat.get("is_curse"))
        attributes.append({
            "url_name": slug,
            "positive": positive,
        })
        grades[(slug, positive)] = stat.get("grade")
    return _match_normalized_item(normalized_config(config), {
        "type": "riven",
        "weapon_url_name": str(card.get("weapon_slug") or ""),
        "re_rolls": card.get("rerolls"),
        "attributes": attributes,
    }, grades)


def _scope_covers(a: dict, b: dict) -> bool:
    """a 的武器范围是否覆盖 b；all > 类型 > 该类型具体武器。"""
    if a.get("weapon"):
        return a["weapon"] == b.get("weapon")

    wildcard = a.get("wildcard") or "all"
    if wildcard == "all":
        return True
    if b.get("weapon"):
        return rivendata.weapon_matches_wildcard(b["weapon"], wildcard)
    return b.get("wildcard") == wildcard


def config_subsumes(a: dict, b: dict) -> bool:
    """配置 a 的全部匹配结果是否覆盖配置 b 的全部匹配结果。"""
    a_normalized = normalized_config(a)
    b_normalized = normalized_config(b)
    if not _scope_covers(a_normalized, b_normalized):
        return False
    if not rated_groups_cover(
            a_normalized["positives"], a_normalized["positive_ratings"],
            b_normalized["positives"], b_normalized["positive_ratings"]):
        return False
    if not rated_groups_cover(
            a_normalized["negatives"], a_normalized["negative_ratings"],
            b_normalized["negatives"], b_normalized["negative_ratings"]):
        return False
    # 不限制洗练的配置覆盖“0洗”；反向不成立。
    if a_normalized["zero_rerolls"] and not b_normalized["zero_rerolls"]:
        return False
    return True
