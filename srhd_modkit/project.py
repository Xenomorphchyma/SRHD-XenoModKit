from __future__ import annotations

import fnmatch
import glob
import hashlib
import json
import os
import re
import shutil
import string
import tempfile
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from .files import build_manifest, iter_files, sha256_file, stage_tree
from .release import (
    DeployPlan,
    DeployResult,
    ReleaseResult,
    build_release,
    deploy_mod,
    plan_deploy,
)
from .scripts import inspect_scr
from .rsm import inspect_rsm_project
from .safe_io import atomic_write_text, publish_files_transactionally
from .toolchain import Toolchain


PROJECT_SCHEMA = "srhd-modkit-project-v1"
PROJECT_BUILD_SCHEMA = "srhd-modkit-project-build-v1"
PROJECT_DEPLOY_SCHEMA = "srhd-modkit-project-deploy-v1"
PROJECT_PUBLISH_SCHEMA = "srhd-modkit-project-publish-v1"
CACHE_SCHEMA = "srhd-modkit-build-cache-v1"

# The cache is disposable derived data. Keep enough recent revisions for
# normal branch/variant switching, but never let abandoned fingerprints grow
# without a bound. Entries used by the current build are always protected,
# even when a very large project exceeds these soft limits by itself.
CACHE_KEEP_PER_ARTIFACT = 3
CACHE_MAX_ENTRIES = 256
CACHE_MAX_BYTES = 2 * 1024 * 1024 * 1024
CACHE_STALE_TEMP_SECONDS = 24 * 60 * 60

PROJECT_FILE_NAME = "srhd-modkit.toml"
LOCAL_PROJECT_FILE_NAME = "srhd-modkit.local.toml"

_VARIANT_CONTROL_KEYS = {
    "inherits",
    "include",
    "exclude",
    "overlays",
    "variables",
    "strip_sources",
}

_WINDOWS_RESERVED_COMPONENTS = {
    "con", "prn", "aux", "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


class ProjectConfigError(ValueError):
    pass


def _safe_output_component(value: str, field: str) -> str:
    """Validate a project-controlled Windows file/directory name.

    Project and variant names are reused as build directory and sidecar file
    names.  Keeping them to one ordinary component prevents path traversal and
    makes a shared project behave the same on every supported Windows host.
    """

    if value != value.strip() or not value or value in {".", ".."}:
        raise ProjectConfigError(f"{field} должен быть безопасным именем одного компонента: {value!r}")
    if any(ord(character) < 32 or character in '<>:"/\\|?*' for character in value):
        raise ProjectConfigError(f"{field} содержит недопустимый символ Windows: {value!r}")
    if value.endswith((".", " ")):
        raise ProjectConfigError(f"{field} не должен оканчиваться точкой или пробелом: {value!r}")
    stem = value.split(".", 1)[0].casefold()
    if stem in _WINDOWS_RESERVED_COMPONENTS:
        raise ProjectConfigError(f"{field} использует зарезервированное имя Windows: {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class ProjectTarget:
    name: str
    root: Path
    prefix: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "root": str(self.root), "prefix": self.prefix}


@dataclass(frozen=True, slots=True)
class ModProject:
    path: Path
    local_path: Path | None
    root: Path
    raw: dict[str, Any]
    name: str
    mod_root: Path
    prefix: str
    default_variant: str
    build_root: Path
    cache_root: Path
    tools_root: Path | None

    @property
    def variants(self) -> Mapping[str, Any]:
        value = self.raw.get("variants", {})
        return value if isinstance(value, Mapping) else {}

    @property
    def artifacts(self) -> tuple[dict[str, Any], ...]:
        value = self.raw.get("artifacts", [])
        return tuple(item for item in value if isinstance(item, dict)) if isinstance(value, list) else ()

    @property
    def targets(self) -> Mapping[str, Any]:
        value = self.raw.get("targets", {})
        return value if isinstance(value, Mapping) else {}

    @property
    def external_builds(self) -> tuple[dict[str, Any], ...]:
        value = self.raw.get("external_builds", [])
        return tuple(item for item in value if isinstance(item, dict)) if isinstance(value, list) else ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": PROJECT_SCHEMA,
            "path": str(self.path),
            "local_path": str(self.local_path) if self.local_path is not None else None,
            "root": str(self.root),
            "name": self.name,
            "mod_root": str(self.mod_root),
            "prefix": self.prefix,
            "default_variant": self.default_variant,
            "build_root": str(self.build_root),
            "cache_root": str(self.cache_root),
            "tools_root": str(self.tools_root) if self.tools_root is not None else None,
            "variants": sorted(self.variants),
            "artifacts": [str(item.get("id", "")) for item in self.artifacts],
            "targets": sorted(self.targets),
            "external_builds": [str(item.get("id", "")) for item in self.external_builds],
        }


@dataclass(frozen=True, slots=True)
class ProjectBuildResult:
    project: ModProject
    variant: str
    output: Path
    prefix: str
    audit_allow: tuple[str, ...]
    artifacts: tuple[dict[str, Any], ...]
    cache_hits: int
    cache_misses: int
    deploy: DeployResult
    provenance_path: Path
    provenance: dict[str, Any]
    schema: str = PROJECT_BUILD_SCHEMA

    def as_dict(self, *, include_audit: bool = True) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "project": str(self.project.path),
            "variant": self.variant,
            "output": str(self.output),
            "prefix": self.prefix,
            "allow": list(self.audit_allow),
            "verified": self.deploy.verified,
            "artifacts": list(self.artifacts),
            "cache": {
                "hits": self.cache_hits,
                "misses": self.cache_misses,
                "maintenance": self.provenance.get("cache", {}).get("maintenance", {}),
            },
            "provenance": str(self.provenance_path),
            "deploy": self.deploy.as_dict(include_audit=include_audit),
        }


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        existing = result.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            result[key] = _deep_merge(existing, value)
        else:
            result[key] = value
    return result


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            value = tomllib.load(stream)
    except tomllib.TOMLDecodeError as exc:
        raise ProjectConfigError(f"Некорректный TOML {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProjectConfigError(f"Корень {path} должен быть TOML-таблицей")
    return value


def find_project_file(start: str | Path = ".") -> Path:
    candidate = Path(start).resolve()
    if candidate.is_file():
        return candidate
    for directory in (candidate, *candidate.parents):
        project = directory / PROJECT_FILE_NAME
        if project.is_file():
            return project
    raise FileNotFoundError(f"Не найден {PROJECT_FILE_NAME} от {candidate}")


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _project_path(
    root: Path,
    value: str | Path,
    field: str,
    *,
    allow_absolute: bool = True,
    reject_links: bool = False,
) -> Path:
    raw = Path(str(value))
    unresolved = raw.absolute() if raw.is_absolute() else (root / raw).absolute()
    if reject_links:
        current = unresolved
        while True:
            if _is_link_or_junction(current):
                raise ProjectConfigError(f"{field} проходит через ссылку или junction: {current}")
            if current == root or root not in current.parents:
                break
            current = current.parent
    candidate = unresolved.resolve()
    if not allow_absolute and root != candidate and root not in candidate.parents:
        raise ProjectConfigError(f"{field} вышел за корень проекта: {candidate}")
    return candidate


def _safe_relative(value: str, field: str) -> PurePosixPath:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(":" in part for part in path.parts)
    ):
        raise ProjectConfigError(f"{field} должен быть безопасным относительным путём: {value!r}")
    return path


def _effective_artifact_source(
    project: ModProject,
    artifact: Mapping[str, Any],
    full_mod: Path,
) -> Path:
    identifier = str(artifact.get("id", ""))
    source = _project_path(
        project.root,
        str(artifact["source"]),
        f"artifact {identifier}.source",
        allow_absolute=False,
        reject_links=True,
    )
    try:
        relative = source.relative_to(project.mod_root)
    except ValueError:
        return source
    return full_mod / relative


def _external_build_issues(project: ModProject) -> list[dict[str, Any]]:
    """Validate explicit hand-off points without executing untrusted builds."""

    issues: list[dict[str, Any]] = []
    for item in project.external_builds:
        identifier = str(item.get("id", ""))
        source = _project_path(
            project.root,
            str(item.get("project", "")),
            f"external_builds.{identifier}.project",
            allow_absolute=False,
            reject_links=True,
        )
        if not source.is_file():
            issues.append(
                {
                    "severity": "error",
                    "code": "project-external-project-missing",
                    "message": f"Внешний проект {identifier!r} не найден: {source}",
                    "path": str(source),
                }
            )
        mode = str(item.get("mode", "unconfigured")).casefold()
        outputs = [
            _project_path(
                project.root,
                value,
                f"external_builds.{identifier}.outputs",
                allow_absolute=False,
                reject_links=True,
            )
            for value in item.get("outputs", [])
        ]
        if mode == "unconfigured":
            issues.append(
                {
                    "severity": "error",
                    "code": "project-external-build-unconfigured",
                    "message": (
                        f"Внешняя {item.get('kind', 'native')} сборка {identifier!r} обнаружена, "
                        "но не подтверждена. Соберите её отдельно, перечислите runtime outputs "
                        "и задайте mode = \"prebuilt\""
                    ),
                    "path": str(source),
                }
            )
        elif not outputs:
            issues.append(
                {
                    "severity": "error",
                    "code": "project-external-output-unlisted",
                    "message": f"Для внешней сборки {identifier!r} не перечислены runtime outputs",
                    "path": str(source),
                }
            )
        for output in outputs:
            if output != project.mod_root and project.mod_root not in output.parents:
                issues.append(
                    {
                        "severity": "error",
                        "code": "project-external-output-outside-mod",
                        "message": (
                            f"Runtime output {output} находится вне mod_root и не попадёт в публикацию"
                        ),
                        "path": str(output),
                    }
                )
                continue
            if (
                not output.is_file()
                or output.is_symlink()
                or bool(getattr(output, "is_junction", lambda: False)())
            ):
                issues.append(
                    {
                        "severity": "error",
                        "code": "project-external-output-missing",
                        "message": f"Runtime output внешней сборки отсутствует: {output}",
                        "path": str(output),
                    }
                )
    return issues


def _external_build_report(project: ModProject) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in project.external_builds:
        identifier = str(item.get("id", ""))
        source = _project_path(
            project.root,
            str(item.get("project", "")),
            f"external_builds.{identifier}.project",
            allow_absolute=False,
            reject_links=True,
        )
        outputs = [
            _project_path(
                project.root,
                value,
                f"external_builds.{identifier}.outputs",
                allow_absolute=False,
                reject_links=True,
            )
            for value in item.get("outputs", [])
        ]
        result.append(
            {
                "id": identifier,
                "kind": str(item.get("kind", "native")),
                "mode": str(item.get("mode", "unconfigured")),
                "project": str(source),
                "project_sha256": sha256_file(source) if source.is_file() else None,
                "outputs": [
                    {
                        "path": str(output),
                        "sha256": sha256_file(output) if output.is_file() else None,
                        "size": output.stat().st_size if output.is_file() else None,
                    }
                    for output in outputs
                ],
            }
        )
    return result


def _validate_project_config(path: Path, raw: Mapping[str, Any]) -> None:
    schema = raw.get("schema")
    if schema != PROJECT_SCHEMA:
        raise ProjectConfigError(f"{path}: ожидалась schema = {PROJECT_SCHEMA!r}")
    for key in ("name", "mod_root", "prefix"):
        if not isinstance(raw.get(key), str) or not str(raw[key]).strip():
            raise ProjectConfigError(f"{path}: обязательное строковое поле {key!r}")
    _safe_output_component(str(raw["name"]), "name")
    _safe_relative(str(raw["prefix"]), "prefix")
    artifacts = raw.get("artifacts", [])
    if not isinstance(artifacts, list):
        raise ProjectConfigError("artifacts должен быть массивом таблиц [[artifacts]]")
    identifiers: set[str] = set()
    for index, item in enumerate(artifacts):
        if not isinstance(item, Mapping):
            raise ProjectConfigError(f"artifacts[{index}] должен быть таблицей")
        identifier = str(item.get("id", "")).strip()
        kind = str(item.get("kind", "")).casefold()
        if not identifier or identifier in identifiers:
            raise ProjectConfigError(f"artifacts[{index}].id отсутствует или повторяется: {identifier!r}")
        if kind not in {"dat", "rson", "rsm", "copy"}:
            raise ProjectConfigError(f"artifacts[{index}].kind не поддерживается: {kind!r}")
        if not isinstance(item.get("source"), str) or not isinstance(item.get("output"), str):
            raise ProjectConfigError(f"artifacts[{index}] требует source и output")
        selected_variants = item.get("variants")
        if selected_variants is not None and (
            not isinstance(selected_variants, list)
            or not all(isinstance(value, str) for value in selected_variants)
        ):
            raise ProjectConfigError(f"artifacts[{index}].variants должен быть массивом строк")
        inputs = item.get("inputs", [])
        if not isinstance(inputs, list) or not all(isinstance(value, str) for value in inputs):
            raise ProjectConfigError(f"artifacts[{index}].inputs должен быть массивом строк")
        identifiers.add(identifier)
    variants = raw.get("variants", {})
    if variants and not isinstance(variants, Mapping):
        raise ProjectConfigError("variants должен быть таблицей")
    if isinstance(variants, Mapping):
        for name, variant in variants.items():
            _safe_output_component(str(name), f"variants.{name}")
            if not isinstance(variant, Mapping):
                raise ProjectConfigError(f"variants.{name} должен быть таблицей")
            if variant.get("inherits") is not None and not isinstance(variant.get("inherits"), str):
                raise ProjectConfigError(f"variants.{name}.inherits должен быть строкой")
            for field in ("include", "exclude", "overlays", "allow"):
                value = variant.get(field, [])
                if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                    raise ProjectConfigError(f"variants.{name}.{field} должен быть массивом строк")
    targets = raw.get("targets", {})
    if targets and not isinstance(targets, Mapping):
        raise ProjectConfigError("targets должен быть таблицей")
    if isinstance(targets, Mapping):
        for name, target in targets.items():
            if not isinstance(target, Mapping):
                raise ProjectConfigError(f"targets.{name} должен быть таблицей")
            for field in ("root", "prefix"):
                if target.get(field) is not None and not isinstance(target.get(field), str):
                    raise ProjectConfigError(f"targets.{name}.{field} должен быть строкой")
    allow = raw.get("allow", [])
    if not isinstance(allow, list) or not all(isinstance(item, str) for item in allow):
        raise ProjectConfigError("allow должен быть массивом строк")
    external_builds = raw.get("external_builds", [])
    if not isinstance(external_builds, list):
        raise ProjectConfigError("external_builds должен быть массивом таблиц [[external_builds]]")
    external_ids: set[str] = set()
    for index, item in enumerate(external_builds):
        if not isinstance(item, Mapping):
            raise ProjectConfigError(f"external_builds[{index}] должен быть таблицей")
        identifier = str(item.get("id", "")).strip()
        if not identifier or identifier.casefold() in external_ids:
            raise ProjectConfigError(f"external_builds[{index}].id отсутствует или повторяется")
        if str(item.get("mode", "unconfigured")).casefold() not in {"unconfigured", "prebuilt"}:
            raise ProjectConfigError(
                f"external_builds[{index}].mode должен быть unconfigured или prebuilt"
            )
        if not isinstance(item.get("project"), str):
            raise ProjectConfigError(f"external_builds[{index}].project должен быть строкой")
        outputs = item.get("outputs", [])
        if not isinstance(outputs, list) or not all(isinstance(value, str) for value in outputs):
            raise ProjectConfigError(f"external_builds[{index}].outputs должен быть массивом строк")
        external_ids.add(identifier.casefold())


def load_project(path: str | Path = ".") -> ModProject:
    project_path = find_project_file(path)
    root = project_path.parent.resolve()
    raw = _load_toml(project_path)
    local_path = root / LOCAL_PROJECT_FILE_NAME
    if local_path.is_file():
        raw = _deep_merge(raw, _load_toml(local_path))
        selected_local: Path | None = local_path
    else:
        selected_local = None
    _validate_project_config(project_path, raw)
    mod_root = _project_path(
        root,
        str(raw["mod_root"]),
        "mod_root",
        allow_absolute=False,
        reject_links=True,
    )
    if not mod_root.is_dir():
        raise NotADirectoryError(mod_root)
    build_root = _project_path(
        root,
        str(raw.get("build_root", ".srhd-build")),
        "build_root",
        reject_links=True,
    )
    cache_root = _project_path(
        root,
        str(raw.get("cache_root", ".srhd-cache")),
        "cache_root",
        reject_links=True,
    )
    for field, candidate in (("build_root", build_root), ("cache_root", cache_root)):
        if candidate == mod_root or mod_root in candidate.parents:
            relative = candidate.relative_to(mod_root)
            if not relative.parts or not relative.parts[0].casefold().startswith(".srhd-"):
                raise ProjectConfigError(
                    f"{field} внутри игрового mod_root должен быть служебным .srhd-* каталогом: {candidate}"
                )
    tools_value = raw.get("tools_root")
    tools_root = _project_path(root, str(tools_value), "tools_root") if tools_value else None
    variants = raw.get("variants", {})
    default_variant = str(raw.get("default_variant") or (next(iter(variants)) if variants else "default"))
    if variants and default_variant not in variants:
        raise ProjectConfigError(f"default_variant {default_variant!r} отсутствует в [variants]")
    project = ModProject(
        path=project_path,
        local_path=selected_local,
        root=root,
        raw=dict(raw),
        name=str(raw["name"]),
        mod_root=mod_root,
        prefix=str(raw["prefix"]),
        default_variant=default_variant,
        build_root=build_root,
        cache_root=cache_root,
        tools_root=tools_root,
    )
    names = tuple(project.variants) if project.variants else ("default",)
    for name in names:
        _selected_name, variant_config, variables = _resolve_variant(project, name)
        prefix = str(_expand(variant_config.get("prefix", project.prefix), variables, "variant.prefix"))
        _safe_relative(prefix, "variant.prefix")
        _selected_artifacts(project, name, variables)
    return project


def _resolve_variant(project: ModProject, name: str | None) -> tuple[str, dict[str, Any], dict[str, str]]:
    selected = name or project.default_variant
    variants = project.variants
    if not variants:
        if selected != "default":
            raise ProjectConfigError(f"Проект не объявляет variant {selected!r}")
        merged: dict[str, Any] = {}
    else:
        visiting: set[str] = set()
        resolved: dict[str, dict[str, Any]] = {}

        def resolve(item_name: str) -> dict[str, Any]:
            if item_name in resolved:
                return resolved[item_name]
            if item_name in visiting:
                raise ProjectConfigError(f"Цикл inherits у variant {item_name!r}")
            raw = variants.get(item_name)
            if not isinstance(raw, Mapping):
                raise ProjectConfigError(f"Неизвестный variant {item_name!r}")
            visiting.add(item_name)
            parent_name = raw.get("inherits")
            parent = resolve(str(parent_name)) if parent_name else {}
            value = _deep_merge(parent, raw)
            visiting.remove(item_name)
            resolved[item_name] = value
            return value

        merged = resolve(selected)

    variables: dict[str, str] = {
        "name": project.name,
        "variant": selected,
    }
    project_variables = project.raw.get("variables", {})
    if isinstance(project_variables, Mapping):
        variables.update({str(key): str(value) for key, value in project_variables.items()})
    variant_variables = merged.get("variables", {})
    if isinstance(variant_variables, Mapping):
        variables.update({str(key): str(value) for key, value in variant_variables.items()})
    for key, value in merged.items():
        if key not in _VARIANT_CONTROL_KEYS and isinstance(value, (str, int, float, bool)):
            variables[str(key)] = str(value)
    return selected, merged, variables


def _expand(value: Any, variables: Mapping[str, str], field: str) -> Any:
    if isinstance(value, str):
        try:
            return string.Template(value).substitute(variables)
        except KeyError as exc:
            raise ProjectConfigError(f"{field}: неизвестная переменная ${{{exc.args[0]}}}") from exc
    if isinstance(value, list):
        return [_expand(item, variables, field) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item, variables, f"{field}.{key}") for key, item in value.items()}
    return value


def _copy_file_verified(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    if source.stat().st_size != destination.stat().st_size or sha256_file(source) != sha256_file(destination):
        raise OSError(f"Копия не прошла SHA-256-проверку: {source} -> {destination}")


def _copy_overlay(source: Path, destination: Path) -> list[Path]:
    if not source.is_dir():
        raise NotADirectoryError(source)
    copied: list[Path] = []
    for path in iter_files(source):
        target = destination / path.relative_to(source)
        _copy_file_verified(path, target)
        copied.append(target)
    return copied


def _apply_variant_files(
    project: ModProject,
    full_mod: Path,
    variant: Mapping[str, Any],
    variables: Mapping[str, str],
) -> dict[str, list[str]]:
    included: list[str] = []
    overlaid: list[str] = []
    removed: list[str] = []
    for raw_overlay in _expand(list(variant.get("overlays", [])), variables, "variant.overlays"):
        overlay_rel = _safe_relative(str(raw_overlay), "variant.overlays")
        overlay = _project_path(
            project.root,
            Path(*overlay_rel.parts),
            "variant.overlays",
            allow_absolute=False,
            reject_links=True,
        )
        if project.root not in overlay.parents:
            raise ProjectConfigError(f"overlay вышел за проект: {overlay}")
        for target in _copy_overlay(overlay, full_mod):
            overlaid.append(target.relative_to(full_mod).as_posix())

    for raw_pattern in _expand(list(variant.get("include", [])), variables, "variant.include"):
        pattern = str(_safe_relative(str(raw_pattern), "variant.include"))
        matches = sorted(glob.glob(str(project.root / pattern), recursive=True), key=str.casefold)
        for raw_match in matches:
            unresolved = Path(raw_match).absolute()
            if not unresolved.is_file():
                continue
            source = _project_path(
                project.root,
                unresolved,
                "variant.include",
                allow_absolute=False,
                reject_links=True,
            )
            relative = source.relative_to(project.root)
            if any(part.casefold().startswith(".srhd-") for part in relative.parts):
                continue
            target = full_mod / relative
            _copy_file_verified(source, target)
            included.append(relative.as_posix())

    patterns = [
        str(_safe_relative(str(item), "variant.exclude")).casefold()
        for item in _expand(list(variant.get("exclude", [])), variables, "variant.exclude")
    ]
    if patterns:
        for path in sorted(
            (item for item in full_mod.rglob("*") if item.is_file()),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            relative = path.relative_to(full_mod).as_posix()
            if any(fnmatch.fnmatch(relative.casefold(), pattern) for pattern in patterns):
                path.unlink()
                removed.append(relative)
        for directory in sorted(
            (item for item in full_mod.rglob("*") if item.is_dir()),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
    return {"included": included, "overlaid": overlaid, "removed": removed}


def _artifact_inputs(
    project: ModProject,
    artifact: Mapping[str, Any],
    source: Path,
    full_mod: Path,
) -> list[Path]:
    inputs: list[Path] = []
    if source.is_dir():
        inputs.extend(iter_files(source))
    elif source.is_file():
        inputs.append(source)
    else:
        raise FileNotFoundError(source)
    if str(artifact.get("kind", "")).casefold() == "rsm" and source.is_file():
        rsm_project = inspect_rsm_project(source)
        inputs.extend(module.path for module in rsm_project.modules)
    for field in ("lang_base",):
        value = artifact.get(field)
        if value:
            inputs.append(
                _project_path(
                    project.root,
                    str(value),
                    field,
                    allow_absolute=False,
                    reject_links=True,
                )
            )
    # rsmc merges language output into an already existing file.  Its original
    # bytes are therefore an input, even though the same path is also an output.
    for field in ("lang_txt", "lang_dat"):
        value = artifact.get(field)
        if value:
            relative = _safe_relative(str(value), f"artifact.{field}")
            candidate = full_mod / Path(*relative.parts)
            if candidate.is_file():
                inputs.append(candidate)
    for value in artifact.get("inputs", []) if isinstance(artifact.get("inputs", []), list) else []:
        candidate = _project_path(
            project.root,
            str(value),
            "artifact.inputs",
            allow_absolute=False,
            reject_links=True,
        )
        if candidate.is_dir():
            inputs.extend(iter_files(candidate))
        elif candidate.is_file():
            inputs.append(candidate)
        else:
            raise FileNotFoundError(candidate)
    unique = {str(path.resolve()).casefold(): path.resolve() for path in inputs}
    return sorted(unique.values(), key=lambda item: str(item).casefold())


def _tool_fingerprints(toolchain: Toolchain, names: Iterable[str]) -> list[dict[str, Any]]:
    return [toolchain.fingerprint(name) for name in sorted(set(names))]


def _artifact_tool_names(artifact: Mapping[str, Any]) -> tuple[str, ...]:
    kind = str(artifact.get("kind", "")).casefold()
    if kind == "dat":
        return ("blockpar",)
    if kind == "rson":
        return ("rscript", "blockpar") if artifact.get("lang_dat") else ("rscript",)
    if kind == "rsm":
        names = ["rsmc", "rscript"]
        if artifact.get("lang_dat"):
            names.append("blockpar")
        return tuple(names)
    return ()


def _artifact_cache_key(
    project: ModProject,
    variant: str,
    artifact: Mapping[str, Any],
    inputs: Sequence[Path],
    tools: Sequence[dict[str, Any]],
    full_mod: Path,
) -> tuple[str, dict[str, Any]]:
    input_rows: list[dict[str, Any]] = []
    for path in inputs:
        try:
            label = path.relative_to(project.root).as_posix()
        except ValueError:
            try:
                label = f"mod:{path.relative_to(full_mod).as_posix()}"
            except ValueError:
                label = str(path)
        input_rows.append({"path": label, "size": path.stat().st_size, "sha256": sha256_file(path)})
    package_root = Path(__file__).resolve().parent
    engine_files = tuple(
        path
        for path in iter_files(package_root)
        if path.suffix.casefold() == ".py"
        or (
            path.suffix.casefold() == ".json"
            and "schemas" in {part.casefold() for part in path.relative_to(package_root).parts}
        )
    )
    payload = {
        "schema": CACHE_SCHEMA,
        "variant": variant,
        "artifact": artifact,
        "inputs": input_rows,
        "tools": list(tools),
        "engine": [
            {"path": item.relative_to(package_root).as_posix(), "sha256": sha256_file(item)}
            for item in engine_files
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), payload


class ArtifactCache:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve() / "artifacts"

    def _entry(self, key: str) -> Path:
        if re.fullmatch(r"[0-9a-f]{64}", key) is None:
            raise ProjectConfigError(f"Некорректный ключ кэша: {key!r}")
        entry = self.root / key[:2] / key
        if self.root != entry and self.root not in entry.parents:
            raise ProjectConfigError(f"Ключ кэша вышел за корень: {key!r}")
        return entry

    @staticmethod
    def _tree_usage(path: Path) -> tuple[int, int]:
        files = 0
        size = 0
        try:
            for current, directories, names in os.walk(path, followlinks=False):
                current_path = Path(current)
                directories[:] = [
                    name
                    for name in directories
                    if not (current_path / name).is_symlink()
                ]
                for name in names:
                    candidate = current_path / name
                    if candidate.is_symlink():
                        continue
                    try:
                        size += candidate.stat().st_size
                        files += 1
                    except OSError:
                        continue
        except OSError:
            pass
        return files, size

    @staticmethod
    def _remove_cache_path(path: Path) -> bool:
        try:
            if path.is_symlink() or path.is_file():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                shutil.rmtree(path)
            return not path.exists()
        except OSError:
            return False

    def _record(self, entry: Path, *, verify_files: bool = False) -> dict[str, Any] | None:
        manifest_path = entry / "manifest.json"
        try:
            if entry.is_symlink():
                return None
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            key = str(manifest["key"])
            fingerprint = manifest["fingerprint"]
            artifact = fingerprint["artifact"]
            variant = str(fingerprint["variant"])
            identifier = str(artifact["id"])
            outputs = manifest["outputs"]
            if (
                manifest.get("schema") != CACHE_SCHEMA
                or entry.name != key
                or entry.parent.name != key[:2]
                or len(key) != 64
                or any(character not in "0123456789abcdef" for character in key)
                or not isinstance(outputs, list)
                or not outputs
            ):
                return None
            expected_names: set[str] = set()
            expected_size = manifest_path.stat().st_size
            expected_files = 1
            for item in outputs:
                if not isinstance(item, Mapping):
                    return None
                relative = _safe_relative(str(item["path"]), "cache.outputs.path")
                folded = relative.as_posix().casefold()
                if folded in expected_names:
                    return None
                expected_names.add(folded)
                cached = entry / "files" / Path(*relative.parts)
                item_size = int(item["size"])
                if verify_files:
                    if (
                        not cached.is_file()
                        or cached.is_symlink()
                        or cached.stat().st_size != item_size
                        or sha256_file(cached) != str(item["sha256"])
                    ):
                        return None
                expected_size += item_size
                expected_files += 1
            if verify_files:
                actual_files, actual_size = self._tree_usage(entry)
                if actual_files != expected_files or actual_size != expected_size:
                    return None
            return {
                "path": entry,
                "key": key,
                "identity": (variant, identifier),
                "fingerprint": fingerprint,
                "files": expected_files,
                "bytes": expected_size,
                "modified": entry.stat().st_mtime,
            }
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError, ProjectConfigError):
            return None

    def probe(self, key: str) -> dict[str, Any] | None:
        """Return a verified cache record without restoring files or touching LRU state."""

        return self._record(self._entry(key), verify_files=True)

    def latest(self, variant: str, artifact_id: str) -> dict[str, Any] | None:
        """Return the newest readable fingerprint for one logical artifact."""

        if not self.root.is_dir() or self.root.is_symlink():
            return None
        matches: list[dict[str, Any]] = []
        for bucket in self.root.iterdir():
            if not bucket.is_dir() or bucket.is_symlink() or len(bucket.name) != 2:
                continue
            for entry in bucket.iterdir():
                if not entry.is_dir() or entry.is_symlink():
                    continue
                record = self._record(entry, verify_files=True)
                if record is not None and record["identity"] == (variant, artifact_id):
                    matches.append(record)
        return max(matches, key=lambda item: item["modified"]) if matches else None

    def maintain(self, *, protected_keys: Sequence[str] = ()) -> dict[str, Any]:
        """Remove stale/invalid derived cache data and bound retained history."""

        protected = set(protected_keys)
        removed_entries = 0
        removed_files = 0
        removed_bytes = 0
        stale_temporaries = 0
        invalid_entries = 0
        records: list[dict[str, Any]] = []
        now = time.time()
        limits = {
            "keep_per_artifact": CACHE_KEEP_PER_ARTIFACT,
            "max_entries": CACHE_MAX_ENTRIES,
            "max_bytes": CACHE_MAX_BYTES,
        }
        if not self.root.is_dir() or self.root.is_symlink():
            return {
                "removed_entries": 0,
                "removed_files": 0,
                "removed_bytes": 0,
                "stale_temporaries": 0,
                "invalid_entries": 0,
                "retained_entries": 0,
                "retained_bytes": 0,
                "limits": limits,
            }

        buckets = [
            item
            for item in self.root.iterdir()
            if item.is_dir() and not item.is_symlink() and len(item.name) == 2
        ]
        for bucket in buckets:
            for entry in list(bucket.iterdir()):
                if not entry.is_dir() or entry.is_symlink():
                    continue
                if entry.name.startswith(".") and (".cache-" in entry.name or ".invalid-" in entry.name):
                    try:
                        old_enough = now - entry.stat().st_mtime >= CACHE_STALE_TEMP_SECONDS
                    except OSError:
                        old_enough = False
                    if old_enough:
                        files, size = self._tree_usage(entry)
                        if self._remove_cache_path(entry):
                            stale_temporaries += 1
                            removed_files += files
                            removed_bytes += size
                    continue
                record = self._record(entry)
                if record is None:
                    if entry.name in protected:
                        continue
                    files, size = self._tree_usage(entry)
                    if self._remove_cache_path(entry):
                        invalid_entries += 1
                        removed_entries += 1
                        removed_files += files
                        removed_bytes += size
                    continue
                records.append(record)

        to_remove: set[Path] = set()
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for record in records:
            groups.setdefault(record["identity"], []).append(record)
        for values in groups.values():
            values.sort(
                key=lambda item: (item["key"] in protected, item["modified"]),
                reverse=True,
            )
            retained_in_group = 0
            for item in values:
                if item["key"] in protected:
                    retained_in_group += 1
                    continue
                if retained_in_group < CACHE_KEEP_PER_ARTIFACT:
                    retained_in_group += 1
                else:
                    to_remove.add(item["path"])

        retained = [record for record in records if record["path"] not in to_remove]
        while (
            len(retained) > CACHE_MAX_ENTRIES
            or sum(int(item["bytes"]) for item in retained) > CACHE_MAX_BYTES
        ):
            candidates = [item for item in retained if item["key"] not in protected]
            if not candidates:
                break
            oldest = min(candidates, key=lambda item: item["modified"])
            to_remove.add(oldest["path"])
            retained.remove(oldest)

        for record in records:
            if record["path"] not in to_remove:
                continue
            if self._remove_cache_path(record["path"]):
                removed_entries += 1
                removed_files += int(record["files"])
                removed_bytes += int(record["bytes"])

        for bucket in buckets:
            try:
                if bucket.is_dir() and not any(bucket.iterdir()):
                    bucket.rmdir()
            except OSError:
                pass
        retained_records = [record for record in records if record["path"].exists()]
        return {
            "removed_entries": removed_entries,
            "removed_files": removed_files,
            "removed_bytes": removed_bytes,
            "stale_temporaries": stale_temporaries,
            "invalid_entries": invalid_entries,
            "retained_entries": len(retained_records),
            "retained_bytes": sum(int(item["bytes"]) for item in retained_records),
            "limits": limits,
        }

    def restore(self, key: str, destination_root: Path) -> tuple[Path, ...] | None:
        entry = self._entry(key)
        manifest_path = entry / "manifest.json"
        try:
            if entry.is_symlink():
                return None
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if manifest.get("schema") != CACHE_SCHEMA or manifest.get("key") != key:
            return None
        raw_outputs = manifest.get("outputs")
        if not isinstance(raw_outputs, list) or not raw_outputs:
            return None
        verified: list[tuple[Path, Path]] = []
        names: set[str] = set()
        try:
            for item in raw_outputs:
                if not isinstance(item, Mapping):
                    return None
                relative = _safe_relative(str(item["path"]), "cache.outputs.path")
                folded = relative.as_posix().casefold()
                if folded in names:
                    return None
                names.add(folded)
                cached = entry / "files" / Path(*relative.parts)
                if (
                    not cached.is_file()
                    or cached.is_symlink()
                    or cached.stat().st_size != int(item["size"])
                    or sha256_file(cached) != str(item["sha256"])
                ):
                    return None
                verified.append((cached, destination_root / Path(*relative.parts)))
        except (KeyError, TypeError, ValueError, OSError, ProjectConfigError):
            return None
        restored: list[Path] = []
        try:
            destination_root.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix=".srhd-cache-restore-",
                dir=destination_root.parent,
            ) as temp_name:
                temp = Path(temp_name)
                publications: list[tuple[Path, Path]] = []
                for cached, target in verified:
                    relative = target.relative_to(destination_root)
                    prepared = temp / relative
                    _copy_file_verified(cached, prepared)
                    publications.append((prepared, target))
                    restored.append(target)
                publish_files_transactionally(publications)
        except OSError:
            return None
        try:
            os.utime(entry, None)
        except OSError:
            pass
        return tuple(restored)

    def store(
        self,
        key: str,
        source_root: Path,
        outputs: Sequence[Path],
        fingerprint: Mapping[str, Any],
    ) -> None:
        entry = self._entry(key)
        if entry.is_dir() and not entry.is_symlink() and self._record(entry, verify_files=True) is not None:
            return
        if entry.exists() and not self._remove_cache_path(entry):
            # A cache problem must never block a valid build. A later run can
            # retry maintenance after antivirus/indexers release the path.
            return
        entry.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f".{key}.cache-", dir=entry.parent) as temp_name:
            temp = Path(temp_name)
            rows: list[dict[str, Any]] = []
            for output in outputs:
                relative = output.resolve().relative_to(source_root.resolve())
                target = temp / "files" / relative
                _copy_file_verified(output, target)
                rows.append(
                    {
                        "path": relative.as_posix(),
                        "size": target.stat().st_size,
                        "sha256": sha256_file(target),
                    }
                )
            manifest = {
                "schema": CACHE_SCHEMA,
                "key": key,
                "fingerprint": fingerprint,
                "outputs": rows,
            }
            (temp / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            try:
                os.replace(temp, entry)
            except OSError:
                # Another identical build won the race. Its entry will be
                # hash-verified on the next restore; never merge cache trees.
                if not entry.is_dir():
                    raise


def _artifact_outputs(full_mod: Path, artifact: Mapping[str, Any]) -> list[Path]:
    kind = str(artifact["kind"]).casefold()
    output_rel = _safe_relative(str(artifact["output"]), "artifact.output")
    output = full_mod / Path(*output_rel.parts)
    if kind == "copy" and output.is_dir():
        return iter_files(output)
    result = [output]
    for field in ("lang_dat", "lang_txt", "lang_fragment"):
        if artifact.get(field):
            relative = _safe_relative(str(artifact[field]), f"artifact.{field}")
            result.append(full_mod / Path(*relative.parts))
    return [path for path in result if path.is_file()]


def _build_artifact(
    project: ModProject,
    variant: str,
    artifact: Mapping[str, Any],
    full_mod: Path,
    generated: Path,
    toolchain: Toolchain,
    cache: ArtifactCache,
    *,
    use_cache: bool,
) -> dict[str, Any]:
    identifier = str(artifact["id"])
    kind = str(artifact["kind"]).casefold()
    source = _effective_artifact_source(project, artifact, full_mod)
    output_rel = _safe_relative(str(artifact["output"]), f"artifact {identifier}.output")
    output = full_mod / Path(*output_rel.parts)
    inputs = _artifact_inputs(project, artifact, source, full_mod)
    tools = _tool_fingerprints(toolchain, _artifact_tool_names(artifact))
    key, fingerprint = _artifact_cache_key(project, variant, artifact, inputs, tools, full_mod)
    restored = cache.restore(key, full_mod) if use_cache else None
    if restored is not None:
        for restored_output in restored:
            if restored_output.suffix.casefold() == ".scr":
                scr_info = inspect_scr(restored_output)
                if not scr_info["supported_version"]:
                    raise RuntimeError(
                        f"Кэшированный SCR {restored_output} имеет неподдерживаемую "
                        f"версию {scr_info['version']}"
                    )
        return {
            "id": identifier,
            "kind": kind,
            "cache": "hit",
            "cache_key": key,
            "inputs": fingerprint["inputs"],
            "tools": tools,
            "outputs": [str(path.relative_to(full_mod).as_posix()) for path in restored],
            "verified": True,
        }

    result: dict[str, Any]
    if kind == "dat":
        output.parent.mkdir(parents=True, exist_ok=True)
        result = toolchain.convert_dat(source, output, overwrite=True, verify=True)
    elif kind == "rson":
        fragment_value = artifact.get("lang_fragment")
        fragment = (
            full_mod / Path(*_safe_relative(str(fragment_value), "artifact.lang_fragment").parts)
            if fragment_value
            else generated / f"{identifier}.lang.txt"
        )
        lang_dat_value = artifact.get("lang_dat")
        lang_dat = (
            full_mod / Path(*_safe_relative(str(lang_dat_value), "artifact.lang_dat").parts)
            if lang_dat_value
            else None
        )
        lang_base_value = artifact.get("lang_base")
        lang_base = (
            _project_path(project.root, str(lang_base_value), "artifact.lang_base", allow_absolute=False)
            if lang_base_value
            else None
        )
        result = toolchain.compile_rson(
            source,
            output,
            fragment,
            lang_dat_output=lang_dat,
            lang_base=lang_base,
            overwrite=True,
            timeout=float(artifact["timeout"]) if artifact.get("timeout") is not None else None,
            check_custom_factions=bool(artifact.get("check_custom_factions", True)),
        )
    elif kind == "rsm":
        lang_txt_value = artifact.get("lang_txt")
        lang_dat_value = artifact.get("lang_dat")
        lang_base_value = artifact.get("lang_base")
        lang_txt = (
            full_mod / Path(*_safe_relative(str(lang_txt_value), "artifact.lang_txt").parts)
            if lang_txt_value
            else None
        )
        lang_dat = (
            full_mod / Path(*_safe_relative(str(lang_dat_value), "artifact.lang_dat").parts)
            if lang_dat_value
            else None
        )
        lang_base = (
            _project_path(project.root, str(lang_base_value), "artifact.lang_base", allow_absolute=False)
            if lang_base_value
            else None
        )
        result = toolchain.build_rsm(
            source,
            output,
            lang_txt_output=lang_txt,
            lang_dat_output=lang_dat,
            lang_base=lang_base,
            overwrite=True,
            timeout=float(artifact["timeout"]) if artifact.get("timeout") is not None else None,
            deep_roundtrip=bool(artifact.get("deep_roundtrip", False)),
        )
    elif kind == "copy":
        if source.is_dir():
            copied = _copy_overlay(source, output)
        else:
            _copy_file_verified(source, output)
            copied = [output]
        result = {"verified": True, "copied": [str(path) for path in copied]}
    else:
        raise ProjectConfigError(f"Неизвестный kind {kind!r}")

    # A directory copy owns only the files copied from its source.  The output
    # directory may already contain baseline mod files; caching those would
    # resurrect an old baseline on the next otherwise valid cache hit.
    outputs = copied if kind == "copy" else _artifact_outputs(full_mod, artifact)
    if not outputs:
        raise RuntimeError(f"Артефакт {identifier!r} не создал ни одного результата")
    if use_cache:
        cache.store(key, full_mod, outputs, fingerprint)
    return {
        "id": identifier,
        "kind": kind,
        "cache": "miss" if use_cache else "disabled",
        "cache_key": key,
        "inputs": fingerprint["inputs"],
        "tools": tools,
        "outputs": [path.relative_to(full_mod).as_posix() for path in outputs],
        "verified": bool(result.get("verified", result.get("status") == "passed")),
        "result": result,
    }


def _selected_artifacts(
    project: ModProject,
    variant: str,
    variables: Mapping[str, str],
) -> tuple[dict[str, Any], ...]:
    selected: list[dict[str, Any]] = []
    output_owners: dict[str, str] = {}
    for raw in project.artifacts:
        variants = raw.get("variants")
        if isinstance(variants, list) and variant not in {str(item) for item in variants} and "*" not in variants:
            continue
        artifact = _expand(raw, variables, f"artifact {raw.get('id', '')}")
        identifier = str(artifact.get("id", ""))
        for field in ("output", "lang_dat", "lang_txt", "lang_fragment"):
            value = artifact.get(field)
            if not value:
                continue
            relative = _safe_relative(str(value), f"artifact {identifier}.{field}")
            key = relative.as_posix().casefold()
            previous = output_owners.get(key)
            if previous is not None:
                raise ProjectConfigError(
                    f"Артефакты {previous!r} и {identifier!r} пишут в один путь {relative}"
                )
            output_owners[key] = identifier
        selected.append(artifact)
    return tuple(selected)


def _validate_effective_artifact_outputs(
    project: ModProject,
    artifacts: Sequence[Mapping[str, Any]],
    full_mod: Path,
) -> None:
    """Reject exact, case-only and file/directory output overlaps."""

    claimed: list[tuple[PurePosixPath, str]] = []
    for artifact in artifacts:
        identifier = str(artifact.get("id", ""))
        output = _safe_relative(str(artifact["output"]), f"artifact {identifier}.output")
        source = _effective_artifact_source(project, artifact, full_mod)
        paths: list[PurePosixPath]
        if str(artifact.get("kind", "")).casefold() == "copy" and source.is_dir():
            paths = [
                output / PurePosixPath(path.relative_to(source).as_posix())
                for path in iter_files(source)
            ]
        else:
            paths = [output]
        for field in ("lang_dat", "lang_txt", "lang_fragment"):
            if artifact.get(field):
                paths.append(_safe_relative(str(artifact[field]), f"artifact {identifier}.{field}"))
        for path in paths:
            folded_parts = tuple(part.casefold() for part in path.parts)
            for previous, owner in claimed:
                previous_parts = tuple(part.casefold() for part in previous.parts)
                common = min(len(folded_parts), len(previous_parts))
                if folded_parts[:common] == previous_parts[:common] and (
                    len(folded_parts) == common or len(previous_parts) == common
                ):
                    raise ProjectConfigError(
                        f"Артефакты {owner!r} и {identifier!r} имеют пересекающиеся "
                        f"выходы {previous} и {path}"
                    )
            claimed.append((path, identifier))


def _stage_artifact_audit_source(
    project: ModProject,
    artifact: Mapping[str, Any],
    full_mod: Path,
) -> list[str]:
    """Expose compiler inputs to the ordinary release audit, then strip them.

    This deliberately happens for cache hits too.  A cached container never
    turns the final audit into a binary-only claim when its checked source is
    available in the project.
    """

    kind = str(artifact["kind"]).casefold()
    if kind not in {"dat", "rson", "rsm"}:
        return []
    identifier = str(artifact["id"])
    source = _effective_artifact_source(project, artifact, full_mod)
    targets: list[tuple[Path, Path]] = []
    if kind == "dat" and source.is_file():
        output = _safe_relative(str(artifact["output"]), f"artifact {identifier}.output")
        if output.parts and output.parts[0].casefold() == "cfg":
            relative = Path(*output.parts[1:]).with_suffix(".txt")
            targets.append((source, full_mod / "SOURCE" / "CFG" / relative))
    elif kind == "rson" and source.is_file():
        targets.append(
            (
                source,
                full_mod / "SOURCE" / "ProjectBuild" / identifier / source.name,
            )
        )
    elif kind == "rsm":
        rsm_root = source.parent if source.is_file() else source
        rsm_files = (
            [module.path for module in inspect_rsm_project(source).modules]
            if source.is_file()
            else [item for item in iter_files(rsm_root) if item.suffix.casefold() == ".rsm"]
        )
        for item in sorted(rsm_files, key=lambda value: str(value).casefold()):
            try:
                relative_item = item.relative_to(rsm_root)
            except ValueError:
                relative_item = Path("_external") / f"{sha256_file(item)[:12]}-{item.name}"
            targets.append(
                (
                    item,
                    full_mod
                    / "SOURCE"
                    / "ProjectBuild"
                    / identifier
                    / relative_item,
                )
            )

    staged: list[str] = []
    for source_path, target in targets:
        if target.is_file():
            if sha256_file(target) != sha256_file(source_path):
                raise ProjectConfigError(
                    f"Audit-source артефакта {identifier!r} конфликтует с {target}"
                )
        else:
            _copy_file_verified(source_path, target)
        staged.append(target.relative_to(full_mod).as_posix())
    return staged


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _audit_allow(
    project: ModProject,
    variant: Mapping[str, Any],
    explicit: Sequence[str],
) -> tuple[str, ...]:
    values: list[str] = []
    for source, field in (
        (project.raw.get("allow", []), "allow"),
        (variant.get("allow", []), "variant.allow"),
        (explicit, "explicit allow"),
    ):
        if not isinstance(source, (list, tuple)) or not all(isinstance(item, str) for item in source):
            raise ProjectConfigError(f"{field} должен быть массивом строк")
        values.extend(item for item in source if item)
    return tuple(dict.fromkeys(values))


def _project_tools_report(toolchain: Toolchain) -> list[dict[str, Any]]:
    return [item for item in toolchain.status() if item.get("available")]


def build_project(
    path: str | Path = ".",
    *,
    variant: str | None = None,
    use_cache: bool = True,
    tools_root: str | Path | None = None,
    warnings_as_errors: bool = False,
    allow: Sequence[str] = (),
    toolchain: Toolchain | None = None,
) -> ProjectBuildResult:
    project = load_project(path)
    external_issues = _external_build_issues(project)
    external_errors = [item for item in external_issues if item["severity"] == "error"]
    if external_errors:
        raise ProjectConfigError(external_errors[0]["message"])
    variant_name, variant_config, variables = _resolve_variant(project, variant)
    prefix = str(_expand(variant_config.get("prefix", project.prefix), variables, "variant.prefix"))
    _safe_relative(prefix, "prefix")
    strip_sources = bool(variant_config.get("strip_sources", project.raw.get("strip_sources", True)))
    audit_allow = _audit_allow(project, variant_config, allow)
    selected_tools_root = Path(tools_root).resolve() if tools_root is not None else project.tools_root
    chain = toolchain or Toolchain(selected_tools_root)
    cache = ArtifactCache(project.cache_root)
    project.build_root.mkdir(parents=True, exist_ok=True)
    artifacts = _selected_artifacts(project, variant_name, variables)

    with tempfile.TemporaryDirectory(prefix=".srhd-project-build-", dir=project.build_root.parent) as temp_name:
        workspace = Path(temp_name)
        full_mod = workspace / "full" / project.mod_root.name
        stage_tree(project.mod_root, full_mod)
        variant_files = _apply_variant_files(project, full_mod, variant_config, variables)
        _validate_effective_artifact_outputs(project, artifacts, full_mod)
        generated = workspace / ".srhd-generated"
        generated.mkdir()
        artifact_results: list[dict[str, Any]] = []
        for artifact in artifacts:
            artifact_result = _build_artifact(
                project,
                variant_name,
                artifact,
                full_mod,
                generated,
                chain,
                cache,
                use_cache=use_cache,
            )
            artifact_result["audit_sources"] = _stage_artifact_audit_source(
                project,
                artifact,
                full_mod,
            )
            artifact_results.append(artifact_result)

        cache_maintenance = (
            cache.maintain(
                protected_keys=[
                    str(item["cache_key"])
                    for item in artifact_results
                    if item.get("cache") != "disabled"
                ]
            )
            if use_cache
            else {
                "removed_entries": 0,
                "removed_files": 0,
                "removed_bytes": 0,
                "stale_temporaries": 0,
                "invalid_entries": 0,
                "retained_entries": 0,
                "retained_bytes": 0,
                "disabled": True,
            }
        )

        build_destination_root = project.build_root / variant_name
        deployment = deploy_mod(
            full_mod,
            build_destination_root,
            prefix=prefix,
            strip_sources=strip_sources,
            tools_root=selected_tools_root,
            warnings_as_errors=warnings_as_errors,
            allow=audit_allow,
            overwrite=True,
        )

    cache_hits = sum(item["cache"] == "hit" for item in artifact_results)
    cache_misses = sum(item["cache"] == "miss" for item in artifact_results)
    provenance = {
        "schema": PROJECT_BUILD_SCHEMA,
        "project": project.as_dict(),
        "variant": variant_name,
        "variables": variables,
        "prefix": prefix,
        "output": str(deployment.destination),
        "strip_sources": strip_sources,
        "allow": list(audit_allow),
        "variant_files": variant_files,
        "artifacts": artifact_results,
        "cache": {
            "enabled": use_cache,
            "root": str(project.cache_root),
            "hits": cache_hits,
            "misses": cache_misses,
            "maintenance": cache_maintenance,
        },
        "tools": _project_tools_report(chain),
        "external_builds": _external_build_report(project),
        "source_manifest": build_manifest(project.mod_root),
        "output_manifest": build_manifest(deployment.destination),
        "audit": deployment.report.as_dict(),
    }
    provenance_path = project.build_root / variant_name / f"{project.name}.build.json"
    _write_json_atomic(provenance_path, provenance)
    return ProjectBuildResult(
        project=project,
        variant=variant_name,
        output=deployment.destination,
        prefix=prefix,
        audit_allow=audit_allow,
        artifacts=tuple(artifact_results),
        cache_hits=cache_hits,
        cache_misses=cache_misses,
        deploy=deployment,
        provenance_path=provenance_path,
        provenance=provenance,
    )


def resolve_project_target(
    project: ModProject,
    name: str | None,
    *,
    variant: str | None = None,
) -> ProjectTarget:
    variant_name, _variant_config, variables = _resolve_variant(project, variant)
    selected = name or str(project.raw.get("default_target", ""))
    if not selected:
        if len(project.targets) == 1:
            selected = next(iter(project.targets))
        else:
            raise ProjectConfigError("Укажите --target либо default_target в проекте")
    raw = project.targets.get(selected)
    if not isinstance(raw, Mapping):
        raise ProjectConfigError(f"Неизвестная цель {selected!r}")
    root_value = raw.get("root")
    if not isinstance(root_value, str) or not root_value:
        raise ProjectConfigError(
            f"targets.{selected}.root отсутствует; задайте локальный путь в {LOCAL_PROJECT_FILE_NAME}"
        )
    root_text = str(_expand(root_value, variables, f"targets.{selected}.root"))
    root = _project_path(project.root, root_text, f"targets.{selected}.root")
    prefix = str(_expand(raw.get("prefix", project.prefix), variables, f"targets.{selected}.prefix"))
    safe_prefix = _safe_relative(prefix, f"targets.{selected}.prefix")
    destination = root.joinpath(*safe_prefix.parts).resolve()
    if (
        destination == project.mod_root
        or destination in project.mod_root.parents
        or project.mod_root in destination.parents
    ):
        raise ProjectConfigError(
            f"Цель {selected!r} пересекается с исходным mod_root: {destination}"
        )
    del variant_name
    return ProjectTarget(selected, root, prefix)


def deploy_project(
    path: str | Path = ".",
    *,
    variant: str | None = None,
    target: str | None = None,
    dry_run: bool = False,
    use_cache: bool = True,
    tools_root: str | Path | None = None,
    warnings_as_errors: bool = False,
    allow: Sequence[str] = (),
) -> dict[str, Any]:
    build = build_project(
        path,
        variant=variant,
        use_cache=use_cache,
        tools_root=tools_root,
        warnings_as_errors=warnings_as_errors,
        allow=allow,
    )
    selected_target = resolve_project_target(build.project, target, variant=build.variant)
    plan = plan_deploy(
        build.output,
        selected_target.root,
        prefix=selected_target.prefix,
        strip_sources=False,
        tools_root=tools_root or build.project.tools_root,
        warnings_as_errors=warnings_as_errors,
        allow=build.audit_allow,
    )
    deployment: DeployResult | None = None
    if not dry_run:
        deployment = deploy_mod(
            build.output,
            selected_target.root,
            prefix=selected_target.prefix,
            strip_sources=False,
            tools_root=tools_root or build.project.tools_root,
            warnings_as_errors=warnings_as_errors,
            allow=build.audit_allow,
            overwrite=True,
        )
    return {
        "schema": PROJECT_DEPLOY_SCHEMA,
        "project": str(build.project.path),
        "variant": build.variant,
        "target": selected_target.as_dict(),
        "dry_run": dry_run,
        "operation_semantics": {
            "game_target_modified": not dry_run,
            "build_performed": True,
            "service_outputs_may_change": [
                str(build.project.build_root),
                str(build.project.cache_root),
            ],
            "passive_preview_command": "project plan",
        },
        "build": build.as_dict(),
        "plan": plan.as_dict(),
        "deploy": deployment.as_dict() if deployment is not None else None,
    }


def _publish_output(
    project: ModProject,
    variant: str,
    variables: Mapping[str, str],
    explicit: str | Path | None,
) -> Path:
    if explicit is not None:
        return Path(explicit).resolve()
    publish = project.raw.get("publish", {})
    raw_output = publish.get("output") if isinstance(publish, Mapping) else None
    if raw_output:
        text = str(_expand(raw_output, variables, "publish.output"))
        return _project_path(project.root, text, "publish.output")
    return (project.root / "Releases" / f"{project.name}-{variant}.zip").resolve()


def publish_project(
    path: str | Path = ".",
    *,
    variant: str | None = None,
    output: str | Path | None = None,
    targets: Sequence[str] | None = None,
    use_cache: bool = True,
    tools_root: str | Path | None = None,
    warnings_as_errors: bool = False,
    allow: Sequence[str] = (),
) -> dict[str, Any]:
    build = build_project(
        path,
        variant=variant,
        use_cache=use_cache,
        tools_root=tools_root,
        warnings_as_errors=warnings_as_errors,
        allow=allow,
    )
    _variant_name, _variant_config, variables = _resolve_variant(build.project, build.variant)
    archive = _publish_output(build.project, build.variant, variables, output)
    release = build_release(
        build.output,
        archive,
        prefix=build.prefix,
        tools_root=tools_root or build.project.tools_root,
        warnings_as_errors=warnings_as_errors,
        allow=build.audit_allow,
        overwrite=True,
    )
    if targets is None:
        publish = build.project.raw.get("publish", {})
        configured = publish.get("targets", []) if isinstance(publish, Mapping) else []
        target_names = [str(item) for item in configured] if isinstance(configured, list) else []
    else:
        target_names = [str(item) for item in targets]
    deployments: list[dict[str, Any]] = []
    for target_name in target_names:
        selected = resolve_project_target(build.project, target_name, variant=build.variant)
        deployed = deploy_mod(
            build.output,
            selected.root,
            prefix=selected.prefix,
            strip_sources=False,
            tools_root=tools_root or build.project.tools_root,
            warnings_as_errors=warnings_as_errors,
            allow=build.audit_allow,
            overwrite=True,
        )
        deployments.append({"target": selected.as_dict(), "result": deployed.as_dict()})
    report = {
        "schema": PROJECT_PUBLISH_SCHEMA,
        "project": str(build.project.path),
        "variant": build.variant,
        "build": build.as_dict(),
        "release": release.as_dict(),
        "deployments": deployments,
        "provenance": {
            "build": str(build.provenance_path),
            "archive_sha256": release.sha256,
            "manifest": str(release.manifest_path),
            "audit": str(release.audit_path),
        },
    }
    report_path = archive.with_suffix(".build.json")
    _write_json_atomic(report_path, report)
    report["report"] = str(report_path)
    return report
