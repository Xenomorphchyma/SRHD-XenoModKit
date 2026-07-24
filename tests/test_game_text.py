from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from srhd_modkit.cli import cmd_script_build, cmd_script_validate
from srhd_modkit.blockpar import parse_blockpar
from srhd_modkit.game_text import (
    lint_blockpar_display_text,
    lint_game_text,
    lint_key_value_display_text,
    lint_rson_display_text,
)
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

    def test_limited_font_symbols_and_compact_numbers_are_warnings(self) -> None:
        decoded = decode_bytes(
            (
                "Band=80–100%: 4–6 транспортов\n"
                "Stage=3/48\n"
                "Safe=100%; 10-40%; DATA/Script\n"
                "Mark=©\n"
            ).encode("cp1251")
        )
        issues = lint_game_text(decoded, "Lang_Rus.txt", require_cp1251=True)
        range_issues = [
            issue
            for issue in issues
            if issue.code == "game-text-typographic-number-range"
        ]
        slash_issues = [
            issue
            for issue in issues
            if issue.code == "game-text-numeric-slash-notation"
        ]
        self.assertEqual(len(range_issues), 2)
        self.assertEqual(len(slash_issues), 1)
        self.assertEqual(
            sum(
                issue.code == "game-text-limited-font-glyph"
                for issue in issues
            ),
            1,
        )
        self.assertTrue(all(issue.severity == "warning" for issue in issues))
        self.assertIn("от 80 до 100%", range_issues[0].message)
        self.assertIn("3 из 48", slash_issues[0].message)
        self.assertFalse(any(issue.location == "line 3" for issue in issues))
        self.assertFalse(
            any("DATA/Script" in (issue.evidence or "") for issue in issues)
        )

    def test_rson_display_lint_ignores_comments_and_finds_dynamic_separator(
        self,
    ) -> None:
        data = deepcopy(SAFE_RSON)
        group = data["Visual.Objects"][0]
        group["Dialogs"] = [
            {
                "Type": "TDialogMsg",
                "Name": "Progress",
                "Parent": -1,
                "#": 10,
                "Msg": "Готовность 80–100%",
            }
        ]
        group["Operations"][0]["Code"].extend(
            [
                "// Документированный диапазон 20–30 не показывается игроку.",
                "status = 'этап ' + current + '/' + total;",
            ]
        )
        issues = lint_rson_display_text(data, Path("display.rson"))
        codes = [issue.code for issue in issues]
        self.assertEqual(
            codes.count("game-text-typographic-number-range"),
            1,
        )
        self.assertEqual(
            codes.count("game-text-dynamic-slash-notation"),
            1,
        )
        self.assertFalse(any("20–30" in (issue.evidence or "") for issue in issues))

    def test_blockpar_display_lint_ignores_comments(self) -> None:
        document = parse_blockpar(
            "// Справочный диапазон 20–30\n"
            "Data ^{\n"
            "    Text=Рабочий диапазон 80–100%\n"
            "}\n"
        )
        issues = lint_blockpar_display_text(document, "Lang_Rus.txt")
        self.assertEqual(len(issues), 1)
        self.assertEqual(
            issues[0].code,
            "game-text-typographic-number-range",
        )
        self.assertNotIn("20–30", issues[0].evidence or "")
        flat_issues = lint_key_value_display_text(
            "// Справочный диапазон 20–30\n"
            "Text=Рабочий диапазон 80–100%\n",
            "ModuleInfo.txt",
        )
        self.assertEqual(len(flat_issues), 1)
        self.assertNotIn("20–30", flat_issues[0].evidence or "")

    def test_script_validate_reports_display_compatibility_warning(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            source = Path(name) / "display.rson"
            data = deepcopy(SAFE_RSON)
            data["Visual.Objects"][0]["Operations"][0]["Code"].append(
                "status = 'Этап 3/48';"
            )
            source.write_text(json.dumps(data), encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                result = cmd_script_validate(
                    SimpleNamespace(source=str(source), json=True)
                )
            payload = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertTrue(payload["valid"])
            self.assertIn(
                "game-text-numeric-slash-notation",
                {issue["code"] for issue in payload["issues"]},
            )

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
