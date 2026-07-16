from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from srhd_modkit.blockpar import parse_blockpar
from srhd_modkit.cli import _runtime_lint_target, cmd_script_audit_mod, cmd_script_build
from srhd_modkit.module_info import parse_module_info
from srhd_modkit.runtime_lint import lint_main_runtime, lint_module_runtime, lint_rson_runtime
from srhd_modkit.scripts import RSON_FILE_ID, RSON_FILE_VERSION, RsonProject


SAFE_RSON = {
    "FileID": RSON_FILE_ID,
    "FileVersion": RSON_FILE_VERSION,
    "ScriptName": "RuntimeSafe",
    "Visual.Objects": [
        {
            "Operations": [
                {
                    "Type": "Top",
                    "Name": "Global",
                    "Parent": -1,
                    "#": 1,
                    "Code": [
                        "GRun();",
                        "runtime_ready = 0;",
                        "runtime_ready_turn = 0;",
                    ],
                },
                {
                    "Type": "Top",
                    "Name": "Turn",
                    "Parent": -1,
                    "#": 2,
                    "Code.Type": "Turn",
                    "Code": [
                        "if(!runtime_ready || CurTurn() <= runtime_ready_turn) exit;",
                        "GetShipPlanet(Player());",
                    ],
                },
            ],
            "States": [
                {
                    "Type": "TState",
                    "Name": "PlayerState",
                    "Parent": -1,
                    "#": 3,
                    "OnActCode": (
                        "[t_OnEnteringForm,t_OnPlayerBuyEq|]\n"
                        "if(ScriptItemActionType(t_OnEnteringForm)) exit;\n"
                        "if(!ScriptItemActionType(t_OnPlayerBuyEq)) exit;\n"
                        "if(!runtime_ready)\n"
                        "{\n"
                        "    runtime_ready = 1;\n"
                        "    runtime_ready_turn = CurTurn();\n"
                        "}\n"
                    ),
                }
            ],
        }
    ],
    "Visual.Links": [],
}


class RuntimeLintTests(unittest.TestCase):
    def test_cross_object_readiness_guard_is_not_linkable(self) -> None:
        project = RsonProject(deepcopy(SAFE_RSON), Path("safe.rson"))
        broken = [
            issue
            for issue in lint_rson_runtime(project)
            if issue.code == "runtime-cross-block-variable-reference"
        ]
        self.assertEqual(len(broken), 2)
        self.assertEqual(
            {"runtime_ready", "runtime_ready_turn"},
            {
                "runtime_ready" if "runtime_ready," in issue.message else "runtime_ready_turn"
                for issue in broken
            },
        )

    def test_broken_turn_is_rejected_before_compilation(self) -> None:
        data = deepcopy(SAFE_RSON)
        turn_code = data["Visual.Objects"][0]["Operations"][1]["Code"]
        turn_code[:] = [
            line
            for line in turn_code
            if "runtime_ready" not in line and "CurTurn() <= runtime_ready_turn" not in line
        ]
        project = RsonProject(data, Path("broken.rson"))
        codes = {issue.code for issue in lint_rson_runtime(project)}
        self.assertIn("runtime-turn-direct-world-access", codes)

    def test_first_ui_event_must_only_arm_readiness_before_world_work(self) -> None:
        data = deepcopy(SAFE_RSON)
        code = data["Visual.Objects"][0]["Operations"][0]["Code"]
        code.extend(
            [
                "function RuntimeUI()",
                "{",
                "    runtime_ready = 1;",
                "    runtime_ready_turn = CurTurn();",
                "    GetShipPlanet(Player());",
                "}",
            ]
        )
        data["Visual.Objects"][0]["States"][0]["OnActCode"] = "[t_OnEnteringForm|]\nRuntimeUI();"
        codes = {issue.code for issue in lint_rson_runtime(RsonProject(data, Path("early-ui.rson")))}
        self.assertIn("runtime-first-ui-event-work", codes)

    def test_user_function_in_another_code_object_is_not_linkable(self) -> None:
        data = deepcopy(SAFE_RSON)
        global_code = data["Visual.Objects"][0]["Operations"][0]["Code"]
        global_code.extend(["function ModTurn()", "{", "    GetShipPlanet(Player());", "}"])
        data["Visual.Objects"][0]["Operations"][1]["Code"] = ["ModTurn();"]
        issues = lint_rson_runtime(RsonProject(data, Path("cross-block.rson")))
        issue = next(item for item in issues if item.code == "runtime-cross-block-function-call")
        self.assertEqual(issue.severity, "error")
        self.assertEqual(issue.evidence, "ModTurn();")

    def test_state_handler_cannot_call_function_from_top_code(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][0]["Code"].extend(
            ["function ModPlayerActCode()", "{", "    runtime_ready = 1;", "}"]
        )
        data["Visual.Objects"][0]["States"][0]["OnActCode"] = (
            "[t_OnEnteringForm|]\nModPlayerActCode();"
        )
        issues = lint_rson_runtime(RsonProject(data, Path("state-cross-block.rson")))
        issue = next(item for item in issues if item.code == "runtime-cross-block-function-call")
        self.assertIn("OnActCode", issue.location or "")
        self.assertEqual(issue.evidence, "ModPlayerActCode();")

    def test_cross_block_call_blocks_build_and_audit(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "SOURCE"
            source.mkdir()
            data = deepcopy(SAFE_RSON)
            data["Visual.Objects"][0]["Operations"][0]["Code"].extend(
                ["function ModTurn()", "{", "    GetShipPlanet(Player());", "}"]
            )
            data["Visual.Objects"][0]["Operations"][1]["Code"] = ["ModTurn();"]
            rson = source / "cross-block.rson"
            rson.write_text(json.dumps(data), encoding="utf-8")
            (root / "ModuleInfo.txt").write_text("Name=Test\nLanguages=Rus\n", encoding="utf-8")

            build_args = SimpleNamespace(
                source=str(rson),
                scr=str(root / "out.scr"),
                lang=str(root / "out.lang"),
                overwrite=False,
                tools_root=None,
                json=False,
            )
            with self.assertRaisesRegex(ValueError, "runtime-cross-block-function-call"):
                cmd_script_build(build_args)

            audit_args = SimpleNamespace(mod=str(root), tools_root=None, json=True)
            with redirect_stdout(StringIO()):
                self.assertEqual(cmd_script_audit_mod(audit_args), 2)

    def test_single_gated_turn_object_with_local_variables_passes_strict(self) -> None:
        data = deepcopy(SAFE_RSON)
        group = data["Visual.Objects"][0]
        group["Operations"][0]["Code"] = ["GRun();"]
        group["Operations"][1]["Code"] = [
            "int eidm_cycle_valid = 0;",
            "eidm_cycle_valid = 1;",
            "if(eidm_cycle_valid) GetShipPlanet(Player());",
        ]
        group["States"][0]["OnActCode"] = ""
        group["Statements"] = [
            {
                "Type": "Tif",
                "Name": "TurnZeroGate",
                "Parent": -1,
                "#": 4,
                "Code.Type": "Turn",
                "Code": ["CurTurn() > 0"],
            }
        ]
        data["Visual.Links"] = [
            {"Type": "TGraphLink", "Begin": 4, "End": 2, "Nom": 0, "Arrow": True}
        ]
        project = RsonProject(data, Path("single-turn-safe.rson"))
        self.assertEqual(lint_rson_runtime(project), [])

    def test_turn_zero_tif_guards_the_whole_downstream_turn_chain(self) -> None:
        data = deepcopy(SAFE_RSON)
        group = data["Visual.Objects"][0]
        group["Operations"][1]["Code"] = ["GetShipPlanet(Player());"]
        group["Operations"].append(
            {
                "Type": "Top",
                "Name": "Downstream",
                "Parent": -1,
                "#": 5,
                "Code.Type": "Turn",
                "Code": ["ShopItems(GetShipPlanet(Player()));"],
            }
        )
        group["Statements"] = [
            {
                "Type": "Tif",
                "Name": "ReadyGate",
                "Parent": -1,
                "#": 4,
                "Code.Type": "Turn",
                "Code": ["CurTurn() > 0"],
            }
        ]
        data["Visual.Links"] = [
            {"Type": "TGraphLink", "Begin": 4, "End": 2, "Nom": 0, "Arrow": True},
            {"Type": "TGraphLink", "Begin": 2, "End": 5, "Nom": 0, "Arrow": True},
        ]
        group["States"][0]["OnActCode"] = ""
        self.assertEqual(lint_rson_runtime(RsonProject(data, Path("gated-chain.rson"))), [])

    def test_alternative_unguarded_path_keeps_downstream_warning(self) -> None:
        data = deepcopy(SAFE_RSON)
        group = data["Visual.Objects"][0]
        group["Operations"][1]["Code"] = ["GetShipPlanet(Player());"]
        group["Statements"] = [
            {
                "Type": "Tif",
                "Name": "ReadyGate",
                "Parent": -1,
                "#": 4,
                "Code.Type": "Turn",
                "Code": ["CurTurn() > 0"],
            },
            {
                "Type": "Tif",
                "Name": "UnguardedRoot",
                "Parent": -1,
                "#": 5,
                "Code.Type": "Turn",
                "Code": ["1"],
            },
        ]
        data["Visual.Links"] = [
            {"Type": "TGraphLink", "Begin": 4, "End": 2, "Nom": 0, "Arrow": True},
            {"Type": "TGraphLink", "Begin": 5, "End": 2, "Nom": 0, "Arrow": True},
        ]
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("mixed-chain.rson")))
        }
        self.assertIn("runtime-turn-direct-world-access", codes)

    def test_false_or_disjunctive_tif_branch_is_not_a_proven_gate(self) -> None:
        for expression, nom in (
            ("CurTurn() > 0", 1),
            ("CurTurn() > 0 || 1", 0),
            ("CurTurn() == 0", 0),
        ):
            with self.subTest(expression=expression, nom=nom):
                data = deepcopy(SAFE_RSON)
                group = data["Visual.Objects"][0]
                group["Operations"][1]["Code"] = ["GetShipPlanet(Player());"]
                group["Statements"] = [
                    {
                        "Type": "Tif",
                        "Name": "NotProven",
                        "Parent": -1,
                        "#": 4,
                        "Code.Type": "Turn",
                        "Code": [expression],
                    }
                ]
                data["Visual.Links"] = [
                    {"Type": "TGraphLink", "Begin": 4, "End": 2, "Nom": nom, "Arrow": True}
                ]
                codes = {
                    issue.code
                    for issue in lint_rson_runtime(RsonProject(data, Path("unproven-gate.rson")))
                }
                self.assertIn("runtime-turn-direct-world-access", codes)

    def test_tif_cannot_reference_variables_initialized_in_global_top(self) -> None:
        data = deepcopy(SAFE_RSON)
        group = data["Visual.Objects"][0]
        group["Statements"] = [
            {
                "Type": "Tif",
                "Name": "BrokenGlobalGate",
                "Parent": -1,
                "#": 4,
                "Code.Type": "Turn",
                "Code": ["runtime_ready && CurTurn() > runtime_ready_turn"],
            }
        ]
        issues = lint_rson_runtime(RsonProject(data, Path("tif-global-var.rson")))
        broken = [
            issue
            for issue in issues
            if issue.code == "runtime-cross-block-variable-reference"
            and "object #4" in (issue.location or "")
        ]
        self.assertEqual({issue.evidence for issue in broken}, {"runtime_ready && CurTurn() > runtime_ready_turn"})
        self.assertEqual(len(broken), 2)
        self.assertTrue(all(issue.severity == "error" for issue in broken))

    def test_state_handler_cannot_read_variable_from_global_top(self) -> None:
        data = deepcopy(SAFE_RSON)
        group = data["Visual.Objects"][0]
        group["Operations"][0]["Code"].append("shared_state = 0;")
        group["States"][0]["OnActCode"] = "[t_OnEnteringForm|]\nif(shared_state) exit;"
        issues = lint_rson_runtime(RsonProject(data, Path("state-global-var.rson")))
        broken = [
            issue
            for issue in issues
            if issue.code == "runtime-cross-block-variable-reference"
            and "OnActCode" in (issue.location or "")
        ]
        self.assertEqual(len(broken), 1)
        self.assertEqual(broken[0].evidence, "if(shared_state) exit;")

    def test_linked_turn_object_cannot_have_empty_code_arrays(self) -> None:
        for field in ("Code", "ActCode", "LinkCode"):
            with self.subTest(field=field):
                data = deepcopy(SAFE_RSON)
                group = data["Visual.Objects"][0]
                turn = group["Operations"][1]
                turn["Code"] = ["1"]
                turn[field] = []
                group["Statements"] = [
                    {
                        "Type": "Tif",
                        "Name": "TurnZeroGate",
                        "Parent": -1,
                        "#": 4,
                        "Code.Type": "Turn",
                        "Code": ["CurTurn() > 0"],
                    }
                ]
                data["Visual.Links"] = [
                    {"Type": "TGraphLink", "Begin": 4, "End": 2, "Nom": 0, "Arrow": True}
                ]
                issues = lint_rson_runtime(RsonProject(data, Path("empty-linked.rson")))
                broken = [issue for issue in issues if issue.code == "runtime-linked-empty-code"]
                self.assertEqual(len(broken), 1)
                self.assertEqual(broken[0].evidence, f"{field}=[]")
                self.assertEqual(broken[0].severity, "error")

    def test_isolated_empty_turn_template_is_not_an_active_branch(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = []
        issues = lint_rson_runtime(RsonProject(data, Path("empty-isolated.rson")))
        self.assertNotIn("runtime-linked-empty-code", {issue.code for issue in issues})

    def test_build_preflight_blocks_linked_empty_turn_object(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "SOURCE"
            source.mkdir()
            data = deepcopy(SAFE_RSON)
            group = data["Visual.Objects"][0]
            group["Operations"][1]["Code"] = []
            group["Statements"] = [
                {
                    "Type": "Tif",
                    "Name": "TurnZeroGate",
                    "Parent": -1,
                    "#": 4,
                    "Code.Type": "Turn",
                    "Code": ["CurTurn() > 0"],
                }
            ]
            data["Visual.Links"] = [
                {"Type": "TGraphLink", "Begin": 4, "End": 2, "Nom": 0, "Arrow": True}
            ]
            rson = source / "empty-linked.rson"
            rson.write_text(json.dumps(data), encoding="utf-8")
            (root / "ModuleInfo.txt").write_text("Name=Test\nLanguages=Rus\n", encoding="utf-8")
            args = SimpleNamespace(
                source=str(rson),
                scr=str(root / "out.scr"),
                lang=str(root / "out.lang"),
                overwrite=False,
                tools_root=None,
                json=False,
            )
            with self.assertRaisesRegex(ValueError, "runtime-linked-empty-code"):
                cmd_script_build(args)

    def test_player_script_run_must_use_actual_player_planet(self) -> None:
        unsafe = parse_blockpar(
            "BV ^{\n"
            "  OnStart ^{\n"
            "    0DayScripts ^{\n"
            "      Test=ScriptRun(ShipStar(Player()), StarPlanets(ShipStar(Player()), 0), 'Test');\n"
            "    }\n"
            "  }\n"
            "}\n"
        )
        issues = lint_main_runtime(unsafe, "Main.txt")
        self.assertEqual(issues[0].code, "runtime-unsafe-player-planet-context")

        safe = parse_blockpar(
            "BV ^{\n"
            "  OnStart ^{\n"
            "    0DayScripts ^{\n"
            "      Test=ScriptRun(ShipStar(Player()), GetShipPlanet(Player()), 'Test');\n"
            "    }\n"
            "  }\n"
            "}\n"
        )
        self.assertEqual(lint_main_runtime(safe, "Main.txt"), [])

    def test_runtime_recursion_and_unbounded_loop_are_reported(self) -> None:
        data = deepcopy(SAFE_RSON)
        code = data["Visual.Objects"][0]["Operations"][0]["Code"]
        code.extend(
            [
                "function Recurse()",
                "{",
                "    while(1) Recurse();",
                "}",
            ]
        )
        data["Visual.Objects"][0]["Operations"][1]["Code"] = ["Recurse();"]
        issues = lint_rson_runtime(RsonProject(data, Path("loop.rson")))
        codes = {issue.code for issue in issues}
        self.assertIn("runtime-recursion-cycle", codes)
        self.assertIn("runtime-unbounded-loop", codes)

    def test_nested_world_loops_on_turn_are_build_blocking(self) -> None:
        data = deepcopy(SAFE_RSON)
        code = data["Visual.Objects"][0]["Operations"][0]["Code"]
        code.extend(
            [
                "function HeavyTurnWork()",
                "{",
                "    for(int i = 0; i < 10; i = i + 1)",
                "    {",
                "        for(int j = 0; j < 10; j = j + 1)",
                "        {",
                "            GetShipPlanet(Player());",
                "        }",
                "    }",
                "}",
            ]
        )
        data["Visual.Objects"][0]["Operations"][1]["Code"] = ["HeavyTurnWork();"]
        issues = lint_rson_runtime(RsonProject(data, Path("heavy.rson")))
        issue = next(item for item in issues if item.code == "runtime-nested-world-loop")
        self.assertEqual(issue.severity, "error")

    def test_module_sections_are_checked_per_language(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "ModuleInfo.txt"
            path.write_text(
                "Name=Test\nSection=OtherMods\nSectionEng=OtherMods\nLanguages=Rus,Eng\n",
                encoding="utf-8",
            )
            codes = {issue.code for issue in lint_module_runtime(parse_module_info(path))}
            self.assertEqual(codes, {"runtime-module-section-rus", "runtime-module-section-eng"})

    def test_onstart_escalates_direct_turn_world_access_to_error(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "SOURCE"
            cfg = source / "CFG"
            cfg.mkdir(parents=True)
            data = deepcopy(SAFE_RSON)
            data["Visual.Objects"][0]["Operations"][1]["Code"] = ["GetShipPlanet(Player());"]
            (source / "direct.rson").write_text(json.dumps(data), encoding="utf-8")
            (cfg / "Main.txt").write_text(
                "BV ^{\n"
                "  OnStart ^{\n"
                "    0DayScripts ^{\n"
                "      Test=ScriptRun(ShipStar(Player()), GetShipPlanet(Player()), 'RuntimeSafe');\n"
                "    }\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )
            result = _runtime_lint_target(root)
            codes = {issue["code"] for issue in result["issues"] if issue["severity"] == "error"}
            self.assertIn("runtime-onstart-unguarded-world", codes)

    def test_build_runs_whole_mod_preflight_before_compiler(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "SOURCE"
            cfg = source / "CFG"
            cfg.mkdir(parents=True)
            data = deepcopy(SAFE_RSON)
            data["Visual.Objects"][0]["Operations"][1]["Code"] = ["GetShipPlanet(Player());"]
            rson = source / "direct.rson"
            rson.write_text(json.dumps(data), encoding="utf-8")
            (root / "ModuleInfo.txt").write_text("Name=Test\nLanguages=Rus\n", encoding="utf-8")
            (cfg / "Main.txt").write_text(
                "BV ^{\n"
                "  OnStart ^{\n"
                "    0DayScripts ^{\n"
                "      Test=ScriptRun(ShipStar(Player()), GetShipPlanet(Player()), 'RuntimeSafe');\n"
                "    }\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )
            args = SimpleNamespace(
                source=str(rson),
                scr=str(root / "out.scr"),
                lang=str(root / "out.lang"),
                overwrite=False,
                tools_root=None,
                json=False,
            )
            with self.assertRaisesRegex(ValueError, "runtime-onstart-unguarded-world"):
                cmd_script_build(args)


if __name__ == "__main__":
    unittest.main()
