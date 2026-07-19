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

    def test_explicit_init_functions_are_shared_with_turn_objects(self) -> None:
        data = deepcopy(SAFE_RSON)
        init = data["Visual.Objects"][0]["Operations"][0]
        init["Code.Type"] = "Init"
        init["Code"].extend(["function ModTurn()", "{", "    result = 1;", "}"])
        data["Visual.Objects"][0]["Operations"][1]["Code"] = ["ModTurn();"]
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("init-library.rson")))
        }
        self.assertNotIn("runtime-cross-block-function-call", codes)

    def test_tvar_is_a_shared_rscript_variable(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Variables"] = [
            {"Type": "TVar", "Name": "runtime_ready", "Parent": -1, "#": 10},
            {"Type": "TVar", "Name": "runtime_ready_turn", "Parent": -1, "#": 11},
        ]
        issues = lint_rson_runtime(RsonProject(data, Path("shared-tvar.rson")))
        self.assertNotIn(
            "runtime-cross-block-variable-reference",
            {issue.code for issue in issues},
        )

    def test_dialog_turn_chain_is_not_treated_as_periodic_turn(self) -> None:
        data = deepcopy(SAFE_RSON)
        group = data["Visual.Objects"][0]
        group["Operations"][1]["Code"] = ["GetShipPlanet(Player());"]
        group["Dialogs"] = [
            {"Type": "TDialogMsg", "Name": "Message", "Parent": -1, "#": 10}
        ]
        data["Visual.Links"] = [
            {"Type": "TGraphLink", "Begin": 10, "End": 2, "Nom": 0, "Arrow": True}
        ]
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("dialog-turn.rson")))
        }
        self.assertNotIn("runtime-turn-direct-world-access", codes)

    def test_mixed_dialog_and_periodic_entry_remains_periodic(self) -> None:
        data = deepcopy(SAFE_RSON)
        group = data["Visual.Objects"][0]
        group["Operations"][1]["Code"] = ["GetShipPlanet(Player());"]
        group["Dialogs"] = [
            {"Type": "TDialogMsg", "Name": "Message", "Parent": -1, "#": 10}
        ]
        group["Statements"] = [
            {
                "Type": "Tif",
                "Name": "Periodic",
                "Parent": -1,
                "#": 11,
                "Code.Type": "Turn",
                "Code": ["1"],
            }
        ]
        data["Visual.Links"] = [
            {"Type": "TGraphLink", "Begin": 10, "End": 2, "Nom": 0, "Arrow": True},
            {"Type": "TGraphLink", "Begin": 11, "End": 2, "Nom": 0, "Arrow": True},
        ]
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("mixed-dialog-turn.rson")))
        }
        self.assertIn("runtime-turn-direct-world-access", codes)

    def test_state_handler_cannot_call_function_from_top_code(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][0]["Code.Type"] = "Init"
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

    def test_raw_item_handle_cannot_be_persisted_through_helper(self) -> None:
        data = deepcopy(SAFE_RSON)
        group = data["Visual.Objects"][0]
        group["Variables"] = [
            {"Type": "TVar", "Name": "cargo_slot", "Parent": -1, "#": 10},
            {"Type": "TVar", "Name": "cargo_registry", "Parent": -1, "#": 11},
        ]
        group["Operations"][0]["Code"].extend(
            [
                "function StoreCargo(int index, dword cargo)",
                "{",
                "    cargo_slot = cargo;",
                "    ArrayAdd(cargo_registry, cargo);",
                "}",
                "function CreateCargo()",
                "{",
                "    dword cargo = CreateQuestItem(0, 1, 1, 1, 0, 0, 0, 0);",
                "    StoreCargo(0, cargo);",
                "}",
            ]
        )
        issues = lint_rson_runtime(RsonProject(data, Path("raw-item.rson")))
        matching = [issue for issue in issues if issue.code == "runtime-persistent-raw-item-handle"]
        self.assertEqual({issue.evidence for issue in matching}, {"StoreCargo(0, cargo);"})
        self.assertTrue(all(issue.severity == "error" for issue in matching))

    def test_item_id_can_be_persisted_and_resolved_each_turn(self) -> None:
        data = deepcopy(SAFE_RSON)
        group = data["Visual.Objects"][0]
        group["Variables"] = [
            {"Type": "TVar", "Name": "cargo_slot", "Parent": -1, "#": 10}
        ]
        group["Operations"][0]["Code"].extend(
            [
                "function StoreCargo(int cargo_id)",
                "{",
                "    cargo_slot = cargo_id;",
                "}",
                "function CreateCargo()",
                "{",
                "    dword cargo = CreateQuestItem(0, 1, 1, 1, 0, 0, 0, 0);",
                "    int cargo_id = Id(cargo);",
                "    StoreCargo(cargo_id);",
                "    dword current_cargo = IdToItem(cargo_slot);",
                "    if(current_cargo) ItemExist(current_cargo);",
                "}",
            ]
        )
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("stable-item-id.rson")))
        }
        self.assertNotIn("runtime-persistent-raw-item-handle", codes)

    def test_persistent_planet_reference_requires_stable_id_restore(self) -> None:
        data = deepcopy(SAFE_RSON)
        group = data["Visual.Objects"][0]
        group["Variables"] = [
            {"Type": "TVar", "Name": "destination", "Parent": -1, "#": 10}
        ]
        group["Operations"][0]["Code"].extend(
            [
                "function SendShip(dword planet)",
                "{",
                "    PlanetToStar(planet);",
                "}",
                "SendShip(destination);",
            ]
        )
        issues = lint_rson_runtime(RsonProject(data, Path("stale-planet.rson")))
        matching = [
            issue
            for issue in issues
            if issue.code == "runtime-persistent-world-object-handle"
        ]
        self.assertEqual(len(matching), 1)
        self.assertIn("IdToPlanet", matching[0].message)
        self.assertEqual(matching[0].evidence, "SendShip(destination);")

    def test_world_reference_migration_clears_legacy_handle_first(self) -> None:
        data = deepcopy(SAFE_RSON)
        group = data["Visual.Objects"][0]
        group["Variables"] = [
            {"Type": "TVar", "Name": "destination", "Parent": -1, "#": 10},
            {"Type": "TVar", "Name": "destination_id", "Parent": -1, "#": 11},
        ]
        group["Operations"][0]["Code"].extend(
            [
                "function RestoreWorldRefs()",
                "{",
                "    if(destination_id) destination = IdToPlanet(destination_id);",
                "}",
                "function UseDestination(dword planet)",
                "{",
                "    PlanetToStar(planet);",
                "}",
                "RestoreWorldRefs();",
                "destination_id = Id(destination);",
                "UseDestination(destination);",
            ]
        )
        issues = lint_rson_runtime(RsonProject(data, Path("legacy-planet.rson")))
        matching = [
            issue
            for issue in issues
            if issue.code == "runtime-persistent-world-object-handle"
        ]
        self.assertEqual(len(matching), 1)
        self.assertIn("не обнуляется", matching[0].message)

    def test_world_reference_restored_from_shared_id_is_safe(self) -> None:
        data = deepcopy(SAFE_RSON)
        group = data["Visual.Objects"][0]
        group["Variables"] = [
            {"Type": "TVar", "Name": "destination", "Parent": -1, "#": 10},
            {"Type": "TVar", "Name": "destination_id", "Parent": -1, "#": 11},
            {"Type": "TVar", "Name": "target_star", "Parent": -1, "#": 12},
            {"Type": "TVar", "Name": "target_star_id", "Parent": -1, "#": 13},
        ]
        group["Operations"][0]["Code"].extend(
            [
                "function IdToStar(int star_id)",
                "{",
                "    result = 0;",
                "    if(!star_id) exit;",
                "    for(int cursor = 0; cursor < GalaxyStars(); cursor = cursor + 1)",
                "    {",
                "        dword star = GalaxyStar(cursor);",
                "        if(star && Id(star) == star_id)",
                "        {",
                "            result = star;",
                "            exit;",
                "        }",
                "    }",
                "}",
                "function RestoreWorldRefs()",
                "{",
                "    destination = 0;",
                "    target_star = 0;",
                "    if(destination_id) destination = IdToPlanet(destination_id);",
                "    if(target_star_id) target_star = IdToStar(target_star_id);",
                "}",
                "function UseWorld(dword planet, dword star)",
                "{",
                "    PlanetToStar(planet);",
                "    StarName(star);",
                "}",
                "RestoreWorldRefs();",
                "destination_id = Id(destination);",
                "target_star_id = Id(target_star);",
                "UseWorld(destination, target_star);",
            ]
        )
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("stable-world-ids.rson")))
        }
        self.assertNotIn("runtime-persistent-world-object-handle", codes)
        self.assertNotIn("runtime-unsupported-engine-call", codes)

    def test_unavailable_engine_id_to_star_is_rejected(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][0]["Code"].append(
            "dword star = IdToStar(42);"
        )
        issues = lint_rson_runtime(RsonProject(data, Path("missing-id-to-star.rson")))
        matching = [
            issue
            for issue in issues
            if issue.code == "runtime-unsupported-engine-call"
        ]
        self.assertEqual(len(matching), 1)
        self.assertIn("Not link var :IdToStar", matching[0].message)
        self.assertEqual(matching[0].evidence, "dword star = IdToStar(42);")

    def test_id_to_ship_requires_guard_above_reserved_ids(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][0]["Code"].extend(
            [
                "function RestoreShip(int ship_id)",
                "{",
                "    dword ship = IdToShip(ship_id);",
                "    if(ship) ShipInScript(ship, 0);",
                "}",
            ]
        )
        issues = lint_rson_runtime(RsonProject(data, Path("unsafe-id-to-ship.rson")))
        matching = [
            issue for issue in issues if issue.code == "runtime-id-to-ship-reserved-id"
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].evidence, "dword ship = IdToShip(ship_id);")

    def test_id_to_ship_guard_above_one_is_safe(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][0]["Code"].extend(
            [
                "function RestoreShip(int ship_id)",
                "{",
                "    if(ship_id <= 1) exit;",
                "    dword ship = IdToShip(ship_id);",
                "    if(ship) ShipInScript(ship, 0);",
                "}",
            ]
        )
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("safe-id-to-ship.rson")))
        }
        self.assertNotIn("runtime-id-to-ship-reserved-id", codes)

    def test_locked_shipjoin_without_initial_state_is_rejected(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][0]["Code"].extend(
            [
                "function SpawnEscort(dword group, dword ship)",
                "{",
                "    ShipJoin(group, ship, 1);",
                "    OrderLock(ship, 1);",
                "}",
            ]
        )
        issues = lint_rson_runtime(RsonProject(data, Path("stateless-escort.rson")))
        matching = [
            issue for issue in issues if issue.code == "runtime-shipjoin-state-suppressed"
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].evidence, "ShipJoin(group, ship, 1);")

    def test_two_argument_shipjoin_keeps_initial_state(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][0]["Code"].extend(
            [
                "function SpawnEscort(dword group, dword ship)",
                "{",
                "    ShipJoin(group, ship);",
                "    OrderLock(ship, 1);",
                "}",
            ]
        )
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("stateful-escort.rson")))
        }
        self.assertNotIn("runtime-shipjoin-state-suppressed", codes)

    def test_explicit_change_state_allows_suppressed_shipjoin_default(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][0]["Code"].extend(
            [
                "function SpawnEscort(dword group, dword ship)",
                "{",
                "    ShipJoin(group, ship, 1);",
                "    ChangeState('Escort', ship);",
                "    OrderLock(ship, 1);",
                "}",
            ]
        )
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("explicit-state.rson")))
        }
        self.assertNotIn("runtime-shipjoin-state-suppressed", codes)

    def test_shipjoin_guarded_by_script_membership_is_rejected(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][0]["Code"].extend(
            [
                "function SpawnTransport(dword group, dword ship)",
                "{",
                "    if(!ShipInCurScript(ship)) ShipJoin(group, ship);",
                "}",
            ]
        )
        issues = lint_rson_runtime(RsonProject(data, Path("wrong-group-guard.rson")))
        matching = [
            issue
            for issue in issues
            if issue.code == "runtime-shipjoin-script-membership-guard"
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(
            matching[0].evidence,
            "if(!ShipInCurScript(ship)) ShipJoin(group, ship);",
        )

    def test_unconditional_shipjoin_is_not_a_script_membership_guard(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][0]["Code"].extend(
            [
                "function SpawnTransport(dword group, dword ship)",
                "{",
                "    ShipJoin(group, ship);",
                "}",
            ]
        )
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("explicit-join.rson")))
        }
        self.assertNotIn("runtime-shipjoin-script-membership-guard", codes)

    def test_unproven_local_star_resolver_does_not_protect_saved_handle(self) -> None:
        data = deepcopy(SAFE_RSON)
        group = data["Visual.Objects"][0]
        group["Variables"] = [
            {"Type": "TVar", "Name": "target_star", "Parent": -1, "#": 10},
            {"Type": "TVar", "Name": "target_star_id", "Parent": -1, "#": 11},
        ]
        group["Operations"][0]["Code"].extend(
            [
                "function IdToStar(int star_id)",
                "{",
                "    result = 0;",
                "    for(int cursor = 0; cursor < GalaxyStars(); cursor = cursor + 1)",
                "    {",
                "        result = result;",
                "    }",
                "    dword star = GalaxyStar(0);",
                "    if(Id(star) == star_id) result = star;",
                "}",
                "function RestoreWorldRefs()",
                "{",
                "    target_star = 0;",
                "    if(target_star_id) target_star = IdToStar(target_star_id);",
                "}",
                "RestoreWorldRefs();",
                "target_star_id = Id(target_star);",
                "StarName(target_star);",
            ]
        )
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("bad-star-resolver.rson")))
        }
        self.assertNotIn("runtime-unsupported-engine-call", codes)
        self.assertIn("runtime-persistent-world-object-handle", codes)

    def test_tvar_world_object_scratch_assigned_before_use_is_safe(self) -> None:
        data = deepcopy(SAFE_RSON)
        group = data["Visual.Objects"][0]
        group["Variables"] = [
            {"Type": "TVar", "Name": "system", "Parent": -1, "#": 10},
            {"Type": "TVar", "Name": "ship", "Parent": -1, "#": 11},
        ]
        group["Operations"][0]["Code"].extend(
            [
                "system = GalaxyStar(0);",
                "for(int cursor = 0; cursor < StarShips(system); cursor = cursor + 1)",
                "{",
                "    ship = StarShips(system, cursor);",
                "    if(ShipInHyperSpace(ship)) continue;",
                "}",
            ]
        )
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("world-scratch.rson")))
        }
        self.assertNotIn("runtime-persistent-world-object-handle", codes)

    def test_unload_then_shipout_in_same_handler_is_rejected(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][0]["Code"].extend(
            [
                "function TakeCargo(int convoy_index, dword ship, dword cargo)",
                "{",
                "    GetItemFromShip(ship, cargo);",
                "}",
                "function DeliverTransport(int convoy_index, dword ship, dword cargo)",
                "{",
                "    TakeCargo(convoy_index, ship, cargo);",
                "    FreeItem(cargo);",
                "    ShipOut(ship);",
                "}",
            ]
        )
        issues = lint_rson_runtime(RsonProject(data, Path("unsafe-shipout.rson")))
        matching = [issue for issue in issues if issue.code == "runtime-landed-shipout-after-mutation"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].evidence, "ShipOut(ship);")

    def test_takeoff_boundary_without_same_turn_shipout_is_safe(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][0]["Code"].extend(
            [
                "function TakeCargo(int convoy_index, dword ship, dword cargo)",
                "{",
                "    GetItemFromShip(ship, cargo);",
                "}",
                "function DeliverTransport(int convoy_index, dword ship, dword cargo)",
                "{",
                "    TakeCargo(convoy_index, ship, cargo);",
                "    FreeItem(cargo);",
                "    OrderTakeOff(ship);",
                "}",
            ]
        )
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("safe-takeoff.rson")))
        }
        self.assertNotIn("runtime-landed-shipout-after-mutation", codes)

    def test_forward_group_iteration_rejects_shipout(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][0]["Code"].extend(
            [
                "function RemoveTransport(dword ship)",
                "{",
                "    ShipOut(ship);",
                "}",
                "function Cleanup(dword group)",
                "{",
                "    for(int cursor = 0; cursor < GroupCount(group); cursor = cursor + 1)",
                "    {",
                "        dword ship = GroupShip(group, cursor);",
                "        RemoveTransport(ship);",
                "    }",
                "}",
            ]
        )
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("unsafe-group.rson")))
        }
        self.assertIn("runtime-group-mutated-during-iteration", codes)

    def test_forward_group_iteration_rejects_index_compensation(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][0]["Code"].extend(
            [
                "function Cleanup(dword group)",
                "{",
                "    for(int cursor = 0; cursor < GroupCount(group); cursor = cursor + 1)",
                "    {",
                "        dword ship = GroupShip(group, cursor);",
                "        ShipOut(ship);",
                "        cursor = cursor - 1;",
                "    }",
                "}",
            ]
        )
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("compensated-group.rson")))
        }
        self.assertIn("runtime-group-mutated-during-iteration", codes)

    def test_group_iteration_allows_reverse_order(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][0]["Code"].extend(
            [
                "function Cleanup(dword group)",
                "{",
                "    for(int cursor = GroupCount(group) - 1; cursor >= 0; cursor = cursor - 1)",
                "    {",
                "        dword ship = GroupShip(group, cursor);",
                "        ShipOut(ship);",
                "    }",
                "}",
            ]
        )
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("reverse-group.rson")))
        }
        self.assertNotIn("runtime-group-mutated-during-iteration", codes)

    def test_reverse_group_mutation_requires_exit_before_recount(self) -> None:
        for name, barrier, expected in (
            ("unsafe", [], True),
            ("safe", ["    if(removed) exit;"], False),
        ):
            with self.subTest(name=name):
                data = deepcopy(SAFE_RSON)
                data["Visual.Objects"][0]["Operations"][0]["Code"].extend(
                    [
                        "function Cleanup(dword group)",
                        "{",
                        "    for(int cursor = GroupCount(group) - 1; cursor >= 0; cursor = cursor - 1)",
                        "    {",
                        "        dword ship = GroupShip(group, cursor);",
                        "        ShipOut(ship);",
                        "        removed = 1;",
                        "    }",
                        *barrier,
                        "    if(GroupCount(group) == 0) result = 1;",
                        "}",
                    ]
                )
                codes = {
                    issue.code
                    for issue in lint_rson_runtime(RsonProject(data, Path(f"{name}-recount.rson")))
                }
                if expected:
                    self.assertIn("runtime-group-recount-after-mutation", codes)
                else:
                    self.assertNotIn("runtime-group-recount-after-mutation", codes)

    def test_one_step_base_case_recursion_is_proven_bounded(self) -> None:
        data = deepcopy(SAFE_RSON)
        init = data["Visual.Objects"][0]["Operations"][0]
        init["Code.Type"] = "Init"
        init["Code"].extend(
            [
                "function choice2(w1, a, w2, b) {",
                "    if(w1 + w2 == 0) {",
                "        result = choice2(1.0, a, 1.0, b);",
                "    } else {",
                "        result = a;",
                "    }",
                "}",
            ]
        )
        data["Visual.Objects"][0]["Operations"][1]["Code"] = ["choice2(0, 1, 0, 2);"]
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("bounded-recursion.rson")))
        }
        self.assertNotIn("runtime-recursion-cycle", codes)

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

    def test_unknown_call_is_rejected_against_runtime_api_registry(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "CurTurn();",
            "MissingModHelper();",
        ]
        issues = lint_rson_runtime(RsonProject(data, Path("unresolved.rson")))
        matching = [
            issue for issue in issues if issue.code == "runtime-unresolved-user-function"
        ]
        self.assertEqual(len(matching), 1)
        self.assertIn("MissingModHelper", matching[0].message)

    def test_imported_tvar_is_accepted_as_callable(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Variables"] = [
            {"Type": "TVar", "Name": "ExternalRoll", "Parent": -1, "#": 12}
        ]
        data["Visual.Objects"][0]["Operations"][1]["Code"] = ["ExternalRoll(1, 2);"]
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("imported.rson")))
        }
        self.assertNotIn("runtime-unresolved-user-function", codes)

    def test_apostrophe_in_on_act_line_comment_is_rejected(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["States"][0]["OnActCode"] = (
            "[t_OnEnteringForm|]\n// user's route helper\nruntime_ready = 1;"
        )
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("apostrophe.rson")))
        }
        self.assertIn("runtime-apostrophe-in-line-comment", codes)
        data["Visual.Objects"][0]["States"][0]["OnActCode"] = (
            "[t_OnEnteringForm|]\n// DebugCall('quoted value');\nruntime_ready = 1;"
        )
        balanced_codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("quoted-comment.rson")))
        }
        self.assertNotIn("runtime-apostrophe-in-line-comment", balanced_codes)

    def test_rscript_array_zero_index_and_zero_dimension_model_are_rejected(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "unknown queue = newarray(1);",
            "ArrayClear(queue);",
            "if(ArrayDim(queue) > 0 && queue[0] > 1) exit;",
        ]
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("array-zero.rson")))
        }
        self.assertIn("runtime-rscript-array-service-index", codes)
        self.assertIn("runtime-rscript-array-empty-dimension", codes)

    def test_one_based_rscript_array_loop_is_safe(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "unknown queue = newarray(1);",
            "ArrayClear(queue);",
            "if(ArrayDim(queue) <= 1) exit;",
            "for(int i = 1; i < ArrayDim(queue); i = i + 1)",
            "{",
            "    if(queue[i] > 1) exit;",
            "}",
        ]
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("array-safe.rson")))
        }
        self.assertNotIn("runtime-rscript-array-service-index", codes)
        self.assertNotIn("runtime-rscript-array-empty-dimension", codes)

    def test_fixed_size_rscript_array_remains_zero_based(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "unknown labels = newarray(5);",
            "labels[0] = CT('First');",
            "for(int i = 0; i < 5; i = i + 1)",
            "{",
            "    labels[i] = i;",
            "}",
        ]
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("fixed-array.rson")))
        }
        self.assertNotIn("runtime-rscript-array-service-index", codes)

    def test_persistent_paired_arrays_need_dimension_proof(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Variables"] = [
            {"Type": "TVar", "Name": "queue_ids", "Init": "newarray(1)", "#": 20},
            {"Type": "TVar", "Name": "queue_turns", "Init": "newarray(1)", "#": 21},
        ]
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "for(int i = 1; i < ArrayDim(queue_ids); i = i + 1)",
            "{",
            "    if(queue_ids[i] && queue_turns[i] <= CurTurn()) exit;",
            "}",
        ]
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("paired.rson")))
        }
        self.assertIn("runtime-rscript-paired-array-dimension", codes)

    def test_rndobject_rejects_proven_item_anchor(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "dword cargo = CreateQuestItem('Cargo', 0);",
            "RndObject(1, 100, cargo);",
            "RndObject(1, 100, Player());",
        ]
        issues = lint_rson_runtime(RsonProject(data, Path("rnd-item.rson")))
        matching = [issue for issue in issues if issue.code == "runtime-rndobject-anchor-type"]
        self.assertEqual(len(matching), 1)
        self.assertIn("cargo", matching[0].evidence or "")

    def test_repeated_detach_unlink_free_chain_is_rejected(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "function RemoveCargo(ship, index)",
            "{",
            "    dword item = GetItemFromShip(ship, index);",
            "    ReleaseItemFromScript(item);",
            "    FreeItem(item);",
            "}",
            "function Deliver(ship)",
            "{",
            "    RemoveCargo(ship, 1);",
            "    RemoveCargo(ship, 2);",
            "}",
        ]
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("mass-free.rson")))
        }
        self.assertIn("runtime-item-list-mutated-during-star-act", codes)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "function RemoveOneCargo(ship, index)",
            "{",
            "    dword item = GetItemFromShip(ship, index);",
            "    ReleaseItemFromScript(item);",
            "    FreeItem(item);",
            "}",
        ]
        single_codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("single-free.rson")))
        }
        self.assertNotIn("runtime-item-list-mutated-during-star-act", single_codes)

    def test_hyperspace_guard_must_precede_order_mutation(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "function Reassign(ship)",
            "{",
            "    ShipSetBad(ship, 0);",
            "    OrderNone(ship);",
            "    if(ShipInHyperSpace(ship)) exit;",
            "}",
        ]
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("late-hyper.rson")))
        }
        self.assertIn("runtime-order-rewrite-in-hyperspace", codes)

        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "function Reassign(ship)",
            "{",
            "    if(ShipInHyperSpace(ship)) exit;",
            "    ShipSetBad(ship, 0);",
            "    OrderNone(ship);",
            "}",
        ]
        safe_codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("early-hyper.rson")))
        }
        self.assertNotIn("runtime-order-rewrite-in-hyperspace", safe_codes)

        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "function SendShip(ship)",
            "{",
            "    if(GetShipPlanet(ship))",
            "    {",
            "        OrderTakeOff(ship);",
            "        exit;",
            "    }",
            "    if(ShipInHyperSpace(ship)) exit;",
            "    OrderNone(ship);",
            "}",
        ]
        branch_codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("branch-exit.rson")))
        }
        self.assertNotIn("runtime-order-rewrite-in-hyperspace", branch_codes)

    def test_helper_group_mutation_cannot_be_reread_same_call(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "function DropShip(ship)",
            "{",
            "    ShipOut(ship);",
            "}",
            "function Cleanup(group)",
            "{",
            "    dword ship = GroupShip(group, 0);",
            "    DropShip(ship);",
            "    GroupCount(group);",
            "}",
        ]
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("group-use-after.rson")))
        }
        self.assertIn("runtime-post-group-mutation-dereference", codes)

    def test_turn_cleanup_gate_prevents_same_date_reentry_warning(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "if(CurTurn() < cleanup_turn) exit;",
            "cleanup_turn = CurTurn() + 1;",
            "dword ship = GroupShip(CleanupGroup, 0);",
            "ShipDestroy(ship);",
            "exit;",
        ]
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("cleanup-gated.rson")))
        }
        self.assertNotIn("runtime-cleanup-without-turn-gate", codes)

        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "dword ship = GroupShip(CleanupGroup, 0);",
            "ShipDestroy(ship);",
            "exit;",
        ]
        unsafe_codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("cleanup-ungated.rson")))
        }
        self.assertIn("runtime-cleanup-without-turn-gate", unsafe_codes)

    def test_shipgetbad_target_cannot_be_propagated_raw(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "dword attacker = ShipGetBad(transport);",
            "if(attacker) GroupSetBad(Escorts, attacker);",
            "OrderFollowShip(escort, attacker, 1, 1);",
        ]
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("stale-bad.rson")))
        }
        self.assertIn("runtime-stale-shipgetbad-follow", codes)

    def test_shipstar_requires_normal_space_and_completed_takeoff(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "function CurrentStar(dword ship)",
            "{",
            "    dword planet = GetShipPlanet(ship);",
            "    dword star = ShipStar(ship);",
            "    if(!star && planet) result = PlanetToStar(planet);",
            "    else result = star;",
            "}",
        ]
        issues = lint_rson_runtime(RsonProject(data, Path("late-dock-fallback.rson")))
        matching = [
            issue for issue in issues if issue.code == "runtime-shipstar-on-docked-ship"
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].evidence, "dword star = ShipStar(ship);")

        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "function CurrentStar(dword ship)",
            "{",
            "    result = 0;",
            "    if(!ship || ShipIsTakeoff(ship)) exit;",
            "    dword planet = GetShipPlanet(ship);",
            "    if(planet) { result = PlanetToStar(planet); exit; }",
            "    dword ruins = GetShipRuins(ship);",
            "    if(ruins) exit;",
            "    if(!ShipInNormalSpace(ship)) exit;",
            "    result = ShipStar(ship);",
            "}",
        ]
        safe_codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("safe-star.rson")))
        }
        self.assertNotIn("runtime-shipstar-on-docked-ship", safe_codes)

        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "dword escort = GroupShip(escorts, 0);",
            "int together = escort && ShipInNormalSpace(escort) &&",
            "               !ShipIsTakeoff(escort) &&",
            "               ShipStar(Player()) == ShipStar(escort);",
        ]
        chain_codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("guard-chain.rson")))
        }
        self.assertNotIn("runtime-shipstar-on-docked-ship", chain_codes)

        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "dword escort = GroupShip(escorts, 0);",
            "int escort_ready = escort && ShipInNormalSpace(escort) && !ShipIsTakeoff(escort);",
            "if(escort_ready)",
            "    result = ShipStar(escort);",
        ]
        flag_codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("guard-flag.rson")))
        }
        self.assertNotIn("runtime-shipstar-on-docked-ship", flag_codes)

        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "dword ranger_center = IdToShip(center_id);",
            "if(!ranger_center || ShipTypeN(ranger_center) != t_RC) exit;",
            "result = ShipStar(ranger_center);",
        ]
        station_codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("station-star.rson")))
        }
        self.assertNotIn("runtime-shipstar-on-docked-ship", station_codes)

    def test_persistent_array_requires_newarray_initialization(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Variables"] = [
            {"Type": "TVar", "Name": "queue", "Parent": -1, "#": 20}
        ]
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "ArrayClear(queue);",
            "ArrayAdd(queue, 42);",
        ]
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("not-array.rson")))
        }
        self.assertIn("runtime-persistent-array-use-without-newarray", codes)

        data["Visual.Objects"][0]["Variables"][0]["Init"] = "newarray(1)"
        initialized_codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("array-init.rson")))
        }
        self.assertNotIn("runtime-persistent-array-use-without-newarray", initialized_codes)

    def test_duplicate_local_names_across_branches_are_rejected(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["States"][0]["OnActCode"] = (
            "[t_OnPlayerBuyEq|]\n"
            "if(choice == 1)\n"
            "{\n"
            "    dword selected = 1;\n"
            "}\n"
            "else\n"
            "{\n"
            "    dword selected = 2;\n"
            "}\n"
        )
        issues = lint_rson_runtime(RsonProject(data, Path("duplicate-local.rson")))
        matching = [
            issue for issue in issues if issue.code == "runtime-duplicate-local-declaration"
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].evidence, "dword selected = 2;")

        data["Visual.Objects"][0]["States"][0]["OnActCode"] = (
            "[t_OnPlayerBuyEq|]\n"
            "if(choice == 1) { dword selected = 1; }\n"
            "else { dword fallback = 2; }\n"
        )
        safe_codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("unique-locals.rson")))
        }
        self.assertNotIn("runtime-duplicate-local-declaration", safe_codes)

    def test_shipgetbad_handle_must_be_resolved_from_live_membership(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "dword attacker = ShipGetBad(transport);",
            "if(attacker && ShipInNormalSpace(attacker)) result = ShipStar(attacker);",
        ]
        issues = lint_rson_runtime(RsonProject(data, Path("raw-shipgetbad.rson")))
        matching = [
            issue
            for issue in issues
            if issue.code == "runtime-shipgetbad-opaque-dereference"
        ]
        self.assertGreaterEqual(len(matching), 1)
        self.assertTrue(any("ShipInNormalSpace" in (issue.evidence or "") for issue in matching))

        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "dword raw_attacker = ShipGetBad(transport);",
            "dword fresh_ship = StarShips(current_star, 0);",
            "if(fresh_ship == raw_attacker && ShipIsTakeoff(fresh_ship)) exit;",
        ]
        station_codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("station-target.rson")))
        }
        self.assertIn(
            "runtime-shipistakeoff-on-unproven-starships-member",
            station_codes,
        )

        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "function ResolveLive(dword raw_ship, dword star)",
            "{",
            "    result = 0;",
            "    for(int i = 0; i < StarShips(star); i = i + 1)",
            "    {",
            "        dword live_ship = StarShip(star, i);",
            "        if(live_ship == raw_ship && ShipTypeN(live_ship) > 0 && ShipTypeN(live_ship) < t_RC)",
            "        {",
            "            result = live_ship;",
            "            exit;",
            "        }",
            "    }",
            "}",
            "dword attacker = ShipGetBad(transport);",
            "dword resolved = ResolveLive(attacker, current_star);",
            "if(resolved) ShipState(resolved);",
        ]
        safe_codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("resolved-shipgetbad.rson")))
        }
        self.assertNotIn("runtime-shipgetbad-opaque-dereference", safe_codes)


if __name__ == "__main__":
    unittest.main()
