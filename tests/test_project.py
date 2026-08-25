from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from srhd_modkit.project import (
    ArtifactCache,
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
            verified_cache = build_project(root)
            self.assertEqual(verified_cache.cache_hits, 1)
            self.assertEqual(verified_cache.cache_misses, 0)

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

    def test_cache_history_is_bounded_and_workspaces_do_not_accumulate(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            _write_project(root)
            asset = root / "assets" / "Mod_ProjectFixture.bin"
            last = None
            for revision in range(7):
                asset.write_bytes(f"release-artifact-{revision}".encode("ascii"))
                last = build_project(root)

            self.assertIsNotNone(last)
            cache_entries = list((root / ".srhd-cache" / "artifacts").rglob("manifest.json"))
            self.assertLessEqual(len(cache_entries), 3)
            self.assertGreaterEqual(
                last.provenance["cache"]["maintenance"]["removed_entries"],
                1,
            )
            self.assertEqual(list(root.glob(".srhd-project-build-*")), [])
            self.assertEqual(
                list((root / ".srhd-cache" / "artifacts").rglob(".*.cache-*")),
                [],
            )
            release_root = root / ".srhd-build" / "release"
            self.assertEqual(
                sorted(path.name for path in release_root.iterdir()),
                ["OtherMods", "ProjectFixture.build.json"],
            )

    def test_failed_project_build_removes_temporary_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            _write_project(root)
            config = root / "srhd-modkit.toml"
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    'source = "assets/${script_name}.bin"',
                    'source = "assets/missing.bin"',
                ),
                encoding="utf-8",
            )

            with self.assertRaises(FileNotFoundError):
                build_project(root)
            self.assertEqual(list(root.glob(".srhd-project-build-*")), [])

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
            self.assertTrue(preview["operation_semantics"]["build_performed"])
            self.assertFalse(preview["operation_semantics"]["game_target_modified"])
            self.assertEqual(
                preview["operation_semantics"]["passive_preview_command"],
                "project plan",
            )
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

    def test_variant_overlay_changes_effective_compiler_input_and_cache_key(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            mod, _game = _write_project(root)
            source = mod / "SOURCE" / "variant.bin"
            source.write_bytes(b"release")
            overlay = root / "overlays" / "earth-test" / "SOURCE"
            overlay.mkdir(parents=True)
            (overlay / "variant.bin").write_bytes(b"earth")
            config = root / "srhd-modkit.toml"
            config.write_text(
                config.read_text(encoding="utf-8")
                + """

[[artifacts]]
id = "effective-variant-input"
kind = "copy"
source = "RuntimeMod/SOURCE/variant.bin"
output = "DATA/effective.bin"
""",
                encoding="utf-8",
            )

            release = build_project(root, variant="release")
            earth = build_project(root, variant="earth-test")
            self.assertEqual((release.output / "DATA" / "effective.bin").read_bytes(), b"release")
            self.assertEqual((earth.output / "DATA" / "effective.bin").read_bytes(), b"earth")
            release_key = next(
                item["cache_key"] for item in release.artifacts if item["id"] == "effective-variant-input"
            )
            earth_key = next(
                item["cache_key"] for item in earth.artifacts if item["id"] == "effective-variant-input"
            )
            self.assertNotEqual(release_key, earth_key)

    def test_project_and_variant_names_cannot_escape_output_roots(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            _write_project(root)
            config = root / "srhd-modkit.toml"
            original = config.read_text(encoding="utf-8")
            config.write_text(
                original.replace('name = "ProjectFixture"', 'name = "../escape"', 1),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ProjectConfigError, "безопасным именем|недопустимый"):
                load_project(root)

            config.write_text(
                original.replace("[variants.release]", "[variants.'../escape']", 1),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ProjectConfigError, "безопасным именем|недопустимый"):
                load_project(root)

    def test_copy_directory_cache_does_not_restore_old_baseline_files(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            mod, _game = _write_project(root)
            bundle = root / "assets" / "bundle"
            bundle.mkdir()
            (bundle / "copied.cmap").write_bytes(b"copied")
            config = root / "srhd-modkit.toml"
            config.write_text(
                config.read_text(encoding="utf-8")
                + """

[[artifacts]]
id = "bundle"
kind = "copy"
source = "assets/bundle"
output = "DATA"
""",
                encoding="utf-8",
            )

            first = build_project(root)
            self.assertEqual(first.cache_misses, 2)
            (mod / "DATA" / "base.cmap").write_bytes(b"new-baseline")
            second = build_project(root)
            bundle_result = next(item for item in second.artifacts if item["id"] == "bundle")
            self.assertEqual(bundle_result["cache"], "hit")
            self.assertEqual((second.output / "DATA" / "base.cmap").read_bytes(), b"new-baseline")
            self.assertEqual((second.output / "DATA" / "copied.cmap").read_bytes(), b"copied")

    def test_cache_rejects_non_sha_key(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            cache = ArtifactCache(Path(name))
            with self.assertRaises(ProjectConfigError):
                cache.probe("../escape")


if __name__ == "__main__":
    unittest.main()
