from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from srhd_modkit.release import (
    ReleaseBlockedError,
    build_release,
    verify_release_archive,
)


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


if __name__ == "__main__":
    unittest.main()
