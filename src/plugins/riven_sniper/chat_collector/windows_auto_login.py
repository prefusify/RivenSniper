"""Windows 下的 Warframe 登录界面自动化与本机凭据存储。"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping

from PIL import Image, ImageGrab

from .nonce_scan import find_memory_markers


WARFRAME_PROCESS_NAME = "warframe.x64.exe"
LAUNCHER_PROCESS_NAME = "launcher.exe"
STEAM_LAUNCH_ARGUMENTS = ("-cluster:public", "-registry:Steam")
CREDENTIAL_STORE_FILENAME = "auto_login_credentials.bin"
INTERNAL_LOGIN_RECOVERY_FILENAME = "auto_login_ee_cfg_recovery.bin"
WARFRAME_CONFIG_FILENAME = "EE.cfg"

_CREDENTIAL_MAGIC = b"RSCRED1\x00"
_INTERNAL_LOGIN_RECOVERY_MAGIC = b"RSEECFG1\x00"
_DPAPI_ENTROPY = b"RivenSniper chat collector auto login v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1

# 游戏登录页不暴露 UI Automation 控件。以下点位按客户区比例定位，来自同一登录布局。
_EMAIL_UNLOCK_POINT = (0.816, 0.531)
_PASSWORD_POINT = (0.723, 0.575)
_LOGIN_POINT = (0.723, 0.615)
_LAUNCHER_START_POINT = (0.75, 0.93)
_MOUSE_CLICK_HOLD_SECONDS = 0.1
_EMAIL_UNLOCK_SETTLE_SECONDS = 1.0
_FIELD_FOCUS_SETTLE_SECONDS = 0.15
_LOGIN_SUBMIT_SETTLE_SECONDS = 2.0
_MIN_CLIENT_WIDTH = 800
_MIN_CLIENT_HEIGHT = 450
_MIN_ASPECT_RATIO = 1.20
_MAX_ASPECT_RATIO = 2.50
_LOGIN_DETECTION_MIN_WIDTH = 480
_LOGIN_DETECTION_MIN_HEIGHT = 270
_LOGIN_DETECTION_MIN_ASPECT_RATIO = 0.90
_LOGIN_DETECTION_MAX_ASPECT_RATIO = 3.50
_MIN_LAUNCHER_WIDTH = 700
_MIN_LAUNCHER_HEIGHT = 400
_LAUNCHER_READY_ZONE = (0.68, 0.89, 0.83, 0.97)
_LOGIN_SIGNATURE_ZONES = (
    (0.63, 0.50, 0.82, 0.55, 16),
    (0.63, 0.55, 0.82, 0.60, 8),
    (0.69, 0.60, 0.76, 0.65, 8),
)
_LOGIN_SIGNATURE_REFERENCE_AREA = 854 * 480
_INTERNAL_LOGIN_UI_MARKERS = (
    b"LoginPanel.Password.PasswordText",
    b"StartLogin()",
)


class AutoLoginError(RuntimeError):
    """自动登录无法安全继续。"""


class CredentialStoreError(ValueError):
    """自动登录凭据文件不可用。"""


class LauncherKind(str, Enum):
    """Warframe 启动器使用的安装渠道。"""

    STANDALONE = "standalone"
    STEAM = "steam"

    @property
    def label(self) -> str:
        if self is LauncherKind.STEAM:
            return "Steam 版"
        return "官网独立版"


@dataclass(frozen=True)
class WarframeLauncher:
    path: Path
    kind: LauncherKind

    @property
    def arguments(self) -> tuple[str, ...]:
        if self.kind is LauncherKind.STEAM:
            return STEAM_LAUNCH_ARGUMENTS
        return ()


@dataclass(frozen=True)
class LoginCredential:
    username: str
    password: str


@dataclass(frozen=True)
class ClientFrame:
    hwnd: int
    left: int
    top: int
    width: int
    height: int

    def safe_rect(self) -> tuple[int, int, int, int]:
        # 登录布局以居中的 16:9 安全画面为基准。宽窗口会在两侧增加画面，
        # 窄窗口则在上下增加画面；不能直接按整个客户区宽度缩放横坐标。
        safe_aspect_ratio = 16 / 9
        if self.width / self.height >= safe_aspect_ratio:
            safe_width = round(self.height * safe_aspect_ratio)
            safe_height = self.height
            safe_left = self.left + (self.width - safe_width) // 2
            safe_top = self.top
        else:
            safe_width = self.width
            safe_height = round(self.width / safe_aspect_ratio)
            safe_left = self.left
            safe_top = self.top + (self.height - safe_height) // 2
        return safe_left, safe_top, safe_width, safe_height

    def point(self, relative: tuple[float, float]) -> tuple[int, int]:
        safe_left, safe_top, safe_width, safe_height = self.safe_rect()
        x_ratio, y_ratio = relative
        return (
            safe_left + round(safe_width * x_ratio),
            safe_top + round(safe_height * y_ratio),
        )

    def relative_point(self, relative: tuple[float, float]) -> tuple[int, int]:
        x_ratio, y_ratio = relative
        return (
            self.left + round(self.width * x_ratio),
            self.top + round(self.height * y_ratio),
        )


def _login_accent_pixel(red: int, green: int, blue: int) -> bool:
    coral = red >= 130 and red - green >= 35 and red - blue >= 15
    gold = (
        red >= 130
        and green >= 90
        and red - green >= 5
        and red - blue >= 35
        and green - blue >= 15
    )
    return coral or gold


def _login_signature_present(image: Image.Image) -> bool:
    width, height = image.size
    if (
        width < _LOGIN_DETECTION_MIN_WIDTH
        or height < _LOGIN_DETECTION_MIN_HEIGHT
    ):
        return False
    if not (
        _LOGIN_DETECTION_MIN_ASPECT_RATIO
        <= width / height
        <= _LOGIN_DETECTION_MAX_ASPECT_RATIO
    ):
        return False
    frame = ClientFrame(0, 0, 0, width, height)
    safe_left, safe_top, safe_width, safe_height = frame.safe_rect()
    area_scale = (safe_width * safe_height) / _LOGIN_SIGNATURE_REFERENCE_AREA
    pixels = image.convert("RGB")
    for left, top, right, bottom, base_minimum in _LOGIN_SIGNATURE_ZONES:
        box = (
            safe_left + round(safe_width * left),
            safe_top + round(safe_height * top),
            safe_left + round(safe_width * right),
            safe_top + round(safe_height * bottom),
        )
        crop = pixels.crop(box)
        minimum = max(3, round(base_minimum * area_scale * 0.7))
        raw = crop.tobytes()
        count = sum(
            _login_accent_pixel(
                raw[index], raw[index + 1], raw[index + 2]
            )
            for index in range(0, len(raw), 3)
        )
        if count < minimum:
            return False
    return True


def _launcher_ready_signature_present(image: Image.Image) -> bool:
    width, height = image.size
    if width < _MIN_LAUNCHER_WIDTH or height < _MIN_LAUNCHER_HEIGHT:
        return False
    left, top, right, bottom = _LAUNCHER_READY_ZONE
    crop = image.convert("RGB").crop((
        round(width * left),
        round(height * top),
        round(width * right),
        round(height * bottom),
    ))
    raw = crop.tobytes()
    red_pixels = sum(
        raw[index] >= 150
        and raw[index] - raw[index + 1] >= 55
        and raw[index] - raw[index + 2] >= 45
        for index in range(0, len(raw), 3)
    )
    return red_pixels >= (crop.width * crop.height * 35) // 100


class _DataBlob(ctypes.Structure):
    _fields_ = (
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    )


def _require_windows() -> None:
    if os.name != "nt":
        raise AutoLoginError("全自动登录只支持 Windows")


def _data_blob(value: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(value)
    pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    return _DataBlob(len(value), pointer), buffer


def _dpapi_protect(value: bytes) -> bytes:
    _require_windows()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = (
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    )
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
    kernel32.LocalFree.restype = wintypes.HLOCAL

    source, source_buffer = _data_blob(value)
    entropy, entropy_buffer = _data_blob(_DPAPI_ENTROPY)
    output = _DataBlob()
    if not crypt32.CryptProtectData(
        ctypes.byref(source),
        "RivenSniper 自动登录凭据",
        ctypes.byref(entropy),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output),
    ):
        raise CredentialStoreError(
            f"Windows 无法加密自动登录凭据（错误 {ctypes.get_last_error()}）"
        )
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(output.pbData, wintypes.HLOCAL))


def _dpapi_unprotect(value: bytes) -> bytes:
    _require_windows()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = (
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    )
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
    kernel32.LocalFree.restype = wintypes.HLOCAL

    source, source_buffer = _data_blob(value)
    entropy, entropy_buffer = _data_blob(_DPAPI_ENTROPY)
    output = _DataBlob()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        ctypes.byref(entropy),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output),
    ):
        raise CredentialStoreError(
            "自动登录凭据无法由当前 Windows 用户解密；请重新配置"
        )
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(output.pbData, wintypes.HLOCAL))


def _atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f"{path.name}.tmp.{os.getpid()}.{time.time_ns()}"
    )
    try:
        with temporary.open("xb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def warframe_config_path() -> Path:
    _require_windows()
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        raise AutoLoginError("无法定位当前 Windows 用户的 Warframe 配置目录")
    return Path(local_app_data) / "Warframe" / WARFRAME_CONFIG_FILENAME


def _replace_config_values(
    original: bytes,
    values: Mapping[bytes, bytes],
) -> bytes:
    newline = b"\r\n" if b"\r\n" in original else b"\n"
    result: list[bytes] = []
    replaced: set[bytes] = set()
    for line in original.splitlines(keepends=True):
        ending = (
            b"\r\n" if line.endswith(b"\r\n")
            else b"\n" if line.endswith(b"\n")
            else b"\r" if line.endswith(b"\r")
            else b""
        )
        body = line[:-len(ending)] if ending else line
        key, separator, _value = body.partition(b"=")
        if separator and key in values:
            if key not in replaced:
                result.append(key + b"=" + values[key] + ending)
                replaced.add(key)
            continue
        result.append(line)

    for key, value in values.items():
        if key in replaced:
            continue
        if result and not result[-1].endswith((b"\n", b"\r")):
            result.append(newline)
        result.append(key + b"=" + value + newline)
    return b"".join(result)


@dataclass
class InternalAutoLoginLease:
    """临时选择游戏内部登录账号，并可精确还原原文件。"""

    path: Path
    _original: bytes = field(repr=False)
    _recovery_path: Path | None = field(default=None, repr=False)

    def restore(self, *, keep_recovery: bool = False) -> None:
        _atomic_write_bytes(self.path, self._original)
        if self._recovery_path is not None and not keep_recovery:
            try:
                self._recovery_path.unlink()
            except FileNotFoundError:
                pass


def internal_login_recovery_path(runtime_root: str | Path) -> Path:
    return Path(runtime_root) / INTERNAL_LOGIN_RECOVERY_FILENAME


def recover_internal_auto_login(
    recovery_path: str | Path,
    *,
    config_path: str | Path | None = None,
) -> bool:
    """恢复上次异常中断前的 EE.cfg；没有恢复文件时返回 False。"""

    recovery = Path(recovery_path)
    try:
        stored = recovery.read_bytes()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise AutoLoginError(f"无法读取自动登录恢复文件：{error}") from error
    if not stored.startswith(_INTERNAL_LOGIN_RECOVERY_MAGIC):
        raise AutoLoginError("自动登录恢复文件格式不受支持")
    try:
        original = _dpapi_unprotect(
            stored[len(_INTERNAL_LOGIN_RECOVERY_MAGIC):]
        )
    except CredentialStoreError as error:
        raise AutoLoginError(f"无法解密自动登录恢复文件：{error}") from error
    path = Path(config_path) if config_path is not None else warframe_config_path()
    try:
        _atomic_write_bytes(path, original)
        recovery.unlink()
    except OSError as error:
        raise AutoLoginError(f"无法恢复 Warframe 配置文件：{error}") from error
    return True


def prepare_internal_auto_login(
    credential: LoginCredential,
    *,
    config_path: str | Path | None = None,
    recovery_path: str | Path | None = None,
) -> InternalAutoLoginLease:
    """让游戏登录 UI 从内部配置选择本轮账号。"""

    username = credential.username.strip()
    if not username or any(character in username for character in "\r\n\x00"):
        raise AutoLoginError("自动登录账号不能包含换行或空字符")
    if not credential.password or "\x00" in credential.password:
        raise AutoLoginError("自动登录密码不能为空且不能包含空字符")

    path = Path(config_path) if config_path is not None else warframe_config_path()
    recovery = Path(recovery_path) if recovery_path is not None else None
    if recovery is not None:
        recover_internal_auto_login(recovery, config_path=path)
    try:
        original = path.read_bytes()
    except FileNotFoundError as error:
        raise AutoLoginError(f"找不到 Warframe 配置文件：{path}") from error
    except OSError as error:
        raise AutoLoginError(f"无法读取 Warframe 配置文件：{error}") from error

    injected = _replace_config_values(
        original,
        {
            b"Client.Email": username.encode("utf-8"),
            b"Client.AutoLogin": b"0",
        },
    )
    if recovery is not None:
        encrypted = _dpapi_protect(original)
        _atomic_write_bytes(
            recovery,
            _INTERNAL_LOGIN_RECOVERY_MAGIC + encrypted,
        )
    try:
        _atomic_write_bytes(path, injected)
    except OSError as error:
        if recovery is not None:
            recover_internal_auto_login(recovery, config_path=path)
        raise AutoLoginError(f"无法写入 Warframe 内部自动登录设置：{error}") from error
    return InternalAutoLoginLease(
        path=path,
        _original=original,
        _recovery_path=recovery,
    )


def save_credentials(
    path: str | Path,
    credentials_by_mode: Mapping[str, Mapping[str, LoginCredential]],
) -> None:
    modes = {
        str(mode): {
            str(slot).upper(): {
                "username": credential.username,
                "password": credential.password,
            }
            for slot, credential in credentials.items()
        }
        for mode, credentials in credentials_by_mode.items()
    }
    payload = json.dumps(
        {"version": 2, "modes": modes},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    encrypted = _dpapi_protect(payload)
    _atomic_write_bytes(Path(path), _CREDENTIAL_MAGIC + encrypted)


def load_credentials(
    path: str | Path,
) -> dict[str, dict[str, LoginCredential]]:
    source = Path(path)
    try:
        stored = source.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise CredentialStoreError(f"无法读取自动登录凭据: {error}") from error
    if not stored.startswith(_CREDENTIAL_MAGIC):
        raise CredentialStoreError("自动登录凭据文件格式不受支持")
    try:
        document = json.loads(
            _dpapi_unprotect(stored[len(_CREDENTIAL_MAGIC):]).decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CredentialStoreError("自动登录凭据内容损坏") from error
    if not isinstance(document, dict):
        raise CredentialStoreError("自动登录凭据内容不是对象")
    if document.get("version") == 1:
        raise CredentialStoreError(
            "旧版自动登录凭据没有区分 4/17 槽模式；"
            "请从菜单 8 重新配置当前模式"
        )
    if document.get("version") != 2:
        raise CredentialStoreError("自动登录凭据版本不受支持")
    raw_modes = document.get("modes")
    if not isinstance(raw_modes, dict):
        raise CredentialStoreError("自动登录凭据缺少 modes")

    credentials_by_mode: dict[str, dict[str, LoginCredential]] = {}
    for raw_mode, raw_slots in raw_modes.items():
        mode = str(raw_mode)
        if mode not in {"4", "17"} or not isinstance(raw_slots, dict):
            raise CredentialStoreError("自动登录凭据包含无效采集模式")
        credentials: dict[str, LoginCredential] = {}
        for raw_slot, raw_credential in raw_slots.items():
            slot = str(raw_slot).upper()
            if not re.fullmatch(r"[A-Q]", slot) or not isinstance(
                raw_credential, dict
            ):
                raise CredentialStoreError("自动登录凭据包含无效槽位")
            username = raw_credential.get("username")
            password = raw_credential.get("password")
            if not isinstance(username, str) or not username or not isinstance(
                password, str
            ) or not password:
                raise CredentialStoreError(
                    f"{mode} 槽模式的槽 {slot} 自动登录凭据不完整"
                )
            credentials[slot] = LoginCredential(username, password)
        credentials_by_mode[mode] = credentials
    return credentials_by_mode


def credential_store_path(runtime_root: str | Path) -> Path:
    return Path(runtime_root) / CREDENTIAL_STORE_FILENAME


def _query_process_executable(pid: int) -> Path | None:
    if os.name != "nt" or pid <= 0:
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
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(buffer))
        if kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(length)
        ):
            return Path(buffer.value)
        return None
    finally:
        kernel32.CloseHandle(handle)


class _ProcessEntry32W(ctypes.Structure):
    _fields_ = (
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    )


def _process_ids_named(executable_name: str) -> tuple[int, ...]:
    _require_windows()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessEntry32W),
    )
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessEntry32W),
    )
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if handle == wintypes.HANDLE(-1).value:
        raise AutoLoginError(
            f"无法枚举游戏进程（错误 {ctypes.get_last_error()}）"
        )
    result: list[int] = []
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel32.Process32FirstW(handle, ctypes.byref(entry)):
            return ()
        while True:
            if entry.szExeFile.casefold() == executable_name.casefold():
                result.append(int(entry.th32ProcessID))
            if not kernel32.Process32NextW(handle, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(handle)
    return tuple(sorted(result))


def warframe_process_ids() -> tuple[int, ...]:
    return _process_ids_named(WARFRAME_PROCESS_NAME)


def launcher_process_ids(launcher: str | Path) -> tuple[int, ...]:
    expected = str(Path(launcher).resolve()).casefold()
    result: list[int] = []
    for pid in _process_ids_named(LAUNCHER_PROCESS_NAME):
        executable = _query_process_executable(pid)
        if executable and str(executable.resolve()).casefold() == expected:
            result.append(pid)
    return tuple(result)


def _steam_roots() -> tuple[Path, ...]:
    if os.name != "nt":
        return ()
    import winreg

    roots: list[Path] = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            for name in ("SteamPath", "SteamExe"):
                try:
                    value = str(winreg.QueryValueEx(key, name)[0]).strip()
                except OSError:
                    continue
                if value:
                    path = Path(value.replace("/", "\\"))
                    roots.append(path.parent if path.suffix else path)
    except OSError:
        pass
    return tuple(roots)


def _steam_library_paths(steam_root: Path) -> tuple[Path, ...]:
    paths = [steam_root]
    source = steam_root / "config" / "libraryfolders.vdf"
    try:
        text = source.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return tuple(paths)
    for raw in re.findall(r'"path"\s*"([^"]+)"', text):
        paths.append(Path(raw.replace("\\\\", "\\")))
    unique: dict[str, Path] = {}
    for path in paths:
        unique.setdefault(str(path).casefold(), path)
    return tuple(unique.values())


def _steam_launcher_paths() -> tuple[Path, ...]:
    paths: list[Path] = []
    for steam_root in _steam_roots():
        for library in _steam_library_paths(steam_root):
            paths.append(
                library
                / "steamapps"
                / "common"
                / "Warframe"
                / "Tools"
                / "Launcher.exe"
            )
    return tuple(paths)


def _standalone_launcher_paths() -> tuple[Path, ...]:
    if os.name != "nt":
        return ()
    import winreg

    paths: list[Path] = []
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Digital Extremes\Warframe\Launcher",
        ) as key:
            try:
                launcher_exe = str(
                    winreg.QueryValueEx(key, "LauncherExe")[0]
                ).strip()
            except OSError:
                launcher_exe = ""
            if launcher_exe:
                paths.append(Path(launcher_exe))

            try:
                download_dir = str(
                    winreg.QueryValueEx(key, "DownloadDir")[0]
                ).strip()
            except OSError:
                download_dir = ""
            if download_dir:
                paths.append(
                    Path(download_dir) / "Public" / "Tools" / "Launcher.exe"
                )
    except OSError:
        pass

    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        paths.append(
            Path(local_app_data)
            / "Warframe"
            / "Downloaded"
            / "Public"
            / "Tools"
            / "Launcher.exe"
        )
    return tuple(paths)


def _same_path(left: Path, right: Path) -> bool:
    try:
        left_path = left.resolve()
    except OSError:
        left_path = left.absolute()
    try:
        right_path = right.resolve()
    except OSError:
        right_path = right.absolute()
    return str(left_path).casefold() == str(right_path).casefold()


def _identify_launcher(
    path: str | Path,
    steam_paths: tuple[Path, ...],
) -> WarframeLauncher:
    resolved = Path(path).expanduser().resolve()
    for steam_path in steam_paths:
        if _same_path(resolved, steam_path):
            return WarframeLauncher(resolved, LauncherKind.STEAM)

    parts = tuple(part.casefold() for part in resolved.parts)
    steam_suffix = (
        "steamapps",
        "common",
        "warframe",
        "tools",
        "launcher.exe",
    )
    if len(parts) >= len(steam_suffix):
        for index in range(len(parts) - len(steam_suffix) + 1):
            if parts[index:index + len(steam_suffix)] == steam_suffix:
                return WarframeLauncher(resolved, LauncherKind.STEAM)
    return WarframeLauncher(resolved, LauncherKind.STANDALONE)


def identify_launcher(path: str | Path) -> WarframeLauncher:
    return _identify_launcher(path, _steam_launcher_paths())


def discover_launcher(
    explicit: str | Path | None = None,
) -> WarframeLauncher:
    _require_windows()
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise AutoLoginError(f"指定的 Warframe 启动器不存在: {path}")
        return identify_launcher(path)

    steam_paths = _steam_launcher_paths()
    candidates: list[Path] = []
    for pid in warframe_process_ids():
        executable = _query_process_executable(pid)
        if executable:
            candidates.append(executable.parent / "Tools" / "Launcher.exe")
    candidates.extend(_standalone_launcher_paths())
    candidates.extend(steam_paths)

    checked: set[str] = set()
    for candidate in candidates:
        key = str(candidate).casefold()
        if key in checked:
            continue
        checked.add(key)
        if candidate.is_file():
            return _identify_launcher(candidate, steam_paths)
    raise AutoLoginError(
        "未找到官网独立版或 Steam 版 Warframe 启动器；"
        "请使用 --launcher 指定 Launcher.exe 的完整路径"
    )


def discover_launcher_path(explicit: str | Path | None = None) -> Path:
    """兼容只需要启动器路径的现有调用。"""
    return discover_launcher(explicit).path


def launch_warframe(
    launcher: WarframeLauncher | str | Path,
) -> subprocess.Popen[bytes]:
    installation = (
        launcher
        if isinstance(launcher, WarframeLauncher)
        else identify_launcher(launcher)
    )
    path = installation.path
    if not path.is_file():
        raise AutoLoginError(f"Warframe 启动器不存在: {path}")
    return subprocess.Popen(
        [str(path), *installation.arguments],
        cwd=path.parent.parent,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_for_new_game_process(
    existing: set[int],
    *,
    timeout: float,
    poll_interval: float = 0.5,
    pid_provider: Callable[[], tuple[int, ...]] = warframe_process_ids,
) -> int:
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        candidates = set(pid_provider()) - existing
        if len(candidates) == 1:
            return candidates.pop()
        if len(candidates) > 1:
            raise AutoLoginError("启动后出现多个 Warframe 进程，无法确定登录目标")
        time.sleep(max(0.05, poll_interval))
    raise AutoLoginError("等待 Warframe 游戏进程启动超时")


def terminate_warframe_process(
    pid: int,
    *,
    wait_timeout: float = 5.0,
) -> bool:
    """终止当前枚举中仍为 Warframe.x64.exe 的已接管 PID。"""
    if pid not in warframe_process_ids():
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x0001, False, pid)
    if not handle:
        return pid not in warframe_process_ids()
    try:
        if pid not in warframe_process_ids():
            return True
        if not kernel32.TerminateProcess(handle, 0):
            return pid not in warframe_process_ids()
    finally:
        kernel32.CloseHandle(handle)

    deadline = time.monotonic() + max(0.0, wait_timeout)
    while time.monotonic() < deadline:
        if pid not in warframe_process_ids():
            return True
        time.sleep(0.05)
    return pid not in warframe_process_ids()


class _MouseInput(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    )


class _KeyboardInput(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    )


class _HardwareInput(ctypes.Structure):
    _fields_ = (
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    )


class _InputUnion(ctypes.Union):
    _fields_ = (
        ("mi", _MouseInput),
        ("ki", _KeyboardInput),
        ("hi", _HardwareInput),
    )


class _Input(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = (("type", wintypes.DWORD), ("value", _InputUnion))


_WndEnumProc = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
)


class WindowsLoginDriver:
    """用客户区比例定位 Warframe 登录页并发送本地输入。"""

    def __init__(self) -> None:
        _require_windows()
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_dpi_awareness()
        self._configure_functions()

    def _configure_dpi_awareness(self) -> None:
        try:
            self.user32.SetProcessDpiAwarenessContext(
                ctypes.c_void_p(-4)
            )
        except AttributeError:
            self.user32.SetProcessDPIAware()

    def _configure_functions(self) -> None:
        self.user32.EnumWindows.argtypes = (
            _WndEnumProc,
            wintypes.LPARAM,
        )
        self.user32.EnumWindows.restype = wintypes.BOOL
        self.user32.GetWindowThreadProcessId.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        )
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.IsWindowVisible.argtypes = (wintypes.HWND,)
        self.user32.IsWindowVisible.restype = wintypes.BOOL
        self.user32.GetClientRect.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
        )
        self.user32.GetClientRect.restype = wintypes.BOOL
        self.user32.ClientToScreen.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.POINT),
        )
        self.user32.ClientToScreen.restype = wintypes.BOOL
        self.user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        self.user32.ShowWindow.restype = wintypes.BOOL
        self.user32.AttachThreadInput.argtypes = (
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.BOOL,
        )
        self.user32.AttachThreadInput.restype = wintypes.BOOL
        self.user32.BringWindowToTop.argtypes = (wintypes.HWND,)
        self.user32.BringWindowToTop.restype = wintypes.BOOL
        self.user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
        self.user32.SetForegroundWindow.restype = wintypes.BOOL
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.WindowFromPoint.argtypes = (wintypes.POINT,)
        self.user32.WindowFromPoint.restype = wintypes.HWND
        self.user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
        self.user32.GetAncestor.restype = wintypes.HWND
        self.user32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
        self.user32.SetCursorPos.restype = wintypes.BOOL
        self.user32.SendInput.argtypes = (
            wintypes.UINT,
            ctypes.POINTER(_Input),
            ctypes.c_int,
        )
        self.user32.SendInput.restype = wintypes.UINT
        self.kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    def _window_handles(self, pid: int) -> list[int]:
        handles: list[int] = []

        @_WndEnumProc
        def callback(hwnd: int, _lparam: int) -> bool:
            owner = wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and self.user32.IsWindowVisible(hwnd):
                rect = wintypes.RECT()
                if self.user32.GetClientRect(hwnd, ctypes.byref(rect)):
                    if rect.right > rect.left and rect.bottom > rect.top:
                        handles.append(int(hwnd))
            return True

        if not self.user32.EnumWindows(callback, 0):
            raise AutoLoginError(
                f"无法枚举游戏窗口（错误 {ctypes.get_last_error()}）"
            )
        return handles

    def wait_for_window(self, pid: int, *, timeout: float) -> int:
        deadline = time.monotonic() + max(0.0, timeout)
        last_error = ""
        while time.monotonic() < deadline:
            handles = self._window_handles(pid)
            candidates: list[tuple[int, ClientFrame]] = []
            for hwnd in handles:
                try:
                    candidates.append((hwnd, self.client_frame(hwnd)))
                except AutoLoginError as error:
                    last_error = str(error)
            if candidates:
                return max(
                    candidates,
                    key=lambda item: item[1].width * item[1].height,
                )[0]
            time.sleep(0.25)
        detail = f"；最近状态：{last_error}" if last_error else ""
        raise AutoLoginError(f"等待 Warframe 登录窗口超时{detail}")

    def wait_for_process_window(
        self,
        pid: int,
        *,
        timeout: float,
    ) -> ClientFrame:
        """等待游戏创建可见窗口，不对内部登录使用的窗口尺寸作限制。"""

        deadline = time.monotonic() + max(0.0, timeout)
        last_error = ""
        while time.monotonic() < deadline:
            candidates: list[ClientFrame] = []
            for hwnd in self._window_handles(pid):
                try:
                    candidates.append(self._raw_client_frame(hwnd))
                except AutoLoginError as error:
                    last_error = str(error)
            if candidates:
                return max(
                    candidates,
                    key=lambda frame: frame.width * frame.height,
                )
            time.sleep(0.25)
        detail = f"；最近状态：{last_error}" if last_error else ""
        raise AutoLoginError(f"等待 Warframe 游戏窗口超时{detail}")

    def wait_for_internal_login_ui(
        self,
        pid: int,
        *,
        timeout: float,
    ) -> ClientFrame:
        """从游戏进程内存确认登录 Lua UI 已加载。"""

        deadline = time.monotonic() + max(0.0, timeout)
        frame = self.wait_for_process_window(pid, timeout=timeout)
        last_error = ""
        visual_observations = 0
        while time.monotonic() < deadline:
            try:
                self._activate(frame.hwnd)
                frame = self._raw_client_frame(frame.hwnd)
                visible = _login_signature_present(
                    self._capture_client(frame)
                )
            except AutoLoginError as error:
                last_error = str(error)
                visible = False
            visual_observations = visual_observations + 1 if visible else 0
            if visual_observations >= 2:
                try:
                    found = find_memory_markers(
                        pid, _INTERNAL_LOGIN_UI_MARKERS
                    )
                except OSError as error:
                    last_error = str(error)
                else:
                    if len(found) == len(_INTERNAL_LOGIN_UI_MARKERS):
                        return frame
            time.sleep(0.25)
        detail = f"；最近状态：{last_error}" if last_error else ""
        raise AutoLoginError(f"等待 Warframe 内部登录 UI 超时{detail}")

    def client_frame(self, hwnd: int) -> ClientFrame:
        frame = self._raw_client_frame(hwnd)
        width = frame.width
        height = frame.height
        if width < _MIN_CLIENT_WIDTH or height < _MIN_CLIENT_HEIGHT:
            raise AutoLoginError(
                "游戏客户区至少需要 800x450；请放大窗口后重试"
            )
        aspect_ratio = width / height
        if not _MIN_ASPECT_RATIO <= aspect_ratio <= _MAX_ASPECT_RATIO:
            raise AutoLoginError(
                "游戏窗口宽高比不在支持范围 1.20-2.50；"
                "请调整窗口尺寸后重试"
            )
        return frame

    def _raw_client_frame(self, hwnd: int) -> ClientFrame:
        rect = wintypes.RECT()
        if not self.user32.GetClientRect(hwnd, ctypes.byref(rect)):
            raise AutoLoginError("无法读取目标窗口客户区尺寸")
        origin = wintypes.POINT(0, 0)
        if not self.user32.ClientToScreen(hwnd, ctypes.byref(origin)):
            raise AutoLoginError("无法读取目标窗口客户区位置")
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        return ClientFrame(
            hwnd=hwnd,
            left=int(origin.x),
            top=int(origin.y),
            width=width,
            height=height,
        )

    def wait_for_launcher_and_start_game(
        self,
        pid: int,
        *,
        timeout: float,
        stable_observations: int = 2,
    ) -> ClientFrame:
        deadline = time.monotonic() + max(0.0, timeout)
        required = max(1, stable_observations)
        stable = 0
        previous: tuple[int, int, int, int, int] | None = None
        last_error = ""
        while time.monotonic() < deadline:
            candidates: list[tuple[int, ClientFrame]] = []
            for hwnd in self._window_handles(pid):
                try:
                    frame = self._raw_client_frame(hwnd)
                    if (
                        frame.width < _MIN_LAUNCHER_WIDTH
                        or frame.height < _MIN_LAUNCHER_HEIGHT
                    ):
                        raise AutoLoginError(
                            "Warframe 启动器窗口至少需要 700x400"
                        )
                    candidates.append((hwnd, frame))
                except AutoLoginError as error:
                    last_error = str(error)
            if not candidates:
                stable = 0
                previous = None
                time.sleep(0.25)
                continue

            hwnd, frame = max(
                candidates,
                key=lambda item: item[1].width * item[1].height,
            )
            try:
                self._activate(hwnd)
                self._assert_target(hwnd, pid)
                frame = self._raw_client_frame(hwnd)
                ready = _launcher_ready_signature_present(
                    self._capture_client(frame)
                )
            except AutoLoginError as error:
                last_error = str(error)
                ready = False
            key = (hwnd, frame.left, frame.top, frame.width, frame.height)
            if ready and key == previous:
                stable += 1
            elif ready:
                stable = 1
            else:
                stable = 0
            previous = key if ready else None
            if stable >= required:
                point = frame.relative_point(_LAUNCHER_START_POINT)
                self._activate(hwnd)
                self._assert_target(hwnd, pid, point=point)
                self._click(point)
                return frame
            time.sleep(0.5)

        detail = f"；最近状态：{last_error}" if last_error else ""
        raise AutoLoginError(
            f"等待 Warframe 启动器“开始游戏”按钮就绪超时{detail}"
        )

    def _activate(self, hwnd: int) -> None:
        self.user32.ShowWindow(hwnd, 9)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            foreground = int(self.user32.GetForegroundWindow() or 0)
            if foreground == hwnd:
                return
            current_thread = int(self.kernel32.GetCurrentThreadId())
            foreground_thread = (
                int(self.user32.GetWindowThreadProcessId(foreground, None))
                if foreground
                else 0
            )
            attached = bool(
                foreground_thread
                and foreground_thread != current_thread
                and self.user32.AttachThreadInput(
                    current_thread, foreground_thread, True
                )
            )
            try:
                self.user32.BringWindowToTop(hwnd)
                self.user32.SetForegroundWindow(hwnd)
            finally:
                if attached:
                    self.user32.AttachThreadInput(
                        current_thread, foreground_thread, False
                    )
            if int(self.user32.GetForegroundWindow() or 0) == hwnd:
                return
            time.sleep(0.05)
        raise AutoLoginError("无法把 Warframe 窗口切到前台")

    def _owner_pid(self, hwnd: int) -> int:
        owner = wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        return int(owner.value)

    def _assert_target(
        self,
        hwnd: int,
        pid: int,
        *,
        point: tuple[int, int] | None = None,
    ) -> None:
        if self._owner_pid(hwnd) != pid:
            raise AutoLoginError("Warframe 窗口已被替换，停止发送登录输入")
        if int(self.user32.GetForegroundWindow() or 0) != hwnd:
            raise AutoLoginError("Warframe 不再是前台窗口，停止发送登录输入")
        if point is None:
            return
        hit = self.user32.WindowFromPoint(wintypes.POINT(*point))
        root = self.user32.GetAncestor(hit, 2) if hit else 0
        if int(root or hit or 0) != hwnd:
            raise AutoLoginError(
                "登录控件位置被其他窗口遮挡，停止发送登录输入"
            )

    def _capture_client(self, frame: ClientFrame) -> Image.Image:
        try:
            image = ImageGrab.grab(
                bbox=(
                    frame.left,
                    frame.top,
                    frame.left + frame.width,
                    frame.top + frame.height,
                ),
                all_screens=True,
            )
        except (OSError, ValueError) as error:
            raise AutoLoginError(f"无法读取 Warframe 登录画面: {error}") from error
        if image.size != (frame.width, frame.height):
            raise AutoLoginError("Warframe 登录画面尺寸在读取期间发生变化")
        return image

    def wait_for_login_page(
        self,
        pid: int,
        *,
        timeout: float,
        stable_observations: int = 2,
    ) -> tuple[int, ClientFrame]:
        deadline = time.monotonic() + max(0.0, timeout)
        required = max(1, stable_observations)
        stable = 0
        previous: tuple[int, int, int, int, int] | None = None
        last_error = ""
        while time.monotonic() < deadline:
            candidates: list[tuple[int, ClientFrame]] = []
            for hwnd in self._window_handles(pid):
                try:
                    candidates.append((hwnd, self.client_frame(hwnd)))
                except AutoLoginError as error:
                    last_error = str(error)
            if not candidates:
                stable = 0
                previous = None
                time.sleep(0.25)
                continue

            hwnd, frame = max(
                candidates,
                key=lambda item: item[1].width * item[1].height,
            )
            try:
                self._activate(hwnd)
                self._assert_target(hwnd, pid)
                frame = self.client_frame(hwnd)
                ready = _login_signature_present(self._capture_client(frame))
            except AutoLoginError as error:
                last_error = str(error)
                ready = False
            key = (hwnd, frame.left, frame.top, frame.width, frame.height)
            if ready and key == previous:
                stable += 1
            elif ready:
                stable = 1
            else:
                stable = 0
            previous = key if ready else None
            if stable >= required:
                return hwnd, frame
            time.sleep(0.5)

        detail = f"；最近状态：{last_error}" if last_error else ""
        raise AutoLoginError(f"等待 Warframe 登录页就绪超时{detail}")

    def _send(self, entries: list[_Input]) -> None:
        if not entries:
            return
        array = (_Input * len(entries))(*entries)
        sent = self.user32.SendInput(
            len(entries), array, ctypes.sizeof(_Input)
        )
        if sent != len(entries):
            raise AutoLoginError(
                f"Windows 只发送了 {sent}/{len(entries)} 个输入事件"
            )

    @staticmethod
    def _scan_code(scan_code: int, *, up: bool = False) -> _Input:
        return _Input(
            type=1,
            ki=_KeyboardInput(
                wVk=0,
                wScan=scan_code,
                dwFlags=0x0008 | (0x0002 if up else 0),
                time=0,
                dwExtraInfo=0,
            ),
        )

    def _clear_focused_field(self) -> None:
        self._send([
            self._scan_code(0x1D),
            self._scan_code(0x1E),
            self._scan_code(0x1E, up=True),
            self._scan_code(0x1D, up=True),
            self._scan_code(0x0E),
            self._scan_code(0x0E, up=True),
        ])

    def _type_text(self, value: str) -> None:
        units = value.encode("utf-16-le")
        entries: list[_Input] = []
        for offset in range(0, len(units), 2):
            unit = int.from_bytes(units[offset:offset + 2], "little")
            entries.extend((
                _Input(
                    type=1,
                    ki=_KeyboardInput(
                        wVk=0,
                        wScan=unit,
                        dwFlags=0x0004,
                        time=0,
                        dwExtraInfo=0,
                    ),
                ),
                _Input(
                    type=1,
                    ki=_KeyboardInput(
                        wVk=0,
                        wScan=unit,
                        dwFlags=0x0004 | 0x0002,
                        time=0,
                        dwExtraInfo=0,
                    ),
                ),
            ))
        self._send(entries)

    def _click(self, point: tuple[int, int]) -> None:
        if not self.user32.SetCursorPos(*point):
            raise AutoLoginError("无法移动鼠标到 Warframe 登录控件")
        down = _Input(
            type=0,
            mi=_MouseInput(
                dx=0,
                dy=0,
                mouseData=0,
                dwFlags=0x0002,
                time=0,
                dwExtraInfo=0,
            ),
        )
        up = _Input(
            type=0,
            mi=_MouseInput(
                dx=0,
                dy=0,
                mouseData=0,
                dwFlags=0x0004,
                time=0,
                dwExtraInfo=0,
            ),
        )
        self._send([down])
        time.sleep(_MOUSE_CLICK_HOLD_SECONDS)
        self._send([up])

    def _click_target(
        self,
        hwnd: int,
        pid: int,
        point: tuple[int, int],
    ) -> None:
        self._activate(hwnd)
        self._assert_target(hwnd, pid, point=point)
        self._click(point)
        self._assert_target(hwnd, pid)

    def _clear_focused_field_target(self, hwnd: int, pid: int) -> None:
        self._assert_target(hwnd, pid)
        self._clear_focused_field()

    def _type_text_target(
        self,
        hwnd: int,
        pid: int,
        value: str,
    ) -> None:
        self._assert_target(hwnd, pid)
        self._type_text(value)

    def _press_shift_tab_target(self, hwnd: int, pid: int) -> None:
        self._assert_target(hwnd, pid)
        self._send([
            self._scan_code(0x2A),
            self._scan_code(0x0F),
            self._scan_code(0x0F, up=True),
            self._scan_code(0x2A, up=True),
        ])

    def _press_enter_target(self, hwnd: int, pid: int) -> None:
        self._assert_target(hwnd, pid)
        self._send([
            self._scan_code(0x1C),
            self._scan_code(0x1C, up=True),
        ])

    def _focus_email_field_target(
        self,
        hwnd: int,
        pid: int,
        frame: ClientFrame,
    ) -> None:
        # 游戏会记住并锁定上次登录的邮箱；先点锁图标解锁并清空，
        # 再从可稳定点击的密码框用 Shift+Tab 返回邮箱框。邮箱框本身的
        # 鼠标命中区域不稳定，直接点击会让账号文本落入密码框。
        self._click_target(hwnd, pid, frame.point(_EMAIL_UNLOCK_POINT))
        time.sleep(_EMAIL_UNLOCK_SETTLE_SECONDS)
        self._click_target(hwnd, pid, frame.point(_PASSWORD_POINT))
        time.sleep(_FIELD_FOCUS_SETTLE_SECONDS)
        self._press_shift_tab_target(hwnd, pid)
        time.sleep(_FIELD_FOCUS_SETTLE_SECONDS)

    def wait_for_login_transition(
        self,
        pid: int,
        hwnd: int,
        *,
        timeout: float = 20.0,
    ) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        last_error = ""
        while time.monotonic() < deadline:
            if hwnd not in self._window_handles(pid):
                return
            try:
                self._assert_target(hwnd, pid)
                frame = self.client_frame(hwnd)
                if not _login_signature_present(self._capture_client(frame)):
                    return
            except AutoLoginError as error:
                last_error = str(error)
            time.sleep(0.25)
        detail = f"；最近状态：{last_error}" if last_error else ""
        raise AutoLoginError(f"提交登录后页面未发生变化{detail}")

    def submit_credentials(
        self,
        pid: int,
        credential: LoginCredential,
        *,
        window_timeout: float = 90.0,
        settle_seconds: float = 8.0,
    ) -> ClientFrame:
        deadline = time.monotonic() + max(0.0, window_timeout)
        self.wait_for_window(pid, timeout=window_timeout)
        remaining = max(0.0, deadline - time.monotonic())
        time.sleep(min(max(0.0, settle_seconds), remaining))
        remaining = max(0.0, deadline - time.monotonic())
        hwnd, frame = self.wait_for_login_page(pid, timeout=remaining)

        self._focus_email_field_target(hwnd, pid, frame)
        self._clear_focused_field_target(hwnd, pid)
        self._type_text_target(hwnd, pid, credential.username)

        self._click_target(hwnd, pid, frame.point(_PASSWORD_POINT))
        time.sleep(_FIELD_FOCUS_SETTLE_SECONDS)
        self._clear_focused_field_target(hwnd, pid)
        self._type_text_target(hwnd, pid, credential.password)

        # SendInput 返回时游戏仍可能在消费整段 Unicode 密码事件；立即点击
        # 会只触发悬停而不提交。给登录页一小段稳定处理时间。
        time.sleep(_LOGIN_SUBMIT_SETTLE_SECONDS)
        self._click_target(hwnd, pid, frame.point(_LOGIN_POINT))
        self.wait_for_login_transition(pid, hwnd)
        return frame

    def submit_internal_credentials(
        self,
        pid: int,
        credential: LoginCredential,
        *,
        window_timeout: float = 90.0,
        settle_seconds: float = 2.0,
    ) -> ClientFrame:
        """读取内部 UI 就绪状态，并通过默认密码焦点提交凭据。"""

        frame = self.wait_for_internal_login_ui(pid, timeout=window_timeout)
        time.sleep(max(0.0, settle_seconds))
        self._activate(frame.hwnd)
        self._clear_focused_field_target(frame.hwnd, pid)
        self._type_text_target(frame.hwnd, pid, credential.password)
        time.sleep(_FIELD_FOCUS_SETTLE_SECONDS)
        self._press_enter_target(frame.hwnd, pid)
        return frame

__all__ = [
    "AutoLoginError",
    "CREDENTIAL_STORE_FILENAME",
    "ClientFrame",
    "CredentialStoreError",
    "INTERNAL_LOGIN_RECOVERY_FILENAME",
    "InternalAutoLoginLease",
    "LauncherKind",
    "LoginCredential",
    "WARFRAME_CONFIG_FILENAME",
    "WarframeLauncher",
    "WindowsLoginDriver",
    "credential_store_path",
    "discover_launcher",
    "discover_launcher_path",
    "identify_launcher",
    "internal_login_recovery_path",
    "launcher_process_ids",
    "launch_warframe",
    "load_credentials",
    "prepare_internal_auto_login",
    "recover_internal_auto_login",
    "save_credentials",
    "terminate_warframe_process",
    "wait_for_new_game_process",
    "warframe_process_ids",
    "warframe_config_path",
]
