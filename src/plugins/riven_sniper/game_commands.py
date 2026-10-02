"""生成可直接复制到 Warframe 聊天框的快捷指令。"""

from __future__ import annotations


def seller_invite_command(seller: str) -> str:
    """返回邀请卖家的游戏内快捷指令。"""
    return f'/inv "{seller}"'


def seller_shortcut_commands(seller: str) -> tuple[str, str, str]:
    """返回联系、邀请和加入卖家的三条独立快捷指令。"""
    return (
        f'/w "{seller}" hi',
        seller_invite_command(seller),
        f'/join "{seller}"',
    )


def seller_shortcuts(seller: str) -> str:
    """返回适用于纯文本消息的三行快捷指令。"""
    return "\n".join(seller_shortcut_commands(seller))


__all__ = [
    "seller_invite_command", "seller_shortcut_commands", "seller_shortcuts",
]
