"""逐槽启动 Warframe、登录并获取聊天采集连接材料。"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure:
        reconfigure(encoding="utf-8", errors="replace")

from scripts.capture_chat_ticket import capture_ticket  # noqa: E402
from src.plugins.riven_sniper.chat_collector.config import (  # noqa: E402
    CollectorConfigError,
    load_accounts,
)
from src.plugins.riven_sniper.chat_collector.nonce_scan import (  # noqa: E402
    find_irc_endpoint,
)
from src.plugins.riven_sniper.chat_collector.runtime import (  # noqa: E402
    pid_identity,
    pid_matches,
    terminate_process,
)
from src.plugins.riven_sniper.chat_collector.supervisor import (  # noqa: E402
    DEFAULT_RUNTIME_ROOT,
    CollectorLayout,
    collector_status,
    collector_topology,
)
from src.plugins.riven_sniper.chat_collector.windows_auto_login import (  # noqa: E402
    AutoLoginError,
    CredentialStoreError,
    LoginCredential,
    WindowsLoginDriver,
    credential_store_path,
    discover_launcher,
    internal_login_recovery_path,
    launcher_process_ids,
    launch_warframe,
    load_credentials,
    prepare_internal_auto_login,
    recover_internal_auto_login,
    save_credentials,
    terminate_warframe_process,
    wait_for_new_game_process,
    warframe_process_ids,
)


_ACTIVE_WORKER_STATUSES = {
    "authenticating",
    "joining",
    "listening",
    "degraded",
    "probing",
    "reconnecting",
}


def _pending_slots(layout: CollectorLayout) -> tuple[str, ...]:
    topology = collector_topology(layout)
    status = collector_status(layout)
    if not status.get("supervisor_running"):
        raise AutoLoginError("采集监督器尚未运行")
    pending: list[str] = []
    for slot in topology.slots:
        state = status["slots"][slot]
        if not state.get("process_alive") or str(
            state.get("status") or ""
        ) not in _ACTIVE_WORKER_STATUSES:
            pending.append(slot)
    return tuple(pending)


def _read_username(slot: str, existing: LoginCredential | None) -> str:
    suffix = "（直接回车保留）" if existing else ""
    while True:
        try:
            value = input(f"槽 {slot} 的登录邮箱/账号{suffix}: ").strip()
        except EOFError as error:
            raise AutoLoginError("标准输入已关闭，无法配置登录账号") from error
        if value:
            return value
        if existing:
            return existing.username
        print("登录邮箱/账号不能为空。")


def _read_password(slot: str, existing: LoginCredential | None) -> str:
    suffix = "（直接回车保留）" if existing else ""
    while True:
        try:
            value = getpass.getpass(f"槽 {slot} 的登录密码{suffix}: ")
        except EOFError as error:
            raise AutoLoginError("标准输入已关闭，无法配置登录密码") from error
        if value:
            return value
        if existing:
            return existing.password
        print("登录密码不能为空。")


def configure_credentials(
    layout: CollectorLayout,
    mode: str,
    slots: tuple[str, ...],
    *,
    update_existing: bool,
) -> dict[str, LoginCredential]:
    path = credential_store_path(layout.root)
    try:
        credentials_by_mode = load_credentials(path)
    except CredentialStoreError:
        if not update_existing:
            raise
        print("现有自动登录凭据无法读取，将重新配置。")
        credentials_by_mode = {}
    credentials = dict(credentials_by_mode.get(mode, {}))

    targets = slots if update_existing else tuple(
        slot for slot in slots if slot not in credentials
    )
    if not targets:
        return credentials

    print("登录密码输入时不会回显；保存文件只含当前 Windows 用户可解密的数据。")
    for slot in targets:
        existing = credentials.get(slot)
        username = _read_username(slot, existing)
        password = _read_password(slot, existing)
        credentials[slot] = LoginCredential(username, password)
        credentials_by_mode[mode] = credentials
        save_credentials(path, credentials_by_mode)
        print(f"槽 {slot} 的自动登录凭据已保存。")
    return credentials


def _wait_for_irc(
    pid: int,
    identity: str,
    *,
    timeout: float,
    poll_interval: float = 2.0,
) -> None:
    deadline = time.monotonic() + max(0.0, timeout)
    last_error = ""
    while time.monotonic() < deadline:
        if not pid_matches(pid, identity):
            raise AutoLoginError("Warframe 在登录完成前退出")
        try:
            host, port = find_irc_endpoint(pid)
        except (OSError, RuntimeError) as error:
            last_error = str(error)
        else:
            if host and port:
                return
        time.sleep(max(0.1, poll_interval))
    detail = f"；最近错误：{last_error}" if last_error else ""
    raise AutoLoginError(f"登录后等待聊天连接超时{detail}")


def _terminate_owned_process(pid: int, identity: str) -> bool:
    if not pid_matches(pid, identity):
        return True
    return terminate_process(pid, identity, wait_timeout=5.0)


def _terminate_owned_game(pid: int, identity: str) -> bool:
    if pid not in warframe_process_ids():
        return True
    if pid_matches(pid, identity) and terminate_process(
        pid, identity, wait_timeout=5.0
    ):
        return True
    return terminate_warframe_process(pid, wait_timeout=5.0)


def _wait_for_owned_process_exit(
    pid: int,
    identity: str,
    *,
    timeout: float,
) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if not pid_matches(pid, identity):
            return True
        time.sleep(0.1)
    return not pid_matches(pid, identity)


def _wait_for_no_warframe_processes(
    *,
    timeout: float,
    poll_interval: float = 0.1,
) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if not warframe_process_ids():
            return True
        time.sleep(max(0.05, poll_interval))
    return not warframe_process_ids()


def run_automatic_capture(
    layout: CollectorLayout,
    *,
    launcher: str | Path | None,
    launch_timeout: float,
    login_timeout: float,
    login_settle_seconds: float,
) -> int:
    topology = collector_topology(layout)
    accounts = load_accounts(
        layout.accounts_path_for(topology), topology=topology
    )
    slots = _pending_slots(layout)
    if not slots:
        print("当前模式的全部槽都已运行，无需重新取票。")
        return 0

    credentials = configure_credentials(
        layout, topology.mode, slots, update_existing=False
    )
    running = set(warframe_process_ids())
    if running:
        raise AutoLoginError(
            "开始全自动取票前必须退出现有 Warframe；脚本只接管自己启动的游戏"
        )
    recover_internal_auto_login(internal_login_recovery_path(layout.root))
    launcher_installation = discover_launcher(launcher)
    launcher_path = launcher_installation.path
    if launcher_process_ids(launcher_path):
        raise AutoLoginError(
            "开始全自动取票前必须退出已有 Warframe 启动器；"
            "脚本只接管自己启动的窗口"
        )
    driver = WindowsLoginDriver()
    failed: list[str] = []
    completed: list[str] = []

    print(
        f"已识别 {launcher_installation.kind.label}启动器："
        f"{launcher_path}"
    )
    print(
        f"准备为 {len(slots)} 个待取票槽逐个启动、登录和认证；"
        "成功后游戏会自动关闭。"
    )
    for slot in slots:
        current_pending = _pending_slots(layout)
        if slot not in current_pending:
            continue
        credential = credentials.get(slot)
        if credential is None:
            failed.append(slot)
            print(f"槽 {slot} 缺少自动登录凭据，已跳过。", file=sys.stderr)
            continue

        pid = 0
        identity = ""
        launcher_pid = 0
        launcher_identity = ""
        internal_login = None
        stop_after_failure = False
        print(f"槽 {slot}：正在启动 Warframe…")
        try:
            baseline = set(warframe_process_ids())
            if baseline:
                raise AutoLoginError("检测到非本脚本启动的 Warframe 进程")
            if launcher_process_ids(launcher_path):
                raise AutoLoginError("检测到非本脚本启动的 Warframe 启动器")
            internal_login = prepare_internal_auto_login(
                credential,
                recovery_path=internal_login_recovery_path(layout.root),
            )
            launcher_process = launch_warframe(launcher_installation)
            launcher_pid = int(launcher_process.pid)
            launcher_identity = str(pid_identity(launcher_pid) or "")
            if not launcher_identity:
                raise AutoLoginError("无法确认新启动器进程的创建身份")
            print(f"槽 {slot}：等待启动器就绪并自动点击“开始游戏”…")
            launcher_frame = driver.wait_for_launcher_and_start_game(
                launcher_pid,
                timeout=launch_timeout,
            )
            print(
                f"槽 {slot}：已在 {launcher_frame.width}x"
                f"{launcher_frame.height} 启动器中点击“开始游戏”，"
                "等待游戏进程…"
            )
            pid = wait_for_new_game_process(
                baseline, timeout=launch_timeout
            )
            identity = str(pid_identity(pid) or "")
            if not identity:
                raise AutoLoginError("无法确认新游戏进程的创建身份")
            if launcher_identity and not _wait_for_owned_process_exit(
                launcher_pid,
                launcher_identity,
                timeout=10.0,
            ):
                if not _terminate_owned_process(
                    launcher_pid, launcher_identity
                ):
                    raise AutoLoginError("游戏启动后无法关闭本次启动器进程")
            launcher_identity = ""
            print(f"槽 {slot}：已检测到游戏进程，等待内部登录初始化…")
            frame = driver.submit_internal_credentials(
                pid,
                credential,
                window_timeout=launch_timeout,
                settle_seconds=login_settle_seconds,
            )
            print(
                f"槽 {slot}：已由内部 UI 提交登录"
                f"（当前客户区 {frame.width}x{frame.height}），等待聊天连接…"
            )
            _wait_for_irc(pid, identity, timeout=login_timeout)
            internal_login.restore(keep_recovery=True)
            print(f"槽 {slot}：聊天连接已建立，开始只读取票。")
            result = capture_ticket(
                slot,
                accounts[slot],
                layout,
                pid=pid,
            )
            if result != 0:
                raise AutoLoginError(f"取票脚本退出码 {result}")
            if not _wait_for_no_warframe_processes(timeout=15.0):
                cleaned = _terminate_owned_game(pid, identity)
                if not cleaned or not _wait_for_no_warframe_processes(
                    timeout=5.0
                ):
                    stop_after_failure = True
                    raise AutoLoginError(
                        "取票完成后 Warframe 进程仍未完全退出；"
                        "停止后续槽以免串号"
                    )
            completed.append(slot)
            print(f"槽 {slot}：全自动取票完成。")
        except KeyboardInterrupt:
            if pid and identity:
                _terminate_owned_game(pid, identity)
            if launcher_pid and launcher_identity:
                _terminate_owned_process(launcher_pid, launcher_identity)
            raise
        except (
            AutoLoginError,
            CredentialStoreError,
            OSError,
            RuntimeError,
            ValueError,
        ) as error:
            cleanup_failed = False
            if pid and identity and not _terminate_owned_game(pid, identity):
                cleanup_failed = True
            if (
                launcher_pid
                and launcher_identity
                and not _terminate_owned_process(launcher_pid, launcher_identity)
            ):
                cleanup_failed = True
            if cleanup_failed:
                print(
                    f"槽 {slot} 失败且无法关闭本次启动的进程；"
                    "停止后续槽以免串号。",
                    file=sys.stderr,
                )
                failed.append(slot)
                break
            failed.append(slot)
            print(f"槽 {slot} 未完成：{error}", file=sys.stderr)
            if stop_after_failure:
                break
        finally:
            if internal_login is not None:
                internal_login.restore()

    if completed:
        print(f"已完成槽：{'/'.join(completed)}")
    if failed:
        print(f"未完成槽：{'/'.join(failed)}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="逐槽自动启动 Warframe、登录并获取聊天采集连接材料"
    )
    parser.add_argument(
        "--runtime-root", default=str(DEFAULT_RUNTIME_ROOT)
    )
    parser.add_argument(
        "--launcher",
        help=(
            "Warframe Launcher.exe 的完整路径；"
            "默认自动查找官网独立版和 Steam 版"
        ),
    )
    parser.add_argument(
        "--configure",
        action="store_true",
        help="只配置或更新当前模式全部槽的自动登录凭据",
    )
    parser.add_argument("--launch-timeout", type=float, default=300.0)
    parser.add_argument("--login-timeout", type=float, default=180.0)
    parser.add_argument("--login-settle-seconds", type=float, default=2.0)
    args = parser.parse_args(argv)

    if os.name != "nt":
        print("全自动登录只支持 Windows。", file=sys.stderr)
        return 2
    layout = CollectorLayout(Path(args.runtime_root).resolve())
    try:
        layout.ensure()
        topology = collector_topology(layout)
        load_accounts(layout.accounts_path_for(topology), topology=topology)
        if args.configure:
            configure_credentials(
                layout,
                topology.mode,
                topology.slots,
                update_existing=True,
            )
            print("当前模式的自动登录凭据配置完成。")
            return 0
        return run_automatic_capture(
            layout,
            launcher=args.launcher,
            launch_timeout=max(1.0, args.launch_timeout),
            login_timeout=max(1.0, args.login_timeout),
            login_settle_seconds=max(0.0, args.login_settle_seconds),
        )
    except KeyboardInterrupt:
        print("全自动取票已取消。", file=sys.stderr)
        return 130
    except (
        AutoLoginError,
        CollectorConfigError,
        CredentialStoreError,
        OSError,
        RuntimeError,
        ValueError,
    ) as error:
        print(f"全自动取票无法开始：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
