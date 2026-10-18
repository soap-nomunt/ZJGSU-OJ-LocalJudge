import ctypes
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_VERSION = "1.0.0"
APP_TITLE = "C 语言本地判题器"

IS_FROZEN = bool(getattr(sys, "frozen", False))

if IS_FROZEN:
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent.parent

BUNDLE_DATA_DIR = APP_DIR / "data"
BANKS_DIR = BUNDLE_DATA_DIR / "banks"
STATEMENTS_DIR = BUNDLE_DATA_DIR / "statements"
TESTS_DIR = BUNDLE_DATA_DIR / "tests"
CATALOG_PATH = BUNDLE_DATA_DIR / "catalog.json"
DATA_VERSION_PATH = BUNDLE_DATA_DIR / "data_version.json"

LOCAL_APP_DATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))


def _is_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _choose_user_data_dir() -> Path:
    if not IS_FROZEN:
        return APP_DIR / "userdata"
    portable_flag = APP_DIR / "portable.flag"
    if portable_flag.exists() and _is_writable(APP_DIR):
        return APP_DIR / "userdata"
    return LOCAL_APP_DATA / "CJudge"


USER_DATA_DIR = _choose_user_data_dir()
WORKSPACE_DIR = USER_DATA_DIR / "workspace"
CUSTOM_TESTS_DIR = USER_DATA_DIR / "custom_tests"
CUSTOM_STATEMENTS_DIR = USER_DATA_DIR / "custom_statements"
USER_BANKS_DIR = USER_DATA_DIR / "banks"
LOGS_DIR = USER_DATA_DIR / "logs"
TEMP_DIR = USER_DATA_DIR / "temp"
SETTINGS_PATH = USER_DATA_DIR / "settings.json"
PROGRESS_PATH = USER_DATA_DIR / "progress.json"

DEFAULT_SETTINGS = {
    "gcc_path": "",
    "time_limit": 1.0,
    "time_grace": 0.5,
    "font_size": 11,
    "window_geometry": "1280x820",
    "external_code_dir": "",
    "skip_passed": False,
    "only_with_tests": True,
    "last_bank": "",
    "compare_mode": "normalized",
}


def ensure_user_dirs() -> None:
    for path in (
        USER_DATA_DIR,
        WORKSPACE_DIR,
        CUSTOM_TESTS_DIR,
        CUSTOM_STATEMENTS_DIR,
        USER_BANKS_DIR,
        LOGS_DIR,
        TEMP_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)


def load_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    try:
        if SETTINGS_PATH.exists():
            with SETTINGS_PATH.open("r", encoding="utf-8-sig") as fp:
                loaded = json.load(fp)
            if isinstance(loaded, dict):
                settings.update(loaded)
    except (OSError, ValueError):
        pass
    return settings


def save_settings(settings: dict) -> None:
    ensure_user_dirs()
    try:
        with SETTINGS_PATH.open("w", encoding="utf-8") as fp:
            json.dump(settings, fp, ensure_ascii=False, indent=2)
    except OSError:
        pass


def bundled_gcc_candidates() -> list[Path]:
    return [
        APP_DIR / "toolchain" / "mingw32" / "bin" / "gcc.exe",
        APP_DIR / "toolchain" / "mingw64" / "bin" / "gcc.exe",
    ]


def find_gcc(settings: dict | None = None) -> str:
    if settings:
        configured = str(settings.get("gcc_path", "")).strip()
        if configured and Path(configured).exists():
            return configured
    for candidate in bundled_gcc_candidates():
        if candidate.exists():
            return str(candidate)
    found = shutil.which("gcc")
    if found:
        return found
    return ""


def gcc_version(gcc_path: str) -> str:
    if not gcc_path or not Path(gcc_path).exists():
        return "未找到 gcc"
    try:
        proc = subprocess.run(
            [short_path(gcc_path), "--version"],
            capture_output=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        text = (proc.stdout or proc.stderr).decode("utf-8", errors="replace")
        first = text.splitlines()[0].strip() if text else ""
        return first or "未知版本"
    except (OSError, subprocess.SubprocessError):
        return "无法读取版本"


def short_path(path: str | Path) -> str:
    text = str(path)
    if os.name != "nt":
        return text
    try:
        buf = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetShortPathNameW(text, buf, len(buf))
        if length:
            return buf.value
    except (AttributeError, OSError):
        pass
    return text


def suppress_error_dialogs() -> None:
    if os.name != "nt":
        return
    try:
        SEM_FAILCRITICALERRORS = 0x0001
        SEM_NOGPFAULTERRORBOX = 0x0002
        SEM_NOOPENFILEERRORBOX = 0x8000
        ctypes.windll.kernel32.SetErrorMode(
            SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX
        )
    except (AttributeError, OSError):
        pass


def set_dpi_awareness() -> None:
    if os.name != "nt":
        return
    suppress_error_dialogs()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def read_text_file(path: str | Path) -> str:
    data = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def write_text_file(path: str | Path, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8", newline="\n")


def load_data_version() -> dict:
    try:
        if DATA_VERSION_PATH.exists():
            with DATA_VERSION_PATH.open("r", encoding="utf-8-sig") as fp:
                value = json.load(fp)
            if isinstance(value, dict):
                return value
    except (OSError, ValueError):
        pass
    return {}
