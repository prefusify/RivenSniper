"""Discord 官方 Bot 私聊入口。

只消费 DirectMessageCreateEvent；服务器频道消息不会进入命令分发。
每个 Discord 用户映射到独立的负数配置作用域，命令实现复用 commands.py。
"""

from __future__ import annotations

from nonebot import logger, on_message
from nonebot.adapters import Event
from nonebot.adapters.discord import Bot, Message, MessageSegment
from nonebot.adapters.discord.event import DirectMessageCreateEvent
from nonebot.rule import Rule

from . import texts
from .commands import (ScopedCommandEvent, TrackingCommandResult,
                       execute_scoped_command, parse_scoped_command)
from .shared import get_config, get_store

DISCORD_MESSAGE_LIMIT = 1900


def split_discord_text(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    """按行优先切分回复，并保证每段不超过 Discord 文本上限。"""
    if not text:
        return ["（空回复）"]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current.rstrip("\n"))
                current = ""
            chunks.append(line[:limit].rstrip("\n"))
            line = line[limit:]
        if current and len(current) + len(line) > limit:
            chunks.append(current.rstrip("\n"))
            current = ""
        current += line
    if current:
        chunks.append(current.rstrip("\n"))
    return chunks or ["（空回复）"]


def adapt_discord_text(text: str) -> str:
    """保留命令名与业务文案，只把 QQ 群语境改成 Discord 私聊语境。"""
    replacements = (
        ("RivenSniper-QQ", "RivenSniper"),
        ("本群", "当前私聊"),
        ("目标所有者", "当前用户"),
    )
    for old, new in replacements:
        text = text.replace(old, new)
    return text


def _is_direct_message(event: Event) -> bool:
    return (get_config().discord_dm_enabled
            and isinstance(event, DirectMessageCreateEvent))


discord_dm = on_message(
    rule=Rule(_is_direct_message), priority=1, block=True)


async def _send_text(bot: Bot, channel_id: int, text: str) -> None:
    for chunk in split_discord_text(text):
        await bot.send_to(channel_id, chunk)


def build_tracking_attachment(result: TrackingCommandResult) -> Message:
    """构建单条 Discord 回复：说明文字与 UTF-8 BOM 文本附件。"""
    filename = result.filename or "rivensniper-riven-history.txt"
    content = (result.export_text or result.text).encode("utf-8-sig")
    return Message([
        MessageSegment.text(adapt_discord_text(result.text) + "\n"),
        MessageSegment.attachment(filename, content=content),
    ])


@discord_dm.handle()
async def _handle_discord_dm(bot: Bot, event: DirectMessageCreateEvent):
    if event.author.bot:
        return

    store = get_store()
    external_id = str(event.author.id)
    target = store.get_target_by_external("discord", external_id)
    if target is None or not target["active"]:
        return
    locale = store.get_target_preferences(target["scope_id"])["locale"]

    parsed = parse_scoped_command(event.get_plaintext())
    if parsed is None:
        await _send_text(
            bot, event.channel_id,
            texts.render(
                "通用.未知命令", locale=locale,
                name=event.get_plaintext().strip() or "?"))
        return

    canonical, args = parsed
    scoped_event = ScopedCommandEvent(
        group_id=target["scope_id"],
        user_id=int(event.author.id),
        platform="discord",
        external_user_id=external_id,
    )
    response = await execute_scoped_command(
        canonical, args, scoped_event, rich=True)
    if response is None:
        return
    if isinstance(response, TrackingCommandResult) and response.export_text is not None:
        await bot.send_to(event.channel_id, build_tracking_attachment(response))
    else:
        text = response.text if isinstance(response, TrackingCommandResult) else response
        await _send_text(bot, event.channel_id, adapt_discord_text(text))
    logger.info("Discord 私聊命令完成: user={} command={}",
                external_id, canonical)
