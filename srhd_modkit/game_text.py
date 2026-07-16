from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Collection

from .textio import DecodedText


_CYRILLIC_MOJIBAKE_RE = re.compile(r"(?:[РС][\u0400-\u045f]){2,}")
_LATIN_MOJIBAKE_RE = re.compile(r"(?:[ÐÑ][^\s]){2,}")


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


def lint_game_text(
    decoded: DecodedText,
    path: str | Path | None = None,
    *,
    require_cp1251: bool = False,
    require_cp1251_representable: bool = False,
    allowed_encodings: Collection[str] | None = None,
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
    return issues
