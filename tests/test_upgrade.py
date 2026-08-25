from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from srhd_modkit.upgrade import check_upgrade


def _version(root: Path, *, dependencies: str, scr_tail: bytes) -> None:
    (root / "DATA" / "Script").mkdir(parents=True)
    (root / "DATA" / "Images").mkdir()
    (root / "ModuleInfo.txt").write_text(
        f"Name=UpgradeFixture\nSection=Test\nLanguages=Rus\nDependence={dependencies}\n",
        encoding="cp1251",
    )
    (root / "DATA" / "Script" / "Mod_UpgradeFixture.scr").write_bytes(
        (8).to_bytes(4, "little") + scr_tail
    )


class UpgradeTests(unittest.TestCase):
    def test_upgrade_check_combines_module_script_and_removed_resource_risks(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            old = root / "old"
            new = root / "new"
            _version(old, dependencies="BaseA", scr_tail=b"old")
            _version(new, dependencies="BaseB", scr_tail=b"new")
            (old / "DATA" / "Images" / "old.gi").write_bytes(b"resource")

            result = check_upgrade(old, new, audit=False)
            codes = {item["code"] for item in result["issues"]}
            self.assertIn("upgrade-dependencies-changed", codes)
            self.assertIn("runtime-saved-script-cache-update-shadow", codes)
            self.assertIn("upgrade-resource-removed", codes)
            self.assertEqual(result["summary"]["changed_scripts"], 1)


if __name__ == "__main__":
    unittest.main()
