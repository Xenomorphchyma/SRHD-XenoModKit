from __future__ import annotations

from collections import defaultdict

from .game_text import lint_game_text
from .models import Issue, ModRecord, normalize_ref
from .textio import read_text


def validate_mod(mod: ModRecord) -> list[Issue]:
    issues: list[Issue] = []
    for text_issue in lint_game_text(
        read_text(mod.module.path),
        mod.module.path,
        allowed_encodings={"cp1251", "utf-16-le", "utf-16-be"},
    ):
        details = f" {text_issue.evidence}" if text_issue.evidence else ""
        issues.append(
            Issue(
                text_issue.severity,
                text_issue.code,
                text_issue.message + details,
                mod.module.path,
                mod.name,
            )
        )
    name = mod.module.name.strip()
    if not name:
        issues.append(Issue("error", "missing-name", "В ModuleInfo.txt отсутствует Name.", mod.module.path, mod.name))
    if not mod.has_cfg and not mod.has_data:
        issues.append(Issue("warning", "missing-content", "Нет ни CFG, ни DATA.", mod.root, mod.name))
    if mod.module.first("Priority") and mod.module.priority is None:
        issues.append(Issue("warning", "invalid-priority", "Priority должен быть целым числом.", mod.module.path, mod.name))
    if not mod.module.languages:
        issues.append(Issue("info", "missing-languages", "Поле Languages не заполнено.", mod.module.path, mod.name))
    if name and name.casefold() != mod.root.name.casefold():
        issues.append(
            Issue(
                "info",
                "name-folder-difference",
                f"Name={name}, имя папки={mod.root.name}.",
                mod.module.path,
                mod.name,
            )
        )
    for line, text in mod.module.malformed_lines:
        issues.append(
            Issue(
                "warning",
                "malformed-line",
                f"Строка {line} не распознана: {text[:120]}",
                mod.module.path,
                mod.name,
            )
        )
    return issues


def _build_reference_index(mods: list[ModRecord]) -> dict[str, list[ModRecord]]:
    index: dict[str, list[ModRecord]] = defaultdict(list)
    for mod in mods:
        refs = {
            mod.name,
            mod.root.name,
            mod.relative_path.as_posix(),
            mod.relative_path.as_posix().replace("/", "\\"),
        }
        for ref in refs:
            normalized = normalize_ref(ref)
            if normalized:
                index[normalized].append(mod)
                index[normalized.split("\\")[-1]].append(mod)
    return index


def validate_collection(mods: list[ModRecord]) -> list[Issue]:
    issues = [issue for mod in mods for issue in validate_mod(mod)]
    by_name: dict[str, list[ModRecord]] = defaultdict(list)
    for mod in mods:
        by_name[normalize_ref(mod.name)].append(mod)
    for normalized, owners in by_name.items():
        if normalized and len(owners) > 1:
            paths = ", ".join(str(owner.relative_path) for owner in owners)
            for owner in owners:
                issues.append(
                    Issue(
                        "error",
                        "duplicate-name",
                        f"Одинаковый Name используется в: {paths}",
                        owner.module.path,
                        owner.name,
                    )
                )

    index = _build_reference_index(mods)
    for mod in mods:
        for dependency in mod.module.dependencies:
            if not index.get(normalize_ref(dependency)):
                issues.append(
                    Issue(
                        "warning",
                        "missing-dependency",
                        f"Зависимость не найдена в сканируемой коллекции: {dependency}",
                        mod.module.path,
                        mod.name,
                    )
                )
    return sorted(issues, key=lambda issue: ({"error": 0, "warning": 1, "info": 2}.get(issue.severity, 9), issue.mod.casefold(), issue.code))
