"""官方 Discord Bot 目标与 QQ 群/私聊寻址。"""

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.plugins.riven_sniper.poller as poller_mod  # noqa: E402
from src.plugins.riven_sniper.poller import SniperPoller  # noqa: E402


class _FakeBot:
    def __init__(self):
        self.calls = []
        self.adapter = type(
            "OneBotAdapter", (), {"get_name": staticmethod(lambda: "OneBot V11")}
        )()

    async def send_group_msg(self, group_id, message):
        self.calls.append(("group", group_id, message))

    async def send_private_msg(self, user_id, message):
        self.calls.append(("private", user_id, message))


async def test_send_to_routes_by_target(monkeypatch):
    bot = _FakeBot()
    poller = types.SimpleNamespace()
    monkeypatch.setattr(
        poller_mod.nonebot, "get_bots", lambda: {"1": bot}, raising=False)
    await SniperPoller._send_to(poller, ("private", 123), "私聊")
    await SniperPoller._send_to(poller, ("group", 456), "群组元")
    await SniperPoller._send_to(poller, 789, "裸群号")
    assert bot.calls == [
        ("private", 123, "私聊"),
        ("group", 456, "群组元"),
        ("group", 789, "裸群号"),
    ]


async def test_send_to_allows_concurrent_snowluma_http_api_calls(monkeypatch):
    class BlockingBot(_FakeBot):
        def __init__(self):
            super().__init__()
            self.active = 0
            self.peak = 0

        async def send_group_msg(self, group_id, message):
            self.active += 1
            self.peak = max(self.peak, self.active)
            await asyncio.sleep(0)
            self.calls.append(("group", group_id, message))
            self.active -= 1

    bot = BlockingBot()
    poller = types.SimpleNamespace()
    monkeypatch.setattr(
        poller_mod.nonebot, "get_bots", lambda: {"1": bot}, raising=False)

    await asyncio.gather(
        SniperPoller._send_to(poller, 123, "第一条"),
        SniperPoller._send_to(poller, 456, "第二条"),
        SniperPoller._send_to(poller, 789, "第三条"),
    )

    assert bot.peak == 3
    assert bot.calls == [
        ("group", 123, "第一条"),
        ("group", 456, "第二条"),
        ("group", 789, "第三条"),
    ]


def test_describe_target():
    assert SniperPoller._describe_target(("private", 5)) == "好友5"
    assert SniperPoller._describe_target(("group", 6)) == "群6"
    assert SniperPoller._describe_target(7) == "群7"
