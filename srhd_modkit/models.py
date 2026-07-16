from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ModuleEntry:
    key: str
    value: str
    line: int


@dataclass(slots=True)
class ModuleInfo:
    path: Path
    encoding: str
    entries: list[ModuleEntry] = field(default_factory=list)
    malformed_lines: list[tuple[int, str]] = field(default_factory=list)

    def values(self, key: str) -> list[str]:
        wanted = normalize_key(key)
        return [entry.value for entry in self.entries if normalize_key(entry.key) == wanted]

    def first(self, key: str, default: str = "") -> str:
        values = self.values(key)
        return values[0] if values else default

    @property
    def name(self) -> str:
        return self.first("Name")

    @property
    def author(self) -> str:
        return self.first("Author")

    @property
    def section(self) -> str:
        return self.first("Section")

    @property
    def languages(self) -> list[str]:
        return split_refs(self.first("Languages"))

    @property
    def dependencies(self) -> list[str]:
        return split_refs(self.first("Dependence"))

    @property
    def conflicts(self) -> list[str]:
        return split_refs(self.first("Conflict"))

    @property
    def priority(self) -> int | None:
        raw = self.first("Priority").strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def as_dict(self) -> dict[str, Any]:
        grouped: dict[str, list[str]] = {}
        for entry in self.entries:
            grouped.setdefault(entry.key, []).append(entry.value)
        return {
            "path": str(self.path),
            "encoding": self.encoding,
            "name": self.name,
            "author": self.author,
            "section": self.section,
            "priority": self.priority,
            "languages": self.languages,
            "dependencies": self.dependencies,
            "conflicts": self.conflicts,
            "fields": grouped,
            "malformed_lines": [
                {"line": number, "text": text}
                for number, text in self.malformed_lines
            ],
        }


@dataclass(slots=True)
class ModRecord:
    root: Path
    relative_path: Path
    module: ModuleInfo
    has_cfg: bool
    has_data: bool
    file_count: int
    total_size: int

    @property
    def name(self) -> str:
        return self.module.name or self.root.name

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "root": str(self.root),
            "relative_path": self.relative_path.as_posix(),
            "encoding": self.module.encoding,
            "author": self.module.author,
            "section": self.module.section,
            "priority": self.module.priority,
            "languages": self.module.languages,
            "dependencies": self.module.dependencies,
            "conflicts": self.module.conflicts,
            "has_cfg": self.has_cfg,
            "has_data": self.has_data,
            "file_count": self.file_count,
            "total_size": self.total_size,
        }


@dataclass(frozen=True, slots=True)
class Issue:
    severity: str
    code: str
    message: str
    path: Path | None = None
    mod: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "path": str(self.path) if self.path else None,
            "mod": self.mod,
        }


def normalize_key(value: str) -> str:
    return "".join(char for char in value.strip().casefold() if char.isalnum())


def normalize_ref(value: str) -> str:
    value = value.strip().strip('"').strip("'").replace("/", "\\")
    return "".join(value.casefold().split())


def split_refs(value: str) -> list[str]:
    if not value:
        return []
    normalized = value.replace(";", ",").replace("|", ",")
    return [part.strip() for part in normalized.split(",") if part.strip()]
