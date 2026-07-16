from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from srhd_modkit.compat import analyze_modset


def _make_mod(
    root: Path,
    name: str,
    *,
    dependence: str,
    conflict: str = "",
    priority: int,
) -> None:
    root.mkdir(parents=True)
    (root / "ModuleInfo.txt").write_text(
        "\n".join(
            (
                f"Name={name}",
                "Section=Test",
                f"Priority={priority}",
                "Languages=Rus",
                f"Dependence={dependence}",
                f"Conflict={conflict}",
                "",
            )
        ),
        encoding="cp1251",
    )
    (root / "DATA").mkdir()
    (root / "CFG").mkdir()


class CompatibilityTests(unittest.TestCase):
    def test_modset_reports_cycles_and_classifies_overlays_without_a_winner(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            mods = base / "Mods"
            first = mods / "Group" / "A"
            second = mods / "Group" / "B"
            _make_mod(first, "A", dependence="B", conflict="B", priority=10)
            _make_mod(second, "B", dependence="A", priority=20)

            for root in (first, second):
                (root / "DATA" / "same.bin").write_bytes(b"same")
            (first / "DATA" / "different.bin").write_bytes(b"one")
            (second / "DATA" / "different.bin").write_bytes(b"two")
            (first / "CFG" / "Merge.txt").write_text(
                "Data ^{\n  Items ~{\n    A=1\n  }\n}\n", encoding="utf-8"
            )
            (second / "CFG" / "Merge.txt").write_text(
                "Data ^{\n  Items ~{\n    B=2\n  }\n}\n", encoding="utf-8"
            )
            config = mods / "ModCFG.txt"
            config.write_text("CurrentMod=Group\\A,Group\\B\n", encoding="cp1251")

            report = analyze_modset(config, mods)
            self.assertEqual([item["name"] for item in report.load_order], ["A", "B"])
            self.assertEqual([item["priority"] for item in report.load_order], [10, 20])
            self.assertEqual(report.cycles, (("A", "B", "A"),))
            by_path = {item.path: item for item in report.collisions}
            self.assertEqual(by_path["DATA/same.bin"].kind, "identical")
            self.assertEqual(by_path["DATA/different.bin"].kind, "binary-replacement")
            self.assertEqual(by_path["CFG/Merge.txt"].kind, "blockpar-merge")
            self.assertTrue(all(item.resolution == "unknown" for item in report.collisions))
            self.assertTrue(any(item.code == "dependency-cycle" for item in report.issues))
            self.assertTrue(any(item.code == "enabled-conflict" for item in report.issues))
            self.assertEqual(report.as_dict()["schema"], "srhd-modkit-modset-v1")


if __name__ == "__main__":
    unittest.main()
