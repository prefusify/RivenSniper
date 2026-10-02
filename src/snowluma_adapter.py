"""SnowLuma 专用 OneBot v11 适配器。

反向 WebSocket 只接收事件和连接状态；全部 OneBot action 固定通过
SnowLuma HTTP API 调用，避免大图片 action 共享反向 WS 写缓冲。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from nonebot.adapters.onebot.v11 import Adapter as BaseOneBotV11Adapter
from nonebot.adapters.onebot.v11.bot import Bot
from nonebot.adapters.onebot.v11.exception import (
    NetworkError,
    OneBotV11AdapterException,
)
from nonebot.adapters.onebot.v11.utils import handle_api_result
from nonebot.drivers import HTTPClientMixin, Request
from nonebot.utils import DataclassEncoder
from typing_extensions import override


class SnowLumaOutcomeUnknown(NetworkError):
    """HTTP action 可能已被 SnowLuma 接收，但没有取得确定响应。"""


class SnowLumaAdapter(BaseOneBotV11Adapter):
    """通过 SnowLuma HTTP API 执行 OneBot action。"""

    def _api_root(self, self_id: str) -> str | None:
        roots = self.onebot_config.onebot_api_roots
        root = roots.get(self_id) or roots.get("*")
        if root is None:
            return None
        value = str(root)
        return value if value.endswith("/") else value + "/"

    @override
    async def _call_api(self, bot: Bot, api: str, **data: Any) -> Any:
        api_root = self._api_root(bot.self_id)
        if api_root is None:
            raise NetworkError(
                "未配置 SnowLuma HTTP API；请设置 ONEBOT_API_ROOTS")
        if not isinstance(self.driver, HTTPClientMixin):
            raise NetworkError("当前 NoneBot driver 不支持 HTTP 客户端")

        timeout = float(data.get("_timeout", self.config.api_timeout))
        headers = {"Content-Type": "application/json"}
        if self.onebot_config.onebot_access_token is not None:
            headers["Authorization"] = (
                "Bearer " + self.onebot_config.onebot_access_token)
        request = Request(
            "POST",
            api_root + api,
            headers=headers,
            timeout=timeout,
            content=json.dumps(data, cls=DataclassEncoder),
        )

        try:
            response = await self.driver.request(request)
            if 200 <= response.status_code < 300:
                if not response.content:
                    raise ValueError("Empty response")
                return handle_api_result(json.loads(response.content))
            raise SnowLumaOutcomeUnknown(
                "SnowLuma HTTP API returned unexpected status code: "
                f"{response.status_code}")
        except (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.PoolTimeout,
        ) as error:
            raise NetworkError(
                "SnowLuma HTTP API connection failed before request") from error
        except OneBotV11AdapterException:
            raise
        except Exception as error:
            raise SnowLumaOutcomeUnknown(
                "SnowLuma HTTP action outcome is unknown") from error
