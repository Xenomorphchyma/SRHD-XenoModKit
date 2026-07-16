from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from srhd_modkit.blockpar import BlockParError, load_blockpar, parse_blockpar
from srhd_modkit.cli import cmd_dat_validate
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
            self.skipTest("BlockParEditor 1.9 или тестовый DAT не найден")
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
            self.skipTest("BlockParEditor 1.9 не найден")
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
            self.assertEqual(load_blockpar(decoded).encoding, "cp1251")

    def test_unrepresentable_game_text_is_rejected_before_dat_conversion(self) -> None:
        if not self.chain.tools["blockpar"].path.is_file():
            self.skipTest("BlockPar codec не найден")
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "Lang.txt"
            source.write_text("Data ^{\n    Text=Цена 10 → 20\n}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Windows-1251"):
                self.chain.convert_dat(source, root / "Lang.dat")

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
