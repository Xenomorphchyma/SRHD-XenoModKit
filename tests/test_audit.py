from __future__ import annotations

import json
import tempfile
import struct
import unittest
from pathlib import Path

from srhd_modkit.audit import AuditProfile, audit_mod
from srhd_modkit.image_codec import RgbaImage, encode_gi
from srhd_modkit.quests import (
    HEADER_QMM_7,
    QuestDocument,
    QuestLocation,
    QuestLocationText,
    QuestMedia,
    QuestParameter,
    QuestParameterChange,
    QuestStrings,
    write_qmm,
)
from srhd_modkit.scripts import RSON_FILE_ID, RSON_FILE_VERSION
from srhd_modkit.toolchain import Toolchain


def _mod(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "ModuleInfo.txt").write_text(
        "Name=AuditFixture\nSection=Test\nPriority=1\nLanguages=Rus\n",
        encoding="cp1251",
    )
    (root / "DATA").mkdir()


def _jpeg(width: int = 343, height: int = 394, components: int = 3) -> bytes:
    component_data = b"".join(bytes((index + 1, 0x11, 0)) for index in range(components))
    sof = bytes((8,)) + struct.pack(">HHB", height, width, components) + component_data
    return b"\xff\xd8\xff\xc0" + struct.pack(">H", len(sof) + 2) + sof + b"\xff\xd9"


def _quest() -> QuestDocument:
    parameter = QuestParameter(0, 1, 0, True, 0, True, False, "Value", (), "", "0")
    location = QuestLocation(
        False,
        0,
        0,
        1,
        0,
        1,
        (QuestParameterChange(),),
        (QuestLocationText("Start", QuestMedia("USED")),),
        False,
        "",
    )
    return QuestDocument(
        HEADER_QMM_7,
        1,
        0,
        "fixture",
        0,
        0,
        0,
        0,
        0,
        0,
        800,
        600,
        16,
        12,
        0,
        60,
        (parameter,),
        QuestStrings("to star", None, None, "to planet", "date", "money", "from planet", "from star", "ranger"),
        "Success",
        "Task",
        (location,),
        (),
    )


class AuditTests(unittest.TestCase):
    def test_release_checks_source_config_cache_against_install_subpath(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "AuditFixture"
            _mod(root)
            script = root / "DATA" / "Script" / "Mod_AuditFixture.scr"
            script.parent.mkdir(parents=True)
            script.write_bytes(struct.pack("<I", 8))
            config = root / "Source" / "Config"
            config.mkdir(parents=True)
            (config / "Main.txt").write_text(
                "Data ^{\n"
                "  Script ^{\n"
                "    Mod_AuditFixture=1,Script.Mod_AuditFixture\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )
            (config / "CacheData.txt").write_text(
                "Script ^{\n"
                "  Mod_AuditFixture=Mods\\OtherMods\\AuditFixture\\DATA\\Script\\Mod_AuditFixture.scr\n"
                "}\n",
                encoding="utf-8",
            )

            report = audit_mod(
                root,
                profile="release",
                install_subpath="AuditFixture",
            )
            matching = [
                item
                for item in report.issues
                if item.code == "cache-script-install-path-mismatch"
            ]
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0].severity, "error")
            self.assertIn("Source\\Config\\CacheData.txt", matching[0].path or "")

    def test_release_blocks_tgroup_without_planet_or_initial_state(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "AuditFixture"
            _mod(root)
            source = root / "SOURCE"
            source.mkdir()
            project = {
                "FileID": RSON_FILE_ID,
                "FileVersion": RSON_FILE_VERSION,
                "ScriptName": "Mod_IncompleteGroup",
                "Visual.Objects": [
                    {
                        "Groups": [
                            {
                                "Type": "TGroup",
                                "Name": "Unplaced",
                                "Parent": -1,
                                "#": 0,
                            }
                        ]
                    }
                ],
                "Visual.Links": [],
            }
            (source / "Mod_IncompleteGroup.rson").write_text(
                json.dumps(project),
                encoding="utf-8",
            )

            report = audit_mod(root, profile="release")
            codes = {item.code for item in report.blocking_issues()}
            self.assertIn("rscript-tgroup-planet-link-missing", codes)
            self.assertIn("rscript-tgroup-state-link-missing", codes)

    def test_release_rejects_code_stub_lang_for_scr_without_rson_key(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "AuditFixture"
            _mod(root)
            source_cfg = root / "SOURCE" / "CFG"
            source_cfg.mkdir(parents=True)
            (source_cfg / "Main.txt").write_text(
                "Data ^{\n"
                "  Script ^{\n"
                "    Mod_Binary=1,Script.Mod_Binary\n"
                "  }\n"
                "}\n",
                encoding="cp1251",
            )
            script = root / "DATA" / "Script" / "Mod_Binary.scr"
            script.parent.mkdir(parents=True)
            script.write_bytes(
                (8).to_bytes(4, "little")
                + "DAnswer('fastexit~Inline answer');".encode("utf-16-le")
                + b"\x00\x00"
            )
            lang_source = root / "SOURCE" / "Lang.txt"
            lang_source.write_text(
                "Script ^{\n"
                "  Mod_Binary ~{\n"
                "    1=DAnswer('fastexit~Inline answer')\n"
                "  }\n"
                "}\n",
                encoding="cp1251",
            )
            lang = root / "DATA" / "Script" / "Lang.dat"
            Toolchain().convert_dat(lang_source, lang)

            report = audit_mod(root, profile="release")
            issue = next(
                item
                for item in report.issues
                if item.code == "script-dialog-lang-value-code-stub"
            )
            self.assertEqual(issue.location, "Script/Mod_Binary/1")
            self.assertIn("недоступны без точной RSON/SCR-ссылки", issue.message)

    def test_release_checks_dialog_language_in_scr_without_rson(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "AuditFixture"
            _mod(root)
            source_cfg = root / "SOURCE" / "CFG"
            source_cfg.mkdir(parents=True)
            (source_cfg / "Main.txt").write_text(
                "Data ^{\n"
                "  Script ^{\n"
                "    Mod_Binary=1,Script.Mod_Binary\n"
                "  }\n"
                "}\n",
                encoding="cp1251",
            )
            script = root / "DATA" / "Script" / "Mod_Binary.scr"
            script.parent.mkdir(parents=True)
            script.write_bytes(
                (8).to_bytes(4, "little")
                + (
                    "DAnswer('fastexit~'+"
                    "CT(\"Script.Mod_Binary.12\"));"
                ).encode("utf-16-le")
                + b"\x00\x00"
            )

            report = audit_mod(root, profile="release")
            issue = next(
                item
                for item in report.issues
                if item.code == "script-dialog-lang-dat-missing"
            )
            self.assertIn("Script/Mod_Binary/12", issue.message)
            self.assertIn("номер объекта недоступен", issue.message)

    def test_release_connects_dialog_answers_to_packaged_script_language(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "AuditFixture"
            _mod(root)
            source = root / "SOURCE"
            source.mkdir()
            project = {
                "FileID": RSON_FILE_ID,
                "FileVersion": RSON_FILE_VERSION,
                "ScriptName": "Mod_Test",
                "Visual.Objects": [
                    {
                        "Dialogs": [
                            {
                                "Type": "TDialog",
                                "Name": "VisibleDialog",
                                "Parent": -1,
                                "#": 1,
                            },
                            {
                                "Type": "TDialogAnswer",
                                "Name": "fastexit",
                                "Parent": -1,
                                "#": 2,
                                "AMsg.Num": 0,
                                "Msg": "DAnswer(CT('Script.Mod_Test.1'));",
                            },
                        ]
                    }
                ],
                "Visual.Links": [
                    {
                        "Type": "TGraphLink",
                        "Begin": 1,
                        "End": 2,
                        "Nom": 0,
                        "Arrow": True,
                    }
                ],
            }
            (source / "Mod_Test.rson").write_text(
                json.dumps(project),
                encoding="utf-8",
            )
            (source / "Mod_Test.lang.txt").write_bytes(
                b"\xff\xfe"
                + (
                    "0=\r\n"
                    "1=DAnswer('fastexit~Visible answer')\r\n"
                ).encode("utf-16-le")
            )

            missing = audit_mod(root, profile="release")
            issue = next(
                item
                for item in missing.issues
                if item.code == "script-dialog-lang-dat-missing"
            )
            self.assertEqual(issue.severity, "error")
            self.assertIn("VisibleDialog", issue.message)
            self.assertIn(issue, missing.blocking_issues())

            lang_source = source / "Lang.txt"
            lang_source.write_text(
                "Script ^{\n"
                "  Mod_Test ~{\n"
                "    1=DAnswer('fastexit~Visible answer')\n"
                "  }\n"
                "}\n",
                encoding="cp1251",
            )
            lang = root / "DATA" / "Script" / "Lang.dat"
            lang.parent.mkdir(parents=True)
            Toolchain().convert_dat(lang_source, lang)
            code_stub = audit_mod(root, profile="release")
            stub_issue = next(
                item
                for item in code_stub.issues
                if item.code == "script-dialog-lang-value-code-stub"
            )
            self.assertIn("VisibleDialog", stub_issue.message)
            self.assertEqual(stub_issue.location, "Script/Mod_Test/1")

            lang_source.write_text(
                "Script ^{\n"
                "  Mod_Test ~{\n"
                "    1=Visible answer\n"
                "  }\n"
                "}\n",
                encoding="cp1251",
            )
            Toolchain().convert_dat(lang_source, lang, overwrite=True)
            runtime_missing = audit_mod(root, profile="release")
            location_issue = next(
                item
                for item in runtime_missing.issues
                if item.code == "script-dialog-lang-runtime-dat-missing"
            )
            self.assertIn("CFG/<язык>/Lang.dat", location_issue.message)
            self.assertIn(location_issue, runtime_missing.blocking_issues())

            runtime_lang = root / "CFG" / "Rus" / "Lang.dat"
            runtime_lang.parent.mkdir(parents=True)
            Toolchain().convert_dat(lang_source, runtime_lang)
            complete = audit_mod(root, profile="release")
            self.assertFalse(
                any(
                    item.code.startswith("script-dialog-lang-")
                    or item.code == "script-generated-lang-unpublished"
                    for item in complete.issues
                )
            )

    def test_release_warns_about_limited_game_text_notation(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "AuditFixture"
            _mod(root)
            source = root / "SOURCE" / "CFG"
            source.mkdir(parents=True)
            (source / "Lang_Rus.txt").write_text(
                "Status=80–100%: 4–6 транспортов; этап 3/48\n",
                encoding="utf-8",
            )

            report = audit_mod(root, profile="release")
            text_codes = [
                item.code
                for item in report.issues
                if item.validator == "game-text"
            ]
            self.assertIn("game-text-typographic-number-range", text_codes)
            self.assertIn("game-text-numeric-slash-notation", text_codes)
            self.assertEqual(
                text_codes.count("game-text-typographic-number-range"),
                2,
            )
            self.assertEqual(
                text_codes.count("game-text-numeric-slash-notation"),
                1,
            )
            self.assertFalse(
                any(
                    item.severity == "error"
                    and item.code.startswith("game-text-")
                    for item in report.issues
                )
            )

    def test_release_warns_when_mod_owned_quest_item_has_no_image(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "AuditFixture"
            _mod(root)
            source = root / "SOURCE"
            cfg = source / "CFG"
            cfg.mkdir(parents=True)
            (cfg / "Lang_Rus.txt").write_text(
                "UselessItems ^{\n"
                "  AuditCargo ^{\n"
                "    Name=Audit cargo\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )
            project = {
                "FileID": RSON_FILE_ID,
                "FileVersion": RSON_FILE_VERSION,
                "ScriptName": "QuestItemFixture",
                "Visual.Objects": [
                    {
                        "Operations": [
                            {
                                "Type": "Top",
                                "Name": "Turn",
                                "Parent": -1,
                                "#": 1,
                                "Code.Type": "Turn",
                                "Code": [
                                    "CreateQuestItem('AuditCargo', 2);",
                                    "CreateQuestItem('AuditCargo', 2);",
                                ],
                            }
                        ]
                    }
                ],
                "Visual.Links": [],
            }
            (source / "quest-item.rson").write_text(
                json.dumps(project),
                encoding="utf-8",
            )

            report = audit_mod(root, profile="release")
            matching = [
                item
                for item in report.issues
                if item.code == "runtime-quest-item-image-missing"
            ]
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0].severity, "warning")
            self.assertIn("Usl_FishCont", matching[0].message)

    def test_release_warns_about_quest_item_key_without_static_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "AuditFixture"
            _mod(root)
            source = root / "SOURCE"
            cfg = source / "CFG"
            cfg.mkdir(parents=True)
            (cfg / "Lang_Rus.txt").write_text(
                "UselessItems ^{\n"
                "  AuditCargo ^{\n"
                "    Name=Audit cargo\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )
            (cfg / "CacheData.txt").write_text(
                "Bm ^{\n"
                "  ItemsUseless ^{\n"
                "    2AuditCargo=Mods\\AuditFixture\\DATA\\ItemsUseless\\2AuditCargo.gi\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )
            project = {
                "FileID": RSON_FILE_ID,
                "FileVersion": RSON_FILE_VERSION,
                "ScriptName": "QuestItemKeyFixture",
                "Visual.Objects": [
                    {
                        "Operations": [
                            {
                                "Type": "Top",
                                "Name": "Turn",
                                "Parent": -1,
                                "#": 1,
                                "Code.Type": "Turn",
                                "Code": ["CreateQuestItem('AuditCargo', 2);"],
                            }
                        ]
                    }
                ],
                "Visual.Links": [],
            }
            (source / "quest-item.rson").write_text(
                json.dumps(project),
                encoding="utf-8",
            )
            image = root / "DATA" / "ItemsUseless" / "2AuditCargo.gi"
            image.parent.mkdir(parents=True)
            image.write_bytes(
                encode_gi(
                    RgbaImage(2, 2, bytes((20, 40, 60, 255)) * 4),
                    "0_32",
                )
            )

            report = audit_mod(root, profile="release")
            matching = [
                item
                for item in report.issues
                if item.code
                == "runtime-quest-item-image-registration-key-invalid"
            ]
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0].severity, "warning")
            self.assertIn("2AuditCargo_s", matching[0].message)
            self.assertIn("сам себя не регистрирует", matching[0].message)

    def test_release_blocks_reachable_custom_faction_without_emblem(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "AuditFixture"
            _mod(root)
            source = root / "SOURCE"
            cfg = source / "CFG"
            cfg.mkdir(parents=True)
            (cfg / "Main.txt").write_text(
                "Data ^{\n  Race ^{\n    Emblem ~{\n    }\n  }\n}\n",
                encoding="utf-8",
            )
            project = {
                "FileID": RSON_FILE_ID,
                "FileVersion": RSON_FILE_VERSION,
                "ScriptName": "CustomFactionFixture",
                "Visual.Objects": [
                    {
                        "Operations": [
                            {
                                "Type": "Top",
                                "Name": "Turn",
                                "Parent": -1,
                                "#": 1,
                                "Code.Type": "Turn",
                                "Code": [
                                    "ShipCustomFaction(Player(), 'SubFactionFixture');"
                                ],
                            }
                        ]
                    }
                ],
                "Visual.Links": [],
            }
            (source / "custom.rson").write_text(
                json.dumps(project),
                encoding="utf-8",
            )

            report = audit_mod(root, profile="release")
            issue = next(
                item
                for item in report.issues
                if item.code == "runtime-custom-faction-emblem-unregistered"
            )
            self.assertEqual(issue.severity, "error")
            self.assertIn(issue, report.blocking_issues())

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

    def test_empty_gai_placeholder_is_warning_not_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "AuditFixture"
            _mod(root)
            path = root / "DATA" / "empty.gai"
            header = bytearray(48 + 3 * 8)
            struct.pack_into("<4sI", header, 0, b"gai\0", 1)
            struct.pack_into("<III", header, 16, 20, 20, 3)
            auxiliary = b"timeline"
            struct.pack_into("<II", header, 32, len(header), len(auxiliary))
            path.write_bytes(bytes(header) + auxiliary)

            report = audit_mod(root, profile="release")
            check = next(item for item in report.checks if item.name == "resource-integrity")
            issue = next(
                item
                for item in check.issues
                if item.code == "resource-empty-animation-placeholder"
            )
            self.assertEqual(issue.severity, "warning")
            self.assertFalse(any(item.code == "resource-invalid" for item in check.issues))
            self.assertNotIn(issue, report.blocking_issues())

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

    def test_broken_text_quest_is_connected_to_unified_audit(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "AuditFixture"
            _mod(root)
            quest = root / "DATA" / "Quest" / "broken.qmm"
            quest.parent.mkdir(parents=True)
            quest.write_bytes(b"not-a-qmm")

            report = audit_mod(root, profile="release")
            check = next(item for item in report.checks if item.name == "text-quests")
            self.assertEqual(check.status, "issues")
            self.assertTrue(any(item.code == "quest-invalid" for item in check.issues))

    def test_python_sources_are_known_and_syntax_checked_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "AuditFixture"
            _mod(root)
            source = root / "SOURCE" / "Tools" / "broken.py"
            source.parent.mkdir(parents=True)
            source.write_text("def broken(:\n    pass\n", encoding="utf-8")

            report = audit_mod(root, profile="release")
            unknown = next(item for item in report.checks if item.name == "unknown-formats")
            python = next(item for item in report.checks if item.name == "python-sources")
            self.assertEqual(unknown.status, "passed")
            self.assertEqual(python.status, "issues")
            self.assertTrue(any(item.code == "python-syntax-invalid" for item in python.issues))

    def test_release_audits_quest_card_pqi_metadata_and_unused_assets(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "AuditFixture"
            _mod(root)
            quest = root / "DATA" / "Quest" / "Rus" / "Fixture.qmm"
            quest.parent.mkdir(parents=True)
            quest.write_bytes(write_qmm(_quest()))
            pqi = root / "DATA" / "PQI"
            pqi.mkdir()
            (pqi / "USED.jpg").write_bytes(_jpeg())
            (pqi / "UNUSED.jpg").write_bytes(_jpeg())

            source_cfg = root / "SOURCE" / "CFG"
            source_cfg.mkdir(parents=True)
            cache_source = source_cfg / "CacheData.txt"
            cache_source.write_text(
                "Bm ^{\n"
                "  PQI ^{\n"
                "    USED=Mods\\OtherMods\\AuditFixture\\DATA\\PQI\\USED.jpg\n"
                "    UNUSED=Mods\\OtherMods\\AuditFixture\\DATA\\PQI\\UNUSED.jpg\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )
            cache = root / "CFG" / "CacheData.dat"
            cache.parent.mkdir()
            Toolchain().convert_dat(cache_source, cache)

            lang_source = source_cfg / "Lang_Rus.txt"
            lang_source.write_text(
                "PlanetQuest ~{\n"
                "  ItemForPlanetQuest ~{\n    937=none\n  }\n"
                "  List ~{\n"
                "    937 ^{\n"
                "      Dif=55\n      Image=Bm.PQI.USED\n      Length=4\n      Name=Fixture\n"
                "    }\n"
                "  }\n"
                "  PlanetQuest ~{\n"
                "    937=Mods\\OtherMods\\AuditFixture\\DATA\\Quest\\Rus\\Fixture.qmm\n"
                "  }\n"
                "  StartText ~{\n    937=Start\n  }\n"
                "}\n",
                encoding="utf-8",
            )
            lang = root / "CFG" / "Rus" / "Lang.dat"
            lang.parent.mkdir()
            Toolchain().convert_dat(lang_source, lang)

            report = audit_mod(root, profile="release")
            cards = next(item for item in report.checks if item.name == "quest-cards")
            media = next(item for item in report.checks if item.name == "quest-media")
            self.assertEqual(cards.details["cards"][0]["hourglasses_expected"], 4)
            self.assertEqual(cards.details["cards"][0]["hardness"], 60)
            unused = [item for item in media.issues if item.code == "quest-pqi-asset-unused"]
            self.assertEqual([Path(item.path).name for item in unused], ["UNUSED.jpg"])
            self.assertFalse(
                any(item.code.startswith("quest-pqi-image-") for item in media.issues)
            )


if __name__ == "__main__":
    unittest.main()
