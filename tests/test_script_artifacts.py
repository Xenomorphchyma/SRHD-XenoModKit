from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from srhd_modkit.blockpar import parse_blockpar
from srhd_modkit.cli import cmd_script_build
from srhd_modkit.script_artifacts import lint_script_cache
from tests.test_runtime_lint import SAFE_RSON


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


if __name__ == "__main__":
    unittest.main()
