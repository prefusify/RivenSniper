"""配置添加核心：解析、规范化、语义查重/覆盖后入库。"""

from __future__ import annotations

from . import matcher, texts
from .criteria import criteria_key, normalized_config
from .formatter import describe_config, format_config_command
from .parsing import ParseError, parse_add


def _equivalent(a: dict, b: dict) -> bool:
    """语义等价允许不同 OR 结构表达同一匹配集合。"""
    return matcher.config_subsumes(a, b) and matcher.config_subsumes(b, a)


def add_config_checked(store, config, group_id: int, text: str
                       ) -> tuple[bool, str, int | None]:
    """解析并校验后入库。返回 (ok, 面向用户的消息, 内部数据库ID)。"""
    try:
        parsed = parse_add(text, locale=texts.current_locale())
    except ParseError as error:
        return False, texts.render("狙击添加.解析失败", error=error), None

    new_config = normalized_config({
        "weapon": parsed.weapon,
        "wildcard": parsed.wildcard,
        "positives": parsed.positives,
        "positive_ratings": parsed.positive_ratings,
        "negatives": parsed.negatives,
        "negative_ratings": parsed.negative_ratings,
        "zero_rerolls": parsed.zero_rerolls,
    })
    for existing in store.list_configs(group_id):
        # 同一武器范围、词条位置和 0 洗条件只有一个配置身份；最低评级
        # 不能作为新增、覆盖或替代同词条规则的旁路。
        if (_config_key(existing) == _config_key(new_config)
                or _equivalent(existing, new_config)):
            number = existing["display_number"]
            return False, texts.render(
                "狙击添加.重复", existing=describe_config(existing), id=number), None
        if matcher.config_subsumes(existing, new_config):
            number = existing["display_number"]
            return False, texts.render(
                "狙击添加.被已有配置覆盖",
                existing=describe_config(existing), id=number), None

    limit = int(config.sniper_max_configs_per_group)
    if limit > 0 and store.count_configs(group_id) >= limit:
        return False, texts.render(
            "狙击添加.达到上限", limit=limit), None

    config_id = store.add_config(
        group_id,
        **{field: new_config[field] for field in _COPY_FIELDS},
    )
    saved = store.get_config(config_id, group_id)
    return True, texts.render(
        "狙击添加.成功", description=describe_config(saved)), config_id


def preview_config(text: str, *, locale: str | None = None) -> tuple[bool, str]:
    """仅解析并返回完整命令，不入库。"""
    locale = locale or texts.current_locale()
    try:
        parsed = parse_add(text, locale=locale)
    except ParseError as error:
        return False, str(error)
    pseudo = normalized_config({
        "weapon": parsed.weapon,
        "wildcard": parsed.wildcard,
        "positives": parsed.positives,
        "positive_ratings": parsed.positive_ratings,
        "negatives": parsed.negatives,
        "negative_ratings": parsed.negative_ratings,
        "zero_rerolls": parsed.zero_rerolls,
    })
    return True, format_config_command(pseudo, locale)


_COPY_FIELDS = (
    "weapon", "wildcard", "positives", "positive_ratings", "negatives",
    "negative_ratings", "zero_rerolls",
)


def _config_key(config: dict) -> tuple:
    return criteria_key(config)


def copy_configs(store, config, src_group: int, dst_group: int
                 ) -> tuple[int, int, int, int]:
    """把源群配置按规范语义复制到目标群，跳过重复或已被完全覆盖的配置。"""
    source = store.list_configs(src_group)
    if not source:
        return 0, 0, 0, 0
    existing = store.list_configs(dst_group)
    existing_keys = {_config_key(item) for item in existing}
    cap = config.sniper_max_configs_per_group
    copied = skipped = capped = 0
    for item in source:
        candidate = {
            **{field: item.get(field) for field in _COPY_FIELDS},
        }
        key = _config_key(candidate)
        redundant = key in existing_keys or any(
            _equivalent(other, candidate)
            or matcher.config_subsumes(other, candidate)
            for other in existing
        )
        if redundant:
            skipped += 1
            continue
        if cap > 0 and store.count_configs(dst_group) >= cap:
            capped += 1
            continue
        config_id = store.add_config(
            dst_group,
            **{field: item.get(field) for field in _COPY_FIELDS},
        )
        saved = store.get_config(config_id, dst_group)
        existing.append(saved)
        existing_keys.add(key)
        copied += 1
    return copied, skipped, capped, len(source)
