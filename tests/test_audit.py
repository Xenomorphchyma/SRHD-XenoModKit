from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from srhd_modkit.audit import AuditProfile, audit_mod
from srhd_modkit.image_codec import RgbaImage, encode_gi
from srhd_modkit.toolchain import Toolchain


def _mod(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "ModuleInfo.txt").write_text(
        "Name=AuditFixture\nSection=Test\nPriority=1\nLanguages=Rus\n",
        encoding="cp1251",
    )
    (root / "DATA").mkdir()


class AuditTests(unittest.TestCase):
    def test_release_deeply_checks_gi_payload(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "AuditFixture"
            _mod(root)
            path = root / "DATA" / "image.gi"
            payload = bytearray(encode_gi(RgbaImage(2, 1, bytes((1, 2, 3, 4)) * 2), "0_32"))
            payload.pop()
            path.write_bytes(payload)

            report = audit_mod(root, profile="release")
            check = next(item for item in report.checks if item.name == "resource-integrity")
            self.assertEqual(check.status, "issues")
            self.assertTrue(any(item.code == "resource-invalid" for item in check.issues))

    def test_unknown_format_is_passthrough_but_coverage_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "AuditFixture"
            _mod(root)
            payload = b"unknown-binary\x00\xff"
            (root / "DATA" / "object.cmap").write_bytes(payload)

            report = audit_mod(root)
            check = next(item for item in report.checks if item.name == "unknown-formats")
            self.assertEqual(check.status, "unsupported")
            self.assertFalse(report.coverage_complete)
            self.assertEqual((root / "DATA" / "object.cmap").read_bytes(), payload)

    def test_release_artifact_can_be_explicitly_suppressed(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "AuditFixture"
            _mod(root)
            backup = root / "ModuleInfo.txt.bak_20260716"
            backup.write_bytes(b"backup")

            blocked = audit_mod(root, profile=AuditProfile.RELEASE)
            issue = next(item for item in blocked.issues if item.code == "release-artifact")
            self.assertFalse(issue.suppressed)
            self.assertIn(issue, blocked.blocking_issues())

            allowed = audit_mod(
                root,
                profile="release",
                allow=("release-artifact:ModuleInfo.txt.bak_*",),
            )
            issue = next(item for item in allowed.issues if item.code == "release-artifact")
            self.assertTrue(issue.suppressed)
            self.assertNotIn(issue, allowed.blocking_issues())
            self.assertEqual(allowed.as_dict()["schema"], "srhd-modkit-audit-v1")

    def test_invalid_standard_signature_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "AuditFixture"
            _mod(root)
            (root / "DATA" / "broken.png").write_bytes(b"not-a-png")
            report = audit_mod(root)
            self.assertTrue(any(item.code == "invalid-signature" for item in report.issues))

    def test_release_detects_source_binary_dat_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "AuditFixture"
            _mod(root)
            source = root / "SOURCE" / "CFG" / "Main.txt"
            source.parent.mkdir(parents=True)
            source.write_text("Data ^{\n  Value=1\n}\n", encoding="utf-8")
            binary = root / "CFG" / "Main.dat"
            binary.parent.mkdir()
            Toolchain().convert_dat(source, binary)
            source.write_text("Data ^{\n  Value=2\n}\n", encoding="utf-8")

            report = audit_mod(root, profile="release")
            self.assertTrue(
                any(item.code == "dat-source-binary-mismatch" for item in report.issues)
            )

    def test_known_alternative_pkg_is_unsupported_not_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "AuditFixture"
            _mod(root)
            (root / "DATA" / "alternate.pkg").write_bytes(b"XPKG" + bytes(60))
            report = audit_mod(root, profile="release")
            check = next(item for item in report.checks if item.name == "resource-integrity")
            self.assertEqual(check.status, "unsupported")
            self.assertFalse(check.complete)
            self.assertFalse(any(item.code == "resource-invalid" for item in report.issues))


if __name__ == "__main__":
    unittest.main()
