"""埋点与日志缓冲测试。"""

import sys
from pathlib import Path

import nonebot

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.plugins.riven_sniper import stats as stats_module  # noqa: E402
from src.plugins.riven_sniper.stats import LogBuffer, PollerStats  # noqa: E402


def test_poller_stats_flow():
    s = PollerStats()
    s.record_poll(100, 5, 2)
    s.record_poll(90, 3, 1)
    snap = s.snapshot()
    assert snap["polls_total"] == 2
    assert snap["last_poll"] == {"candidates": 90, "new": 3, "hits": 1}
    assert snap["today"]["hits"] == 3
    assert snap["consecutive_failures"] == 0


def test_poller_stats_failures_reset_on_success():
    s = PollerStats()
    s.record_poll_failure()
    s.record_poll_failure()
    assert s.snapshot()["consecutive_failures"] == 2
    s.record_poll(10, 0, 0)
    assert s.snapshot()["consecutive_failures"] == 0


def test_poller_stats_day_rollover():
    s = PollerStats()
    s.record_poll(10, 1, 1)
    s.record_push(True)
    s._day = "2000-01-01"  # 模拟跨天
    snap = s.snapshot()
    assert snap["today"] == {
        "hits": 0,
        "pushes": 0,
        "push_failures": 0,
        "push_items": 0,
        "push_item_failures": 0,
    }


def test_poller_stats_distinguishes_requests_from_batched_items():
    s = PollerStats()
    s.record_push(True, items=4)
    s.record_push(False, items=2)

    assert s.snapshot()["today"] == {
        "hits": 0,
        "pushes": 1,
        "push_failures": 1,
        "push_items": 4,
        "push_item_failures": 2,
    }


def test_delivery_latency_snapshot_is_aggregated_without_target_ids():
    s = PollerStats()
    s.record_delivery_timing(
        "irc", platform="discord", outcome="sent", queue_seconds=0.1,
        api_seconds=0.2, source_seconds=0.3, source_end_seconds=0.5)
    s.record_delivery_timing(
        "irc", platform="qq", outcome="rate_limited_retry", queue_seconds=0.2,
        api_seconds=0.4, source_seconds=0.6, source_end_seconds=1.0)

    latency = s.snapshot()["delivery_latency"]
    irc = latency["by_source"]["irc"]
    assert irc["queue_to_api_start"] == {
        "samples": 2, "p50_ms": 150.0, "p95_ms": 195.0,
        "max_ms": 200.0,
    }
    assert irc["source_to_api_start"]["max_ms"] == 600.0
    assert irc["source_to_api_end"]["max_ms"] == 1000.0
    assert latency["by_platform"]["discord"][
        "discord_or_qq_api"]["max_ms"] == 200.0
    assert latency["by_outcome"]["rate_limited_retry"][
        "discord_or_qq_api"]["max_ms"] == 400.0
    assert "target" not in repr(latency).lower()


def test_delivery_latency_keeps_full_time_window(monkeypatch):
    now = 1_000.0
    monkeypatch.setattr(stats_module.time, "time", lambda: now)
    s = PollerStats()
    for _ in range(3000):
        s.record_delivery_timing(
            "irc", platform="discord", outcome="sent", queue_seconds=0.1,
            api_seconds=0.2)
    assert s.snapshot()["delivery_latency"]["all"][
        "queue_to_api_start"]["samples"] == 3000

    now += 1801
    assert s.snapshot()["delivery_latency"]["all"][
        "queue_to_api_start"] == {"samples": 0}


class _Msg:
    """loguru Message 桩。"""

    def __init__(self, level, msg):
        import datetime
        self.record = {
            "time": datetime.datetime.now(),
            "level": type("L", (), {"name": level})(),
            "message": msg,
            "name": "test",
        }


def test_log_buffer_levels_and_limit():
    buf = LogBuffer(maxlen=5)
    for i in range(8):
        buf.sink(_Msg("INFO", f"info{i}"))
    buf.sink(_Msg("WARNING", "warn1"))
    assert len(buf.recent(100)) == 5  # 环形上限
    warns = buf.recent(100, "WARNING")
    assert len(warns) == 1 and warns[0]["msg"] == "warn1"


def test_log_buffer_subscribe():
    buf = LogBuffer()
    q = buf.subscribe()
    buf.sink(_Msg("INFO", "hello"))
    assert q.get_nowait()["msg"] == "hello"
    buf.unsubscribe(q)
    buf.sink(_Msg("INFO", "after"))
    assert q.empty()


def test_install_log_sink_adds_rotated_persistent_bot_log(tmp_path, monkeypatch):
    calls = []

    def add(*args, **kwargs):
        calls.append((args, kwargs))

    log_path = tmp_path / "logs" / "bot.log"
    monkeypatch.setattr(stats_module, "_sink_installed", False)
    monkeypatch.setattr(stats_module, "_RUNTIME_LOG_PATH", log_path)
    monkeypatch.setattr(nonebot.logger, "add", add)

    stats_module.install_log_sink()

    assert log_path.parent.is_dir()
    assert calls[0][0] == (stats_module.LOGS.sink,)
    assert calls[1][0] == (str(log_path),)
    assert calls[1][1]["rotation"] == "20 MB"
    assert calls[1][1]["retention"] == "14 days"
