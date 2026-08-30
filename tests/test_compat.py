from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from srhd_modkit.compat import analyze_modset


def _native_plugin_dll() -> bytes:
    pe_offset = 0x80
    optional_size = 0xE0
    optional = pe_offset + 24
    section = optional + optional_size
    data = bytearray(0x600)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, pe_offset)
    data[pe_offset : pe_offset + 4] = b"PE\0\0"
    struct.pack_into(
        "<HHIIIHH", data, pe_offset + 4, 0x014C, 1, 0, 0, 0, optional_size, 0x2102
    )
    struct.pack_into("<H", data, optional, 0x10B)
    struct.pack_into("<II", data, optional + 96, 0x1000, 0x120)
    data[section : section + 8] = b".edata\0\0"
    struct.pack_into("<IIII", data, section + 8, 0x300, 0x1000, 0x300, 0x200)
    struct.pack_into(
        "<IIHHIIIIIII",
        data,
        0x200,
        0,
        0,
        0,
        0,
        0x1060,
        1,
        2,
        2,
        0x1040,
        0x1048,
        0x1050,
    )
    struct.pack_into("<II", data, 0x240, 0x1100, 0x1110)
    struct.pack_into("<II", data, 0x248, 0x1080, 0x10A0)
    struct.pack_into("<HH", data, 0x250, 0, 1)
    data[0x260 : 0x260 + len(b"Fixture.dll\0")] = b"Fixture.dll\0"
    data[0x280 : 0x280 + len(b"XenoPlugin_Query\0")] = b"XenoPlugin_Query\0"
    data[0x2A0 : 0x2A0 + len(b"XenoPlugin_Initialize\0")] = b"XenoPlugin_Initialize\0"
    return bytes(data)


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
    def test_native_plugins_follow_effective_mod_order_without_executing_query(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            mods = base / "Mods"
            native_mod = mods / "OtherMods" / "Native"
            plain_mod = mods / "OtherMods" / "Plain"
            _make_mod(native_mod, "Native", dependence="", priority=90)
            _make_mod(plain_mod, "Plain", dependence="", priority=10)
            native = native_mod / "Native"
            native.mkdir()
            (native / "Fixture.XenoPlugin.dll").write_bytes(_native_plugin_dll())
            config = mods / "ModCFG.txt"
            config.write_text(
                "CurrentMod=OtherMods\\Native,OtherMods\\Plain\n",
                encoding="cp1251",
            )

            report = analyze_modset(config, mods)
            self.assertEqual([item["name"] for item in report.load_order], ["Plain", "Native"])
            self.assertFalse(report.load_order[0]["native_loader"]["detected"])
            self.assertEqual(report.load_order[1]["native_loader"]["plugins"], 1)
            self.assertFalse(
                report.load_order[1]["native_loader"]["runtime_query_executed"]
            )
            self.assertFalse(
                report.load_order[1]["native_loader"]["static_validation_complete"]
            )

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

    def test_duplicate_display_names_do_not_collapse_dependency_graph_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            mods = base / "Mods"
            first = mods / "Group" / "First"
            second = mods / "Group" / "Second"
            _make_mod(first, "Same", dependence="Group\\Second", priority=1)
            _make_mod(second, "Same", dependence="Group\\First", priority=2)
            config = mods / "ModCFG.txt"
            config.write_text("CurrentMod=Group\\First,Group\\Second\n", encoding="cp1251")

            report = analyze_modset(config, mods)
            self.assertEqual(len(report.cycles), 1)
            self.assertIn("Same [Group/First]", report.cycles[0])
            self.assertIn("Same [Group/Second]", report.cycles[0])
            self.assertEqual(report.dependency_edges[0]["from_path"], "Group/First")
            self.assertEqual(report.dependency_edges[0]["to_paths"], ["Group/Second"])


if __name__ == "__main__":
    unittest.main()
