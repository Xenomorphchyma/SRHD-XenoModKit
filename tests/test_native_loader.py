from __future__ import annotations

import struct
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from srhd_modkit.audit import audit_mod
from srhd_modkit.blockpar import parse_blockpar
from srhd_modkit.native_loader import (
    initialize_native_mod,
    inspect_native_dll,
    validate_native_mod,
)
from srhd_modkit.runtime_lint import lint_imported_functions
from srhd_modkit.scripts import RSON_FILE_ID, RSON_FILE_VERSION, RsonProject
from srhd_modkit.project import load_project
from srhd_modkit.project_ops import initialize_project
from srhd_modkit.schemas import validate_schema_document


def _plugin_dll(
    *,
    machine: int = 0x014C,
    magic: int = 0x10B,
    extra_export: str | None = None,
) -> bytes:
    pe_offset = 0x80
    optional_size = 0xE0 if magic == 0x10B else 0xF0
    optional = pe_offset + 24
    section = optional + optional_size
    data = bytearray(0x800)
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
    exports = ["XenoPlugin_Query", "XenoPlugin_Initialize"]
    if extra_export:
        exports.append(extra_export)
    struct.pack_into(
        "<IIHHIIIIIII",
        data,
        export,
        0,
        0,
        0,
        0,
        0x1100,
        1,
        len(exports),
        len(exports),
        0x1040,
        0x1050,
        0x1060,
    )
    struct.pack_into(
        "<" + "I" * len(exports),
        data,
        0x240,
        *(0x1200 + index * 0x10 for index in range(len(exports))),
    )
    cursor = 0x280
    name_rvas: list[int] = []
    for export_name in exports:
        encoded = export_name.encode("ascii") + b"\0"
        name_rvas.append(0x1000 + (cursor - 0x200))
        data[cursor : cursor + len(encoded)] = encoded
        cursor += len(encoded)
    struct.pack_into("<" + "I" * len(exports), data, 0x250, *name_rvas)
    struct.pack_into("<" + "H" * len(exports), data, 0x260, *range(len(exports)))
    data[0x300 : 0x300 + len(b"Fixture.dll\0")] = b"Fixture.dll\0"
    return bytes(data)


def _mod(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "ModuleInfo.txt").write_text(
        "Name=NativeFixture\nSection=OtherMods\nPriority=4\nLanguages=Rus\n",
        encoding="cp1251",
    )


class NativeLoaderTests(unittest.TestCase):
    def test_imported_function_closes_rson_main_signature_and_pe_export(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "NativeFixture"
            _mod(root)
            native = root / "Native"
            native.mkdir()
            dll = native / "Fixture.XenoPlugin.dll"
            dll.write_bytes(_plugin_dll(extra_export="Fixture_GetValue"))
            project = RsonProject(
                {
                    "FileID": RSON_FILE_ID,
                    "FileVersion": RSON_FILE_VERSION,
                    "ScriptName": "Mod_NativeFixture",
                    "Visual.Objects": [
                        {
                            "Operations": [
                                {
                                    "Type": "Top",
                                    "Name": "Turn",
                                    "#": 1,
                                    "Code.Type": "Turn",
                                    "Code": [
                                        "unknown getter = ImportedFunction('FixtureLibrary', 'Fixture_GetValue');",
                                        "int value = getter(7);",
                                    ],
                                }
                            ]
                        }
                    ],
                    "Visual.Links": [],
                },
                root / "SOURCE" / "Mod_NativeFixture.rson",
            )
            main = parse_blockpar(
                "Data ^{\n"
                " ScriptLibs ^{\n"
                "  FixtureLibrary ^{\n"
                "   Path=Mods\\OtherMods\\NativeFixture\\Native\\Fixture.XenoPlugin.dll\n"
                "   Fixture_GetValue=int,Fixture_GetValue,int\n"
                "  }\n"
                "  Mod_NativeFixture=FixtureLibrary\n"
                " }\n"
                "}\n"
            )
            report = lint_imported_functions(root, [project], [(root / "CFG" / "Main.dat", main)])
            self.assertTrue(report.complete)
            self.assertEqual(report.issues, ())
            self.assertEqual(report.checked_dlls, (str(dll.resolve()),))

            missing = parse_blockpar(
                "Data ^{\n"
                " ScriptLibs ^{\n"
                "  FixtureLibrary ^{\n"
                "   Path=Mods\\OtherMods\\NativeFixture\\Native\\Fixture.XenoPlugin.dll\n"
                "  }\n"
                "  Mod_NativeFixture=FixtureLibrary\n"
                " }\n"
                "}\n"
            )
            codes = {
                issue.code
                for issue in lint_imported_functions(
                    root, [project], [(root / "CFG" / "Main.dat", missing)]
                ).issues
            }
            self.assertIn("runtime-imported-function-registration-missing", codes)

            unbound = parse_blockpar(
                "Data ^{\n"
                " ScriptLibs ^{\n"
                "  FixtureLibrary ^{\n"
                "   Path=Mods\\OtherMods\\NativeFixture\\Native\\Fixture.XenoPlugin.dll\n"
                "   Fixture_GetValue=int,Fixture_GetValue,int\n"
                "  }\n"
                " }\n"
                "}\n"
            )
            codes = {
                issue.code
                for issue in lint_imported_functions(
                    root, [project], [(root / "CFG" / "Main.dat", unbound)]
                ).issues
            }
            self.assertIn("runtime-imported-function-script-binding-missing", codes)

            wrong_arity_data = deepcopy(project.data)
            wrong_arity_data["Visual.Objects"][0]["Operations"][0]["Code"][1] = (
                "int value = getter();"
            )
            wrong_arity = RsonProject(wrong_arity_data, project.path)
            codes = {
                issue.code
                for issue in lint_imported_functions(
                    root, [wrong_arity], [(root / "CFG" / "Main.dat", main)]
                ).issues
            }
            self.assertIn("runtime-imported-function-arity-mismatch", codes)

            wrong_export = parse_blockpar(
                "Data ^{\n"
                " ScriptLibs ^{\n"
                "  FixtureLibrary ^{\n"
                "   Path=Mods\\OtherMods\\NativeFixture\\Native\\Fixture.XenoPlugin.dll\n"
                "   Fixture_GetValue=int,Fixture_Missing,int\n"
                "  }\n"
                "  Mod_NativeFixture=FixtureLibrary\n"
                " }\n"
                "}\n"
            )
            codes = {
                issue.code
                for issue in lint_imported_functions(
                    root, [project], [(root / "CFG" / "Main.dat", wrong_export)]
                ).issues
            }
            self.assertIn("runtime-imported-function-pe-export-missing", codes)

            unavailable = lint_imported_functions(root, [project], None)
            self.assertFalse(unavailable.complete)
            self.assertIn(
                "runtime-imported-function-main-unavailable",
                {issue.code for issue in unavailable.issues},
            )

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
