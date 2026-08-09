from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from srhd_modkit.rsm import inspect_rsm_project


class RsmProjectTests(unittest.TestCase):
    def test_modular_project_records_import_closure_and_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "vars.rsm").write_text(
                'localVar("ready", "int", "0");\n', encoding="utf-8"
            )
            (root / "turn.rsm").write_text(
                "export function onTurn() { ready = 1; }\n", encoding="utf-8"
            )
            entry = root / "main.rsm"
            entry.write_text(
                'scriptName("Mod_Test");\n'
                "import from './vars.rsm';\n"
                "import from './turn.rsm';\n",
                encoding="utf-8",
            )

            project = inspect_rsm_project(entry)

            self.assertTrue(project.valid)
            self.assertEqual(project.script_name, "Mod_Test")
            self.assertEqual(len(project.modules), 3)
            self.assertTrue(all(len(module.sha256) == 64 for module in project.modules))

    def test_missing_import_and_missing_script_name_are_errors(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            entry = Path(name) / "main.rsm"
            entry.write_text("import from './missing.rsm';\n", encoding="utf-8")

            project = inspect_rsm_project(entry)
            codes = {issue.code for issue in project.issues}

            self.assertFalse(project.valid)
            self.assertIn("rsm-import-missing", codes)
            self.assertIn("rsm-script-name", codes)

    def test_import_cycle_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            entry = root / "main.rsm"
            entry.write_text(
                'scriptName("Mod_Cycle");\nimport from "./other.rsm";\n',
                encoding="utf-8",
            )
            (root / "other.rsm").write_text(
                'import from "./main.rsm";\n', encoding="utf-8"
            )

            project = inspect_rsm_project(entry)

            self.assertIn("rsm-import-cycle", {issue.code for issue in project.issues})

    def test_utf8_bom_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            entry = Path(name) / "main.rsm"
            entry.write_bytes(b"\xef\xbb\xbf" + b'scriptName("Mod_Bom");\n')
            self.assertTrue(inspect_rsm_project(entry).valid)

    def test_commented_imports_and_script_names_are_not_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            entry = Path(name) / "main.rsm"
            entry.write_text(
                "/*\n"
                "scriptName(\"Wrong\");\n"
                "import from './missing.rsm';\n"
                "*/\n"
                "// import from './also-missing.rsm';\n"
                "scriptName(\"Mod_Real\");\n",
                encoding="utf-8",
            )
            project = inspect_rsm_project(entry)
            self.assertTrue(project.valid)
            self.assertEqual(project.script_name, "Mod_Real")
            self.assertEqual(len(project.modules), 1)


if __name__ == "__main__":
    unittest.main()
