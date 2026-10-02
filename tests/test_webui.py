"""WebUI API 测试（独立 FastAPI 应用 + 内存库，不依赖 NoneBot 运行时）。"""

import json
import re
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.plugins.riven_sniper import shared  # noqa: E402
from src.plugins.riven_sniper.chat_tracking import TrackingStore  # noqa: E402
from src.plugins.riven_sniper.store import Store  # noqa: E402
from src.plugins.riven_sniper.webui import mount  # noqa: E402
from src.plugins.riven_sniper.webui.api import router  # noqa: E402

ADMIN_HTML = (Path(__file__).resolve().parents[1] /
              "src/plugins/riven_sniper/webui/static/index.html").read_text(
                  encoding="utf-8")


@pytest.fixture()
def client(monkeypatch, tmp_path):
    store = Store(":memory:")
    store.upsert_qq_target(111, 999, enabled=True)
    cfg = types.SimpleNamespace(
        sniper_poll_interval=15.0, sniper_dry_run=True,
        sniper_max_configs_per_group=20, sniper_send_interval=1.5,
        sniper_send_concurrency=0, trade_message_ttl_seconds=60,
        discord_dm_enabled=False,
        bargain_max_distinct_slugs=30,
        irc_track_db_path=str(tmp_path / "tracking.db"))
    monkeypatch.setattr(shared, "_store", store)
    monkeypatch.setattr(shared, "_config", cfg)
    monkeypatch.setattr(shared, "_poller", None)

    app = FastAPI()
    app.include_router(router, prefix="/admin")
    with TestClient(app) as c:
        yield c, store
    store.close()


def test_console_is_open_without_authentication(client):
    c, _ = client
    assert c.get("/admin/").status_code == 200
    assert c.get("/admin/api/status").status_code == 200
    assert c.get("/admin/api/targets").status_code == 200
    assert c.post("/admin/api/login", json={"password": "unused"}).status_code == 404
    assert c.post("/admin/api/logout").status_code == 404
    assert c.get("/admin/api/whitelist").status_code == 404
    assert 'id="login"' not in ADMIN_HTML
    assert "doLogin" not in ADMIN_HTML
    assert "doLogout" not in ADMIN_HTML


def test_command_directory_exposes_mapping_names_and_editable_triggers():
    assert "内部固定映射名" in ADMIN_HTML
    assert "内部固定映射名仅供程序识别" in ADMIN_HTML
    assert "caOpenName" in ADMIN_HTML
    assert "c.mapping_name" in ADMIN_HTML
    assert "c.trigger_name" in ADMIN_HTML
    assert "c.standard_path" not in ADMIN_HTML


def test_attribute_catalog_renders_builtin_aliases_as_read_only():
    assert "a.builtin_aliases || []" in ADMIN_HTML
    assert 'title="内置别名（固定）"' in ADMIN_HTML
    assert "const chips = builtinChips + customChips" in ADMIN_HTML


def test_send_concurrency_help_matches_per_target_parallel_delivery():
    assert "不同目标可并发，同一目标保持顺序" in ADMIN_HTML
    assert "QQ 协议调用仍串行" not in ADMIN_HTML


def test_mount_does_not_require_console_configuration(monkeypatch):
    import nonebot

    app = FastAPI()
    monkeypatch.setattr(nonebot, "get_app", lambda: app)
    monkeypatch.setattr(
        nonebot, "get_driver",
        lambda: types.SimpleNamespace(config=types.SimpleNamespace(port=8180)),
    )

    assert mount() is True
    with TestClient(app) as c:
        assert c.get("/admin/").status_code == 200


def test_status_shape(client):
    c, store = client
    store.add_config(
        111, weapon="torid", wildcard=None,
        positives=[["critical_chance"], ["multishot"]], negatives=[],
    )
    d = c.get("/admin/api/status").json()
    assert d["bot_connected"] is False
    assert d["discord_ready"] is False
    assert d["snowluma"] is None
    assert "napcat" not in d
    assert d["counts"]["configs"] == 1
    g = next(x for x in d["groups"] if x["group_id"] == 111)
    assert g["configs"] == 1 and g["enabled"]
    target = next(x for x in d["targets"] if x["scope_id"] == 111)
    assert target["platform"] == "qq" and target["label"] == "QQ群 111"
    assert "poller" in d and "uptime_seconds" in d["poller"]
    assert d["sender"] is None


def test_status_exposes_sender_health(client, monkeypatch):
    c, _ = client
    health = {
        "state": "restarting",
        "running": False,
        "restart_count": 2,
        "last_error": "RuntimeError: test",
        "last_error_at": 1.0,
        "last_started_at": 2.0,
        "last_success_at": 3.0,
    }
    fake_poller = types.SimpleNamespace(
        queue_depth=4, sender_health=health,
        delivery_status={
            "total": 4,
            "queued_by_source": {"irc": 3, "wm": 1},
        })
    monkeypatch.setattr(shared, "_poller", fake_poller)

    d = c.get("/admin/api/status").json()
    assert d["sender"] == health
    assert d["queue_depth"] == 4
    assert d["delivery"]["queued_by_source"] == {"irc": 3, "wm": 1}


def test_generic_target_api_supports_discord_without_config_owners(client):
    c, store = client
    discord = store.upsert_discord_target("432738652867526657")

    catalog = c.get("/admin/api/targets").json()["targets"]
    by_scope = {target["scope_id"]: target for target in catalog}
    assert by_scope[discord["scope_id"]]["label"] == (
        "Discord 用户 432738652867526657")

    authored_id = store.add_config(
        discord["scope_id"],
        weapon="boltor", wildcard=None,
        positives=[["critical_chance"], ["multishot"]], negatives=[],
    )
    authored = next(
        row for row in c.get(
            f"/admin/api/targets/{discord['scope_id']}/configs"
        ).json()["configs"]
        if row["id"] == authored_id
    )
    assert "creator_qq" not in authored

    created = c.post(
        f"/admin/api/targets/{discord['scope_id']}/configs",
        json={"text": "托里德 暴击 多重"},
    )
    assert created.status_code == 200
    cid = created.json()["id"]
    assert len(store.list_configs(discord["scope_id"])) == 2
    created_row = next(
        row for row in c.get(
            f"/admin/api/targets/{discord['scope_id']}/configs"
        ).json()["configs"]
        if row["id"] == cid
    )
    assert "creator_qq" not in created_row

    assert c.post(
        f"/admin/api/targets/{discord['scope_id']}/blacklist",
        json={"seller": "BadSeller"},
    ).status_code == 200
    assert c.post(
        f"/admin/api/targets/{discord['scope_id']}/blacklist",
        json={"seller": "Example\u00a0o", "scope": "channel"},
    ).status_code == 200
    assert store.list_blacklist(discord["scope_id"]) == ["BadSeller"]
    assert store.list_blacklist(
        discord["scope_id"], scope="channel") == ["Example o"]

    assert c.patch(
        "/admin/api/commands/sniper.list",
        json={"enabled": False, "scope_id": discord["scope_id"]},
    ).status_code == 200
    assert store.is_command_disabled("sniper.list")

    assert c.patch(
        f"/admin/api/targets/{discord['scope_id']}/configs/{cid}",
        json={"enabled": False},
    ).status_code == 405
    assert "enabled" not in store.get_config(cid, discord["scope_id"])
    assert c.get("/admin/api/targets/-99999/configs").status_code == 404


def test_config_can_be_listed_and_deleted(client):
    c, store = client
    # 先占用一个内部 ID，证明 WebUI 展示编号不是数据库 ID。
    store.add_config(
        222, weapon="torid", wildcard=None,
        positives=[["critical_chance"], ["multishot"]], negatives=[],
    )
    cid = store.add_config(
        111, weapon="torid", wildcard=None,
        positives=[["critical_chance"], ["multishot"]], negatives=[],
    )
    d = c.get("/admin/api/groups/111/configs").json()
    assert len(d["configs"]) == 1
    shown = d["configs"][0]
    assert shown["id"] == cid == 2
    assert shown["display_number"] == 1
    assert shown["command"].startswith("s ")
    assert "@我" not in shown["command"]
    assert "description" in shown

    r = c.patch(f"/admin/api/groups/111/configs/{cid}", json={"enabled": False})
    assert r.status_code == 405

    assert c.delete(f"/admin/api/groups/111/configs/{cid}").status_code == 200
    assert store.get_config(cid, 111) is None
    assert c.delete(f"/admin/api/groups/111/configs/{cid}").status_code == 404

    actions = [a["action"] for a in c.get("/admin/api/audit").json()["audit"]]
    assert "config_toggle" not in actions and "config_delete" in actions


def test_console_can_copy_configs_between_targets(client):
    c, store = client
    store.upsert_qq_target(222, 888, enabled=True)
    store.add_config(
        222, weapon="torid", wildcard=None,
        positives=[["critical_chance"], ["multishot"]], negatives=[],
    )

    copied = c.post(
        "/admin/api/targets/111/configs/copy",
        json={"source_scope_id": 222},
    )

    assert copied.status_code == 200
    assert copied.json() == {
        "copied": 1, "skipped": 0, "capped": 0, "total": 1}
    target_config = store.list_configs(111)[0]
    assert target_config["weapon"] == "torid"
    assert "creator_qq" not in target_config


def test_config_display_numbers_remain_fixed_after_delete(client):
    c, store = client
    ids = [
        store.add_config(
            111, weapon="torid", wildcard=None,
            positives=[[first], [second]], negatives=[],
        )
        for first, second in [
            ("critical_chance", "multishot"),
            ("critical_damage", "multishot"),
            ("critical_chance", "critical_damage"),
        ]
    ]
    before = c.get("/admin/api/groups/111/configs").json()["configs"]
    assert [item["id"] for item in before] == ids
    assert [item["display_number"] for item in before] == [1, 2, 3]
    assert all(item["command"].startswith("s ") for item in before)

    assert c.delete(f"/admin/api/groups/111/configs/{ids[1]}").status_code == 200
    after = c.get("/admin/api/groups/111/configs").json()["configs"]
    assert [item["id"] for item in after] == [ids[0], ids[2]]
    assert [item["display_number"] for item in after] == [1, 3]


def test_discord_target_is_console_managed_and_channel_defaults_off(client):
    c, store = client
    created = c.post("/admin/api/managed-targets", json={
        "platform": "discord", "external_id": "678901234567890123",
    })
    assert created.status_code == 200
    target = created.json()["target"]
    preference = store.get_target_preferences(target["scope_id"])
    assert preference["locale"] == "zh"
    assert preference["channel_enabled"] is False

    changed = c.patch(
        f"/admin/api/targets/{target['scope_id']}/preferences",
        json={"locale": "en", "channel_enabled": True},
    )
    assert changed.status_code == 200
    assert changed.json()["preferences"]["locale"] == "en"
    assert changed.json()["preferences"]["channel_enabled"] is True
    assert target["scope_id"] not in store.channel_delivery_scope_ids()

    store.add_config(
        target["scope_id"], weapon="torid", wildcard=None,
        positives=[["critical_damage"], ["multishot"]], negatives=[],
    )
    assert target["scope_id"] in store.channel_delivery_scope_ids()


def test_console_manages_qq_owner_and_target_state(client):
    c, store = client
    created = c.post("/admin/api/managed-targets", json={
        "platform": "qq", "external_id": "222", "owner_qq": "43",
        "enabled": False,
    })
    assert created.status_code == 200
    assert created.json()["target"]["owner_qq"] == 43
    assert created.json()["target"]["active"] is False

    changed = c.patch("/admin/api/managed-targets/222", json={
        "owner_qq": "44", "enabled": True,
    })
    assert changed.status_code == 200
    assert changed.json()["target"]["owner_qq"] == 44
    assert changed.json()["target"]["active"] is True
    assert store.get_target(222)["owner_qq"] == 44


def test_console_manages_target_notes_and_enable_duration(client):
    c, store = client
    created = c.post("/admin/api/managed-targets", json={
        "platform": "qq", "external_id": "222", "owner_qq": "43",
        "note": "测试目标", "enabled": False,
    })
    assert created.status_code == 200
    assert created.json()["target"]["note"] == "测试目标"
    assert created.json()["target"]["enabled_until"] is None

    enabled = c.patch("/admin/api/managed-targets/222", json={
        "enabled": True, "duration_days": 60,
    })
    assert enabled.status_code == 200
    target = enabled.json()["target"]
    assert target["enabled"] is True
    assert target["enabled_until"] - target["updated_at"] == 60 * 86400

    edited = c.patch("/admin/api/managed-targets/222", json={
        "note": "更新后的备注",
    })
    assert edited.status_code == 200
    assert edited.json()["target"]["note"] == "更新后的备注"
    assert edited.json()["target"]["enabled_until"] == target["enabled_until"]
    arbitrary_days = c.patch("/admin/api/managed-targets/222", json={
        "enabled": True, "duration_days": 31,
    })
    assert arbitrary_days.status_code == 200
    target = arbitrary_days.json()["target"]
    assert target["enabled_until"] - target["updated_at"] == 31 * 86400
    renewed = c.post("/admin/api/managed-targets/222/renew", json={
        "duration_days": 7,
    })
    assert renewed.status_code == 200
    assert renewed.json()["target"]["enabled_until"] == (
        target["enabled_until"] + 7 * 86400)
    assert c.patch("/admin/api/managed-targets/222", json={
        "enabled": True, "duration_days": 0,
    }).status_code == 422

    permanent = c.patch("/admin/api/managed-targets/222", json={
        "enabled": True, "duration_days": None,
    })
    assert permanent.status_code == 200
    assert permanent.json()["target"]["enabled_until"] is None
    assert c.post("/admin/api/managed-targets/222/renew", json={
        "duration_days": 7,
    }).status_code == 400
    assert store.get_target(111)["enabled_until"] is None


def test_console_reads_latest_platform_usernames_as_read_only(
        client, monkeypatch):
    c, store = client
    discord = store.upsert_discord_target("678901234567890123")

    class FakeAdapter:
        def __init__(self, name):
            self.name = name

        def get_name(self):
            return self.name

    class FakeQQBot:
        adapter = FakeAdapter("OneBot V11")

        async def get_group_member_info(self, **_kwargs):
            return {"card": "最新群名片", "nickname": "QQ 昵称"}

    class FakeDiscordBot:
        adapter = FakeAdapter("Discord")

        async def get_user(self, **_kwargs):
            return types.SimpleNamespace(
                global_name="Discord 显示名", username="discord_name")

    import nonebot
    monkeypatch.setattr(nonebot, "get_bots", lambda: {
        "qq": FakeQQBot(), "discord": FakeDiscordBot(),
    })

    targets = c.get("/admin/api/managed-targets").json()["targets"]
    by_scope = {target["scope_id"]: target for target in targets}
    assert by_scope[111]["username"] == "最新群名片"
    assert by_scope[discord["scope_id"]]["username"] == "Discord 显示名"
    assert c.patch("/admin/api/managed-targets/111", json={
        "username": "伪造名称",
    }).status_code == 422


def test_target_management_frontend_exposes_notes_expiry_and_delete():
    for marker in (
        'id="pt-note"',
        'id="pt-edit-username" readonly',
        'id="pt-edit-note"',
        'id="pt-enable-choice"',
        'step="1"',
        '限时时长可设置为任意正整数天',
        'managedTargetRenew(',
        '续期天数从当前到期时间继续累加',
        '· 剩余 ${esc(targetRemainingText(expiry))}',
        'managedTargetDelete(',
        '共享市场/追踪数据与系统日志会保留',
    ):
        assert marker in ADMIN_HTML


def test_target_delete_removes_only_target_owned_data_and_keeps_logs(
        client):
    c, store = client
    store.upsert_qq_target(222, 888, enabled=True, note="保留目标")
    for scope_id, weapon, seller in (
        (111, "torid", "DeleteSeller"),
        (222, "rubico", "KeepSeller"),
    ):
        store.add_config(
            scope_id, weapon=weapon, wildcard=None,
            positives=[["critical_chance"], ["multishot"]], negatives=[])
        store.add_blacklist(scope_id, seller)
        store.add_blacklist(scope_id, seller + "Channel", scope="channel")
        store.add_bargain_item(scope_id, f"item_{scope_id}", 0.2)
        store.add_bargain_riven_item(scope_id, weapon, 0.3)
        store._conn.execute(
            """INSERT INTO player_trackers
               (scope_id,account_id,target_nick,enabled,created_at)
               VALUES (?,?,?,?,?)""",
            (scope_id, f"pending-nick:{scope_id}", f"Player{scope_id}", 1, 1),
        )
        store._conn.execute(
            """INSERT INTO bargain_log
               (ts,group_id,kind,slug,order_id,price,baseline,discount,seller)
               VALUES (1,?,'item',?, ?,10,20,0.5,?)""",
            (scope_id, f"item_{scope_id}", f"order_{scope_id}", seller),
        )
    store._conn.execute(
        "INSERT INTO seen_auctions (id,ts) VALUES ('shared-auction',1)")
    store._conn.commit()
    store.add_audit("tester", "test", "before_delete", "QQ群 111", "保留")

    tracking_path = Path(shared._config.irc_track_db_path)
    with TrackingStore(tracking_path) as tracking:
        tracking.connection.execute(
            "INSERT INTO players (account_id,first_seen,last_seen) VALUES (?,?,?)",
            ("0123456789abcdef01234567", 1, 1),
        )
        session_id = tracking.connection.execute(
            """INSERT INTO presence_sessions
               (account_id,started_at,last_seen_at) VALUES (?,?,?)""",
            ("0123456789abcdef01234567", 1, 1),
        ).lastrowid
        tracking.connection.executemany(
            """INSERT INTO presence_alerts
               (scope_id,session_id,region_key,alerted_at) VALUES (?,?,?,?)""",
            ((111, session_id, "EN_NA", 1),
             (222, session_id, "EN_NA", 1)),
        )

    deleted = c.delete("/admin/api/managed-targets/111")

    assert deleted.status_code == 200
    assert deleted.json()["removed"]["presence_alerts"] == 1
    assert store.get_target(111) is None
    assert store.get_target(222)["note"] == "保留目标"
    for table, column in (
        ("configs", "group_id"),
        ("blacklist", "group_id"),
        ("channel_blacklist", "group_id"),
        ("target_preferences", "scope_id"),
        ("player_trackers", "scope_id"),
        ("bargain_items", "group_id"),
        ("bargain_riven_items", "group_id"),
    ):
        assert store._conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column}=111"
        ).fetchone()[0] == 0
        assert store._conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column}=222"
        ).fetchone()[0] == 1
    assert store._conn.execute(
        "SELECT COUNT(*) FROM bargain_log"
    ).fetchone()[0] == 2
    assert store._conn.execute(
        "SELECT COUNT(*) FROM seen_auctions WHERE id='shared-auction'"
    ).fetchone()[0] == 1
    actions = [row["action"] for row in store.list_audit()]
    assert "before_delete" in actions and "target_delete" in actions

    with TrackingStore(tracking_path, read_only=True) as tracking:
        assert tracking.connection.execute(
            "SELECT COUNT(*) FROM presence_alerts WHERE scope_id=111"
        ).fetchone()[0] == 0
        assert tracking.connection.execute(
            "SELECT COUNT(*) FROM presence_alerts WHERE scope_id=222"
        ).fetchone()[0] == 1
        assert tracking.connection.execute(
            "SELECT COUNT(*) FROM players"
        ).fetchone()[0] == 1
        assert tracking.connection.execute(
            "SELECT COUNT(*) FROM presence_sessions"
        ).fetchone()[0] == 1


def test_logs_endpoint(client):
    c, _ = client
    from src.plugins.riven_sniper.stats import LOGS
    d = c.get("/admin/api/logs?level=INFO&n=10").json()
    assert isinstance(d["logs"], list)


def test_tracking_console_surface_is_removed(client):
    c, _ = client
    assert c.get("/admin/api/tracking", params={"query": "Seller"}).status_code == 404
    assert c.get(
        "/admin/api/tracking/export", params={"query": "Seller"},
    ).status_code == 404
    assert 'data-nav="tracking"' not in ADMIN_HTML
    assert 'id="sec-tracking"' not in ADMIN_HTML
    assert "trackingSearch" not in ADMIN_HTML


def test_admin_boundaries_hide_internal_identifiers(client):
    c, store = client
    account_id = "0123456789abcdef01234567"
    fingerprint = "a" * 64

    for seller in (account_id, f"Alias {account_id}", f"Alias {fingerprint}"):
        response = c.post(
            "/admin/api/targets/111/blacklist", json={"seller": seller})
        assert response.status_code == 400
    assert store.list_blacklist(111) == []

    store._conn.execute(
        "INSERT INTO blacklist (group_id,seller,created_at) "
        "VALUES (?,?,?)", (111, f"Legacy {account_id}", 1))
    store._conn.commit()
    store.add_audit(
        "test", "test", "legacy", f"Legacy {account_id}", fingerprint)

    from src.plugins.riven_sniper.stats import LOGS
    entry = {
        "ts": "00:00:00", "iso": "2026-07-31T00:00:00+00:00",
        "level": "WARNING",
        "msg": f"legacy seller={account_id} fingerprint={fingerprint}",
        "name": "test",
    }
    LOGS._buf.append(entry)
    try:
        payload = {
            "configs": c.get("/admin/api/targets/111/configs").json(),
            "logs": c.get("/admin/api/logs?level=INFO&n=500").json(),
            "audit": c.get("/admin/api/audit?n=500").json(),
        }
        public = json.dumps(payload)
        assert account_id not in public
        assert fingerprint not in public
        assert payload["configs"]["blacklist"] == []
    finally:
        try:
            LOGS._buf.remove(entry)
        except ValueError:
            pass


def test_index_page_served(client):
    c, _ = client
    r = c.get("/admin/")
    assert r.status_code == 200
    assert "RivenSniper 控制台" in r.text


def test_inline_handler_args_are_html_escaped():
    assert "function js(s) { return esc(JSON.stringify" in ADMIN_HTML
    assert "prompt(`为 ${label} 添加别名`)" not in ADMIN_HTML


def test_admin_console_consolidates_redundant_navigation_and_controls():
    ids = re.findall(r'\bid="([^"]+)"', ADMIN_HTML)
    assert len(ids) == len(set(ids))

    for section in ("sec-access", "sec-records", "sec-system"):
        assert f'id="{section}"' in ADMIN_HTML
    for removed in (
            "sec-whitelist", "sec-members", "sec-feedusers",
            "sec-logs", "sec-audit", "st-bg-max",
            "bg-gsel", "cmd-gsel", "msel", "tp-gsel", "bglogtable"):
        assert f'id="{removed}"' not in ADMIN_HTML

    assert 'id="global-gsel"' in ADMIN_HTML
    assert re.search(
        r'<header class="page-header">.*?'
        r'<div class="group-context" id="group-context" hidden>',
        ADMIN_HTML,
        re.DOTALL,
    )
    assert 'class="context-copy"' not in ADMIN_HTML
    assert "context.hidden = !targeted" in ADMIN_HTML
    assert "const showTargetPreferences = route.page === 'access'" in ADMIN_HTML
    assert "context.classList.toggle('with-preferences', showTargetPreferences)" in ADMIN_HTML
    assert ".group-context.with-preferences { grid-template-columns:1fr 1fr; }" in ADMIN_HTML
    assert ".group-context.with-preferences { grid-template-columns:1fr; }" in ADMIN_HTML
    assert "return page === 'configs' || page === 'access' ||" in ADMIN_HTML
    assert "(page === 'bargain' && tab === 'monitor')" in ADMIN_HTML
    assert 'id="bp-max"' in ADMIN_HTML
    assert "sysPollNow('dash-action-out', this)" in ADMIN_HTML
    assert "sysTestPush('dash-test-group', this)" in ADMIN_HTML
    assert "sysRefreshData('data-out', this)" in ADMIN_HTML
    assert "whitelist:'access/whitelist'" not in ADMIN_HTML
    assert "bgLoadLog" not in ADMIN_HTML
    assert 'data-tab="history"' not in ADMIN_HTML
    assert "推送记录" not in ADMIN_HTML
    assert "和推送历史" not in ADMIN_HTML
    assert 'id="target-locale-field" for="target-locale" hidden' in ADMIN_HTML
    assert 'id="target-channel-field"' in ADMIN_HTML
    assert 'id="target-locale"' in ADMIN_HTML
    assert 'id="target-channel"' in ADMIN_HTML
    assert "syncTargetPreferences" in ADMIN_HTML
    assert "targetPreferenceChanged" in ADMIN_HTML
    assert "logs:'records/logs'" in ADMIN_HTML
    assert ".field-span-10 { grid-column:span 10; }" in ADMIN_HTML
    assert 'id="global-gsel" name="current_target" disabled' in ADMIN_HTML
    assert "/ 仅连接同一位置的 OR 备选" in ADMIN_HTML
    assert "重复词条或别名会拒绝" in ADMIN_HTML


def test_admin_console_guards_target_switches_and_keeps_log_events():
    assert "request !== configRequest || gid !== currentScopeId()" in ADMIN_HTML
    assert "request !== bgGroupRequest || gid !== currentScopeId()" in ADMIN_HTML
    assert "request !== commandRequest" in ADMIN_HTML
    assert "memberRequest" not in ADMIN_HTML
    assert "syncTargetOptions(st.targets || st.groups)" in ADMIN_HTML
    assert "`/targets/${gid}/configs`" in ADMIN_HTML
    assert "evtSrc.onopen" in ADMIN_HTML
    assert "if (!logPaused && panel.classList.contains('active'))" in ADMIN_HTML
    assert "if (logPaused) return" not in ADMIN_HTML


def test_seller_blacklist_uses_full_width_top_form_and_lower_table():
    start = ADMIN_HTML.index(
        '<div class="tab-panel" data-tab-page="configs" data-tab="blacklist">')
    end = ADMIN_HTML.index('<div id="sec-bargain"', start)
    panel = ADMIN_HTML[start:end]

    assert 'class="split-layout"' not in panel
    assert 'class="surface" style="margin-bottom:14px"' in panel
    assert 'class="form-field field-span-2" for="bl-scope"' in panel
    assert 'class="form-field field-span-8" for="bl-name"' in panel
    assert '<option value="all">全部</option>' in panel
    assert '<textarea id="bl-name"' in panel
    assert '<input id="bl-name"' not in panel
    assert '每行一个卖家名，单个最多 32 个字符' in panel
    assert panel.index('id="blacklist-add-title"') < panel.index(
        'id="blacklist-list-title"')


def test_admin_console_removes_all_input_placeholders():
    assert re.search(r'\bplaceholder\s*=', ADMIN_HTML) is None
    assert ".placeholder =" not in ADMIN_HTML
    assert "::placeholder" not in ADMIN_HTML


def test_alias_updates_preserve_expanded_groups_and_page_position():
    assert "function captureAliasView()" in ADMIN_HTML
    assert "details.weapon-group[open]" in ADMIN_HTML
    assert 'data-group="${esc(group)}"' in ADMIN_HTML
    assert "q || openWeaponGroups.has(group)" in ADMIN_HTML
    assert "view.anchorId = weaponRow.id" in ADMIN_HTML
    assert "anchor.scrollIntoView({block:'start'})" in ADMIN_HTML
    assert "restoreAliasView(view)" in ADMIN_HTML
    assert re.search(
        r"async function waInlineSave\(\).*?await loadAliases\(true, view\)",
        ADMIN_HTML,
        re.DOTALL,
    )
    assert re.search(
        r"async function waInlineDelete\(\).*?await loadAliases\(true, view\)",
        ADMIN_HTML,
        re.DOTALL,
    )
    assert "wildcardEditOpen(newAlias, category)" in ADMIN_HTML
    assert "aeOpen(newAlias, shown)" in ADMIN_HTML


def test_reply_text_console_is_removed():
    assert 'data-tab="texts"' not in ADMIN_HTML
    assert 'id="txttable"' not in ADMIN_HTML
    assert "loadTexts" not in ADMIN_HTML
    assert "txSave" not in ADMIN_HTML
    assert "命令管理" in ADMIN_HTML


def test_admin_console_interactions_keep_basic_accessibility_semantics():
    assert 'class="skip-link"' in ADMIN_HTML
    assert 'aria-live="polite" aria-atomic="true"' in ADMIN_HTML
    assert "button.setAttribute('role', 'tab')" in ADMIN_HTML
    assert "const shouldFocusMain" in ADMIN_HTML
    assert "function restoreFocusById(" in ADMIN_HTML
    assert "$('#pt-enable-choice').focus({preventScroll:true})" in ADMIN_HTML
    assert 'id="pt-state-trigger-${t.scope_id}"' in ADMIN_HTML
    assert "href=\"javascript:" not in ADMIN_HTML
