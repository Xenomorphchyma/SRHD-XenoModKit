from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .quest_formula import QuestFormulaError, validate_quest_formula


QUEST_SCHEMA = "srhd-modkit-quest-v1"
QUEST_REPORT_SCHEMA = "srhd-modkit-quest-report-v1"
HEADER_QM_2 = 0x423A35D2
HEADER_QM_3 = 0x423A35D3
HEADER_QM_4 = 0x423A35D4
HEADER_QMM_6 = 0x423A35D6
HEADER_QMM_7 = 0x423A35D7
HEADER_QMM_7_OLD_BEHAVIOUR = 0x069F6BD7
SUPPORTED_HEADERS = {
    HEADER_QM_2,
    HEADER_QM_3,
    HEADER_QM_4,
    HEADER_QMM_6,
    HEADER_QMM_7,
    HEADER_QMM_7_OLD_BEHAVIOUR,
}
LEGACY_HEADERS = {HEADER_QM_2, HEADER_QM_3, HEADER_QM_4}
QMM_HEADERS = {HEADER_QMM_6, HEADER_QMM_7, HEADER_QMM_7_OLD_BEHAVIOUR}
LOCATION_TEXTS_QM = 10
MAX_PARAMETERS = 4096
MAX_RECORDS = 1_000_000
MAX_STRING_UNITS = 16_000_000
MAX_QUEST_SIZE = 512 * 1024 * 1024


class QuestFormatError(ValueError):
    """A QM/QMM file is truncated, unsafe, or contradicts its own structure."""


@dataclass(frozen=True, slots=True)
class QuestMedia:
    img: str | None = None
    sound: str | None = None
    track: str | None = None


@dataclass(frozen=True, slots=True)
class QuestShowingRange:
    from_value: int
    to_value: int
    text: str


@dataclass(frozen=True, slots=True)
class QuestParameter:
    minimum: int
    maximum: int
    param_type: int
    show_when_zero: bool
    crit_type: int
    active: bool
    is_money: bool
    name: str
    showing_info: tuple[QuestShowingRange, ...]
    crit_value_text: str
    starting_formula: str
    media: QuestMedia = field(default_factory=QuestMedia)


@dataclass(frozen=True, slots=True)
class QuestParameterChange:
    change: int = 0
    showing_type: int = 0
    change_type: int = 1
    changing_formula: str = ""
    crit_text: str = ""
    media: QuestMedia = field(default_factory=QuestMedia)


@dataclass(frozen=True, slots=True)
class QuestParameterCondition:
    must_from: int
    must_to: int
    equal_values: tuple[int, ...] = ()
    equal_flag: bool = False
    mod_values: tuple[int, ...] = ()
    mod_flag: bool = False


@dataclass(frozen=True, slots=True)
class QuestLocationText:
    text: str
    media: QuestMedia = field(default_factory=QuestMedia)


@dataclass(frozen=True, slots=True)
class QuestLocation:
    day_passed: bool
    x: int
    y: int
    id: int
    max_visits: int
    location_type: int
    parameter_changes: tuple[QuestParameterChange, ...]
    texts: tuple[QuestLocationText, ...]
    text_by_formula: bool
    text_select_formula: str


@dataclass(frozen=True, slots=True)
class QuestJump:
    priority: float
    day_passed: bool
    id: int
    from_location_id: int
    to_location_id: int
    always_show: bool
    jumping_count_limit: int
    showing_order: int
    parameter_changes: tuple[QuestParameterChange, ...]
    parameter_conditions: tuple[QuestParameterCondition, ...]
    formula_to_pass: str
    text: str
    description: str
    media: QuestMedia = field(default_factory=QuestMedia)


@dataclass(frozen=True, slots=True)
class QuestStrings:
    to_star: str
    parsec: str | None
    artefact: str | None
    to_planet: str
    date: str
    money: str
    from_planet: str
    from_star: str
    ranger: str


@dataclass(frozen=True, slots=True)
class QuestDocument:
    header: int
    major_version: int | None
    minor_version: int | None
    change_log: str | None
    giving_race: int
    when_done: int
    planet_race: int
    player_career: int
    player_race: int
    reputation_change: int
    screen_width: int
    screen_height: int
    grid_width: int
    grid_height: int
    default_jump_count_limit: int
    hardness: int
    parameters: tuple[QuestParameter, ...]
    strings: QuestStrings
    success_text: str
    task_text: str
    locations: tuple[QuestLocation, ...]
    jumps: tuple[QuestJump, ...]

    @property
    def source_format(self) -> str:
        return "QMM" if self.header in QMM_HEADERS else "QM"

    @property
    def old_tge_behaviour(self) -> bool:
        return self.header in LEGACY_HEADERS or self.header == HEADER_QMM_7_OLD_BEHAVIOUR

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value.update(
            {
                "schema": QUEST_SCHEMA,
                "source_format": self.source_format,
                "header_hex": f"0x{self.header:08x}",
                "old_tge_behaviour": self.old_tge_behaviour,
            }
        )
        return value

    def semantic_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for key in ("header", "major_version", "minor_version", "change_log"):
            value.pop(key, None)
        # QMM replaced the two legacy substitution labels with engine-defined
        # tokens.  The reference TGE conversion deliberately does not serialize
        # them, so they are format metadata rather than QMM gameplay state.
        value["strings"]["parsec"] = None
        value["strings"]["artefact"] = None
        return value

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "QuestDocument":
        schema = raw.get("schema")
        if schema not in {None, QUEST_SCHEMA}:
            raise QuestFormatError(f"Неизвестная схема JSON квеста: {schema!r}")

        def media(value: Mapping[str, Any] | None) -> QuestMedia:
            value = value or {}
            return QuestMedia(value.get("img"), value.get("sound"), value.get("track"))

        def showing(value: Mapping[str, Any]) -> QuestShowingRange:
            return QuestShowingRange(
                int(value["from_value"]), int(value["to_value"]), str(value.get("text", ""))
            )

        def parameter(value: Mapping[str, Any]) -> QuestParameter:
            return QuestParameter(
                int(value["minimum"]),
                int(value["maximum"]),
                int(value["param_type"]),
                bool(value["show_when_zero"]),
                int(value["crit_type"]),
                bool(value["active"]),
                bool(value["is_money"]),
                str(value.get("name", "")),
                tuple(showing(item) for item in value.get("showing_info", ())),
                str(value.get("crit_value_text", "")),
                str(value.get("starting_formula", "")),
                media(value.get("media")),
            )

        def change(value: Mapping[str, Any]) -> QuestParameterChange:
            return QuestParameterChange(
                int(value.get("change", 0)),
                int(value.get("showing_type", 0)),
                int(value.get("change_type", 1)),
                str(value.get("changing_formula", "")),
                str(value.get("crit_text", "")),
                media(value.get("media")),
            )

        def condition(value: Mapping[str, Any]) -> QuestParameterCondition:
            return QuestParameterCondition(
                int(value["must_from"]),
                int(value["must_to"]),
                tuple(int(item) for item in value.get("equal_values", ())),
                bool(value.get("equal_flag", False)),
                tuple(int(item) for item in value.get("mod_values", ())),
                bool(value.get("mod_flag", False)),
            )

        def location_text(value: Mapping[str, Any]) -> QuestLocationText:
            return QuestLocationText(str(value.get("text", "")), media(value.get("media")))

        def location(value: Mapping[str, Any]) -> QuestLocation:
            return QuestLocation(
                bool(value["day_passed"]),
                int(value["x"]),
                int(value["y"]),
                int(value["id"]),
                int(value.get("max_visits", 0)),
                int(value["location_type"]),
                tuple(change(item) for item in value.get("parameter_changes", ())),
                tuple(location_text(item) for item in value.get("texts", ())),
                bool(value.get("text_by_formula", False)),
                str(value.get("text_select_formula", "")),
            )

        def jump(value: Mapping[str, Any]) -> QuestJump:
            return QuestJump(
                float(value["priority"]),
                bool(value["day_passed"]),
                int(value["id"]),
                int(value["from_location_id"]),
                int(value["to_location_id"]),
                bool(value["always_show"]),
                int(value.get("jumping_count_limit", 0)),
                int(value.get("showing_order", 0)),
                tuple(change(item) for item in value.get("parameter_changes", ())),
                tuple(condition(item) for item in value.get("parameter_conditions", ())),
                str(value.get("formula_to_pass", "")),
                str(value.get("text", "")),
                str(value.get("description", "")),
                media(value.get("media")),
            )

        strings_raw = raw["strings"]
        strings = QuestStrings(
            str(strings_raw.get("to_star", "")),
            strings_raw.get("parsec"),
            strings_raw.get("artefact"),
            str(strings_raw.get("to_planet", "")),
            str(strings_raw.get("date", "")),
            str(strings_raw.get("money", "")),
            str(strings_raw.get("from_planet", "")),
            str(strings_raw.get("from_star", "")),
            str(strings_raw.get("ranger", "")),
        )
        return cls(
            int(raw["header"]),
            int(raw["major_version"]) if raw.get("major_version") is not None else None,
            int(raw["minor_version"]) if raw.get("minor_version") is not None else None,
            raw.get("change_log"),
            int(raw["giving_race"]),
            int(raw["when_done"]),
            int(raw["planet_race"]),
            int(raw["player_career"]),
            int(raw["player_race"]),
            int(raw["reputation_change"]),
            int(raw["screen_width"]),
            int(raw["screen_height"]),
            int(raw["grid_width"]),
            int(raw["grid_height"]),
            int(raw["default_jump_count_limit"]),
            int(raw["hardness"]),
            tuple(parameter(item) for item in raw.get("parameters", ())),
            strings,
            str(raw.get("success_text", "")),
            str(raw.get("task_text", "")),
            tuple(location(item) for item in raw.get("locations", ())),
            tuple(jump(item) for item in raw.get("jumps", ())),
        )


@dataclass(frozen=True, slots=True)
class QuestIssue:
    severity: str
    code: str
    message: str
    location: str | None = None
    evidence: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class _Reader:
    def __init__(self, data: bytes):
        self.data = memoryview(data)
        self.offset = 0

    def need(self, size: int, label: str) -> None:
        if size < 0 or self.offset + size > len(self.data):
            raise QuestFormatError(
                f"{label}: требуется {size} байт по смещению 0x{self.offset:x}, "
                f"размер файла 0x{len(self.data):x}"
            )

    def int32(self, label: str) -> int:
        self.need(4, label)
        value = struct.unpack_from("<i", self.data, self.offset)[0]
        self.offset += 4
        return value

    def byte(self, label: str) -> int:
        self.need(1, label)
        value = self.data[self.offset]
        self.offset += 1
        return int(value)

    def float64(self, label: str) -> float:
        self.need(8, label)
        value = struct.unpack_from("<d", self.data, self.offset)[0]
        self.offset += 8
        if not math.isfinite(value):
            raise QuestFormatError(f"{label}: значение не является конечным числом")
        return value

    def skip(self, size: int, label: str) -> None:
        self.need(size, label)
        self.offset += size

    def string(self, label: str, *, optional: bool = False) -> str | None:
        present = self.int32(f"{label} presence")
        if not present:
            return None if optional else ""
        length = self.int32(f"{label} length")
        if length < 0 or length > MAX_STRING_UNITS:
            raise QuestFormatError(f"{label}: неправдоподобная длина UTF-16 {length}")
        size = length * 2
        self.need(size, label)
        raw = bytes(self.data[self.offset : self.offset + size])
        self.offset += size
        try:
            return raw.decode("utf-16le")
        except UnicodeDecodeError as exc:
            raise QuestFormatError(f"{label}: повреждённый UTF-16LE") from exc

    def finish(self) -> None:
        if self.offset != len(self.data):
            raise QuestFormatError(
                f"После структуры квеста осталось {len(self.data) - self.offset} байт "
                f"по смещению 0x{self.offset:x}"
            )


class _Writer:
    def __init__(self):
        self.data = bytearray()

    def int32(self, value: int) -> None:
        try:
            self.data.extend(struct.pack("<i", value))
        except struct.error as exc:
            raise QuestFormatError(f"Значение {value} не помещается в Int32") from exc

    def byte(self, value: int) -> None:
        if not 0 <= value <= 255:
            raise QuestFormatError(f"Значение {value} не помещается в байт")
        self.data.append(value)

    def float64(self, value: float) -> None:
        if not math.isfinite(value):
            raise QuestFormatError("Приоритет перехода должен быть конечным числом")
        self.data.extend(struct.pack("<d", value))

    def string(self, value: str | None) -> None:
        if value is None:
            self.int32(0)
            return
        encoded = value.encode("utf-16le")
        self.int32(1)
        self.int32(len(encoded) // 2)
        self.data.extend(encoded)


def _bounded_count(value: int, label: str, maximum: int = MAX_RECORDS) -> int:
    if value < 0 or value > maximum:
        raise QuestFormatError(f"{label}: неправдоподобное количество {value}")
    return value


def _bool_byte(reader: _Reader, label: str) -> bool:
    return bool(reader.byte(label))


def _bool_int(reader: _Reader, label: str) -> bool:
    return bool(reader.int32(label))


def _change_type(*, formula: bool, value: bool, percentage: bool) -> int:
    return 3 if formula else 0 if value else 2 if percentage else 1


def _default_change() -> QuestParameterChange:
    return QuestParameterChange()


def _default_condition(param: QuestParameter) -> QuestParameterCondition:
    return QuestParameterCondition(param.minimum, param.maximum)


def _parse_parameter(reader: _Reader, *, qmm: bool, index: int) -> QuestParameter:
    prefix = f"parameter[{index}]"
    minimum = reader.int32(f"{prefix}.minimum")
    maximum = reader.int32(f"{prefix}.maximum")
    if qmm:
        param_type = reader.byte(f"{prefix}.type")
        unknown = tuple(reader.byte(f"{prefix}.reserved[{item}]") for item in range(3))
        if any(unknown):
            raise QuestFormatError(f"{prefix}: резервные байты не равны нулю: {unknown}")
    else:
        reader.int32(f"{prefix}.legacy-reserved-1")
        param_type = reader.byte(f"{prefix}.type")
        reader.int32(f"{prefix}.legacy-reserved-2")
    show_when_zero = _bool_byte(reader, f"{prefix}.show-when-zero")
    crit_type = reader.byte(f"{prefix}.crit-type")
    active = _bool_byte(reader, f"{prefix}.active")
    showing_count = _bounded_count(reader.int32(f"{prefix}.showing-count"), f"{prefix}.showing-count")
    is_money = _bool_byte(reader, f"{prefix}.is-money")
    name = str(reader.string(f"{prefix}.name"))
    showing: list[QuestShowingRange] = []
    for range_index in range(showing_count):
        showing.append(
            QuestShowingRange(
                reader.int32(f"{prefix}.range[{range_index}].from"),
                reader.int32(f"{prefix}.range[{range_index}].to"),
                str(reader.string(f"{prefix}.range[{range_index}].text")),
            )
        )
    crit_value_text = str(reader.string(f"{prefix}.crit-text"))
    media = (
        QuestMedia(
            reader.string(f"{prefix}.image", optional=True),
            reader.string(f"{prefix}.sound", optional=True),
            reader.string(f"{prefix}.track", optional=True),
        )
        if qmm
        else QuestMedia()
    )
    starting_formula = str(reader.string(f"{prefix}.starting"))
    return QuestParameter(
        minimum,
        maximum,
        param_type,
        show_when_zero,
        crit_type,
        active,
        is_money,
        name,
        tuple(showing),
        crit_value_text,
        starting_formula,
        media,
    )


def _parse_qmm_change(reader: _Reader, prefix: str) -> tuple[int, QuestParameterChange]:
    param_id = reader.int32(f"{prefix}.parameter-id")
    change = reader.int32(f"{prefix}.change")
    showing_type = reader.byte(f"{prefix}.showing-type")
    change_type = reader.byte(f"{prefix}.change-type")
    formula = str(reader.string(f"{prefix}.formula"))
    crit_text = str(reader.string(f"{prefix}.crit-text"))
    media = QuestMedia(
        reader.string(f"{prefix}.image", optional=True),
        reader.string(f"{prefix}.sound", optional=True),
        reader.string(f"{prefix}.track", optional=True),
    )
    return param_id, QuestParameterChange(change, showing_type, change_type, formula, crit_text, media)


def _parse_location(
    reader: _Reader,
    *,
    qmm: bool,
    params_count: int,
    index: int,
) -> QuestLocation:
    prefix = f"location[{index}]"
    day_passed = _bool_int(reader, f"{prefix}.day-passed")
    x = reader.int32(f"{prefix}.x")
    y = reader.int32(f"{prefix}.y")
    location_id = reader.int32(f"{prefix}.id")
    if qmm:
        max_visits = reader.int32(f"{prefix}.max-visits")
        location_type = reader.byte(f"{prefix}.type")
        changes = [_default_change() for _ in range(params_count)]
        affected = _bounded_count(reader.int32(f"{prefix}.affected"), f"{prefix}.affected", params_count)
        seen: set[int] = set()
        for change_index in range(affected):
            param_id, change = _parse_qmm_change(reader, f"{prefix}.change[{change_index}]")
            if not 1 <= param_id <= params_count:
                raise QuestFormatError(f"{prefix}: параметр изменения {param_id} вне диапазона")
            if param_id in seen:
                raise QuestFormatError(f"{prefix}: параметр изменения {param_id} повторяется")
            seen.add(param_id)
            changes[param_id - 1] = change
        text_count = _bounded_count(reader.int32(f"{prefix}.text-count"), f"{prefix}.text-count")
        texts = tuple(
            QuestLocationText(
                str(reader.string(f"{prefix}.text[{text_index}]")),
                QuestMedia(
                    reader.string(f"{prefix}.text[{text_index}].image", optional=True),
                    reader.string(f"{prefix}.text[{text_index}].sound", optional=True),
                    reader.string(f"{prefix}.text[{text_index}].track", optional=True),
                ),
            )
            for text_index in range(text_count)
        )
        text_by_formula = _bool_byte(reader, f"{prefix}.text-by-formula")
        text_select_formula = str(reader.string(f"{prefix}.text-select-formula"))
    else:
        max_visits = 0
        flags = tuple(_bool_byte(reader, f"{prefix}.legacy-flag[{item}]") for item in range(5))
        starting, success, failed, deadly, empty = flags
        location_type = 1 if starting else 3 if success else 2 if empty else 5 if deadly else 4 if failed else 0
        changes = []
        for param_index in range(params_count):
            item = f"{prefix}.change[{param_index}]"
            reader.skip(12, f"{item}.reserved-1")
            change = reader.int32(f"{item}.change")
            showing_type = reader.byte(f"{item}.showing-type")
            reader.skip(4, f"{item}.reserved-2")
            percentage = _bool_byte(reader, f"{item}.percentage")
            value = _bool_byte(reader, f"{item}.value")
            formula = _bool_byte(reader, f"{item}.formula-flag")
            formula_text = str(reader.string(f"{item}.formula"))
            reader.skip(10, f"{item}.reserved-3")
            crit_text = str(reader.string(f"{item}.crit-text"))
            changes.append(
                QuestParameterChange(
                    change,
                    showing_type,
                    _change_type(formula=formula, value=value, percentage=percentage),
                    formula_text,
                    crit_text,
                )
            )
        texts = tuple(
            QuestLocationText(str(reader.string(f"{prefix}.text[{text_index}]")))
            for text_index in range(LOCATION_TEXTS_QM)
        )
        text_by_formula = _bool_byte(reader, f"{prefix}.text-by-formula")
        reader.skip(4, f"{prefix}.legacy-text-reserved")
        reader.string(f"{prefix}.legacy-text-1")
        reader.string(f"{prefix}.legacy-text-2")
        text_select_formula = str(reader.string(f"{prefix}.text-select-formula"))
    return QuestLocation(
        day_passed,
        x,
        y,
        location_id,
        max_visits,
        location_type,
        tuple(changes),
        texts,
        text_by_formula,
        text_select_formula,
    )


def _parse_jump(
    reader: _Reader,
    *,
    qmm: bool,
    parameters: Sequence[QuestParameter],
    index: int,
) -> QuestJump:
    prefix = f"jump[{index}]"
    priority = reader.float64(f"{prefix}.priority")
    day_passed = _bool_int(reader, f"{prefix}.day-passed")
    jump_id = reader.int32(f"{prefix}.id")
    from_id = reader.int32(f"{prefix}.from")
    to_id = reader.int32(f"{prefix}.to")
    if not qmm:
        reader.skip(1, f"{prefix}.legacy-reserved")
    always_show = _bool_byte(reader, f"{prefix}.always-show")
    jumping_count_limit = reader.int32(f"{prefix}.count-limit")
    showing_order = reader.int32(f"{prefix}.showing-order")
    if qmm:
        changes = [_default_change() for _ in parameters]
        conditions = [_default_condition(param) for param in parameters]
        condition_count = _bounded_count(
            reader.int32(f"{prefix}.condition-count"), f"{prefix}.condition-count", len(parameters)
        )
        seen_conditions: set[int] = set()
        for condition_index in range(condition_count):
            item = f"{prefix}.condition[{condition_index}]"
            param_id = reader.int32(f"{item}.parameter-id")
            if not 1 <= param_id <= len(parameters) or param_id in seen_conditions:
                raise QuestFormatError(f"{item}: некорректный или повторный параметр {param_id}")
            seen_conditions.add(param_id)
            must_from = reader.int32(f"{item}.from")
            must_to = reader.int32(f"{item}.to")
            equal_count = _bounded_count(reader.int32(f"{item}.equal-count"), f"{item}.equal-count")
            equal_flag = _bool_byte(reader, f"{item}.equal-flag")
            equal_values = tuple(reader.int32(f"{item}.equal[{n}]") for n in range(equal_count))
            mod_count = _bounded_count(reader.int32(f"{item}.mod-count"), f"{item}.mod-count")
            mod_flag = _bool_byte(reader, f"{item}.mod-flag")
            mod_values = tuple(reader.int32(f"{item}.mod[{n}]") for n in range(mod_count))
            conditions[param_id - 1] = QuestParameterCondition(
                must_from, must_to, equal_values, equal_flag, mod_values, mod_flag
            )
        change_count = _bounded_count(
            reader.int32(f"{prefix}.change-count"), f"{prefix}.change-count", len(parameters)
        )
        seen_changes: set[int] = set()
        for change_index in range(change_count):
            param_id, change = _parse_qmm_change(reader, f"{prefix}.change[{change_index}]")
            if not 1 <= param_id <= len(parameters) or param_id in seen_changes:
                raise QuestFormatError(
                    f"{prefix}.change[{change_index}]: некорректный или повторный параметр {param_id}"
                )
            seen_changes.add(param_id)
            changes[param_id - 1] = change
    else:
        changes = []
        conditions = []
        for param_index in range(len(parameters)):
            item = f"{prefix}.parameter[{param_index}]"
            reader.skip(4, f"{item}.reserved-1")
            must_from = reader.int32(f"{item}.from")
            must_to = reader.int32(f"{item}.to")
            change = reader.int32(f"{item}.change")
            showing_type = reader.int32(f"{item}.showing-type")
            reader.skip(1, f"{item}.reserved-2")
            percentage = _bool_byte(reader, f"{item}.percentage")
            value = _bool_byte(reader, f"{item}.value")
            formula = _bool_byte(reader, f"{item}.formula-flag")
            formula_text = str(reader.string(f"{item}.formula"))
            equal_count = _bounded_count(reader.int32(f"{item}.equal-count"), f"{item}.equal-count")
            equal_flag = _bool_byte(reader, f"{item}.equal-flag")
            equal_values = tuple(reader.int32(f"{item}.equal[{n}]") for n in range(equal_count))
            mod_count = _bounded_count(reader.int32(f"{item}.mod-count"), f"{item}.mod-count")
            mod_flag = _bool_byte(reader, f"{item}.mod-flag")
            mod_values = tuple(reader.int32(f"{item}.mod[{n}]") for n in range(mod_count))
            crit_text = str(reader.string(f"{item}.crit-text"))
            changes.append(
                QuestParameterChange(
                    change,
                    showing_type,
                    _change_type(formula=formula, value=value, percentage=percentage),
                    formula_text,
                    crit_text,
                )
            )
            conditions.append(
                QuestParameterCondition(
                    must_from, must_to, equal_values, equal_flag, mod_values, mod_flag
                )
            )
    formula_to_pass = str(reader.string(f"{prefix}.formula-to-pass"))
    text = str(reader.string(f"{prefix}.text"))
    description = str(reader.string(f"{prefix}.description"))
    media = (
        QuestMedia(
            reader.string(f"{prefix}.image", optional=True),
            reader.string(f"{prefix}.sound", optional=True),
            reader.string(f"{prefix}.track", optional=True),
        )
        if qmm
        else QuestMedia()
    )
    return QuestJump(
        priority,
        day_passed,
        jump_id,
        from_id,
        to_id,
        always_show,
        jumping_count_limit,
        showing_order,
        tuple(changes),
        tuple(conditions),
        formula_to_pass,
        text,
        description,
        media,
    )


def parse_quest(data: bytes | bytearray | memoryview) -> QuestDocument:
    if len(data) > MAX_QUEST_SIZE:
        raise QuestFormatError(
            f"QM/QMM превышает безопасный предел {MAX_QUEST_SIZE} байт: {len(data)}"
        )
    raw = bytes(data)
    reader = _Reader(raw)
    header = reader.int32("header")
    if header not in SUPPORTED_HEADERS:
        raise QuestFormatError(f"Неизвестный заголовок QM/QMM 0x{header & 0xffffffff:08x}")
    qmm = header in QMM_HEADERS
    if qmm:
        major = reader.int32("major-version") if header != HEADER_QMM_6 else None
        minor = reader.int32("minor-version") if header != HEADER_QMM_6 else None
        change_log = reader.string("change-log", optional=True) if header != HEADER_QMM_6 else None
        giving_race = reader.byte("giving-race")
        when_done = reader.byte("when-done")
        planet_race = reader.byte("planet-race")
        player_career = reader.byte("player-career")
        player_race = reader.byte("player-race")
        reputation = reader.int32("reputation-change")
        screen_width = reader.int32("screen-width")
        screen_height = reader.int32("screen-height")
        grid_width = reader.int32("grid-width")
        grid_height = reader.int32("grid-height")
        default_jump_limit = reader.int32("default-jump-count-limit")
        hardness = reader.int32("hardness")
        params_count = _bounded_count(reader.int32("parameters-count"), "parameters-count", MAX_PARAMETERS)
    else:
        major = minor = None
        change_log = None
        params_count = {HEADER_QM_2: 24, HEADER_QM_3: 48, HEADER_QM_4: 96}[header]
        reader.int32("legacy-reserved-1")
        giving_race = reader.byte("giving-race")
        when_done = reader.byte("when-done")
        reader.int32("legacy-reserved-2")
        planet_race = reader.byte("planet-race")
        reader.int32("legacy-reserved-3")
        player_career = reader.byte("player-career")
        reader.int32("legacy-reserved-4")
        player_race = reader.byte("player-race")
        reputation = reader.int32("reputation-change")
        screen_width = reader.int32("screen-width")
        screen_height = reader.int32("screen-height")
        grid_width = reader.int32("grid-width")
        grid_height = reader.int32("grid-height")
        reader.int32("legacy-reserved-5")
        default_jump_limit = reader.int32("default-jump-count-limit")
        hardness = reader.int32("hardness")

    parameters = tuple(
        _parse_parameter(reader, qmm=qmm, index=index) for index in range(params_count)
    )
    strings = QuestStrings(
        str(reader.string("strings.to-star")),
        reader.string("strings.parsec", optional=True) if not qmm else None,
        reader.string("strings.artefact", optional=True) if not qmm else None,
        str(reader.string("strings.to-planet")),
        str(reader.string("strings.date")),
        str(reader.string("strings.money")),
        str(reader.string("strings.from-planet")),
        str(reader.string("strings.from-star")),
        str(reader.string("strings.ranger")),
    )
    locations_count = _bounded_count(reader.int32("locations-count"), "locations-count")
    jumps_count = _bounded_count(reader.int32("jumps-count"), "jumps-count")
    success_text = str(reader.string("success-text"))
    task_text = str(reader.string("task-text"))
    if not qmm:
        reader.string("legacy-unknown-text")
    locations = tuple(
        _parse_location(reader, qmm=qmm, params_count=params_count, index=index)
        for index in range(locations_count)
    )
    jumps = tuple(
        _parse_jump(reader, qmm=qmm, parameters=parameters, index=index)
        for index in range(jumps_count)
    )
    reader.finish()
    return QuestDocument(
        header,
        major,
        minor,
        change_log,
        giving_race,
        when_done,
        planet_race,
        player_career,
        player_race,
        reputation,
        screen_width,
        screen_height,
        grid_width,
        grid_height,
        default_jump_limit,
        hardness,
        parameters,
        strings,
        success_text,
        task_text,
        locations,
        jumps,
    )


def load_quest(path: str | Path) -> QuestDocument:
    path = Path(path)
    size = path.stat().st_size
    if size > MAX_QUEST_SIZE:
        raise QuestFormatError(
            f"QM/QMM превышает безопасный предел {MAX_QUEST_SIZE} байт: {size}"
        )
    return parse_quest(path.read_bytes())


def _change_changed(value: QuestParameterChange) -> bool:
    return value != QuestParameterChange()


def _condition_changed(value: QuestParameterCondition, param: QuestParameter) -> bool:
    return value != _default_condition(param)


def _write_change(writer: _Writer, value: QuestParameterChange, index: int) -> None:
    writer.int32(index + 1)
    writer.int32(value.change)
    writer.byte(value.showing_type)
    writer.byte(value.change_type)
    writer.string(value.changing_formula)
    writer.string(value.crit_text)
    writer.string(value.media.img)
    writer.string(value.media.sound)
    writer.string(value.media.track)


def write_qmm(document: QuestDocument) -> bytes:
    writer = _Writer()
    target_header = (
        HEADER_QMM_7_OLD_BEHAVIOUR
        if document.old_tge_behaviour
        else HEADER_QMM_7
    )
    writer.int32(target_header)
    writer.int32(1 if document.major_version is None else document.major_version)
    writer.int32(0 if document.minor_version is None else document.minor_version)
    writer.string(document.change_log)
    for value in (
        document.giving_race,
        document.when_done,
        document.planet_race,
        document.player_career,
        document.player_race,
    ):
        writer.byte(value)
    writer.int32(document.reputation_change)
    writer.int32(document.screen_width)
    writer.int32(document.screen_height)
    writer.int32(document.grid_width)
    writer.int32(document.grid_height)
    writer.int32(document.default_jump_count_limit)
    writer.int32(document.hardness)
    writer.int32(len(document.parameters))
    for param in document.parameters:
        writer.int32(param.minimum)
        writer.int32(param.maximum)
        writer.byte(param.param_type)
        writer.data.extend(b"\0\0\0")
        writer.byte(1 if param.show_when_zero else 0)
        writer.byte(param.crit_type)
        writer.byte(1 if param.active else 0)
        writer.int32(len(param.showing_info))
        writer.byte(1 if param.is_money else 0)
        writer.string(param.name)
        for item in param.showing_info:
            writer.int32(item.from_value)
            writer.int32(item.to_value)
            writer.string(item.text)
        writer.string(param.crit_value_text)
        writer.string(param.media.img)
        writer.string(param.media.sound)
        writer.string(param.media.track)
        writer.string(param.starting_formula)
    for value in (
        document.strings.to_star,
        document.strings.to_planet,
        document.strings.date,
        document.strings.money,
        document.strings.from_planet,
        document.strings.from_star,
        document.strings.ranger,
    ):
        writer.string(value)
    writer.int32(len(document.locations))
    writer.int32(len(document.jumps))
    writer.string(document.success_text)
    writer.string(document.task_text)
    for location in document.locations:
        writer.int32(1 if location.day_passed else 0)
        writer.int32(location.x)
        writer.int32(location.y)
        writer.int32(location.id)
        writer.int32(location.max_visits)
        writer.byte(location.location_type)
        affected = [
            (index, change)
            for index, change in enumerate(location.parameter_changes)
            if _change_changed(change)
        ]
        writer.int32(len(affected))
        for index, change in affected:
            _write_change(writer, change, index)
        writer.int32(len(location.texts))
        for item in location.texts:
            writer.string(item.text)
            writer.string(item.media.img)
            writer.string(item.media.sound)
            writer.string(item.media.track)
        writer.byte(1 if location.text_by_formula else 0)
        writer.string(location.text_select_formula)
    for jump in document.jumps:
        writer.float64(jump.priority)
        writer.int32(1 if jump.day_passed else 0)
        writer.int32(jump.id)
        writer.int32(jump.from_location_id)
        writer.int32(jump.to_location_id)
        writer.byte(1 if jump.always_show else 0)
        writer.int32(jump.jumping_count_limit)
        writer.int32(jump.showing_order)
        conditions = [
            (index, condition)
            for index, condition in enumerate(jump.parameter_conditions)
            if index < len(document.parameters)
            and _condition_changed(condition, document.parameters[index])
        ]
        writer.int32(len(conditions))
        for index, condition in conditions:
            writer.int32(index + 1)
            writer.int32(condition.must_from)
            writer.int32(condition.must_to)
            writer.int32(len(condition.equal_values))
            writer.byte(1 if condition.equal_flag else 0)
            for value in condition.equal_values:
                writer.int32(value)
            writer.int32(len(condition.mod_values))
            writer.byte(1 if condition.mod_flag else 0)
            for value in condition.mod_values:
                writer.int32(value)
        changes = [
            (index, change)
            for index, change in enumerate(jump.parameter_changes)
            if _change_changed(change)
        ]
        writer.int32(len(changes))
        for index, change in changes:
            _write_change(writer, change, index)
        writer.string(jump.formula_to_pass)
        writer.string(jump.text)
        writer.string(jump.description)
        writer.string(jump.media.img)
        writer.string(jump.media.sound)
        writer.string(jump.media.track)
    return bytes(writer.data)


def _formula_issue(
    issues: list[QuestIssue],
    formula: str,
    location: str,
    params_count: int,
) -> None:
    if not formula.strip():
        return
    try:
        validate_quest_formula(formula, params_count=params_count)
    except QuestFormulaError as exc:
        issues.append(QuestIssue("error", "quest-formula-invalid", str(exc), location, formula))


def _automatic_cycle(document: QuestDocument) -> tuple[int, ...] | None:
    outgoing: dict[int, list[QuestJump]] = {}
    for jump in document.jumps:
        outgoing.setdefault(jump.from_location_id, []).append(jump)
    graph: dict[int, tuple[int, ...]] = {}
    for location in document.locations:
        jumps = outgoing.get(location.id, [])
        if any(jump.text for jump in jumps):
            continue
        automatic: list[int] = []
        for jump in jumps:
            if jump.text or jump.formula_to_pass or jump.jumping_count_limit:
                continue
            if any(
                _condition_changed(condition, document.parameters[index])
                for index, condition in enumerate(jump.parameter_conditions)
                if index < len(document.parameters)
            ):
                continue
            automatic.append(jump.to_location_id)
        if automatic:
            graph[location.id] = tuple(automatic)

    visiting: list[int] = []
    active: set[int] = set()
    complete: set[int] = set()

    def visit(node: int) -> tuple[int, ...] | None:
        if node in active:
            start = visiting.index(node)
            return tuple(visiting[start:] + [node])
        if node in complete:
            return None
        active.add(node)
        visiting.append(node)
        for target in graph.get(node, ()):
            found = visit(target)
            if found:
                return found
        visiting.pop()
        active.remove(node)
        complete.add(node)
        return None

    for node in graph:
        found = visit(node)
        if found:
            return found
    return None


def validate_quest(document: QuestDocument) -> tuple[QuestIssue, ...]:
    issues: list[QuestIssue] = []
    params_count = len(document.parameters)
    if not params_count:
        issues.append(QuestIssue("warning", "quest-no-parameters", "В квесте нет параметров"))
    if params_count > 96:
        issues.append(
            QuestIssue(
                "warning",
                "quest-parameter-count-high",
                f"В квесте {params_count} параметров; штатные версии TGE используют не более 96",
            )
        )
    for index, param in enumerate(document.parameters, 1):
        location = f"parameter[{index}]"
        if param.minimum > param.maximum:
            issues.append(
                QuestIssue(
                    "error",
                    "quest-parameter-range-invalid",
                    f"Минимум {param.minimum} больше максимума {param.maximum}",
                    location,
                )
            )
        if param.param_type not in range(4):
            issues.append(
                QuestIssue("error", "quest-parameter-type-invalid", f"Тип {param.param_type} неизвестен", location)
            )
        if param.crit_type not in range(2):
            issues.append(
                QuestIssue("error", "quest-parameter-crit-invalid", f"Критический тип {param.crit_type} неизвестен", location)
            )
        _formula_issue(issues, param.starting_formula, f"{location}.starting_formula", params_count)
        for range_index, item in enumerate(param.showing_info):
            if item.from_value > item.to_value:
                issues.append(
                    QuestIssue(
                        "error",
                        "quest-showing-range-invalid",
                        f"Начало {item.from_value} больше конца {item.to_value}",
                        f"{location}.showing_info[{range_index}]",
                    )
                )

    location_ids = [item.id for item in document.locations]
    duplicate_locations = sorted(value for value, count in Counter(location_ids).items() if count > 1)
    for value in duplicate_locations:
        issues.append(
            QuestIssue("error", "quest-location-id-duplicate", f"Идентификатор локации {value} повторяется")
        )
    jump_ids = [item.id for item in document.jumps]
    duplicate_jumps = sorted(value for value, count in Counter(jump_ids).items() if count > 1)
    for value in duplicate_jumps:
        issues.append(QuestIssue("error", "quest-jump-id-duplicate", f"Идентификатор перехода {value} повторяется"))

    starting = [item.id for item in document.locations if item.location_type == 1]
    if len(starting) != 1:
        issues.append(
            QuestIssue(
                "error",
                "quest-start-location-count",
                f"Ожидалась одна стартовая локация, найдено {len(starting)}",
            )
        )
    valid_locations = set(location_ids)
    for index, location in enumerate(document.locations):
        label = f"location[{index}]#{location.id}"
        if location.location_type not in range(6):
            issues.append(
                QuestIssue("error", "quest-location-type-invalid", f"Тип {location.location_type} неизвестен", label)
            )
        if len(location.parameter_changes) != params_count:
            issues.append(
                QuestIssue(
                    "error",
                    "quest-change-count-mismatch",
                    f"Изменений параметров {len(location.parameter_changes)}, ожидалось {params_count}",
                    label,
                )
            )
        if location.text_by_formula:
            if not location.text_select_formula.strip():
                issues.append(
                    QuestIssue(
                        "warning",
                        "quest-location-formula-missing",
                        "Включён выбор текста по формуле, но формула пуста",
                        label,
                    )
                )
            else:
                _formula_issue(issues, location.text_select_formula, f"{label}.text_select_formula", params_count)
        for param_index, change in enumerate(location.parameter_changes):
            if change.change_type not in range(4):
                issues.append(
                    QuestIssue(
                        "error",
                        "quest-change-type-invalid",
                        f"Тип изменения {change.change_type} неизвестен",
                        f"{label}.parameter_changes[{param_index}]",
                    )
                )
            if change.change_type == 3:
                if not change.changing_formula.strip():
                    issues.append(
                        QuestIssue(
                            "warning",
                            "quest-change-formula-missing",
                            "Формульное изменение не содержит формулы",
                            f"{label}.parameter_changes[{param_index}]",
                        )
                    )
                else:
                    _formula_issue(
                        issues,
                        change.changing_formula,
                        f"{label}.parameter_changes[{param_index}].formula",
                        params_count,
                    )

    for index, jump in enumerate(document.jumps):
        label = f"jump[{index}]#{jump.id}"
        if jump.from_location_id not in valid_locations:
            issues.append(
                QuestIssue(
                    "error",
                    "quest-jump-source-missing",
                    f"Исходная локация {jump.from_location_id} отсутствует",
                    label,
                )
            )
        if jump.to_location_id not in valid_locations:
            issues.append(
                QuestIssue(
                    "error",
                    "quest-jump-target-missing",
                    f"Целевая локация {jump.to_location_id} отсутствует",
                    label,
                )
            )
        if len(jump.parameter_changes) != params_count or len(jump.parameter_conditions) != params_count:
            issues.append(
                QuestIssue(
                    "error",
                    "quest-jump-parameter-count-mismatch",
                    "Число изменений или условий не совпадает с числом параметров",
                    label,
                )
            )
        _formula_issue(issues, jump.formula_to_pass, f"{label}.formula_to_pass", params_count)
        for param_index, condition in enumerate(jump.parameter_conditions):
            if condition.must_from > condition.must_to:
                issues.append(
                    QuestIssue(
                        "error",
                        "quest-condition-range-invalid",
                        f"Минимум {condition.must_from} больше максимума {condition.must_to}",
                        f"{label}.parameter_conditions[{param_index}]",
                    )
                )
            if 0 in condition.mod_values:
                issues.append(
                    QuestIssue(
                        "error",
                        "quest-condition-mod-zero",
                        "Проверка делимости содержит ноль",
                        f"{label}.parameter_conditions[{param_index}]",
                    )
                )
        for param_index, change in enumerate(jump.parameter_changes):
            if change.change_type == 3:
                if not change.changing_formula.strip():
                    issues.append(
                        QuestIssue(
                            "warning",
                            "quest-change-formula-missing",
                            "Формульное изменение не содержит формулы",
                            f"{label}.parameter_changes[{param_index}]",
                        )
                    )
                else:
                    _formula_issue(
                        issues,
                        change.changing_formula,
                        f"{label}.parameter_changes[{param_index}].formula",
                        params_count,
                    )

    if len(starting) == 1:
        adjacency: dict[int, set[int]] = {}
        for jump in document.jumps:
            adjacency.setdefault(jump.from_location_id, set()).add(jump.to_location_id)
        reached = {starting[0]}
        pending = [starting[0]]
        while pending:
            current = pending.pop()
            for target in adjacency.get(current, set()):
                if target not in reached:
                    reached.add(target)
                    pending.append(target)
        for value in sorted(valid_locations - reached):
            issues.append(
                QuestIssue(
                    "warning",
                    "quest-location-unreachable",
                    f"Локация {value} недостижима даже без учёта условий переходов",
                    f"location#{value}",
                )
            )

    cycle = _automatic_cycle(document)
    if cycle:
        issues.append(
            QuestIssue(
                "warning",
                "quest-automatic-cycle",
                "Обнаружен потенциально бесконечный цикл автоматических пустых переходов",
                evidence=" -> ".join(map(str, cycle)),
            )
        )
    return tuple(issues)


def inspect_quest(path: str | Path) -> dict[str, Any]:
    path = Path(path).resolve()
    document = load_quest(path)
    issues = validate_quest(document)
    return {
        "schema": QUEST_REPORT_SCHEMA,
        "path": str(path),
        "format": document.source_format,
        "header": document.header,
        "header_hex": f"0x{document.header:08x}",
        "major_version": document.major_version,
        "minor_version": document.minor_version,
        "old_tge_behaviour": document.old_tge_behaviour,
        "parameters": len(document.parameters),
        "locations": len(document.locations),
        "jumps": len(document.jumps),
        "issues": [item.as_dict() for item in issues],
        "valid": not any(item.severity == "error" for item in issues),
        "capabilities": ["inspect", "validate", "export-json", "build-qmm", "roundtrip"],
    }


def verify_quest(path: str | Path) -> dict[str, Any]:
    path = Path(path).resolve()
    document = load_quest(path)
    issues = validate_quest(document)
    encoded = write_qmm(document)
    reparsed = parse_quest(encoded)
    semantic_equal = document.semantic_dict() == reparsed.semantic_dict()
    deterministic = write_qmm(reparsed) == encoded
    if not semantic_equal:
        raise QuestFormatError("QMM round-trip изменил игровую модель квеста")
    if not deterministic:
        raise QuestFormatError("Повторная сборка QMM не детерминирована")
    return {
        **inspect_quest(path),
        "roundtrip": True,
        "semantic_equal": True,
        "deterministic": True,
        "rebuilt_size": len(encoded),
        "rebuilt_sha256": hashlib.sha256(encoded).hexdigest(),
        "verified": not any(item.severity == "error" for item in issues),
    }


def export_quest_json(
    source: str | Path,
    output: str | Path,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    source = Path(source).resolve()
    output = Path(output).resolve()
    document = load_quest(source)
    payload = json.dumps(document.as_dict(), ensure_ascii=False, indent=2) + "\n"
    _atomic_write(output, payload.encode("utf-8"), overwrite=overwrite)
    return {
        "source": str(source),
        "output": str(output),
        "schema": QUEST_SCHEMA,
        "bytes": output.stat().st_size,
        "parameters": len(document.parameters),
        "locations": len(document.locations),
        "jumps": len(document.jumps),
    }


def load_quest_json(path: str | Path) -> QuestDocument:
    path = Path(path).resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QuestFormatError(f"JSON квеста не читается: {exc}") from exc
    if not isinstance(raw, dict):
        raise QuestFormatError("Корень JSON квеста должен быть объектом")
    return QuestDocument.from_dict(raw)


def build_quest(
    document: QuestDocument,
    output: str | Path,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    output = Path(output).resolve()
    issues = validate_quest(document)
    errors = [item for item in issues if item.severity == "error"]
    if errors:
        raise QuestFormatError(
            f"Сборка заблокирована: ошибок {len(errors)}; первая: {errors[0].message}"
        )
    payload = write_qmm(document)
    reparsed = parse_quest(payload)
    if document.semantic_dict() != reparsed.semantic_dict():
        raise QuestFormatError("Контрольная загрузка собранного QMM изменила игровую модель")
    if write_qmm(reparsed) != payload:
        raise QuestFormatError("Контрольная повторная сборка QMM не детерминирована")
    _atomic_write(output, payload, overwrite=overwrite)
    return {
        "schema": QUEST_REPORT_SCHEMA,
        "output": str(output),
        "format": "QMM",
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "parameters": len(document.parameters),
        "locations": len(document.locations),
        "jumps": len(document.jumps),
        "warnings": [item.as_dict() for item in issues if item.severity == "warning"],
        "verified": True,
        "roundtrip": True,
        "deterministic": True,
    }


def build_quest_from_json(
    source: str | Path,
    output: str | Path,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    return build_quest(load_quest_json(source), output, overwrite=overwrite)


def _atomic_write(path: Path, payload: bytes, *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"Файл уже существует: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.srhd-", dir=path.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        temp.write_bytes(payload)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def quest_media(document: QuestDocument) -> dict[str, tuple[str, ...]]:
    images: set[str] = set()
    sounds: set[str] = set()
    tracks: set[str] = set()

    def add(value: QuestMedia) -> None:
        if value.img:
            images.add(value.img)
        if value.sound:
            sounds.add(value.sound)
        if value.track:
            tracks.add(value.track)

    for param in document.parameters:
        add(param.media)
    for location in document.locations:
        for change in location.parameter_changes:
            add(change.media)
        for item in location.texts:
            add(item.media)
    for jump in document.jumps:
        add(jump.media)
        for change in jump.parameter_changes:
            add(change.media)
    return {
        "images": tuple(sorted(images, key=str.casefold)),
        "sounds": tuple(sorted(sounds, key=str.casefold)),
        "tracks": tuple(sorted(tracks, key=str.casefold)),
    }


def validate_quest_files(paths: Iterable[str | Path]) -> dict[str, Any]:
    reports = [inspect_quest(path) for path in paths]
    return {
        "schema": QUEST_REPORT_SCHEMA,
        "files": reports,
        "file_count": len(reports),
        "valid": all(item["valid"] for item in reports),
    }
