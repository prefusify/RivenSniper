"""项目使用的 Discord 适配器扩展。"""

from __future__ import annotations

import json
from http import HTTPStatus

from nonebot.adapters.discord import Adapter as BaseDiscordAdapter
from nonebot.adapters.discord.exception import RateLimitException
from nonebot.drivers import Request, Response


class DiscordRateLimitError(RateLimitException):
    """保留 Discord 429 响应中的退避时间和全局限流标记。"""

    def __init__(self, response: Response) -> None:
        super().__init__(response)
        body: dict = {}
        if response.content:
            try:
                body = json.loads(response.content)
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
                pass
        raw_retry_after = (
            response.headers.get("Retry-After") or body.get("retry_after"))
        try:
            retry_after = float(raw_retry_after)
        except (TypeError, ValueError):
            retry_after = 0.0
        self.retry_after = max(0.0, retry_after)
        self.global_rate_limit = bool(body.get("global"))
        request = response.request
        self.route_key = (
            f"{request.method} {request.url.path}" if request else None)


class DiscordAdapter(BaseDiscordAdapter):
    """在生成 API 层丢弃响应头之前抛出带退避信息的 429 异常。"""

    async def request(self, setup: Request) -> Response:
        response = await super().request(setup)
        if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
            raise DiscordRateLimitError(response)
        return response


__all__ = ["DiscordAdapter", "DiscordRateLimitError"]
