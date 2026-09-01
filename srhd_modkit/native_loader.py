from __future__ import annotations

import configparser
import locale
import os
import re
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Iterable

from .files import iter_files, sha256_file
from .module_info import find_module_info, parse_module_info


NATIVE_LOADER_SCHEMA = "srhd-modkit-native-loader-v1"
NATIVE_LOADER_MINIMUM_VERSION = "0.6.5"
NATIVE_LOADER_TESTED_VERSION = "0.6.7"
NATIVE_LOADER_SOURCE_URL = "https://github.com/Xenomorphchyma/XenoMods"
# Public compatibility alias retained for callers that used the original API.
NATIVE_LOADER_VERSION = NATIVE_LOADER_MINIMUM_VERSION
NATIVE_HOST_API = 1
_QUERY_EXPORT = "XenoPlugin_Query"
_INITIALIZE_EXPORT = "XenoPlugin_Initialize"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True, slots=True)
class NativeLoaderIssue:
    severity: str
    code: str
    message: str
    path: str | None = None
    remediation: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "remediation": self.remediation,
        }


@dataclass(frozen=True, slots=True)
class PeDllInfo:
    path: Path
    machine: int
    architecture: str
    pe_kind: str
    is_dll: bool
    exports: tuple[str, ...]
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "machine": f"0x{self.machine:04X}",
            "architecture": self.architecture,
            "pe_kind": self.pe_kind,
            "is_dll": self.is_dll,
            "exports": list(self.exports),
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class NativePluginInfo:
    source: str
    manifest: Path | None
    dll: Path | None
    config: Path | None
    enabled: bool
    legacy: bool
    pe: PeDllInfo | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "manifest": str(self.manifest) if self.manifest is not None else None,
            "dll": str(self.dll) if self.dll is not None else None,
            "config": str(self.config) if self.config is not None else None,
            "enabled": self.enabled,
            "legacy": self.legacy,
            "pe": self.pe.as_dict() if self.pe is not None else None,
        }


@dataclass(frozen=True, slots=True)
class NativeLoaderReport:
    root: Path
    plugins: tuple[NativePluginInfo, ...]
    issues: tuple[NativeLoaderIssue, ...]
    detected: bool
    complete: bool
    schema: str = NATIVE_LOADER_SCHEMA

    @property
    def valid(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "root": str(self.root),
            "loader": {
                "minimum_version": NATIVE_LOADER_VERSION,
                "tested_version": NATIVE_LOADER_TESTED_VERSION,
                "source": NATIVE_LOADER_SOURCE_URL,
                "host_api": NATIVE_HOST_API,
                "activation": "active-ModCFG-entry + ModuleInfo Priority/Dependence",
                "runtime_query_executed": False,
            },
            "detected": self.detected,
            "valid": self.valid,
            "complete": self.complete,
            "plugins": [plugin.as_dict() for plugin in self.plugins],
            "issues": [issue.as_dict() for issue in self.issues],
            "summary": {
                "plugins": len(self.plugins),
                "enabled": sum(plugin.enabled for plugin in self.plugins),
                "errors": sum(issue.severity == "error" for issue in self.issues),
                "warnings": sum(issue.severity == "warning" for issue in self.issues),
            },
        }


def _u16(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 2 > len(data):
        raise ValueError("PE обрезан при чтении uint16")
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise ValueError("PE обрезан при чтении uint32")
    return struct.unpack_from("<I", data, offset)[0]


def inspect_native_dll(path: str | Path) -> PeDllInfo:
    """Inspect a plugin DLL without loading or executing untrusted code."""

    source = Path(path).resolve()
    data = source.read_bytes()
    if len(data) < 64 or data[:2] != b"MZ":
        raise ValueError("файл не имеет DOS/PE сигнатуры MZ")
    pe_offset = _u32(data, 0x3C)
    if pe_offset + 24 > len(data) or data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise ValueError("файл не имеет сигнатуры PE\\0\\0")
    coff = pe_offset + 4
    machine = _u16(data, coff)
    section_count = _u16(data, coff + 2)
    optional_size = _u16(data, coff + 16)
    characteristics = _u16(data, coff + 18)
    optional = coff + 20
    magic = _u16(data, optional)
    if magic == 0x10B:
        pe_kind = "PE32"
        directory = optional + 96
    elif magic == 0x20B:
        pe_kind = "PE32+"
        directory = optional + 112
    else:
        raise ValueError(f"неподдерживаемая PE optional-header magic 0x{magic:04X}")
    if optional + optional_size > len(data):
        raise ValueError("PE optional header обрезан")
    export_rva = _u32(data, directory) if directory + 8 <= optional + optional_size else 0
    section_table = optional + optional_size
    sections: list[tuple[int, int, int, int]] = []
    for index in range(section_count):
        entry = section_table + index * 40
        if entry + 40 > len(data):
            raise ValueError("таблица секций PE обрезана")
        virtual_size = _u32(data, entry + 8)
        virtual_address = _u32(data, entry + 12)
        raw_size = _u32(data, entry + 16)
        raw_offset = _u32(data, entry + 20)
        sections.append((virtual_address, max(virtual_size, raw_size), raw_offset, raw_size))

    def rva_offset(rva: int, size: int = 1) -> int:
        for virtual, span, raw, raw_size in sections:
            if virtual <= rva and rva + size <= virtual + span:
                offset = raw + (rva - virtual)
                if offset + size > len(data) or rva - virtual + size > raw_size:
                    raise ValueError(f"RVA 0x{rva:X} указывает за файловые данные секции")
                return offset
        if rva + size <= len(data):
            return rva
        raise ValueError(f"RVA 0x{rva:X} не принадлежит PE-секции")

    exports: list[str] = []
    if export_rva:
        export = rva_offset(export_rva, 40)
        number_of_names = _u32(data, export + 24)
        names_rva = _u32(data, export + 32)
        if number_of_names > 65536:
            raise ValueError(f"неправдоподобное число экспортов PE: {number_of_names}")
        names_offset = rva_offset(names_rva, number_of_names * 4) if number_of_names else 0
        for index in range(number_of_names):
            name_rva = _u32(data, names_offset + index * 4)
            name_offset = rva_offset(name_rva)
            end = data.find(b"\0", name_offset, min(len(data), name_offset + 4096))
            if end < 0:
                raise ValueError("имя PE-экспорта не завершено NUL")
            exports.append(data[name_offset:end].decode("ascii", errors="replace"))

    architecture = {0x014C: "x86", 0x8664: "x64", 0x01C4: "arm"}.get(
        machine, f"machine-0x{machine:04X}"
    )
    return PeDllInfo(
        source,
        machine,
        architecture,
        pe_kind,
        bool(characteristics & 0x2000),
        tuple(sorted(set(exports), key=str.casefold)),
        sha256_file(source),
    )


def _decode_ini(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith(b"\xff\xfe"):
        return data.decode("utf-16")
    if data.startswith(b"\xfe\xff"):
        raise UnicodeError("XenoNativeLoader Host API V1 поддерживает UTF-16LE BOM, но не UTF-16BE")
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        encoding = locale.getpreferredencoding(False) or "cp1251"
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            return data.decode("cp1251")


def _read_ini(path: Path) -> configparser.ConfigParser:
    # Windows profile APIs tolerate repeated keys/sections in old INI files.
    # Do not reject a mod config more strictly than the Loader itself.
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str.casefold
    parser.read_string(_decode_ini(path), source=str(path))
    return parser


def _section_name(parser: configparser.ConfigParser, wanted: str) -> str | None:
    folded = wanted.casefold()
    return next((section for section in parser.sections() if section.casefold() == folded), None)


def _bool_value(
    parser: configparser.ConfigParser,
    section: str,
    key: str,
    default: bool,
) -> tuple[bool, bool]:
    actual_section = _section_name(parser, section)
    if actual_section is None or not parser.has_option(actual_section, key):
        return default, True
    raw = parser.get(actual_section, key).strip().casefold()
    if raw in _TRUE_VALUES:
        return True, True
    if raw in _FALSE_VALUES:
        return False, True
    return default, False


def _inside(root: Path, path: Path) -> bool:
    resolved_root = root.resolve()
    resolved = path.resolve(strict=False)
    return resolved == resolved_root or resolved_root in resolved.parents


def _manifest_relative_path(manifest: Path, value: str, root: Path) -> Path:
    raw = value.strip().replace("/", "\\")
    windows = PureWindowsPath(raw)
    if not raw or windows.is_absolute() or windows.drive or ".." in windows.parts:
        raise ValueError(f"небезопасный относительный путь {value!r}")
    candidate = manifest.parent.joinpath(*windows.parts)
    if not _inside(root, candidate):
        raise ValueError(f"путь выходит за корень мода: {value!r}")
    return candidate.resolve(strict=False)


def _native_tree(root: Path) -> Path | None:
    return next(
        (child for child in root.iterdir() if child.is_dir() and child.name.casefold() == "native"),
        None,
    )


def _plugin_pe_issues(path: Path, pe: PeDllInfo, legacy: bool) -> list[NativeLoaderIssue]:
    issues: list[NativeLoaderIssue] = []
    if pe.architecture != "x86" or pe.pe_kind != "PE32":
        issues.append(
            NativeLoaderIssue(
                "error",
                "native-loader-plugin-not-x86",
                f"Space Rangers HD и XenoNativeLoader требуют x86 PE32 DLL, получено {pe.architecture} {pe.pe_kind}",
                str(path),
                "Соберите плагин компилятором MSVC x86.",
            )
        )
    if not pe.is_dll:
        issues.append(
            NativeLoaderIssue(
                "error",
                "native-loader-plugin-not-dll",
                "PE-файл не имеет флага IMAGE_FILE_DLL",
                str(path),
            )
        )
    if not legacy:
        folded = {name.casefold() for name in pe.exports}
        missing = [
            name
            for name in (_QUERY_EXPORT, _INITIALIZE_EXPORT)
            if name.casefold() not in folded
        ]
        if missing:
            issues.append(
                NativeLoaderIssue(
                    "error",
                    "native-loader-plugin-abi-export-missing",
                    "Современный plugin-DLL не экспортирует " + ", ".join(missing),
                    str(path),
                    "Экспортируйте обе C ABI-функции через extern \"C\" и .def без декорированного публичного имени.",
                )
            )
    return issues


def validate_native_mod(path: str | Path) -> NativeLoaderReport:
    root = Path(path).resolve()
    if not root.is_dir():
        raise NotADirectoryError(root)
    files = iter_files(root)
    native = _native_tree(root)
    root_manifests = [
        item for item in files
        if item.parent == root and item.name.casefold() == "xenonativeplugin.ini"
    ]
    native_files = iter_files(native) if native is not None else []
    manifests = root_manifests + [
        item
        for item in native_files
        if item.name.casefold() == "xenonativeplugin.ini"
        or item.name.casefold().endswith(".xenomanifest.ini")
    ]
    automatic_dlls = [
        item for item in native_files if item.name.casefold().endswith(".xenoplugin.dll")
    ]
    detected = bool(manifests or automatic_dlls)
    plugins: list[NativePluginInfo] = []
    issues: list[NativeLoaderIssue] = []
    discovered_paths: set[str] = set()

    for bundled in files:
        if bundled.name.casefold() not in {"xenocore.dll", "dsound.dll", "xenonative.ini"}:
            continue
        issues.append(
            NativeLoaderIssue(
                "warning",
                "native-loader-runtime-bundled-in-mod",
                f"{bundled.name} относится к установке XenoNativeLoader рядом с Rangers.exe, а не к каталогу обычного мода",
                str(bundled),
                "Поставляйте plugin-DLL мода отдельно; сам Loader устанавливается пользователем один раз.",
            )
        )

    def inspect_plugin(
        *,
        source: str,
        manifest: Path | None,
        dll: Path | None,
        config: Path | None,
        enabled: bool,
        legacy: bool,
    ) -> None:
        pe: PeDllInfo | None = None
        if enabled and dll is not None:
            key = str(dll.resolve(strict=False)).casefold()
            if key in discovered_paths:
                issues.append(
                    NativeLoaderIssue(
                        "warning",
                        "native-loader-plugin-discovered-twice",
                        "Одна DLL обнаруживается manifest- и automatic-механизмом; Loader оставит только первое в порядке discovery",
                        str(dll),
                    )
                )
            else:
                discovered_paths.add(key)
            if not dll.is_file():
                issues.append(
                    NativeLoaderIssue(
                        "error",
                        "native-loader-plugin-dll-missing",
                        "Manifest ссылается на отсутствующую plugin-DLL",
                        str(dll),
                    )
                )
            else:
                try:
                    pe = inspect_native_dll(dll)
                    issues.extend(_plugin_pe_issues(dll, pe, legacy))
                except Exception as exc:
                    issues.append(
                        NativeLoaderIssue(
                            "error",
                            "native-loader-plugin-pe-invalid",
                            str(exc),
                            str(dll),
                        )
                    )
        plugins.append(NativePluginInfo(source, manifest, dll, config, enabled, legacy, pe))

    for manifest in sorted(manifests, key=lambda value: str(value).casefold()):
        try:
            parser = _read_ini(manifest)
        except Exception as exc:
            issues.append(
                NativeLoaderIssue("error", "native-loader-manifest-invalid", str(exc), str(manifest))
            )
            plugins.append(NativePluginInfo("manifest", manifest, None, None, False, False))
            continue
        plugin_section = _section_name(parser, "Plugin")
        if plugin_section is None:
            issues.append(
                NativeLoaderIssue(
                    "error",
                    "native-loader-manifest-plugin-section-missing",
                    "Manifest не содержит секцию [Plugin]",
                    str(manifest),
                )
            )
            plugins.append(NativePluginInfo("manifest", manifest, None, None, False, False))
            continue
        enabled, valid_enabled = _bool_value(parser, "Plugin", "Enabled", True)
        legacy, valid_legacy = _bool_value(parser, "Plugin", "Legacy", False)
        for key, valid in (("Enabled", valid_enabled), ("Legacy", valid_legacy)):
            if not valid:
                issues.append(
                    NativeLoaderIssue(
                        "warning",
                        "native-loader-manifest-bool-invalid",
                        f"[Plugin] {key} имеет неизвестное логическое значение; Loader применит default",
                        str(manifest),
                    )
                )
        if not enabled:
            inspect_plugin(
                source="manifest", manifest=manifest, dll=None, config=manifest, enabled=False, legacy=legacy
            )
            continue
        dll_value = parser.get(plugin_section, "Dll", fallback="").strip()
        if not dll_value:
            issues.append(
                NativeLoaderIssue(
                    "error",
                    "native-loader-manifest-dll-missing",
                    "В [Plugin] отсутствует обязательный Dll",
                    str(manifest),
                )
            )
            inspect_plugin(
                source="manifest", manifest=manifest, dll=None, config=manifest, enabled=True, legacy=legacy
            )
            continue
        try:
            dll = _manifest_relative_path(manifest, dll_value, root)
            config_value = parser.get(plugin_section, "Config", fallback="").strip()
            config = (
                _manifest_relative_path(manifest, config_value, root)
                if config_value
                else manifest
            )
        except ValueError as exc:
            issues.append(
                NativeLoaderIssue(
                    "error", "native-loader-manifest-path-unsafe", str(exc), str(manifest)
                )
            )
            inspect_plugin(
                source="manifest", manifest=manifest, dll=None, config=None, enabled=True, legacy=legacy
            )
            continue
        if config != manifest and not config.is_file():
            issues.append(
                NativeLoaderIssue(
                    "warning",
                    "native-loader-plugin-config-missing",
                    "Manifest указывает отсутствующий Config; plugin получит путь, но сможет использовать только собственные defaults",
                    str(config),
                )
            )
        inspect_plugin(
            source="manifest", manifest=manifest, dll=dll, config=config, enabled=True, legacy=legacy
        )

    for dll in sorted(automatic_dlls, key=lambda value: str(value).casefold()):
        config = dll.with_suffix(".ini")
        enabled = True
        if config.is_file():
            try:
                parser = _read_ini(config)
                enabled, valid = _bool_value(parser, "Plugin", "Enabled", True)
                if not valid:
                    issues.append(
                        NativeLoaderIssue(
                            "warning",
                            "native-loader-config-bool-invalid",
                            "[Plugin] Enabled имеет неизвестное значение; Loader применит default=1",
                            str(config),
                        )
                    )
            except Exception as exc:
                issues.append(
                    NativeLoaderIssue(
                        "warning",
                        "native-loader-plugin-config-unreadable",
                        f"ModKit не смог прочитать automatic config: {exc}; Loader может применить defaults",
                        str(config),
                    )
                )
        inspect_plugin(
            source="automatic",
            manifest=None,
            dll=dll,
            config=config,
            enabled=enabled,
            legacy=False,
        )

    # Static inspection deliberately never calls Query: doing so would execute
    # arbitrary mod code in ModKit's process. The loader itself resolves IDs
    # and exclusive capabilities at game start.
    complete = not detected
    return NativeLoaderReport(root, tuple(plugins), tuple(issues), detected, complete)


_SDK_HEADER = r'''#pragma once
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <cstddef>
#include <cstdint>

static const std::uint32_t XENO_NATIVE_HOST_API_V1 = 1;
static const std::uint64_t XENO_PLUGIN_CAP_GALAXY_GENERATOR = 1ull << 0;

typedef void (WINAPI* XenoHostLogFn)(const wchar_t*, const wchar_t*);
typedef BOOL (WINAPI* XenoConfigGetBoolFn)(const wchar_t*, const wchar_t*, const wchar_t*, BOOL);
typedef int (WINAPI* XenoConfigGetIntFn)(const wchar_t*, const wchar_t*, const wchar_t*, int, int, int);
typedef DWORD (WINAPI* XenoConfigGetStringFn)(const wchar_t*, const wchar_t*, const wchar_t*, const wchar_t*, wchar_t*, DWORD);

struct XenoPluginHostV1 {
    std::uint32_t size; std::uint32_t apiVersion; HMODULE gameModule;
    const wchar_t* gameRoot; const wchar_t* executablePath; const wchar_t* pluginRoot;
    const wchar_t* configPath; XenoHostLogFn log; XenoConfigGetBoolFn configGetBool;
    XenoConfigGetIntFn configGetInt; XenoConfigGetStringFn configGetString;
};
static const std::uint32_t XENO_PLUGIN_HOST_V1_BASE_SIZE = static_cast<std::uint32_t>(offsetof(XenoPluginHostV1, configGetBool));

struct XenoPluginInfoV1 {
    std::uint32_t size; std::uint32_t requiredHostApi; wchar_t id[64];
    wchar_t version[32]; wchar_t description[160]; std::uint64_t exclusiveCapabilities;
};
static const std::uint32_t XENO_PLUGIN_INFO_V1_BASE_SIZE = static_cast<std::uint32_t>(offsetof(XenoPluginInfoV1, exclusiveCapabilities));
'''


def _cpp_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def initialize_native_mod(
    path: str | Path,
    *,
    plugin_id: str,
    name: str | None = None,
    version: str = "0.1.0",
    description: str = "Space Rangers HD native plugin",
    author: str = "Xenomorphchyma",
    capability: str = "none",
    overwrite: bool = False,
) -> dict[str, Any]:
    """Create a minimal, auditable Host API V1 plugin tested with Loader 0.6.7."""

    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,62}", plugin_id) is None:
        raise ValueError("plugin id должен начинаться с латинской буквы и содержать до 63 ASCII-символов")
    if capability not in {"none", "galaxy-generator"}:
        raise ValueError("capability должен быть none или galaxy-generator")
    root = Path(path).resolve()
    project_file = root.parent / "srhd-modkit.toml"
    generated = [
        root / "ModuleInfo.txt",
        root / "Native" / f"{plugin_id}.XenoPlugin.ini",
        root / "SOURCE" / "Native" / plugin_id / "xeno_plugin_api.h",
        root / "SOURCE" / "Native" / plugin_id / f"{plugin_id}.cpp",
        root / "SOURCE" / "Native" / plugin_id / f"{plugin_id}.def",
        root / "SOURCE" / "Native" / "build.ps1",
        project_file,
    ]
    existing = [item for item in generated if item.exists()]
    if existing and not overwrite:
        raise FileExistsError("Не перезаписаны существующие файлы: " + ", ".join(str(item) for item in existing))
    for parent in {item.parent for item in generated}:
        parent.mkdir(parents=True, exist_ok=True)
    selected_name = name or plugin_id
    capability_cpp = (
        "XENO_PLUGIN_CAP_GALAXY_GENERATOR" if capability == "galaxy-generator" else "0"
    )
    cpp = f'''#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include "xeno_plugin_api.h"

extern "C" BOOL WINAPI XenoPlugin_Query(XenoPluginInfoV1* info) {{
    if (!info || info->size < XENO_PLUGIN_INFO_V1_BASE_SIZE) return FALSE;
    info->requiredHostApi = XENO_NATIVE_HOST_API_V1;
    lstrcpynW(info->id, L"{_cpp_literal(plugin_id)}", 64);
    lstrcpynW(info->version, L"{_cpp_literal(version)}", 32);
    lstrcpynW(info->description, L"{_cpp_literal(description)}", 160);
    if (info->size >= sizeof(XenoPluginInfoV1)) info->exclusiveCapabilities = {capability_cpp};
    return TRUE;
}}

extern "C" DWORD WINAPI XenoPlugin_Initialize(const XenoPluginHostV1* host) {{
    if (!host || host->size < XENO_PLUGIN_HOST_V1_BASE_SIZE || host->apiVersion < XENO_NATIVE_HOST_API_V1) return 1;
    if (host->log) host->log(L"{_cpp_literal(plugin_id)}", L"initialized");
    // Install only signature-verified hooks here. Return non-zero before partial mutation on unsupported EXE.
    return 0;
}}

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID) {{
    if (reason == DLL_PROCESS_ATTACH) DisableThreadLibraryCalls(instance);
    return TRUE;
}}
'''
    definition = f'''LIBRARY "{plugin_id}.XenoPlugin.dll"
EXPORTS
    XenoPlugin_Query=_XenoPlugin_Query@4
    XenoPlugin_Initialize=_XenoPlugin_Initialize@4
'''
    build_script = f'''param([string]$OutputDirectory = (Join-Path $PSScriptRoot "..\\..\\Native"))
$ErrorActionPreference = "Stop"
$vswhere = "${{env:ProgramFiles(x86)}}\\Microsoft Visual Studio\\Installer\\vswhere.exe"
if (-not (Test-Path -LiteralPath $vswhere -PathType Leaf)) {{ throw "Install MSVC x86 Build Tools (vswhere.exe missing)." }}
$cl = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -find "VC\\Tools\\MSVC\\**\\bin\\Hostx64\\x86\\cl.exe" | Select-Object -First 1
if (-not $cl) {{ throw "MSVC x86 compiler not found." }}
$cl = (Get-Item -LiteralPath $cl).FullName
$vc = [IO.Path]::GetFullPath((Join-Path (Split-Path $cl -Parent) "..\\..\\.."))
$kits = "${{env:ProgramFiles(x86)}}\\Windows Kits\\10"
$sdk = Get-ChildItem -LiteralPath (Join-Path $kits "Include") -Directory | Where-Object {{ Test-Path (Join-Path $_.FullName "um\\Windows.h") }} | Sort-Object Name -Descending | Select-Object -First 1 -ExpandProperty Name
if (-not $sdk) {{ throw "Windows SDK not found." }}
$oldInclude, $oldLib = $env:INCLUDE, $env:LIB
$temp = Join-Path ([IO.Path]::GetTempPath()) ("srhd-native-" + [Guid]::NewGuid().ToString("N"))
try {{
    New-Item -ItemType Directory -Path $temp,$OutputDirectory -Force | Out-Null
    $env:INCLUDE = @((Join-Path $vc "include"),(Join-Path $kits "Include\\$sdk\\ucrt"),(Join-Path $kits "Include\\$sdk\\shared"),(Join-Path $kits "Include\\$sdk\\um")) -join ";"
    $env:LIB = @((Join-Path $vc "lib\\x86"),(Join-Path $kits "Lib\\$sdk\\ucrt\\x86"),(Join-Path $kits "Lib\\$sdk\\um\\x86")) -join ";"
    $dll = Join-Path $temp "{plugin_id}.XenoPlugin.dll"
    & $cl /nologo /MT /O2 /EHsc /std:c++17 /W4 /permissive- /LD (Join-Path $PSScriptRoot "{plugin_id}\\{plugin_id}.cpp") "/I$(Join-Path $PSScriptRoot "{plugin_id}")" ("/Fo" + $temp + "\\") "/Fe:$dll" /link /Brepro "/DEF:$(Join-Path $PSScriptRoot "{plugin_id}\\{plugin_id}.def")"
    if ($LASTEXITCODE -ne 0) {{ throw "MSVC failed with exit code $LASTEXITCODE." }}
    Copy-Item -LiteralPath $dll -Destination (Join-Path $OutputDirectory "{plugin_id}.XenoPlugin.dll") -Force
}} finally {{
    $env:INCLUDE, $env:LIB = $oldInclude, $oldLib
    if (Test-Path -LiteralPath $temp) {{ [IO.Directory]::Delete($temp, $true) }}
}}
'''
    relative_mod = root.relative_to(project_file.parent).as_posix()
    project_text = f'''schema = "srhd-modkit-project-v1"
name = "{selected_name}"
mod_root = "{relative_mod}"
prefix = "OtherMods/{root.name}"
default_variant = "release"
build_root = ".srhd-build"
cache_root = ".srhd-cache"

[variants.release]

[[external_builds]]
id = "{plugin_id.casefold()}-native"
kind = "xeno-native-plugin"
project = "{relative_mod}/SOURCE/Native/build.ps1"
mode = "prebuilt"
outputs = ["{relative_mod}/Native/{plugin_id}.XenoPlugin.dll"]
'''
    contents: dict[Path, tuple[str, str]] = {
        root / "ModuleInfo.txt": (
            f"Name={selected_name}\nAuthor={author}\nSection=OtherMods\nPriority=0\nLanguages=Rus\n",
            "cp1251",
        ),
        root / "Native" / f"{plugin_id}.XenoPlugin.ini": ("[Plugin]\nEnabled=1\n", "utf-8"),
        root / "SOURCE" / "Native" / plugin_id / "xeno_plugin_api.h": (_SDK_HEADER, "utf-8"),
        root / "SOURCE" / "Native" / plugin_id / f"{plugin_id}.cpp": (cpp, "utf-8"),
        root / "SOURCE" / "Native" / plugin_id / f"{plugin_id}.def": (definition, "ascii"),
        root / "SOURCE" / "Native" / "build.ps1": (build_script, "utf-8"),
        project_file: (project_text, "utf-8"),
    }
    temporary_files: list[tuple[Path, Path]] = []
    try:
        for destination, (content, encoding) in contents.items():
            descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{destination.name}.srhd-", dir=destination.parent
            )
            os.close(descriptor)
            temporary = Path(temp_name)
            temporary.write_text(content, encoding=encoding)
            temporary_files.append((temporary, destination))
        for temporary, destination in temporary_files:
            os.replace(temporary, destination)
    finally:
        for temporary, _destination in temporary_files:
            temporary.unlink(missing_ok=True)
    return {
        "schema": "srhd-modkit-native-loader-init-v1",
        "root": str(root),
        "plugin_id": plugin_id,
        "loader_minimum_version": NATIVE_LOADER_VERSION,
        "loader_tested_version": NATIVE_LOADER_TESTED_VERSION,
        "loader_source": NATIVE_LOADER_SOURCE_URL,
        "host_api": NATIVE_HOST_API,
        "files": [str(path) for path in generated],
        "next": [
            f"powershell -File {root / 'SOURCE' / 'Native' / 'build.ps1'}",
            f"python -B srhd.py native validate {root} --json",
            f"python -B srhd.py project build {project_file.parent} --json",
        ],
    }


__all__ = [
    "NATIVE_HOST_API",
    "NATIVE_LOADER_MINIMUM_VERSION",
    "NATIVE_LOADER_SCHEMA",
    "NATIVE_LOADER_SOURCE_URL",
    "NATIVE_LOADER_TESTED_VERSION",
    "NATIVE_LOADER_VERSION",
    "NativeLoaderIssue",
    "NativeLoaderReport",
    "NativePluginInfo",
    "PeDllInfo",
    "initialize_native_mod",
    "inspect_native_dll",
    "validate_native_mod",
]
