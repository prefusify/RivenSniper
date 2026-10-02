"""用固定样例展示捡漏算法，不联网、不发消息、不写数据库。

用法: uv run python scripts/bargain_dry_run.py
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.plugins.riven_sniper import bargain  # noqa: E402


def _pct(value: Decimal) -> str:
    return f"{value * 100:.1f}%"


def main() -> None:
    now = datetime.now(timezone.utc)
    print("当前四项控制台参数:", bargain.params())

    print("\n>>> 普通道具：取 WFM 90days 中目标等级的最新日桶 avg_price")
    statistics = {
        "statistics_closed": {
            "90days": [
                {"id": "old-r10", "datetime": "2026-07-19T00:00:00Z",
                 "mod_rank": 10, "avg_price": "108.25", "volume": 80},
                {"id": "rank-0", "datetime": "2026-07-20T00:00:00Z",
                 "mod_rank": 0, "avg_price": "18.5", "volume": 20},
                {"id": "new-r10", "datetime": "2026-07-20T00:00:00Z",
                 "mod_rank": 10, "avg_price": "100.75", "volume": 95},
            ]
        }
    }
    daily = bargain.latest_item_daily_average(
        statistics, target_rank=10, max_rank=10)
    assert daily is not None
    item_hit = bargain.evaluate_price("70", daily.price, Decimal("0.20"))
    print(f"满级最新日均: {daily.price}p；新订单: 70p")
    print("结果:", (f"命中，低 {_pct(item_hit.discount)}" if item_hit else "未命中"))

    print("\n>>> 紫卡：online + ingame 直售价格排序，排除最低价后取第2~5名")
    prices = ["20", "260", "270", "280", "290"]
    auctions = [
        {
            "id": f"sample-{index}",
            "visible": True,
            "is_direct_sell": True,
            "buyout_price": price,
            "starting_price": "1",
            "owner": {
                "status": "ingame" if index % 2 else "online",
                # 同一卖家的不同挂单也分别参与样本。
                "ingame_name": "SameSeller" if index < 2 else f"Seller{index}",
            },
        }
        for index, price in enumerate(prices)
    ]
    auctions.append({
        "id": "auction-only", "visible": True, "is_direct_sell": False,
        "buyout_price": None, "starting_price": "1",
        "owner": {"status": "ingame", "ingame_name": "AuctionSeller"},
    })
    hour_sample = bargain.build_riven_hour_sample(auctions)
    assert hour_sample is not None
    print(f"小时样本: {hour_sample.price}p（直售单 {hour_sample.order_count} 条，"
          f"样本离散度 {_pct(hour_sample.spread)}）")

    samples = [
        {"sampled_at": (now - timedelta(hours=age)).timestamp(), "price": price}
        for age, price in [(1, "275"), (8, "281.5"), (20, "278.25"), (30, "999")]
    ]
    rolling, count = bargain.rolling_riven_average(
        samples, now=now.timestamp(), hours=24, min_samples=1)
    assert rolling is not None
    print(f"24小时滚动均价: {rolling}p（有效小时样本 {count} 个）")

    fresh = {
        "id": "candidate-new", "created": (now - timedelta(minutes=30)).isoformat(),
        "is_direct_sell": True, "buyout_price": "150",
        "owner": {"status": "offline", "ingame_name": "OfflineSeller"},
    }
    riven_hit = bargain.evaluate_price(
        bargain.direct_buyout_price(fresh), rolling, Decimal("0.35"))
    print("30分钟前创建的 offline 直售单:",
          "可判断" if bargain.is_fresh_riven_listing(fresh, now=now) else "过期")
    print("结果:", (f"命中，低 {_pct(riven_hit.discount)}" if riven_hit else "未命中"))

    old = dict(fresh, id="candidate-old",
               created=(now - timedelta(hours=2)).isoformat())
    print("2小时前创建的直售单:",
          "可判断" if bargain.is_fresh_riven_listing(old, now=now) else "不触发提醒")
    print("※ 生产运行时还会永久记录已提醒的紫卡挂单 ID，避免再次推送。")


if __name__ == "__main__":
    main()
