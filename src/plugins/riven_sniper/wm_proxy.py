"""代理配置中可选的 SSH 本地转发；不在 VPS 执行查询或匹配。"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


class TunnelGroup:
    """每条 Windows SSH 连接最多承担 128 个出口，避免单连接描述符耗尽。"""

    def __init__(self, configs: list[dict]):
        self.tunnels = [SshTunnel(config) for config in configs]

    @property
    def ready(self) -> bool:
        return all(tunnel.ready for tunnel in self.tunnels)

    def start(self) -> None:
        for tunnel in self.tunnels:
            tunnel.start()

    async def close(self) -> None:
        await asyncio.gather(*(tunnel.close() for tunnel in self.tunnels))


def proxy_routes(data: dict) -> tuple[list[str], TunnelGroup | None]:
    urls = data["proxies"]
    if not isinstance(urls, list) or not urls or not all(
            isinstance(url, str) and url.startswith("http://") for url in urls):
        raise ValueError("代理配置需要非空 HTTP 代理 URL 列表")
    if len(set(urls)) != len(urls):
        raise ValueError("代理出口不能重复")
    if not data.get("ssh"):
        return urls, None
    config = data["ssh"]
    port = int(config["local_port"])
    count = (len(urls) + 127) // 128
    if not 1 <= port <= 65536 - count:
        raise ValueError("SSH 转发端口范围无效")
    routed = []
    for index, url in enumerate(urls):
        parsed = urlsplit(url)
        if parsed.hostname != "127.0.0.1" or parsed.port != port:
            raise ValueError("SSH 代理 URL 必须指向配置的本地回环端口")
        auth = parsed.netloc.rpartition("@")[0]
        # 相邻出口分散到不同 SSH 连接，避免一轮查询集中挤占单条 TCP 转发。
        authority = (auth + "@" if auth else "") + f"127.0.0.1:{port + index % count}"
        routed.append(urlunsplit(parsed._replace(netloc=authority)))
    return routed, TunnelGroup([{**config, "local_port": port + i} for i in range(count)])


class SshTunnel:
    def __init__(self, config: dict):
        self.config = config
        self.ready = False
        self._task: asyncio.Task | None = None
        self._process: subprocess.Popen | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    def _launch(self):
        cfg = self.config
        identity = str(Path(cfg["identity_file"]).expanduser())
        local_port, remote_port = int(cfg["local_port"]), int(cfg["remote_port"])
        if not (1 <= local_port <= 65535 and 1 <= remote_port <= 65535):
            raise ValueError("SSH 转发端口无效")
        return subprocess.Popen([
            "ssh", "-N", "-T", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", "ExitOnForwardFailure=yes",
            "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3", "-i", identity,
            "-p", str(cfg.get("port", 22)),
            "-L", f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
            f"{cfg.get('user', 'root')}@{cfg['host']}",
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    async def _stop_process(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
        try:
            await asyncio.to_thread(process.wait, timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            await asyncio.to_thread(process.wait)

    async def _run(self) -> None:
        try:
            while True:
                try:
                    # Windows 的部分 ASGI 事件循环不支持 asyncio 子进程，启动放到线程。
                    launch = asyncio.create_task(asyncio.to_thread(self._launch))
                    try:
                        self._process = await asyncio.shield(launch)
                    except asyncio.CancelledError:
                        # 取消线程等待不会停止 Popen；先接回进程句柄，再由 finally 清理。
                        self._process = await launch
                        raise
                    while self._process.poll() is None:
                        try:
                            _, writer = await asyncio.wait_for(asyncio.open_connection(
                                "127.0.0.1", int(self.config["local_port"])), timeout=1)
                            writer.close()
                            await writer.wait_closed()
                            self.ready = self._process.poll() is None
                        except (OSError, TimeoutError):
                            self.ready = False
                        await asyncio.sleep(1)
                except (OSError, ValueError, KeyError):
                    self.ready = False
                finally:
                    self.ready = False
                    await self._stop_process()
                await asyncio.sleep(5)
        finally:
            self.ready = False
            await self._stop_process()

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
