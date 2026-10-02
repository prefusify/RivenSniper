"""频道玩家、紫卡与可见时段的本地库。

玩家身份只接受 IRC hostmask 中的 24 位稳定 ID。紫卡身份由 OMG 原始卡面位
生成，昵称、卡片等级和洗练次数都不参与身份推断。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from weakref import WeakKeyDictionary

from . import riven_link
from .chat_collector.protocol import is_account_id
from .platform_identity import (
    PLATFORM_ORDER,
    PLATFORM_UNKNOWN,
    normalize_player_nick,
    normalize_platform,
    player_nick_lookup_variants,
    resolve_player_identity,
)
from .privacy import redact_hidden_identifiers_deep


TRACKING_SCHEMA_VERSION = 20260810
_MIGRATABLE_TRACKING_SCHEMA_VERSIONS = {20260801, 20260808}
TRACKING_PAGE_SIZE = 25
TRACKING_EXPORT_LIMIT = 5_000
CHANNEL_RIVEN_BASE_DEDUPE_SECONDS = 3600


class TrackingExportTooLarge(ValueError):
    def __init__(self, total: int, limit: int | None = None):
        self.total = int(total)
        self.limit = int(TRACKING_EXPORT_LIMIT if limit is None else limit)
        super().__init__(
            f"追踪历史共 {self.total} 条，超过单次导出上限 {self.limit} 条"
        )


def _normalized_nick_rows(
    rows: Iterable[sqlite3.Row],
) -> list[dict[str, Any]]:
    """规范化并合并仅空格编码不同的历史昵称行。"""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        item["nick"] = normalize_player_nick(item["nick"])
        key = (str(item["platform"]), str(item["nick"]).casefold())
        existing = merged.get(key)
        if existing is None:
            merged[key] = item
            continue
        existing["first_seen"] = min(
            int(existing["first_seen"]), int(item["first_seen"]))
        existing["last_seen"] = max(
            int(existing["last_seen"]), int(item["last_seen"]))
    return list(merged.values())


_BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
  account_id TEXT PRIMARY KEY,
  first_seen INTEGER NOT NULL,
  last_seen INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS player_platforms (
  account_id TEXT NOT NULL,
  platform TEXT NOT NULL,
  current_nick TEXT NOT NULL COLLATE NOCASE,
  first_seen INTEGER NOT NULL,
  last_seen INTEGER NOT NULL,
  PRIMARY KEY (account_id, platform),
  FOREIGN KEY (account_id) REFERENCES players(account_id)
);
CREATE TABLE IF NOT EXISTS player_nicks (
  account_id TEXT NOT NULL,
  platform TEXT NOT NULL,
  nick TEXT NOT NULL COLLATE NOCASE,
  first_seen INTEGER NOT NULL,
  last_seen INTEGER NOT NULL,
  PRIMARY KEY (account_id, platform, nick),
  FOREIGN KEY (account_id, platform)
    REFERENCES player_platforms(account_id, platform)
);
CREATE TABLE IF NOT EXISTS rivens (
  content_hash TEXT PRIMARY KEY,
  riven_no INTEGER NOT NULL UNIQUE,
  category TEXT NOT NULL,
  weapon_index INTEGER NOT NULL,
  polarity INTEGER NOT NULL,
  lvl_req INTEGER NOT NULL,
  stats BLOB NOT NULL,
  first_seen INTEGER NOT NULL,
  last_seen INTEGER NOT NULL,
  max_rerolls INTEGER NOT NULL DEFAULT 0,
  first_event_key TEXT,
  first_channel TEXT,
  first_seller_id TEXT,
  first_seller_nick TEXT,
  first_seller_platform TEXT,
  last_event_key TEXT,
  last_channel TEXT
);
CREATE TABLE IF NOT EXISTS riven_seq (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  n INTEGER NOT NULL
);
INSERT OR IGNORE INTO riven_seq(id, n) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS riven_ownerships (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  content_hash TEXT NOT NULL,
  holder_id TEXT,
  holder_nick TEXT,
  holder_platform TEXT,
  first_seen INTEGER NOT NULL,
  last_seen INTEGER NOT NULL,
  first_channel TEXT NOT NULL,
  last_channel TEXT NOT NULL,
  first_event_key TEXT NOT NULL,
  last_event_key TEXT NOT NULL,
  ambiguous INTEGER NOT NULL DEFAULT 0 CHECK (ambiguous IN (0, 1)),
  UNIQUE (content_hash, first_seen),
  FOREIGN KEY (content_hash) REFERENCES rivens(content_hash)
);
CREATE TABLE IF NOT EXISTS riven_push_dedupe (
  content_hash TEXT PRIMARY KEY,
  last_seen_at INTEGER NOT NULL,
  last_event_key TEXT NOT NULL,
  last_channel TEXT NOT NULL,
  FOREIGN KEY (content_hash) REFERENCES rivens(content_hash)
);

CREATE TABLE IF NOT EXISTS presence_events (
  event_key TEXT PRIMARY KEY,
  observed_at INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  account_id TEXT,
  nick TEXT,
  platform TEXT,
  channel TEXT,
  observer_key TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS presence_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  observed_at INTEGER NOT NULL,
  channel TEXT NOT NULL,
  observer_key TEXT NOT NULL,
  member_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS presence_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id TEXT NOT NULL,
  started_at INTEGER NOT NULL,
  last_seen_at INTEGER NOT NULL,
  ended_at INTEGER,
  end_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_presence_open_session
  ON presence_sessions(account_id) WHERE ended_at IS NULL;
CREATE TABLE IF NOT EXISTS presence_memberships (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL REFERENCES presence_sessions(id),
  channel TEXT NOT NULL,
  observer_key TEXT NOT NULL,
  joined_at INTEGER NOT NULL,
  left_at INTEGER,
  discovered_via TEXT NOT NULL DEFAULT 'event'
    CHECK (discovered_via IN ('event', 'snapshot')),
  last_confirmed_at INTEGER NOT NULL,
  UNIQUE (session_id, channel, observer_key, joined_at)
);
CREATE INDEX IF NOT EXISTS ix_presence_membership_open
  ON presence_memberships(session_id, left_at);
CREATE INDEX IF NOT EXISTS ix_presence_membership_channel_open
  ON presence_memberships(observer_key, channel) WHERE left_at IS NULL;
CREATE TABLE IF NOT EXISTS presence_alerts (
  scope_id INTEGER NOT NULL,
  session_id INTEGER NOT NULL REFERENCES presence_sessions(id),
  region_key TEXT NOT NULL,
  alerted_at INTEGER NOT NULL,
  PRIMARY KEY (scope_id, session_id)
);
"""

_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS ix_nicks_name ON player_nicks(nick COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS ix_nicks_account_time
  ON player_nicks(account_id, platform, first_seen);
CREATE INDEX IF NOT EXISTS ix_platforms_account_time
  ON player_platforms(account_id, last_seen);
CREATE INDEX IF NOT EXISTS ix_players_last ON players(last_seen);
CREATE UNIQUE INDEX IF NOT EXISTS ix_rivens_no ON rivens(riven_no);
CREATE INDEX IF NOT EXISTS ix_ownerships_hash_time
  ON riven_ownerships(content_hash, first_seen, id);
CREATE INDEX IF NOT EXISTS ix_ownerships_holder_time
  ON riven_ownerships(holder_id, last_seen);
CREATE INDEX IF NOT EXISTS ix_ownerships_holder_cards
  ON riven_ownerships(holder_id, content_hash, last_seen)
  WHERE ambiguous=0;
CREATE INDEX IF NOT EXISTS ix_presence_events_time
  ON presence_events(observed_at);
CREATE INDEX IF NOT EXISTS ix_presence_snapshots_time
  ON presence_snapshots(observed_at);
CREATE INDEX IF NOT EXISTS ix_presence_sessions_ended
  ON presence_sessions(ended_at, id) WHERE ended_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_presence_alerts_session
  ON presence_alerts(session_id);
"""

@dataclass(frozen=True, slots=True)
class ObservedRiven:
    fingerprint: str
    riven_no: int
    card: dict[str, Any]
    card_index: int
    event_key: str
    push_allowed: bool
    dedupe_elapsed_seconds: int | None


class TrackingStore:
    def __init__(self, path: str | Path, *, read_only: bool = False):
        self.path = Path(path)
        self.read_only = bool(read_only)
        if self.read_only:
            self.connection = sqlite3.connect(
                self.path.resolve().as_uri() + "?mode=ro",
                uri=True, timeout=30.0, isolation_level=None,
                check_same_thread=False,
            )
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA query_only=ON")
            self.connection.execute("PRAGMA busy_timeout=30000")
            version = int(self.connection.execute(
                "PRAGMA user_version").fetchone()[0])
            if version != TRACKING_SCHEMA_VERSION:
                self.connection.close()
                raise RuntimeError(
                    f"追踪库 schema 版本不兼容: {version} != "
                    f"{TRACKING_SCHEMA_VERSION}"
                )
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            str(self.path), timeout=30.0, isolation_level=None,
            check_same_thread=False,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout=30000")
        self._migrate_schema()
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(_BASE_SCHEMA)
        self.connection.executescript(_INDEX_SCHEMA)
        self.connection.execute(f"PRAGMA user_version={TRACKING_SCHEMA_VERSION}")

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "TrackingStore":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _migrate_schema(self) -> None:
        """迁移已知追踪 schema；未知版本保持原库不变并拒绝启动。"""
        tables = [
            str(row[0]) for row in self.connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if not tables or version == TRACKING_SCHEMA_VERSION:
            return
        if version not in _MIGRATABLE_TRACKING_SCHEMA_VERSIONS:
            self.connection.close()
            raise RuntimeError(
                f"追踪库 schema 版本不兼容: {version} -> "
                f"{TRACKING_SCHEMA_VERSION}；原数据库未修改"
            )
        backup = self.path.with_name(
            f"{self.path.stem}.backup-"
            f"{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"
            f"{self.path.suffix}"
        )
        backup_connection = sqlite3.connect(str(backup))
        try:
            self.connection.backup(backup_connection)
        finally:
            backup_connection.close()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            if version == 20260801:
                # 正数 scope 是旧 QQ 群目标；其他追踪数据和 Discord 提醒完整保留。
                self.connection.execute(
                    "DELETE FROM presence_alerts WHERE scope_id>0")
            self.connection.execute(
                "ALTER TABLE presence_memberships ADD COLUMN "
                "discovered_via TEXT NOT NULL DEFAULT 'event' "
                "CHECK (discovered_via IN ('event', 'snapshot'))"
            )
            self.connection.execute(
                "ALTER TABLE presence_memberships ADD COLUMN "
                "last_confirmed_at INTEGER NOT NULL DEFAULT 0"
            )
            self.connection.execute(
                "UPDATE presence_memberships SET last_confirmed_at=joined_at "
                "WHERE last_confirmed_at=0"
            )
            self.connection.execute(
                """CREATE TABLE presence_snapshots (
                     snapshot_id TEXT PRIMARY KEY,
                     observed_at INTEGER NOT NULL,
                     channel TEXT NOT NULL,
                     observer_key TEXT NOT NULL,
                     member_count INTEGER NOT NULL
                   )"""
            )
            self.connection.execute(
                "CREATE INDEX ix_presence_snapshots_time "
                "ON presence_snapshots(observed_at)"
            )
            self.connection.execute(
                f"PRAGMA user_version={TRACKING_SCHEMA_VERSION}")
            self.connection.execute("COMMIT")
        except Exception:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    # ---- 玩家与紫卡观测 ----

    def _observe_player(
        self, account_id: str, platform: str, nick: str, observed_at: int, *,
        prefer_equal_time: bool = False,
    ) -> None:
        platform = normalize_platform(platform)
        row = self.connection.execute(
            "SELECT first_seen,last_seen FROM players WHERE account_id=?",
            (account_id,),
        ).fetchone()
        if row is None:
            self.connection.execute(
                "INSERT INTO players(account_id,first_seen,last_seen) VALUES (?,?,?)",
                (account_id, observed_at, observed_at),
            )
        else:
            self.connection.execute(
                "UPDATE players SET first_seen=?,last_seen=? WHERE account_id=?",
                (min(int(row["first_seen"]), observed_at),
                 max(int(row["last_seen"]), observed_at), account_id),
            )

        identity = self.connection.execute(
            """SELECT current_nick,first_seen,last_seen FROM player_platforms
               WHERE account_id=? AND platform=?""",
            (account_id, platform),
        ).fetchone()
        if identity is None:
            self.connection.execute(
                """INSERT INTO player_platforms
                   (account_id,platform,current_nick,first_seen,last_seen)
                   VALUES (?,?,?,?,?)""",
                (account_id, platform, nick, observed_at, observed_at),
            )
        else:
            current_nick = (
                nick if (observed_at > int(identity["last_seen"])
                         or (prefer_equal_time
                             and observed_at == int(identity["last_seen"])))
                else normalize_player_nick(identity["current_nick"])
            )
            self.connection.execute(
                """UPDATE player_platforms
                   SET current_nick=?,first_seen=?,last_seen=?
                   WHERE account_id=? AND platform=?""",
                (current_nick, min(int(identity["first_seen"]), observed_at),
                 max(int(identity["last_seen"]), observed_at),
                 account_id, platform),
            )

        history = self.connection.execute(
            """SELECT first_seen,last_seen FROM player_nicks
               WHERE account_id=? AND platform=? AND nick=?""",
            (account_id, platform, nick),
        ).fetchone()
        if history is None:
            self.connection.execute(
                """INSERT INTO player_nicks
                   (account_id,platform,nick,first_seen,last_seen)
                   VALUES (?,?,?,?,?)""",
                (account_id, platform, nick, observed_at, observed_at),
            )
        else:
            self.connection.execute(
                """UPDATE player_nicks SET first_seen=?,last_seen=?
                   WHERE account_id=? AND platform=? AND nick=?""",
                (min(int(history["first_seen"]), observed_at),
                 max(int(history["last_seen"]), observed_at),
                 account_id, platform, nick),
            )

    def ingest(
        self, message: dict[str, Any], *, dedupe_seconds: int,
        historical: bool = False,
    ) -> list[ObservedRiven]:
        """持久化并原子决定每张卡是否可推送。

        ``historical=True`` 只建立历史与去重基线，绝不返回可推送卡。
        """
        observed, _result, _player_riven_count = self._observe_message(
            message, dedupe_seconds=max(0, int(dedupe_seconds)),
            historical=historical,
        )
        return observed

    def ingest_with_player_riven_count(
        self, message: dict[str, Any], *, dedupe_seconds: int,
    ) -> tuple[list[ObservedRiven], int | None]:
        """持久化投递消息，并返回稳定账号累计去重紫卡数。"""
        observed, _result, player_riven_count = self._observe_message(
            message,
            dedupe_seconds=max(0, int(dedupe_seconds)),
            historical=False,
            include_player_riven_count=True,
        )
        return observed, player_riven_count

    def ingest_historical_batch(
        self, messages: Iterable[dict[str, Any]], *, dedupe_seconds: int,
    ) -> int:
        """在一个短事务中写入一批历史消息，返回处理条数。"""
        batch = tuple(messages)
        if not batch:
            return 0
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            for message in batch:
                self._observe_message(
                    message,
                    dedupe_seconds=max(0, int(dedupe_seconds)),
                    historical=True,
                    in_transaction=True,
                )
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        return len(batch)

    def _observe_message(
        self, message: dict[str, Any], *, dedupe_seconds: int | None,
        historical: bool, include_player_riven_count: bool = False,
        in_transaction: bool = False,
    ) -> tuple[list[ObservedRiven], dict[str, int], int | None]:
        sender_id = str(message.get("sender_id") or "").strip().lower()
        if not is_account_id(sender_id):
            sender_id = ""
        irc_nick = str(
            message.get("irc_nick") or message.get("nick") or ""
        ).strip()
        nick, platform = resolve_player_identity(
            irc_nick,
            explicit_platform=message.get("platform"),
            raw=message.get("raw"),
        )
        nick = nick or "?"
        channel = str(message.get("chan") or "?").strip() or "?"
        text = str(message.get("text") or "")
        observed_at = _timestamp(message.get("t", message.get("ts")))
        record_key = str(message.get("key") or "").strip()
        if not record_key:
            record_key = hashlib.sha256(
                f"{observed_at}\0{sender_id}\0{platform}\0{channel}\0"
                f"{irc_nick or nick}\0{text}".encode()
            ).hexdigest()
        decoded: list[tuple[str, str, dict[str, Any]]] = []
        for match in riven_link.OMG_RE.finditer(text):
            card = riven_link.decode_link(match.group(1), match.group(2))
            if card is not None:
                decoded.append((match.group(1), match.group(2), card))

        result = {
            "player_touch": 0,
            "riven_upsert": 0,
            "ownership_new": 0,
            "push_allowed": 0,
        }
        player_riven_count: int | None = None
        output: list[ObservedRiven] = []
        if not in_transaction:
            self.connection.execute("BEGIN IMMEDIATE")
        try:
            if sender_id and nick != "?":
                self._observe_player(
                    sender_id, platform, nick, observed_at,
                )
                result["player_touch"] = 1
            for index, (category, b64, card) in enumerate(decoded):
                fingerprint = content_fingerprint(category, card)
                event_key = hashlib.sha256(
                    f"{record_key}\0{index}\0{b64}".encode("utf-8")
                ).hexdigest()
                riven_no = self._upsert_riven(
                    fingerprint, category, card, observed_at,
                    event_key=event_key, channel=channel,
                    seller_id=sender_id or None, seller_nick=nick,
                    seller_platform=platform,
                )
                result["riven_upsert"] += 1
                if sender_id:
                    result["ownership_new"] += int(self._record_ownership(
                        fingerprint, observed_at, sender_id, nick, platform,
                        channel, event_key,
                    ))
                allowed = False
                elapsed: int | None = None
                if dedupe_seconds is not None:
                    allowed, elapsed = self._touch_dedupe(
                        fingerprint, observed_at, event_key, channel,
                        dedupe_seconds, historical=historical,
                    )
                    result["push_allowed"] += int(allowed)
                output.append(ObservedRiven(
                    fingerprint, riven_no, card, index, event_key, allowed,
                    elapsed,
                ))
            if include_player_riven_count and sender_id:
                player_riven_count = self._count_player_rivens(sender_id)
            if not in_transaction:
                self.connection.execute("COMMIT")
        except Exception:
            if not in_transaction:
                self.connection.execute("ROLLBACK")
            raise
        return output, result, player_riven_count

    def _count_player_rivens(self, account_id: str) -> int:
        """沿用玩家报告口径统计稳定账号曾持有的去重紫卡。"""
        return int(self.connection.execute(
            """SELECT COUNT(DISTINCT content_hash) FROM riven_ownerships
               WHERE holder_id=? AND ambiguous=0""",
            (account_id,),
        ).fetchone()[0])

    def _touch_dedupe(
        self, fingerprint: str, observed_at: int, event_key: str, channel: str,
        window: int, *, historical: bool,
    ) -> tuple[bool, int | None]:
        row = self.connection.execute(
            """SELECT last_seen_at,last_event_key FROM riven_push_dedupe
               WHERE content_hash=?""",
            (fingerprint,),
        ).fetchone()
        if row is None:
            self.connection.execute(
                "INSERT INTO riven_push_dedupe(content_hash,last_seen_at,last_event_key,last_channel) "
                "VALUES (?,?,?,?)", (fingerprint, observed_at, event_key, channel),
            )
            return not historical, None
        previous = int(row["last_seen_at"])
        if str(row["last_event_key"]) == event_key:
            return False, None
        elapsed = observed_at - previous
        if window == 0:
            if observed_at >= previous:
                self.connection.execute(
                    "UPDATE riven_push_dedupe SET last_seen_at=?,"
                    "last_event_key=?,last_channel=? WHERE content_hash=?",
                    (observed_at, event_key, channel, fingerprint),
                )
            return not historical, elapsed
        if observed_at <= previous:
            return False, elapsed
        allowed = not historical and elapsed >= window
        self.connection.execute(
            "UPDATE riven_push_dedupe SET last_seen_at=?,last_event_key=?,last_channel=? "
            "WHERE content_hash=?",
            (observed_at, event_key, channel, fingerprint),
        )
        return allowed, elapsed

    def _record_ownership(
        self, content_hash: str, observed_at: int, holder_id: str,
        holder_nick: str, holder_platform: str, channel: str, event_key: str,
    ) -> bool:
        """记录一次可靠持有者状态，并压缩连续相同持有者。"""
        latest = self.connection.execute(
            """SELECT * FROM riven_ownerships WHERE content_hash=?
               ORDER BY first_seen DESC,id DESC LIMIT 1""",
            (content_hash,),
        ).fetchone()
        if latest is None:
            self._insert_ownership(
                content_hash, observed_at, holder_id, holder_nick,
                holder_platform, channel, event_key,
            )
            return True
        if (not latest["ambiguous"] and latest["holder_id"] == holder_id
                and observed_at >= int(latest["last_seen"])):
            self.connection.execute(
                """UPDATE riven_ownerships
                   SET holder_nick=?,holder_platform=?,last_seen=?,
                       last_channel=?,last_event_key=? WHERE id=?""",
                (holder_nick, holder_platform, observed_at, channel, event_key,
                 int(latest["id"])),
            )
            return False
        if observed_at > int(latest["last_seen"]):
            self._insert_ownership(
                content_hash, observed_at, holder_id, holder_nick,
                holder_platform, channel, event_key,
            )
            return True

        before = self.connection.execute(
            """SELECT COUNT(*) FROM riven_ownerships
               WHERE content_hash=? AND ambiguous=0""",
            (content_hash,),
        ).fetchone()[0]
        events: list[dict[str, Any]] = []
        for row in self.connection.execute(
            """SELECT * FROM riven_ownerships
               WHERE content_hash=? ORDER BY first_seen,id""",
            (content_hash,),
        ).fetchall():
            if row["ambiguous"]:
                events.append({
                    "observed_at": int(row["first_seen"]),
                    "ambiguous": True,
                    "channel": str(row["first_channel"]),
                    "event_key": str(row["first_event_key"]),
                })
                continue
            first = {
                "observed_at": int(row["first_seen"]),
                "holder_id": str(row["holder_id"]),
                "holder_nick": str(row["holder_nick"]),
                "holder_platform": str(row["holder_platform"]),
                "channel": str(row["first_channel"]),
                "event_key": str(row["first_event_key"]),
            }
            events.append(first)
            if int(row["last_seen"]) != int(row["first_seen"]):
                events.append({
                    **first,
                    "observed_at": int(row["last_seen"]),
                    "holder_nick": str(row["holder_nick"]),
                    "holder_platform": str(row["holder_platform"]),
                    "channel": str(row["last_channel"]),
                    "event_key": str(row["last_event_key"]),
                })
        events.append({
            "observed_at": int(observed_at),
            "holder_id": holder_id,
            "holder_nick": holder_nick,
            "holder_platform": holder_platform,
            "channel": channel,
            "event_key": event_key,
        })
        spans = _ownership_spans(events)
        self._replace_ownerships(content_hash, spans)
        after = sum(not span["ambiguous"] for span in spans)
        return after > int(before)

    def _insert_ownership(
        self, content_hash: str, observed_at: int, holder_id: str,
        holder_nick: str, holder_platform: str, channel: str, event_key: str,
    ) -> None:
        self.connection.execute(
            """INSERT INTO riven_ownerships
               (content_hash,holder_id,holder_nick,holder_platform,
                first_seen,last_seen,first_channel,last_channel,
                first_event_key,last_event_key,ambiguous)
               VALUES (?,?,?,?,?,?,?,?,?,?,0)""",
            (content_hash, holder_id, holder_nick, holder_platform,
             observed_at, observed_at, channel, channel, event_key, event_key),
        )

    def _replace_ownerships(
        self, content_hash: str, spans: list[dict[str, Any]],
    ) -> None:
        self.connection.execute(
            "DELETE FROM riven_ownerships WHERE content_hash=?", (content_hash,),
        )
        self.connection.executemany(
            """INSERT INTO riven_ownerships
               (content_hash,holder_id,holder_nick,holder_platform,
                first_seen,last_seen,first_channel,last_channel,
                first_event_key,last_event_key,ambiguous)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            [(
                content_hash, span.get("holder_id"), span.get("holder_nick"),
                span.get("holder_platform"), int(span["first_seen"]),
                int(span["last_seen"]), str(span["first_channel"]),
                str(span["last_channel"]), str(span["first_event_key"]),
                str(span["last_event_key"]), int(bool(span["ambiguous"])),
            ) for span in spans],
        )

    def _upsert_riven(
        self, fingerprint: str, category: str, card: dict[str, Any], observed_at: int,
        *, event_key: str, channel: str, seller_id: str | None, seller_nick: str,
        seller_platform: str,
    ) -> int:
        row = self.connection.execute(
            """SELECT riven_no,first_seen,last_seen,max_rerolls
               FROM rivens WHERE content_hash=?""",
            (fingerprint,),
        ).fetchone()
        rerolls = max(0, int(card["rerolls"]))
        if row is not None:
            first_seen = min(int(row["first_seen"]), observed_at)
            update_discovery = observed_at < int(row["first_seen"])
            self.connection.execute(
                """UPDATE rivens SET first_seen=?,last_seen=?,max_rerolls=?,
                       first_event_key=CASE WHEN ? THEN ? ELSE first_event_key END,
                       first_channel=CASE WHEN ? THEN ? ELSE first_channel END,
                       first_seller_id=CASE WHEN ? THEN ? ELSE first_seller_id END,
                       first_seller_nick=CASE WHEN ? THEN ? ELSE first_seller_nick END,
                       first_seller_platform=CASE WHEN ? THEN ?
                         ELSE first_seller_platform END,
                       last_event_key=CASE WHEN ? THEN ? ELSE last_event_key END,
                       last_channel=CASE WHEN ? THEN ? ELSE last_channel END
                   WHERE content_hash=?""",
                (first_seen, max(int(row["last_seen"]), observed_at),
                 max(int(row["max_rerolls"]), rerolls),
                 int(update_discovery), event_key, int(update_discovery), channel,
                  int(update_discovery), seller_id, int(update_discovery), seller_nick,
                  int(update_discovery), seller_platform,
                  int(observed_at >= int(row["last_seen"])), event_key,
                  int(observed_at >= int(row["last_seen"])), channel,
                  fingerprint),
            )
            return int(row["riven_no"])

        self.connection.execute("UPDATE riven_seq SET n=n+1 WHERE id=1")
        number = int(self.connection.execute(
            "SELECT n FROM riven_seq WHERE id=1"
        ).fetchone()[0])
        stats = json.dumps([
            {"tag": stat["ref"], "idx": stat["_index"],
             "fb": stat["_float_bits"], "roll": stat["roll"],
             "curse": stat["is_curse"]}
            for stat in card["stats"]
        ], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.connection.execute(
            """INSERT INTO rivens
               (content_hash,riven_no,category,weapon_index,polarity,lvl_req,stats,
                first_seen,last_seen,max_rerolls,first_event_key,first_channel,
                first_seller_id,first_seller_nick,first_seller_platform,
                last_event_key,last_channel)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fingerprint, number, category, int(card["weapon_index"]),
             int(card["polarity_code"]), int(card["lvl_req"]), stats,
             observed_at, observed_at, rerolls, event_key, channel,
             seller_id, seller_nick, seller_platform, event_key, channel),
        )
        return number

    # ---- 查询 ----

    def find_players(self, query: str) -> list[dict[str, Any]]:
        value = str(query or "").strip()
        account_query = is_account_id(value.lower())
        if account_query:
            rows = self.connection.execute(
                """SELECT account_id,first_seen,last_seen FROM players
                   WHERE account_id=?""",
                (value.lower(),),
            ).fetchall()
        else:
            variants = player_nick_lookup_variants(value)
            if not variants:
                return []
            placeholders = ",".join("?" for _ in variants)
            rows = self.connection.execute(
                f"""SELECT DISTINCT p.account_id,p.first_seen,p.last_seen
                    FROM player_nicks n INDEXED BY ix_nicks_name
                    JOIN players p ON p.account_id=n.account_id
                    WHERE n.nick IN ({placeholders}) COLLATE NOCASE
                    ORDER BY p.last_seen DESC,p.account_id""",
                variants,
            ).fetchall()
        output: list[dict[str, Any]] = []
        platform_order = {
            platform: index for index, platform in enumerate(PLATFORM_ORDER)
        }
        for row in rows:
            player = dict(row)
            matched_identities: list[dict[str, Any]] = []
            if not account_query:
                placeholders = ",".join("?" for _ in variants)
                matched_identities = _normalized_nick_rows(
                    self.connection.execute(
                        f"""SELECT platform,nick,first_seen,last_seen
                            FROM player_nicks
                            WHERE account_id=?
                              AND nick IN ({placeholders}) COLLATE NOCASE""",
                        (player["account_id"], *variants),
                    ).fetchall()
                )
                matched_identities.sort(key=lambda identity: (
                    platform_order.get(
                        str(identity["platform"]), len(platform_order),
                    ),
                    -int(identity["last_seen"]),
                    str(identity["nick"]).casefold(),
                ))
            if matched_identities:
                identity = matched_identities[0]
                display_nick = str(identity["nick"])
            else:
                identity = self._latest_platform_identity(player["account_id"])
                display_nick = (
                    normalize_player_nick(identity["current_nick"])
                    if identity is not None else "玩家"
                )
            player["nick"] = display_nick
            player["platform"] = (
                identity["platform"] if identity is not None
                else PLATFORM_UNKNOWN
            )
            player["matched_identities"] = matched_identities
            output.append(player)
        return output

    def _latest_platform_identity(
        self, account_id: str,
    ) -> sqlite3.Row | None:
        return self.connection.execute(
            """SELECT platform,current_nick,first_seen,last_seen
               FROM player_platforms WHERE account_id=?
               ORDER BY last_seen DESC,platform LIMIT 1""",
            (account_id,),
        ).fetchone()

    def player_report(
        self, account_id: str, *, page: int = 1,
        page_size: int = TRACKING_PAGE_SIZE, all_results: bool = False,
    ) -> dict[str, Any] | None:
        account_id = str(account_id or "").strip().lower()
        player = self.connection.execute(
            """SELECT account_id,first_seen,last_seen FROM players
               WHERE account_id=?""",
            (account_id,),
        ).fetchone()
        if player is None:
            return None
        identities = [dict(row) for row in self.connection.execute(
            """SELECT platform,current_nick,first_seen,last_seen
               FROM player_platforms WHERE account_id=?""",
            (account_id,),
        ).fetchall()]
        for identity in identities:
            identity["current_nick"] = normalize_player_nick(
                identity["current_nick"])
        order = {platform: index for index, platform in enumerate(PLATFORM_ORDER)}
        identities.sort(key=lambda row: (
            order.get(str(row["platform"]), len(order)),
            -int(row["last_seen"]),
        ))
        names_by_platform: dict[str, list[dict[str, Any]]] = {}
        names = _normalized_nick_rows(self.connection.execute(
            """SELECT platform,nick,first_seen,last_seen FROM player_nicks
               WHERE account_id=? ORDER BY first_seen,nick""",
            (account_id,),
        ).fetchall())
        for name in names:
            names_by_platform.setdefault(str(name["platform"]), []).append(name)
        for identity in identities:
            identity["names"] = names_by_platform.get(
                str(identity["platform"]), [],
            )
        total = self._count_player_rivens(account_id)
        if all_results and total > TRACKING_EXPORT_LIMIT:
            raise TrackingExportTooLarge(total)
        pagination = _pagination(total, page, page_size, all_results=all_results)
        sql = """WITH held AS (
                   SELECT content_hash,MAX(last_seen) AS holder_last_seen
                   FROM riven_ownerships
                   WHERE holder_id=? AND ambiguous=0 GROUP BY content_hash
                 )
                 SELECT r.riven_no,r.category,r.weapon_index,r.polarity,
                        r.lvl_req,r.stats,r.first_seen,r.last_seen,
                        held.holder_last_seen
                 FROM held JOIN rivens r ON r.content_hash=held.content_hash
                 ORDER BY held.holder_last_seen DESC,r.riven_no"""
        parameters: list[Any] = [account_id]
        if not all_results:
            sql += " LIMIT ? OFFSET ?"
            parameters.extend((page_size, pagination["offset"]))
        rivens = [
            _public_riven_row(row) for row in self.connection.execute(
                sql, parameters,
            ).fetchall()
        ]
        latest = self._latest_platform_identity(account_id)
        public_player = dict(player)
        public_player.pop("account_id", None)
        return redact_hidden_identifiers_deep({
            **public_player,
            "nick": (normalize_player_nick(latest["current_nick"])
                     if latest is not None else "玩家"),
            "platform": latest["platform"] if latest is not None else PLATFORM_UNKNOWN,
            "platforms": identities,
            "names": [
                name for identity in identities for name in identity["names"]
            ],
            "rivens": rivens,
            "pagination": pagination,
        })

    def riven_report(
        self, riven_no: int, *, page: int = 1,
        page_size: int = TRACKING_PAGE_SIZE, all_results: bool = False,
    ) -> dict[str, Any] | None:
        riven = self.connection.execute(
            "SELECT * FROM rivens WHERE riven_no=?", (int(riven_no),)
        ).fetchone()
        if riven is None:
            return None
        content_hash = str(riven["content_hash"])
        total = int(self.connection.execute(
            """SELECT COUNT(*) FROM riven_ownerships
               WHERE content_hash=? AND ambiguous=0""",
            (content_hash,),
        ).fetchone()[0])
        if all_results and total > TRACKING_EXPORT_LIMIT:
            raise TrackingExportTooLarge(total)
        pagination = _pagination(
            total, page, page_size, all_results=all_results,
        )
        sql = """SELECT current.holder_nick,current.holder_platform,
                        current.first_seen,current.last_seen,
                        current.first_channel,current.last_channel,
                        (
                          SELECT CASE WHEN previous.ambiguous=0
                                      THEN previous.holder_nick END
                          FROM riven_ownerships previous
                          WHERE previous.content_hash=current.content_hash
                            AND (previous.first_seen<current.first_seen OR (
                              previous.first_seen=current.first_seen
                              AND previous.id<current.id
                            ))
                          ORDER BY previous.first_seen DESC,previous.id DESC
                          LIMIT 1
                        ) AS from_nick
                 FROM riven_ownerships current
                 WHERE current.content_hash=? AND current.ambiguous=0
                 ORDER BY current.first_seen,current.id"""
        parameters: list[Any] = [content_hash]
        if not all_results:
            sql += " LIMIT ? OFFSET ?"
            parameters.extend((page_size, pagination["offset"]))
        ownerships = [
            {
                "from_nick": (normalize_player_nick(row["from_nick"])
                              if row["from_nick"] is not None else None),
                "to_nick": normalize_player_nick(row["holder_nick"]),
                "to_platform": str(row["holder_platform"]),
                "observed_at": int(row["first_seen"]),
                "last_seen": int(row["last_seen"]),
                "channel": str(row["first_channel"]),
                "last_channel": str(row["last_channel"]),
            }
            for row in self.connection.execute(sql, parameters).fetchall()
        ]
        public_riven = _public_riven_row(riven)
        return redact_hidden_identifiers_deep({
            **public_riven,
            "ownerships": ownerships,
            "pagination": pagination,
        })

    # ---- 在线会话 ----

    def observe_presence_batch(
        self, events: Iterable[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """按输入顺序在一个短事务中处理一批 presence 事件。"""
        batch = tuple(events)
        if not batch:
            return []
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            results = [self._observe_presence_event(event) for event in batch]
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        return results

    def _observe_presence_event(
        self, event: dict[str, Any],
    ) -> dict[str, Any]:
        event_type = str(event.get("type") or "").strip().lower()
        raw_timestamp = event.get("t", event.get("ts"))
        observed_at = _timestamp(raw_timestamp)
        account_id = str(event.get("sender_id") or "").strip().lower()
        if not is_account_id(account_id):
            account_id = ""
        raw = event.get("raw")
        irc_nick = str(
            event.get("irc_nick") or event.get("nick") or ""
        ).strip()
        nick, platform = resolve_player_identity(
            irc_nick,
            explicit_platform=event.get("platform"),
            raw=raw,
        )
        old_irc_nick = str(
            event.get("old_irc_nick") or event.get("old_nick") or ""
        ).strip()
        old_nick, old_platform = resolve_player_identity(
            old_irc_nick,
            explicit_platform=platform,
            raw=raw,
        )
        channel = str(event.get("chan") or "").strip()
        observer = str(event.get("observer_key") or "").strip()
        if not observer:
            observer = ":".join((
                str(event.get("collector_run_id") or "legacy"),
                str(event.get("slot") or "?"),
            ))
        event_key = str(
            event.get("event_key") or event.get("snapshot_id") or ""
        ).strip()
        if not event_key:
            event_key = hashlib.sha256(
                f"{raw_timestamp}\0{event_type}\0{account_id}\0{platform}\0"
                f"{irc_nick or nick}\0{channel}\0{observer}".encode()
            ).hexdigest()

        if event_type == "channel_snapshot":
            return self._apply_channel_snapshot(
                event,
                snapshot_id=str(event.get("snapshot_id") or "").strip(),
                observed_at=observed_at,
                channel=channel,
                observer=observer,
            )

        result = {"recorded": False, "became_online": False,
                  "left_all_channels": False, "session_id": None,
                  "ended_session_id": None, "account_id": account_id,
                  "nick": nick, "irc_nick": irc_nick or nick,
                  "platform": platform}
        inserted = self.connection.execute(
            """INSERT OR IGNORE INTO presence_events
               (event_key,observed_at,event_type,account_id,nick,platform,
                channel,observer_key)
               VALUES (?,?,?,?,?,?,?,?)""",
            (event_key, observed_at, event_type, account_id or None,
             nick or None, platform, channel or None, observer),
        )
        if not inserted.rowcount:
            return result
        result["recorded"] = True
        if event_type == "observer_start":
            slot = observer.rsplit(":", 1)[-1]
            stale = self.connection.execute(
                """SELECT DISTINCT m.observer_key
                   FROM presence_memberships m
                   JOIN presence_sessions s ON s.id=m.session_id
                   WHERE m.observer_key<>? AND m.observer_key LIKE ?
                     AND s.ended_at IS NULL""",
                (observer, f"%:{slot}"),
            ).fetchall()
            for row in stale:
                self._close_observer(str(row["observer_key"]), observed_at)
        elif event_type == "observer_stop":
            self._close_observer(observer, observed_at)
        elif account_id:
            if event_type == "nick" and old_nick and old_nick != nick:
                # 协议明确给出的旧名同样是可靠历史；先记录旧名，再让
                # 同一时间点的新名通过 prefer_equal_time 成为当前名称。
                self._observe_player(
                    account_id, old_platform, old_nick, observed_at,
                )
            if nick:
                self._observe_player(
                    account_id, platform, nick, observed_at,
                    prefer_equal_time=event_type == "nick",
                )
            if event_type == "join":
                session = self.connection.execute(
                    "SELECT id FROM presence_sessions WHERE account_id=? AND ended_at IS NULL",
                    (account_id,),
                ).fetchone()
                if session is not None:
                    open_session_id = int(session["id"])
                    if not self._session_has_active_memberships(
                            open_session_id):
                        # 兼容旧版本遗留的空会话：不补发历史离开提醒，
                        # 但本次 JOIN 必须建立新周期并重新取得提醒资格。
                        self._close_session(
                            open_session_id, observed_at,
                            "memberships_empty",
                        )
                        session = None
                if session is None:
                    cursor = self.connection.execute(
                        "INSERT INTO presence_sessions(account_id,started_at,last_seen_at) VALUES (?,?,?)",
                        (account_id, observed_at, observed_at),
                    )
                    session_id = int(cursor.lastrowid)
                    result["became_online"] = True
                else:
                    session_id = int(session["id"])
                    self.connection.execute(
                        "UPDATE presence_sessions SET last_seen_at=MAX(last_seen_at,?) WHERE id=?",
                        (observed_at, session_id),
                    )
                self.connection.execute(
                    """INSERT OR IGNORE INTO presence_memberships
                       (session_id,channel,observer_key,joined_at,
                        discovered_via,last_confirmed_at)
                       VALUES (?,?,?,?,?,?)""",
                    (session_id, channel or "?", observer, observed_at,
                     "event", observed_at),
                )
                self.connection.execute(
                    """UPDATE presence_memberships
                       SET last_confirmed_at=MAX(last_confirmed_at,?)
                       WHERE session_id=? AND channel=? AND observer_key=?
                         AND left_at IS NULL""",
                    (observed_at, session_id, channel or "?", observer),
                )
                result["session_id"] = session_id
            elif event_type == "part":
                session = self.connection.execute(
                    "SELECT id FROM presence_sessions "
                    "WHERE account_id=? AND ended_at IS NULL",
                    (account_id,),
                ).fetchone()
                if session is not None:
                    session_id = int(session["id"])
                    parted = self.connection.execute(
                        """UPDATE presence_memberships SET left_at=?
                           WHERE session_id=? AND channel=?
                             AND left_at IS NULL""",
                        (observed_at, session_id, channel),
                    )
                    if (parted.rowcount
                            and not self._session_has_active_memberships(
                                session_id)):
                        self._close_session(
                            session_id, observed_at, "all_parts",
                        )
                        result["left_all_channels"] = True
                        result["ended_session_id"] = session_id
            elif event_type == "quit":
                session_id = self._close_account(
                    account_id, observed_at, "quit",
                )
                if session_id is not None:
                    result["left_all_channels"] = True
                    result["ended_session_id"] = session_id
            elif event_type == "nick":
                self.connection.execute(
                    "UPDATE presence_sessions SET last_seen_at=MAX(last_seen_at,?) "
                    "WHERE account_id=? AND ended_at IS NULL",
                    (observed_at, account_id),
                )
        return result

    def _apply_channel_snapshot(
        self,
        event: dict[str, Any],
        *,
        snapshot_id: str,
        observed_at: int,
        channel: str,
        observer: str,
    ) -> dict[str, Any]:
        if not snapshot_id or not channel.startswith("#") or not observer:
            raise ValueError("频道快照缺少 snapshot_id、频道或观察器")
        raw_members = event.get("members")
        if not isinstance(raw_members, list):
            raise ValueError("频道快照 members 必须是数组")
        members: dict[str, tuple[str, str]] = {}
        for raw_member in raw_members:
            if not isinstance(raw_member, list) or len(raw_member) != 2:
                raise ValueError("频道快照成员必须是 [account_id, irc_nick]")
            account_id = str(raw_member[0] or "").strip().lower()
            irc_nick = str(raw_member[1] or "").strip()
            if not is_account_id(account_id) or not irc_nick:
                raise ValueError("频道快照成员身份无效")
            nick, platform = resolve_player_identity(irc_nick)
            if not nick:
                raise ValueError("频道快照成员昵称无效")
            members[account_id] = (nick, platform)

        result = {
            "recorded": False,
            "snapshot": True,
            "became_online": False,
            "left_all_channels": False,
            "session_id": None,
            "ended_session_id": None,
            "account_id": "",
            "nick": "",
            "irc_nick": "",
            "platform": PLATFORM_UNKNOWN,
            "member_count": len(members),
            "added_memberships": 0,
            "closed_memberships": 0,
        }
        inserted = self.connection.execute(
            """INSERT OR IGNORE INTO presence_snapshots
               (snapshot_id,observed_at,channel,observer_key,member_count)
               VALUES (?,?,?,?,?)""",
            (snapshot_id, observed_at, channel, observer, len(members)),
        )
        if not inserted.rowcount:
            return result
        result["recorded"] = True

        current_rows = self.connection.execute(
            """SELECT m.id,m.session_id,s.account_id
               FROM presence_memberships m
               JOIN presence_sessions s ON s.id=m.session_id
               WHERE m.channel=? AND m.observer_key=? AND m.left_at IS NULL
                 AND s.ended_at IS NULL""",
            (channel, observer),
        ).fetchall()
        current: dict[str, list[sqlite3.Row]] = {}
        for row in current_rows:
            current.setdefault(str(row["account_id"]), []).append(row)

        for account_id in sorted(members):
            nick, platform = members[account_id]
            self._observe_player(account_id, platform, nick, observed_at)
            existing = current.get(account_id, [])
            if existing:
                membership_ids = [int(row["id"]) for row in existing]
                placeholders = ",".join("?" for _ in membership_ids)
                self.connection.execute(
                    f"""UPDATE presence_memberships
                        SET last_confirmed_at=MAX(last_confirmed_at,?)
                        WHERE id IN ({placeholders})""",
                    (observed_at, *membership_ids),
                )
                session_ids = {int(row["session_id"]) for row in existing}
                for session_id in session_ids:
                    self.connection.execute(
                        "UPDATE presence_sessions "
                        "SET last_seen_at=MAX(last_seen_at,?) WHERE id=?",
                        (observed_at, session_id),
                    )
                continue

            session = self.connection.execute(
                "SELECT id FROM presence_sessions "
                "WHERE account_id=? AND ended_at IS NULL",
                (account_id,),
            ).fetchone()
            if session is not None:
                session_id = int(session["id"])
                if not self._session_has_active_memberships(session_id):
                    self._close_session(
                        session_id, observed_at, "memberships_empty",
                    )
                    session = None
            if session is None:
                cursor = self.connection.execute(
                    "INSERT INTO presence_sessions"
                    "(account_id,started_at,last_seen_at) VALUES (?,?,?)",
                    (account_id, observed_at, observed_at),
                )
                session_id = int(cursor.lastrowid)
            else:
                session_id = int(session["id"])
                self.connection.execute(
                    "UPDATE presence_sessions "
                    "SET last_seen_at=MAX(last_seen_at,?) WHERE id=?",
                    (observed_at, session_id),
                )
            self.connection.execute(
                """INSERT INTO presence_memberships
                   (session_id,channel,observer_key,joined_at,
                    discovered_via,last_confirmed_at)
                   VALUES (?,?,?,?,?,?)""",
                (session_id, channel, observer, observed_at,
                 "snapshot", observed_at),
            )
            result["added_memberships"] += 1

        absent = set(current) - set(members)
        affected_sessions: set[int] = set()
        for account_id in absent:
            rows = current[account_id]
            membership_ids = [int(row["id"]) for row in rows]
            placeholders = ",".join("?" for _ in membership_ids)
            closed = self.connection.execute(
                f"""UPDATE presence_memberships SET left_at=?
                    WHERE id IN ({placeholders}) AND left_at IS NULL""",
                (observed_at, *membership_ids),
            )
            result["closed_memberships"] += int(closed.rowcount)
            affected_sessions.update(int(row["session_id"]) for row in rows)
        for session_id in affected_sessions:
            if not self._session_has_active_memberships(session_id):
                self._close_session(
                    session_id, observed_at, "snapshot_absent",
                )
        return result

    def _session_has_active_memberships(self, session_id: int) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM presence_memberships "
            "WHERE session_id=? AND left_at IS NULL LIMIT 1",
            (session_id,),
        ).fetchone() is not None

    def _close_session(
        self, session_id: int, observed_at: int, reason: str,
    ) -> None:
        self.connection.execute(
            "UPDATE presence_memberships SET left_at=? "
            "WHERE session_id=? AND left_at IS NULL",
            (observed_at, session_id),
        )
        self.connection.execute(
            "UPDATE presence_sessions SET last_seen_at=MAX(last_seen_at,?),"
            "ended_at=?,end_reason=? WHERE id=? AND ended_at IS NULL",
            (observed_at, observed_at, reason, session_id),
        )

    def _close_account(
        self, account_id: str, observed_at: int, reason: str,
    ) -> int | None:
        session = self.connection.execute(
            "SELECT id FROM presence_sessions "
            "WHERE account_id=? AND ended_at IS NULL",
            (account_id,),
        ).fetchone()
        if session is None:
            return None
        session_id = int(session["id"])
        self._close_session(session_id, observed_at, reason)
        return session_id

    def _close_observer(self, observer: str, observed_at: int) -> None:
        sessions = [int(row["session_id"]) for row in self.connection.execute(
            """SELECT DISTINCT m.session_id
               FROM presence_memberships m
               JOIN presence_sessions s ON s.id=m.session_id
               WHERE m.observer_key=? AND s.ended_at IS NULL""", (observer,),
        ).fetchall()]
        self.connection.execute(
            "UPDATE presence_memberships SET left_at=? WHERE observer_key=? AND left_at IS NULL",
            (observed_at, observer),
        )
        for session_id in sessions:
            if not self._session_has_active_memberships(session_id):
                self.connection.execute(
                    "UPDATE presence_sessions SET last_seen_at=MAX(last_seen_at,?),"
                    "ended_at=?,end_reason='observer_stop' "
                    "WHERE id=? AND ended_at IS NULL",
                    (observed_at, observed_at, session_id),
                )

    def claim_presence_alert(
        self, scope_ids: Iterable[int], session_id: int, region_key: str, *,
        alerted_at: int | None = None,
    ) -> list[int]:
        """认领首次地区或地区变化提醒；同地区后续 JOIN 不重复认领。"""
        now = _timestamp(alerted_at)
        normalized_region = str(region_key).strip().upper()
        if not normalized_region:
            raise ValueError("上线提醒地区键不能为空")
        claimed: list[int] = []
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            for scope_id in dict.fromkeys(int(value) for value in scope_ids):
                cursor = self.connection.execute(
                    """INSERT INTO presence_alerts
                       (scope_id,session_id,region_key,alerted_at)
                       VALUES (?,?,?,?)
                       ON CONFLICT(scope_id,session_id) DO UPDATE SET
                         region_key=excluded.region_key,
                         alerted_at=excluded.alerted_at
                       WHERE presence_alerts.region_key<>excluded.region_key""",
                    (scope_id, int(session_id), normalized_region, now),
                )
                if cursor.rowcount:
                    claimed.append(scope_id)
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        return claimed

    def claimed_presence_scope_ids(
        self, scope_ids: Iterable[int], session_id: int,
    ) -> list[int]:
        candidates = list(dict.fromkeys(int(value) for value in scope_ids))
        if not candidates:
            return []
        placeholders = ",".join("?" for _ in candidates)
        rows = self.connection.execute(
            f"""SELECT scope_id FROM presence_alerts
                WHERE session_id=? AND scope_id IN ({placeholders})
                ORDER BY scope_id""",
            (int(session_id), *candidates),
        ).fetchall()
        return [int(row["scope_id"]) for row in rows]

    def delete_target_data(self, scope_id: int) -> int:
        """删除目标专属的提醒幂等状态，保留全部共享追踪事实。"""
        if self.read_only:
            raise RuntimeError("只读追踪库不能删除目标关联数据")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self.connection.execute(
                "DELETE FROM presence_alerts WHERE scope_id=?",
                (int(scope_id),),
            )
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        return int(cursor.rowcount)

    def prune_presence_history(
        self, before_ts: int, *, batch_size: int = 10_000,
        max_batches: int = 50,
    ) -> dict[str, int | bool]:
        """分批清理可重建的旧 presence 明细，永久追踪表不受影响。"""
        if self.read_only:
            raise RuntimeError("只读追踪库不能清理 presence 历史")
        if batch_size <= 0 or max_batches <= 0:
            raise ValueError("presence 清理批量参数必须大于 0")

        cutoff = int(before_ts)
        totals = {"events": 0, "snapshots": 0, "sessions": 0,
                  "memberships": 0, "alerts": 0}
        for _ in range(max_batches):
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                events = self.connection.execute(
                    """DELETE FROM presence_events
                       WHERE rowid IN (
                         SELECT rowid FROM presence_events
                         WHERE observed_at<? ORDER BY observed_at LIMIT ?
                       )""",
                    (cutoff, batch_size),
                ).rowcount
                snapshots = self.connection.execute(
                    """DELETE FROM presence_snapshots
                       WHERE rowid IN (
                         SELECT rowid FROM presence_snapshots
                         WHERE observed_at<? ORDER BY observed_at LIMIT ?
                       )""",
                    (cutoff, batch_size),
                ).rowcount
                alerts = self.connection.execute(
                    """DELETE FROM presence_alerts
                       WHERE session_id IN (
                         SELECT id FROM presence_sessions
                         WHERE ended_at IS NOT NULL AND ended_at<?
                         ORDER BY ended_at,id LIMIT ?
                       )""",
                    (cutoff, batch_size),
                ).rowcount
                memberships = self.connection.execute(
                    """DELETE FROM presence_memberships
                       WHERE session_id IN (
                         SELECT id FROM presence_sessions
                         WHERE ended_at IS NOT NULL AND ended_at<?
                         ORDER BY ended_at,id LIMIT ?
                       )""",
                    (cutoff, batch_size),
                ).rowcount
                sessions = self.connection.execute(
                    """DELETE FROM presence_sessions
                       WHERE id IN (
                         SELECT id FROM presence_sessions
                         WHERE ended_at IS NOT NULL AND ended_at<?
                         ORDER BY ended_at,id LIMIT ?
                       )""",
                    (cutoff, batch_size),
                ).rowcount
                self.connection.execute("COMMIT")
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

            totals["events"] += events
            totals["snapshots"] += snapshots
            totals["alerts"] += alerts
            totals["memberships"] += memberships
            totals["sessions"] += sessions
            if (events < batch_size and snapshots < batch_size
                    and sessions < batch_size):
                break

        remaining = bool(self.connection.execute(
            """SELECT
                 EXISTS(SELECT 1 FROM presence_events
                        WHERE observed_at<? LIMIT 1)
                 OR EXISTS(SELECT 1 FROM presence_snapshots
                           WHERE observed_at<? LIMIT 1)
                 OR EXISTS(SELECT 1 FROM presence_sessions
                           WHERE ended_at IS NOT NULL AND ended_at<? LIMIT 1)""",
            (cutoff, cutoff, cutoff),
        ).fetchone()[0])
        self.connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
        return {**totals, "remaining": remaining}


def _ownership_spans(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """把可靠持有者事件折叠为连续区间；同秒冲突作为时间线断点。"""
    ordered = sorted(
        enumerate(events), key=lambda item: (int(item[1]["observed_at"]), item[0]),
    )
    states: list[dict[str, Any]] = []
    index = 0
    while index < len(ordered):
        observed_at = int(ordered[index][1]["observed_at"])
        same_time: list[dict[str, Any]] = []
        while (index < len(ordered)
               and int(ordered[index][1]["observed_at"]) == observed_at):
            same_time.append(ordered[index][1])
            index += 1
        holder_ids = {
            str(event["holder_id"])
            for event in same_time if event.get("holder_id")
        }
        representative = same_time[-1]
        if any(event.get("ambiguous") for event in same_time) or len(holder_ids) != 1:
            states.append({
                "observed_at": observed_at,
                "ambiguous": True,
                "channel": str(representative.get("channel") or "?"),
                "event_key": str(representative.get("event_key") or "?"),
            })
            continue
        holder_id = holder_ids.pop()
        representative = next(
            event for event in reversed(same_time)
            if str(event.get("holder_id") or "") == holder_id
        )
        states.append({
            "observed_at": observed_at,
            "holder_id": holder_id,
            "holder_nick": str(representative.get("holder_nick") or "?"),
            "holder_platform": str(
                representative.get("holder_platform") or PLATFORM_UNKNOWN),
            "channel": str(representative.get("channel") or "?"),
            "event_key": str(representative.get("event_key") or "?"),
            "ambiguous": False,
        })

    spans: list[dict[str, Any]] = []
    for state in states:
        observed_at = int(state["observed_at"])
        if state["ambiguous"]:
            spans.append({
                "holder_id": None,
                "holder_nick": None,
                "holder_platform": None,
                "first_seen": observed_at,
                "last_seen": observed_at,
                "first_channel": state["channel"],
                "last_channel": state["channel"],
                "first_event_key": state["event_key"],
                "last_event_key": state["event_key"],
                "ambiguous": True,
            })
            continue
        previous = spans[-1] if spans else None
        if (previous is not None and not previous["ambiguous"]
                and previous["holder_id"] == state["holder_id"]):
            previous.update({
                "holder_nick": state["holder_nick"],
                "holder_platform": state["holder_platform"],
                "last_seen": observed_at,
                "last_channel": state["channel"],
                "last_event_key": state["event_key"],
            })
            continue
        spans.append({
            "holder_id": state["holder_id"],
            "holder_nick": state["holder_nick"],
            "holder_platform": state["holder_platform"],
            "first_seen": observed_at,
            "last_seen": observed_at,
            "first_channel": state["channel"],
            "last_channel": state["channel"],
            "first_event_key": state["event_key"],
            "last_event_key": state["event_key"],
            "ambiguous": False,
        })
    return spans


def _pagination(
    total: int, page: int, page_size: int, *, all_results: bool,
) -> dict[str, int | bool]:
    page = max(1, int(page))
    page_size = max(1, int(page_size))
    pages = (total + page_size - 1) // page_size
    out_of_range = bool(not all_results and page > max(1, pages))
    offset = 0 if all_results else total if out_of_range else (page - 1) * page_size
    return {
        "page": 1 if all_results else page,
        "page_size": page_size,
        "total": int(total),
        "pages": pages,
        "offset": offset,
        "start": 0 if total == 0 or out_of_range else offset + 1,
        "end": (int(total) if all_results else
                0 if out_of_range else min(int(total), offset + page_size)),
        "out_of_range": out_of_range,
        "all_results": all_results,
    }


def _public_riven_row(row: sqlite3.Row) -> dict[str, Any]:
    public = dict(row)
    raw_stats = public.get("stats")
    if isinstance(raw_stats, bytes):
        decoded = json.loads(raw_stats.decode("utf-8"))
    elif isinstance(raw_stats, str):
        decoded = json.loads(raw_stats)
    else:
        decoded = raw_stats or []
    public["stats"] = [{
        "tag": str(stat.get("tag") or ""),
        "roll": float(stat.get("roll") or 0.0),
        "is_curse": bool(stat.get("curse")),
    } for stat in decoded]
    if public.get("first_seller_nick"):
        public["first_seller_nick"] = normalize_player_nick(
            public["first_seller_nick"])
    for key in (
        "content_hash", "first_event_key", "first_seller_id",
        "last_event_key", "max_rerolls",
    ):
        public.pop(key, None)
    return public


def _timestamp(value: Any) -> int:
    if isinstance(value, (int, float)):
        timestamp = int(value)
        return timestamp // 1000 if timestamp > 10_000_000_000 else timestamp
    text = str(value or "").strip()
    if text:
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            return int(datetime.fromisoformat(text).timestamp())
        except ValueError:
            try:
                return int(float(text))
            except ValueError:
                pass
    return int(datetime.now(timezone.utc).timestamp())


def content_fingerprint(category: str, card: dict[str, Any]) -> str:
    parts = [category, str(card["weapon_index"]), str(card["polarity_code"]),
             str(card["lvl_req"])]
    ordered = sorted(card["stats"], key=lambda stat: (
        1 if stat["is_curse"] else 0,
        int(stat["_index"]), int(stat["_float_bits"]),
    ))
    parts.extend(
        f"{1 if stat['is_curse'] else 0}:{stat['_index']}:{stat['_float_bits']}"
        for stat in ordered
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


__all__ = [
    "CHANNEL_RIVEN_BASE_DEDUPE_SECONDS", "ObservedRiven",
    "TRACKING_EXPORT_LIMIT", "TRACKING_PAGE_SIZE",
    "TrackingExportTooLarge", "TrackingStore", "content_fingerprint",
]


def tracking_database_path(config: Any) -> Path:
    configured = str(getattr(config, "irc_track_db_path", "") or "")
    if configured:
        return Path(configured)
    feed_dir = str(getattr(config, "irc_feed_dir", "") or "")
    if feed_dir:
        return Path(feed_dir).parent / "track.db"
    feed_path = str(getattr(config, "irc_feed_path", "") or "")
    return (Path(feed_path).parent if feed_path else Path(".runtime/chat_collector")) / "track.db"


def open_tracking_reader(config: Any) -> TrackingStore:
    """已有追踪库使用纯只读连接；未启用采集时仍创建可查询的空库。"""
    path = tracking_database_path(config)
    with _READER_INIT_LOCK:
        if not path.is_file():
            with TrackingStore(path):
                pass
    return TrackingStore(path, read_only=True)


_QUERY_CONCURRENCY = 4
_READER_INIT_LOCK = threading.Lock()
_QUERY_GATES: WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore,
] = WeakKeyDictionary()


async def run_tracking_query(callback):
    """在有界后台线程中执行查询，避免阻塞 Bot/FastAPI 事件循环。"""
    loop = asyncio.get_running_loop()
    gate = _QUERY_GATES.get(loop)
    if gate is None:
        gate = asyncio.Semaphore(_QUERY_CONCURRENCY)
        _QUERY_GATES[loop] = gate
    async with gate:
        return await asyncio.to_thread(callback)


__all__.extend((
    "open_tracking_reader", "run_tracking_query", "tracking_database_path",
))
