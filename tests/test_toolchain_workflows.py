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
from srhd_modkit.toolchain import Toolchain, _rscript_timeout_policy


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
    def test_progress_timeout_is_bounded_and_zero_disables_deadlines(self) -> None:
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

            self.assertEqual(small_timeout, 300.0)
            self.assertEqual(large_timeout, small_timeout)
            self.assertEqual(small_policy["mode"], "progress-aware")
            self.assertEqual(small_policy["progress_seconds"], 60.0)
            self.assertEqual(large_policy["progress_seconds"], 60.0)
            self.assertEqual(explicit, 90.0)
            self.assertEqual(explicit_policy["progress_seconds"], 60.0)
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


if __name__ == "__main__":
    unittest.main()
