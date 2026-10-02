from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import auto_capture_chat_tickets as automatic
from src.plugins.riven_sniper.chat_collector import windows_auto_login as login
from src.plugins.riven_sniper.chat_collector.supervisor import CollectorLayout


def test_credential_store_roundtrip_never_writes_plaintext(tmp_path, monkeypatch):
    monkeypatch.setattr(
        login, "_dpapi_protect", lambda value: b"cipher:" + value[::-1]
    )
    monkeypatch.setattr(
        login,
        "_dpapi_unprotect",
        lambda value: value.removeprefix(b"cipher:")[::-1],
    )
    path = tmp_path / "credentials.bin"
    expected = {
        "4": {
            "A": login.LoginCredential(
                "alpha@example.invalid", "password-alpha"
            ),
        },
        "17": {
            "Q": login.LoginCredential(
                "queue@example.invalid", "password-queue"
            ),
        },
    }

    login.save_credentials(path, expected)

    stored = path.read_bytes()
    assert b"alpha@example.invalid" not in stored
    assert b"password-alpha" not in stored
    assert login.load_credentials(path) == expected


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_windows_dpapi_credential_roundtrip(tmp_path):
    path = tmp_path / "credentials.bin"
    expected = {
        "4": {
            "A": login.LoginCredential(
                "dpapi-test@example.invalid", "dummy-dpapi-password"
            )
        }
    }

    login.save_credentials(path, expected)

    assert login.load_credentials(path) == expected
    assert b"dpapi-test@example.invalid" not in path.read_bytes()


def test_internal_auto_login_selects_email_and_restores_exact_file(tmp_path):
    path = tmp_path / "EE.cfg"
    original = (
        b"Graphics.TextureQuality=HIGH\r\n"
        b"Client.Email=previous@example.invalid\r\n"
    )
    path.write_bytes(original)

    lease = login.prepare_internal_auto_login(
        login.LoginCredential("new@example.invalid", "secret-password"),
        config_path=path,
    )

    injected = path.read_bytes()
    assert b"Client.Email=new@example.invalid\r\n" in injected
    assert b"Client.AutoLogin=0\r\n" in injected
    assert b"Client.PlayerHash=" not in injected
    assert b"secret-password" not in injected

    lease.restore()
    assert path.read_bytes() == original


def test_internal_auto_login_recovers_after_interrupted_run(tmp_path, monkeypatch):
    monkeypatch.setattr(
        login, "_dpapi_protect", lambda value: b"cipher:" + value[::-1]
    )
    monkeypatch.setattr(
        login,
        "_dpapi_unprotect",
        lambda value: value.removeprefix(b"cipher:")[::-1],
    )
    path = tmp_path / "EE.cfg"
    recovery = tmp_path / "recovery.bin"
    original = b"Client.Email=previous@example.invalid\r\n"
    path.write_bytes(original)

    lease = login.prepare_internal_auto_login(
        login.LoginCredential("new@example.invalid", "secret-password"),
        config_path=path,
        recovery_path=recovery,
    )
    assert recovery.exists()
    assert path.read_bytes() != original

    lease.restore(keep_recovery=True)
    assert path.read_bytes() == original
    assert recovery.exists()

    path.write_bytes(b"Client.Email=changed-after-restore@example.invalid\r\n")
    assert login.recover_internal_auto_login(
        recovery,
        config_path=path,
    )
    assert path.read_bytes() == original
    assert not recovery.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows input API")
def test_windows_login_driver_initializes():
    login.WindowsLoginDriver()


def test_legacy_credential_store_requires_reconfiguration(tmp_path, monkeypatch):
    monkeypatch.setattr(login, "_dpapi_unprotect", lambda value: value)
    path = tmp_path / "credentials.bin"
    path.write_bytes(
        login._CREDENTIAL_MAGIC
        + json.dumps({
            "version": 1,
            "slots": {
                "A": {
                    "username": "legacy@example.invalid",
                    "password": "legacy-password",
                }
            },
        }).encode("utf-8")
    )

    with pytest.raises(login.CredentialStoreError, match="没有区分 4/17"):
        login.load_credentials(path)


def test_client_frame_uses_centered_sixteen_by_nine_safe_area():
    wide = login.ClientFrame(
        hwnd=1, left=100, top=50, width=2560, height=1368
    )
    sixteen_by_nine = login.ClientFrame(
        hwnd=2, left=1300, top=650, width=854, height=480
    )
    live_resized = login.ClientFrame(
        hwnd=3, left=1132, top=332, width=1622, height=1052
    )

    assert wide.point((0.723, 0.531)) == (1922, 776)
    assert sixteen_by_nine.point((0.723, 0.531)) == (1917, 905)
    assert sixteen_by_nine.relative_point((0.75, 0.93)) == (1940, 1096)
    assert live_resized.safe_rect() == (1132, 402, 1622, 912)
    assert live_resized.point(login._EMAIL_UNLOCK_POINT) == (2456, 886)
    assert live_resized.point(login._PASSWORD_POINT) == (2305, 926)
    assert live_resized.point(login._LOGIN_POINT) == (2305, 963)


def _login_page_fixture(
    *,
    size: tuple[int, int] = (854, 480),
    missing_zone: int | None = None,
    accent: tuple[int, int, int] = (220, 70, 75),
) -> Image.Image:
    image = Image.new("RGB", size, (5, 18, 35))
    draw = ImageDraw.Draw(image)
    frame = login.ClientFrame(0, 0, 0, *image.size)
    left, top, width, height = frame.safe_rect()
    for index, (x1, y1, x2, y2, _minimum) in enumerate(
        login._LOGIN_SIGNATURE_ZONES
    ):
        if index == missing_zone:
            continue
        start_x = left + round(width * x1) + 2
        start_y = top + round(height * y1) + 2
        draw.rectangle(
            (start_x, start_y, start_x + 5, start_y + 3),
            fill=accent,
        )
    return image


def test_login_signature_requires_all_three_login_regions():
    assert login._login_signature_present(_login_page_fixture())
    assert not login._login_signature_present(
        _login_page_fixture(missing_zone=1)
    )


def test_login_signature_supports_narrow_resized_window():
    assert login._login_signature_present(
        _login_page_fixture(size=(1082, 703))
    )


def test_login_signature_accepts_current_gold_accent():
    assert login._login_signature_present(
        _login_page_fixture(accent=(190, 169, 102))
    )


def test_activate_attaches_to_foreground_input_queue():
    class FakeUser32:
        def __init__(self) -> None:
            self.foreground = 99
            self.attach_calls: list[tuple[int, int, bool]] = []

        def ShowWindow(self, _hwnd: int, _command: int) -> bool:
            return True

        def GetForegroundWindow(self) -> int:
            return self.foreground

        def GetWindowThreadProcessId(
            self, _hwnd: int, _owner: object
        ) -> int:
            return 17

        def AttachThreadInput(
            self, current: int, foreground: int, attach: bool
        ) -> bool:
            self.attach_calls.append((current, foreground, attach))
            return True

        def BringWindowToTop(self, _hwnd: int) -> bool:
            return True

        def SetForegroundWindow(self, hwnd: int) -> bool:
            self.foreground = hwnd
            return True

    class FakeKernel32:
        @staticmethod
        def GetCurrentThreadId() -> int:
            return 23

    driver = login.WindowsLoginDriver.__new__(login.WindowsLoginDriver)
    driver.user32 = FakeUser32()
    driver.kernel32 = FakeKernel32()

    driver._activate(10)

    assert driver.user32.attach_calls == [
        (23, 17, True),
        (23, 17, False),
    ]


def test_launcher_signature_requires_enabled_red_start_button():
    ready = Image.new("RGB", (1024, 640), (20, 25, 30))
    disabled = ready.copy()
    draw = ImageDraw.Draw(ready)
    left, top, right, bottom = login._LAUNCHER_READY_ZONE
    draw.rectangle((
        round(ready.width * left),
        round(ready.height * top),
        round(ready.width * right),
        round(ready.height * bottom),
    ), fill=(220, 55, 25))

    assert login._launcher_ready_signature_present(ready)
    assert not login._launcher_ready_signature_present(disabled)


def test_launcher_driver_clicks_ready_start_button(monkeypatch):
    driver = login.WindowsLoginDriver.__new__(login.WindowsLoginDriver)
    frame = login.ClientFrame(10, 100, 200, 1024, 640)
    image = Image.new("RGB", (1024, 640), (20, 25, 30))
    draw = ImageDraw.Draw(image)
    left, top, right, bottom = login._LAUNCHER_READY_ZONE
    draw.rectangle((
        round(image.width * left),
        round(image.height * top),
        round(image.width * right),
        round(image.height * bottom),
    ), fill=(220, 55, 25))
    clicks: list[tuple[int, int]] = []

    monkeypatch.setattr(driver, "_window_handles", lambda _pid: [10])
    monkeypatch.setattr(driver, "_raw_client_frame", lambda _hwnd: frame)
    monkeypatch.setattr(driver, "_activate", lambda _hwnd: None)
    monkeypatch.setattr(
        driver, "_assert_target", lambda _hwnd, _pid, **_kwargs: None
    )
    monkeypatch.setattr(driver, "_capture_client", lambda _frame: image)
    monkeypatch.setattr(driver, "_click", clicks.append)

    assert driver.wait_for_launcher_and_start_game(
        123, timeout=0.1, stable_observations=1
    ) == frame
    assert clicks == [frame.relative_point(login._LAUNCHER_START_POINT)]


def test_launcher_process_ids_match_only_requested_executable(
    tmp_path, monkeypatch,
):
    expected = tmp_path / "Warframe" / "Tools" / "Launcher.exe"
    other = tmp_path / "Other" / "Launcher.exe"
    monkeypatch.setattr(login, "_process_ids_named", lambda _name: (10, 20))
    monkeypatch.setattr(
        login,
        "_query_process_executable",
        lambda pid: expected if pid == 10 else other,
    )

    assert login.launcher_process_ids(expected) == (10,)


def test_standalone_launcher_paths_use_registry_and_local_app_data(
    tmp_path, monkeypatch,
):
    launcher_exe = tmp_path / "LauncherHome" / "Tools" / "Launcher.exe"
    download_dir = tmp_path / "GameData" / "Downloaded"
    local_app_data = tmp_path / "AppData" / "Local"

    class FakeKey:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class FakeWinreg:
        HKEY_CURRENT_USER = object()

        @staticmethod
        def OpenKey(_root, _name):
            return FakeKey()

        @staticmethod
        def QueryValueEx(_key, name):
            return {
                "LauncherExe": (str(launcher_exe), 1),
                "DownloadDir": (str(download_dir), 1),
            }[name]

    monkeypatch.setitem(sys.modules, "winreg", FakeWinreg)
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))

    assert login._standalone_launcher_paths() == (
        launcher_exe,
        download_dir / "Public" / "Tools" / "Launcher.exe",
        local_app_data
        / "Warframe"
        / "Downloaded"
        / "Public"
        / "Tools"
        / "Launcher.exe",
    )


def test_discover_launcher_prefers_standalone_over_steam(
    tmp_path, monkeypatch,
):
    standalone = (
        tmp_path
        / "AppData"
        / "Local"
        / "Warframe"
        / "Downloaded"
        / "Public"
        / "Tools"
        / "Launcher.exe"
    )
    steam = (
        tmp_path
        / "SteamLibrary"
        / "steamapps"
        / "common"
        / "Warframe"
        / "Tools"
        / "Launcher.exe"
    )
    standalone.parent.mkdir(parents=True)
    standalone.touch()
    steam.parent.mkdir(parents=True)
    steam.touch()
    monkeypatch.setattr(login, "_require_windows", lambda: None)
    monkeypatch.setattr(login, "warframe_process_ids", lambda: ())
    monkeypatch.setattr(
        login, "_standalone_launcher_paths", lambda: (standalone,)
    )
    steam_scans = 0

    def steam_launcher_paths():
        nonlocal steam_scans
        steam_scans += 1
        return (steam,)

    monkeypatch.setattr(login, "_steam_launcher_paths", steam_launcher_paths)

    discovered = login.discover_launcher()

    assert discovered == login.WarframeLauncher(
        standalone.resolve(), login.LauncherKind.STANDALONE
    )
    assert steam_scans == 1


@pytest.mark.parametrize(
    ("relative_path", "expected_kind"),
    [
        (
            "SteamLibrary/steamapps/common/Warframe/Tools/Launcher.exe",
            login.LauncherKind.STEAM,
        ),
        (
            "Warframe/Downloaded/Public/Tools/Launcher.exe",
            login.LauncherKind.STANDALONE,
        ),
    ],
)
def test_identify_launcher_distinguishes_installation_layout(
    tmp_path, monkeypatch, relative_path, expected_kind,
):
    launcher = tmp_path / relative_path
    monkeypatch.setattr(login, "_steam_launcher_paths", lambda: ())

    assert login.identify_launcher(launcher) == login.WarframeLauncher(
        launcher.resolve(), expected_kind
    )


@pytest.mark.parametrize(
    ("kind", "expected_arguments"),
    [
        (login.LauncherKind.STANDALONE, ()),
        (login.LauncherKind.STEAM, login.STEAM_LAUNCH_ARGUMENTS),
    ],
)
def test_launch_warframe_uses_distribution_specific_arguments(
    tmp_path, monkeypatch, kind, expected_arguments,
):
    launcher = tmp_path / "Warframe" / "Tools" / "Launcher.exe"
    launcher.parent.mkdir(parents=True)
    launcher.touch()
    calls = []

    def fake_popen(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return object()

    monkeypatch.setattr(login.subprocess, "Popen", fake_popen)

    login.launch_warframe(login.WarframeLauncher(launcher, kind))

    arguments, kwargs = calls[0]
    assert arguments == [str(launcher), *expected_arguments]
    assert kwargs["cwd"] == launcher.parent.parent


def test_wait_for_window_ignores_unsupported_transition_window(monkeypatch):
    driver = login.WindowsLoginDriver.__new__(login.WindowsLoginDriver)
    monkeypatch.setattr(driver, "_window_handles", lambda _pid: [10, 20])

    def frame(hwnd):
        if hwnd == 10:
            raise login.AutoLoginError("过渡窗口")
        return login.ClientFrame(hwnd, 0, 0, 1280, 720)

    monkeypatch.setattr(driver, "client_frame", frame)

    assert driver.wait_for_window(123, timeout=0.1) == 20


def test_internal_login_wait_accepts_small_game_window(monkeypatch):
    driver = login.WindowsLoginDriver.__new__(login.WindowsLoginDriver)
    monkeypatch.setattr(driver, "_window_handles", lambda _pid: [10])
    monkeypatch.setattr(
        driver,
        "_raw_client_frame",
        lambda hwnd: login.ClientFrame(hwnd, 0, 0, 640, 360),
    )

    assert driver.wait_for_process_window(
        123,
        timeout=0.1,
    ) == login.ClientFrame(10, 0, 0, 640, 360)


def test_wait_for_internal_login_ui_uses_process_markers(monkeypatch):
    driver = login.WindowsLoginDriver.__new__(login.WindowsLoginDriver)
    frame = login.ClientFrame(10, 0, 0, 640, 360)
    monkeypatch.setattr(
        driver,
        "wait_for_process_window",
        lambda _pid, **_kwargs: frame,
    )
    monkeypatch.setattr(
        login,
        "find_memory_markers",
        lambda _pid, markers: frozenset(markers),
    )
    monkeypatch.setattr(driver, "_activate", lambda _hwnd: None)
    monkeypatch.setattr(driver, "_raw_client_frame", lambda _hwnd: frame)
    monkeypatch.setattr(driver, "_capture_client", lambda _frame: object())
    monkeypatch.setattr(login, "_login_signature_present", lambda _image: True)
    monkeypatch.setattr(login.time, "sleep", lambda _seconds: None)

    assert driver.wait_for_internal_login_ui(
        123,
        timeout=0.1,
    ) == frame


def test_internal_login_waits_for_visual_page_before_memory_scan(monkeypatch):
    driver = login.WindowsLoginDriver.__new__(login.WindowsLoginDriver)
    frame = login.ClientFrame(10, 0, 0, 640, 360)
    scans = []
    observations = iter((False, True, True))
    monkeypatch.setattr(
        driver,
        "wait_for_process_window",
        lambda _pid, **_kwargs: frame,
    )
    monkeypatch.setattr(driver, "_activate", lambda _hwnd: None)
    monkeypatch.setattr(driver, "_raw_client_frame", lambda _hwnd: frame)
    monkeypatch.setattr(driver, "_capture_client", lambda _frame: object())
    monkeypatch.setattr(
        login,
        "_login_signature_present",
        lambda _image: next(observations),
    )
    monkeypatch.setattr(
        login,
        "find_memory_markers",
        lambda _pid, markers: scans.append(markers) or frozenset(markers),
    )
    monkeypatch.setattr(login.time, "sleep", lambda _seconds: None)

    assert driver.wait_for_internal_login_ui(
        123,
        timeout=0.1,
    ) == frame
    assert scans == [login._INTERNAL_LOGIN_UI_MARKERS]


def test_submit_internal_credentials_uses_default_password_focus(monkeypatch):
    driver = login.WindowsLoginDriver.__new__(login.WindowsLoginDriver)
    frame = login.ClientFrame(10, 0, 0, 640, 360)
    calls = []
    monkeypatch.setattr(
        driver,
        "wait_for_internal_login_ui",
        lambda _pid, **_kwargs: frame,
    )
    monkeypatch.setattr(
        driver,
        "_activate",
        lambda hwnd: calls.append(("activate", hwnd)),
    )
    monkeypatch.setattr(
        driver,
        "_clear_focused_field_target",
        lambda hwnd, pid: calls.append(("clear", hwnd, pid)),
    )
    monkeypatch.setattr(
        driver,
        "_type_text_target",
        lambda hwnd, pid, value: calls.append(("type", hwnd, pid, value)),
    )
    monkeypatch.setattr(
        driver,
        "_press_enter_target",
        lambda hwnd, pid: calls.append(("enter", hwnd, pid)),
    )
    monkeypatch.setattr(login.time, "sleep", lambda _seconds: None)

    credential = login.LoginCredential("user@example.invalid", "password")
    assert driver.submit_internal_credentials(
        123,
        credential,
        window_timeout=30,
        settle_seconds=0,
    ) == frame
    assert calls == [
        ("activate", 10),
        ("clear", 10, 123),
        ("type", 10, 123, "password"),
        ("enter", 10, 123),
    ]


def test_target_validation_rejects_stolen_focus_and_overlay():
    class FakeUser32:
        foreground = 10
        hit = 10

        def GetWindowThreadProcessId(self, _hwnd, owner):
            owner._obj.value = 123

        def GetForegroundWindow(self):
            return self.foreground

        def WindowFromPoint(self, _point):
            return self.hit

        def GetAncestor(self, hwnd, _flag):
            return hwnd

    driver = login.WindowsLoginDriver.__new__(login.WindowsLoginDriver)
    driver.user32 = FakeUser32()
    driver._assert_target(10, 123, point=(100, 100))

    driver.user32.foreground = 99
    with pytest.raises(login.AutoLoginError, match="不再是前台窗口"):
        driver._assert_target(10, 123)

    driver.user32.foreground = 10
    driver.user32.hit = 99
    with pytest.raises(login.AutoLoginError, match="其他窗口遮挡"):
        driver._assert_target(10, 123, point=(100, 100))


def test_submit_credentials_clicks_login_and_waits_for_page_transition(
    monkeypatch,
):
    driver = login.WindowsLoginDriver.__new__(login.WindowsLoginDriver)
    frame = login.ClientFrame(10, 100, 200, 1280, 720)
    events: list[tuple[object, ...]] = []
    sleeps: list[float] = []
    monkeypatch.setattr(driver, "wait_for_window", lambda _pid, **_kwargs: 10)
    monkeypatch.setattr(
        driver,
        "wait_for_login_page",
        lambda _pid, **_kwargs: (10, frame),
    )
    monkeypatch.setattr(
        driver,
        "_click_target",
        lambda hwnd, pid, point: events.append(("click", hwnd, pid, point)),
    )
    monkeypatch.setattr(
        driver,
        "_clear_focused_field_target",
        lambda hwnd, pid: events.append(("clear", hwnd, pid)),
    )
    monkeypatch.setattr(
        driver,
        "_type_text_target",
        lambda hwnd, pid, value: events.append(("type", hwnd, pid, value)),
    )
    monkeypatch.setattr(
        driver,
        "_press_shift_tab_target",
        lambda hwnd, pid: events.append(("shift-tab", hwnd, pid)),
    )
    monkeypatch.setattr(
        driver,
        "wait_for_login_transition",
        lambda pid, hwnd: events.append(("transition", hwnd, pid)),
    )
    monkeypatch.setattr(login.time, "sleep", sleeps.append)

    credential = login.LoginCredential("user@example.invalid", "password")
    assert driver.submit_credentials(
        123,
        credential,
        window_timeout=1,
        settle_seconds=0,
    ) == frame
    assert events == [
        ("click", 10, 123, frame.point(login._EMAIL_UNLOCK_POINT)),
        ("click", 10, 123, frame.point(login._PASSWORD_POINT)),
        ("shift-tab", 10, 123),
        ("clear", 10, 123),
        ("type", 10, 123, "user@example.invalid"),
        ("click", 10, 123, frame.point(login._PASSWORD_POINT)),
        ("clear", 10, 123),
        ("type", 10, 123, "password"),
        ("click", 10, 123, frame.point(login._LOGIN_POINT)),
        ("transition", 10, 123),
    ]
    assert sleeps == [0, 1.0, 0.15, 0.15, 0.15, 2.0]


def test_shift_tab_uses_keyboard_scan_codes_for_game_input():
    driver = login.WindowsLoginDriver.__new__(login.WindowsLoginDriver)
    batches: list[list[object]] = []
    driver._assert_target = lambda _hwnd, _pid: None
    driver._send = batches.append

    driver._press_shift_tab_target(10, 123)

    assert [entry.ki.wVk for entry in batches[0]] == [0, 0, 0, 0]
    assert [entry.ki.wScan for entry in batches[0]] == [
        0x2A,
        0x0F,
        0x0F,
        0x2A,
    ]
    assert [entry.ki.dwFlags for entry in batches[0]] == [
        0x0008,
        0x0008,
        0x0008 | 0x0002,
        0x0008 | 0x0002,
    ]


def test_clear_field_uses_keyboard_scan_codes():
    driver = login.WindowsLoginDriver.__new__(login.WindowsLoginDriver)
    batches: list[list[object]] = []
    driver._send = batches.append

    driver._clear_focused_field()

    assert [entry.ki.wVk for entry in batches[0]] == [0, 0, 0, 0, 0, 0]
    assert [entry.ki.wScan for entry in batches[0]] == [
        0x1D,
        0x1E,
        0x1E,
        0x1D,
        0x0E,
        0x0E,
    ]
    assert [entry.ki.dwFlags for entry in batches[0]] == [
        0x0008,
        0x0008,
        0x0008 | 0x0002,
        0x0008 | 0x0002,
        0x0008,
        0x0008 | 0x0002,
    ]


def test_click_holds_mouse_button_before_release(monkeypatch):
    class FakeUser32:
        @staticmethod
        def SetCursorPos(_x: int, _y: int) -> bool:
            return True

    driver = login.WindowsLoginDriver.__new__(login.WindowsLoginDriver)
    driver.user32 = FakeUser32()
    batches: list[list[object]] = []
    sleeps: list[float] = []
    monkeypatch.setattr(driver, "_send", batches.append)
    monkeypatch.setattr(login.time, "sleep", sleeps.append)

    driver._click((100, 200))

    assert [batch[0].mi.dwFlags for batch in batches] == [0x0002, 0x0004]
    assert sleeps == [login._MOUSE_CLICK_HOLD_SECONDS]


def test_game_cleanup_falls_back_when_creation_identity_is_unreadable(
    monkeypatch,
):
    calls: list[tuple[int, float]] = []
    monkeypatch.setattr(automatic, "warframe_process_ids", lambda: (123,))
    monkeypatch.setattr(automatic, "pid_matches", lambda _pid, _identity: False)
    monkeypatch.setattr(
        automatic,
        "terminate_warframe_process",
        lambda pid, *, wait_timeout: calls.append((pid, wait_timeout)) or True,
    )

    assert automatic._terminate_owned_game(123, "creation")
    assert calls == [(123, 5.0)]


def test_slot_handoff_waits_until_all_game_processes_exit(monkeypatch):
    observations = iter(((123,), (123,), ()))
    monkeypatch.setattr(
        automatic,
        "warframe_process_ids",
        lambda: next(observations),
    )
    monkeypatch.setattr(automatic.time, "sleep", lambda _seconds: None)

    assert automatic._wait_for_no_warframe_processes(timeout=0.1)


def test_configure_credentials_keeps_four_and_seventeen_modes_separate(
    tmp_path, monkeypatch,
):
    layout = CollectorLayout(tmp_path / "runtime")
    layout.ensure()
    usernames = iter(("four@example.invalid", "seventeen@example.invalid"))
    passwords = iter(("four-password", "seventeen-password"))
    monkeypatch.setattr(login, "_dpapi_protect", lambda value: value)
    monkeypatch.setattr(login, "_dpapi_unprotect", lambda value: value)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(usernames))
    monkeypatch.setattr(automatic.getpass, "getpass", lambda _prompt: next(passwords))

    automatic.configure_credentials(
        layout, "4", ("A",), update_existing=False
    )
    automatic.configure_credentials(
        layout, "17", ("A",), update_existing=False
    )

    stored = login.load_credentials(login.credential_store_path(layout.root))
    assert stored["4"]["A"].username == "four@example.invalid"
    assert stored["17"]["A"].username == "seventeen@example.invalid"


def test_steam_library_paths_reads_secondary_library(tmp_path):
    steam = tmp_path / "steam"
    library = tmp_path / "secondary"
    config = steam / "config"
    config.mkdir(parents=True)
    config.joinpath("libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n  "1"\n  {\n'
        f'    "path" "{str(library).replace(chr(92), chr(92) * 2)}"\n'
        "  }\n}\n",
        encoding="utf-8",
    )

    assert login._steam_library_paths(steam) == (steam, library)


def test_wait_for_new_game_process_ignores_existing_pid():
    observations = iter(((100,), (100,), (100, 200)))

    assert login.wait_for_new_game_process(
        {100},
        timeout=1,
        poll_interval=0.001,
        pid_provider=lambda: next(observations),
    ) == 200


def test_automatic_capture_runs_pending_slots_sequentially(tmp_path, monkeypatch):
    layout = CollectorLayout(tmp_path / "runtime")
    layout.ensure()
    layout.accounts_path.write_text(
        json.dumps({
            slot: {"nick": f"Collector{slot}"}
            for slot in "ABCD"
        }),
        encoding="utf-8",
    )
    credentials = {
        slot: login.LoginCredential(
            f"{slot.lower()}@example.invalid", f"password-{slot}"
        )
        for slot in "AB"
    }
    process_ids = iter((4101, 4102))
    launcher_ids = iter((3101, 3102))
    started: list[tuple[int, float]] = []
    prepared: list[str] = []
    submitted: list[tuple[int, str]] = []
    restored: list[tuple[str, bool]] = []
    captured: list[tuple[str, str, int]] = []

    class FakeDriver:
        def wait_for_launcher_and_start_game(self, pid, *, timeout):
            started.append((pid, timeout))
            return login.ClientFrame(pid, 0, 0, 1024, 640)

        def submit_internal_credentials(
            self, pid, credential, *, window_timeout, settle_seconds,
        ):
            assert window_timeout == 30
            assert settle_seconds == 0
            submitted.append((pid, credential.username))
            return login.ClientFrame(pid, 0, 0, 1280, 720)

    class FakeInternalLogin:
        def __init__(self, username):
            self.username = username

        def restore(self, *, keep_recovery=False):
            restored.append((self.username, keep_recovery))

    monkeypatch.setattr(automatic, "_pending_slots", lambda _layout: ("A", "B"))
    monkeypatch.setattr(
        automatic,
        "configure_credentials",
        lambda *_args, **_kwargs: credentials,
    )
    monkeypatch.setattr(automatic, "warframe_process_ids", lambda: ())
    monkeypatch.setattr(automatic, "launcher_process_ids", lambda _path: ())
    launcher = login.WarframeLauncher(
        Path("Launcher.exe"), login.LauncherKind.STANDALONE
    )
    monkeypatch.setattr(automatic, "discover_launcher", lambda _path: launcher)
    monkeypatch.setattr(automatic, "WindowsLoginDriver", FakeDriver)
    monkeypatch.setattr(
        automatic,
        "prepare_internal_auto_login",
        lambda credential, **_kwargs: (
            prepared.append(credential.username)
            or FakeInternalLogin(credential.username)
        ),
    )
    monkeypatch.setattr(
        automatic,
        "launch_warframe",
        lambda _path: type("FakeProcess", (), {"pid": next(launcher_ids)})(),
    )
    monkeypatch.setattr(
        automatic,
        "wait_for_new_game_process",
        lambda *_args, **_kwargs: next(process_ids),
    )
    monkeypatch.setattr(automatic, "pid_identity", lambda pid: f"identity-{pid}")
    monkeypatch.setattr(automatic, "_wait_for_irc", lambda *_args, **_kwargs: None)

    def capture(slot, nick, _layout, *, pid):
        captured.append((slot, nick, pid))
        return 0

    monkeypatch.setattr(automatic, "capture_ticket", capture)

    assert automatic.run_automatic_capture(
        layout,
        launcher=None,
        launch_timeout=30,
        login_timeout=60,
        login_settle_seconds=0,
    ) == 0
    assert started == [(3101, 30), (3102, 30)]
    assert prepared == ["a@example.invalid", "b@example.invalid"]
    assert submitted == [
        (4101, "a@example.invalid"),
        (4102, "b@example.invalid"),
    ]
    assert restored == [
        ("a@example.invalid", True),
        ("a@example.invalid", False),
        ("b@example.invalid", True),
        ("b@example.invalid", False),
    ]
    assert captured == [
        ("A", "CollectorA", 4101),
        ("B", "CollectorB", 4102),
    ]
