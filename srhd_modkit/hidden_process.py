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


_HIDDEN_DESKTOP_PREFIX = "SRHDModKit_"
_LEGACY_TOOL_MUTEX = r"Local\SRHD_XenoModKit_LegacyGUI_v1"

_CREATE_SUSPENDED = 0x00000004
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_WAIT_OBJECT_0 = 0x00000000
_WAIT_ABANDONED = 0x00000080
_WAIT_TIMEOUT = 0x00000102
_WAIT_FAILED = 0xFFFFFFFF
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000


@dataclass(frozen=True)
class HiddenProcessResult:
    exit_code: int
    forced_after_outputs: bool
    elapsed_seconds: float
    window_text: tuple[str, ...] = ()
    queue_seconds: float = 0.0
    progress_updates: int = 0
    last_progress_seconds: float = 0.0


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


class STARTUPINFOEX(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", STARTUPINFO),
        ("lpAttributeList", ctypes.c_void_p),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
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
    ]


def run_on_hidden_desktop(
    application: str | Path,
    arguments: Sequence[str],
    *,
    cwd: str | Path,
    expected_outputs: Sequence[str | Path] = (),
    timeout: float | None = 120.0,
    progress_timeout: float | None = None,
    settle_seconds: float = 1.5,
    abort_window_patterns: Sequence[str] = (),
    control_actions: Sequence[HiddenControlAction] = (),
) -> HiddenProcessResult:
    """Run a legacy GUI-subsystem CLI on an invisible Windows desktop.

    Some legacy tools display modal dialogs even in their documented CLI mode.
    Output files are watched until stable; a process waiting on a completion
    dialog is then terminated without ever exposing that dialog to the user.
    ``timeout`` is a hard total deadline. ``progress_timeout`` is a sliding
    inactivity window reset by output, process-I/O, or control-action progress.
    """
    if os.name != "nt":
        raise OSError("Скрытый desktop доступен только в Windows")
    application = Path(application).resolve()
    cwd = Path(cwd).resolve()
    outputs = [Path(path).resolve() for path in expected_outputs]
    if timeout is not None and timeout < 0:
        raise ValueError("Общий таймаут не может быть отрицательным")
    if progress_timeout is not None and progress_timeout < 0:
        raise ValueError("Таймаут прогресса не может быть отрицательным")
    if progress_timeout == 0:
        progress_timeout = None

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
    kernel32.GetProcessIoCounters.argtypes = [wintypes.HANDLE, ctypes.POINTER(IO_COUNTERS)]
    kernel32.GetProcessIoCounters.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    initialize_attributes = getattr(kernel32, "InitializeProcThreadAttributeList", None)
    update_attribute = getattr(kernel32, "UpdateProcThreadAttribute", None)
    delete_attributes = getattr(kernel32, "DeleteProcThreadAttributeList", None)
    if initialize_attributes is not None:
        initialize_attributes.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)]
        initialize_attributes.restype = wintypes.BOOL
    if update_attribute is not None:
        update_attribute.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        update_attribute.restype = wintypes.BOOL
    if delete_attributes is not None:
        delete_attributes.argtypes = [ctypes.c_void_p]
        delete_attributes.restype = None

    process = PROCESS_INFORMATION()
    mutex = wintypes.HANDLE()
    mutex_acquired = False
    desktop = wintypes.HANDLE()
    job = wintypes.HANDLE()
    process_in_job = False
    queue_started = time.monotonic()
    queue_seconds = 0.0
    started = queue_started

    def terminate_process_tree(exit_code: int) -> None:
        if not process.hProcess:
            return
        terminated = False
        if job and process_in_job:
            terminated = bool(kernel32.TerminateJobObject(job, exit_code))
        if not terminated:
            kernel32.TerminateProcess(process.hProcess, exit_code)
        kernel32.WaitForSingleObject(process.hProcess, 5000)

    try:
        mutex = kernel32.CreateMutexW(None, False, _LEGACY_TOOL_MUTEX)
        if not mutex:
            raise ctypes.WinError(ctypes.get_last_error())
        while True:
            mutex_wait = kernel32.WaitForSingleObject(mutex, 100)
            if mutex_wait in {_WAIT_OBJECT_0, _WAIT_ABANDONED}:
                break
            if mutex_wait != _WAIT_TIMEOUT:
                raise ctypes.WinError(ctypes.get_last_error())
        mutex_acquired = True
        queue_seconds = time.monotonic() - queue_started

        desktop_name = f"{_HIDDEN_DESKTOP_PREFIX}{uuid.uuid4().hex}"
        desktop = user32.CreateDesktopW(desktop_name, None, None, 0, 0x000F01FF, None)
        if not desktop:
            raise ctypes.WinError(ctypes.get_last_error())

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        startup = STARTUPINFO()
        startup.cb = ctypes.sizeof(startup)
        startup.lpDesktop = f"WinSta0\\{desktop_name}"
        startup.dwFlags = 0x00000001  # STARTF_USESHOWWINDOW
        startup.wShowWindow = 0  # SW_HIDE
        startup_pointer = ctypes.byref(startup)
        create_flags = _CREATE_UNICODE_ENVIRONMENT | _CREATE_SUSPENDED

        # On current Windows, attach the process to the job atomically at
        # CreateProcess time. The suspended AssignProcess fallback supports
        # older systems while preventing the child from spawning descendants.
        attribute_buffer = None
        attribute_initialized = False
        job_handles = None
        atomic_job = False
        if initialize_attributes and update_attribute and delete_attributes:
            attribute_size = ctypes.c_size_t()
            initialize_attributes(None, 1, 0, ctypes.byref(attribute_size))
            if attribute_size.value:
                attribute_buffer = ctypes.create_string_buffer(attribute_size.value)
                attribute_pointer = ctypes.cast(attribute_buffer, ctypes.c_void_p)
                if initialize_attributes(attribute_pointer, 1, 0, ctypes.byref(attribute_size)):
                    attribute_initialized = True
                    job_handles = (wintypes.HANDLE * 1)(job)
                    if update_attribute(
                        attribute_pointer,
                        0,
                        _PROC_THREAD_ATTRIBUTE_JOB_LIST,
                        ctypes.cast(job_handles, ctypes.c_void_p),
                        ctypes.sizeof(job_handles),
                        None,
                        None,
                    ):
                        startup_ex = STARTUPINFOEX()
                        startup_ex.StartupInfo = startup
                        startup_ex.StartupInfo.cb = ctypes.sizeof(startup_ex)
                        startup_ex.lpAttributeList = attribute_pointer
                        startup_pointer = ctypes.cast(ctypes.byref(startup_ex), ctypes.POINTER(STARTUPINFO))
                        create_flags |= _EXTENDED_STARTUPINFO_PRESENT
                        atomic_job = True

        command = ctypes.create_unicode_buffer(subprocess.list2cmdline([str(application), *map(str, arguments)]))
        create_error = 0
        try:
            created = kernel32.CreateProcessW(
                str(application),
                command,
                None,
                None,
                False,
                create_flags,
                None,
                str(cwd),
                startup_pointer,
                ctypes.byref(process),
            )
            if not created:
                create_error = ctypes.get_last_error()
        finally:
            if attribute_initialized:
                delete_attributes(ctypes.cast(attribute_buffer, ctypes.c_void_p))
        if not created:
            raise ctypes.WinError(create_error)
        process_in_job = atomic_job
        if not process_in_job:
            if not kernel32.AssignProcessToJobObject(job, process.hProcess):
                error = ctypes.get_last_error()
                kernel32.TerminateProcess(process.hProcess, 125)
                kernel32.WaitForSingleObject(process.hProcess, 5000)
                raise ctypes.WinError(error)
            process_in_job = True
        if kernel32.ResumeThread(process.hThread) == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())

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
        last_progress_at = started
        last_progress_probe = started
        last_io_signature: tuple[int, int, int, int] | None = None
        progress_updates = 0

        def process_io_signature() -> tuple[int, int, int, int] | None:
            counters = IO_COUNTERS()
            if not kernel32.GetProcessIoCounters(process.hProcess, ctypes.byref(counters)):
                return None
            return (
                int(counters.ReadOperationCount),
                int(counters.WriteOperationCount),
                int(counters.ReadTransferCount),
                int(counters.WriteTransferCount),
            )

        def mark_progress(at: float) -> None:
            nonlocal last_progress_at, progress_updates
            last_progress_at = at
            progress_updates += 1

        def abort_for_timeout(message: str) -> None:
            nonlocal captured_window_text
            captured_window_text = _read_desktop_window_text(user32, desktop)
            windows = "; ".join(_read_desktop_window_diagnostics(user32, desktop))
            controls = "; ".join(_read_hidden_dialog_controls(user32, desktop))
            terminate_process_tree(124)
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
                f"{message}{suffix}{window_suffix}{control_suffix}{automation}"
            )

        while True:
            wait = kernel32.WaitForSingleObject(process.hProcess, 100)
            now = time.monotonic()
            if wait == _WAIT_OBJECT_0:  # process exited
                break
            if wait == _WAIT_FAILED:
                raise ctypes.WinError(ctypes.get_last_error())
            if progress_timeout is not None and now - last_progress_probe >= 0.25:
                last_progress_probe = now
                io_signature = process_io_signature()
                if (
                    last_io_signature is not None
                    and io_signature is not None
                    and io_signature != last_io_signature
                ):
                    mark_progress(now)
                if io_signature is not None:
                    last_io_signature = io_signature
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
                    mark_progress(now)
                    action_dispatched = False
                    if next_button_action < len(control_actions):
                        button_action_ready_at = now + max(
                            0.0,
                            control_actions[next_button_action].delay_seconds,
                        )
                elif not action_dispatched or now - last_action_dispatch >= max(0.1, action.retry_seconds):
                    was_action_dispatched = action_dispatched
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
                            if not was_action_dispatched:
                                mark_progress(now)
                            action_dispatched = True
                        else:
                            mark_progress(now)
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
                        terminate_process_tree(0)
                        forced = True
                        break
                else:
                    last_signature = signature
                    stable_since = now
                    mark_progress(now)
            # Some legacy tools show a bogus modal after successfully writing
            # their outputs. Once all expected files exist, settle and verify
            # those files instead of treating the post-success dialog as fatal.
            if not outputs_ready and abort_window_patterns and now - last_window_probe >= 0.25:
                last_window_probe = now
                window_text = _read_desktop_window_text(user32, desktop)
                combined = "\n".join(window_text).casefold()
                if any(pattern.casefold() in combined for pattern in abort_window_patterns):
                    terminate_process_tree(1)
                    details = "; ".join(window_text)
                    raise RuntimeError(f"Процесс показал окно ошибки: {details}")
            if progress_timeout is not None and now - last_progress_at >= progress_timeout:
                abort_for_timeout(
                    f"Процесс не показал подтверждённого прогресса за "
                    f"{progress_timeout:.0f} секунд (последний прогресс через "
                    f"{last_progress_at - started:.1f} с после запуска)"
                )
            if timeout is not None and now - started >= timeout:
                abort_for_timeout(
                    f"Процесс превысил общий аварийный лимит {timeout:.0f} секунд, "
                    f"несмотря на {progress_updates} обновлений прогресса"
                )
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(process.hProcess, ctypes.byref(exit_code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return HiddenProcessResult(
            exit_code=int(exit_code.value),
            forced_after_outputs=forced,
            elapsed_seconds=time.monotonic() - started,
            window_text=captured_window_text,
            queue_seconds=queue_seconds,
            progress_updates=progress_updates,
            last_progress_seconds=max(0.0, last_progress_at - started),
        )
    finally:
        # Explicitly terminate the job even after the main process exits: a
        # legacy executable must never leave helper processes behind. If the
        # Python parent is killed before this block, KILL_ON_JOB_CLOSE performs
        # the same cleanup when Windows closes the parent's job handle.
        terminate_process_tree(125)
        if process.hThread:
            kernel32.CloseHandle(process.hThread)
        if process.hProcess:
            kernel32.CloseHandle(process.hProcess)
        if job:
            kernel32.CloseHandle(job)
        if desktop:
            user32.CloseDesktop(desktop)
        if mutex_acquired:
            kernel32.ReleaseMutex(mutex)
        if mutex:
            kernel32.CloseHandle(mutex)


def inspect_hidden_processes() -> dict[str, object]:
    """List active processes whose threads belong to an SRHD private desktop."""

    if os.name != "nt":
        raise OSError("Диагностика скрытых desktop доступна только в Windows")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetProcessWindowStation.restype = wintypes.HANDLE
    user32.EnumDesktopsW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.LPARAM]
    user32.EnumDesktopsW.restype = wintypes.BOOL
    user32.OpenDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user32.OpenDesktopW.restype = wintypes.HANDLE
    user32.EnumDesktopWindows.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.LPARAM]
    user32.EnumDesktopWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    user32.CloseDesktop.restype = wintypes.BOOL
    user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
    user32.GetThreadDesktop.restype = wintypes.HANDLE
    user32.GetUserObjectInformationW.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetUserObjectInformationW.restype = wintypes.BOOL
    _configure_snapshot_api(kernel32)

    desktop_names: set[str] = set()
    desktop_callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.LPWSTR, wintypes.LPARAM)

    @desktop_callback_type
    def desktop_callback(name: str, _parameter: int) -> bool:
        if name.startswith(_HIDDEN_DESKTOP_PREFIX):
            desktop_names.add(name)
        return True

    station = user32.GetProcessWindowStation()
    if not station or not user32.EnumDesktopsW(station, desktop_callback, 0):
        raise ctypes.WinError(ctypes.get_last_error())

    process_ids: dict[str, set[int]] = {name: set() for name in desktop_names}
    window_callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    for name in sorted(desktop_names):
        desktop = user32.OpenDesktopW(name, 0, False, 0x00000041)  # READOBJECTS | ENUMERATE
        if not desktop:
            continue
        try:
            @window_callback_type
            def window_callback(window: int, _parameter: int, *, desktop_name: str = name) -> bool:
                process_id = wintypes.DWORD()
                user32.GetWindowThreadProcessId(window, ctypes.byref(process_id))
                if process_id.value:
                    process_ids.setdefault(desktop_name, set()).add(int(process_id.value))
                return True

            user32.EnumDesktopWindows(desktop, window_callback, 0)
        finally:
            user32.CloseDesktop(desktop)

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
    invalid_handle = ctypes.c_void_p(-1).value
    if snapshot and snapshot != invalid_handle:
        try:
            entry = THREADENTRY32()
            entry.dwSize = ctypes.sizeof(entry)
            available = bool(kernel32.Thread32First(snapshot, ctypes.byref(entry)))
            while available:
                desktop = user32.GetThreadDesktop(entry.th32ThreadID)
                name = _user_object_name(user32, desktop) if desktop else None
                if name and name.startswith(_HIDDEN_DESKTOP_PREFIX):
                    desktop_names.add(name)
                    process_ids.setdefault(name, set()).add(int(entry.th32OwnerProcessID))
                entry.dwSize = ctypes.sizeof(entry)
                available = bool(kernel32.Thread32Next(snapshot, ctypes.byref(entry)))
        finally:
            kernel32.CloseHandle(snapshot)

    process_snapshot = _snapshot_processes(kernel32)
    desktops: list[dict[str, object]] = []
    all_process_ids: set[int] = set()
    for name in sorted(desktop_names, key=str.casefold):
        processes: list[dict[str, object]] = []
        for process_id in sorted(process_ids.get(name, ())):
            all_process_ids.add(process_id)
            details = process_snapshot.get(process_id, {"pid": process_id, "parent_pid": None, "name": None})
            processes.append(details)
        desktops.append({"name": name, "processes": processes})
    return {
        "schema": "srhd-modkit-process-audit-v1",
        "status": "issues" if desktops else "passed",
        "desktop_prefix": _HIDDEN_DESKTOP_PREFIX,
        "desktop_count": len(desktops),
        "process_count": len(all_process_ids),
        "desktops": desktops,
    }


def terminate_hidden_processes() -> dict[str, object]:
    """Terminate known legacy tools found on SRHD private desktops."""

    before = inspect_hidden_processes()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    allowed = {
        "rscript.exe",
        "blockpareditor.exe",
        "blockpareditor.legacy.exe",
        "reseditor_hai128.exe",
    }
    targets: set[int] = set()
    for desktop in before["desktops"]:
        for process in desktop["processes"]:
            targets.add(int(process["pid"]))

    actions: list[dict[str, object]] = []
    for process_id in sorted(targets):
        handle = kernel32.OpenProcess(
            _PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE,
            False,
            process_id,
        )
        if not handle:
            actions.append(
                {"pid": process_id, "status": "failed", "error": str(ctypes.WinError(ctypes.get_last_error()))}
            )
            continue
        try:
            executable = _query_process_image(kernel32, handle)
            executable_name = Path(executable).name.casefold() if executable else None
            if executable_name not in allowed:
                actions.append(
                    {
                        "pid": process_id,
                        "status": "skipped",
                        "executable": executable,
                        "reason": "Процесс на служебном desktop не является известным редактором SRHD",
                    }
                )
                continue
            if not kernel32.TerminateProcess(handle, 125):
                actions.append(
                    {
                        "pid": process_id,
                        "status": "failed",
                        "executable": executable,
                        "error": str(ctypes.WinError(ctypes.get_last_error())),
                    }
                )
                continue
            kernel32.WaitForSingleObject(handle, 5000)
            actions.append({"pid": process_id, "status": "terminated", "executable": executable})
        finally:
            kernel32.CloseHandle(handle)

    if targets:
        time.sleep(0.2)
    remaining = inspect_hidden_processes()
    return {
        "schema": "srhd-modkit-process-cleanup-v1",
        "status": "passed" if remaining["desktop_count"] == 0 else "issues",
        "before": before,
        "actions": actions,
        "remaining": remaining,
    }


def _configure_snapshot_api(kernel32: ctypes.WinDLL) -> None:
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL


def _user_object_name(user32: ctypes.WinDLL, handle: wintypes.HANDLE) -> str | None:
    needed = wintypes.DWORD()
    user32.GetUserObjectInformationW(handle, 2, None, 0, ctypes.byref(needed))  # UOI_NAME
    if not needed.value:
        return None
    characters = max(1, (needed.value + ctypes.sizeof(ctypes.c_wchar) - 1) // ctypes.sizeof(ctypes.c_wchar))
    buffer = ctypes.create_unicode_buffer(characters)
    if not user32.GetUserObjectInformationW(handle, 2, buffer, needed.value, ctypes.byref(needed)):
        return None
    return buffer.value


def _snapshot_processes(kernel32: ctypes.WinDLL) -> dict[int, dict[str, object]]:
    result: dict[int, dict[str, object]] = {}
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return result
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        available = bool(kernel32.Process32FirstW(snapshot, ctypes.byref(entry)))
        while available:
            process_id = int(entry.th32ProcessID)
            handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, process_id)
            executable = None
            if handle:
                try:
                    executable = _query_process_image(kernel32, handle)
                finally:
                    kernel32.CloseHandle(handle)
            result[process_id] = {
                "pid": process_id,
                "parent_pid": int(entry.th32ParentProcessID),
                "name": entry.szExeFile,
                "executable": executable,
            }
            entry.dwSize = ctypes.sizeof(entry)
            available = bool(kernel32.Process32NextW(snapshot, ctypes.byref(entry)))
    finally:
        kernel32.CloseHandle(snapshot)
    return result


def _query_process_image(kernel32: ctypes.WinDLL, handle: wintypes.HANDLE) -> str | None:
    size = wintypes.DWORD(32768)
    buffer = ctypes.create_unicode_buffer(size.value)
    if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
        return None
    return buffer.value


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
