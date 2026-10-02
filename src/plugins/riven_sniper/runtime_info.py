"""进程启动时记录的代码身份，供启动器识别仍在运行的旧构建。"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from .chat_collector.runtime import atomic_write_json, pid_identity


REPO_ROOT = Path(__file__).resolve().parents[3]
BOT_STATE_PATH = REPO_ROOT / ".runtime" / "bot_state.json"


def latest_source_mtime() -> float:
    candidates = [REPO_ROOT / "bot.py", REPO_ROOT / "pyproject.toml"]
    for root in (REPO_ROOT / "src", REPO_ROOT / "scripts"):
        if root.is_dir():
            candidates.extend(
                path for path in root.rglob("*")
                if path.is_file() and "__pycache__" not in path.parts
            )
    mtimes = []
    for path in candidates:
        try:
            mtimes.append(path.stat().st_mtime)
        except OSError:
            pass
    return max(mtimes, default=0.0)


def git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return result.stdout.strip() if result.returncode == 0 else "unknown"


@lru_cache(maxsize=1)
def runtime_identity() -> dict[str, object]:
    source_mtime = latest_source_mtime()
    return {
        "pid": os.getpid(),
        "process_identity": pid_identity(os.getpid()),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit(),
        "source_mtime": source_mtime,
        "source_mtime_utc": (
            datetime.fromtimestamp(source_mtime, timezone.utc).isoformat()
            if source_mtime else None
        ),
    }


def write_bot_state(status: str, identity: dict[str, object]) -> None:
    atomic_write_json(BOT_STATE_PATH, {
        **identity,
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
