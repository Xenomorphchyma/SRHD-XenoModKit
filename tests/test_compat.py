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
    priority: int | str | None,
) -> None:
    root.mkdir(parents=True)
    priority_line = [] if priority is None else [f"Priority={priority}"]
    (root / "ModuleInfo.txt").write_text(
        "\n".join(
            (
                f"Name={name}",
                "Section=Test",
                *priority_line,
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
            self.assertEqual([item["effective_priority"] for item in report.load_order], [10, 20])
            self.assertEqual([item["configured_order"] for item in report.load_order], [0, 1])
            self.assertEqual(report.cycles, (("A", "B", "A"),))
            self.assertEqual(report.conflict_edges[0]["status"], "enabled")
            self.assertEqual(report.conflict_edges[0]["to"], ["B"])
            by_path = {item.path: item for item in report.collisions}
            self.assertEqual(by_path["DATA/same.bin"].kind, "identical")
            self.assertEqual(by_path["DATA/different.bin"].kind, "binary-replacement")
            self.assertEqual(by_path["CFG/Merge.txt"].kind, "blockpar-merge")
            self.assertTrue(all(item.resolution == "unknown" for item in report.collisions))
            self.assertTrue(any(item.code == "dependency-cycle" for item in report.issues))
            self.assertTrue(any(item.code == "enabled-conflict" for item in report.issues))
            self.assertEqual(report.as_dict()["schema"], "srhd-modkit-modset-v1")
            self.assertEqual(
                report.as_dict()["order_policy"],
                {
                    "field": "Priority",
                    "direction": "ascending",
                    "stable": True,
                    "missing_priority": 0,
                    "tie_breaker": "CurrentMod",
                    "writes_config": False,
                },
            )

    def test_priority_sort_is_stable_and_applies_to_collision_owners(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            mods = base / "Mods"
            low = mods / "Group" / "Low"
            high = mods / "Group" / "High"
            _make_mod(low, "Low", dependence="", priority=10)
            _make_mod(high, "High", dependence="", priority=90)
            (low / "DATA" / "same.bin").write_bytes(b"low")
            (high / "DATA" / "same.bin").write_bytes(b"high")
            config = mods / "ModCFG.txt"
            config.write_text(
                "CurrentMod=Group\\High,Group\\Low\n",
                encoding="cp1251",
            )

            report = analyze_modset(config, mods)
            self.assertEqual([item["name"] for item in report.load_order], ["Low", "High"])
            self.assertEqual([item["order"] for item in report.load_order], [0, 1])
            self.assertEqual([item["configured_order"] for item in report.load_order], [1, 0])
            self.assertEqual(
                [owner.mod for owner in report.collisions[0].owners],
                ["Low", "High"],
            )
            self.assertEqual(
                [owner.configured_order for owner in report.collisions[0].owners],
                [1, 0],
            )
            self.assertEqual(report.collisions[0].resolution, "unknown")
            self.assertTrue(
                any(issue.code == "modcfg-priority-order-mismatch" for issue in report.issues)
            )
            self.assertEqual(
                config.read_text(encoding="cp1251"),
                "CurrentMod=Group\\High,Group\\Low\n",
            )

    def test_equal_priorities_keep_currentmod_as_tie_breaker(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            mods = base / "Mods"
            first = mods / "Group" / "First"
            second = mods / "Group" / "Second"
            _make_mod(first, "First", dependence="", priority=5)
            _make_mod(second, "Second", dependence="", priority=5)
            config = mods / "ModCFG.txt"
            config.write_text(
                "CurrentMod=Group\\Second,Group\\First\n",
                encoding="cp1251",
            )

            report = analyze_modset(config, mods)
            self.assertEqual(
                [item["name"] for item in report.load_order],
                ["Second", "First"],
            )
            self.assertFalse(
                any(issue.code == "modcfg-priority-order-mismatch" for issue in report.issues)
            )

    def test_missing_and_invalid_priority_are_explicit_zero_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            mods = base / "Mods"
            explicit = mods / "Group" / "Explicit"
            missing = mods / "Group" / "Missing"
            invalid = mods / "Group" / "Invalid"
            _make_mod(explicit, "Explicit", dependence="", priority=1)
            _make_mod(missing, "Missing", dependence="", priority=None)
            _make_mod(invalid, "Invalid", dependence="", priority="bad")
            config = mods / "ModCFG.txt"
            config.write_text(
                "CurrentMod=Group\\Explicit,Group\\Missing,Group\\Invalid\n",
                encoding="cp1251",
            )

            report = analyze_modset(config, mods)
            self.assertEqual(
                [item["name"] for item in report.load_order],
                ["Missing", "Invalid", "Explicit"],
            )
            self.assertEqual(
                [item["effective_priority"] for item in report.load_order],
                [0, 0, 1],
            )
            self.assertEqual(
                [item["priority_source"] for item in report.load_order],
                ["default-zero", "invalid-assumed-zero", "explicit"],
            )
            self.assertTrue(any(issue.code == "invalid-priority" for issue in report.issues))
            self.assertTrue(
                any(issue.code == "modcfg-priority-order-mismatch" for issue in report.issues)
            )


if __name__ == "__main__":
    unittest.main()
