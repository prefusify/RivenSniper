"""可持久化的 JSONL 完整行游标。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .chat_collector.runtime import atomic_write_json


_ANCHOR_BYTES = 64


@dataclass(frozen=True)
class ReadBatch:
    path: Path
    lines: tuple[bytes, ...]
    offset: int
    signature: str
    anchor: str
    start_offset: int = 0
    line_offsets: tuple[int, ...] = ()


class JsonlCheckpointReader:
    """每个源独立游标；显式 commit 后才跨进程推进。"""

    def __init__(
        self, checkpoint_path: str | Path, *, namespace: str | None = None,
    ):
        self.path = Path(checkpoint_path)
        self.namespace = namespace
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            document = {}
        except OSError as error:
            raise OSError(f"无法读取 JSONL 检查点 {self.path}: {error}") from error
        else:
            try:
                document = json.loads(raw)
            except json.JSONDecodeError as error:
                raise ValueError(f"JSONL 检查点已损坏: {self.path}") from error
            if not isinstance(document, dict):
                raise ValueError(f"JSONL 检查点根节点必须是对象: {self.path}")
        sources = document.get("sources")
        if sources is not None and not isinstance(sources, dict):
            raise ValueError(f"JSONL 检查点 sources 必须是对象: {self.path}")
        namespace_matches = (
            namespace is None or document.get("namespace") == namespace)
        self._sources = (
            sources or {}
            if namespace_matches
            else {}
        )
        self._initialized = (
            (bool(document.get("initialized")) or bool(self._sources))
            if namespace is None
            else bool(document.get("initialized")) and namespace_matches
        )
        self._pending_initializations: dict[str, ReadBatch] = {}
        self._resolved_paths: dict[Path, str] = {}

    @property
    def has_sources(self) -> bool:
        return bool(self._sources)

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    def has_source(self, path: str | Path) -> bool:
        return self.path_key(path) in self._sources

    def path_key(self, path: str | Path) -> str:
        """返回与持久化检查点一致的规范化源键。"""
        return self._key(Path(path))

    def tracked_paths(self) -> tuple[Path, ...]:
        paths: list[Path] = []
        for saved in self._sources.values():
            if isinstance(saved, dict) and saved.get("path"):
                paths.append(Path(str(saved["path"])))
        return tuple(paths)

    def is_caught_up(
        self, path: str | Path, *, allow_trailing_partial: bool = False,
    ) -> bool:
        """判断所有完整 JSONL 记录是否已提交。

        活跃文件默认仍要求提交到物理 EOF。归档清理可以显式忽略最后一个
        没有换行的残缺记录；这种尾部通常来自写入进程异常退出，旧日期文件
        不会再被追加修复。
        """
        source = Path(path)
        saved = self._sources.get(self._key(source))
        if not source.is_file():
            return False
        try:
            expected = (
                self._last_complete_offset(source)
                if allow_trailing_partial else source.stat().st_size
            )
            if not isinstance(saved, dict):
                return bool(
                    allow_trailing_partial
                    and self._initialized
                    and expected == 0
                )
            offset = int(saved.get("offset", 0))
            if saved.get("signature") != self._signature(source):
                return False
            if offset != expected:
                return False
            return not offset or saved.get("anchor", "") == self._anchor(source, offset)
        except (OSError, TypeError, ValueError):
            return False

    def _resolved_path(self, path: Path) -> str:
        resolved = self._resolved_paths.get(path)
        if resolved is None:
            resolved = str(path.resolve())
            self._resolved_paths[path] = resolved
        return resolved

    def _key(self, path: Path) -> str:
        return self._resolved_path(path).casefold()

    @staticmethod
    def _signature(path: Path) -> str:
        stat = path.stat()
        return f"{stat.st_dev}:{stat.st_ino}"

    @staticmethod
    def _anchor(path: Path, offset: int) -> str:
        if offset <= 0:
            return ""
        length = min(_ANCHOR_BYTES, offset)
        with path.open("rb") as stream:
            stream.seek(offset - length)
            return stream.read(length).hex()

    @staticmethod
    def _last_complete_offset(path: Path) -> int:
        size = path.stat().st_size
        if size <= 0:
            return 0
        with path.open("rb") as stream:
            stream.seek(size - 1)
            if stream.read(1) == b"\n":
                return size
            position = size
            while position > 0:
                start = max(0, position - 8192)
                stream.seek(start)
                chunk = stream.read(position - start)
                newline = chunk.rfind(b"\n")
                if newline >= 0:
                    return start + newline + 1
                position = start
        return 0

    def initialize_at_end(self, path: str | Path) -> None:
        source = Path(path)
        key = self._key(source)
        if key in self._sources or not source.is_file():
            return
        batch = self._pending_initializations.get(key)
        if batch is None:
            offset = self._last_complete_offset(source)
            batch = ReadBatch(
                source,
                (),
                offset,
                self._signature(source),
                self._anchor(source, offset),
                start_offset=offset,
            )
        try:
            self.commit(batch)
        except OSError:
            # 保留首次观察到的边界；重试时不能跳到更晚的 EOF 而漏掉新记录。
            self._pending_initializations[key] = batch
            raise
        self._pending_initializations.pop(key, None)

    def read_complete(
        self, path: str | Path, *, max_bytes: int | None = None,
    ) -> ReadBatch:
        source = Path(path)
        if max_bytes is not None and max_bytes <= 0:
            raise ValueError("JSONL 单批读取上限必须大于 0")
        key = self._key(source)
        saved = self._sources.get(key)
        offset = int(saved.get("offset", 0)) if isinstance(saved, dict) else 0
        saved_signature = saved.get("signature") if isinstance(saved, dict) else None
        saved_anchor = saved.get("anchor", "") if isinstance(saved, dict) else ""
        anchor_before = b""
        with source.open("rb") as stream:
            stat = os.fstat(stream.fileno())
            signature = f"{stat.st_dev}:{stat.st_ino}"
            if saved_signature != signature or offset > stat.st_size:
                offset = 0
            elif offset:
                length = min(_ANCHOR_BYTES, offset)
                stream.seek(offset - length)
                anchor_before = stream.read(length)
                if saved_anchor != anchor_before.hex():
                    # 同一路径被截断后迅速写得比旧 offset 更长时，尺寸不足以识别轮转。
                    offset = 0
                    anchor_before = b""
            stream.seek(offset)
            chunk = stream.read(max_bytes)
        last_newline = chunk.rfind(b"\n")
        if last_newline < 0:
            return ReadBatch(
                source, (), offset, signature, anchor_before.hex(),
                start_offset=offset,
            )
        committed = offset + last_newline + 1
        complete = chunk[: last_newline + 1]
        raw_lines = complete.splitlines(keepends=True)
        lines = tuple(line.rstrip(b"\r\n") for line in raw_lines)
        position = offset
        line_offsets = []
        for raw_line in raw_lines:
            position += len(raw_line)
            line_offsets.append(position)
        committed_anchor = (
            complete[-_ANCHOR_BYTES:]
            if len(complete) >= _ANCHOR_BYTES
            else (anchor_before + complete)[-_ANCHOR_BYTES:]
        )
        return ReadBatch(
            source, lines, committed, signature, committed_anchor.hex(),
            start_offset=offset, line_offsets=tuple(line_offsets),
        )

    def prefix(self, batch: ReadBatch, line_count: int) -> ReadBatch:
        """构造只提交批次前若干完整行的检查点。"""
        count = int(line_count)
        if count < 0 or count > len(batch.lines):
            raise ValueError("JSONL 批次前缀行数超出范围")
        if count == len(batch.lines):
            return batch
        offset = (
            batch.start_offset if count == 0
            else batch.line_offsets[count - 1]
        )
        return ReadBatch(
            batch.path,
            batch.lines[:count],
            offset,
            batch.signature,
            self._anchor(batch.path, offset),
            start_offset=batch.start_offset,
            line_offsets=batch.line_offsets[:count],
        )

    def commit(self, batch: ReadBatch) -> None:
        updated = dict(self._sources)
        updated[self._key(batch.path)] = {
            "path": self._resolved_path(batch.path),
            "offset": batch.offset,
            "signature": batch.signature,
            "anchor": batch.anchor,
        }
        initialized = self._initialized or (
            self.namespace is None and bool(updated))
        document = {
            "version": 1, "initialized": initialized, "sources": updated,
        }
        if self.namespace is not None:
            document["namespace"] = self.namespace
            document["initialized"] = initialized
        atomic_write_json(self.path, document)
        self._sources = updated
        self._initialized = initialized

    def forget(self, path: str | Path) -> bool:
        """从检查点移除已删除且无需再恢复的源文件。"""
        key = self._key(Path(path))
        if key not in self._sources:
            return False
        updated = dict(self._sources)
        updated.pop(key, None)
        document = {
            "version": 1,
            "initialized": self._initialized,
            "sources": updated,
        }
        if self.namespace is not None:
            document["namespace"] = self.namespace
        atomic_write_json(self.path, document)
        self._sources = updated
        self._pending_initializations.pop(key, None)
        self._resolved_paths.pop(Path(path), None)
        return True

    def mark_initialized(self) -> None:
        """持久化首次启用边界；目录为空时也必须跨重启保留。"""
        if self._initialized:
            return
        document = {
            "version": 1, "initialized": True,
            "sources": dict(self._sources),
        }
        if self.namespace is not None:
            document["namespace"] = self.namespace
            document["initialized"] = True
        atomic_write_json(self.path, document)
        self._initialized = True
