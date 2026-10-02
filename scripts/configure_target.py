"""供 Windows 启动器初始化统一 Bot 目标。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.plugins.riven_sniper.store import Store


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("has-qq")
    add = subparsers.add_parser("upsert-qq")
    add.add_argument("group_id", type=int)
    add.add_argument("owner_qq", type=int)
    args = parser.parse_args()

    store = Store()
    try:
        if args.command == "has-qq":
            return 0 if store.list_targets("qq") else 1
        store.upsert_qq_target(args.group_id, args.owner_qq, enabled=True)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
