"""游戏聊天消息中可稳定识别的结构化标记。"""

from __future__ import annotations

import re


# 游戏内紫卡链接：``[OMG-<类别>:<base64>]``。类别恒以 RandomModRare
# 结尾；只把完整链接视为紫卡消息，不根据自然语言里的 riven/紫卡字样猜测。
RIVEN_LINK_RE = re.compile(
    r"\[OMG-(\w+RandomModRare):([A-Za-z0-9+/=]+)\]"
)


def contains_riven_link(text: str) -> bool:
    return RIVEN_LINK_RE.search(text or "") is not None
