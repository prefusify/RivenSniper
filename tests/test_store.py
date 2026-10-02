"""存储层测试：配置、推送目标偏好和固定展示编号。"""

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.plugins.riven_sniper.criteria import (  # noqa: E402
    ANY_ATTRIBUTE,
    groups_as_lists,
)
from src.plugins.riven_sniper.store import SCHEMA_VERSION, Store  # noqa: E402


def _mem_store():
    return Store(":memory:")


def _add(store, group_id=1, *, weapon="torid", positives=None,
         positive_ratings=None, negatives=None, negative_ratings=None,
         zero_rerolls=False):
    return store.add_config(
        group_id,
        weapon=weapon,
        wildcard=None,
        positives=positives or [["critical_chance"], ["multishot"]],
        positive_ratings=positive_ratings,
        negatives=negatives,
        negative_ratings=negative_ratings,
        zero_rerolls=zero_rerolls,
    )


def test_unknown_database_is_not_modified(tmp_path):
    db_path = tmp_path / "old.db"
    old = sqlite3.connect(db_path)
    old.executescript("""
        CREATE TABLE configs (id INTEGER PRIMARY KEY, old_value TEXT);
        CREATE TABLE global_whitelist (qq INTEGER PRIMARY KEY);
        INSERT INTO configs VALUES (1, 'discard me');
        INSERT INTO global_whitelist VALUES (42);
    """)
    old.commit()
    old.close()

    before = db_path.read_bytes()
    with pytest.raises(RuntimeError, match="数据库未被修改"):
        Store(db_path)
    assert db_path.read_bytes() == before


def test_supported_schema_migration_drops_qq_configs_and_preserves_others(
        tmp_path):
    db_path = tmp_path / "supported-old.db"
    old = sqlite3.connect(db_path)
    usage_day = time.strftime(
        "%Y-%m-%d", time.localtime(time.time() - 6 * 86400))
    old.executescript(f"""
        CREATE TABLE configs (
            id INTEGER PRIMARY KEY, group_id INTEGER, creator_qq INTEGER,
            weapon TEXT, wildcard TEXT, positives TEXT, negatives TEXT,
            zero_rerolls INTEGER, enabled INTEGER, created_at INTEGER);
        CREATE TABLE blacklist (
            group_id INTEGER, seller TEXT COLLATE NOCASE, added_by INTEGER,
            created_at INTEGER, PRIMARY KEY(group_id,seller));
        CREATE TABLE channel_blacklist (
            group_id INTEGER, seller TEXT COLLATE NOCASE, added_by INTEGER,
            created_at INTEGER, PRIMARY KEY(group_id,seller));
        CREATE TABLE disabled_commands (
            command TEXT, group_id INTEGER, created_at INTEGER,
            PRIMARY KEY(command,group_id));
        CREATE TABLE command_renames (
            command TEXT PRIMARY KEY, custom_name TEXT, keep_original INTEGER);
        CREATE TABLE command_aliases (
            alias TEXT PRIMARY KEY, command TEXT);
        CREATE TABLE command_texts (
            key TEXT, locale TEXT, text TEXT, PRIMARY KEY(key,locale));
        CREATE TABLE command_usage (
            command TEXT, day TEXT, count INTEGER,
            PRIMARY KEY(command,day));
        CREATE TABLE command_permissions (
            command TEXT PRIMARY KEY, level TEXT);
        CREATE TABLE member_permissions (
            group_id INTEGER, qq INTEGER, level TEXT, updated_at INTEGER,
            PRIMARY KEY(group_id,qq));
        CREATE TABLE platform_targets (
            id INTEGER PRIMARY KEY, platform TEXT, external_id TEXT,
            authorized INTEGER, is_admin INTEGER, expires_at INTEGER,
            granted_by TEXT, created_at INTEGER, updated_at INTEGER,
            UNIQUE(platform,external_id));
        CREATE TABLE target_preferences (
            scope_id INTEGER PRIMARY KEY, locale TEXT, channel_enabled INTEGER,
            updated_at INTEGER);
        CREATE TABLE player_trackers (
            id INTEGER PRIMARY KEY, scope_id INTEGER, account_id TEXT,
            target_nick TEXT, creator_id TEXT, enabled INTEGER,
            created_at INTEGER, UNIQUE(scope_id,account_id));
        CREATE TABLE bargain_items (
            id INTEGER PRIMARY KEY, group_id INTEGER, creator_qq INTEGER,
            slug TEXT, threshold REAL, level TEXT, enabled INTEGER,
            created_at INTEGER, UNIQUE(group_id,slug));
        CREATE TABLE bargain_riven_items (
            id INTEGER PRIMARY KEY, group_id INTEGER, creator_qq INTEGER,
            weapon_slug TEXT, threshold REAL, enabled INTEGER,
            created_at INTEGER, UNIQUE(group_id,weapon_slug));
        CREATE TABLE bargain_groups (
            group_id INTEGER PRIMARY KEY, items_enabled INTEGER,
            riven_enabled INTEGER, updated_at INTEGER);
        CREATE TABLE seen_auctions (id TEXT PRIMARY KEY, ts INTEGER);
        CREATE TABLE feed_users (
            id INTEGER PRIMARY KEY, username TEXT COLLATE NOCASE UNIQUE,
            password_hash TEXT, tier TEXT, enabled INTEGER,
            channel_enabled INTEGER, created_at INTEGER, updated_at INTEGER);

        INSERT INTO configs VALUES
            (1,111,42,'torid',NULL,'[["critical_chance"],["multishot"]]',
             '[]',0,0,1),
            (2,-1,1001,'rubico',NULL,'[["critical_damage"],["multishot"]]',
             '[]',1,1,2);
        INSERT INTO blacklist VALUES (111,'QQSeller',42,1),(-1,'DMSeller',1001,2);
        INSERT INTO channel_blacklist VALUES
            (111,'QQChannel',42,1),(-1,'DMChannel',1001,2);
        INSERT INTO disabled_commands VALUES
            ('狙击列表',0,1),('狙击添加',111,2),('命令大全',0,3);
        INSERT INTO command_renames VALUES
            ('命令大全','旧帮助',1),('狙击列表','查看监听',1),
            ('词条列表','语言',1);
        INSERT INTO command_aliases VALUES
            ('帮助旧名','命令大全'),('查看监听别名','狙击列表'),
            ('狙击复制','词条列表');
        INSERT INTO command_texts VALUES
            ('命令大全.正文','zh','旧帮助'),('黑名单.标题','zh','保留文案');
        INSERT INTO command_usage VALUES
            ('命令大全','{usage_day}',3),('狙击列表','{usage_day}',4);
        INSERT INTO command_permissions VALUES ('狙击列表','manager');
        INSERT INTO member_permissions VALUES (111,43,'manager',1);
        INSERT INTO platform_targets VALUES
            (1,'discord','1001',1,1,NULL,'legacy',1,2);
        INSERT INTO target_preferences VALUES
            (111,'en',1,1),(-1,'en',1,2);
        INSERT INTO player_trackers VALUES
            (1,111,'pending-nick:qq','QQPlayer','qq:42',1,1),
            (2,-1,'pending-nick:dm','DMPlayer','discord:1001',1,2);
        INSERT INTO bargain_items VALUES
            (1,111,42,'arcane_grace',0.2,'max',0,1),
            (2,-1,1001,'mesa_prime_set',0.3,NULL,1,2);
        INSERT INTO bargain_riven_items VALUES
            (1,111,42,'torid',0.3,1,1),
            (2,-1,1001,'rubico',0.4,0,2);
        INSERT INTO bargain_groups VALUES (111,1,1,1),(-1,1,1,2);
        INSERT INTO seen_auctions VALUES ('seen-before-migration',1);
        INSERT INTO feed_users VALUES
            (1,'PreservedFeed','hash','pro',1,1,1,2);
        PRAGMA user_version=20260731;
    """)
    old.commit()
    old.close()

    store = Store(db_path)

    assert store.migration_backup_path is not None
    assert store.migration_backup_path.exists()
    with sqlite3.connect(store.migration_backup_path) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 20260731
        assert backup.execute(
            "SELECT COUNT(*) FROM configs WHERE group_id=111").fetchone()[0] == 1
    assert store.list_configs(111) == []
    dm_configs = store.list_configs(-1)
    assert len(dm_configs) == 1
    assert dm_configs[0]["display_number"] == 1
    assert "creator_qq" not in dm_configs[0] and "enabled" not in dm_configs[0]
    assert store.list_blacklist(111) == []
    assert store.list_blacklist(-1) == ["DMSeller"]
    assert store.list_blacklist(-1, scope="channel") == ["DMChannel"]
    assert store.list_player_trackers(111) == []
    assert store.list_player_trackers(-1)[0]["target_nick"] == "DMPlayer"
    assert store.list_bargain_items(111) == []
    assert store.list_bargain_items(-1)[0]["slug"] == "mesa_prime_set"
    assert "creator_qq" not in store.list_bargain_items(-1)[0]
    assert store.list_bargain_riven_items(111) == []
    assert store.list_bargain_riven_items(-1)[0]["weapon_slug"] == "rubico"
    assert "enabled" not in store.list_bargain_riven_items(-1)[0]
    target = store.get_target(-1)
    assert target["platform"] == "discord" and target["external_id"] == "1001"
    assert target["active"] is True
    assert not {"is_admin", "expires_at", "granted_by"} & target.keys()
    assert store.get_target(111) is None
    assert store.get_target_preferences(-1)["locale"] == "en"
    assert store.get_target_preferences(111)["locale"] == "zh"
    assert store._conn.execute(
        "SELECT username FROM feed_users WHERE id=1"
    ).fetchone()["username"] == "PreservedFeed"
    assert store.mark_seen(["seen-before-migration"]) == []
    assert store.is_command_disabled("sniper.list") is True
    assert store.list_command_aliases() == {
        "sniper.list": ["查看监听别名"]}
    assert store.command_usage_7d()["sniper.list"] == 4
    tables = {row[0] for row in store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "command_texts" not in tables
    assert "command_permissions" not in tables
    assert "member_permissions" not in tables
    assert "command_renames" not in tables
    assert "bargain_groups" not in tables
    store.close()


def test_riven_bargain_switch_migration_discards_values_and_keeps_watches(
        tmp_path):
    db_path = tmp_path / "riven-switches.db"
    store = Store(db_path)
    store.upsert_qq_target(111, 42, enabled=True)
    item_id = store.add_bargain_riven_item(111, "torid", 0.35)
    with store._conn:
        user_id = store._conn.execute(
            """INSERT INTO feed_users
               (username,password_hash,tier,enabled,channel_enabled,
                created_at,updated_at)
               VALUES ('LegacySwitches','hash','plus',1,0,1,1)"""
        ).lastrowid
        store._conn.execute(
            """INSERT INTO feed_bargain_settings
               (user_id,items_enabled,updated_at) VALUES (?,1,1)""",
            (user_id,),
        )
        store._conn.execute(
            """INSERT INTO feed_bargain_riven_items
               (user_id,weapon_slug,threshold,created_at,updated_at)
               VALUES (?,'rubico',0.4,1,1)""",
            (user_id,),
        )
        store._conn.execute(
            "INSERT INTO disabled_commands (command_id,created_at) VALUES (?,1)",
            ("bargain.riven.enable",),
        )
        store._conn.execute(
            """INSERT INTO command_aliases (alias,command_id)
               VALUES ('toggle-old','bargain.riven.toggle')"""
        )
        store._conn.execute(
            """INSERT INTO command_aliases (alias,command_id)
               VALUES ('add-old','blacklist.list.add')"""
        )
        store._conn.execute(
            """INSERT INTO command_aliases (alias,command_id)
               VALUES ('黑名单 添加','blacklist.add')"""
        )
        store._conn.execute(
            """INSERT INTO command_usage (command_id,day,count)
               VALUES ('bargain.riven.disable','2026-08-09',3)"""
        )
        store._conn.execute(
            """CREATE TABLE command_texts (
                key TEXT NOT NULL, locale TEXT NOT NULL, text TEXT NOT NULL,
                PRIMARY KEY (key,locale))"""
        )
        store._conn.execute(
            """INSERT INTO command_texts (key,locale,text)
               VALUES ('捡漏紫卡.用法','zh','旧开关帮助')"""
        )
    store.close()

    legacy = sqlite3.connect(db_path)
    legacy.executescript("""
        ALTER TABLE bargain_riven_items
            ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1;
        UPDATE bargain_riven_items SET enabled=0;
        CREATE TABLE bargain_groups (
            group_id INTEGER PRIMARY KEY,
            riven_enabled INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL
        );
        INSERT INTO bargain_groups VALUES (111,0,1);
        ALTER TABLE feed_bargain_settings
            ADD COLUMN riven_enabled INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE feed_bargain_riven_items
            ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1;
        UPDATE feed_bargain_riven_items SET enabled=0;
        PRAGMA user_version=20260809;
    """)
    legacy.commit()
    legacy.close()

    migrated = Store(db_path)

    assert migrated.migration_backup_path is not None
    assert migrated._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    row = migrated.get_bargain_riven_item(item_id, 111)
    assert row["weapon_slug"] == "torid" and row["threshold"] == 0.35
    assert "enabled" not in row
    assert migrated._conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bargain_groups'"
    ).fetchone() is None
    feed_settings_columns = {
        column[1] for column in migrated._conn.execute(
            "PRAGMA table_info(feed_bargain_settings)"
        )
    }
    feed_riven_columns = {
        column[1] for column in migrated._conn.execute(
            "PRAGMA table_info(feed_bargain_riven_items)"
        )
    }
    assert "riven_enabled" not in feed_settings_columns
    assert "enabled" not in feed_riven_columns
    assert migrated._conn.execute(
        "SELECT weapon_slug FROM feed_bargain_riven_items"
    ).fetchone()[0] == "rubico"
    assert migrated._conn.execute(
        "SELECT 1 FROM disabled_commands WHERE command_id LIKE 'bargain.riven.%'"
    ).fetchone() is None
    assert migrated._conn.execute(
        "SELECT 1 FROM command_aliases WHERE command_id LIKE 'bargain.riven.%' "
        "AND command_id NOT IN ('bargain.riven.list','bargain.riven.delete')"
    ).fetchone() is None
    assert migrated._conn.execute(
        "SELECT 1 FROM command_aliases WHERE command_id='blacklist.list.add' "
        "OR alias='黑名单 添加'"
    ).fetchone() is None
    assert migrated._conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='command_texts'"
    ).fetchone() is None
    migrated.close()

    reopened = Store(db_path)
    assert reopened._conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='command_texts'"
    ).fetchone() is None
    reopened.close()


def test_text_table_migration_backs_up_and_preserves_business_data(tmp_path):
    path = tmp_path / "text-migration.db"
    store = Store(path)
    store.upsert_qq_target(111, 42, enabled=True)
    _add(store, 111)
    store.kv_set("keep-setting", "unchanged")
    tables = [row[0] for row in store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    before = {name: [tuple(row) for row in store._conn.execute(
        f'SELECT * FROM "{name}"')] for name in tables}
    store._conn.executescript("""
        CREATE TABLE command_texts (
            key TEXT NOT NULL, locale TEXT NOT NULL, text TEXT NOT NULL,
            PRIMARY KEY (key,locale));
        INSERT INTO command_texts VALUES
            ('黑名单.空','zh','旧中文内容'),
            ('黑名单.空','en','Old English copy');
        PRAGMA user_version=20260908;
    """)
    store.close()

    migrated = Store(path)
    assert migrated._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert migrated._conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='command_texts'"
    ).fetchone() is None
    after = {name: [tuple(row) for row in migrated._conn.execute(
        f'SELECT * FROM "{name}"')] for name in tables}
    assert after == before
    with sqlite3.connect(migrated.migration_backup_path) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 20260908
        assert backup.execute("SELECT COUNT(*) FROM command_texts").fetchone()[0] == 2
    migrated.close()

    reopened = Store(path)
    assert reopened.migration_backup_path is None
    assert reopened._conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='command_texts'"
    ).fetchone() is None
    reopened.close()


def test_target_management_migration_keeps_enabled_targets_permanent(tmp_path):
    db_path = tmp_path / "target-management.db"
    store = Store(db_path)
    store.upsert_qq_target(111, 42, enabled=True)
    store.upsert_qq_target(222, 43, enabled=False)
    store.close()

    legacy = sqlite3.connect(db_path)
    legacy.executescript("""
        DROP INDEX IF EXISTS idx_targets_active;
        ALTER TABLE targets RENAME TO targets_with_management;
        CREATE TABLE targets (
            scope_id INTEGER PRIMARY KEY,
            platform TEXT NOT NULL,
            external_id TEXT NOT NULL,
            owner_qq INTEGER,
            enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            UNIQUE (platform, external_id),
            CHECK (
                (platform='qq' AND scope_id>0 AND owner_qq IS NOT NULL)
                OR (platform<>'qq' AND scope_id<0 AND owner_qq IS NULL)
            )
        );
        INSERT INTO targets
            (scope_id,platform,external_id,owner_qq,enabled,created_at,updated_at)
        SELECT scope_id,platform,external_id,owner_qq,enabled,created_at,updated_at
        FROM targets_with_management;
        DROP TABLE targets_with_management;
        PRAGMA user_version=20260810;
    """)
    legacy.commit()
    legacy.close()

    migrated = Store(db_path)

    assert migrated.migration_backup_path is not None
    permanent = migrated.get_target(111)
    assert permanent["enabled"] is True
    assert permanent["active"] is True
    assert permanent["enabled_until"] is None
    assert permanent["note"] == ""
    disabled = migrated.get_target(222)
    assert disabled["enabled"] is False
    assert disabled["enabled_until"] is None
    migrated.close()


def test_target_note_timed_enable_and_expiry_are_scope_local():
    store = _mem_store()
    timed = store.upsert_qq_target(
        111, 42, enabled=True, duration_days=30, note="主推送目标")
    store.upsert_qq_target(222, 43, enabled=True, note="不受影响")

    assert timed["enabled_until"] - timed["updated_at"] == 30 * 86400
    assert store.set_target_note(111, "新的备注")["note"] == "新的备注"
    assert store.expire_targets(now=timed["enabled_until"] - 1) == []
    assert store.expire_targets(now=timed["enabled_until"]) == [111]

    expired = store.get_target(111)
    assert expired["enabled"] is False
    assert expired["active"] is False
    assert expired["enabled_until"] == timed["enabled_until"]
    assert expired["note"] == "新的备注"
    assert store.get_target(222)["active"] is True
    assert any(row["action"] == "target_expired"
               for row in store.list_audit())

    renewed = store.set_target_enabled(111, True, duration_days=31)
    assert renewed["active"] is True
    assert renewed["enabled_until"] - renewed["updated_at"] == 31 * 86400
    permanent = store.set_target_enabled(111, True)
    assert permanent["enabled_until"] is None
    with pytest.raises(ValueError, match="正整数"):
        store.set_target_enabled(111, True, duration_days=0)
    store.close()


def test_target_renewal_extends_deadline_and_reactivates_expired_target(
        monkeypatch):
    now = [1_700_000_000]
    monkeypatch.setattr(
        "src.plugins.riven_sniper.store.time.time", lambda: now[0])
    store = _mem_store()
    timed = store.upsert_qq_target(
        111, 42, enabled=True, duration_days=30)

    now[0] += 60
    renewed = store.renew_target(111, 5)
    assert renewed["enabled_until"] == timed["enabled_until"] + 5 * 86400
    assert renewed["active"] is True

    now[0] = renewed["enabled_until"]
    assert store.expire_targets() == [111]
    now[0] += 60
    reactivated = store.renew_target(111, 2)
    assert reactivated["enabled"] is True
    assert reactivated["enabled_until"] == now[0] + 2 * 86400

    store.upsert_qq_target(222, 43, enabled=True)
    with pytest.raises(ValueError, match="没有可续期"):
        store.renew_target(222, 1)
    store.close()


def test_retired_web_delivery_tables_remain_in_schema():
    store = Store(":memory:")
    tables = {row[0] for row in store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert {
        "feed_users",
        "feed_sessions",
        "feed_user_settings",
        "feed_filter_attributes",
        "feed_seller_blacklist",
        "feed_channel_filter_rules",
        "feed_bargain_settings",
        "feed_bargain_items",
        "feed_bargain_riven_items",
    } <= tables
    store.close()


def test_unknown_legacy_web_delivery_database_is_preserved(tmp_path):
    path = tmp_path / "legacy-feed.db"
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE feed_users (
            id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT,
            tier TEXT, enabled INTEGER, created_at INTEGER, updated_at INTEGER
        );
        CREATE TABLE feed_seller_blacklist (
            user_id INTEGER, scope TEXT, seller TEXT, created_at INTEGER
        );
        INSERT INTO feed_users VALUES
            (1, 'LegacyOrder', 'old', 'pro', 1, 1, 1);
        INSERT INTO feed_seller_blacklist VALUES (1, 'wm', 'First', 100);
        INSERT INTO feed_seller_blacklist VALUES (1, 'wm', 'Second', 200);
    """)
    connection.commit()
    connection.close()

    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="数据库未被修改"):
        Store(path)
    assert path.read_bytes() == before


def test_current_database_keeps_existing_blacklist_as_wm_and_adds_channel(
        tmp_path):
    db_path = tmp_path / "current.db"
    current = sqlite3.connect(db_path)
    current.executescript(f"""
        CREATE TABLE blacklist (
            group_id INTEGER NOT NULL,
            seller TEXT NOT NULL COLLATE NOCASE,
            added_by INTEGER,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (group_id, seller)
        );
        INSERT INTO blacklist VALUES (1, 'ExistingSeller', 42, 1);
        PRAGMA user_version={SCHEMA_VERSION};
    """)
    current.commit()
    current.close()

    store = Store(db_path)

    assert store.list_blacklist(1, scope="wm") == ["ExistingSeller"]
    assert store.list_blacklist(1, scope="channel") == []
    assert store._conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='channel_blacklist'"
    ).fetchone() is not None
    store.close()


def test_target_preferences_and_player_trackers_are_scope_local():
    s = _mem_store()
    assert s.get_target_preferences(1)["locale"] == "zh"
    assert s.get_target_preferences(1)["channel_enabled"] is False
    assert s.get_target_preferences(1)["channel_dedupe_hours"] == 1
    assert s.set_target_locale(1, "en")["locale"] == "en"
    assert s.set_target_channel_enabled(1, True)["channel_enabled"] is True
    assert s.set_target_channel_dedupe_hours(
        1, 72)["channel_dedupe_hours"] == 72
    untouched = s.get_target_preferences(2)
    assert untouched["scope_id"] == 2
    assert untouched["locale"] == "zh"
    assert untouched["channel_enabled"] is False
    assert untouched["channel_dedupe_hours"] == 1
    for invalid in (0, 73, 1.5, True):
        with pytest.raises(ValueError, match="1～72"):
            s.set_target_channel_dedupe_hours(1, invalid)

    account_id = "0123456789abcdef01234567"
    first, created = s.add_player_tracker(
        1, "SomePlayer", account_id=account_id)
    duplicate, duplicate_created = s.add_player_tracker(
        1, "someplayer", account_id=account_id)
    other, other_created = s.add_player_tracker(
        2, "SomePlayer", account_id=account_id)
    assert (duplicate, duplicate_created) == (first, False)
    assert other_created is True and other != first
    tracker = s.list_player_trackers(1)[0]
    assert tracker["target_nick"] == "someplayer"
    assert tracker["resolved"] is True
    assert tracker["account_id"] == account_id
    assert s.delete_player_tracker(first, 1)
    s.close()


def test_target_channel_dedupe_migration_defaults_existing_targets_to_one_hour(
        tmp_path):
    db_path = tmp_path / "target-dedupe.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript("""
        CREATE TABLE target_preferences (
            scope_id INTEGER PRIMARY KEY,
            locale TEXT NOT NULL,
            channel_enabled INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        INSERT INTO target_preferences VALUES (111,'zh',1,10);
        PRAGMA user_version=20260812;
    """)
    legacy.commit()
    legacy.close()

    store = Store(db_path)

    assert store.migration_backup_path is not None
    assert store.get_target_preferences(111)["channel_dedupe_hours"] == 1
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == (
        SCHEMA_VERSION)
    store.close()


def test_editable_command_name_migration_flattens_aliases_without_conflicts(
        tmp_path):
    db_path = tmp_path / "editable-command-names.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript("""
        CREATE TABLE command_aliases (
            parent_id TEXT NOT NULL DEFAULT '',
            alias TEXT NOT NULL COLLATE NOCASE,
            command_id TEXT NOT NULL,
            PRIMARY KEY (parent_id, alias)
        );
        CREATE TABLE disabled_commands (
            command_id TEXT PRIMARY KEY,
            created_at INTEGER NOT NULL
        );
        INSERT INTO command_aliases VALUES
            ('','Trackers','tracker.manage'),
            ('tracker.manage','list','tracker.manage.list'),
            ('bargain.riven','list','bargain.riven.list'),
            ('tracking.open','riven','tracking.open.riven'),
            ('tracking.open','card','tracking.open.riven'),
            ('bargain.riven','watch','bargain.riven.list'),
            ('tracker.manage','shared','tracker.manage.list'),
            ('bargain.riven','shared','bargain.riven.list'),
            ('','开盒紫卡','sniper.add'),
            ('','sniper.add','sniper.list');
        INSERT INTO disabled_commands VALUES
            ('bargain.riven',10),
            ('tracking.open',11),
            ('tracker.manage',12);
        PRAGMA user_version=20260813;
    """)
    legacy.commit()
    legacy.close()

    store = Store(db_path)

    assert store.migration_backup_path is not None
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == (
        SCHEMA_VERSION)
    assert store.list_command_names()["tracking.open.riven"] == "开盒紫卡"
    assert store.list_command_aliases() == {
        "bargain.riven.list": ["watch"],
        "tracker.manage": ["Trackers"],
        "tracking.open.riven": ["card", "riven"],
    }
    assert set(store.list_disabled_commands()) >= {
        "bargain.riven", "bargain.riven.list", "bargain.riven.delete",
        "tracking.open", "tracking.open.riven",
        "tracker.manage", "tracker.manage.list", "tracker.manage.delete",
    }
    alias_columns = {
        row[1] for row in store._conn.execute(
            "PRAGMA table_info(command_aliases)")
    }
    assert "parent_id" not in alias_columns
    store.close()


def test_minimum_rating_migration_keeps_old_configs_unrestricted(tmp_path):
    db_path = tmp_path / "minimum-ratings.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript("""
        CREATE TABLE configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            display_number INTEGER NOT NULL,
            weapon TEXT,
            wildcard TEXT,
            positives TEXT NOT NULL,
            negatives TEXT NOT NULL,
            zero_rerolls INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        );
        INSERT INTO configs VALUES (
            1,111,1,'torid',NULL,
            '[["critical_chance"],["multishot"]]','[]',0,10
        );
        PRAGMA user_version=20260814;
    """)
    legacy.commit()
    legacy.close()

    store = Store(db_path)

    assert store.migration_backup_path is not None
    config = store.get_config(1, 111)
    assert config["positive_ratings"] == []
    assert config["negative_ratings"] == []
    columns = {
        row[1] for row in store._conn.execute("PRAGMA table_info(configs)")
    }
    assert {"positive_ratings", "negative_ratings"} <= columns
    raw = store._conn.execute(
        "SELECT positive_ratings,negative_ratings FROM configs WHERE id=1"
    ).fetchone()
    assert tuple(raw) == ("[]", "[]")
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == (
        SCHEMA_VERSION)
    store.close()


def test_command_names_and_aliases_are_globally_unique():
    store = _mem_store()

    assert store.set_command_name("tracking.open", "查人")
    assert store.list_command_names()["tracking.open"] == "查人"
    assert not store.set_command_name("tracking.open", "狙击添加")
    assert not store.set_command_name("tracking.open", "sniper.add")
    assert not store.set_command_name("tracking.open", "查 人")
    assert store.add_command_alias("find", "tracking.open")
    assert not store.add_command_alias("find", "sniper.add")
    assert not store.add_command_alias("查人", "sniper.add")
    assert not store.add_command_alias("tracking.open", "sniper.add")
    assert not store.set_command_name("sniper.add", "find")
    store.close()


def test_channel_delivery_requires_at_least_one_sniper_config():
    s = _mem_store()
    s.upsert_qq_target(1, 42, enabled=True)
    s.set_target_channel_enabled(1, True)

    assert s.channel_delivery_scope_ids(platforms=()) == []

    config_id = _add(s, group_id=1)
    assert s.channel_delivery_scope_ids(platforms=()) == [1]

    assert s.delete_config(config_id, 1) is True
    assert s.channel_delivery_scope_ids(platforms=()) == []
    s.close()


def test_target_wm_and_channel_blacklists_are_independent():
    s = _mem_store()

    assert s.add_blacklist(1, "SameSeller", scope="wm")
    assert s.add_blacklist(1, "SameSeller", scope="channel")
    assert s.add_blacklist(2, "SameSeller", scope="wm")
    assert s.list_blacklist(1, scope="wm") == ["SameSeller"]
    assert s.list_blacklist(1, scope="channel") == ["SameSeller"]
    assert s.is_blacklisted(1, "sameseller", scope="wm")
    assert s.is_blacklisted(1, "sameseller", scope="channel")
    assert s.blacklisted_scope_ids("sameseller", scope="wm") == {1, 2}
    assert s.blacklisted_scope_ids("sameseller", scope="channel") == {1}

    assert s.remove_blacklist(1, "SameSeller", scope="channel")
    assert s.is_blacklisted(1, "SameSeller", scope="wm")
    assert not s.is_blacklisted(1, "SameSeller", scope="channel")

    # 32 字符限制仅属于命令和 WebUI 入口，不改变 Store 的存量兼容边界。
    legacy_long_name = "x" * 33
    assert s.add_blacklist(1, legacy_long_name, scope="wm")
    assert s.remove_blacklist(1, legacy_long_name, scope="wm")
    s.close()


def test_blacklists_normalize_unicode_spaces_and_match_legacy_rows():
    s = _mem_store()

    assert s.add_blacklist(1, "Example o", scope="wm")
    assert s.is_blacklisted(1, "Example\u00a0o", scope="wm")
    assert s.is_blacklisted(1, "Example\u202fo", scope="wm")
    assert not s.add_blacklist(1, "Example\u00a0o", scope="wm")
    assert s.list_blacklist(1, scope="wm") == ["Example o"]

    s._conn.execute(
        "INSERT INTO channel_blacklist (group_id,seller,created_at) "
        "VALUES (?,?,0)",
        (1, "Example\u00a0o"),
    )
    s._conn.commit()
    assert s.is_blacklisted(1, "Example o", scope="channel")
    assert s.list_blacklist(1, scope="channel") == ["Example o"]
    assert s.remove_blacklist(1, "Example\u202fo", scope="channel")
    assert not s.is_blacklisted(1, "Example o", scope="channel")
    s.close()


def test_pending_player_tracker_normalizes_irc_space_variant():
    s = _mem_store()

    first, created = s.add_player_tracker(1, "Example\u00a0o")
    duplicate, duplicate_created = s.add_player_tracker(1, "Example o")

    assert created is True
    assert (duplicate, duplicate_created) == (first, False)
    assert s.list_player_trackers(1)[0]["target_nick"] == "Example o"
    s.close()


def test_internal_identifiers_are_rejected_at_store_boundaries():
    s = _mem_store()
    account_id = "0123456789abcdef01234567"

    assert not s.add_blacklist(1, f"Alias {account_id}")
    assert not s.remove_blacklist(1, f"Alias {account_id}")
    with pytest.raises(ValueError, match="昵称格式无效"):
        s.add_player_tracker(1, f"Alias {account_id}")

    assert s.list_blacklist(1) == []
    assert s.list_player_trackers(1) == []
    s.close()


def test_config_roundtrip_preserves_or_any_and_zero_rerolls():
    s = _mem_store()
    positives = [
        ["multishot", "critical_chance"],
        [ANY_ATTRIBUTE],
        [ANY_ATTRIBUTE],
    ]
    negatives = [["zoom", "recoil"]]
    config_id = _add(
        s,
        positives=positives,
        negatives=negatives,
        zero_rerolls=True,
    )

    config = s.get_config(config_id, 1)
    assert config["positives"] == groups_as_lists(positives)
    assert config["negatives"] == groups_as_lists(negatives)
    assert config["zero_rerolls"] is True
    assert "creator_qq" not in config
    assert "at_creator" not in config
    assert config["display_number"] == 1
    assert s.list_configs(1) == [config]
    s.close()


def test_config_roundtrip_preserves_per_alternative_minimum_ratings():
    s = _mem_store()
    config_id = _add(
        s,
        positives=[
            ["multishot", "critical_chance"],
            ["fire_rate_/_attack_speed"],
        ],
        positive_ratings=[
            {"multishot": "B+", "critical_chance": "A"},
            {"fire_rate_/_attack_speed": "C+"},
        ],
        negatives=[["zoom", "recoil"]],
        negative_ratings=[{"zoom": "A", "recoil": "F"}],
    )

    config = s.get_config(config_id, 1)
    assert config["positive_ratings"] == [
        {"critical_chance": "A", "multishot": "B+"},
        {"fire_rate_/_attack_speed": "C+"},
    ]
    assert config["negative_ratings"] == [{"recoil": "F", "zoom": "A"}]
    assert json.loads(s._conn.execute(
        "SELECT positive_ratings FROM configs WHERE id=?", (config_id,)
    ).fetchone()[0]) == config["positive_ratings"]
    s.close()


def test_restored_combo_count_curse_roundtrips_through_sqlite():
    s = _mem_store()
    config_id = _add(
        s,
        weapon="skana",
        negatives=[["chance_to_gain_combo_count"]],
    )

    assert s.get_config(config_id, 1)["negatives"] == [
        ["chance_to_gain_combo_count"]
    ]
    raw = s._conn.execute(
        "SELECT negatives FROM configs WHERE id=?", (config_id,)
    ).fetchone()[0]
    assert json.loads(raw) == [["chance_to_gain_combo_count"]]
    s.close()


def test_current_schema_data_survives_reopen(tmp_path):
    db_path = tmp_path / "current.db"
    store = Store(db_path)
    store.upsert_qq_target(1, 42, enabled=True)
    config_id = _add(store, zero_rerolls=True)
    store.set_target_locale(1, "en")
    store.close()

    reopened = Store(db_path)
    assert reopened.get_config(config_id, 1)["zero_rerolls"] is True
    assert reopened.get_target_preferences(1)["locale"] == "en"
    reopened.close()


def test_display_numbers_are_fixed_and_not_reused():
    s = _mem_store()
    first_id = _add(s, weapon="torid")
    _add(s, group_id=2, weapon="boltor")  # 其他群的内部 ID 不影响本群编号
    deleted_id = _add(s, weapon="nami_solo")
    last_id = _add(s, weapon="boltor")

    assert [(c["id"], c["display_number"]) for c in s.list_configs(1)] == [
        (first_id, 1), (deleted_id, 2), (last_id, 3)
    ]
    assert s.delete_config(deleted_id, 1) is True
    assert [(c["id"], c["display_number"]) for c in s.list_configs(1)] == [
        (first_id, 1), (last_id, 3)
    ]
    assert s.get_config(last_id, 1)["display_number"] == 3
    assert s.get_config_by_display_number(2, 1) is None
    assert s.get_config_by_display_number(3, 1)["id"] == last_id

    new_id = _add(s, weapon="nami_solo")
    assert new_id > last_id
    assert s.get_config(new_id, 1)["display_number"] == 4
    assert s.get_config(last_id, 1)["id"] == last_id
    s.close()


def test_platform_target_has_stable_isolated_scope(tmp_path):
    db_path = tmp_path / "target-scopes.db"
    s = Store(db_path)
    alice = s.upsert_discord_target(1001)
    alice_again = s.upsert_discord_target("1001")
    bob = s.upsert_discord_target("1002")

    assert alice["scope_id"] < 0
    assert alice_again["scope_id"] == alice["scope_id"]
    assert bob["scope_id"] != alice["scope_id"]
    assert s.get_target(alice["scope_id"])["external_id"] == "1001"
    assert s.get_target(1) is None

    _add(s, group_id=alice["scope_id"])
    assert len(s.list_configs(alice["scope_id"])) == 1
    assert s.list_configs(bob["scope_id"]) == []

    # 模拟升级前已有目标、尚无持久分配游标的数据库。
    s._conn.execute(
        "DELETE FROM settings_kv WHERE key='discord_scope_next'")
    s._conn.commit()
    s.close()

    s = Store(db_path)
    assert s.delete_target(bob["scope_id"]) is not None
    s.close()

    s = Store(db_path)
    carol = s.upsert_discord_target("1003")
    assert carol["scope_id"] < bob["scope_id"]
    assert carol["scope_id"] != bob["scope_id"]
    s.close()


def test_discord_target_enable_and_delivery_scope_lifecycle():
    s = _mem_store()
    original = s.upsert_discord_target("1001", enabled=False)
    assert original["active"] is False
    enabled = s.upsert_discord_target("1001", enabled=True)
    assert enabled["scope_id"] == original["scope_id"]
    assert enabled["active"] is True
    s.upsert_qq_target(123, 42, enabled=True)
    assert s.active_delivery_scope_ids() == {123, original["scope_id"]}

    s.set_target_enabled(original["scope_id"], False)
    assert s.get_target_by_external("discord", "1001")["active"] is False
    assert s.active_delivery_scope_ids() == {123}
    s.close()


def test_delivery_scopes_can_exclude_disabled_platforms():
    s = _mem_store()
    s.upsert_qq_target(123, 42, enabled=True)
    discord = s.upsert_discord_target("1001")
    other = s.upsert_discord_target("2001")

    assert s.active_delivery_scope_ids(platforms=()) == {123}
    assert s.active_delivery_scope_ids(
        platforms=("discord",)) == {
            123, discord["scope_id"], other["scope_id"]}
    assert s.active_delivery_scope_ids() == {
        123, discord["scope_id"], other["scope_id"]}
    s.close()


def test_channel_and_tracker_scopes_require_current_runtime_targets():
    s = _mem_store()
    s.upsert_qq_target(111, 42, enabled=True)
    s.upsert_qq_target(222, 42, enabled=False)
    discord = s.upsert_discord_target("1001")
    s.set_target_channel_enabled(111, True)
    s.set_target_channel_enabled(222, True)
    s.set_target_channel_enabled(discord["scope_id"], True)
    for scope_id in (111, discord["scope_id"]):
        _add(s, group_id=scope_id)
    account_id = "0123456789abcdef01234567"
    for scope_id in (111, 222, discord["scope_id"]):
        s.add_player_tracker(
            scope_id, "SomePlayer",
            account_id=account_id)

    assert s.channel_delivery_scope_ids(
        platforms=()) == [111]
    assert s.tracker_scope_ids(
        account_id, platforms=()) == [111]
    assert s.channel_delivery_scope_ids(
        platforms=("discord",)) == [discord["scope_id"], 111]
    assert s.tracker_scope_ids(
        account_id, platforms=("discord",)) == [discord["scope_id"], 111]

    s.set_target_enabled(discord["scope_id"], False)
    assert s.channel_delivery_scope_ids(
        platforms=("discord",)) == [111]
    assert s.tracker_scope_ids(
        account_id, platforms=("discord",)) == [111]
    s.close()


def test_discord_target_catalog_filters_disabled_targets():
    s = _mem_store()
    discord = s.upsert_discord_target("1001")
    second = s.upsert_discord_target("2001")
    s.upsert_discord_target("1002", enabled=False)

    active_targets = s.list_targets("discord", active_only=True)
    assert {(target["platform"], target["external_id"])
            for target in active_targets} == {
                ("discord", "1001"), ("discord", "2001")}

    s.set_target_enabled(second["scope_id"], False)
    assert [target["scope_id"]
            for target in s.list_targets("discord", active_only=True)] == [
                discord["scope_id"]]
    assert s.list_targets(
        "discord", active_only=True)[0]["external_id"] == "1001"
    s.close()
