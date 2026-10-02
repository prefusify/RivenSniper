"""在 Linux VPS 部署 WM IPv6 出口池；不运行任何 WM 查询或业务逻辑。

示例：python3 provision_wm_proxy.py --prefix <分配的/64> --interface eth0 --count 512
生成的 client.json 含代理凭据，只能复制到 Bot 的私有运行目录。
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import subprocess
import urllib.request


ROOT = Path("/opt/rivensniper-wm")
CONF = Path("/etc/rivensniper-wm")
VERSION = "1.0.0"
PACKAGE_SHA256 = "a27525c24a7240895d5de8d5fa6b7489638a2b98aa1cee81cf1030163ab3d92b"


def addresses(network: dict) -> list[str]:
    prefix = ipaddress.IPv6Network(network["prefix"])
    if prefix.prefixlen != 64 or not 1 <= network["count"] <= 4096:
        raise ValueError("需要已分配的 /64 与 1～4096 个出口")
    return [str(prefix.network_address + 0xC0DE0000 + index)
            for index in range(1, network["count"] + 1)]


def ensure_addresses(network: dict) -> None:
    interface = network["interface"]
    current = json.loads(subprocess.check_output(
        ["ip", "-6", "-j", "addr", "show", "dev", interface], text=True))
    present = {a["local"] for row in current for a in row["addr_info"]}
    for address in addresses(network):
        if address not in present:
            subprocess.run(["ip", "-6", "addr", "add", address + "/128",
                            "dev", interface], check=True)


def write_private(path: Path, text: str, mode: int = 0o600) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix")
    parser.add_argument("--interface", default="eth0")
    parser.add_argument("--count", type=int, default=512)
    parser.add_argument("--port", type=int, default=23990)
    parser.add_argument("--addresses-only", action="store_true")
    args = parser.parse_args()
    if args.addresses_only:
        ensure_addresses(json.loads((CONF / "network.json").read_text()))
        return
    if os.geteuid() != 0 or not args.prefix:
        parser.error("部署需要 root 和 --prefix")
    if not 1024 <= args.port <= 65535:
        parser.error("代理端口必须在 1024～65535 之间")
    network = {"prefix": args.prefix, "interface": args.interface, "count": args.count}
    exits = addresses(network)
    ROOT.mkdir(parents=True, exist_ok=True)
    CONF.mkdir(parents=True, exist_ok=True)
    package = ROOT / f"3proxy-{VERSION}.deb"
    if not package.exists():
        url = (f"https://github.com/3proxy/3proxy/releases/download/{VERSION}/"
               f"3proxy-{VERSION}.x86_64.deb")
        with urllib.request.urlopen(url, timeout=60) as response:
            package.write_bytes(response.read())
    if hashlib.sha256(package.read_bytes()).hexdigest() != PACKAGE_SHA256:
        raise RuntimeError("3proxy 包校验失败")
    extracted = ROOT / ("package-" + VERSION)
    subprocess.run(["dpkg-deb", "--extract", str(package), str(extracted)], check=True)
    binary = extracted / "bin" / "3proxy"
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise RuntimeError("3proxy 包缺少 bin/3proxy")
    try:
        account = pwd.getpwnam("rivensniper-wm")
    except KeyError:
        subprocess.run(["useradd", "--system", "--no-create-home", "--shell",
                        "/usr/sbin/nologin", "rivensniper-wm"], check=True)
        account = pwd.getpwnam("rivensniper-wm")
    secret_path = CONF / "proxy.secret"
    if not secret_path.exists():
        write_private(secret_path, secrets.token_urlsafe(32))
    password = secret_path.read_text().strip()
    lines = ["nserver 127.0.0.53", "nscache6 65536", "timeouts 1 5 15 30 60 120 15 30",
             f"maxconn {max(1024, args.count + 128)}", "auth strong", "log /dev/null"]
    for index in range(args.count):
        lines.append(f"users wm{index:04d}:CL:{password}")
    for index, address in enumerate(exits):
        lines.extend([
            f"allow wm{index:04d} 127.0.0.1 api.warframe.market,api64.ipify.org 443 HTTP_CONNECT",
            f"parent 1000 extip {address} 0",
        ])
    lines.extend(["deny *", f"proxy -6 -a -p{args.port} -i127.0.0.1"])
    proxy_config = CONF / "3proxy.cfg"
    write_private(proxy_config, "\n".join(lines) + "\n", 0o640)
    os.chown(proxy_config, 0, account.pw_gid)
    write_private(CONF / "network.json", json.dumps(network), 0o644)
    write_private(CONF / "client.json", json.dumps({
        "proxies": [f"http://wm{i:04d}:{password}@127.0.0.1:{args.port}"
                    for i in range(args.count)]}, indent=2))
    installed_script = ROOT / "provision.py"
    if Path(__file__).resolve() != installed_script:
        shutil.copyfile(__file__, installed_script)
    installed_script.chmod(0o755)
    unit = f"""[Unit]
Description=RivenSniper WM IPv6 proxy pool
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=rivensniper-wm
Group=rivensniper-wm
ExecStartPre=+/usr/bin/python3 {installed_script} --addresses-only
ExecStart={binary} {proxy_config}
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
LimitNOFILE={max(8192, 4 * args.count + 1024)}
TasksMax={max(2048, args.count + 256)}

[Install]
WantedBy=multi-user.target
"""
    write_private(Path("/etc/systemd/system/rivensniper-wm-proxy.service"), unit, 0o644)
    ensure_addresses(network)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", "rivensniper-wm-proxy"], check=True)
    subprocess.run(["systemctl", "restart", "rivensniper-wm-proxy"], check=True)
    print(json.dumps({"version": VERSION, "exits": len(exits), "port": args.port,
                      "client_config": str(CONF / "client.json")}))


if __name__ == "__main__":
    main()
