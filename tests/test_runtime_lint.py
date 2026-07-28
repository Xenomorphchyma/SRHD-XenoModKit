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
from srhd_modkit.cli import (
    _runtime_lint_target,
    cmd_script_audit_mod,
    cmd_script_build,
    cmd_script_validate,
    main,
)
from srhd_modkit.image_codec import RgbaImage, encode_gi
from srhd_modkit.module_info import parse_module_info
from srhd_modkit.runtime_lint import (
    compare_storage_schemas,
    lint_custom_faction_resources,
    lint_literal_ct_keys,
    lint_main_runtime,
    lint_module_runtime,
    lint_quest_item_images,
    lint_rson_runtime,
)
from srhd_modkit.scripts import RSON_FILE_ID, RSON_FILE_VERSION, RsonProject
from srhd_modkit.toolchain import ScriptBuildFailure


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

    def test_modal_dialog_reachable_from_nextday_is_rejected(self) -> None:
        data = deepcopy(SAFE_RSON)
        group = data["Visual.Objects"][0]
        group["Dialogs"] = [
            {
                "Type": "TDialog",
                "Name": "WarningDialog",
                "Parent": -1,
                "#": 10,
            }
        ]
        group["Operations"][1]["Code"] = [
            "function ShowWarning()",
            "{",
            "    Dialog(WarningDialog, Player());",
            "}",
            "ShowWarning();",
        ]
        matching = [
            issue
            for issue in lint_rson_runtime(
                RsonProject(data, Path("nextday-dialog.rson"))
            )
            if issue.code == "runtime-modal-dialog-from-nextday"
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].severity, "error")
        self.assertIn("ShowWarning", matching[0].message)

        data["Visual.Links"] = [
            {
                "Type": "TGraphLink",
                "Begin": 10,
                "End": 2,
                "Nom": 0,
                "Arrow": True,
            }
        ]
        safe_codes = {
            issue.code
            for issue in lint_rson_runtime(
                RsonProject(data, Path("user-dialog.rson"))
            )
        }
        self.assertNotIn("runtime-modal-dialog-from-nextday", safe_codes)

    def test_truce_reachable_from_tstate_is_rejected_but_dialog_action_is_allowed(
        self,
    ) -> None:
        data = deepcopy(SAFE_RSON)
        group = data["Visual.Objects"][0]
        group["Operations"].append(
            {
                "Type": "Top",
                "Name": "StateReconcile",
                "Parent": -1,
                "#": 10,
                "Code.Type": "Turn",
                "Code": [
                    "function ReconcileShips()",
                    "{",
                    "    TruceBetweenShips(Player(), CurShip);",
                    "}",
                    "if(GetData(3) == 1) exit;",
                    "SetData(1, 3);",
                    "ReconcileShips();",
                    "SetData(0, 3);",
                ],
            }
        )
        data["Visual.Links"] = [
            {
                "Type": "TGraphLink",
                "Begin": 3,
                "End": 10,
                "Nom": 0,
                "Arrow": True,
            }
        ]
        matching = [
            issue
            for issue in lint_rson_runtime(
                RsonProject(data, Path("state-truce.rson"))
            )
            if issue.code == "runtime-truce-state-reentry-cycle"
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].severity, "error")
        self.assertIn("ReconcileShips", matching[0].message)

        group["Dialogs"] = [
            {
                "Type": "TDialogAnswer",
                "Name": "Sorry",
                "Parent": -1,
                "#": 11,
                "AMsg.Num": "0",
                "Msg": "Sorry",
            }
        ]
        data["Visual.Links"] = [
            {
                "Type": "TGraphLink",
                "Begin": 11,
                "End": 10,
                "Nom": 0,
                "Arrow": True,
            }
        ]
        safe_codes = {
            issue.code
            for issue in lint_rson_runtime(
                RsonProject(data, Path("dialog-truce.rson"))
            )
        }
        self.assertNotIn("runtime-truce-state-reentry-cycle", safe_codes)

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
            with self.assertRaisesRegex(
                ScriptBuildFailure,
                "runtime-linked-empty-code",
            ) as caught:
                cmd_script_build(args)
            report = caught.exception.as_dict()
            self.assertEqual(report["schema"], "srhd-modkit-script-build-v1")
            self.assertEqual(report["status"], "failed")
            self.assertFalse(report["preflight_passed"])
            self.assertFalse(report["compiler_started"])
            self.assertFalse(report["compiler_output_created"])
            self.assertEqual(
                report["failure"]["code"],
                "script-build-runtime-preflight-failed",
            )
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "script",
                        "build",
                        str(rson),
                        "--scr",
                        str(root / "json.scr"),
                        "--lang",
                        str(root / "json.lang"),
                        "--json",
                    ]
                )
            payload = json.loads(output.getvalue())
            self.assertEqual(exit_code, 1)
            self.assertEqual(payload["status"], "failed")
            self.assertFalse(payload["preflight_passed"])
            self.assertFalse(payload["compiler_output_created"])

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

    def test_onload_same_name_guard_is_not_reported_without_scr_comparison(self) -> None:
        document = parse_blockpar(
            "BV ^{\n"
            "  OnLoad ^{\n"
            "    Runtime ^{\n"
            "      01=if(!IsScriptActive('Mod_Worker')) "
            "ScriptRun(ShipStar(Player()), GetShipPlanet(Player()), 'Mod_Worker');\n"
            "    }\n"
            "  }\n"
            "}\n"
        )
        self.assertNotIn(
            "runtime-saved-script-cache-update-shadow",
            {issue.code for issue in lint_main_runtime(document, "Main.txt")},
        )

    def test_mod_owned_quest_item_without_image_is_deduplicated(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "dword first = CreateQuestItem('CargoType', 2);",
            "dword second = CreateQuestItem('CargoType', 2);",
        ]
        language = parse_blockpar(
            "UselessItems ^{\n"
            "  CargoType ^{\n"
            "    Name=Cargo\n"
            "  }\n"
            "}\n"
        )
        project = RsonProject(data, Path("quest-item.rson"))
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            issues = lint_quest_item_images(
                root,
                (project,),
                (),
                {"rus": ((Path("Lang_Rus.txt"), language),)},
            )
            matching = [
                issue
                for issue in issues
                if issue.code == "runtime-quest-item-image-missing"
            ]
            self.assertEqual(len(matching), 1)
            self.assertIn("2 вызовах", matching[0].message)
            self.assertIn("Usl_FishCont", matching[0].message)

            cache = parse_blockpar(
                "Bm ^{\n"
                "  ItemsUseless ^{\n"
                "    2CargoType_s=DATA\\ItemsUseless\\2CargoType.gi\n"
                "  }\n"
                "}\n"
            )
            self.assertEqual(
                lint_quest_item_images(
                    root,
                    (project,),
                    ((Path("CacheData.txt"), cache),),
                    {"rus": ((Path("Lang_Rus.txt"), language),)},
                ),
                [],
            )

    def test_quest_item_requires_exact_static_cache_key_even_with_gi(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "CreateQuestItem('CargoType', 2);",
        ]
        language = parse_blockpar(
            "UselessItems ^{\n"
            "  CargoType ^{\n"
            "    Name=Cargo\n"
            "  }\n"
            "}\n"
        )
        project = RsonProject(data, Path("quest-item-key.rson"))
        wrong_cache = parse_blockpar(
            "Bm ^{\n"
            "  ItemsUseless ^{\n"
            "    2CargoType=DATA\\ItemsUseless\\2CargoType.gi\n"
            "  }\n"
            "}\n"
        )
        wrong_variant_cache = parse_blockpar(
            "Bm ^{\n"
            "  ItemsUseless ^{\n"
            "    2CargoType_c=DATA\\ItemsUseless\\2CargoType.gai\n"
            "    3CargoType_s=DATA\\ItemsUseless\\2CargoType.gi\n"
            "  }\n"
            "}\n"
        )
        correct_cache = parse_blockpar(
            "Bm ^{\n"
            "  ItemsUseless ^{\n"
            "    2CargoType_s=DATA\\ItemsUseless\\2CargoType.gi\n"
            "  }\n"
            "}\n"
        )
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            image = root / "DATA" / "ItemsUseless" / "2CargoType.gi"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"validity is checked by the resource audit")

            issues = lint_quest_item_images(
                root,
                (project,),
                ((Path("CacheData.txt"), wrong_cache),),
                {"rus": ((Path("Lang_Rus.txt"), language),)},
            )
            self.assertEqual(len(issues), 1)
            self.assertEqual(
                issues[0].code,
                "runtime-quest-item-image-registration-key-invalid",
            )
            self.assertIn("2CargoType_s", issues[0].message)
            self.assertIn("сам себя не регистрирует", issues[0].message)
            self.assertIn("Usl_FishCont", issues[0].message)

            variant_issues = lint_quest_item_images(
                root,
                (project,),
                ((Path("CacheData.txt"), wrong_variant_cache),),
                {"rus": ((Path("Lang_Rus.txt"), language),)},
            )
            self.assertEqual(len(variant_issues), 1)
            self.assertIn("2CargoType_c", variant_issues[0].message)
            self.assertIn("3CargoType_s", variant_issues[0].message)

            source_cache_path = root / "SOURCE" / "CFG" / "CacheData.txt"
            final_cache_path = root / "CFG" / "CacheData.dat"
            self.assertEqual(
                lint_quest_item_images(
                    root,
                    (project,),
                    (
                        (source_cache_path, correct_cache),
                        (final_cache_path, wrong_cache),
                    ),
                    {"rus": ((Path("Lang_Rus.txt"), language),)},
                )[0].code,
                "runtime-quest-item-image-registration-key-invalid",
            )
            self.assertEqual(
                lint_quest_item_images(
                    root,
                    (project,),
                    (
                        (source_cache_path, wrong_cache),
                        (final_cache_path, correct_cache),
                    ),
                    {"rus": ((Path("Lang_Rus.txt"), language),)},
                ),
                [],
            )

    def test_quest_item_static_target_is_verified_without_requiring_external_assets(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "CreateQuestItem('CargoType', 2);",
        ]
        language = parse_blockpar(
            "UselessItems ^{\n"
            "  CargoType ^{\n"
            "    Name=Cargo\n"
            "  }\n"
            "}\n"
        )
        project = RsonProject(data, Path("quest-item-target.rson"))

        def cache(value: str):
            return parse_blockpar(
                "Bm ^{\n"
                "  ItemsUseless ^{\n"
                f"    2CargoType_s={value}\n"
                "  }\n"
                "}\n"
            )

        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "QuestFixture"
            root.mkdir()
            (root / "ModuleInfo.txt").write_text(
                "Name=QuestFixture\n"
                "Section=Test\n"
                "Languages=Rus\n"
                "Dependence=SharedIcons\n",
                encoding="cp1251",
            )
            image = root / "DATA" / "ItemsUseless" / "Cargo.gi"
            image.parent.mkdir(parents=True)
            image.write_bytes(
                encode_gi(
                    RgbaImage(2, 2, bytes((20, 40, 60, 255)) * 4),
                    "2",
                )
            )
            common = (
                (project,),
                {"rus": ((Path("Lang_Rus.txt"), language),)},
            )

            own = cache(
                r"Mods\OtherMods\QuestFixture\DATA\ItemsUseless\Cargo.gi"
            )
            self.assertEqual(
                lint_quest_item_images(
                    root,
                    common[0],
                    ((Path("CacheData.txt"), own),),
                    common[1],
                ),
                [],
            )

            image.write_bytes(
                encode_gi(
                    RgbaImage(2, 2, bytes((20, 40, 60, 255)) * 4),
                    "0_32",
                )
            )
            layout_issues = lint_quest_item_images(
                root,
                common[0],
                ((Path("CacheData.txt"), own),),
                common[1],
            )
            self.assertEqual(len(layout_issues), 1)
            self.assertEqual(
                layout_issues[0].code,
                "runtime-quest-item-image-layout-atypical",
            )
            self.assertEqual(layout_issues[0].severity, "warning")
            self.assertIn("--mode 2", layout_issues[0].message)

            missing = cache(
                r"Mods\OtherMods\QuestFixture\DATA\ItemsUseless\Missing.gi"
            )
            missing_issues = lint_quest_item_images(
                root,
                common[0],
                ((Path("CacheData.txt"), missing),),
                common[1],
            )
            self.assertEqual(
                missing_issues[0].code,
                "runtime-quest-item-image-target-missing",
            )
            self.assertEqual(missing_issues[0].severity, "warning")

            image.write_bytes(b"not a gi")
            invalid_issues = lint_quest_item_images(
                root,
                common[0],
                ((Path("CacheData.txt"), own),),
                common[1],
            )
            self.assertEqual(
                invalid_issues[0].code,
                "runtime-quest-item-image-target-invalid",
            )

            base = cache(r"DATA\ItemsUseless\2Usl_FishCont.gi")
            dependency = cache(
                r"Mods\OtherMods\SharedIcons\DATA\ItemsUseless\Cargo.gi"
            )
            for document in (base, dependency):
                self.assertEqual(
                    lint_quest_item_images(
                        root,
                        common[0],
                        ((Path("CacheData.txt"), document),),
                        common[1],
                    ),
                    [],
                )

            undeclared = cache(
                r"Mods\OtherMods\AccidentalIcons\DATA\ItemsUseless\Cargo.gi"
            )
            undeclared_issues = lint_quest_item_images(
                root,
                common[0],
                ((Path("CacheData.txt"), undeclared),),
                common[1],
            )
            self.assertEqual(
                undeclared_issues[0].code,
                "runtime-quest-item-image-target-external-undeclared",
            )

            animated = cache(
                r"Mods\OtherMods\QuestFixture\DATA\ItemsUseless\Cargo.gai"
            )
            animated_issues = lint_quest_item_images(
                root,
                common[0],
                ((Path("CacheData.txt"), animated),),
                common[1],
            )
            self.assertEqual(
                animated_issues[0].code,
                "runtime-quest-item-image-target-format-invalid",
            )

    def test_shared_player_and_npc_state_requires_curship_separation(self) -> None:
        data = deepcopy(SAFE_RSON)
        group = data["Visual.Objects"][0]
        group["Groups"] = [
            {"Type": "TGroup", "Name": "NPCs", "AddPlayer": False, "#": 10},
            {"Type": "TGroup", "Name": "RuntimePlayer", "AddPlayer": True, "#": 11},
        ]
        group["Operations"].append(
            {
                "Type": "Top",
                "Name": "StateRuntime",
                "Code.Type": "Turn",
                "Code": ["ShipOwner(CurShip, 2);"],
                "#": 12,
            }
        )
        data["Visual.Links"] = [
            {"Type": "TGraphLink", "Begin": 10, "End": 3, "Nom": 0, "Arrow": True},
            {"Type": "TGraphLink", "Begin": 11, "End": 3, "Nom": 0, "Arrow": True},
            {"Type": "TGraphLink", "Begin": 3, "End": 12, "Nom": 0, "Arrow": True},
        ]
        project = RsonProject(data, Path("shared-player-state.rson"))
        self.assertIn(
            "runtime-shared-state-mutates-player",
            {issue.code for issue in lint_rson_runtime(project)},
        )

        group["Operations"][-1]["Code"] = [
            "if(CurShip == Player()) exit;",
            "ShipOwner(CurShip, 2);",
        ]
        self.assertNotIn(
            "runtime-shared-state-mutates-player",
            {issue.code for issue in lint_rson_runtime(project)},
        )
        group["Operations"][-1]["Code"] = [
            "if(CurShip != Player() || allow_player) ShipOwner(CurShip, 2);",
        ]
        self.assertIn(
            "runtime-shared-state-mutates-player",
            {issue.code for issue in lint_rson_runtime(project)},
        )

    def test_recurring_state_shipbad_write_requires_value_change_guard(self) -> None:
        data = deepcopy(SAFE_RSON)
        group = data["Visual.Objects"][0]
        group["Groups"] = [
            {"Type": "TGroup", "Name": "NPCs", "AddPlayer": False, "#": 10},
        ]
        group["Operations"].append(
            {
                "Type": "Top",
                "Name": "StateRuntime",
                "Code.Type": "Turn",
                "Code": ["ShipSetBad(CurShip, 0);"],
                "#": 12,
            }
        )
        data["Visual.Links"] = [
            {"Type": "TGraphLink", "Begin": 10, "End": 3, "Nom": 0, "Arrow": True},
            {"Type": "TGraphLink", "Begin": 3, "End": 12, "Nom": 0, "Arrow": True},
        ]
        project = RsonProject(data, Path("state-shipbad.rson"))
        self.assertIn(
            "runtime-state-unconditional-shipbad-write",
            {issue.code for issue in lint_rson_runtime(project)},
        )

        group["Operations"][-1]["Code"] = [
            "if(ShipGetBad(CurShip)) ShipSetBad(CurShip, 0);",
        ]
        self.assertNotIn(
            "runtime-state-unconditional-shipbad-write",
            {issue.code for issue in lint_rson_runtime(project)},
        )
        group["Operations"][-1]["Code"] = [
            "if(force_clear || ShipGetBad(CurShip)) ShipSetBad(CurShip, 0);",
        ]
        self.assertIn(
            "runtime-state-unconditional-shipbad-write",
            {issue.code for issue in lint_rson_runtime(project)},
        )

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

    def test_forced_pickup_transfer_requires_marker_cleanup(self) -> None:
        def matching(body: list[str]):
            data = deepcopy(SAFE_RSON)
            data["Visual.Objects"][0]["Operations"][0]["Code"].extend(
                ["function TakeMarkedLoot(dword ship, dword star, dword item)", "{", *body, "}"]
            )
            return [
                issue
                for issue in lint_rson_runtime(
                    RsonProject(data, Path("pickup-marker.rson"))
                )
                if issue.code == "runtime-shippicksitem-stale-after-forced-transfer"
            ]

        unsafe = matching(
            [
                "    ShipPicksItem(ship, item, 1);",
                "    dword taken = GetItemFromStar(star, item);",
                "    if(taken) AddItemToShip(ship, taken);",
                "    exit;",
            ]
        )
        self.assertEqual(len(unsafe), 1)
        self.assertEqual(unsafe[0].severity, "warning")
        self.assertEqual(unsafe[0].evidence, "if(taken) AddItemToShip(ship, taken);")

        safe_after = matching(
            [
                "    ShipPicksItem(ship, item, 1);",
                "    dword taken = GetItemFromStar(star, item);",
                "    if(taken) AddItemToShip(ship, taken);",
                "    ShipPicksItem(ship, item, 0);",
            ]
        )
        self.assertEqual(safe_after, [])

        safe_before = matching(
            [
                "    ShipPicksItem(ship, item, 1);",
                "    ShipPicksItem(ship, item, 0);",
                "    dword taken = GetItemFromStar(star, item);",
                "    if(taken) AddItemToShip(ship, taken);",
            ]
        )
        self.assertEqual(safe_before, [])

        vanilla_pickup = matching(
            [
                "    ShipPicksItem(ship, item, 1);",
                "    ShipFreeFlight(ship);",
            ]
        )
        self.assertEqual(vanilla_pickup, [])

        wrong_item_cleanup = matching(
            [
                "    ShipPicksItem(ship, item, 1);",
                "    dword taken = GetItemFromStar(star, item);",
                "    if(taken) AddItemToShip(ship, taken);",
                "    ShipPicksItem(ship, other_item, 0);",
            ]
        )
        self.assertEqual(len(wrong_item_cleanup), 1)

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

    def test_growing_membership_scan_inside_world_pair_is_warning(self) -> None:
        data = deepcopy(SAFE_RSON)
        turn = data["Visual.Objects"][0]["Operations"][1]["Code"]
        turn.extend(
            [
                "function ResolveStar(int wanted_id)",
                "{",
                "    result = 0;",
                "    for(int resolve_i = 0; resolve_i < GalaxyStars(); resolve_i = resolve_i + 1)",
                "    {",
                "        dword resolve_star = GalaxyStar(resolve_i);",
                "        if(resolve_star && Id(resolve_star) == wanted_id) { result = resolve_star; exit; }",
                "    }",
                "}",
                "function BuildRoute()",
                "{",
                "    unknown queue_ids = newarray(1);",
                "    ArrayClear(queue_ids);",
                "    int star_count = GalaxyStars();",
                "    ArrayAdd(queue_ids, 1);",
                "    for(int node = 1; node <= star_count; node = node + 1)",
                "    {",
                "        dword current = ResolveStar(queue_ids[node]);",
                "        for(int candidate = 0; candidate < GalaxyStars(); candidate = candidate + 1)",
                "        {",
                "            int already_seen = 0;",
                "            for(int seen = 1; seen < ArrayDim(queue_ids); seen = seen + 1)",
                "            {",
                "                if(queue_ids[seen] == Id(GalaxyStar(candidate))) already_seen = 1;",
                "            }",
                "            if(!already_seen) ArrayAdd(queue_ids, Id(GalaxyStar(candidate)));",
                "        }",
                "    }",
                "}",
                "BuildRoute();",
            ]
        )
        issues = lint_rson_runtime(RsonProject(data, Path("cubic-route.rson")))
        membership = next(
            item for item in issues if item.code == "runtime-hot-growing-membership-scan"
        )
        hidden = next(
            item
            for item in issues
            if item.code == "runtime-user-function-world-loop-cost-propagation"
        )
        self.assertEqual(membership.severity, "warning")
        self.assertIn("O(S^3)", membership.message)
        self.assertIn("Turn -> BuildRoute", membership.message)
        self.assertEqual(hidden.severity, "info")
        self.assertIn("BuildRoute -> ResolveStar", hidden.message)
        self.assertIn("O(S^2)", hidden.message)

    def test_flat_world_pair_pass_is_not_reported_as_cubic(self) -> None:
        data = deepcopy(SAFE_RSON)
        turn = data["Visual.Objects"][0]["Operations"][1]["Code"]
        turn.extend(
            [
                "function BuildMatrix()",
                "{",
                "    int star_count = GalaxyStars();",
                "    int pair_limit = star_count * star_count;",
                "    for(int pair = 0; pair < pair_limit; pair = pair + 1)",
                "    {",
                "        int left = pair / star_count;",
                "        int right = pair - left * star_count;",
                "        dword left_star = GalaxyStar(left);",
                "        dword right_star = GalaxyStar(right);",
                "    }",
                "}",
                "BuildMatrix();",
            ]
        )
        issues = lint_rson_runtime(RsonProject(data, Path("flat-pair.rson")))
        hot_codes = {
            item.code
            for item in issues
            if item.code.startswith("runtime-hot-")
            or item.code == "runtime-user-function-world-loop-cost-propagation"
        }
        self.assertEqual(hot_codes, set())

    def test_world_scan_helper_inside_flat_world_pair_warns_about_cubic_cost(self) -> None:
        data = deepcopy(SAFE_RSON)
        turn = data["Visual.Objects"][0]["Operations"][1]["Code"]
        turn.extend(
            [
                "function ResolveStar(int wanted_id)",
                "{",
                "    result = 0;",
                "    for(int resolve_i = 0; resolve_i < GalaxyStars(); resolve_i = resolve_i + 1)",
                "    {",
                "        dword resolve_star = GalaxyStar(resolve_i);",
                "        if(resolve_star && Id(resolve_star) == wanted_id) { result = resolve_star; exit; }",
                "    }",
                "}",
                "function BuildMatrix()",
                "{",
                "    int star_count = GalaxyStars();",
                "    int pair_limit = star_count * star_count;",
                "    for(int pair = 0; pair < pair_limit; pair = pair + 1)",
                "    {",
                "        dword star = ResolveStar(pair);",
                "    }",
                "}",
                "BuildMatrix();",
            ]
        )
        issues = lint_rson_runtime(RsonProject(data, Path("hidden-cubic.rson")))
        hidden = next(
            item
            for item in issues
            if item.code == "runtime-user-function-world-loop-cost-propagation"
        )
        self.assertEqual(hidden.severity, "warning")
        self.assertIn("O(S^3)", hidden.message)
        self.assertIn("Turn -> BuildMatrix -> ResolveStar", hidden.message)

    def test_unreachable_cubic_helper_does_not_create_hot_path_warning(self) -> None:
        data = deepcopy(SAFE_RSON)
        turn = data["Visual.Objects"][0]["Operations"][1]["Code"]
        turn.extend(
            [
                "function UnusedRouteBuilder()",
                "{",
                "    unknown queue_ids = newarray(1);",
                "    int star_count = GalaxyStars();",
                "    for(int node = 0; node < star_count; node = node + 1)",
                "    {",
                "        for(int candidate = 0; candidate < GalaxyStars(); candidate = candidate + 1)",
                "        {",
                "            for(int seen = 1; seen < ArrayDim(queue_ids); seen = seen + 1)",
                "            {",
                "                if(queue_ids[seen] == candidate) result = 1;",
                "            }",
                "            ArrayAdd(queue_ids, candidate);",
                "        }",
                "    }",
                "}",
            ]
        )
        issues = lint_rson_runtime(RsonProject(data, Path("unused-cubic.rson")))
        self.assertFalse(
            any(
                item.code in {
                    "runtime-hot-growing-membership-scan",
                    "runtime-user-function-world-loop-cost-propagation",
                }
                for item in issues
            )
        )

    def test_rescaling_world_count_does_not_invent_another_dimension(self) -> None:
        data = deepcopy(SAFE_RSON)
        turn = data["Visual.Objects"][0]["Operations"][1]["Code"]
        turn.extend(
            [
                "function ScanWorld()",
                "{",
                "    for(int scan_i = 0; scan_i < GalaxyStars(); scan_i = scan_i + 1)",
                "    {",
                "        dword scan_star = GalaxyStar(scan_i);",
                "    }",
                "}",
                "function RescaledPass()",
                "{",
                "    int star_count = GalaxyStars();",
                "    star_count = star_count * 2;",
                "    for(int i = 0; i < star_count; i = i + 1)",
                "    {",
                "        ScanWorld();",
                "    }",
                "}",
                "RescaledPass();",
            ]
        )
        issues = lint_rson_runtime(RsonProject(data, Path("rescaled-world.rson")))
        hidden = next(
            item
            for item in issues
            if item.code == "runtime-user-function-world-loop-cost-propagation"
        )
        self.assertEqual(hidden.severity, "info")
        self.assertIn("O(S^2)", hidden.message)

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

    def test_runtime_target_checks_literal_ct_keys_in_declared_languages(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "SOURCE"
            cfg = source / "CFG"
            cfg.mkdir(parents=True)
            data = deepcopy(SAFE_RSON)
            data["Visual.Objects"][0]["Operations"][1]["Code"] = [
                "AddPlanetNews(CT('OwnMod.Missing'));",
            ]
            (source / "literal-ct.rson").write_text(json.dumps(data), encoding="utf-8")
            (root / "ModuleInfo.txt").write_text(
                "Name=Test\nLanguages=Rus,Eng\n",
                encoding="utf-8",
            )
            (cfg / "Lang_Rus.txt").write_text(
                "OwnMod ^{\n    Existing=Есть\n}\n",
                encoding="utf-8",
            )
            (cfg / "Lang_Eng.txt").write_text(
                "OwnMod ^{\n    Existing=Exists\n    Missing=Ready\n}\n",
                encoding="utf-8",
            )

            result = _runtime_lint_target(root)
            codes = [issue["code"] for issue in result["issues"]]
            self.assertEqual(codes.count("runtime-ct-key-missing"), 1)
            self.assertEqual(codes.count("runtime-empty-text-to-nonempty-sink"), 1)
            self.assertEqual(len(result["language"]), 2)

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
        self.assertIn("runtime-shipstar-on-docked-ship", chain_codes)
        self.assertIn("runtime-object-api-behind-boolean-guard", chain_codes)

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

    def test_partial_storage_initializer_does_not_borrow_newarray_from_migration(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Variables"] = [
            {"Type": "TVar", "Name": "tank_slots", "Parent": -1, "#": 20},
            {"Type": "TVar", "Name": "tank_item_ids", "Parent": -1, "#": 21},
        ]
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "function MigrateOldSave()",
            "{",
            "    tank_slots = newarray(1);",
            "    tank_item_ids = newarray(1);",
            "}",
            "function InitStorage()",
            "{",
            "    tank_slots = newarray(1);",
            "    ArrayClear(tank_slots);",
            "    ArrayClear(tank_item_ids);",
            "}",
            "InitStorage();",
        ]
        issues = lint_rson_runtime(RsonProject(data, Path("partial-storage.rson")))
        matching = [
            issue
            for issue in issues
            if issue.code == "runtime-persistent-array-use-before-newarray"
        ]
        self.assertEqual(len(matching), 1)
        self.assertIn("tank_item_ids", matching[0].message)

        data["Visual.Objects"][0]["Operations"][1]["Code"].insert(
            8, "    tank_item_ids = newarray(1);"
        )
        fixed_codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("complete-storage.rson")))
        }
        self.assertNotIn("runtime-persistent-array-use-before-newarray", fixed_codes)

    def test_conditional_newarray_does_not_dominate_unconditional_arrayclear(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Variables"] = [
            {"Type": "TVar", "Name": "queue", "Parent": -1, "#": 20},
            {"Type": "TVar", "Name": "other", "Parent": -1, "#": 21},
        ]
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "function InitStorage()",
            "{",
            "    other = newarray(1);",
            "    if(reuse_old)",
            "    {",
            "        queue = newarray(1);",
            "    }",
            "    ArrayClear(queue);",
            "}",
        ]
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("conditional-array.rson")))
        }
        self.assertIn("runtime-persistent-array-use-before-newarray", codes)

    def test_fixed_array_slots_must_be_typed_before_numeric_read(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Variables"] = [
            {"Type": "TVar", "Name": "ship_ids", "Parent": -1, "#": 20},
        ]
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "function ResetIds()",
            "{",
            "    ship_ids = newarray(7);",
            "}",
            "function HasId(int wanted)",
            "{",
            "    result = 0;",
            "    for(int i = 1; i <= 6; i = i + 1)",
            "        if(ship_ids[i] == wanted) { result = 1; exit; }",
            "}",
        ]
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("untyped-fixed.rson")))
        }
        self.assertIn("runtime-fixed-array-untyped-slot", codes)

        data["Visual.Objects"][0]["Operations"][1]["Code"][3:3] = [
            "    for(int reset_i = 0; reset_i <= 6; reset_i = reset_i + 1)",
            "        ship_ids[reset_i] = 0;",
        ]
        fixed_codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("typed-fixed.rson")))
        }
        self.assertNotIn("runtime-fixed-array-untyped-slot", fixed_codes)

    def test_fixed_array_direct_float_and_string_writes_are_typed(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Variables"] = [
            {"Type": "TVar", "Name": "values", "Parent": -1, "#": 20},
        ]
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "values = newarray(3);",
            "values[0] = 0.6;",
            'values[1] = "ready";',
            'values[2] = CT("Script.RuntimeSafe.1");',
            "result = values[0];",
            "result = values[1];",
            "result = values[2];",
        ]
        codes = {
            issue.code
            for issue in lint_rson_runtime(
                RsonProject(data, Path("typed-direct-fixed.rson"))
            )
        }
        self.assertNotIn("runtime-fixed-array-untyped-slot", codes)

    def test_fixed_array_index_contract_uses_known_loop_bound(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Variables"] = [
            {"Type": "TVar", "Name": "ship_ids", "Parent": -1, "#": 20},
            {"Type": "TVar", "Name": "ship_count", "Init": "6", "Parent": -1, "#": 21},
        ]
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "function FillIds()",
            "{",
            "    ship_ids = newarray(7);",
            "    for(int reset_i = 0; reset_i <= 6; reset_i = reset_i + 1)",
            "        ship_ids[reset_i] = 0;",
            "    for(int ship_index = 1; ship_index <= ship_count; ship_index = ship_index + 1)",
            "        ship_ids[ship_index + 1] = ship_index;",
            "}",
        ]
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("fixed-oob.rson")))
        }
        self.assertIn("runtime-fixed-array-index-contract", codes)

    def test_fixed_array_loop_must_not_include_arraydim_index(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Variables"] = [
            {"Type": "TVar", "Name": "transport_ids", "Parent": -1, "#": 20},
        ]
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "function ResetIds()",
            "{",
            "    transport_ids = newarray(7);",
            "    for(int reset_i = 0; reset_i < ArrayDim(transport_ids); reset_i = reset_i + 1)",
            "        transport_ids[reset_i] = 0;",
            "}",
            "function IsTransportId(int wanted)",
            "{",
            "    result = 0;",
            "    for(int i = 1; i <= ArrayDim(transport_ids); i = i + 1)",
            "        if(transport_ids[i] == wanted) { result = 1; exit; }",
            "}",
        ]
        issues = lint_rson_runtime(RsonProject(data, Path("arraydim-oob.rson")))
        matching = [
            issue
            for issue in issues
            if issue.code == "runtime-fixed-array-index-contract"
        ]
        self.assertEqual(len(matching), 1)
        self.assertIn("последний индекс", matching[0].message)

        data["Visual.Objects"][0]["Operations"][1]["Code"][9] = (
            "    for(int i = 1; i < ArrayDim(transport_ids); i = i + 1)"
        )
        fixed_codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("arraydim-safe.rson")))
        }
        self.assertNotIn("runtime-fixed-array-index-contract", fixed_codes)

    def test_runtime_persistent_fixed_array_terminal_slot_is_cross_scope_advisory(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Variables"] = [
            {"Type": "TVar", "Name": "transport_ids", "Parent": -1, "#": 20},
        ]
        init = data["Visual.Objects"][0]["Operations"][0]
        init["Code.Type"] = "Init"
        init["Code"] = [
            "function ResetIds()",
            "{",
            "    transport_ids = newarray(7);",
            "    for(int i = 0; i <= 6; i = i + 1)",
            "        transport_ids[i] = 0;",
            "    if(transport_ids[6]) result = 1;",
            "}",
        ]
        data["Visual.Objects"][0]["Operations"][1]["Code"] = ["ResetIds();"]
        same_scope = [
            issue
            for issue in lint_rson_runtime(RsonProject(data, Path("terminal-slot.rson")))
            if issue.code == "runtime-persistent-fixed-array-terminal-slot"
        ]
        self.assertEqual(same_scope, [])

        init["Code"].extend(
            [
                "function IsTransportId(int wanted)",
                "{",
                "    result = 0;",
                "    for(int read_i = 1; read_i <= 6; read_i = read_i + 1)",
                "        if(transport_ids[read_i] == wanted) { result = 1; exit; }",
                "}",
            ]
        )
        data["Visual.Objects"][0]["Operations"][1]["Code"].append(
            "IsTransportId(42);"
        )
        cross_scope = [
            issue
            for issue in lint_rson_runtime(RsonProject(data, Path("terminal-cross-scope.rson")))
            if issue.code == "runtime-persistent-fixed-array-terminal-slot"
        ]
        self.assertEqual(len(cross_scope), 1)
        self.assertEqual(cross_scope[0].severity, "warning")

        init["Code"][2] = (
            "    transport_ids = newarray(8);"
        )
        reserved_codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("terminal-spare.rson")))
        }
        self.assertNotIn("runtime-persistent-fixed-array-terminal-slot", reserved_codes)

    def test_dynamic_persistent_array_live_dimension_drift_is_reported(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Variables"] = [
            {"Type": "TVar", "Name": "ship_ids", "Parent": -1, "#": 20},
        ]
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "function ResetIds()",
            "{",
            "    ship_ids = newarray(1);",
            "    ArrayClear(ship_ids);",
            "    ArrayAdd(ship_ids, 7);",
            "}",
            "for(int i = 1; i < ArrayDim(ship_ids); i = i + 1)",
            "    if(ship_ids[i] == 7) exit;",
        ]
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("persistent-drift.rson")))
        }
        self.assertIn("runtime-persistent-array-live-dimension-drift", codes)

    def test_literal_ct_keys_are_checked_in_every_language_and_fatal_sink(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "AddPlanetNews(Format(CT('OwnMod.Missing'), '#name#', Name(Player())));",
            "DText(CT('BaseGame.ExternalKey'));",
        ]
        project = RsonProject(data, Path("ct-missing.rson"))
        russian = parse_blockpar(
            "OwnMod ^{\n    Existing=Есть\n}\n"
        )
        english = parse_blockpar(
            "OwnMod ^{\n    Existing=Exists\n    Missing=Ready\n}\n"
        )
        issues = lint_literal_ct_keys(
            [project],
            {
                "rus": [(Path("Lang_Rus.txt"), russian)],
                "eng": [(Path("Lang_Eng.txt"), english)],
            },
        )
        codes = [issue.code for issue in issues]
        self.assertEqual(codes.count("runtime-ct-key-missing"), 1)
        self.assertEqual(codes.count("runtime-empty-text-to-nonempty-sink"), 1)
        self.assertIn("rus:Lang_Rus.txt", issues[0].message)
        self.assertNotIn("BaseGame.ExternalKey", "\n".join(issue.message for issue in issues))

        russian.find_node("OwnMod").set_parameter("Missing", "Готово", create=True)
        self.assertEqual(
            lint_literal_ct_keys(
                [project],
                {
                    "rus": [(Path("Lang_Rus.txt"), russian)],
                    "eng": [(Path("Lang_Eng.txt"), english)],
                },
            ),
            [],
        )

    def test_nested_localization_wrappers_are_rejected_without_cross_wrapper_noise(self) -> None:
        data = deepcopy(SAFE_RSON)
        group = data["Visual.Objects"][0]
        group["Operations"][1]["Code"] = [
            'result = CT(CT("OwnMod.Button"));',
            '// CT(CT("OwnMod.Comment"))',
        ]
        group["Dialogs"] = [
            {
                "Type": "TDialogMsg",
                "Name": "Message",
                "Parent": -1,
                "#": 30,
                "Msg": 'DAnswer(DAnswer(CT("OwnMod.Answer")));',
            }
        ]
        issues = [
            issue
            for issue in lint_rson_runtime(RsonProject(data, Path("nested-lang.rson")))
            if issue.code == "runtime-nested-localization-wrapper"
        ]
        self.assertEqual(len(issues), 2)
        self.assertTrue(any("CT(CT" in issue.message for issue in issues))
        self.assertTrue(any("DAnswer(DAnswer" in issue.message for issue in issues))

        group["Operations"][1]["Code"] = ['result = DAnswer(CT("OwnMod.Answer"));']
        group["Dialogs"][0]["Msg"] = 'DAnswer(CT("OwnMod.Answer"));'
        safe_codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("single-lang.rson")))
        }
        self.assertNotIn("runtime-nested-localization-wrapper", safe_codes)

    def test_special_shipowner_is_rejected_for_guarded_ranger(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "dword ship = Player();",
            "if(ShipTypeN(ship) == t_Ranger)",
            "{",
            "    ShipOwner(ship, Kling);",
            "}",
        ]
        issues = [
            issue
            for issue in lint_rson_runtime(RsonProject(data, Path("ranger-owner.rson")))
            if issue.code == "runtime-shipowner-class-discriminator-mismatch"
        ]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, "error")
        self.assertIn("ShipStanding", issues[0].message)

    def test_ranger_type_proof_flows_through_user_functions(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "function MarkRanger(dword ranger)",
            "{",
            "    ShipOwner(ranger, 5);",
            "}",
            "function PassRanger(dword ranger)",
            "{",
            "    MarkRanger(ranger);",
            "}",
            "function CheckShip(dword ship)",
            "{",
            "    if(ShipTypeN(ship) != t_Ranger || !ship) exit;",
            "    PassRanger(ship);",
            "}",
            "CheckShip(Player());",
        ]
        issues = [
            issue
            for issue in lint_rson_runtime(RsonProject(data, Path("ranger-callgraph.rson")))
            if issue.code == "runtime-shipowner-class-discriminator-mismatch"
        ]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].evidence, "ShipOwner(ranger, 5);")

    def test_ranger_factories_prove_shipowner_class_mismatch(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "dword bought = BuyRanger(star, 1, 1, 1);",
            "ShipOwner(bought, None);",
            "dword existing = GalaxyRangers(index);",
            "ShipOwner(existing, PirateClan);",
            "dword player = Player();",
            "ShipOwner(player, 5);",
        ]
        matching = [
            issue
            for issue in lint_rson_runtime(RsonProject(data, Path("ranger-factory.rson")))
            if issue.code == "runtime-shipowner-class-discriminator-mismatch"
        ]
        self.assertEqual(len(matching), 3)
        self.assertTrue(any("None" in (issue.evidence or "") for issue in matching))
        self.assertTrue(any("PirateClan" in (issue.evidence or "") for issue in matching))

    def test_shipowner_rule_keeps_racial_and_unproven_changes_available(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "dword ranger = BuyRanger(star, 1, 1, 1);",
            "ShipOwner(ranger, 4);",
            "ShipOwner(ranger, racial_owner);",
            "ShipStanding(ranger, Kling);",
            "dword unknown_ship = candidate;",
            "ShipOwner(unknown_ship, None);",
        ]
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("owner-safe.rson")))
        }
        self.assertNotIn("runtime-shipowner-class-discriminator-mismatch", codes)

    def test_conditional_ranger_factory_does_not_prove_all_paths(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "dword ship = candidate;",
            "if(use_new_ranger)",
            "{",
            "    ship = BuyRanger(star, 1, 1, 1);",
            "}",
            "ShipOwner(ship, 5);",
        ]
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("conditional-ranger.rson")))
        }
        self.assertNotIn("runtime-shipowner-class-discriminator-mismatch", codes)

    def test_custom_faction_requires_registered_ship_emblem(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "ShipCustomFaction(",
            "    Player(),",
            "    'SubFactionFixture'",
            ");",
        ]
        project = RsonProject(data, Path("custom-faction.rson"))

        standalone = [
            issue
            for issue in lint_rson_runtime(project)
            if issue.code == "runtime-custom-faction-emblem-unregistered"
        ]
        self.assertEqual(len(standalone), 1)
        self.assertEqual(standalone[0].severity, "warning")
        self.assertIn("Main.dat не передан", standalone[0].message)

        missing_main = parse_blockpar("Data ^{\n  Race ^{\n    Emblem ~{\n    }\n  }\n}\n")
        missing = lint_custom_faction_resources((project,), (missing_main,))
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0].severity, "error")
        self.assertIn("Data/Race/Emblem/2SubFactionFixture", missing[0].message)

    def test_custom_faction_accepts_direct_and_nested_emblem_registration(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "ShipCustomFaction(Player(), 'SubFactionFixture');",
        ]
        project = RsonProject(data, Path("custom-faction.rson"))
        documents = (
            parse_blockpar(
                "Data ^{\n  Race ^{\n    Emblem ~{\n"
                "      2SubFactionFixture=Alpha,Bm.Race.Emblem.2Fixture\n"
                "    }\n  }\n}\n"
            ),
            parse_blockpar(
                "Data ^{\n  Race ^{\n    Emblem ~{\n"
                "      2SubFaction ^{\n"
                "        Fixture=Alpha,Bm.Race.Emblem.2Fixture\n"
                "      }\n"
                "    }\n  }\n}\n"
            ),
        )
        for document in documents:
            with self.subTest(document=document.to_text()):
                self.assertEqual(
                    lint_custom_faction_resources((project,), (document,)),
                    [],
                )

    def test_exact_resource_free_custom_faction_marker_is_not_generalized(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "ShipCustomFaction(Player(), '');",
            "ShipCustomFaction(Player(), 'SubFactionFixedStanding');",
            "ShipCustomFaction(Player(), 'PirateClan');",
        ]
        safe_codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("builtin-faction.rson")))
        }
        self.assertNotIn("runtime-custom-faction-emblem-unregistered", safe_codes)

        data["Visual.Objects"][0]["Operations"][1]["Code"].append(
            "ShipCustomFaction(Player(), 'SubFactionFixedStandingSuffix');"
        )
        issues = [
            issue
            for issue in lint_rson_runtime(RsonProject(data, Path("similar-faction.rson")))
            if issue.code == "runtime-custom-faction-emblem-unregistered"
        ]
        self.assertEqual(len(issues), 1)
        self.assertIn("2SubFactionFixedStandingSuffix", issues[0].message)

    def test_script_validate_reports_unverified_custom_faction(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            source = Path(name) / "custom.rson"
            data = deepcopy(SAFE_RSON)
            data["Visual.Objects"][0]["Operations"][1]["Code"] = [
                "ShipCustomFaction(Player(), 'SubFactionFixture');",
            ]
            source.write_text(json.dumps(data), encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                result = cmd_script_validate(
                    SimpleNamespace(source=str(source), json=True)
                )
            payload = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertTrue(payload["valid"])
            self.assertEqual(
                [issue["code"] for issue in payload["issues"]],
                ["runtime-custom-faction-emblem-unregistered"],
            )

    def test_unreachable_custom_faction_is_warning_with_main(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][0]["Code"].extend(
            [
                "function UnusedFaction(dword ship)",
                "{",
                "    ShipCustomFaction(ship, 'SubFactionUnused');",
                "}",
            ]
        )
        project = RsonProject(data, Path("unused-faction.rson"))
        main = parse_blockpar("Data ^{\n  Race ^{\n    Emblem ~{\n    }\n  }\n}\n")
        matching = lint_custom_faction_resources((project,), (main,))
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].severity, "warning")
        self.assertIn("недостижимы", matching[0].message)

    def test_build_blocks_missing_custom_faction_emblem_before_compiler(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "SOURCE"
            cfg = source / "CFG"
            cfg.mkdir(parents=True)
            data = deepcopy(SAFE_RSON)
            data["Visual.Objects"][0]["Operations"][1]["Code"] = [
                "ShipCustomFaction(Player(), 'SubFactionFixture');",
            ]
            rson = source / "custom.rson"
            rson.write_text(json.dumps(data), encoding="utf-8")
            (root / "ModuleInfo.txt").write_text(
                "Name=Test\nLanguages=Rus\n",
                encoding="utf-8",
            )
            (cfg / "Main.txt").write_text(
                "Data ^{\n  Race ^{\n    Emblem ~{\n    }\n  }\n}\n",
                encoding="utf-8",
            )
            args = SimpleNamespace(
                source=str(rson),
                scr=str(root / "out.scr"),
                lang=str(root / "out.lang"),
                lang_dat=None,
                lang_base=None,
                timeout=None,
                overwrite=False,
                tools_root=None,
                json=False,
            )
            with self.assertRaisesRegex(
                ValueError,
                "runtime-custom-faction-emblem-unregistered",
            ):
                cmd_script_build(args)

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
            "dword first = 1, selected = 2;\n"
            "int other = 3, selected = 4;\n"
        )
        comma_issues = lint_rson_runtime(RsonProject(data, Path("comma-locals.rson")))
        self.assertEqual(
            sum(
                issue.code == "runtime-duplicate-local-declaration"
                for issue in comma_issues
            ),
            1,
        )

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

    def test_unregistered_tvar_assignment_is_a_link_error(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "missing_storage_version = 1;"
        ]
        issues = lint_rson_runtime(RsonProject(data, Path("missing-tvar.rson")))
        matching = [
            issue
            for issue in issues
            if issue.code == "runtime-code-uses-unregistered-tvar"
        ]
        self.assertEqual(len(matching), 1)
        self.assertIn("missing_storage_version", matching[0].message)

        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "int local_storage_version = 0;",
            "local_storage_version = 1;",
        ]
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("local-var.rson")))
        }
        self.assertNotIn("runtime-code-uses-unregistered-tvar", codes)

    def test_object_api_guard_must_be_a_separate_statement(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "dword target = 0;",
            "if(!target || !ShipCanJump(Player(), ShipStar(Player()), target, 1)) exit;",
        ]
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("eager-bool.rson")))
        }
        self.assertIn("runtime-object-api-behind-boolean-guard", codes)

        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "dword target = 0;",
            "if(!target) exit;",
            "if(!ShipCanJump(Player(), ShipStar(Player()), target, 1)) exit;",
        ]
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("sequential-guard.rson")))
        }
        self.assertNotIn("runtime-object-api-behind-boolean-guard", codes)

    def test_nullable_engine_handles_require_explicit_guards(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "function CountPirateBases(dword ranger)",
            "{",
            "    result = 0;",
            "    for(int i = 0; i < GalaxyStars(); i = i + 1)",
            "    {",
            "        dword star = GalaxyStar(i);",
            "        dword base = StarRuins(star, 'PB');",
            "        if(base && ShipTypeN(base) == t_PB && RelationToRanger(base, ranger) >= 10)",
            "            result = result + 1;",
            "    }",
            "}",
        ]
        issues = lint_rson_runtime(RsonProject(data, Path("raw-handles.rson")))
        codes = {issue.code for issue in issues}
        self.assertIn("runtime-object-api-without-explicit-guard", codes)
        self.assertIn("runtime-object-api-behind-boolean-guard", codes)
        self.assertIn("runtime-redundant-star-ruins-type-dereference", codes)
        missing_guards = [
            issue
            for issue in issues
            if issue.code == "runtime-object-api-without-explicit-guard"
        ]
        self.assertTrue(any("starruins" in issue.message for issue in missing_guards))
        self.assertTrue(any("shiptypen" in issue.message for issue in missing_guards))

        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "function CountPirateBases(dword ranger)",
            "{",
            "    result = 0;",
            "    for(int i = 0; i < GalaxyStars(); i = i + 1)",
            "    {",
            "        dword star = GalaxyStar(i);",
            "        if(!star) continue;",
            "        dword base = StarRuins(star, 'PB');",
            "        if(!base) continue;",
            "        if(RelationToRanger(base, ranger) < 10) continue;",
            "        result = result + 1;",
            "    }",
            "}",
        ]
        safe_codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("guarded-handles.rson")))
        }
        self.assertNotIn("runtime-object-api-without-explicit-guard", safe_codes)
        self.assertNotIn("runtime-object-api-behind-boolean-guard", safe_codes)
        self.assertNotIn("runtime-redundant-star-ruins-type-dereference", safe_codes)

    def test_nullable_handle_rule_covers_nested_and_boolean_forms(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "dword base = StarRuins(GalaxyStar(i), 'PB');",
            "if(!base || ShipTypeN(base) != t_PB) exit;",
            "dword dom = StarRuins(known_star, 'PB');",
            "if(dom && ranger && RelationToRanger(dom, ranger) == 100) result = 1;",
        ]
        issues = lint_rson_runtime(RsonProject(data, Path("nested-handles.rson")))
        codes = {issue.code for issue in issues}
        self.assertIn("runtime-object-api-without-explicit-guard", codes)
        self.assertIn("runtime-object-api-behind-boolean-guard", codes)
        self.assertIn("runtime-redundant-star-ruins-type-dereference", codes)

        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "dword star = GalaxyStar(i);",
            "if(!star) exit;",
            "dword base = StarRuins(star, 'PB');",
            "if(!base) exit;",
            "if(base && condition && RelationToRanger(base, ranger) == 100) result = 1;",
        ]
        guarded_codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("prior-guard.rson")))
        }
        self.assertNotIn("runtime-object-api-without-explicit-guard", guarded_codes)
        self.assertNotIn("runtime-object-api-behind-boolean-guard", guarded_codes)

    def test_nullable_handle_allows_api_in_separately_guarded_if_body(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "dword star = GalaxyStar(i);",
            "if(star) result = StarOwner(star);",
            "dword base = StarRuins(known_star, 'PB');",
            "if(base) result = Id(base);",
            "dword other_star = GalaxyStar(i + 1);",
            "if(other_star)",
            "    result = StarName(other_star);",
        ]
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("guarded-body.rson")))
        }
        self.assertNotIn("runtime-object-api-without-explicit-guard", codes)

    def test_nullable_handle_accepts_proven_user_predicate_guard(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "function IsLiveStar(dword star)",
            "{",
            "    result = 0;",
            "    if(!star) exit;",
            "    result = 1;",
            "}",
            "function IsFreeStar(dword star)",
            "{",
            "    result = 0;",
            "    if(!IsLiveStar(star)) exit;",
            "    result = 1;",
            "}",
            "dword star = GalaxyStar(i);",
            "if(!IsLiveStar(star)) continue;",
            "int owner = StarOwner(star);",
            "dword other_star = GalaxyStar(i + 1);",
            "if(!IsFreeStar(other_star) || other_star == star) continue;",
            "int distance = Dist(star, other_star);",
        ]
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("predicate-guard.rson")))
        }
        self.assertNotIn("runtime-object-api-without-explicit-guard", codes)

        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "function UnsafePredicate(dword star)",
            "{",
            "    result = 0;",
            "    StarOwner(star);",
            "    if(!star) exit;",
            "    result = 1;",
            "}",
            "dword star = GalaxyStar(i);",
            "if(!UnsafePredicate(star)) continue;",
            "result = StarName(star);",
        ]
        unsafe_codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("unsafe-predicate.rson")))
        }
        self.assertIn("runtime-object-api-without-explicit-guard", unsafe_codes)

    def test_nullable_handle_accepts_eager_safe_compound_null_guard(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "dword star = GalaxyStar(i);",
            "if(!star || star == source_star) continue;",
            "result = Id(star);",
        ]
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("compound-guard.rson")))
        }
        self.assertNotIn("runtime-object-api-without-explicit-guard", codes)

    def test_nullable_handle_deduplicates_cascade_but_keeps_dist_root(self) -> None:
        data = deepcopy(SAFE_RSON)
        data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "function IsLiveStar(dword star)",
            "{",
            "    result = 0;",
            "    if(!star) exit;",
            "    result = 1;",
            "}",
            "dword star = GalaxyStar(i);",
            "if(!IsLiveStar(star) || StarOwner(star) != 0) continue;",
            "int threat = StarEnemyThreatLevel(star, 1);",
            "result = Id(star);",
            "int distance = Dist(GalaxyStar(j), known_star);",
        ]
        issues = lint_rson_runtime(RsonProject(data, Path("root-causes.rson")))
        missing_guards = [
            issue
            for issue in issues
            if issue.code == "runtime-object-api-without-explicit-guard"
        ]
        self.assertEqual(len(missing_guards), 2)
        self.assertIn("StarOwner(star)", missing_guards[0].evidence)
        self.assertIn("Dist(GalaxyStar(j)", missing_guards[1].evidence)

    def test_dialog_injection_reports_late_persistent_turn_gate_as_advisory(self) -> None:
        data = deepcopy(SAFE_RSON)
        group = data["Visual.Objects"][0]
        group["Variables"] = [
            {"Type": "TVar", "Name": "panel_initialized", "Init": "0", "#": 4}
        ]
        group["Operations"][1]["Code"].append("panel_initialized = 1;")
        group["Operations"].append(
            {
                "Type": "Top",
                "Name": "DialogBegin",
                "Parent": -1,
                "#": 5,
                "Code.Type": "DialogBegin",
                "Code": [
                    "if(panel_initialized)",
                    "{",
                    "    AddDialogInject('ControlPanel', '', 'Open', 60);",
                    "}",
                ],
            }
        )
        issues = lint_rson_runtime(RsonProject(data, Path("late-dialog.rson")))
        matching = [
            issue
            for issue in issues
            if issue.code == "runtime-dialog-inject-delayed-persistent-gate"
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].severity, "info")

        group["Operations"][-1]["Code"].insert(0, "panel_initialized = 1;")
        safe_codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("local-dialog-gate.rson")))
        }
        self.assertNotIn("runtime-dialog-inject-delayed-persistent-gate", safe_codes)

    def test_dialog_msg_rejects_eager_invalid_index_and_warns_about_late_value(self) -> None:
        data = deepcopy(SAFE_RSON)
        group = data["Visual.Objects"][0]
        group["Variables"] = [
            {
                "Type": "TVar",
                "Name": "descriptions",
                "Init": "newarray(5)",
                "Parent": -1,
                "#": 20,
            },
            {
                "Type": "TVar",
                "Name": "level",
                "Init": "0",
                "Parent": -1,
                "#": 21,
            },
            {
                "Type": "TVar",
                "Name": "cost",
                "Init": "0",
                "Parent": -1,
                "#": 22,
            },
        ]
        group["Dialogs"] = [
            {"Type": "TDialog", "Name": "Upgrade", "Parent": -1, "#": 10},
            {
                "Type": "TDialogMsg",
                "Name": "Details",
                "Parent": -1,
                "#": 11,
                "DMsg.Num": "13",
                "Msg": "Hull <descriptions[level-1]>, cost <cost>",
            },
        ]
        group["Operations"].append(
            {
                "Type": "Top",
                "Name": "PrepareUpgrade",
                "Parent": 10,
                "#": 12,
                "Code.Type": "Turn",
                "Code": [
                    "DChange(13);",
                    "level = 1;",
                    "cost = GalaxyMoney(0);",
                ],
            }
        )
        data["Visual.Links"] = [
            {"Type": "TGraphLink", "Begin": 10, "End": 12, "Nom": 0, "Arrow": True}
        ]
        issues = lint_rson_runtime(RsonProject(data, Path("eager-dialog-msg.rson")))
        by_code = {issue.code: issue for issue in issues}
        self.assertEqual(
            by_code["runtime-dialog-msg-eager-array-index"].severity,
            "error",
        )
        self.assertIn("-1", by_code["runtime-dialog-msg-eager-array-index"].message)
        self.assertEqual(
            by_code["runtime-dialog-msg-eager-mutable-value"].severity,
            "warning",
        )

        group["Dialogs"][1]["Msg"] = "Cost <cost>"
        group["Operations"][-1]["Code"] = [
            "cost = GalaxyMoney(0);",
            "DChange(13);",
        ]
        safe_codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("handler-text.rson")))
        }
        self.assertNotIn("runtime-dialog-msg-eager-array-index", safe_codes)
        self.assertNotIn("runtime-dialog-msg-eager-mutable-value", safe_codes)

    def test_linked_dtext_warns_when_dialog_message_already_has_text(self) -> None:
        data = deepcopy(SAFE_RSON)
        group = data["Visual.Objects"][0]
        group["Dialogs"] = [
            {
                "Type": "TDialogMsg",
                "Name": "Details",
                "Parent": -1,
                "#": 10,
                "DMsg.Num": "13",
                "Msg": "Full highlighted reply",
            },
        ]
        group["Operations"].append(
            {
                "Type": "Top",
                "Name": "DetailsHandler",
                "Parent": -1,
                "#": 11,
                "Code.Type": "Turn",
                "Code": ['DText("only suffix");'],
            }
        )
        data["Visual.Links"] = [
            {"Type": "TGraphLink", "Begin": 10, "End": 11, "Nom": 0, "Arrow": True}
        ]
        matching = [
            issue
            for issue in lint_rson_runtime(
                RsonProject(data, Path("dialog-dtext-overwrite.rson"))
            )
            if issue.code == "runtime-dialog-handler-dtext-overwrite"
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].severity, "warning")

        group["Dialogs"][0]["Msg"] = ""
        safe_codes = {
            issue.code
            for issue in lint_rson_runtime(
                RsonProject(data, Path("dialog-dtext-only.rson"))
            )
        }
        self.assertNotIn("runtime-dialog-handler-dtext-overwrite", safe_codes)

    def test_dialog_forward_reference_to_persistent_array_is_blocked(self) -> None:
        data = deepcopy(SAFE_RSON)
        group = data["Visual.Objects"][0]
        group["Operations"][0]["Code"].append("dialog_items = newarray(1);")
        group["Dialogs"] = [
            {"Type": "TDialog", "Name": "ArrayDialog", "Parent": -1, "#": 4}
        ]
        group["Operations"].append(
            {
                "Type": "Top",
                "Name": "ArrayDialogBegin",
                "Parent": -1,
                "#": 5,
                "Code.Type": "DialogBegin",
                "Code": ["if(ArrayDim(dialog_items) > 1) result = dialog_items[1];"],
            }
        )
        group["Variables"] = [
            {
                "Type": "TVar",
                "Name": "dialog_items",
                "Parent": -1,
                "#": 6,
                "Var.Type": "None",
                "Init": "",
            }
        ]
        data["Visual.Links"] = [
            {"Type": "TGraphLink", "Begin": 4, "End": 5, "Nom": 0, "Arrow": True}
        ]
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("dialog-array-forward.rson")))
        }
        self.assertIn("rscript-dialog-persistent-array", codes)

        group["Variables"][0]["#"] = 4
        group["Dialogs"][0]["#"] = 5
        group["Operations"][-1]["#"] = 6
        data["Visual.Links"][0]["Begin"] = 5
        data["Visual.Links"][0]["End"] = 6
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("dialog-array-backward.rson")))
        }
        self.assertNotIn("rscript-dialog-persistent-array", codes)

    def test_storage_comparison_detects_array_hidden_by_legacy_gate(self) -> None:
        old_data = deepcopy(SAFE_RSON)
        old_data["Visual.Objects"][0]["Variables"] = [
            {"Type": "TVar", "Name": "initialized", "Parent": -1, "#": 4, "Init": "0"}
        ]
        old_data["Visual.Objects"][0]["Operations"][0]["Code"].append("initialized = 0;")
        new_data = deepcopy(old_data)
        new_data["Visual.Objects"][0]["Variables"].append(
            {
                "Type": "TVar",
                "Name": "saved_items",
                "Parent": -1,
                "#": 5,
                "Var.Type": "None",
                "Init": "",
            }
        )
        new_data["Visual.Objects"][0]["Operations"][0]["Code"].extend(
            [
                "if(!initialized)",
                "{",
                "    saved_items = newarray(1);",
                "    initialized = 1;",
                "}",
            ]
        )
        new_data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "if(ArrayDim(saved_items) > 1) result = saved_items[1];"
        ]
        old = RsonProject(old_data, Path("old.rson"))
        new = RsonProject(new_data, Path("new.rson"))
        comparison = compare_storage_schemas(old, new)
        self.assertEqual(comparison["status"], "issues")
        self.assertEqual(comparison["added_arrays"], ["saved_items"])
        self.assertEqual(
            comparison["issues"][0]["code"],
            "runtime-new-persistent-array-without-storage-migration",
        )

    def test_storage_comparison_blocks_persistent_array_capacity_change(self) -> None:
        old_data = deepcopy(SAFE_RSON)
        old_data["Visual.Objects"][0]["Variables"] = [
            {"Type": "TVar", "Name": "transport_ids", "Parent": -1, "#": 20},
        ]
        old_data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "transport_ids = newarray(1);",
            "ArrayClear(transport_ids);",
        ]
        new_data = deepcopy(old_data)
        new_data["Visual.Objects"][0]["Operations"][1]["Code"] = [
            "transport_ids = newarray(7);",
            "for(int i = 0; i < ArrayDim(transport_ids); i = i + 1)",
            "    transport_ids[i] = 0;",
        ]
        comparison = compare_storage_schemas(
            RsonProject(old_data, Path("old-dynamic.rson")),
            RsonProject(new_data, Path("new-fixed.rson")),
        )
        self.assertEqual(comparison["status"], "issues")
        self.assertEqual(
            comparison["changed_arrays"],
            [{"name": "transport_ids", "old_sizes": [1], "new_sizes": [7]}],
        )
        self.assertIn(
            "runtime-persistent-array-size-changed",
            {issue["code"] for issue in comparison["issues"]},
        )

    def test_dialog_targets_transitions_ether_and_warrior_advisories(self) -> None:
        data = deepcopy(SAFE_RSON)
        group = data["Visual.Objects"][0]
        group["Dialogs"] = [
            {"Type": "TDialog", "Name": "LoopDialog", "Parent": -1, "#": 4}
        ]
        group["Operations"].append(
            {
                "Type": "Top",
                "Name": "LoopHandler",
                "Parent": -1,
                "#": 5,
                "Code.Type": "Turn",
                "Code": [
                    "InjectAnswer('LoopDialog', 'Again', 0);",
                    "DChange(99);",
                    "Ether(0, 'Stable', 'One');",
                    "EtherDelete('Stable');",
                    "Ether(0, 'Stable', 'Two');",
                    "dword released = BuyWarrior(home_planet);",
                    "ShipOut(released);",
                ],
            }
        )
        data["Visual.Links"] = [
            {"Type": "TGraphLink", "Begin": 4, "End": 5, "Nom": 0, "Arrow": True}
        ]
        codes = {
            issue.code
            for issue in lint_rson_runtime(RsonProject(data, Path("dialog-semantics.rson")))
        }
        self.assertIn("dialog-inject-self-target", codes)
        self.assertIn("dialog-transition-number-missing", codes)
        self.assertIn("runtime-ether-id-reuse-after-delete", codes)
        self.assertIn("runtime-warrior-home-unchanged", codes)


if __name__ == "__main__":
    unittest.main()
