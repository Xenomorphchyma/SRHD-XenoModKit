from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path

from srhd_modkit.cli import main
from srhd_modkit.scripts import RSON_FILE_ID, RSON_FILE_VERSION, RsonProject, inspect_scr, load_rson


SAMPLE = {
    "FileID": RSON_FILE_ID,
    "FileVersion": RSON_FILE_VERSION,
    "ScriptName": "TestMod",
    "ScriptFileOut": "TestMod.scr",
    "ScriptTextOut": "TestMod.txt",
    "Visual.Objects": [
        {
            "Operations": [
                {"Type": "Top", "Name": "Init", "Parent": -1, "#": 1, "Total.Lines": 1, "Code": ["GRun();"]},
                {"Type": "Top", "Name": "Turn", "Parent": -1, "#": 2, "Total.Lines": 1, "Code": ["HullHP(Player(),1);"]},
            ]
        }
    ],
    "Visual.Links": [{"Type": "TGraphLink", "Begin": 1, "End": 2, "Nom": 0, "Arrow": True}],
    "BlockPar.EC.Total.Strings": 0,
    "BlockPar.EC": [],
}


class RsonTests(unittest.TestCase):
    def _project(self, root: Path):
        path = root / "test.rson"
        path.write_text(json.dumps(SAMPLE), encoding="utf-8")
        return load_rson(path)

    def test_valid_project_and_search(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = self._project(Path(name))
            self.assertEqual(project.validate(), [])
            self.assertEqual(project.summary()["objects"], 2)
            result = project.search_code("HullHP")
            self.assertEqual(result[0]["object_id"], 2)

    def test_titem_requires_items_collection_and_place_field(self) -> None:
        valid = deepcopy(SAMPLE)
        valid["Visual.Objects"][0]["Items"] = [
            {
                "Type": "TItem",
                "Name": "Cargo",
                "Parent": -1,
                "#": 3,
                "Class": 5,
                "Item.Type": 0,
                "Owner": 6,
                "+Place": "",
            }
        ]
        self.assertEqual(RsonProject(valid, Path("valid-item.rson")).validate(), [])

        wrong_collection = deepcopy(valid)
        item = wrong_collection["Visual.Objects"][0].pop("Items")[0]
        wrong_collection["Visual.Objects"][0]["Operations"].append(item)
        codes = {
            issue.code
            for issue in RsonProject(wrong_collection, Path("wrong-item.rson")).validate()
        }
        self.assertIn("rson-titem-collection", codes)

        missing_place = deepcopy(valid)
        del missing_place["Visual.Objects"][0]["Items"][0]["+Place"]
        codes = {
            issue.code
            for issue in RsonProject(missing_place, Path("missing-place.rson")).validate()
        }
        self.assertIn("rson-titem-place", codes)

    def test_optional_items_count_must_match_when_serialized(self) -> None:
        data = deepcopy(SAMPLE)
        data["Visual.Objects"][0]["Items"] = [
            {"Type": "TItem", "Name": "Cargo", "Parent": -1, "#": 3, "+Place": ""}
        ]
        self.assertNotIn(
            "rson-items-count",
            {issue.code for issue in RsonProject(data, Path("implicit-count.rson")).validate()},
        )
        data["Visual.Objects"][0]["Items.Count"] = 0
        self.assertIn(
            "rson-items-count",
            {issue.code for issue in RsonProject(data, Path("bad-count.rson")).validate()},
        )

    def test_rscript_410f_rejects_a_fifth_real_tgroup(self) -> None:
        data = deepcopy(SAMPLE)
        data["Visual.Objects"][0]["Groups"] = [
            {
                "Type": "TGroup",
                "Name": f"Group{index}",
                "Parent": -1,
                "#": index + 2,
            }
            for index in range(1, 5)
        ]
        self.assertNotIn(
            "rscript-tgroup-hard-limit",
            {issue.code for issue in RsonProject(data, Path("four-groups.rson")).validate()},
        )
        data["Visual.Objects"][0]["Groups"].append(
            {"Type": "TGroup", "Name": "Fifth", "Parent": -1, "#": 7}
        )
        issue = next(
            issue
            for issue in RsonProject(data, Path("five-groups.rson")).validate()
            if issue.code == "rscript-tgroup-hard-limit"
        )
        self.assertEqual(issue.severity, "error")
        self.assertIn("#7 Fifth", issue.message)

    def test_tgroup_requires_outgoing_planet_and_initial_state_links(self) -> None:
        data = deepcopy(SAMPLE)
        data["Visual.Objects"][0].update(
            {
                "Groups": [
                    {"Type": "TGroup", "Name": "RuntimePlayer", "Parent": -1, "#": 3}
                ],
                "Planets": [
                    {"Type": "TPlanet", "Name": "PlanetAnchor", "Parent": -1, "#": 4}
                ],
                "States": [
                    {"Type": "TState", "Name": "InitialState", "Parent": -1, "#": 5}
                ],
            }
        )

        missing = RsonProject(deepcopy(data), Path("missing-links.rson")).validate()
        self.assertEqual(
            {
                issue.code
                for issue in missing
                if issue.code.startswith("rscript-tgroup-")
            },
            {
                "rscript-tgroup-planet-link-missing",
                "rscript-tgroup-state-link-missing",
            },
        )

        only_planet = deepcopy(data)
        only_planet["Visual.Links"].append(
            {"Type": "TGraphLink", "Begin": 3, "End": 4, "Nom": 0, "Arrow": True}
        )
        codes = {
            issue.code
            for issue in RsonProject(only_planet, Path("only-planet.rson")).validate()
        }
        self.assertNotIn("rscript-tgroup-planet-link-missing", codes)
        self.assertIn("rscript-tgroup-state-link-missing", codes)

        complete = deepcopy(only_planet)
        complete["Visual.Links"].append(
            {"Type": "TGraphLink", "Begin": 3, "End": 5, "Nom": 0, "Arrow": True}
        )
        codes = {
            issue.code
            for issue in RsonProject(complete, Path("complete-group.rson")).validate()
        }
        self.assertNotIn("rscript-tgroup-planet-link-missing", codes)
        self.assertNotIn("rscript-tgroup-state-link-missing", codes)

        wrong_direction = deepcopy(data)
        wrong_direction["Visual.Links"].extend(
            [
                {"Type": "TGraphLink", "Begin": 4, "End": 3, "Nom": 0, "Arrow": True},
                {"Type": "TGraphLink", "Begin": 5, "End": 3, "Nom": 0, "Arrow": True},
            ]
        )
        codes = {
            issue.code
            for issue in RsonProject(wrong_direction, Path("reverse-links.rson")).validate()
        }
        self.assertIn("rscript-tgroup-planet-link-missing", codes)
        self.assertIn("rscript-tgroup-state-link-missing", codes)

    def test_dialog_numbers_are_global_dense_and_unique(self) -> None:
        data = deepcopy(SAMPLE)
        data["Visual.Objects"][0]["Dialogs"] = [
            {
                "Type": "TDialogMsg",
                "Name": "First",
                "Parent": -1,
                "#": 3,
                "DMsg.Num": "0",
                "Msg": "",
            },
            {
                "Type": "TDialogMsg",
                "Name": "Second",
                "Parent": -1,
                "#": 4,
                "DMsg.Num": 0,
                "Msg": "",
            },
            {
                "Type": "TDialogAnswer",
                "Name": "Sparse",
                "Parent": -1,
                "#": 5,
                "AMsg.Num": 10,
                "Msg": "",
            },
            {"Type": "TDialog", "Name": "Repeated", "Parent": -1, "#": 6},
            {"Type": "TDialog", "Name": "Repeated", "Parent": -1, "#": 7},
        ]
        issues = RsonProject(data, Path("dialog-numbers.rson")).validate()
        codes = {issue.code for issue in issues}
        self.assertIn("dialog-global-number-collision", codes)
        self.assertIn("dialog-noncanonical-numbering", codes)
        self.assertIn("dialog-duplicate-name", codes)

    def test_dialog_answer_msg_cannot_contain_rscript_expression(self) -> None:
        data = deepcopy(SAMPLE)
        data["Visual.Objects"][0]["Dialogs"] = [
            {
                "Type": "TDialogAnswer",
                "Name": "Broken",
                "Parent": -1,
                "#": 3,
                "AMsg.Num": 0,
                "Msg": "DAnswer('restart~' + CT('Mod.Answer'));",
            }
        ]
        codes = {
            issue.code
            for issue in RsonProject(data, Path("dialog-msg-code.rson")).validate()
        }
        self.assertIn("dialog-answer-msg-contains-rscript-expression", codes)

        data["Visual.Objects"][0]["Dialogs"][0]["Msg"] = (
            'DAnswer(CT("Script.TestMod.41"));'
        )
        canonical_codes = {
            issue.code
            for issue in RsonProject(data, Path("dialog-msg-canonical.rson")).validate()
        }
        self.assertNotIn("dialog-answer-msg-contains-rscript-expression", canonical_codes)
        self.assertNotIn("rscript-dialog-answer-msg-missing-semicolon", canonical_codes)

    def test_dialog_answer_expression_requires_semicolon(self) -> None:
        data = deepcopy(SAMPLE)
        data["Visual.Objects"][0]["Dialogs"] = [
            {
                "Type": "TDialogAnswer",
                "Name": "MissingSemicolon",
                "Parent": -1,
                "#": 3,
                "AMsg.Num": 0,
                "Msg": "DAnswer('fastexit~Visible text')",
            }
        ]
        issues = RsonProject(data, Path("dialog-msg-semicolon.rson")).validate()
        issue = next(
            issue
            for issue in issues
            if issue.code == "rscript-dialog-answer-msg-missing-semicolon"
        )
        self.assertEqual(issue.severity, "error")
        self.assertTrue(
            any(
                issue.code == "rscript-dialog-answer-msg-inline-text"
                for issue in issues
            )
        )

        data["Visual.Objects"][0]["Dialogs"][0]["Msg"] += ";"
        codes = {
            issue.code
            for issue in RsonProject(data, Path("dialog-msg-semicolon.rson")).validate()
        }
        self.assertNotIn("rscript-dialog-answer-msg-missing-semicolon", codes)

    def test_dialog_answer_inline_visible_text_is_rejected_but_exit_is_allowed(self) -> None:
        data = deepcopy(SAMPLE)
        answer = {
            "Type": "TDialogAnswer",
            "Name": "Inline",
            "Parent": -1,
            "#": 3,
            "AMsg.Num": 0,
            "Msg": "DAnswer('fastexit~Visible text');",
        }
        data["Visual.Objects"][0]["Dialogs"] = [answer]
        issues = RsonProject(data, Path("dialog-inline-text.rson")).validate()
        self.assertTrue(
            any(
                issue.code == "rscript-dialog-answer-msg-inline-text"
                for issue in issues
            )
        )

        answer["Msg"] = "DAnswer('exit');"
        codes = {
            issue.code
            for issue in RsonProject(data, Path("dialog-exit.rson")).validate()
        }
        self.assertNotIn("rscript-dialog-answer-msg-inline-text", codes)

    def test_set_code_updates_line_count_and_survives_json(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            project = self._project(root)
            project.set_code(2, ["a=1;", "b=2;"])
            output = project.save(root / "edited.rson")
            edited = load_rson(output)
            item = edited.object_by_id(2)
            self.assertEqual(item["Code"], ["a=1;", "b=2;"])
            self.assertEqual(item["Total.Lines"], 2)
            self.assertEqual(edited.validate(), [])

    def test_set_field_updates_json_and_protects_object_id(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = self._project(Path(name))
            project.set_field(2, "Name", "Changed")
            self.assertEqual(project.object_by_id(2)["Name"], "Changed")
            with self.assertRaises(ValueError):
                project.set_field(2, "#", 10)

    def test_broken_link_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = self._project(Path(name))
            project.data["Visual.Links"][0]["End"] = 999
            self.assertTrue(any(issue.code == "rson-link-ref" for issue in project.validate()))

    def test_syntax_preflight_rejects_uncommented_prose_before_compiler(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = self._project(Path(name))
            project.object_by_id(1)["Code"] = [
                'message="Русский текст в строке допустим";',
                "// Русский комментарий допустим",
                "q=0;Правильная, но неработающая строка",
            ]
            issues = project.validate()
            matching = [issue for issue in issues if issue.code == "rscript-uncommented-text"]
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0].location, "object #1 Code:3")

    def test_syntax_preflight_reports_unclosed_comment_and_delimiter(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = self._project(Path(name))
            project.object_by_id(1)["Code"] = ["if(Player() {", "/* unfinished"]
            codes = {issue.code for issue in project.validate()}
            self.assertIn("rscript-unclosed-comment", codes)
            self.assertIn("rscript-unbalanced-delimiter", codes)

    def test_state_events_are_headless_and_preserve_handler(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            project = self._project(root)
            project.data["Visual.Objects"][0]["Operations"].append(
                {
                    "Type": "TState",
                    "Name": "PlayerState",
                    "Parent": -1,
                    "#": 3,
                    "OnActCode": "PlayerActCode();",
                }
            )
            project.set_state_events(3, ["t_OnEnteringForm", "t_OnPlayerBuyEq", "t_OnEnteringForm"])
            state = project.object_by_id(3)
            self.assertEqual(
                state["OnActCode"],
                "[t_OnEnteringForm,t_OnPlayerBuyEq|]\nPlayerActCode();",
            )
            self.assertEqual(project.state_events(3), ["t_OnEnteringForm", "t_OnPlayerBuyEq"])
            self.assertEqual(project.summary()["state_event_subscriptions"][0]["object_id"], 3)
            project.set_state_events(3, [])
            self.assertEqual(state["OnActCode"], "PlayerActCode();")

    def test_set_code_updates_state_handler_and_preserves_event_signature(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = self._project(Path(name))
            project.data["Visual.Objects"][0]["Operations"].append(
                {
                    "Type": "TState",
                    "Name": "PlayerState",
                    "Parent": -1,
                    "#": 3,
                    "OnActCode": "[t_OnPlayerBuyEq|]\nOldHandler();",
                }
            )
            project.set_code(3, ["if(!ScriptItemActionType(t_OnPlayerBuyEq)) exit;", "NewHandler();"], field="OnActCode")
            self.assertEqual(
                project.object_by_id(3)["OnActCode"],
                "[t_OnPlayerBuyEq|]\nif(!ScriptItemActionType(t_OnPlayerBuyEq)) exit;\nNewHandler();",
            )
            with self.assertRaises(ValueError):
                project.set_code(2, ["NotAState();"], field="OnActCode")
            with self.assertRaises(ValueError):
                project.set_code(3, ["[t_OnEnteringForm|]", "Bad();"], field="OnActCode")

    def test_cli_set_code_updates_on_act_code_headlessly(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            project = self._project(root)
            project.data["Visual.Objects"][0]["Operations"].append(
                {
                    "Type": "TState",
                    "Name": "PlayerState",
                    "Parent": -1,
                    "#": 3,
                    "OnActCode": "[t_OnPlayerBuyEq|]\nOldHandler();",
                }
            )
            project.save(project.path)
            handler = root / "player-buy.txt"
            handler.write_text("if(!ScriptItemActionType(t_OnPlayerBuyEq)) exit;\nNormalizePurchase();\n", encoding="utf-8")
            output = root / "edited.rson"
            stdout = StringIO()

            with redirect_stdout(stdout):
                result = main(
                    [
                        "script",
                        "set-code",
                        str(project.path),
                        str(output),
                        "--id",
                        "3",
                        "--field",
                        "OnActCode",
                        "--code-file",
                        str(handler),
                        "--json",
                    ]
                )

            self.assertEqual(result, 0)
            self.assertEqual(json.loads(stdout.getvalue())["field"], "OnActCode")
            self.assertEqual(
                load_rson(output).object_by_id(3)["OnActCode"],
                "[t_OnPlayerBuyEq|]\nif(!ScriptItemActionType(t_OnPlayerBuyEq)) exit;\nNormalizePurchase();",
            )

    def test_invalid_state_event_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = self._project(Path(name))
            with self.assertRaises(ValueError):
                project.set_state_events(2, ["t_OnEnteringForm"])

    def test_decompiled_state_event_suffix_is_valid_and_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = self._project(Path(name))
            project.data["Visual.Objects"][0]["Operations"].append(
                {
                    "Type": "TState",
                    "Name": "RecoveredState",
                    "Parent": -1,
                    "#": 3,
                    "OnActCode": "[t_OnItemPickUp,t_OnEnteringForm|0]\r\nRecoveredHandler();",
                }
            )

            self.assertEqual(project.validate(), [])
            self.assertEqual(project.state_events(3), ["t_OnItemPickUp", "t_OnEnteringForm"])
            project.set_state_events(3, ["t_OnPlayerBuyEq"])
            self.assertEqual(
                project.object_by_id(3)["OnActCode"],
                "[t_OnPlayerBuyEq|0]\nRecoveredHandler();",
            )

    def test_graph_clone_add_and_delete_are_reference_safe(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = self._project(Path(name))
            clone = project.clone_object(2, name="Turn copy")
            self.assertEqual(clone["#"], 3)
            self.assertEqual(clone["Name"], "Turn copy")
            self.assertEqual(clone["Code"], ["HullHP(Player(),1);"])
            link = project.add_link(2, 3, nom=1, arrow=False)
            self.assertEqual(link["Type"], "TGraphLink")
            with self.assertRaises(ValueError):
                project.delete_object(2)
            removed = project.delete_object(2, detach_references=True)
            self.assertEqual(removed["removed_links"], 2)
            issues = project.validate()
            self.assertEqual(
                {issue.code for issue in issues},
                {"rson-object-id-range"},
            )

    def test_sparse_object_ids_are_rejected_before_rscript_hangs(self) -> None:
        data = deepcopy(SAMPLE)
        data["Visual.Objects"][0]["Operations"][1]["#"] = 107
        data["Visual.Links"][0]["End"] = 107
        issues = RsonProject(data, Path("sparse-ids.rson")).validate()
        matching = [issue for issue in issues if issue.code == "rson-object-id-range"]
        self.assertEqual(len(matching), 1)
        self.assertIn("#1..#107", matching[0].message)

    def test_delete_link_uses_stable_zero_based_index(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = self._project(Path(name))
            removed = project.delete_link(0)
            self.assertEqual((removed["Begin"], removed["End"]), (1, 2))
            self.assertEqual(project.data["Visual.Links"], [])
            with self.assertRaises(IndexError):
                project.delete_link(0)

    def test_inspect_scr_reports_compiled_event_signatures(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "events.scr"
            path.write_bytes(
                (8).to_bytes(4, "little")
                + "[t_OnEnteringForm,t_OnPlayerBuyEq|]".encode("utf-16-le")
                + b"\x00\x00"
            )
            result = inspect_scr(path)
            self.assertEqual(result["event_signatures"], ["[t_OnEnteringForm,t_OnPlayerBuyEq|]"])

    def test_inspect_scr_reports_proven_dialog_language_keys(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "Mod_Binary.scr"
            code = (
                "DAnswer('fastexit~'+CT(\"Script.Mod_Binary.12\"));"
                "DText(CT(\"Script.Mod_Binary.13\"));"
            )
            path.write_bytes(
                (8).to_bytes(4, "little")
                + code.encode("utf-16-le")
                + b"\x00\x00"
            )
            result = inspect_scr(path)
            self.assertEqual(
                result["dialog_language_keys"],
                [{"script_name": "Mod_Binary", "key": "12"}],
            )


if __name__ == "__main__":
    unittest.main()
