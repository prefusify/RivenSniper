"""Windows 双击启动器的结构与首次配置回归。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WINDOWS_SCRIPTS = ROOT / "scripts" / "windows"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")

sys.path.insert(0, str(ROOT))

from src.plugins.riven_sniper.chat_collector.runtime import pid_identity  # noqa: E402


def _run(script: str, project: Path, *arguments: str, input_text: str = ""):
    assert POWERSHELL is not None
    return subprocess.run(
        [
            POWERSHELL,
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(WINDOWS_SCRIPTS / script),
            "-ProjectRoot",
            str(project),
            *arguments,
        ],
        cwd=ROOT,
        env=os.environ.copy(),
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


def _fake_uv(project: Path, body: str = "") -> Path:
    binary = project / "fake-bin"
    binary.mkdir()
    path = binary / "uv.cmd"
    path.write_text(
        "@echo off\n" + body + "\nexit /b 0\n",
        encoding="ascii",
    )
    return path


def _with_fake_uv(project: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["PATH"] = str(project / "fake-bin") + os.pathsep + environment["PATH"]
    return environment


def _prepare_bot_project(project: Path) -> Path:
    project.mkdir()
    shutil.copyfile(ROOT / ".env.example", project / ".env.example")
    for name in ("bot.py", "pyproject.toml", "uv.lock"):
        (project / name).write_text("", encoding="utf-8")
    (project / ".env").write_text(
        "ONEBOT_ACCESS_TOKEN=test-token-with-enough-entropy\n"
        'ONEBOT_API_ROOTS={"*":"http://127.0.0.1:3000/"}\n'
        "PORT=8180\n",
        encoding="utf-8",
    )
    return _fake_uv(project)


@pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="Windows launcher")
@pytest.mark.parametrize("script", ["start_bot.ps1", "start_collector.ps1"])
def test_powershell_launcher_doctor_is_noninteractive(script):
    result = _run(script, ROOT, "-Doctor")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "自检通过" in result.stdout


def test_root_launchers_are_portable_ascii_wrappers():
    for name, target in (
        ("启动BOT.cmd", "start_bot.ps1"),
        ("启动聊天采集.cmd", "start_collector.ps1"),
    ):
        content = (ROOT / name).read_text(encoding="utf-8")
        assert content.isascii()
        assert "%~dp0" in content
        assert target in content
        assert "E:\\" not in content


def test_powershell_launchers_keep_windows_powershell_utf8_bom():
    for path in WINDOWS_SCRIPTS.glob("*.ps1"):
        assert path.read_bytes().startswith(b"\xef\xbb\xbf"), path.name


@pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="Windows launcher")
def test_bot_first_run_wizard_writes_valid_minimum_config(tmp_path):
    project = tmp_path / "bot project"
    project.mkdir()
    shutil.copyfile(ROOT / ".env.example", project / ".env.example")
    for name in ("bot.py", "pyproject.toml", "uv.lock"):
        (project / name).write_text("", encoding="utf-8")
    fake_uv = _fake_uv(
        project,
        'if /I "%~5"=="has-qq" exit /b 1\n'
        'if /I "%~5"=="upsert-qq" echo %~6,%~7>target.txt',
    )

    result = subprocess.run(
        [
            POWERSHELL,
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(WINDOWS_SCRIPTS / "start_bot.ps1"),
            "-ProjectRoot",
            str(project),
            "-UvPath",
            str(fake_uv),
        ],
        cwd=ROOT,
        env=_with_fake_uv(project),
        input="123456789\n987654321\n\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    config = (project / ".env").read_text(encoding="utf-8")
    assert "SNIPER_GROUPS=" not in config
    assert "SNIPER_ADMINS=" not in config
    assert "WEBUI_PASSWORD=" not in config
    assert (project / "target.txt").read_text(
        encoding="utf-8").strip() == "123456789,987654321"
    token_line = next(
        line for line in config.splitlines()
        if line.startswith("ONEBOT_ACCESS_TOKEN=")
    )
    assert len(token_line.partition("=")[2]) >= 32
    assert "改成随机字符串" not in token_line
    assert 'ONEBOT_API_ROOTS={"*":"http://127.0.0.1:3000/"}' in config


@pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="Windows launcher")
def test_bot_launcher_upgrades_existing_env_with_snowluma_http_root(tmp_path):
    project = tmp_path / "bot upgrade project"
    fake_uv = _prepare_bot_project(project)
    env_path = project / ".env"
    existing = env_path.read_text(encoding="utf-8")
    env_path.write_text(
        "\n".join(
            line for line in existing.splitlines()
            if not line.startswith("ONEBOT_API_ROOTS=")
        ) + "\n",
        encoding="utf-8",
    )

    result = _run(
        "start_bot.ps1", project, "-UvPath", str(fake_uv),
        input_text="\n",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    upgraded = env_path.read_text(encoding="utf-8")
    assert 'ONEBOT_API_ROOTS={"*":"http://127.0.0.1:3000/"}' in upgraded


@pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="Windows launcher")
def test_bot_launcher_displays_configured_snowluma_http_root(tmp_path):
    project = tmp_path / "bot custom http project"
    fake_uv = _prepare_bot_project(project)
    env_path = project / ".env"
    config = env_path.read_text(encoding="utf-8").replace(
        'ONEBOT_API_ROOTS={"*":"http://127.0.0.1:3000/"}',
        'ONEBOT_API_ROOTS={"*":"http://127.0.0.1:3900/"}',
    )
    env_path.write_text(config, encoding="utf-8")

    result = _run(
        "start_bot.ps1", project, "-UvPath", str(fake_uv),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert 'HTTP API：{"*":"http://127.0.0.1:3900/"}' in result.stdout
    assert "HTTP Server：127.0.0.1:3000" not in result.stdout


@pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="Windows launcher")
def test_bot_launcher_rejects_a_live_matching_process(tmp_path):
    project = tmp_path / "bot live project"
    fake_uv = _prepare_bot_project(project)
    runtime = project / ".runtime"
    runtime.mkdir()
    (runtime / "bot_state.json").write_text(
        json.dumps({
            "status": "running",
            "pid": os.getpid(),
            "process_identity": pid_identity(os.getpid()),
            "started_at": "2026-07-28T00:00:00+00:00",
            "git_commit": "test",
            "source_mtime_utc": "2100-01-01T00:00:00+00:00",
        }),
        encoding="utf-8",
    )

    result = _run(
        "start_bot.ps1", project, "-UvPath", str(fake_uv),
    )

    assert result.returncode == 1
    assert "BOT 已经运行，无需重复启动" in result.stdout


@pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="Windows launcher")
def test_bot_launcher_ignores_malformed_stale_state(tmp_path):
    project = tmp_path / "bot stale state project"
    fake_uv = _prepare_bot_project(project)
    runtime = project / ".runtime"
    runtime.mkdir()
    (runtime / "bot_state.json").write_text("{broken", encoding="utf-8")

    result = _run(
        "start_bot.ps1", project, "-UvPath", str(fake_uv),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "旧 BOT 状态文件无法解析" in result.stdout


@pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="Windows launcher")
def test_bot_launcher_ignores_incomplete_stale_state(tmp_path):
    project = tmp_path / "bot incomplete state project"
    fake_uv = _prepare_bot_project(project)
    runtime = project / ".runtime"
    runtime.mkdir()
    (runtime / "bot_state.json").write_text("{}", encoding="utf-8")

    result = _run(
        "start_bot.ps1", project, "-UvPath", str(fake_uv),
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="Windows launcher")
def test_collector_first_run_prepares_accounts_psk_and_bot_feed(tmp_path):
    project = tmp_path / "collector project"
    (project / "scripts" / "windows").mkdir(parents=True)
    (project / "configs").mkdir()
    shutil.copyfile(ROOT / ".env.example", project / ".env.example")
    (project / ".env").write_text(
        "TRADE_MESSAGE_TTL_SECONDS=17\n", encoding="utf-8")
    shutil.copyfile(
        ROOT / "configs" / "chat_collector_accounts.example.json",
        project / "configs" / "chat_collector_accounts.example.json",
    )
    shutil.copyfile(
        ROOT / "configs" / "chat_collector_shards.json",
        project / "configs" / "chat_collector_shards.json",
    )
    for name in (
        "scripts/run_chat_collector.py",
        "scripts/capture_chat_ticket.py",
        "scripts/auto_capture_chat_tickets.py",
        "src/plugins/riven_sniper/chat_collector/windows_auto_login.py",
        "scripts/windows/run_collector_supervisor.ps1",
        "pyproject.toml",
        "uv.lock",
    ):
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    (project / "psk_current.bin").write_bytes(bytes(range(64)))
    fake_uv = _fake_uv(
        project,
        'if /I "%~5"=="init" (\n'
        '  if not exist "%~7" mkdir "%~7"\n'
        '  copy /Y "configs\\chat_collector_accounts.example.json" '
        '"%~7\\accounts.json" >nul\n'
        ')',
    )

    result = subprocess.run(
        [
            POWERSHELL,
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(WINDOWS_SCRIPTS / "start_collector.ps1"),
            "-ProjectRoot",
            str(project),
            "-UvPath",
            str(fake_uv),
            "-PrepareOnly",
        ],
        cwd=ROOT,
        env=_with_fake_uv(project),
        input="Alpha\nBravo\nCharlie\nDelta\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    runtime = project / ".runtime" / "chat_collector"
    accounts = json.loads((runtime / "accounts.json").read_text(encoding="utf-8"))
    assert [accounts[slot]["nick"] for slot in "ABCD"] == [
        "Alpha", "Bravo", "Charlie", "Delta",
    ]
    assert (runtime / "psk_current.bin").read_bytes() == bytes(range(64))
    config = (project / ".env").read_text(encoding="utf-8")
    assert "IRC_FEED_ENABLED=true" in config
    assert "IRC_FEED_DIR=" in config
    assert "IRC_FEED_CHECKPOINT_PATH=" in config
    assert "SEND_QUEUE_MAXSIZE=1000" in config
    assert "TRADE_MESSAGE_TTL_SECONDS=17" in config
    assert "SEND_MAX_RETRIES=1" in config
    assert "SEND_RETRY_DELAY_SECONDS=2" in config


@pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="Windows launcher")
def test_collector_first_run_can_prepare_regional_seventeen_slots(tmp_path):
    project = tmp_path / "collector 17 project"
    (project / "scripts" / "windows").mkdir(parents=True)
    (project / "configs").mkdir()
    shutil.copyfile(ROOT / ".env.example", project / ".env.example")
    for name in (
        "chat_collector_accounts.example.json",
        "chat_collector_accounts_17.example.json",
        "chat_collector_shards.json",
        "chat_collector_shards_17.json",
    ):
        shutil.copyfile(ROOT / "configs" / name, project / "configs" / name)
    for name in (
        "scripts/run_chat_collector.py",
        "scripts/capture_chat_ticket.py",
        "scripts/auto_capture_chat_tickets.py",
        "src/plugins/riven_sniper/chat_collector/windows_auto_login.py",
        "scripts/windows/run_collector_supervisor.ps1",
        "pyproject.toml",
        "uv.lock",
    ):
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    (project / "psk_current.bin").write_bytes(bytes(range(64)))
    fake_uv = _fake_uv(
        project,
        'if /I "%~5"=="init" (\n'
        '  if not exist "%~7" mkdir "%~7"\n'
        '  copy /Y "configs\\chat_collector_accounts.example.json" '
        '"%~7\\accounts.json" >nul\n'
        ')\n'
        'if /I "%~5"=="mode" (\n'
        '  copy /Y "configs\\chat_collector_accounts_17.example.json" '
        '"%~8\\accounts_17.json" >nul\n'
        '  > "%~8\\collector_mode.json" echo {"mode":"17"}\n'
        ')',
    )
    nicks = [f"Collector{slot}" for slot in "ABCDEFGHIJKLMNOPQ"]

    result = subprocess.run(
        [
            POWERSHELL,
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(WINDOWS_SCRIPTS / "start_collector.ps1"),
            "-ProjectRoot",
            str(project),
            "-UvPath",
            str(fake_uv),
            "-CollectorMode",
            "17",
            "-PrepareOnly",
        ],
        cwd=ROOT,
        env=_with_fake_uv(project),
        input="\n".join(nicks) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    runtime = project / ".runtime" / "chat_collector"
    accounts = json.loads(
        (runtime / "accounts_17.json").read_text(encoding="utf-8")
    )
    assert [accounts[slot]["nick"] for slot in "ABCDEFGHIJKLMNOPQ"] == nicks, (
        result.stdout + result.stderr
    )
    assert json.loads(
        (runtime / "collector_mode.json").read_text(encoding="utf-8")
    )["mode"] == "17"
    assert "验证十七槽账号、单地区分片、自动快照关闭状态和 PSK" in result.stdout


def test_collector_stop_ui_waits_for_confirmed_process_exit():
    source = (WINDOWS_SCRIPTS / "start_collector.ps1").read_text(
        encoding="utf-8-sig"
    )

    assert "等待监督器与四槽进程完全退出" in source
    assert "-not $status.supervisor_running" in source
    assert "$state.process_alive" in source
    assert "采集已完全停止" in source
    assert "尚未发送的 IRC 消息已撤销" in source


@pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="Windows launcher")
def test_collector_stop_menu_reports_only_after_processes_exit(tmp_path):
    project = tmp_path / "collector stop project"
    runtime = project / ".runtime" / "chat_collector"
    (project / "scripts" / "windows").mkdir(parents=True)
    (project / "configs").mkdir()
    runtime.mkdir(parents=True)
    shutil.copyfile(ROOT / ".env.example", project / ".env.example")
    shutil.copyfile(
        ROOT / "configs" / "chat_collector_accounts.example.json",
        project / "configs" / "chat_collector_accounts.example.json",
    )
    shutil.copyfile(
        ROOT / "configs" / "chat_collector_shards.json",
        project / "configs" / "chat_collector_shards.json",
    )
    accounts = {slot: {"nick": f"Collector{slot}"} for slot in "ABCD"}
    (runtime / "accounts.json").write_text(
        json.dumps(accounts), encoding="utf-8",
    )
    for name in (
        "scripts/run_chat_collector.py",
        "scripts/capture_chat_ticket.py",
        "scripts/auto_capture_chat_tickets.py",
        "src/plugins/riven_sniper/chat_collector/windows_auto_login.py",
        "scripts/windows/run_collector_supervisor.ps1",
        "pyproject.toml",
        "uv.lock",
    ):
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    (project / "psk_current.bin").write_bytes(bytes(range(64)))
    running = {
        "supervisor_running": True,
        "supervisor_pid": 4321,
        "slots": {
            slot: {"status": "listening", "process_alive": True}
            for slot in "ABCD"
        },
    }
    stopped = {
        "supervisor_running": False,
        "supervisor_pid": None,
        "control": {"run_id": "run-a", "status": "stopped"},
        "delivery": {
            "run_id": "run-a", "accepting": False,
            "irc_queued": 0, "irc_inflight": 0,
        },
        "slots": {
            slot: {"status": "stopped", "process_alive": False}
            for slot in "ABCD"
        },
    }
    fake_uv = _fake_uv(
        project,
        'if /I "%~5"=="stop" type nul > ".runtime\\chat_collector\\fake.stopped"\n'
        'if /I "%~5"=="status" (\n'
        '  if exist ".runtime\\chat_collector\\fake.stopped" (\n'
        f"    echo {json.dumps(stopped, separators=(',', ':'))}\n"
        "  ) else (\n"
        f"    echo {json.dumps(running, separators=(',', ':'))}\n"
        "  )\n"
        ")",
    )

    result = _run(
        "start_collector.ps1",
        project,
        "-UvPath",
        str(fake_uv),
        input_text="4\n",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "采集已完全停止，尚未发送的 IRC 消息已撤销" in result.stdout
    assert "监督器：未运行" in result.stdout


@pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="Windows launcher")
def test_collector_menu_tolerates_uv_warnings_and_uses_chinese_status(tmp_path):
    project = tmp_path / "collector status project"
    runtime = project / ".runtime" / "chat_collector"
    (project / "scripts" / "windows").mkdir(parents=True)
    (project / "configs").mkdir()
    runtime.mkdir(parents=True)
    shutil.copyfile(ROOT / ".env.example", project / ".env.example")
    shutil.copyfile(
        ROOT / "configs" / "chat_collector_accounts.example.json",
        project / "configs" / "chat_collector_accounts.example.json",
    )
    shutil.copyfile(
        ROOT / "configs" / "chat_collector_shards.json",
        project / "configs" / "chat_collector_shards.json",
    )
    accounts = {slot: {"nick": f"Collector{slot}"} for slot in "ABCD"}
    (runtime / "accounts.json").write_text(
        json.dumps(accounts), encoding="utf-8",
    )
    for name in (
        "scripts/run_chat_collector.py",
        "scripts/capture_chat_ticket.py",
        "scripts/auto_capture_chat_tickets.py",
        "src/plugins/riven_sniper/chat_collector/windows_auto_login.py",
        "scripts/windows/run_collector_supervisor.ps1",
        "pyproject.toml",
        "uv.lock",
    ):
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    (project / "psk_current.bin").write_bytes(bytes(range(64)))
    status = {
        "supervisor_running": True,
        "supervisor_pid": 4321,
        "slots": {
            slot: {"status": "listening", "process_alive": True}
            for slot in "ABCD"
        },
    }
    fake_uv = _fake_uv(
        project,
        'if /I "%~5"=="status" (\n'
        "  echo uv warning 1>&2\n"
        f"  echo {json.dumps(status, separators=(',', ':'))}\n"
        ")",
    )

    result = _run(
        "start_collector.ps1",
        project,
        "-UvPath",
        str(fake_uv),
        input_text="0\n",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "采集监督器已经在运行" in result.stdout
    assert "槽 A：正常采集中 / 进程运行中" in result.stdout


@pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="Windows launcher")
def test_collector_menu_can_arm_manual_snapshot_for_selected_slot(tmp_path):
    project = tmp_path / "collector snapshot project"
    runtime = project / ".runtime" / "chat_collector"
    (project / "scripts" / "windows").mkdir(parents=True)
    (project / "configs").mkdir()
    runtime.mkdir(parents=True)
    shutil.copyfile(ROOT / ".env.example", project / ".env.example")
    shutil.copyfile(
        ROOT / "configs" / "chat_collector_accounts.example.json",
        project / "configs" / "chat_collector_accounts.example.json",
    )
    shutil.copyfile(
        ROOT / "configs" / "chat_collector_shards.json",
        project / "configs" / "chat_collector_shards.json",
    )
    accounts = {slot: {"nick": f"Collector{slot}"} for slot in "ABCD"}
    (runtime / "accounts.json").write_text(
        json.dumps(accounts), encoding="utf-8",
    )
    for name in (
        "scripts/run_chat_collector.py",
        "scripts/capture_chat_ticket.py",
        "scripts/auto_capture_chat_tickets.py",
        "src/plugins/riven_sniper/chat_collector/windows_auto_login.py",
        "scripts/windows/run_collector_supervisor.ps1",
        "pyproject.toml",
        "uv.lock",
    ):
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    (project / "psk_current.bin").write_bytes(bytes(range(64)))
    status = {
        "supervisor_running": True,
        "supervisor_pid": 4321,
        "slots": {
            slot: {"status": "listening", "process_alive": True}
            for slot in "ABCD"
        },
    }
    fake_uv = _fake_uv(
        project,
        'if /I "%~5"=="status" (\n'
        f"  echo {json.dumps(status, separators=(',', ':'))}\n"
        ")\n"
        'if /I "%~5"=="snapshot" (\n'
        "  echo %* > snapshot_args.txt\n"
        '  echo {"request_id":"snapshot-test","slot":"B",'
        '"channels":["#T_FR"],"status":"armed"}\n'
        ")",
    )

    result = _run(
        "start_collector.ps1",
        project,
        "-UvPath",
        str(fake_uv),
        input_text="6\nb\n0\n",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1. 全自动为尚未运行的槽登录取票（推荐）" in result.stdout
    assert "6. 手动执行一次成员快照" in result.stdout
    assert "7. 手动为尚未运行的槽逐个取票" in result.stdout
    assert "8. 配置或更新全自动登录凭据" in result.stdout
    assert "槽 B 手动快照请求已装载" in result.stdout
    snapshot_args = (project / "snapshot_args.txt").read_text(encoding="utf-8")
    assert "snapshot --slot B --runtime-root" in snapshot_args
