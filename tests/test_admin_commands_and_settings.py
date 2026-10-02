"""管理端全局命令开关、调用统计、词库与设置测试。"""

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

from src.plugins.riven_sniper import envutil, rivendata, shared  # noqa: E402
from src.plugins.riven_sniper.criteria import ANY_ATTRIBUTE  # noqa: E402
from src.plugins.riven_sniper.store import Store  # noqa: E402
from src.plugins.riven_sniper.webui import api as webui_api  # noqa: E402


# ---- store：命令开关与调用统计 ----

def test_command_switch_is_global_only():
    s = Store(":memory:")
    assert not s.is_command_disabled("tracking.open")
    s.disable_command("tracking.open")
    assert s.is_command_disabled("tracking.open")
    assert s.enable_command("tracking.open")
    assert not s.enable_command("tracking.open")
    s.close()


def test_command_usage_7d():
    s = Store(":memory:")
    for _ in range(3):
        s.record_command_usage("sniper.add")
    s.record_command_usage("tracking.open")
    usage = s.command_usage_7d()
    assert usage["sniper.add"] == 3 and usage["tracking.open"] == 1
    s.close()


# ---- envutil ----

def test_update_env_file_preserves_comments(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# 注释保留\nSNIPER_POLL_INTERVAL=15\nOTHER=x\n", encoding="utf-8")
    envutil.update_env_file(
        {"SNIPER_POLL_INTERVAL": "10", "NEW_KEY": "[1,2]"}, path=p)
    lines = p.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# 注释保留"
    assert "SNIPER_POLL_INTERVAL=10" in lines
    assert "OTHER=x" in lines
    assert "NEW_KEY=[1,2]" in lines


# ---- rivendata 词库 CRUD（隔离数据目录）----

@pytest.fixture()
def tmp_data(tmp_path, monkeypatch):
    for f in ("weapons.json", "attributes.json", "riven_values.json", "aliases.json"):
        shutil.copy(ROOT / "data" / f, tmp_path / f)
    monkeypatch.setattr(rivendata, "DATA_DIR", tmp_path)
    rivendata.invalidate_caches()
    yield tmp_path
    rivendata.invalidate_caches()  # 清掉指向 tmp 的缓存，避免污染其他测试


def test_attribute_alias_crud(tmp_data):
    rivendata.add_attribute_alias("测试别名", "critical_chance", show_in_list=True)
    assert rivendata.resolve_attribute("测试别名") == "critical_chance"
    data = json.loads((tmp_data / "aliases.json").read_text(encoding="utf-8"))
    assert data["attributes"]["测试别名"] == "critical_chance"
    assert "测试别名" in data["attribute_display"]["critical_chance"]

    assert rivendata.remove_attribute_alias("测试别名")
    assert rivendata.resolve_attribute("测试别名") is None
    assert not rivendata.remove_attribute_alias("测试别名")


def test_any_attribute_alias_crud(tmp_data):
    rivendata.add_attribute_alias("随便", ANY_ATTRIBUTE, show_in_list=True)

    assert rivendata.resolve_attribute("随便") == ANY_ATTRIBUTE
    data = json.loads((tmp_data / "aliases.json").read_text(encoding="utf-8"))
    assert data["attributes"]["随便"] == ANY_ATTRIBUTE
    assert "随便" in data["attribute_display"][ANY_ATTRIBUTE]


def test_attribute_alias_validation(tmp_data):
    with pytest.raises(ValueError):
        rivendata.add_attribute_alias("x", "not_a_slug")
    with pytest.raises(ValueError):  # 已有别名映射到其他词条
        rivendata.add_attribute_alias("暴击", "critical_damage")
    with pytest.raises(ValueError):  # 标准词条名不能作为别名
        rivendata.add_attribute_alias("multishot", "critical_chance")
    with pytest.raises(ValueError):  # 特殊词条标准名不能改指向真实词条
        rivendata.add_attribute_alias("任意", "critical_chance")


def test_weapon_alias_crud(tmp_data):
    rivendata.add_weapon_alias("绝路p", "rubico")
    assert rivendata.resolve_weapon("绝路p") == "rubico"
    with pytest.raises(ValueError):
        rivendata.add_weapon_alias("x", "not_a_weapon")
    assert rivendata.remove_weapon_alias("绝路p")
    assert rivendata.resolve_weapon("绝路p") is None


# ---- WebUI P1 端点 ----

@pytest.fixture()
def client(monkeypatch, tmp_data, tmp_path):
    store = Store(":memory:")
    store.upsert_qq_target(111, 999, enabled=True)
    cfg = types.SimpleNamespace(
        sniper_poll_interval=15.0, sniper_dry_run=True,
        sniper_max_configs_per_group=20, sniper_send_interval=1.5,
        sniper_send_concurrency=0, send_queue_maxsize=1000,
        trade_message_ttl_seconds=60,
        bargain_max_distinct_slugs=30,
        discord_dm_enabled=False)
    monkeypatch.setattr(shared, "_store", store)
    monkeypatch.setattr(shared, "_config", cfg)
    monkeypatch.setattr(shared, "_poller", None)
    env = tmp_path / ".env"
    env.write_text("SNIPER_POLL_INTERVAL=15\n", encoding="utf-8")
    monkeypatch.setattr(envutil, "ENV_PATH", env)
    app = FastAPI()
    app.include_router(webui_api.router, prefix="/admin")
    with TestClient(app) as c:
        yield c, store, cfg, env
    store.close()


def test_commands_list_and_toggle(client):
    c, store, _, _ = client
    d = c.get("/admin/api/commands").json()
    names = {x["trigger_name"] for x in d["commands"]}
    assert {"狙击添加", "去重"} <= names and "命令大全" not in names
    assert {
        "捡漏紫卡列表", "捡漏紫卡删除", "开盒紫卡",
        "上线提醒列表", "上线提醒删除",
    } <= names
    assert all(item["mapping_name"] == item["id"] for item in d["commands"])
    dedupe = next(x for x in d["commands"] if x["id"] == "channel.dedupe")
    assert dedupe["description"] == "查看或设置当前目标的频道去重时间"
    assert all("permission" not in item for item in d["commands"])
    removed = {
        "blacklist.list.add",
        "blacklist.list.delete",
        "bargain.riven.help",
        "bargain.riven.enable",
        "bargain.riven.disable",
        "bargain.riven.toggle",
    }
    assert removed.isdisjoint(x["id"] for x in d["commands"])

    # 仅保留全局紧急停用。
    assert c.patch("/admin/api/commands/tracking.open",
                   json={"enabled": False}).status_code == 200
    assert store.is_command_disabled("tracking.open")
    d = c.get("/admin/api/commands").json()
    kb = next(x for x in d["commands"] if x["id"] == "tracking.open")
    assert not kb["enabled_global"]
    assert "disabled_scopes" not in kb

    assert c.patch("/admin/api/commands/retired.help",
                   json={"enabled": False}).status_code == 404
    assert c.patch("/admin/api/commands/not-found",
                   json={"enabled": False}).status_code == 404
    for command_id in removed:
        assert c.patch(
            f"/admin/api/commands/{command_id}",
            json={"enabled": False},
        ).status_code == 404

    assert c.post("/admin/api/commands/tracking.open/permission",
                  json={"level": "manager"}).status_code == 404


def test_aliases_endpoints(client):
    c, _, _, _ = client
    d = c.get("/admin/api/aliases").json()
    assert len(d["attributes"]) == len(rivendata.attributes()) + 1
    assert len(d["weapons_index"]) > 400
    any_attribute = next(
        item for item in d["attributes"] if item["slug"] == ANY_ATTRIBUTE
    )
    assert any_attribute["name_zh"] == "任意"
    assert any_attribute["name_en"] == "Any"
    assert any_attribute["builtin_aliases"] == ["any"]
    combo = next(
        item for item in d["attributes"]
        if item["slug"] == "chance_to_gain_combo_count"
    )
    assert combo["name_zh"] == "连击数获取几率"
    assert combo["builtin_aliases"] == []
    assert {"cgc", "连击获取"} <= set(combo["aliases"])
    assert {"cgc", "连击获取"} <= set(combo["display"])

    r = c.post("/admin/api/aliases/attribute",
               json={"alias": "面板别名", "slug": "critical_chance", "show_in_list": True})
    assert r.status_code == 200
    d = c.get("/admin/api/aliases").json()
    cc = next(a for a in d["attributes"] if a["slug"] == "critical_chance")
    assert "面板别名" in cc["aliases"] and "面板别名" in cc["display"]

    r = c.post("/admin/api/aliases/attribute", json={
        "alias": "任意别名",
        "slug": ANY_ATTRIBUTE,
        "show_in_list": True,
    })
    assert r.status_code == 200
    d = c.get("/admin/api/aliases").json()
    any_attribute = next(
        item for item in d["attributes"] if item["slug"] == ANY_ATTRIBUTE
    )
    assert "任意别名" in any_attribute["aliases"]
    assert "任意别名" in any_attribute["display"]
    preview = c.post(
        "/admin/api/parse-preview",
        json={"text": "托里德 任意别名@A 暴击"},
    ).json()
    assert preview["ok"] and "any@A" in preview["message"]

    # 冲突与无效
    assert c.post("/admin/api/aliases/attribute",
                  json={"alias": "面板别名", "slug": "critical_damage"}).status_code == 400
    assert c.post("/admin/api/aliases/attribute",
                  json={"alias": "y", "slug": "bad_slug"}).status_code == 400

    assert c.delete("/admin/api/aliases/attribute/面板别名").status_code == 200
    assert c.delete("/admin/api/aliases/attribute/面板别名").status_code == 404
    assert c.delete("/admin/api/aliases/attribute/任意别名").status_code == 200

    assert c.post("/admin/api/aliases/weapon",
                  json={"alias": "绝路p", "slug": "rubico"}).status_code == 200
    assert c.get("/admin/api/aliases").json()["weapon_aliases"]["绝路p"] == "rubico"
    assert c.delete("/admin/api/aliases/weapon/绝路p").status_code == 200


def test_member_permission_endpoints_are_removed(client):
    c, _, _, _ = client
    assert c.get("/admin/api/groups/111/members").status_code == 404
    assert c.patch("/admin/api/groups/111/members/2/permission",
                   json={"level": "manager"}).status_code == 404


def test_settings_hot_update(client):
    c, _, cfg, env = client
    d = c.get("/admin/api/settings").json()
    assert d["hot"]["sniper_poll_interval"] == 15.0
    assert d["hot"]["sniper_send_concurrency"] == 0
    assert d["hot"]["trade_message_ttl_seconds"] == 60
    assert d["readonly"]["irc_riven_base_dedupe_hours"] == 1

    class FakePoller:
        notifications = 0
        poll_now_requests = 0

        async def notify_runtime_settings_changed(self, *, poll_now=False):
            self.notifications += 1
            self.poll_now_requests += int(poll_now)

    fake_poller = FakePoller()
    shared._poller = fake_poller

    r = c.patch("/admin/api/settings", json={
        "sniper_poll_interval": 1,
        "sniper_send_interval": 0,
        "sniper_send_concurrency": 3,
        "trade_message_ttl_seconds": 17.5,
        "sniper_max_configs_per_group": 0,
        "bargain_max_distinct_slugs": 0,
    })
    assert r.status_code == 200
    # 内存即时生效
    assert cfg.sniper_poll_interval == 1
    assert cfg.sniper_send_interval == 0
    assert cfg.sniper_send_concurrency == 3
    assert cfg.trade_message_ttl_seconds == 17.5
    assert cfg.sniper_max_configs_per_group == 0
    assert cfg.bargain_max_distinct_slugs == 0
    assert fake_poller.notifications == 1
    assert fake_poller.poll_now_requests == 0
    # 写回 .env
    text = env.read_text(encoding="utf-8")
    assert "SNIPER_POLL_INTERVAL=1.0" in text
    assert "SNIPER_SEND_INTERVAL=0.0" in text
    assert "SNIPER_SEND_CONCURRENCY=3" in text
    assert "TRADE_MESSAGE_TTL_SECONDS=17.5" in text
    assert "SNIPER_MAX_CONFIGS_PER_GROUP=0" in text
    assert "BARGAIN_MAX_DISTINCT_SLUGS=0" in text

    # 后续字段校验失败时，前面的并发设置不能部分热生效或写入 .env。
    text_before_invalid = env.read_text(encoding="utf-8")
    assert c.patch("/admin/api/settings", json={
        "sniper_send_concurrency": 4,
        "sniper_max_configs_per_group": -1,
    }).status_code == 400
    assert cfg.sniper_send_concurrency == 3
    assert fake_poller.notifications == 1
    assert env.read_text(encoding="utf-8") == text_before_invalid

    assert c.patch("/admin/api/settings", json={
        "sniper_poll_interval": 3600}).status_code == 200
    assert fake_poller.notifications == 2
    assert fake_poller.poll_now_requests == 0

    # 没有现实保护作用的容量上限不拒绝，只保留有运行语义的下界。
    assert c.patch("/admin/api/settings", json={
        "sniper_send_concurrency": 101,
        "sniper_max_configs_per_group": 100_000,
        "bargain_max_distinct_slugs": 201,
    }).status_code == 200
    assert cfg.sniper_send_concurrency == 101
    assert cfg.sniper_max_configs_per_group == 100_000
    assert cfg.bargain_max_distinct_slugs == 201

    # 仍需拒绝会破坏轮询、队列或 TTL 语义的值。
    assert c.patch("/admin/api/settings",
                   json={"sniper_poll_interval": 0}).status_code == 400
    assert c.patch("/admin/api/settings",
                   json={"sniper_poll_interval": 3601}).status_code == 400
    assert c.patch("/admin/api/settings",
                   json={"sniper_send_interval": -0.1}).status_code == 400
    assert c.patch("/admin/api/settings", json={
        "sniper_send_concurrency": -1}).status_code == 400
    assert c.patch("/admin/api/settings", json={
        "trade_message_ttl_seconds": 0}).status_code == 400
    assert c.patch("/admin/api/settings", json={
        "sniper_max_configs_per_group": -1}).status_code == 400
    assert c.patch("/admin/api/settings", json={
        "bargain_max_distinct_slugs": -1}).status_code == 400
    assert c.patch("/admin/api/settings", json={
        "irc_riven_dedupe_seconds": 0}).status_code == 400
    assert c.patch("/admin/api/settings", json={}).status_code == 400
