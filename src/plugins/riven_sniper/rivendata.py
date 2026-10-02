"""静态数据访问：武器、词条、基准值、别名。"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .criteria import ANY_ATTRIBUTE

DATA_DIR = Path(__file__).resolve().parents[3] / "data"

_SPECIAL_ATTRIBUTES = {
    ANY_ATTRIBUTE: {
        "name_zh": "任意",
        "name_en": "Any",
    },
}

# WFM rivenType/group -> 游戏内紫卡类别（riven_values.json 的键）
# 注意：WFM 把大型枪械的 rivenType 标为 rifle，必须先按 group 判断 archgun
RIVEN_CATEGORY = {
    "archgun": "LotusArchgunRandomModRare",
    "rifle": "LotusRifleRandomModRare",
    "shotgun": "LotusShotgunRandomModRare",
    "pistol": "LotusPistolRandomModRare",
    "melee": "PlayerMeleeWeaponRandomModRare",
    "kitgun": "LotusModularPistolRandomModRare",
    "zaw": "LotusModularMeleeRandomModRare",
}

_WILDCARD_SYNTAX_ZH = {
    "all": "全部", "rifle": "步枪", "shotgun": "霰弹枪", "pistol": "手枪",
    "melee": "近战", "archgun": "空战", "kitgun": "组合枪", "zaw": "自制近战",
}


def _load(name: str) -> dict:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def weapons() -> dict:
    return _load("weapons.json")


@lru_cache(maxsize=1)
def attributes() -> dict:
    return _load("attributes.json")


def attribute_catalog() -> dict:
    """返回可用于狙击条件的真实词条和特殊词条。"""
    return {**attributes(), **_SPECIAL_ATTRIBUTES}


def builtin_attribute_aliases(slug: str, language: str = "zh") -> list[str]:
    """返回特殊词条始终可识别并展示的内置别名。"""
    meta = _SPECIAL_ATTRIBUTES.get(slug) or {}
    alternate = meta.get("name_zh" if language == "en" else "name_en")
    return [alternate.lower()] if alternate else []


def attribute_short_name(slug: str, language: str = "en") -> str:
    """从可解析名称与别名中选最短简写，同长度优先沿用词库展示顺序。"""
    meta = attribute_catalog()[slug]
    preferred = aliases().get("attribute_display", {}).get(slug, [])
    candidates = [*preferred, meta.get(f"name_{language}", ""),
                  *(alias for alias, target in aliases()["attributes"].items()
                    if target == slug)]
    available = list(dict.fromkeys(
        name.lower() for name in candidates if name
        and _syntax_token_safe(name)
        and (name.isascii() if language == "en" else not name.isascii())
        and resolve_attribute(name) == slug
    ))
    if available:
        return min(available, key=len)
    return attribute_syntax_name(slug, language)


@lru_cache(maxsize=1)
def attribute_abbreviations() -> dict[str, str]:
    return _load("attribute_abbreviations.json")


@lru_cache(maxsize=1)
def riven_values() -> dict:
    return _load("riven_values.json")


@lru_cache(maxsize=1)
def aliases() -> dict:
    return _load("aliases.json")


def _syntax_token_safe(value: str) -> bool:
    """标准配置中的单个名称不能占用 AND/OR/正负号分隔符。"""
    return bool(value) and not any(char.isspace() or char in "/+-!" for char in value)


@lru_cache(maxsize=1)
def _attribute_syntax_names() -> dict[str, str]:
    """词条 slug -> 不依赖可编辑别名的稳定标准语法名。"""
    result: dict[str, str] = {}
    occupied: dict[str, str] = {}
    for slug, meta in attributes().items():
        zh = meta.get("name_zh") or ""
        candidates = [zh, *(part.strip() for part in zh.split("/")), slug]
        name = next((candidate for candidate in candidates
                     if _syntax_token_safe(candidate)), None)
        if not name:
            raise ValueError(f"词条缺少可用于标准语法的名称: {slug}")
        key = name.lower()
        if key in occupied and occupied[key] != slug:
            raise ValueError(f"标准词条语法名冲突: {name}")
        occupied[key] = slug
        result[slug] = name
    return result


@lru_cache(maxsize=1)
def _attribute_syntax_index() -> dict[str, str]:
    result = {name.lower(): slug for slug, name in _attribute_syntax_names().items()}
    for slug, meta in attributes().items():
        for name in (meta.get("name_zh"), meta.get("name_en")):
            if name:
                result[name.strip().lower()] = slug
    for slug, meta in _SPECIAL_ATTRIBUTES.items():
        for name in (meta.get("name_zh"), meta.get("name_en")):
            if name:
                result[name.strip().lower()] = slug
    return result


@lru_cache(maxsize=1)
def _weapon_name_index() -> dict[str, str]:
    """中英文名/别名（小写）-> slug"""
    idx: dict[str, str] = {}
    for slug, w in weapons().items():
        idx[slug] = slug
        if w.get("name_zh"):
            idx[w["name_zh"].lower()] = slug
        if w.get("name_en"):
            idx[w["name_en"].lower()] = slug
            idx[w["name_en"].lower().replace(" ", "_")] = slug
    for alias, slug in aliases().get("weapons", {}).items():
        idx[alias.lower()] = slug
    return idx


def resolve_weapon(text: str) -> str | None:
    """解析武器输入 -> slug；不匹配返回 None。"""
    return _weapon_name_index().get(text.strip().lower())


def resolve_wildcard(text: str) -> str | None:
    """解析武器类型通配符 -> all/rifle/shotgun/pistol/melee/archgun/kitgun/zaw"""
    token = text.strip().lower()
    for wildcard, name in _WILDCARD_SYNTAX_ZH.items():
        if token in {wildcard, name.lower()}:
            return wildcard
    return aliases()["wildcards"].get(token)


def resolve_attribute(text: str) -> str | None:
    """解析词条别名 -> WFM slug"""
    t = text.strip().lower()
    if t in attributes():
        return t
    if t in _attribute_syntax_index():
        return _attribute_syntax_index()[t]
    return aliases()["attributes"].get(t)


def weapon_category(slug: str) -> str | None:
    """武器 slug -> 紫卡基准值类别键。group 优先（archgun 特例）。"""
    w = weapons().get(slug)
    if not w:
        return None
    if w.get("group") == "archgun":
        return RIVEN_CATEGORY["archgun"]
    return RIVEN_CATEGORY.get(w.get("riven_type") or "")


def weapon_matches_wildcard(slug: str, wildcard: str) -> bool:
    w = weapons().get(slug)
    if not w:
        return False
    if wildcard == "all":
        return True
    if wildcard == "archgun":
        return w.get("group") == "archgun"
    return w.get("riven_type") == wildcard and w.get("group") != "archgun"


def weapon_display_name(slug: str) -> str:
    w = weapons().get(slug)
    if not w:
        return slug
    zh = w.get("name_zh")
    en = w.get("name_en") or slug
    return f"{zh} ({en})" if zh else en


def weapon_syntax_name(slug: str, language: str = "zh") -> str:
    """返回可被狙击命令解析器原样读回的单语武器名。"""
    weapon = weapons().get(slug) or {}
    preferred = ((weapon.get("name_en"),)
                 if language == "en" else
                 (weapon.get("name_zh"), weapon.get("name_en")))
    for candidate in (*preferred, slug):
        if candidate and resolve_weapon(candidate) == slug:
            return candidate
    return slug


def weapon_short_name(slug: str) -> str:
    """示例使用当前可解析的最短英文武器名或别名。"""
    names = [weapon_syntax_name(slug, "en"),
             *(name for name, target in _weapon_name_index().items()
               if target == slug)]
    return min((name for name in names if name.isascii()
                and resolve_weapon(name) == slug), key=len, default=slug)


def wildcard_short_name(wildcard: str) -> str:
    names = [wildcard, *(name for name, target in aliases()["wildcards"].items()
                         if target == wildcard)]
    return min((name for name in names if name.isascii()
                and _syntax_token_safe(name)
                and resolve_wildcard(name) == wildcard), key=len)


def attribute_syntax_name(slug: str, language: str = "zh") -> str:
    """返回数据库中的对应语言词条名；有语法分隔符时用引号保护。"""
    if slug not in attribute_catalog():
        raise ValueError(f"未知词条: {slug}")
    name = attribute_name(slug, language)
    return f'"{name}"' if any(char.isspace() or char in "/+" for char in name) else name


def wildcard_syntax_name(wildcard: str, language: str = "zh") -> str:
    """返回可复制回群命令的武器类型名称。"""
    preferred = wildcard if language == "en" else _WILDCARD_SYNTAX_ZH.get(wildcard)
    if preferred and resolve_wildcard(preferred) == wildcard:
        return preferred
    for alias, target in aliases().get("wildcards", {}).items():
        if target == wildcard and _syntax_token_safe(alias):
            return alias
    return wildcard


def localized_name(zh: str | None, en: str | None, language: str,
                   fallback: str) -> str:
    """从现有双语数据中选择显示名，不维护第二套名称映射。"""
    if language == "en":
        return en or fallback
    return zh or en or fallback


def weapon_name(slug: str, language: str = "zh") -> str:
    """按语言返回武器名；名称唯一来源为 ``weapons.json``。"""
    meta = weapons().get(slug) or {}
    return localized_name(meta.get("name_zh"), meta.get("name_en"), language,
                          slug)


def attribute_name(slug: str, language: str = "zh") -> str:
    """按语言返回真实词条或特殊词条的标准名称。"""
    meta = attribute_catalog().get(slug) or {}
    return localized_name(meta.get("name_zh"), meta.get("name_en"), language,
                          slug)


def attribute_card_name(slug: str, language: str = "zh") -> str:
    """返回卡图词条名；英文卡使用数据表维护的固定简写。"""
    meta = attributes().get(slug) or {}
    if language == "en":
        return (attribute_abbreviations().get(slug) or meta.get("name_en")
                or slug)
    return meta.get("name_zh") or meta.get("name_en") or slug


# WFM 词条的 gameRef 是枪械版 tag；近战/自制近战基准表用独立的 Melee 版 tag
_MELEE_TAG_MAP = {
    "WeaponDamageAmountMod": "WeaponMeleeDamageMod",
    "WeaponFactionDamageGrineer": "WeaponMeleeFactionDamageGrineer",
    "WeaponFactionDamageCorpus": "WeaponMeleeFactionDamageCorpus",
    "WeaponFactionDamageInfested": "WeaponMeleeFactionDamageInfested",
}


@lru_cache(maxsize=1)
def _attribute_ref_index() -> dict[str, str]:
    """游戏内部词条引用 -> 现有 WFM 词条 slug。"""
    result = {
        meta["game_ref"]: slug
        for slug, meta in attributes().items()
        if meta.get("game_ref")
    }
    for standard_ref, melee_ref in _MELEE_TAG_MAP.items():
        if standard_ref in result:
            result[melee_ref] = result[standard_ref]
    return result


def attribute_slug_from_ref(game_ref: str) -> str | None:
    """把频道紫卡解码词条归一到现有词条表；未知引用不泄露到页面。"""
    return _attribute_ref_index().get(game_ref)


def invalidate_caches():
    """词库文件被修改后调用，否则改动不生效（本模块全部 lru_cache）。"""
    for fn in (weapons, attributes, attribute_abbreviations, riven_values,
               aliases, _weapon_name_index,
               _attribute_syntax_names, _attribute_syntax_index,
               _attribute_ref_index):
        fn.cache_clear()


def _save_aliases(data: dict):
    """原子写回 aliases.json 并失效缓存。"""
    import os
    import tempfile
    path = DATA_DIR / "aliases.json"
    fd, tmp = tempfile.mkstemp(dir=str(DATA_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    invalidate_caches()


def add_attribute_alias(alias: str, slug: str, show_in_list: bool = False):
    """新增词条别名。冲突/无效时抛 ValueError。"""
    alias = alias.strip().lower()
    if not alias:
        raise ValueError("别名不能为空")
    if slug not in attribute_catalog():
        raise ValueError(f"标准词条不存在: {slug}")
    data = _load("aliases.json")
    existing = data["attributes"].get(alias)
    if existing and existing != slug:
        raise ValueError(f"别名「{alias}」已映射到 {existing}，请先删除")
    standard_slug = _attribute_syntax_index().get(alias)
    if ((alias in attributes() and alias != slug)
            or (standard_slug is not None and standard_slug != slug)):
        raise ValueError(f"「{alias}」是标准词条名，不能作为其他词条的别名")
    data["attributes"][alias] = slug
    display = data.setdefault("attribute_display", {}).setdefault(slug, [])
    if show_in_list and alias not in display:
        display.append(alias)
    if not show_in_list and alias in display:
        display.remove(alias)
    _save_aliases(data)


def update_attribute_alias(alias: str, new_alias: str | None = None,
                           show_in_list: bool | None = None):
    """修改词条别名（改名/切换展示）。别名不存在抛 KeyError，校验失败抛 ValueError。"""
    alias = alias.strip().lower()
    data = _load("aliases.json")
    if alias not in data["attributes"]:
        raise KeyError(f"别名不存在: {alias}")
    slug = data["attributes"][alias]
    display = data.setdefault("attribute_display", {}).setdefault(slug, [])
    was_shown = alias in display

    target = alias
    if new_alias:
        new_alias = new_alias.strip().lower()
        if not new_alias:
            raise ValueError("新别名不能为空")
        if new_alias != alias:
            existing = data["attributes"].get(new_alias)
            if existing and existing != slug:
                raise ValueError(f"别名「{new_alias}」已映射到 {existing}，请先删除")
            standard_slug = _attribute_syntax_index().get(new_alias)
            if ((new_alias in attributes() and new_alias != slug)
                    or (standard_slug is not None and standard_slug != slug)):
                raise ValueError(f"「{new_alias}」是标准词条名，不能作为其他词条的别名")
            del data["attributes"][alias]
            data["attributes"][new_alias] = slug
            if alias in display:
                display.remove(alias)
            target = new_alias

    shown = was_shown if show_in_list is None else show_in_list
    if shown and target not in display:
        display.append(target)
    if not shown and target in display:
        display.remove(target)
    _save_aliases(data)


def remove_attribute_alias(alias: str) -> bool:
    alias = alias.strip().lower()
    data = _load("aliases.json")
    if alias not in data["attributes"]:
        return False
    slug = data["attributes"].pop(alias)
    display = data.get("attribute_display", {}).get(slug, [])
    if alias in display:
        display.remove(alias)
    _save_aliases(data)
    return True


def add_weapon_alias(alias: str, slug: str):
    alias = alias.strip().lower()
    if not alias:
        raise ValueError("别名不能为空")
    if slug not in weapons():
        raise ValueError(f"武器不存在: {slug}")
    data = _load("aliases.json")
    data.setdefault("weapons", {})[alias] = slug
    _save_aliases(data)


def update_weapon_alias(alias: str, new_alias: str | None = None,
                        slug: str | None = None):
    """修改武器别名（改名/改目标武器）。不存在抛 KeyError，校验失败抛 ValueError。"""
    alias = alias.strip().lower()
    data = _load("aliases.json")
    weapons_map = data.setdefault("weapons", {})
    if alias not in weapons_map:
        raise KeyError(f"武器别名不存在: {alias}")
    target_slug = slug or weapons_map[alias]
    if target_slug not in weapons():
        raise ValueError(f"武器不存在: {target_slug}")
    target_name = (new_alias or alias).strip().lower()
    if not target_name:
        raise ValueError("新别名不能为空")
    if target_name != alias and target_name in weapons_map:
        raise ValueError(f"武器别名「{target_name}」已存在")
    del weapons_map[alias]
    weapons_map[target_name] = target_slug
    _save_aliases(data)


def remove_weapon_alias(alias: str) -> bool:
    alias = alias.strip().lower()
    data = _load("aliases.json")
    if alias not in data.get("weapons", {}):
        return False
    del data["weapons"][alias]
    _save_aliases(data)
    return True


# 通配符类别（武器种类；all=全部武器）的规范键 -> 中文名。
WILDCARD_CATEGORY_ZH = {
    "all": "全部武器", "rifle": "步枪", "shotgun": "霰弹枪", "pistol": "手枪",
    "melee": "近战", "archgun": "曲翼枪械", "kitgun": "组合枪", "zaw": "自制近战",
}

# WFM 返回的紫卡极性展示名；不是可编辑的输入别名。
POLARITY_CATEGORY_ZH = {
    "madurai": "V槽", "vazarin": "D槽", "naramon": "杠槽", "zenurik": "Z槽",
}


def add_wildcard_alias(alias: str, category: str):
    """新增武器类型别名；冲突或类别非法时抛 ValueError。"""
    alias = alias.strip().lower()
    if not alias:
        raise ValueError("别名不能为空")
    if category not in WILDCARD_CATEGORY_ZH:
        raise ValueError(f"类别不存在: {category}")
    data = _load("aliases.json")
    sect = data.setdefault("wildcards", {})
    existing = sect.get(alias)
    if existing and existing != category:
        name = WILDCARD_CATEGORY_ZH.get(existing, existing)
        raise ValueError(f"别名「{alias}」已映射到 {name}，请先删除")
    sect[alias] = category
    _save_aliases(data)


def update_wildcard_alias(
    alias: str, new_alias: str | None = None, category: str | None = None,
):
    """修改武器类型别名；不存在抛 KeyError，非法抛 ValueError。"""
    alias = alias.strip().lower()
    data = _load("aliases.json")
    sect = data.setdefault("wildcards", {})
    if alias not in sect:
        raise KeyError(f"别名不存在: {alias}")
    target_cat = category or sect[alias]
    if target_cat not in WILDCARD_CATEGORY_ZH:
        raise ValueError(f"类别不存在: {target_cat}")
    target_name = (new_alias or alias).strip().lower()
    if not target_name:
        raise ValueError("新别名不能为空")
    if target_name != alias and target_name in sect:
        raise ValueError(f"别名「{target_name}」已存在")
    del sect[alias]
    sect[target_name] = target_cat
    _save_aliases(data)


def remove_wildcard_alias(alias: str) -> bool:
    alias = alias.strip().lower()
    data = _load("aliases.json")
    sect = data.setdefault("wildcards", {})
    if alias not in sect:
        return False
    del sect[alias]
    _save_aliases(data)
    return True


def base_value(category: str, game_ref: str) -> float | None:
    entries = {e["tag"]: e["value"] for e in riven_values().get(category, [])}
    v = entries.get(game_ref)
    if v is None:
        v = entries.get(_MELEE_TAG_MAP.get(game_ref, ""))
    return v
