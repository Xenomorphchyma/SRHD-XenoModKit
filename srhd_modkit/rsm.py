from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


IMPORT_RE = re.compile(
    r"(?m)^\s*import\s+from\s+(?P<quote>['\"])(?P<path>.+?)(?P=quote)\s*;"
)
SCRIPT_NAME_RE = re.compile(
    r"(?m)^\s*scriptName\s*\(\s*(?P<quote>['\"])(?P<name>.*?)(?P=quote)\s*\)\s*;"
)


@dataclass(frozen=True)
class RsmIssue:
    severity: str
    code: str
    message: str
    path: str | None = None
    location: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "location": self.location,
        }


@dataclass(frozen=True)
class RsmModule:
    path: Path
    imports: tuple[Path, ...]
    sha256: str
    bytes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "imports": [str(path) for path in self.imports],
            "sha256": self.sha256,
            "bytes": self.bytes,
        }


@dataclass(frozen=True)
class RsmProject:
    entry: Path
    script_name: str | None
    modules: tuple[RsmModule, ...]
    issues: tuple[RsmIssue, ...]

    @property
    def valid(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "srhd-modkit-rsm-v1",
            "entry": str(self.entry),
            "script_name": self.script_name,
            "valid": self.valid,
            "modules": [module.as_dict() for module in self.modules],
            "issues": [issue.as_dict() for issue in self.issues],
        }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _mask_rsm_comments(text: str) -> str:
    """Blank comments while preserving offsets, strings and newlines."""

    result = list(text)
    index = 0
    quote: str | None = None
    escaped = False
    line_comment = False
    block_comment = False
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if line_comment:
            if char in "\r\n":
                line_comment = False
            else:
                result[index] = " "
            index += 1
            continue
        if block_comment:
            if char == "*" and next_char == "/":
                result[index] = result[index + 1] = " "
                block_comment = False
                index += 2
                continue
            if char not in "\r\n":
                result[index] = " "
            index += 1
            continue
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char == "/" and next_char == "/":
            result[index] = result[index + 1] = " "
            line_comment = True
            index += 2
            continue
        if char == "/" and next_char == "*":
            result[index] = result[index + 1] = " "
            block_comment = True
            index += 2
            continue
        index += 1
    return "".join(result)


def inspect_rsm_project(entry: str | Path) -> RsmProject:
    """Discover a modular RSM project without pretending to parse the DSL.

    The standalone ``rsmc`` compiler remains authoritative for syntax. This
    pass proves that the import closure is readable, finite and explicit, and
    records every source hash before an external compiler is started.
    """

    entry_path = Path(entry).resolve()
    issues: list[RsmIssue] = []
    modules: list[RsmModule] = []
    visiting: list[Path] = []
    visited: set[str] = set()
    entry_text: str | None = None

    if entry_path.suffix.casefold() != ".rsm":
        issues.append(
            RsmIssue(
                "error",
                "rsm-entry-extension",
                "Точка входа RSM должна иметь расширение .rsm",
                str(entry_path),
            )
        )
    if not entry_path.is_file():
        issues.append(
            RsmIssue("error", "rsm-entry-missing", "Точка входа RSM не найдена", str(entry_path))
        )
        return RsmProject(entry_path, None, (), tuple(issues))

    project_root = entry_path.parent

    def visit(path: Path) -> None:
        nonlocal entry_text
        resolved = path.resolve()
        folded = str(resolved).casefold()
        if resolved in visiting:
            cycle = visiting[visiting.index(resolved) :] + [resolved]
            issues.append(
                RsmIssue(
                    "error",
                    "rsm-import-cycle",
                    "Цикл импортов RSM: " + " -> ".join(item.name for item in cycle),
                    str(resolved),
                )
            )
            return
        if folded in visited:
            return
        if not resolved.is_file():
            issues.append(
                RsmIssue(
                    "error",
                    "rsm-import-missing",
                    "Импортированный RSM-модуль не найден",
                    str(resolved),
                )
            )
            return
        if resolved.suffix.casefold() != ".rsm":
            issues.append(
                RsmIssue(
                    "error",
                    "rsm-import-extension",
                    "Импорт RSM должен указывать на файл .rsm",
                    str(resolved),
                )
            )
            return
        raw = resolved.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            issues.append(
                RsmIssue(
                    "error",
                    "rsm-encoding",
                    f"RSM должен быть UTF-8: {exc}",
                    str(resolved),
                )
            )
            return
        if "\x00" in text:
            issues.append(
                RsmIssue(
                    "error",
                    "rsm-nul-byte",
                    "RSM содержит нулевой символ",
                    str(resolved),
                )
            )
        if resolved == entry_path:
            entry_text = text
        code_text = _mask_rsm_comments(text)
        imports: list[Path] = []
        for match in IMPORT_RE.finditer(code_text):
            import_text = match.group("path").strip()
            imported = Path(import_text)
            target = imported if imported.is_absolute() else resolved.parent / imported
            target = target.resolve()
            imports.append(target)
            try:
                target.relative_to(project_root)
            except ValueError:
                issues.append(
                    RsmIssue(
                        "warning",
                        "rsm-import-outside-project",
                        "RSM импортирует модуль вне каталога точки входа; зависимость "
                        "будет зафиксирована хешем, но её нужно включить в исходники",
                        str(resolved),
                        f"line {_line_number(text, match.start())}",
                    )
                )
        visiting.append(resolved)
        for target in imports:
            visit(target)
        visiting.pop()
        visited.add(folded)
        modules.append(RsmModule(resolved, tuple(imports), _sha256(raw), len(raw)))

    visit(entry_path)
    script_name: str | None = None
    if entry_text is not None:
        names = [
            match.group("name").strip()
            for match in SCRIPT_NAME_RE.finditer(_mask_rsm_comments(entry_text))
        ]
        if len(names) != 1 or not names[0]:
            issues.append(
                RsmIssue(
                    "error",
                    "rsm-script-name",
                    "Точка входа должна содержать ровно один непустой scriptName(\"...\");",
                    str(entry_path),
                )
            )
        else:
            script_name = names[0]
            if any(ord(char) < 32 for char in script_name) or any(
                char in script_name for char in "\\/:*?\"<>|"
            ):
                issues.append(
                    RsmIssue(
                        "error",
                        "rsm-script-name-unsafe",
                        "scriptName содержит недопустимый управляющий или файловый символ",
                        str(entry_path),
                    )
                )
    modules.sort(key=lambda module: str(module.path).casefold())
    return RsmProject(entry_path, script_name, tuple(modules), tuple(issues))
