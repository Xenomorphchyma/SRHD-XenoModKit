from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from srhd_modkit.audit import audit_mod
from srhd_modkit.blockpar import parse_blockpar
from srhd_modkit.script_artifacts import lint_script_cache
from srhd_modkit.scr_format import (
    ScrFormatError,
    check_scr_structure,
    parse_scr_structure,
    scr_structure_issues,
)
from srhd_modkit.scripts import inspect_scr


def _ws(text: str) -> bytes:
    return text.encode("utf-16-le") + b"\x00\x00"


def _i32(*values: int) -> bytes:
    return struct.pack(f"<{len(values)}i", *values)


def _scr(version: int = 8, *, place_kind: int = 0, constraint_target: int = 0) -> bytes:
    """Write every section in the order ``TScript.LoadFromBuffer`` reads it."""

    legacy = version < 7
    body = bytearray()
    # Global variables: int, string, float, and an array holding an int.
    body += _i32(4)
    body += _ws("Counter") + bytes([1]) + _i32(5)
    body += _ws("Title") + bytes([4]) + _ws("Привет")
    body += _ws("Ratio") + bytes([3]) + struct.pack("<d", 0.5)
    body += _ws("Table") + bytes([9]) + _i32(1) + _ws("") + bytes([1]) + _i32(7)
    body += _i32(1)  # constellations
    body += _i32(2)  # stars
    for index, name in enumerate(("StarA", "StarB")):
        body += _ws(name) + _i32(0)
        if legacy:
            body += b"\x00"
        body += b"\x01\x00"
        if index == 1:
            body += _i32(1, constraint_target)
            body += _i32(0, 10) if not legacy else _i32(0, 0, 10, 0, 0, 0)
            body += b"\x00"
        else:
            body += _i32(0)
        body += _i32(1) + _ws(f"Planet{index}") + _i32(0, 0, 0, 0, 0, 100) + _ws("")
        body += _i32(1) + _i32(1, 0, 0) + b"\x00" + _i32(0, 0, 0, 0, 0)
        if legacy:
            body += _i32(0, 0)
        body += _i32(0, 0, 0, 0, 0, 0)
        if legacy:
            body += _i32(0, 0)
        body += struct.pack("<ff", 0.0, 1.0) + _ws("")
    body += _i32(1) + _ws("Place") + _ws("StarA") + _i32(place_kind)
    if place_kind == 0:
        body += struct.pack("<ffi", 0.0, 1.0, 5)
    elif place_kind == 6:
        body += _ws("A") + _ws("B") + _i32(5)
    body += _i32(1) + _ws("Item") + _ws("Place") + _i32(0, 0, 1, 1, 0, 0) + _ws("")
    body += _i32(1) + _ws("Group") + _ws("Planet0") + _i32(0, 0, 0, 1, 2, 0, 0, 0, 0, 0)
    if legacy:
        body += _i32(0)
    body += b"\x00"
    if legacy:
        body += _i32(0, 0, 0, 0)
    body += _i32(0, 0, 0, 0, 0, 0, 0) + _ws("") + struct.pack("<ff", 0.0, 1.0) + _ws("")
    body += _i32(1) + _i32(0, 0, 5, 5) + struct.pack("<ff", 0.0, 1000.0)
    body += _ws("int a = 1;") + _ws("a = a + 1;")
    if version > 5:
        body += _ws("")
    body += _i32(2)
    body += _ws("Idle") + _i32(0) + _i32(0) + _ws("") + b"\x00" + _ws("") + _ws("") + _ws("") + _ws("")
    body += _ws("Move") + _i32(1) + _ws("StarB") + _i32(1) + _ws("Group") + _ws("") + b"\x01"
    body += _ws("") + _ws("[t_Any|]x=1;") + _ws("x=2;") + _ws("x=3;")
    body += _i32(1) + _ws("Dlg") + _ws("DText('hi');")
    body += _i32(1) + _ws("Msg") + _ws("DText('msg');")
    body += _i32(1) + _ws("Ans") + _ws("Ok") + _ws("DChange(Msg);")
    header = struct.pack("<IH", version, 6)
    return header + bytes(body)


class ScrStructureTests(unittest.TestCase):
    def test_complete_file_reads_to_the_last_byte(self) -> None:
        for version in (6, 7, 8):
            with self.subTest(version=version):
                data = _scr(version)
                structure = parse_scr_structure(data)
                self.assertEqual(structure.end_offset, len(data))
                self.assertEqual(structure.counts["variables"], 4)
                self.assertEqual(structure.counts["stars"], 2)
                self.assertEqual(structure.counts["states"], 2)
                self.assertEqual(structure.counts["dialog_answers"], 1)
                self.assertEqual(scr_structure_issues(check_scr_structure(data)), [])

    def test_every_truncation_is_rejected(self) -> None:
        data = _scr()
        for size in range(len(data)):
            with self.subTest(size=size):
                self.assertFalse(check_scr_structure(data[:size])["valid"])

    def test_variable_header_offset_is_followed(self) -> None:
        data = _scr()
        padded = struct.pack("<IH", 8, 10) + b"\xAA\xBB\xCC\xDD" + data[6:]
        self.assertEqual(parse_scr_structure(padded).end_offset, len(padded))

    def test_game_rejections_are_reported(self) -> None:
        cases = {
            "version": struct.pack("<IH", 9, 6) + _scr()[6:],
            "places": _scr(place_kind=7),
            "stars": _scr(constraint_target=5),
        }
        for section, data in cases.items():
            with self.subTest(section=section):
                with self.assertRaises(ScrFormatError) as raised:
                    parse_scr_structure(data)
                self.assertEqual(raised.exception.section, "header" if section == "version" else section)

    def test_coordinate_place_reads_two_targets(self) -> None:
        data = _scr(place_kind=6)
        self.assertEqual(parse_scr_structure(data).end_offset, len(data))

    def test_overwritten_code_text_is_damage(self) -> None:
        data = bytearray(_scr())
        code = bytes(data).index("a = a + 1;".encode("utf-16-le"))
        data[code:code + 4] = b"\xff\xff\xff\xff"
        result = check_scr_structure(bytes(data))
        self.assertFalse(result["valid"])
        self.assertIn("U+FFFF", result["error"])
        self.assertEqual(result["section"], "code")
        self.assertEqual(result["offset"], code)

    def test_lone_surrogate_is_damage_but_a_pair_is_text(self) -> None:
        clean = _scr()
        marker = "x=3;".encode("utf-16-le")
        paired = clean.replace(marker, "😀=3".encode("utf-16-le"))
        self.assertTrue(check_scr_structure(paired)["valid"])
        lone = clean.replace(marker, b"\x00\xd8" + "=3;".encode("utf-16-le"))
        self.assertFalse(check_scr_structure(lone)["valid"])

    def test_trailing_bytes_are_a_warning(self) -> None:
        result = check_scr_structure(_scr() + b"\x00\x00")
        self.assertTrue(result["valid"])
        self.assertEqual(result["trailing_bytes"], 2)
        issues = scr_structure_issues(result)
        self.assertEqual([(item[0], item[1]) for item in issues], [("warning", "scr-trailing-data")])

    def test_inspect_scr_carries_the_structure(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "Mod_Test.scr"
            path.write_bytes(_scr()[:-3])
            info = inspect_scr(path)
            self.assertFalse(info["structure"]["valid"])
            self.assertEqual(info["structure"]["section"], "dialog_answers")


class MissingScriptTests(unittest.TestCase):
    def _cache(self, *lines: str):
        return parse_blockpar("Script ^{\n" + "".join(f"  {line}\n" for line in lines) + "}\n")

    def test_registered_mapping_to_a_deleted_file_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "TestMod"
            cache = self._cache(
                "Mod_Test=Mods\\OtherMods\\TestMod\\DATA\\Script\\Mod_Test.scr",
                "Mod_Spare=Mods\\OtherMods\\TestMod\\DATA\\Script\\Mod_Spare.scr",
                "Mod_Base=Mods\\OtherMods\\BaseMod\\DATA\\Script\\Mod_Base.scr",
            )
            issues = lint_script_cache(
                root,
                [],
                {"mod_test": ["1,Script.Mod_Test"], "mod_base": ["1,Script.Mod_Base"]},
                [(root / "CFG" / "CacheData.txt", cache)],
            )
            found = {(issue.code, issue.severity, issue.location) for issue in issues}
            self.assertEqual(
                found,
                {
                    ("cache-script-file-missing", "error", "Script/Mod_Test"),
                    ("cache-script-file-missing", "warning", "Script/Mod_Spare"),
                },
            )

    def test_present_file_is_not_reported(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "TestMod"
            script = root / "DATA" / "Script" / "Mod_Test.scr"
            cache = self._cache("Mod_Test=Mods\\OtherMods\\TestMod\\DATA\\Script\\Mod_Test.scr")
            issues = lint_script_cache(
                root,
                [script],
                {"mod_test": ["1,Script.Mod_Test"]},
                [(root / "CFG" / "CacheData.txt", cache)],
            )
            self.assertNotIn("cache-script-file-missing", {issue.code for issue in issues})


class AuditIntegrationTests(unittest.TestCase):
    def _mod(self, root: Path, script_bytes: bytes | None) -> None:
        root.mkdir(parents=True)
        (root / "ModuleInfo.txt").write_text(
            "Name=TestMod\nSection=Test\nPriority=1\nLanguages=Rus\n",
            encoding="cp1251",
        )
        script_dir = root / "DATA" / "Script"
        script_dir.mkdir(parents=True)
        if script_bytes is not None:
            (script_dir / "Mod_Test.scr").write_bytes(script_bytes)
        cfg = root / "SOURCE" / "CFG"
        cfg.mkdir(parents=True)
        (cfg / "Main.txt").write_text(
            "Data ^{\n Script ^{\n  Mod_Test=1,Script.Mod_Test\n }\n}\n",
            encoding="cp1251",
        )
        (cfg / "CacheData.txt").write_text(
            "Script ^{\n  Mod_Test=Mods\\TestMod\\DATA\\Script\\Mod_Test.scr\n}\n",
            encoding="cp1251",
        )

    def _codes(self, script_bytes: bytes | None) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "Mods" / "TestMod"
            self._mod(root, script_bytes)
            report = audit_mod(root, profile="dev")
            return {item.code: item.severity for item in report.issues}

    def test_audit_accepts_a_whole_script(self) -> None:
        codes = self._codes(_scr())
        self.assertNotIn("scr-structure-invalid", codes)
        self.assertNotIn("cache-script-file-missing", codes)

    def test_audit_rejects_a_damaged_script(self) -> None:
        data = bytearray(_scr())
        middle = len(data) // 2
        data[middle:middle + 16] = b"\xff" * 16
        self.assertEqual(self._codes(bytes(data)).get("scr-structure-invalid"), "error")

    def test_audit_rejects_a_deleted_registered_script(self) -> None:
        self.assertEqual(self._codes(None).get("cache-script-file-missing"), "error")


if __name__ == "__main__":
    unittest.main()
