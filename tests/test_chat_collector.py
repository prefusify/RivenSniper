"""独立聊天采集器的配置、协议和运行时契约。"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.plugins.riven_sniper.chat_collector.config import (
    EXPECTED_CHANNELS,
    REGIONAL_SLOT_LOCALES,
    SEVENTEEN_SLOT_TOPOLOGY,
    PresenceSnapshotPolicy,
    load_accounts,
    load_presence_snapshot_policy,
    load_reconnect_policy,
    load_shards,
)
from src.plugins.riven_sniper.chat_message import contains_riven_link
from src.plugins.riven_sniper.chat_collector.protocol import (
    Heartbeat,
    JoinReport,
    JoinTracker,
    delayjoin_whox_command,
    is_who_unsupported,
    parse_presence,
    parse_privmsg,
    parse_who_end,
    parse_whox_member,
    parse_whox_query_type,
)
from src.plugins.riven_sniper.chat_collector.presence_snapshot import (
    PresenceSnapshotScheduler,
    SnapshotRequest,
    arm_snapshot_request,
    load_snapshot_request,
    update_snapshot_request,
)
from src.plugins.riven_sniper.chat_collector.protocol_probe import (
    ProtocolProbeAnalyzer,
    ProtocolProbeRequest,
    arm_protocol_probe,
    channel_probe_commands,
    protocol_probe_commands,
)
from src.plugins.riven_sniper.chat_collector.runtime import (
    append_jsonl,
    atomic_write_json,
    pid_identity,
    pid_matches,
    read_json,
    terminate_process,
)
from src.plugins.riven_sniper.chat_collector import runtime as runtime_module
from src.plugins.riven_sniper.chat_collector.session import AuthResult, IrcSession, Ticket
from src.plugins.riven_sniper.chat_collector import supervisor as supervisor_module
from src.plugins.riven_sniper.chat_collector.supervisor import (
    CollectorLayout,
    SupervisorLock,
    TicketStore,
    collector_topology,
    collector_status,
    initialize_layout,
    request_stop,
    run_supervisor,
    set_collector_mode,
)


ROOT = Path(__file__).resolve().parents[1]


def test_product_shards_exactly_cover_supported_matrix():
    shards = load_shards(ROOT / "configs" / "chat_collector_shards.json")
    assigned = [channel for slot in shards.values() for channel in slot]

    assert len(assigned) == 68
    assert len(set(assigned)) == 68
    assert set(assigned) == EXPECTED_CHANNELS
    assert all(len(channels) <= 20 for channels in shards.values())
    assert not {"#G_EN", "#Q_EN", "#R_EN", "#T_EN"} & set(assigned)


def test_product_presence_snapshot_policy_is_bounded():
    policy = load_presence_snapshot_policy(
        ROOT / "configs" / "chat_collector_shards.json"
    )

    assert policy.enabled is True
    assert policy.manual_enabled is True
    assert policy.initial_delay_seconds == 5
    assert policy.request_gap_seconds == 4
    assert policy.timeout_seconds == 30
    assert policy.max_record_bytes == 768 * 1024
    reconnect = load_reconnect_policy(
        ROOT / "configs" / "chat_collector_shards.json"
    )
    assert reconnect.enabled is True
    assert reconnect.backoff_seconds == (15, 30, 60, 120, 300)
    assert reconnect.max_attempts == 12
    assert reconnect.reuse_ttl_hours == 6


def test_regional_17_shards_cover_each_locale_without_snapshots():
    path = ROOT / "configs" / "chat_collector_shards_17.json"
    shards = load_shards(path, topology=SEVENTEEN_SLOT_TOPOLOGY)
    assigned = [channel for channels in shards.values() for channel in channels]
    policy = load_presence_snapshot_policy(path)

    assert len(shards) == 17
    assert len(assigned) == 68
    assert set(assigned) == EXPECTED_CHANNELS
    assert policy.enabled is False
    assert policy.manual_enabled is True
    for slot, locale in REGIONAL_SLOT_LOCALES.items():
        assert set(shards[slot]) == {
            f"#{kind}_{locale}" for kind in "GQRT"
        }

    scheduler = PresenceSnapshotScheduler(
        shards["A"], slot="A",
        collector_account_id="111111111111111111111111",
        policy=policy,
        started_at=0,
    )
    assert scheduler.next_command(86_400, shards["A"]) is None
    assert scheduler.health()["snapshot_coverage"] == "passive"


def test_collector_mode_defaults_to_four_and_can_select_seventeen(tmp_path):
    layout = CollectorLayout(tmp_path)
    result = initialize_layout(layout)

    assert result["collector_mode"] == "4"
    assert collector_topology(layout).mode == "4"
    selected = set_collector_mode(layout, "17")
    accounts = load_accounts(
        layout.accounts_path_for(selected), topology=selected,
    )
    status = collector_status(layout)

    assert selected.mode == "17"
    assert tuple(accounts) == selected.slots
    assert status["collector_mode"] == "17"
    assert status["expected_slots"] == list(selected.slots)
    assert status["slots"]["Q"]["region"] == "EN_AS"


def test_collector_mode_cli_switches_to_seventeen(tmp_path):
    entry = ROOT / "scripts" / "run_chat_collector.py"
    initialize = subprocess.run(
        [sys.executable, str(entry), "init", "--runtime-root", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    switched = subprocess.run(
        [
            sys.executable,
            str(entry),
            "mode",
            "17",
            "--runtime-root",
            str(tmp_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert initialize.returncode == 0, initialize.stderr
    assert switched.returncode == 0, switched.stderr
    assert json.loads(switched.stdout)["mode"] == "17"
    assert collector_topology(CollectorLayout(tmp_path)).mode == "17"


def test_manual_snapshot_cli_arms_request_for_live_assigned_slot(tmp_path):
    entry = ROOT / "scripts" / "run_chat_collector.py"
    layout = CollectorLayout(tmp_path)
    initialize_layout(layout)
    atomic_write_json(layout.state_path("B"), {
        "slot": "B",
        "status": "listening",
        "pid": os.getpid(),
        "process_identity": pid_identity(os.getpid()),
    })

    armed = subprocess.run(
        [
            sys.executable,
            str(entry),
            "snapshot",
            "--slot",
            "B",
            "--channel",
            "#T_ZH",
            "--runtime-root",
            str(tmp_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    status = subprocess.run(
        [
            sys.executable,
            str(entry),
            "snapshot-status",
            "--runtime-root",
            str(tmp_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert armed.returncode == 0, armed.stderr
    assert status.returncode == 0, status.stderr
    assert json.loads(armed.stdout)["channels"] == ["#T_ZH"]
    assert json.loads(status.stdout)["status"] == "armed"


def test_manual_snapshot_cli_replaces_stale_armed_request(tmp_path):
    entry = ROOT / "scripts" / "run_chat_collector.py"
    layout = CollectorLayout(tmp_path)
    initialize_layout(layout)
    atomic_write_json(layout.state_path("A"), {
        "slot": "A",
        "status": "needs_ticket",
        "pid": 0,
        "process_identity": "",
    })
    atomic_write_json(layout.state_path("B"), {
        "slot": "B",
        "status": "listening",
        "pid": os.getpid(),
        "process_identity": pid_identity(os.getpid()),
    })
    stale = SnapshotRequest.create(slot="A", channels=("#T_FR",))
    arm_snapshot_request(layout.snapshot_request_path, stale)

    result = subprocess.run(
        [
            sys.executable,
            str(entry),
            "snapshot",
            "--slot",
            "B",
            "--channel",
            "#T_ZH",
            "--runtime-root",
            str(tmp_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 0, result.stderr
    assert "stale_armed_worker_not_running" in result.stderr
    current = read_json(layout.snapshot_request_path)
    assert current["request_id"] != stale.request_id
    assert current["status"] == "armed"
    assert current["slot"] == "B"


def test_manual_snapshot_cli_replaces_stale_running_request(tmp_path):
    entry = ROOT / "scripts" / "run_chat_collector.py"
    layout = CollectorLayout(tmp_path)
    initialize_layout(layout)
    atomic_write_json(layout.state_path("B"), {
        "slot": "B",
        "status": "listening",
        "pid": os.getpid(),
        "process_identity": pid_identity(os.getpid()),
    })
    stale = SnapshotRequest.create(slot="A", channels=("#T_FR",))
    arm_snapshot_request(layout.snapshot_request_path, stale)
    update_snapshot_request(
        layout.snapshot_request_path,
        stale,
        status="running",
        worker_pid=2_147_483_647,
        worker_identity="missing",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(entry),
            "snapshot",
            "--slot",
            "B",
            "--channel",
            "#T_ZH",
            "--runtime-root",
            str(tmp_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 0, result.stderr
    assert "stale_running_worker_exited" in result.stderr
    current = read_json(layout.snapshot_request_path)
    assert current["request_id"] != stale.request_id
    assert current["status"] == "armed"


def test_manual_snapshot_cli_cancels_stale_request_before_target_validation(
        tmp_path):
    entry = ROOT / "scripts" / "run_chat_collector.py"
    layout = CollectorLayout(tmp_path)
    initialize_layout(layout)
    stale = SnapshotRequest.create(slot="A", channels=("#T_FR",))
    arm_snapshot_request(layout.snapshot_request_path, stale)

    result = subprocess.run(
        [
            sys.executable,
            str(entry),
            "snapshot",
            "--slot",
            "B",
            "--channel",
            "#T_ZH",
            "--runtime-root",
            str(tmp_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 2
    assert "stale_armed_worker_not_running" in result.stderr
    assert "worker 当前未运行" in result.stderr
    current = read_json(layout.snapshot_request_path)
    assert current["request_id"] == stale.request_id
    assert current["status"] == "cancelled"


def test_collector_mode_rejects_switch_while_supervisor_is_alive(tmp_path):
    layout = CollectorLayout(tmp_path)
    layout.ensure()
    atomic_write_json(layout.supervisor_lock, {
        "pid": os.getpid(),
        "process_identity": pid_identity(os.getpid()),
    })

    with pytest.raises(RuntimeError, match="必须先正常停止"):
        set_collector_mode(layout, "17")


def test_collector_mode_rejects_switch_while_orphan_worker_is_alive(tmp_path):
    layout = CollectorLayout(tmp_path)
    initialize_layout(layout)
    atomic_write_json(layout.state_path("A"), {
        "slot": "A",
        "status": "listening",
        "pid": os.getpid(),
        "process_identity": pid_identity(os.getpid()),
    })

    with pytest.raises(RuntimeError, match="槽 A 的 worker 仍在运行"):
        set_collector_mode(layout, "17")

    assert collector_topology(layout).mode == "4"
    assert not layout.accounts_path_for(SEVENTEEN_SLOT_TOPOLOGY).exists()


def test_ticket_and_probe_accept_regional_slot_q():
    ticket = Ticket.from_dict({
        "slot": "Q",
        "host": "127.0.0.1",
        "port": 6697,
        "nick": "CollectorQ",
        "account_id": "0123456789abcdef01234567",
        "nonce": "nonce-q",
    }, expected_slot="Q")
    request = ProtocolProbeRequest.create(
        slot="Q",
        target_nick="Target",
        target_platform="windows",
    )

    assert ticket.slot == "Q"
    assert request.slot == "Q"


def test_seventeen_slot_supervisor_waits_for_all_regional_tickets(tmp_path):
    layout = CollectorLayout(tmp_path)
    layout.ensure()
    topology = set_collector_mode(layout, "17")
    atomic_write_json(
        layout.accounts_path_for(topology),
        {slot: {"nick": f"Collector{slot}"} for slot in topology.slots},
    )
    layout.psk_path.write_bytes(bytes(range(64)))
    outcomes: list[object] = []

    def run():
        try:
            outcomes.append(run_supervisor(
                layout,
                script_path=ROOT / "scripts" / "run_chat_collector.py",
            ))
        except Exception as error:  # pragma: no cover - assertion reports payload
            outcomes.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 3
    try:
        while time.monotonic() < deadline:
            control = read_json(layout.control_path) or {}
            if control.get("status") == "running":
                break
            time.sleep(0.02)
        status = collector_status(layout)
        assert status["supervisor_running"] is True
        assert status["expected_slots"] == list(topology.slots)
        assert control["collector_mode"] == "17"
        assert control["expected_slots"] == list(topology.slots)
        assert status["slots"]["Q"]["region"] == "EN_AS"
    finally:
        request_stop(layout)
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert outcomes == [0]


def test_privmsg_parser_preserves_stable_sender_id():
    message = parse_privmsg(
        ":Seller!0123456789ABCDEF01234567_0@host PRIVMSG #T_ZH :出紫卡 120pl"
    )

    assert message is not None
    assert message.nick == "Seller"
    assert message.sender_id == "0123456789abcdef01234567"
    assert message.chan == "#T_ZH"
    assert message.text == "出紫卡 120pl"


def test_protocol_parsers_preserve_cross_platform_nicknames_with_spaces():
    account_id = "0123456789abcdef01234567"
    message = parse_privmsg(
        f":Xbox Seller\ue001!{account_id}_1@host "
        "PRIVMSG #T_ZH :出售紫卡"
    )
    joined = parse_presence(
        f":Xbox Seller\ue001!{account_id}_1@host JOIN :#T_ZH"
    )
    renamed = parse_presence(
        f":Xbox Seller\ue001!{account_id}_1@host "
        "NICK :Xbox Seller New\ue001"
    )

    assert message is not None
    assert message.nick == "Xbox Seller\ue001"
    assert message.sender_id == account_id
    assert joined is not None and joined.nick == "Xbox Seller\ue001"
    assert joined.chan == "#T_ZH"
    assert renamed is not None and renamed.new_nick == "Xbox Seller New\ue001"


def test_delayed_whox_protocol_parses_stable_member_identity():
    account_id = "0123456789abcdef01234567"

    assert delayjoin_whox_command("#t_zh", "101") == (
        "WHO #T_ZH d%tnu,101"
    )
    assert parse_whox_member(
        f":irc.example 354 Collector 101 {account_id}_0 :Player Name\ue001",
        query_type="101",
    ) == (account_id, "Player Name\ue001")
    assert parse_whox_member(
        f":irc.example 354 Collector 102 {account_id}_0 :Player",
        query_type="101",
    ) is None
    assert parse_whox_query_type(
        f":irc.example 354 Collector 101 {account_id}_0 :Player"
    ) == "101"
    with pytest.raises(ValueError, match="1 到 3 位"):
        delayjoin_whox_command("#T_ZH", "1001")
    assert parse_who_end(
        ":irc.example 315 Collector #t_zh :End of /WHO list."
    ) == "#T_ZH"
    assert is_who_unsupported(
        ":irc.example 421 Collector WHO :Unknown command"
    ) is True


def test_presence_snapshot_scheduler_requires_complete_whox_and_applies_overrides():
    first_id = "0123456789abcdef01234567"
    second_id = "89abcdef0123456701234567"
    joined_id = "abcdef012345670123456789"
    unrelated_id = "fedcba987654321001234567"
    collector_id = "111111111111111111111111"
    policy = PresenceSnapshotPolicy(
        initial_delay_seconds=0,
        request_gap_seconds=0.1,
        timeout_seconds=5,
        max_record_bytes=64 * 1024,
    )
    scheduler = PresenceSnapshotScheduler(
        ("#T_ZH",), slot="A", collector_account_id=collector_id,
        policy=policy, started_at=0,
    )

    assert scheduler.next_command(0, ("#T_ZH",)) == (
        "WHO #T_ZH d%tnu,101"
    )
    for account_id, nick in (
        (first_id, "First\ue000"),
        (second_id, "Second\ue001"),
        (collector_id, "Collector\ue000"),
    ):
        assert scheduler.observe_line(
            f":irc 354 Collector 101 {account_id}_0 :{nick}",
            now=0.1,
            joined_channels=("#T_ZH",),
        ) is None
    parted = parse_presence(
        f":First\ue000!{first_id}_0@host PART #T_ZH :gone"
    )
    joined = parse_presence(
        f":Late\ue002!{joined_id}_0@host JOIN :#T_ZH"
    )
    assert parted is not None and joined is not None
    scheduler.observe_presence(parted)
    scheduler.observe_presence(joined)
    renamed = parse_presence(
        f":Second\ue001!{second_id}_0@host NICK :Second Renamed\ue001"
    )
    unrelated_rename = parse_presence(
        f":Other\ue000!{unrelated_id}_0@host NICK :Other Renamed\ue000"
    )
    assert renamed is not None and unrelated_rename is not None
    scheduler.observe_presence(renamed)
    scheduler.observe_presence(unrelated_rename)

    event = scheduler.observe_line(
        ":irc 315 Collector #T_ZH :End of /WHO list.",
        now=0.2,
        joined_channels=("#T_ZH",),
    )

    assert event is not None
    assert event["type"] == "channel_snapshot"
    assert event["chan"] == "#T_ZH"
    assert event["members"] == [
        [second_id, "Second Renamed\ue001"],
        [joined_id, "Late\ue002"],
    ]
    assert scheduler.health()["snapshot_coverage"] == "complete"
    assert scheduler.next_command(3_600, ("#T_ZH",)) is None


def test_presence_snapshot_scheduler_discards_timed_out_partial_reply():
    scheduler = PresenceSnapshotScheduler(
        ("#T_ZH",), slot="A",
        collector_account_id="111111111111111111111111",
        policy=PresenceSnapshotPolicy(
            initial_delay_seconds=0,
            request_gap_seconds=0.1,
            timeout_seconds=5,
            max_record_bytes=64 * 1024,
        ),
        started_at=0,
    )
    assert scheduler.next_command(0, ("#T_ZH",)) is not None
    assert scheduler.next_command(5, ("#T_ZH",)) is None
    health = scheduler.health()
    assert health["snapshot_failed_count"] == 1
    assert health["snapshot_last_error"] == "timeout"
    assert scheduler.next_command(3_600, ("#T_ZH",)) is None


def test_presence_snapshot_scheduler_rejects_mismatched_whox_tag():
    collector_id = "111111111111111111111111"
    scheduler = PresenceSnapshotScheduler(
        ("#T_ZH",), slot="A", collector_account_id=collector_id,
        policy=PresenceSnapshotPolicy(
            initial_delay_seconds=0,
            request_gap_seconds=0.1,
            timeout_seconds=5,
            max_record_bytes=64 * 1024,
        ),
        started_at=0,
    )
    assert scheduler.next_command(0, ("#T_ZH",)) == (
        "WHO #T_ZH d%tnu,101"
    )
    scheduler.observe_line(
        f":irc 354 Collector 001 {collector_id}_0 :Collector\ue000",
        now=0.1,
        joined_channels=("#T_ZH",),
    )
    assert scheduler.observe_line(
        ":irc 315 Collector #T_ZH :End of /WHO list.",
        now=0.2,
        joined_channels=("#T_ZH",),
    ) is None
    health = scheduler.health()
    assert health["snapshot_coverage"] == "degraded"
    assert health["snapshot_last_error"] == "query_type_mismatch"
    assert health["snapshot_failed_channels"] == ["#T_ZH"]


def test_presence_snapshot_scheduler_requires_matching_reply_before_empty_snapshot():
    collector_id = "111111111111111111111111"
    scheduler = PresenceSnapshotScheduler(
        ("#T_ZH",), slot="A", collector_account_id=collector_id,
        policy=PresenceSnapshotPolicy(
            initial_delay_seconds=0,
            request_gap_seconds=0.1,
            timeout_seconds=5,
            max_record_bytes=64 * 1024,
        ),
        started_at=0,
    )
    assert scheduler.next_command(0, ("#T_ZH",)) is not None
    assert scheduler.observe_line(
        ":irc 315 Collector #T_ZH :End of /WHO list.",
        now=0.2,
        joined_channels=("#T_ZH",),
    ) is None
    assert scheduler.health()["snapshot_last_error"] == (
        "no_matching_reply"
    )
    assert scheduler.next_command(61, ("#T_ZH",)) is None
    health = scheduler.health()
    assert health["snapshot_coverage"] == "degraded"
    assert health["snapshot_failed_channels"] == ["#T_ZH"]


def test_manual_snapshot_request_state_machine_rejects_active_replacement(tmp_path):
    path = tmp_path / "snapshot_request.json"
    request = SnapshotRequest.create(slot="A", channels=("#T_ZH",))

    arm_snapshot_request(path, request)
    loaded = load_snapshot_request(path)
    assert loaded == request
    with pytest.raises(RuntimeError, match="尚未完成"):
        arm_snapshot_request(
            path,
            SnapshotRequest.create(slot="B", channels=("#T_FR",)),
        )

    update_snapshot_request(path, request, status="running", started_at="now")
    update_snapshot_request(
        path,
        request,
        status="complete",
        completed_at="later",
        result={"snapshot_coverage": "complete"},
    )
    assert read_json(path)["status"] == "complete"


def test_manual_snapshot_scheduler_finishes_when_requested_channel_is_missing():
    scheduler = PresenceSnapshotScheduler(
        ("#T_ZH",),
        slot="Q",
        collector_account_id="111111111111111111111111",
        policy=PresenceSnapshotPolicy(
            enabled=True,
            initial_delay_seconds=0,
            request_gap_seconds=0.1,
            timeout_seconds=5,
            max_record_bytes=64 * 1024,
        ),
        started_at=0,
        stagger_by_slot=False,
        expire_unjoined=True,
    )

    assert scheduler.next_command(0, ()) is None
    assert scheduler.next_command(5, ()) is None
    assert scheduler.finished is True
    assert scheduler.health()["snapshot_failed_channels"] == ["#T_ZH"]


def test_manual_snapshot_counts_pending_channel_loss_once():
    scheduler = PresenceSnapshotScheduler(
        ("#T_ZH",),
        slot="A",
        collector_account_id="111111111111111111111111",
        policy=PresenceSnapshotPolicy(
            enabled=True,
            initial_delay_seconds=0,
            request_gap_seconds=0.1,
            timeout_seconds=5,
            max_record_bytes=64 * 1024,
        ),
        started_at=0,
        stagger_by_slot=False,
        expire_unjoined=True,
    )

    assert scheduler.next_command(0, ("#T_ZH",)) is not None
    assert scheduler.next_command(5, ()) is None
    assert scheduler.finished is True
    assert scheduler.health()["snapshot_failed_count"] == 1


def test_listener_executes_armed_manual_snapshot(tmp_path, monkeypatch):
    account_id = "0123456789abcdef01234567"
    ticket = Ticket.from_dict({
        "slot": "A",
        "host": "127.0.0.1",
        "port": 6695,
        "nick": "Collector",
        "account_id": account_id,
        "nonce": f"accountId={account_id}&nonce=test",
    })
    session = IrcSession(ticket, bytes(range(64)), plain=True)
    tracker = JoinTracker(("#T_ZH",), own_nick="Collector")
    tracker.confirmed.add("#T_ZH")
    request_path = tmp_path / "snapshot_request.json"
    request = SnapshotRequest.create(slot="A", channels=("#T_ZH",))
    arm_snapshot_request(request_path, request)
    sent: list[str] = []
    events: list[dict] = []
    incoming = iter((
        ":irc 354 Collector 101 89abcdef0123456701234567_0 :Member\ue000",
        ":irc 315 Collector #T_ZH :End of /WHO list.",
    ))
    monkeypatch.setattr(session, "send_raw", sent.append)
    monkeypatch.setattr(session, "_readline", lambda _timeout: next(incoming))
    session.on_presence = events.append

    def should_stop():
        state = read_json(request_path) or {}
        return state.get("status") in {"complete", "failed"}

    reason = session.listen(
        tracker,
        on_privmsg=lambda _entry: None,
        should_stop=should_stop,
        presence_snapshot_policy=PresenceSnapshotPolicy(
            enabled=False,
            manual_enabled=True,
            initial_delay_seconds=5,
            request_gap_seconds=0.1,
            timeout_seconds=5,
            max_record_bytes=64 * 1024,
        ),
        snapshot_request_path=request_path,
    )

    assert reason == "stop"
    assert sent == ["WHO #T_ZH d%tnu,101", "QUIT :collector stop"]
    assert events[0]["type"] == "channel_snapshot"
    assert events[0]["members"] == [
        ["89abcdef0123456701234567", "Member\ue000"],
    ]
    state = read_json(request_path)
    assert state["status"] == "complete"
    assert state["result"]["snapshot_coverage"] == "complete"
    assert state["worker_pid"] == os.getpid()
    assert state["worker_identity"] == pid_identity(os.getpid())


def test_listener_ignores_manual_snapshot_for_other_slot(tmp_path, monkeypatch):
    account_id = "0123456789abcdef01234567"
    session = IrcSession(Ticket.from_dict({
        "slot": "A",
        "host": "127.0.0.1",
        "port": 6695,
        "nick": "Collector",
        "account_id": account_id,
        "nonce": f"accountId={account_id}&nonce=test",
    }), bytes(range(64)), plain=True)
    tracker = JoinTracker(("#T_ZH",), own_nick="Collector")
    tracker.confirmed.add("#T_ZH")
    request_path = tmp_path / "snapshot_request.json"
    request = SnapshotRequest.create(slot="B", channels=("#T_ZH",))
    arm_snapshot_request(request_path, request)
    sent: list[str] = []
    monkeypatch.setattr(session, "send_raw", sent.append)
    monkeypatch.setattr(session, "_readline", lambda _timeout: None)

    reason = session.listen(
        tracker,
        on_privmsg=lambda _entry: None,
        should_stop=lambda: False,
        presence_snapshot_policy=PresenceSnapshotPolicy(enabled=False),
        snapshot_request_path=request_path,
    )

    assert reason == "eof"
    assert sent == []
    assert read_json(request_path)["status"] == "armed"


def test_riven_link_filter_requires_complete_structured_link():
    link = (
        "[OMG-LotusRifleRandomModRare:"
        "8T9NiKpb9TjuN70Tfedj9RLDSiibxgUk]"
    )
    assert contains_riven_link(f"500 出 {link}")
    assert not contains_riven_link("WTB Torid riven 500p")
    assert not contains_riven_link("[Rifle Riven Mod] 10p")
    assert not contains_riven_link(
        "[OMG-LotusRifleRandomModRare:8T9NiKpb9TjuN70Tfedj9RLDSiibxgUk"
    )


def test_partial_join_is_degraded_not_healthy():
    tracker = JoinTracker(["#T_ZH", "#T_FR", "#T_DE"], own_nick="Collector")
    tracker.observe(":irc.example 353 Collector = #T_ZH :a b")
    tracker.observe(":irc.example 366 Collector #T_FR :End of /NAMES list")
    tracker.observe(":irc.example 405 Collector #T_DE :Too many channels")

    report = tracker.report(settled=True)
    assert report.status == "degraded"
    assert report.joined == ("#T_FR", "#T_ZH")
    assert report.missing == ("#T_DE",)
    assert report.rejected["#T_DE"].startswith("405 ")


def test_join_broadcast_only_confirms_our_own_nick():
    tracker = JoinTracker(["#T_ZH"], own_nick="Collector")
    tracker.observe(":SomebodyElse!id@host JOIN :#T_ZH")
    assert tracker.report(settled=False).status == "joining"

    tracker.observe(":Collector!id@host JOIN :#T_ZH")
    assert tracker.report(settled=False).status == "healthy"

    tracker.observe(":server KICK #T_ZH Collector :rejoin required")
    report = tracker.report(settled=True)
    assert report.status == "failed"
    assert report.rejected["#T_ZH"] == "KICK rejoin required"


def test_heartbeat_requires_matching_pong_before_deadline():
    heartbeat = Heartbeat(interval=30.0, timeout=10.0)
    assert heartbeat.next_ping(100.0, "A") is None
    assert heartbeat.next_ping(130.0, "A") == "collector-A-130000"
    assert not heartbeat.observe(":irc.example PONG server :some-other-token")
    assert not heartbeat.expired(139.999)
    assert heartbeat.expired(140.0)

    heartbeat = Heartbeat(interval=30.0, timeout=10.0)
    assert heartbeat.next_ping(30.0, "B") is None
    token = heartbeat.next_ping(60.0, "B")
    assert token
    assert heartbeat.observe(f":irc.example PONG server :{token}")
    assert not heartbeat.expired(100.0)


def test_protocol_probe_uses_platform_wire_nick_and_summarizes_replies():
    account_id = "0123456789abcdef01234567"
    request = ProtocolProbeRequest.create(
        slot="B",
        target_nick="ExamplePlayer",
        target_platform="windows",
        target_account_id=account_id,
        duration_seconds=600,
        query_interval_seconds=10,
    )
    assert request.target_irc_nick == "ExamplePlayer\ue000"
    assert request.target_account_id == account_id
    assert protocol_probe_commands(
        request.target_irc_nick, request.target_account_id
    ) == (
        "CAP LS 302",
        "MONITOR + ExamplePlayer\ue000",
        "MONITOR S",
        "ISON ExamplePlayer\ue000",
        "WHOIS ExamplePlayer\ue000",
        "WATCH + ExamplePlayer\ue000",
        "USERHOST ExamplePlayer\ue000",
        f"WHO {account_id}_0 u%tnu,42",
        "WHOWAS ExamplePlayer\ue000 1",
        "MODULES",
        "STATS m",
    )

    analyzer = ProtocolProbeAnalyzer(request.target_irc_nick, account_id)
    for line in (
        ":server 005 Collector MONITOR=100 UTF8ONLY :are supported by this server",
        f":server 731 Collector :{request.target_irc_nick}",
        f":server 303 Collector :{request.target_irc_nick}",
        f":server 311 Collector {request.target_irc_nick} "
        f"{account_id}_0 host * :ExamplePlayer",
    ):
        analyzer.observe(line)

    summary = analyzer.summary()
    assert summary["monitor"] == "supported"
    assert summary["monitor_offline_seen"] is True
    assert summary["ison"] == "supported"
    assert summary["ison_online_seen"] is True
    assert summary["whois"] == "supported"
    assert summary["whois_online_seen"] is True
    assert summary["account_ids"] == [account_id]
    assert summary["numeric_codes"] == [5, 731, 303, 311]


def test_protocol_probe_summarizes_combined_research_commands():
    account_id = "0123456789abcdef01234567"
    analyzer = ProtocolProbeAnalyzer("ExamplePlayer\ue000", account_id)
    for line in (
        ":server 005 Collector WHOX :are supported by this server",
        f":server 604 Collector ExamplePlayer\ue000 {account_id}_0 host :is online",
        f":server 302 Collector :ExamplePlayer\ue000=+{account_id}_0@host",
        f":server 354 Collector 42 {account_id}_0 :ExamplePlayer\ue000",
        ":server 315 Collector :End of /WHO list",
        f":server 314 Collector ExamplePlayer\ue000 {account_id}_0 host * :realname",
        ":server 369 Collector ExamplePlayer\ue000 :End of WHOWAS",
        ":server 702 Collector monitor.so :monitor module",
        ":server 703 Collector :End of MODULES",
        ":server 212 Collector ISON 21 420",
        ":server 219 Collector m :End of STATS",
    ):
        analyzer.observe(line)

    summary = analyzer.summary()
    assert summary["watch"] == "supported"
    assert summary["watch_online_seen"] is True
    assert summary["userhost"] == "supported"
    assert summary["userhost_online_seen"] is True
    assert summary["whox"] == "supported"
    assert summary["whox_online_seen"] is True
    assert summary["whox_nicks"] == ["ExamplePlayer\ue000"]
    assert summary["whowas"] == "supported"
    assert summary["whowas_seen"] is True
    assert summary["modules"] == "supported"
    assert summary["stats"] == "supported"
    assert summary["account_ids"] == [account_id]


def test_protocol_probe_compares_plain_and_delayjoin_channel_who():
    account_id = "0123456789abcdef01234567"
    channels = ("#G_ZH", "#T_ZH")
    request = ProtocolProbeRequest.create(
        slot="B",
        target_nick="ExamplePlayer",
        target_platform="windows",
        target_account_id=account_id,
        probe_channels=channels,
    )
    assert request.probe_channels == channels
    assert ProtocolProbeRequest.from_dict(request.to_dict()).probe_channels == channels
    assert channel_probe_commands(channels) == (
        ("#G_ZH", "plain", "101", "WHO #G_ZH %tnu,101"),
        ("#G_ZH", "delayjoin", "201", "WHO #G_ZH d%tnu,201"),
        ("#T_ZH", "plain", "102", "WHO #T_ZH %tnu,102"),
        ("#T_ZH", "delayjoin", "202", "WHO #T_ZH d%tnu,202"),
    )
    assert protocol_probe_commands(
        request.target_irc_nick,
        account_id,
        include_delayjoined=True,
    )[7] == f"WHO {account_id}_0 du%tnu,42"

    analyzer = ProtocolProbeAnalyzer("ExamplePlayer\ue000", account_id, channels)
    analyzer.observe(":server 366 Collector #T_ZH :End of /NAMES list")
    analyzer.begin_identity_query()
    analyzer.observe(
        f":server 354 Collector 42 {account_id}_0 :ExamplePlayer\ue000"
    )
    analyzer.observe(
        f":server 315 Collector {account_id}_0 :End of /WHO list"
    )
    analyzer.begin_channel_query("#T_ZH", "plain", "102", 1)
    analyzer.observe(
        ":server 354 Collector 102 aaaaaaaaaaaaaaaaaaaaaaaa_0 :SomebodyElse"
    )
    analyzer.observe(":server 315 Collector #T_ZH :End of /WHO list")
    analyzer.begin_channel_query("#T_ZH", "delayjoin", "202", 1)
    analyzer.observe(
        f":server 354 Collector 202 {account_id}_0 :ExamplePlayer\ue000"
    )
    analyzer.observe(":server 315 Collector #T_ZH :End of /WHO list")

    summary = analyzer.summary()
    assert summary["identity_current_online"] is True
    assert summary["probe_joined_channels"] == ["#T_ZH"]
    assert summary["channel_delayjoin_confirmed"] is True
    assert summary["channel_delayjoin_confirmed_channels"] == ["#T_ZH"]
    assert summary["channel_probe_observations"][0]["target_seen"] is False
    assert summary["channel_probe_observations"][1]["target_seen"] is True


def test_channel_probe_requires_stable_account_id():
    with pytest.raises(ValueError, match="稳定账号 ID"):
        ProtocolProbeRequest.create(
            slot="B",
            target_nick="ExamplePlayer",
            target_platform="windows",
            probe_channels=("#T_ZH",),
        )


def test_protocol_probe_recognizes_unknown_command_replies():
    analyzer = ProtocolProbeAnalyzer("ExamplePlayer\ue000")
    analyzer.observe(":server 421 Collector CAP :Unknown command")
    analyzer.observe(":server 421 Collector MONITOR :Unknown command")
    analyzer.observe(":server 421 Collector ISON :Unknown command")
    analyzer.observe(":server 421 Collector WHOIS :Unknown command")
    analyzer.observe(":server 421 Collector WATCH :Unknown command")
    analyzer.observe(":server 421 Collector USERHOST :Unknown command")
    analyzer.observe(":server 421 Collector WHO :Unknown command")
    analyzer.observe(":server 421 Collector WHOWAS :Unknown command")
    analyzer.observe(":server 421 Collector MODULES :Unknown command")
    analyzer.observe(":server 421 Collector STATS :Unknown command")

    assert analyzer.summary()["cap"] == "unsupported"
    assert analyzer.summary()["monitor"] == "unsupported"
    assert analyzer.summary()["ison"] == "unsupported"
    assert analyzer.summary()["whois"] == "unsupported"
    assert analyzer.summary()["watch"] == "unsupported"
    assert analyzer.summary()["userhost"] == "unsupported"
    assert analyzer.summary()["whox"] == "unsupported"
    assert analyzer.summary()["whowas"] == "unsupported"
    assert analyzer.summary()["modules"] == "unsupported"
    assert analyzer.summary()["stats"] == "unsupported"


def test_pid_probe_does_not_signal_live_process():
    identity = pid_identity(os.getpid())
    assert identity is not None
    assert pid_matches(os.getpid(), identity)
    assert not pid_matches(2_147_483_647, identity)
    assert not terminate_process(2_147_483_647, identity)


@pytest.mark.skipif(os.name != "nt", reason="Windows process identity contract")
def test_terminate_process_requires_exact_creation_identity():
    process = subprocess.Popen([
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
    ])
    try:
        identity = pid_identity(process.pid)
        assert identity
        assert not terminate_process(process.pid, identity + "-wrong")
        assert process.poll() is None
        assert terminate_process(process.pid, identity)
        process.wait(timeout=3)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)


@pytest.mark.skipif(os.name != "nt", reason="Windows process identity contract")
def test_terminate_process_waits_for_process_handle(monkeypatch):
    class FakeFunction:
        def __init__(self, result):
            self.result = result
            self.calls = []

        def __call__(self, *args):
            self.calls.append(args)
            return self.result

    class FakeKernel32:
        OpenProcess = FakeFunction(100)
        TerminateProcess = FakeFunction(True)
        WaitForSingleObject = FakeFunction(0x00000102)
        CloseHandle = FakeFunction(True)

    kernel32 = FakeKernel32()
    matches = iter((True, False))
    monkeypatch.setattr(
        runtime_module,
        "pid_matches",
        lambda _pid, _identity: next(matches),
    )
    monkeypatch.setattr(
        runtime_module,
        "_windows_handle_identity",
        lambda _kernel32, _handle: "creation",
    )
    monkeypatch.setattr(
        runtime_module.ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: kernel32,
    )

    assert not runtime_module.terminate_process(
        123,
        "creation",
        wait_timeout=0.25,
    )
    assert kernel32.WaitForSingleObject.calls == [(100, 250)]


def test_atomic_json_failure_never_overwrites_destination(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text('{"before":true}', encoding="utf-8")

    def fail_replace(_source, _destination):
        raise PermissionError("locked")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(PermissionError):
        atomic_write_json(path, {"after": True}, retries=2, retry_delay=0)

    assert path.read_text(encoding="utf-8") == '{"before":true}'
    assert list(tmp_path.glob("state.json.tmp.*")) == []


def test_jsonl_writer_discards_only_crashed_tail_before_append(tmp_path):
    path = tmp_path / "slot.jsonl"
    path.write_bytes(b'{"ok":1}\n{"torn":')

    append_jsonl(path, {"ok": 2})

    assert path.read_bytes() == b'{"ok":1}\n{"ok":2}\n'


def test_ticket_is_scrubbed_only_when_marked_authenticated(tmp_path):
    layout = CollectorLayout(tmp_path)
    layout.ensure()
    value = {
        "slot": "A",
        "host": "127.0.0.1",
        "port": 6695,
        "nick": "Collector",
        "account_id": "0123456789abcdef01234567",
        "nonce": "accountId=0123456789abcdef01234567&nonce=test",
        "used": False,
    }
    atomic_write_json(layout.ticket_path("A"), value)
    store = TicketStore(layout, "A")
    ticket = store.load_available()
    assert ticket is not None
    assert read_json(layout.ticket_path("A"))["nonce"] == value["nonce"]

    store.mark_used(ticket, outcome="authenticated")
    saved = read_json(layout.ticket_path("A"))
    assert saved["used"] is True
    assert saved["outcome"] == "authenticated"
    assert "nonce" not in saved
    assert store.load_available() is None


def test_registration_and_join_tracker_use_irc_wire_nick():
    ticket = Ticket.from_dict({
        "slot": "A",
        "host": "127.0.0.1",
        "port": 6695,
        "nick": "-Collector.test",
        "account_id": "0123456789abcdef01234567",
        "nonce": "accountId=0123456789abcdef01234567&nonce=test",
    })
    session = IrcSession(ticket, bytes(range(64)), plain=True)

    class SocketSink:
        def __init__(self):
            self.payload = b""

        def sendall(self, payload):
            self.payload += payload

    sink = SocketSink()
    session.socket = sink
    session.send_registration("token")
    tracker, report = session.join_channels(
        ["#T_ZH"],
        on_privmsg=lambda _entry: None,
        should_stop=lambda: True,
    )

    assert sink.payload == (
        b"NICK `-Collector|test\r\n"
        b"USER 0123456789abcdef01234567_0 0 * token\r\n"
    )
    assert tracker.own_nick == "`-collector|test"
    assert report.status == "stopped"


def test_collector_emits_wire_and_game_nicks_separately():
    account_id = "0123456789abcdef01234567"
    ticket = Ticket.from_dict({
        "slot": "A",
        "host": "127.0.0.1",
        "port": 6695,
        "nick": "Collector",
        "account_id": account_id,
        "nonce": f"accountId={account_id}&nonce=test",
    })
    session = IrcSession(ticket, bytes(range(64)), plain=True)
    tracker = JoinTracker([], own_nick="Collector")
    presence_events = []
    messages = []
    session.on_presence = presence_events.append

    session._handle_line(
        f":`-T|E|S-ExamplePlayer\ue000!{account_id}_0@host JOIN :#T_ZH",
        tracker,
        messages.append,
        None,
    )
    session._handle_line(
        f":ExamplePlayer|\ue004!{account_id}_4@host PRIVMSG #T_ZH :hello",
        tracker,
        messages.append,
        None,
    )
    session._handle_line(
        f":Xbox Seller\ue001!{account_id}_1@host PRIVMSG #T_ZH :hello",
        tracker,
        messages.append,
        None,
    )

    assert presence_events[0]["irc_nick"] == "`-T|E|S-ExamplePlayer\ue000"
    assert presence_events[0]["nick"] == "-T.E.S-ExamplePlayer"
    assert presence_events[0]["platform"] == "windows"
    assert messages[0]["irc_nick"] == "ExamplePlayer|\ue004"
    assert messages[0]["nick"] == "ExamplePlayer."
    assert messages[0]["platform"] == "ios"
    assert messages[1]["irc_nick"] == "Xbox Seller\ue001"
    assert messages[1]["nick"] == "Xbox Seller"
    assert messages[1]["platform"] == "xbox"


def test_session_protocol_probe_sends_queries_and_records_raw_evidence(monkeypatch):
    ticket = Ticket.from_dict({
        "slot": "B",
        "host": "127.0.0.1",
        "port": 6695,
        "nick": "Collector",
        "account_id": "0123456789abcdef01234567",
        "nonce": "accountId=0123456789abcdef01234567&nonce=test",
    })
    session = IrcSession(ticket, bytes(range(64)), plain=True)

    class SocketSink:
        def __init__(self):
            self.payload = b""

        def sendall(self, payload):
            self.payload += payload

    session.socket = SocketSink()
    incoming = iter((
        ":server 730 Collector :ExamplePlayer\ue000!0123456789abcdef01234567_0@host",
        None,
    ))
    monkeypatch.setattr(session, "_readline", lambda _remaining: next(incoming))
    events = []

    result = session.probe_online_status(
        "ExamplePlayer\ue000",
        duration_seconds=2,
        query_interval_seconds=10,
        on_event=lambda direction, line: events.append((direction, line)),
    )

    sent = session.socket.payload.decode("utf-8").splitlines()
    assert sent == [
        "CAP LS 302",
        "MONITOR + ExamplePlayer\ue000",
        "MONITOR S",
        "ISON ExamplePlayer\ue000",
        "WHOIS ExamplePlayer\ue000",
        "WHOIS ExamplePlayer\ue000",
    ]
    assert events[:5] == [("out", line) for line in sent[:5]]
    assert events[5][0] == "in"
    assert events[6] == ("out", "WHOIS ExamplePlayer\ue000")
    assert result["reason"] == "eof"
    assert result["monitor"] == "supported"
    assert result["monitor_online_seen"] is True
    assert result["account_ids"] == ["0123456789abcdef01234567"]


def test_plain_session_auth_and_partial_join_remain_degraded():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    received: list[str] = []

    def server():
        connection, _address = listener.accept()
        with connection:
            connection.settimeout(2)
            connection.sendall(b":mock NOTICE * :Auth 00000001:\r\n")
            buffer = b""
            while True:
                chunk = connection.recv(4096)
                if not chunk:
                    return
                buffer += chunk
                while b"\r\n" in buffer:
                    raw, buffer = buffer.split(b"\r\n", 1)
                    line = raw.decode()
                    received.append(line)
                    if line.startswith("USER "):
                        connection.sendall(b":mock 001 Collector :Welcome\r\n")
                    elif line == "JOIN #T_ZH":
                        connection.sendall(
                            b":mock 366 Collector #T_ZH :End of /NAMES list\r\n"
                        )
                    elif line == "JOIN #T_FR":
                        connection.sendall(
                            b":mock 405 Collector #T_FR :Too many channels\r\n"
                        )
                        return

    thread = threading.Thread(target=server, daemon=True)
    thread.start()
    ticket = Ticket.from_dict({
        "slot": "A",
        "host": "127.0.0.1",
        "port": port,
        "nick": "Collector",
        "account_id": "0123456789abcdef01234567",
        "nonce": "accountId=0123456789abcdef01234567&nonce=test",
    })
    with IrcSession(ticket, bytes(range(64)), plain=True) as session:
        auth = session.connect_and_auth()
        assert auth.authenticated
        assert auth.diagnostics["registration_user_format"] == "account_id_0"
        assert auth.diagnostics["challenge_count"] == 1
        _tracker, report = session.join_channels(
            ["#T_ZH", "#T_FR"],
            on_privmsg=lambda _entry: None,
            batch_size=2,
            batch_gap=0,
            settle_timeout=0.2,
        )
    thread.join(timeout=2)
    listener.close()

    assert report.status == "degraded"
    assert report.joined == ("#T_ZH",)
    assert report.missing == ("#T_FR",)
    assert any(line.startswith("NICK Collector") for line in received)
    # 当前 PC 客户端的真实注册抓包和独立 001 样本都要求 ``_0``。
    assert any(
        line.startswith("USER 0123456789abcdef01234567_0 0 * ")
        for line in received
    )


def test_bad_credentials_error_preserves_rejection_reason():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def server():
        connection, _address = listener.accept()
        with connection:
            connection.settimeout(2)
            connection.sendall(b":mock NOTICE * :Auth 00000001:\r\n")
            buffer = b""
            while b"USER " not in buffer:
                chunk = connection.recv(4096)
                if not chunk:
                    return
                buffer += chunk
            connection.sendall(
                b"ERROR :Closing Link: Collector (Bad Credentials)\r\n"
            )

    thread = threading.Thread(target=server, daemon=True)
    thread.start()
    ticket = Ticket.from_dict({
        "slot": "A",
        "host": "127.0.0.1",
        "port": port,
        "nick": "Collector",
        "account_id": "0123456789abcdef01234567",
        "nonce": "accountId=0123456789abcdef01234567&nonce=test",
    })
    with IrcSession(ticket, bytes(range(64)), plain=True) as session:
        auth = session.connect_and_auth()
    thread.join(timeout=2)
    listener.close()

    assert auth.status == "rejected:Bad Credentials"
    assert auth.response_sent is True
    assert auth.diagnostics["registration_user_format"] == "account_id_0"


def test_join_honors_stop_before_sending_or_consuming_more_channels():
    ticket = Ticket.from_dict({
        "slot": "A",
        "host": "127.0.0.1",
        "port": 6695,
        "nick": "Collector",
        "account_id": "0123456789abcdef01234567",
        "nonce": "accountId=0123456789abcdef01234567&nonce=test",
    })
    session = IrcSession(ticket, bytes(range(64)), plain=True)

    _tracker, report = session.join_channels(
        ["#T_ZH", "#T_FR"],
        on_privmsg=lambda _entry: None,
        should_stop=lambda: True,
    )

    assert report.status == "stopped"
    assert report.joined == ()


def test_supervisor_lock_is_exclusive_and_status_remains_readable(tmp_path):
    layout = CollectorLayout(tmp_path)
    layout.ensure()
    with SupervisorLock(layout.supervisor_lock):
        status = collector_status(layout)
        assert status["supervisor_running"] is True
        with pytest.raises(RuntimeError, match="已运行"):
            SupervisorLock(layout.supervisor_lock).acquire()
    assert collector_status(layout)["supervisor_running"] is False


def test_supervisor_summary_exposes_riven_and_filter_counts(tmp_path):
    layout = CollectorLayout(tmp_path)
    layout.ensure()
    atomic_write_json(layout.state_path("A"), {
        "slot": "A",
        "status": "listening",
        "pid": os.getpid(),
        "process_identity": pid_identity(os.getpid()),
        "joined": ["#T_ZH", "#Q_ZH"],
        "privmsg_count": 12,
        "filtered_privmsg_count": 345,
        "snapshot_count": 7,
        "privmsg_by_channel": {"#T_ZH": 12, "#Q_ZH": 0},
        "filtered_privmsg_by_channel": {"#T_ZH": 300, "#Q_ZH": 45},
    })

    summary = supervisor_module._supervisor_summary(layout)

    assert "A:采集中 频道2 快照7次 紫卡12条 过滤345条" in summary
    assert "B:等待票据" in summary
    shards = {
        "A": ("#T_ZH", "#Q_ZH"),
        "B": ("#T_EN",),
        "C": ("#R_ZH",),
        "D": ("#G_ZH",),
    }
    channel_lines = supervisor_module._supervisor_channel_lines(layout, shards)
    assert channel_lines[0] == "  槽 A · 2 个频道"
    assert channel_lines[1] == "    #T_ZH  12 / 312  │  #Q_ZH   0 /  45"
    assert channel_lines[2] == "  槽 B · 1 个频道"
    assert channel_lines[3] == "    #T_EN   0 /   0"

    report = supervisor_module._supervisor_report(layout, shards)
    report_lines = report.splitlines()
    assert report_lines[0] == "四槽概览"
    assert report_lines[1].split() == [
        "槽", "状态", "频道", "快照", "紫卡", "过滤",
    ]
    assert report_lines[2].split() == [
        "A", "采集中", "2", "7", "12", "345",
    ]
    assert report_lines[3].split() == [
        "B", "等待票据", "—", "—", "—", "—",
    ]
    assert "逐频道（紫卡 / 总消息）" in report_lines


def test_supervisor_log_is_persisted_with_full_timestamp(tmp_path, capsys):
    log_path = tmp_path / "supervisor.log"

    supervisor_module._print_supervisor("四槽概览\n  A  采集中", log_path=log_path)

    output = capsys.readouterr().out
    persisted = log_path.read_text(encoding="utf-8")
    assert output == persisted
    assert "四槽概览" in persisted
    assert "  A  采集中" in persisted
    assert "+" in persisted[:35]
    assert persisted.count("[") == 1
    assert output.splitlines()[1] == "    A  采集中"


def test_supervisor_log_rotates_while_running(tmp_path, capsys, monkeypatch):
    log_path = tmp_path / "supervisor.log"
    old_content = "旧日志内容\n" * 4
    log_path.write_text(old_content, encoding="utf-8")
    rotate_log = supervisor_module._rotate_log
    monkeypatch.setattr(
        supervisor_module,
        "_rotate_log",
        lambda path: rotate_log(path, max_bytes=16),
    )

    supervisor_module._print_supervisor("十七槽概览", log_path=log_path)

    capsys.readouterr()
    assert log_path.with_suffix(".log.1").read_text(
        encoding="utf-8",
    ) == old_content
    assert "十七槽概览" in log_path.read_text(encoding="utf-8")


def test_supervisor_channel_lines_wrap_after_three_channels(tmp_path):
    layout = CollectorLayout(tmp_path)
    layout.ensure()
    shards = {
        "A": ("#T_EN_NA", "#T_EN_EU", "#T_EN_AS", "#T_EN_SA"),
        "B": ("#T_ZH",),
        "C": ("#T_JA",),
        "D": ("#T_KO",),
    }

    lines = supervisor_module._supervisor_channel_lines(layout, shards)

    assert lines[0] == "  槽 A · 4 个频道"
    assert lines[1].count("│") == 2
    assert all(channel in lines[1] for channel in shards["A"][:3])
    assert lines[2].strip().startswith("#T_EN_SA")
    assert "│" not in lines[2]


def test_foreground_supervisor_waits_for_tickets_and_stops_cleanly(tmp_path):
    layout = CollectorLayout(tmp_path)
    layout.ensure()
    atomic_write_json(
        layout.accounts_path,
        {slot: {"nick": f"Collector{slot}"} for slot in "ABCD"},
    )
    layout.psk_path.write_bytes(bytes(range(64)))
    atomic_write_json(layout.control_path, {
        "run_id": "old-run",
        "status": "stopped",
        "started_at": "2000-01-01T00:00:00+00:00",
    })
    outcomes: list[object] = []

    def run():
        try:
            outcomes.append(run_supervisor(
                layout,
                script_path=ROOT / "scripts" / "run_chat_collector.py",
            ))
        except Exception as error:  # pragma: no cover - assertion reports payload
            outcomes.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status = collector_status(layout)
        control = read_json(layout.control_path) or {}
        if (status["supervisor_running"]
                and control.get("run_id") != "old-run"):
            break
        time.sleep(0.02)
    assert collector_status(layout)["supervisor_running"] is True
    running_control = read_json(layout.control_path)
    assert running_control["run_id"] != "old-run"
    assert running_control["started_at"] != "2000-01-01T00:00:00+00:00"

    request_stop(layout)
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert outcomes == [0]
    assert collector_status(layout)["supervisor_running"] is False
    assert read_json(layout.control_path)["started_at"] == running_control["started_at"]


def test_losing_supervisor_does_not_remove_existing_stop_request(tmp_path):
    layout = CollectorLayout(tmp_path)
    layout.ensure()
    atomic_write_json(
        layout.accounts_path,
        {slot: {"nick": f"Collector{slot}"} for slot in "ABCD"},
    )
    layout.psk_path.write_bytes(bytes(range(64)))
    request_stop(layout)

    with SupervisorLock(layout.supervisor_lock):
        with pytest.raises(RuntimeError, match="已运行"):
            run_supervisor(
                layout,
                script_path=ROOT / "scripts" / "run_chat_collector.py",
            )

    assert layout.stop_flag.is_file()


def test_pending_slot_stop_never_consumes_a_new_ticket(tmp_path, monkeypatch):
    layout = CollectorLayout(tmp_path)
    layout.ensure()
    atomic_write_json(
        layout.accounts_path,
        {slot: {"nick": f"Collector{slot}"} for slot in "ABCD"},
    )
    layout.psk_path.write_bytes(bytes(range(64)))
    ticket = {
        "slot": "A",
        "host": "127.0.0.1",
        "port": 6695,
        "nick": "CollectorA",
        "account_id": "0123456789abcdef01234567",
        "nonce": "accountId=0123456789abcdef01234567&nonce=test",
        "used": False,
    }
    atomic_write_json(layout.ticket_path("A"), ticket)
    atomic_write_json(layout.slot_stop_path("A"), {"reason": "operator"})

    class UnexpectedSession:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("停止中的槽不应建立 IRC 会话")

    monkeypatch.setattr(supervisor_module, "IrcSession", UnexpectedSession)

    assert supervisor_module.run_slot("A", layout) == 3
    assert read_json(layout.ticket_path("A"))["used"] is False
    assert read_json(layout.state_path("A"))["status"] == "stop_requested"
    assert collector_status(layout)["slots"]["A"]["stop_requested"] is True


def test_state_write_failure_after_auth_does_not_burn_session(
        tmp_path, monkeypatch):
    layout = CollectorLayout(tmp_path)
    layout.ensure()
    atomic_write_json(
        layout.accounts_path,
        {slot: {"nick": f"Collector{slot}"} for slot in "ABCD"},
    )
    layout.psk_path.write_bytes(bytes(range(64)))
    atomic_write_json(layout.ticket_path("A"), {
        "slot": "A",
        "host": "127.0.0.1",
        "port": 6695,
        "nick": "CollectorA",
        "account_id": "0123456789abcdef01234567",
        "nonce": "accountId=0123456789abcdef01234567&nonce=test",
        "used": False,
    })

    class FakeSession:
        def __init__(self, *_args, **_kwargs):
            pass

        def connect_and_auth(self):
            return AuthResult("authenticated", True)

        def join_channels(self, channels, *, on_privmsg, should_stop):
            return object(), JoinReport("healthy", tuple(channels), (), {})

        def listen(self, *_args, **_kwargs):
            return "stop"

        def close(self):
            pass

    original_state = supervisor_module._state

    def fail_joining(layout_arg, slot, status, **details):
        if status == "joining":
            raise PermissionError("state file locked")
        return original_state(layout_arg, slot, status, **details)

    monkeypatch.setattr(supervisor_module, "IrcSession", FakeSession)
    monkeypatch.setattr(supervisor_module, "_state", fail_joining)

    assert supervisor_module.run_slot("A", layout) == 0
    assert read_json(layout.ticket_path("A"))["used"] is True


def test_armed_protocol_probe_replaces_join_and_persists_result(
        tmp_path, monkeypatch):
    layout = CollectorLayout(tmp_path)
    layout.ensure()
    atomic_write_json(
        layout.accounts_path,
        {slot: {"nick": f"Collector{slot}"} for slot in "ABCD"},
    )
    layout.psk_path.write_bytes(bytes(range(64)))
    atomic_write_json(layout.ticket_path("B"), {
        "slot": "B",
        "host": "127.0.0.1",
        "port": 6695,
        "nick": "CollectorB",
        "account_id": "0123456789abcdef01234567",
        "nonce": "accountId=0123456789abcdef01234567&nonce=test",
        "used": False,
    })
    request = ProtocolProbeRequest.create(
        slot="B",
        target_nick="ExamplePlayer",
        target_platform="windows",
        target_account_id="0123456789abcdef01234567",
        probe_channels=("#T_ZH",),
        duration_seconds=60,
        query_interval_seconds=10,
    )
    arm_protocol_probe(layout.protocol_probe_path, request)

    class FakeSession:
        def __init__(self, *_args, **_kwargs):
            pass

        def connect_and_auth(self):
            return AuthResult("authenticated", True)

        def join_channels(self, *_args, **_kwargs):
            raise AssertionError("已装载协议探测时不应 JOIN")

        def probe_online_status(
                self, target_irc_nick, *, on_event, **_kwargs):
            assert target_irc_nick == "ExamplePlayer\ue000"
            assert _kwargs["target_account_id"] == "0123456789abcdef01234567"
            assert _kwargs["probe_channels"] == ("#T_ZH",)
            on_event("out", f"MONITOR + {target_irc_nick}")
            on_event(
                "in",
                ":server 731 CollectorB :ExamplePlayer\ue000",
            )
            return {
                "reason": "complete",
                "monitor": "supported",
                "monitor_offline_seen": True,
                "account_ids": [],
            }

        def close(self):
            pass

    monkeypatch.setattr(supervisor_module, "IrcSession", FakeSession)

    assert supervisor_module.run_slot("B", layout) == 0
    request_state = read_json(layout.protocol_probe_path)
    assert request_state["status"] == "complete"
    assert request_state["result"]["monitor"] == "supported"
    summary = read_json(request_state["summary_path"])
    assert summary["status"] == "complete"
    assert summary["target_irc_nick"] == "ExamplePlayer\ue000"
    assert summary["target_account_id"] == "0123456789abcdef01234567"
    assert summary["monitor_offline_seen"] is True
    events = [json.loads(line) for line in Path(
        request_state["event_path"]
    ).read_text(encoding="utf-8").splitlines()]
    assert [event["direction"] for event in events] == ["out", "in"]
    state = read_json(layout.state_path("B"))
    assert state["status"] == "probe_complete"
    assert state["probe_result"]["monitor"] == "supported"
    assert read_json(layout.ticket_path("B"))["used"] is True


def test_slot_persists_only_complete_riven_links(tmp_path, monkeypatch):
    layout = CollectorLayout(tmp_path)
    layout.ensure()
    atomic_write_json(
        layout.accounts_path,
        {slot: {"nick": f"Collector{slot}"} for slot in "ABCD"},
    )
    layout.psk_path.write_bytes(bytes(range(64)))
    atomic_write_json(layout.ticket_path("A"), {
        "slot": "A",
        "host": "127.0.0.1",
        "port": 6695,
        "nick": "CollectorA",
        "account_id": "0123456789abcdef01234567",
        "nonce": "accountId=0123456789abcdef01234567&nonce=test",
        "used": False,
    })
    link = (
        "[OMG-LotusRifleRandomModRare:"
        "8T9NiKpb9TjuN70Tfedj9RLDSiibxgUk]"
    )

    class FakeSession:
        def __init__(self, *_args, **_kwargs):
            pass

        def connect_and_auth(self):
            return AuthResult("authenticated", True)

        def join_channels(self, channels, *, on_privmsg, should_stop):
            assert not should_stop()
            for index, text in enumerate((
                "WTB Torid riven 500p",
                "[Rifle Riven Mod] 10p",
                f"500 出 {link}",
                "[OMG-LotusRifleRandomModRare:truncated",
            )):
                on_privmsg({
                    "t": f"2026-07-26T00:00:0{index}+00:00",
                    "slot": "A",
                    "nick": "Seller",
                    "sender_id": "abcdef0123456789abcdef01",
                    "chan": "#T_ZH",
                    "text": text,
                    "account": "CollectorA",
                })
            return object(), JoinReport("healthy", tuple(channels), (), {})

        def listen(self, *_args, health_callback, **_kwargs):
            health_callback({
                "status": "degraded",
                "joined": ["#T_ZH"],
                "missing": ["#T_FR"],
                "rejected": {"#T_FR": "473"},
                "last_line_at": "2026-07-26T00:01:00+00:00",
            })
            return "stop"

        def close(self):
            pass

    monkeypatch.setattr(supervisor_module, "IrcSession", FakeSession)

    assert supervisor_module.run_slot("A", layout) == 0
    files = list(layout.feed_dir.glob("privmsg_*_A.jsonl"))
    assert len(files) == 1
    saved = [json.loads(line) for line in files[0].read_text(
        encoding="utf-8"
    ).splitlines()]
    assert [entry["text"] for entry in saved] == [f"500 出 {link}"]
    state = read_json(layout.state_path("A"))
    assert state["privmsg_count"] == 1
    assert state["filtered_privmsg_count"] == 3
    assert state["privmsg_by_channel"]["#T_ZH"] == 1
    assert state["filtered_privmsg_by_channel"]["#T_ZH"] == 3
    assert state["privmsg_by_channel"]["#G_EN_AS"] == 0
    assert state["status"] == "stopped"
    assert state["joined"] == ["#T_ZH"]
    assert state["missing"] == ["#T_FR"]
    assert state["rejected"] == {"#T_FR": "473"}
    assert state["last_line_at"] == "2026-07-26T00:01:00+00:00"


def test_join_socket_failure_reconnects_with_in_memory_nonce(
        tmp_path, monkeypatch):
    layout = CollectorLayout(tmp_path)
    layout.ensure()
    atomic_write_json(
        layout.accounts_path,
        {slot: {"nick": f"Collector{slot}"} for slot in "ABCD"},
    )
    layout.psk_path.write_bytes(bytes(range(64)))
    atomic_write_json(layout.ticket_path("A"), {
        "slot": "A",
        "host": "127.0.0.1",
        "port": 6695,
        "nick": "CollectorA",
        "account_id": "0123456789abcdef01234567",
        "nonce": "accountId=0123456789abcdef01234567&nonce=test",
        "used": False,
    })

    sessions = []

    class FakeSession:
        def __init__(self, *_args, **_kwargs):
            self.index = len(sessions)
            sessions.append(self)
            self.on_presence = None

        def connect_and_auth(self):
            return AuthResult("authenticated", True)

        def join_channels(self, channels, *, on_privmsg, should_stop):
            if self.index == 0:
                raise ConnectionResetError("peer reset")
            return object(), JoinReport("healthy", tuple(channels), (), {})

        def listen(self, *_args, **_kwargs):
            return "stop"

        def close(self):
            pass

    monkeypatch.setattr(supervisor_module, "IrcSession", FakeSession)
    monkeypatch.setattr(
        supervisor_module,
        "_reconnect_delay",
        lambda _policy, _attempt, _slot: 0.01,
    )

    assert supervisor_module.run_slot("A", layout) == 0
    state = read_json(layout.state_path("A"))
    assert state["status"] == "stopped"
    assert state["ticket_id"]
    assert len(sessions) == 2
    ticket = read_json(layout.ticket_path("A"))
    assert ticket["used"] is True
    assert "nonce" not in ticket
    presence = list(layout.feed_dir.glob("presence_*_A.jsonl"))
    events = [
        json.loads(line)["type"]
        for line in presence[0].read_text(encoding="utf-8").splitlines()
    ]
    assert events == [
        "observer_start", "observer_stop", "observer_start", "observer_stop",
    ]


def test_reconnect_keeps_message_counters_across_sessions(tmp_path, monkeypatch):
    layout = CollectorLayout(tmp_path)
    layout.ensure()
    atomic_write_json(
        layout.accounts_path,
        {slot: {"nick": f"Collector{slot}"} for slot in "ABCD"},
    )
    layout.psk_path.write_bytes(bytes(range(64)))
    atomic_write_json(layout.ticket_path("A"), {
        "slot": "A",
        "host": "127.0.0.1",
        "port": 6695,
        "nick": "CollectorA",
        "account_id": "0123456789abcdef01234567",
        "nonce": "accountId=0123456789abcdef01234567&nonce=test",
        "used": False,
    })
    link = (
        "[OMG-LotusRifleRandomModRare:"
        "8T9NiKpb9TjuN70Tfedj9RLDSiibxgUk]"
    )
    sessions = []

    class FakeSession:
        def __init__(self, *_args, **_kwargs):
            self.index = len(sessions)
            sessions.append(self)
            self.on_presence = None

        def connect_and_auth(self):
            return AuthResult("authenticated", True)

        def join_channels(self, channels, *, on_privmsg, should_stop):
            on_privmsg({
                "t": f"2026-08-13T00:00:0{self.index}+00:00",
                "slot": "A",
                "nick": f"Seller{self.index}",
                "sender_id": "abcdef0123456789abcdef01",
                "chan": channels[0],
                "text": f"{self.index} 出 {link}",
                "account": "CollectorA",
            })
            return object(), JoinReport("healthy", tuple(channels), (), {})

        def listen(self, *_args, **_kwargs):
            return "eof" if self.index == 0 else "stop"

        def close(self):
            pass

    monkeypatch.setattr(supervisor_module, "IrcSession", FakeSession)
    monkeypatch.setattr(
        supervisor_module,
        "_reconnect_delay",
        lambda _policy, _attempt, _slot: 0.01,
    )

    assert supervisor_module.run_slot("A", layout) == 0
    assert len(sessions) == 2
    state = read_json(layout.state_path("A"))
    assert state["status"] == "stopped"
    assert state["privmsg_count"] == 2
    assert state["privmsg_by_channel"]["#G_EN_AS"] == 2


def test_reconnect_auth_rejection_immediately_needs_new_ticket(
        tmp_path, monkeypatch):
    layout = CollectorLayout(tmp_path)
    layout.ensure()
    atomic_write_json(
        layout.accounts_path,
        {slot: {"nick": f"Collector{slot}"} for slot in "ABCD"},
    )
    layout.psk_path.write_bytes(bytes(range(64)))
    atomic_write_json(layout.ticket_path("A"), {
        "slot": "A",
        "host": "127.0.0.1",
        "port": 6695,
        "nick": "CollectorA",
        "account_id": "0123456789abcdef01234567",
        "nonce": "accountId=0123456789abcdef01234567&nonce=test",
        "used": False,
    })
    sessions = []

    class FakeSession:
        def __init__(self, *_args, **_kwargs):
            self.index = len(sessions)
            sessions.append(self)
            self.on_presence = None

        def connect_and_auth(self):
            if self.index == 0:
                return AuthResult("authenticated", True)
            return AuthResult("rejected:Bad Credentials", True)

        def join_channels(self, channels, *, on_privmsg, should_stop):
            assert self.index == 0
            return object(), JoinReport("healthy", tuple(channels), (), {})

        def listen(self, *_args, **_kwargs):
            return "eof"

        def close(self):
            pass

    monkeypatch.setattr(supervisor_module, "IrcSession", FakeSession)
    monkeypatch.setattr(
        supervisor_module,
        "_reconnect_delay",
        lambda _policy, _attempt, _slot: 0.01,
    )

    assert supervisor_module.run_slot("A", layout) == 6
    assert len(sessions) == 2
    state = read_json(layout.state_path("A"))
    assert state["status"] == "needs_ticket"
    assert state["reason"] == "reconnect_auth:rejected:Bad Credentials"


def test_expired_ticket_does_not_enter_reconnect_delay(tmp_path, monkeypatch):
    layout = CollectorLayout(tmp_path)
    layout.ensure()
    atomic_write_json(
        layout.accounts_path,
        {slot: {"nick": f"Collector{slot}"} for slot in "ABCD"},
    )
    layout.psk_path.write_bytes(bytes(range(64)))
    atomic_write_json(layout.ticket_path("A"), {
        "slot": "A",
        "host": "127.0.0.1",
        "port": 6695,
        "nick": "CollectorA",
        "account_id": "0123456789abcdef01234567",
        "nonce": "accountId=0123456789abcdef01234567&nonce=test",
        "created_at": "2000-01-01T00:00:00+00:00",
        "used": False,
    })
    sessions = []

    class FakeSession:
        def __init__(self, *_args, **_kwargs):
            sessions.append(self)
            self.on_presence = None

        def connect_and_auth(self):
            return AuthResult("authenticated", True)

        def join_channels(self, channels, *, on_privmsg, should_stop):
            return object(), JoinReport("healthy", tuple(channels), (), {})

        def listen(self, *_args, **_kwargs):
            return "eof"

        def close(self):
            pass

    monkeypatch.setattr(supervisor_module, "IrcSession", FakeSession)
    monkeypatch.setattr(
        supervisor_module,
        "_reconnect_delay",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("过期票据不应计算重连退避")
        ),
    )

    assert supervisor_module.run_slot("A", layout) == 6
    assert len(sessions) == 1
    state = read_json(layout.state_path("A"))
    assert state["status"] == "needs_ticket"
    assert state["reason"] == "reuse_ttl_expired"


def test_reconnect_attempt_limit_stops_retryable_auth_failures(
        tmp_path, monkeypatch):
    layout = CollectorLayout(tmp_path)
    layout.ensure()
    atomic_write_json(
        layout.accounts_path,
        {slot: {"nick": f"Collector{slot}"} for slot in "ABCD"},
    )
    layout.psk_path.write_bytes(bytes(range(64)))
    atomic_write_json(layout.ticket_path("A"), {
        "slot": "A",
        "host": "127.0.0.1",
        "port": 6695,
        "nick": "CollectorA",
        "account_id": "0123456789abcdef01234567",
        "nonce": "accountId=0123456789abcdef01234567&nonce=test",
        "used": False,
    })
    sessions = []

    class FakeSession:
        def __init__(self, *_args, **_kwargs):
            self.index = len(sessions)
            sessions.append(self)
            self.on_presence = None

        def connect_and_auth(self):
            return AuthResult(
                "authenticated" if self.index == 0 else "eof",
                self.index == 0,
            )

        def join_channels(self, channels, *, on_privmsg, should_stop):
            return object(), JoinReport("healthy", tuple(channels), (), {})

        def listen(self, *_args, **_kwargs):
            return "eof"

        def close(self):
            pass

    monkeypatch.setattr(supervisor_module, "IrcSession", FakeSession)
    monkeypatch.setattr(
        supervisor_module,
        "load_reconnect_policy",
        lambda _path: supervisor_module.ReconnectPolicy(
            enabled=True,
            backoff_seconds=(0.1,),
            max_attempts=1,
            reuse_ttl_hours=6,
        ),
    )
    monkeypatch.setattr(
        supervisor_module,
        "_reconnect_delay",
        lambda _policy, _attempt, _slot: 0.01,
    )

    assert supervisor_module.run_slot("A", layout) == 6
    assert len(sessions) == 2
    state = read_json(layout.state_path("A"))
    assert state["status"] == "needs_ticket"
    assert state["reason"] == "reconnect_attempts_exhausted"


def test_initial_retryable_auth_failure_reuses_ticket_in_worker_memory(
        tmp_path, monkeypatch):
    layout = CollectorLayout(tmp_path)
    layout.ensure()
    atomic_write_json(
        layout.accounts_path,
        {slot: {"nick": f"Collector{slot}"} for slot in "ABCD"},
    )
    layout.psk_path.write_bytes(bytes(range(64)))
    atomic_write_json(layout.ticket_path("A"), {
        "slot": "A",
        "host": "127.0.0.1",
        "port": 6695,
        "nick": "CollectorA",
        "account_id": "0123456789abcdef01234567",
        "nonce": "accountId=0123456789abcdef01234567&nonce=test",
        "used": False,
    })

    sessions = []

    class FakeSession:
        def __init__(self, *_args, **_kwargs):
            self.index = len(sessions)
            sessions.append(self)
            self.on_presence = None

        def connect_and_auth(self):
            return AuthResult(
                "auth_timeout" if self.index == 0 else "authenticated",
                True,
            )

        def join_channels(self, channels, *, on_privmsg, should_stop):
            return object(), JoinReport("healthy", tuple(channels), (), {})

        def listen(self, *_args, **_kwargs):
            return "stop"

        def close(self):
            pass

    monkeypatch.setattr(supervisor_module, "IrcSession", FakeSession)
    monkeypatch.setattr(
        supervisor_module,
        "_reconnect_delay",
        lambda _policy, _attempt, _slot: 0.01,
    )

    assert supervisor_module.run_slot("A", layout) == 0
    assert len(sessions) == 2
    ticket = read_json(layout.ticket_path("A"))
    state = read_json(layout.state_path("A"))
    assert ticket["used"] is True
    assert ticket["outcome"] == "auth_retrying"
    assert "nonce" not in ticket
    assert state["status"] == "stopped"


def test_initial_auth_retry_limit_keeps_nonce_only_in_worker_memory(
        tmp_path, monkeypatch):
    layout = CollectorLayout(tmp_path)
    layout.ensure()
    atomic_write_json(
        layout.accounts_path,
        {slot: {"nick": f"Collector{slot}"} for slot in "ABCD"},
    )
    layout.psk_path.write_bytes(bytes(range(64)))
    atomic_write_json(layout.ticket_path("A"), {
        "slot": "A",
        "host": "127.0.0.1",
        "port": 6695,
        "nick": "CollectorA",
        "account_id": "0123456789abcdef01234567",
        "nonce": "accountId=0123456789abcdef01234567&nonce=test",
        "used": False,
    })

    class FakeSession:
        def __init__(self, *_args, **_kwargs):
            self.on_presence = None

        def connect_and_auth(self):
            return AuthResult("auth_timeout", True)

        def close(self):
            pass

    monkeypatch.setattr(supervisor_module, "IrcSession", FakeSession)
    monkeypatch.setattr(
        supervisor_module,
        "load_reconnect_policy",
        lambda _path: supervisor_module.ReconnectPolicy(
            enabled=True,
            backoff_seconds=(0.1,),
            max_attempts=1,
            reuse_ttl_hours=6,
        ),
    )
    monkeypatch.setattr(
        supervisor_module,
        "_reconnect_delay",
        lambda _policy, _attempt, _slot: 0.01,
    )

    assert supervisor_module.run_slot("A", layout) == 6
    ticket = read_json(layout.ticket_path("A"))
    state = read_json(layout.state_path("A"))
    assert ticket["used"] is True
    assert ticket["outcome"] == "auth_retrying"
    assert "nonce" not in ticket
    assert state["status"] == "needs_ticket"
    assert state["reason"] == "reconnect_attempts_exhausted"


def test_listener_distinguishes_sink_and_internal_failures(monkeypatch):
    ticket = Ticket.from_dict({
        "slot": "A",
        "host": "127.0.0.1",
        "port": 6695,
        "nick": "Collector",
        "account_id": "0123456789abcdef01234567",
        "nonce": "accountId=0123456789abcdef01234567&nonce=test",
    })
    line = ":Seller!0123456789abcdef01234567_0@host PRIVMSG #T_ZH :hello"

    def session_with_line():
        session = IrcSession(ticket, bytes(range(64)), plain=True)
        monkeypatch.setattr(session, "_readline", lambda _remaining: line)
        return session

    tracker = JoinTracker(["#T_ZH"], own_nick="Collector")
    storage = session_with_line().listen(
        tracker,
        on_privmsg=lambda _entry: (_ for _ in ()).throw(PermissionError("disk")),
        should_stop=lambda: False,
    )
    internal = session_with_line().listen(
        tracker,
        on_privmsg=lambda _entry: (_ for _ in ()).throw(ValueError("bug")),
        should_stop=lambda: False,
    )

    assert storage.startswith("storage_error:")
    assert internal.startswith("internal_error:")
