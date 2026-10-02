"""通过外部只读扫描为独立 IRC worker 获取连接材料。

先启动采集监督器，再用目标账号进入游戏世界并确认聊天可用。本脚本读取游戏
当前 IRC 端点、只读扫描进程内存中的 nonce 候选，并交给对应 worker 逐一认证。
认证成功后立即结束本次取票使用的游戏进程，避免两条同账号会话并存。
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import os
import sys
import time
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure:
        reconfigure(encoding="utf-8", errors="replace")

from src.plugins.riven_sniper.chat_collector.config import (  # noqa: E402
    ALL_SLOTS,
    load_accounts,
)
from src.plugins.riven_sniper.chat_collector.nonce_scan import (  # noqa: E402
    IRC_PORT_HIGH,
    IRC_PORT_LOW,
    find_game_pid,
    find_irc_endpoint,
    rank_hits,
    scan_process,
)
from src.plugins.riven_sniper.chat_collector.runtime import (  # noqa: E402
    atomic_write_json,
    pid_identity,
    read_json,
    terminate_process,
)
from src.plugins.riven_sniper.chat_collector.session import Ticket  # noqa: E402
from src.plugins.riven_sniper.chat_collector.supervisor import (  # noqa: E402
    DEFAULT_RUNTIME_ROOT,
    CollectorLayout,
    collector_status,
    collector_topology,
)


MODULE_NAME = "warframe.x64.exe"
_ACTIVE_WORKER_STATUSES = {
    "authenticating",
    "joining",
    "listening",
    "degraded",
    "probing",
    "reconnecting",
}
_TERMINAL_WORKER_STATUSES = {
    "auth_failed",
    "ticket_error",
    "config_error",
    "failed",
    "storage_error",
    "internal_error",
    "disconnected",
    "needs_ticket",
}


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def exe_build_id(path: str | Path) -> str:
    digest = hashlib.sha1()
    size = 0
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
            size += len(chunk)
    return f"{size:x}-{digest.hexdigest()[:16]}"


def process_executable(pid: int) -> str | None:
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    )
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(buffer))
        if kernel32.QueryFullProcessImageNameW(
            handle,
            0,
            buffer,
            ctypes.byref(length),
        ):
            return buffer.value
        return None
    finally:
        kernel32.CloseHandle(handle)


def _invalidate_ticket(path: Path, ticket_id: str, reason: str) -> None:
    current = read_json(path)
    if not current or current.get("ticket_id") != ticket_id or current.get("used"):
        return
    current["used"] = True
    current["invalidated_at"] = _utc()
    current["invalidated_reason"] = reason
    current["outcome"] = "invalidated"
    current.pop("nonce", None)
    current.pop("nonce_str", None)
    atomic_write_json(path, current)


def _wait_for_worker_exit(
    layout: CollectorLayout,
    slot: str,
    ticket_id: str,
    *,
    timeout: float = 5.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = collector_status(layout)["slots"][slot]
        if (
            state.get("ticket_id") == ticket_id
            and not state.get("process_alive")
        ):
            return True
        time.sleep(0.1)
    state = collector_status(layout)["slots"][slot]
    return not state.get("process_alive")


def _stop_authenticated_worker(
    layout: CollectorLayout,
    slot: str,
    ticket_id: str,
) -> bool:
    try:
        atomic_write_json(
            layout.slot_stop_path(slot),
            {
                "requested_at": _utc(),
                "reason": "game_terminate_failed",
                "ticket_id": ticket_id,
            },
        )
    except OSError as error:
        print(f"无法写入槽 {slot} 停止请求: {error}", file=sys.stderr)
        return False

    deadline = time.monotonic() + 5.0
    latest: dict = {}
    saw_current_worker = False
    while time.monotonic() < deadline:
        latest = collector_status(layout)["slots"][slot]
        if latest.get("ticket_id") == ticket_id:
            saw_current_worker = True
        if saw_current_worker and not latest.get("process_alive"):
            break
        time.sleep(0.1)
    if saw_current_worker and latest.get("process_alive"):
        terminate_process(
            int(latest.get("pid") or 0),
            str(latest.get("process_identity") or "") or None,
        )
        latest = collector_status(layout)["slots"][slot]
    stopped = (
        saw_current_worker
        and latest.get("ticket_id") == ticket_id
        and not latest.get("process_alive")
    )
    if stopped:
        try:
            layout.slot_stop_path(slot).unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            print(f"槽 {slot} 停止标记清理失败: {error}", file=sys.stderr)
            return False
    return stopped


def _wait_for_candidate(
    layout: CollectorLayout,
    slot: str,
    ticket_id: str,
    *,
    timeout: float,
) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout
    last_worker_update = ""
    while time.monotonic() < deadline:
        current = read_json(layout.ticket_path(slot)) or {}
        if current.get("ticket_id") == ticket_id and current.get("used"):
            outcome = str(current.get("outcome") or "")
            if outcome == "authenticated":
                return True, outcome
            if outcome != "auth_retrying":
                return False, outcome or "unknown"

        state = read_json(layout.state_path(slot)) or {}
        if state.get("ticket_id") == ticket_id:
            worker_update = str(state.get("updated_at") or "")
            if worker_update and worker_update != last_worker_update:
                last_worker_update = worker_update
                deadline = time.monotonic() + timeout
        if (
            state.get("ticket_id") == ticket_id
            and state.get("status") in _TERMINAL_WORKER_STATUSES
        ):
            detail = (
                state.get("outcome")
                or state.get("reason")
                or state.get("error")
                or state.get("status")
            )
            return False, str(detail)
        time.sleep(0.1)
    return False, "worker 未在期限内完成候选认证"


def _prepare_capture(
    slot: str,
    nick: str,
    layout: CollectorLayout,
) -> tuple[dict, dict]:
    layout.ensure()
    topology = collector_topology(layout)
    if slot not in topology.slots:
        raise RuntimeError(f"槽 {slot} 不属于当前 {topology.mode} 槽模式")
    status = collector_status(layout)
    if not status["supervisor_running"]:
        raise RuntimeError("监督器尚未运行；请先执行 run_chat_collector.py run")
    accounts_path = layout.accounts_path_for(topology)
    configured_nick = load_accounts(accounts_path, topology=topology)[slot]
    if configured_nick.casefold() != nick.casefold():
        raise RuntimeError(
            f"--nick {nick!r} 与 {accounts_path.name} 槽 {slot} 的昵称不一致"
        )
    slot_status = status["slots"][slot]
    if layout.stop_flag.exists() or layout.slot_stop_path(slot).exists():
        raise RuntimeError("该槽仍有停止请求；请先重启监督器并确认状态")
    if (
        slot_status.get("process_alive")
        and slot_status.get("status") in _ACTIVE_WORKER_STATUSES
    ):
        raise RuntimeError(f"槽 {slot} 已在运行，拒绝覆盖票据")

    existing = read_json(layout.ticket_path(slot)) or {}
    if existing and not existing.get("used"):
        terminal = (
            not slot_status.get("process_alive")
            and slot_status.get("status") in _TERMINAL_WORKER_STATUSES
        )
        old_id = str(existing.get("ticket_id") or "")
        if terminal and old_id:
            _invalidate_ticket(layout.ticket_path(slot), old_id, "由新取票替换")
        else:
            raise RuntimeError(f"槽 {slot} 已有未消费票据，拒绝覆盖")
    return status, existing


def capture_ticket(
    slot: str,
    nick: str,
    layout: CollectorLayout,
    *,
    pid: int = 0,
    consume_timeout: float = 90.0,
    max_candidates: int = 3,
    max_region_bytes: int = 512 << 20,
    first_hit: bool = False,
) -> int:
    slot = slot.upper()
    try:
        _, previous_ticket = _prepare_capture(slot, nick, layout)
        game_pid = int(pid) or find_game_pid()
        if not game_pid:
            raise RuntimeError(
                "找不到 Warframe.x64 进程；请先用目标账号进入世界并确认聊天可用"
            )
        process_identity = pid_identity(game_pid)
        if not process_identity:
            raise RuntimeError(f"无法确认进程 {game_pid} 的创建身份")
        executable = process_executable(game_pid)
        if (
            not executable
            or not Path(executable).is_file()
            or Path(executable).name.casefold() != MODULE_NAME
        ):
            raise RuntimeError(f"PID {game_pid} 不是可读取的 Warframe.x64.exe")
        build_id = exe_build_id(executable)
        host, port = find_irc_endpoint(game_pid)
        if not host or not port:
            raise RuntimeError(
                f"进程 {game_pid} 没有连到 {IRC_PORT_LOW}-{IRC_PORT_HIGH} 的已建立连接；"
                "请确认已进入世界且聊天可用"
            )
        print(f"游戏 PID={game_pid} build={build_id}，已确认 IRC 连接")
        print("开始只读扫描内存；不会注入游戏或修改任何进程字节…")
        started = time.monotonic()
        hits, statistics = scan_process(
            game_pid,
            max_region_bytes=max_region_bytes,
            first_hit=first_hit,
            progress=True,
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2

    previous_account_id = str(previous_ticket.get("account_id") or "").lower()
    ranked = rank_hits(hits)
    if previous_account_id:
        ranked.sort(key=lambda hit: hit.account_id != previous_account_id)
    ranked = ranked[: max(1, int(max_candidates))]
    print(
        f"扫描 {statistics['regions']} 个区域 "
        f"{statistics['bytes'] / 1048576:.0f} MB，"
        f"耗时 {time.monotonic() - started:.1f}s，"
        f"准备验证 {len(ranked)} 条候选"
    )
    if not ranked:
        print(
            "没有找到 nonce；请确认聊天连接已建立，或增大 --max-region-mb 后重试",
            file=sys.stderr,
        )
        return 1

    captured_at = _utc()
    ticket_path = layout.ticket_path(slot)
    active_ticket_id = ""
    try:
        for index, hit in enumerate(ranked, start=1):
            value = {
                "version": 1,
                "slot": slot,
                "created_at": captured_at,
                "host": host,
                "port": port,
                "nick": nick,
                "account_id": hit.account_id,
                "nonce": hit.nonce,
                "game_build": build_id,
                "capture_pid": game_pid,
                "used": False,
            }
            try:
                ticket = Ticket.from_dict(value, expected_slot=slot)
            except ValueError as error:
                print(f"候选 {index} 格式无效，跳过: {error}")
                continue
            value["ticket_id"] = ticket.ticket_id
            active_ticket_id = ticket.ticket_id
            atomic_write_json(ticket_path, value)
            print(
                f"候选 {index}/{len(ranked)} 已交给 worker 验证 "
                f"（指纹 {hit.fingerprint}，结构 {hit.structure}）"
            )
            authenticated, outcome = _wait_for_candidate(
                layout,
                slot,
                ticket.ticket_id,
                timeout=consume_timeout,
            )
            if authenticated:
                print(f"槽 {slot} 已认证，正在结束取票使用的游戏进程")
                game_terminated = terminate_process(
                    game_pid,
                    process_identity,
                    wait_timeout=15.0,
                )
                if not game_terminated:
                    worker_stopped = _stop_authenticated_worker(
                        layout,
                        slot,
                        ticket.ticket_id,
                    )
                    raise RuntimeError(
                        "collector 已认证，但无法结束游戏进程；"
                        f"worker_stopped={worker_stopped}，本票据必须重新获取"
                    )
                return 0

            print(f"候选 {index} 未通过认证: {outcome}")
            _invalidate_ticket(ticket_path, ticket.ticket_id, outcome)
            active_ticket_id = ""
            if not _wait_for_worker_exit(layout, slot, ticket.ticket_id):
                raise RuntimeError(
                    f"槽 {slot} 的候选 worker 未正常退出，停止继续尝试"
                )
        print("全部候选均未通过认证；请重新登录游戏后再取票", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        if active_ticket_id:
            _invalidate_ticket(ticket_path, active_ticket_id, "operator_cancelled")
        print("取票已取消", file=sys.stderr)
        return 1
    except (OSError, RuntimeError) as error:
        if active_ticket_id:
            _invalidate_ticket(ticket_path, active_ticket_id, str(error))
        print(f"取票失败: {error}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="外部只读获取 IRC 登录票据")
    parser.add_argument("--slot", required=True, choices=list(ALL_SLOTS))
    parser.add_argument("--nick", required=True)
    parser.add_argument("--pid", type=int, default=0, help="多开时必须明确指定游戏 PID")
    parser.add_argument("--runtime-root", default=str(DEFAULT_RUNTIME_ROOT))
    parser.add_argument(
        "--consume-timeout",
        type=float,
        default=90.0,
        help="worker 无状态更新时的超时秒数",
    )
    parser.add_argument("--max-candidates", type=int, default=3)
    parser.add_argument("--max-region-mb", type=int, default=512)
    parser.add_argument("--first-hit", action="store_true")
    args = parser.parse_args(argv)
    return capture_ticket(
        args.slot,
        args.nick,
        CollectorLayout(Path(args.runtime_root).resolve()),
        pid=args.pid,
        consume_timeout=args.consume_timeout,
        max_candidates=args.max_candidates,
        max_region_bytes=max(1, args.max_region_mb) << 20,
        first_hit=args.first_hit,
    )


if __name__ == "__main__":
    raise SystemExit(main())
