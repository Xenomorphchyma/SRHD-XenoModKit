from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from srhd_modkit.language import diff_languages, language_coverage, remap_languages
from srhd_modkit.toolchain import Toolchain


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

    def test_remap_moves_overlay_keys_onto_the_new_numbering(self) -> None:
        """The rebuilt numbering wins: the overlay follows its texts, an unknown key stays put."""

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            truth = root / "truth.txt"
            onto = root / "fragment.txt"
            overlay = root / "overlay.txt"
            truth.write_text(
                "Script ^{\n    ModX ^{\n        10=Alpha\n        20=Beta\n        30=Gamma\n    }\n}\n",
                encoding="utf-8",
            )
            onto.write_text("5=Beta\n3=Alpha\n7=Gamma\n", encoding="utf-8")
            overlay.write_text(
                "Script ^{\n    ModX ^{\n        10=Альфа\n        20=Бета\n        30=Гамма\n"
                "        99=Прочее\n    }\n}\nName=Значение\n",
                encoding="utf-8",
            )
            result = remap_languages(
                truth, onto, [overlay], out_dir=root / "out", script="ModX"
            )
            self.assertTrue(result["valid"])
            self.assertEqual(result["summary"]["mapped"], 3)
            self.assertEqual(result["summary"]["unmatched"], 0)
            text = (root / "out" / "overlay.txt").read_text(encoding="utf-8")
            self.assertIn("3=Альфа", text)
            self.assertIn("5=Бета", text)
            self.assertIn("7=Гамма", text)
            self.assertIn("99=Прочее", text)
            self.assertIn("Name=Значение", text)
            self.assertNotIn("10=Альфа", text)

    def test_remap_pairs_duplicate_texts_by_order_and_drops_collisions(self) -> None:
        """Equal texts keep the key order; a kept key outvoted for its number is dropped."""

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            truth = root / "truth.txt"
            onto = root / "fragment.txt"
            overlay = root / "overlay.txt"
            truth.write_text(
                "Script ^{\n    ModX ^{\n        10=Same\n        20=Same\n        30=Other\n    }\n}\n",
                encoding="utf-8",
            )
            onto.write_text("2=Same\n1=Same\n5=Other\n9=Unmatched\n", encoding="utf-8")
            overlay.write_text(
                "Script ^{\n    ModX ^{\n        10=A\n        20=B\n        30=D\n        5=C\n        9=E\n    }\n}\n",
                encoding="utf-8",
            )
            result = remap_languages(truth, onto, [overlay], out_dir=root / "out", script="ModX")
            self.assertEqual(result["summary"]["mapped"], 3)
            self.assertEqual(result["summary"]["dropped"], 2)
            text = (root / "out" / "overlay.txt").read_text(encoding="utf-8")
            self.assertIn("1=A", text)
            self.assertIn("2=B", text)
            self.assertIn("5=D", text)
            self.assertNotIn("5=C", text)
            self.assertNotIn("9=E", text)

    def test_remap_pairs_messages_that_differ_only_in_placeholder_style(self) -> None:
        """planet/star and <0>/<1> are one message: the pair is made and reported as normalized."""

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            truth = root / "truth.txt"
            onto = root / "fragment.txt"
            overlay = root / "overlay.txt"
            truth.write_text(
                "Script ^{\n    ModX ^{\n        10=Колонизация планеты planet (система star)\n    }\n}\n",
                encoding="utf-8",
            )
            onto.write_text("4=Колонизация планеты <0> (система <1>)\n", encoding="utf-8")
            overlay.write_text(
                "Script ^{\n    ModX ^{\n        10=Colonization of planet planet (star system)\n    }\n}\n",
                encoding="utf-8",
            )
            result = remap_languages(
                truth,
                onto,
                [overlay],
                out_dir=root / "out",
                script="ModX",
                placeholder_tokens=("planet", "star"),
            )
            self.assertEqual(result["summary"]["mapped"], 1)
            self.assertEqual(result["scripts"][0]["normalized"], [{"old": "10", "new": "4"}])
            text = (root / "out" / "overlay.txt").read_text(encoding="utf-8")
            self.assertIn("4=Colonization of planet planet (star system)", text)

    def test_remap_keeps_and_reports_a_key_without_a_counterpart(self) -> None:
        """A key whose text left the new script is not destroyed: it stays and fails the run."""

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            truth = root / "truth.txt"
            onto = root / "fragment.txt"
            overlay = root / "overlay.txt"
            truth.write_text(
                "Script ^{\n    ModX ^{\n        10=Alpha\n        20=Beta\n    }\n}\n",
                encoding="utf-8",
            )
            onto.write_text("1=Alpha\n", encoding="utf-8")
            overlay.write_text(
                "Script ^{\n    ModX ^{\n        10=A\n        20=B\n    }\n}\n", encoding="utf-8"
            )
            result = remap_languages(truth, onto, [overlay], out_dir=root / "out", script="ModX")
            self.assertFalse(result["valid"])
            self.assertEqual(result["summary"]["unmatched"], 1)
            text = (root / "out" / "overlay.txt").read_text(encoding="utf-8")
            self.assertIn("1=A", text)
            self.assertIn("20=B", text)

    def test_remap_requires_the_script_name_for_a_fragment(self) -> None:
        """A number=value fragment carries no script name, so --script must be supplied."""

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            truth = root / "truth.txt"
            onto = root / "fragment.txt"
            overlay = root / "overlay.txt"
            truth.write_text(
                "Script ^{\n    ModX ^{\n        10=Alpha\n    }\n}\n", encoding="utf-8"
            )
            onto.write_text("1=Alpha\n", encoding="utf-8")
            overlay.write_text(
                "Script ^{\n    ModX ^{\n        10=A\n    }\n}\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "--script"):
                remap_languages(truth, onto, [overlay], out_dir=root / "out")

    def test_remap_writes_a_signed_dat_pair_when_the_codec_is_available(self) -> None:
        """The production path: Lang.dat in, re-keyed Lang.dat out through the BlockPar codec."""

        chain = Toolchain()
        if not chain.tools["blockpar"].path.is_file():
            self.skipTest("BlockParEditor отсутствует")
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "truth.txt").write_text(
                "Script ^{\n    ModX ^{\n        10=Alpha\n        20=Beta\n    }\n}\n",
                encoding="utf-8",
            )
            (root / "overlay.txt").write_text(
                "Script ^{\n    ModX ^{\n        10=A\n        20=B\n    }\n}\n", encoding="utf-8"
            )
            (root / "fragment.txt").write_text("3=Alpha\n9=Beta\n", encoding="utf-8")
            chain.convert_dat(root / "truth.txt", root / "truth.dat", overwrite=True, verify=True)
            chain.convert_dat(root / "overlay.txt", root / "overlay.dat", overwrite=True, verify=True)
            result = remap_languages(
                root / "truth.dat",
                root / "fragment.txt",
                [root / "overlay.dat"],
                out_dir=root / "out",
                script="ModX",
            )
            out_dat = root / "out" / "overlay.dat"
            self.assertTrue(out_dat.is_file())
            self.assertEqual(result["summary"]["mapped"], 2)
            chain.convert_dat(out_dat, root / "out.txt", overwrite=True)
            text = (root / "out.txt").read_text(encoding="utf-16")
            self.assertIn("3=A", text)
            self.assertIn("9=B", text)

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
