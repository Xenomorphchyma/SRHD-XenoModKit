from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import tempfile
from ctypes import wintypes
from pathlib import Path


_MANIFEST = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <assemblyIdentity type="win32" name="SRHD.BlockParEditor.Legacy" version="1.0.0.0" processorArchitecture="x86"/>
  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3">
    <security><requestedPrivileges><requestedExecutionLevel level="asInvoker" uiAccess="false"/></requestedPrivileges></security>
  </trustInfo>
  <application xmlns="urn:schemas-microsoft-com:asm.v3">
    <windowsSettings><activeCodePage xmlns="http://schemas.microsoft.com/SMI/2019/WindowsSettings">ru-RU</activeCodePage></windowsSettings>
  </application>
</assembly>'''


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_legacy_codepage_executable(
    original: str | Path,
    derived: str | Path,
) -> Path:
    """Build a locale-scoped copy of a legacy executable.

    Windows configured with system ACP 65001 breaks VB6 ANSI/Unicode
    conversions. Windows 11 supports a locale-specific ``activeCodePage`` in
    the process manifest, so only this private codec process uses CP1251. The
    original vendor executable is never modified.
    """
    if os.name != "nt":
        raise OSError("Manifest legacy-кодировки поддерживается только в Windows")
    original = Path(original).resolve()
    derived = Path(derived).resolve()
    if not original.is_file():
        raise FileNotFoundError(original)
    marker = derived.with_suffix(derived.suffix + ".json")
    source_sha256 = _sha256(original)
    manifest_sha256 = hashlib.sha256(_MANIFEST).hexdigest()
    if derived.is_file() and marker.is_file():
        try:
            state = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if (
            state.get("source_sha256") == source_sha256
            and state.get("manifest_sha256") == manifest_sha256
            and state.get("derived_sha256") == _sha256(derived)
        ):
            return derived

    derived.parent.mkdir(parents=True, exist_ok=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]
    kernel32.BeginUpdateResourceW.restype = wintypes.HANDLE
    kernel32.UpdateResourceW.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.WORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.UpdateResourceW.restype = wintypes.BOOL
    kernel32.EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]
    kernel32.EndUpdateResourceW.restype = wintypes.BOOL

    with tempfile.TemporaryDirectory(prefix=".srhd-manifest-", dir=derived.parent) as name:
        staged = Path(name) / derived.name
        shutil.copy2(original, staged)
        handle = kernel32.BeginUpdateResourceW(str(staged), False)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        committed = False
        try:
            buffer = ctypes.create_string_buffer(_MANIFEST)
            if not kernel32.UpdateResourceW(
                handle,
                ctypes.c_void_p(24),  # RT_MANIFEST
                ctypes.c_void_p(1),
                1033,
                buffer,
                len(_MANIFEST),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel32.EndUpdateResourceW(handle, False):
                raise ctypes.WinError(ctypes.get_last_error())
            committed = True
        finally:
            if not committed:
                kernel32.EndUpdateResourceW(handle, True)
        os.replace(staged, derived)

    state = {
        "purpose": "BlockParEditor 1.9 with per-process ru-RU legacy code page",
        "source": str(original),
        "source_sha256": source_sha256,
        "manifest_sha256": manifest_sha256,
        "derived_sha256": _sha256(derived),
    }
    marker.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return derived
