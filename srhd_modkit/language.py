from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .blockpar import BlockParDocument, BlockParNode, BlockParParameter, load_blockpar
from .files import iter_files, sha256_file
from .module_info import find_module_info, parse_module_info
from .toolchain import Toolchain


LANG_SCHEMA = "srhd-modkit-lang-v1"
_CODE_STUB_RE = re.compile(r"^\s*(?:DAnswer|DText|CT|Format)\s*\(", re.IGNORECASE)


def _flatten_entries(
    entries: Iterable[BlockParNode | BlockParParameter | Any],
    prefix: tuple[str, ...] = (),
) -> list[dict[str, str]]:
    values: list[dict[str, str]] = []
    node_counts: dict[str, int] = {}
    parameter_counts: dict[str, int] = {}
    for entry in entries:
        if isinstance(entry, BlockParNode):
            folded = entry.name.casefold()
            node_counts[folded] = node_counts.get(folded, 0) + 1
            occurrence = node_counts[folded]
            segment = entry.name if occurrence == 1 else f"{entry.name}[{occurrence}]"
            values.extend(_flatten_entries(entry.entries, prefix + (segment,)))
        elif isinstance(entry, BlockParParameter):
            folded = entry.key.casefold()
            parameter_counts[folded] = parameter_counts.get(folded, 0) + 1
            occurrence = parameter_counts[folded]
            segment = entry.key if occurrence == 1 else f"{entry.key}[{occurrence}]"
            values.append({"path": "/".join(prefix + (segment,)), "value": entry.value})
    return values


def _load_language_document(
    path: Path,
    toolchain: Toolchain | None,
    temp: Path,
) -> BlockParDocument | None:
    if path.suffix.casefold() == ".txt":
        return load_blockpar(path)
    if path.suffix.casefold() != ".dat":
        raise ValueError(f"Языковой файл должен быть Lang.dat или Lang.txt: {path}")
    if toolchain is None:
        raise RuntimeError("Для Lang.dat не инициализирован BlockPar toolchain")
    decoded = temp / f"{len(list(temp.iterdir())):04d}-{path.stem}.txt"
    toolchain.convert_dat(path, decoded, overwrite=True)
    if decoded.stat().st_size == 0:
        return None
    return load_blockpar(decoded)


def _snapshot(path: Path, toolchain: Toolchain | None, temp: Path) -> dict[str, Any]:
    document = _load_language_document(path, toolchain, temp)
    entries = _flatten_entries(document.entries) if document is not None else []
    code_stubs = [item for item in entries if _CODE_STUB_RE.match(item["value"])]
    empty = [item for item in entries if not item["value"].strip()]
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "entries": entries,
        "entry_count": len(entries),
        "code_stubs": code_stubs,
        "empty": empty,
    }


def extract_language(
    source: str | Path,
    output: str | Path,
    *,
    tools_root: str | Path | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    source_path = Path(source).resolve()
    output_path = Path(output).resolve()
    if source_path.suffix.casefold() != ".dat" or output_path.suffix.casefold() != ".txt":
        raise ValueError("lang extract принимает Lang.dat и создаёт Lang.txt")
    result = Toolchain(tools_root).convert_dat(source_path, output_path, overwrite=overwrite)
    return {"schema": LANG_SCHEMA, "operation": "extract", **result}


def build_language(
    source: str | Path,
    output: str | Path,
    *,
    tools_root: str | Path | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    source_path = Path(source).resolve()
    output_path = Path(output).resolve()
    if source_path.suffix.casefold() != ".txt" or output_path.suffix.casefold() != ".dat":
        raise ValueError("lang build принимает Lang.txt и создаёт Lang.dat")
    result = Toolchain(tools_root).convert_dat(source_path, output_path, overwrite=overwrite, verify=True)
    return {"schema": LANG_SCHEMA, "operation": "build", **result}


def diff_languages(
    left: str | Path,
    right: str | Path,
    *,
    tools_root: str | Path | None = None,
) -> dict[str, Any]:
    left_path = Path(left).resolve()
    right_path = Path(right).resolve()
    chain = (
        Toolchain(tools_root)
        if left_path.suffix.casefold() == ".dat" or right_path.suffix.casefold() == ".dat"
        else None
    )
    with tempfile.TemporaryDirectory(prefix="srhd-lang-diff-") as temp_name:
        temp = Path(temp_name)
        left_value = _snapshot(left_path, chain, temp)
        right_value = _snapshot(right_path, chain, temp)
    left_map = {item["path"].casefold(): item for item in left_value["entries"]}
    right_map = {item["path"].casefold(): item for item in right_value["entries"]}
    added: list[dict[str, str]] = []
    removed: list[dict[str, str]] = []
    changed: list[dict[str, str]] = []
    unchanged = 0
    for key in sorted(left_map.keys() | right_map.keys()):
        before = left_map.get(key)
        after = right_map.get(key)
        if before is None:
            added.append(after)
        elif after is None:
            removed.append(before)
        elif before["value"] != after["value"]:
            changed.append(
                {"path": after["path"], "left": before["value"], "right": after["value"]}
            )
        else:
            unchanged += 1
    return {
        "schema": LANG_SCHEMA,
        "operation": "diff",
        "left": {key: value for key, value in left_value.items() if key != "entries"},
        "right": {key: value for key, value in right_value.items() if key != "entries"},
        "added": added,
        "removed": removed,
        "changed": changed,
        "summary": {
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
            "unchanged": unchanged,
        },
    }


def _language_files(mod: Path) -> dict[str, Path]:
    candidates: dict[str, list[Path]] = {}
    for path in iter_files(mod):
        relative = path.relative_to(mod)
        if (
            len(relative.parts) >= 3
            and relative.parts[0].casefold() == "cfg"
            and path.name.casefold() in {"lang.dat", "lang.txt"}
        ):
            candidates.setdefault(relative.parts[1].casefold(), []).append(path)
    result: dict[str, Path] = {}
    for language, values in candidates.items():
        result[language] = sorted(
            values,
            key=lambda item: (item.suffix.casefold() != ".dat", str(item).casefold()),
        )[0]
    return result


def language_coverage(
    mod: str | Path,
    *,
    base: str | None = None,
    tools_root: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(mod).resolve()
    if not root.is_dir():
        raise NotADirectoryError(root)
    info_path = find_module_info(root)
    if info_path is None:
        raise FileNotFoundError(f"ModuleInfo.txt не найден в {root}")
    module = parse_module_info(info_path)
    found = _language_files(root)
    declared = module.languages or sorted(found)
    base_name = (base or (declared[0] if declared else "")).casefold()
    chain = (
        Toolchain(tools_root)
        if any(path.suffix.casefold() == ".dat" for path in found.values())
        else None
    )
    issues: list[dict[str, Any]] = []
    snapshots: dict[str, dict[str, Any]] = {}
    with tempfile.TemporaryDirectory(prefix="srhd-lang-coverage-") as temp_name:
        temp = Path(temp_name)
        for language in declared:
            path = found.get(language.casefold())
            if path is None:
                issues.append(
                    {
                        "severity": "error",
                        "code": "lang-declared-file-missing",
                        "message": f"Для объявленного языка {language} отсутствует CFG/{language}/Lang.dat или Lang.txt",
                        "language": language,
                    }
                )
                continue
            try:
                snapshot = _snapshot(path, chain, temp)
            except Exception as exc:
                issues.append(
                    {
                        "severity": "error",
                        "code": "lang-file-unreadable",
                        "message": f"{language}: {exc}",
                        "language": language,
                        "path": str(path),
                    }
                )
                continue
            snapshots[language.casefold()] = snapshot
            for item in snapshot["code_stubs"]:
                issues.append(
                    {
                        "severity": "error",
                        "code": "lang-value-code-stub",
                        "message": f"{language} {item['path']} содержит RScript-код вместо отображаемого текста",
                        "language": language,
                        "path": str(path),
                        "key": item["path"],
                    }
                )
            for item in snapshot["empty"]:
                issues.append(
                    {
                        "severity": "warning",
                        "code": "lang-value-empty",
                        "message": f"{language} {item['path']} имеет пустое значение",
                        "language": language,
                        "path": str(path),
                        "key": item["path"],
                    }
                )

    base_snapshot = snapshots.get(base_name)
    if declared and base_snapshot is None:
        issues.append(
            {
                "severity": "error",
                "code": "lang-base-unavailable",
                "message": f"Базовый язык {base or declared[0]} недоступен для сравнения",
            }
        )
    base_keys = (
        {item["path"].casefold(): item["path"] for item in base_snapshot["entries"]}
        if base_snapshot is not None
        else {}
    )
    languages: list[dict[str, Any]] = []
    for language in declared:
        snapshot = snapshots.get(language.casefold())
        if snapshot is None:
            languages.append({"language": language, "available": False, "missing": [], "extra": []})
            continue
        keys = {item["path"].casefold(): item["path"] for item in snapshot["entries"]}
        missing = [base_keys[key] for key in sorted(base_keys.keys() - keys.keys())]
        extra = [keys[key] for key in sorted(keys.keys() - base_keys.keys())]
        if language.casefold() != base_name:
            for key in missing:
                issues.append(
                    {
                        "severity": "error",
                        "code": "lang-key-missing",
                        "message": f"{language}: отсутствует ключ {key} базового языка",
                        "language": language,
                        "key": key,
                    }
                )
        languages.append(
            {
                "language": language,
                "available": True,
                "path": snapshot["path"],
                "entries": snapshot["entry_count"],
                "missing": missing,
                "extra": extra,
                "code_stubs": len(snapshot["code_stubs"]),
                "empty": len(snapshot["empty"]),
            }
        )
    return {
        "schema": LANG_SCHEMA,
        "operation": "coverage",
        "mod": str(root),
        "base": base or (declared[0] if declared else None),
        "declared_languages": declared,
        "languages": languages,
        "issues": issues,
        "valid": not any(item["severity"] == "error" for item in issues),
        "summary": {
            "languages": len(declared),
            "errors": sum(item["severity"] == "error" for item in issues),
            "warnings": sum(item["severity"] == "warning" for item in issues),
        },
    }


__all__ = [
    "extract_language",
    "build_language",
    "diff_languages",
    "language_coverage",
]
