"""聊天追踪库的数据保全与乱序写入回归。"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import threading
import time
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.plugins.riven_sniper.chat_tracking import (
    TrackingStore,
    content_fingerprint,
    run_tracking_query,
)
from src.plugins.riven_sniper import riven_link
from src.plugins.riven_sniper.platform_identity import (
    game_nick_to_irc,
    irc_nick_to_game,
    normalize_player_nick,
    platform_from_irc_raw,
    resolve_player_identity,
    split_platform_nick,
)


RIVEN_TEXT = (
    "[OMG-LotusRifleRandomModRare:"
    "9b51zSsL1EkVt78mCp1r4/fpQ+1jxghk]"
)
TORID_RIVEN_TEXT = (
    "[OMG-LotusRifleRandomModRare:"
    "8T9NiKpb9TjuN70Tfedj9RLDSiibxgUk]"
)


def test_all_private_platform_characters_have_stable_mappings():
    expected = [
        "windows", "xbox", "playstation", "switch",
        "ios", "android", "switch2",
    ]

    assert [
        split_platform_nick(f"Player{chr(0xE000 + index)}")
        for index in range(7)
    ] == [("Player", platform) for platform in expected]
    markers = ["0", "1_ABCD", "2", "3", "4X", "5", "3X"]
    assert [platform_from_irc_raw(
        f":Player!0123456789abcdef01234567_{marker}@host JOIN :#T_ZH"
    ) for marker in markers] == expected


def test_warframe_nick_codec_covers_every_allowed_special_case():
    pairs = {
        "ExamplePlayer.": "ExamplePlayer|",
        "00Example_Player": "`00Example_Player",
        "-DemoPlayer-": "`-DemoPlayer-",
        ".example.": "|example|",
        "_Example_Player_": "_Example_Player_",
        "-T.E.S-ExamplePlayer": "`-T|E|S-ExamplePlayer",
        "Example o": "Example\u00a0o",
    }

    for game_nick, irc_nick in pairs.items():
        assert game_nick_to_irc(game_nick) == irc_nick
        assert irc_nick_to_game(irc_nick) == game_nick


def test_player_nick_normalizes_unicode_space_separators():
    assert normalize_player_nick(" Example\u00a0o ") == "Example o"
    assert normalize_player_nick("A\u202fB\u3000C") == "A B C"
    assert resolve_player_identity("Example\u00a0o\ue001") == (
        "Example o", "xbox",
    )


def test_identity_decodes_wire_nick_after_extracting_platform():
    assert resolve_player_identity("`-T|E|S-ExamplePlayer\ue000") == (
        "-T.E.S-ExamplePlayer", "windows",
    )


def _message(*, timestamp: int, seller_id: str = "0123456789abcdef01234567",
             nick: str = "Seller", channel: str = "#T_ZH",
             platform: str = "windows") -> dict:
    return {
        "t": timestamp,
        "nick": nick,
        "platform": platform,
        "sender_id": seller_id,
        "chan": channel,
        "text": RIVEN_TEXT,
    }


def _observe_player(
    store: TrackingStore, account_id: str, nick: str, *,
    platform: str = "unknown", observed_at: int,
) -> None:
    store.ingest({
        "t": observed_at,
        "nick": nick,
        "platform": platform,
        "sender_id": account_id,
        "chan": "#T_ZH",
        "text": "",
    }, dedupe_seconds=0, historical=True)


def _observe_presence(
    store: TrackingStore, event: dict,
) -> dict:
    return store.observe_presence_batch((event,))[0]


def test_unknown_legacy_tracking_database_is_preserved(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE listings (
          id INTEGER PRIMARY KEY,
          t INTEGER NOT NULL,
          content_hash TEXT NOT NULL,
          seller_id TEXT NOT NULL,
          price INTEGER,
          chan_id INTEGER NOT NULL,
          lvl INTEGER NOT NULL,
          rerolls INTEGER NOT NULL
        );
        INSERT INTO listings
          (id, t, content_hash, seller_id, price, chan_id, lvl, rerolls)
        VALUES
          (7, 100, 'card-hash', '0123456789abcdef01234567', 50, 3, 8, 2);
        """
    )
    conn.commit()
    conn.close()

    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="原数据库未修改"):
        TrackingStore(path)
    assert path.read_bytes() == before


def test_unknown_single_nick_schema_is_preserved(tmp_path):
    path = tmp_path / "previous.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE players (
          account_id TEXT PRIMARY KEY,
          nick TEXT NOT NULL,
          first_seen INTEGER NOT NULL,
          last_seen INTEGER NOT NULL
        );
        INSERT INTO players VALUES (
          '0123456789abcdef01234567', 'LegacyName', 100, 200
        );
        PRAGMA user_version=20260728;
        """
    )
    conn.close()

    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="原数据库未修改"):
        TrackingStore(path)
    assert path.read_bytes() == before


def test_unknown_nick_encoding_schema_is_preserved(tmp_path):
    path = tmp_path / "previous-nick-encoding.db"
    account_id = "0123456789abcdef01234567"
    with TrackingStore(path) as store:
        _observe_player(store,
            account_id, "ExamplePlayer|", platform="windows", observed_at=100,
        )

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version=20260729")
    connection.close()

    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="原数据库未修改"):
        TrackingStore(path)
    assert path.read_bytes() == before


def test_unknown_presence_alert_schema_is_preserved(tmp_path):
    path = tmp_path / "previous-presence-alert.db"
    account_id = "0123456789abcdef01234567"
    with TrackingStore(path) as store:
        _observe_player(store,
            account_id, "Player", platform="windows", observed_at=100,
        )

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version=20260730")
    connection.close()

    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="原数据库未修改"):
        TrackingStore(path)
    assert path.read_bytes() == before


def test_supported_tracking_migration_removes_only_qq_alerts(tmp_path):
    path = tmp_path / "track.db"
    account_id = "0123456789abcdef01234567"
    with TrackingStore(path) as store:
        presence = _observe_presence(store, {
            "type": "join", "t": 100, "sender_id": account_id,
            "nick": "PreservedPlayer", "chan": "#T_ZH",
            "observer_key": "run:A", "event_key": "join-migration",
        })
        session_id = presence["session_id"]
        assert store.claim_presence_alert(
            [111, -1], session_id, "ZH", alerted_at=100) == [111, -1]

    connection = sqlite3.connect(path)
    connection.executescript(
        """
        DROP INDEX ix_presence_membership_open;
        DROP INDEX ix_presence_snapshots_time;
        DROP TABLE presence_snapshots;
        ALTER TABLE presence_memberships RENAME TO current_memberships;
        CREATE TABLE presence_memberships (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id INTEGER NOT NULL REFERENCES presence_sessions(id),
          channel TEXT NOT NULL,
          observer_key TEXT NOT NULL,
          joined_at INTEGER NOT NULL,
          left_at INTEGER,
          UNIQUE (session_id, channel, observer_key, joined_at)
        );
        INSERT INTO presence_memberships
          (id,session_id,channel,observer_key,joined_at,left_at)
        SELECT id,session_id,channel,observer_key,joined_at,left_at
        FROM current_memberships;
        DROP TABLE current_memberships;
        CREATE INDEX ix_presence_membership_open
          ON presence_memberships(session_id, left_at);
        """
    )
    connection.execute("PRAGMA user_version=20260801")
    connection.commit()
    connection.close()

    with TrackingStore(path) as migrated:
        alerts = migrated.connection.execute(
            "SELECT scope_id,region_key FROM presence_alerts ORDER BY scope_id"
        ).fetchall()
        player = migrated.connection.execute(
            "SELECT first_seen,last_seen FROM players WHERE account_id=?",
            (account_id,),
        ).fetchone()

    assert [(row["scope_id"], row["region_key"]) for row in alerts] == [
        (-1, "ZH")]
    assert tuple(player) == (100, 100)
    with TrackingStore(path, read_only=True) as reader:
        membership = reader.connection.execute(
            "SELECT discovered_via,last_confirmed_at "
            "FROM presence_memberships"
        ).fetchone()
        assert tuple(membership) == ("event", 100)
    backups = list(tmp_path.glob("track.backup-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute(
            "SELECT COUNT(*) FROM presence_alerts").fetchone()[0] == 2
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 20260801


def test_supported_20260808_migration_preserves_all_alerts(tmp_path):
    path = tmp_path / "track.db"
    account_id = "0123456789abcdef01234567"
    with TrackingStore(path) as store:
        presence = _observe_presence(store, {
            "type": "join", "t": 100, "sender_id": account_id,
            "nick": "Player", "chan": "#T_ZH",
            "observer_key": "run:A", "event_key": "join-migration",
        })
        store.claim_presence_alert(
            [111, -1], presence["session_id"], "ZH", alerted_at=100,
        )

    connection = sqlite3.connect(path)
    connection.executescript(
        """
        DROP INDEX ix_presence_membership_open;
        DROP INDEX ix_presence_snapshots_time;
        DROP TABLE presence_snapshots;
        ALTER TABLE presence_memberships RENAME TO current_memberships;
        CREATE TABLE presence_memberships (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id INTEGER NOT NULL REFERENCES presence_sessions(id),
          channel TEXT NOT NULL,
          observer_key TEXT NOT NULL,
          joined_at INTEGER NOT NULL,
          left_at INTEGER,
          UNIQUE (session_id, channel, observer_key, joined_at)
        );
        INSERT INTO presence_memberships
          (id,session_id,channel,observer_key,joined_at,left_at)
        SELECT id,session_id,channel,observer_key,joined_at,left_at
        FROM current_memberships;
        DROP TABLE current_memberships;
        CREATE INDEX ix_presence_membership_open
          ON presence_memberships(session_id, left_at);
        PRAGMA user_version=20260808;
        """
    )
    connection.close()

    with TrackingStore(path) as migrated:
        alerts = migrated.connection.execute(
            "SELECT scope_id FROM presence_alerts ORDER BY scope_id"
        ).fetchall()
        snapshots = migrated.connection.execute(
            "SELECT COUNT(*) FROM presence_snapshots"
        ).fetchone()[0]

    assert [row["scope_id"] for row in alerts] == [-1, 111]
    assert snapshots == 0


def test_out_of_order_player_events_keep_current_nick_and_min_first_seen(tmp_path):
    with TrackingStore(tmp_path / "track.db") as store:
        _observe_player(store,
            "0123456789abcdef01234567", "CurrentNick",
            platform="windows", observed_at=200,
        )
        _observe_player(store,
            "0123456789abcdef01234567", "OldNick",
            platform="windows", observed_at=100,
        )
        player = store.connection.execute(
            "SELECT current_nick,first_seen,last_seen FROM player_platforms"
        ).fetchone()
        old_nick = store.connection.execute(
            "SELECT first_seen, last_seen FROM player_nicks WHERE nick='OldNick'"
        ).fetchone()

    assert tuple(player) == ("CurrentNick", 100, 200)
    assert tuple(old_nick) == (100, 100)


def test_same_timestamp_does_not_nondeterministically_rewrite_current_nick(tmp_path):
    with TrackingStore(tmp_path / "track.db") as store:
        _observe_player(store,
            "0123456789abcdef01234567", "FirstNick",
            platform="windows", observed_at=200,
        )
        _observe_player(store,
            "0123456789abcdef01234567", "SecondNick",
            platform="windows", observed_at=200,
        )
        player = store.connection.execute(
            "SELECT current_nick,first_seen,last_seen FROM player_platforms"
        ).fetchone()

    assert tuple(player) == ("FirstNick", 200, 200)


def test_one_account_keeps_independent_current_nicks_per_platform(tmp_path):
    account_id = "0123456789abcdef01234567"
    with TrackingStore(tmp_path / "track.db") as store:
        _observe_player(store,
            account_id, "PcSeller", platform="windows", observed_at=100,
        )
        _observe_player(store,
            account_id, "MobileSeller", platform="ios", observed_at=200,
        )
        _observe_player(store,
            account_id, "PcRenamed", platform="windows", observed_at=300,
        )
        report = store.player_report(account_id)

    identities = {
        row["platform"]: row for row in report["platforms"]
    }
    assert identities["windows"]["current_nick"] == "PcRenamed"
    assert identities["ios"]["current_nick"] == "MobileSeller"
    assert {row["nick"] for row in identities["windows"]["names"]} == {
        "PcSeller", "PcRenamed",
    }
    assert [row["nick"] for row in identities["ios"]["names"]] == [
        "MobileSeller",
    ]


def test_same_nick_on_two_platforms_remains_two_platform_identities(tmp_path):
    account_id = "0123456789abcdef01234567"
    with TrackingStore(tmp_path / "track.db") as store:
        _observe_player(store,
            account_id, "SharedName", platform="windows", observed_at=100,
        )
        _observe_player(store,
            account_id, "SharedName", platform="android", observed_at=200,
        )
        report = store.player_report(account_id)
        matches = store.find_players("SharedName")

    assert len(report["platforms"]) == 2
    assert {row["platform"] for row in report["platforms"]} == {
        "windows", "android",
    }
    assert [row["account_id"] for row in matches] == [account_id]
    assert [row["platform"] for row in matches[0]["matched_identities"]] == [
        "windows", "android",
    ]


def test_former_name_lookup_keeps_the_matched_platform_identity(tmp_path):
    account_id = "0123456789abcdef01234567"
    with TrackingStore(tmp_path / "track.db") as store:
        _observe_player(store,
            account_id, "PcAlias", platform="windows", observed_at=100,
        )
        _observe_player(store,
            account_id, "MobileNow", platform="ios", observed_at=200,
        )
        matches = store.find_players("PcAlias")
        report = store.player_report(account_id)

    assert len(matches) == 1
    assert matches[0]["nick"] == "PcAlias"
    assert matches[0]["platform"] == "windows"
    assert matches[0]["matched_identities"] == [{
        "platform": "windows",
        "nick": "PcAlias",
        "first_seen": 100,
        "last_seen": 100,
    }]
    assert {
        row["current_nick"] for row in report["platforms"]
    } == {"PcAlias", "MobileNow"}


def test_private_platform_glyph_is_removed_after_platform_is_captured(tmp_path):
    account_id = "0123456789abcdef01234567"
    with TrackingStore(tmp_path / "track.db") as store:
        _observe_player(store, account_id, "XboxName\ue001", observed_at=100)
        report = store.player_report(account_id)

    assert report["platforms"][0]["platform"] == "xbox"
    assert report["platforms"][0]["current_nick"] == "XboxName"
    assert report["names"][0]["nick"] == "XboxName"


def test_wire_nick_is_stored_and_queried_as_game_nick(tmp_path):
    account_id = "0123456789abcdef01234567"
    with TrackingStore(tmp_path / "track.db") as store:
        _observe_player(store,
            account_id, "`-T|E|S-ExamplePlayer\ue000", observed_at=100,
        )
        matches = store.find_players("-T.E.S-ExamplePlayer")
        report = store.player_report(account_id)

    assert matches[0]["account_id"] == account_id
    assert report["platforms"][0]["platform"] == "windows"
    assert report["platforms"][0]["current_nick"] == "-T.E.S-ExamplePlayer"
    assert report["names"][0]["nick"] == "-T.E.S-ExamplePlayer"


def test_historical_nbsp_nick_is_queried_and_displayed_canonically(tmp_path):
    account_id = "0123456789abcdef01234567"
    legacy_nick = "Example\u00a0o"
    with TrackingStore(tmp_path / "track.db") as store:
        riven_no = store.ingest(
            _message(
                timestamp=100,
                seller_id=account_id,
                nick="Example o",
                platform="xbox",
            ),
            dedupe_seconds=0,
        )[0].riven_no
        store.connection.execute(
            "UPDATE player_platforms SET current_nick=?",
            (legacy_nick,),
        )
        store.connection.execute(
            "UPDATE player_nicks SET nick=?",
            (legacy_nick,),
        )
        store.connection.execute(
            "UPDATE rivens SET first_seller_nick=?",
            (legacy_nick,),
        )
        store.connection.execute(
            "UPDATE riven_ownerships SET holder_nick=?",
            (legacy_nick,),
        )

        matches = store.find_players("Example o")
        player = store.player_report(account_id)
        riven = store.riven_report(riven_no)

    assert matches[0]["nick"] == "Example o"
    assert matches[0]["matched_identities"][0]["nick"] == "Example o"
    assert player["nick"] == "Example o"
    assert player["platforms"][0]["current_nick"] == "Example o"
    assert player["names"][0]["nick"] == "Example o"
    assert riven["first_seller_nick"] == "Example o"
    assert riven["ownerships"][0]["to_nick"] == "Example o"


def test_same_holder_observation_updates_range_without_new_record(tmp_path):
    text = "200pl 出" + RIVEN_TEXT
    newer = {
        "t": 200,
        "nick": "Seller",
        "sender_id": "0123456789abcdef01234567",
        "chan": "#T_ZH",
        "text": text,
    }
    older = {**newer, "t": 100}
    with TrackingStore(tmp_path / "track.db") as store:
        store.ingest(newer, dedupe_seconds=0, historical=True)
        store.ingest(newer, dedupe_seconds=0, historical=True)
        store.ingest(older, dedupe_seconds=0, historical=True)
        riven = store.connection.execute(
            "SELECT first_seen, last_seen, max_rerolls FROM rivens"
        ).fetchone()
        ownerships = store.connection.execute(
            "SELECT COUNT(*) FROM riven_ownerships"
        ).fetchone()[0]

    assert tuple(riven[:2]) == (100, 200)
    assert ownerships == 1


def test_content_fingerprint_uses_roll_bits_not_rank_or_rerolls():
    match = riven_link.OMG_RE.search(RIVEN_TEXT)
    card = riven_link.decode_link(match.group(1), match.group(2))
    original = content_fingerprint(match.group(1), card)

    display_change = deepcopy(card)
    display_change["lvl"] = 0
    display_change["rerolls"] += 50
    display_change["stats"].reverse()
    assert content_fingerprint(match.group(1), display_change) == original

    roll_change = deepcopy(card)
    roll_change["stats"][0]["_float_bits"] ^= 1
    assert content_fingerprint(match.group(1), roll_change) != original


def test_global_fingerprint_dedupe_spans_channels_and_restart(tmp_path):
    path = tmp_path / "track.db"
    with TrackingStore(path) as store:
        first = store.ingest(
            _message(timestamp=100, channel="#T_ZH"), dedupe_seconds=3600)
        second = store.ingest(
            _message(timestamp=200, channel="#T_EN_NA"), dedupe_seconds=3600)
        assert [item.push_allowed for item in first] == [True]
        assert [item.push_allowed for item in second] == [False]
        assert first[0].dedupe_elapsed_seconds is None
        assert second[0].dedupe_elapsed_seconds == 100
        number = first[0].riven_no

    with TrackingStore(path) as restarted:
        third = restarted.ingest(
            _message(timestamp=3800, channel="#T_FR"), dedupe_seconds=3600)
        assert [item.push_allowed for item in third] == [True]
        assert third[0].dedupe_elapsed_seconds == 3600
        assert third[0].riven_no == number


def test_deleted_riven_number_is_never_reused(tmp_path):
    with TrackingStore(tmp_path / "track.db") as store:
        first = store.ingest(
            _message(timestamp=100), dedupe_seconds=0)[0]
        store.connection.execute(
            "DELETE FROM riven_push_dedupe WHERE content_hash=?",
            (first.fingerprint,),
        )
        store.connection.execute(
            "DELETE FROM riven_ownerships WHERE content_hash=?",
            (first.fingerprint,),
        )
        store.connection.execute(
            "DELETE FROM rivens WHERE content_hash=?", (first.fingerprint,),
        )
        second = store.ingest(
            _message(timestamp=200), dedupe_seconds=0)[0]

    assert first.riven_no == 1
    assert second.riven_no == 2


def test_anonymous_discovery_does_not_create_holder_attribution(tmp_path):
    with TrackingStore(tmp_path / "track.db") as store:
        observed = store.ingest(
            _message(timestamp=100, seller_id="", nick="Anonymous"),
            dedupe_seconds=3600,
        )
        ownership_count = store.connection.execute(
            "SELECT COUNT(*) FROM riven_ownerships"
        ).fetchone()[0]
        player_count = store.connection.execute(
            "SELECT COUNT(*) FROM players").fetchone()[0]

    assert observed and ownership_count == 0
    assert player_count == 0


def test_delivery_ingest_returns_current_distinct_player_riven_count(tmp_path):
    account_id = "0123456789abcdef01234567"
    with TrackingStore(tmp_path / "track.db") as store:
        first, first_count = store.ingest_with_player_riven_count(
            _message(timestamp=100, seller_id=account_id),
            dedupe_seconds=0,
        )
        multi = _message(timestamp=200, seller_id=account_id)
        multi["text"] = f"{RIVEN_TEXT} 重复{RIVEN_TEXT} 新卡{TORID_RIVEN_TEXT}"
        observed, current_count = store.ingest_with_player_riven_count(
            multi,
            dedupe_seconds=0,
        )
        anonymous = _message(timestamp=300, seller_id="", nick="Anonymous")
        _anonymous_observed, unknown_count = (
            store.ingest_with_player_riven_count(
                anonymous,
                dedupe_seconds=0,
            )
        )
        report_total = store.player_report(account_id)["pagination"]["total"]

    assert len(first) == 1 and first_count == 1
    assert len(observed) == 3 and current_count == 2
    assert report_total == current_count
    assert unknown_count is None


def test_transfer_chain_requires_ordered_stable_accounts(tmp_path):
    first_id = "0123456789abcdef01234567"
    second_id = "89abcdef0123456701234567"
    with TrackingStore(tmp_path / "track.db") as store:
        number = store.ingest(
            _message(timestamp=100, seller_id=first_id, nick="First"),
            dedupe_seconds=0,
        )[0].riven_no
        store.ingest(
            _message(timestamp=200, seller_id=second_id, nick="Second"),
            dedupe_seconds=0,
        )
        report = store.riven_report(number)

    assert [(row["from_nick"], row["to_nick"], row["observed_at"])
            for row in report["ownerships"]] == [
        (None, "First", 100),
        ("First", "Second", 200),
    ]
    assert "content_hash" not in report
    assert "first_event_key" not in report
    assert "first_seller_id" not in report
    assert all("holder_id" not in row and "event_key" not in row
               for row in report["ownerships"])


def test_riven_returning_to_original_holder_is_a_new_transfer(tmp_path):
    first_id = "0123456789abcdef01234567"
    second_id = "89abcdef0123456701234567"
    with TrackingStore(tmp_path / "track.db") as store:
        number = store.ingest(
            _message(timestamp=100, seller_id=first_id, nick="First"),
            dedupe_seconds=0,
        )[0].riven_no
        store.ingest(
            _message(timestamp=200, seller_id=second_id, nick="Second"),
            dedupe_seconds=0,
        )
        store.ingest(
            _message(timestamp=250, seller_id=second_id, nick="Second"),
            dedupe_seconds=0,
        )
        store.ingest(
            _message(timestamp=300, seller_id=first_id, nick="First"),
            dedupe_seconds=0,
        )
        report = store.riven_report(number, all_results=True)
        rows = store.connection.execute(
            """SELECT holder_nick,first_seen,last_seen FROM riven_ownerships
               ORDER BY first_seen"""
        ).fetchall()

    assert [tuple(row) for row in rows] == [
        ("First", 100, 100),
        ("Second", 200, 250),
        ("First", 300, 300),
    ]
    assert [(row["from_nick"], row["to_nick"])
            for row in report["ownerships"]] == [
        (None, "First"), ("First", "Second"), ("Second", "First"),
    ]


def test_public_tracking_reports_redact_identifiers_inside_nicknames(tmp_path):
    account_id = "0123456789abcdef01234567"
    fingerprint = "a" * 64
    nick = f"Alias {account_id} {fingerprint}"
    with TrackingStore(tmp_path / "track.db") as store:
        _observe_player(store,
            account_id, nick, platform="windows", observed_at=100)
        number = store.ingest(
            _message(timestamp=101, seller_id=account_id, nick=nick),
            dedupe_seconds=0,
        )[0].riven_no
        player = store.player_report(account_id)
        riven = store.riven_report(number)

    public = str((player, riven))
    assert account_id not in public
    assert fingerprint not in public


def test_same_second_multiple_stable_sellers_do_not_infer_direction(tmp_path):
    first_id = "0123456789abcdef01234567"
    second_id = "89abcdef0123456701234567"
    third_id = "fedcba987654321001234567"
    with TrackingStore(tmp_path / "track.db") as store:
        number = store.ingest(
            _message(timestamp=100, seller_id=first_id, nick="First"),
            dedupe_seconds=0,
        )[0].riven_no
        store.ingest(
            _message(timestamp=200, seller_id=second_id, nick="Second"),
            dedupe_seconds=0,
        )
        store.ingest(
            _message(timestamp=200, seller_id=third_id, nick="Third"),
            dedupe_seconds=0,
        )
        store.ingest(
            _message(timestamp=300, seller_id=first_id, nick="First"),
            dedupe_seconds=0,
        )
        report = store.riven_report(number)

    assert all(row["from_nick"] is None for row in report["ownerships"])


def test_zero_dedupe_window_allows_distinct_same_second_observations(tmp_path):
    first = _message(timestamp=100, nick="First", channel="#T_ZH")
    second = _message(timestamp=100, nick="Second", channel="#T_EN_NA")
    second["key"] = "second-channel-event"

    with TrackingStore(tmp_path / "track.db") as store:
        assert store.ingest(first, dedupe_seconds=0)[0].push_allowed is True
        assert store.ingest(second, dedupe_seconds=0)[0].push_allowed is True
        assert store.ingest(second, dedupe_seconds=0)[0].push_allowed is False


def test_presence_alert_claims_on_region_changes_within_online_session(tmp_path):
    account_id = "0123456789abcdef01234567"

    def event(kind: str, timestamp: int, *, channel: str = "#T_ZH") -> dict:
        return {
            "type": kind, "t": timestamp, "sender_id": account_id,
            "nick": "Player", "chan": channel, "observer_key": "run:A",
            "event_key": f"{kind}-{timestamp}-{channel}",
        }

    with TrackingStore(tmp_path / "track.db") as store:
        first = _observe_presence(store, event("join", 100))
        second_join = _observe_presence(store,
            event("join", 101, channel="#Q_ZH"))
        assert first["became_online"] is True
        assert second_join["session_id"] == first["session_id"]
        assert store.claim_presence_alert(
            [1], first["session_id"], "EN_NA", alerted_at=100,
        ) == [1]
        assert store.claim_presence_alert(
            [1], first["session_id"], "EN_NA", alerted_at=101,
        ) == []
        assert store.claim_presence_alert(
            [1], first["session_id"], "EN_EU", alerted_at=102,
        ) == [1]
        assert store.claim_presence_alert(
            [1], first["session_id"], "EN_EU", alerted_at=103,
        ) == []
        assert store.claim_presence_alert(
            [1], first["session_id"], "EN_NA", alerted_at=104,
        ) == [1]

        quit_result = _observe_presence(store, event("quit", 200))
        assert quit_result["left_all_channels"] is True
        assert quit_result["ended_session_id"] == first["session_id"]
        next_session = _observe_presence(store, event("join", 300))
        assert next_session["session_id"] != first["session_id"]
        assert store.claim_presence_alert(
            [1], next_session["session_id"], "EN_NA", alerted_at=300,
        ) == [1]


def test_channel_snapshots_reconcile_memberships_without_creating_alerts(
        tmp_path):
    first_id = "0123456789abcdef01234567"
    second_id = "89abcdef0123456701234567"
    third_id = "abcdef012345670123456789"

    def snapshot(snapshot_id: str, timestamp: int, members: list[list[str]]):
        return {
            "type": "channel_snapshot",
            "snapshot_id": snapshot_id,
            "t": timestamp,
            "chan": "#T_ZH",
            "observer_key": "run:A",
            "members": members,
        }

    with TrackingStore(tmp_path / "track.db") as store:
        first = _observe_presence(store, snapshot("snapshot-1", 100, [
            [first_id, "First\ue000"],
            [second_id, "Second\ue001"],
        ]))
        replay = _observe_presence(store, snapshot("snapshot-1", 100, [
            [first_id, "First\ue000"],
        ]))
        first_session = store.connection.execute(
            "SELECT id FROM presence_sessions WHERE account_id=?",
            (first_id,),
        ).fetchone()[0]
        live_join = _observe_presence(store, {
            "type": "join", "t": 110, "sender_id": first_id,
            "irc_nick": "First\ue000", "chan": "#Q_ZH",
            "observer_key": "run:A", "event_key": "first-live-join",
        })
        second = _observe_presence(store, snapshot("snapshot-2", 120, [
            [first_id, "First Renamed\ue000"],
            [third_id, "Third\ue002"],
        ]))
        memberships = store.connection.execute(
            """SELECT s.account_id,m.channel,m.discovered_via,
                      m.last_confirmed_at,m.left_at
               FROM presence_memberships m
               JOIN presence_sessions s ON s.id=m.session_id
               ORDER BY s.account_id,m.channel"""
        ).fetchall()
        second_session = store.connection.execute(
            "SELECT ended_at,end_reason FROM presence_sessions "
            "WHERE account_id=?",
            (second_id,),
        ).fetchone()
        alerts = store.connection.execute(
            "SELECT COUNT(*) FROM presence_alerts"
        ).fetchone()[0]

    assert first["recorded"] is True
    assert first["added_memberships"] == 2
    assert first["session_id"] is None
    assert replay["recorded"] is False
    assert live_join["session_id"] == first_session
    assert live_join["became_online"] is False
    assert second["added_memberships"] == 1
    assert second["closed_memberships"] == 1
    assert [tuple(row) for row in memberships] == [
        (first_id, "#Q_ZH", "event", 110, None),
        (first_id, "#T_ZH", "snapshot", 120, None),
        (second_id, "#T_ZH", "snapshot", 100, 120),
        (third_id, "#T_ZH", "snapshot", 120, None),
    ]
    assert tuple(second_session) == (120, "snapshot_absent")
    assert alerts == 0


def test_channel_snapshot_requires_explicit_snapshot_id(tmp_path):
    with TrackingStore(tmp_path / "track.db") as store:
        with pytest.raises(ValueError, match="snapshot_id"):
            _observe_presence(store, {
                "type": "channel_snapshot", "t": 100,
                "chan": "#T_ZH", "observer_key": "run:A", "members": [],
            })
        assert store.connection.execute(
            "SELECT COUNT(*) FROM presence_snapshots"
        ).fetchone()[0] == 0


def test_channel_snapshot_lookup_uses_open_membership_index(tmp_path):
    with TrackingStore(tmp_path / "track.db") as store:
        plan = [row[3] for row in store.connection.execute(
            """EXPLAIN QUERY PLAN
               SELECT m.id,m.session_id,s.account_id
               FROM presence_memberships m
               JOIN presence_sessions s ON s.id=m.session_id
               WHERE m.channel=? AND m.observer_key=? AND m.left_at IS NULL
                 AND s.ended_at IS NULL""",
            ("#T_ZH", "run:A"),
        )]

    assert any(
        "ix_presence_membership_channel_open" in step for step in plan
    )


def test_last_part_ends_visible_session_and_same_region_rejoin_realerts(
        tmp_path):
    account_id = "0123456789abcdef01234567"

    def event(kind: str, timestamp: int, channel: str) -> dict:
        return {
            "type": kind, "t": timestamp, "sender_id": account_id,
            "nick": "Player", "chan": channel, "observer_key": "run:A",
            "event_key": f"{kind}-{timestamp}-{channel}",
        }

    with TrackingStore(tmp_path / "track.db") as store:
        first = _observe_presence(store, event("join", 100, "#G_ZH"))
        second_join = _observe_presence(store, event("join", 101, "#Q_ZH"))
        assert second_join["session_id"] == first["session_id"]
        assert store.claim_presence_alert(
            [1], first["session_id"], "ZH", alerted_at=100,
        ) == [1]

        first_part = _observe_presence(store, event("part", 110, "#G_ZH"))
        last_part = _observe_presence(store, event("part", 111, "#Q_ZH"))
        assert first_part["left_all_channels"] is False
        assert last_part["left_all_channels"] is True
        assert last_part["ended_session_id"] == first["session_id"]

        next_session = _observe_presence(store, event("join", 120, "#G_ZH"))
        assert next_session["session_id"] != first["session_id"]
        assert store.claim_presence_alert(
            [1], next_session["session_id"], "ZH", alerted_at=120,
        ) == [1]
        closed = store.connection.execute(
            "SELECT end_reason FROM presence_sessions WHERE id=?",
            (first["session_id"],),
        ).fetchone()

    assert closed["end_reason"] == "all_parts"


def test_join_restarts_legacy_session_with_no_active_memberships(tmp_path):
    account_id = "0123456789abcdef01234567"

    def event(timestamp: int) -> dict:
        return {
            "type": "join", "t": timestamp, "sender_id": account_id,
            "nick": "Player", "chan": "#G_ZH", "observer_key": "run:A",
            "event_key": f"join-{timestamp}",
        }

    with TrackingStore(tmp_path / "track.db") as store:
        first = _observe_presence(store, event(100))
        store.connection.execute(
            "UPDATE presence_memberships SET left_at=110 WHERE session_id=?",
            (first["session_id"],),
        )

        next_session = _observe_presence(store, event(120))
        old_session = store.connection.execute(
            "SELECT ended_at,end_reason FROM presence_sessions WHERE id=?",
            (first["session_id"],),
        ).fetchone()

    assert next_session["session_id"] != first["session_id"]
    assert next_session["became_online"] is True
    assert old_session["ended_at"] == 120
    assert old_session["end_reason"] == "memberships_empty"


def test_same_second_nick_event_authoritatively_updates_current_name(tmp_path):
    account_id = "0123456789abcdef01234567"
    with TrackingStore(tmp_path / "track.db") as store:
        _observe_presence(store, {
            "type": "join", "t": 100, "sender_id": account_id,
            "nick": "OldNick", "chan": "#T_ZH", "observer_key": "run:A",
            "event_key": "join-old",
        })
        _observe_presence(store, {
            "type": "nick", "t": 100, "sender_id": account_id,
            "nick": "NewNick", "old_nick": "OldNick", "chan": "",
            "observer_key": "run:A", "event_key": "nick-new",
        })
        player = store.find_players(account_id)[0]
        report = store.player_report(account_id)

    assert player["nick"] == "NewNick"
    assert {row["nick"] for row in report["names"]} == {"OldNick", "NewNick"}


def test_first_nick_event_preserves_protocol_old_name(tmp_path):
    account_id = "0123456789abcdef01234567"
    with TrackingStore(tmp_path / "track.db") as store:
        _observe_presence(store, {
            "type": "nick",
            "t": "2026-07-28T12:00:00.125+00:00",
            "sender_id": account_id,
            "old_nick": "BeforeRename",
            "nick": "AfterRename",
            "observer_key": "run:A",
        })
        player = store.player_report(account_id)

    assert player is not None
    assert player["nick"] == "AfterRename"
    assert {row["nick"] for row in player["names"]} == {
        "BeforeRename", "AfterRename",
    }


def test_same_second_reconnects_get_distinct_fallback_event_keys(tmp_path):
    account_id = "0123456789abcdef01234567"

    def event(kind: str, timestamp: str) -> dict:
        return {
            "type": kind,
            "t": timestamp,
            "sender_id": account_id,
            "nick": "Player",
            "chan": "#T_ZH" if kind == "join" else "",
            "observer_key": "run:A",
        }

    with TrackingStore(tmp_path / "track.db") as store:
        first = _observe_presence(store, event(
            "join", "2026-07-28T00:00:00.100000+00:00"))
        _observe_presence(store, event(
            "quit", "2026-07-28T00:00:00.200000+00:00"))
        second = _observe_presence(store, event(
            "join", "2026-07-28T00:00:00.300000+00:00"))

    assert first["session_id"] is not None
    assert second["session_id"] is not None
    assert second["session_id"] != first["session_id"]


def test_observer_stop_closes_session_with_only_that_observer(tmp_path):
    account_id = "0123456789abcdef01234567"

    def event(kind: str, timestamp: int, *, observer="run:A") -> dict:
        return {
            "type": kind,
            "t": timestamp,
            "sender_id": account_id,
            "nick": "Player",
            "chan": "#T_ZH" if kind in {"join", "part"} else "",
            "observer_key": observer,
            "event_key": f"{kind}-{timestamp}-{observer}",
        }

    with TrackingStore(tmp_path / "track.db") as store:
        first = _observe_presence(store, event("join", 100))
        _observe_presence(store, event("observer_stop", 120))
        second = _observe_presence(store, event(
            "join", 130, observer="next:A"))

    assert first["session_id"] is not None
    assert second["session_id"] is not None
    assert second["session_id"] != first["session_id"]


def test_new_observer_generation_closes_session_from_stale_observer(tmp_path):
    account_id = "0123456789abcdef01234567"

    def event(kind: str, timestamp: int, observer: str, channel: str = "") -> dict:
        return {
            "type": kind,
            "t": timestamp,
            "sender_id": account_id,
            "nick": "Player",
            "chan": channel,
            "observer_key": observer,
            "event_key": f"{kind}-{timestamp}-{observer}",
        }

    with TrackingStore(tmp_path / "track.db") as store:
        first = _observe_presence(store, event(
            "join", 100, "old:A", "#T_ZH"))
        _observe_presence(store, event(
            "observer_start", 120, "new:A"))
        second = _observe_presence(store, event(
            "join", 130, "new:A", "#T_ZH"))

    assert second["session_id"] != first["session_id"]


def test_nickname_lookup_is_driven_by_nickname_index(tmp_path):
    path = tmp_path / "track.db"
    with TrackingStore(path) as store:
        for index in range(200):
            _observe_player(store,
                f"{index:024x}", f"Player{index}",
                platform="windows", observed_at=index,
            )
        assert store.find_players("Player137")[0]["nick"] == "Player137"
        plan = [row[3] for row in store.connection.execute(
            """EXPLAIN QUERY PLAN
               SELECT DISTINCT p.account_id,p.first_seen,p.last_seen
               FROM player_nicks n INDEXED BY ix_nicks_name
               JOIN players p ON p.account_id=n.account_id
               WHERE n.nick IN (?,?) COLLATE NOCASE
               ORDER BY p.last_seen DESC,p.account_id""",
            ("Player 137", "Player\u00a0137"),
        )]

    assert any("ix_nicks_name" in step for step in plan)
    assert all("SCAN p" not in step for step in plan)


def test_read_only_tracking_connection_queries_during_writer_lifetime(tmp_path):
    path = tmp_path / "track.db"
    with TrackingStore(path) as writer:
        _observe_player(writer,
            "0123456789abcdef01234567", "Player",
            platform="windows", observed_at=100,
        )
        with TrackingStore(path, read_only=True) as reader:
            assert reader.find_players("Player")[0]["nick"] == "Player"
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                reader.connection.execute(
                    "UPDATE players SET last_seen=200")


def test_presence_batch_and_retention_keep_permanent_history_and_open_state(
        tmp_path):
    first_id = "0123456789abcdef01234567"
    second_id = "89abcdef0123456701234567"

    def event(kind: str, timestamp: int, account_id: str, key: str) -> dict:
        return {
            "type": kind,
            "t": timestamp,
            "sender_id": account_id,
            "nick": "Player",
            "chan": "#T_ZH" if kind in {"join", "part"} else "",
            "observer_key": "run:A",
            "event_key": key,
        }

    with TrackingStore(tmp_path / "track.db") as store:
        old_join, old_quit, open_join, recent_join = store.observe_presence_batch((
            event("join", 100, first_id, "old-join"),
            event("quit", 110, first_id, "old-quit"),
            event("join", 120, second_id, "open-join"),
            event("join", 300, first_id, "recent-join"),
        ))
        assert old_join["session_id"] == old_quit["ended_session_id"]
        assert open_join["session_id"] != recent_join["session_id"]
        store.claim_presence_alert(
            [1], old_join["session_id"], "ZH", alerted_at=100)

        result = store.prune_presence_history(
            200, batch_size=1, max_batches=20)
        counts = {
            table: store.connection.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "players", "presence_events", "presence_sessions",
                "presence_memberships", "presence_alerts",
            )
        }
        open_sessions = store.connection.execute(
            "SELECT COUNT(*) FROM presence_sessions WHERE ended_at IS NULL"
        ).fetchone()[0]

    assert result == {
        "events": 3,
        "snapshots": 0,
        "sessions": 1,
        "memberships": 1,
        "alerts": 1,
        "remaining": False,
    }
    assert counts == {
        "players": 2,
        "presence_events": 1,
        "presence_sessions": 2,
        "presence_memberships": 2,
        "presence_alerts": 0,
    }
    assert open_sessions == 2


async def test_tracking_queries_use_bounded_background_concurrency():
    active = 0
    peak = 0
    lock = threading.Lock()

    def query() -> None:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1

    await asyncio.gather(*(run_tracking_query(query) for _ in range(12)))

    assert peak == 4
