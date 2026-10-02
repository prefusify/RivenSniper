import asyncio
import sys
from pathlib import Path

from nonebot.adapters.onebot.v11 import Message, MessageSegment

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.dry_run import message_preview, render_queued_messages  # noqa: E402
from src.plugins.riven_sniper.delivery import (  # noqa: E402
    DeliveryItem,
    DeliverySource,
)


class _FakePoller:
    def __init__(self):
        self.queue = asyncio.Queue()

    async def _render_payload_for_target(self, target, payload):
        return Message([
            MessageSegment.text(f"{target}:{payload}"),
            MessageSegment.image("base64://aW1hZ2U="),
        ])


async def test_dry_run_consumes_delivery_items_and_renders_target_payloads():
    poller = _FakePoller()
    item = DeliveryItem(
        source=DeliverySource.WM,
        target=123,
        payload="hit",
        attempts=0,
        enqueued_at=1.0,
        expires_at=2.0,
    )
    poller.queue.put_nowait(item)

    rendered = await render_queued_messages(poller)

    assert rendered[0][0] is item
    assert message_preview(rendered[0][1]) == "123:hit[image]"
    assert poller.queue.empty()
    await asyncio.wait_for(poller.queue.join(), timeout=1)


def test_dry_run_previews_single_message_segment():
    assert message_preview(MessageSegment.text("单段消息")) == "单段消息"


def test_dry_run_redacts_inline_base64_image():
    assert message_preview("前文base64://aW1hZ2U=后文") == "前文[image]后文"
