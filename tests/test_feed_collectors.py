"""IRC 文件消费、追踪、投递与捡漏运行时回归。"""

import asyncio
import json
import os
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.plugins.riven_sniper import bargain, marketdata
from src.plugins.riven_sniper import irc_feed as irc_feed_mod
from src.plugins.riven_sniper.bargainpoller import BargainPoller
from src.plugins.riven_sniper.chat_collector.runtime import pid_identity
from src.plugins.riven_sniper.delivery import DeliveryItem, DeliverySource
from src.plugins.riven_sniper.feed_health import (
    irc_feed_file_health,
    irc_feed_paths,
    irc_feed_sources_health,
)
from src.plugins.riven_sniper.feed_cursor import JsonlCheckpointReader
from src.plugins.riven_sniper.irc_feed import IrcChatFeed
from src.plugins.riven_sniper.store import Store


VALID_RIVEN = (
    "[OMG-LotusRifleRandomModRare:"
    "9b51zSsL1EkVt78mCp1r4/fpQ+1jxghk]"
)
TORID_RIVEN = (
    "[OMG-LotusRifleRandomModRare:"
    "8T9NiKpb9TjuN70Tfedj9RLDSiibxgUk]"
)


class FakeDeliveryPoller:
    def __init__(self, queue=None, *, channel_scopes=()):
        self.queue = queue or asyncio.Queue()
        self.store = Store(":memory:")
        for scope_id in channel_scopes:
            self.store.upsert_qq_target(scope_id, scope_id, enabled=True)
            self.store.set_target_channel_enabled(scope_id, True)
            self.store.add_config(
                scope_id, weapon="vectis", wildcard=None,
                positives=[
                    ["toxin_damage"], ["critical_chance"], ["multishot"],
                ],
                negatives=[["magazine_capacity"]],
            )
        self.generation = None
        self.accepting = False
        self.inflight = 0

    def activate_source(self, source, generation):
        assert source == DeliverySource.IRC
        self.generation = generation
        self.accepting = True

    def new_delivery(
            self, source, target, payload, *, generation=None,
            observed_at=None, now=None):
        created_at = time.time() if now is None else now
        return DeliveryItem(
            source, target, payload, 0, created_at, created_at + 60,
            generation, observed_at)

    def enqueue_delivery(self, item):
        if item.source == DeliverySource.IRC and (
                not self.accepting or item.generation != self.generation):
            return False
        self.queue.put_nowait(item)
        return True

    def cancel_source(self, source, generation=None):
        removed = 0
        retained = []
        while not self.queue.empty():
            item = self.queue.get_nowait()
            if item.source == source and (
                    generation is None or item.generation == generation):
                removed += 1
            else:
                retained.append(item)
        for item in retained:
            self.queue.put_nowait(item)
        self.accepting = False
        return {
            "queued": removed, "retrying": 0, "inflight": self.inflight,
        }

    @property
    def delivery_status(self):
        irc = sum(
            item.source == DeliverySource.IRC
            for item in tuple(getattr(self.queue, "_queue", ())))
        return {
            "queued_by_source": {"irc": irc},
            "inflight_by_source": {"irc": self.inflight},
        }


def test_irc_feed_file_health_distinguishes_missing_live_and_stale(tmp_path):
    path = tmp_path / "chat.jsonl"
    assert irc_feed_file_health(path, now=200)["status"] == "missing"

    path.write_text("{}\n", encoding="utf-8")
    path.touch()
    timestamp = path.stat().st_mtime
    assert irc_feed_file_health(
        path, stale_seconds=180, now=timestamp + 179)["status"] == "live"
    stale = irc_feed_file_health(
        path, stale_seconds=180, now=timestamp + 180)
    assert stale["status"] == "stale"
    assert stale["last_event_at"] == timestamp
    assert stale["age_seconds"] == 180


def test_four_slot_health_reports_partial_coverage_as_degraded(tmp_path):
    paths = tuple(tmp_path / f"privmsg_2026-07-26_{slot}.jsonl" for slot in "ABCD")
    paths[0].write_text("{}\n", encoding="utf-8")
    now = paths[0].stat().st_mtime + 1

    health = irc_feed_sources_health(paths, stale_seconds=180, now=now)

    assert health["status"] == "degraded"
    assert health["live_source_count"] == 1
    assert health["expected_source_count"] == 4
    assert [source["status"] for source in health["sources"].values()] == [
        "live", "missing", "missing", "missing",
    ]


def test_feed_paths_follow_seventeen_slot_mode(tmp_path):
    runtime = tmp_path / "chat_collector"
    feed_dir = runtime / "feed"
    feed_dir.mkdir(parents=True)
    (runtime / "collector_mode.json").write_text(
        json.dumps({"mode": "17"}), encoding="utf-8",
    )

    paths = irc_feed_paths(feed_dir=feed_dir, now=0)

    assert len(paths) == 17
    assert paths[0].name == "privmsg_1970-01-01_A.jsonl"
    assert paths[-1].name == "privmsg_1970-01-01_Q.jsonl"


def test_four_slot_health_uses_worker_heartbeat_when_filtered_feed_is_quiet(
        tmp_path):
    feed_dir = tmp_path / "feed"
    states_dir = tmp_path / "states"
    feed_dir.mkdir()
    states_dir.mkdir()
    now = time.time()
    updated_at = datetime.fromtimestamp(now - 30, timezone.utc).isoformat()
    paths = tuple(
        feed_dir / f"privmsg_2026-07-26_{slot}.jsonl" for slot in "ABCD"
    )
    for slot in "ABCD":
        (states_dir / f"{slot}.json").write_text(json.dumps({
            "slot": slot,
            "status": "listening",
            "pid": os.getpid(),
            "process_identity": pid_identity(os.getpid()),
            "joined": [f"#T_{slot}"],
            "updated_at": updated_at,
            "privmsg_count": 0,
            "filtered_privmsg_count": 100,
        }), encoding="utf-8")

    health = irc_feed_sources_health(paths, stale_seconds=180, now=now)

    assert health["status"] == "live"
    assert health["live_source_count"] == 4
    assert all(
        source["source"] == "collector_state"
        for source in health["sources"].values()
    )


def test_reconnecting_worker_is_reported_as_starting(tmp_path):
    feed_dir = tmp_path / "feed"
    states_dir = tmp_path / "states"
    feed_dir.mkdir()
    states_dir.mkdir()
    now = time.time()
    path = feed_dir / "privmsg_2026-08-13_A.jsonl"
    (states_dir / "A.json").write_text(json.dumps({
        "slot": "A",
        "status": "reconnecting",
        "pid": os.getpid(),
        "process_identity": pid_identity(os.getpid()),
        "joined": [],
        "updated_at": datetime.fromtimestamp(
            now - 10, timezone.utc,
        ).isoformat(),
    }), encoding="utf-8")

    health = irc_feed_file_health(path, stale_seconds=180, now=now)

    assert health["status"] == "starting"
    assert health["process_alive"] is True


def test_four_slot_health_preserves_worker_channel_degradation(tmp_path):
    feed_dir = tmp_path / "feed"
    states_dir = tmp_path / "states"
    feed_dir.mkdir()
    states_dir.mkdir()
    now = time.time()
    updated_at = datetime.fromtimestamp(now - 10, timezone.utc).isoformat()
    paths = tuple(
        feed_dir / f"privmsg_2026-07-26_{slot}.jsonl" for slot in "ABCD"
    )
    for slot in "ABCD":
        status = "degraded" if slot == "B" else "listening"
        (states_dir / f"{slot}.json").write_text(json.dumps({
            "slot": slot,
            "status": status,
            "pid": os.getpid(),
            "process_identity": pid_identity(os.getpid()),
            "joined": [f"#T_{slot}"],
            "missing": ["#Q_EN"] if status == "degraded" else [],
            "updated_at": updated_at,
        }), encoding="utf-8")

    health = irc_feed_sources_health(paths, stale_seconds=180, now=now)

    assert health["status"] == "degraded"
    assert health["live_source_count"] == 4
    assert health["sources"][str(paths[1])]["status"] == "degraded"


async def test_collector_stop_cancels_pending_irc_generation(tmp_path):
    runtime = tmp_path / "chat_collector"
    feed_dir = runtime / "feed"
    feed_dir.mkdir(parents=True)
    checkpoint = runtime / "cursor.json"
    control = runtime / "collector_control.json"
    path = feed_dir / "privmsg_2026-07-28_A.jsonl"
    control.write_text(json.dumps({
        "run_id": "run-a", "status": "running", "pid": os.getpid(),
        "process_identity": pid_identity(os.getpid()),
    }), encoding="utf-8")
    path.write_text(json.dumps({
        "collector_run_id": "run-a",
        "t": time.time(), "slot": "A", "nick": "Seller",
        "chan": "#T_ZH", "text": VALID_RIVEN,
    }) + "\n", encoding="utf-8")
    config = types.SimpleNamespace(
        irc_feed_dir=str(feed_dir), irc_feed_path="",
        irc_feed_checkpoint_path=str(checkpoint),
    )
    poller = FakeDeliveryPoller(channel_scopes=[123])
    feed = IrcChatFeed(config, poller)

    assert feed._sync_control() is True
    await feed._tick(path)
    assert poller.queue.qsize() == 1
    assert poller.queue.get_nowait().generation == "run-a"
    await feed._tick(path)
    assert poller.queue.empty()

    # 再放入一条待发 IRC，随后用监督器停止状态撤销。
    pending = poller.new_delivery(
        DeliverySource.IRC, ("group", 123), "pending",
        generation="run-a")
    assert poller.enqueue_delivery(pending)
    control.write_text(json.dumps({
        "run_id": "run-a", "status": "stopping",
    }), encoding="utf-8")
    assert feed._sync_control() is False
    assert poller.queue.empty()
    state = json.loads(
        (runtime / "irc_delivery_state.json").read_text(encoding="utf-8"))
    assert state["accepting"] is False
    assert state["cancelled"]["queued"] == 1


def test_collector_stop_refreshes_delivery_ack_until_inflight_drains(tmp_path):
    runtime = tmp_path / "chat_collector"
    feed_dir = runtime / "feed"
    feed_dir.mkdir(parents=True)
    control = runtime / "collector_control.json"
    control.write_text(json.dumps({
        "run_id": "run-a", "status": "running", "pid": os.getpid(),
        "process_identity": pid_identity(os.getpid()),
    }), encoding="utf-8")
    config = types.SimpleNamespace(
        irc_feed_dir=str(feed_dir), irc_feed_path="",
        irc_feed_checkpoint_path=str(runtime / "cursor.json"),
    )
    poller = FakeDeliveryPoller()
    feed = IrcChatFeed(config, poller)
    assert feed._sync_control() is True

    poller.inflight = 1
    control.write_text(json.dumps({
        "run_id": "run-a", "status": "stopping",
    }), encoding="utf-8")
    assert feed._sync_control() is False
    state_path = runtime / "irc_delivery_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["irc_inflight"] == 1
    assert state["cancelled"]["inflight"] == 1

    poller.inflight = 0
    assert feed._sync_control() is False
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["irc_inflight"] == 0
    assert state["cancelled"]["inflight"] == 1


def test_bot_started_after_collector_stop_acknowledges_existing_generation(
        tmp_path):
    runtime = tmp_path / "chat_collector"
    feed_dir = runtime / "feed"
    feed_dir.mkdir(parents=True)
    (runtime / "collector_control.json").write_text(json.dumps({
        "run_id": "stopped-run", "status": "stopped", "pid": 123,
        "process_identity": "dead",
    }), encoding="utf-8")
    config = types.SimpleNamespace(
        irc_feed_dir=str(feed_dir), irc_feed_path="",
        irc_feed_checkpoint_path=str(runtime / "cursor.json"),
    )
    poller = FakeDeliveryPoller()
    feed = IrcChatFeed(config, poller)

    assert poller.accepting is False
    assert feed._sync_control() is False
    state = json.loads(
        (runtime / "irc_delivery_state.json").read_text(encoding="utf-8"))
    assert state["run_id"] == "stopped-run"
    assert state["accepting"] is False
    assert state["irc_queued"] == 0
    assert state["irc_inflight"] == 0


async def test_new_collector_run_skips_old_generation_records(tmp_path):
    runtime = tmp_path / "chat_collector"
    feed_dir = runtime / "feed"
    feed_dir.mkdir(parents=True)
    path = feed_dir / "privmsg_2026-07-28_A.jsonl"
    control = runtime / "collector_control.json"
    control.write_text(json.dumps({
        "run_id": "run-b", "status": "running", "pid": os.getpid(),
        "process_identity": pid_identity(os.getpid()),
    }), encoding="utf-8")
    records = [
        {
            "collector_run_id": "run-a", "t": 1, "slot": "A",
            "nick": "Old", "chan": "#T_ZH", "text": VALID_RIVEN,
        },
        {
            "collector_run_id": "run-b", "t": 2, "slot": "A",
            "nick": "New", "chan": "#T_ZH", "text": VALID_RIVEN,
        },
    ]
    path.write_text("".join(
        json.dumps(record) + "\n" for record in records), encoding="utf-8")
    config = types.SimpleNamespace(
        irc_feed_dir=str(feed_dir), irc_feed_path="",
        irc_feed_checkpoint_path=str(runtime / "cursor.json"),
    )
    poller = FakeDeliveryPoller(channel_scopes=[123])
    feed = IrcChatFeed(config, poller)

    assert feed._sync_control() is True
    await feed._tick(path)

    assert poller.queue.qsize() == 1
    assert poller.queue.get_nowait().payload.seller == "New"


async def test_channel_feed_skips_undecodable_card_and_keeps_valid_mixed_card(
        tmp_path, monkeypatch):
    path = tmp_path / "chat.jsonl"
    checkpoint = tmp_path / "cursor.json"
    invalid = "[OMG-PlayerMeleeWeaponRandomModRare:OwAAEAAAAA==]"
    valid = (
        "[OMG-LotusRifleRandomModRare:"
        "9b51zSsL1EkVt78mCp1r4/fpQ+1jxghk]"
    )
    records = [
        {
            "dir": "in", "ts": 1, "nick": "Broken",
            "chan": "#T_ZH", "text": invalid,
        },
        {
            "dir": "in", "ts": 2, "nick": "Mixed Seller",
            "platform": "ios",
            "chan": "#T_ZH", "text": f"200出{invalid}，另一个{valid}",
        },
    ]
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    queue = asyncio.Queue()
    config = types.SimpleNamespace(
        irc_feed_checkpoint_path=str(checkpoint),
    )
    feed = IrcChatFeed(
        config, FakeDeliveryPoller(queue, channel_scopes=[123]))

    await feed._tick(path, dry=False)

    item = queue.get_nowait()
    target, pushed = item.target, item.payload
    assert target == 123
    assert pushed.seller == "Mixed Seller"
    assert pushed.seller_platform == "ios"
    assert len(pushed.cards) == 1
    assert pushed.cards[0].decoded["riven_name"] == "Sati-toxicron"
    assert queue.empty()
    assert JsonlCheckpointReader(checkpoint).read_complete(path).lines == ()


async def test_channel_payload_keeps_duplicate_name_context_without_details(
        tmp_path, monkeypatch):
    path = tmp_path / "chat.jsonl"
    text = f"第一张{VALID_RIVEN}，重复一张{VALID_RIVEN}"
    path.write_text(
        json.dumps({
            "dir": "in", "ts": time.time(), "nick": "Seller",
            "chan": "#T_ZH", "text": text,
        }) + "\n",
        encoding="utf-8",
    )
    queue = asyncio.Queue()
    config = types.SimpleNamespace()
    feed = IrcChatFeed(
        config, FakeDeliveryPoller(queue, channel_scopes=[123]),
    )

    await feed._tick(path, dry=False)

    item = queue.get_nowait()
    payload = item.payload
    assert payload.raw_text == text
    assert len(payload.cards) == 1
    assert len(payload.message_cards) == 2
    assert [card.duplicate for card in payload.message_cards] == [False, True]
    assert [card.card_index for card in payload.message_cards] == [0, 1]
    assert len(payload.display_cards) == 2
    assert [card.duplicate for card in payload.display_cards] == [False, True]
    assert payload.seller_riven_count is None


async def test_channel_feed_filters_each_targets_cards_by_sniper_configs(
        tmp_path, monkeypatch):
    account_id = "0123456789abcdef01234567"
    path = tmp_path / "chat.jsonl"
    path.write_text(json.dumps({
        "dir": "in", "ts": time.time(), "nick": "Seller",
        "sender_id": account_id,
        "chan": "#T_ZH",
        "text": f"{VALID_RIVEN} {TORID_RIVEN} 再发{TORID_RIVEN}",
    }) + "\n", encoding="utf-8")
    config = types.SimpleNamespace()
    poller = FakeDeliveryPoller()
    for scope_id in (111, 222, 333, 444, 555):
        poller.store.upsert_qq_target(scope_id, scope_id, enabled=True)
        poller.store.set_target_channel_enabled(scope_id, True)
    poller.store.add_config(
        111, weapon="vectis", wildcard=None,
        positives=[["toxin_damage"], ["critical_chance"], ["multishot"]],
        negatives=[["magazine_capacity"]],
    )
    poller.store.add_config(
        222, weapon="torid", wildcard=None,
        positives=[["critical_damage"], ["toxin_damage"], ["multishot"]],
        negatives=[["ammo_maximum"]],
    )
    poller.store.add_config(
        333, weapon="rubico", wildcard=None,
        positives=[["toxin_damage"], ["critical_chance"], ["multishot"]],
        negatives=[["magazine_capacity"]],
    )
    for weapon, positives, negatives in (
        ("vectis", [["toxin_damage"], ["critical_chance"], ["multishot"]],
         [["magazine_capacity"]]),
        ("torid", [["critical_damage"], ["toxin_damage"], ["multishot"]],
         [["ammo_maximum"]]),
    ):
        poller.store.add_config(
            444, weapon=weapon, wildcard=None,
            positives=positives, negatives=negatives)
    poller.store.add_config(
        555, weapon="vectis", wildcard=None,
        positives=[["toxin_damage"], ["critical_chance"], ["multishot"]],
        negatives=[["magazine_capacity"]],
    )
    poller.store.add_blacklist(
        555, "Seller", scope="channel")
    count_calls: list[str] = []
    count_player_rivens = irc_feed_mod.TrackingStore._count_player_rivens

    def counted_player_rivens(store, requested_account_id):
        count_calls.append(requested_account_id)
        return count_player_rivens(store, requested_account_id)

    monkeypatch.setattr(
        irc_feed_mod.TrackingStore,
        "_count_player_rivens",
        counted_player_rivens,
    )
    feed = IrcChatFeed(config, poller)

    await feed._tick(path)

    deliveries = [poller.queue.get_nowait() for _ in range(poller.queue.qsize())]
    assert [item.target for item in deliveries] == [111, 222, 444]
    assert count_calls == [account_id]
    assert [item.payload.seller_riven_count for item in deliveries] == [2, 2, 2]
    assert [[card.decoded["weapon_slug"] for card in item.payload.cards]
            for item in deliveries] == [
                ["vectis"], ["torid"], ["vectis", "torid"]]
    assert [[card.decoded["weapon_slug"]
             for card in item.payload.display_cards]
            for item in deliveries] == [
                ["vectis"],
                ["torid", "torid"],
                ["vectis", "torid", "torid"],
            ]
    assert [[card.duplicate for card in item.payload.display_cards]
            for item in deliveries] == [
                [False], [False, True], [False, False, True]]


async def test_channel_feed_applies_fixed_base_and_target_dedupe_hours(tmp_path):
    path = tmp_path / "chat.jsonl"
    records = [
        {
            "dir": "in", "ts": timestamp, "nick": f"Seller{index}",
            "chan": "#T_ZH", "text": VALID_RIVEN,
        }
        for index, timestamp in enumerate((100, 3699, 7300), start=1)
    ]
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    poller = FakeDeliveryPoller(channel_scopes=[111, 222])
    poller.store.set_target_channel_dedupe_hours(111, 1)
    poller.store.set_target_channel_dedupe_hours(222, 2)
    feed = IrcChatFeed(
        types.SimpleNamespace(irc_riven_dedupe_seconds=0), poller,
    )

    await feed._tick(path)

    deliveries = [poller.queue.get_nowait() for _ in range(poller.queue.qsize())]
    assert [(item.target, item.payload.seller) for item in deliveries] == [
        (111, "Seller1"),
        (222, "Seller1"),
        (111, "Seller3"),
    ]


async def test_slot_feed_retries_torn_line_and_resumes_checkpoint(tmp_path, monkeypatch):
    path = tmp_path / "privmsg_2026-07-26_A.jsonl"
    checkpoint = tmp_path / "cursor.json"
    first = {
        "t": "2026-07-26T01:02:03+00:00",
        "slot": "A",
        "nick": "First",
        "sender_id": "0123456789abcdef01234567",
        "chan": "#T_ZH",
        "text": f"first message {VALID_RIVEN}",
        "account": "CollectorA",
    }
    second = {**first, "t": "2026-07-26T02:02:04+00:00", "nick": "Second"}
    first_line = json.dumps(first, ensure_ascii=False) + "\n"
    second_line = json.dumps(second, ensure_ascii=False)
    path.write_text(first_line + second_line[:-2], encoding="utf-8")
    config = types.SimpleNamespace(irc_feed_checkpoint_path=str(checkpoint))
    first_poller = FakeDeliveryPoller(channel_scopes=[123])
    feed = IrcChatFeed(config, first_poller)
    await feed._tick(path, dry=False)
    assert first_poller.queue.get_nowait().payload.seller == "First"
    assert first_poller.queue.empty()

    with path.open("a", encoding="utf-8") as stream:
        stream.write(second_line[-2:] + "\n")
    second_poller = FakeDeliveryPoller(channel_scopes=[123])
    restarted = IrcChatFeed(config, second_poller)
    await restarted._tick(path, dry=False)

    assert second_poller.queue.get_nowait().payload.seller == "Second"
    assert second_poller.queue.empty()
    normalized = irc_feed_mod.normalize_record(first)
    assert normalized["sender_id"] == "0123456789abcdef01234567"
    assert normalized["slot"] == "A"
    assert normalized["platform"] == "unknown"


def test_normalize_record_extracts_platform_before_cleaning_private_glyph():
    normalized = irc_feed_mod.normalize_record({
        "t": 100,
        "slot": "A",
        "nick": "MobileSeller\ue004",
        "sender_id": "0123456789abcdef01234567",
        "chan": "#T_ZH",
        "text": f"message {VALID_RIVEN}",
    })

    assert normalized["nick"] == "MobileSeller"
    assert normalized["platform"] == "ios"


def test_normalize_record_parses_raw_cross_platform_nickname_with_spaces():
    account_id = "0123456789abcdef01234567"
    normalized = irc_feed_mod.normalize_record({
        "t": 100,
        "slot": "A",
        "raw": (
            f":Xbox\u00a0Seller\ue001!{account_id}_1@host "
            f"PRIVMSG #T_ZH :message {VALID_RIVEN}"
        ),
    })

    assert normalized["irc_nick"] == "Xbox\u00a0Seller\ue001"
    assert normalized["nick"] == "Xbox Seller"
    assert normalized["platform"] == "xbox"
    assert normalized["sender_id"] == account_id


def test_normalize_record_preserves_wire_nick_and_restores_game_nick():
    wire_nick = "`-T|E|S-ExamplePlayer\ue000"
    normalized = irc_feed_mod.normalize_record({
        "t": 100,
        "slot": "A",
        "nick": "-T.E.S-ExamplePlayer",
        "irc_nick": wire_nick,
        "sender_id": "0123456789abcdef01234567",
        "chan": "#T_ZH",
        "text": f"message {VALID_RIVEN}",
    })

    assert normalized["irc_nick"] == wire_nick
    assert normalized["nick"] == "-T.E.S-ExamplePlayer"
    assert normalized["platform"] == "windows"
    assert wire_nick in normalized["key"]


async def test_channel_blacklist_matches_restored_game_nick(tmp_path):
    path = tmp_path / "privmsg.jsonl"
    path.write_text(json.dumps({
        "dir": "in",
        "ts": time.time(),
        "nick": "ExamplePlayer.",
        "irc_nick": "ExamplePlayer|\ue000",
        "sender_id": "0123456789abcdef01234567",
        "chan": "#T_ZH",
        "text": VALID_RIVEN,
    }) + "\n", encoding="utf-8")
    config = types.SimpleNamespace(
        irc_feed_path=str(path),
        irc_feed_checkpoint_path=str(tmp_path / "cursor.json"),
        irc_track_db_path=str(tmp_path / "track.db"),
    )
    poller = FakeDeliveryPoller(channel_scopes=[123])
    poller.store.add_blacklist(123, "ExamplePlayer.", scope="channel")
    feed = IrcChatFeed(config, poller)

    await feed._tick(path)

    assert poller.queue.empty()
    assert feed._tracker.find_players("ExamplePlayer.")[0]["account_id"] == (
        "0123456789abcdef01234567"
    )
    feed._tracker.close()


async def test_channel_blacklist_matches_nbsp_space_nick(tmp_path):
    account_id = "0123456789abcdef01234567"
    path = tmp_path / "privmsg.jsonl"
    path.write_text(json.dumps({
        "dir": "in",
        "ts": time.time(),
        "irc_nick": "Example\u00a0o\ue001",
        "sender_id": account_id,
        "chan": "#T_ZH",
        "text": VALID_RIVEN,
    }) + "\n", encoding="utf-8")
    config = types.SimpleNamespace(
        irc_feed_path=str(path),
        irc_feed_checkpoint_path=str(tmp_path / "cursor.json"),
        irc_track_db_path=str(tmp_path / "track.db"),
    )
    poller = FakeDeliveryPoller(channel_scopes=[123])
    poller.store.add_blacklist(123, "Example o", scope="channel")
    feed = IrcChatFeed(config, poller)

    await feed._tick(path)

    assert poller.queue.empty()
    match = feed._tracker.find_players("Example o")[0]
    assert match["account_id"] == account_id
    assert match["nick"] == "Example o"
    feed._tracker.close()


async def test_feed_recovers_all_new_daily_files_after_downtime(
        tmp_path, monkeypatch):
    feed_dir = tmp_path / "feed"
    feed_dir.mkdir()
    checkpoint = tmp_path / "feed_cursor.json"
    today = datetime.now(timezone.utc).date()

    def write_day(day, nick):
        path = feed_dir / f"privmsg_{day.isoformat()}_A.jsonl"
        path.write_text(json.dumps({
            "t": f"{day.isoformat()}T01:00:00+00:00",
            "slot": "A",
            "nick": nick,
            "sender_id": "0123456789abcdef01234567",
            "chan": "#T_ZH",
            "text": f"message from {nick} {VALID_RIVEN}",
        }) + "\n", encoding="utf-8")
        return path

    oldest = write_day(today - timedelta(days=2), "Old")
    reader = JsonlCheckpointReader(
        checkpoint, namespace=irc_feed_mod._DELIVERY_CURSOR_NAMESPACE)
    old_batch = reader.read_complete(oldest)
    reader.commit(old_batch)
    reader.mark_initialized()
    write_day(today - timedelta(days=1), "Yesterday")
    write_day(today, "Today")

    config = types.SimpleNamespace(
        irc_feed_dir=str(feed_dir), irc_feed_path="",
        irc_feed_checkpoint_path=str(checkpoint),
    )
    poller = FakeDeliveryPoller(channel_scopes=[123])
    feed = IrcChatFeed(config, poller)
    delivery_reader = feed._ensure_reader()

    for path in feed._poll_source_paths(delivery_reader):
        await feed._tick(path, dry=False)

    assert [poller.queue.get_nowait().payload.seller for _ in range(2)] == [
        "Yesterday", "Today",
    ]
    assert poller.queue.empty()


def test_retention_deletes_only_old_files_consumed_by_every_cursor(tmp_path):
    feed_dir = tmp_path / "feed"
    feed_dir.mkdir()
    today = datetime.now(timezone.utc).date()
    old_day = today - timedelta(days=10)
    cutoff = today - timedelta(days=7)
    old_privmsg = feed_dir / f"privmsg_{old_day.isoformat()}_A.jsonl"
    unread_privmsg = feed_dir / f"privmsg_{old_day.isoformat()}_B.jsonl"
    old_presence = feed_dir / f"presence_{old_day.isoformat()}_A.jsonl"
    recent_presence = feed_dir / f"presence_{today.isoformat()}_A.jsonl"
    for path in (unread_privmsg, recent_presence):
        path.write_text('{"ok":true}\n', encoding="utf-8")
    for path in (old_privmsg, old_presence):
        path.write_bytes(b'{"ok":true}\n{"torn":')

    config = types.SimpleNamespace(
        irc_feed_dir=str(feed_dir),
        irc_feed_path="",
        irc_feed_checkpoint_path=str(tmp_path / "delivery_cursor.json"),
        irc_track_db_path=str(tmp_path / "track.db"),
    )
    feed = IrcChatFeed(config, FakeDeliveryPoller())
    delivery = feed._ensure_reader()
    tracking = feed._ensure_tracking_reader()
    presence = feed._ensure_presence_reader()
    for reader, paths in (
        (delivery, (old_privmsg, unread_privmsg)),
        (tracking, (old_privmsg,)),
        (presence, (old_presence, recent_presence)),
    ):
        for path in paths:
            batch = reader.read_complete(path)
            reader.commit(batch)
        reader.mark_initialized()

    result = feed._prune_consumed_feed_files(cutoff)

    assert result["files"] == 2
    assert old_privmsg.exists() is False
    assert old_presence.exists() is False
    assert unread_privmsg.exists() is True
    assert recent_presence.exists() is True
    assert delivery.has_source(old_privmsg) is False
    assert tracking.has_source(old_privmsg) is False
    assert presence.has_source(old_presence) is False


async def test_presence_alert_failure_does_not_skip_later_batch_results(
        tmp_path, monkeypatch):
    feed_dir = tmp_path / "feed"
    feed_dir.mkdir()
    path = feed_dir / "presence_2026-07-28_A.jsonl"
    events = [
        {
            "type": "join", "t": 100 + index,
            "sender_id": f"{index + 1:024x}", "nick": f"Player{index}",
            "chan": "#T_ZH", "observer_key": "run:A",
            "event_key": f"join-{index}",
        }
        for index in range(2)
    ]
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    config = types.SimpleNamespace(
        irc_feed_dir=str(feed_dir),
        irc_feed_path="",
        irc_track_db_path=str(tmp_path / "track.db"),
        discord_dm_enabled=False,
    )
    feed = IrcChatFeed(config, FakeDeliveryPoller())
    feed._ensure_tracker()
    reader = feed._ensure_presence_reader()
    handled = []

    async def handle(event, _result, *, dry):
        del dry
        if event["event_key"] == "join-0":
            raise RuntimeError("delivery failed")
        handled.append(event["event_key"])

    monkeypatch.setattr(feed, "_handle_presence_result", handle)

    await feed._presence_ticks((path,), reader, dry=False)

    assert handled == ["join-1"]
    assert reader.is_caught_up(path) is True
    assert feed._tracker.connection.execute(
        "SELECT COUNT(*) FROM presence_events"
    ).fetchone()[0] == 2
    feed._tracker.close()
    feed._tracker = None


async def test_maintenance_prunes_old_presence_after_cursor_is_safe(tmp_path):
    feed_dir = tmp_path / "feed"
    feed_dir.mkdir()
    track_db = tmp_path / "track.db"
    config = types.SimpleNamespace(
        irc_feed_dir=str(feed_dir),
        irc_feed_path="",
        irc_feed_checkpoint_path=str(tmp_path / "delivery_cursor.json"),
        irc_track_db_path=str(track_db),
        irc_feed_retention_days=7,
        irc_presence_retention_days=7,
    )
    feed = IrcChatFeed(config, FakeDeliveryPoller())
    presence = feed._prepare_presence_reader()
    assert presence is not None and presence.is_initialized
    feed._ensure_tracker()
    old = int(time.time()) - 8 * 86400
    account_id = "0123456789abcdef01234567"
    feed._tracker.observe_presence_batch((
        {
            "type": "join", "t": old, "sender_id": account_id,
            "nick": "Player", "chan": "#T_ZH",
            "observer_key": "run:A", "event_key": "old-join",
        },
        {
            "type": "quit", "t": old + 1, "sender_id": account_id,
            "nick": "Player", "chan": "",
            "observer_key": "run:A", "event_key": "old-quit",
        },
    ))

    await feed._maybe_maintain()

    assert feed._tracker.connection.execute(
        "SELECT COUNT(*) FROM presence_events").fetchone()[0] == 0
    assert feed._tracker.connection.execute(
        "SELECT COUNT(*) FROM presence_sessions").fetchone()[0] == 0
    assert feed._tracker.connection.execute(
        "SELECT COUNT(*) FROM players").fetchone()[0] == 1
    feed._tracker.close()


async def test_first_activation_skips_all_existing_daily_history(
        tmp_path, monkeypatch):
    feed_dir = tmp_path / "feed"
    feed_dir.mkdir()
    today = datetime.now(timezone.utc).date()
    paths = [
        feed_dir / f"privmsg_{(today - timedelta(days=offset)).isoformat()}_A.jsonl"
        for offset in (2, 1, 0)
    ]
    for index, path in enumerate(paths):
        path.write_text(json.dumps({
            "t": time.time(), "slot": "A", "nick": f"Old{index}",
            "chan": "#T_ZH", "text": "old",
        }) + "\n", encoding="utf-8")
    config = types.SimpleNamespace(
        irc_feed_dir=str(feed_dir), irc_feed_path="",
        irc_feed_checkpoint_path=str(tmp_path / "cursor.json"),
    )
    poller = FakeDeliveryPoller()
    feed = IrcChatFeed(config, poller)
    reader = feed._ensure_reader()
    initialize_at_end = reader.initialize_at_end
    failed_once = False

    def flaky_initialize(path):
        nonlocal failed_once
        if Path(path) == paths[1] and not failed_once:
            failed_once = True
            raise PermissionError("checkpoint locked")
        initialize_at_end(path)

    monkeypatch.setattr(reader, "initialize_at_end", flaky_initialize)

    assert feed._initialize_existing_sources(reader) is False
    assert feed._initialize_existing_sources(reader) is True
    assert {path.name for path in reader.tracked_paths()} == {
        path.name for path in paths
    }
    for path in feed._poll_source_paths(reader):
        await feed._tick(path, dry=False)

    assert poller.queue.empty()


async def test_platform_identity_upgrade_skips_old_delivery_tail(tmp_path):
    path = tmp_path / "privmsg.jsonl"
    path.write_bytes(b"")
    checkpoint = tmp_path / "feed_cursor.json"
    old_reader = JsonlCheckpointReader(
        checkpoint, namespace="channel-delivery-20260728",
    )
    old_reader.initialize_at_end(path)
    old_reader.mark_initialized()
    path.write_text(json.dumps({
        "t": 100,
        "nick": "OldUnread\ue000",
        "sender_id": "0123456789abcdef01234567",
        "chan": "#T_ZH",
        "text": VALID_RIVEN,
    }) + "\n", encoding="utf-8")
    config = types.SimpleNamespace(
        irc_feed_dir="", irc_feed_path=str(path),
        irc_feed_checkpoint_path=str(checkpoint),
        irc_track_db_path=str(tmp_path / "track.db"),
    )
    feed = IrcChatFeed(config, FakeDeliveryPoller())
    reader = feed._ensure_reader()

    assert feed._delivery_needs_initialization is True
    assert feed._initialize_existing_sources(reader) is True
    feed._delivery_needs_initialization = False
    feed._ensure_tracker(path)
    await feed._tick(path)

    assert feed._tracker.find_players("OldUnread") == []
    feed._tracker.close()


def test_first_presence_file_created_after_empty_activation_is_polled(tmp_path):
    feed_dir = tmp_path / "feed"
    feed_dir.mkdir()
    config = types.SimpleNamespace(
        irc_feed_dir=str(feed_dir),
        irc_feed_path="",
        irc_track_db_path=str(tmp_path / "track.db"),
    )
    feed = IrcChatFeed(config, FakeDeliveryPoller())
    reader = feed._prepare_presence_reader()
    assert reader is not None
    assert reader.has_sources is False

    path = feed_dir / "presence_2026-07-28_A.jsonl"
    path.write_text('{"type":"join"}\n', encoding="utf-8")
    (feed_dir / "presence_2026-07-28_X.jsonl").write_text(
        '{"type":"join"}\n', encoding="utf-8")

    assert feed._poll_presence_paths(reader) == (path,)


async def test_tracking_failure_keeps_independent_cursor_without_replaying_delivery(
        tmp_path, monkeypatch):
    path = tmp_path / "privmsg_2026-07-26_A.jsonl"
    path.write_text(json.dumps({
        "t": "2026-07-26T01:02:03+00:00",
        "slot": "A",
        "nick": "TrackedSeller",
        "sender_id": "0123456789abcdef01234567",
        "chan": "#T_ZH",
        "text": "plain trade message",
    }) + "\n", encoding="utf-8")
    delivery_checkpoint = tmp_path / "delivery.json"
    tracking_checkpoint = tmp_path / "track_history_cursor.json"
    config = types.SimpleNamespace(
        irc_feed_path=str(path), irc_feed_dir="",
        irc_feed_checkpoint_path=str(delivery_checkpoint),
    )
    feed = IrcChatFeed(config, FakeDeliveryPoller())

    await feed._tick(path, dry=False)

    class FailingTracker:
        def ingest_historical_batch(self, _messages, *, dedupe_seconds):
            del dedupe_seconds
            raise OSError("database unavailable")

    observed = []

    class WorkingTracker:
        def ingest_historical_batch(self, messages, *, dedupe_seconds):
            del dedupe_seconds
            observed.extend(messages)
            return len(messages)

    feed._history_tracker = FailingTracker()
    await feed._track_tick(path)
    assert JsonlCheckpointReader(tracking_checkpoint).read_complete(path).lines
    assert JsonlCheckpointReader(delivery_checkpoint).read_complete(path).lines == ()

    feed._history_tracker = WorkingTracker()
    await feed._track_tick(path)

    assert [message["nick"] for message in observed] == ["TrackedSeller"]
    assert JsonlCheckpointReader(tracking_checkpoint).read_complete(path).lines == ()


async def test_history_backlog_does_not_block_realtime_delivery(tmp_path):
    config = types.SimpleNamespace(
        irc_feed_dir="", irc_feed_path=str(tmp_path / "feed.jsonl"),
        irc_feed_interval=0.01, irc_feed_stale_seconds=180,
        sniper_dry_run=False,
    )
    poller = FakeDeliveryPoller()
    feed = IrcChatFeed(config, poller)
    feed._delivery_needs_initialization = False
    history_started = asyncio.Event()
    release_history = asyncio.Event()
    fresh_available = asyncio.Event()
    fresh_detected = asyncio.Event()
    source = tmp_path / "feed.jsonl"

    feed._ensure_reader = lambda _fallback=None: object()
    feed._prepare_presence_reader = lambda: None
    feed._prepare_tracking = lambda _fallback=None: True
    feed._ensure_tracking_reader = lambda _fallback=None: object()
    feed._source_paths = lambda: (source,)
    feed._poll_source_paths = lambda _reader, **_kwargs: (source,)
    feed._sync_control = lambda: True
    feed._report_health = lambda _health, _paths: None

    async def no_maintenance():
        pass

    async def track_backlog(_path):
        history_started.set()
        await release_history.wait()

    async def detect_fresh(_path, dry=False):
        del dry
        if fresh_available.is_set():
            fresh_detected.set()

    feed._maybe_maintain = no_maintenance
    feed._track_tick = track_backlog
    feed._tick = detect_fresh
    task = asyncio.create_task(feed._loop())
    try:
        await asyncio.wait_for(history_started.wait(), 1)
        fresh_available.set()
        await asyncio.wait_for(fresh_detected.wait(), 0.5)
    finally:
        release_history.set()
        feed._stop.set()
        await asyncio.wait_for(task, 1)
        poller.store.close()


async def test_tracking_retries_database_startup_skips_old_history_only(
        tmp_path, monkeypatch):
    path = tmp_path / "chat.jsonl"

    def record(nick):
        return json.dumps({
            "t": "2026-07-26T01:02:03+00:00",
            "slot": "A",
            "nick": nick,
            "sender_id": "0123456789abcdef01234567",
            "chan": "#T_ZH",
            "text": f"message from {nick}",
        }) + "\n"

    path.write_text(record("History"), encoding="utf-8")
    config = types.SimpleNamespace(
        irc_feed_path=str(path), irc_feed_dir="",
    )
    feed = IrcChatFeed(config, FakeDeliveryPoller())
    monkeypatch.setattr(
        feed,
        "_ensure_tracker",
        lambda _fallback=None: (_ for _ in ()).throw(
            OSError("database unavailable")
        ),
    )

    with pytest.raises(OSError, match="database unavailable"):
        feed._prepare_tracking(path)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(record("AfterStartup"))

    observed = []

    class WorkingTracker:
        def ingest_historical_batch(self, messages, *, dedupe_seconds):
            del dedupe_seconds
            observed.extend(messages)
            return len(messages)

    def start_tracker(_fallback=None):
        tracker = WorkingTracker()
        feed._tracker = tracker
        feed._history_tracker = tracker
        feed._presence_tracker = tracker

    monkeypatch.setattr(feed, "_ensure_tracker", start_tracker)
    assert feed._prepare_tracking(path) is True
    await feed._track_tick(path)

    assert [message["nick"] for message in observed] == ["AfterStartup"]


async def test_tracking_and_presence_first_activation_skip_existing_records(
        tmp_path):
    feed_dir = tmp_path / "feed"
    feed_dir.mkdir()
    day = datetime.now(timezone.utc).date().isoformat()
    privmsg = feed_dir / f"privmsg_{day}_A.jsonl"
    presence = feed_dir / f"presence_{day}_A.jsonl"
    old_id = "0123456789abcdef01234567"
    new_id = "89abcdef0123456701234567"

    def message(account_id, nick, timestamp):
        return {
            "t": timestamp, "slot": "A", "nick": nick,
            "sender_id": account_id, "chan": "#T_ZH",
            "text": f"message from {nick} {VALID_RIVEN}",
        }

    def join(account_id, nick, timestamp):
        return {
            "type": "join", "t": timestamp, "slot": "A", "nick": nick,
            "sender_id": account_id, "chan": "#T_ZH",
            "observer_key": "run:A", "event_key": f"join-{account_id}-{timestamp}",
        }

    privmsg.write_text(
        json.dumps(message(old_id, "OldPlayer", 100)) + "\n",
        encoding="utf-8")
    presence.write_text(
        json.dumps(join(old_id, "OldPlayer", 100)) + "\n",
        encoding="utf-8")
    config = types.SimpleNamespace(
        irc_feed_dir=str(feed_dir), irc_feed_path="",
        irc_track_db_path=str(tmp_path / "track.db"),
        discord_dm_enabled=False,
    )
    poller = FakeDeliveryPoller()
    poller.store.upsert_qq_target(123, 123, enabled=True)
    poller.store.set_target_channel_enabled(123, True)
    poller.store.add_player_tracker(123, "NewPlayer")
    feed = IrcChatFeed(config, poller)

    presence_reader = feed._prepare_presence_reader()
    assert feed._prepare_tracking() is True
    with privmsg.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(message(new_id, "NewPlayer", 200)) + "\n")
    with presence.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(join(new_id, "NewPlayer", 200)) + "\n")

    tracking_reader = feed._ensure_tracking_reader()
    for path in feed._poll_source_paths(tracking_reader):
        await feed._track_tick(path)
    for path in feed._poll_presence_paths(presence_reader):
        await feed._presence_ticks((path,), presence_reader, dry=False)

    assert feed._tracker.find_players("OldPlayer") == []
    assert feed._tracker.find_players("NewPlayer")[0]["account_id"] == new_id
    reminder = poller.queue.get_nowait()
    assert reminder.target == 123
    assert "NewPlayer" in reminder.payload
    tracker = poller.store.list_player_trackers(123)[0]
    assert tracker["resolved"] is True
    assert tracker["account_id"] == new_id
    assert poller.queue.empty()


async def test_presence_backlog_is_merged_chronologically_across_slots(tmp_path):
    feed_dir = tmp_path / "feed"
    feed_dir.mkdir()
    account_id = "0123456789abcdef01234567"
    day = "2026-07-28"
    (feed_dir / f"presence_{day}_A.jsonl").write_text(json.dumps({
        "type": "quit",
        "t": "2026-07-28T00:00:00.200000+00:00",
        "sender_id": account_id,
        "nick": "Player",
        "observer_key": "run:A",
        "event_key": "quit-later",
    }) + "\n", encoding="utf-8")
    (feed_dir / f"presence_{day}_B.jsonl").write_text(json.dumps({
        "type": "join",
        "t": "2026-07-28T00:00:00.100000+00:00",
        "sender_id": account_id,
        "nick": "Player",
        "chan": "#T_ZH",
        "observer_key": "run:B",
        "event_key": "join-earlier",
    }) + "\n", encoding="utf-8")
    config = types.SimpleNamespace(
        irc_feed_dir=str(feed_dir),
        irc_feed_path="",
        irc_track_db_path=str(tmp_path / "track.db"),
        discord_dm_enabled=False,
    )
    feed = IrcChatFeed(config, FakeDeliveryPoller())
    feed._ensure_tracker()
    reader = feed._ensure_presence_reader()

    await feed._presence_ticks(
        feed._presence_paths(), reader, dry=False)

    sessions = feed._tracker.connection.execute(
        "SELECT started_at,ended_at FROM presence_sessions"
    ).fetchall()
    assert [tuple(row) for row in sessions] == [(1785196800, 1785196800)]
    feed._tracker.close()
    feed._tracker = None


async def test_bounded_presence_backlog_keeps_cross_slot_chronology(
        tmp_path, monkeypatch):
    feed_dir = tmp_path / "feed"
    feed_dir.mkdir()
    day = "2026-07-28"

    def line(timestamp, key, *, padded=False):
        value = {
            "type": "observer_start",
            "t": timestamp,
            "observer_key": key,
            "event_key": key,
        }
        if padded:
            value["padding"] = "x" * 300
        return (json.dumps(value) + "\n").encode()

    a_lines = (line(100, "a-100"), line(400, "a-400"))
    b_lines = (
        line(200, "b-200", padded=True),
        line(300, "b-300", padded=True),
        line(500, "b-500", padded=True),
    )
    (feed_dir / f"presence_{day}_A.jsonl").write_bytes(b"".join(a_lines))
    (feed_dir / f"presence_{day}_B.jsonl").write_bytes(b"".join(b_lines))
    limit = max(len(a_lines[0]) + len(a_lines[1]), len(b_lines[0]))
    assert limit < len(b_lines[0]) + len(b_lines[1])
    monkeypatch.setattr(irc_feed_mod, "_READ_BATCH_BYTES", limit)

    config = types.SimpleNamespace(
        irc_feed_dir=str(feed_dir),
        irc_feed_path="",
        irc_track_db_path=str(tmp_path / "track.db"),
        discord_dm_enabled=False,
    )
    feed = IrcChatFeed(config, FakeDeliveryPoller())
    feed._ensure_tracker()
    reader = feed._ensure_presence_reader()

    for _ in range(3):
        await feed._presence_ticks(
            feed._poll_presence_paths(reader), reader, dry=False)

    keys = [row[0] for row in feed._tracker.connection.execute(
        "SELECT event_key FROM presence_events ORDER BY rowid")]
    assert keys == ["a-100", "b-200", "b-300", "a-400", "b-500"]
    assert all(reader.is_caught_up(path) for path in feed._presence_paths())
    feed._tracker.close()
    feed._tracker = None


async def test_presence_alerts_repeat_only_after_region_changes(tmp_path):
    feed_dir = tmp_path / "feed"
    feed_dir.mkdir()
    day = datetime.now(timezone.utc).date().isoformat()
    presence = feed_dir / f"presence_{day}_A.jsonl"
    account_id = "0123456789abcdef01234567"

    def event(kind, timestamp, channel=""):
        return {
            "type": kind,
            "t": timestamp,
            "sender_id": account_id,
            "nick": "RegionPlayer",
            "chan": channel,
            "observer_key": "run:A",
            "event_key": f"{kind}-{timestamp}-{channel}",
        }

    events = [
        event("join", 100, "#G_EN_NA"),
        event("join", 101, "#Q_EN_NA"),
        event("part", 102, "#G_EN_NA"),
        event("join", 103, "#R_EN_EU"),
        event("join", 104, "#T_EN_EU"),
        event("join", 105, "#G_EN_NA"),
        event("quit", 106),
        event("join", 107, "#G_EN_NA"),
    ]
    presence.write_text(
        "".join(json.dumps(item) + "\n" for item in events),
        encoding="utf-8",
    )
    config = types.SimpleNamespace(
        irc_feed_dir=str(feed_dir),
        irc_feed_path="",
        irc_track_db_path=str(tmp_path / "track.db"),
        discord_dm_enabled=False,
    )
    poller = FakeDeliveryPoller()
    poller.store.upsert_qq_target(123, 123, enabled=True)
    poller.store.set_target_channel_enabled(123, True)
    poller.store.add_player_tracker(
        123, "RegionPlayer", account_id=account_id)
    feed = IrcChatFeed(config, poller)
    feed._ensure_tracker()
    reader = feed._ensure_presence_reader()

    await feed._presence_ticks((presence,), reader, dry=False)

    reminders = []
    while not poller.queue.empty():
        reminders.append(poller.queue.get_nowait().payload)
    assert len(reminders) == 5
    assert "[北美]" in reminders[0]
    assert "[欧洲]" in reminders[1]
    assert "[北美]" in reminders[2]
    assert reminders[3] == "频道提醒：RegionPlayer 已离开所有受监控频道"
    assert "[北美]" in reminders[4]
    feed._tracker.close()
    feed._tracker = None


async def test_presence_last_part_alerts_leave_and_same_region_rejoin(
        tmp_path):
    feed_dir = tmp_path / "feed"
    feed_dir.mkdir()
    day = datetime.now(timezone.utc).date().isoformat()
    presence = feed_dir / f"presence_{day}_A.jsonl"
    account_id = "0123456789abcdef01234567"

    def event(kind, timestamp, channel=""):
        return {
            "type": kind,
            "t": timestamp,
            "sender_id": account_id,
            "nick": "RejoinPlayer",
            "chan": channel,
            "observer_key": "run:A",
            "event_key": f"{kind}-{timestamp}-{channel}",
        }

    events = [
        event("join", 100, "#G_ZH"),
        event("join", 101, "#Q_ZH"),
        event("part", 102, "#G_ZH"),
        event("part", 103, "#Q_ZH"),
        event("join", 104, "#G_ZH"),
    ]
    presence.write_text(
        "".join(json.dumps(item) + "\n" for item in events),
        encoding="utf-8",
    )
    config = types.SimpleNamespace(
        irc_feed_dir=str(feed_dir),
        irc_feed_path="",
        irc_track_db_path=str(tmp_path / "track.db"),
        discord_dm_enabled=False,
    )
    poller = FakeDeliveryPoller()
    poller.store.upsert_qq_target(123, 123, enabled=True)
    poller.store.set_target_channel_enabled(123, True)
    poller.store.add_player_tracker(
        123, "RejoinPlayer", account_id=account_id)
    feed = IrcChatFeed(config, poller)
    feed._ensure_tracker()
    reader = feed._ensure_presence_reader()

    await feed._presence_ticks((presence,), reader, dry=False)

    reminders = []
    while not poller.queue.empty():
        reminders.append(poller.queue.get_nowait().payload)
    assert reminders == [
        "频道提醒：RejoinPlayer 出现在 [简体中文]",
        "频道提醒：RejoinPlayer 已离开所有受监控频道",
        "频道提醒：RejoinPlayer 出现在 [简体中文]",
    ]
    feed._tracker.close()
    feed._tracker = None


async def test_snapshot_members_do_not_emit_orphan_presence_alerts(tmp_path):
    feed_dir = tmp_path / "feed"
    feed_dir.mkdir()
    day = datetime.now(timezone.utc).date().isoformat()
    presence = feed_dir / f"presence_{day}_A.jsonl"
    account_id = "0123456789abcdef01234567"
    events = [
        {
            "type": "channel_snapshot",
            "snapshot_id": "snapshot-1",
            "t": 100,
            "chan": "#G_ZH",
            "observer_key": "run:A",
            "members": [[account_id, "SnapshotPlayer\ue000"]],
        },
        {
            "type": "part", "t": 101, "sender_id": account_id,
            "irc_nick": "SnapshotPlayer\ue000", "chan": "#G_ZH",
            "observer_key": "run:A", "event_key": "snapshot-player-part",
        },
        {
            "type": "join", "t": 102, "sender_id": account_id,
            "irc_nick": "SnapshotPlayer\ue000", "chan": "#G_ZH",
            "observer_key": "run:A", "event_key": "snapshot-player-join",
        },
    ]
    presence.write_text(
        "".join(json.dumps(item) + "\n" for item in events),
        encoding="utf-8",
    )
    config = types.SimpleNamespace(
        irc_feed_dir=str(feed_dir),
        irc_feed_path="",
        irc_track_db_path=str(tmp_path / "track.db"),
        discord_dm_enabled=False,
    )
    poller = FakeDeliveryPoller()
    poller.store.upsert_qq_target(123, 123, enabled=True)
    poller.store.set_target_channel_enabled(123, True)
    poller.store.add_player_tracker(
        123, "SnapshotPlayer", account_id=account_id,
    )
    feed = IrcChatFeed(config, poller)
    feed._ensure_tracker()
    reader = feed._ensure_presence_reader()

    await feed._presence_ticks((presence,), reader, dry=False)

    reminders = []
    while not poller.queue.empty():
        reminders.append(poller.queue.get_nowait().payload)
    assert reminders == [
        "频道提醒：SnapshotPlayer 出现在 [简体中文]",
    ]
    assert feed._tracker.connection.execute(
        "SELECT COUNT(*) FROM presence_snapshots"
    ).fetchone()[0] == 1
    assert reader.is_caught_up(presence)
    feed._tracker.close()
    feed._tracker = None


def test_irc_targets_follow_database_and_disabled_platforms():
    config = types.SimpleNamespace(discord_dm_enabled=False)
    poller = FakeDeliveryPoller(channel_scopes=[111, 222])
    discord = poller.store.upsert_discord_target("1001")
    poller.store.set_target_channel_enabled(discord["scope_id"], True)
    poller.store.add_config(
        discord["scope_id"], weapon="torid", wildcard=None,
        positives=[["critical_damage"], ["multishot"]], negatives=[],
    )
    feed = IrcChatFeed(config, poller)

    assert feed._targets() == [111, 222]
    config.discord_dm_enabled = True
    assert feed._targets() == [discord["scope_id"], 111, 222]
    poller.store.set_target_enabled(111, False)
    poller.store.set_target_enabled(222, False)
    assert feed._targets() == [discord["scope_id"]]
    poller.store.set_target_enabled(discord["scope_id"], False)
    assert feed._targets() == []


def test_irc_targets_require_a_sniper_config():
    config = types.SimpleNamespace(discord_dm_enabled=False)
    poller = FakeDeliveryPoller()
    poller.store.upsert_qq_target(111, 42, enabled=True)
    poller.store.set_target_channel_enabled(111, True)
    feed = IrcChatFeed(config, poller)

    assert feed._targets() == []

    poller.store.add_config(
        111, weapon="torid", wildcard=None,
        positives=[["critical_damage"], ["multishot"]], negatives=[],
    )
    assert feed._targets() == [111]


async def test_bargain_sidecar_preserves_item_data_for_qq_text(monkeypatch):
    store = Store(":memory:")
    queue = asyncio.Queue()
    config = types.SimpleNamespace(bargain_max_distinct_slugs=10)
    poller = BargainPoller(store, config, FakeDeliveryPoller(queue))
    poller._baro_ready.set()
    monkeypatch.setattr(
        marketdata, "id_to_slug",
        lambda item_id: "arcane_grace" if item_id == "item-1" else None)
    monkeypatch.setattr(marketdata, "item_max_rank", lambda _slug: 0)
    store.upsert_qq_target(1, 42, enabled=True)
    store.add_bargain_item(1, "arcane_grace", 0.2, None)
    store.upsert_bargain_item_daily_baseline(
        "arcane_grace", "", "100.5", source_id="daily",
        source_datetime="2026-07-21T00:00:00Z", source_ts=100,
        volume=12)
    poller._item_stats_verified_at[("arcane_grace", "")] = time.time()

    order = {
        "id": "order-1", "type": "sell", "visible": True,
        "itemId": "item-1", "platinum": 50, "quantity": 2,
        "user": {"ingameName": "DealSeller", "status": "ingame"},
    }
    pushes = await poller.handle_new_item_order(order)

    assert pushes == 1
    queued = queue.get_nowait().payload
    hit = bargain.evaluate_price(50, "100.5", 0.2, samples=12)
    assert isinstance(queued, bargain.BargainItemPushPayload)
    assert queued.slug == "arcane_grace"
    assert queued.order == order
    assert queued.hit == hit
    assert bargain.build_item_push_text(
        queued.slug, queued.order, queued.hit, locale=queued.locale,
    ) == bargain.build_item_push_text("arcane_grace", order, hit)
    await poller.wfm.close()
    store.close()
