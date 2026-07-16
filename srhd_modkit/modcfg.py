from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .discovery import discover_mods
from .models import Issue, ModRecord, normalize_ref
from .textio import read_text


CURRENT_MOD_RE = re.compile(r"(?im)^\s*CurrentMod\s*=\s*(?P<value>[^\r\n]*)")


@dataclass(frozen=True, slots=True)
class ModConfig:
    path: Path
    encoding: str
    enabled: list[str]
    has_current_mod: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "encoding": self.encoding,
            "has_current_mod": self.has_current_mod,
            "enabled": self.enabled,
        }


def parse_modcfg(path: str | Path) -> ModConfig:
    path = Path(path).resolve()
    decoded = read_text(path)
    match = CURRENT_MOD_RE.search(decoded.text)
    if not match:
        return ModConfig(path, decoded.encoding, [], False)
    enabled = [
        part.strip().strip('"').strip("'").replace("/", "\\")
        for part in match.group("value").split(",")
        if part.strip()
    ]
    return ModConfig(path, decoded.encoding, enabled, True)


def _record_indexes(mods: list[ModRecord]) -> tuple[dict[str, ModRecord], dict[str, list[ModRecord]]]:
    by_path: dict[str, ModRecord] = {}
    by_ref: dict[str, list[ModRecord]] = {}
    for mod in mods:
        path_ref = mod.relative_path.as_posix().replace("/", "\\")
        by_path[normalize_ref(path_ref)] = mod
        for ref in (mod.name, mod.root.name, path_ref):
            key = normalize_ref(ref)
            if key:
                by_ref.setdefault(key, []).append(mod)
                by_ref.setdefault(key.split("\\")[-1], []).append(mod)
    return by_path, by_ref


def validate_modcfg(
    config: ModConfig,
    mods_root: str | Path,
    mods: list[ModRecord] | None = None,
) -> list[Issue]:
    mods_root = Path(mods_root).resolve()
    mods = discover_mods(mods_root) if mods is None else mods
    issues: list[Issue] = []
    if not config.has_current_mod:
        return [Issue("error", "missing-currentmod", "Строка CurrentMod не найдена.", config.path)]

    by_path, by_ref = _record_indexes(mods)
    enabled_keys: set[str] = set()
    enabled_records: list[ModRecord] = []
    for ref in config.enabled:
        key = normalize_ref(ref)
        if key in enabled_keys:
            issues.append(Issue("warning", "duplicate-enabled", f"Повтор в CurrentMod: {ref}", config.path))
            continue
        enabled_keys.add(key)
        record = by_path.get(key)
        if record is None:
            issues.append(Issue("error", "missing-enabled-path", f"Папка включённого мода не найдена: {ref}", config.path))
        else:
            enabled_records.append(record)

    enabled_path_keys = {normalize_ref(mod.relative_path.as_posix().replace("/", "\\")) for mod in enabled_records}
    reported_conflicts: set[tuple[str, str]] = set()
    for mod in enabled_records:
        for dependency in mod.module.dependencies:
            matches = by_ref.get(normalize_ref(dependency), [])
            if not matches:
                issues.append(Issue("warning", "unknown-dependency", f"{mod.name}: зависимость не найдена: {dependency}", mod.module.path, mod.name))
            elif not any(normalize_ref(item.relative_path.as_posix().replace("/", "\\")) in enabled_path_keys for item in matches):
                issues.append(Issue("error", "disabled-dependency", f"{mod.name}: зависимость отключена: {dependency}", mod.module.path, mod.name))
        for conflict in mod.module.conflicts:
            for other in by_ref.get(normalize_ref(conflict), []):
                other_key = normalize_ref(other.relative_path.as_posix().replace("/", "\\"))
                if other_key not in enabled_path_keys or other is mod:
                    continue
                pair = tuple(sorted((mod.name.casefold(), other.name.casefold())))
                if pair in reported_conflicts:
                    continue
                reported_conflicts.add(pair)
                issues.append(Issue("error", "enabled-conflict", f"Одновременно включены конфликтующие моды: {mod.name} и {other.name}", config.path, mod.name))
    return issues

