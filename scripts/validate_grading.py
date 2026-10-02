"""评分公式实证：拉取 WFM 最新真实听单，统计反解 roll 的在界率。

新听单（当前倾向下创建）应该几乎全部在 [0,1] 内；老听单会因倾向调整出界。
用法: uv run python scripts/validate_grading.py
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.plugins.riven_sniper.grading import grade_auction_item  # noqa: E402
from src.plugins.riven_sniper.formatter import format_hit  # noqa: E402
from src.plugins.riven_sniper.criteria import ANY_ATTRIBUTE  # noqa: E402
from src.plugins.riven_sniper.version import VERSION  # noqa: E402

HEADERS = {
    "User-Agent": f"rivensniper/{VERSION} (validation script)",
    "Platform": "pc",
}


def main():
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    r = httpx.get("https://api.warframe.market/v1/auctions", headers=HEADERS, timeout=30)
    r.raise_for_status()
    auctions = [a for a in r.json()["payload"]["auctions"]
                if a["item"]["type"] == "riven"]
    recent_cutoff = datetime.now(timezone.utc) - timedelta(days=2)

    total = in_range = out_range = nodata = 0
    fitted_items = variant_fits = rank_fits = 0
    for a in auctions:
        result = grade_auction_item(a["item"])
        if result.fitted:
            fitted_items += 1
            if result.variant:
                variant_fits += 1
            if result.assumed_rank is not None:
                rank_fits += 1
        for g in result.stats:
            total += 1
            if g.grade == "?":
                nodata += 1
            elif g.grade == "X":
                out_range += 1
            else:
                in_range += 1

    print(f"听单数: {len(auctions)}  词条总数: {total}")
    print(f"在界(可评分): {in_range}  出界(X): {out_range}  无数据(?): {nodata}")
    if total:
        print(f"词条在界率: {in_range/total:.1%}")
    print(f"整卡拟合成功: {fitted_items}/{len(auctions)}"
          f"（其中按变体倾向 {variant_fits}，按非标注等级(0级/满级) {rank_fits}）")

    # 打样一条完整推送消息
    if auctions:
        sample_cfg = dict(
            id=0, group_id=0, enabled=1, weapon=None, wildcard="all",
            positives=[[ANY_ATTRIBUTE], [ANY_ATTRIBUTE]], negatives=[],
            zero_rerolls=False,
        )
        print("\n===== 推送消息样例 =====")
        print(format_hit(sample_cfg, auctions[0]))


if __name__ == "__main__":
    main()
