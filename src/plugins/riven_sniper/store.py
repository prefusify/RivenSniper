"""SQLite 持久化：狙击配置、卖家黑名单、已见拍卖去重、捡漏监控。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path

from .command_meta import (
    COMMAND_BY_ID,
    COMMAND_NODES,
    LEGACY_COMMAND_ID_BY_NAME,
    LEGACY_SUBCOMMANDS_BY_PARENT,
    RETIRED_BOT_COMMANDS,
    command_token_conflict,
)
from .criteria import groups_satisfiable, rated_groups_as_lists
from .platform_identity import (
    normalize_player_nick,
    player_nick_lookup_variants,
)
from .privacy import contains_hidden_identifier

DB_PATH = Path(__file__).resolve().parents[3] / "sniper.db"
SCHEMA_VERSION = 2026090801
_MIGRATABLE_SCHEMA_VERSIONS = frozenset({
    20260731, 20260808, 20260809, 20260810, 20260811, 20260812,
    20260813, 20260814, 20260815, 20260908,
})
_PENDING_TRACKER_PREFIX = "pending-nick:"
SUPPORTED_LOCALES = frozenset({"zh", "en"})
TARGET_BLACKLIST_SCOPES = frozenset({"wm", "channel"})
TARGET_NOTE_MAX_LENGTH = 500
TARGET_CHANNEL_DEDUPE_MIN_HOURS = 1
TARGET_CHANNEL_DEDUPE_MAX_HOURS = 72
_DISCORD_SCOPE_NEXT_KEY = "discord_scope_next"


def _choice(value: str, choices: frozenset[str], field: str) -> str:
    value = value.strip().lower()
    if value not in choices:
        raise ValueError(f"invalid {field}: {value}")
    return value


def _decimal_text(value: Decimal | str | int | float) -> str:
    """以十进制定点文本保存价格，禁止先经 SQLite REAL 舍入。"""
    return str(Decimal(str(value)))


def _bargain_level(value: str | None) -> str | None:
    if value not in (None, "0", "max"):
        raise ValueError("bargain item level must be '0', 'max', or null")
    return value


def _target_note(value: object) -> str:
    note = str(value or "").strip()
    if len(note) > TARGET_NOTE_MAX_LENGTH:
        raise ValueError(
            f"目标备注不能超过 {TARGET_NOTE_MAX_LENGTH} 个字符")
    return note


def _target_enabled_until(
    enabled: bool, duration_days: int | None, now: int,
) -> int | None:
    if not enabled:
        if duration_days is not None:
            raise ValueError("停用目标不能设置有效期")
        return None
    if duration_days is None:
        return None
    if isinstance(duration_days, bool) or not isinstance(duration_days, int):
        raise ValueError("限时启用天数必须是整数")
    days = duration_days
    if days <= 0:
        raise ValueError("限时启用天数必须是正整数")
    return now + days * 86400

_SCHEMA = """
CREATE TABLE IF NOT EXISTS configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    display_number INTEGER NOT NULL CHECK (display_number > 0),
    weapon TEXT,             -- 武器 slug；NULL 表示用通配符
    wildcard TEXT,           -- all/rifle/shotgun/pistol/melee/archgun/kitgun/zaw
    positives TEXT NOT NULL DEFAULT '[]',  -- JSON: AND 位置数组；每个位置内数组为 OR
    positive_ratings TEXT NOT NULL DEFAULT '[]', -- JSON: 每个正词条 OR 备选的最低评级
    negatives TEXT NOT NULL DEFAULT '[]',  -- JSON: 负词条位置数组；空数组=必须无负词条
    negative_ratings TEXT NOT NULL DEFAULT '[]', -- JSON: 每个负词条 OR 备选的最低评级
    zero_rerolls INTEGER NOT NULL DEFAULT 0 CHECK (zero_rerolls IN (0, 1)),
    created_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_configs_group_display_number
    ON configs (group_id, display_number);
CREATE TABLE IF NOT EXISTS config_number_sequences (
    group_id INTEGER PRIMARY KEY,
    next_number INTEGER NOT NULL CHECK (next_number > 0)
);
CREATE TABLE IF NOT EXISTS blacklist (
    group_id INTEGER NOT NULL,
    seller TEXT NOT NULL COLLATE NOCASE,   -- WFM 游戏内名字
    created_at INTEGER NOT NULL,
    PRIMARY KEY (group_id, seller)
);
CREATE TABLE IF NOT EXISTS channel_blacklist (
    group_id INTEGER NOT NULL,
    seller TEXT NOT NULL COLLATE NOCASE,   -- 游戏频道昵称
    created_at INTEGER NOT NULL,
    PRIMARY KEY (group_id, seller)
);
CREATE TABLE IF NOT EXISTS seen_auctions (
    id TEXT PRIMARY KEY,
    ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS wm_sniper_notified (
    scope_id INTEGER NOT NULL,
    auction_id TEXT NOT NULL,
    notified_at INTEGER NOT NULL,
    PRIMARY KEY (scope_id, auction_id)
);
CREATE INDEX IF NOT EXISTS idx_wm_sniper_notified_time
    ON wm_sniper_notified (notified_at);
CREATE TABLE IF NOT EXISTS disabled_commands (
    command_id TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS command_names (
    command_id TEXT PRIMARY KEY,
    trigger_name TEXT NOT NULL COLLATE NOCASE UNIQUE
);
CREATE TABLE IF NOT EXISTS command_aliases (
    alias TEXT PRIMARY KEY COLLATE NOCASE,
    command_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings_kv (
    key TEXT PRIMARY KEY,            -- 通用 KV（卡图样式等 JSON 配置）
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS command_usage (
    command_id TEXT NOT NULL,
    day TEXT NOT NULL,           -- YYYY-MM-DD
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (command_id, day)
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    actor TEXT NOT NULL,         -- "webui" / "qq:12345"
    via TEXT NOT NULL,           -- webui / qq
    action TEXT NOT NULL,
    target TEXT,
    detail TEXT
);
-- 统一目标。QQ 群使用正群号作为 scope_id；Discord 私聊使用稳定负数作用域。
CREATE TABLE IF NOT EXISTS targets (
    scope_id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    external_id TEXT NOT NULL,
    owner_qq INTEGER,
    note TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    enabled_until INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE (platform, external_id),
    CHECK (
        (platform='qq' AND scope_id>0 AND owner_qq IS NOT NULL)
        OR (platform<>'qq' AND scope_id<0 AND owner_qq IS NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_targets_active
    ON targets (platform, enabled);
CREATE TABLE IF NOT EXISTS target_preferences (
    scope_id INTEGER PRIMARY KEY,
    locale TEXT NOT NULL DEFAULT 'zh' CHECK (locale IN ('zh', 'en')),
    channel_enabled INTEGER NOT NULL DEFAULT 0 CHECK (channel_enabled IN (0, 1)),
    wm_fast_enabled INTEGER NOT NULL DEFAULT 0 CHECK (wm_fast_enabled IN (0, 1)),
    wm_fast_generation INTEGER NOT NULL DEFAULT 0,
    channel_dedupe_hours INTEGER NOT NULL DEFAULT 1
        CHECK (channel_dedupe_hours BETWEEN 1 AND 72),
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS player_trackers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_id INTEGER NOT NULL,
    account_id TEXT NOT NULL,
    target_nick TEXT NOT NULL COLLATE NOCASE DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at INTEGER NOT NULL,
    UNIQUE (scope_id, account_id)
);
CREATE INDEX IF NOT EXISTS idx_player_trackers_account
    ON player_trackers (account_id, enabled, scope_id);
CREATE TABLE IF NOT EXISTS bargain_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    slug TEXT NOT NULL,              -- WFM 道具 slug
    threshold REAL,                  -- 折扣阈值（0.25=低25%）；NULL=用全局默认
    level TEXT CHECK (level IS NULL OR level IN ('0', 'max')),
    created_at INTEGER NOT NULL,
    UNIQUE (group_id, slug)
);
CREATE TABLE IF NOT EXISTS bargain_riven_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    weapon_slug TEXT NOT NULL,       -- WFM 武器 slug
    threshold REAL,                  -- 折扣阈值（0.35=低35%）；NULL=用全局默认
    created_at INTEGER NOT NULL,
    UNIQUE (group_id, weapon_slug)
);
CREATE TABLE IF NOT EXISTS bargain_item_daily_baselines (
    slug TEXT NOT NULL,
    bucket TEXT NOT NULL DEFAULT '',
    price TEXT NOT NULL,               -- Decimal 字符串，避免 SQLite REAL 精度损失
    source_id TEXT NOT NULL,           -- WM 日桶的稳定标识
    source_datetime TEXT NOT NULL,     -- WM 原始时间，便于审计
    source_ts INTEGER NOT NULL,        -- 可比较的 UTC 时间戳
    volume INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (slug, bucket)
);
CREATE TABLE IF NOT EXISTS bargain_riven_samples (
    weapon_slug TEXT NOT NULL,
    sample_hour INTEGER NOT NULL,      -- UTC 整点 epoch 秒；每武器每小时最多一条
    sampled_at INTEGER NOT NULL,
    price TEXT NOT NULL,               -- Decimal 字符串
    order_count INTEGER NOT NULL,
    spread TEXT NOT NULL,              -- Decimal 字符串
    PRIMARY KEY (weapon_slug, sample_hour)
);
CREATE INDEX IF NOT EXISTS idx_bargain_riven_samples_window
    ON bargain_riven_samples (weapon_slug, sampled_at);
CREATE TABLE IF NOT EXISTS bargain_riven_sample_state (
    weapon_slug TEXT PRIMARY KEY,
    attempt_hour INTEGER NOT NULL,     -- 最近一次已认领的 UTC 整点
    attempted_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS bargain_riven_notified (
    auction_id TEXT PRIMARY KEY,
    notified_at INTEGER NOT NULL       -- 永久保留；同一挂单永不重复提醒
);
CREATE TABLE IF NOT EXISTS bargain_item_seen (
    order_id TEXT PRIMARY KEY,
    seen_at INTEGER NOT NULL           -- WS 事件幂等；价格变化也不产生第二次机会
);
CREATE TABLE IF NOT EXISTS bargain_baro_pauses (
    event_id TEXT NOT NULL,
    slug TEXT NOT NULL,
    bucket TEXT NOT NULL DEFAULT '',
    previous_source_ts INTEGER,        -- 暂停前日桶；NULL 表示当时尚无统计桶
    paused_at INTEGER NOT NULL,
    resumed_at INTEGER,
    PRIMARY KEY (event_id, slug, bucket)
);
CREATE INDEX IF NOT EXISTS idx_bargain_baro_active
    ON bargain_baro_pauses (slug, bucket, resumed_at, paused_at);
-- 已退役的捡漏推送记录表。无业务读写路径；保留结构和数据供 schema 初始化及显式迁移。
CREATE TABLE IF NOT EXISTS bargain_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    group_id INTEGER NOT NULL,
    kind TEXT NOT NULL,              -- item / riven
    slug TEXT NOT NULL,
    bucket TEXT NOT NULL DEFAULT '',
    order_id TEXT NOT NULL,
    price REAL NOT NULL,
    baseline REAL NOT NULL,
    discount REAL NOT NULL,          -- 0.32 = 低32%
    seller TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_bargain_log_ts ON bargain_log (ts);
CREATE INDEX IF NOT EXISTS idx_bargain_log_cd ON bargain_log (group_id, kind, slug, bucket, ts);

-- 已退役网页消息流的历史表。无业务读写路径；保留结构和数据供 schema 初始化及显式迁移。
CREATE TABLE IF NOT EXISTS feed_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT 'free' CHECK (tier IN ('free', 'plus', 'pro')),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    channel_enabled INTEGER NOT NULL DEFAULT 0 CHECK (channel_enabled IN (0, 1)),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS feed_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES feed_users(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feed_sessions_user ON feed_sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_feed_sessions_expiry ON feed_sessions (expires_at);
CREATE TABLE IF NOT EXISTS feed_user_settings (
    user_id INTEGER PRIMARY KEY REFERENCES feed_users(id) ON DELETE CASCADE,
    wm_blacklist_applies_bargain INTEGER NOT NULL DEFAULT 0
        CHECK (wm_blacklist_applies_bargain IN (0, 1)),
    locale TEXT NOT NULL DEFAULT 'zh' CHECK (locale IN ('zh', 'en')),
    theme TEXT NOT NULL DEFAULT 'dark' CHECK (theme IN ('dark', 'light')),
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS feed_filter_attributes (
    user_id INTEGER NOT NULL REFERENCES feed_users(id) ON DELETE CASCADE,
    attribute_slug TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, attribute_slug)
);
CREATE TABLE IF NOT EXISTS feed_seller_blacklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES feed_users(id) ON DELETE CASCADE,
    scope TEXT NOT NULL CHECK (scope IN ('wm', 'channel')),
    seller TEXT NOT NULL COLLATE NOCASE,
    created_at INTEGER NOT NULL,
    UNIQUE (user_id, scope, seller)
);
CREATE TABLE IF NOT EXISTS feed_channel_filter_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES feed_users(id) ON DELETE CASCADE,
    servers TEXT NOT NULL,
    channel_types TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feed_channel_filter_rules_user
    ON feed_channel_filter_rules (user_id, created_at, id);
CREATE TABLE IF NOT EXISTS feed_bargain_settings (
    user_id INTEGER PRIMARY KEY REFERENCES feed_users(id) ON DELETE CASCADE,
    items_enabled INTEGER NOT NULL DEFAULT 0 CHECK (items_enabled IN (0, 1)),
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS feed_bargain_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES feed_users(id) ON DELETE CASCADE,
    slug TEXT NOT NULL,
    threshold REAL,
    level TEXT CHECK (level IS NULL OR level IN ('0', 'max')),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE (user_id, slug)
);
CREATE INDEX IF NOT EXISTS idx_feed_bargain_items_user
    ON feed_bargain_items (user_id, created_at, id);
CREATE TABLE IF NOT EXISTS feed_bargain_riven_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES feed_users(id) ON DELETE CASCADE,
    weapon_slug TEXT NOT NULL,
    threshold REAL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE (user_id, weapon_slug)
);
CREATE INDEX IF NOT EXISTS idx_feed_bargain_riven_items_user
    ON feed_bargain_riven_items (user_id, created_at, id);
"""


class Store:
    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path) if str(path) != ":memory:" else None
        self.migration_backup_path: Path | None = None
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._migrate_schema()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._ensure_discord_scope_sequence()
        self._purge_retired_command_data()
        self._ensure_command_names()
        self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._conn.commit()

    def _migrate_schema(self) -> None:
        """按已知版本迁移；未知非空 schema 拒绝启动且不修改数据。"""
        tables = [
            str(row[0]) for row in self._conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if not tables or version == SCHEMA_VERSION:
            return
        if version not in _MIGRATABLE_SCHEMA_VERSIONS:
            self._conn.close()
            raise RuntimeError(
                f"不支持的 sniper.db schema 版本 {version}；"
                f"当前版本为 {SCHEMA_VERSION}，数据库未被修改")
        self._backup_before_migration(version)
        if version == 20260731:
            self._migrate_from_20260731()
            version = 20260808
        if version == 20260808:
            self._migrate_command_model()
            version = 20260809
        if version == 20260809:
            self._migrate_riven_bargain_always_on()
            version = 20260810
        if version == 20260810:
            self._migrate_target_management()
            version = 20260811
        if version == 20260811:
            self._migrate_fixed_config_display_numbers()
            version = 20260812
        if version == 20260812:
            self._migrate_target_channel_dedupe()
            version = 20260813
        if version == 20260813:
            self._migrate_editable_command_names()
            version = 20260814
        if version == 20260814:
            self._migrate_minimum_ratings()
            version = 20260815
        if version == 20260815:
            self._migrate_wm_fast()
            version = 20260908
        if version == 20260908:
            self._migrate_code_managed_texts()

    def _migrate_code_managed_texts(self) -> None:
        """文案已归入 JSON；迁移前的数据库备份保留旧覆盖内容。"""
        try:
            self._conn.executescript(f"""
                BEGIN IMMEDIATE;
                DROP TABLE IF EXISTS command_texts;
                PRAGMA user_version={SCHEMA_VERSION};
                COMMIT;
            """)
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def _migrate_wm_fast(self) -> None:
        """新增关闭的快速偏好与目标级去重；普通轮询的已见记录保持原语义。"""
        columns = {str(row[1]) for row in self._conn.execute(
            "PRAGMA table_info(target_preferences)")}
        additions = []
        if columns and "wm_fast_enabled" not in columns:
            additions.append("ALTER TABLE target_preferences ADD COLUMN wm_fast_enabled "
                             "INTEGER NOT NULL DEFAULT 0 CHECK (wm_fast_enabled IN (0, 1));")
        if columns and "wm_fast_generation" not in columns:
            additions.append("ALTER TABLE target_preferences ADD COLUMN wm_fast_generation "
                             "INTEGER NOT NULL DEFAULT 0;")
        try:
            self._conn.executescript(f"""
                BEGIN IMMEDIATE;
                {''.join(additions)}
                CREATE TABLE IF NOT EXISTS wm_sniper_notified (
                    scope_id INTEGER NOT NULL,
                    auction_id TEXT NOT NULL,
                    notified_at INTEGER NOT NULL,
                    PRIMARY KEY (scope_id, auction_id)
                );
                CREATE INDEX IF NOT EXISTS idx_wm_sniper_notified_time
                    ON wm_sniper_notified (notified_at);
                PRAGMA user_version=20260908;
                COMMIT;
            """)
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def _backup_before_migration(self, version: int) -> None:
        if self.path is None:
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_path = self.path.with_name(
            f"{self.path.name}.v{version}.{stamp}.bak")
        suffix = 1
        while backup_path.exists():
            backup_path = self.path.with_name(
                f"{self.path.name}.v{version}.{stamp}.{suffix}.bak")
            suffix += 1
        backup = sqlite3.connect(str(backup_path))
        try:
            self._conn.backup(backup)
        finally:
            backup.close()
        self.migration_backup_path = backup_path

    def _migrate_from_20260731(self) -> None:
        """删除旧 QQ 目标配置，并无损迁移 Discord 与全局数据。"""
        script = f"""
        PRAGMA foreign_keys=OFF;
        BEGIN IMMEDIATE;

        ALTER TABLE configs RENAME TO legacy_configs;
        CREATE TABLE configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            weapon TEXT,
            wildcard TEXT,
            positives TEXT NOT NULL DEFAULT '[]',
            negatives TEXT NOT NULL DEFAULT '[]',
            zero_rerolls INTEGER NOT NULL DEFAULT 0 CHECK (zero_rerolls IN (0, 1)),
            created_at INTEGER NOT NULL
        );
        INSERT INTO configs
            (id,group_id,weapon,wildcard,positives,negatives,zero_rerolls,created_at)
        SELECT id,group_id,weapon,wildcard,positives,negatives,zero_rerolls,created_at
        FROM legacy_configs WHERE group_id<0;

        ALTER TABLE blacklist RENAME TO legacy_blacklist;
        CREATE TABLE blacklist (
            group_id INTEGER NOT NULL,
            seller TEXT NOT NULL COLLATE NOCASE,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (group_id, seller)
        );
        INSERT INTO blacklist (group_id,seller,created_at)
        SELECT group_id,seller,created_at FROM legacy_blacklist WHERE group_id<0;

        ALTER TABLE channel_blacklist RENAME TO legacy_channel_blacklist;
        CREATE TABLE channel_blacklist (
            group_id INTEGER NOT NULL,
            seller TEXT NOT NULL COLLATE NOCASE,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (group_id, seller)
        );
        INSERT INTO channel_blacklist (group_id,seller,created_at)
        SELECT group_id,seller,created_at
        FROM legacy_channel_blacklist WHERE group_id<0;

        ALTER TABLE disabled_commands RENAME TO legacy_disabled_commands;
        CREATE TABLE disabled_commands (
            command TEXT PRIMARY KEY,
            created_at INTEGER NOT NULL
        );
        INSERT OR IGNORE INTO disabled_commands (command,created_at)
        SELECT command,MIN(created_at) FROM legacy_disabled_commands
        WHERE group_id=0 GROUP BY command;

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
        SELECT -id,platform,external_id,NULL,authorized,created_at,updated_at
        FROM platform_targets;

        ALTER TABLE target_preferences RENAME TO legacy_target_preferences;
        CREATE TABLE target_preferences (
            scope_id INTEGER PRIMARY KEY,
            locale TEXT NOT NULL DEFAULT 'zh' CHECK (locale IN ('zh', 'en')),
            channel_enabled INTEGER NOT NULL DEFAULT 0 CHECK (channel_enabled IN (0, 1)),
            updated_at INTEGER NOT NULL
        );
        INSERT INTO target_preferences (scope_id,locale,channel_enabled,updated_at)
        SELECT scope_id,locale,channel_enabled,updated_at
        FROM legacy_target_preferences WHERE scope_id<0;

        ALTER TABLE player_trackers RENAME TO legacy_player_trackers;
        CREATE TABLE player_trackers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_id INTEGER NOT NULL,
            account_id TEXT NOT NULL,
            target_nick TEXT NOT NULL COLLATE NOCASE DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
            created_at INTEGER NOT NULL,
            UNIQUE (scope_id, account_id)
        );
        INSERT INTO player_trackers
            (id,scope_id,account_id,target_nick,enabled,created_at)
        SELECT id,scope_id,account_id,target_nick,enabled,created_at
        FROM legacy_player_trackers WHERE scope_id<0;

        ALTER TABLE bargain_items RENAME TO legacy_bargain_items;
        CREATE TABLE bargain_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            slug TEXT NOT NULL,
            threshold REAL,
            level TEXT CHECK (level IS NULL OR level IN ('0', 'max')),
            created_at INTEGER NOT NULL,
            UNIQUE (group_id, slug)
        );
        INSERT INTO bargain_items (id,group_id,slug,threshold,level,created_at)
        SELECT id,group_id,slug,threshold,level,created_at
        FROM legacy_bargain_items WHERE group_id<0;

        ALTER TABLE bargain_riven_items RENAME TO legacy_bargain_riven_items;
        CREATE TABLE bargain_riven_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            weapon_slug TEXT NOT NULL,
            threshold REAL,
            created_at INTEGER NOT NULL,
            UNIQUE (group_id, weapon_slug)
        );
        INSERT INTO bargain_riven_items
            (id,group_id,weapon_slug,threshold,created_at)
        SELECT id,group_id,weapon_slug,threshold,created_at
        FROM legacy_bargain_riven_items WHERE group_id<0;

        DROP TABLE bargain_groups;

        DROP TABLE legacy_configs;
        DROP TABLE legacy_blacklist;
        DROP TABLE legacy_channel_blacklist;
        DROP TABLE legacy_disabled_commands;
        DROP TABLE legacy_target_preferences;
        DROP TABLE legacy_player_trackers;
        DROP TABLE legacy_bargain_items;
        DROP TABLE legacy_bargain_riven_items;
        DROP TABLE platform_targets;
        DROP TABLE command_permissions;
        DROP TABLE member_permissions;

        PRAGMA user_version=20260808;
        COMMIT;
        PRAGMA foreign_keys=ON;
        """
        try:
            self._conn.executescript(script)
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            self._conn.execute("PRAGMA foreign_keys=ON")
            raise

    def _migrate_command_model(self) -> None:
        """命令配置改用固定内部 ID；旧自定义命令名明确不迁移。"""
        disabled = self._conn.execute(
            "SELECT command, created_at FROM disabled_commands"
        ).fetchall()
        aliases = self._conn.execute(
            "SELECT alias, command FROM command_aliases"
        ).fetchall()
        usage = self._conn.execute(
            "SELECT command, day, count FROM command_usage"
        ).fetchall()
        try:
            self._conn.executescript("""
                BEGIN IMMEDIATE;
                ALTER TABLE disabled_commands RENAME TO legacy_disabled_commands_v2;
                CREATE TABLE disabled_commands (
                    command_id TEXT PRIMARY KEY,
                    created_at INTEGER NOT NULL
                );
                ALTER TABLE command_aliases RENAME TO legacy_command_aliases_v2;
                CREATE TABLE command_aliases (
                    parent_id TEXT NOT NULL DEFAULT '',
                    alias TEXT NOT NULL COLLATE NOCASE,
                    command_id TEXT NOT NULL,
                    PRIMARY KEY (parent_id, alias)
                );
                ALTER TABLE command_usage RENAME TO legacy_command_usage_v2;
                CREATE TABLE command_usage (
                    command_id TEXT NOT NULL,
                    day TEXT NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (command_id, day)
                );
            """)
            for row in disabled:
                command_id = LEGACY_COMMAND_ID_BY_NAME.get(row["command"])
                if command_id:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO disabled_commands "
                        "(command_id,created_at) VALUES (?,?)",
                        (command_id, row["created_at"]),
                    )
            for row in aliases:
                command_id = LEGACY_COMMAND_ID_BY_NAME.get(row["command"])
                if command_id:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO command_aliases "
                        "(parent_id,alias,command_id) VALUES ('',?,?)",
                        (row["alias"], command_id),
                    )
            for row in usage:
                command_id = LEGACY_COMMAND_ID_BY_NAME.get(row["command"])
                if command_id:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO command_usage "
                        "(command_id,day,count) VALUES (?,?,?)",
                        (command_id, row["day"], row["count"]),
                    )
            self._conn.execute("DROP TABLE legacy_disabled_commands_v2")
            self._conn.execute("DROP TABLE legacy_command_aliases_v2")
            self._conn.execute("DROP TABLE legacy_command_usage_v2")
            self._conn.execute("DROP TABLE command_renames")
            self._conn.execute("PRAGMA user_version=20260809")
            self._conn.commit()
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def _migrate_riven_bargain_always_on(self) -> None:
        """丢弃紫卡捡漏专属开关，保留所有监控条目和阈值。"""
        feed_settings_columns = {
            row[1] for row in self._conn.execute(
                "PRAGMA table_info(feed_bargain_settings)"
            )
        }
        feed_riven_columns = {
            row[1] for row in self._conn.execute(
                "PRAGMA table_info(feed_bargain_riven_items)"
            )
        }
        feed_settings_migration = ""
        if "riven_enabled" in feed_settings_columns:
            feed_settings_migration = """
                ALTER TABLE feed_bargain_settings
                    RENAME TO legacy_feed_bargain_settings;
                CREATE TABLE feed_bargain_settings (
                    user_id INTEGER PRIMARY KEY REFERENCES feed_users(id)
                        ON DELETE CASCADE,
                    items_enabled INTEGER NOT NULL DEFAULT 0
                        CHECK (items_enabled IN (0, 1)),
                    updated_at INTEGER NOT NULL
                );
                INSERT INTO feed_bargain_settings
                    (user_id,items_enabled,updated_at)
                SELECT user_id,items_enabled,updated_at
                FROM legacy_feed_bargain_settings;
                DROP TABLE legacy_feed_bargain_settings;
            """
        feed_riven_migration = ""
        if "enabled" in feed_riven_columns:
            feed_riven_migration = """
                DROP INDEX IF EXISTS idx_feed_bargain_riven_items_user;
                ALTER TABLE feed_bargain_riven_items
                    RENAME TO legacy_feed_bargain_riven_items;
                CREATE TABLE feed_bargain_riven_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES feed_users(id)
                        ON DELETE CASCADE,
                    weapon_slug TEXT NOT NULL,
                    threshold REAL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE (user_id, weapon_slug)
                );
                INSERT INTO feed_bargain_riven_items
                    (id,user_id,weapon_slug,threshold,created_at,updated_at)
                SELECT id,user_id,weapon_slug,threshold,created_at,updated_at
                FROM legacy_feed_bargain_riven_items;
                DROP TABLE legacy_feed_bargain_riven_items;
                CREATE INDEX idx_feed_bargain_riven_items_user
                    ON feed_bargain_riven_items (user_id, created_at, id);
            """
        script = f"""
            BEGIN IMMEDIATE;
            ALTER TABLE bargain_riven_items
                RENAME TO legacy_bargain_riven_items_always_on;
            CREATE TABLE bargain_riven_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                weapon_slug TEXT NOT NULL,
                threshold REAL,
                created_at INTEGER NOT NULL,
                UNIQUE (group_id, weapon_slug)
            );
            INSERT INTO bargain_riven_items
                (id,group_id,weapon_slug,threshold,created_at)
            SELECT id,group_id,weapon_slug,threshold,created_at
            FROM legacy_bargain_riven_items_always_on;
            DROP TABLE legacy_bargain_riven_items_always_on;
            DROP TABLE IF EXISTS bargain_groups;
            {feed_settings_migration}
            {feed_riven_migration}
            PRAGMA user_version=20260810;
            COMMIT;
        """
        try:
            self._conn.executescript(script)
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def _migrate_target_management(self) -> None:
        """为统一目标增加备注和可选的限时启用期限。"""
        columns = {
            str(row[1]) for row in self._conn.execute(
                "PRAGMA table_info(targets)")
        }
        note_migration = (
            "" if "note" in columns
            else "ALTER TABLE targets ADD COLUMN note TEXT NOT NULL DEFAULT '';"
        )
        expiry_migration = (
            "" if "enabled_until" in columns
            else "ALTER TABLE targets ADD COLUMN enabled_until INTEGER;"
        )
        try:
            self._conn.executescript(f"""
                BEGIN IMMEDIATE;
                {note_migration}
                {expiry_migration}
                PRAGMA user_version=20260811;
                COMMIT;
            """)
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def _migrate_fixed_config_display_numbers(self) -> None:
        """按目标内现有创建顺序固化狙击配置展示编号。"""
        columns = {
            str(row[1]) for row in self._conn.execute(
                "PRAGMA table_info(configs)")
        }
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            if "display_number" not in columns:
                self._conn.execute(
                    "ALTER TABLE configs ADD COLUMN display_number INTEGER "
                    "NOT NULL DEFAULT 1 CHECK (display_number > 0)")
                counters: dict[int, int] = {}
                rows = self._conn.execute(
                    "SELECT id,group_id FROM configs ORDER BY group_id,id"
                ).fetchall()
                for row in rows:
                    group_id = int(row["group_id"])
                    number = counters.get(group_id, 0) + 1
                    counters[group_id] = number
                    self._conn.execute(
                        "UPDATE configs SET display_number=? WHERE id=?",
                        (number, int(row["id"])),
                    )
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "idx_configs_group_display_number "
                "ON configs (group_id, display_number)")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS config_number_sequences ("
                "group_id INTEGER PRIMARY KEY, "
                "next_number INTEGER NOT NULL CHECK (next_number > 0))")
            self._conn.execute(
                "INSERT OR IGNORE INTO config_number_sequences "
                "(group_id,next_number) "
                "SELECT group_id,MAX(display_number)+1 FROM configs "
                "GROUP BY group_id")
            self._conn.execute("PRAGMA user_version=20260812")
            self._conn.commit()
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def _migrate_target_channel_dedupe(self) -> None:
        """为每个目标增加 1～72 小时的频道紫卡去重窗口。"""
        columns = {
            str(row[1]) for row in self._conn.execute(
                "PRAGMA table_info(target_preferences)")
        }
        column_migration = (
            "" if "channel_dedupe_hours" in columns else
            "ALTER TABLE target_preferences "
            "ADD COLUMN channel_dedupe_hours INTEGER NOT NULL DEFAULT 1 "
            "CHECK (channel_dedupe_hours BETWEEN 1 AND 72);"
        )
        try:
            self._conn.executescript(f"""
                BEGIN IMMEDIATE;
                {column_migration}
                PRAGMA user_version=20260813;
                COMMIT;
            """)
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def _migrate_editable_command_names(self) -> None:
        """持久化可编辑触发名，并把子命令别名提升为全局命令别名。"""
        alias_columns = {
            str(row[1]) for row in self._conn.execute(
                "PRAGMA table_info(command_aliases)")
        }
        # 已是新表形态时只补齐版本；用于显式迁移链的幂等执行。
        if "parent_id" not in alias_columns:
            self._conn.executescript(f"""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS command_names (
                    command_id TEXT PRIMARY KEY,
                    trigger_name TEXT NOT NULL COLLATE NOCASE UNIQUE
                );
                PRAGMA user_version=20260814;
                COMMIT;
            """)
            return

        rows = self._conn.execute(
            "SELECT alias,command_id FROM command_aliases"
        ).fetchall()
        names = {node.id: node.default_name for node in COMMAND_NODES}
        candidates: list[tuple[str, str, str]] = []
        for row in rows:
            command_id = str(row["command_id"])
            alias = str(row["alias"]).strip()
            folded = alias.casefold()
            if command_id not in COMMAND_BY_ID or not alias:
                continue
            if any(character.isspace() for character in alias):
                continue
            if command_token_conflict(command_id, alias, names, {}):
                continue
            candidates.append((command_id, alias, folded))
        duplicate_aliases = {
            folded for _command_id, _alias, folded in candidates
            if sum(1 for candidate in candidates if candidate[2] == folded) > 1
        }
        kept = [
            (alias, command_id)
            for command_id, alias, folded in candidates
            if folded not in duplicate_aliases
        ]

        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute("""
                CREATE TABLE command_names (
                    command_id TEXT PRIMARY KEY,
                    trigger_name TEXT NOT NULL COLLATE NOCASE UNIQUE
                )
            """)
            self._conn.executemany(
                "INSERT INTO command_names (command_id,trigger_name) VALUES (?,?)",
                ((node.id, node.default_name) for node in COMMAND_NODES),
            )
            self._conn.execute(
                "ALTER TABLE command_aliases RENAME TO legacy_command_aliases_v3")
            self._conn.execute("""
                CREATE TABLE command_aliases (
                    alias TEXT PRIMARY KEY COLLATE NOCASE,
                    command_id TEXT NOT NULL
                )
            """)
            self._conn.executemany(
                "INSERT INTO command_aliases (alias,command_id) VALUES (?,?)",
                kept,
            )
            # 旧子命令执行前只检查父命令开关；父命令已停用时，升级后拆出的
            # 独立入口也必须保持停用，避免升级瞬间重新开放原有功能。
            for parent_id, child_ids in LEGACY_SUBCOMMANDS_BY_PARENT.items():
                self._conn.executemany(
                    "INSERT OR IGNORE INTO disabled_commands "
                    "(command_id,created_at) "
                    "SELECT ?,created_at FROM disabled_commands "
                    "WHERE command_id=?",
                    ((child_id, parent_id) for child_id in child_ids),
                )
            self._conn.execute("DROP TABLE legacy_command_aliases_v3")
            self._conn.execute("PRAGMA user_version=20260814")
            self._conn.commit()
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def _migrate_minimum_ratings(self) -> None:
        """为旧狙击配置增加正负词条最低评级映射；旧行默认不限制。"""
        config_table_exists = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='configs'"
        ).fetchone() is not None
        columns = ({
            str(row[1]) for row in self._conn.execute(
                "PRAGMA table_info(configs)")
        } if config_table_exists else set())
        statements = []
        if config_table_exists and "positive_ratings" not in columns:
            statements.append(
                "ALTER TABLE configs ADD COLUMN positive_ratings TEXT "
                "NOT NULL DEFAULT '[]';")
        if config_table_exists and "negative_ratings" not in columns:
            statements.append(
                "ALTER TABLE configs ADD COLUMN negative_ratings TEXT "
                "NOT NULL DEFAULT '[]';")
        try:
            self._conn.executescript("\n".join((
                "BEGIN IMMEDIATE;",
                *statements,
                "PRAGMA user_version=20260815;",
                "COMMIT;",
            )))
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def _purge_retired_command_data(self) -> None:
        command_ids = sorted(COMMAND_BY_ID)
        placeholders = ",".join("?" for _ in command_ids)
        self._conn.execute(
            f"DELETE FROM disabled_commands WHERE command_id NOT IN ({placeholders})",
            command_ids,
        )
        self._conn.execute(
            f"DELETE FROM command_usage WHERE command_id NOT IN ({placeholders})",
            command_ids,
        )
        self._conn.execute(
            f"DELETE FROM command_aliases WHERE command_id NOT IN ({placeholders})",
            command_ids,
        )
        self._conn.execute(
            f"DELETE FROM command_names WHERE command_id NOT IN ({placeholders})",
            command_ids,
        )
        retired = sorted(RETIRED_BOT_COMMANDS)
        retired_placeholders = ",".join("?" for _ in retired)
        self._conn.execute(
            f"DELETE FROM command_aliases "
            f"WHERE alias IN ({retired_placeholders})",
            retired,
        )

    def _ensure_command_names(self) -> None:
        existing = {
            str(row["command_id"])
            for row in self._conn.execute(
                "SELECT command_id FROM command_names")
        }
        self._conn.executemany(
            "INSERT INTO command_names (command_id,trigger_name) VALUES (?,?)",
            (
                (node.id, node.default_name)
                for node in COMMAND_NODES
                if node.id not in existing
            ),
        )

    def _ensure_discord_scope_sequence(self) -> None:
        """从现有目标补种单调游标，删除目标后不再复用内部作用域。"""
        self._conn.execute(
            """INSERT OR IGNORE INTO settings_kv (key,value)
               SELECT ?,CAST(COALESCE(MIN(scope_id),0)-1 AS TEXT)
               FROM targets WHERE scope_id<0""",
            (_DISCORD_SCOPE_NEXT_KEY,),
        )

    # ---- 狙击配置 ----

    @staticmethod
    def _decode_config(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["positives"], data["positive_ratings"] = rated_groups_as_lists(
            json.loads(data["positives"] or "[]"),
            json.loads(data["positive_ratings"] or "[]"),
        )
        data["negatives"], data["negative_ratings"] = rated_groups_as_lists(
            json.loads(data["negatives"] or "[]"),
            json.loads(data["negative_ratings"] or "[]"),
        )
        data["zero_rerolls"] = bool(data["zero_rerolls"])
        return data

    def add_config(self, group_id: int, *, weapon: str | None,
                   wildcard: str | None, positives: list[list[str]],
                   positive_ratings: list[dict[str, str]] | None = None,
                   negatives: list[list[str]] | None = None,
                   negative_ratings: list[dict[str, str]] | None = None,
                   zero_rerolls: bool = False) -> int:
        positives, positive_ratings = rated_groups_as_lists(
            positives, positive_ratings)
        negatives, negative_ratings = rated_groups_as_lists(
            negatives, negative_ratings)
        if bool(weapon) == bool(wildcard):
            raise ValueError("狙击配置必须且只能指定具体武器或一种武器范围")
        if len(positives) not in (2, 3) or not groups_satisfiable(positives):
            raise ValueError("狙击配置必须包含 2 至 3 个可分别匹配的正词条位置")
        if len(negatives) > 1:
            raise ValueError("狙击配置最多包含一个负词条位置")
        with self._conn:
            display_number = self._conn.execute(
                """INSERT INTO config_number_sequences (group_id,next_number)
                   VALUES (?,2)
                   ON CONFLICT(group_id) DO UPDATE
                   SET next_number=next_number+1
                   RETURNING next_number-1""",
                (group_id,),
            ).fetchone()[0]
            cur = self._conn.execute(
                """INSERT INTO configs
                   (group_id, display_number, weapon, wildcard, positives,
                     positive_ratings, negatives, negative_ratings,
                     zero_rerolls, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (group_id, display_number, weapon, wildcard,
                  json.dumps(positives), json.dumps(positive_ratings),
                  json.dumps(negatives), json.dumps(negative_ratings),
                  int(zero_rerolls), int(time.time())),
            )
        return cur.lastrowid

    def list_configs(self, group_id: int | None = None) -> list[dict]:
        sql = "SELECT * FROM configs"
        args: tuple = ()
        if group_id is not None:
            sql += " WHERE group_id=?"
            args = (group_id,)
        sql += " ORDER BY id"
        rows = self._conn.execute(sql, args).fetchall()
        return [self._decode_config(row) for row in rows]

    def get_config(self, config_id: int, group_id: int) -> dict | None:
        r = self._conn.execute(
            "SELECT * FROM configs WHERE id=? AND group_id=?", (config_id, group_id)
        ).fetchone()
        if not r:
            return None
        return self._decode_config(r)

    def get_config_by_display_number(self, display_number: int,
                                     group_id: int) -> dict | None:
        """按目标内固定展示编号取配置；内部 ID 始终保持不变。"""
        if display_number < 1:
            return None
        row = self._conn.execute(
            "SELECT * FROM configs WHERE group_id=? AND display_number=?",
            (group_id, display_number),
        ).fetchone()
        if not row:
            return None
        return self._decode_config(row)

    def delete_config(self, config_id: int, group_id: int) -> bool:
        cur = self._conn.execute(
            "DELETE FROM configs WHERE id=? AND group_id=?", (config_id, group_id)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def count_configs(self, group_id: int) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM configs WHERE group_id=?", (group_id,)
        ).fetchone()[0]

    # ---- 目标级 WM / 频道卖家黑名单 ----
    @staticmethod
    def _target_blacklist_table(scope: str) -> str:
        scope = _choice(
            scope, TARGET_BLACKLIST_SCOPES, "target blacklist scope")
        return "blacklist" if scope == "wm" else "channel_blacklist"

    def add_blacklist(self, group_id: int, seller: str,
                      scope: str = "wm") -> bool:
        table = self._target_blacklist_table(scope)
        seller = normalize_player_nick(seller)
        if not seller or contains_hidden_identifier(seller):
            return False
        variants = player_nick_lookup_variants(seller)
        placeholders = ",".join("?" for _ in variants)
        if self._conn.execute(
            f"SELECT 1 FROM {table} WHERE group_id=? "
            f"AND seller IN ({placeholders})",
            (group_id, *variants),
        ).fetchone() is not None:
            return False
        try:
            self._conn.execute(
                f"INSERT INTO {table} "
                "(group_id, seller, created_at) VALUES (?,?,?)",
                (group_id, seller, int(time.time())),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def remove_blacklist(self, group_id: int, seller: str,
                         scope: str = "wm") -> bool:
        table = self._target_blacklist_table(scope)
        seller = normalize_player_nick(seller)
        if not seller or contains_hidden_identifier(seller):
            return False
        variants = player_nick_lookup_variants(seller)
        placeholders = ",".join("?" for _ in variants)
        cur = self._conn.execute(
            f"DELETE FROM {table} WHERE group_id=? "
            f"AND seller IN ({placeholders})",
            (group_id, *variants),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def list_blacklist(self, group_id: int, scope: str = "wm") -> list[str]:
        table = self._target_blacklist_table(scope)
        rows = self._conn.execute(
            f"SELECT seller FROM {table} "
            "WHERE group_id=? ORDER BY created_at, rowid",
            (group_id,),
        ).fetchall()
        sellers: list[str] = []
        seen: set[str] = set()
        for row in rows:
            seller = normalize_player_nick(row["seller"])
            key = seller.casefold()
            if seller and key not in seen:
                sellers.append(seller)
                seen.add(key)
        return sellers

    def is_blacklisted(self, group_id: int, seller: str,
                       scope: str = "wm") -> bool:
        table = self._target_blacklist_table(scope)
        variants = player_nick_lookup_variants(seller)
        if not variants:
            return False
        placeholders = ",".join("?" for _ in variants)
        return self._conn.execute(
            f"SELECT 1 FROM {table} WHERE group_id=? "
            f"AND seller IN ({placeholders})",
            (group_id, *variants),
        ).fetchone() is not None

    def blacklisted_scope_ids(
            self, seller: str, scope: str = "wm") -> set[int]:
        """一次查询返回拉黑该卖家的全部目标，供批量匹配路径复用。"""
        table = self._target_blacklist_table(scope)
        variants = player_nick_lookup_variants(seller)
        if not variants:
            return set()
        placeholders = ",".join("?" for _ in variants)
        return {
            int(row["group_id"])
            for row in self._conn.execute(
                f"SELECT DISTINCT group_id FROM {table} "
                f"WHERE seller IN ({placeholders})",
                variants,
            ).fetchall()
        }

    def commit(self):
        self._conn.commit()

    # ---- 命令开关与调用统计 ----
    def disable_command(self, command_id: str):
        if command_id not in COMMAND_BY_ID:
            raise ValueError(f"unknown command id: {command_id}")
        self._conn.execute(
            "INSERT OR IGNORE INTO disabled_commands (command_id, created_at) "
            "VALUES (?,?)", (command_id, int(time.time())))
        self._conn.commit()

    def enable_command(self, command_id: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM disabled_commands WHERE command_id=?", (command_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def is_command_disabled(self, command_id: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM disabled_commands WHERE command_id=?", (command_id,)
        ).fetchone() is not None

    def list_disabled_commands(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT command_id FROM disabled_commands ORDER BY command_id"
        ).fetchall()
        return [str(r["command_id"]) for r in rows]

    def list_command_names(self) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT command_id,trigger_name FROM command_names "
            "ORDER BY command_id"
        ).fetchall()
        return {
            str(row["command_id"]): str(row["trigger_name"])
            for row in rows
        }

    def set_command_name(self, command_id: str, trigger_name: str) -> bool:
        trigger_name = trigger_name.strip()
        if command_id not in COMMAND_BY_ID or not trigger_name:
            return False
        if any(character.isspace() for character in trigger_name):
            return False
        names = self.list_command_names()
        if command_id not in names or command_token_conflict(
            command_id,
            trigger_name,
            names,
            self.list_command_aliases(),
            skip_name=True,
        ):
            return False
        try:
            cur = self._conn.execute(
                "UPDATE command_names SET trigger_name=? WHERE command_id=?",
                (trigger_name, command_id),
            )
            self._conn.commit()
            return cur.rowcount > 0
        except sqlite3.IntegrityError:
            return False

    def add_command_alias(self, alias: str, command_id: str) -> bool:
        alias = alias.strip()
        if command_id not in COMMAND_BY_ID or not alias:
            return False
        if any(character.isspace() for character in alias):
            return False
        saved = self.list_command_aliases()
        if command_token_conflict(
            command_id, alias, self.list_command_names(), saved,
        ):
            return False
        try:
            self._conn.execute(
                "INSERT INTO command_aliases (alias,command_id) VALUES (?,?)",
                (alias, command_id))
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def rename_command_alias(
        self, command_id: str, alias: str, new_alias: str,
    ) -> bool:
        new_alias = new_alias.strip()
        if command_id not in COMMAND_BY_ID or not new_alias:
            return False
        if any(character.isspace() for character in new_alias):
            return False
        saved = self.list_command_aliases()
        if alias not in saved.get(command_id, []):
            return False
        if new_alias.casefold() != alias.casefold() and command_token_conflict(
            command_id,
            new_alias,
            self.list_command_names(),
            saved,
            skip_alias=alias,
        ):
            return False
        try:
            cur = self._conn.execute(
                "UPDATE command_aliases SET alias=? "
                "WHERE command_id=? AND alias=?",
                (new_alias, command_id, alias),
            )
            self._conn.commit()
            return cur.rowcount > 0
        except sqlite3.IntegrityError:
            return False

    def remove_command_alias(self, command_id: str, alias: str) -> bool:
        if command_id not in COMMAND_BY_ID:
            return False
        cur = self._conn.execute(
            "DELETE FROM command_aliases "
            "WHERE command_id=? AND alias=?",
            (command_id, alias),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def list_command_aliases(self) -> dict[str, list[str]]:
        """返回 command_id -> 别名列表；别名在全部命令间全局唯一。"""
        rows = self._conn.execute(
            "SELECT command_id,alias FROM command_aliases "
            "ORDER BY command_id,alias COLLATE NOCASE"
        ).fetchall()
        result: dict[str, list[str]] = {}
        for row in rows:
            result.setdefault(row["command_id"], []).append(row["alias"])
        return result

    def kv_get(self, key: str) -> str | None:
        r = self._conn.execute(
            "SELECT value FROM settings_kv WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def kv_set(self, key: str, value: str):
        self._conn.execute(
            """INSERT INTO settings_kv (key, value) VALUES (?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (key, value))
        self._conn.commit()

    def record_command_usage(self, command_id: str):
        day = time.strftime("%Y-%m-%d")
        self._conn.execute(
            """INSERT INTO command_usage (command_id, day, count) VALUES (?,?,1)
               ON CONFLICT(command_id, day) DO UPDATE SET count=count+1""",
            (command_id, day))
        self._conn.commit()

    def command_usage_7d(self) -> dict[str, int]:
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - 6 * 86400))
        rows = self._conn.execute(
            "SELECT command_id, SUM(count) AS n FROM command_usage WHERE day >= ? "
            "GROUP BY command_id", (cutoff,)).fetchall()
        return {r["command_id"]: r["n"] for r in rows}

    # ---- 单所有者 QQ 群与 Discord 私聊目标 ----

    @staticmethod
    def _decode_target(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        enabled_until = result.get("enabled_until")
        result["active"] = (
            result["enabled"]
            and (enabled_until is None or int(enabled_until) > int(time.time()))
        )
        return result

    def expire_targets(self, *, now: int | None = None) -> list[int]:
        """停用已到期目标，并为每次自动停用保留系统审计记录。"""
        if self._conn.in_transaction:
            return []
        current = int(time.time()) if now is None else int(now)
        rows = self._conn.execute(
            """SELECT scope_id,platform,external_id,enabled_until
               FROM targets
               WHERE enabled=1 AND enabled_until IS NOT NULL
                 AND enabled_until<=?
               ORDER BY scope_id""",
            (current,),
        ).fetchall()
        if not rows:
            return []
        expired: list[int] = []
        with self._conn:
            for row in rows:
                cursor = self._conn.execute(
                    """UPDATE targets SET enabled=0,updated_at=?
                       WHERE scope_id=? AND enabled=1
                         AND enabled_until IS NOT NULL
                         AND enabled_until<=?""",
                    (current, int(row["scope_id"]), current),
                )
                if not cursor.rowcount:
                    continue
                label = (
                    f"QQ群 {row['external_id']}"
                    if row["platform"] == "qq"
                    else f"{str(row['platform']).upper()} 用户 "
                         f"{row['external_id']}"
                )
                self._conn.execute(
                    """INSERT INTO audit_log
                       (ts,actor,via,action,target,detail)
                       VALUES (?,'system','system','target_expired',?,?)""",
                    (current, label,
                     f"enabled_until={int(row['enabled_until'])}"),
                )
                expired.append(int(row["scope_id"]))
        return expired

    def get_target(self, scope_id: int) -> dict | None:
        self.expire_targets()
        row = self._conn.execute(
            """SELECT scope_id,platform,external_id,owner_qq,note,enabled,
                      enabled_until,created_at,updated_at
               FROM targets WHERE scope_id=?""", (int(scope_id),),
        ).fetchone()
        return self._decode_target(row)

    def get_target_by_external(
            self, platform: str, external_id: str | int) -> dict | None:
        self.expire_targets()
        row = self._conn.execute(
            """SELECT scope_id,platform,external_id,owner_qq,note,enabled,
                      enabled_until,created_at,updated_at
               FROM targets WHERE platform=? AND external_id=?""",
            (str(platform).strip().lower(), str(external_id).strip()),
        ).fetchone()
        return self._decode_target(row)

    def upsert_qq_target(
            self, group_id: int, owner_qq: int, *, enabled: bool = True,
            duration_days: int | None = None,
            note: str | None = None) -> dict:
        group_id = int(group_id)
        owner_qq = int(owner_qq)
        if group_id <= 0 or owner_qq <= 0:
            raise ValueError("QQ群号和所有者 QQ 必须是正整数")
        now = int(time.time())
        existing = self.get_target(group_id)
        stored_note = (
            existing["note"] if note is None and existing is not None
            else _target_note(note)
        )
        enabled_until = _target_enabled_until(
            bool(enabled), duration_days, now)
        self._conn.execute(
            """INSERT INTO targets
                   (scope_id,platform,external_id,owner_qq,note,enabled,
                    enabled_until,created_at,updated_at)
               VALUES (?,'qq',?,?,?,?,?,?,?)
               ON CONFLICT(scope_id) DO UPDATE SET
                   owner_qq=excluded.owner_qq,
                   note=excluded.note,
                   enabled=excluded.enabled,
                   enabled_until=excluded.enabled_until,
                   updated_at=excluded.updated_at""",
            (group_id, str(group_id), owner_qq, stored_note,
             int(bool(enabled)), enabled_until, now, now),
        )
        self._conn.commit()
        self.get_target_preferences(group_id)
        return self.get_target(group_id)

    def upsert_discord_target(
            self, external_id: str | int, *, enabled: bool = True,
            duration_days: int | None = None,
            note: str | None = None) -> dict:
        """创建或更新 Discord 私聊目标；外部用户即目标所有者。"""
        platform = "discord"
        external_id = str(external_id).strip()
        if not external_id:
            raise ValueError("Discord 用户 ID 不能为空")
        target = self.get_target_by_external(platform, external_id)
        if target is not None:
            if note is not None:
                target = self.set_target_note(target["scope_id"], note)
            target = self.set_target_enabled(
                target["scope_id"], enabled, duration_days=duration_days)
            return target
        now = int(time.time())
        stored_note = _target_note(note)
        enabled_until = _target_enabled_until(
            bool(enabled), duration_days, now)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            target = self.get_target_by_external(platform, external_id)
            if target is None:
                scope_id = int(self._conn.execute(
                    "SELECT value FROM settings_kv WHERE key=?",
                    (_DISCORD_SCOPE_NEXT_KEY,),
                ).fetchone()["value"])
                self._conn.execute(
                    """INSERT INTO settings_kv (key,value) VALUES (?,?)
                       ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                    (_DISCORD_SCOPE_NEXT_KEY, str(scope_id - 1)),
                )
                self._conn.execute(
                    """INSERT INTO targets
                           (scope_id,platform,external_id,note,enabled,
                            enabled_until,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (scope_id, platform, external_id, stored_note,
                     int(bool(enabled)), enabled_until, now, now),
                )
            self._conn.execute("COMMIT")
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise
        target = self.get_target_by_external(platform, external_id)
        if target is None:  # pragma: no cover
            raise RuntimeError("failed to create platform target")
        self.get_target_preferences(target["scope_id"])
        return target

    def set_target_enabled(
            self, scope_id: int, enabled: bool, *,
            duration_days: int | None = None) -> dict | None:
        now = int(time.time())
        enabled_until = _target_enabled_until(
            bool(enabled), duration_days, now)
        cursor = self._conn.execute(
            """UPDATE targets
               SET enabled=?,enabled_until=?,updated_at=?
               WHERE scope_id=?""",
            (int(bool(enabled)), enabled_until, now, int(scope_id)),
        )
        self._conn.commit()
        return self.get_target(scope_id) if cursor.rowcount else None

    def renew_target(
            self, scope_id: int, duration_days: int) -> dict | None:
        """从现有到期时间续期；已到期目标从当前时间重新起算并启用。"""
        target = self.get_target(scope_id)
        if target is None:
            return None
        current_until = target["enabled_until"]
        if current_until is None:
            raise ValueError("目标没有可续期的限时有效期")
        now = int(time.time())
        enabled_until = _target_enabled_until(
            True, duration_days, max(now, int(current_until)))
        self._conn.execute(
            """UPDATE targets
               SET enabled=1,enabled_until=?,updated_at=?
               WHERE scope_id=?""",
            (enabled_until, now, int(scope_id)),
        )
        self._conn.commit()
        return self.get_target(scope_id)

    def set_target_note(self, scope_id: int, note: str) -> dict | None:
        cursor = self._conn.execute(
            "UPDATE targets SET note=?,updated_at=? WHERE scope_id=?",
            (_target_note(note), int(time.time()), int(scope_id)),
        )
        self._conn.commit()
        return self.get_target(scope_id) if cursor.rowcount else None

    def set_target_owner(self, scope_id: int, owner_qq: int) -> dict | None:
        owner_qq = int(owner_qq)
        target = self.get_target(scope_id)
        if target is None:
            return None
        if target["platform"] != "qq" or owner_qq <= 0:
            raise ValueError("只有 QQ 群目标可以设置正整数所有者 QQ")
        self._conn.execute(
            "UPDATE targets SET owner_qq=?,updated_at=? WHERE scope_id=?",
            (owner_qq, int(time.time()), int(scope_id)),
        )
        self._conn.commit()
        return self.get_target(scope_id)

    def delete_target(self, scope_id: int) -> dict | None:
        """原子删除目标及主库内全部目标专属数据，保留共享数据和审计。"""
        scope_id = int(scope_id)
        target = self.get_target(scope_id)
        if target is None:
            return None
        owned_tables = {
            "configs": "group_id",
            "blacklist": "group_id",
            "channel_blacklist": "group_id",
            "target_preferences": "scope_id",
            "wm_sniper_notified": "scope_id",
            "player_trackers": "scope_id",
            "bargain_items": "group_id",
            "bargain_riven_items": "group_id",
        }
        removed: dict[str, int] = {}
        with self._conn:
            for table, column in owned_tables.items():
                cursor = self._conn.execute(
                    f"DELETE FROM {table} WHERE {column}=?", (scope_id,))
                removed[table] = int(cursor.rowcount)
            self._conn.execute(
                "DELETE FROM config_number_sequences WHERE group_id=?",
                (scope_id,),
            )
            cursor = self._conn.execute(
                "DELETE FROM targets WHERE scope_id=?", (scope_id,))
            if not cursor.rowcount:
                raise RuntimeError("目标删除期间发生并发变化")
        return {"target": target, "removed": removed}

    def list_targets(self, platform: str | None = None, *,
                     active_only: bool = False) -> list[dict]:
        self.expire_targets()
        sql = (
            "SELECT scope_id,platform,external_id,owner_qq,note,enabled,"
            "enabled_until,created_at,updated_at FROM targets WHERE 1=1")
        args: list[object] = []
        if platform is not None:
            sql += " AND platform=?"
            args.append(str(platform).strip().lower())
        if active_only:
            sql += " AND enabled=1"
        rows = self._conn.execute(
            sql + " ORDER BY platform,created_at,scope_id", args,
        ).fetchall()
        return [self._decode_target(row) for row in rows]

    def active_delivery_scope_ids(
            self, *, platforms: Iterable[str] | None = None) -> set[int]:
        allowed_platforms = (
            {"discord"} if platforms is None else
            {str(value).strip().lower() for value in platforms})
        return {
            int(target["scope_id"])
            for target in self.list_targets(active_only=True)
            if (target["platform"] == "qq"
                or target["platform"] in allowed_platforms)
        }

    # ---- 推送目标语言与频道消息开关 ----

    def bootstrap_target_preferences(self, scope_ids: Iterable[int]) -> None:
        """为已知目标建立默认中文、频道关闭的偏好。"""
        scopes = {int(value) for value in scope_ids}
        now = int(time.time())
        with self._conn:
            for scope_id in sorted(scopes):
                self._conn.execute(
                    """INSERT OR IGNORE INTO target_preferences
                       (scope_id,locale,channel_enabled,updated_at)
                       VALUES (?,'zh',0,?)""", (scope_id, now))

    def get_target_preferences(self, scope_id: int) -> dict:
        scope_id = int(scope_id)
        now = int(time.time())
        self._conn.execute(
            """INSERT OR IGNORE INTO target_preferences
               (scope_id,locale,channel_enabled,updated_at) VALUES (?,'zh',0,?)""",
            (scope_id, now),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT scope_id,locale,channel_enabled,channel_dedupe_hours,"
            "wm_fast_enabled,wm_fast_generation,updated_at "
            "FROM target_preferences WHERE scope_id=?", (scope_id,),
        ).fetchone()
        return {**dict(row), "channel_enabled": bool(row["channel_enabled"]),
                "wm_fast_enabled": bool(row["wm_fast_enabled"])}

    def set_target_wm_fast_enabled(self, scope_id: int, enabled: bool) -> dict:
        self.get_target_preferences(scope_id)
        with self._conn:
            self._conn.execute(
                "UPDATE target_preferences SET wm_fast_enabled=?,"
                "wm_fast_generation=wm_fast_generation+1,updated_at=? "
                "WHERE scope_id=? AND wm_fast_enabled<>?",
                (int(enabled), int(time.time()), int(scope_id), int(enabled)))
        return self.get_target_preferences(scope_id)

    def wm_fast_configs(self, *, discord_enabled: bool = False) -> list[dict]:
        rows = self._conn.execute(
            "SELECT configs.*,preferences.wm_fast_generation "
            "FROM configs JOIN targets ON targets.scope_id=configs.group_id "
            "JOIN target_preferences AS preferences "
            "ON preferences.scope_id=targets.scope_id "
            "WHERE targets.enabled=1 AND preferences.wm_fast_enabled=1 "
            "AND (targets.enabled_until IS NULL OR targets.enabled_until>?) "
            "AND (targets.platform='qq' OR (? AND targets.platform='discord')) "
            "ORDER BY configs.id", (int(time.time()), int(discord_enabled)))
        return [self._decode_config(row) for row in rows]

    def claim_wm_notifications(self, scope_id: int, auction_ids: list[str]) -> set[str]:
        """在统一入队前认领目标/挂单；两个获取入口使用相同的 30 天窗口。"""
        claimed = set()
        with self._conn:
            for aid in auction_ids:
                result = self._conn.execute(
                    "INSERT OR IGNORE INTO wm_sniper_notified VALUES (?,?,?)",
                    (scope_id, aid, int(time.time())))
                if result.rowcount:
                    claimed.add(aid)
        return claimed

    def release_wm_notifications(self, scope_id: int, auction_ids: list[str]) -> None:
        with self._conn:
            self._conn.executemany(
                "DELETE FROM wm_sniper_notified WHERE scope_id=? AND auction_id=?",
                ((scope_id, aid) for aid in auction_ids))

    def set_target_locale(self, scope_id: int, locale: str) -> dict:
        locale = _choice(locale, SUPPORTED_LOCALES, "locale")
        self.get_target_preferences(scope_id)
        self._conn.execute(
            "UPDATE target_preferences SET locale=?,updated_at=? WHERE scope_id=?",
            (locale, int(time.time()), int(scope_id)),
        )
        self._conn.commit()
        return self.get_target_preferences(scope_id)

    def set_target_channel_enabled(self, scope_id: int, enabled: bool) -> dict:
        self.get_target_preferences(scope_id)
        self._conn.execute(
            "UPDATE target_preferences SET channel_enabled=?,updated_at=? WHERE scope_id=?",
            (int(bool(enabled)), int(time.time()), int(scope_id)),
        )
        self._conn.commit()
        return self.get_target_preferences(scope_id)

    def set_target_channel_dedupe_hours(
            self, scope_id: int, hours: int) -> dict:
        if isinstance(hours, bool) or not isinstance(hours, int) or not (
                TARGET_CHANNEL_DEDUPE_MIN_HOURS
                <= hours <= TARGET_CHANNEL_DEDUPE_MAX_HOURS):
            raise ValueError("频道去重时间需为 1～72 小时的整数")
        self.get_target_preferences(scope_id)
        self._conn.execute(
            "UPDATE target_preferences "
            "SET channel_dedupe_hours=?,updated_at=? WHERE scope_id=?",
            (hours, int(time.time()), int(scope_id)),
        )
        self._conn.commit()
        return self.get_target_preferences(scope_id)

    def channel_delivery_scope_ids(
            self, *, platforms: Iterable[str] | None = None,
    ) -> list[int]:
        """返回已开启频道消息、至少有一条狙击配置且当前仍可投递的目标。"""
        rows = self._conn.execute(
            """SELECT scope_id FROM target_preferences AS preferences
               WHERE channel_enabled=1
                 AND EXISTS (
                     SELECT 1 FROM configs
                     WHERE configs.group_id=preferences.scope_id
                 )
               """
            "ORDER BY scope_id"
        ).fetchall()
        active = self.active_delivery_scope_ids(platforms=platforms)
        return [int(row["scope_id"]) for row in rows
                if int(row["scope_id"]) in active]

    # ---- 玩家上线提醒 ----

    @staticmethod
    def _pending_tracker_keys(nick: str) -> tuple[str, ...]:
        return tuple(
            _PENDING_TRACKER_PREFIX + hashlib.sha256(
                variant.casefold().encode("utf-8")
            ).hexdigest()
            for variant in player_nick_lookup_variants(nick)
        )

    def add_player_tracker(
        self, scope_id: int, target_nick: str, *,
        account_id: str | None = None,
    ) -> tuple[int, bool]:
        target_nick = normalize_player_nick(target_nick)
        if not target_nick or contains_hidden_identifier(target_nick):
            raise ValueError("上线提醒昵称格式无效")
        account_key = str(account_id or "").strip().lower()
        pending_keys = self._pending_tracker_keys(target_nick)
        pending_key = pending_keys[0]
        if not account_key:
            account_key = pending_key
        now = int(time.time())
        with self._conn:
            placeholders = ",".join("?" for _ in pending_keys)
            if account_id:
                pending = self._conn.execute(
                    "SELECT id FROM player_trackers WHERE scope_id=? "
                    f"AND account_id IN ({placeholders}) ORDER BY id",
                    (int(scope_id), *pending_keys),
                ).fetchall()
                existing = self._conn.execute(
                    "SELECT id FROM player_trackers "
                    "WHERE scope_id=? AND account_id=?",
                    (int(scope_id), account_key),
                ).fetchone()
                if pending:
                    if existing is None:
                        winner = int(pending[0]["id"])
                        self._conn.execute(
                            "UPDATE player_trackers "
                            "SET account_id=?,target_nick=? WHERE id=?",
                            (account_key, target_nick, winner),
                        )
                        self._conn.executemany(
                            "DELETE FROM player_trackers WHERE id=?",
                            [(int(row["id"]),) for row in pending[1:]],
                        )
                        return winner, False
                    self._conn.executemany(
                        "DELETE FROM player_trackers WHERE id=?",
                        [(int(row["id"]),) for row in pending],
                    )
            else:
                pending = self._conn.execute(
                    "SELECT id FROM player_trackers WHERE scope_id=? "
                    f"AND account_id IN ({placeholders}) ORDER BY id",
                    (int(scope_id), *pending_keys),
                ).fetchall()
                if pending:
                    winner = int(pending[0]["id"])
                    self._conn.execute(
                        "UPDATE player_trackers SET target_nick=? WHERE id=?",
                        (target_nick, winner),
                    )
                    self._conn.executemany(
                        "DELETE FROM player_trackers WHERE id=?",
                        [(int(row["id"]),) for row in pending[1:]],
                    )
                    return winner, False
            cursor = self._conn.execute(
                """INSERT OR IGNORE INTO player_trackers
                   (scope_id,account_id,target_nick,enabled,created_at)
                   VALUES (?,?,?,1,?)""",
                (int(scope_id), account_key, target_nick, now),
            )
            row = self._conn.execute(
                "SELECT id FROM player_trackers WHERE scope_id=? AND account_id=?",
                (int(scope_id), account_key),
            ).fetchone()
            if not cursor.rowcount:
                self._conn.execute(
                    "UPDATE player_trackers SET target_nick=? WHERE id=?",
                    (target_nick, int(row["id"])),
                )
        return int(row["id"]), bool(cursor.rowcount)

    def list_player_trackers(
        self, scope_id: int | None = None, *, account_id: str | None = None,
    ) -> list[dict]:
        sql = ("SELECT id,scope_id,account_id,target_nick,enabled,"
               "created_at FROM player_trackers WHERE 1=1")
        args: list[object] = []
        if scope_id is not None:
            sql += " AND scope_id=?"
            args.append(int(scope_id))
        if account_id is not None:
            sql += " AND account_id=?"
            args.append(str(account_id).strip().lower())
        rows = self._conn.execute(sql + " ORDER BY id", args).fetchall()
        return [{
            **dict(row),
            "target_nick": normalize_player_nick(row["target_nick"]),
            "enabled": bool(row["enabled"]),
            "resolved": not str(row["account_id"]).startswith(
                _PENDING_TRACKER_PREFIX),
        } for row in rows]

    def delete_player_tracker(
        self, tracker_id: int, scope_id: int,
    ) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM player_trackers WHERE id=? AND scope_id=?",
            (int(tracker_id), int(scope_id)),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def tracker_scope_ids(
        self, account_id: str, *, platforms: Iterable[str] | None = None,
        nick: str = "",
    ) -> list[int]:
        """返回追踪该玩家且当前仍有效的投递作用域。"""
        account_id = str(account_id).strip().lower()
        nick = normalize_player_nick(nick)
        if nick:
            pending_keys = self._pending_tracker_keys(nick)
            placeholders = ",".join("?" for _ in pending_keys)
            with self._conn:
                pending = self._conn.execute(
                    "SELECT id,scope_id FROM player_trackers "
                    f"WHERE account_id IN ({placeholders}) "
                    "AND enabled=1 ORDER BY id",
                    pending_keys,
                ).fetchall()
                for row in pending:
                    existing = self._conn.execute(
                        "SELECT id FROM player_trackers "
                        "WHERE scope_id=? AND account_id=?",
                        (int(row["scope_id"]), account_id),
                    ).fetchone()
                    if existing is None:
                        self._conn.execute(
                            "UPDATE player_trackers "
                            "SET account_id=?,target_nick=? WHERE id=?",
                            (account_id, nick, int(row["id"])),
                        )
                    else:
                        self._conn.execute(
                            "DELETE FROM player_trackers WHERE id=?",
                            (int(row["id"]),),
                        )
                self._conn.execute(
                    "UPDATE player_trackers SET target_nick=? "
                    "WHERE account_id=? AND enabled=1",
                    (nick, account_id),
                )
        rows = self._conn.execute(
            "SELECT scope_id FROM player_trackers WHERE account_id=? AND enabled=1 "
            "ORDER BY scope_id", (account_id,),
        ).fetchall()
        active = self.active_delivery_scope_ids(platforms=platforms)
        channel_enabled = {
            int(row["scope_id"])
            for row in self._conn.execute(
                "SELECT scope_id FROM target_preferences WHERE channel_enabled=1"
            ).fetchall()
        }
        return [int(row["scope_id"]) for row in rows
                if int(row["scope_id"]) in active
                and int(row["scope_id"]) in channel_enabled]

    # ---- 审计 ----
    def add_audit(self, actor: str, via: str, action: str,
                  target: str = "", detail: str = ""):
        self._conn.execute(
            "INSERT INTO audit_log (ts, actor, via, action, target, detail) "
            "VALUES (?,?,?,?,?,?)",
            (int(time.time()), actor, via, action, target, detail))
        self._conn.commit()

    def list_audit(self, n: int = 100) -> list[dict]:
        rows = self._conn.execute(
            "SELECT ts, actor, via, action, target, detail FROM audit_log "
            "ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [dict(r) for r in rows]

    # ---- 去重 ----
    def mark_seen(self, auction_ids: list[str]) -> list[str]:
        """标记拍卖为已见，返回其中此前未见过的 id。"""
        now = int(time.time())
        fresh = []
        for aid in auction_ids:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO seen_auctions (id, ts) VALUES (?,?)", (aid, now)
            )
            if cur.rowcount:
                fresh.append(aid)
        self._conn.commit()
        return fresh

    # 清理窗口 30 天：WFM 卖家可"顶帖"让老听单重回最新列表，
    # 窗口太短会把顶帖的老听单当新听单重复推送
    def prune_seen(self, max_age_seconds: int = 30 * 86400) -> int:
        self._conn.execute(
            "DELETE FROM wm_sniper_notified WHERE notified_at < ?",
            (int(time.time()) - max_age_seconds,))
        cur = self._conn.execute(
            "DELETE FROM seen_auctions WHERE ts < ?", (int(time.time()) - max_age_seconds,)
        )
        self._conn.commit()
        return cur.rowcount

    # ---- 捡漏：道具监控条目 ----
    def add_bargain_item(self, group_id: int, slug: str,
                         threshold: float | None = None,
                         level: str | None = None) -> int | None:
        """添加监控条目；同群同道具重复返回 None。"""
        level = _bargain_level(level)
        try:
            cur = self._conn.execute(
                """INSERT INTO bargain_items
                   (group_id,slug,threshold,level,created_at) VALUES (?,?,?,?,?)""",
                (group_id, slug, threshold, level, int(time.time())))
            self._conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    def list_bargain_items(self, group_id: int | None = None) -> list[dict]:
        sql = "SELECT * FROM bargain_items"
        args: tuple = ()
        if group_id is not None:
            sql += " WHERE group_id=?"
            args = (group_id,)
        rows = self._conn.execute(sql + " ORDER BY id", args).fetchall()
        return [dict(r) for r in rows]

    def get_bargain_item(self, item_id: int, group_id: int) -> dict | None:
        r = self._conn.execute(
            "SELECT * FROM bargain_items WHERE id=? AND group_id=?",
            (item_id, group_id)).fetchone()
        return dict(r) if r else None

    def delete_bargain_item(self, item_id: int, group_id: int) -> bool:
        cur = self._conn.execute(
            "DELETE FROM bargain_items WHERE id=? AND group_id=?",
            (item_id, group_id))
        self._conn.commit()
        return cur.rowcount > 0

    def set_bargain_item_threshold(self, item_id: int, group_id: int,
                                   threshold: float | None) -> bool:
        cur = self._conn.execute(
            "UPDATE bargain_items SET threshold=? WHERE id=? AND group_id=?",
            (threshold, item_id, group_id))
        self._conn.commit()
        return cur.rowcount > 0

    def set_bargain_item_level(self, item_id: int, group_id: int,
                               level: str | None) -> bool:
        level = _bargain_level(level)
        cur = self._conn.execute(
            "UPDATE bargain_items SET level=? WHERE id=? AND group_id=?",
            (level, item_id, group_id))
        self._conn.commit()
        return cur.rowcount > 0

    # ---- 捡漏：紫卡监控条目（独立清单，镜像道具条目）----
    def add_bargain_riven_item(self, group_id: int, weapon_slug: str,
                               threshold: float | None = None) -> int | None:
        """添加紫卡监控武器；同群同武器重复返回 None。"""
        try:
            cur = self._conn.execute(
                """INSERT INTO bargain_riven_items
                   (group_id,weapon_slug,threshold,created_at) VALUES (?,?,?,?)""",
                (group_id, weapon_slug, threshold, int(time.time())))
            self._conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    def list_bargain_riven_items(self, group_id: int | None = None) -> list[dict]:
        sql = "SELECT * FROM bargain_riven_items"
        args: tuple = ()
        if group_id is not None:
            sql += " WHERE group_id=?"
            args = (group_id,)
        rows = self._conn.execute(sql + " ORDER BY id", args).fetchall()
        return [dict(r) for r in rows]

    def get_bargain_riven_item(self, item_id: int, group_id: int) -> dict | None:
        r = self._conn.execute(
            "SELECT * FROM bargain_riven_items WHERE id=? AND group_id=?",
            (item_id, group_id)).fetchone()
        return dict(r) if r else None

    def delete_bargain_riven_item(self, item_id: int, group_id: int) -> bool:
        cur = self._conn.execute(
            "DELETE FROM bargain_riven_items WHERE id=? AND group_id=?",
            (item_id, group_id))
        self._conn.commit()
        return cur.rowcount > 0

    def set_bargain_riven_item_threshold(
            self, item_id: int, group_id: int,
            threshold: float | None) -> bool:
        cur = self._conn.execute(
            "UPDATE bargain_riven_items SET threshold=? WHERE id=? AND group_id=?",
            (threshold, item_id, group_id))
        self._conn.commit()
        return cur.rowcount > 0

    @staticmethod
    def _bargain_distinct_slugs_sql() -> str:
        """两类 Bot 捡漏配置共用的全局 slug 集合。"""
        return """
            SELECT slug FROM bargain_items
            UNION SELECT weapon_slug AS slug FROM bargain_riven_items
        """

    def count_bargain_distinct_slugs(self) -> int:
        row = self._conn.execute(
            f"SELECT COUNT(*) FROM ({self._bargain_distinct_slugs_sql()})"
        ).fetchone()
        return int(row[0])

    def has_bargain_slug(self, slug: str) -> bool:
        row = self._conn.execute(
            f"SELECT 1 FROM ({self._bargain_distinct_slugs_sql()}) "
            "WHERE slug=? LIMIT 1", (slug,)).fetchone()
        return row is not None

    # ---- 捡漏：WM 普通道具日桶基准 ----
    @staticmethod
    def _decode_item_daily_baseline(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        result["price"] = Decimal(result["price"])
        return result

    def upsert_bargain_item_daily_baseline(
            self, slug: str, bucket: str,
            price: Decimal | str | int | float, *, source_id: str,
            source_datetime: str, source_ts: int, volume: int = 0,
            updated_at: int | None = None) -> bool:
        """写入 WM 最新日桶；乱序到达的更旧日桶不会覆盖当前基准。"""
        cur = self._conn.execute(
            """INSERT INTO bargain_item_daily_baselines
                   (slug, bucket, price, source_id, source_datetime, source_ts,
                    volume, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(slug, bucket) DO UPDATE SET
                   price=excluded.price,
                   source_id=excluded.source_id,
                   source_datetime=excluded.source_datetime,
                   source_ts=excluded.source_ts,
                   volume=excluded.volume,
                   updated_at=excluded.updated_at
               WHERE excluded.source_ts >= bargain_item_daily_baselines.source_ts""",
            (slug, bucket, _decimal_text(price), source_id, source_datetime,
             int(source_ts), int(volume),
             int(time.time()) if updated_at is None else int(updated_at)))
        self._conn.commit()
        return cur.rowcount > 0

    def get_bargain_item_daily_baseline(
            self, slug: str, bucket: str = "") -> dict | None:
        row = self._conn.execute(
            """SELECT slug, bucket, price, source_id, source_datetime,
                      source_ts, volume, updated_at
               FROM bargain_item_daily_baselines
               WHERE slug=? AND bucket=?""",
            (slug, bucket)).fetchone()
        return self._decode_item_daily_baseline(row)

    def list_bargain_item_daily_baselines(
            self, slug: str | None = None) -> list[dict]:
        sql = (
            "SELECT slug, bucket, price, source_id, source_datetime, "
            "source_ts, volume, updated_at FROM bargain_item_daily_baselines")
        args: tuple = ()
        if slug is not None:
            sql += " WHERE slug=?"
            args = (slug,)
        rows = self._conn.execute(
            sql + " ORDER BY slug, bucket", args).fetchall()
        return [self._decode_item_daily_baseline(row) for row in rows]

    # ---- 捡漏：紫卡小时样本与滚动窗口 ----
    @staticmethod
    def bargain_utc_hour(ts: int | float | None = None) -> int:
        value = time.time() if ts is None else ts
        return int(value) // 3600 * 3600

    def claim_bargain_riven_sample_hour(
            self, weapon_slug: str, sample_hour: int | None = None,
            attempted_at: int | None = None) -> bool:
        """原子认领一个 UTC 小时；同武器同小时只有首次调用返回 True。"""
        attempted_at = int(time.time()) if attempted_at is None else int(attempted_at)
        sample_hour = self.bargain_utc_hour(
            attempted_at if sample_hour is None else sample_hour)
        cur = self._conn.execute(
            """INSERT INTO bargain_riven_sample_state
                   (weapon_slug, attempt_hour, attempted_at) VALUES (?,?,?)
               ON CONFLICT(weapon_slug) DO UPDATE SET
                   attempt_hour=excluded.attempt_hour,
                   attempted_at=excluded.attempted_at
               WHERE excluded.attempt_hour > bargain_riven_sample_state.attempt_hour""",
            (weapon_slug, sample_hour, attempted_at))
        self._conn.commit()
        return cur.rowcount > 0

    def get_bargain_riven_sample_attempt(
            self, weapon_slug: str) -> dict | None:
        row = self._conn.execute(
            """SELECT weapon_slug, attempt_hour, attempted_at
               FROM bargain_riven_sample_state WHERE weapon_slug=?""",
            (weapon_slug,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _decode_riven_sample(row: sqlite3.Row) -> dict:
        result = dict(row)
        result["price"] = Decimal(result["price"])
        result["spread"] = Decimal(result["spread"])
        return result

    def add_bargain_riven_sample(
            self, weapon_slug: str,
            price: Decimal | str | int | float, order_count: int,
            spread: Decimal | str | int | float, *,
            sampled_at: int | None = None,
            sample_hour: int | None = None) -> bool:
        """记录一个有效地板样本；每武器每 UTC 小时只保留一条。"""
        sampled_at = int(time.time()) if sampled_at is None else int(sampled_at)
        sample_hour = self.bargain_utc_hour(
            sampled_at if sample_hour is None else sample_hour)
        cur = self._conn.execute(
            """INSERT OR IGNORE INTO bargain_riven_samples
                   (weapon_slug, sample_hour, sampled_at, price, order_count, spread)
               VALUES (?,?,?,?,?,?)""",
            (weapon_slug, sample_hour, sampled_at, _decimal_text(price),
             int(order_count), _decimal_text(spread)))
        self._conn.commit()
        return cur.rowcount > 0

    def list_bargain_riven_samples(
            self, weapon_slug: str, since_ts: int = 0,
            until_ts: int | None = None) -> list[dict]:
        sql = (
            "SELECT weapon_slug, sample_hour, sampled_at, price, order_count, "
            "spread FROM bargain_riven_samples "
            "WHERE weapon_slug=? AND sampled_at>=?")
        args: list = [weapon_slug, int(since_ts)]
        if until_ts is not None:
            sql += " AND sampled_at<=?"
            args.append(int(until_ts))
        rows = self._conn.execute(
            sql + " ORDER BY sampled_at, sample_hour", args).fetchall()
        return [self._decode_riven_sample(row) for row in rows]

    # ---- 捡漏：候选订单幂等 ----
    def mark_bargain_riven_notified(
            self, auction_id: str, notified_at: int | None = None) -> bool:
        """永久登记已成功推送的紫卡挂单；首次登记返回 True。"""
        cur = self._conn.execute(
            """INSERT OR IGNORE INTO bargain_riven_notified
                   (auction_id, notified_at) VALUES (?,?)""",
            (auction_id,
             int(time.time()) if notified_at is None else int(notified_at)))
        self._conn.commit()
        return cur.rowcount > 0

    def is_bargain_riven_notified(self, auction_id: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM bargain_riven_notified WHERE auction_id=?",
            (auction_id,)).fetchone() is not None

    def mark_bargain_item_order_seen(
            self, order_id: str, seen_at: int | None = None) -> bool:
        """登记普通道具 WS 新建事件；同订单价格变化也不会再次触发。"""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO bargain_item_seen (order_id, seen_at) VALUES (?,?)",
            (order_id, int(time.time()) if seen_at is None else int(seen_at)))
        self._conn.commit()
        return cur.rowcount > 0

    def mark_bargain_item_orders_seen(
            self, order_ids: list[str], seen_at: int | None = None) -> list[str]:
        now = int(time.time()) if seen_at is None else int(seen_at)
        fresh: list[str] = []
        for order_id in order_ids:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO bargain_item_seen
                       (order_id, seen_at) VALUES (?,?)""",
                (order_id, now))
            if cur.rowcount:
                fresh.append(order_id)
        self._conn.commit()
        return fresh

    def is_bargain_item_order_seen(self, order_id: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM bargain_item_seen WHERE order_id=?",
            (order_id,)).fetchone() is not None

    # ---- 捡漏：虚空商人暂停 ----
    def add_bargain_baro_pause(
            self, event_id: str, slug: str, bucket: str = "", *,
            previous_source_ts: int | None = None,
            paused_at: int | None = None) -> bool:
        cur = self._conn.execute(
            """INSERT OR IGNORE INTO bargain_baro_pauses
                   (event_id, slug, bucket, previous_source_ts, paused_at, resumed_at)
               VALUES (?,?,?,?,?,NULL)""",
            (event_id, slug, bucket, previous_source_ts,
             int(time.time()) if paused_at is None else int(paused_at)))
        self._conn.commit()
        return cur.rowcount > 0

    def get_active_bargain_baro_pause(
            self, slug: str, bucket: str = "") -> dict | None:
        row = self._conn.execute(
            """SELECT event_id, slug, bucket, previous_source_ts, paused_at,
                      resumed_at
               FROM bargain_baro_pauses
               WHERE slug=? AND bucket=? AND resumed_at IS NULL
               ORDER BY paused_at DESC LIMIT 1""",
            (slug, bucket)).fetchone()
        return dict(row) if row else None

    def is_bargain_item_paused(self, slug: str, bucket: str = "") -> bool:
        return self.get_active_bargain_baro_pause(slug, bucket) is not None

    def resume_bargain_baro_pauses_for_baseline(
            self, slug: str, bucket: str, new_source_ts: int, *,
            resumed_at: int | None = None) -> int:
        """当日桶已前进时恢复对应道具；无旧桶的新道具在首个桶出现后恢复。"""
        cur = self._conn.execute(
            """UPDATE bargain_baro_pauses SET resumed_at=?
               WHERE slug=? AND bucket=? AND resumed_at IS NULL
                 AND (previous_source_ts IS NULL OR previous_source_ts < ?)""",
            (int(time.time()) if resumed_at is None else int(resumed_at),
             slug, bucket, int(new_source_ts)))
        self._conn.commit()
        return cur.rowcount

    def backup_to(self, path: str):
        """SQLite 在线备份到指定文件。"""
        source = self._conn if self.path is None else sqlite3.connect(str(self.path))
        try:
            target = sqlite3.connect(path)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            if source is not self._conn:
                source.close()

    def close(self):
        self._conn.close()
