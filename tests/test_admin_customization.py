"""管理端命令别名、已移除接口和测试推送队列格式测试。"""

import sys
import time
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.plugins.riven_sniper import envutil, shared, texts  # noqa: E402
from src.plugins.riven_sniper.command_meta import (  # noqa: E402
    ACTIVE_ALIASES,
    ACTIVE_COMMAND_NAMES,
    COMMAND_NODES,
)
from src.plugins.riven_sniper.delivery import DeliveryItem  # noqa: E402
from src.plugins.riven_sniper.store import Store  # noqa: E402
from src.plugins.riven_sniper.webui import api as webui_api  # noqa: E402


def _cfg_ns(**kw):
    base = dict(sniper_poll_interval=15.0, sniper_dry_run=True,
                sniper_max_configs_per_group=20, sniper_send_interval=1.5,
                sniper_send_concurrency=0, send_queue_maxsize=1000,
                trade_message_ttl_seconds=60,
                bargain_max_distinct_slugs=30,
                discord_dm_enabled=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


@pytest.fixture()
def client(monkeypatch, tmp_path):
    store = Store(":memory:")
    store.upsert_qq_target(111, 999, enabled=True)
    monkeypatch.setattr(shared, "_store", store)
    monkeypatch.setattr(shared, "_config", _cfg_ns())
    monkeypatch.setattr(shared, "_poller", None)
    env = tmp_path / ".env"
    env.write_text("X=1\n", encoding="utf-8")
    monkeypatch.setattr(envutil, "ENV_PATH", env)
    for key in list(ACTIVE_ALIASES):
        monkeypatch.delitem(ACTIVE_ALIASES, key)
    for key in list(ACTIVE_COMMAND_NAMES):
        monkeypatch.delitem(ACTIVE_COMMAND_NAMES, key)
    for node in COMMAND_NODES:
        monkeypatch.setitem(ACTIVE_COMMAND_NAMES, node.id, node.default_name)
    app = FastAPI()
    app.include_router(webui_api.router, prefix="/admin")
    with TestClient(app) as c:
        yield c, store
    store.close()


# ---- 命令别名 ----

def test_command_alias_crud_and_pending(client):
    c, store = client
    r = c.post("/admin/api/commands/sniper.add/aliases", json={"alias": "狙"})
    assert r.status_code == 200 and r.json()["restart_required"]
    assert store.list_command_aliases() == {"sniper.add": ["狙"]}
    d = c.get("/admin/api/commands").json()
    helper = next(x for x in d["commands"] if x["id"] == "sniper.add")
    assert helper["aliases"] == ["狙"] and helper["pending_restart"]
    assert helper["mapping_name"] == "sniper.add"
    assert helper["trigger_name"] == "狙击添加"
    assert "standard_name" not in helper and "standard_path" not in helper
    # 运行中已生效 -> 不再 pending
    ACTIVE_ALIASES["sniper.add"] = ["狙"]
    helper = next(x for x in c.get("/admin/api/commands").json()["commands"]
                  if x["id"] == "sniper.add")
    assert not helper["pending_restart"]
    # 全局冲突：其他触发名 / 退役名 / 内部映射名 / 已有别名 / 空白
    assert c.post("/admin/api/commands/tracking.open/aliases",
                  json={"alias": "黑名单"}).status_code == 400
    assert c.post("/admin/api/commands/tracking.open/aliases",
                  json={"alias": "狙击复制"}).status_code == 400
    assert c.post("/admin/api/commands/tracking.open/aliases",
                  json={"alias": "sniper.add"}).status_code == 400
    assert c.post("/admin/api/commands/tracking.open/aliases",
                  json={"alias": "狙"}).status_code == 400
    assert c.post("/admin/api/commands/tracking.open/aliases",
                  json={"alias": "查 人"}).status_code == 400
    assert c.post("/admin/api/commands/tracking.open/aliases",
                  json={"alias": "  "}).status_code == 400
    assert c.post("/admin/api/commands/not-found/aliases",
                  json={"alias": "x"}).status_code == 404
    changed = c.patch(
        "/admin/api/commands/tracking.open/name", json={"name": "查人"})
    assert changed.status_code == 200 and changed.json()["restart_required"]
    assert store.list_command_names()["tracking.open"] == "查人"
    renamed = next(
        x for x in c.get("/admin/api/commands").json()["commands"]
        if x["id"] == "tracking.open"
    )
    assert renamed["trigger_name"] == "查人"
    assert renamed["name_pending_restart"] and renamed["pending_restart"]
    assert c.patch(
        "/admin/api/commands/tracking.open/name",
        json={"name": "sniper.add"},
    ).status_code == 400
    assert c.patch(
        "/admin/api/commands/tracking.open/name",
        json={"name": "狙"},
    ).status_code == 400
    assert c.patch("/admin/api/commands/sniper.add/aliases/狙",
                   json={"new_alias": "狙狙"}).status_code == 200
    assert c.patch("/admin/api/commands/sniper.add/aliases/狙狙",
                   json={"new_alias": "命令大全"}).status_code == 400
    assert store.list_command_aliases() == {"sniper.add": ["狙狙"]}
    assert c.patch("/admin/api/commands/sniper.add/aliases/不存在",
                   json={"new_alias": "x"}).status_code == 404
    assert c.delete(
        "/admin/api/commands/sniper.add/aliases/狙狙").status_code == 200
    assert store.list_command_aliases() == {}
    assert c.delete(
        "/admin/api/commands/sniper.add/aliases/狙狙").status_code == 404


def test_command_name_and_alias_uniqueness_is_global(client):
    c, _ = client
    assert c.post(
        "/admin/api/commands/tracker.manage.list/aliases",
        json={"alias": "ls"},
    ).status_code == 200
    assert c.post(
        "/admin/api/commands/tracker.manage.delete/aliases",
        json={"alias": "ls"},
    ).status_code == 400
    assert c.post(
        "/admin/api/commands/bargain.riven.list/aliases",
        json={"alias": "ls"},
    ).status_code == 400
    assert c.patch(
        "/admin/api/commands/bargain.riven.list/name",
        json={"name": "ls"},
    ).status_code == 400


def test_base_aliases_seeded_and_editable(client):
    """历史内置别名首启种入 DB 后即为普通可编辑别名：唯一性生效、可删、种子幂等。"""
    from src.plugins.riven_sniper import command_meta
    c, store = client
    command_meta.seed_base_aliases(store)
    m = store.list_command_aliases()
    assert set(m) == {node.id for node in COMMAND_NODES}
    assert m["blacklist.add"] == ["b"]
    assert set(m["tracker.manage"]) == {"t", "Trackers"}
    assert set(m["tracking.open.riven"]) == {"rh", "riven"}
    n = sum(map(len, m.values()))
    # 幂等：再种一次数量不变
    command_meta.seed_base_aliases(store)
    assert sum(map(len, store.list_command_aliases().values())) == n
    # GET /commands 里作为可编辑别名出现，且不再有 base_aliases 字段
    helper = next(x for x in c.get("/admin/api/commands").json()["commands"]
                  if x["id"] == "tracker.manage")
    assert "Trackers" in helper["aliases"] and "base_aliases" not in helper
    # 唯一性：已种入的别名不能再加到别的命令
    assert c.post("/admin/api/commands/tracking.open/aliases",
                  json={"alias": "Trackers"}).status_code == 400
    # 已删除的空格子命令写法不能作为顶级别名恢复。
    assert c.post("/admin/api/commands/blacklist.add/aliases",
                  json={"alias": "黑名单 添加"}).status_code == 400
    assert c.post("/admin/api/commands/blacklist.delete/aliases",
                  json={"alias": "黑名单 删除"}).status_code == 400
    # 可删除；删后重新种子（标志已置位）不会复活
    assert store.remove_command_alias("tracker.manage", "Trackers")
    command_meta.seed_base_aliases(store)
    assert "Trackers" not in store.list_command_aliases().get("tracker.manage", [])


def test_command_alias_seed_respects_deleted_legacy_top_level_aliases():
    from src.plugins.riven_sniper import command_meta
    store = Store(":memory:")
    store.kv_set("base_aliases_seeded", "1")

    command_meta.seed_base_aliases(store)

    aliases = store.list_command_aliases()
    assert "blacklist.add" not in aliases
    assert "tracker.manage" not in aliases
    assert aliases == {
        "tracking.open.riven": ["rh", "riven"],
        "bargain.riven.list": ["rdl"],
        "bargain.riven.delete": ["rdd"],
        "tracker.manage.list": ["tl"],
        "tracker.manage.delete": ["td"],
    }
    store.close()


# ---- 命令文案 ----

def test_usage_texts_use_english_aliases_after_chinese_trigger_rename(client, monkeypatch):
    for node in COMMAND_NODES:
        monkeypatch.setitem(ACTIVE_ALIASES, node.id, list(node.default_aliases))
    monkeypatch.setitem(ACTIVE_COMMAND_NAMES, "tracking.open", "查玩家")
    monkeypatch.setitem(ACTIVE_COMMAND_NAMES, "tracking.open.riven", "查紫卡")
    monkeypatch.setitem(ACTIVE_COMMAND_NAMES, "tracker.manage", "提醒添加")
    monkeypatch.setitem(ACTIVE_COMMAND_NAMES, "tracker.manage.list", "提醒清单")
    monkeypatch.setitem(ACTIVE_COMMAND_NAMES, "tracker.manage.delete", "提醒移除")
    monkeypatch.setitem(ACTIVE_COMMAND_NAMES, "bargain.riven", "紫卡监控")
    monkeypatch.setitem(ACTIVE_COMMAND_NAMES, "bargain.riven.list", "紫卡清单")
    monkeypatch.setitem(ACTIVE_COMMAND_NAMES, "bargain.riven.delete", "紫卡移除")

    assert "w SomePlayer" in texts.render("开盒.用法")
    assert "rh 3" in texts.render("开盒.用法")
    tracker_usage = texts.render("上线提醒.用法", locale="en")
    assert "t SomePlayer" in tracker_usage
    assert "\ntl\n" in tracker_usage and "td 3" in tracker_usage
    bargain_usage = texts.render("捡漏紫卡.用法")
    assert "rd Torid 30" in bargain_usage
    assert "\nrdl\n" in bargain_usage and "rdd 3" in bargain_usage


def test_reply_text_api_is_removed(client):
    c, _ = client
    assert c.get("/admin/api/texts").status_code == 404
    assert c.put("/admin/api/texts/黑名单添加.成功",
                 json={"text": "{name} 已更新"}).status_code == 404
    assert c.delete("/admin/api/texts/黑名单添加.成功").status_code == 404


# ---- 固定卡图样式 ----

def test_legacy_card_style_webui_is_removed(client):
    c, _ = client
    assert c.get("/admin/api/card-style").status_code == 404
    assert c.put("/admin/api/card-style", json={"style": {}}).status_code == 404
    assert c.post("/admin/api/card-style/preview",
                  json={"style": {}}).status_code == 404

    admin_html = (ROOT / "src/plugins/riven_sniper/webui/static/index.html").read_text(
        encoding="utf-8")
    assert "cardstyle" not in admin_html and "card-style" not in admin_html


# ---- 测试推送队列格式 ----

def test_test_push_enqueues_structured_delivery(client, monkeypatch):
    import asyncio
    c, store = client

    class FakePoller:
        def __init__(self):
            self.queue = asyncio.Queue()

        @staticmethod
        def new_delivery(source, target, payload):
            now = time.time()
            return DeliveryItem(source, target, payload, 0, now, now + 60)

        def enqueue_delivery(self, item):
            self.queue.put_nowait(item)
            return True

    fp = FakePoller()
    monkeypatch.setattr(shared, "_poller", fp)
    r = c.post("/admin/api/system/test-push", json={"group_id": 111})
    assert r.status_code == 200 and r.json()["queue_depth"] == 1
    item = fp.queue.get_nowait()
    assert item.target == 111 and item.attempts == 0
    assert isinstance(item.enqueued_at, float)
    assert "测试推送" in item.payload

    discord = store.upsert_discord_target("1001")
    store.set_target_locale(discord["scope_id"], "en")
    shared._config.discord_dm_enabled = True
    r = c.post("/admin/api/system/test-push",
               json={"scope_id": discord["scope_id"]})
    assert r.status_code == 200
    item = fp.queue.get_nowait()
    assert item.target == discord["scope_id"] and item.attempts == 0
    assert "RivenSniper-QQ" not in item.payload
    assert "test notification" in item.payload and "测试推送" not in item.payload
