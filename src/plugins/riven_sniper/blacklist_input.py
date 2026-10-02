"""卖家黑名单入口的多行输入解析与校验。"""

from __future__ import annotations

from .platform_identity import normalize_player_nick
from .privacy import contains_hidden_identifier


BLACKLIST_NICK_MAX_LENGTH = 32


class BlacklistInputError(ValueError):
    """黑名单入口包含空值、内部标识或超长昵称。"""

    def __init__(self, reason: str, line_number: int = 0):
        super().__init__(reason)
        self.reason = reason
        self.line_number = line_number


def parse_blacklist_names(value: object) -> list[str]:
    """按非空行解析昵称，保留每个昵称内部的普通空格。"""
    names: list[str] = []
    for line_number, line in enumerate(str(value or "").splitlines(), 1):
        name = normalize_player_nick(line)
        if not name:
            continue
        if contains_hidden_identifier(name):
            raise BlacklistInputError("invalid", line_number)
        if len(name) > BLACKLIST_NICK_MAX_LENGTH:
            raise BlacklistInputError("too_long", line_number)
        names.append(name)
    if not names:
        raise BlacklistInputError("empty")
    return names


__all__ = [
    "BLACKLIST_NICK_MAX_LENGTH",
    "BlacklistInputError",
    "parse_blacklist_names",
]
