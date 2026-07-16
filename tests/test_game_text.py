from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from srhd_modkit.cli import cmd_script_build
from srhd_modkit.game_text import lint_game_text
from srhd_modkit.textio import decode_bytes
from tests.test_runtime_lint import SAFE_RSON


class GameTextTests(unittest.TestCase):
    def test_cp1251_russian_game_text_is_accepted(self) -> None:
        decoded = decode_bytes("Описание=Русский текст\n".encode("cp1251"))
        self.assertEqual(decoded.encoding, "cp1251")
        self.assertEqual(lint_game_text(decoded, "ModuleInfo.txt", require_cp1251=True), [])

    def test_utf8_russian_game_text_is_rejected(self) -> None:
        decoded = decode_bytes("Описание=Русский текст\n".encode("utf-8"))
        codes = {
            issue.code
            for issue in lint_game_text(
                decoded,
                "ModuleInfo.txt",
                allowed_encodings={"cp1251", "utf-16-le", "utf-16-be"},
            )
        }
        self.assertIn("game-text-wrong-encoding", codes)

    def test_utf16_module_info_is_accepted(self) -> None:
        decoded = decode_bytes(b"\xff\xfe" + "Описание=Русский текст\n".encode("utf-16-le"))
        issues = lint_game_text(
            decoded,
            "ModuleInfo.txt",
            allowed_encodings={"cp1251", "utf-16-le", "utf-16-be"},
        )
        self.assertEqual(issues, [])

    def test_mojibake_and_unrepresentable_symbol_are_reported(self) -> None:
        mojibake = decode_bytes("Описание=РџСЂРѕРІРµСЂРєР°\n".encode("cp1251"))
        self.assertIn("game-text-mojibake", {issue.code for issue in lint_game_text(mojibake)})

        source = decode_bytes("Цена: 10 → 20\n".encode("utf-8"))
        issues = lint_game_text(source, "Lang_Rus.txt", require_cp1251_representable=True)
        issue = next(item for item in issues if item.code == "game-text-not-cp1251")
        self.assertIn("U+2192", issue.evidence or "")

    def test_build_stops_before_compiler_on_utf8_module_info(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "EncodingMod"
            source = root / "SOURCE"
            source.mkdir(parents=True)
            data = deepcopy(SAFE_RSON)
            group = data["Visual.Objects"][0]
            group["Operations"] = [group["Operations"][0]]
            group["Operations"][0]["Code"] = ["GRun();"]
            group["States"] = []
            rson = source / "Mod_Encoding.rson"
            rson.write_text(json.dumps(data), encoding="utf-8")
            (root / "ModuleInfo.txt").write_text(
                "Name=EncodingMod\nLanguages=Rus\nSmallDescription=Русский текст\n",
                encoding="utf-8",
            )
            args = SimpleNamespace(
                source=str(rson),
                scr=str(root / "DATA" / "Script" / "Mod_Encoding.scr"),
                lang=str(root / "DATA" / "Script" / "Lang.dat"),
                overwrite=False,
                tools_root=None,
                json=False,
            )
            with self.assertRaisesRegex(ValueError, "game-text-wrong-encoding"):
                cmd_script_build(args)


if __name__ == "__main__":
    unittest.main()
