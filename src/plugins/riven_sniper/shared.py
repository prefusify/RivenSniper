"""插件级共享单例（store / config），供 commands 与 poller 使用。"""

from __future__ import annotations

import time

from nonebot import get_plugin_config

from .config import Config
from .store import Store

_store: Store | None = None
_config: Config | None = None
_poller = None  # SniperPoller 实例（WebUI 读队列深度等运行态）
_bargain = None  # BargainPoller 实例（紫卡钩子与 WebUI 运行态）

# QQ 账号在线态：来自 OneBot 心跳事件的 status.online。
# WS 连着≠账号在线——账号被踢下线（KickedOffLine）后 SnowLuma 仍保持反向 WS，
# 但发送会持续超时。心跳每 30s 上报一次，据此判定「可否真正发送」。
# None = 尚未收到心跳（未知，按连接态处理）。
_bot_online: bool | None = None
_bot_online_ts: float = 0.0


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


def close_store() -> None:
    """关闭并清除进程级 Store，供应用生命周期正常退出。"""
    global _store
    store, _store = _store, None
    if store is not None:
        store.close()


def get_config() -> Config:
    global _config
    if _config is None:
        _config = get_plugin_config(Config)
    return _config


def set_poller(p) -> None:
    global _poller
    _poller = p


def get_poller():
    return _poller


def set_bargain(p) -> None:
    global _bargain
    _bargain = p


def get_bargain():
    return _bargain


def set_bot_online(online: bool) -> None:
    """记录 OneBot 心跳上报的账号在线态（含时间戳）。"""
    global _bot_online, _bot_online_ts
    _bot_online = online
    _bot_online_ts = time.time()


def bot_online_state() -> tuple[bool | None, float]:
    """返回 (最近心跳的账号在线态, 心跳时间戳)。在线态为 None 表示尚未收到心跳。"""
    return _bot_online, _bot_online_ts
