from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from srhd_modkit.files import compare_trees, iter_files, stage_tree


class StageTests(unittest.TestCase):
    def test_stage_copies_every_format_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "source"
            source.mkdir()
            (source / "ModuleInfo.txt").write_text("Name=Example", encoding="utf-16")
            for extension, payload in (
                (".dat", b"\x00\x01dat"),
                (".gai", b"gai\0binary"),
                (".unknown", b"must survive"),
            ):
                path = source / "Data" / f"asset{extension}"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
            destination = root / "destination"
            result = stage_tree(source, destination)
            self.assertTrue(result["verified"])
            self.assertEqual(compare_trees(source, destination)["summary"]["changed"], 0)
            self.assertEqual(compare_trees(source, destination)["summary"]["added"], 0)
            self.assertEqual(compare_trees(source, destination)["summary"]["removed"], 0)

    def test_stage_refuses_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            destination.mkdir()
            with self.assertRaises(FileExistsError):
                stage_tree(source, destination)

    def test_internal_srhd_staging_directories_are_never_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "ModuleInfo.txt").write_text("Name=Example", encoding="utf-8")
            internal = root / ".srhd-dat-interrupted"
            internal.mkdir()
            (internal / "leaked.txt").write_text("temporary", encoding="utf-8")
            self.assertEqual([path.name for path in iter_files(root)], ["ModuleInfo.txt"])


if __name__ == "__main__":
    unittest.main()
