"""管理端配置、命令别名、黑名单与系统操作测试。"""

import asyncio
import shutil
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.plugins.riven_sniper import configops, envutil, rivendata, shared  # noqa: E402
from src.plugins.riven_sniper.config import Config  # noqa: E402
from src.plugins.riven_sniper.parsing import parse_add  # noqa: E402
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


# ---- configops ----


def test_limit_defaults_and_validation():
    cfg = Config()
    assert cfg.sniper_send_interval == 0
    assert "webui_password" not in Config.model_fields
    assert cfg.send_queue_maxsize == 1000
    assert cfg.trade_message_ttl_seconds == 60
    assert cfg.send_max_retries == 1
    assert cfg.send_retry_delay_seconds == 2
    assert cfg.irc_feed_interval == 0.2
    assert Config(trade_message_ttl_seconds=17.5).trade_message_ttl_seconds == 17.5
    with pytest.raises(ValueError):
        Config(trade_message_ttl_seconds=0)
    with pytest.raises(ValueError):
        Config(send_queue_maxsize=0)
    assert cfg.sniper_max_configs_per_group == 0
    assert cfg.bargain_max_distinct_slugs == 0
    assert Config(bargain_max_distinct_slugs=8).bargain_max_distinct_slugs == 8
    assert Config(sniper_max_configs_per_group=100_000).sniper_max_configs_per_group == 100_000
    assert Config(bargain_max_distinct_slugs=201).bargain_max_distinct_slugs == 201
    assert Config(sniper_send_concurrency=101).sniper_send_concurrency == 101


@pytest.mark.parametrize(("field", "value"), [
    ("sniper_poll_interval", 0),
    ("sniper_poll_interval", 3601),
    ("sniper_send_interval", -0.1),
    ("sniper_send_interval", 60.1),
    ("sniper_max_configs_per_group", -1),
    ("bargain_max_distinct_slugs", -1),
    ("irc_feed_interval", 0),
    ("irc_feed_stale_seconds", 0),
])
def test_env_config_rejects_runtime_values_outside_supported_ranges(
        field, value):
    with pytest.raises(ValueError):
        Config(**{field: value})


def test_add_config_checked_and_duplicate():
    s = Store(":memory:")
    cfg = _cfg_ns()
    ok, msg, cid = configops.add_config_checked(
        s, cfg, 111, "托里德 +暴击 +多重")
    assert ok and cid == 1 and "Torid" in msg
    command = next(line for line in msg.splitlines()
                   if line.startswith("s "))
    assert parse_add(command.removeprefix("s ")) == parse_add(
        "托里德 暴击 多重")
    # + 只是空格的输入别名，两种写法规范化后必须判为同一配置。
    ok2, msg2, cid2 = configops.add_config_checked(
        s, cfg, 111, "托里德 暴击 多重")
    assert not ok2 and cid2 is None and "配置已存在" in msg2
    ok3, msg3, _ = configops.add_config_checked(
        s, cfg, 111, "不存在武器 暴击 多重")
    assert not ok3 and "添加失败" in msg3
    s.close()


def test_add_config_zero_cap_means_unlimited():
    s = Store(":memory:")
    cfg = _cfg_ns(sniper_max_configs_per_group=0)
    for text in (
            "torid 暴击 多重",
            "nami_solo 暴击 多重",
            "boltor 暴击 多重"):
        assert configops.add_config_checked(s, cfg, 111, text)[0]
    assert len(s.list_configs(111)) == 3
    s.close()


def test_removed_at_creator_syntax_is_rejected():
    s = Store(":memory:")
    cfg = _cfg_ns()
    ok, msg, config_id = configops.add_config_checked(
        s, cfg, 111, "托里德 暴击 多重 @我")
    assert not ok and config_id is None and "@我" in msg
    assert s.list_configs(111) == []
    s.close()


def test_config_dedup_no_longer_has_a_creator_delivery_domain():
    s = Store(":memory:")
    cfg = _cfg_ns()
    text = "托里德 暴击 多重"
    assert configops.add_config_checked(s, cfg, 111, text)[0]
    ok, msg, _ = configops.add_config_checked(
        s, cfg, 111, "托里德 +暴击 +多重")
    assert not ok and "配置已存在" in msg
    assert len(s.list_configs(111)) == 1
    s.close()


def test_preview_config():
    original = parse_add("托里德 +暴击/多重 +射速 -任意 0洗")
    ok, command = configops.preview_config(
        "托里德 +暴击/多重 +射速 -任意 0洗")
    assert ok and command.startswith("s Torid ")
    assert "+" not in command and "未洗" not in command
    reparsed = parse_add(command.removeprefix("s "))
    assert reparsed == original

    ok2, msg2 = configops.preview_config("托里德 暴击 多重 2+1")
    assert not ok2 and "已删除 2+1/2-1" in msg2


def test_default_config_preview_uses_short_english_command():
    ok, command = configops.preview_config("托里德 暴击 多重")
    assert ok and command == "s Torid cc ms"


def test_covered_config_reports_redundancy():
    s = Store(":memory:")
    cfg = _cfg_ns()
    assert configops.add_config_checked(
        s, cfg, 111, "步枪 任意 任意")[0]
    ok, msg, _ = configops.add_config_checked(
        s, cfg, 111, "托里德 暴击 多重")
    assert not ok
    assert "现有配置已覆盖这些条件" in msg and "重复推送" not in msg
    command = next(line for line in msg.splitlines()
                   if line.startswith("s "))
    assert parse_add(command.removeprefix("s "))
    s.close()


# ---- store：命令触发名、别名与备份 ----

def test_command_aliases_store_uses_fixed_ids():
    s = Store(":memory:")
    assert s.add_command_alias("查人", "tracking.open")
    assert s.list_command_aliases() == {"tracking.open": ["查人"]}
    assert not s.add_command_alias("狙击复制", "tracking.open")
    assert not s.add_command_alias("无效", "not-found")
    s.close()


def test_unknown_old_schema_is_left_unchanged(tmp_path):
    """未知旧库不再清空，拒绝启动并保持原数据。"""
    import sqlite3
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.executescript("""
        CREATE TABLE configs (id INTEGER PRIMARY KEY, group_id INTEGER,
            creator_qq INTEGER, weapon TEXT, wildcard TEXT,
            positives TEXT DEFAULT '[]', negatives_mode TEXT DEFAULT 'any',
            negatives TEXT DEFAULT '[]', threshold INTEGER, reroll_min INTEGER,
            reroll_max INTEGER, polarity TEXT, buyout_only INTEGER DEFAULT 0,
            at_creator INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 1, created_at INTEGER);
        CREATE TABLE bargain_items (id INTEGER PRIMARY KEY, group_id INTEGER,
            creator_qq INTEGER, slug TEXT, threshold REAL,
            enabled INTEGER DEFAULT 1, created_at INTEGER, UNIQUE(group_id, slug));
        CREATE TABLE global_whitelist (qq INTEGER PRIMARY KEY, expires_at INTEGER,
            granted_by INTEGER, created_at INTEGER);
        CREATE TABLE wfm_users (user_id TEXT, ingame_name TEXT);
        INSERT INTO configs
            (id,group_id,creator_qq,weapon,positives,negatives,at_creator,created_at)
            VALUES
            (1,1,42,'torid','[["critical_chance"],["multishot"]]','[]',0,1),
            (2,1,43,'torid','[["critical_chance"],["multishot"]]','[]',1,2);
        INSERT INTO global_whitelist VALUES (42,NULL,1,1);
        INSERT INTO wfm_users VALUES ('legacy','LegacyName');
    """)
    c.commit()
    c.close()
    before = db.read_bytes()
    with pytest.raises(RuntimeError, match="数据库未被修改"):
        Store(db)
    assert db.read_bytes() == before


def test_backup_to(tmp_path):
    s = Store(":memory:")
    s.add_blacklist(1, "Seller")
    out = tmp_path / "bk.db"
    s.backup_to(str(out))
    assert out.read_bytes()[:16] == b"SQLite format 3\x00"
    import sqlite3
    c = sqlite3.connect(out)
    assert c.execute("SELECT COUNT(*) FROM blacklist").fetchone()[0] == 1
    c.close()
    s.close()


# ---- rivendata 别名修改（隔离数据目录）----

@pytest.fixture()
def tmp_data(tmp_path, monkeypatch):
    for f in ("weapons.json", "attributes.json", "riven_values.json", "aliases.json"):
        shutil.copy(ROOT / "data" / f, tmp_path / f)
    monkeypatch.setattr(rivendata, "DATA_DIR", tmp_path)
    rivendata.invalidate_caches()
    yield tmp_path
    rivendata.invalidate_caches()


def test_update_attribute_alias(tmp_data):
    rivendata.add_attribute_alias("旧名", "critical_chance", show_in_list=True)
    # 改名保留展示状态
    rivendata.update_attribute_alias("旧名", new_alias="新名")
    assert rivendata.resolve_attribute("新名") == "critical_chance"
    assert rivendata.resolve_attribute("旧名") is None
    al = rivendata.aliases()
    assert "新名" in al["attribute_display"]["critical_chance"]
    # 切换展示
    rivendata.update_attribute_alias("新名", show_in_list=False)
    assert "新名" not in rivendata.aliases()["attribute_display"]["critical_chance"]
    # 校验
    with pytest.raises(KeyError):
        rivendata.update_attribute_alias("不存在的")
    with pytest.raises(ValueError):
        rivendata.update_attribute_alias("新名", new_alias="爆伤")  # 已映射到其他词条
    with pytest.raises(ValueError):
        rivendata.update_attribute_alias("新名", new_alias="multishot")  # 标准词条名


def test_update_attribute_alias_same_slug_merge(tmp_data):
    """改名撞上同 slug 的既有别名：等价合并，不应报错。"""
    rivendata.add_attribute_alias("临时名", "critical_chance")
    rivendata.update_attribute_alias("临时名", new_alias="cc")  # cc 已存在且同 slug
    assert rivendata.resolve_attribute("cc") == "critical_chance"
    assert rivendata.resolve_attribute("临时名") is None


def test_update_weapon_alias(tmp_data):
    rivendata.add_weapon_alias("绝路p", "rubico")
    rivendata.update_weapon_alias("绝路p", new_alias="大绝路")
    assert rivendata.resolve_weapon("大绝路") == "rubico"
    rivendata.update_weapon_alias("大绝路", slug="torid")
    assert rivendata.resolve_weapon("大绝路") == "torid"
    with pytest.raises(KeyError):
        rivendata.update_weapon_alias("不存在")
    with pytest.raises(ValueError):
        rivendata.update_weapon_alias("大绝路", slug="bad_slug")
    rivendata.add_weapon_alias("另一个", "rubico")
    with pytest.raises(ValueError):
        rivendata.update_weapon_alias("另一个", new_alias="大绝路")  # 撞名


def test_wildcard_alias_crud(tmp_data):
    rivendata.add_wildcard_alias("大喷子", "shotgun")
    assert rivendata.resolve_wildcard("大喷子") == "shotgun"
    rivendata.update_wildcard_alias("大喷子", new_alias="喷喷")
    assert rivendata.resolve_wildcard("喷喷") == "shotgun"
    rivendata.update_wildcard_alias("喷喷", category="rifle")
    assert rivendata.resolve_wildcard("喷喷") == "rifle"
    assert rivendata.remove_wildcard_alias("喷喷") is True
    assert rivendata.resolve_wildcard("喷喷") is None
    assert rivendata.remove_wildcard_alias("喷喷") is False
    with pytest.raises(ValueError):
        rivendata.add_wildcard_alias("x", "not_a_category")
    with pytest.raises(KeyError):
        rivendata.update_wildcard_alias("不存在")
    rivendata.add_wildcard_alias("a1", "all")
    rivendata.add_wildcard_alias("a2", "all")
    with pytest.raises(ValueError):
        rivendata.update_wildcard_alias("a2", new_alias="a1")  # 撞名


# ---- WebUI P2 端点 ----

@pytest.fixture()
def client(monkeypatch, tmp_data, tmp_path):
    store = Store(":memory:")
    store.upsert_qq_target(111, 999, enabled=True)
    cfg = _cfg_ns()
    monkeypatch.setattr(shared, "_store", store)
    monkeypatch.setattr(shared, "_config", cfg)
    monkeypatch.setattr(shared, "_poller", None)
    env = tmp_path / ".env"
    env.write_text("X=1\n", encoding="utf-8")
    monkeypatch.setattr(envutil, "ENV_PATH", env)
    app = FastAPI()
    app.include_router(webui_api.router, prefix="/admin")
    with TestClient(app) as c:
        yield c, store, cfg
    store.close()


def test_command_name_api_uses_fixed_mapping_name(client):
    c, store, _ = client
    assert c.patch(
        "/admin/api/commands/tracking.open/name",
        json={"name": "查人"},
    ).status_code == 200
    item = next(
        row for row in c.get("/admin/api/commands").json()["commands"]
        if row["id"] == "tracking.open"
    )
    assert item["mapping_name"] == "tracking.open"
    assert item["trigger_name"] == "查人"
    assert store.list_command_names()["tracking.open"] == "查人"
    assert "standard_name" not in item and "standard_path" not in item


def test_alias_patch_endpoints(client):
    c, _, _ = client
    c.post("/admin/api/aliases/attribute",
           json={"alias": "别名甲", "slug": "critical_chance", "show_in_list": False})
    r = c.patch("/admin/api/aliases/attribute/别名甲",
                json={"new_alias": "别名乙", "show_in_list": True})
    assert r.status_code == 200
    d = c.get("/admin/api/aliases").json()
    cc = next(a for a in d["attributes"] if a["slug"] == "critical_chance")
    assert "别名乙" in cc["aliases"] and "别名乙" in cc["display"]
    assert c.patch("/admin/api/aliases/attribute/不存在",
                   json={"new_alias": "x"}).status_code == 404
    c.post("/admin/api/aliases/attribute",
           json={"alias": "zshow", "slug": "critical_chance", "show_in_list": True})
    c.post("/admin/api/aliases/attribute",
           json={"alias": "ahide", "slug": "critical_chance", "show_in_list": False})
    cc = next(a for a in c.get("/admin/api/aliases").json()["attributes"]
              if a["slug"] == "critical_chance")
    assert cc["aliases"].index("zshow") < cc["aliases"].index("ahide")

    c.post("/admin/api/aliases/weapon", json={"alias": "wa1", "slug": "rubico"})
    assert c.patch("/admin/api/aliases/weapon/wa1",
                   json={"new_alias": "wa2", "slug": "torid"}).status_code == 200
    d = c.get("/admin/api/aliases").json()
    assert d["weapon_aliases"]["wa2"] == "torid"
    torid = next(w for w in d["weapons_index"] if w["slug"] == "torid")
    assert torid["group"] == "primary"
    assert "wa2" in torid["aliases"]
    assert c.patch("/admin/api/aliases/weapon/wa1",
                   json={"new_alias": "x"}).status_code == 404


def test_category_alias_endpoints(client):
    c, _, _ = client
    d = c.get("/admin/api/aliases").json()
    assert d["wildcard_categories"]["all"] == "全部武器"
    # 通配符：增 / 改类别 / 删
    assert c.post("/admin/api/aliases/category/wildcard",
                  json={"alias": "巨喷", "category": "shotgun"}).status_code == 200
    assert c.get("/admin/api/aliases").json()["wildcards"]["巨喷"] == "shotgun"
    assert c.patch("/admin/api/aliases/category/wildcard/巨喷",
                   json={"category": "rifle"}).status_code == 200
    assert c.get("/admin/api/aliases").json()["wildcards"]["巨喷"] == "rifle"
    assert c.delete("/admin/api/aliases/category/wildcard/巨喷").status_code == 200
    assert c.delete("/admin/api/aliases/category/wildcard/巨喷").status_code == 404
    # 非法类别 400，已移除的极性别名端点和未知类别端点均为 404
    assert c.post("/admin/api/aliases/category/wildcard",
                  json={"alias": "z", "category": "nope"}).status_code == 400
    assert c.post("/admin/api/aliases/category/polarity",
                  json={"alias": "z", "category": "madurai"}).status_code == 404
    assert c.post("/admin/api/aliases/category/nonsense",
                  json={"alias": "z", "category": "all"}).status_code == 404


def test_blacklist_api(client):
    c, store, _ = client
    assert c.post("/admin/api/groups/111/blacklist",
                  json={"seller": "BadGuy"}).status_code == 200
    assert store.is_blacklisted(111, "BadGuy")
    assert c.post("/admin/api/groups/111/blacklist",
                  json={"seller": "BadGuy"}).status_code == 400
    assert c.post("/admin/api/groups/111/blacklist", json={
        "seller": "BadGuy", "scope": "channel"}).status_code == 200
    assert store.is_blacklisted(111, "BadGuy", scope="wm")
    assert store.is_blacklisted(111, "BadGuy", scope="channel")
    assert c.post("/admin/api/groups/111/blacklist",
                  json={"seller": "  "}).status_code == 400
    detail = c.get("/admin/api/groups/111/configs").json()
    assert detail["blacklists"] == {
        "wm": ["BadGuy"], "channel": ["BadGuy"]}
    assert c.delete(
        "/admin/api/groups/111/blacklist/BadGuy?scope=channel"
    ).status_code == 200
    assert store.is_blacklisted(111, "BadGuy", scope="wm")
    assert not store.is_blacklisted(111, "BadGuy", scope="channel")
    assert c.delete("/admin/api/groups/111/blacklist/BadGuy").status_code == 200
    assert c.delete("/admin/api/groups/111/blacklist/BadGuy").status_code == 404


def test_blacklist_api_accepts_multiline_all_and_validates_before_writing(
        client):
    c, store, _ = client
    response = c.post("/admin/api/groups/111/blacklist", json={
        "seller": "Alpha\nExample\u00a0o\n\nAlpha",
        "scope": "all",
    })
    assert response.status_code == 200
    assert response.json() == {"ok": True, "added": 4, "existing": 2}
    assert store.list_blacklist(111, scope="wm") == ["Alpha", "Example o"]
    assert store.list_blacklist(111, scope="channel") == [
        "Alpha", "Example o"]

    response = c.post("/admin/api/groups/111/blacklist", json={
        "seller": f"ValidSeller\n{'x' * 33}",
        "scope": "wm",
    })
    assert response.status_code == 400
    assert "第 2 行" in response.json()["detail"]
    assert not store.is_blacklisted(111, "ValidSeller", scope="wm")

    assert c.post("/admin/api/groups/111/blacklist", json={
        "seller": "y" * 32,
        "scope": "wm",
    }).status_code == 200


def test_removed_whitelist_api_is_absent(client):
    c, _, _ = client
    assert c.get("/admin/api/whitelist").status_code == 404
    assert c.post("/admin/api/whitelist", json={"qq": 1}).status_code == 404


def test_parse_preview_and_config_create(client):
    c, store, _ = client
    r = c.post(
        "/admin/api/parse-preview",
        json={"text": "托里德 +暴击/多重 +射速 -任意 0洗"},
    )
    preview = r.json()
    assert preview["ok"] and preview["message"].startswith("s Torid ")
    assert "+" not in preview["message"]
    r = c.post("/admin/api/parse-preview", json={"text": "瞎写的"})
    assert not r.json()["ok"]

    # @我 已从所有入口删除，预览和入库都必须走普通解析错误。
    at_text = "托里德 暴击 多重 @我"
    r = c.post("/admin/api/parse-preview", json={"text": at_text})
    assert not r.json()["ok"] and "@我" in r.json()["message"]
    r = c.post("/admin/api/groups/111/configs", json={"text": at_text})
    assert r.status_code == 400 and "@我" in r.json()["detail"]

    text = "托里德 +暴击 +多重"
    r = c.post("/admin/api/groups/111/configs", json={"text": text})
    assert r.status_code == 200
    created = r.json()
    cid = created["id"]
    assert created["display_number"] == 1
    assert created["command"].startswith("s Torid ")
    assert "+" not in created["command"]
    assert "creator_qq" not in store.get_config(cid, 111)
    assert c.post("/admin/api/groups/111/configs",
                  json={"text": "托里德 暴击 多重"}).status_code == 400

    rated_text = "托里德 暴击率@A 射速@B+ -变焦@C+"
    preview = c.post(
        "/admin/api/parse-preview", json={"text": rated_text}).json()
    assert preview["ok"]
    assert "cc@A" in preview["message"]
    assert '@B+' in preview["message"] and "-z@C+" in preview["message"]
    rated = c.post(
        "/admin/api/groups/111/configs", json={"text": rated_text})
    assert rated.status_code == 200
    saved = store.get_config(rated.json()["id"], 111)
    assert saved["positive_ratings"] == [
        {"critical_chance": "A"},
        {"fire_rate_/_attack_speed": "B+"},
    ]
    assert saved["negative_ratings"] == [{"zoom": "C+"}]
    listed = c.get("/admin/api/groups/111/configs").json()["configs"]
    assert any(item["command"] == rated.json()["command"] for item in listed)


def test_system_endpoints(client, monkeypatch):
    c, _, _ = client
    assert c.post("/admin/api/system/poll-now").status_code == 503
    assert c.post("/admin/api/system/test-push",
                  json={"group_id": 111}).status_code == 503

    import asyncio

    class FakePoller:
        def __init__(self):
            self.queue = asyncio.Queue()
            self.polled = 0

        async def _poll_once(self):
            self.polled += 1

        @staticmethod
        def new_delivery(source, target, payload):
            return source, target, payload

        def enqueue_delivery(self, item):
            self.queue.put_nowait(item)
            return True

    fp = FakePoller()
    monkeypatch.setattr(shared, "_poller", fp)
    assert c.post("/admin/api/system/poll-now").status_code == 200
    assert fp.polled == 1
    r = c.post("/admin/api/system/test-push", json={"group_id": 111})
    assert r.status_code == 200 and r.json()["queue_depth"] == 1

    # 备份下载：SQLite 魔数
    r = c.get("/admin/api/system/backup")
    assert r.status_code == 200
    assert r.content[:16] == b"SQLite format 3\x00"

    # 数据刷新：mock 子进程，避免测试真实联网/覆盖 data
    called = []

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return b"weapons: 1  attributes: 1\n", None

        def kill(self):
            called.append("kill")

    async def fake_exec(*args, **kwargs):
        called.append(("exec", args))
        return FakeProc()

    monkeypatch.setattr(webui_api.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(webui_api.rivendata, "invalidate_caches",
                        lambda: called.append("invalidate"))
    r = c.post("/admin/api/system/refresh-data")
    assert r.status_code == 200
    assert r.json()["ok"]
    assert called[0][0] == "exec"
    assert "invalidate" in called


def test_restart_requests_uvicorn_graceful_shutdown(monkeypatch):
    signals = []
    monkeypatch.setattr(
        webui_api.signal, "raise_signal", signals.append)

    webui_api._request_graceful_shutdown()

    assert signals == [webui_api.signal.SIGINT]


@pytest.mark.asyncio
async def test_refresh_data_timeout_reaps_subprocess(monkeypatch):
    class FakeProc:
        returncode = None
        killed = False
        waited = False

        async def communicate(self):
            await asyncio.Event().wait()

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            self.waited = True
            return self.returncode

    proc = FakeProc()

    async def fake_exec(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(
        webui_api.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(webui_api, "_DATA_REFRESH_TIMEOUT_SECONDS", 0.001)

    with pytest.raises(HTTPException) as error:
        await webui_api.refresh_data()

    assert error.value.status_code == 504
    assert proc.killed and proc.waited


@pytest.mark.asyncio
async def test_refresh_data_cancellation_reaps_subprocess(monkeypatch):
    started = asyncio.Event()

    class FakeProc:
        returncode = None
        killed = False
        waited = False

        async def communicate(self):
            started.set()
            await asyncio.Event().wait()

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            self.waited = True
            return self.returncode

    proc = FakeProc()

    async def fake_exec(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(
        webui_api.asyncio, "create_subprocess_exec", fake_exec)
    task = asyncio.create_task(webui_api.refresh_data())
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert proc.killed and proc.waited
