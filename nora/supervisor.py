"""Detached supervisor: keeps the bot alive with no console window attached.

`run.ps1` starts this with pythonw.exe, so closing any terminal window cannot
kill Nora. The supervisor owns the bot process, restarts it when it dies, and
stops cleanly when `scripts/stop.ps1` drops the sentinel file.

Internal env knobs (mostly for tests):
  NORA_DATA_DIR           where nora.pid / stop / logs live (default ./data)
  NORA_SUPERVISOR_CMD     JSON list overriding the child command
  NORA_RESTART_SECONDS    base restart delay (default 10)
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_GRACE_SECONDS = 15.0
_MAX_RESTART_SECONDS = 60.0
_LOG_FILE: Path | None = None
_K32: object | None = None


# -- helpers -------------------------------------------------------------
def _data_dir() -> Path:
    return Path(os.environ.get("NORA_DATA_DIR") or (ROOT / "data"))


def _log(message: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n"
    if _LOG_FILE is not None:
        try:
            with open(_LOG_FILE, "a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            pass
    if sys.stdout is not None:
        try:
            sys.stdout.write(line)
            sys.stdout.flush()
        except Exception:
            pass


def _command() -> list[str]:
    raw = os.environ.get("NORA_SUPERVISOR_CMD")
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, list) and parsed:
            return [str(part) for part in parsed]
    return [sys.executable, "-m", "nora", "bot"]


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = _kernel32()
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return False
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_pid(path: Path) -> int | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
        return int(value) if value else None
    except (OSError, ValueError):
        return None


def _acquire_pid_file(path: Path) -> bool:
    """Atomically claim the pid file; False if another live supervisor owns it."""
    for _ in range(2):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            other = _read_pid(path)
            if other is not None and _alive(other):
                return False
            try:
                path.unlink()
            except OSError:
                return False
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        return True
    return False


def _kernel32():
    """One properly prototyped kernel32 handle: ctypes truncates handles otherwise."""
    global _K32
    if _K32 is None:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        k32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.Process32FirstW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessEntry),
        ]
        k32.Process32NextW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessEntry),
        ]
        _K32 = k32
    return _K32


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def _process_table() -> list[tuple[int, int, str]]:
    """(pid, parent_pid, image_name) for every process, Windows only."""
    if sys.platform != "win32":
        return []
    k32 = _kernel32()
    TH32CS_SNAPPROCESS = 0x00000002
    snapshot = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return []
    rows: list[tuple[int, int, str]] = []
    entry = _ProcessEntry()
    entry.dwSize = ctypes.sizeof(_ProcessEntry)
    try:
        more = k32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            rows.append(
                (
                    int(entry.th32ProcessID),
                    int(entry.th32ParentProcessID),
                    entry.szExeFile,
                )
            )
            more = k32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snapshot)
    return rows


def _pids_named(image: str) -> list[int]:
    return [pid for pid, _, name in _process_table() if name.lower() == image.lower()]


def _process_image_path(pid: int) -> str | None:
    if sys.platform != "win32":
        return None
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = _kernel32()
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buffer))
        if k32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return None
    finally:
        k32.CloseHandle(handle)


def _terminate(pid: int) -> bool:
    if sys.platform != "win32":
        try:
            os.kill(pid, 9)
            return True
        except OSError:
            return False
    PROCESS_TERMINATE = 0x0001
    k32 = _kernel32()
    handle = k32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(k32.TerminateProcess(handle, 1))
    finally:
        k32.CloseHandle(handle)


def _descendants(pid: int) -> list[int]:
    children: dict[int, list[int]] = {}
    for child, parent, _ in _process_table():
        children.setdefault(parent, []).append(child)
    found: list[int] = []
    stack = [pid]
    while stack:
        for child in children.get(stack.pop(), []):
            found.append(child)
            stack.append(child)
    return found


def _kill_tree(pid: int) -> None:
    """Kill a process and everything it started (bot -> llama-server)."""
    for target in reversed(_descendants(pid)):
        _terminate(target)
    _terminate(pid)


def model_server_pids() -> list[int]:
    """Only our bundled llama-server, never another project's."""
    try:
        target = (ROOT / "vendor" / "llama" / "llama-server.exe").resolve()
    except OSError:
        return []
    found: list[int] = []
    for pid in _pids_named("llama-server.exe"):
        path = _process_image_path(pid)
        if not path:
            continue
        try:
            if Path(path).resolve() == target:
                found.append(pid)
        except OSError:
            continue
    return found


def _reap_model_servers() -> int:
    """Kill model servers left behind by an earlier hard exit (frees VRAM).

    Only the real instance does this: a supervisor started against a custom
    data dir (tests, a second profile) must not touch the live bot's GPU.
    """
    try:
        if _data_dir().resolve() != (ROOT / "data").resolve():
            return 0
    except OSError:
        return 0
    pids = model_server_pids()
    for pid in pids:
        _terminate(pid)
    return len(pids)


# -- main loop -----------------------------------------------------------
def run_supervisor() -> int:
    global _LOG_FILE

    data = _data_dir()
    data.mkdir(parents=True, exist_ok=True)
    _LOG_FILE = data / "supervisor.log"
    pid_file = data / "nora.pid"
    stop_file = data / "stop"
    bot_log = data / "bot.out.log"
    restart_seconds = float(os.environ.get("NORA_RESTART_SECONDS", "10") or 10)

    if not _acquire_pid_file(pid_file):
        _log("another supervisor is already running, nothing to do")
        return 0

    # A sentinel left over from a previous stop must not kill this run.
    stop_file.unlink(missing_ok=True)
    reaped = _reap_model_servers()
    if reaped:
        _log(f"cleaned up {reaped} stale model server process(es)")
    command = _command()
    _log(f"supervisor {os.getpid()} starting: {' '.join(command)}")

    child: subprocess.Popen | None = None
    delay = restart_seconds
    stop_requested = False

    try:
        while not stop_requested:
            if stop_file.exists():
                break

            creationflags = 0
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                creationflags = subprocess.CREATE_NO_WINDOW

            started = time.monotonic()
            with open(bot_log, "ab") as out:
                child = subprocess.Popen(
                    command,
                    cwd=str(ROOT),
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                )
            _log(f"bot started as pid {child.pid}")

            while child.poll() is None:
                if stop_file.exists():
                    _log("stop requested, letting the bot shut down cleanly")
                    deadline = time.monotonic() + _GRACE_SECONDS
                    while child.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.25)
                    if child.poll() is None:
                        _log("bot ignored the stop request, killing it")
                        _kill_tree(child.pid)
                    stop_requested = True
                    break
                time.sleep(0.25)

            code = child.poll()
            if stop_requested or stop_file.exists():
                stop_requested = True
                _log(f"bot exited with {code}")
                break

            lived = time.monotonic() - started
            if lived < 30:
                # crash loop: back off instead of hammering the logs
                delay = min(_MAX_RESTART_SECONDS, max(restart_seconds, delay * 2))
            else:
                delay = restart_seconds
            _log(f"bot exited with {code} after {lived:.0f}s, restarting in {delay:.0f}s")

            wait_until = time.monotonic() + delay
            while time.monotonic() < wait_until and not stop_file.exists():
                time.sleep(0.25)
    except KeyboardInterrupt:
        _log("interrupted from the keyboard")
        return 0
    except Exception:
        _log("supervisor crashed")
        raise
    finally:
        if child is not None and child.poll() is None:
            _log("supervisor exiting, killing the bot")
            _kill_tree(child.pid)
        # Anything the bot had adopted from an earlier run would keep the GPU busy.
        leftover = _reap_model_servers()
        if leftover:
            _log(f"stopped {leftover} leftover model server(s)")
        stop_file.unlink(missing_ok=True)
        pid_file.unlink(missing_ok=True)
        _log("supervisor stopped")

    return 0


if __name__ == "__main__":
    raise SystemExit(run_supervisor())
