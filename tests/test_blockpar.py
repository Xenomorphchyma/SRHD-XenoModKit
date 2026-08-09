from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from srhd_modkit.blockpar import BlockParError, load_blockpar, parse_blockpar
from srhd_modkit.audit import AuditProfile, audit_mod
from srhd_modkit.cli import _game_text_lint_target, cmd_dat_validate
from srhd_modkit.toolchain import Toolchain, is_empty_rscript_lang_dat


SAMPLE = """Data ^{\r
    SE ^{\r
        Ship ~{\r
            Cost=10\r
            Tag=first\r
            Tag=second\r
        }\r
        Ship ^{\r
            Cost=20\r
        }\r
    }\r
}\r
"""


class BlockParParserTests(unittest.TestCase):
    def test_parse_render_preserves_unchanged_text(self) -> None:
        document = parse_blockpar(SAMPLE)
        self.assertEqual(document.to_text(), SAMPLE)
        self.assertEqual(document.find_node("Data/SE/Ship").parameters_named("Cost")[0].value, "10")
        self.assertEqual(document.find_node("Data/SE/Ship[2]").parameters_named("Cost")[0].value, "20")
        self.assertEqual(document.find_node("Data/SE/Ship").operator, "~")

    def test_set_and_create_parameter(self) -> None:
        document = parse_blockpar(SAMPLE)
        node = document.find_node("Data/SE/Ship")
        self.assertEqual(node.set_parameter("Cost", "15"), 1)
        self.assertEqual(node.set_parameter("NewValue", "yes", create=True), 1)
        reparsed = parse_blockpar(document.to_text())
        edited = reparsed.find_node("Data/SE/Ship")
        self.assertEqual(edited.parameters_named("Cost")[0].value, "15")
        self.assertEqual(edited.parameters_named("NewValue")[0].value, "yes")

    def test_invalid_braces_are_rejected(self) -> None:
        with self.assertRaises(BlockParError):
            parse_blockpar("Data ^{\n  A=1\n")

    def test_apply_operations(self) -> None:
        document = parse_blockpar(SAMPLE)
        changes = document.apply_operations(
            [
                {"op": "set", "node": "Data/SE/Ship", "key": "Cost", "value": "15"},
                {"op": "delete-parameter", "node": "Data/SE/Ship", "key": "Tag", "all": True},
                {"op": "add-node", "parent": "Data/SE", "name": "Station", "operator": "~"},
                {"op": "set", "node": "Data/SE/Station", "key": "Enabled", "value": "1", "create": True},
                {"op": "delete-node", "node": "Data/SE/Ship[2]"},
            ]
        )
        self.assertEqual(len(changes), 5)
        reparsed = parse_blockpar(document.to_text())
        self.assertEqual(reparsed.find_node("Data/SE/Ship").parameters_named("Cost")[0].value, "15")
        self.assertFalse(reparsed.find_node("Data/SE/Ship").parameters_named("Tag"))
        self.assertEqual(reparsed.find_node("Data/SE/Station").operator, "~")
        self.assertEqual(reparsed.find_node("Data/SE/Station").parameters_named("Enabled")[0].value, "1")
        with self.assertRaises(KeyError):
            reparsed.find_node("Data/SE/Ship[2]")

    def test_canonical_semantic_accepts_editor_sorting_but_not_value_changes(self) -> None:
        left = parse_blockpar("Data ^{\n    Z=1\n    A=2\n}\n")
        reordered = parse_blockpar("Data ^{\n    A=2\n    Z=1\n}\n")
        changed = parse_blockpar("Data ^{\n    A=3\n    Z=1\n}\n")
        self.assertNotEqual(left.semantic(), reordered.semantic())
        self.assertEqual(left.canonical_semantic(), reordered.canonical_semantic())
        self.assertNotEqual(left.canonical_semantic(), changed.canonical_semantic())

    def test_ensure_node_creates_only_missing_path(self) -> None:
        document = parse_blockpar("Data ^{\n}\n")
        node = document.ensure_node("Data/Script/Custom")
        self.assertEqual(node.name, "Custom")
        self.assertIs(document.ensure_node("Data/Script/Custom"), node)
        self.assertEqual(len(document.roots), 1)


class BlockParCliIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.chain = Toolchain()
        cls.source = Path(r"D:\SRHD_Modding\Projects\ModWorkspaces\Kotyanka\Cat_PirateClan\CFG\Main.dat")

    def test_real_dat_text_dat_roundtrip_is_semantically_exact(self) -> None:
        if not self.chain.tools["blockpar"].path.is_file() or not self.source.is_file():
            self.skipTest("BlockParEditor или тестовый DAT не найден")
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            text = root / "Main.txt"
            rebuilt = root / "Main.dat"
            text2 = root / "Main2.txt"
            self.chain.convert_dat(self.source, text)
            self.chain.convert_dat(text, rebuilt)
            self.chain.convert_dat(rebuilt, text2)
            self.assertEqual(load_blockpar(text).semantic(), load_blockpar(text2).semantic())

    def test_ascii_text_is_encoded_without_blockpar_gui(self) -> None:
        if not self.chain.tools["blockpar"].path.is_file():
            self.skipTest("BlockParEditor не найден")
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "Lang.txt"
            source.write_text(
                "Script ^{\n    Test ^{\n        Description=Equipment inflation\n    }\n}\n",
                encoding="utf-8",
            )
            dat = root / "Lang.dat"
            decoded = root / "Lang.decoded.txt"
            self.chain.convert_dat(source, dat)
            self.chain.convert_dat(dat, decoded)
            self.assertEqual(
                load_blockpar(source).canonical_semantic(),
                load_blockpar(decoded).canonical_semantic(),
            )

    def test_unicode_text_is_encoded_without_blockpar_gui(self) -> None:
        if not self.chain.tools["blockpar"].path.is_file():
            self.skipTest("BlockPar codec не найден")
        source = Path(__file__).parent / "fixtures" / "unicode_blockpar.txt"
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            dat = root / "Lang.dat"
            decoded = root / "Lang.txt"
            self.chain.convert_dat(source, dat)
            self.chain.convert_dat(dat, decoded)
            self.assertEqual(
                load_blockpar(source).canonical_semantic(),
                load_blockpar(decoded).canonical_semantic(),
            )
            self.assertEqual(
                load_blockpar(decoded).find_node("Data/Rus").parameters_named("Description")[0].value,
                "Проверка русского текста",
            )
            self.assertIn(load_blockpar(decoded).encoding, {"cp1251", "utf-16-le"})

    def test_utf8_cp1251_and_utf16_sources_have_the_same_semantics(self) -> None:
        if not self.chain.tools["blockpar"].path.is_file():
            self.skipTest("BlockPar codec не найден")
        text = "Data ^{\n    Rus ^{\n        Description=Проверка кодировки\n    }\n}\n"
        encodings = {
            "utf8": text.encode("utf-8"),
            "cp1251": text.encode("cp1251"),
            "utf16": text.encode("utf-16"),
        }
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            expected = parse_blockpar(text).canonical_semantic()
            for label, payload in encodings.items():
                with self.subTest(encoding=label):
                    source = root / f"{label}.txt"
                    dat = root / f"{label}.dat"
                    decoded = root / f"{label}.decoded.txt"
                    source.write_bytes(payload)
                    result = self.chain.convert_dat(source, dat)
                    self.assertTrue(result["verified"])
                    self.chain.convert_dat(dat, decoded)
                    self.assertEqual(load_blockpar(decoded).canonical_semantic(), expected)

    def test_repeated_dat_builds_are_compared_by_tree_not_binary_hash(self) -> None:
        if not self.chain.tools["blockpar"].path.is_file():
            self.skipTest("BlockPar codec не найден")
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "Main.txt"
            source.write_text("Data ^{\n    Value=42\n}\n", encoding="utf-8")
            outputs = [root / "first.dat", root / "second.dat"]
            results = [self.chain.convert_dat(source, path) for path in outputs]
            decoded = []
            for index, path in enumerate(outputs):
                text_path = root / f"decoded-{index}.txt"
                self.chain.convert_dat(path, text_path)
                decoded.append(load_blockpar(text_path).canonical_semantic())
            self.assertTrue(all(result["verified"] for result in results))
            self.assertEqual(decoded[0], decoded[1])

    def test_blockpar_21_utf16_export_is_not_the_dat_payload_encoding(self) -> None:
        if not self.chain.tools["blockpar"].path.is_file():
            self.skipTest("BlockPar codec не найден")
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "EncodingMod"
            source = root / "SOURCE" / "CFG" / "Rus" / "Lang.txt"
            target = root / "CFG" / "Rus" / "Lang.dat"
            source.parent.mkdir(parents=True)
            target.parent.mkdir(parents=True)
            source.write_text(
                "Data ^{\n    Rus ^{\n        Description=Корректный текст\n    }\n}\n",
                encoding="utf-8",
            )
            (root / "ModuleInfo.txt").write_text(
                "Name=EncodingMod\nLanguages=Rus\nSmallDescription=Encoding probe\n",
                encoding="utf-16",
            )
            self.chain.convert_dat(source, target)
            result = _game_text_lint_target(root)
            codes = {issue["code"] for issue in result["issues"]}
            self.assertNotIn("game-text-wrong-encoding", codes)
            self.assertNotIn("game-text-not-cp1251", codes)
            report = audit_mod(root, profile=AuditProfile.DEV)
            audit_codes = {issue.code for issue in report.issues}
            self.assertNotIn("game-text-wrong-encoding", audit_codes)

    def test_real_blockpar_19_accepts_utf8_cp1251_and_utf16_sources(self) -> None:
        legacy_root = self.chain.tools_root / "BlockParEditor19"
        legacy_executable = legacy_root / "BlockParEditor.exe"
        legacy_library = legacy_root / "BlockParEditor.dll"
        if not legacy_executable.is_file() or not legacy_library.is_file():
            self.skipTest("Сохранённый BlockParEditor 1.9 не найден")
        text = "Data ^{\n    Rus ^{\n        Description=Проверка версии 1.9\n    }\n}\n"
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            tool_root = root / "tools"
            tool_dir = tool_root / "BlockParEditor"
            tool_dir.mkdir(parents=True)
            shutil.copy2(legacy_executable, tool_dir / legacy_executable.name)
            shutil.copy2(legacy_library, tool_dir / legacy_library.name)
            chain = Toolchain(tool_root)
            self.assertEqual(chain.tools["blockpar"].version, "1.9")
            self.assertEqual(chain.tools["blockpar"].compatibility, "legacy-cp1251")
            expected = parse_blockpar(text).canonical_semantic()
            for label, payload in {
                "utf8": text.encode("utf-8"),
                "cp1251": text.encode("cp1251"),
                "utf16": text.encode("utf-16"),
            }.items():
                with self.subTest(encoding=label):
                    source = root / f"legacy-{label}.txt"
                    dat = root / f"legacy-{label}.dat"
                    decoded = root / f"legacy-{label}.decoded.txt"
                    source.write_bytes(payload)
                    built = chain.convert_dat(source, dat)
                    chain.convert_dat(dat, decoded)
                    self.assertTrue(built["verified"])
                    self.assertEqual(load_blockpar(decoded).canonical_semantic(), expected)

    def test_unrepresentable_game_text_is_rejected_before_dat_conversion(self) -> None:
        if not self.chain.tools["blockpar"].path.is_file():
            self.skipTest("BlockPar codec не найден")
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "Lang.txt"
            source.write_text("Data ^{\n    Text=Цена 10 → 20\n}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Windows-1251"):
                self.chain.convert_dat(source, root / "Lang.dat")

    def test_inline_double_slash_is_rejected_before_blockpar_truncates_url(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "Lang.txt"
            source.write_text(
                "Data ^{\n    Help=https://example.com/modding\n}\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError,
                "blockpar-inline-comment-truncation-risk",
            ):
                self.chain.convert_dat(source, root / "Lang.dat")

    def test_dat_validate_warns_about_limited_display_notation(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            source = Path(name) / "Lang.txt"
            source.write_text(
                "Data ^{\n"
                "    Range=80–100%\n"
                "    Progress=этап 3/48\n"
                "}\n",
                encoding="utf-8",
            )
            output = StringIO()
            args = SimpleNamespace(source=str(source), tools_root=None, json=True)
            with redirect_stdout(output):
                self.assertEqual(cmd_dat_validate(args), 0)
            payload = json.loads(output.getvalue())
            self.assertTrue(payload["valid"])
            self.assertEqual(
                {
                    issue["code"]
                    for issue in payload["issues"]
                },
                {
                    "game-text-typographic-number-range",
                    "game-text-numeric-slash-notation",
                },
            )

    def test_empty_rscript_lang_dat_is_validated_headlessly(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "DATA" / "Script" / "Lang.dat"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"\xff\xfe")
            decoded = root / "Lang.txt"

            self.assertTrue(is_empty_rscript_lang_dat(source))
            result = self.chain.convert_dat(source, decoded)
            self.assertEqual(decoded.read_bytes(), b"")
            self.assertEqual(result["format"], "rscript-empty-lang-dat")
            self.assertTrue(result["verified"])

            output = StringIO()
            args = SimpleNamespace(source=str(source), tools_root=None, json=False)
            with redirect_stdout(output):
                self.assertEqual(cmd_dat_validate(args), 0)
            self.assertIn("Пустой RScript DATA/Script/Lang.dat корректен", output.getvalue())

            cfg_lang = root / "CFG" / "Lang.dat"
            cfg_lang.parent.mkdir()
            cfg_lang.write_bytes(b"\xff\xfe")
            self.assertFalse(is_empty_rscript_lang_dat(cfg_lang))


if __name__ == "__main__":
    unittest.main()
