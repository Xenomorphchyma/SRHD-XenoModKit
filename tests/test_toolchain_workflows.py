from __future__ import annotations

import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from srhd_modkit.scripts import RSON_FILE_ID, RSON_FILE_VERSION, RsonProject, load_rson
from srhd_modkit.scripts import inspect_scr
from srhd_modkit.blockpar import parse_blockpar
from srhd_modkit.toolchain import (
    ScriptBuildFailure,
    Toolchain,
    _decompiled_runtime_issue,
    _rscript_failure_diagnostic,
    _rscript_timeout_policy,
    inspect_rscript_lang_fragment,
)
from srhd_modkit.runtime_lint import RuntimeIssue
from srhd_modkit.hidden_process import HiddenProcessTimeout


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
            update_issues = result["comparison"]["update_issues"]
            self.assertEqual(len(update_issues), 1)
            self.assertEqual(
                update_issues[0]["code"],
                "runtime-saved-script-cache-update-shadow",
            )
            self.assertEqual(update_issues[0]["severity"], "warning")
            self.assertEqual(update_issues[0]["script_name"], "Workflow")
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

    def test_silent_rscript_main_window_stall_has_complete_failure_report(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "source.rson"
            source.write_text(json.dumps(PROJECT), encoding="utf-8")
            rscript = root / "RScript" / "RScript.exe"
            rscript.parent.mkdir()
            rscript.write_bytes(b"fixture")
            chain = Toolchain(root)
            timeout = HiddenProcessTimeout(
                "Процесс не показал подтверждённого прогресса; "
                "скрытое окно: RScript 4.10f; RScript; OK; Build; "
                "Dat files params (optional); Script params",
                timeout_kind="progress",
                exit_code=124,
                elapsed_seconds=60.0,
                window_text=("RScript 4.10f", "Build", "Script params"),
                window_diagnostics=("RScript / #32770",),
                dialog_controls=("OK", "Build"),
                control_diagnostics=(),
                progress_updates=1,
                last_progress_seconds=1.3,
            )
            with patch(
                "srhd_modkit.toolchain.run_on_hidden_desktop",
                side_effect=timeout,
            ), self.assertRaises(ScriptBuildFailure) as caught:
                chain._compile_rson_with_rscript(
                    source,
                    root / "out.scr",
                    root / "lang.txt",
                    timeout=1,
                )

            report = caught.exception.as_dict()
            self.assertEqual(report["schema"], "srhd-modkit-script-build-v1")
            self.assertEqual(report["status"], "failed")
            self.assertTrue(report["preflight_passed"])
            self.assertTrue(report["compiler_started"])
            self.assertFalse(report["compiler_output_created"])
            self.assertFalse(report["published_outputs"])
            self.assertEqual(
                report["failure"]["code"],
                "rscript-build-silent-main-window-stall",
            )
            self.assertEqual(report["compiler"]["version"], "4.10f")
            self.assertIn("timeout", report["compiler"])
            self.assertEqual(report["compiler"]["exit_code"], 124)
            self.assertEqual(report["compiler"]["last_progress_seconds"], 1.3)
            self.assertEqual(
                report["failure"]["process"]["window_diagnostics"],
                ["RScript / #32770"],
            )

    def test_rscript_lang_fragment_is_classified_without_treating_it_as_dat(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            complete = root / "complete.txt"
            incomplete = root / "incomplete.txt"
            code_stub = root / "code-stub.txt"
            empty = root / "empty.txt"
            invalid = root / "invalid.txt"
            duplicate = root / "duplicate.txt"
            complete.write_bytes("0=Готово\r\n1=Назад\r\n".encode("utf-16"))
            incomplete.write_bytes(
                '0=Script.Workflow.4\r\n1=DAnswer(CT("Script.Workflow.5"));\r\n'.encode(
                    "utf-16"
                )
            )
            code_stub.write_bytes(
                "0=\r\n1=DAnswer('fastexit~Ой, извини')\r\n".encode("utf-16")
            )
            empty.write_bytes(b"\xff\xfe")
            invalid.write_bytes("0=Повреждён�\r\n".encode("utf-16"))
            duplicate.write_bytes("0=Один\r\n0=Два\r\n".encode("utf-16"))

            self.assertEqual(inspect_rscript_lang_fragment(complete).status, "complete")
            value = inspect_rscript_lang_fragment(incomplete)
            self.assertEqual(value.status, "incomplete")
            self.assertEqual(value.placeholder_keys, ("0", "1"))
            self.assertEqual(
                value.referenced_ct_keys,
                ("Script.Workflow.4", "Script.Workflow.5"),
            )
            stub_value = inspect_rscript_lang_fragment(code_stub)
            self.assertEqual(stub_value.status, "incomplete")
            self.assertEqual(stub_value.placeholder_keys, ("1",))
            self.assertEqual(inspect_rscript_lang_fragment(empty).status, "empty")
            invalid_value = inspect_rscript_lang_fragment(invalid)
            self.assertEqual(invalid_value.status, "invalid")
            self.assertEqual(invalid_value.invalid_text_keys, ("0",))
            with self.assertRaisesRegex(ValueError, "Дублирующийся ключ"):
                inspect_rscript_lang_fragment(duplicate)

    def test_script_lang_base_rejects_code_stub_values(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            fragment_path = root / "fragment.lang.txt"
            fragment_path.write_bytes(
                "0=\r\n1=DAnswer('fastexit~Ой, извини')\r\n".encode("utf-16")
            )
            fragment = inspect_rscript_lang_fragment(fragment_path)
            base = root / "Lang.txt"
            base.write_text(
                "Script ^{\n"
                "  Workflow ~{\n"
                "    1=DAnswer('fastexit~Ой, извини')\n"
                "  }\n"
                "}\n",
                encoding="cp1251",
            )
            project = RsonProject(deepcopy(PROJECT), root / "Workflow.rson")
            with self.assertRaisesRegex(ValueError, "RScript-код вместо видимого текста"):
                Toolchain()._prepare_script_lang_dat(
                    project,
                    fragment,
                    root / "Lang.dat",
                    root,
                    base=base,
                )

    def test_compile_does_not_publish_incomplete_fragment_as_lang_dat(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "workflow.rson"
            source.write_text(json.dumps(PROJECT), encoding="utf-8")
            scr = root / "workflow.scr"
            lang_dat = root / "Lang.dat"
            chain = Toolchain()

            def fake_compile(_source, scr_output, lang_output, **_kwargs):
                scr_output.parent.mkdir(parents=True, exist_ok=True)
                scr_output.write_bytes((8).to_bytes(4, "little") + b"compiled")
                lang_output.write_bytes('0=Script.Workflow.0\r\n'.encode("utf-16"))
                process = SimpleNamespace(
                    exit_code=0,
                    forced_after_outputs=False,
                    elapsed_seconds=0.01,
                    queue_seconds=0.0,
                    progress_updates=1,
                    last_progress_seconds=0.01,
                )
                return process, inspect_scr(scr_output), {"mode": "test"}

            with patch.object(chain, "_compile_rson_with_rscript", side_effect=fake_compile):
                with self.assertRaisesRegex(ValueError, "неполный языковой фрагмент"):
                    chain.compile_rson(source, scr, lang_dat)

            self.assertFalse(scr.exists())
            self.assertFalse(lang_dat.exists())

    def test_compile_rejects_invalid_cp1251_language_text_before_publish(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "workflow.rson"
            source.write_text(json.dumps(PROJECT), encoding="utf-8")
            scr = root / "workflow.scr"
            fragment = root / "workflow.lang.txt"
            chain = Toolchain()

            def fake_compile(_source, scr_output, lang_output, **_kwargs):
                scr_output.parent.mkdir(parents=True, exist_ok=True)
                scr_output.write_bytes((8).to_bytes(4, "little") + b"compiled")
                lang_output.write_bytes("0=Повреждён�\r\n".encode("utf-16"))
                process = SimpleNamespace(
                    exit_code=0,
                    forced_after_outputs=False,
                    elapsed_seconds=0.01,
                    queue_seconds=0.0,
                    progress_updates=1,
                    last_progress_seconds=0.01,
                )
                return process, inspect_scr(scr_output), {"mode": "test"}

            with patch.object(chain, "_compile_rson_with_rscript", side_effect=fake_compile):
                with self.assertRaisesRegex(ValueError, "не совместимый с CP1251"):
                    chain.compile_rson(source, scr, fragment)

            self.assertFalse(scr.exists())
            self.assertFalse(fragment.exists())

    def test_compile_can_preserve_verified_lang_base_for_incomplete_rson(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            data = deepcopy(PROJECT)
            data["Visual.Objects"][0]["Operations"][0]["Code"] = [
                'result = CT("Script.Workflow.0");'
            ]
            source = root / "workflow.rson"
            source.write_text(json.dumps(data), encoding="utf-8")
            scr = root / "workflow.scr"
            lang_dat = root / "Lang.dat"
            base = root / "base.dat"
            base.write_bytes(b"verified-base-dat")
            base_document = parse_blockpar(
                "Script ^{\n    Workflow ~{\n        0=Сохранённый текст\n    }\n}\n",
                encoding="cp1251",
            )
            chain = Toolchain()

            def fake_compile(_source, scr_output, lang_output, **_kwargs):
                scr_output.parent.mkdir(parents=True, exist_ok=True)
                scr_output.write_bytes((8).to_bytes(4, "little") + b"compiled")
                lang_output.write_bytes('0=Script.Workflow.0\r\n'.encode("utf-16"))
                process = SimpleNamespace(
                    exit_code=0,
                    forced_after_outputs=False,
                    elapsed_seconds=0.01,
                    queue_seconds=0.0,
                    progress_updates=1,
                    last_progress_seconds=0.01,
                )
                return process, inspect_scr(scr_output), {"mode": "test"}

            with patch.object(chain, "_compile_rson_with_rscript", side_effect=fake_compile), patch.object(
                chain,
                "_load_script_lang_base",
                return_value=(base_document, base),
            ):
                result = chain.compile_rson(
                    source,
                    scr,
                    lang_dat,
                    lang_base=base,
                )

            self.assertEqual(lang_dat.read_bytes(), base.read_bytes())
            self.assertEqual(result["language"]["fragment"]["status"], "incomplete")
            self.assertEqual(result["language"]["game_dat"]["mode"], "preserved-base")
            self.assertEqual(
                result["language"]["warnings"][0]["code"],
                "rscript-lang-fragment-incomplete",
            )

    def test_compile_wraps_complete_fragment_before_building_lang_dat(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "workflow.rson"
            source.write_text(json.dumps(PROJECT), encoding="utf-8")
            scr = root / "workflow.scr"
            fragment = root / "workflow.lang.txt"
            lang_dat = root / "DATA" / "Script" / "Lang.dat"
            chain = Toolchain()
            captured: dict[str, str] = {}

            def fake_compile(_source, scr_output, lang_output, **_kwargs):
                scr_output.parent.mkdir(parents=True, exist_ok=True)
                scr_output.write_bytes((8).to_bytes(4, "little") + b"compiled")
                lang_output.write_bytes("0=Готово\r\n1=Назад\r\n".encode("utf-16"))
                process = SimpleNamespace(
                    exit_code=0,
                    forced_after_outputs=False,
                    elapsed_seconds=0.01,
                    queue_seconds=0.0,
                    progress_updates=1,
                    last_progress_seconds=0.01,
                )
                return process, inspect_scr(scr_output), {"mode": "test"}

            def fake_convert(source_path, destination_path, **_kwargs):
                source_path = Path(source_path)
                destination_path = Path(destination_path)
                captured["text"] = source_path.read_text(encoding="cp1251")
                destination_path.write_bytes(b"verified-game-dat")
                return {"verified": True}

            with patch.object(chain, "_compile_rson_with_rscript", side_effect=fake_compile), patch.object(
                chain,
                "convert_dat",
                side_effect=fake_convert,
            ):
                result = chain.compile_rson(
                    source,
                    scr,
                    fragment,
                    lang_dat_output=lang_dat,
                )

            self.assertIn("Script ^{", captured["text"])
            self.assertIn("Workflow ~{", captured["text"])
            self.assertIn("0=Готово", captured["text"])
            self.assertEqual(fragment.read_bytes()[:2], b"\xff\xfe")
            self.assertEqual(lang_dat.read_bytes(), b"verified-game-dat")
            self.assertEqual(result["language"]["game_dat"]["mode"], "generated")
            self.assertEqual(
                result["language"]["warnings"][0]["code"],
                "rscript-lang-dat-nonruntime-path",
            )

    def test_compile_builds_blockpar_container_for_empty_game_lang_dat(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "workflow.rson"
            source.write_text(json.dumps(PROJECT), encoding="utf-8")
            scr = root / "workflow.scr"
            lang_dat = root / "Lang.dat"
            chain = Toolchain()
            captured: dict[str, str] = {}

            def fake_compile(_source, scr_output, lang_output, **_kwargs):
                scr_output.parent.mkdir(parents=True, exist_ok=True)
                scr_output.write_bytes((8).to_bytes(4, "little") + b"compiled")
                lang_output.write_bytes(b"\xff\xfe")
                process = SimpleNamespace(
                    exit_code=0,
                    forced_after_outputs=False,
                    elapsed_seconds=0.01,
                    queue_seconds=0.0,
                    progress_updates=1,
                    last_progress_seconds=0.01,
                )
                return process, inspect_scr(scr_output), {"mode": "test"}

            def fake_convert(source_path, destination_path, **_kwargs):
                source_path = Path(source_path)
                destination_path = Path(destination_path)
                captured["text"] = source_path.read_text(encoding="cp1251")
                destination_path.write_bytes(b"verified-empty-game-dat")
                return {"verified": True}

            with patch.object(chain, "_compile_rson_with_rscript", side_effect=fake_compile), patch.object(
                chain,
                "convert_dat",
                side_effect=fake_convert,
            ):
                result = chain.compile_rson(source, scr, lang_dat)

            self.assertIn("Script ^{", captured["text"])
            self.assertIn("Workflow ~{", captured["text"])
            self.assertNotEqual(lang_dat.read_bytes(), b"\xff\xfe")
            self.assertEqual(
                result["language"]["game_dat"]["mode"],
                "generated-empty",
            )

    def test_complete_fragment_must_cover_referenced_script_keys(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            data = deepcopy(PROJECT)
            data["Visual.Objects"][0]["Operations"][0]["Code"] = [
                'result = CT("Script.Workflow.9");'
            ]
            source = root / "workflow.rson"
            source.write_text(json.dumps(data), encoding="utf-8")
            scr = root / "workflow.scr"
            lang_dat = root / "Lang.dat"
            chain = Toolchain()

            def fake_compile(_source, scr_output, lang_output, **_kwargs):
                scr_output.parent.mkdir(parents=True, exist_ok=True)
                scr_output.write_bytes((8).to_bytes(4, "little") + b"compiled")
                lang_output.write_bytes("0=Готово\r\n".encode("utf-16"))
                process = SimpleNamespace(
                    exit_code=0,
                    forced_after_outputs=False,
                    elapsed_seconds=0.01,
                    queue_seconds=0.0,
                    progress_updates=1,
                    last_progress_seconds=0.01,
                )
                return process, inspect_scr(scr_output), {"mode": "test"}

            with patch.object(chain, "_compile_rson_with_rscript", side_effect=fake_compile):
                with self.assertRaisesRegex(ValueError, "не покрывает Script/Workflow"):
                    chain.compile_rson(source, scr, lang_dat)

            self.assertFalse(scr.exists())
            self.assertFalse(lang_dat.exists())


if __name__ == "__main__":
    unittest.main()
