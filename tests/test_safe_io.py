from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import srhd_modkit.safe_io as safe_io
from srhd_modkit.safe_io import publish_files_transactionally


class SafeIoTests(unittest.TestCase):
    def test_multi_file_publish_rolls_every_destination_back(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            first_source = root / "first.new"
            second_source = root / "second.new"
            first_target = root / "first.dat"
            second_target = root / "second.dat"
            first_source.write_bytes(b"new-first")
            second_source.write_bytes(b"new-second")
            first_target.write_bytes(b"old-first")
            second_target.write_bytes(b"old-second")
            original_replace = safe_io.os.replace

            def fail_second_publish(source, destination):
                source_path = Path(source)
                destination_path = Path(destination)
                if destination_path == second_target and ".srhd-publish-" in source_path.name:
                    raise PermissionError("simulated Windows lock")
                return original_replace(source, destination)

            with patch("srhd_modkit.safe_io.os.replace", side_effect=fail_second_publish):
                with self.assertRaises(PermissionError):
                    publish_files_transactionally(
                        ((first_source, first_target), (second_source, second_target))
                    )

            self.assertEqual(first_target.read_bytes(), b"old-first")
            self.assertEqual(second_target.read_bytes(), b"old-second")
            self.assertEqual(list(root.glob(".srhd-*")), [])

    def test_failed_rollback_preserves_the_only_backup(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            first_source = root / "first.new"
            second_source = root / "second.new"
            first_target = root / "first.dat"
            second_target = root / "second.dat"
            first_source.write_bytes(b"new-first")
            second_source.write_bytes(b"new-second")
            first_target.write_bytes(b"old-first")
            second_target.write_bytes(b"old-second")
            original_replace = safe_io.os.replace

            def fail_publish_and_first_rollback(source, destination):
                source_path = Path(source)
                destination_path = Path(destination)
                if destination_path == second_target and ".srhd-publish-" in source_path.name:
                    raise PermissionError("simulated publish lock")
                if destination_path == first_target and ".srhd-backup-" in source_path.name:
                    raise PermissionError("simulated rollback lock")
                return original_replace(source, destination)

            with patch(
                "srhd_modkit.safe_io.os.replace",
                side_effect=fail_publish_and_first_rollback,
            ):
                with self.assertRaisesRegex(RuntimeError, "ручного восстановления"):
                    publish_files_transactionally(
                        ((first_source, first_target), (second_source, second_target))
                    )

            backups = list(root.glob(".srhd-backup-*-first.dat"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), b"old-first")
            self.assertFalse(first_target.exists())
            self.assertEqual(second_target.read_bytes(), b"old-second")

    def test_failed_removal_of_published_file_never_deletes_old_backup(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            first_source = root / "first.new"
            second_source = root / "second.new"
            first_target = root / "first.dat"
            second_target = root / "second.dat"
            first_source.write_bytes(b"new-first")
            second_source.write_bytes(b"new-second")
            first_target.write_bytes(b"old-first")
            second_target.write_bytes(b"old-second")
            original_replace = safe_io.os.replace
            original_unlink = Path.unlink

            def fail_second_publish(source, destination):
                if Path(destination) == second_target and ".srhd-publish-" in Path(source).name:
                    raise PermissionError("simulated publish lock")
                return original_replace(source, destination)

            def fail_new_file_removal(path, *args, **kwargs):
                if Path(path) == first_target:
                    raise PermissionError("simulated removal lock")
                return original_unlink(path, *args, **kwargs)

            with patch("srhd_modkit.safe_io.os.replace", side_effect=fail_second_publish), patch(
                "pathlib.Path.unlink", side_effect=fail_new_file_removal, autospec=True
            ):
                with self.assertRaisesRegex(RuntimeError, "ручного восстановления"):
                    publish_files_transactionally(
                        ((first_source, first_target), (second_source, second_target))
                    )

            self.assertEqual(first_target.read_bytes(), b"new-first")
            backups = list(root.glob(".srhd-backup-*-first.dat"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), b"old-first")
            self.assertEqual(second_target.read_bytes(), b"old-second")


if __name__ == "__main__":
    unittest.main()
