"""Structural reader for compiled RScript ``.scr`` files.

The layout mirrors ``TScript.LoadFromBuffer`` of Rangers.exe 2.1.2500 (the
same in the Steam release and the 2026-10 prerelease builds). The game reads
the file front to back with ``TBufEC`` and raises on the first read past the
end, an out-of-range star reference or an unknown place kind, so a file that
fails here is a file the game refuses to load. Nothing is executed or
compiled; code sections are only checked to be complete UTF-16 strings.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from typing import Any

SCR_MIN_VERSION = 5
SCR_MAX_VERSION = 8

_PLACE_POLAR = 0
_PLACE_PLANET_POSITION = 1
_PLACE_DOCKED_AT_PLANET = 2
_PLACE_STAR_BEARING = 3
_PLACE_ITEM_POSITION = 4
_PLACE_GROUP_RELATIVE = 5
_PLACE_COORDINATE_RELATIVE = 6

_STATE_IDLE = 0
_STATE_NORMAL_AI = 5

_VALUE_INT32 = 1
_VALUE_UINT32 = 2
_VALUE_FLOAT64 = 3
_VALUE_STRING = 4
_VALUE_ARRAY = 9
_VALUE_LAST = 10

# Control characters other than tab/CR/LF, lone surrogates and the two
# non-characters never occur in strings written by RScript: none of 590
# distinct SCR files from the game, its add-ons and local mods contain them.
# Overwritten bytes produce them quickly, so they mark damage, not style.
_DAMAGED_TEXT = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")

# Nested script arrays are written by the compiler only for global variables;
# a deeper chain can only come from damaged data and would recurse forever.
_MAX_ARRAY_DEPTH = 32


class ScrFormatError(ValueError):
    """The game would reject the file at ``offset``."""

    def __init__(self, message: str, offset: int, section: str) -> None:
        super().__init__(message)
        self.offset = offset
        self.section = section


@dataclass
class ScrStructure:
    version: int
    variables_offset: int
    size: int
    end_offset: int
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def trailing_bytes(self) -> int:
        return self.size - self.end_offset

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": True,
            "version": self.version,
            "variables_offset": self.variables_offset,
            "end_offset": self.end_offset,
            "trailing_bytes": self.trailing_bytes,
            "counts": dict(self.counts),
        }


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.position = 0
        self.section = "header"

    def fail(self, message: str, offset: int | None = None) -> ScrFormatError:
        return ScrFormatError(message, self.position if offset is None else offset, self.section)

    def _take(self, size: int, what: str) -> bytes:
        end = self.position + size
        if end > len(self.data):
            raise self.fail(
                f"{what}: нужно {size} байт, а до конца файла осталось {len(self.data) - self.position}"
            )
        chunk = self.data[self.position:end]
        self.position = end
        return chunk

    def u8(self, what: str) -> int:
        return self._take(1, what)[0]

    def u16(self, what: str) -> int:
        return struct.unpack("<H", self._take(2, what))[0]

    def i32(self, what: str) -> int:
        return struct.unpack("<i", self._take(4, what))[0]

    def u32(self, what: str) -> int:
        return struct.unpack("<I", self._take(4, what))[0]

    def skip(self, size: int, what: str) -> None:
        self._take(size, what)

    def wide_string(self, what: str) -> str:
        start = self.position
        cursor = start
        data = self.data
        while cursor + 1 < len(data):
            if data[cursor] == 0 and data[cursor + 1] == 0:
                text = data[start:cursor].decode("utf-16-le", errors="surrogatepass")
                bad = _DAMAGED_TEXT.search(text)
                if bad is not None:
                    raise self.fail(
                        f"{what}: символ U+{ord(bad.group()):04X} не встречается в тексте, "
                        "собранном RScript (повреждённые байты)",
                        start + 2 * bad.start(),
                    )
                self.position = cursor + 2
                return text
            cursor += 2
        raise self.fail(f"{what}: строка UTF-16 не завершена нулём до конца файла", start)

    def count(self, what: str) -> int:
        offset = self.position
        value = self.i32(what)
        # Each element occupies at least one byte, so a count above the
        # remaining size can only be damage; the game would read past the end.
        if value > len(self.data) - self.position:
            raise self.fail(
                f"{what}: количество {value} больше, чем байт осталось в файле",
                offset,
            )
        return max(value, 0)


def _read_variable(reader: _Reader, depth: int) -> None:
    reader.wide_string("имя переменной")
    offset = reader.position
    kind = reader.u8("тип переменной")
    if kind > _VALUE_LAST:
        raise reader.fail(f"неизвестный тип переменной {kind}", offset)
    if kind in (_VALUE_INT32, _VALUE_UINT32):
        reader.skip(4, "значение переменной")
    elif kind == _VALUE_FLOAT64:
        reader.skip(8, "значение переменной")
    elif kind == _VALUE_STRING:
        reader.wide_string("строковое значение переменной")
    elif kind == _VALUE_ARRAY:
        if depth >= _MAX_ARRAY_DEPTH:
            raise reader.fail("слишком глубокая вложенность массивов переменных", offset)
        for _ in range(reader.count("размер массива переменной")):
            _read_variable(reader, depth + 1)


def parse_scr_structure(data: bytes) -> ScrStructure:
    """Walk the whole file as the game does; raise :class:`ScrFormatError`."""

    reader = _Reader(data)
    version = reader.u32("версия")
    if version < SCR_MIN_VERSION or version > SCR_MAX_VERSION:
        raise reader.fail(f"версия {version} вне поддерживаемого игрой диапазона 5..8", 0)
    variables_offset = reader.u16("смещение блока переменных")
    if variables_offset > len(data):
        raise reader.fail(f"смещение блока переменных {variables_offset} за концом файла", 4)
    reader.position = variables_offset
    counts: dict[str, int] = {}

    reader.section = "variables"
    total = reader.count("число глобальных переменных")
    for _ in range(total):
        _read_variable(reader, 0)
    counts["variables"] = total

    reader.section = "constellations"
    counts["constellations"] = reader.count("число созвездий")

    reader.section = "stars"
    stars = reader.count("число звёзд")
    for star in range(stars):
        reader.wide_string("имя звезды")
        reader.i32("индекс созвездия")
        if version < 7:
            reader.u8("устаревший флаг звезды")
        reader.u8("RejectHostilePresence")
        reader.u8("ProtectStar")
        for _ in range(reader.count("число ограничений звезды")):
            offset = reader.position
            other = reader.i32("индекс другой звезды")
            # Stars[Index] on a TList of the stars read so far, including this one.
            if other < 0 or other > star:
                raise reader.fail(
                    f"ограничение звезды {star} ссылается на звезду {other}, которой ещё нет",
                    offset,
                )
            reader.skip(8 if version >= 7 else 24, "расстояния ограничения")
            reader.u8("RequireBlackHole")
        for _ in range(reader.count("число планет звезды")):
            reader.wide_string("имя планеты")
            reader.skip(24, "маски и орбита планеты")
            reader.wide_string("текст выбора планеты")
        for _ in range(reader.count("число требований к кораблям")):
            reader.skip(12, "требование к кораблю")
            reader.u8("PlayerOnly")
            reader.skip(20, "требование к кораблю")
            if version < 7:
                reader.skip(8, "устаревшие поля требования")
            reader.skip(24, "статусы требования")
            if version < 7:
                reader.skip(8, "устаревшие поля требования")
            reader.skip(8, "сила требования")
            reader.wide_string("станции требования")
    counts["stars"] = stars

    reader.section = "places"
    places = reader.count("число мест")
    for _ in range(places):
        reader.wide_string("имя места")
        reader.wide_string("исходная переменная места")
        offset = reader.position
        kind = reader.i32("тип места")
        if kind == _PLACE_POLAR:
            reader.skip(12, "параметры места")
        elif kind in (_PLACE_PLANET_POSITION, _PLACE_ITEM_POSITION):
            reader.wide_string("цель места")
            reader.skip(4, "радиус места")
        elif kind == _PLACE_DOCKED_AT_PLANET:
            reader.wide_string("цель места")
        elif kind in (_PLACE_STAR_BEARING, _PLACE_GROUP_RELATIVE):
            reader.wide_string("цель места")
            reader.skip(12, "параметры места")
        elif kind == _PLACE_COORDINATE_RELATIVE:
            reader.wide_string("цель места")
            reader.wide_string("вторая цель места")
            reader.skip(4, "радиус места")
        else:
            raise reader.fail(f"неизвестный тип места {kind} (игра: Script.Place.Type)", offset)
    counts["places"] = places

    reader.section = "items"
    items = reader.count("число предметов")
    for _ in range(items):
        reader.wide_string("имя предмета")
        reader.wide_string("место предмета")
        reader.skip(24, "параметры предмета")
        reader.wide_string("конфигурация предмета")
    counts["items"] = items

    reader.section = "groups"
    groups = reader.count("число групп")
    for _ in range(groups):
        reader.wide_string("имя группы")
        reader.wide_string("планета группы")
        reader.skip(40, "параметры группы")
        if version < 7:
            reader.skip(4, "устаревшее поле группы")
        reader.u8("IncludePlayer")
        if version < 7:
            reader.skip(16, "устаревшие поля группы")
        reader.skip(28, "статусы группы")
        reader.wide_string("диалог станции группы")
        reader.skip(8, "сила группы")
        reader.wide_string("станции группы")
    counts["groups"] = groups

    reader.section = "group_relations"
    relations = reader.count("число отношений групп")
    reader.skip(relations * 24, "отношения групп")
    counts["group_relations"] = relations

    reader.section = "code"
    reader.wide_string("код инициализации")
    reader.wide_string("код хода")
    if version > 5:
        reader.wide_string("код диалогов")

    reader.section = "states"
    states = reader.count("число состояний")
    for _ in range(states):
        reader.wide_string("имя состояния")
        kind = reader.i32("тип состояния")
        if kind not in (_STATE_IDLE, _STATE_NORMAL_AI):
            reader.wide_string("цель состояния")
        for _ in range(reader.count("число вражеских групп состояния")):
            reader.wide_string("вражеская группа состояния")
        reader.wide_string("подбираемый предмет состояния")
        reader.u8("PickUpNearbyItems")
        reader.wide_string("диалог состояния")
        reader.wide_string("OnAction состояния")
        reader.wide_string("код входа в состояние")
        reader.wide_string("код состояния")
    counts["states"] = states

    for section, label in (
        ("dialogs", "диалог"),
        ("dialog_messages", "сообщение диалога"),
    ):
        reader.section = section
        total = reader.count(f"число: {label}")
        for _ in range(total):
            reader.wide_string(f"имя: {label}")
            reader.wide_string(f"код: {label}")
        counts[section] = total

    reader.section = "dialog_answers"
    answers = reader.count("число ответов диалога")
    for _ in range(answers):
        reader.wide_string("имя ответа")
        reader.wide_string("текст ответа")
        reader.wide_string("код ответа")
    counts["dialog_answers"] = answers

    return ScrStructure(
        version=version,
        variables_offset=variables_offset,
        size=len(data),
        end_offset=reader.position,
        counts=counts,
    )


def scr_structure_issues(structure: dict[str, Any]) -> list[tuple[str, str, str]]:
    """``(severity, code, message)`` for a :func:`check_scr_structure` result."""

    if not structure.get("valid"):
        return [
            (
                "error",
                "scr-structure-invalid",
                f"Игра не загрузит SCR: {structure.get('error')} "
                f"(раздел {structure.get('section')}, смещение {structure.get('offset')})",
            )
        ]
    trailing = int(structure.get("trailing_bytes") or 0)
    if trailing:
        return [
            (
                "warning",
                "scr-trailing-data",
                f"После конца данных SCR лежат ещё {trailing} байт; RScript таких файлов "
                "не пишет, файл мог быть склеен или повреждён",
            )
        ]
    return []


def check_scr_structure(data: bytes) -> dict[str, Any]:
    """Non-raising summary used by ``inspect_scr`` and the audits."""

    try:
        return parse_scr_structure(data).as_dict()
    except ScrFormatError as exc:
        return {
            "valid": False,
            "error": str(exc),
            "offset": exc.offset,
            "section": exc.section,
        }
