from __future__ import annotations

import ctypes
import os
import subprocess
import threading
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


@dataclass(frozen=True)
class HiddenControlAction:
    """Click or type into one control on a private invisible desktop."""

    parent_title: str | None = None
    parent_class: str | None = None
    button_text: str | None = None
    button_control_id: int | None = None
    button_class: str | None = None
    type_text: str | None = None
    force_enable: bool = False
    delay_seconds: float = 0.0
    confirm_parent_title: str | None = None
    confirm_parent_class: str | None = None
    retry_seconds: float = 1.0


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
    control_actions: Sequence[HiddenControlAction] = (),
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
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetDlgCtrlID.argtypes = [wintypes.HWND]
    user32.GetDlgCtrlID.restype = ctypes.c_int
    user32.IsWindowEnabled.argtypes = [wintypes.HWND]
    user32.IsWindowEnabled.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.EnableWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
    user32.EnableWindow.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = wintypes.LPARAM
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
    next_button_action = 0
    button_diagnostics = ["not found" for _ in control_actions]
    button_action_ready_at = (
        started + max(0.0, control_actions[0].delay_seconds)
        if control_actions
        else started
    )
    action_dispatched = False
    last_action_dispatch = started
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
            if next_button_action < len(control_actions) and now >= button_action_ready_at:
                action = control_actions[next_button_action]
                confirmation_requested = (
                    action.confirm_parent_title is not None
                    or action.confirm_parent_class is not None
                )
                confirmed = action_dispatched and _hidden_window_exists(
                    user32,
                    desktop,
                    int(process.dwProcessId),
                    title=action.confirm_parent_title,
                    class_name=action.confirm_parent_class,
                )
                if confirmed:
                    next_button_action += 1
                    action_dispatched = False
                    if next_button_action < len(control_actions):
                        button_action_ready_at = now + max(
                            0.0,
                            control_actions[next_button_action].delay_seconds,
                        )
                elif not action_dispatched or now - last_action_dispatch >= max(0.1, action.retry_seconds):
                    applied, diagnostic = _apply_hidden_control_action(
                        user32,
                        desktop,
                        int(process.dwProcessId),
                        action,
                    )
                    if diagnostic:
                        button_diagnostics[next_button_action] = diagnostic
                    if applied:
                        last_action_dispatch = now
                        if confirmation_requested:
                            action_dispatched = True
                        else:
                            next_button_action += 1
                            if next_button_action < len(control_actions):
                                button_action_ready_at = now + max(
                                    0.0,
                                    control_actions[next_button_action].delay_seconds,
                                )
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
                windows = "; ".join(_read_desktop_window_diagnostics(user32, desktop))
                controls = "; ".join(_read_hidden_dialog_controls(user32, desktop))
                kernel32.TerminateProcess(process.hProcess, 124)
                kernel32.WaitForSingleObject(process.hProcess, 5000)
                details = "; ".join(captured_window_text)
                suffix = f"; скрытое окно: {details}" if details else ""
                window_suffix = f"; верхние окна: {windows}" if windows else ""
                control_suffix = f"; контролы диалога: {controls}" if controls else ""
                automation = (
                    f"; автоматизация контролов {next_button_action}/{len(control_actions)}: "
                    + " | ".join(button_diagnostics)
                    if control_actions
                    else ""
                )
                raise TimeoutError(
                    f"Процесс не завершился за {timeout:.0f} секунд{suffix}{window_suffix}"
                    f"{control_suffix}{automation}"
                )
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


def _read_desktop_window_diagnostics(user32: ctypes.WinDLL, desktop: wintypes.HANDLE) -> tuple[str, ...]:
    values: list[str] = []

    def read_text(hwnd: int) -> str:
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(max(1, length + 1))
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        return buffer.value

    def read_class(hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buffer, len(buffer))
        return buffer.value

    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def callback(hwnd: int, _parameter: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        values.append(
            f"{read_text(hwnd)!r}/{read_class(hwnd)!r}"
            f" enabled={bool(user32.IsWindowEnabled(hwnd))}"
            f" visible={bool(user32.IsWindowVisible(hwnd))}"
        )
        return True

    user32.EnumDesktopWindows(desktop, callback, 0)
    return tuple(values)


def _read_hidden_dialog_controls(user32: ctypes.WinDLL, desktop: wintypes.HANDLE) -> tuple[str, ...]:
    values: list[str] = []

    def read_text(hwnd: int) -> str:
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(max(1, length + 1))
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        return buffer.value

    def read_class(hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buffer, len(buffer))
        return buffer.value

    child_callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    window_callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @child_callback_type
    def child_callback(hwnd: int, _parameter: int) -> bool:
        values.append(
            f"{read_text(hwnd)!r}/{read_class(hwnd)!r}"
            f" id={user32.GetDlgCtrlID(hwnd)} enabled={bool(user32.IsWindowEnabled(hwnd))}"
        )
        return True

    @window_callback_type
    def window_callback(hwnd: int, _parameter: int) -> bool:
        if read_class(hwnd) in {"#32770", "TFormSCR"} and user32.IsWindowVisible(hwnd):
            user32.EnumChildWindows(hwnd, child_callback, 0)
        return True

    user32.EnumDesktopWindows(desktop, window_callback, 0)
    return tuple(values)


def _apply_hidden_control_action(
    user32: ctypes.WinDLL,
    desktop: wintypes.HANDLE,
    process_id: int,
    action: HiddenControlAction,
) -> tuple[bool, str | None]:
    """Apply one control action to a matching window owned by ``process_id``."""

    def window_text(hwnd: int) -> str:
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        return buffer.value

    def class_name(hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buffer, len(buffer))
        return buffer.value

    def owned_by_process(hwnd: int) -> bool:
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        return int(owner.value) == process_id

    def matches(value: str, expected: str | None) -> bool:
        return expected is None or value.casefold() == expected.casefold()

    clicked = False
    diagnostic: str | None = None
    parent_window = 0
    child_callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    window_callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @child_callback_type
    def child_callback(hwnd: int, _parameter: int) -> bool:
        nonlocal clicked, diagnostic
        if clicked:
            return False
        if action.button_text is not None and not matches(window_text(hwnd), action.button_text):
            return True
        if action.button_control_id is not None and user32.GetDlgCtrlID(hwnd) != action.button_control_id:
            return True
        if action.button_class is not None and not matches(class_name(hwnd), action.button_class):
            return True
        if action.button_text is None and action.button_control_id is None and action.button_class is None:
            return True
        diagnostic = (
            f"parent={window_text(parent_window)!r}/{class_name(parent_window)!r}, "
            f"parent_enabled={bool(user32.IsWindowEnabled(parent_window))}, "
            f"control={window_text(hwnd)!r}/{class_name(hwnd)!r}, "
            f"id={user32.GetDlgCtrlID(hwnd)}, enabled={bool(user32.IsWindowEnabled(hwnd))}"
        )
        if not user32.IsWindowEnabled(hwnd):
            if not action.force_enable:
                return True
            user32.EnableWindow(hwnd, True)
        if action.type_text is not None:
            user32.SendMessageW(hwnd, 0x00B1, 0, -1)  # EM_SETSEL
            user32.SendMessageW(hwnd, 0x0303, 0, 0)  # WM_CLEAR
            encoded = action.type_text.encode("utf-16-le")
            for offset in range(0, len(encoded), 2):
                code_unit = int.from_bytes(encoded[offset : offset + 2], "little")
                user32.SendMessageW(hwnd, 0x0102, code_unit, 0)  # WM_CHAR
            clicked = True
            return False
        # A Delphi click handler can open a modal SaveDialog. SendMessage would
        # then block this polling loop, so dispatch it on a daemon thread while
        # the main thread continues with the next dialog action.
        threading.Thread(
            target=user32.SendMessageW,
            args=(hwnd, 0x00F5, 0, 0),  # BM_CLICK
            daemon=True,
        ).start()
        clicked = True
        return False

    @window_callback_type
    def window_callback(hwnd: int, _parameter: int) -> bool:
        nonlocal parent_window
        if not owned_by_process(hwnd):
            return True
        if not matches(window_text(hwnd), action.parent_title):
            return True
        if not matches(class_name(hwnd), action.parent_class):
            return True
        parent_window = hwnd
        user32.EnumChildWindows(hwnd, child_callback, 0)
        return not clicked

    user32.EnumDesktopWindows(desktop, window_callback, 0)
    return clicked, diagnostic


def _hidden_window_exists(
    user32: ctypes.WinDLL,
    desktop: wintypes.HANDLE,
    process_id: int,
    *,
    title: str | None,
    class_name: str | None,
) -> bool:
    found = False

    def read_text(hwnd: int) -> str:
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(max(1, length + 1))
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        return buffer.value

    def read_class(hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buffer, len(buffer))
        return buffer.value

    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def callback(hwnd: int, _parameter: int) -> bool:
        nonlocal found
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if int(owner.value) != process_id:
            return True
        if title is not None and read_text(hwnd).casefold() != title.casefold():
            return True
        if class_name is not None and read_class(hwnd).casefold() != class_name.casefold():
            return True
        found = True
        return False

    user32.EnumDesktopWindows(desktop, callback, 0)
    return found
