"""写回 .env 的热更新设置项（保留注释与未知行）。"""

from __future__ import annotations

from pathlib import Path

ENV_PATH = Path(__file__).resolve().parents[3] / ".env"


def update_env_file(updates: dict[str, str], path: Path | None = None):
    """替换已存在的 KEY=... 行；不存在的 KEY 追加到文件末尾。"""
    p = path or ENV_PATH
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    remaining = dict(updates)
    out = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped else None
        if key and not stripped.startswith("#") and key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for k, v in remaining.items():
        out.append(f"{k}={v}")
    p.write_text("\n".join(out) + "\n", encoding="utf-8")
