"""捡漏核心：日桶、紫卡滚动基准、候选幂等与 Baro 暂停。"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.plugins.riven_sniper import bargain, marketdata, rivendata  # noqa: E402
from src.plugins.riven_sniper.bargainpoller import BargainPoller  # noqa: E402
from src.plugins.riven_sniper.delivery import DeliveryItem  # noqa: E402
from src.plugins.riven_sniper.store import Store  # noqa: E402

GROUP = 302875968


@pytest.fixture(autouse=True)
def fresh_params():
    bargain._params.clear()
    bargain._params.update(bargain.DEFAULT_PARAMS)
    yield
    bargain._params.clear()
    bargain._params.update(bargain.DEFAULT_PARAMS)


@pytest.fixture()
def tmp_market(tmp_path, monkeypatch):
    items = {
        "arcane_grace": {
            "id": "IID-RANKED", "zh": "赋能·优雅", "en": "Arcane Grace",
            "tags": ["arcane_enhancement"], "max_rank": 5,
            "game_ref": "/Lotus/Types/Items/MiscItems/ArcaneGrace",
        },
        "mesa_prime_set": {
            "id": "IID-PLAIN", "zh": "女枪手Prime 一套",
            "en": "Mesa Prime Set", "tags": ["prime"], "max_rank": 0,
            "game_ref": "/Lotus/Types/Recipes/WarframeRecipes/MesaPrimeSet",
        },
        "axi_a1_relic": {
            "id": "IID-RELIC", "zh": "古纪 A1 遗物", "en": "Axi A1 Relic",
            "tags": ["relic"], "max_rank": 0,
            "game_ref": "/Lotus/Types/Game/Projections/T3VoidProjectionA1",
        },
    }
    (tmp_path / "market_items.json").write_text(
        json.dumps({"fetched_at": 1, "items": items}, ensure_ascii=False),
        encoding="utf-8")
    for name in ("weapons.json", "attributes.json", "riven_values.json",
                 "aliases.json"):
        shutil.copy(ROOT / "data" / name, tmp_path / name)
    monkeypatch.setattr(rivendata, "DATA_DIR", tmp_path)
    rivendata.invalidate_caches()
    marketdata.invalidate()
    yield tmp_path
    rivendata.invalidate_caches()
    marketdata.invalidate()


def _cfg(*, max_items=30, discord=False):
    return types.SimpleNamespace(
        bargain_max_distinct_slugs=max_items, sniper_dry_run=True,
        discord_dm_enabled=discord)


def test_resolve_item_uses_aliases_for_exact_matches_only(tmp_market):
    (tmp_market / "market_item_aliases.json").write_text(
        json.dumps({"aliases": {
            "arcane_grace": ["优雅简称"],
            "axi_a1_relic": ["古A1"],
        }}, ensure_ascii=False),
        encoding="utf-8",
    )
    marketdata.invalidate()

    assert marketdata.resolve_item("优雅简称") == ["arcane_grace"]
    assert marketdata.resolve_item("古-A1") == ["axi_a1_relic"]
    assert marketdata.resolve_item("简称") == []


def test_resolve_item_preserves_exact_name_ambiguity(tmp_market):
    (tmp_market / "market_item_aliases.json").write_text(
        json.dumps({"aliases": {
            "arcane_grace": ["共享简称"],
            "mesa_prime_set": ["共享简称"],
        }}, ensure_ascii=False),
        encoding="utf-8",
    )
    marketdata.invalidate()

    assert marketdata.resolve_item("共享简称") == [
        "arcane_grace", "mesa_prime_set",
    ]


class _QueueDeliveryPoller:
    def __init__(self, queue: asyncio.Queue):
        self.queue = queue

    def new_delivery(self, source, target, payload):
        now = time.time()
        return DeliveryItem(source, target, payload, 0, now, now + 60)

    def enqueue_delivery(self, item):
        self.queue.put_nowait(item)
        return True


def _bargain_poller(store, config, queue=None):
    delivery_queue = asyncio.Queue() if queue is None else queue
    return BargainPoller(
        store, config, _QueueDeliveryPoller(delivery_queue))


def _order(price, *, oid="order-1", item_id="IID-RANKED", rank=5,
           seller="Seller", status="ingame", subtype=None):
    order = {
        "id": oid, "type": "sell", "visible": True, "platinum": price,
        "quantity": 1, "itemId": item_id,
        "user": {"ingameName": seller, "status": status},
    }
    if rank is not None:
        order["rank"] = rank
    if subtype is not None:
        order["subtype"] = subtype
    return order


def _created(seconds_ago=0, *, now=None):
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(seconds=seconds_ago)).isoformat().replace("+00:00", "Z")


def _auction(price, *, aid="auction-1", status="ingame", seller="Seller",
             direct=True, created=None, visible=True, closed=False,
             private=False, starting=1, weapon="torid"):
    return {
        "id": aid, "buyout_price": price, "starting_price": starting,
        "is_direct_sell": direct, "created": created or _created(),
        "visible": visible, "closed": closed, "private": private,
        "item": {
            "type": "riven", "weapon_url_name": weapon,
            "name": "Visi-crita", "re_rolls": 0, "mod_rank": 8,
            "mastery_level": 10, "attributes": [],
        },
        "owner": {"ingame_name": seller, "status": status},
    }


def test_bargain_pushes_add_only_invite_shortcut(tmp_market):
    hit = bargain.Hit(
        Decimal("150"), Decimal("390"), Decimal("0.615"),
    )

    riven_text = bargain.build_riven_push_text(
        "torid", _auction(150, seller="Bargain Seller"), hit,
    )
    item_text = bargain.build_item_push_text(
        "arcane_grace", _order(45, seller="Item Seller"), hit,
    )

    assert "/w Bargain Seller Hi!" in riven_text
    assert '/inv "Bargain Seller"' in riven_text
    assert '/w "Bargain Seller" hi' not in riven_text
    assert '/join "Bargain Seller"' not in riven_text
    assert "/w Item Seller Hi!" in item_text
    assert '/inv "Item Seller"' in item_text
    assert '/w "Item Seller" hi' not in item_text
    assert '/join "Item Seller"' not in item_text


# ---- 最小参数与等级契约 ----

def test_only_four_parameters_and_window_sample_constraints():
    assert bargain.DEFAULT_PARAMS == {
        "item_threshold": 0.20,
        "riven_threshold": 0.35,
        "riven_rolling_hours": 24,
        "riven_min_valid_samples": 1,
    }
    merged, error = bargain.validate_params({
        "item_threshold": "0.25", "riven_threshold": 0.4,
        "riven_rolling_hours": 12, "riven_min_valid_samples": 12,
    })
    assert error is None
    assert merged["riven_rolling_hours"] == 12
    assert merged["riven_min_valid_samples"] == 12

    for update, text in (
        ({"riven_rolling_hours": 11}, "不能小于 12"),
        ({"riven_min_valid_samples": 0}, "不能小于 1"),
        ({"riven_rolling_hours": 12, "riven_min_valid_samples": 13},
         "不能大于"),
        ({"min_gap": 5}, "未知参数"),
    ):
        _, error = bargain.validate_params(update)
        assert text in error


def test_params_persist_decimal_values_and_ignore_removed_legacy_keys():
    store = Store(":memory:")
    assert bargain.save_params(store, {
        "item_threshold": 0.3, "riven_threshold": 0.4,
        "riven_rolling_hours": 36, "riven_min_valid_samples": 7,
    }) is None
    bargain._params.clear()
    bargain._params.update(bargain.DEFAULT_PARAMS)
    bargain.load_params(store)
    assert bargain.params() == {
        "item_threshold": 0.3, "riven_threshold": 0.4,
        "riven_rolling_hours": 36, "riven_min_valid_samples": 7,
    }

    store.kv_set(bargain.KV_KEY, json.dumps({
        "item_threshold": 0.27, "cooldown": 999, "min_gap": 5,
    }))
    bargain.load_params(store)
    assert bargain.params()["item_threshold"] == 0.27
    assert set(bargain.params()) == set(bargain.DEFAULT_PARAMS)
    store.close()


def test_config_descriptions_preserve_fractional_global_thresholds(tmp_market):
    previous = bargain.params()
    try:
        bargain._params["item_threshold"] = 0.255
        bargain._params["riven_threshold"] = 0.375
        assert "低于参考价至少 25.5%" in bargain.describe_item({
            "id": 1, "slug": "mesa_prime_set", "threshold": None,
            "level": None,
        })
        assert "低于参考价至少 37.5%" in bargain.describe_riven_item({
            "id": 2, "weapon_slug": "torid", "threshold": None,
        })
    finally:
        bargain._params.clear()
        bargain._params.update(previous)


def test_max_rank_zero_has_no_level_and_ranked_item_requires_zero_or_max(
        tmp_market):
    assert marketdata.game_ref_to_slugs(
        "/Lotus/StoreItems/Types/Items/MiscItems/ArcaneGrace"
    ) == ("arcane_grace",)
    assert marketdata.game_ref_to_slugs(
        "/Lotus/StoreItems/Types/Game/Projections/"
        "T3VoidProjectionA1Gold"
    ) == ("axi_a1_relic",)
    assert marketdata.item_max_rank("mesa_prime_set") == 0
    assert bargain.validate_level_choice("mesa_prime_set", None) == (None, None)
    assert bargain.validate_level_choice("mesa_prime_set", "0")[1]
    assert bargain.level_allowed(None, None, 0)
    assert bargain.level_allowed(0, None, 0)
    assert not bargain.level_allowed(1, None, 0)

    assert marketdata.item_max_rank("arcane_grace") == 5
    assert bargain.validate_level_choice("arcane_grace", None)[1]
    assert bargain.validate_level_choice("arcane_grace", "0") == ("0", None)
    assert bargain.validate_level_choice("arcane_grace", "max") == ("max", None)
    assert bargain.validate_level_choice("arcane_grace", "both")[1]
    assert bargain.level_allowed(0, "0", 5)
    assert bargain.level_allowed(5, "max", 5)
    assert not bargain.level_allowed(3, "max", 5)
    assert not bargain.level_allowed(5, "0", 5)


@pytest.mark.asyncio
async def test_catalog_refresh_rejects_metadata_loss_and_keeps_old_file(
        tmp_market):
    path = tmp_market / "market_items.json"
    before = path.read_bytes()

    def response(request):
        return httpx.Response(200, request=request, json={"data": [
            {"slug": "arcane_grace", "id": "IID-RANKED", "i18n": {
                "en": {"name": "Arcane Grace"},
                "zh-hans": {"name": "赋能·优雅"},
            }},
            {"slug": "mesa_prime_set", "id": "IID-PLAIN", "i18n": {
                "en": {"name": "Mesa Prime Set"},
                "zh-hans": {"name": "女枪手Prime 一套"},
            }},
            {"slug": "axi_a1_relic", "id": "IID-RELIC", "i18n": {
                "en": {"name": "Axi A1 Relic"},
                "zh-hans": {"name": "古纪 A1 遗物"},
            }},
        ]})

    async with httpx.AsyncClient(
            transport=httpx.MockTransport(response)) as client:
        assert not await marketdata.refresh_catalog(client)
    assert path.read_bytes() == before
    assert marketdata.item_max_rank("arcane_grace") == 5


@pytest.mark.asyncio
async def test_catalog_refresh_preserves_existing_rank_when_field_is_missing(
        tmp_market):
    rows = [
        {
            "slug": "arcane_grace", "id": "IID-RANKED",
            "gameRef": "/Lotus/Types/Items/MiscItems/ArcaneGrace",
            "i18n": {"en": {"name": "Arcane Grace"},
                     "zh-hans": {"name": "赋能·优雅"}},
        },
        {
            "slug": "mesa_prime_set", "id": "IID-PLAIN", "maxRank": 0,
            "gameRef": "/Lotus/StoreItems/MesaPrimeSet",
            "i18n": {"en": {"name": "Mesa Prime Set"},
                     "zh-hans": {"name": "女枪手Prime 一套"}},
        },
        {
            "slug": "axi_a1_relic", "id": "IID-RELIC", "maxRank": 0,
            "gameRef": "/Lotus/Types/Game/Projections/T3VoidProjectionA1",
            "i18n": {"en": {"name": "Axi A1 Relic"},
                     "zh-hans": {"name": "古纪 A1 遗物"}},
        },
    ]

    def response(request):
        return httpx.Response(200, request=request, json={"data": rows})

    async with httpx.AsyncClient(
            transport=httpx.MockTransport(response)) as client:
        assert await marketdata.refresh_catalog(client)
    assert marketdata.item_max_rank("arcane_grace") == 5


def test_catalog_validation_rejects_major_partial_rank_loss(monkeypatch):
    current = {
        f"ranked_{index}": {
            "id": f"id-{index}", "en": f"Ranked {index}",
            "game_ref": f"/Lotus/Ranked/{index}", "max_rank": 10,
        }
        for index in range(10)
    }
    projected = {
        slug: {**row, "max_rank": 10 if index < 2 else 0}
        for index, (slug, row) in enumerate(current.items())
    }
    monkeypatch.setattr(marketdata, "items", lambda: current)
    with pytest.raises(ValueError, match="maxRank"):
        marketdata._validate_catalog(projected)


def test_add_item_enforces_level_selection(tmp_market):
    store = Store(":memory:")
    ok, message, _ = bargain.add_item_checked(
        store, _cfg(), GROUP, "Arcane Grace")
    assert not ok and "必须选择" in message

    ok, _, item_id = bargain.add_item_checked(
        store, _cfg(), GROUP, "Arcane Grace 满")
    assert ok and store.get_bargain_item(item_id, GROUP)["level"] == "max"

    ok, _, plain_id = bargain.add_item_checked(
        store, _cfg(), GROUP, "Mesa Prime Set")
    assert ok and store.get_bargain_item(plain_id, GROUP)["level"] is None
    store.delete_bargain_item(plain_id, GROUP)
    ok, message, _ = bargain.add_item_checked(
        store, _cfg(), GROUP, "Mesa Prime Set 0")
    assert not ok and "没有等级" in message
    store.close()


def test_zero_distinct_slug_limit_is_unlimited(tmp_market):
    store = Store(":memory:")
    cfg = _cfg(max_items=0)
    additions = [
        bargain.add_item_checked(
            store, cfg, GROUP, "Arcane Grace 满"),
        bargain.add_item_checked(
            store, cfg, GROUP, "Mesa Prime Set"),
        bargain.add_item_checked(
            store, cfg, GROUP, "Axi A1 Relic"),
        bargain.add_riven_item_checked(
            store, cfg, GROUP, "Torid"),
    ]
    assert all(ok for ok, _message, _item_id in additions)
    assert store.count_bargain_distinct_slugs() == 4
    store.close()


def test_distinct_slug_count_ignores_retired_web_delivery_tables():
    store = Store(":memory:")
    store.add_bargain_item(GROUP, "arcane_grace")
    store.add_bargain_item(GROUP + 1, "arcane_grace")
    store.add_bargain_riven_item(GROUP, "torid")
    assert store.count_bargain_distinct_slugs() == 2

    with store._conn:
        user_id = store._conn.execute(
            """INSERT INTO feed_users
               (username,password_hash,tier,enabled,channel_enabled,
                created_at,updated_at) VALUES ('Legacy','hash','plus',1,0,1,1)"""
        ).lastrowid
        store._conn.execute(
            """INSERT INTO feed_bargain_items
               (user_id,slug,threshold,level,enabled,created_at,updated_at)
               VALUES (?,'mesa_prime_set',NULL,NULL,1,1,1)""", (user_id,))
        store._conn.execute(
            """INSERT INTO feed_bargain_riven_items
               (user_id,weapon_slug,threshold,created_at,updated_at)
               VALUES (?,'boltor',NULL,1,1)""", (user_id,))

    assert store.count_bargain_distinct_slugs() == 2
    assert not store.has_bargain_slug("mesa_prime_set")
    assert not store.has_bargain_slug("axi_a1_relic")
    store.close()


@pytest.mark.asyncio
async def test_bargain_scopes_follow_target_and_platform_state():
    store = Store(":memory:")
    target = store.upsert_discord_target("1001")
    scope_id = target["scope_id"]
    store.add_bargain_item(scope_id, "arcane_grace")
    store.add_bargain_riven_item(scope_id, "torid")

    enabled = _bargain_poller(store, _cfg(discord=True))
    assert enabled._watched_group_items()["arcane_grace"][0]["group_id"] == scope_id
    assert enabled._watched_group_rivens()["torid"][0]["group_id"] == scope_id

    store.set_target_enabled(scope_id, False)
    assert enabled._watched_group_items() == {}
    assert enabled._watched_group_rivens() == {}
    store.set_target_enabled(scope_id, True)

    disabled = _bargain_poller(store, _cfg(discord=False))
    assert disabled._watched_group_items() == {}
    assert disabled._watched_group_rivens() == {}
    await enabled.wfm.close()
    await disabled.wfm.close()
    store.close()


# ---- 普通道具：90days 最新日桶 ----

def test_latest_daily_average_is_exact_rank_subtype_and_never_falls_back():
    payload = {
        "statistics_closed": {
            "48hours": [{
                "id": "48h", "datetime": "2026-07-21T23:00:00Z",
                "avg_price": 999, "mod_rank": 5,
            }],
            "90days": [
                {"id": "max-new", "datetime": "2026-07-21T00:00:00Z",
                 "avg_price": "105.125", "mod_rank": 5, "volume": 8},
                {"id": "zero-newer", "datetime": "2026-07-22T00:00:00Z",
                 "avg_price": 8, "mod_rank": 0},
                {"id": "max-old", "datetime": "2026-07-20T00:00:00Z",
                 "avg_price": 100, "mod_rank": 5},
                {"id": "radiant", "datetime": "2026-07-23T00:00:00Z",
                 "avg_price": 200, "mod_rank": 5, "subtype": "radiant"},
            ],
        },
    }
    row = bargain.latest_item_daily_average(
        payload, target_rank=5, max_rank=5)
    assert row.price == Decimal("105.125")
    assert row.stat_id == "max-new"
    assert row.bucket == "rank:5"
    assert row.volume == 8

    radiant = bargain.latest_item_daily_average(
        payload, target_rank=5, max_rank=5, subtype="radiant")
    assert radiant.price == Decimal("200")
    assert radiant.bucket == "rank:5|subtype:radiant"
    assert bargain.latest_item_daily_average(
        payload, target_rank=5, max_rank=10) is None
    assert bargain.latest_item_daily_average(
        payload, target_rank=10, max_rank=10) is None


def test_max_rank_zero_daily_bucket_accepts_only_unranked_or_rank_zero():
    rows = [
        {"id": "wrong", "datetime": "2026-07-22T00:00:00Z",
         "avg_price": 1, "mod_rank": 1},
        {"id": "plain", "datetime": "2026-07-21T00:00:00Z",
         "avg_price": "77.25"},
        {"id": "rank-zero", "datetime": "2026-07-20T00:00:00Z",
         "avg_price": 70, "mod_rank": 0},
    ]
    row = bargain.latest_item_daily_average(
        rows, target_rank=None, max_rank=0)
    assert row.price == Decimal("77.25") and row.bucket == ""


def test_daily_baseline_store_keeps_decimal_and_rejects_older_bucket():
    store = Store(":memory:")
    assert store.upsert_bargain_item_daily_baseline(
        "arcane_grace", "rank:5", Decimal("101.125"), source_id="new",
        source_datetime="2026-07-21T00:00:00Z", source_ts=200, volume=9)
    assert not store.upsert_bargain_item_daily_baseline(
        "arcane_grace", "rank:5", Decimal("1"), source_id="old",
        source_datetime="2026-07-20T00:00:00Z", source_ts=100, volume=1)
    saved = store.get_bargain_item_daily_baseline("arcane_grace", "rank:5")
    assert saved["price"] == Decimal("101.125")
    assert saved["source_id"] == "new"
    store.close()


# ---- 紫卡：直售样本、滚动均价和新单时限 ----

def test_riven_sample_online_and_ingame_equal_no_seller_dedup_and_p2_to_p5():
    auctions = [
        _auction(1, aid="p1", status="online", seller="Same"),
        _auction(100, aid="p2", status="ingame", seller="Same"),
        _auction(100, aid="p3", status="online", seller="Same"),
        _auction(100, aid="p4", status="ingame", seller="Same"),
        _auction(100, aid="p5", status="online", seller="Same"),
        _auction(2, aid="offline", status="offline"),
    ]
    sample = bargain.build_riven_hour_sample(auctions)
    assert sample.price == Decimal("100")
    assert sample.order_count == 5
    assert sample.spread == Decimal("0")


def test_riven_sample_needs_five_direct_visible_orders_and_never_uses_starting():
    valid = [_auction(100 + i, aid=f"v{i}") for i in range(4)]
    excluded = [
        _auction(None, aid="starting-only", direct=False, starting=1),
        _auction(None, aid="no-buyout", direct=True, starting=1),
        _auction(1, aid="offline", status="offline"),
        _auction(1, aid="hidden", visible=False),
        _auction(1, aid="closed", closed=True),
        _auction(1, aid="private", private=True),
    ]
    assert bargain.build_riven_hour_sample(valid + excluded) is None
    assert bargain.direct_buyout_price(excluded[0]) is None
    valid.append(_auction(104, aid="v4"))
    assert bargain.build_riven_hour_sample(valid + excluded) is not None


def test_riven_sample_spread_35_percent_boundary_is_inclusive():
    at_boundary = [
        _auction("1", aid="floor"),
        _auction("82.5", aid="a"), _auction("100", aid="b"),
        _auction("100", aid="c"), _auction("117.5", aid="d"),
    ]
    sample = bargain.build_riven_hour_sample(at_boundary)
    assert sample.price == Decimal("100")
    assert sample.spread == Decimal("0.35")

    over = [
        _auction("1", aid="floor"),
        _auction("82.4", aid="a"), _auction("100", aid="b"),
        _auction("100", aid="c"), _auction("117.6", aid="d"),
    ]
    assert bargain.build_riven_hour_sample(over) is None


def test_rolling_average_uses_exact_x_hour_window_minimum_and_decimal():
    now = 1_000_000.0
    samples = [
        {"sampled_at": now, "price": "100.1"},
        {"sampled_at": now - 12 * 3600, "price": "100.2"},  # 边界计入
        {"sampled_at": now - 12 * 3600 - 1, "price": "1"},
        {"sampled_at": now - 1, "price": "bad"},
    ]
    average, count = bargain.rolling_riven_average(
        samples, now=now, hours=12, min_samples=2)
    assert average == Decimal("100.15") and count == 2
    missing, count = bargain.rolling_riven_average(
        samples, now=now, hours=12, min_samples=3)
    assert missing is None and count == 2
    assert bargain.rolling_riven_average(
        samples, now=now, hours=10 ** 400, min_samples=1) == (
            Decimal("67.1"), 3)


def test_riven_created_time_exact_hour_allowed_older_or_bad_rejected():
    now = datetime(2026, 7, 21, 12, tzinfo=timezone.utc)
    assert bargain.is_fresh_riven_listing(
        {"created": _created(3600, now=now)}, now=now)
    assert not bargain.is_fresh_riven_listing(
        {"created": _created(3600.001, now=now)}, now=now)
    assert not bargain.is_fresh_riven_listing(
        {"created": (now + timedelta(seconds=1)).isoformat()}, now=now)
    assert not bargain.is_fresh_riven_listing({"created": "not-a-time"}, now=now)
    assert not bargain.is_fresh_riven_listing({}, now=now)


def test_riven_sample_store_and_candidate_ids_survive_restart(tmp_path):
    database = tmp_path / "bargain.db"
    store = Store(database)
    hour = store.bargain_utc_hour(100_000)
    assert store.add_bargain_riven_sample(
        "torid", "100.125", 5, "0.1", sampled_at=100_001,
        sample_hour=hour)
    assert not store.add_bargain_riven_sample(
        "torid", "1", 5, "0", sampled_at=100_002, sample_hour=hour)
    saved = store.list_bargain_riven_samples("torid")
    assert saved[0]["price"] == Decimal("100.125")
    assert saved[0]["spread"] == Decimal("0.1")
    assert store.mark_bargain_riven_notified("auction-1", 100)
    assert not store.mark_bargain_riven_notified("auction-1", 200)
    assert store.is_bargain_riven_notified("auction-1")
    assert store.mark_bargain_item_order_seen("order-1", 100)
    store.close()

    reopened = Store(database)
    assert reopened.is_bargain_riven_notified("auction-1")
    assert not reopened.mark_bargain_riven_notified("auction-1", 300)
    assert reopened.is_bargain_item_order_seen("order-1")
    assert not reopened.mark_bargain_item_order_seen("order-1", 300)
    reopened.close()


@pytest.mark.asyncio
async def test_riven_sample_network_failure_does_not_consume_hour(tmp_market):
    now = int(time.time())
    store = Store(":memory:")
    poller = _bargain_poller(store, _cfg())

    async def failed(_weapon):
        raise httpx.ConnectError(
            "offline", request=httpx.Request("GET", "https://example.invalid"))

    poller.wfm.riven_search = failed
    with pytest.raises(httpx.ConnectError):
        await poller._sample_riven_weapon("torid", now=now)
    assert store.get_bargain_riven_sample_attempt("torid") is None

    async def recovered(_weapon):
        return [_auction(100 + i, aid=f"sample-{i}") for i in range(5)]

    poller.wfm.riven_search = recovered
    assert await poller._sample_riven_weapon("torid", now=now)
    assert store.get_bargain_riven_sample_attempt("torid") is not None
    await poller.wfm.close()
    store.close()


# ---- 运行时：只消费新事件、缓存基准、黑名单隔离 ----

def _watched_item_store(*, level="max"):
    store = Store(":memory:")
    store.upsert_qq_target(GROUP, 42, enabled=True)
    store.add_bargain_item(GROUP, "arcane_grace", 0.20, level)
    return store


@pytest.mark.asyncio
async def test_item_handler_is_ws_event_only_and_order_id_once(tmp_market):
    store = _watched_item_store()
    store.upsert_bargain_item_daily_baseline(
        "arcane_grace", "rank:5", "100.5", source_id="daily",
        source_datetime="2026-07-21T00:00:00Z", source_ts=100, volume=12)
    store.add_blacklist(GROUP, "Seller")  # QQ 狙击黑名单不得拦截捡漏。
    store.add_blacklist(GROUP, "Seller", scope="channel")
    queue = asyncio.Queue()
    poller = _bargain_poller(store, _cfg(), queue)
    poller._baro_ready.set()
    poller._item_stats_verified_at[("arcane_grace", "rank:5")] = time.time()

    first = await poller.handle_new_item_order(_order("80.4"))
    changed = await poller.handle_new_item_order(_order("1", oid="order-1"))
    assert first == 1 and changed == 0
    assert queue.qsize() == 1
    payload = queue.get_nowait().payload
    assert isinstance(payload, bargain.BargainItemPushPayload)
    assert payload.slug == "arcane_grace"
    assert payload.order["id"] == "order-1"
    assert store.is_bargain_item_order_seen("order-1")
    await poller.wfm.close()
    store.close()


@pytest.mark.asyncio
async def test_item_handler_requires_exact_rank_bucket_and_cached_daily_baseline(
        tmp_market):
    store = _watched_item_store()
    queue = asyncio.Queue()
    poller = _bargain_poller(store, _cfg(), queue)
    poller._baro_ready.set()
    # 0级桶即使存在，也绝不作为满级配置的回退。
    store.upsert_bargain_item_daily_baseline(
        "arcane_grace", "rank:0", 100, source_id="zero",
        source_datetime="2026-07-21T00:00:00Z", source_ts=100)
    poller._item_stats_verified_at[("arcane_grace", "rank:0")] = time.time()
    assert await poller.handle_new_item_order(_order(1, oid="no-max")) == 0
    # 中间等级无论价格多低都不参与。
    store.upsert_bargain_item_daily_baseline(
        "arcane_grace", "rank:3", 100, source_id="middle",
        source_datetime="2026-07-21T00:00:00Z", source_ts=100)
    poller._item_stats_verified_at[("arcane_grace", "rank:3")] = time.time()
    assert await poller.handle_new_item_order(
        _order(1, oid="middle", rank=3)) == 0
    assert queue.empty()
    await poller.wfm.close()
    store.close()


@pytest.mark.asyncio
async def test_item_handler_requires_recent_in_process_statistics_verification(
        tmp_market):
    store = _watched_item_store()
    store.upsert_bargain_item_daily_baseline(
        "arcane_grace", "rank:5", 100, source_id="cached-day",
        source_datetime="2026-07-20T00:00:00Z", source_ts=100)
    queue = asyncio.Queue()
    poller = _bargain_poller(store, _cfg(), queue)
    poller._baro_ready.set()

    # 重启后尚未成功读取 WM 统计时，持久化旧基准不能直接用于提醒。
    assert await poller.handle_new_item_order(
        _order(50, oid="before-verification")) == 0
    assert not store.is_bargain_item_order_seen("before-verification")

    async def statistics(_slug):
        return [{
            "id": "cached-day", "datetime": "2026-07-20T00:00:00Z",
            "avg_price": 100, "mod_rank": 5, "volume": 8,
        }]

    poller.wfm.item_statistics = statistics
    await poller._refresh_item_statistics(
        "arcane_grace", store.list_bargain_items())
    assert store.is_bargain_item_order_seen("before-verification")
    assert queue.qsize() == 1
    assert await poller.handle_new_item_order(
        _order(50, oid="after-verification")) == 1

    poller._item_stats_verified_at[("arcane_grace", "rank:5")] = (
        time.time() - poller._ITEM_STATS_VALID_SECONDS - 1)
    assert await poller.handle_new_item_order(
        _order(50, oid="after-verification-expired")) == 0
    await poller.wfm.close()
    store.close()


@pytest.mark.asyncio
async def test_item_handler_waits_for_initial_baro_snapshot(tmp_market):
    store = _watched_item_store()
    store.upsert_bargain_item_daily_baseline(
        "arcane_grace", "rank:5", 100, source_id="daily",
        source_datetime="2026-07-21T00:00:00Z", source_ts=100)
    queue = asyncio.Queue()
    poller = _bargain_poller(store, _cfg(), queue)
    poller._item_stats_verified_at[("arcane_grace", "rank:5")] = time.time()

    assert await poller.handle_new_item_order(
        _order(50, oid="before-baro-snapshot")) == 0
    assert not store.is_bargain_item_order_seen("before-baro-snapshot")

    async def no_baro():
        return {"VoidTraders": []}

    poller.wfm.world_state = no_baro
    assert await poller._baro_once() == 0
    assert store.is_bargain_item_order_seen("before-baro-snapshot")
    assert queue.qsize() == 1
    assert await poller.handle_new_item_order(
        _order(50, oid="after-baro-snapshot")) == 1
    await poller.wfm.close()
    store.close()


@pytest.mark.asyncio
async def test_pending_startup_item_is_discarded_if_baro_stock_is_active(
        tmp_market):
    now = int(time.time())
    store = _watched_item_store()
    store.upsert_bargain_item_daily_baseline(
        "arcane_grace", "rank:5", 100, source_id="before-baro",
        source_datetime=datetime.fromtimestamp(
            now - 86400, timezone.utc).isoformat(), source_ts=now - 86400)
    queue = asyncio.Queue()
    poller = _bargain_poller(store, _cfg(), queue)
    poller._item_stats_verified_at[("arcane_grace", "rank:5")] = time.time()

    assert await poller.handle_new_item_order(
        _order(50, oid="pending-baro-item")) == 0

    async def active_baro():
        return {"VoidTraders": [{
            "_id": {"$oid": "baro-pending-event"},
            "Activation": {"$date": {"$numberLong": str((now - 60) * 1000)}},
            "Expiry": {"$date": {"$numberLong": str((now + 3600) * 1000)}},
            "Manifest": [{
                "ItemType": "/Lotus/StoreItems/Types/Items/MiscItems/ArcaneGrace",
            }],
        }]}

    poller.wfm.world_state = active_baro
    assert await poller._baro_once() == 1
    assert store.is_bargain_item_order_seen("pending-baro-item")
    assert queue.empty()
    await poller.wfm.close()
    store.close()


@pytest.mark.asyncio
async def test_pending_item_is_discarded_after_successful_missing_bucket(
        tmp_market):
    store = _watched_item_store()
    store.upsert_bargain_item_daily_baseline(
        "arcane_grace", "rank:5", 100, source_id="stale-max",
        source_datetime="2026-07-20T00:00:00Z", source_ts=100)
    queue = asyncio.Queue()
    poller = _bargain_poller(store, _cfg(), queue)
    poller._baro_ready.set()

    assert await poller.handle_new_item_order(
        _order(50, oid="missing-current-bucket")) == 0

    async def only_zero_rank(_slug):
        return [{
            "id": "zero-only", "datetime": "2026-07-21T00:00:00Z",
            "avg_price": 20, "mod_rank": 0, "volume": 5,
        }]

    poller.wfm.item_statistics = only_zero_rank
    await poller._refresh_item_statistics(
        "arcane_grace", store.list_bargain_items())
    assert store.is_bargain_item_order_seen("missing-current-bucket")
    assert queue.empty()
    await poller.wfm.close()
    store.close()


@pytest.mark.asyncio
async def test_active_baro_snapshot_immediately_covers_new_config(tmp_market):
    now = int(time.time())
    store = Store(":memory:")
    poller = _bargain_poller(store, _cfg())

    async def active_baro():
        return {"VoidTraders": [{
            "_id": {"$oid": "active-before-config"},
            "Activation": {"$date": {"$numberLong": str((now - 60) * 1000)}},
            "Expiry": {"$date": {"$numberLong": str((now + 3600) * 1000)}},
            "Manifest": [{
                "ItemType": "/Lotus/StoreItems/Types/Items/MiscItems/ArcaneGrace",
            }],
        }]}

    poller.wfm.world_state = active_baro
    assert await poller._baro_once() == 0  # 扫描时尚无监控配置。
    store.upsert_qq_target(GROUP, 42, enabled=True)
    store.add_bargain_item(GROUP, "arcane_grace", 0.20, "max")
    store.upsert_bargain_item_daily_baseline(
        "arcane_grace", "rank:5", 100, source_id="pre-baro-day",
        source_datetime=datetime.fromtimestamp(
            now - 3600, timezone.utc).isoformat(), source_ts=now - 3600)
    poller._item_stats_verified_at[("arcane_grace", "rank:5")] = time.time()

    assert await poller.handle_new_item_order(
        _order(1, oid="new-config-during-baro")) == 0
    await poller.wfm.close()
    store.close()


def _watched_riven_store():
    store = Store(":memory:")
    store.upsert_qq_target(GROUP, 42, enabled=True)
    store.add_bargain_riven_item(GROUP, "torid", 0.35)
    return store


@pytest.mark.asyncio
async def test_riven_candidate_uses_rolling_cache_allows_offline_and_is_permanent(
        tmp_market, monkeypatch):
    now = int(time.time())
    store = _watched_riven_store()
    store.add_blacklist(GROUP, "Seller")
    store.add_blacklist(GROUP, "Seller", scope="channel")
    store.add_bargain_riven_sample(
        "torid", "100.25", 5, "0.1", sampled_at=now,
        sample_hour=store.bargain_utc_hour(now))
    queue = asyncio.Queue()
    poller = _bargain_poller(store, _cfg(), queue)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("候选判断不得即时查询紫卡挂单簿")

    poller.wfm.riven_search = forbidden
    monkeypatch.setattr(time, "time", lambda: float(now))
    candidate = _auction(
        60, aid="offline-deal", status="offline",
        created=_created(10, now=datetime.fromtimestamp(now, timezone.utc)))
    assert await poller.on_fresh_riven_auctions([candidate]) == 1
    assert await poller.on_fresh_riven_auctions([candidate]) == 0
    assert queue.qsize() == 1
    payload = queue.get_nowait().payload
    assert isinstance(payload, bargain.BargainRivenPushPayload)
    assert payload.target_scope == GROUP
    assert payload.auction["id"] == "offline-deal"
    assert store.is_bargain_riven_notified("offline-deal")
    await poller.wfm.close()
    store.close()


@pytest.mark.asyncio
async def test_old_or_non_direct_riven_never_notifies_but_old_orders_can_sample(
        tmp_market, monkeypatch):
    now = int(time.time())
    store = _watched_riven_store()
    store.add_bargain_riven_sample(
        "torid", 100, 5, 0, sampled_at=now,
        sample_hour=store.bargain_utc_hour(now))
    queue = asyncio.Queue()
    poller = _bargain_poller(store, _cfg(), queue)
    monkeypatch.setattr(time, "time", lambda: float(now))
    now_dt = datetime.fromtimestamp(now, timezone.utc)
    old = _auction(1, aid="old", created=_created(3601, now=now_dt))
    auction_only = _auction(
        None, aid="starting", direct=False, starting=1,
        created=_created(1, now=now_dt))
    assert await poller.on_fresh_riven_auctions([old, auction_only]) == 0
    assert queue.empty()

    # 年龄不参与样本筛选；五个一小时前创建的在线直售单仍可形成样本。
    old_book = [
        _auction(100 + i, aid=f"old-{i}", created=_created(86400, now=now_dt))
        for i in range(5)
    ]
    sample = bargain.build_riven_hour_sample(old_book)
    assert sample is not None
    await poller.wfm.close()
    store.close()


@pytest.mark.asyncio
async def test_riven_poller_respects_custom_window_and_minimum(tmp_market, monkeypatch):
    now = int(time.time())
    store = _watched_riven_store()
    store.add_bargain_riven_sample(
        "torid", 100, 5, 0, sampled_at=now - 11 * 3600,
        sample_hour=store.bargain_utc_hour(now - 11 * 3600))
    store.add_bargain_riven_sample(
        "torid", 200, 5, 0, sampled_at=now - 13 * 3600,
        sample_hour=store.bargain_utc_hour(now - 13 * 3600))
    merged, error = bargain.validate_params({
        "riven_rolling_hours": 12, "riven_min_valid_samples": 2})
    assert error is None
    bargain._params.update(merged)
    poller = _bargain_poller(store, _cfg())
    baseline, count = poller._riven_rolling_baseline("torid", now)
    assert baseline is None and count == 1

    bargain._params["riven_min_valid_samples"] = 1
    baseline, count = poller._riven_rolling_baseline("torid", now)
    assert baseline == Decimal("100") and count == 1
    await poller.wfm.close()
    store.close()


@pytest.mark.asyncio
async def test_new_riven_sample_request_jumps_a_long_scan_queue(tmp_market):
    store = Store(":memory:")
    store.upsert_qq_target(GROUP, 42, enabled=True)
    store.add_bargain_riven_item(GROUP, "alpha")
    store.add_bargain_riven_item(GROUP, "zeta")
    poller = _bargain_poller(store, _cfg())
    calls = []

    async def sample(weapon, *, immediate=False, now=None):
        calls.append((weapon, immediate))
        if weapon == "alpha":
            poller.request_riven_sample("urgent")
        return True

    poller._sample_riven_weapon = sample
    assert await poller._riven_sample_tick() == 3
    assert calls == [
        ("alpha", False), ("urgent", True), ("zeta", False)]
    await poller.wfm.close()
    store.close()


@pytest.mark.asyncio
async def test_restart_does_not_treat_existing_riven_config_as_new(tmp_market):
    store = Store(":memory:")
    store.upsert_qq_target(GROUP, 42, enabled=True)
    store.add_bargain_riven_item(GROUP, "torid")
    now = int(time.time())
    store.claim_bargain_riven_sample_hour(
        "torid", sample_hour=store.bargain_utc_hour(now), attempted_at=now)
    poller = _bargain_poller(store, _cfg())
    requests = 0

    async def search(_weapon):
        nonlocal requests
        requests += 1
        return []

    poller.wfm.riven_search = search
    assert await poller._riven_sample_tick() == 0
    assert requests == 0
    await poller.wfm.close()
    store.close()


@pytest.mark.asyncio
async def test_catalog_rank_changes_reconcile_group_configs(tmp_market):
    store = Store(":memory:")
    ranked_group = store.add_bargain_item(
        GROUP, "arcane_grace", level=None)
    plain_group = store.add_bargain_item(
        GROUP, "mesa_prime_set", level="max")
    poller = _bargain_poller(store, _cfg())

    assert poller._reconcile_item_config_levels() == 2
    ranked = store.get_bargain_item(ranked_group, GROUP)
    assert "enabled" not in ranked and ranked["level"] == "max"
    assert store.get_bargain_item(plain_group, GROUP)["level"] is None
    await poller.wfm.close()
    store.close()


# ---- Baro：直到新日桶才恢复 ----

@pytest.mark.asyncio
async def test_baro_dynamic_schedule_uses_boundaries_and_coarse_calibration():
    now = 1_800_000_000
    store = Store(":memory:")
    poller = _bargain_poller(store, _cfg())

    def trader(activation: int, expiry: int, *, manifest=True, event_id=True):
        return {
            "_id": {"$oid": "baro-event"} if event_id else None,
            "Activation": {"$date": {"$numberLong": str(activation * 1000)}},
            "Expiry": {"$date": {"$numberLong": str(expiry * 1000)}},
            "Manifest": ([{"ItemType": "/Lotus/Items/Example"}]
                         if manifest else []),
        }

    assert poller._baro_delay_from_state(
        {"VoidTraders": []}, now=now) == 3600
    assert poller._baro_delay_from_state({
        "VoidTraders": [trader(now + 14 * 86400, now + 16 * 86400,
                               manifest=False)],
    }, now=now) == 6 * 3600
    assert poller._baro_delay_from_state({
        "VoidTraders": [trader(now + 2 * 3600, now + 50 * 3600,
                               manifest=False)],
    }, now=now) == 2 * 3600 + 5
    assert poller._baro_delay_from_state({
        "VoidTraders": [trader(now - 60, now + 2 * 3600)],
    }, now=now) == 2 * 3600 + 5
    assert poller._baro_delay_from_state({
        "VoidTraders": [trader(now - 60, now + 2 * 3600, manifest=False)],
    }, now=now) == 60
    assert poller._baro_delay_from_state({
        "VoidTraders": [trader(now - 60, now + 2 * 3600, event_id=False)],
    }, now=now) == 60

    await poller.wfm.close()
    store.close()


@pytest.mark.asyncio
async def test_baro_loop_uses_dynamic_delay_and_resets_failure_backoff(
        monkeypatch):
    store = Store(":memory:")
    poller = _bargain_poller(store, _cfg())
    attempts = 0
    delays = []

    async def baro_once():
        nonlocal attempts
        attempts += 1
        if attempts in {1, 3}:
            raise httpx.ConnectError(
                "offline", request=httpx.Request("GET", "https://example.invalid"))
        poller._baro_next_delay_seconds = 777
        return 0

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(poller, "_baro_once", baro_once)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await poller._baro_loop()
    assert delays == [60, 777, 60]

    await poller.wfm.close()
    store.close()


@pytest.mark.asyncio
async def test_baro_pause_survives_same_daily_bucket_and_resumes_on_new_one(
        tmp_market):
    now = time.time()
    old_bucket_ts = int(now) - 3600
    new_bucket_ts = int(now) + 3600
    store = _watched_item_store()
    store.upsert_bargain_item_daily_baseline(
        "arcane_grace", "rank:5", 100, source_id="day-1",
        source_datetime=datetime.fromtimestamp(
            old_bucket_ts, timezone.utc).isoformat(),
        source_ts=old_bucket_ts, volume=5)
    poller = _bargain_poller(store, _cfg())

    async def world_state():
        return {"VoidTraders": [{
            "_id": {"$oid": "baro-event"},
            "Activation": {"$date": {"$numberLong": str(int((now - 10) * 1000))}},
            "Expiry": {"$date": {"$numberLong": str(int((now + 3600) * 1000))}},
            # 世界状态带 /StoreItems，WFM 目录 gameRef 不带；映射需规范化。
            "Manifest": [{
                "ItemType": "/Lotus/StoreItems/Types/Items/MiscItems/ArcaneGrace",
            }],
        }]}

    poller.wfm.world_state = world_state
    assert await poller._baro_once() == 1
    assert store.is_bargain_item_paused("arcane_grace", "rank:5")

    stats = [{
        "id": "day-1", "datetime": datetime.fromtimestamp(
            old_bucket_ts, timezone.utc).isoformat(),
        "avg_price": 100, "mod_rank": 5, "volume": 5,
    }]

    async def item_statistics(_slug):
        return list(stats)

    poller.wfm.item_statistics = item_statistics
    await poller._refresh_item_statistics(
        "arcane_grace", store.list_bargain_items())
    assert store.is_bargain_item_paused("arcane_grace", "rank:5")

    stats[:] = [{
        "id": "day-2", "datetime": datetime.fromtimestamp(
            new_bucket_ts, timezone.utc).isoformat(),
        "avg_price": 80, "mod_rank": 5, "volume": 9,
    }]
    await poller._refresh_item_statistics(
        "arcane_grace", store.list_bargain_items())
    assert not store.is_bargain_item_paused("arcane_grace", "rank:5")
    assert store.get_bargain_item_daily_baseline(
        "arcane_grace", "rank:5")["price"] == Decimal("80")
    # 商人仍在场时后续轮询不能把已经等到新日桶的道具再次暂停。
    assert await poller._baro_once() == 0
    assert not store.is_bargain_item_paused("arcane_grace", "rank:5")
    await poller.wfm.close()
    store.close()


@pytest.mark.asyncio
async def test_baro_does_not_pause_when_post_activation_bucket_already_cached(
        tmp_market):
    now = int(time.time())
    store = _watched_item_store()
    store.upsert_bargain_item_daily_baseline(
        "arcane_grace", "rank:5", 80, source_id="post-baro-day",
        source_datetime=datetime.fromtimestamp(
            now - 1800, timezone.utc).isoformat(), source_ts=now - 1800)
    poller = _bargain_poller(store, _cfg())

    async def world_state():
        return {"VoidTraders": [{
            "_id": {"$oid": "baro-late-poll"},
            "Activation": {"$date": {"$numberLong": str((now - 3600) * 1000)}},
            "Expiry": {"$date": {"$numberLong": str((now + 3600) * 1000)}},
            "Manifest": [{
                "ItemType": "/Lotus/StoreItems/Types/Items/MiscItems/ArcaneGrace",
            }],
        }]}

    poller.wfm.world_state = world_state
    assert await poller._baro_once() == 0
    assert not store.is_bargain_item_paused("arcane_grace", "rank:5")
    await poller.wfm.close()
    store.close()


@pytest.mark.asyncio
async def test_baro_rank_buckets_resume_independently(tmp_market):
    now = int(time.time())
    old_ts, new_ts = now - 3600, now + 3600
    store = Store(":memory:")
    store.upsert_qq_target(GROUP, 42, enabled=True)
    store.upsert_qq_target(GROUP + 1, 42, enabled=True)
    store.add_bargain_item(GROUP, "arcane_grace", level="max")
    store.add_bargain_item(GROUP + 1, "arcane_grace", level="0")
    for bucket in ("rank:0", "rank:5"):
        store.upsert_bargain_item_daily_baseline(
            "arcane_grace", bucket, 100, source_id="old",
            source_datetime=datetime.fromtimestamp(
                old_ts, timezone.utc).isoformat(), source_ts=old_ts)
    poller = _bargain_poller(store, _cfg())

    async def world_state():
        return {"VoidTraders": [{
            "_id": {"$oid": "baro-ranks"},
            "Activation": {"$date": {"$numberLong": str((now - 10) * 1000)}},
            "Expiry": {"$date": {"$numberLong": str((now + 3600) * 1000)}},
            "Manifest": [{
                "ItemType": "/Lotus/StoreItems/Types/Items/MiscItems/ArcaneGrace",
            }],
        }]}

    poller.wfm.world_state = world_state
    assert await poller._baro_once() == 2

    async def statistics(_slug):
        return [
            {"id": "new-zero", "datetime": datetime.fromtimestamp(
                new_ts, timezone.utc).isoformat(), "avg_price": 70,
             "mod_rank": 0},
            {"id": "old-max", "datetime": datetime.fromtimestamp(
                old_ts, timezone.utc).isoformat(), "avg_price": 100,
             "mod_rank": 5},
        ]

    poller.wfm.item_statistics = statistics
    await poller._refresh_item_statistics(
        "arcane_grace", store.list_bargain_items())
    assert not store.is_bargain_item_paused("arcane_grace", "rank:0")
    assert store.is_bargain_item_paused("arcane_grace", "rank:5")
    await poller.wfm.close()
    store.close()
