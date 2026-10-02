"""分槽 JSONL 读取只提交完整记录，并可跨进程恢复。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.plugins.riven_sniper import feed_cursor as feed_cursor_module
from src.plugins.riven_sniper.feed_cursor import JsonlCheckpointReader
from src.plugins.riven_sniper.chat_collector.runtime import append_jsonl


def test_partial_jsonl_line_is_retried_after_restart(tmp_path):
    feed = tmp_path / "privmsg_2026-07-26_A.jsonl"
    checkpoint = tmp_path / "cursor.json"
    complete = b'{"text":"one"}\n'
    partial = b'{"text":"two"'
    feed.write_bytes(complete + partial)

    reader = JsonlCheckpointReader(checkpoint)
    batch = reader.read_complete(feed)
    assert batch.lines == (complete.rstrip(b"\n"),)
    reader.commit(batch)

    feed.write_bytes(complete + partial + b'}\n')
    restarted = JsonlCheckpointReader(checkpoint)
    batch = restarted.read_complete(feed)
    assert batch.lines == (b'{"text":"two"}',)
    restarted.commit(batch)

    assert JsonlCheckpointReader(checkpoint).read_complete(feed).lines == ()


def test_consumed_archive_can_ignore_a_terminal_partial_record(tmp_path):
    feed = tmp_path / "presence_2026-07-26_A.jsonl"
    checkpoint = tmp_path / "cursor.json"
    feed.write_bytes(b'{"complete":1}\n{"torn":')
    reader = JsonlCheckpointReader(checkpoint)
    batch = reader.read_complete(feed)
    reader.commit(batch)
    reader.mark_initialized()

    assert reader.is_caught_up(feed) is False
    assert reader.is_caught_up(
        feed, allow_trailing_partial=True,
    ) is True


def test_empty_terminal_partial_archive_is_consumed_after_initialization(tmp_path):
    feed = tmp_path / "presence_2026-07-26_A.jsonl"
    feed.write_bytes(b'{"torn":')
    reader = JsonlCheckpointReader(tmp_path / "cursor.json")
    reader.mark_initialized()

    assert reader.is_caught_up(
        feed, allow_trailing_partial=True,
    ) is True


def test_rotation_resets_only_the_rotated_source(tmp_path):
    checkpoint = tmp_path / "cursor.json"
    first = tmp_path / "A.jsonl"
    second = tmp_path / "B.jsonl"
    first.write_bytes(b"a\n")
    second.write_bytes(b"b\n")
    reader = JsonlCheckpointReader(checkpoint)
    for path in (first, second):
        batch = reader.read_complete(path)
        reader.commit(batch)

    first.write_bytes(b"new-a\n")
    reader = JsonlCheckpointReader(checkpoint)
    assert reader.read_complete(first).lines == (b"new-a",)
    assert reader.read_complete(second).lines == ()


def test_same_size_rewrite_is_detected_by_anchor(tmp_path):
    checkpoint = tmp_path / "cursor.json"
    feed = tmp_path / "A.jsonl"
    feed.write_bytes(b"old\n")
    reader = JsonlCheckpointReader(checkpoint)
    reader.commit(reader.read_complete(feed))

    feed.write_bytes(b"new\n")

    assert reader.read_complete(feed).lines == (b"new",)


def test_initialize_at_end_anchors_before_a_torn_tail(tmp_path):
    feed = tmp_path / "privmsg_2026-07-26_A.jsonl"
    checkpoint = tmp_path / "cursor.json"
    feed.write_bytes(b'{"old":1}\n{"torn":')

    reader = JsonlCheckpointReader(checkpoint)
    reader.initialize_at_end(feed)
    append_jsonl(feed, {"new": 2})

    batch = JsonlCheckpointReader(checkpoint).read_complete(feed)
    assert batch.lines == (b'{"new":2}',)


def test_corrupt_checkpoint_fails_closed_instead_of_skipping_unread_data(tmp_path):
    feed = tmp_path / "privmsg_2026-07-26_A.jsonl"
    checkpoint = tmp_path / "cursor.json"
    feed.write_bytes(b'{"unread":1}\n')
    checkpoint.write_text('{"version":1,"sources":', encoding="utf-8")

    with pytest.raises(ValueError, match="检查点已损坏"):
        JsonlCheckpointReader(checkpoint)

    assert feed.read_bytes() == b'{"unread":1}\n'


def test_failed_commit_does_not_advance_only_the_in_memory_cursor(
        tmp_path, monkeypatch):
    feed = tmp_path / "privmsg_2026-07-26_A.jsonl"
    checkpoint = tmp_path / "cursor.json"
    feed.write_bytes(b'{"one":1}\n')
    reader = JsonlCheckpointReader(checkpoint)
    batch = reader.read_complete(feed)
    write_checkpoint = feed_cursor_module.atomic_write_json

    monkeypatch.setattr(
        feed_cursor_module,
        "atomic_write_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PermissionError("checkpoint locked")
        ),
    )
    with pytest.raises(PermissionError):
        reader.commit(batch)
    assert reader.read_complete(feed).lines == (b'{"one":1}',)

    monkeypatch.setattr(feed_cursor_module, "atomic_write_json", write_checkpoint)
    reader.commit(batch)
    assert reader.read_complete(feed).lines == ()


def test_failed_initial_checkpoint_retries_the_original_eof_boundary(
        tmp_path, monkeypatch):
    feed = tmp_path / "privmsg_2026-07-26_A.jsonl"
    checkpoint = tmp_path / "cursor.json"
    feed.write_bytes(b'{"history":1}\n')
    reader = JsonlCheckpointReader(checkpoint)
    write_checkpoint = feed_cursor_module.atomic_write_json
    monkeypatch.setattr(
        feed_cursor_module,
        "atomic_write_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PermissionError("checkpoint locked")
        ),
    )

    with pytest.raises(PermissionError):
        reader.initialize_at_end(feed)
    append_jsonl(feed, {"after_activation": 2})

    monkeypatch.setattr(feed_cursor_module, "atomic_write_json", write_checkpoint)
    reader.initialize_at_end(feed)
    batch = JsonlCheckpointReader(checkpoint).read_complete(feed)
    assert batch.lines == (b'{"after_activation":2}',)


def test_namespace_discards_legacy_offsets_then_resumes_new_events(tmp_path):
    feed = tmp_path / "privmsg_2026-07-26_A.jsonl"
    checkpoint = tmp_path / "cursor.json"
    feed.write_bytes(b'{"legacy":1}\n')
    legacy = JsonlCheckpointReader(checkpoint)
    legacy.commit(legacy.read_complete(feed))

    current = JsonlCheckpointReader(checkpoint, namespace="current-pipeline")
    assert current.has_sources is False
    assert current.is_initialized is False
    current.initialize_at_end(feed)
    current.mark_initialized()
    append_jsonl(feed, {"current": 2})

    restarted = JsonlCheckpointReader(
        checkpoint, namespace="current-pipeline")
    assert restarted.is_initialized is True
    assert restarted.read_complete(feed).lines == (b'{"current":2}',)


def test_empty_activation_marker_survives_restart(tmp_path):
    checkpoint = tmp_path / "cursor.json"
    reader = JsonlCheckpointReader(checkpoint, namespace="current-pipeline")
    reader.mark_initialized()

    restarted = JsonlCheckpointReader(
        checkpoint, namespace="current-pipeline")
    assert restarted.is_initialized is True
    assert restarted.has_sources is False


def test_bounded_reads_advance_only_across_complete_lines(tmp_path):
    feed = tmp_path / "presence_2026-08-05_A.jsonl"
    checkpoint = tmp_path / "cursor.json"
    feed.write_bytes(b"a\nbb\nccc\n")
    reader = JsonlCheckpointReader(checkpoint)

    first = reader.read_complete(feed, max_bytes=5)
    assert first.lines == (b"a", b"bb")
    reader.commit(first)
    second = reader.read_complete(feed, max_bytes=5)
    assert second.lines == (b"ccc",)
    reader.commit(second)

    assert reader.is_caught_up(feed)


def test_batch_prefix_commits_only_processed_lines(tmp_path):
    feed = tmp_path / "presence_2026-08-05_A.jsonl"
    checkpoint = tmp_path / "cursor.json"
    feed.write_bytes(b"one\ntwo\nthree\n")
    reader = JsonlCheckpointReader(checkpoint)
    batch = reader.read_complete(feed)

    reader.commit(reader.prefix(batch, 2))

    assert reader.read_complete(feed).lines == (b"three",)


def test_forget_removes_deleted_source_without_losing_initialized_marker(
        tmp_path):
    feed = tmp_path / "presence_2026-08-05_A.jsonl"
    checkpoint = tmp_path / "cursor.json"
    feed.write_bytes(b"event\n")
    reader = JsonlCheckpointReader(checkpoint, namespace="presence")
    reader.initialize_at_end(feed)
    reader.mark_initialized()

    feed.unlink()
    assert reader.forget(feed) is True
    assert reader.forget(feed) is False
    restarted = JsonlCheckpointReader(checkpoint, namespace="presence")

    assert restarted.is_initialized is True
    assert restarted.has_source(feed) is False
