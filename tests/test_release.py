from __future__ import annotations

import tempfile
import unittest
import zipfile
import os
from pathlib import Path
from unittest.mock import patch

import srhd_modkit.release as release_module
from srhd_modkit.audit import AuditCheck, AuditIssue, AuditProfile, AuditReport
from srhd_modkit.release import (
    ReleaseBlockedError,
    build_release,
    cleanup_deployments,
    deploy_mod,
    inspect_deployments,
    plan_deploy,
    rollback_deployment,
    verify_release_archive,
)
from srhd_modkit.schemas import validate_schema_document


def _mod(root: Path) -> bytes:
    root.mkdir(parents=True)
    (root / "ModuleInfo.txt").write_text(
        "Name=ReleaseFixture\nSection=Test\nPriority=1\nLanguages=Rus\n",
        encoding="cp1251",
    )
    (root / "DATA").mkdir()
    payload = bytes(range(256))
    (root / "DATA" / "opaque.cmap").write_bytes(payload)
    return payload


class ReleaseTests(unittest.TestCase):
    def test_release_prefix_is_used_as_exact_install_subpath(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            root = base / "ReleaseFixture"
            _mod(root)
            report = AuditReport(str(root), AuditProfile.RELEASE, ())
            with patch("srhd_modkit.release.audit_mod", return_value=report) as audit:
                result = build_release(
                    root,
                    base / "nested.zip",
                    prefix="OtherMods/ReleaseFixture",
                )
            self.assertEqual(
                audit.call_args.kwargs["install_subpath"],
                "OtherMods/ReleaseFixture",
            )
            with zipfile.ZipFile(result.output) as archive:
                self.assertIn(
                    "OtherMods/ReleaseFixture/ModuleInfo.txt",
                    archive.namelist(),
                )

    def test_release_is_deterministic_verified_and_keeps_metadata_outside(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            root = base / "ReleaseFixture"
            payload = _mod(root)
            first = build_release(root, base / "one.zip")
            second = build_release(root, base / "two.zip")

            self.assertEqual(first.sha256, second.sha256)
            self.assertTrue(first.verified)
            self.assertTrue(first.manifest_path.is_file())
            self.assertTrue(first.audit_path.is_file())
            self.assertTrue(validate_schema_document(first.as_dict(include_audit=False))["valid"])
            self.assertTrue(validate_schema_document(first.manifest_path)["valid"])
            with zipfile.ZipFile(first.output) as archive:
                names = archive.namelist()
                self.assertNotIn("ReleaseFixture.audit.json", names)
                self.assertNotIn("ReleaseFixture.manifest.json", names)
                self.assertEqual(archive.read("ReleaseFixture/DATA/opaque.cmap"), payload)

    def test_release_is_blocked_before_writing_archive(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            root = base / "ReleaseFixture"
            _mod(root)
            (root / "DATA" / "bad.png").write_bytes(b"broken")
            output = base / "blocked.zip"
            with self.assertRaises(ReleaseBlockedError):
                build_release(root, output)
            self.assertFalse(output.exists())

    def test_release_can_strip_sources_without_changing_default(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            root = base / "ReleaseFixture"
            _mod(root)
            source = root / "Source"
            source.mkdir()
            (source / "project.source.txt").write_text("source", encoding="utf-8")
            (root / "LOOSE.RSM").write_text("source", encoding="utf-8")
            (root / "srhd-modkit.toml").write_text("schema='test'", encoding="utf-8")
            (root / "srhd-modkit.local.toml").write_text("secret='local'", encoding="utf-8")

            normal = build_release(root, base / "normal.zip")
            stripped = build_release(root, base / "stripped.zip", strip_sources=True)

            with zipfile.ZipFile(normal.output) as archive:
                self.assertIn("ReleaseFixture/Source/project.source.txt", archive.namelist())
                self.assertIn("ReleaseFixture/srhd-modkit.toml", archive.namelist())
            with zipfile.ZipFile(stripped.output) as archive:
                self.assertNotIn("ReleaseFixture/Source/project.source.txt", archive.namelist())
                self.assertNotIn("ReleaseFixture/LOOSE.RSM", archive.namelist())
                self.assertNotIn("ReleaseFixture/srhd-modkit.toml", archive.namelist())
                self.assertNotIn("ReleaseFixture/srhd-modkit.local.toml", archive.namelist())
                self.assertIn("ReleaseFixture/DATA/opaque.cmap", archive.namelist())
            self.assertTrue(stripped.strip_sources)

    def test_deploy_replaces_tree_and_removes_sources_and_stale_files(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            root = base / "ReleaseFixture"
            _mod(root)
            source = root / "SOURCE"
            source.mkdir()
            (source / "project.source.txt").write_text("source", encoding="utf-8")
            mods_root = base / "Game" / "Mods"
            destination = mods_root / "OtherMods" / "ReleaseFixture"
            destination.mkdir(parents=True)
            (destination / "ModuleInfo.txt").write_text("old", encoding="cp1251")
            (destination / "stale.bin").write_bytes(b"stale")
            modcfg = mods_root / "ModCFG.txt"
            modcfg.write_bytes(b"unchanged")

            result = deploy_mod(
                root,
                mods_root,
                prefix="OtherMods/ReleaseFixture",
                overwrite=True,
            )

            self.assertTrue(result.verified)
            self.assertTrue(result.replaced_existing)
            self.assertEqual(result.stale_files_removed, 1)
            self.assertFalse((destination / "stale.bin").exists())
            self.assertFalse((destination / "SOURCE").exists())
            self.assertEqual((destination / "DATA" / "opaque.cmap").read_bytes(), bytes(range(256)))
            self.assertEqual(modcfg.read_bytes(), b"unchanged")
            self.assertFalse(result.modcfg_modified)

    def test_deploy_refuses_existing_destination_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            root = base / "ReleaseFixture"
            _mod(root)
            mods_root = base / "Mods"
            (mods_root / "ReleaseFixture").mkdir(parents=True)
            with self.assertRaises(FileExistsError):
                deploy_mod(root, mods_root)

    def test_deploy_restores_previous_tree_when_post_publish_verification_fails(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            root = base / "ReleaseFixture"
            _mod(root)
            mods_root = base / "Mods"
            destination = mods_root / "ReleaseFixture"
            destination.mkdir(parents=True)
            (destination / "ModuleInfo.txt").write_text("old", encoding="cp1251")
            old_payload = destination / "old-only.bin"
            old_payload.write_bytes(b"old")
            original_build_manifest = release_module.build_manifest
            destination_reads = 0

            def corrupt_published_manifest(path, *args, **kwargs):
                nonlocal destination_reads
                manifest = original_build_manifest(path, *args, **kwargs)
                if Path(path).resolve() == destination.resolve():
                    destination_reads += 1
                    if destination_reads == 3:
                        manifest = {**manifest, "files": []}
                return manifest

            with patch("srhd_modkit.release.build_manifest", side_effect=corrupt_published_manifest):
                with self.assertRaises(OSError):
                    deploy_mod(root, mods_root, overwrite=True)

            self.assertEqual(old_payload.read_bytes(), b"old")
            self.assertFalse((destination / "DATA" / "opaque.cmap").exists())
            self.assertEqual(
                list(destination.parent.glob(".ReleaseFixture.srhd-deploy-*")),
                [],
            )

    def test_deploy_preserves_backup_when_windows_blocks_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            root = base / "ReleaseFixture"
            _mod(root)
            mods_root = base / "Mods"
            destination = mods_root / "ReleaseFixture"
            destination.mkdir(parents=True)
            (destination / "ModuleInfo.txt").write_text("old", encoding="cp1251")
            (destination / "old-only.bin").write_bytes(b"old")
            original_build_manifest = release_module.build_manifest
            original_replace = release_module.os.replace
            destination_reads = 0

            def corrupt_published_manifest(path, *args, **kwargs):
                nonlocal destination_reads
                manifest = original_build_manifest(path, *args, **kwargs)
                if Path(path).resolve() == destination.resolve():
                    destination_reads += 1
                    if destination_reads == 3:
                        manifest = {**manifest, "files": []}
                return manifest

            def block_backup_restore(source, target):
                if Path(source).name == "previous" and Path(target).resolve() == destination.resolve():
                    raise PermissionError("simulated Windows lock")
                return original_replace(source, target)

            with (
                patch("srhd_modkit.release.build_manifest", side_effect=corrupt_published_manifest),
                patch("srhd_modkit.release.os.replace", side_effect=block_backup_restore),
            ):
                with self.assertRaisesRegex(RuntimeError, "резервная копия сохранена"):
                    deploy_mod(root, mods_root, overwrite=True)

            transactions = list(destination.parent.glob(".ReleaseFixture.srhd-deploy-*"))
            self.assertEqual(len(transactions), 1)
            self.assertEqual((transactions[0] / "previous" / "old-only.bin").read_bytes(), b"old")

    def test_deploy_plan_is_read_only_and_lists_exact_changes(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            root = base / "ReleaseFixture"
            _mod(root)
            source = root / "SOURCE"
            source.mkdir()
            (source / "project.rson").write_bytes(b"source")
            mods_root = base / "Mods"
            destination = mods_root / "ReleaseFixture"
            destination.mkdir(parents=True)
            (destination / "ModuleInfo.txt").write_text("old", encoding="cp1251")
            (destination / "stale.bin").write_bytes(b"stale")

            report = AuditReport(str(root), AuditProfile.RELEASE, ())
            with patch("srhd_modkit.release.audit_mod", return_value=report):
                plan = plan_deploy(root, mods_root)

            self.assertFalse(plan.blocked)
            self.assertIn("DATA/opaque.cmap", plan.added)
            self.assertIn("ModuleInfo.txt", plan.changed)
            self.assertIn("stale.bin", plan.removed)
            self.assertIn("SOURCE/project.rson", plan.excluded)
            self.assertTrue((destination / "stale.bin").is_file())

    def test_doctor_can_rollback_and_cleanup_preserved_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            target = base / "Mods" / "Example"
            target.mkdir(parents=True)
            (target / "new.bin").write_bytes(b"new")
            transaction_id = "a" * 32
            transaction = target.parent / f".Example.srhd-deploy-{transaction_id}"
            backup = transaction / "previous"
            backup.mkdir(parents=True)
            (backup / "old.bin").write_bytes(b"old")
            metadata = {
                "schema": "srhd-modkit-deploy-transaction-v1",
                "id": transaction_id,
                "state": "rollback-failed",
                "destination": str(target),
                "backup": str(backup),
                "staged": str(transaction / "new"),
            }
            (transaction / "transaction.json").write_text(
                __import__("json").dumps(metadata), encoding="utf-8"
            )

            inspected = inspect_deployments(base / "Mods")
            self.assertEqual(inspected["summary"]["recoverable"], 1)
            preview = cleanup_deployments(base / "Mods")
            self.assertEqual(preview["summary"]["refused"], 1)
            self.assertTrue(transaction.is_dir())

            rollback = rollback_deployment(target)
            self.assertTrue(rollback["restored"])
            self.assertEqual((target / "old.bin").read_bytes(), b"old")
            self.assertTrue(Path(rollback["displaced_current"]).is_dir())

            cleanup = cleanup_deployments(base / "Mods", apply=True)
            self.assertEqual(cleanup["summary"]["removed"], 1)
            self.assertFalse(transaction.exists())

    def test_archive_verifier_rejects_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            archive_path = Path(name) / "unsafe.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../escape.txt", b"escape")
            manifest = {
                "files": [
                    {
                        "path": "../escape.txt",
                        "size": 6,
                        "sha256": "b314708a3028f6caea57edc003e37ef3e7c560b6872f50ec651e80e84970b72b",
                    }
                ]
            }
            with self.assertRaises(ValueError):
                verify_release_archive(archive_path, manifest, prefix="")

    def test_archive_verifier_checks_unsafe_directory_entries(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            archive_path = Path(name) / "unsafe-directory.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../outside/", b"")
            with self.assertRaisesRegex(ValueError, "Небезопасный путь ZIP"):
                verify_release_archive(archive_path, {"files": []}, prefix="")

    def test_archive_verifier_rejects_duplicate_manifest_paths(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            archive_path = Path(name) / "duplicate-manifest.zip"
            payload = b"data"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("DATA/value.bin", payload)
            row = {
                "path": "DATA/value.bin",
                "size": len(payload),
                "sha256": __import__("hashlib").sha256(payload).hexdigest(),
            }
            with self.assertRaisesRegex(ValueError, "дубль"):
                verify_release_archive(
                    archive_path,
                    {"files": [row, dict(row)]},
                    prefix="",
                )

    def test_cleanup_refuses_transaction_owned_by_live_deploy_process(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "Mods"
            target = root / "Example"
            target.parent.mkdir(parents=True)
            transaction_id = "b" * 32
            transaction = root / f".Example.srhd-deploy-{transaction_id}"
            transaction.mkdir()
            (transaction / "transaction.json").write_text(
                __import__("json").dumps(
                    {
                        "schema": "srhd-modkit-deploy-transaction-v1",
                        "id": transaction_id,
                        "state": "preparing",
                        "pid": os.getpid(),
                        "destination": str(target),
                    }
                ),
                encoding="utf-8",
            )
            result = cleanup_deployments(root, apply=True, force=True)
            self.assertEqual(result["summary"]["removed"], 0)
            self.assertEqual(result["summary"]["refused"], 1)
            self.assertTrue(transaction.is_dir())

    def test_cleanup_refuses_active_transaction_when_pid_liveness_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "Mods"
            target = root / "Example"
            target.parent.mkdir(parents=True)
            transaction_id = "c" * 32
            transaction = root / f".Example.srhd-deploy-{transaction_id}"
            transaction.mkdir()
            (transaction / "transaction.json").write_text(
                __import__("json").dumps(
                    {
                        "schema": "srhd-modkit-deploy-transaction-v1",
                        "id": transaction_id,
                        "state": "staged",
                        "pid": 987654,
                        "destination": str(target),
                    }
                ),
                encoding="utf-8",
            )
            with patch("srhd_modkit.release._pid_state", return_value="unknown"):
                result = cleanup_deployments(root, apply=True, force=True)
            self.assertEqual(result["summary"]["removed"], 0)
            self.assertEqual(result["summary"]["refused"], 1)
            self.assertIn("Нельзя доказать", result["refused"][0]["reason"])
            self.assertTrue(transaction.is_dir())

    def test_rollback_ignores_forged_backup_path_from_transaction_json(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "Mods"
            target = root / "Example"
            target.mkdir(parents=True)
            (target / "current.bin").write_bytes(b"current")
            external = Path(name) / "outside"
            external.mkdir()
            (external / "victim.bin").write_bytes(b"outside")
            transaction_id = "e" * 32
            transaction = root / f".Example.srhd-deploy-{transaction_id}"
            backup = transaction / "previous"
            backup.mkdir(parents=True)
            (backup / "safe.bin").write_bytes(b"safe")
            (transaction / "transaction.json").write_text(
                __import__("json").dumps(
                    {
                        "schema": "srhd-modkit-deploy-transaction-v1",
                        "id": transaction_id,
                        "state": "rollback-failed",
                        "destination": str(target),
                        "backup": str(external),
                        "staged": str(external),
                    }
                ),
                encoding="utf-8",
            )

            result = rollback_deployment(target)
            self.assertTrue(result["restored"])
            self.assertEqual((target / "safe.bin").read_bytes(), b"safe")
            self.assertEqual((external / "victim.bin").read_bytes(), b"outside")

    def test_deploy_lock_symlink_is_rejected_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            mod = base / "ReleaseFixture"
            _mod(mod)
            mods = base / "Mods"
            mods.mkdir()
            victim = base / "victim.txt"
            victim.write_text("unchanged", encoding="utf-8")
            lock = mods / ".ReleaseFixture.srhd-deploy.lock"
            try:
                os.symlink(victim, lock)
            except OSError as exc:
                self.skipTest(f"Символические ссылки недоступны: {exc}")
            with self.assertRaisesRegex(ValueError, "lock|ссылк"):
                deploy_mod(mod, mods)
            self.assertEqual(victim.read_text(encoding="utf-8"), "unchanged")

    def test_stale_deploy_lock_requires_force_and_can_be_cleaned_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "Mods"
            root.mkdir()
            target = root / "Example"
            lock = root / ".Example.srhd-deploy.lock"
            lock.write_text(
                __import__("json").dumps(
                    {
                        "schema": "srhd-modkit-deploy-lock-v1",
                        "pid": 987654,
                        "destination": str(target),
                    }
                ),
                encoding="utf-8",
            )
            with patch("srhd_modkit.release._pid_state", return_value="dead"):
                preview = cleanup_deployments(root)
                self.assertEqual(preview["summary"]["refused"], 1)
                self.assertTrue(lock.is_file())
                cleaned = cleanup_deployments(root, apply=True, force=True)
            self.assertEqual(cleaned["summary"]["removed"], 1)
            self.assertFalse(lock.exists())

    def test_release_reaudits_exact_staged_distribution(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            mod = base / "ReleaseFixture"
            _mod(mod)
            clean = AuditReport(str(mod), AuditProfile.RELEASE, ())
            issue = AuditIssue("error", "staged-only-error", "Ошибка точного состава")
            blocked = AuditReport(
                str(mod),
                AuditProfile.RELEASE,
                (AuditCheck("staged", "issues", (issue,)),),
            )
            with patch(
                "srhd_modkit.release.audit_mod",
                side_effect=(clean, blocked),
            ):
                with self.assertRaises(ReleaseBlockedError) as caught:
                    build_release(mod, base / "blocked.zip")
            self.assertEqual(caught.exception.report.issues[0].code, "staged-only-error")
            self.assertFalse((base / "blocked.zip").exists())

    def test_require_complete_is_opt_in_for_passthrough_formats(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            mod = base / "ReleaseFixture"
            _mod(mod)
            normal = build_release(mod, base / "normal.zip")
            self.assertFalse(normal.report.coverage_complete)
            with self.assertRaisesRegex(ReleaseBlockedError, "audit-coverage-incomplete"):
                build_release(
                    mod,
                    base / "strict.zip",
                    require_complete=True,
                )


if __name__ == "__main__":
    unittest.main()
