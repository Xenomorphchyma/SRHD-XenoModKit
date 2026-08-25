from __future__ import annotations

from pathlib import Path
from typing import Any

from .audit import AuditProfile, audit_mod
from .files import compare_trees, iter_files, sha256_file
from .module_info import find_module_info, parse_module_info
from .runtime_lint import compare_storage_schemas
from .scripts import inspect_scr, load_rson
from .toolchain import Toolchain


UPGRADE_SCHEMA = "srhd-modkit-upgrade-v1"
_CONFIG_NAMES = {"main.dat", "main.txt", "cachedata.dat", "cachedata.txt", "lang.dat", "lang.txt"}
_RESOURCE_EXTENSIONS = {
    ".gi",
    ".gai",
    ".hai",
    ".pkg",
    ".png",
    ".jpg",
    ".jpeg",
    ".wav",
    ".webm",
    ".qm",
    ".qmm",
    ".cmap",
    ".vdo",
}


def _issue(
    severity: str,
    code: str,
    message: str,
    *,
    path: str | None = None,
    evidence: str | None = None,
) -> dict[str, Any]:
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "path": path,
        "evidence": evidence,
    }


def _relative_files(root: Path, suffix: str) -> dict[str, Path]:
    return {
        path.relative_to(root).as_posix().casefold(): path
        for path in iter_files(root)
        if path.suffix.casefold() == suffix
    }


def _module_delta(old: Path, new: Path, issues: list[dict[str, Any]]) -> dict[str, Any]:
    old_path = find_module_info(old)
    new_path = find_module_info(new)
    if old_path is None or new_path is None:
        issues.append(
            _issue(
                "error",
                "upgrade-module-info-missing",
                "Обе версии должны содержать ModuleInfo.txt",
                evidence=f"old={old_path}; new={new_path}",
            )
        )
        return {"old": None, "new": None, "changed_fields": {}}
    before = parse_module_info(old_path)
    after = parse_module_info(new_path)
    fields = {
        "name": (before.name, after.name),
        "author": (before.author, after.author),
        "section": (before.section, after.section),
        "priority": (before.priority, after.priority),
        "languages": (before.languages, after.languages),
        "dependencies": (before.dependencies, after.dependencies),
        "conflicts": (before.conflicts, after.conflicts),
    }
    changed = {
        key: {"old": left, "new": right}
        for key, (left, right) in fields.items()
        if left != right
    }
    if before.name and after.name and before.name.casefold() != after.name.casefold():
        issues.append(
            _issue(
                "warning",
                "upgrade-module-name-changed",
                f"Имя модуля изменилось: {before.name!r} -> {after.name!r}",
                path=str(new_path),
            )
        )
    removed_dependencies = sorted(set(before.dependencies) - set(after.dependencies), key=str.casefold)
    added_dependencies = sorted(set(after.dependencies) - set(before.dependencies), key=str.casefold)
    if removed_dependencies or added_dependencies:
        issues.append(
            _issue(
                "info",
                "upgrade-dependencies-changed",
                "Изменён набор Dependence; проверьте установку и совместимость набора модов",
                path=str(new_path),
                evidence=f"added={added_dependencies}; removed={removed_dependencies}",
            )
        )
    return {
        "old": before.as_dict(),
        "new": after.as_dict(),
        "changed_fields": changed,
        "added_dependencies": added_dependencies,
        "removed_dependencies": removed_dependencies,
    }


def _rson_comparisons(old: Path, new: Path, issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    old_files = _relative_files(old, ".rson")
    new_files = _relative_files(new, ".rson")
    rows: list[dict[str, Any]] = []
    for relative in sorted(old_files.keys() & new_files.keys()):
        left_path = old_files[relative]
        right_path = new_files[relative]
        if sha256_file(left_path) == sha256_file(right_path):
            continue
        try:
            left = load_rson(left_path)
            right = load_rson(right_path)
            storage = compare_storage_schemas(left, right)
            left_name = str(left.summary().get("name", "")).strip()
            right_name = str(right.summary().get("name", "")).strip()
            row = {
                "path": right_path.relative_to(new).as_posix(),
                "old_name": left_name,
                "new_name": right_name,
                "storage": storage,
            }
            rows.append(row)
            if left_name and right_name and left_name.casefold() != right_name.casefold():
                issues.append(
                    _issue(
                        "info",
                        "upgrade-runtime-script-name-changed",
                        f"Runtime-имя скрипта изменилось: {left_name!r} -> {right_name!r}",
                        path=str(right_path),
                    )
                )
            if left_name and right_name and left_name.casefold() == right_name.casefold():
                issues.append(
                    _issue(
                        "warning",
                        "runtime-saved-script-cache-update-shadow",
                        (
                            f"Код скрипта {right_name!r} изменился без смены runtime-имени. "
                            "Активное сохранение может продолжить исполнять сериализованный код"
                        ),
                        path=str(right_path),
                    )
                )
            for storage_issue in storage.get("issues", []):
                issues.append(
                    _issue(
                        str(storage_issue.get("severity", "error")),
                        str(storage_issue.get("code", "upgrade-storage-incompatible")),
                        str(storage_issue.get("message", "Несовместимая persistent-схема")),
                        path=str(right_path),
                        evidence=storage_issue.get("evidence"),
                    )
                )
        except Exception as exc:
            issues.append(
                _issue(
                    "warning",
                    "upgrade-rson-compare-failed",
                    f"Не удалось сравнить RSON {relative}: {exc}",
                    path=str(right_path),
                )
            )
    return rows


def _scr_comparisons(
    old: Path,
    new: Path,
    issues: list[dict[str, Any]],
    *,
    toolchain: Toolchain | None,
    deep_scripts: bool,
) -> list[dict[str, Any]]:
    old_files = _relative_files(old, ".scr")
    new_files = _relative_files(new, ".scr")
    rows: list[dict[str, Any]] = []
    for relative in sorted(old_files.keys() & new_files.keys()):
        left = old_files[relative]
        right = new_files[relative]
        left_hash = sha256_file(left)
        right_hash = sha256_file(right)
        if left_hash == right_hash:
            continue
        left_info = inspect_scr(left)
        right_info = inspect_scr(right)
        row: dict[str, Any] = {
            "path": right.relative_to(new).as_posix(),
            "old_sha256": left_hash,
            "new_sha256": right_hash,
            "old": left_info,
            "new": right_info,
            "deep": None,
        }
        if left.stem.casefold() == right.stem.casefold() and not any(
            item["code"] == "runtime-saved-script-cache-update-shadow"
            and str(item.get("path", "")).casefold().endswith(f"{right.stem.casefold()}.rson")
            for item in issues
        ):
            issues.append(
                _issue(
                    "warning",
                    "runtime-saved-script-cache-update-shadow",
                    (
                        f"SCR {right.stem!r} изменился без смены имени файла/runtime epoch; "
                        "замена на диске не доказывает обновление активного SAV"
                    ),
                    path=str(right),
                    evidence=f"{left_hash} -> {right_hash}",
                )
            )
        if deep_scripts:
            try:
                if toolchain is None:
                    raise RuntimeError("toolchain не инициализирован для глубокого сравнения")
                row["deep"] = toolchain.compare_scr(left, right)
                issues.extend(row["deep"].get("comparison", {}).get("update_issues", []))
            except Exception as exc:
                row["deep"] = {"verified": False, "error": str(exc)}
                issues.append(
                    _issue(
                        "warning",
                        "upgrade-deep-scr-compare-failed",
                        f"Глубокое сравнение {relative} не завершено: {exc}",
                        path=str(right),
                    )
                )
        rows.append(row)
    return rows


def _runtime_names(root: Path) -> list[str]:
    names: dict[str, str] = {}
    for path in iter_files(root):
        if path.suffix.casefold() == ".rson":
            try:
                name = str(load_rson(path).summary().get("name", "")).strip()
            except Exception:
                continue
        elif path.suffix.casefold() == ".scr":
            name = path.stem
        else:
            continue
        if name:
            names.setdefault(name.casefold(), name)
    return sorted(names.values(), key=str.casefold)


def check_upgrade(
    old: str | Path,
    new: str | Path,
    *,
    tools_root: str | Path | None = None,
    deep_scripts: bool = False,
    audit: bool = True,
) -> dict[str, Any]:
    old_root = Path(old).resolve()
    new_root = Path(new).resolve()
    if not old_root.is_dir() or not new_root.is_dir():
        raise NotADirectoryError(old_root if not old_root.is_dir() else new_root)
    issues: list[dict[str, Any]] = []
    tree = compare_trees(old_root, new_root)
    module = _module_delta(old_root, new_root, issues)
    toolchain = Toolchain(tools_root) if deep_scripts else None
    old_runtime_names = _runtime_names(old_root)
    new_runtime_names = _runtime_names(new_root)
    old_runtime_folded = {value.casefold(): value for value in old_runtime_names}
    new_runtime_folded = {value.casefold(): value for value in new_runtime_names}
    removed_runtime_names = [
        old_runtime_folded[key]
        for key in sorted(old_runtime_folded.keys() - new_runtime_folded.keys())
    ]
    added_runtime_names = [
        new_runtime_folded[key]
        for key in sorted(new_runtime_folded.keys() - old_runtime_folded.keys())
    ]
    if removed_runtime_names or added_runtime_names:
        issues.append(
            _issue(
                "info",
                "upgrade-runtime-script-set-changed",
                "Изменён набор runtime-имён; проверьте OnLoad, миграцию активных экземпляров и старые SAV",
                evidence=f"added={added_runtime_names}; removed={removed_runtime_names}",
            )
        )
    rson = _rson_comparisons(old_root, new_root, issues)
    scr = _scr_comparisons(
        old_root,
        new_root,
        issues,
        toolchain=toolchain,
        deep_scripts=deep_scripts,
    )

    configuration = {
        "added": [path for path in tree["added"] if Path(path).name.casefold() in _CONFIG_NAMES],
        "removed": [path for path in tree["removed"] if Path(path).name.casefold() in _CONFIG_NAMES],
        "changed": [path for path in tree["changed"] if Path(path).name.casefold() in _CONFIG_NAMES],
    }
    removed_resources = [
        path for path in tree["removed"] if Path(path).suffix.casefold() in _RESOURCE_EXTENSIONS
    ]
    for path in removed_resources:
        issues.append(
            _issue(
                "warning",
                "upgrade-resource-removed",
                f"Ресурс удалён в новой версии: {path}. Проверьте Main/CacheData/Lang и ссылки скриптов",
                path=str(new_root / path),
            )
        )
    for path in configuration["removed"]:
        issues.append(
            _issue(
                "warning",
                "upgrade-config-artifact-removed",
                f"Удалён конфигурационный артефакт {path}",
                path=str(new_root / path),
            )
        )

    audits: dict[str, Any] | None = None
    if audit:
        old_audit = audit_mod(old_root, profile=AuditProfile.RELEASE, tools_root=tools_root)
        new_audit = audit_mod(new_root, profile=AuditProfile.RELEASE, tools_root=tools_root)
        audits = {"old": old_audit.as_dict(), "new": new_audit.as_dict()}
        for audit_issue in new_audit.issues:
            if audit_issue.severity == "error" and not audit_issue.suppressed:
                issues.append(
                    _issue(
                        "error",
                        f"new-audit:{audit_issue.code}",
                        audit_issue.message,
                        path=audit_issue.path,
                        evidence=audit_issue.evidence,
                    )
                )

    # Keep one issue for identical evidence produced by source and deep SCR checks.
    deduplicated: list[dict[str, Any]] = []
    signatures: set[tuple[Any, ...]] = set()
    for item in issues:
        signature = (item.get("severity"), item.get("code"), item.get("path"), item.get("message"))
        if signature not in signatures:
            signatures.add(signature)
            deduplicated.append(item)
    return {
        "schema": UPGRADE_SCHEMA,
        "old": str(old_root),
        "new": str(new_root),
        "compatible": not any(item["severity"] == "error" for item in deduplicated),
        "tree": tree,
        "module_info": module,
        "configuration": configuration,
        "removed_resources": removed_resources,
        "scripts": {
            "rson": rson,
            "scr": scr,
            "deep_requested": deep_scripts,
            "runtime_names": {
                "old": old_runtime_names,
                "new": new_runtime_names,
                "added": added_runtime_names,
                "removed": removed_runtime_names,
            },
        },
        "audit": audits,
        "issues": deduplicated,
        "summary": {
            "errors": sum(item["severity"] == "error" for item in deduplicated),
            "warnings": sum(item["severity"] == "warning" for item in deduplicated),
            "changed_files": tree["summary"]["changed"],
            "added_files": tree["summary"]["added"],
            "removed_files": tree["summary"]["removed"],
            "changed_scripts": len(rson) + len(scr),
        },
    }


__all__ = ["check_upgrade"]
