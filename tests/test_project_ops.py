from __future__ import annotations

import os
import hashlib
import tempfile
import time
import unittest
from pathlib import Path

from srhd_modkit.project import ProjectConfigError, build_project, load_project
from srhd_modkit.project_ops import clean_project, doctor_project, initialize_project, plan_project


def _copy_project(root: Path) -> Path:
    mod = root / "RuntimeMod"
    (mod / "DATA").mkdir(parents=True)
    (mod / "ModuleInfo.txt").write_text(
        "Name=ProjectOpsFixture\nSection=Test\nPriority=1\nLanguages=Rus\n",
        encoding="cp1251",
    )
    (mod / "DATA" / "base.cmap").write_bytes(b"base")
    assets = root / "assets"
    assets.mkdir()
    (assets / "worker.cmap").write_bytes(b"worker")
    (root / "srhd-modkit.toml").write_text(
        """schema = "srhd-modkit-project-v1"
name = "ProjectOpsFixture"
mod_root = "RuntimeMod"
prefix = "OtherMods/ProjectOpsFixture"
build_root = ".srhd-build"
cache_root = ".srhd-cache"

[variants.release]

[[artifacts]]
id = "worker"
kind = "copy"
source = "assets/worker.cmap"
output = "DATA/worker.cmap"
""",
        encoding="utf-8",
    )
    return mod


class ProjectOperationsTests(unittest.TestCase):
    def test_init_discovers_dat_source_without_guessing_unknown_files(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            mod = root / "Existing"
            (mod / "SOURCE" / "CFG").mkdir(parents=True)
            (mod / "CFG").mkdir()
            (mod / "ModuleInfo.txt").write_text(
                "Name=Existing\nSection=Test\nLanguages=Rus\n", encoding="cp1251"
            )
            (mod / "SOURCE" / "CFG" / "Main.txt").write_text(
                "Data ^{\n  Test=1\n}\n", encoding="utf-8"
            )
            (mod / "CFG" / "Main.dat").write_bytes(b"existing")
            (mod / "DATA").mkdir()
            (mod / "DATA" / "unknown.bin").write_bytes(b"opaque")

            result = initialize_project(mod)
            project = load_project(result["output"])
            self.assertEqual(len(project.artifacts), 1)
            self.assertEqual(project.artifacts[0]["kind"], "dat")
            self.assertEqual(project.artifacts[0]["output"], "CFG/Main.dat")
            self.assertNotIn("unknown.bin", Path(result["output"]).read_text(encoding="utf-8"))

    def test_init_excludes_all_ambiguous_sources_for_one_output(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            mod = root / "Ambiguous"
            (mod / "SOURCE" / "CFG").mkdir(parents=True)
            (mod / "SOURCES" / "Config").mkdir(parents=True)
            (mod / "ModuleInfo.txt").write_text(
                "Name=Ambiguous\nSection=Test\nLanguages=Rus\n", encoding="cp1251"
            )
            for path in (
                mod / "SOURCE" / "CFG" / "Main.txt",
                mod / "SOURCES" / "Config" / "Main.txt",
            ):
                path.write_text("Data ^{\n Value=1\n}\n", encoding="utf-8")

            result = initialize_project(mod)
            self.assertEqual(result["artifacts"], [])
            self.assertEqual(result["issues"][0]["code"], "project-init-ambiguous-dat-source")

    def test_init_detects_external_dotnet_runtime_and_blocks_unconfirmed_publish(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            mod = root / "NativeMod"
            (mod / "SOURCE" / "Native").mkdir(parents=True)
            (mod / "DATA").mkdir()
            (mod / "ModuleInfo.txt").write_text(
                "Name=NativeMod\nSection=Test\nLanguages=Rus\n",
                encoding="cp1251",
            )
            (mod / "SOURCE" / "Native" / "Worker.csproj").write_text(
                '<Project Sdk="Microsoft.NET.Sdk" />',
                encoding="utf-8",
            )
            (mod / "DATA" / "Worker.dll").write_bytes(b"MZcompiled")

            initialized = initialize_project(mod)
            self.assertEqual(initialized["summary"]["external_builds"], 1)
            self.assertEqual(initialized["external_builds"][0]["kind"], "dotnet")
            plan = plan_project(root)
            self.assertTrue(plan["blocked"])
            self.assertTrue(
                any(item["code"] == "project-external-build-unconfigured" for item in plan["issues"])
            )
            with self.assertRaisesRegex(ProjectConfigError, "не подтверждена"):
                build_project(root)

            config = Path(initialized["output"])
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    'mode = "unconfigured"',
                    'mode = "prebuilt"',
                ),
                encoding="utf-8",
            )
            built = build_project(root)
            self.assertTrue((built.output / "DATA" / "Worker.dll").is_file())
            self.assertEqual(
                built.provenance["external_builds"][0]["outputs"][0]["sha256"],
                hashlib.sha256(b"MZcompiled").hexdigest(),
            )

    def test_init_blocks_unconfirmed_runtime_binary_without_source_project(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            mod = root / "LauncherMod"
            (mod / "DATA").mkdir(parents=True)
            (mod / "SOURCE" / "Tools").mkdir(parents=True)
            (mod / "ModuleInfo.txt").write_text(
                "Name=LauncherMod\nSection=Test\nLanguages=Rus\n",
                encoding="cp1251",
            )
            (mod / "Launcher.exe").write_bytes(b"MZruntime")
            # A developer helper under SOURCE is not shipped and must not be
            # mistaken for a runtime launcher.
            (mod / "SOURCE" / "Tools" / "Generator.exe").write_bytes(b"MZtool")

            initialized = initialize_project(mod)

            self.assertEqual(initialized["summary"]["external_builds"], 1)
            external = initialized["external_builds"][0]
            self.assertEqual(external["kind"], "prebuilt-binary")
            self.assertEqual(external["outputs"], ["LauncherMod/Launcher.exe"])
            self.assertTrue(plan_project(root)["blocked"])

    def test_init_keeps_independent_solution_but_deduplicates_simple_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            mod = root / "NativeMod"
            source = mod / "SOURCE" / "Native"
            source.mkdir(parents=True)
            (mod / "DATA").mkdir()
            (mod / "ModuleInfo.txt").write_text(
                "Name=NativeMod\nSection=Test\nLanguages=Rus\n",
                encoding="cp1251",
            )
            worker = source / "Worker.csproj"
            worker.write_text('<Project Sdk="Microsoft.NET.Sdk" />', encoding="utf-8")
            (source / "Worker.sln").write_text(
                'Project("{A}") = "Worker", "Worker.csproj", "{B}"\nEndProject\n',
                encoding="utf-8",
            )
            (source / "Suite.sln").write_text(
                'Project("{A}") = "External", "External.vcxproj", "{B}"\nEndProject\n',
                encoding="utf-8",
            )
            (mod / "DATA" / "Worker.dll").write_bytes(b"MZworker")
            (mod / "DATA" / "Suite.dll").write_bytes(b"MZsuite")

            initialized = initialize_project(mod)
            projects = {
                Path(item["project"]).name: item
                for item in initialized["external_builds"]
            }
            self.assertEqual(set(projects), {"Worker.csproj", "Suite.sln"})
            self.assertEqual(projects["Worker.csproj"]["outputs"], ["NativeMod/DATA/Worker.dll"])
            self.assertEqual(projects["Suite.sln"]["outputs"], ["NativeMod/DATA/Suite.dll"])

    def test_plan_reports_cache_reason_and_leaves_no_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            _copy_project(root)
            before = plan_project(root)
            self.assertEqual(before["artifacts"][0]["cache"], "miss")
            self.assertEqual(before["artifacts"][0]["reasons"], ["no-previous-cache-entry"])
            self.assertIn("DATA/worker.cmap", before["files"]["game"])
            self.assertTrue(before["destinations"]["build"].endswith("OtherMods\\ProjectOpsFixture"))
            self.assertEqual(list(root.glob(".srhd-project-plan-*")), [])

            build_project(root)
            after = plan_project(root)
            self.assertEqual(after["artifacts"][0]["cache"], "hit")
            self.assertFalse(after["artifacts"][0]["rebuild"])
            self.assertEqual(list(root.glob(".srhd-project-plan-*")), [])

    def test_plan_blocks_copy_directory_overlapping_another_output(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            _copy_project(root)
            assets = root / "assets" / "bundle"
            assets.mkdir()
            (assets / "same.cmap").write_bytes(b"bundle")
            config = root / "srhd-modkit.toml"
            config.write_text(
                config.read_text(encoding="utf-8")
                + """

[[artifacts]]
id = "bundle"
kind = "copy"
source = "assets/bundle"
output = "DATA"

[[artifacts]]
id = "same-file"
kind = "copy"
source = "assets/worker.cmap"
output = "DATA/same.cmap"
""",
                encoding="utf-8",
            )
            plan = plan_project(root)
            self.assertTrue(plan["blocked"])
            self.assertTrue(any(item["code"] == "project-plan-output-overlap" for item in plan["issues"]))
            with self.assertRaisesRegex(ProjectConfigError, "пересекающиеся выходы"):
                build_project(root)

    def test_doctor_and_clean_are_read_only_until_apply(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            _copy_project(root)
            leftover = root / ".srhd-project-build-abandoned"
            leftover.mkdir()
            (leftover / "temp.bin").write_bytes(b"temporary")
            old = time.time() - 2 * 24 * 60 * 60
            os.utime(leftover, (old, old))

            doctor = doctor_project(root)
            self.assertTrue(doctor["healthy"])
            self.assertTrue(any(item["stale"] for item in doctor["workspaces"]))
            preview = clean_project(root)
            self.assertEqual(preview["summary"]["candidates"], 1)
            self.assertTrue(leftover.is_dir())
            applied = clean_project(root, apply=True)
            self.assertEqual(applied["summary"]["removed"], 1)
            self.assertFalse(leftover.exists())


if __name__ == "__main__":
    unittest.main()
