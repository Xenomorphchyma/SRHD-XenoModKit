from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from srhd_modkit.audit import audit_mod
from srhd_modkit.native_loader import (
    initialize_native_mod,
    inspect_native_dll,
    validate_native_mod,
)
from srhd_modkit.project import load_project
from srhd_modkit.project_ops import initialize_project
from srhd_modkit.schemas import validate_schema_document


def _plugin_dll(*, machine: int = 0x014C, magic: int = 0x10B) -> bytes:
    pe_offset = 0x80
    optional_size = 0xE0 if magic == 0x10B else 0xF0
    optional = pe_offset + 24
    section = optional + optional_size
    data = bytearray(0x600)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, pe_offset)
    data[pe_offset : pe_offset + 4] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", data, pe_offset + 4, machine, 1, 0, 0, 0, optional_size, 0x2102)
    struct.pack_into("<H", data, optional, magic)
    directory = optional + (96 if magic == 0x10B else 112)
    struct.pack_into("<II", data, directory, 0x1000, 0x120)
    data[section : section + 8] = b".edata\0\0"
    struct.pack_into("<IIII", data, section + 8, 0x300, 0x1000, 0x300, 0x200)
    export = 0x200
    struct.pack_into(
        "<IIHHIIIIIII",
        data,
        export,
        0,
        0,
        0,
        0,
        0x1060,
        1,
        2,
        2,
        0x1040,
        0x1048,
        0x1050,
    )
    struct.pack_into("<II", data, 0x240, 0x1100, 0x1110)
    struct.pack_into("<II", data, 0x248, 0x1080, 0x10A0)
    struct.pack_into("<HH", data, 0x250, 0, 1)
    data[0x260 : 0x260 + len(b"Fixture.dll\0")] = b"Fixture.dll\0"
    data[0x280 : 0x280 + len(b"XenoPlugin_Query\0")] = b"XenoPlugin_Query\0"
    data[0x2A0 : 0x2A0 + len(b"XenoPlugin_Initialize\0")] = b"XenoPlugin_Initialize\0"
    return bytes(data)


def _mod(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "ModuleInfo.txt").write_text(
        "Name=NativeFixture\nSection=OtherMods\nPriority=4\nLanguages=Rus\n",
        encoding="cp1251",
    )


class NativeLoaderTests(unittest.TestCase):
    def test_inspect_pe32_exports_without_loading_dll(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            dll = Path(name) / "Fixture.XenoPlugin.dll"
            dll.write_bytes(_plugin_dll())
            info = inspect_native_dll(dll)
            self.assertEqual(info.architecture, "x86")
            self.assertEqual(info.pe_kind, "PE32")
            self.assertTrue(info.is_dll)
            self.assertEqual(
                set(info.exports),
                {"XenoPlugin_Query", "XenoPlugin_Initialize"},
            )

    def test_validate_automatic_plugin_and_integrate_with_audit(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "NativeFixture"
            _mod(root)
            native = root / "Native"
            native.mkdir()
            (native / "Fixture.XenoPlugin.dll").write_bytes(_plugin_dll())
            (native / "Fixture.XenoPlugin.ini").write_text(
                "[plugin]\nEnabled=yes\n[Fixture]\nValue=1\n",
                encoding="utf-8",
            )
            report = validate_native_mod(root)
            self.assertTrue(report.valid)
            self.assertTrue(report.detected)
            self.assertFalse(report.complete)
            self.assertEqual(report.plugins[0].source, "automatic")
            self.assertTrue(validate_schema_document(report.as_dict())["valid"])
            audit = audit_mod(root, profile="dev")
            check = next(item for item in audit.checks if item.name == "native-loader")
            self.assertEqual(check.status, "passed")
            self.assertFalse(check.complete)
            self.assertNotIn("missing-content", {issue.code for issue in audit.issues})

    def test_manifest_rejects_escape_and_x64_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "NativeFixture"
            _mod(root)
            (root / "XenoNativePlugin.ini").write_text(
                "[Plugin]\nDll=..\\outside.dll\nLegacy=0\n",
                encoding="utf-8",
            )
            outside = root.parent / "outside.dll"
            outside.write_bytes(_plugin_dll(machine=0x8664, magic=0x20B))
            codes = {issue.code for issue in validate_native_mod(root).issues}
            self.assertIn("native-loader-manifest-path-unsafe", codes)

            (root / "XenoNativePlugin.ini").write_text(
                "[Plugin]\nDll=Native\\Wide.Runtime.dll\nLegacy=0\n",
                encoding="utf-8",
            )
            native = root / "Native"
            native.mkdir()
            (native / "Wide.Runtime.dll").write_bytes(_plugin_dll(machine=0x8664, magic=0x20B))
            codes = {issue.code for issue in validate_native_mod(root).issues}
            self.assertIn("native-loader-plugin-not-x86", codes)

    def test_native_init_creates_safe_sdk_scaffold_and_project(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "ExampleNative"
            result = initialize_native_mod(
                root,
                plugin_id="ExampleRuntime",
                capability="galaxy-generator",
            )
            self.assertEqual(result["loader_minimum_version"], "0.6.5")
            self.assertTrue((root / "SOURCE" / "Native" / "build.ps1").is_file())
            build_text = (root / "SOURCE" / "Native" / "build.ps1").read_text(
                encoding="utf-8"
            )
            self.assertIn('("/Fo" + $temp', build_text)
            self.assertTrue((root / "Native" / "ExampleRuntime.XenoPlugin.ini").is_file())
            project = load_project(root.parent)
            self.assertEqual(project.external_builds[0]["kind"], "xeno-native-plugin")
            self.assertEqual(project.external_builds[0]["mode"], "prebuilt")

    def test_project_init_links_native_build_script_to_all_plugin_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            mod = root / "ExistingNative"
            _mod(mod)
            source = mod / "SOURCE" / "Native"
            source.mkdir(parents=True)
            (source / "build.ps1").write_text("param()\n", encoding="utf-8")
            native = mod / "Native"
            native.mkdir()
            for plugin in ("One", "Two"):
                (native / f"{plugin}.XenoPlugin.dll").write_bytes(_plugin_dll())
            initialized = initialize_project(mod)
            self.assertEqual(len(initialized["external_builds"]), 1)
            external = initialized["external_builds"][0]
            self.assertEqual(external["kind"], "xeno-native-plugin")
            self.assertEqual(
                {Path(item).name for item in external["outputs"]},
                {"One.XenoPlugin.dll", "Two.XenoPlugin.dll"},
            )


if __name__ == "__main__":
    unittest.main()
