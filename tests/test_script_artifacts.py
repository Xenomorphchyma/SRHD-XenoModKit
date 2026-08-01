from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from srhd_modkit.blockpar import parse_blockpar
from srhd_modkit.cli import (
    cmd_script_audit_mod,
    cmd_script_build,
    cmd_script_lint_runtime,
)
from srhd_modkit.script_artifacts import (
    lint_script_cache,
    lint_script_dialog_language,
)
from srhd_modkit.scripts import RSON_FILE_ID, RSON_FILE_VERSION, RsonProject
from srhd_modkit.toolchain import Toolchain
from tests.test_runtime_lint import SAFE_RSON


def _dialog_project() -> RsonProject:
    return RsonProject(
        {
            "ScriptName": "Mod_Test",
            "Visual.Objects": [
                {
                    "Dialogs": [
                        {
                            "Type": "TDialog",
                            "Name": "TestDialog",
                            "Parent": -1,
                            "#": 1,
                        },
                        {
                            "Type": "TDialogMsg",
                            "Name": "",
                            "Parent": -1,
                            "#": 3,
                            "DMsg.Num": 0,
                            "Msg": "",
                        },
                        {
                            "Type": "TDialogAnswer",
                            "Name": "fastexit",
                            "Parent": -1,
                            "#": 5,
                            "AMsg.Num": 0,
                            "Msg": "DAnswer(CT('Script.Mod_Test.1'));",
                        },
                        {
                            "Type": "TDialogAnswer",
                            "Name": "fastexit",
                            "Parent": -1,
                            "#": 6,
                            "AMsg.Num": 1,
                            "Msg": "DAnswer(CT('Script.Mod_Test.2'));",
                        },
                    ],
                    "Operations": [
                        {
                            "Type": "Top",
                            "Name": "DialogRoot",
                            "Parent": -1,
                            "#": 2,
                            "Code": ["DChange(0);"],
                        },
                        {
                            "Type": "Top",
                            "Name": "DialogMessage",
                            "Parent": -1,
                            "#": 4,
                            "Code": ["DAdd(0);", "DAdd(1);"],
                        },
                    ],
                }
            ],
            "Visual.Links": [
                {"Begin": 1, "End": 2},
                {"Begin": 3, "End": 4},
            ],
        },
        Path("Mod_Test.rson"),
    )


_GENERATED_DIALOG = {
    "Mod_Test": (
        Path("Mod_Test.lang.txt"),
        (
            ("0", ""),
            ("1", "DAnswer('fastexit~First answer')"),
            ("2", "DAnswer('fastexit~Second answer')"),
        ),
    )
}


def _write_dialog_mod(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "ModuleInfo.txt").write_text(
        "Name=DialogFixture\nLanguages=Rus\n",
        encoding="cp1251",
    )
    project = deepcopy(_dialog_project().data)
    project["FileID"] = RSON_FILE_ID
    project["FileVersion"] = RSON_FILE_VERSION
    for link in project["Visual.Links"]:
        link.update({"Type": "TGraphLink", "Nom": 0, "Arrow": True})
    source = root / "SOURCE"
    source.mkdir()
    (source / "Mod_Test.rson").write_text(
        json.dumps(project),
        encoding="utf-8",
    )
    (source / "Mod_Test.lang.txt").write_bytes(
        b"\xff\xfe"
        + (
            "0=\r\n"
            "1=DAnswer('fastexit~First answer')\r\n"
            "2=DAnswer('fastexit~Second answer')\r\n"
        ).encode("utf-16-le")
    )


class ScriptArtifactTests(unittest.TestCase):
    def test_wrong_local_cache_target_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "TestMod"
            script = root / "DATA" / "Script" / "Mod_Test.scr"
            cache = parse_blockpar(
                "Script ^{\n"
                "  Mod_Test=Mods\\OtherMods\\MainMod\\DATA\\Script\\Mod_Main.scr\n"
                "}\n"
            )
            issues = lint_script_cache(
                root,
                [script],
                {"mod_test": ["1,Script.Mod_Test"]},
                [(root / "SOURCE" / "CFG" / "CacheData.txt", cache)],
            )
            codes = {issue.code for issue in issues}
            self.assertIn("cache-script-key-path-mismatch", codes)
            self.assertIn("cache-script-local-path-mismatch", codes)

    def test_external_dependency_cache_entry_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "TestMod"
            script = root / "DATA" / "Script" / "Mod_Test.scr"
            cache = parse_blockpar(
                "Script ~{\n"
                "  Mod_Test=Mods\\OtherMods\\TestMod\\DATA\\Script\\Mod_Test.scr\n"
                "  PC_part0=Mods\\Tweaks\\UtilityFunctionsPack\\DATA\\Script\\PC_part0.scr\n"
                "}\n"
            )
            issues = lint_script_cache(
                root,
                [script],
                {"mod_test": ["1,Script.Mod_Test"]},
                [(root / "CFG" / "CacheData.txt", cache)],
                install_subpath="OtherMods/TestMod",
            )
            self.assertEqual(issues, [])

    def test_installed_mod_cache_path_must_match_actual_mods_location(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "Mods" / "LifeBeforeRanger"
            script = root / "DATA" / "Script" / "Mod_LifeBeforeRanger.scr"
            cache = parse_blockpar(
                "Script ^{\n"
                "  Mod_LifeBeforeRanger=Mods\\OtherMods\\LifeBeforeRanger\\DATA\\Script\\Mod_LifeBeforeRanger.scr\n"
                "}\n"
            )
            issues = lint_script_cache(
                root,
                [script],
                {"mod_lifebeforeranger": ["1,Script.Mod_LifeBeforeRanger"]},
                [(root / "SOURCE" / "CFG" / "CacheData.txt", cache)],
            )
            matching = [
                issue
                for issue in issues
                if issue.code == "cache-script-install-path-mismatch"
            ]
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0].severity, "error")
            self.assertIn(
                r"ожидается: Mods\LifeBeforeRanger\DATA\Script\Mod_LifeBeforeRanger.scr",
                matching[0].evidence or "",
            )

    def test_workspace_nested_cache_path_is_reported_as_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "TestMod"
            script = root / "DATA" / "Script" / "Mod_Test.scr"
            cache = parse_blockpar(
                "Script ^{\n"
                "  Mod_Test=Mods\\OtherMods\\TestMod\\DATA\\Script\\Mod_Test.scr\n"
                "}\n"
            )
            issues = lint_script_cache(
                root,
                [script],
                {"mod_test": ["1,Script.Mod_Test"]},
                [(root / "SOURCE" / "CFG" / "CacheData.txt", cache)],
            )
            matching = [
                issue
                for issue in issues
                if issue.code == "cache-script-install-path-unverified"
            ]
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0].severity, "warning")
            self.assertIn(r"Mods\OtherMods\TestMod", matching[0].message)

    def test_declared_release_prefix_accepts_nested_installation(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "TestMod"
            script = root / "DATA" / "Script" / "Mod_Test.scr"
            cache = parse_blockpar(
                "Script ^{\n"
                "  Mod_Test=Mods\\OtherMods\\TestMod\\DATA\\Script\\Mod_Test.scr\n"
                "}\n"
            )
            issues = lint_script_cache(
                root,
                [script],
                {"mod_test": ["1,Script.Mod_Test"]},
                [(root / "SOURCE" / "CFG" / "CacheData.txt", cache)],
                install_subpath="OtherMods/TestMod",
            )
            self.assertEqual(issues, [])

    def test_source_and_binary_cache_semantics_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "TestMod"
            script = root / "DATA" / "Script" / "Mod_Test.scr"
            good = parse_blockpar(
                "Script ^{\n"
                "  Mod_Test=Mods\\OtherMods\\TestMod\\DATA\\Script\\Mod_Test.scr\n"
                "}\n"
            )
            stale = parse_blockpar(
                "Script ^{\n"
                "  Mod_Old=Mods\\OtherMods\\OldMod\\DATA\\Script\\Mod_Old.scr\n"
                "}\n"
            )
            issues = lint_script_cache(
                root,
                [script],
                {"mod_test": ["1,Script.Mod_Test"]},
                [
                    (root / "SOURCE" / "CFG" / "CacheData.txt", good),
                    (root / "CFG" / "CacheData.dat", stale),
                ],
            )
            codes = {issue.code for issue in issues}
            self.assertIn("cachedata-source-binary-mismatch", codes)
            self.assertIn("cache-script-missing", codes)

    def test_build_stops_before_compiler_on_stale_cache_path(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "TestMod"
            source = root / "SOURCE"
            cfg = source / "CFG"
            cfg.mkdir(parents=True)
            rson = source / "Mod_Test.rson"
            rson_data = deepcopy(SAFE_RSON)
            group = rson_data["Visual.Objects"][0]
            group["Operations"] = [group["Operations"][0]]
            group["Operations"][0]["Code"] = ["GRun();"]
            group["States"] = []
            rson.write_text(json.dumps(rson_data), encoding="utf-8")
            (root / "ModuleInfo.txt").write_text("Name=TestMod\nLanguages=Rus\n", encoding="utf-8")
            (cfg / "Main.txt").write_text(
                "Data ^{\n"
                "  Script ^{\n"
                "    Mod_Test=1,Script.Mod_Test\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )
            (cfg / "CacheData.txt").write_text(
                "Script ^{\n"
                "  Mod_Test=Mods\\OtherMods\\MainMod\\DATA\\Script\\Mod_Main.scr\n"
                "}\n",
                encoding="utf-8",
            )
            args = SimpleNamespace(
                source=str(rson),
                scr=str(root / "DATA" / "Script" / "Mod_Test.scr"),
                lang=str(root / "DATA" / "Script" / "Lang.dat"),
                overwrite=False,
                tools_root=None,
                json=False,
            )
            with self.assertRaisesRegex(ValueError, "cache-script-key-path-mismatch"):
                cmd_script_build(args)

    def test_dialog_answers_require_a_packaged_lang_dat(self) -> None:
        issues = lint_script_dialog_language(
            [_dialog_project()],
            [],
            _GENERATED_DIALOG,
            checked_scripts=["Mod_Test"],
        )
        self.assertEqual(
            [issue.code for issue in issues],
            ["script-dialog-lang-dat-missing"],
        )
        self.assertIn("TestDialog", issues[0].message)
        self.assertIn("TDialogAnswer #5", issues[0].message)
        self.assertIn("Script/Mod_Test/1,2", issues[0].message)

    def test_dialog_language_reports_missing_node_key_and_empty_value(self) -> None:
        missing_node = parse_blockpar("Script ^{\n  Other ~{\n  }\n}\n")
        issues = lint_script_dialog_language(
            [_dialog_project()],
            [(Path("CFG/Rus/Lang.dat"), missing_node)],
            _GENERATED_DIALOG,
            checked_scripts=["Mod_Test"],
        )
        self.assertEqual(
            [issue.code for issue in issues],
            ["script-dialog-lang-node-missing"],
        )

        incomplete = parse_blockpar(
            "Script ^{\n"
            "  Mod_Test ~{\n"
            "    1=\n"
            "  }\n"
            "}\n"
        )
        issues = lint_script_dialog_language(
            [_dialog_project()],
            [(Path("CFG/Rus/Lang.dat"), incomplete)],
            _GENERATED_DIALOG,
            checked_scripts=["Mod_Test"],
        )
        codes = [issue.code for issue in issues]
        self.assertIn("script-dialog-lang-value-empty", codes)
        self.assertIn("script-dialog-lang-key-missing", codes)
        self.assertIn("script-generated-lang-unpublished", codes)
        missing_key = next(
            issue
            for issue in issues
            if issue.code == "script-dialog-lang-key-missing"
        )
        self.assertIn("TestDialog", missing_key.message)
        self.assertEqual(missing_key.location, "Script/Mod_Test/2")

    def test_data_script_lang_is_not_runtime_proof_for_static_answers(self) -> None:
        compact = parse_blockpar(
            "Script ^{\n"
            "  Mod_Test ~{\n"
            "    1=First answer\n"
            "    2=Second answer\n"
            "  }\n"
            "}\n"
        )
        issues = lint_script_dialog_language(
            [_dialog_project()],
            [(Path("DATA/Script/Lang.dat"), compact)],
            _GENERATED_DIALOG,
            checked_scripts=["Mod_Test"],
        )
        self.assertEqual(
            [issue.code for issue in issues],
            ["script-dialog-lang-runtime-dat-missing"],
        )
        self.assertIn("CFG/<язык>/Lang.dat", issues[0].message)
        self.assertIn("DATA/Script/Lang.dat", issues[0].message)

    def test_complete_dialog_language_passes_cfg_language_layout(self) -> None:
        translated = parse_blockpar(
            "Script ^{\n"
            "  Mod_Test ~{\n"
            "    1=Первый ответ\n"
            "    2=Второй ответ\n"
            "  }\n"
            "}\n"
        )
        unrelated_compact = parse_blockpar("Script ^{\n  Other ~{\n  }\n}\n")
        self.assertEqual(
            lint_script_dialog_language(
                [_dialog_project()],
                [
                    (Path("DATA/Script/Lang.dat"), unrelated_compact),
                    (Path("CFG/Rus/Lang.dat"), translated),
                ],
                _GENERATED_DIALOG,
                checked_scripts=["Mod_Test"],
            ),
            [],
        )

    def test_canonical_dialog_key_is_checked_without_generated_fragment(self) -> None:
        project = _dialog_project()
        answer = project.object_by_id(5)
        answer["Msg"] = "DAnswer(CT('Script.Mod_Test.41'));"
        project.object_by_id(6)["Msg"] = ""
        language = parse_blockpar(
            "Script ^{\n"
            "  Mod_Test ~{\n"
            "    41=Canonical answer\n"
            "  }\n"
            "}\n"
        )
        self.assertEqual(
            lint_script_dialog_language(
                [project],
                [(Path("CFG/Rus/Lang.dat"), language)],
                checked_scripts=["Mod_Test"],
            ),
            [],
        )

    def test_dialog_language_rejects_rscript_code_stub_values(self) -> None:
        code_stubs = parse_blockpar(
            "Script ^{\n"
            "  Mod_Test ~{\n"
            "    1=DAnswer('fastexit~First answer')\n"
            "    2=DText('Second answer')\n"
            "  }\n"
            "}\n"
        )
        issues = lint_script_dialog_language(
            [_dialog_project()],
            [(Path("CFG/Rus/Lang.dat"), code_stubs)],
            _GENERATED_DIALOG,
            checked_scripts=["Mod_Test"],
        )
        matching = [
            issue
            for issue in issues
            if issue.code == "script-dialog-lang-value-code-stub"
        ]
        self.assertEqual(len(matching), 2)
        self.assertTrue(all("TestDialog" in issue.message for issue in matching))
        self.assertFalse(
            any(issue.code == "script-generated-lang-unpublished" for issue in issues)
        )

    def test_scr_only_dialog_key_requires_packaged_language(self) -> None:
        binary = {
            "path": str(Path("DATA/Script/Mod_Binary.scr").resolve()),
            "name": "Mod_Binary",
            "dialog_language_keys": [
                {"script_name": "Mod_Binary", "key": "12"},
                {"script_name": "Mod_Binary", "key": "13"},
            ],
        }
        issues = lint_script_dialog_language(
            [],
            [],
            checked_scripts=["Mod_Binary"],
            binary_scripts=[binary],
        )
        self.assertEqual(
            [issue.code for issue in issues],
            ["script-dialog-lang-dat-missing"],
        )
        self.assertIn("Script/Mod_Binary/12,13", issues[0].message)
        self.assertIn("номер объекта недоступен", issues[0].message)

        data_only = parse_blockpar(
            "Script ^{\n"
            "  Mod_Binary ~{\n"
            "    12=First binary answer\n"
            "    13=Second binary answer\n"
            "  }\n"
            "}\n"
        )
        issues = lint_script_dialog_language(
            [],
            [(Path("DATA/Script/Lang.dat"), data_only)],
            checked_scripts=["Mod_Binary"],
            binary_scripts=[binary],
        )
        self.assertEqual(
            [issue.code for issue in issues],
            ["script-dialog-lang-runtime-dat-missing"],
        )

    def test_script_audit_and_runtime_cli_report_missing_dialog_language(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "DialogFixture"
            _write_dialog_mod(root)

            output = StringIO()
            with redirect_stdout(output):
                code = cmd_script_audit_mod(
                    SimpleNamespace(mod=str(root), tools_root=None, json=True)
                )
            self.assertEqual(code, 2)
            report = json.loads(output.getvalue())
            self.assertIn(
                "script-dialog-lang-dat-missing",
                {issue["code"] for issue in report["issues"]},
            )
            self.assertEqual(report["artifact_lint"]["language"], [
                str((root / "SOURCE" / "Mod_Test.lang.txt").resolve())
            ])

            output = StringIO()
            with redirect_stdout(output):
                code = cmd_script_lint_runtime(
                    SimpleNamespace(
                        target=str(root),
                        tools_root=None,
                        main=None,
                        module_info=None,
                        strict=False,
                        json=True,
                    )
                )
            self.assertEqual(code, 2)
            report = json.loads(output.getvalue())
            self.assertIn(
                "script-dialog-lang-dat-missing",
                {issue["code"] for issue in report["issues"]},
            )

            lang_source = root / "SOURCE" / "Lang.txt"
            lang_source.write_text(
                "Script ^{\n"
                "  Mod_Test ~{\n"
                "    1=DAnswer('fastexit~First answer')\n"
                "    2=DAnswer('fastexit~Second answer')\n"
                "  }\n"
                "}\n",
                encoding="cp1251",
            )
            lang_dat = root / "DATA" / "Script" / "Lang.dat"
            lang_dat.parent.mkdir(parents=True)
            Toolchain().convert_dat(lang_source, lang_dat)
            output = StringIO()
            with redirect_stdout(output):
                code = cmd_script_lint_runtime(
                    SimpleNamespace(
                        target=str(root),
                        tools_root=None,
                        main=None,
                        module_info=None,
                        strict=False,
                        json=True,
                    )
                )
            self.assertEqual(code, 2)
            report = json.loads(output.getvalue())
            self.assertIn(
                "script-dialog-lang-value-code-stub",
                {issue["code"] for issue in report["issues"]},
            )


if __name__ == "__main__":
    unittest.main()
