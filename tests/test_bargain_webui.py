"""捡漏 WebUI 端点：参数校验、名称解析、监控条目 CRUD、推送日志。"""

import json
import shutil
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.plugins.riven_sniper import bargain, envutil, marketdata, rivendata, shared  # noqa: E402
from src.plugins.riven_sniper.store import Store  # noqa: E402
from src.plugins.riven_sniper.webui import api as webui_api  # noqa: E402
GID = 111
HTML = (ROOT / "src/plugins/riven_sniper/webui/static/index.html").read_text(
    encoding="utf-8")


def _cfg_ns(**kw):
    base = dict(sniper_poll_interval=15.0, sniper_dry_run=True,
                sniper_max_configs_per_group=20, sniper_send_interval=1.5,
                sniper_send_concurrency=0, trade_message_ttl_seconds=60,
                bargain_max_distinct_slugs=3,
                discord_dm_enabled=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


@pytest.fixture()
def client(monkeypatch, tmp_path):
    items = {
        "arcane_grace": {"id": "IID1", "zh": "赋能·优雅", "en": "Arcane Grace",
                         "tags": ["arcane_enhancement"], "max_rank": 5},
        "mesa_prime_set": {"id": "IID2", "zh": "女枪手Prime 一套",
                           "en": "Mesa Prime Set", "tags": ["prime"],
                           "max_rank": 0},
        "mesa_prime_chassis": {"id": "IID3", "zh": "女枪手Prime 机体",
                               "en": "Mesa Prime Chassis", "tags": ["prime"],
                               "max_rank": 0},
        "axi_a1_relic": {"id": "IID4", "zh": "古纪 A1 遗物",
                         "en": "Axi A1 Relic", "tags": ["relic"],
                         "max_rank": 0},
    }
    (tmp_path / "market_items.json").write_text(
        json.dumps({"fetched_at": 1, "items": items}, ensure_ascii=False),
        encoding="utf-8")
    for f in ("weapons.json", "attributes.json", "riven_values.json", "aliases.json"):
        shutil.copy(ROOT / "data" / f, tmp_path / f)
    monkeypatch.setattr(rivendata, "DATA_DIR", tmp_path)
    rivendata.invalidate_caches()
    marketdata.invalidate()

    store = Store(":memory:")
    store.upsert_qq_target(GID, 999, enabled=True)
    monkeypatch.setattr(shared, "_store", store)
    monkeypatch.setattr(shared, "_config", _cfg_ns())
    monkeypatch.setattr(shared, "_poller", None)
    env = tmp_path / ".env"
    env.write_text("X=1\n", encoding="utf-8")
    monkeypatch.setattr(envutil, "ENV_PATH", env)
    bargain._params.clear()
    bargain._params.update(bargain.DEFAULT_PARAMS)
    app = FastAPI()
    app.include_router(webui_api.router, prefix="/admin")
    with TestClient(app) as c:
        yield c, store
    store.close()
    rivendata.invalidate_caches()
    marketdata.invalidate()
    bargain._params.clear()
    bargain._params.update(bargain.DEFAULT_PARAMS)


def test_bargain_endpoints_are_open_without_authentication(client):
    c, _ = client
    fresh = TestClient(c.app)
    assert fresh.get("/admin/api/bargain/params").status_code == 200
    assert fresh.get(f"/admin/api/groups/{GID}/bargain").status_code == 200


def test_console_exposes_only_rebuilt_bargain_parameters_and_dynamic_levels():
    bargain_start = HTML.index('<div id="sec-bargain"')
    bargain_end = HTML.index('<div id="sec-records"', bargain_start)
    system_start = HTML.index('<div id="sec-system"')
    system_end = HTML.index('</main>', system_start)
    bargain_panel = HTML[bargain_start:bargain_end]
    system_panel = HTML[system_start:system_end]

    for control_id in (
            "bp-item", "bp-riven", "bp-riven-hours", "bp-riven-samples"):
        assert f'id="{control_id}"' in system_panel
        assert f'id="{control_id}"' not in bargain_panel
    for removed_id in (
            "bp-base", "bp-gap", "bp-samples", "bp-ttl", "bp-cd", "bp-cap",
            "st-bg-recent", "st-bg-baseline"):
        assert f'id="{removed_id}"' not in HTML
    assert 'id="bg-level-field" hidden' in HTML
    assert 'id="bg-level"' in HTML
    assert "Number(match.max_rank) > 0" in HTML
    assert "bargain-rivens" in HTML
    assert "bg-master-toggle" not in HTML
    assert "bgConfigToggle" not in HTML
    assert "riven_enabled" not in HTML
    assert "q !== $('#bg-name').value.trim()" in HTML
    assert "bargainParams.item_threshold * 100).toFixed(8)" in HTML
    assert "Math.round(p.item_threshold * 100)" not in HTML
    assert 'data-tab="params"' not in bargain_panel
    assert "算法参数" not in bargain_panel
    assert "bgLoadParams" not in HTML
    assert 'id="bg-edit-panel" hidden' in HTML
    assert 'id="bg-edit-level"' in HTML
    assert 'id="bg-edit-threshold"' in HTML
    assert "function bgOpenEdit(" in HTML
    assert "async function bgEditSave(" in HTML
    assert 'id="bg-edit-trigger-item-${gid}-${it.id}"' in HTML
    assert "firstField.focus({preventScroll:true})" in HTML
    assert "restoreFocusById(returnFocusId)" in HTML
    assert "prompt('折扣阈值" not in HTML
    assert "prompt('道具等级" not in HTML


def test_params_get_put_and_validation(client):
    c, store = client
    d = c.get("/admin/api/bargain/params").json()
    assert d["params"]["item_threshold"] == 0.20
    assert d["defaults"]["riven_threshold"] == 0.35
    assert d["defaults"]["riven_rolling_hours"] == 24
    assert d["defaults"]["riven_min_valid_samples"] == 1

    r = c.put("/admin/api/bargain/params",
              json={"params": {"item_threshold": 0.255,
                               "riven_rolling_hours": 36,
                               "riven_min_valid_samples": 12}})
    assert r.status_code == 200
    assert r.json()["params"]["item_threshold"] == 0.255
    assert bargain.params()["riven_rolling_hours"] == 36

    assert c.put("/admin/api/bargain/params",
                 json={"params": {"riven_rolling_hours": 11}}).status_code == 400
    assert c.put("/admin/api/bargain/params",
                 json={"params": {"riven_min_valid_samples": 37}}).status_code == 400
    assert c.put("/admin/api/bargain/params",
                 json={"params": {"bogus": 1}}).status_code == 400
    assert any(a["action"] == "bargain_params" for a in store.list_audit(20))


def test_resolve(client):
    c, _ = client
    d = c.get("/admin/api/bargain/resolve", params={"q": "优雅"}).json()
    assert [m["slug"] for m in d["matches"]] == ["arcane_grace"]
    assert d["matches"][0]["max_rank"] == 5
    assert d["matches"][0]["has_level"] is True
    assert d["available"]
    multi = c.get("/admin/api/bargain/resolve", params={"q": "mesa prime"}).json()
    assert len(multi["matches"]) == 2
    assert c.get("/admin/api/bargain/resolve", params={"q": ""}).json()["matches"] == []


def test_bargain_listing_has_no_switches_and_items_crud(client):
    c, store = client
    d = c.get(f"/admin/api/groups/{GID}/bargain").json()
    assert "settings" not in d
    assert d["items"] == []

    r = c.patch(f"/admin/api/groups/{GID}/bargain", json={"items_enabled": True})
    assert r.status_code == 405
    assert c.patch(f"/admin/api/groups/{GID}/bargain", json={}).status_code == 405

    assert c.post(f"/admin/api/groups/{GID}/bargain-items",
                  json={"query": "Arcane Grace", "threshold": 25}).status_code == 400
    r = c.post(f"/admin/api/groups/{GID}/bargain-items",
               json={"query": "Arcane Grace", "threshold": 25,
                     "level": "max"})
    assert r.status_code == 200
    iid = r.json()["id"]
    row = store.get_bargain_item(iid, GID)
    assert row["slug"] == "arcane_grace" and row["threshold"] == 0.25
    assert row["level"] == "max"

    d = c.get(f"/admin/api/groups/{GID}/bargain").json()
    assert d["items"][0]["display"] == "赋能·优雅 (Arcane Grace)"
    assert d["items"][0]["max_rank"] == 5

    # 歧义 / 重复 / 找不到 → 400
    assert c.post(f"/admin/api/groups/{GID}/bargain-items",
                  json={"query": "mesa prime"}).status_code == 400
    assert c.post(f"/admin/api/groups/{GID}/bargain-items",
                  json={"query": "arcane grace"}).status_code == 400
    assert c.post(f"/admin/api/groups/{GID}/bargain-items",
                  json={"query": "不存在xyz"}).status_code == 400
    # maxRank=0 不接受等级字段。
    assert c.post(f"/admin/api/groups/{GID}/bargain-items",
                  json={"query": "Mesa Prime Set", "level": "0"}).status_code == 400

    # 改阈值 / 恢复默认阈值；普通道具不再有单项开关。
    assert c.patch(f"/admin/api/groups/{GID}/bargain-items/{iid}",
                   json={"threshold": 40}).status_code == 200
    assert store.get_bargain_item(iid, GID)["threshold"] == 0.40
    assert c.patch(f"/admin/api/groups/{GID}/bargain-items/{iid}",
                   json={"clear_threshold": True}).status_code == 200
    assert store.get_bargain_item(iid, GID)["threshold"] is None
    assert c.patch(f"/admin/api/groups/{GID}/bargain-items/{iid}",
                   json={"enabled": False}).status_code == 400
    assert "enabled" not in store.get_bargain_item(iid, GID)
    assert c.patch(f"/admin/api/groups/{GID}/bargain-items/{iid}",
                   json={"threshold": 120}).status_code == 422  # 越界
    assert c.patch(f"/admin/api/groups/{GID}/bargain-items/{iid}",
                   json={"threshold": 25.5}).status_code == 422
    assert c.patch(f"/admin/api/groups/{GID}/bargain-items/9999",
                   json={"enabled": True}).status_code == 404
    assert c.patch(f"/admin/api/groups/{GID}/bargain-items/{iid}",
                   json={"level": "0"}).status_code == 200
    assert store.get_bargain_item(iid, GID)["level"] == "0"

    assert c.delete(f"/admin/api/groups/{GID}/bargain-items/{iid}").status_code == 200
    assert c.delete(f"/admin/api/groups/{GID}/bargain-items/{iid}").status_code == 404


def test_cap_counts_global_distinct_slugs(client):
    c, store = client
    arcane = c.post(f"/admin/api/groups/{GID}/bargain-items", json={
        "query": "Arcane Grace", "level": "0",
    })
    assert arcane.status_code == 200
    assert c.post(f"/admin/api/groups/{GID}/bargain-items", json={
        "query": "Mesa Prime Set",
    }).status_code == 200
    assert c.post(f"/admin/api/groups/{GID}/bargain-rivens", json={
        "query": "Torid",
    }).status_code == 200
    assert store.count_bargain_distinct_slugs() == 3

    # 其它群复用已存在 slug 不占新额度；首次出现的新 slug 会被拒绝。
    other_gid = GID + 1
    store.upsert_qq_target(other_gid, 999, enabled=True)
    reused = c.post(f"/admin/api/groups/{other_gid}/bargain-items", json={
        "query": "Arcane Grace", "level": "max",
    })
    assert reused.status_code == 200
    assert store.count_bargain_distinct_slugs() == 3
    capped = c.post(f"/admin/api/groups/{other_gid}/bargain-items", json={
        "query": "Axi A1 Relic",
    })
    assert capped.status_code == 400 and "上限" in capped.json()["detail"]

    # 删除最后一条同 slug 配置后才释放额度。
    reused_id = reused.json()["id"]
    assert c.delete(
        f"/admin/api/groups/{GID}/bargain-items/{arcane.json()['id']}"
    ).status_code == 200
    assert store.count_bargain_distinct_slugs() == 3
    assert c.delete(
        f"/admin/api/groups/{other_gid}/bargain-items/{reused_id}"
    ).status_code == 200
    assert store.count_bargain_distinct_slugs() == 2
    assert c.post(f"/admin/api/groups/{other_gid}/bargain-items", json={
        "query": "Axi A1 Relic",
    }).status_code == 200


def test_log_endpoint_is_removed(client):
    c, _ = client
    assert c.get("/admin/api/bargain/log").status_code == 404


def test_settings_includes_bargain_limit_only(client):
    c, cfg = client[0], shared.get_config()
    d = c.get("/admin/api/settings").json()
    assert "bargain_recent_interval" not in d["hot"]
    assert "bargain_baseline_interval" not in d["hot"]
    r = c.patch("/admin/api/settings", json={"bargain_max_distinct_slugs": 4})
    assert r.status_code == 200 and cfg.bargain_max_distinct_slugs == 4


def test_riven_crud_in_admin_console(client):
    c, store = client
    created = c.post(f"/admin/api/groups/{GID}/bargain-rivens",
                     json={"query": "Torid", "threshold": 40})
    assert created.status_code == 200
    iid = created.json()["id"]
    assert store.get_bargain_riven_item(iid, GID)["threshold"] == 0.40
    assert c.post(f"/admin/api/groups/{GID}/bargain-rivens",
                  json={"query": "Nami Solo", "threshold": 4.6}).status_code == 422

    listing = c.get(f"/admin/api/groups/{GID}/bargain").json()
    assert listing["riven_weapons"][0]["weapon_slug"] == "torid"
    assert c.patch(f"/admin/api/groups/{GID}/bargain-rivens/{iid}",
                   json={"threshold": 30}).status_code == 200
    assert c.patch(f"/admin/api/groups/{GID}/bargain-rivens/{iid}",
                   json={"enabled": False}).status_code == 400
    assert c.patch(f"/admin/api/groups/{GID}/bargain-rivens/{iid}",
                   json={"threshold": 30.5}).status_code == 422
    assert store.get_bargain_riven_item(iid, GID)["threshold"] == 0.30
    assert "enabled" not in store.get_bargain_riven_item(iid, GID)
    assert c.patch(f"/admin/api/groups/{GID}/bargain-rivens/{iid}",
                   json={"clear_threshold": True}).status_code == 200
    assert store.get_bargain_riven_item(iid, GID)["threshold"] is None
    assert c.delete(f"/admin/api/groups/{GID}/bargain-rivens/{iid}").status_code == 200
    assert c.delete(f"/admin/api/groups/{GID}/bargain-rivens/{iid}").status_code == 404
