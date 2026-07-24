from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Collection, Iterable, Mapping

from .blockpar import BlockParDocument, BlockParNode, BlockParParameter
from .textio import DecodedText


_CYRILLIC_MOJIBAKE_RE = re.compile(r"(?:[РС][\u0400-\u045f]){2,}")
_LATIN_MOJIBAKE_RE = re.compile(r"(?:[ÐÑ][^\s]){2,}")
_NUMERIC_TYPOGRAPHIC_RANGE_RE = re.compile(
    r"(?<![\w.,])"
    r"(?P<left>\d+(?:[.,]\d+)?)"
    r"\s*(?P<separator>[\u2012\u2013\u2014\u2015])\s*"
    r"(?P<right>\d+(?:[.,]\d+)?)"
    r"(?P<percent>\s*%)?"
)
_NUMERIC_SLASH_RE = re.compile(
    r"(?<![\w/])(?P<left>\d+)\s*/\s*(?P<right>\d+)(?![\w/])"
)
_RISKY_FONT_GLYPHS = {
    "\u00a0": "обычный пробел",
    "\u00ad": "удалить скрытый перенос",
    "\u00d7": "x",
    "\u2010": "-",
    "\u2011": "-",
    "\u2012": "-",
    "\u2013": "-",
    "\u2014": "-",
    "\u2015": "-",
    "\u2018": "'",
    "\u2019": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u2022": "-",
    "\u2026": "...",
}
_VANILLA_CONFIRMED_NONASCII_SYMBOLS = {"«", "»", "№"}
_RSON_DISPLAY_FIELDS = {
    "caption",
    "description",
    "hint",
    "label",
    "msg",
    "text",
    "title",
}


@dataclass(frozen=True)
class GameTextIssue:
    severity: str
    code: str
    message: str
    path: str | None = None
    location: str | None = None
    evidence: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _line_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _snippet(text: str, offset: int, length: int = 120) -> str:
    line_start = text.rfind("\n", 0, offset) + 1
    line_end = text.find("\n", offset)
    if line_end < 0:
        line_end = len(text)
    value = text[line_start:line_end].strip()[:length]
    return "".join("�" if 0xDC80 <= ord(char) <= 0xDCFF else char for char in value)


def _display_location(text: str, offset: int, location: str | None) -> str:
    line = _line_for_offset(text, offset)
    if location:
        return location if "\n" not in text else f"{location} line {line}"
    return f"line {line}"


def _uses_russian_display_context(
    text: str,
    offset: int,
    path: str | Path | None,
) -> bool:
    if re.search(r"[А-Яа-яЁё]", _snippet(text, offset)):
        return True
    if path is None:
        return False
    value = Path(path)
    folded_parts = {part.casefold() for part in value.parts}
    return "rus" in folded_parts or "_rus" in value.stem.casefold()


def lint_game_display_text(
    text: str,
    path: str | Path | None = None,
    *,
    location: str | None = None,
) -> list[GameTextIssue]:
    """Warn about display forms outside SRHD's conservative text subset.

    Windows-1251 representability proves byte compatibility, not that every
    bitmap/UI font used by SRHD contains the glyph.  Numeric slash notation is
    a separate readability advisory: slash and percent are supported by the
    vanilla language corpus and are not forbidden on their own.
    """

    source = str(Path(path).resolve()) if path else None
    issues: list[GameTextIssue] = []
    covered: set[int] = set()

    for match in _NUMERIC_TYPOGRAPHIC_RANGE_RE.finditer(text):
        separator = match.group("separator")
        percent = "%" if match.group("percent") else ""
        cyrillic = _uses_russian_display_context(
            text,
            match.start(),
            path,
        )
        if cyrillic:
            replacement = (
                f"от {match.group('left')} до {match.group('right')}{percent}"
            )
        else:
            replacement = (
                f"from {match.group('left')} to {match.group('right')}{percent}"
            )
        issues.append(
            GameTextIssue(
                "warning",
                "game-text-typographic-number-range",
                "Числовой диапазон использует редкое типографское тире. "
                "Оно кодируется Windows-1251, но отображение зависит от "
                f"конкретного игрового шрифта; безопасная запись: {replacement!r}",
                source,
                _display_location(text, match.start(), location),
                (
                    f"{match.group(0)!r} -> {replacement!r}; "
                    f"{separator!r}=U+{ord(separator):04X}"
                ),
            )
        )
        covered.update(range(match.start(), match.end()))

    for match in _NUMERIC_SLASH_RE.finditer(text):
        cyrillic = _uses_russian_display_context(
            text,
            match.start(),
            path,
        )
        separator = "из" if cyrillic else "of"
        replacement = f"{match.group('left')} {separator} {match.group('right')}"
        issues.append(
            GameTextIssue(
                "warning",
                "game-text-numeric-slash-notation",
                "Запись «число/число» поддерживается не всеми контекстами "
                "одинаково ясно. Если это прогресс или счётчик, используйте "
                f"словесную форму {replacement!r}; обозначения, коды и дроби "
                "могут оставить осознанное предупреждение",
                source,
                _display_location(text, match.start(), location),
                f"{match.group(0)!r} -> {replacement!r}",
            )
        )

    reported_glyphs: set[tuple[int, str]] = set()
    for offset, character in enumerate(text):
        replacement = _RISKY_FONT_GLYPHS.get(character)
        if replacement is None:
            if (
                ord(character) <= 127
                or character in _VANILLA_CONFIRMED_NONASCII_SYMBOLS
                or unicodedata.category(character)[:1] not in {"P", "S", "Z"}
            ):
                continue
            try:
                character.encode("cp1251")
            except UnicodeEncodeError:
                continue
            replacement = "простую ASCII- или словесную форму"
        if offset in covered:
            continue
        line = _line_for_offset(text, offset)
        key = (line, character)
        if key in reported_glyphs:
            continue
        reported_glyphs.add(key)
        issues.append(
            GameTextIssue(
                "warning",
                "game-text-limited-font-glyph",
                f"Символ {character!r} (U+{ord(character):04X}) кодируется "
                "Windows-1251, но присутствует не во всех шрифтах и текстовых "
                f"поверхностях SRHD. Для надёжного отображения используйте {replacement!r}",
                source,
                _display_location(text, offset, location),
                _snippet(text, offset),
            )
        )
    return issues


def lint_blockpar_display_text(
    document: BlockParDocument,
    path: str | Path | None = None,
) -> list[GameTextIssue]:
    """Inspect BlockPar parameter values without treating comments as UI text."""

    issues: list[GameTextIssue] = []

    def walk(
        entries: Iterable[BlockParNode | BlockParParameter],
        prefix: str = "BlockPar",
    ) -> None:
        for entry in entries:
            if isinstance(entry, BlockParParameter):
                issues.extend(
                    lint_game_display_text(
                        entry.value,
                        path,
                        location=f"{prefix}/{entry.key}",
                    )
                )
            elif isinstance(entry, BlockParNode):
                walk(entry.entries, f"{prefix}/{entry.name}")

    walk(document.entries)
    return issues


def lint_key_value_display_text(
    text: str,
    path: str | Path | None = None,
) -> list[GameTextIssue]:
    """Inspect flat/BlockPar-style values while skipping comment-only lines."""

    issues: list[GameTextIssue] = []
    in_block_comment = False
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if in_block_comment:
            if "*/" in stripped:
                in_block_comment = False
            continue
        if stripped.startswith("/*"):
            if "*/" not in stripped:
                in_block_comment = True
            continue
        if stripped.startswith(("//", "*")) or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        issues.extend(
            lint_game_display_text(
                value,
                path,
                location=f"line {number} {key.rstrip()}",
            )
        )
    return issues


def _iter_rscript_string_literals(
    text: str,
) -> Iterable[tuple[str, int, int, int]]:
    """Yield literal value, source line and source span, skipping comments."""

    state = "code"
    quote = ""
    value: list[str] = []
    start = 0
    start_line = 1
    line = 1
    index = 0
    while index < len(text):
        character = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if state == "line-comment":
            if character == "\n":
                state = "code"
        elif state == "block-comment":
            if character == "*" and following == "/":
                state = "code"
                index += 1
        elif state == "string":
            if character == "\\" and following:
                value.extend((character, following))
                index += 1
            elif character == quote:
                yield "".join(value), start_line, start, index + 1
                state = "code"
                value = []
            else:
                value.append(character)
        elif character == "/" and following == "/":
            state = "line-comment"
            index += 1
        elif character == "/" and following == "*":
            state = "block-comment"
            index += 1
        elif character in {"'", '"'}:
            state = "string"
            quote = character
            value = []
            start = index
            start_line = line
        if character == "\n":
            line += 1
        index += 1


def _lint_rscript_display_literals(
    text: str,
    path: str | Path | None,
    location: str,
) -> list[GameTextIssue]:
    issues: list[GameTextIssue] = []
    source = str(Path(path).resolve()) if path else None
    for value, line, start, end in _iter_rscript_string_literals(text):
        literal_location = f"{location}:{line}"
        issues.extend(
            lint_game_display_text(value, path, location=literal_location)
        )
        if value != "/":
            continue
        line_start = text.rfind("\n", 0, start) + 1
        line_end = text.find("\n", end)
        if line_end < 0:
            line_end = len(text)
        before = text[line_start:start]
        after = text[end:line_end]
        if not re.search(r"\+\s*$", before) or not re.match(r"\s*\+", after):
            continue
        issues.append(
            GameTextIssue(
                "warning",
                "game-text-dynamic-slash-notation",
                "Строка собирается как «значение + '/' + значение». Если это "
                "видимый прогресс или счётчик, используйте словесный разделитель "
                "' из ' для русского текста или ' of ' для английского; пути и "
                "осознанные технические обозначения могут оставить предупреждение",
                source,
                literal_location,
                text[line_start:line_end].strip(),
            )
        )
    return issues


def lint_rson_display_text(
    data: Mapping[str, Any],
    path: str | Path | None = None,
) -> list[GameTextIssue]:
    """Inspect only display fields and RScript literals, not code comments."""

    issues: list[GameTextIssue] = []

    def walk(value: Any, location: str = "RSON") -> None:
        if isinstance(value, Mapping):
            object_id = value.get("#")
            object_label = (
                f"object #{object_id}"
                if isinstance(object_id, int)
                else location
            )
            for key, child in value.items():
                folded = str(key).casefold()
                child_location = f"{object_label} {key}"
                if folded.endswith("code") and isinstance(child, (str, list)):
                    code = (
                        child
                        if isinstance(child, str)
                        else "\n".join(str(line) for line in child)
                    )
                    issues.extend(
                        _lint_rscript_display_literals(
                            code,
                            path,
                            child_location,
                        )
                    )
                elif folded in _RSON_DISPLAY_FIELDS and isinstance(child, str):
                    issues.extend(
                        lint_game_display_text(
                            child,
                            path,
                            location=child_location,
                        )
                    )
                elif isinstance(child, (Mapping, list)):
                    walk(child, child_location)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{location}[{index}]")

    walk(data)
    return issues


def lint_game_text(
    decoded: DecodedText,
    path: str | Path | None = None,
    *,
    require_cp1251: bool = False,
    require_cp1251_representable: bool = False,
    allowed_encodings: Collection[str] | None = None,
    check_display_compatibility: bool = True,
) -> list[GameTextIssue]:
    """Validate text that will be consumed by the legacy SRHD runtime.

    ``require_cp1251`` applies to a final game-facing file or decrypted DAT
    payload. ``require_cp1251_representable`` applies to editable UTF-8 source
    that will later be encoded into such a payload.
    """
    source = str(Path(path).resolve()) if path else None
    text = decoded.text
    issues: list[GameTextIssue] = []

    suspicious = _CYRILLIC_MOJIBAKE_RE.search(text) or _LATIN_MOJIBAKE_RE.search(text)
    replacement = text.find("\ufffd")
    surrogate = next((index for index, char in enumerate(text) if 0xDC80 <= ord(char) <= 0xDCFF), -1)
    if suspicious or replacement >= 0 or surrogate >= 0:
        offset = suspicious.start() if suspicious else replacement if replacement >= 0 else surrogate
        issues.append(
            GameTextIssue(
                "error",
                "game-text-mojibake",
                "Текст уже содержит признаки двойного декодирования или потерянные символы",
                source,
                f"line {_line_for_offset(text, offset)}",
                _snippet(text, offset),
            )
        )

    normalized = decoded.encoding.casefold().replace("_", "-")
    normalized_allowed = {
        value.casefold().replace("_", "-") for value in (allowed_encodings or ())
    }
    ascii_utf8 = text.isascii() and normalized == "utf-8" and not decoded.had_bom
    if normalized_allowed and normalized not in normalized_allowed and not ascii_utf8:
        issues.append(
            GameTextIssue(
                "error",
                "game-text-wrong-encoding",
                "Кодировка файла не поддерживается игрой для этого типа текста",
                source,
                evidence=(
                    f"encoding={decoded.encoding}; допустимо: "
                    + ", ".join(sorted(normalized_allowed))
                ),
            )
        )
    if require_cp1251:
        if normalized != "cp1251" and not ascii_utf8:
            issues.append(
                GameTextIssue(
                    "error",
                    "game-text-wrong-encoding",
                    f"Игра ожидает Windows-1251, но файл/полезная нагрузка определены как {decoded.encoding}",
                    source,
                    evidence=f"encoding={decoded.encoding}",
                )
            )

    if require_cp1251 or require_cp1251_representable:
        try:
            text.encode("cp1251")
        except UnicodeEncodeError as exc:
            bad = text[exc.start : max(exc.end, exc.start + 1)]
            issues.append(
                GameTextIssue(
                    "error",
                    "game-text-not-cp1251",
                    "Текст содержит символ, который невозможно безопасно передать игре через Windows-1251",
                    source,
                    f"line {_line_for_offset(text, exc.start)}",
                    f"{bad!r} (U+{ord(bad[0]):04X}) — {_snippet(text, exc.start)}",
                )
            )
    if check_display_compatibility:
        issues.extend(lint_game_display_text(text, path))
    return issues
