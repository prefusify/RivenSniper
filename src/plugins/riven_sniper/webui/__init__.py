"""WebUI 管理面板：挂载到 NoneBot 的 FastAPI 应用（/admin）。"""

from __future__ import annotations

import nonebot
from nonebot import logger


def mount() -> bool:
    app = nonebot.get_app()
    from .api import router
    app.include_router(router, prefix="/admin")
    logger.info("WebUI 已挂载: http://127.0.0.1:{}/admin",
                nonebot.get_driver().config.port)
    return True
