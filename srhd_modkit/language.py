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


def _decode_language_text(path: Path) -> str:
    """Decode a language TXT by BOM/NUL sniffing, so UTF-16 text is never read as UTF-8."""

    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in raw[:32]:
        return raw.decode("utf-16")
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp1251", "replace")


def _language_text(path: Path, toolchain: Toolchain | None, temp: Path) -> str:
    """Return a language as editable text, decoding a DAT into ``temp`` first."""

    if path.suffix.casefold() == ".dat":
        if toolchain is None:
            raise RuntimeError("Для Lang.dat не инициализирован BlockPar toolchain")
        decoded = temp / f"{len(list(temp.iterdir())):04d}-{path.stem}.txt"
        toolchain.convert_dat(path, decoded, overwrite=True)
        return _decode_language_text(decoded)
    return _decode_language_text(path)


def _script_keys_from_text(text: str) -> dict[str, dict[str, str]]:
    """Collect ``Script/<name>/<n>`` values from a Lang tree, following the block path."""

    scripts: dict[str, dict[str, str]] = {}
    path: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "}":
            if path:
                path.pop()
            continue
        block = re.match(r"^(.*?)\s*(\^\{|~\{)$", stripped)
        if block:
            path.append(block.group(1).strip())
            continue
        if len(path) >= 2 and path[0].casefold() == "script" and "=" in stripped:
            key, value = stripped.split("=", 1)
            key = key.strip()
            if key.isdecimal():
                scripts.setdefault(path[1], {}).setdefault(key, value)
    return scripts


def _fragment_keys(text: str, script: str) -> dict[str, dict[str, str]]:
    """Read an RScript ``number=value`` fragment (the ``--lang`` output of ``script build``)."""

    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key.isdecimal():
            values.setdefault(key, value)
    return {script: values} if values else {}


def _placeholder_signature(text: str, tokens: tuple[str, ...]) -> str:
    """Collapse ``<n>`` and caller-listed word tokens, so one message in two token styles pairs up."""

    collapsed = re.sub(r"<[^<>]{1,24}>", "#", text)
    for token in tokens:
        collapsed = re.sub(
            rf"(?<![0-9A-Za-z_]){re.escape(token)}(?![0-9A-Za-z_])", "#", collapsed
        )
    return collapsed


def _pair_keys_by_text(
    old: dict[str, str],
    new: dict[str, str],
    tokens: tuple[str, ...] = (),
) -> tuple[dict[str, str], list[dict[str, Any]], list[dict[str, str]]]:
    """Pair old and new keys through the text; within a duplicate group keep the key order.

    A pair whose texts differ only in placeholder style (``planet``/``star`` against ``<0>``/``<1>``)
    still carries the same message, so ``tokens`` lists the word placeholders to treat as equal; such
    pairs are returned separately for the report.
    """

    old_by_text: dict[str, list[str]] = {}
    for key, value in old.items():
        old_by_text.setdefault(value, []).append(key)
    new_by_text: dict[str, list[str]] = {}
    for key, value in new.items():
        new_by_text.setdefault(value, []).append(key)
    mapping: dict[str, str] = {}
    groups: list[dict[str, Any]] = []
    for value, old_keys in old_by_text.items():
        new_keys = new_by_text.get(value)
        if not new_keys:
            continue
        old_keys = sorted(old_keys, key=int)
        new_keys = sorted(new_keys, key=int)
        for index, old_key in enumerate(old_keys):
            if index < len(new_keys):
                mapping[old_key] = new_keys[index]
        if len(old_keys) != len(new_keys):
            groups.append({"text": value, "old": old_keys, "new": new_keys})

    normalized: list[dict[str, str]] = []
    if tokens:
        by_signature: dict[str, list[str]] = {}
        for key, value in new.items():
            if key in mapping.values():
                continue
            by_signature.setdefault(_placeholder_signature(value, tokens), []).append(key)
        for old_key in sorted((key for key in old if key not in mapping), key=int):
            candidates = sorted(by_signature.get(_placeholder_signature(old[old_key], tokens), []), key=int)
            if not candidates:
                continue
            target = candidates.pop(0)
            mapping[old_key] = target
            normalized.append({"old": old_key, "new": target})
    return mapping, groups, normalized


def _rewrite_script_keys(
    text: str,
    mappings: dict[str, dict[str, str]],
    known: dict[str, dict[str, str]],
    occupied: dict[str, set[str]],
) -> tuple[str, dict[str, Any]]:
    """Move ``Script/<name>/<n>`` keys onto the new numbering, keeping the rest verbatim."""

    lines = text.split("\n")
    path: list[str] = []
    plan: dict[int, tuple[str | None, str, str]] = {}
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "}":
            if path:
                path.pop()
            continue
        block = re.match(r"^(.*?)\s*(\^\{|~\{)$", stripped)
        if block:
            path.append(block.group(1).strip())
            continue
        if len(path) < 2 or path[0].casefold() != "script" or "=" not in stripped:
            continue
        head = stripped.split("=", 1)[0].strip()
        for name, mapping in mappings.items():
            if path[1].casefold() == name.casefold() and head.isdecimal():
                plan[index] = (mapping.get(head), path[1], head)
                break

    # A key left in place must not land on a number the rebuilt script already uses for another
    # text: that would override a live string with a stranger's.  Such keys are dropped and reported.
    dropped: set[int] = set()
    for index, (target, script, old_key) in plan.items():
        if target is None and old_key in occupied.get(script.casefold(), set()):
            dropped.add(index)

    out: list[str] = []
    stats = {"mapped": 0, "kept": 0, "dropped": 0, "unmatched": 0}
    for index, line in enumerate(lines):
        if index in dropped:
            stats["dropped"] += 1
            continue
        row = plan.get(index)
        if row is None:
            out.append(line)
            continue
        target, script, old_key = row
        if target is None:
            stats["kept"] += 1
            if old_key in known.get(script, {}):
                stats["unmatched"] += 1
            out.append(line)
            continue
        match = re.match(r"^(\s*)(\d+)(\s*=.*)$", line)
        out.append(f"{match.group(1)}{target}{match.group(3)}" if match else line)
        stats["mapped"] += 1
    return "\n".join(out), stats


def remap_languages(
    truth: str | Path,
    onto: str | Path,
    languages: Iterable[str | Path],
    *,
    out_dir: str | Path,
    script: str | None = None,
    placeholder_tokens: Iterable[str] = (),
    tools_root: str | Path | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Re-key language overlays onto the script numbering of a rebuilt SCR.

    ``truth`` is one language in the old numbering, ``onto`` the same language in the new one
    (a Lang DAT/TXT or the RScript fragment a rebuild emits).  The pair maps old keys to new ones
    through the text, and every language in ``languages`` is moved onto that numbering, so the
    localization DATs keep overriding the rebuilt SCR.  The language taken as the truth is
    arbitrary: a mod authored in English uses English there and feeds the Russian overlay.
    """

    truth_path = Path(truth).resolve()
    onto_path = Path(onto).resolve()
    out_path = Path(out_dir).resolve()
    language_paths = [Path(item).resolve() for item in languages]
    if not language_paths:
        raise ValueError("lang remap требует хотя бы один --language")
    needs_toolchain = any(
        item.suffix.casefold() == ".dat" for item in (truth_path, onto_path, *language_paths)
    )
    chain = Toolchain(tools_root) if needs_toolchain else None
    out_path.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".srhd-lang-remap-") as name:
        temp = Path(name)
        truth_scripts = _script_keys_from_text(_language_text(truth_path, chain, temp))
        onto_text = _language_text(onto_path, chain, temp)
        onto_scripts = _script_keys_from_text(onto_text)
        if not onto_scripts:
            if not script:
                raise ValueError("--onto выглядит фрагментом RScript: укажите --script <имя>")
            onto_scripts = _fragment_keys(onto_text, script)
        if not truth_scripts or not onto_scripts:
            raise ValueError("--truth и --onto не дали ни одного ключа Script.<имя>.<n>")

        mappings: dict[str, dict[str, str]] = {}
        groups: dict[str, list[dict[str, Any]]] = {}
        normalized: dict[str, list[dict[str, str]]] = {}
        tokens = tuple(placeholder_tokens)
        for name, old_keys in truth_scripts.items():
            new_keys = next(
                (value for key, value in onto_scripts.items() if key.casefold() == name.casefold()),
                None,
            )
            if not new_keys:
                continue
            mappings[name], groups[name], normalized[name] = _pair_keys_by_text(
                old_keys, new_keys, tokens
            )
        if not any(mappings.values()):
            raise ValueError("--truth и --onto не дали ни одного соответствия по текстам")

        results: list[dict[str, Any]] = []
        occupied = {
            name.casefold(): set(keys) for name, keys in onto_scripts.items()
        }
        for item in language_paths:
            rewritten, stats = _rewrite_script_keys(
                _language_text(item, chain, temp), mappings, truth_scripts, occupied
            )
            if item.suffix.casefold() == ".dat":
                staged = temp / f"{item.stem}.txt"
                staged.write_text(rewritten, encoding="utf-16", newline="")
                target = out_path / item.name
                if target.exists() and not overwrite:
                    raise FileExistsError(f"Результат уже существует: {target}")
                chain.convert_dat(staged, target, overwrite=True, verify=True)
            else:
                target = out_path / item.name
                if target.exists() and not overwrite:
                    raise FileExistsError(f"Результат уже существует: {target}")
                encoding = "utf-16" if item.read_bytes().startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8"
                target.write_text(rewritten, encoding=encoding, newline="")
            results.append({"path": str(item), "output": str(target), **stats})
    return {
        "schema": LANG_SCHEMA,
        "operation": "remap",
        "truth": str(truth_path),
        "onto": str(onto_path),
        "scripts": [
            {
                "script": name,
                "mapped": len(mapping),
                "normalized": normalized.get(name, []),
                "duplicate_text_groups": groups.get(name, []),
            }
            for name, mapping in sorted(mappings.items())
        ],
        "languages": results,
        "valid": all(item["unmatched"] == 0 for item in results),
        "summary": {
            "languages": len(results),
            "mapped": sum(item["mapped"] for item in results),
            "unmatched": sum(item["unmatched"] for item in results),
            "dropped": sum(item["dropped"] for item in results),
        },
    }


__all__ = [
    "extract_language",
    "build_language",
    "diff_languages",
    "language_coverage",
    "remap_languages",
]
