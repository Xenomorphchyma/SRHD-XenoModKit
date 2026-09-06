"""Shared, exact-code diagnostic allowances for audit and compilation."""
from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Iterable, Mapping, Any


def matching_allowance(code: str, path: str | None, root: Path, rules: Iterable[str]) -> str | None:
    for rule in rules:
        expected, separator, pattern = rule.partition(":")
        if code != expected:
            continue
        if separator:
            if not path:
                continue
            candidate = Path(path)
            try:
                candidate = candidate.relative_to(root)
            except ValueError:
                pass
            if not fnmatch.fnmatch(candidate.as_posix().casefold(), pattern.replace("\\", "/").casefold()):
                continue
        return rule
    return None


def runtime_issue_rows(issues: Iterable[Any], root: Path, rules: Iterable[str] = ()) -> list[dict[str, Any]]:
    rules = tuple(rules)
    rows = []
    for issue in issues:
        row = dict(issue) if isinstance(issue, Mapping) else issue.as_dict()
        rule = matching_allowance(row["code"], row.get("path"), root, rules)
        if rule is not None:
            row.update(suppressed=True, suppression=rule)
        rows.append(row)
    return rows
