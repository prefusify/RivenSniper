"""市场道具别名数据与解析完整性。"""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.plugins.riven_sniper import marketdata  # noqa: E402


def test_market_item_aliases_resolve_uniquely():
    payload = json.loads(
        (ROOT / "data" / "market_item_aliases.json").read_text(
            encoding="utf-8"))
    aliases = payload["aliases"]

    assert aliases
    assert set(aliases) <= set(marketdata.items())
    for slug, names in aliases.items():
        assert names
        assert len(names) == len({marketdata._norm(name) for name in names})
        for name in names:
            assert marketdata.resolve_item(name) == [slug]
