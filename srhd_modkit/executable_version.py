from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, order=True)
class ExecutableVersion:
    """Windows fixed-file version without a dependency on pywin32/pefile."""

    major: int
    minor: int
    patch: int = 0
    build: int = 0

    @property
    def parts(self) -> tuple[int, int, int, int]:
        return (self.major, self.minor, self.patch, self.build)

    def at_least(self, major: int, minor: int) -> bool:
        return self.parts >= (major, minor, 0, 0)

    def dotted(self, *, components: int = 2) -> str:
        if components < 1 or components > 4:
            raise ValueError("Количество компонентов версии должно быть от 1 до 4")
        return ".".join(str(value) for value in self.parts[:components])


class _VSFixedFileInfo(ctypes.Structure):
    _fields_ = [
        ("dwSignature", wintypes.DWORD),
        ("dwStrucVersion", wintypes.DWORD),
        ("dwFileVersionMS", wintypes.DWORD),
        ("dwFileVersionLS", wintypes.DWORD),
        ("dwProductVersionMS", wintypes.DWORD),
        ("dwProductVersionLS", wintypes.DWORD),
        ("dwFileFlagsMask", wintypes.DWORD),
        ("dwFileFlags", wintypes.DWORD),
        ("dwFileOS", wintypes.DWORD),
        ("dwFileType", wintypes.DWORD),
        ("dwFileSubtype", wintypes.DWORD),
        ("dwFileDateMS", wintypes.DWORD),
        ("dwFileDateLS", wintypes.DWORD),
    ]


def detect_executable_version(path: str | Path) -> ExecutableVersion | None:
    """Read the PE VERSIONINFO resource, returning ``None`` when unavailable."""

    candidate = Path(path).resolve()
    if os.name != "nt" or not candidate.is_file():
        return None
    try:
        version = ctypes.WinDLL("version", use_last_error=True)
        version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        version.GetFileVersionInfoW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        version.GetFileVersionInfoW.restype = wintypes.BOOL
        version.VerQueryValueW.argtypes = [
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.UINT),
        ]
        version.VerQueryValueW.restype = wintypes.BOOL

        ignored = wintypes.DWORD()
        size = version.GetFileVersionInfoSizeW(str(candidate), ctypes.byref(ignored))
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(str(candidate), 0, size, buffer):
            return None
        pointer = ctypes.c_void_p()
        length = wintypes.UINT()
        if not version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
            return None
        if not pointer.value or length.value < ctypes.sizeof(_VSFixedFileInfo):
            return None
        fixed = ctypes.cast(pointer, ctypes.POINTER(_VSFixedFileInfo)).contents
        if fixed.dwSignature != 0xFEEF04BD:
            return None
        return ExecutableVersion(
            fixed.dwFileVersionMS >> 16,
            fixed.dwFileVersionMS & 0xFFFF,
            fixed.dwFileVersionLS >> 16,
            fixed.dwFileVersionLS & 0xFFFF,
        )
    except (OSError, ValueError):
        return None

