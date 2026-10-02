"""采集器运行时原语：原子状态文件、JSONL 单写者和安全 PID 探测。"""

from __future__ import annotations

import ctypes
import json
import os
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any


def atomic_write_json(
    path: str | Path,
    value: Any,
    *,
    retries: int = 6,
    retry_delay: float = 0.04,
) -> None:
    """写临时文件后原子替换；替换失败时绝不原地覆盖目标。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(
        f"{target.name}.tmp.{os.getpid()}.{time.time_ns()}"
    )
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())

        last_error: OSError | None = None
        for attempt in range(max(1, retries)):
            try:
                os.replace(temporary, target)
                return
            except OSError as error:
                last_error = error
                if attempt + 1 < max(1, retries) and retry_delay > 0:
                    time.sleep(retry_delay * (attempt + 1))
        assert last_error is not None
        raise last_error
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def read_json(path: str | Path) -> dict[str, Any] | None:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def append_jsonl(path: str | Path, value: dict[str, Any], *, retries: int = 4) -> None:
    """向一个分槽文件追加一条完整 UTF-8 JSONL 记录。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    last_error: OSError | None = None
    for attempt in range(max(1, retries)):
        try:
            with target.open("a+b", buffering=0) as stream:
                _discard_incomplete_jsonl_tail(stream)
                stream.seek(0, os.SEEK_END)
                start = stream.tell()
                try:
                    remaining = memoryview(payload)
                    while remaining:
                        written = stream.write(remaining)
                        if not written:
                            raise OSError("JSONL append returned zero bytes")
                        remaining = remaining[written:]
                    stream.flush()
                except OSError:
                    # 每槽只有一个写者，因此可安全回滚本次未完成的 append。
                    stream.truncate(start)
                    stream.flush()
                    raise
            return
        except OSError as error:
            last_error = error
            if attempt + 1 < max(1, retries):
                time.sleep(0.04 * (attempt + 1))
    assert last_error is not None
    raise last_error


def _discard_incomplete_jsonl_tail(stream: Any) -> None:
    """删除上次崩溃留下的末尾残行，保留此前所有完整行。"""
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    if size == 0:
        return
    stream.seek(size - 1)
    if stream.read(1) == b"\n":
        return
    position = size
    while position > 0:
        start = max(0, position - 8192)
        stream.seek(start)
        chunk = stream.read(position - start)
        newline = chunk.rfind(b"\n")
        if newline >= 0:
            stream.truncate(start + newline + 1)
            return
        position = start
    stream.truncate(0)


def pid_identity(pid: int) -> str | None:
    """返回进程创建身份；Windows 路径只查询句柄，不会发送信号。"""
    if pid <= 0:
        return None
    if os.name == "nt":
        return _windows_pid_identity(pid)
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        # Linux stat 第 22 字段是启动 tick；括号内进程名可能含空格。
        fields = proc_stat.read_text(encoding="ascii").rsplit(")", 1)[1].split()
        return fields[19]
    except (OSError, IndexError):
        return None


def pid_matches(pid: int, identity: str | None) -> bool:
    return bool(identity) and pid_identity(pid) == identity


def resume_process(pid: int, identity: str | None) -> bool:
    """仅在 PID 创建身份匹配时恢复 Windows 进程。"""
    if os.name != "nt" or not pid_matches(pid, identity):
        return False
    process_suspend_resume = 0x0800
    process_query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
    ntdll.NtResumeProcess.restype = wintypes.LONG
    handle = kernel32.OpenProcess(
        process_suspend_resume | process_query_limited_information, False, pid
    )
    if not handle:
        return False
    try:
        if _windows_handle_identity(kernel32, handle) != identity:
            return False
        return ntdll.NtResumeProcess(handle) == 0
    finally:
        kernel32.CloseHandle(handle)


def terminate_process(
    pid: int,
    identity: str | None,
    *,
    exit_code: int = 0,
    wait_timeout: float = 3.0,
) -> bool:
    """仅终止创建身份仍匹配的 Windows 进程，并等待该身份消失。"""
    if os.name != "nt" or not pid_matches(pid, identity):
        return False
    process_terminate = 0x0001
    process_query_limited_information = 0x1000
    synchronize = 0x00100000
    wait_object_0 = 0x00000000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(
        process_terminate | process_query_limited_information | synchronize,
        False,
        pid,
    )
    if not handle:
        return not pid_matches(pid, identity)
    try:
        # PID 可能在首次检查与 OpenProcess 之间被复用；以已打开句柄再核对一次。
        if _windows_handle_identity(kernel32, handle) != identity:
            return False
        if not kernel32.TerminateProcess(handle, int(exit_code)):
            return kernel32.WaitForSingleObject(handle, 0) == wait_object_0
        wait_milliseconds = min(
            0xFFFFFFFE,
            round(max(0.0, wait_timeout) * 1000),
        )
        return (
            kernel32.WaitForSingleObject(handle, wait_milliseconds)
            == wait_object_0
        )
    finally:
        kernel32.CloseHandle(handle)


def _windows_pid_identity(pid: int) -> str | None:
    process_query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return None
    try:
        return _windows_handle_identity(kernel32, handle)
    finally:
        kernel32.CloseHandle(handle)


def _windows_handle_identity(kernel32: Any, handle: Any) -> str | None:
    """从已打开的句柄读取创建时间，避免 PID 复用检查中的竞态。"""
    still_active = 259
    kernel32.GetExitCodeProcess.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    )
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    exit_code = wintypes.DWORD()
    if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
        return None
    if exit_code.value != still_active:
        return None
    creation = wintypes.FILETIME()
    exit_time = wintypes.FILETIME()
    kernel = wintypes.FILETIME()
    user = wintypes.FILETIME()
    if not kernel32.GetProcessTimes(
        handle,
        ctypes.byref(creation),
        ctypes.byref(exit_time),
        ctypes.byref(kernel),
        ctypes.byref(user),
    ):
        return None
    return f"{creation.dwHighDateTime:08x}{creation.dwLowDateTime:08x}"
