from __future__ import annotations

import fnmatch
import os
import tempfile
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .blockpar import BlockParDocument, load_blockpar
from .discovery import discover_mods, load_mod
from .files import iter_files
from .formats import get_format_spec, inspect_file
from .game_text import lint_game_text
from .module_info import find_module_info, parse_module_info
from .resources import verify_resource
from .runtime_lint import (
    has_onstart_script_run,
    lint_main_runtime,
    lint_module_runtime,
    lint_rson_runtime,
)
from .script_artifacts import lint_script_cache
from .scripts import inspect_scr, load_rson
from .textio import DecodedText, read_text
from .toolchain import Toolchain, is_empty_rscript_lang_dat
from .validation import validate_collection, validate_mod


AUDIT_SCHEMA = "srhd-modkit-audit-v1"
CHECK_STATUSES = {"passed", "issues", "skipped", "unsupported", "failed"}


class AuditProfile(str, Enum):
    DEV = "dev"
    RELEASE = "release"

    @classmethod
    def parse(cls, value: str | AuditProfile) -> AuditProfile:
        return value if isinstance(value, cls) else cls(value.casefold())


@dataclass(frozen=True, slots=True)
class AuditIssue:
    severity: str
    code: str
    message: str
    path: str | None = None
    mod: str = ""
    validator: str = ""
    location: str | None = None
    evidence: str | None = None
    remediation: str | None = None
    suppressed: bool = False
    suppression: str | None = None

    @classmethod
    def from_value(
        cls,
        value: Any,
        *,
        validator: str,
        mod: str = "",
        path: str | Path | None = None,
    ) -> AuditIssue:
        if isinstance(value, cls):
            return replace(value, validator=value.validator or validator, mod=value.mod or mod)
        raw = value.as_dict() if hasattr(value, "as_dict") else dict(value)
        issue_path = raw.get("path", path)
        return cls(
            severity=str(raw.get("severity", "warning")),
            code=str(raw.get("code", "unknown-issue")),
            message=str(raw.get("message", value)),
            path=str(issue_path) if issue_path else None,
            mod=str(raw.get("mod") or mod),
            validator=validator,
            location=raw.get("location"),
            evidence=raw.get("evidence"),
            remediation=raw.get("remediation"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "mod": self.mod,
            "validator": self.validator,
            "location": self.location,
            "evidence": self.evidence,
            "remediation": self.remediation,
            "suppressed": self.suppressed,
            "suppression": self.suppression,
        }


@dataclass(frozen=True, slots=True)
class AuditCheck:
    name: str
    status: str
    issues: tuple[AuditIssue, ...] = ()
    checked_files: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)
    complete: bool = True

    def __post_init__(self) -> None:
        if self.status not in CHECK_STATUSES:
            raise ValueError(f"Неизвестное состояние проверки: {self.status}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "complete": self.complete,
            "checked_files": list(self.checked_files),
            "details": self.details,
            "issues": [issue.as_dict() for issue in self.issues],
        }


@dataclass(frozen=True, slots=True)
class AuditReport:
    target: str
    profile: AuditProfile
    checks: tuple[AuditCheck, ...]
    children: tuple[AuditReport, ...] = ()
    allowed: tuple[str, ...] = ()
    schema: str = AUDIT_SCHEMA

    @property
    def issues(self) -> tuple[AuditIssue, ...]:
        own = tuple(issue for check in self.checks for issue in check.issues)
        nested = tuple(issue for child in self.children for issue in child.issues)
        return own + nested

    @property
    def coverage_complete(self) -> bool:
        return all(check.complete for check in self.checks) and all(
            child.coverage_complete for child in self.children
        )

    def blocking_issues(self, *, warnings_as_errors: bool = False) -> tuple[AuditIssue, ...]:
        blocking = {"error", "warning"} if warnings_as_errors else {"error"}
        return tuple(
            issue for issue in self.issues if not issue.suppressed and issue.severity in blocking
        )

    def as_dict(self) -> dict[str, Any]:
        levels = ("error", "warning", "info")
        summary = {
            level: sum(issue.severity == level and not issue.suppressed for issue in self.issues)
            for level in levels
        }
        summary["suppressed"] = sum(issue.suppressed for issue in self.issues)
        summary["checks"] = len(self.checks) + sum(len(child.checks) for child in self.children)
        return {
            "schema": self.schema,
            "target": self.target,
            "profile": self.profile.value,
            "coverage_complete": self.coverage_complete,
            "allowed": list(self.allowed),
            "summary": summary,
            "checks": [check.as_dict() for check in self.checks],
            "mods": [child.as_dict() for child in self.children],
            "issues": [issue.as_dict() for issue in self.issues],
        }


@dataclass(slots=True)
class AuditContext:
    root: Path
    profile: AuditProfile
    tools: Toolchain
    temp: Path
    mod_name: str = ""
    dat_documents: dict[Path, BlockParDocument | None] = field(default_factory=dict)
    dat_failures: dict[Path, Exception] = field(default_factory=dict)


Validator = Callable[[AuditContext], AuditCheck]


@dataclass(frozen=True, slots=True)
class RegisteredValidator:
    name: str
    runner: Validator
    profiles: frozenset[AuditProfile]


class AuditRegistry:
    def __init__(self) -> None:
        self._validators: list[RegisteredValidator] = []

    def register(
        self,
        name: str,
        runner: Validator,
        *,
        profiles: Iterable[AuditProfile] = (AuditProfile.DEV, AuditProfile.RELEASE),
    ) -> None:
        if any(item.name == name for item in self._validators):
            raise ValueError(f"Проверка уже зарегистрирована: {name}")
        self._validators.append(RegisteredValidator(name, runner, frozenset(profiles)))

    def run(self, context: AuditContext) -> tuple[AuditCheck, ...]:
        checks: list[AuditCheck] = []
        for item in self._validators:
            if context.profile not in item.profiles:
                checks.append(
                    AuditCheck(
                        item.name,
                        "skipped",
                        details={"reason": f"проверка не входит в профиль {context.profile.value}"},
                        complete=False,
                    )
                )
                continue
            try:
                check = item.runner(context)
                checks.append(check if check.name == item.name else replace(check, name=item.name))
            except Exception as exc:
                severity = "error" if context.profile is AuditProfile.RELEASE else "warning"
                checks.append(
                    AuditCheck(
                        item.name,
                        "failed",
                        (
                            AuditIssue(
                                severity,
                                "audit-validator-failed",
                                str(exc),
                                str(context.root),
                                context.mod_name,
                                item.name,
                            ),
                        ),
                        complete=False,
                    )
                )
        return tuple(checks)


def _issue(
    context: AuditContext,
    validator: str,
    severity: str,
    code: str,
    message: str,
    path: Path | str | None = None,
    **kwargs: Any,
) -> AuditIssue:
    return AuditIssue(
        severity,
        code,
        message,
        str(Path(path).resolve()) if path else None,
        context.mod_name,
        validator,
        **kwargs,
    )


def _status(issues: Sequence[AuditIssue]) -> str:
    return "issues" if issues else "passed"


def _all_entries(root: Path) -> list[Path]:
    entries: list[Path] = []
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        entries.extend(current_path / name for name in dirs)
        entries.extend(current_path / name for name in files)
    return sorted(entries, key=lambda path: path.relative_to(root).as_posix().casefold())


def _relative_index(root: Path) -> dict[str, Path]:
    return {
        path.relative_to(root).as_posix().casefold(): path
        for path in iter_files(root)
    }


def _load_dat(context: AuditContext, path: Path) -> BlockParDocument | None:
    path = path.resolve()
    if path in context.dat_documents:
        return context.dat_documents[path]
    if path in context.dat_failures:
        raise context.dat_failures[path]
    if is_empty_rscript_lang_dat(path):
        context.dat_documents[path] = None
        return None
    output = context.temp / f"dat-{len(context.dat_documents) + len(context.dat_failures):06d}.txt"
    try:
        context.tools.convert_dat(path, output, verify=False)
        document = load_blockpar(output)
        context.dat_documents[path] = document
        return document
    except Exception as exc:
        context.dat_failures[path] = exc
        raise


def _structure_check(context: AuditContext) -> AuditCheck:
    name = "structure"
    issues: list[AuditIssue] = []
    info_path = find_module_info(context.root)
    if info_path is None:
        issues.append(
            _issue(context, name, "error", "module-info-missing", "ModuleInfo.txt не найден", context.root)
        )
    else:
        mod = load_mod(context.root)
        context.mod_name = mod.name
        issues.extend(AuditIssue.from_value(item, validator=name, mod=mod.name) for item in validate_mod(mod))

    entries = _all_entries(context.root)
    folded: dict[str, list[Path]] = {}
    for path in entries:
        relative = path.relative_to(context.root).as_posix()
        folded.setdefault(relative.casefold(), []).append(path)
        if path.is_symlink():
            issues.append(
                _issue(
                    context,
                    name,
                    "error",
                    "unsafe-symlink",
                    "Символические ссылки не допускаются в дереве релиза",
                    path,
                )
            )
    for paths in folded.values():
        if len(paths) > 1:
            issues.append(
                _issue(
                    context,
                    name,
                    "error",
                    "case-collision",
                    "Пути различаются только регистром: "
                    + ", ".join(path.relative_to(context.root).as_posix() for path in paths),
                    paths[0],
                )
            )
    return AuditCheck(name, _status(issues), tuple(issues), tuple(str(path) for path in entries))


_JUNK_PATTERNS = (
    "*.pyc",
    "*.pyo",
    "*.tmp",
    "*.temp",
    "*.swp",
    "*.swo",
    "*.orig",
    "*.rej",
    "*.old",
    "*.bak",
    "*.bak_*",
    "*~",
    ".ds_store",
    "thumbs.db",
    "desktop.ini",
)


def _workspace_artifacts_check(context: AuditContext) -> AuditCheck:
    name = "workspace-artifacts"
    issues: list[AuditIssue] = []
    severity = "error" if context.profile is AuditProfile.RELEASE else "warning"
    for path in _all_entries(context.root):
        relative = path.relative_to(context.root).as_posix()
        folded_parts = [part.casefold() for part in path.relative_to(context.root).parts]
        junk = any(part.startswith(".srhd-") for part in folded_parts)
        junk = junk or any(fnmatch.fnmatch(path.name.casefold(), pattern) for pattern in _JUNK_PATTERNS)
        if junk:
            issues.append(
                _issue(
                    context,
                    name,
                    severity,
                    "release-artifact",
                    f"Служебный или резервный файл не должен попадать в релиз: {relative}",
                    path,
                    remediation="Удалите файл из рабочей копии или явно подавите правило для этого пути.",
                )
            )
    return AuditCheck(name, _status(issues), tuple(issues))


def _format_signatures_check(context: AuditContext) -> AuditCheck:
    name = "format-signatures"
    issues: list[AuditIssue] = []
    checked: list[str] = []
    for path in iter_files(context.root):
        try:
            info = inspect_file(path)
            if info["signature_valid"] is not None:
                checked.append(str(path))
            if info["signature_valid"] is False:
                issues.append(
                    _issue(
                        context,
                        name,
                        "error",
                        "invalid-signature",
                        f"Расширение {path.suffix or '<без расширения>'} не соответствует сигнатуре файла",
                        path,
                        evidence=info.get("signature_reason"),
                    )
                )
        except Exception as exc:
            issues.append(_issue(context, name, "error", "format-inspection-failed", str(exc), path))
    return AuditCheck(name, _status(issues), tuple(issues), tuple(checked))


def _unknown_formats_check(context: AuditContext) -> AuditCheck:
    name = "unknown-formats"
    unknown: dict[str, int] = {}
    paths: list[str] = []
    for path in iter_files(context.root):
        if get_format_spec(path) is None:
            extension = path.suffix.casefold() or "<без расширения>"
            unknown[extension] = unknown.get(extension, 0) + 1
            paths.append(str(path))
    if not paths:
        return AuditCheck(name, "passed")
    return AuditCheck(
        name,
        "unsupported",
        checked_files=tuple(paths),
        details={
            "formats": dict(sorted(unknown.items())),
            "handling": "passthrough-sha256",
            "reason": "Файлы будут сохранены побайтно, но их внутренняя структура не проверяется.",
        },
        complete=False,
    )


def _dat_candidates(context: AuditContext) -> list[Path]:
    paths = [path for path in iter_files(context.root) if path.suffix.casefold() == ".dat"]
    if context.profile is AuditProfile.RELEASE:
        return paths
    critical_names = {"main.dat", "cachedata.dat", "lang.dat"}
    return [path for path in paths if path.name.casefold() in critical_names]


def _dat_check(context: AuditContext) -> AuditCheck:
    name = "blockpar-dat"
    candidates = _dat_candidates(context)
    if not candidates:
        return AuditCheck(name, "skipped", details={"reason": "DAT-файлы для профиля не найдены"})
    issues: list[AuditIssue] = []
    checked: list[str] = []
    for path in candidates:
        try:
            _load_dat(context, path)
            checked.append(str(path))
        except Exception as exc:
            issues.append(_issue(context, name, "error", "dat-invalid", str(exc), path))

    if context.profile is AuditProfile.RELEASE:
        source_cfg = context.root / "SOURCE" / "CFG"
        cfg = context.root / "CFG"
        if source_cfg.is_dir():
            for source in sorted(source_cfg.rglob("*.txt")):
                relative = source.relative_to(source_cfg)
                binary = cfg / relative.with_suffix(".dat")
                if not binary.is_file():
                    continue
                try:
                    source_document = load_blockpar(source)
                    binary_document = _load_dat(context, binary)
                    if binary_document is None or (
                        source_document.canonical_semantic() != binary_document.canonical_semantic()
                    ):
                        issues.append(
                            _issue(
                                context,
                                name,
                                "error",
                                "dat-source-binary-mismatch",
                                "Исходный TXT и игровой DAT содержат разные деревья BlockPar",
                                binary,
                                evidence=f"source={source.resolve()}",
                            )
                        )
                except Exception as exc:
                    issues.append(
                        _issue(
                            context,
                            name,
                            "error",
                            "dat-source-compare-failed",
                            str(exc),
                            source,
                            evidence=f"binary={binary.resolve()}",
                        )
                    )
    complete = context.profile is AuditProfile.RELEASE or len(candidates) == len(
        [path for path in iter_files(context.root) if path.suffix.casefold() == ".dat"]
    )
    return AuditCheck(
        name,
        _status(issues),
        tuple(issues),
        tuple(checked),
        details={"scope": "all" if context.profile is AuditProfile.RELEASE else "critical"},
        complete=complete,
    )


def _text_check(context: AuditContext) -> AuditCheck:
    name = "game-text"
    issues: list[AuditIssue] = []
    checked: list[str] = []
    info_path = find_module_info(context.root)
    if info_path:
        try:
            values = lint_game_text(
                read_text(info_path),
                info_path,
                allowed_encodings={"cp1251", "utf-16-le", "utf-16-be"},
            )
            issues.extend(AuditIssue.from_value(item, validator=name, mod=context.mod_name) for item in values)
            checked.append(str(info_path.resolve()))
        except Exception as exc:
            issues.append(_issue(context, name, "error", "game-text-load", str(exc), info_path))

    for path in iter_files(context.root):
        relative = path.relative_to(context.root)
        folded_parts = [part.casefold() for part in relative.parts]
        if path.suffix.casefold() == ".rson":
            try:
                issues.extend(
                    AuditIssue.from_value(item, validator=name, mod=context.mod_name)
                    for item in lint_game_text(read_text(path), path)
                )
                checked.append(str(path))
            except Exception as exc:
                issues.append(_issue(context, name, "error", "game-text-load", str(exc), path))
        elif path.suffix.casefold() == ".txt" and "cfg" in folded_parts:
            try:
                final_cfg = folded_parts[0] == "cfg"
                russian = "rus" in folded_parts or "_rus" in path.stem.casefold()
                decoded = read_text(path)
                values = lint_game_text(
                    decoded,
                    path,
                    require_cp1251=final_cfg and russian,
                    require_cp1251_representable=not (final_cfg and russian),
                )
                issues.extend(AuditIssue.from_value(item, validator=name, mod=context.mod_name) for item in values)
                checked.append(str(path))
            except Exception as exc:
                issues.append(_issue(context, name, "error", "game-text-load", str(exc), path))

    for path, document in context.dat_documents.items():
        relative_parts = [part.casefold() for part in path.relative_to(context.root).parts]
        if document is None or not relative_parts or relative_parts[0] != "cfg" or "rus" not in relative_parts:
            continue
        decoded = DecodedText(document.to_text(include_raw=False), document.encoding, document.had_bom)
        issues.extend(
            AuditIssue.from_value(item, validator=name, mod=context.mod_name)
            for item in lint_game_text(decoded, path, require_cp1251=True)
        )
        checked.append(str(path))
    return AuditCheck(name, _status(issues), tuple(issues), tuple(dict.fromkeys(checked)))


def _resource_integrity_check(context: AuditContext) -> AuditCheck:
    name = "resource-integrity"
    resources = [
        path for path in iter_files(context.root) if path.suffix.casefold() in {".gai", ".hai", ".pkg"}
    ]
    if not resources:
        return AuditCheck(name, "skipped", details={"reason": "GAI/HAI/PKG не найдены"})
    issues: list[AuditIssue] = []
    checked: list[str] = []
    for path in resources:
        try:
            verify_resource(path)
            checked.append(str(path))
        except Exception as exc:
            issues.append(_issue(context, name, "error", "resource-invalid", str(exc), path))
    return AuditCheck(name, _status(issues), tuple(issues), tuple(checked))


def _registrations(document: BlockParDocument) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    try:
        node = document.find_node("Data/Script")
    except KeyError:
        return result
    for parameter in node.parameters:
        result.setdefault(parameter.key.casefold(), []).append(parameter.value)
    return result


def _script_check(context: AuditContext) -> AuditCheck:
    name = "scripts"
    files = iter_files(context.root)
    scripts = [
        path
        for path in files
        if path.suffix.casefold() == ".scr"
        and path.relative_to(context.root).as_posix().casefold().startswith("data/script/")
    ]
    rsons = [path for path in files if path.suffix.casefold() == ".rson"]
    if not scripts and not rsons:
        return AuditCheck(name, "skipped", details={"reason": "SCR/RSON не найдены"})

    issues: list[AuditIssue] = []
    checked: list[str] = []
    for path in scripts:
        try:
            info = inspect_scr(path)
            checked.append(str(path))
            if not info["supported_version"]:
                issues.append(
                    _issue(
                        context,
                        name,
                        "error",
                        "scr-version",
                        f"Версия SCR {info['version']} не поддерживается",
                        path,
                    )
                )
        except Exception as exc:
            issues.append(_issue(context, name, "error", "scr-invalid", str(exc), path))

    index = _relative_index(context.root)
    main_path = index.get("cfg/main.dat") or index.get("source/cfg/main.txt")
    main_document: BlockParDocument | None = None
    registrations: dict[str, list[str]] = {}
    onstart = False
    if scripts and main_path is None:
        issues.append(
            _issue(
                context,
                name,
                "error",
                "main-dat-missing",
                "Есть SCR, но отсутствует CFG/Main.dat или SOURCE/CFG/Main.txt",
                context.root,
            )
        )
    elif main_path is not None:
        try:
            main_document = load_blockpar(main_path) if main_path.suffix.casefold() == ".txt" else _load_dat(context, main_path)
            if main_document is not None:
                registrations = _registrations(main_document)
                runtime_values = lint_main_runtime(main_document, main_path)
                issues.extend(
                    AuditIssue.from_value(item, validator=name, mod=context.mod_name)
                    for item in runtime_values
                )
                onstart = has_onstart_script_run(main_document)
            checked.append(str(main_path))
        except Exception as exc:
            issues.append(_issue(context, name, "error", "main-dat-invalid", str(exc), main_path))

    for path in scripts:
        expected = f"script.{path.stem}".casefold()
        values = registrations.get(path.stem.casefold(), [])
        if main_path is not None and not any(expected in value.casefold() for value in values):
            issues.append(
                _issue(
                    context,
                    name,
                    "error",
                    "scr-unregistered",
                    f"{path.name} не зарегистрирован в Data/Script",
                    path,
                )
            )

    runtime_values: list[Any] = []
    valid_rsons = 0
    for path in rsons:
        try:
            project = load_rson(path)
            structural = project.validate()
            issues.extend(
                AuditIssue.from_value(item, validator=name, mod=context.mod_name, path=path)
                for item in structural
            )
            if not any(item.severity == "error" for item in structural):
                valid_rsons += 1
                values = lint_rson_runtime(project)
                runtime_values.extend(values)
                issues.extend(
                    AuditIssue.from_value(item, validator=name, mod=context.mod_name, path=path)
                    for item in values
                )
            checked.append(str(path))
        except Exception as exc:
            issues.append(_issue(context, name, "error", "rson-invalid", str(exc), path))

    info_path = find_module_info(context.root)
    if info_path:
        try:
            issues.extend(
                AuditIssue.from_value(item, validator=name, mod=context.mod_name)
                for item in lint_module_runtime(parse_module_info(info_path))
            )
        except Exception as exc:
            issues.append(_issue(context, name, "error", "runtime-module-load", str(exc), info_path))

    onstart_risks = {
        "runtime-turn-direct-world-access",
        "runtime-turn-before-ui",
        "runtime-ui-readiness-source-missing",
    }
    if onstart and any(getattr(item, "code", "") in onstart_risks for item in runtime_values):
        issues.append(
            _issue(
                context,
                name,
                "error",
                "runtime-onstart-unguarded-world",
                "OnStart достигает Player/мира без доказанного барьера t_OnEnteringForm",
                main_path,
            )
        )

    cache_documents: list[tuple[Path, BlockParDocument]] = []
    for relative in ("source/cfg/cachedata.txt", "cfg/cachedata.txt", "cfg/cachedata.dat"):
        path = index.get(relative)
        if path is None:
            continue
        try:
            document = load_blockpar(path) if path.suffix.casefold() == ".txt" else _load_dat(context, path)
            if document is not None:
                cache_documents.append((path, document))
                checked.append(str(path))
        except Exception as exc:
            issues.append(_issue(context, name, "error", "cachedata-load", str(exc), path))
    issues.extend(
        AuditIssue.from_value(item, validator=name, mod=context.mod_name)
        for item in lint_script_cache(context.root, scripts, registrations, cache_documents)
    )

    semantic_complete = not scripts or valid_rsons > 0
    if scripts and not semantic_complete:
        issues.append(
            _issue(
                context,
                name,
                "info",
                "scr-semantic-analysis-unavailable",
                "SCR проверен бинарно, но без RSON полный смысловой анализ невозможен",
                context.root,
            )
        )
    return AuditCheck(
        name,
        _status(issues),
        tuple(issues),
        tuple(dict.fromkeys(checked)),
        details={"scr": len(scripts), "rson": len(rsons), "valid_rson": valid_rsons},
        complete=semantic_complete,
    )


def default_registry() -> AuditRegistry:
    registry = AuditRegistry()
    registry.register("structure", _structure_check)
    registry.register("workspace-artifacts", _workspace_artifacts_check)
    registry.register("format-signatures", _format_signatures_check)
    registry.register("unknown-formats", _unknown_formats_check)
    registry.register("blockpar-dat", _dat_check)
    registry.register("game-text", _text_check)
    registry.register("scripts", _script_check)
    registry.register(
        "resource-integrity",
        _resource_integrity_check,
        profiles=(AuditProfile.RELEASE,),
    )
    return registry


def _apply_allowances(report: AuditReport, rules: Sequence[str]) -> AuditReport:
    if not rules:
        return report
    target = Path(report.target)

    def suppress(issue: AuditIssue) -> AuditIssue:
        for raw_rule in rules:
            code, separator, pattern = raw_rule.partition(":")
            if issue.code != code:
                continue
            if separator:
                if not issue.path:
                    continue
                path = Path(issue.path)
                try:
                    candidate = path.relative_to(target).as_posix()
                except ValueError:
                    candidate = path.as_posix()
                if not fnmatch.fnmatch(candidate.casefold(), pattern.replace("\\", "/").casefold()):
                    continue
            return replace(issue, suppressed=True, suppression=raw_rule)
        return issue

    checks = tuple(
        replace(check, issues=tuple(suppress(issue) for issue in check.issues))
        for check in report.checks
    )
    children = tuple(_apply_allowances(child, rules) for child in report.children)
    return replace(report, checks=checks, children=children, allowed=tuple(rules))


def audit_mod(
    path: str | Path,
    *,
    profile: str | AuditProfile = AuditProfile.DEV,
    tools_root: str | Path | None = None,
    allow: Sequence[str] = (),
    registry: AuditRegistry | None = None,
) -> AuditReport:
    root = Path(path).resolve()
    if not root.is_dir():
        raise NotADirectoryError(root)
    parsed_profile = AuditProfile.parse(profile)
    with tempfile.TemporaryDirectory(prefix="srhd-audit-") as temp_name:
        context = AuditContext(root, parsed_profile, Toolchain(tools_root), Path(temp_name))
        checks = (registry or default_registry()).run(context)
    return _apply_allowances(
        AuditReport(str(root), parsed_profile, checks),
        allow,
    )


def audit_collection(
    root: str | Path,
    *,
    profile: str | AuditProfile = AuditProfile.DEV,
    tools_root: str | Path | None = None,
    allow: Sequence[str] = (),
    registry: AuditRegistry | None = None,
) -> AuditReport:
    collection_root = Path(root).resolve()
    mods = discover_mods(collection_root)
    parsed_profile = AuditProfile.parse(profile)
    collection_issues = [
        AuditIssue.from_value(item, validator="collection", mod=getattr(item, "mod", ""))
        for item in validate_collection(mods)
        if item.code in {"duplicate-name", "missing-dependency"}
    ]
    collection_check = AuditCheck(
        "collection",
        _status(collection_issues),
        tuple(collection_issues),
        details={"mods": len(mods)},
    )
    children = tuple(
        audit_mod(
            mod.root,
            profile=parsed_profile,
            tools_root=tools_root,
            allow=allow,
            registry=registry,
        )
        for mod in mods
    )
    return _apply_allowances(
        AuditReport(
            str(collection_root),
            parsed_profile,
            (collection_check,),
            children,
        ),
        allow,
    )

