from __future__ import annotations

import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from srhd_modkit.scripts import RSON_FILE_ID, RSON_FILE_VERSION, load_rson
from srhd_modkit.scripts import inspect_scr
from srhd_modkit.toolchain import (
    Toolchain,
    _decompiled_runtime_issue,
    _rscript_failure_diagnostic,
    _rscript_timeout_policy,
)
from srhd_modkit.runtime_lint import RuntimeIssue


PROJECT = {
    "FileID": RSON_FILE_ID,
    "FileVersion": RSON_FILE_VERSION,
    "ScriptName": "Workflow",
    "Visual.Objects": [
        {
            "Operations": [
                {
                    "Type": "Top",
                    "Name": "Init",
                    "Parent": -1,
                    "#": 1,
                    "Code.Type": "Init",
                    "Code": ["result = 1;"],
                }
            ]
        }
    ],
    "Visual.Links": [],
}


class ToolchainWorkflowTests(unittest.TestCase):
    def test_decompiled_runtime_issues_keep_analysis_provenance(self) -> None:
        sensitive = _decompiled_runtime_issue(
            RuntimeIssue(
                "warning",
                "runtime-turn-direct-world-access",
                "canonical graph may lose the source gate",
            )
        )
        regular = _decompiled_runtime_issue(
            RuntimeIssue("error", "runtime-object-api-without-explicit-guard", "unsafe")
        )
        self.assertEqual(sensitive["analysis_origin"], "decompiled-rson")
        self.assertTrue(sensitive["canonicalization_sensitive"])
        self.assertFalse(regular["canonicalization_sensitive"])

    def test_progress_timeout_scales_and_zero_disables_deadlines(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            small = root / "small.rson"
            large = root / "large.rson"
            small.write_text(json.dumps(PROJECT), encoding="utf-8")
            data = deepcopy(PROJECT)
            data["Visual.Objects"][0]["Operations"][0]["Code"] = ["result = 1;"] * 5000
            large.write_text(json.dumps(data), encoding="utf-8")

            small_timeout, small_policy = _rscript_timeout_policy(small, "compile", None)
            large_timeout, large_policy = _rscript_timeout_policy(large, "compile", None)
            explicit, explicit_policy = _rscript_timeout_policy(large, "compile", 90)
            disabled, disabled_policy = _rscript_timeout_policy(large, "compile", 0)

            self.assertEqual(small_timeout, 600.0)
            self.assertGreater(large_timeout, small_timeout)
            self.assertEqual(small_policy["mode"], "adaptive")
            self.assertEqual(small_policy["progress_seconds"], 60.0)
            self.assertGreater(large_policy["progress_seconds"], 60.0)
            self.assertEqual(explicit, 90.0)
            self.assertEqual(explicit_policy["progress_seconds"], 90.0)
            self.assertIsNone(disabled)
            self.assertEqual(disabled_policy["mode"], "disabled")
            self.assertIsNone(disabled_policy["progress_seconds"])

    def test_failed_validation_never_publishes_main_output(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "source.scr"
            output = root / "verified.rson"
            unverified = root / "explicit-unverified.rson"
            source.write_bytes((8).to_bytes(4, "little") + b"test")
            chain = Toolchain(root / "tools")
            stale = root / ".srhd-decompile-stale"
            stale.mkdir()
            marker = stale / ".srhd-transaction"
            marker.write_text("decompile-v1\n", encoding="ascii")
            os.utime(marker, (0, 0))
            unmarked = root / ".srhd-decompile-user-data"
            unmarked.mkdir()

            def fake_recover(_source, recovered, **_kwargs):
                data = deepcopy(PROJECT)
                data["Visual.Objects"][0]["Operations"][0]["Code"] = [
                    "q=0;Потерянный комментарий"
                ]
                recovered.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                return SimpleNamespace(exit_code=0, forced_after_outputs=False, elapsed_seconds=0.01), {
                    "mode": "progress-aware",
                    "seconds": 300.0,
                    "progress_seconds": 60.0,
                }

            with patch.object(chain, "_recover_scr_with_rscript", side_effect=fake_recover):
                result = chain.decompile_scr(
                    source,
                    output,
                    keep_unverified=unverified,
                )

            self.assertFalse(result["verified"])
            self.assertEqual(result["status"], "unverified")
            self.assertFalse(output.exists())
            self.assertTrue(unverified.is_file())
            self.assertIn(
                "rscript-uncommented-text",
                {issue["code"] for issue in result["validation_issues"]},
            )
            self.assertFalse(stale.exists())
            self.assertTrue(unmarked.is_dir())
            self.assertEqual(
                [Path(value).resolve() for value in result["stale_transactions_removed"]],
                [stale.resolve()],
            )

    def test_compare_scr_reports_code_and_runtime_deltas_without_persisting_rson(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            chain = Toolchain(root / "tools")
            (root / "left.scr").write_bytes((8).to_bytes(4, "little") + b"left")
            (root / "right.scr").write_bytes((8).to_bytes(4, "little") + b"right")

            def fake_decompile(source, destination, **_kwargs):
                data = deepcopy(PROJECT)
                is_right = Path(source).stem == "right"
                if is_right:
                    data["Visual.Objects"][0]["Operations"][0]["Code"] = ["result = 2;"]
                destination = Path(destination)
                destination.write_text(json.dumps(data), encoding="utf-8")
                project = load_rson(destination)
                issue = {
                    "severity": "warning",
                    "code": "right-only" if is_right else "left-only",
                    "message": "changed",
                    "path": str(destination),
                    "location": "object #1 Code",
                    "evidence": None,
                }
                return {
                    "source": str(source),
                    "status": "verified",
                    "verified": True,
                    "source_sha256": "right" if is_right else "left",
                    "source_version": 8,
                    "lang_dat": "Lang.dat",
                    "dialogs_imported": not is_right,
                    "lang_import": {
                        "status": "failed-fallback" if is_right else "passed",
                        "fallback_used": is_right,
                        "diagnostic": None,
                    },
                    "recovered_project": project.summary(),
                    "roundtrip": {},
                    "deep_roundtrip": None,
                    "runtime_issues": [issue],
                    "phases": [],
                    "error": None,
                    "timeouts": {},
                }

            with patch.object(chain, "decompile_scr", side_effect=fake_decompile):
                result = chain.compare_scr(root / "left.scr", root / "right.scr")

            self.assertTrue(result["verified"])
            self.assertTrue(result["comparison"]["code_changed"])
            self.assertTrue(result["comparison"]["event_signatures_match"])
            self.assertEqual(len(result["comparison"]["changed_blocks"]), 1)
            self.assertEqual(len(result["comparison"]["runtime_issues"]["added"]), 1)
            self.assertEqual(len(result["comparison"]["runtime_issues"]["resolved"]), 1)
            self.assertFalse(result["comparison"]["temporary_projects_persisted"])
            self.assertTrue(result["right"]["lang_import"]["fallback_used"])
            self.assertFalse(result["right"]["dialogs_imported"])

    def test_tfileec_modal_is_structured_and_lang_fallback_is_explicit(self) -> None:
        diagnostic = _rscript_failure_diagnostic(
            TimeoutError(
                r"Процесс остановлен; контролы диалога: TFileEC.Open. FileName=D:\RScript\BlockPar\temp.txt."
            )
        )
        self.assertIsNotNone(diagnostic)
        self.assertEqual(diagnostic["code"], "decompile-lang-import-tfileec-open")
        self.assertTrue(diagnostic["temp_path"].endswith("temp.txt"))

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "source.scr"
            output = root / "verified.rson"
            lang = root / "Lang.dat"
            source.write_bytes((8).to_bytes(4, "little") + b"source")
            lang.write_bytes(b"not-empty")
            chain = Toolchain(root / "tools")
            recover_calls: list[Path | None] = []

            def fake_recover(_source, recovered, *, lang_dat, **_kwargs):
                recover_calls.append(lang_dat)
                if lang_dat is not None:
                    raise TimeoutError(
                        r"TFileEC.Open. FileName=D:\RScript\BlockPar\temp.txt."
                    )
                recovered.write_text(json.dumps(PROJECT), encoding="utf-8")
                return SimpleNamespace(
                    exit_code=0,
                    forced_after_outputs=False,
                    elapsed_seconds=0.01,
                    queue_seconds=0.0,
                    progress_updates=1,
                    last_progress_seconds=0.01,
                ), {
                    "mode": "explicit-test",
                    "seconds": 60.0,
                    "progress_seconds": 60.0,
                }

            def fake_compile(_source, scr_output, lang_output, **_kwargs):
                scr_output.write_bytes((8).to_bytes(4, "little") + b"rebuilt")
                lang_output.write_text("", encoding="utf-8")
                process = SimpleNamespace(
                    exit_code=0,
                    forced_after_outputs=False,
                    elapsed_seconds=0.01,
                    queue_seconds=0.0,
                    progress_updates=1,
                    last_progress_seconds=0.01,
                )
                return process, inspect_scr(scr_output), {
                    "mode": "explicit-test",
                    "seconds": 60.0,
                    "progress_seconds": 60.0,
                }

            with patch.object(chain, "_recover_scr_with_rscript", side_effect=fake_recover), patch.object(
                chain, "_compile_rson_with_rscript", side_effect=fake_compile
            ):
                result = chain.decompile_scr(
                    source,
                    output,
                    lang_dat=lang,
                    fallback_without_lang=True,
                )

            self.assertTrue(result["verified"])
            self.assertFalse(result["dialogs_imported"])
            self.assertTrue(result["lang_import"]["fallback_used"])
            self.assertEqual(result["lang_import"]["status"], "failed-fallback")
            self.assertEqual(recover_calls, [lang.resolve(), None])


if __name__ == "__main__":
    unittest.main()
