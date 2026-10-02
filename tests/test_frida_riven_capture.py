"""研究采集脚本的纯文本过滤/去重回归。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.plugins.riven_sniper.chat_collector.nonce_scan import NonceHit

SCRIPT = ROOT / "scripts" / "frida_riven_capture.py"
SPEC = importlib.util.spec_from_file_location("frida_riven_capture", SCRIPT)
capture = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(capture)

TICKET_SCRIPT = SCRIPT.with_name("capture_chat_ticket.py")
TICKET_SPEC = importlib.util.spec_from_file_location("capture_chat_ticket", TICKET_SCRIPT)
ticket_capture = importlib.util.module_from_spec(TICKET_SPEC)
assert TICKET_SPEC and TICKET_SPEC.loader
TICKET_SPEC.loader.exec_module(ticket_capture)


def test_default_channels_use_observed_english_region_names():
    assert capture.ENGLISH_CHAT_REGION_SUFFIXES == (
        "NA", "EU", "SA", "RU", "AS",
    )
    assert capture.ENGLISH_TRADE_CHANNELS == (
        "#T_EN_NA",
        "#T_EN_EU",
        "#T_EN_SA",
        "#T_EN_RU",
        "#T_EN_AS",
    )
    assert len(capture.DEFAULT_CHANNELS) == 17
    assert "#T_EN" not in capture.DEFAULT_CHANNELS
    assert "#T_EN_OC" not in capture.DEFAULT_CHANNELS


def test_successful_external_ticket_scan_terminates_game(tmp_path, monkeypatch):
    layout = ticket_capture.CollectorLayout(tmp_path / "runtime")
    layout.ensure()
    executable = tmp_path / "Warframe.x64.exe"
    executable.write_bytes(b"build fixture")
    hit = NonceHit(
        account_id="0123456789abcdef01234567",
        nonce="accountId=0123456789abcdef01234567&nonce=fixture",
        address=0x1234,
    )
    assert "fixture" not in hit.structure
    assert hit.account_id not in hit.structure
    actions: list[tuple[str, object]] = []

    monkeypatch.setattr(ticket_capture, "_prepare_capture", lambda *_args: ({}, {}))
    monkeypatch.setattr(ticket_capture, "find_game_pid", lambda: 4242)
    monkeypatch.setattr(ticket_capture, "pid_identity", lambda _pid: "game-id")
    monkeypatch.setattr(
        ticket_capture, "process_executable", lambda _pid: str(executable)
    )
    monkeypatch.setattr(ticket_capture, "exe_build_id", lambda _path: "build-id")
    monkeypatch.setattr(
        ticket_capture, "find_irc_endpoint", lambda _pid: ("127.0.0.1", 6695)
    )
    monkeypatch.setattr(
        ticket_capture,
        "scan_process",
        lambda *_args, **_kwargs: (
            [hit],
            {"regions": 1, "bytes": 4096, "hits": 1, "loose": 1},
        ),
    )

    def authenticate(_layout, slot, ticket_id, *, timeout):
        assert timeout == 1
        current = ticket_capture.read_json(layout.ticket_path(slot))
        assert current and current["ticket_id"] == ticket_id
        current["used"] = True
        current["outcome"] = "authenticated"
        current.pop("nonce", None)
        ticket_capture.atomic_write_json(layout.ticket_path(slot), current)
        return True, "authenticated"

    monkeypatch.setattr(ticket_capture, "_wait_for_candidate", authenticate)

    def terminate(pid, identity, *, wait_timeout):
        actions.append(("terminate", (pid, identity, wait_timeout)))
        return True

    monkeypatch.setattr(ticket_capture, "terminate_process", terminate)

    result = ticket_capture.capture_ticket(
        "A", "CollectorA", layout, consume_timeout=1
    )

    assert result == 0
    assert actions == [("terminate", (4242, "game-id", 15.0))]
    saved = ticket_capture.read_json(layout.ticket_path("A"))
    assert saved["used"] is True
    assert "nonce" not in saved


def test_external_ticket_scan_retries_next_candidate(tmp_path, monkeypatch):
    layout = ticket_capture.CollectorLayout(tmp_path / "runtime")
    layout.ensure()
    executable = tmp_path / "Warframe.x64.exe"
    executable.write_bytes(b"build fixture")
    hits = [
        NonceHit(
            account_id="0123456789abcdef01234567",
            nonce=f"accountId=0123456789abcdef01234567&nonce=candidate{index}",
            address=0x1000 + index,
        )
        for index in (1, 2)
    ]
    attempts: list[str] = []

    monkeypatch.setattr(ticket_capture, "_prepare_capture", lambda *_args: ({}, {}))
    monkeypatch.setattr(ticket_capture, "find_game_pid", lambda: 4242)
    monkeypatch.setattr(ticket_capture, "pid_identity", lambda _pid: "game-id")
    monkeypatch.setattr(
        ticket_capture, "process_executable", lambda _pid: str(executable)
    )
    monkeypatch.setattr(ticket_capture, "exe_build_id", lambda _path: "build-id")
    monkeypatch.setattr(
        ticket_capture, "find_irc_endpoint", lambda _pid: ("127.0.0.1", 6695)
    )
    monkeypatch.setattr(
        ticket_capture,
        "scan_process",
        lambda *_args, **_kwargs: (
            hits,
            {"regions": 2, "bytes": 8192, "hits": 2, "loose": 2},
        ),
    )
    monkeypatch.setattr(ticket_capture, "_wait_for_worker_exit", lambda *_args: True)

    def authenticate(_layout, slot, ticket_id, *, timeout):
        current = ticket_capture.read_json(layout.ticket_path(slot))
        assert current and current["ticket_id"] == ticket_id
        attempts.append(current["nonce"])
        if len(attempts) == 1:
            return False, "rejected:E409"
        current["used"] = True
        current["outcome"] = "authenticated"
        current.pop("nonce", None)
        ticket_capture.atomic_write_json(layout.ticket_path(slot), current)
        return True, "authenticated"

    monkeypatch.setattr(ticket_capture, "_wait_for_candidate", authenticate)
    monkeypatch.setattr(
        ticket_capture,
        "terminate_process",
        lambda *_args, **_kwargs: True,
    )

    result = ticket_capture.capture_ticket(
        "A", "CollectorA", layout, consume_timeout=1
    )

    assert result == 0
    assert attempts == [hit.nonce for hit in hits]
    saved = ticket_capture.read_json(layout.ticket_path("A"))
    assert saved["used"] is True
    assert saved["outcome"] == "authenticated"
    assert "nonce" not in saved


def test_candidate_wait_timeout_resets_when_worker_state_advances(
        tmp_path, monkeypatch):
    layout = ticket_capture.CollectorLayout(tmp_path / "runtime")
    layout.ensure()
    ticket_id = "ticket-1"
    ticket_capture.atomic_write_json(layout.ticket_path("A"), {
        "ticket_id": ticket_id,
        "used": False,
    })
    ticket_capture.atomic_write_json(layout.state_path("A"), {
        "ticket_id": ticket_id,
        "status": "reconnecting",
        "updated_at": "first",
    })
    monotonic = iter((0.0, 0.0, 0.6, 0.6, 1.2, 1.2, 1.8))
    sleeps = 0

    def sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            ticket = ticket_capture.read_json(layout.ticket_path("A"))
            ticket["used"] = True
            ticket["outcome"] = "auth_retrying"
            ticket_capture.atomic_write_json(layout.ticket_path("A"), ticket)
            state = ticket_capture.read_json(layout.state_path("A"))
            state["updated_at"] = "second"
            ticket_capture.atomic_write_json(layout.state_path("A"), state)
        elif sleeps == 2:
            ticket = ticket_capture.read_json(layout.ticket_path("A"))
            ticket["used"] = True
            ticket["outcome"] = "authenticated"
            ticket_capture.atomic_write_json(layout.ticket_path("A"), ticket)

    monkeypatch.setattr(ticket_capture.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(ticket_capture.time, "sleep", sleep)

    assert ticket_capture._wait_for_candidate(
        layout, "A", ticket_id, timeout=1.0,
    ) == (True, "authenticated")


def test_only_complete_riven_privmsg_is_accepted():
    raw = (
        ":seller\ue000!account@host PRIVMSG #T_ZH :出"
        "[OMG-LotusRifleRandomModRare:8T9NiKpb9TjuN70Tfedj9RLDSiibxgUk] 500"
    )
    assert capture.parse_riven_privmsg(raw) == {
        "dir": "in",
        "nick": "seller\ue000",
        "sender_id": "",
        "chan": "#T_ZH",
        "text": "出[OMG-LotusRifleRandomModRare:8T9NiKpb9TjuN70Tfedj9RLDSiibxgUk] 500",
    }
    assert capture.parse_riven_privmsg(
        ":seller!a@h PRIVMSG #T_ZH :80出金垃圾"
    ) is None
    assert capture.parse_riven_privmsg(
        ":seller!a@h PRIVMSG #T_ZH :[MOD-123]"
    ) is None
    assert capture.parse_riven_privmsg(
        ":seller!a@h PRIVMSG #T_ZH :[OMG-LotusRifleRandomModRare:truncated"
    ) is None


def test_plain_privmsg_can_be_normalized_for_channel_sampling():
    raw = ":speaker!a@h PRIVMSG #G_ZH :区域频道测试 123"
    assert capture.parse_privmsg(raw) == {
        "dir": "in",
        "nick": "speaker",
        "sender_id": "",
        "chan": "#G_ZH",
        "text": "区域频道测试 123",
    }
    assert capture.parse_privmsg("NOTICE #G_ZH :not a message") is None

    assert capture.parse_privmsg(
        ":speaker!0123456789ABCDEF01234567_0@host PRIVMSG #G_ZH :hello"
    )["sender_id"] == "0123456789abcdef01234567"

    assert capture.parse_outgoing_privmsg("PRIVMSG #G_ZH :区域频道测试 456") == {
        "dir": "out",
        "nick": "self",
        "chan": "#G_ZH",
        "text": "区域频道测试 456",
    }


def test_immediate_handler_duplicates_are_suppressed():
    recent = capture.RecentMessages(ttl=10)
    record = {"nick": "seller", "chan": "#T_ZH", "text": "[OMG-x]"}
    assert recent.add(record, 100.0)
    assert not recent.add(record, 100.001)
    assert recent.add(record, 110.0)


def test_outgoing_riven_sample_is_normalized_but_plain_chat_is_ignored():
    raw = (
        "PRIVMSG #G_MY_SQUAD :S01 "
        "[OMG-LotusPistolRandomModRare:8T5FIDIL9VI5Fz4lT8W70lSTGcubjgCI]"
    )
    assert capture.parse_outgoing_riven(raw) == {
        "dir": "out",
        "nick": "self",
        "chan": "#G_MY_SQUAD",
        "text": "S01 [OMG-LotusPistolRandomModRare:8T5FIDIL9VI5Fz4lT8W70lSTGcubjgCI]",
    }
    assert capture.parse_outgoing_riven("PRIVMSG #Q_ZH :test123") is None


def test_same_text_from_different_channel_or_seller_is_distinct():
    recent = capture.RecentMessages(ttl=10)
    assert recent.add({"nick": "a", "chan": "#T_ZH", "text": "same"}, 1)
    assert recent.add({"nick": "b", "chan": "#T_ZH", "text": "same"}, 1)
    assert recent.add({"nick": "a", "chan": "#T_EN_NA", "text": "same"}, 1)


def test_join_confirmation_and_rejection_responses_are_classified():
    assert capture.parse_irc_join_response(
        ":irc.example 353 collector = #T_EN_NA :alice bob"
    ) == {
        "kind": "confirmed",
        "channel": "#T_EN_NA",
        "code": 353,
        "source": "irc.example",
        "target": "collector",
        "detail": "alice bob",
    }
    assert capture.parse_irc_join_response(
        ":irc.example 405 collector #T_FR :You have joined too many channels"
    ) == {
        "kind": "rejected",
        "channel": "#T_FR",
        "code": 405,
        "source": "irc.example",
        "target": "collector",
        "detail": "You have joined too many channels",
    }
    assert capture.parse_irc_join_response(
        ":irc.example 366 collector #T_RU :End of /NAMES list"
    )["kind"] == "confirmed"


def test_server_join_broadcast_is_parsed_without_being_assumed_to_be_self():
    assert capture.parse_irc_join_response(
        ":collector!account@host JOIN :#Q_DE\r\n"
    ) == {
        "kind": "join",
        "channel": "#Q_DE",
        "code": None,
        "source": "collector",
        "target": None,
        "detail": "",
    }
    assert capture.parse_irc_join_response(
        ":irc.example 001 collector :Welcome"
    ) is None
