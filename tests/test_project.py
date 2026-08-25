from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from srhd_modkit.project import (
    ProjectConfigError,
    build_project,
    deploy_project,
    load_project,
    publish_project,
    resolve_project_target,
)


def _write_project(root: Path) -> tuple[Path, Path]:
    mod = root / "RuntimeMod"
    (mod / "DATA").mkdir(parents=True)
    (mod / "ModuleInfo.txt").write_text(
        "Name=ProjectFixture\nSection=Test\nPriority=1\nLanguages=Rus\n",
        encoding="cp1251",
    )
    (mod / "DATA" / "base.cmap").write_bytes(b"base")
    (mod / "SOURCE").mkdir()
    (mod / "SOURCE" / "notes.md").write_text("source", encoding="utf-8")

    assets = root / "assets"
    assets.mkdir()
    (assets / "Mod_ProjectFixture.bin").write_bytes(b"release-artifact")
    (assets / "Mod_ProjectFixtureEarthTest.bin").write_bytes(b"test-artifact")
    test_common = root / "TEST" / "common"
    test_common.mkdir(parents=True)
    (test_common / "diagnostic.cmap").write_bytes(b"diagnostic")

    overlay = root / "overlays" / "earth-test"
    (overlay / "DATA").mkdir(parents=True)
    (overlay / "DATA" / "variant.cmap").write_bytes(b"earth")

    game_mods = root / "Game" / "Mods"
    shared = """\
schema = "srhd-modkit-project-v1"
name = "ProjectFixture"
mod_root = "RuntimeMod"
prefix = "OtherMods/ProjectFixture"
default_variant = "release"
build_root = ".srhd-build"
cache_root = ".srhd-cache"
default_target = "game"

[variants.release]
script_name = "Mod_ProjectFixture"

[variants.earth-test]
inherits = "release"
script_name = "Mod_ProjectFixtureEarthTest"
overlays = ["overlays/earth-test"]
include = ["TEST/common/**"]

[[artifacts]]
id = "worker"
kind = "copy"
source = "assets/${script_name}.bin"
output = "DATA/Script/${script_name}.bin"

[targets.game]
prefix = "OtherMods/ProjectFixture"

[publish]
output = "Releases/${name}-${variant}.zip"
targets = ["game"]
"""
    (root / "srhd-modkit.toml").write_text(shared, encoding="utf-8")
    (root / "srhd-modkit.local.toml").write_text(
        f'[targets.game]\nroot = "{game_mods.as_posix()}"\n',
        encoding="utf-8",
    )
    return mod, game_mods


class ProjectTests(unittest.TestCase):
    def test_variants_build_source_free_and_cache_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            _write_project(root)

            first = build_project(root)
            self.assertEqual(first.variant, "release")
            self.assertEqual(first.cache_hits, 0)
            self.assertEqual(first.cache_misses, 1)
            self.assertFalse((first.output / "SOURCE").exists())
            self.assertEqual(
                (first.output / "DATA" / "Script" / "Mod_ProjectFixture.bin").read_bytes(),
                b"release-artifact",
            )
            self.assertTrue(first.provenance_path.is_file())

            second = build_project(root)
            self.assertEqual(second.cache_hits, 1)
            self.assertEqual(second.cache_misses, 0)
            self.assertTrue(second.deploy.verified)

            cached_file = next((root / ".srhd-cache" / "artifacts").rglob("files/DATA/Script/*.bin"))
            cached_file.write_bytes(b"corrupted-cache")
            recovered = build_project(root)
            self.assertEqual(recovered.cache_hits, 0)
            self.assertEqual(recovered.cache_misses, 1)
            self.assertEqual(
                (recovered.output / "DATA" / "Script" / "Mod_ProjectFixture.bin").read_bytes(),
                b"release-artifact",
            )

            asset = root / "assets" / "Mod_ProjectFixture.bin"
            asset.write_bytes(b"release-artifact-v2")
            third = build_project(root)
            self.assertEqual(third.cache_hits, 0)
            self.assertEqual(third.cache_misses, 1)
            self.assertEqual(
                (third.output / "DATA" / "Script" / "Mod_ProjectFixture.bin").read_bytes(),
                b"release-artifact-v2",
            )

            earth = build_project(root, variant="earth-test")
            self.assertEqual(earth.variant, "earth-test")
            self.assertEqual((earth.output / "DATA" / "variant.cmap").read_bytes(), b"earth")
            self.assertEqual((earth.output / "TEST" / "common" / "diagnostic.cmap").read_bytes(), b"diagnostic")
            self.assertEqual(
                (earth.output / "DATA" / "Script" / "Mod_ProjectFixtureEarthTest.bin").read_bytes(),
                b"test-artifact",
            )
            self.assertFalse((earth.output / "DATA" / "Script" / "Mod_ProjectFixture.bin").exists())

    def test_deploy_dry_run_is_read_only_and_publish_reuses_one_build(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            _mod, game_mods = _write_project(root)
            destination = game_mods / "OtherMods" / "ProjectFixture"
            destination.mkdir(parents=True)
            (destination / "ModuleInfo.txt").write_bytes(b"old")
            (destination / "stale.bin").write_bytes(b"stale")

            preview = deploy_project(root, dry_run=True)
            self.assertTrue(preview["dry_run"])
            self.assertIsNone(preview["deploy"])
            self.assertIn("stale.bin", preview["plan"]["removed"])
            self.assertTrue((destination / "stale.bin").is_file())

            published = publish_project(root)
            archive = Path(published["release"]["output"])
            self.assertTrue(archive.is_file())
            self.assertTrue(Path(published["release"]["manifest"]).is_file())
            self.assertTrue(Path(published["release"]["audit"]).is_file())
            self.assertTrue(Path(published["report"]).is_file())
            self.assertFalse((destination / "stale.bin").exists())
            self.assertTrue((destination / "DATA" / "base.cmap").is_file())
            with zipfile.ZipFile(archive) as value:
                self.assertIn(
                    "OtherMods/ProjectFixture/DATA/base.cmap",
                    value.namelist(),
                )

    def test_local_config_supplies_machine_path_without_changing_shared_config(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            _write_project(root)
            project = load_project(root)
            self.assertIsNotNone(project.local_path)
            self.assertNotIn("root", project.path.read_text(encoding="utf-8").split("[targets.game]", 1)[1].split("[publish]", 1)[0])
            self.assertEqual(project.targets["game"]["root"], (root / "Game" / "Mods").as_posix())

    def test_project_validation_rejects_variant_cycle_and_output_collision(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            _write_project(root)
            config = root / "srhd-modkit.toml"
            original = config.read_text(encoding="utf-8")
            config.write_text(
                original
                + """\

[[artifacts]]
id = "collision"
kind = "copy"
source = "assets/${script_name}.bin"
output = "DATA/Script/${script_name}.bin"
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ProjectConfigError, "один путь"):
                load_project(root)

            value = original.replace(
                "[variants.release]\nscript_name",
                '[variants.release]\ninherits = "earth-test"\nscript_name',
            )
            config.write_text(value, encoding="utf-8")
            with self.assertRaisesRegex(ProjectConfigError, "Цикл inherits"):
                load_project(root)

    def test_project_target_cannot_overwrite_source_mod_root(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            _write_project(root)
            (root / "srhd-modkit.local.toml").write_text(
                f'[targets.game]\nroot = "{root.as_posix()}"\nprefix = "RuntimeMod"\n',
                encoding="utf-8",
            )
            project = load_project(root)
            with self.assertRaisesRegex(ProjectConfigError, "пересекается"):
                resolve_project_target(project, "game")


if __name__ == "__main__":
    unittest.main()
