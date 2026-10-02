"""快速目标偏好、持久去重、旧版数据库迁移及管理入口。"""

import json
import sqlite3
import sys
import time
import types
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.plugins.riven_sniper import shared, envutil
from src.plugins.riven_sniper.config import Config
from src.plugins.riven_sniper.store import Store, SCHEMA_VERSION
from src.plugins.riven_sniper.webui.api import router
from src.plugins.riven_sniper.wfm_fast import FastRivenPoller


def add(store, scope=101):
    return store.add_config(scope, weapon="torid", wildcard=None,
                            positives=[["critical_chance"], ["critical_damage"], ["multishot"]],
                            negatives=[])


def test_previous_schema_migration_backs_up_and_preserves_rules_and_preferences(tmp_path):
    path = tmp_path / "sniper.db"
    store = Store(path)
    store.upsert_qq_target(101, 1, enabled=True)
    cid = add(store)
    store.set_target_locale(101, "en")
    store.set_target_channel_enabled(101, True)
    store.close()
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            DROP TABLE wm_sniper_notified;
            ALTER TABLE target_preferences DROP COLUMN wm_fast_enabled;
            ALTER TABLE target_preferences DROP COLUMN wm_fast_generation;
            PRAGMA user_version=20260815;
        """)
    migrated = Store(path)
    try:
        prefs = migrated.get_target_preferences(101)
        assert prefs["locale"] == "en" and prefs["channel_enabled"]
        assert not prefs["wm_fast_enabled"] and prefs["wm_fast_generation"] == 0
        assert migrated.get_config(cid, 101)["weapon"] == "torid"
        assert migrated._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        with sqlite3.connect(migrated.migration_backup_path) as backup:
            assert backup.execute("PRAGMA user_version").fetchone()[0] == 20260815
            assert "wm_fast_enabled" not in {
                row[1] for row in backup.execute("PRAGMA table_info(target_preferences)")}
    finally:
        migrated.close()


def test_target_scope_generation_activity_and_persistent_notification_ledger(tmp_path):
    path = tmp_path / "sniper.db"
    store = Store(path)
    for scope in (101, 102):
        store.upsert_qq_target(scope, 1, enabled=True)
        add(store, scope)
    assert store.wm_fast_configs() == []
    store.set_target_wm_fast_enabled(101, True)
    store.set_target_wm_fast_enabled(101, True)
    assert store.get_target_preferences(101)["wm_fast_generation"] == 1
    assert [c["group_id"] for c in store.wm_fast_configs()] == [101]
    assert store.claim_wm_notifications(101, ["a", "a", "b"]) == {"a", "b"}
    assert store.claim_wm_notifications(102, ["a"]) == {"a"}
    store.set_target_wm_fast_enabled(101, False)
    store.set_target_wm_fast_enabled(101, True)
    assert store.get_target_preferences(101)["wm_fast_generation"] == 3
    store.set_target_enabled(101, False)
    assert store.wm_fast_configs() == []
    store.close()
    reopened = Store(path)
    try:
        assert reopened.claim_wm_notifications(101, ["a", "b"]) == set()
        reopened.release_wm_notifications(101, ["a"])
        assert reopened.claim_wm_notifications(101, ["a"]) == {"a"}
        reopened._conn.execute("UPDATE wm_sniper_notified SET notified_at=?", (int(time.time())-31*86400,))
        reopened._conn.commit()
        reopened.prune_seen()
        assert reopened.claim_wm_notifications(101, ["b"]) == {"b"}
    finally:
        reopened.close()


def test_discord_only_joins_fast_queries_when_platform_is_enabled():
    store = Store(":memory:")
    target = store.upsert_discord_target("123456789012345678", enabled=True)
    scope = target["scope_id"]
    add(store, scope)
    store.set_target_wm_fast_enabled(scope, True)
    try:
        assert store.wm_fast_configs() == []
        assert len(store.wm_fast_configs(discord_enabled=True)) == 1
    finally:
        store.close()


def test_api_switch_rule_status_and_hot_interval_are_scoped_and_persistent(monkeypatch):
    store = Store(":memory:")
    for scope in (101, 102):
        store.upsert_qq_target(scope, 1, enabled=True)
        add(store, scope)
    config = Config()
    fast = FastRivenPoller(store, config, None)
    monkeypatch.setattr(shared, "_store", store)
    monkeypatch.setattr(shared, "_config", config)
    monkeypatch.setattr(shared, "_poller", types.SimpleNamespace(fast=fast))
    changes = []
    monkeypatch.setattr(envutil, "update_env_file", changes.append)
    app = FastAPI()
    app.include_router(router, prefix="/admin")
    try:
        with TestClient(app) as client:
            result = client.patch("/admin/api/targets/101/preferences", json={"wm_fast_enabled": True})
            assert result.status_code == 200
            assert result.json()["preferences"]["wm_fast_enabled"] is True
            assert not store.get_target_preferences(102)["wm_fast_enabled"]
            fast.sync(store.wm_fast_configs(), now=time.time())
            status = client.get("/admin/api/wm-fast?scope_id=101").json()
            assert status["query_count"] == 1
            assert status["rules"][0]["eligible"] and status["rules"][0]["baseline_count"] == 1
            assert "id" not in status["rules"][0] and "proxies" not in json.dumps(status)
            assert client.get("/admin/api/wm-fast?scope_id=102").json()["query_count"] == 0
            assert client.get("/admin/api/wm-fast?scope_id=999").status_code == 404
            assert client.patch("/admin/api/settings", json={"wm_fast_interval": 1.5}).status_code == 200
            assert config.wm_fast_interval == 1.5 and changes == [{"WM_FAST_INTERVAL": "1.5"}]
            assert client.patch("/admin/api/settings", json={"wm_fast_interval": .5}).status_code == 422
            assert config.wm_fast_interval == 1.5
            # 同一查询已有订阅完成基线，新增的规则仍需独立完成自己的首轮。
            state = next(iter(fast.queries.values()))
            state.observe([], now=time.time())
            store.add_config(101, weapon="torid", wildcard=None,
                             positives=[["critical_chance"], ["critical_damage"], ["multishot"]],
                             negatives=[["zoom"]])
            client.patch("/admin/api/targets/102/preferences", json={"wm_fast_enabled": True})
            fast.sync(store.wm_fast_configs(), now=time.time())
            status = client.get("/admin/api/wm-fast?scope_id=101").json()
            assert status["query_count"] == 1
            assert [r["baseline_count"] for r in status["rules"]] == [0, 1]
            other = client.get("/admin/api/wm-fast?scope_id=102").json()
            assert other["rules"][0]["baseline_count"] == 1
            state.observe([], now=time.time())
            status = client.get("/admin/api/wm-fast?scope_id=101").json()
            assert [r["baseline_count"] for r in status["rules"]] == [0, 0]
            store.set_target_enabled(101, False)
            assert client.get("/admin/api/wm-fast?scope_id=101").json()["state"] == "target_inactive"
    finally:
        store.close()
