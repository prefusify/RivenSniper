"""从外部只读扫描游戏进程内存中的 IRC nonce。"""

from __future__ import annotations

import argparse
import ctypes
import json
import re
import subprocess
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass


_NONCE_PATTERN = re.compile(
    rb"accountId=([0-9a-fA-F]{24})&nonce=([!-~]{1,192})\x00"
)
_LOOSE_PATTERN = re.compile(
    rb"accountId=[0-9a-fA-F]{24}&nonce=[!-~]{1,192}"
)
_CLEAN_NONCE_FIELD = re.compile(r"[A-Za-z0-9_\-+/=.:%~]+")
IRC_PORT_LOW = 6695
IRC_PORT_HIGH = 6709

_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_VM_READ = 0x0010
_MEM_COMMIT = 0x1000
_MEM_PRIVATE = 0x20000
_PAGE_READWRITE = 0x04
_PAGE_WRITECOPY = 0x08
_PAGE_EXECUTE_READWRITE = 0x40
_PAGE_EXECUTE_WRITECOPY = 0x80
_PAGE_GUARD = 0x100
_READABLE_WRITABLE = frozenset({
    _PAGE_READWRITE,
    _PAGE_WRITECOPY,
    _PAGE_EXECUTE_READWRITE,
    _PAGE_EXECUTE_WRITECOPY,
})


class _MemoryBasicInformation64(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_ulonglong),
        ("AllocationBase", ctypes.c_ulonglong),
        ("AllocationProtect", wintypes.DWORD),
        ("_alignment1", wintypes.DWORD),
        ("RegionSize", ctypes.c_ulonglong),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("_alignment2", wintypes.DWORD),
    ]


@dataclass(frozen=True)
class NonceHit:
    account_id: str
    nonce: str
    address: int
    truncated: bool = False

    @property
    def fingerprint(self) -> str:
        import hashlib

        return hashlib.sha256(self.nonce.encode("ascii")).hexdigest()[:12]

    @property
    def field(self) -> str:
        return self.nonce.split("&nonce=", 1)[-1]

    @property
    def structure(self) -> str:
        """返回不含认证材料明文的结构视图。"""
        return (
            f"accountId=<HEX{len(self.account_id)}>"
            f"&nonce=<ASCII{len(self.field)}>"
            f",truncated={str(self.truncated).lower()}"
        )


def rank_hits(hits: list[NonceHit]) -> list[NonceHit]:
    """稳定排序候选，不在认证前擅自丢弃可能有效的值。"""

    def sort_key(hit: NonceHit) -> tuple[int, int, int, int]:
        plausible = 41 <= len(hit.nonce) <= 120
        clean = bool(_CLEAN_NONCE_FIELD.fullmatch(hit.field))
        return (
            0 if plausible else 1,
            0 if clean else 1,
            -len(hit.nonce),
            hit.address,
        )

    return sorted(hits, key=sort_key)


def _kernel32() -> ctypes.WinDLL:
    if sys.platform != "win32":
        raise OSError("nonce 内存扫描只支持 Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    )
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.VirtualQueryEx.argtypes = (
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.POINTER(_MemoryBasicInformation64),
        ctypes.c_size_t,
    )
    kernel32.VirtualQueryEx.restype = ctypes.c_size_t
    kernel32.ReadProcessMemory.argtypes = (
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    )
    kernel32.ReadProcessMemory.restype = wintypes.BOOL
    return kernel32


def _iter_regions(
    kernel32: ctypes.WinDLL,
    handle: int,
    *,
    max_region_bytes: int,
):
    address = 0
    information = _MemoryBasicInformation64()
    limit = 0x7FFFFFFFFFFF
    while address < limit:
        written = kernel32.VirtualQueryEx(
            handle,
            ctypes.c_void_p(address),
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
        if not written:
            break
        size = int(information.RegionSize)
        if size <= 0:
            break
        protection = int(information.Protect)
        if (
            int(information.State) == _MEM_COMMIT
            and int(information.Type) == _MEM_PRIVATE
            and not protection & _PAGE_GUARD
            and protection in _READABLE_WRITABLE
            and size <= max_region_bytes
        ):
            yield int(information.BaseAddress), size
        address = int(information.BaseAddress) + size


def _read_chunk(
    kernel32: ctypes.WinDLL,
    handle: int,
    address: int,
    size: int,
) -> bytes:
    buffer = ctypes.create_string_buffer(size)
    read = ctypes.c_size_t(0)
    if not kernel32.ReadProcessMemory(
        handle,
        ctypes.c_void_p(address),
        buffer,
        size,
        ctypes.byref(read),
    ):
        return buffer.raw[: read.value] if read.value else b""
    return buffer.raw[: read.value]


def scan_process(
    pid: int,
    *,
    chunk_bytes: int = 4 << 20,
    max_region_bytes: int = 512 << 20,
    first_hit: bool = False,
    progress: bool = False,
) -> tuple[list[NonceHit], dict[str, int]]:
    """只读扫描目标进程，返回候选及扫描统计。"""
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(
        _PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ,
        False,
        int(pid),
    )
    if not handle:
        raise OSError(
            f"无法以只读方式打开进程 {pid}（错误 {ctypes.get_last_error()}）；"
            "请用管理员权限运行"
        )
    statistics = {"regions": 0, "bytes": 0, "hits": 0, "loose": 0}
    found: dict[tuple[str, str], NonceHit] = {}
    overlap = 256
    try:
        for base, size in _iter_regions(
            kernel32,
            handle,
            max_region_bytes=max_region_bytes,
        ):
            statistics["regions"] += 1
            offset = 0
            tail = b""
            tail_address = base
            while offset < size:
                length = min(chunk_bytes, size - offset)
                data = _read_chunk(kernel32, handle, base + offset, length)
                if not data:
                    offset += length
                    tail = b""
                    continue
                statistics["bytes"] += len(data)
                window = tail + data
                window_address = tail_address if tail else base + offset
                statistics["loose"] += len(_LOOSE_PATTERN.findall(window))
                for match in _NONCE_PATTERN.finditer(window):
                    account_id = match.group(1).decode("ascii").lower()
                    prefix = (
                        f"accountId={match.group(1).decode('ascii')}&nonce="
                    )
                    raw_field = match.group(2).decode("ascii")
                    # 游戏内存中通常保存完整查询串。认证哈希只使用 nonce 前的
                    # accountId 与 nonce 两段，因此必须在后续第一个 & 处截断。
                    head = raw_field.split("&", 1)[0]
                    variants = (
                        [(head, True), (raw_field, False)]
                        if head != raw_field
                        else [(raw_field, False)]
                    )
                    for field, truncated in variants:
                        if not field:
                            continue
                        nonce = prefix + field
                        key = (account_id, nonce)
                        if key in found:
                            continue
                        found[key] = NonceHit(
                            account_id=account_id,
                            nonce=nonce,
                            address=window_address + match.start(),
                            truncated=truncated,
                        )
                        statistics["hits"] += 1
                if first_hit and found:
                    return list(found.values()), statistics
                tail = window[-overlap:]
                tail_address = window_address + max(0, len(window) - overlap)
                offset += length
            if progress and statistics["regions"] % 200 == 0:
                print(
                    f"  已扫描 {statistics['regions']} 个区域 "
                    f"{statistics['bytes'] / 1048576:.0f} MB，"
                    f"候选 {statistics['hits']} 条",
                    flush=True,
                )
    finally:
        kernel32.CloseHandle(handle)
    return list(found.values()), statistics


def find_memory_markers(
    pid: int,
    markers: tuple[bytes, ...],
    *,
    chunk_bytes: int = 4 << 20,
    max_region_bytes: int = 512 << 20,
) -> frozenset[bytes]:
    """只读查找目标进程中的固定标记，找到全部标记后立即返回。"""

    requested = frozenset(marker for marker in markers if marker)
    if not requested:
        return frozenset()
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(
        _PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ,
        False,
        int(pid),
    )
    if not handle:
        raise OSError(
            f"无法以只读方式打开进程 {pid}（错误 {ctypes.get_last_error()}）；"
            "请用管理员权限运行"
        )
    overlap = max(len(marker) for marker in requested) - 1
    found: set[bytes] = set()
    try:
        for base, size in _iter_regions(
            kernel32,
            handle,
            max_region_bytes=max_region_bytes,
        ):
            offset = 0
            tail = b""
            while offset < size:
                length = min(chunk_bytes, size - offset)
                data = _read_chunk(kernel32, handle, base + offset, length)
                if not data:
                    offset += length
                    tail = b""
                    continue
                window = tail + data
                found.update(
                    marker for marker in requested - found if marker in window
                )
                if found == requested:
                    return frozenset(found)
                tail = window[-overlap:] if overlap else b""
                offset += length
    finally:
        kernel32.CloseHandle(handle)
    return frozenset(found)


def _powershell_json(command: str, *, timeout: float) -> object:
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise OSError(f"无法查询 Windows 进程状态: {error}") from error
    payload = completed.stdout.strip()
    if completed.returncode != 0 or not payload:
        return []
    try:
        return json.loads(payload)
    except json.JSONDecodeError as error:
        raise OSError("Windows 进程查询返回了无效 JSON") from error


def find_game_pids(name: str = "Warframe.x64") -> tuple[int, ...]:
    value = _powershell_json(
        f"Get-Process -Name '{name}' -ErrorAction SilentlyContinue | "
        "Sort-Object StartTime -Descending | Select-Object -ExpandProperty Id | "
        "ConvertTo-Json -Compress",
        timeout=30,
    )
    raw = value if isinstance(value, list) else [value]
    return tuple(int(pid) for pid in raw if str(pid).isdigit())


def find_game_pid(name: str = "Warframe.x64") -> int:
    """在恰好存在一个游戏进程时返回 PID。"""
    pids = find_game_pids(name)
    if not pids:
        return 0
    if len(pids) > 1:
        raise RuntimeError(
            "检测到多个 Warframe.x64 进程，请使用 --pid 明确指定当前账号进程"
        )
    return pids[0]


def find_irc_endpoint(pid: int) -> tuple[str, int]:
    """读取目标进程当前已建立的 Warframe IRC 连接。"""
    value = _powershell_json(
        f"Get-NetTCPConnection -OwningProcess {int(pid)} -State Established "
        "-ErrorAction SilentlyContinue | Where-Object "
        f"{{ $_.RemotePort -ge {IRC_PORT_LOW} -and "
        f"$_.RemotePort -le {IRC_PORT_HIGH} }} | "
        "Select-Object RemoteAddress,RemotePort | ConvertTo-Json -Compress",
        timeout=60,
    )
    rows = value if isinstance(value, list) else ([value] if isinstance(value, dict) else [])
    endpoints = {
        (str(row.get("RemoteAddress") or "").strip(), int(row.get("RemotePort") or 0))
        for row in rows
        if isinstance(row, dict)
    }
    endpoints.discard(("", 0))
    if not endpoints:
        return "", 0
    if len(endpoints) > 1:
        raise RuntimeError(
            f"进程 {pid} 同时存在多个 IRC 端点，无法确定应使用哪一个"
        )
    return next(iter(endpoints))


def _selftest() -> int:
    marker_account = "0123456789abcdef01234567"
    marker_nonce = "a1b2c3d4e"
    marker_extra = "&ct=STM&relics=1&_a=Background.beef"
    child_code = (
        "import os,time,sys\n"
        f"blob = 'accountId={marker_account}&nonce={marker_nonce}{marker_extra}'\n"
        "keep = [blob, blob.encode('ascii')]\n"
        "sys.stdout.write(str(os.getpid()) + '\\n'); sys.stdout.flush()\n"
        "time.sleep(120)\n"
        "print(len(keep))\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        reported = child.stdout.readline().strip()
        if not reported.isdigit():
            print(f"子进程未就绪: {reported!r}", file=sys.stderr)
            return 1
        started = time.monotonic()
        hits, statistics = scan_process(
            int(reported),
            max_region_bytes=64 << 20,
        )
        ranked = rank_hits(hits)
        expected = f"accountId={marker_account}&nonce={marker_nonce}"
        passed = bool(ranked) and ranked[0].nonce == expected
        print(
            f"扫描 {statistics['regions']} 个区域 "
            f"{statistics['bytes'] / 1048576:.1f} MB，"
            f"耗时 {time.monotonic() - started:.2f}s"
        )
        print(
            f"{'PASS' if passed else 'FAIL'} "
            "从完整查询串截取 accountId 与 nonce，并将其排为首选"
        )
        return 0 if passed else 1
    finally:
        child.kill()
        child.wait(timeout=10)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="外部只读 nonce 扫描器")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if not args.selftest:
        parser.error("生产取票请使用 scripts/capture_chat_ticket.py")
    return _selftest()


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "IRC_PORT_HIGH",
    "IRC_PORT_LOW",
    "NonceHit",
    "find_game_pid",
    "find_game_pids",
    "find_irc_endpoint",
    "find_memory_markers",
    "rank_hits",
    "scan_process",
]
