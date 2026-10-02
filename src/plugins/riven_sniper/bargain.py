"""捡漏核心算法。

普通道具只把 WFM ``statistics_closed.90days`` 的最新日桶均价作为基准；
紫卡用每小时采集的 p2~p5 均价构成可配置时长的滚动均价。所有价格判定
都使用 :class:`~decimal.Decimal`，只在通知展示时取整。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable, Mapping, Sequence

from . import marketdata, rivendata, texts
from .game_commands import seller_invite_command

KV_KEY = "bargain_params"
RIVEN_SAMPLE_MAX_SPREAD = Decimal("0.35")
RIVEN_NEW_LISTING_MAX_AGE_SECONDS = 3600

DEFAULT_PARAMS: dict[str, float | int] = {
    "item_threshold": 0.20,
    "riven_threshold": 0.35,
    "riven_rolling_hours": 24,
    "riven_min_valid_samples": 1,
}
_INT_KEYS = {"riven_rolling_hours", "riven_min_valid_samples"}

_params: dict[str, float | int] = dict(DEFAULT_PARAMS)


def params() -> dict[str, float | int]:
    return dict(_params)


def validate_params(raw: dict) -> tuple[dict, str | None]:
    """校验部分更新并返回完整参数。"""
    if not isinstance(raw, dict):
        return {}, "参数必须是对象"
    merged = dict(_params)
    for key, value in raw.items():
        if key not in DEFAULT_PARAMS:
            return {}, f"未知参数: {key}"
        if isinstance(value, bool):
            return {}, f"{key} 必须是数字"
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return {}, f"{key} 必须是数字"
        if not number.is_finite():
            return {}, f"{key} 必须是有限数字"
        if key in _INT_KEYS:
            if number != number.to_integral_value():
                return {}, f"{key} 必须是整数"
            integer = int(number)
            minimum = 12 if key == "riven_rolling_hours" else 1
            if integer < minimum:
                return {}, f"{key} 不能小于 {minimum}"
            merged[key] = integer
        else:
            if not Decimal("0.05") <= number <= Decimal("0.90"):
                return {}, f"{key} 超出范围 0.05~0.90"
            merged[key] = float(number)
    if int(merged["riven_min_valid_samples"]) > int(
            merged["riven_rolling_hours"]):
        return {}, "最低有效样本数不能大于滚动均价时长"
    return merged, None


def load_params(store) -> None:
    """从数据库加载当前支持的参数，忽略未在 ``DEFAULT_PARAMS`` 中定义的键。"""
    global _params
    _params = dict(DEFAULT_PARAMS)
    try:
        raw = json.loads(store.kv_get(KV_KEY) or "{}")
        if not isinstance(raw, dict):
            return
        known = {key: value for key, value in raw.items()
                 if key in DEFAULT_PARAMS}
        merged, error = validate_params(known)
        if error is None:
            _params = merged
    except (TypeError, ValueError, json.JSONDecodeError):
        return


def save_params(store, raw: dict) -> str | None:
    merged, error = validate_params(raw)
    if error:
        return error
    store.kv_set(KV_KEY, json.dumps(merged, separators=(",", ":")))
    _params.clear()
    _params.update(merged)
    return None


def as_decimal(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _parse_datetime(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ---- 分桶与等级 ----

_SUBTYPE_ZH = {
    "intact": "完整", "exceptional": "优良", "flawless": "无瑕",
    "radiant": "光辉", "unrevealed": "未鉴定", "revealed": "已鉴定",
    "blueprint": "蓝图", "crafted": "成品",
}
_LEVEL_TOKENS = {"0": "0", "0级": "0", "满": "max", "满级": "max",
                 "max": "max"}
_LEVEL_LABEL = {"0": " 0级", "max": " 满级"}
_STATUS_ZH = {
    "ingame": "游戏中",
    "online": "在线",
    "offline": "离线",
    "invisible": "隐身",
}
_STATUS_EN = {
    "ingame": "In game",
    "online": "Online",
    "offline": "Offline",
    "invisible": "Invisible",
}


def item_bucket_of(order: Mapping, max_rank: int) -> str:
    """普通道具订单桶；``maxRank=0`` 时忽略上游偶尔携带的 ``rank=0``。"""
    parts: list[str] = []
    if max_rank > 0 and order.get("rank") is not None:
        parts.append(f"rank:{order['rank']}")
    if order.get("subtype"):
        parts.append(f"subtype:{order['subtype']}")
    return "|".join(parts)


def bucket_for_level(level: str | None, max_rank: int,
                     subtype: str | None = None) -> str:
    parts: list[str] = []
    if max_rank > 0:
        if level == "0":
            parts.append("rank:0")
        elif level == "max":
            parts.append(f"rank:{max_rank}")
    if subtype:
        parts.append(f"subtype:{subtype}")
    return "|".join(parts)


def bucket_display(bucket: str) -> str:
    if not bucket:
        return ""
    labels: list[str] = []
    for part in bucket.split("|"):
        kind, _, value = part.partition(":")
        if kind == "rank":
            labels.append("0级" if value == "0" else f"R{value}")
        elif kind == "subtype":
            labels.append(_SUBTYPE_ZH.get(value, value))
    return f"[{'/'.join(labels)}]" if labels else ""


def plain_bucket_display(bucket: str, locale: str = "zh") -> str:
    """群消息使用的无装饰等级文本。"""
    if locale == "en":
        labels: list[str] = []
        for part in bucket.split("|") if bucket else ():
            kind, _, value = part.partition(":")
            labels.append(
                f"Rank {value}" if kind == "rank" else value.replace("_", " ").title()
            )
        return f" {'/'.join(labels)}" if labels else ""
    label = bucket_display(bucket)
    return f" {label[1:-1]}" if label else ""


def parse_level_token(token: str) -> str | None:
    return _LEVEL_TOKENS.get(token.strip().casefold())


def validate_level_choice(
    slug: str, level: str | None, *, locale: str = "zh",
) -> tuple[str | None, str | None]:
    """验证监控等级。

    ``maxRank=0`` 的道具不接受也不保存等级；有等级道具必须明确选择 0 级
    或满级。
    """
    max_rank = marketdata.item_max_rank(slug)
    normalized = str(level).strip().casefold() if level is not None else None
    if normalized == "":
        normalized = None
    if max_rank <= 0:
        if normalized is not None:
            return None, ("This item has no ranks; do not select a rank."
                          if locale == "en" else
                          "该道具没有等级，不提供等级选项")
        return None, None
    if normalized not in {"0", "max"}:
        return None, (
            f"This item requires Rank 0 or max rank (R{max_rank})."
            if locale == "en" else
            f"该道具必须选择 0级 或 满级（满级为 R{max_rank}）"
        )
    return normalized, None


def level_allowed(order_rank, level: str | None, max_rank: int) -> bool:
    """订单等级是否与配置精确一致；任何中间等级都不参与。"""
    if max_rank <= 0:
        if level is not None or order_rank is None:
            return level is None
        try:
            return int(order_rank) == 0
        except (TypeError, ValueError):
            return False
    if level not in {"0", "max"} or order_rank is None:
        return False
    try:
        rank = int(order_rank)
    except (TypeError, ValueError):
        return False
    return rank == (0 if level == "0" else max_rank)


# ---- 普通道具：WFM 90 天统计的最新日桶 ----

@dataclass(frozen=True, slots=True)
class DailyAverage:
    price: Decimal
    bucket: str
    stat_id: str
    stat_datetime: str
    stat_timestamp: float
    volume: int


def _stat_rank_matches(row: Mapping, target_rank: int | None,
                       max_rank: int) -> bool:
    raw = row.get("mod_rank")
    if max_rank <= 0:
        if raw in (None, ""):
            return True
        try:
            return int(raw) == 0
        except (TypeError, ValueError):
            return False
    if target_rank not in {0, max_rank} or raw in (None, ""):
        return False
    try:
        return int(raw) == target_rank
    except (TypeError, ValueError):
        return False


def latest_item_daily_average(
        rows: Sequence[Mapping] | Mapping, *, target_rank: int | None,
        max_rank: int, subtype: str | None = None) -> DailyAverage | None:
    """返回指定等级/规格的最新 ``90days`` 日桶 ``avg_price``。

    接受 WFM 90days 行列表，也兼容完整 statistics payload。不会在目标等级
    缺失时回退到别的等级。
    """
    if isinstance(rows, Mapping):
        payload = rows.get("payload") or rows
        closed = (payload.get("statistics_closed") or payload
                  if isinstance(payload, Mapping) else {})
        rows = closed.get("90days") or [] if isinstance(closed, Mapping) else []
    candidates: list[tuple[datetime, Mapping, Decimal]] = []
    for row in rows:
        if not _stat_rank_matches(row, target_rank, max_rank):
            continue
        if subtype is not None and row.get("subtype") != subtype:
            continue
        if subtype is None and row.get("subtype") not in (None, ""):
            continue
        price = as_decimal(row.get("avg_price"))
        when = _parse_datetime(row.get("datetime"))
        if price is None or price <= 0 or when is None:
            continue
        candidates.append((when, row, price))
    if not candidates:
        return None
    when, row, price = max(candidates, key=lambda entry: entry[0])
    bucket = bucket_for_level(
        "0" if target_rank == 0 and max_rank > 0 else
        "max" if target_rank == max_rank and max_rank > 0 else None,
        max_rank, subtype)
    raw_datetime = str(row.get("datetime") or when.isoformat())
    return DailyAverage(
        price=price,
        bucket=bucket,
        stat_id=str(row.get("id") or raw_datetime),
        stat_datetime=raw_datetime,
        stat_timestamp=when.timestamp(),
        volume=int(row.get("volume") or 0),
    )


# ---- 紫卡：小时地板样本与滚动均价 ----

@dataclass(frozen=True, slots=True)
class RivenFloorSample:
    price: Decimal
    order_count: int
    spread: Decimal


def direct_buyout_price(auction: Mapping) -> Decimal | None:
    """只认明确的一口价；竞拍起拍价永远不能进入算法。"""
    if auction.get("is_direct_sell") is not True:
        return None
    price = as_decimal(auction.get("buyout_price"))
    return price if price is not None and price > 0 else None


def build_riven_hour_sample(
        auctions: Iterable[Mapping], *,
        max_spread: Decimal = RIVEN_SAMPLE_MAX_SPREAD,
        ) -> RivenFloorSample | None:
    prices: list[Decimal] = []
    for auction in auctions:
        if (auction.get("closed") or auction.get("private")
                or not auction.get("visible", True)):
            continue
        status = str((auction.get("owner") or {}).get("status") or "").casefold()
        if status not in {"ingame", "online"}:
            continue
        price = direct_buyout_price(auction)
        if price is not None:
            prices.append(price)
    prices.sort()
    if len(prices) < 5:
        return None
    selected = prices[1:5]
    mean = sum(selected, Decimal("0")) / Decimal(len(selected))
    spread = (selected[-1] - selected[0]) / mean
    if spread > max_spread:
        return None
    return RivenFloorSample(price=mean, order_count=len(prices), spread=spread)


def rolling_riven_average(
        samples: Iterable[Mapping], *, now: float, hours: int,
        min_samples: int) -> tuple[Decimal | None, int]:
    # 时长按需求不设上限；大于 Unix 纪元总时长时等价于“保留全部样本”，
    # 避免先转 float 或传入 SQLite 造成溢出。
    cutoff = max(0, int(float(now)) - int(hours) * 3600)
    valid: list[Decimal] = []
    for sample in samples:
        sampled_at = sample.get("sampled_at", sample.get("sample_hour"))
        try:
            timestamp = float(sampled_at)
        except (TypeError, ValueError):
            continue
        price = as_decimal(sample.get("price", sample.get("floor_price")))
        if timestamp >= cutoff and price is not None and price > 0:
            valid.append(price)
    if len(valid) < int(min_samples):
        return None, len(valid)
    return sum(valid, Decimal("0")) / Decimal(len(valid)), len(valid)


def is_fresh_riven_listing(
        auction: Mapping, *, now: datetime | float | None = None,
        max_age_seconds: int = RIVEN_NEW_LISTING_MAX_AGE_SECONDS) -> bool:
    created = _parse_datetime(auction.get("created"))
    if created is None:
        return False
    if now is None:
        current = datetime.now(timezone.utc)
    elif isinstance(now, datetime):
        current = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)
    else:
        current = datetime.fromtimestamp(float(now), timezone.utc)
    age = (current - created).total_seconds()
    return 0 <= age <= max_age_seconds


@dataclass(frozen=True, slots=True)
class Hit:
    price: Decimal
    baseline: Decimal
    discount: Decimal
    samples: int = 0


@dataclass(slots=True)
class BargainItemPushPayload:
    """延迟到发送阶段按目标平台渲染的普通道具捡漏负载。"""

    slug: str
    order: dict
    hit: Hit
    locale: str
    target_scope: int
    rendered: object | None = None


@dataclass(slots=True)
class BargainRivenPushPayload:
    """延迟到发送阶段按目标平台渲染的紫卡捡漏负载。"""

    weapon_slug: str
    auction: dict
    hit: Hit
    locale: str
    target_scope: int
    rendered: object | None = None


def evaluate_price(price, baseline, threshold,
                   *, samples: int = 0) -> Hit | None:
    price_d = as_decimal(price)
    baseline_d = as_decimal(baseline)
    threshold_d = as_decimal(threshold)
    if (price_d is None or baseline_d is None or threshold_d is None
            or price_d <= 0 or baseline_d <= 0
            or not Decimal("0") <= threshold_d < Decimal("1")):
        return None
    discount = (baseline_d - price_d) / baseline_d
    if discount < threshold_d:
        return None
    return Hit(price_d, baseline_d, discount, int(samples))


# ---- 监控条目操作（QQ 命令与控制台共用）----

_PCT_RE = re.compile(r"^(?:折扣)?(\d{1,3})%?$")


def parse_threshold_token(token: str) -> tuple[float | None, bool]:
    match = _PCT_RE.match(token.strip())
    if not match:
        return None, False
    value = int(match.group(1))
    return ((value / 100.0, True) if 5 <= value <= 90 else (None, True))


def _threshold_percent(value) -> str:
    number = as_decimal(value) or Decimal("0")
    return format((number * 100).normalize(), "f")


def describe_item(row: Mapping, locale: str | None = None) -> str:
    locale = locale or texts.current_locale()
    threshold = row.get("threshold")
    pct = _threshold_percent(
        threshold if threshold is not None else _params["item_threshold"])
    name = marketdata.item_name(row["slug"], locale)
    if locale == "en":
        line = f"#{row['id']} {name} at least {pct}% below reference"
        line += {"0": " Rank 0", "max": " Max rank"}.get(
            row.get("level") or "", "")
    else:
        line = f"编号 {row['id']} {name} 低于参考价至少 {pct}%"
        line += _LEVEL_LABEL.get(row.get("level") or "", "")
    return line


def add_item_checked(store, config, group_id: int,
                     text: str) -> tuple[bool, str, int | None]:
    parts = text.split()
    if not parts:
        return False, texts.render("捡漏添加.用法"), None
    threshold = None
    level = None
    while len(parts) >= 2:
        token = parts[-1]
        parsed_level = parse_level_token(token)
        if parsed_level is not None and level is None:
            level = parsed_level
            parts.pop()
            continue
        value, looks_like = parse_threshold_token(token)
        if looks_like and threshold is None:
            if value is None:
                return False, texts.render(
                    "捡漏添加.折扣格式错误", value=token), None
            threshold = value
            parts.pop()
            continue
        break
    name = " ".join(parts)
    matches = marketdata.resolve_item(name)
    if not matches:
        message = texts.render("捡漏添加.找不到", name=name)
        if not marketdata.available():
            message += "\n" + texts.render("捡漏添加.数据缺失")
        return False, message, None
    if len(matches) > 1:
        candidates = "\n".join(
            marketdata.item_name(slug, texts.current_locale())
            for slug in matches[:5])
        return False, texts.render(
            "捡漏添加.多个匹配", name=name, candidates=candidates), None
    slug = matches[0]
    level, level_error = validate_level_choice(
        slug, level, locale=texts.current_locale())
    if level_error:
        return False, level_error, None
    limit = int(config.bargain_max_distinct_slugs)
    if (limit > 0 and not store.has_bargain_slug(slug)
            and store.count_bargain_distinct_slugs() >= limit):
        return False, texts.render(
            "捡漏添加.达到上限", limit=limit), None
    item_id = store.add_bargain_item(
        group_id, slug, threshold, level)
    if item_id is None:
        return False, texts.render(
            "捡漏添加.已存在",
            name=marketdata.item_name(slug, texts.current_locale())), None
    saved = store.get_bargain_item(item_id, group_id)
    message = texts.render("捡漏添加.成功", description=describe_item(saved))
    return True, message, item_id


def item_threshold_of(row: Mapping) -> Decimal:
    value = row.get("threshold")
    return Decimal(str(value if value is not None else _params["item_threshold"]))


def riven_threshold_of(row: Mapping) -> Decimal:
    value = row.get("threshold")
    return Decimal(str(value if value is not None else _params["riven_threshold"]))


def describe_riven_item(row: Mapping, locale: str | None = None) -> str:
    locale = locale or texts.current_locale()
    threshold = row.get("threshold")
    pct = _threshold_percent(
        threshold if threshold is not None else _params["riven_threshold"])
    weapon = rivendata.weapon_name(row["weapon_slug"], locale)
    line = (f"#{row['id']} {weapon} Riven at least {pct}% below reference"
            if locale == "en" else
            f"编号 {row['id']} {weapon} 紫卡 低于参考价至少 {pct}%")
    return line


def add_riven_item_checked(store, config, group_id: int,
                           text: str) -> tuple[bool, str, int | None]:
    parts = text.split()
    if not parts:
        return False, texts.render("捡漏紫卡.用法"), None
    threshold = None
    if len(parts) >= 2:
        value, looks_like = parse_threshold_token(parts[-1])
        if looks_like:
            if value is None:
                return False, texts.render(
                    "捡漏添加.折扣格式错误", value=parts[-1]), None
            threshold = value
            parts.pop()
    name = " ".join(parts)
    slug = rivendata.resolve_weapon(name)
    if slug is None:
        return False, texts.render("捡漏紫卡.找不到武器", name=name), None
    limit = int(config.bargain_max_distinct_slugs)
    if (limit > 0 and not store.has_bargain_slug(slug)
            and store.count_bargain_distinct_slugs() >= limit):
        return False, texts.render(
            "捡漏紫卡.达到上限", limit=limit), None
    item_id = store.add_bargain_riven_item(
        group_id, slug, threshold)
    if item_id is None:
        return False, texts.render(
            "捡漏紫卡.已存在",
            name=rivendata.weapon_name(slug, texts.current_locale())), None
    saved = store.get_bargain_riven_item(item_id, group_id)
    message = texts.render(
        "捡漏紫卡.添加成功", description=describe_riven_item(saved))
    return True, message, item_id


# ---- 推送文案 ----

def format_price(value) -> str:
    number = as_decimal(value) or Decimal("0")
    return format(number.normalize(), "f")


def display_price(value) -> int:
    number = as_decimal(value) or Decimal("0")
    return int(number.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def build_item_push_text(
    slug: str, order: Mapping, hit: Hit, *, locale: str = "zh",
) -> str:
    zh, en = marketdata.item_names(slug)
    rank = order.get("rank")
    max_rank = marketdata.item_max_rank(slug)
    whisper_item = ((en or slug) +
                    (f" (rank {rank})"
                     if max_rank > 0 and rank is not None else ""))
    user = order.get("user") or {}
    seller = str(user.get("ingameName") or "?")
    rendered = texts.render(
        "捡漏推送.道具",
        name=marketdata.item_name(slug, locale),
        name_en=en or slug,
        bucket=plain_bucket_display(item_bucket_of(order, max_rank), locale),
        price=format_price(order.get("platinum")),
        baseline=display_price(hit.baseline),
        discount=round(float(hit.discount) * 100),
        quantity=order.get("quantity") or 1,
        seller=seller,
        seller_status=(_STATUS_EN if locale == "en" else _STATUS_ZH).get(
            user.get("status"), user.get("status") or (
                "Unknown" if locale == "en" else "未知状态")),
        whisper_item=whisper_item,
        locale=locale)
    return f"{rendered}\n{seller_invite_command(seller)}"


def build_riven_push_text(
    weapon_slug: str, auction: Mapping, hit: Hit, *, locale: str = "zh",
) -> str:
    item = auction.get("item") or {}
    weapon = rivendata.weapons().get(weapon_slug) or {}
    owner = auction.get("owner") or {}
    seller = str(owner.get("ingame_name") or "?")
    rendered = texts.render(
        "捡漏推送.紫卡",
        weapon=rivendata.weapon_name(weapon_slug, locale),
        weapon_en=weapon.get("name_en") or weapon_slug,
        riven=item.get("name") or "",
        price=format_price(auction.get("buyout_price")),
        baseline=display_price(hit.baseline),
        discount=round(float(hit.discount) * 100),
        re_rolls=item.get("re_rolls", 0),
        seller=seller,
        seller_status=(_STATUS_EN if locale == "en" else _STATUS_ZH).get(
            owner.get("status"), owner.get("status") or (
                "Unknown" if locale == "en" else "未知状态")),
        locale=locale)
    return f"{rendered}\n{seller_invite_command(seller)}"
