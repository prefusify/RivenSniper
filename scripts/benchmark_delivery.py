"""可复现的投递容量与 IRC 积压基准。

该脚本只使用临时数据库和模拟平台 API，不读取 ``.env``、不连接外部服务，
也不属于常规 pytest 套件。输出为 JSON，便于在同一机器、同一参数下比较提交。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

SOURCE_ROOT = Path(os.environ.get(
    "RIVEN_BENCHMARK_SOURCE_ROOT",
    Path(__file__).resolve().parents[1],
)).resolve()
sys.path.insert(0, str(SOURCE_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from src.plugins.riven_sniper.chat_tracking import TrackingStore
from src.plugins.riven_sniper.criteria import ANY_ATTRIBUTE
from src.plugins.riven_sniper.delivery import DeliverySource
from src.plugins.riven_sniper.feed_cursor import JsonlCheckpointReader
from src.plugins.riven_sniper.irc_feed import IrcChatFeed
from src.plugins.riven_sniper.poller import SniperPoller
from src.plugins.riven_sniper.store import Store
from nonebot import logger
from nonebot.utils import DataclassEncoder

# 基准结果需要能直接交给 ConvertFrom-Json 等工具；生产日志行为不在这个
# 独立进程中验证，因此移除默认 Loguru sink，只在 stdout 输出最终 JSON。
logger.remove()


def _queued_deliveries(poller) -> tuple:
    snapshot = getattr(poller, "queued_deliveries", None)
    return tuple(snapshot) if snapshot is not None else tuple(poller.queue._queue)


def _percentile(values: list[float], ratio: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * ratio
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


class _FanoutStore:
    def get_target(self, scope_id: int) -> dict:
        return {
            "scope_id": int(scope_id),
            "platform": "qq",
            "external_id": str(scope_id),
            "active": True,
        }


async def _fanout_case(
    *, targets: int, messages_per_target: int, api_delay_ms: float,
) -> dict[str, float | int]:
    config = SimpleNamespace(
        send_queue_maxsize=max(10_000, targets * messages_per_target + 1),
        sniper_send_concurrency=0,
        sniper_send_interval=0.0,
        sniper_dry_run=False,
        trade_message_ttl_seconds=300.0,
        discord_dm_enabled=False,
    )
    poller = SniperPoller(_FanoutStore(), config)
    poller._target_bot_connected = lambda _target: True

    async def render(_target, payload, _rendered=None):
        return payload

    send_starts: list[float] = []
    sent_messages: list[str] = []

    async def send(_target, _message, *, nonce=None):
        del nonce
        send_starts.append(time.perf_counter())
        sent_messages.append(str(_message))
        await asyncio.sleep(api_delay_ms / 1000)

    poller._render_payload_for_target = render
    poller._send_to = send
    worker = asyncio.create_task(
        poller._send_loop(poller.queue, lambda: 0.0)
    )
    started = time.perf_counter()
    for message_index in range(messages_per_target):
        for target_index in range(targets):
            poller.enqueue_delivery(poller.new_delivery(
                DeliverySource.SYSTEM,
                ("group", str(target_index + 1)),
                f"{message_index}:{target_index}",
            ))
    await poller.queue.join()
    elapsed = time.perf_counter() - started
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
    await poller.wfm.close()

    latencies_ms = [(value - started) * 1000 for value in send_starts]
    total = targets * messages_per_target
    unique_messages = len(set(sent_messages))
    return {
        "targets": targets,
        "messages_per_target": messages_per_target,
        "messages": total,
        "api_delay_ms": api_delay_ms,
        "elapsed_ms": round(elapsed * 1000, 3),
        "throughput_messages_per_second": round(total / elapsed, 3),
        "sent_messages": len(sent_messages),
        "unique_messages": unique_messages,
        "missing_messages": total - unique_messages,
        "duplicate_messages": len(sent_messages) - unique_messages,
        "send_start_p50_ms": round(statistics.median(latencies_ms), 3),
        "send_start_p95_ms": round(_percentile(latencies_ms, 0.95), 3),
        "send_start_max_ms": round(max(latencies_ms), 3),
    }


class _FeedPoller:
    def activate_source(self, *_args) -> None:
        pass


def _idle_feed_case(*, cycles: int, repeats: int) -> dict[str, object]:
    """复现 17 槽无新消息时的路径选择与游标读取固定开销。"""
    poll_runs = []
    read_runs = []
    for _ in range(repeats):
        with tempfile.TemporaryDirectory(
                prefix="riven-idle-feed-benchmark-") as raw_root:
            root = Path(raw_root)
            feed_dir = root / "feeds"
            feed_dir.mkdir()
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            paths = []
            for slot in "ABCDEFGHIJKLMNOPQ":
                path = feed_dir / f"privmsg_{day}_{slot}.jsonl"
                path.write_bytes(b"{}\n")
                paths.append(path)
            (root / "collector_control.json").write_text(json.dumps({
                "status": "running",
                "run_id": "benchmark",
                "expected_slots": list("ABCDEFGHIJKLMNOPQ"),
            }), encoding="utf-8")
            reader = JsonlCheckpointReader(root / "cursor.json")
            for path in paths:
                reader.commit(reader.read_complete(path))
            reader.mark_initialized()
            feed = IrcChatFeed(SimpleNamespace(
                irc_feed_dir=str(feed_dir), irc_feed_path="",
            ), _FeedPoller())

            started = time.perf_counter()
            for _ in range(cycles):
                feed._poll_source_paths(reader)
            poll_runs.append((time.perf_counter() - started) * 1000)

            started = time.perf_counter()
            for _ in range(cycles):
                for path in paths:
                    reader.read_complete(path, max_bytes=1024 * 1024)
            read_runs.append((time.perf_counter() - started) * 1000)
    return {
        "sources": 17,
        "cycles": cycles,
        "repeats": repeats,
        "source_selection_median_ms": round(statistics.median(poll_runs), 3),
        "idle_cursor_read_median_ms": round(statistics.median(read_runs), 3),
        "source_selection_runs_ms": [round(value, 3) for value in poll_runs],
        "idle_cursor_read_runs_ms": [round(value, 3) for value in read_runs],
    }


class _TargetFanoutPoller(_FeedPoller):
    def __init__(self, store: Store):
        self.store = store
        self.deliveries = []

    def new_delivery(self, source, target, payload, **metadata):
        return source, target, payload, metadata

    def enqueue_delivery(self, delivery) -> bool:
        self.deliveries.append(delivery)
        return True


class _HeadOfLineFeed(IrcChatFeed):
    """保留生产循环调度，只把磁盘/SQLite 工作替换为确定性耗时。"""

    def __init__(self, history_delay: float, interval: float):
        config = SimpleNamespace(
            irc_feed_dir="",
            irc_feed_path="benchmark.jsonl",
            irc_feed_interval=interval,
            sniper_dry_run=False,
            irc_feed_stale_seconds=180.0,
        )
        super().__init__(config, _FeedPoller())
        self._delivery_needs_initialization = False
        self.history_delay = history_delay
        self.history_started = asyncio.Event()
        self.fresh_available = asyncio.Event()
        self.fresh_detected = asyncio.Event()
        self.fresh_available_at = 0.0
        self.fresh_detected_at = 0.0

    def _ensure_reader(self, _fallback=None):
        return object()

    def _prepare_presence_reader(self):
        return object()

    def _prepare_tracking(self, _fallback=None) -> bool:
        return True

    def _ensure_tracking_reader(self, _fallback=None):
        return object()

    def _source_paths(self):
        return (Path("benchmark.jsonl"),)

    def _poll_source_paths(self, _reader, *, include_all=False):
        del include_all
        return self._source_paths()

    def _poll_presence_paths(self, _reader):
        return ()

    def _sync_control(self) -> bool:
        return True

    def _report_health(self, _health, _paths) -> None:
        pass

    async def _maybe_maintain(self) -> None:
        pass

    async def _tick(self, _path: Path, dry: bool = False) -> None:
        del dry
        if self.fresh_available.is_set() and not self.fresh_detected.is_set():
            self.fresh_detected_at = time.perf_counter()
            self.fresh_detected.set()

    async def _track_tick(self, _path: Path) -> None:
        if not self.history_started.is_set():
            self.history_started.set()
            await asyncio.sleep(self.history_delay)

    async def _presence_ticks(self, *_args, **_kwargs) -> None:
        pass


async def _head_of_line_case(
    *, history_delay_ms: float, interval_ms: float,
) -> dict[str, float]:
    feed = _HeadOfLineFeed(history_delay_ms / 1000, interval_ms / 1000)
    task = asyncio.create_task(feed._loop())
    await asyncio.wait_for(feed.history_started.wait(), timeout=5)
    feed.fresh_available_at = time.perf_counter()
    feed.fresh_available.set()
    await asyncio.wait_for(feed.fresh_detected.wait(), timeout=5)
    latency = feed.fresh_detected_at - feed.fresh_available_at
    feed._stop.set()
    await asyncio.wait_for(task, timeout=5)
    return {
        "simulated_history_batch_ms": history_delay_ms,
        "feed_interval_ms": interval_ms,
        "fresh_detection_latency_ms": round(latency * 1000, 3),
    }


async def _history_case(records: int) -> dict[str, float | int]:
    with tempfile.TemporaryDirectory(prefix="riven-benchmark-") as raw_root:
        root = Path(raw_root)
        feed_path = root / "privmsg_2026-01-01_A.jsonl"
        rows = []
        base_time = 1_700_000_000
        for index in range(records):
            rows.append(json.dumps({
                "t": base_time + index,
                "dir": "in",
                "sender_id": f"{index + 1:024x}",
                "irc_nick": f"player{index}",
                "nick": f"player{index}",
                "platform": "pc",
                "chan": "#T_EN",
                "text": f"WTB ordinary item {index}",
                "event_key": f"benchmark-{index}",
            }, separators=(",", ":")))
        feed_path.write_text("\n".join(rows) + "\n", encoding="utf-8")

        database = root / "track.db"
        config = SimpleNamespace(
            irc_feed_dir=str(root),
            irc_feed_path="",
            irc_track_db_path=str(database),
            discord_dm_enabled=False,
        )
        feed = IrcChatFeed(config, _FeedPoller())
        feed._tracker = TrackingStore(database)
        reader = feed._ensure_tracking_reader(feed_path)
        reader.mark_initialized()
        started = time.perf_counter()
        chunks = 0
        stored = 0
        while stored < records:
            before = stored
            await feed._track_tick(feed_path)
            chunks += 1
            stored = int(feed._tracker.connection.execute(
                "SELECT COUNT(*) FROM players"
            ).fetchone()[0])
            if stored == before:
                break
        elapsed = time.perf_counter() - started
        feed._tracker.close()
        return {
            "records": records,
            "stored_players": stored,
            "read_chunks": chunks,
            "complete": stored == records,
            "elapsed_ms": round(elapsed * 1000, 3),
            "records_per_second": round(stored / elapsed, 3),
        }


async def _wm_batch_case(
    auctions: int, *, render: bool,
) -> dict[str, int | float]:
    with tempfile.TemporaryDirectory(prefix="riven-wm-benchmark-") as raw_root:
        store = Store(Path(raw_root) / "sniper.db")
        target = store.upsert_qq_target(123456, 654321, enabled=True)
        store.add_config(
            target["scope_id"], weapon=None, wildcard="all",
            positives=[
                ["base_damage_/_melee_damage"], ["critical_damage"],
            ],
            negatives=[[ANY_ATTRIBUTE]],
        )
        config = SimpleNamespace(
            sniper_poll_interval=1.0,
            sniper_dry_run=False,
            sniper_send_interval=0.0,
            sniper_send_concurrency=0,
            send_queue_maxsize=max(1000, auctions + 1),
            trade_message_ttl_seconds=300.0,
            send_max_retries=1,
            send_retry_delay_seconds=2.0,
            discord_dm_enabled=False,
        )
        poller = SniperPoller(store, config)
        poller._first_pass = False
        item = {
            "weapon_url_name": "nami_solo",
            "name": "visi-loctitis",
            "type": "riven",
            "mod_rank": 8,
            "re_rolls": 38,
            "mastery_level": 14,
            "polarity": "vazarin",
            "attributes": [
                {"url_name": "base_damage_/_melee_damage",
                 "value": 237.0, "positive": True},
                {"url_name": "critical_damage",
                 "value": 109.4, "positive": True},
                {"url_name": "critical_chance_on_slide_attack",
                 "value": -116.2, "positive": False},
            ],
        }
        recent = [
            {
                "id": f"benchmark-{index}",
                "item": item,
                "owner": {
                    "id": f"owner-{index}",
                    "ingame_name": f"seller{index}",
                    "status": "ingame",
                },
                "starting_price": 100,
                "buyout_price": 150,
                "is_direct_sell": False,
                "closed": False,
                "private": False,
                "visible": True,
                "platform": "pc",
            }
            for index in range(auctions)
        ]

        async def recent_auctions():
            return recent

        poller.wfm.recent_auctions = recent_auctions
        await poller._poll_once()
        counters = poller.delivery_status["counters_by_source"]["wm"]
        result: dict[str, int | float] = {
            "logical_items": int(counters.get("logical_items", 0)),
            "queued_api_actions": poller.queue.qsize(),
            "api_requests_saved": int(counters.get("api_requests_saved", 0)),
        }
        if render:
            started = time.perf_counter()
            sizes = []
            for delivery in _queued_deliveries(poller):
                message = await poller._render_payload(delivery.payload)
                sizes.append(len(json.dumps(
                    {"message": message}, cls=DataclassEncoder,
                    separators=(",", ":"),
                ).encode("utf-8")))
            result.update({
                "render_ms": round((time.perf_counter() - started) * 1000, 3),
                "total_action_json_bytes": sum(sizes),
                "max_action_json_bytes": max(sizes, default=0),
            })
        await poller.wfm.close()
        store.close()
        return result


async def _irc_target_case(targets: int) -> dict[str, float | int]:
    with tempfile.TemporaryDirectory(prefix="riven-target-benchmark-") as raw_root:
        root = Path(raw_root)
        store = Store(root / "sniper.db")
        for index in range(targets):
            target = store.upsert_discord_target(str(10_000 + index))
            store.set_target_channel_enabled(target["scope_id"], True)
            store.add_config(
                target["scope_id"], weapon="vectis", wildcard=None,
                positives=[
                    ["toxin_damage"], ["critical_chance"], ["multishot"],
                ],
                negatives=[["magazine_capacity"]],
            )
        path = root / "privmsg_2026-01-01_A.jsonl"
        path.write_text(json.dumps({
            "t": time.time(),
            "dir": "in",
            "collector_run_id": "legacy",
            "slot": "A",
            "sender_id": "0123456789abcdef01234567",
            "nick": "benchmark-seller",
            "irc_nick": "benchmark-seller",
            "platform": "pc",
            "chan": "#T_EN",
            "text": (
                "[OMG-LotusRifleRandomModRare:"
                "9b51zSsL1EkVt78mCp1r4/fpQ+1jxghk]"
            ),
            "event_key": "benchmark-target-fanout",
        }, separators=(",", ":")) + "\n", encoding="utf-8")
        poller = _TargetFanoutPoller(store)
        config = SimpleNamespace(
            irc_feed_dir="",
            irc_feed_path=str(path),
            irc_feed_checkpoint_path=str(root / "delivery-cursor.json"),
            irc_track_db_path=str(root / "track.db"),
            discord_dm_enabled=True,
        )
        feed = IrcChatFeed(config, poller)
        feed._ensure_tracker(path)
        started = time.perf_counter()
        await feed._tick(path)
        elapsed = time.perf_counter() - started
        for tracker in {
            id(value): value for value in (
                feed._tracker,
                getattr(feed, "_history_tracker", None),
                getattr(feed, "_presence_tracker", None),
            ) if value is not None
        }.values():
            tracker.close()
        store.close()
        return {
            "targets": targets,
            "deliveries": len(poller.deliveries),
            "elapsed_ms": round(elapsed * 1000, 3),
            "targets_per_second": round(targets / elapsed, 3),
        }


async def _wm_match_case(
    *, targets: int, configs_per_target: int, auctions: int,
) -> dict[str, float | int]:
    with tempfile.TemporaryDirectory(prefix="riven-match-benchmark-") as raw_root:
        store = Store(Path(raw_root) / "sniper.db")
        for target_index in range(targets):
            target = store.upsert_qq_target(
                200_000 + target_index, 300_000 + target_index, enabled=True)
            for _ in range(configs_per_target):
                store.add_config(
                    target["scope_id"], weapon=None, wildcard="all",
                    positives=[
                        ["base_damage_/_melee_damage"], ["critical_damage"],
                    ],
                    negatives=[[ANY_ATTRIBUTE]],
                )
        config = SimpleNamespace(
            sniper_poll_interval=1.0,
            sniper_dry_run=False,
            sniper_send_interval=0.0,
            sniper_send_concurrency=0,
            send_queue_maxsize=max(10_000, targets * auctions),
            trade_message_ttl_seconds=300.0,
            send_max_retries=1,
            send_retry_delay_seconds=2.0,
            discord_dm_enabled=False,
        )
        poller = SniperPoller(store, config)
        poller._first_pass = False
        item = {
            "weapon_url_name": "nami_solo",
            "name": "visi-loctitis",
            "type": "riven",
            "mod_rank": 8,
            "re_rolls": 38,
            "mastery_level": 14,
            "polarity": "vazarin",
            "attributes": [
                {"url_name": "base_damage_/_melee_damage",
                 "value": 237.0, "positive": True},
                {"url_name": "critical_damage",
                 "value": 109.4, "positive": True},
                {"url_name": "critical_chance_on_slide_attack",
                 "value": -116.2, "positive": False},
            ],
        }
        recent = [
            {
                "id": f"match-{index}",
                "item": item,
                "owner": {
                    "id": f"owner-{index}",
                    "ingame_name": f"seller{index}",
                    "status": "ingame",
                },
                "starting_price": 100,
                "buyout_price": 150,
                "is_direct_sell": False,
                "closed": False,
                "private": False,
                "visible": True,
                "platform": "pc",
            }
            for index in range(auctions)
        ]

        async def recent_auctions():
            return recent

        poller.wfm.recent_auctions = recent_auctions
        started = time.perf_counter()
        await poller._poll_once()
        elapsed = time.perf_counter() - started
        counters = poller.delivery_status["counters_by_source"]["wm"]
        result = {
            "targets": targets,
            "configs": targets * configs_per_target,
            "auctions": auctions,
            "logical_items": int(counters.get("logical_items", 0)),
            "queued_api_actions": poller.queue.qsize(),
            "elapsed_ms": round(elapsed * 1000, 3),
        }
        await poller.wfm.close()
        store.close()
        return result


async def _run(args: argparse.Namespace) -> dict:
    output: dict[str, object] = {
        "schema": 1,
        "clock": "time.perf_counter",
        "source_root": str(SOURCE_ROOT),
    }
    if args.case in {"all", "head-of-line"}:
        output["irc_head_of_line"] = await _head_of_line_case(
            history_delay_ms=args.history_delay_ms,
            interval_ms=args.interval_ms,
        )
    if args.case in {"all", "feed-idle"}:
        output["irc_idle_feed"] = _idle_feed_case(
            cycles=args.feed_idle_cycles,
            repeats=args.feed_idle_repeats,
        )
    if args.case in {"all", "history"}:
        output["irc_history_ingest"] = await _history_case(args.records)
    if args.case in {"all", "fanout"}:
        output["delivery_fanout"] = [
            await _fanout_case(
                targets=targets,
                messages_per_target=args.messages_per_target,
                api_delay_ms=args.api_delay_ms,
            )
            for targets in args.targets
        ]
    if args.case in {"all", "wm-batch"}:
        output["qq_wm_burst"] = await _wm_batch_case(
            args.wm_burst, render=args.render_wm)
    if args.case in {"all", "irc-targets"}:
        output["irc_target_fanout"] = [
            await _irc_target_case(targets) for targets in args.irc_targets
        ]
    if args.case in {"all", "wm-match"}:
        output["wm_match"] = await _wm_match_case(
            targets=args.wm_targets,
            configs_per_target=args.wm_configs_per_target,
            auctions=args.wm_match_auctions,
        )
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case", choices=(
            "all", "head-of-line", "history", "fanout", "wm-batch",
            "irc-targets", "feed-idle",
            "wm-match",
        ),
        default="all",
    )
    parser.add_argument("--history-delay-ms", type=float, default=500.0)
    parser.add_argument("--interval-ms", type=float, default=20.0)
    parser.add_argument("--feed-idle-cycles", type=int, default=100)
    parser.add_argument("--feed-idle-repeats", type=int, default=7)
    parser.add_argument("--records", type=int, default=2000)
    parser.add_argument("--targets", type=int, nargs="+", default=(16, 64, 128))
    parser.add_argument("--messages-per-target", type=int, default=8)
    parser.add_argument("--api-delay-ms", type=float, default=20.0)
    parser.add_argument("--wm-burst", type=int, default=100)
    parser.add_argument("--render-wm", action="store_true")
    parser.add_argument(
        "--irc-targets", type=int, nargs="+", default=(16, 64, 128))
    parser.add_argument("--wm-targets", type=int, default=16)
    parser.add_argument("--wm-configs-per-target", type=int, default=25)
    parser.add_argument("--wm-match-auctions", type=int, default=100)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
