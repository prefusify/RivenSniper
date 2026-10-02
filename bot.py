import os
import socket
import sys

import nonebot
from nonebot import get_plugin_config
from nonebot.adapters.onebot.v11.config import Config as OneBotConfig
from nonebot.drivers import HTTPClientMixin

from src.discord_adapter import DiscordAdapter
from src.snowluma_adapter import SnowLumaAdapter

nonebot.init()

driver = nonebot.get_driver()


def _validate_snowluma_config() -> None:
    onebot = get_plugin_config(OneBotConfig)
    if not onebot.onebot_api_roots:
        print(
            "[启动失败] 未配置 SnowLuma HTTP API。请在 .env 设置 "
            "ONEBOT_API_ROOTS，或重新运行启动BOT.cmd 完成升级。",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if not isinstance(driver, HTTPClientMixin):
        print(
            "[启动失败] DRIVER 必须包含 ~httpx，SnowLuma OneBot action "
            "需要 HTTP 客户端。",
            file=sys.stderr,
        )
        raise SystemExit(1)


_validate_snowluma_config()
driver.register_adapter(SnowLumaAdapter)
if getattr(driver.config, "discord_dm_enabled", False):
    driver.register_adapter(DiscordAdapter)

nonebot.load_plugins("src/plugins")


def _find_port_holder(port: int) -> str:
    """Windows 下用 netstat/tasklist 找出占用端口的进程，失败返回空串。"""
    if os.name != "nt":
        return ""
    import re
    import subprocess
    try:
        flags = subprocess.CREATE_NO_WINDOW
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, timeout=10, creationflags=flags).stdout
        pids = set()
        for line in out.splitlines():
            parts = line.split()
            if (len(parts) >= 5 and parts[0] == "TCP"
                    and parts[3] == "LISTENING" and parts[1].endswith(f":{port}")):
                pids.add(parts[4])
        names = []
        for pid in sorted(pids):
            t = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                               capture_output=True, text=True, timeout=10,
                               creationflags=flags).stdout
            m = re.match(r'"([^"]+)"', t.strip())
            names.append(f"{m.group(1) if m else '?'}（PID {pid}）")
        return "、".join(names)
    except Exception:
        return ""


def _preflight_check_port() -> None:
    """启动前预检监听端口。

    端口被占用时 uvicorn 只会抛一行绑定错误就退出，控制台窗口一闪而过很难看清。
    这里提前探测并给出中文提示。已知坑：Steam 的 steamwebhelper 开启 CEF 远程调试
    （Steam 目录存在 .cef-enable-remote-debugging 文件）时会占用 127.0.0.1:8080。
    """
    host, port = str(driver.config.host), int(driver.config.port)
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            if os.name != "nt":
                # POSIX 下与 uvicorn 一致，允许复用 TIME_WAIT 端口；
                # Windows 的 SO_REUSEADDR 语义不同（能绑上他人正监听的端口），不能设
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
    except OSError as e:
        print(f"[启动失败] 监听地址 {host}:{port} 不可用：{e}", file=sys.stderr)
        holder = _find_port_holder(port)
        if holder:
            print(f"端口 {port} 已被占用：{holder}", file=sys.stderr)
        print(
            "常见原因与处理：\n"
            "  1. Steam 的 steamwebhelper 开启 CEF 远程调试后会占用 127.0.0.1:8080\n"
            "     （Steam 目录存在 .cef-enable-remote-debugging 文件时触发）。\n"
            "     处理：编辑 .env 换一个 PORT，并把 SnowLuma 反向 WS 地址同步改为\n"
            "     ws://127.0.0.1:<新端口>/onebot/v11/ws；或删除该文件并重启 Steam。\n"
            "  2. 旧的 bot 实例尚未退出：结束旧进程后重试。",
            file=sys.stderr,
        )
        if os.name == "nt" and sys.stdin.isatty():
            try:
                input("按回车键退出...")
            except EOFError:
                pass
        raise SystemExit(1)


if __name__ == "__main__":
    _preflight_check_port()
    # 反向 WebSocket 对端可能在停止监听后仍保持连接。Uvicorn 默认无限
    # 等待现有连接，会让控制台重启永久卡在 lifespan shutdown 之前。
    # 超时后只取消仍未退出的服务器连接任务，随后仍执行插件关闭钩子。
    nonebot.run(timeout_graceful_shutdown=10)
