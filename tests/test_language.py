from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from srhd_modkit.language import diff_languages, language_coverage


class LanguageWorkflowTests(unittest.TestCase):
    def test_diff_compares_semantic_keys(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            left = root / "left.txt"
            right = root / "right.txt"
            left.write_text("Data ^{\n A=Один\n B=Два\n}\n", encoding="utf-8")
            right.write_text("Data ^{\n A=Раз\n C=Три\n}\n", encoding="utf-8")
            result = diff_languages(left, right)
            self.assertEqual(result["summary"], {"added": 1, "removed": 1, "changed": 1, "unchanged": 0})

    def test_coverage_finds_missing_keys_and_rscript_code_stubs(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            mod = Path(name) / "Mod"
            (mod / "CFG" / "Rus").mkdir(parents=True)
            (mod / "CFG" / "Eng").mkdir(parents=True)
            (mod / "ModuleInfo.txt").write_text(
                "Name=LangFixture\nSection=Test\nLanguages=Rus,Eng\n", encoding="cp1251"
            )
            (mod / "CFG" / "Rus" / "Lang.txt").write_text(
                "Data ^{\n A=Текст\n B=Ответ\n}\n", encoding="utf-8"
            )
            (mod / "CFG" / "Eng" / "Lang.txt").write_text(
                "Data ^{\n A=DAnswer('stub')\n}\n", encoding="utf-8"
            )
            result = language_coverage(mod, base="Rus")
            codes = {item["code"] for item in result["issues"]}
            self.assertFalse(result["valid"])
            self.assertIn("lang-key-missing", codes)
            self.assertIn("lang-value-code-stub", codes)


if __name__ == "__main__":
    unittest.main()
