"""全市场道具目录：名称、等级、gameRef 与 WFM itemId 映射。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
from functools import lru_cache
from pathlib import Path

import httpx

from . import rivendata
from .version import VERSION

FILE_NAME = "market_items.json"
ALIAS_FILE_NAME = "market_item_aliases.json"
ITEMS_URL = "https://api.warframe.market/v2/items"
REFRESH_HEADERS = {
    "User-Agent": f"rivensniper/{VERSION}",
    "Language": "zh-hans",
}

# 规范化时剔除的分隔符：空格、间隔号、连字符、下划线、括号
_NORM_RE = re.compile(r"[\s·・\-_()（）]+")


def _norm(text: str) -> str:
    return _NORM_RE.sub("", text.strip().casefold())


@lru_cache(maxsize=1)
def _data() -> dict:
    # DATA_DIR 调用时动态取自 rivendata：测试的 tmp_data fixture 同样对本模块生效
    try:
        raw = json.loads(
            (rivendata.DATA_DIR / FILE_NAME).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"fetched_at": 0, "items": {}}
    return raw


def items() -> dict[str, dict]:
    return _data()["items"]


@lru_cache(maxsize=1)
def item_aliases() -> dict[str, list[str]]:
    """返回独立维护的市场道具输入别名。"""
    try:
        raw = json.loads(
            (rivendata.DATA_DIR / ALIAS_FILE_NAME).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    aliases = raw.get("aliases")
    return aliases if isinstance(aliases, dict) else {}


def available() -> bool:
    return bool(items())


@lru_cache(maxsize=1)
def _id_index() -> dict[str, str]:
    return {v["id"]: slug for slug, v in items().items() if v.get("id")}


def id_to_slug(item_id: str) -> str | None:
    return _id_index().get(item_id)


def _max_rank(record: dict) -> int:
    value = record.get("max_rank", record.get("maxRank"))
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def item_max_rank(slug: str) -> int:
    """返回道具最高等级；无等级体系（含上游 null）统一为 0。"""
    return _max_rank(items().get(slug) or {})


def item_game_ref(slug: str) -> str | None:
    record = items().get(slug) or {}
    value = record.get("game_ref", record.get("gameRef"))
    return value if isinstance(value, str) and value else None


def _normalize_game_ref(game_ref: str) -> str:
    """把 World State 的商店包装路径还原为物品本体路径。"""
    value = game_ref.strip()
    prefix = "/Lotus/StoreItems/"
    if value.startswith(prefix):
        value = "/Lotus/" + value[len(prefix):]
    # World State 的遗物库存路径附带精炼档后缀，而 WFM 用一个基准 gameRef
    # 配合订单 subtype 表示精炼等级。
    if value.startswith("/Lotus/Types/Game/Projections/"):
        for suffix in ("Bronze", "Silver", "Gold", "Platinum"):
            if value.endswith(suffix):
                value = value[:-len(suffix)]
                break
    return value


@lru_cache(maxsize=1)
def _game_ref_index() -> dict[str, tuple[str, ...]]:
    index: dict[str, list[str]] = {}
    for slug in items():
        game_ref = item_game_ref(slug)
        if game_ref:
            for key in {game_ref, _normalize_game_ref(game_ref)}:
                index.setdefault(key, []).append(slug)
    return {key: tuple(slugs) for key, slugs in index.items()}


def game_ref_to_slugs(game_ref: str) -> tuple[str, ...]:
    value = game_ref.strip()
    return (_game_ref_index().get(value)
            or _game_ref_index().get(_normalize_game_ref(value)) or ())


@lru_cache(maxsize=1)
def _name_index() -> tuple[
    dict[str, tuple[str, ...]], list[tuple[str, str]],
]:
    """(精确索引: 规范化名->候选 slug, 子串搜索表)。"""
    exact: dict[str, list[str]] = {}
    subs: list[tuple[str, str]] = []

    def add_exact(name: str, slug: str) -> None:
        normalized = _norm(name)
        if not normalized:
            return
        targets = exact.setdefault(normalized, [])
        if slug not in targets:
            targets.append(slug)

    for slug, v in items().items():
        add_exact(slug, slug)
        for name in (v.get("zh"), v.get("en")):
            if not name:
                continue
            n = _norm(name)
            if n:
                add_exact(name, slug)
                subs.append((n, slug))
    for slug, aliases in item_aliases().items():
        if slug not in items() or not isinstance(aliases, list):
            continue
        for alias in aliases:
            if isinstance(alias, str):
                add_exact(alias, slug)
    return {name: tuple(slugs) for name, slugs in exact.items()}, subs


def resolve_item(text: str) -> list[str]:
    """道具名（中/英/slug/别名）-> 候选 slug 列表。

    名称与别名均容忍空格和分隔符差异。别名只参与精确匹配；标准中英文名
    还会做子串匹配（最多 10 个，名字短的优先）。查询过短（规范化后
    <2 字符）不做子串。
    """
    q = _norm(text)
    if not q:
        return []
    exact, subs = _name_index()
    if q in exact:
        return list(exact[q])
    if len(q) < 2:
        return []
    hits: dict[str, int] = {}  # slug -> 最短命中名长度
    for name, slug in subs:
        if q in name:
            cur = hits.get(slug)
            if cur is None or len(name) < cur:
                hits[slug] = len(name)
    ranked = sorted(hits.items(), key=lambda kv: (kv[1], kv[0]))
    return [slug for slug, _ in ranked[:10]]


def item_names(slug: str) -> tuple[str | None, str | None]:
    v = items().get(slug) or {}
    return v.get("zh"), v.get("en")


def item_display_name(slug: str) -> str:
    zh, en = item_names(slug)
    if zh and en:
        return f"{zh} ({en})"
    return zh or en or slug


def item_name(slug: str, language: str = "zh") -> str:
    """按语言返回道具名；名称唯一来源为 ``market_items.json``。"""
    zh, en = item_names(slug)
    return rivendata.localized_name(zh, en, language, slug)


def _catalog_record(item: dict, previous: dict | None = None) -> tuple[str, dict]:
    slug = item.get("slug")
    if not isinstance(slug, str) or not slug:
        raise ValueError("WFM item is missing slug")
    i18n = item.get("i18n") or {}
    raw_max_rank = item.get("maxRank", item.get("max_rank"))
    max_rank = _max_rank(item)
    # maxRank 在 v2 模型中是可选字段。已有的有等级道具若某次响应临时
    # 缺字段，不能把它解释为明确的 maxRank=0 并破坏用户的等级选择。
    if raw_max_rank in (None, "") and previous is not None:
        max_rank = _max_rank(previous)
    return slug, {
        "id": item.get("id"),
        "zh": (i18n.get("zh-hans") or {}).get("name"),
        "en": (i18n.get("en") or {}).get("name"),
        "tags": item.get("tags") or [],
        "max_rank": max_rank,
        "game_ref": item.get("gameRef", item.get("game_ref")),
    }


def _validate_catalog(projected: dict[str, dict]) -> None:
    """拒绝会破坏等级判断或 WS 映射的截断/变更响应。"""
    if not projected:
        raise ValueError("WFM item catalog is empty")
    current = items()
    if current and len(projected) < max(1, int(len(current) * 0.8)):
        raise ValueError("WFM item catalog is unexpectedly truncated")
    old_ranked = sum(_max_rank(row) > 0 for row in current.values())
    new_ranked = sum(_max_rank(row) > 0 for row in projected.values())
    if old_ranked and new_ranked < old_ranked * 0.8:
        raise ValueError("WFM item catalog lost maxRank metadata")
    for field in ("id", "en", "game_ref"):
        old_coverage = (sum(bool(row.get(field)) for row in current.values())
                        / len(current)) if current else 0.0
        new_coverage = (sum(bool(row.get(field)) for row in projected.values())
                        / len(projected))
        if old_coverage >= 0.5 and new_coverage < old_coverage * 0.8:
            raise ValueError(f"WFM item catalog lost {field} metadata")


def _replace_catalog(path: Path, payload: dict) -> None:
    """在同目录完整落盘后替换，任何写入失败都不破坏旧目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", delete=False,
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
        ) as stream:
            temp_path = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=1)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


async def refresh_catalog(client: httpx.AsyncClient | None = None) -> bool:
    """从 WFM 刷新本地目录；成功原子替换，失败时保留旧文件与缓存。

    返回 ``True`` 表示本次刷新成功，``False`` 表示请求、数据解析或写入失败。
    目录内容未变化时不重写文件，避免仅更新时间戳导致工作区产生差异。
    可由启动任务立即调用，之后由每日调度再次调用。
    """
    owned_client = client is None
    http = client or httpx.AsyncClient(headers=REFRESH_HEADERS, timeout=30)
    try:
        # 与狙击、统计、紫卡采样共用同一进程级 WFM 请求门，避免启动时
        # 目录刷新成为绕过通用限速的额外请求。
        from .wfm import observe_public_response, wait_for_public_request
        await wait_for_public_request()
        response = await http.get(ITEMS_URL)
        observe_public_response(response)
        response.raise_for_status()
        raw = response.json().get("data")
        if not isinstance(raw, list) or not raw:
            raise ValueError("WFM item catalog is empty")
        current = items()
        projected = dict(
            _catalog_record(
                item, current.get(str(item.get("slug") or "")))
            for item in raw)
        _validate_catalog(projected)
        if projected != current:
            payload = {"fetched_at": int(time.time()), "items": projected}
            path = rivendata.DATA_DIR / FILE_NAME
            await asyncio.to_thread(_replace_catalog, path, payload)
    except Exception:
        return False
    finally:
        if owned_client:
            await http.aclose()
    invalidate()
    return True


def invalidate():
    """market_items.json 更新后调用（同 rivendata.invalidate_caches 语义）。"""
    for fn in (_data, item_aliases, _id_index, _game_ref_index, _name_index):
        fn.cache_clear()
