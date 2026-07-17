from __future__ import annotations

import json
import struct
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from srhd_modkit.quest_formula import QuestFormulaError, validate_quest_formula
from srhd_modkit.quests import (
    HEADER_QMM_6,
    HEADER_QMM_7,
    QuestDocument,
    QuestJump,
    QuestLocation,
    QuestLocationText,
    QuestMedia,
    QuestParameter,
    QuestParameterChange,
    QuestParameterCondition,
    QuestShowingRange,
    QuestStrings,
    build_quest_from_json,
    export_quest_json,
    parse_quest,
    validate_quest,
    verify_quest,
    write_qmm,
)


def _document() -> QuestDocument:
    params = (
        QuestParameter(
            0,
            10,
            0,
            True,
            0,
            True,
            False,
            "Счётчик",
            (QuestShowingRange(0, 10, "[p1]"),),
            "",
            "1+1",
            QuestMedia("start", None, "music"),
        ),
        QuestParameter(-5, 5, 1, False, 1, True, False, "Риск", (), "Провал", "0"),
    )
    no_changes = (QuestParameterChange(), QuestParameterChange())
    locations = (
        QuestLocation(False, 0, 0, 1, 0, 1, no_changes, (QuestLocationText("Старт"),), False, ""),
        QuestLocation(
            False,
            10,
            20,
            2,
            0,
            0,
            (
                QuestParameterChange(1, 0, 1),
                QuestParameterChange(0, 0, 3, "[p2]+1"),
            ),
            (QuestLocationText("Дальше", QuestMedia("scene", "click", None)),),
            False,
            "",
        ),
        QuestLocation(False, 20, 20, 3, 0, 3, no_changes, (QuestLocationText("Успех"),), False, ""),
    )
    conditions = (
        QuestParameterCondition(0, 10),
        QuestParameterCondition(-5, 5),
    )
    jumps = (
        QuestJump(1.0, False, 1, 1, 2, False, 0, 0, no_changes, conditions, "[p1]>=0", "Идти", ""),
        QuestJump(1.0, False, 2, 2, 3, False, 0, 0, no_changes, conditions, "", "Завершить", ""),
    )
    return QuestDocument(
        HEADER_QMM_7,
        1,
        2,
        "fixture",
        4,
        1,
        4,
        1,
        4,
        0,
        800,
        600,
        16,
        12,
        0,
        50,
        params,
        QuestStrings("звезда", None, None, "планета", "дата", "деньги", "откуда", "система", "рейнджер"),
        "Победа",
        "Задание",
        locations,
        jumps,
    )


class QuestTests(unittest.TestCase):
    def test_qmm_writer_roundtrips_semantically_and_deterministically(self) -> None:
        document = _document()
        payload = write_qmm(document)
        reparsed = parse_quest(payload)
        self.assertEqual(document.semantic_dict(), reparsed.semantic_dict())
        self.assertEqual(write_qmm(reparsed), payload)

    def test_qmm6_is_read_and_upgraded_to_qmm7(self) -> None:
        payload = write_qmm(replace(_document(), change_log=None))
        qmm6 = struct.pack("<i", HEADER_QMM_6) + payload[16:]
        parsed = parse_quest(qmm6)
        self.assertEqual(parsed.header, HEADER_QMM_6)
        upgraded = parse_quest(write_qmm(parsed))
        self.assertEqual(upgraded.header, HEADER_QMM_7)
        self.assertEqual(parsed.semantic_dict(), upgraded.semantic_dict())

    def test_json_export_and_build_need_no_external_editor(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "quest.qmm"
            source.write_bytes(write_qmm(_document()))
            model = root / "quest.json"
            export_quest_json(source, model)
            raw = json.loads(model.read_text(encoding="utf-8"))
            self.assertEqual(raw["schema"], "srhd-modkit-quest-v1")
            output = root / "rebuilt.qmm"
            result = build_quest_from_json(model, output)
            self.assertTrue(result["verified"])
            self.assertTrue(verify_quest(output)["deterministic"])

    def test_validation_finds_missing_target_and_bad_formula_reference(self) -> None:
        document = _document()
        broken_jump = QuestJump(
            1.0,
            False,
            99,
            1,
            999,
            False,
            0,
            0,
            document.jumps[0].parameter_changes,
            document.jumps[0].parameter_conditions,
            "[p99]",
            "",
            "",
        )
        broken = replace(document, jumps=document.jumps + (broken_jump,))
        codes = {item.code for item in validate_quest(broken)}
        self.assertIn("quest-jump-target-missing", codes)
        self.assertIn("quest-formula-invalid", codes)

    def test_formula_parser_matches_tge_parameter_and_range_syntax(self) -> None:
        node = validate_quest_formula("[p1] div100 mod 3 + [1..4;8]", params_count=2)
        self.assertEqual(node.parameter_indices, frozenset({0}))
        with self.assertRaises(QuestFormulaError):
            validate_quest_formula("[p3]+1", params_count=2)

    def test_automatic_empty_jump_cycle_is_reported(self) -> None:
        document = _document()
        no_changes = (QuestParameterChange(), QuestParameterChange())
        conditions = tuple(
            QuestParameterCondition(param.minimum, param.maximum) for param in document.parameters
        )
        locations = (
            QuestLocation(False, 0, 0, 1, 0, 1, no_changes, (QuestLocationText(""),), False, ""),
            QuestLocation(False, 0, 0, 2, 0, 2, no_changes, (QuestLocationText(""),), False, ""),
        )
        jumps = (
            QuestJump(1.0, False, 1, 1, 2, False, 0, 0, no_changes, conditions, "", "", ""),
            QuestJump(1.0, False, 2, 2, 1, False, 0, 0, no_changes, conditions, "", "", ""),
        )
        cycle = replace(document, locations=locations, jumps=jumps)
        self.assertTrue(any(item.code == "quest-automatic-cycle" for item in validate_quest(cycle)))


if __name__ == "__main__":
    unittest.main()
