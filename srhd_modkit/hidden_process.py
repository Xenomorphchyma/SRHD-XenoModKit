from __future__ import annotations

import ctypes
import os
import subprocess
import time
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class HiddenProcessResult:
    exit_code: int
    forced_after_outputs: bool
    elapsed_seconds: float
    window_text: tuple[str, ...] = ()


class STARTUPINFO(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


def run_on_hidden_desktop(
    application: str | Path,
    arguments: Sequence[str],
    *,
    cwd: str | Path,
    expected_outputs: Sequence[str | Path] = (),
    timeout: float = 120.0,
    settle_seconds: float = 1.5,
    abort_window_patterns: Sequence[str] = (),
) -> HiddenProcessResult:
    """Run a legacy GUI-subsystem CLI on an invisible Windows desktop.

    Some legacy tools display modal dialogs even in their documented CLI mode.
    Output files are watched until stable; a process waiting on a completion
    dialog is then terminated without ever exposing that dialog to the user.
    """
    if os.name != "nt":
        raise OSError("Скрытый desktop доступен только в Windows")
    application = Path(application).resolve()
    cwd = Path(cwd).resolve()
    outputs = [Path(path).resolve() for path in expected_outputs]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.CreateDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    user32.CreateDesktopW.restype = wintypes.HANDLE
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    user32.CloseDesktop.restype = wintypes.BOOL
    user32.EnumDesktopWindows.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.LPARAM]
    user32.EnumDesktopWindows.restype = wintypes.BOOL
    user32.EnumChildWindows.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.LPARAM]
    user32.EnumChildWindows.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFO),
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    kernel32.CreateProcessW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    desktop_name = f"SRHDModKit_{uuid.uuid4().hex}"
    desktop = user32.CreateDesktopW(desktop_name, None, None, 0, 0x000F01FF, None)
    if not desktop:
        raise ctypes.WinError(ctypes.get_last_error())
    startup = STARTUPINFO()
    startup.cb = ctypes.sizeof(startup)
    startup.lpDesktop = f"WinSta0\\{desktop_name}"
    startup.dwFlags = 0x00000001  # STARTF_USESHOWWINDOW
    startup.wShowWindow = 0  # SW_HIDE
    process = PROCESS_INFORMATION()
    command = ctypes.create_unicode_buffer(subprocess.list2cmdline([str(application), *map(str, arguments)]))
    started = time.monotonic()
    forced = False
    captured_window_text: tuple[str, ...] = ()
    last_signature: tuple[tuple[int, int], ...] | None = None
    stable_since: float | None = None
    last_window_probe = started
    try:
        created = kernel32.CreateProcessW(
            str(application),
            command,
            None,
            None,
            False,
            0x00000400,  # CREATE_UNICODE_ENVIRONMENT
            None,
            str(cwd),
            ctypes.byref(startup),
            ctypes.byref(process),
        )
        if not created:
            raise ctypes.WinError(ctypes.get_last_error())
        while True:
            wait = kernel32.WaitForSingleObject(process.hProcess, 100)
            now = time.monotonic()
            if wait == 0:  # process exited
                break
            outputs_ready = bool(outputs) and all(path.is_file() for path in outputs)
            if outputs_ready:
                signature = tuple((path.stat().st_size, path.stat().st_mtime_ns) for path in outputs)
                if signature == last_signature:
                    stable_since = stable_since or now
                    if now - stable_since >= settle_seconds:
                        captured_window_text = _read_desktop_window_text(user32, desktop)
                        kernel32.TerminateProcess(process.hProcess, 0)
                        kernel32.WaitForSingleObject(process.hProcess, 5000)
                        forced = True
                        break
                else:
                    last_signature = signature
                    stable_since = now
            # Some legacy tools show a bogus modal after successfully writing
            # their outputs. Once all expected files exist, settle and verify
            # those files instead of treating the post-success dialog as fatal.
            if not outputs_ready and abort_window_patterns and now - last_window_probe >= 0.25:
                last_window_probe = now
                window_text = _read_desktop_window_text(user32, desktop)
                combined = "\n".join(window_text).casefold()
                if any(pattern.casefold() in combined for pattern in abort_window_patterns):
                    kernel32.TerminateProcess(process.hProcess, 1)
                    kernel32.WaitForSingleObject(process.hProcess, 5000)
                    details = "; ".join(window_text)
                    raise RuntimeError(f"Процесс показал окно ошибки: {details}")
            if now - started >= timeout:
                captured_window_text = _read_desktop_window_text(user32, desktop)
                kernel32.TerminateProcess(process.hProcess, 124)
                kernel32.WaitForSingleObject(process.hProcess, 5000)
                details = "; ".join(captured_window_text)
                suffix = f"; скрытое окно: {details}" if details else ""
                raise TimeoutError(f"Процесс не завершился за {timeout:.0f} секунд{suffix}")
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(process.hProcess, ctypes.byref(exit_code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return HiddenProcessResult(
            exit_code=int(exit_code.value),
            forced_after_outputs=forced,
            elapsed_seconds=time.monotonic() - started,
            window_text=captured_window_text,
        )
    finally:
        if process.hThread:
            kernel32.CloseHandle(process.hThread)
        if process.hProcess:
            kernel32.CloseHandle(process.hProcess)
        user32.CloseDesktop(desktop)


def _read_desktop_window_text(user32: ctypes.WinDLL, desktop: wintypes.HANDLE) -> tuple[str, ...]:
    """Read diagnostic text from windows on the private desktop."""
    values: list[str] = []

    def add_text(hwnd: int) -> None:
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return
        buffer = ctypes.create_unicode_buffer(length + 1)
        if user32.GetWindowTextW(hwnd, buffer, len(buffer)):
            value = buffer.value.strip()
            if value and value not in values:
                values.append(value)

    child_callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @child_callback_type
    def child_callback(hwnd: int, _parameter: int) -> bool:
        add_text(hwnd)
        return True

    window_callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @window_callback_type
    def window_callback(hwnd: int, _parameter: int) -> bool:
        add_text(hwnd)
        user32.EnumChildWindows(hwnd, child_callback, 0)
        return True

    user32.EnumDesktopWindows(desktop, window_callback, 0)
    return tuple(values)
