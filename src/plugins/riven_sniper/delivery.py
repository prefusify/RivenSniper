"""统一的内存消息投递模型。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DeliverySource(StrEnum):
    WM = "wm"
    BARGAIN = "bargain"
    IRC = "irc"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class DeliveryItem:
    """一条等待发送的消息；生命周期仅限当前 Bot 进程。"""

    source: DeliverySource
    target: object
    payload: object
    attempts: int
    enqueued_at: float
    expires_at: float
    generation: str | None = None
    observed_at: float | None = None

    def expired(self, now: float) -> bool:
        return now >= self.expires_at
