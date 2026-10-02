from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.plugins.riven_sniper import chat_tracking  # noqa: E402
from src.plugins.riven_sniper.chat_tracking import (  # noqa: E402
    TrackingExportTooLarge,
    TrackingStore,
)
from src.plugins.riven_sniper.tracking_view import (  # noqa: E402
    format_ownership,
    format_riven_summary,
    parse_tracking_request,
)
from tests.test_chat_tracking import _message  # noqa: E402


def _seed_distinct_rivens(store: TrackingStore, total: int = 30) -> str:
    account_id = "0123456789abcdef01234567"
    first = store.ingest(
        _message(timestamp=100, seller_id=account_id, nick="Seller"),
        dedupe_seconds=0,
    )[0]
    for number in range(2, total + 1):
        content_hash = f"{number:064x}"
        store.connection.execute(
            """INSERT INTO rivens
               SELECT ?,?,category,weapon_index,polarity,lvl_req,stats,
                      first_seen+?,last_seen+?,max_rerolls,first_event_key,
                      first_channel,first_seller_id,first_seller_nick,
                      first_seller_platform,last_event_key,last_channel
               FROM rivens WHERE content_hash=?""",
            (content_hash, number, number, number, first.fingerprint),
        )
        store.connection.execute(
            """INSERT INTO riven_ownerships
               (content_hash,holder_id,holder_nick,holder_platform,
                first_seen,last_seen,first_channel,last_channel,
                first_event_key,last_event_key,ambiguous)
               VALUES (?,?,?,?,?,?,?,?,?,?,0)""",
            (content_hash, account_id, "Seller", "windows", 100 + number,
             100 + number, "#T_ZH", "#T_ZH", f"f{number}", f"f{number}"),
        )
    store.connection.execute("UPDATE riven_seq SET n=? WHERE id=1", (total,))
    return account_id


def test_tracking_parser_supports_pages_and_full_export():
    assert parse_tracking_request("Seller 页 2").page == 2
    assert parse_tracking_request("Seller 全部").export_all is True
    riven = parse_tracking_request("紫卡 #1234 page 3")
    assert (riven.kind, riven.target, riven.page) == ("riven", 1234, 3)
    independent = parse_tracking_request("#1234 page 3", kind="riven")
    assert (independent.kind, independent.target, independent.page) == (
        "riven", 1234, 3)
    assert parse_tracking_request("紫卡 #1234", kind="player").kind == "player"
    assert parse_tracking_request("紫卡 #x") is None
    assert parse_tracking_request("#x", kind="riven") is None
    assert parse_tracking_request("Seller 页 0") is None


def test_player_rivens_are_paged_by_25_and_full_query_works_within_limit(tmp_path):
    with TrackingStore(tmp_path / "tracking.db") as store:
        account_id = _seed_distinct_rivens(store)
        first = store.player_report(account_id, page=1)
        second = store.player_report(account_id, page=2)
        oversized = store.player_report(account_id, page=10**30)
        exported = store.player_report(account_id, all_results=True)

    assert len(first["rivens"]) == 25
    assert len(second["rivens"]) == 5
    assert first["pagination"]["total"] == 30
    assert second["pagination"]["start"] == 26
    assert oversized["pagination"]["out_of_range"] is True
    assert oversized["pagination"]["start"] == 0
    assert oversized["rivens"] == []
    assert len(exported["rivens"]) == 30


def test_full_tracking_export_rejects_results_over_resource_limit(
        tmp_path, monkeypatch):
    monkeypatch.setattr(chat_tracking, "TRACKING_EXPORT_LIMIT", 2)
    with TrackingStore(tmp_path / "tracking.db") as store:
        account_id = _seed_distinct_rivens(store, 3)
        with pytest.raises(TrackingExportTooLarge) as caught:
            store.player_report(account_id, all_results=True)

    assert (caught.value.total, caught.value.limit) == (3, 2)


def test_riven_history_page_keeps_previous_holder_without_loading_all(tmp_path):
    with TrackingStore(tmp_path / "tracking.db") as store:
        number = None
        for index in range(30):
            observed = store.ingest(
                _message(
                    timestamp=100 + index,
                    seller_id=f"{index + 1:024x}",
                    nick=f"Seller{index}",
                ),
                dedupe_seconds=0,
            )
            number = observed[0].riven_no
        second = store.riven_report(number, page=2)

    assert second["pagination"]["total"] == 30
    assert len(second["ownerships"]) == 5
    assert second["ownerships"][0]["from_nick"] == "Seller24"
    assert second["ownerships"][0]["to_nick"] == "Seller25"


def test_shared_riven_format_contains_permanent_number_full_stats_mr_and_time(
        tmp_path):
    with TrackingStore(tmp_path / "tracking.db") as store:
        account_id = _seed_distinct_rivens(store, 1)
        card = store.player_report(account_id)["rivens"][0]
        riven = store.riven_report(1)["ownerships"][0]

    summary = format_riven_summary(card, "zh")
    owner = format_ownership(riven, "zh")
    assert summary.startswith("#1 | [")
    assert "CC" in summary and "|" in summary
    assert "MR:" in summary
    assert "1970/1/1" in summary
    assert "%" in summary and "UTC" in summary
    assert "UTC" in owner
    assert "首次记录的持有者: Seller [PC]" in owner
    assert "观测" not in summary + owner
