"""端到端 dry-run：不连 QQ，真实轮询 WFM 并把渲染结果打印到控制台。

流程：第一轮标记现存听单 -> 人为把最新5条从去重表删掉 -> 第二轮把它们当新听单
跑完整的 匹配->黑名单->评分->统一队列->按目标格式化 链路，但不调用外部发送 API。

用法: uv run python scripts/dry_run.py
"""

import asyncio
import re
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.plugins.riven_sniper.criteria import ANY_ATTRIBUTE  # noqa: E402
from src.plugins.riven_sniper.delivery import DeliveryItem  # noqa: E402
from src.plugins.riven_sniper.poller import SniperPoller  # noqa: E402
from src.plugins.riven_sniper.store import Store  # noqa: E402

DB = Path(__file__).resolve().parent / "dryrun.db"
GROUP = 123456789


def message_preview(message) -> str:
    """隐藏图片载荷，只展示 dry-run 需要核对的消息结构与文字。"""
    if isinstance(message, str):
        return re.sub(r"base64://[A-Za-z0-9+/=]+", "[image]", message)
    parts: list[str] = []
    segments = (message,) if hasattr(message, "type") else message
    for segment in segments:
        if segment.type == "text":
            parts.append(str(segment.data.get("text") or ""))
        elif segment.type in {"image", "attachment"}:
            parts.append(f"[{segment.type}]")
        else:
            parts.append(f"[{segment.type}]")
    return "".join(parts)


async def render_queued_messages(
    poller: SniperPoller,
) -> list[tuple[DeliveryItem, object]]:
    """消费统一队列，并执行正式发送前使用的目标级延迟渲染。"""
    rendered: list[tuple[DeliveryItem, object]] = []
    while not poller.queue.empty():
        item = poller.queue.get_nowait()
        try:
            if not isinstance(item, DeliveryItem):
                raise TypeError(f"未知队列项：{type(item).__name__}")
            message = await poller._render_payload_for_target(
                item.target, item.payload)
            rendered.append((item, message))
        finally:
            poller.queue.task_done()
    return rendered


async def main():
    sys.stdout.reconfigure(encoding="utf-8")
    if DB.exists():
        DB.unlink()
    store = Store(DB)
    poller = None
    try:
        store.upsert_qq_target(GROUP, 10000, enabled=True)
        # 四种合法形态覆盖 2/3 个正词条与无负/有负组合。
        for positive_count in (2, 3):
            for negatives in ([], [[ANY_ATTRIBUTE]]):
                store.add_config(
                    GROUP, weapon=None, wildcard="all",
                    positives=[[ANY_ATTRIBUTE] for _ in range(positive_count)],
                    negatives=negatives,
                )
        # 一条带条件的配置：任意步枪，必须有暴击并且总共恰好两个正词条。
        store.add_config(GROUP, weapon=None, wildcard="rifle",
                         positives=[["critical_chance"], [ANY_ATTRIBUTE]],
                         negatives=[])
        # 黑名单演示：这个名字的卖家会被跳过
        store.add_blacklist(GROUP, "SomeBlacklistedSeller")

        # 不启动发送循环，因此不会调用平台 API；这里必须关闭轮询器自身的
        # dry_run 文本捷径，才能把可渲染 payload 放入统一队列。
        config = types.SimpleNamespace(
            sniper_poll_interval=15.0, sniper_dry_run=False,
            sniper_max_configs_per_group=20, sniper_send_interval=0.2)

        poller = SniperPoller(store, config)

        print(">>> 第一轮轮询（只标记现存听单）")
        await poller._poll_once()

        # 把最新 5 条从去重表删除，模拟它们是新出现的
        ids = [r["id"] for r in store._conn.execute(
            "SELECT id FROM seen_auctions LIMIT 5").fetchall()]
        placeholders = ",".join("?" for _ in ids)
        if ids:
            store._conn.execute(
                f"DELETE FROM seen_auctions WHERE id IN ({placeholders})", ids)
            store._conn.commit()
        print(f">>> 已把 {len(ids)} 条听单重置为未见")

        print(">>> 第二轮轮询（应产生命中推送）")
        await poller._poll_once()

        queued = poller.queue.qsize()
        rendered = await render_queued_messages(poller)
        print(f">>> 推送队列: {queued} 条消息，成功渲染 {len(rendered)} 条")
        for index, (item, message) in enumerate(rendered, start=1):
            print(f"\n----- 消息 {index} -> 群{item.target} -----")
            print(message_preview(message))
    finally:
        if poller is not None:
            await poller.wfm.close()
        store.close()
        DB.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
