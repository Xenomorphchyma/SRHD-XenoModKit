from __future__ import annotations

import fnmatch
import glob
import hashlib
import json
import os
import shutil
import string
import tempfile
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
from .toolchain import Toolchain


PROJECT_SCHEMA = "srhd-modkit-project-v1"
PROJECT_BUILD_SCHEMA = "srhd-modkit-project-build-v1"
PROJECT_DEPLOY_SCHEMA = "srhd-modkit-project-deploy-v1"
PROJECT_PUBLISH_SCHEMA = "srhd-modkit-project-publish-v1"
CACHE_SCHEMA = "srhd-modkit-build-cache-v1"

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


class ProjectConfigError(ValueError):
    pass


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
            "cache": {"hits": self.cache_hits, "misses": self.cache_misses},
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


def _project_path(root: Path, value: str | Path, field: str, *, allow_absolute: bool = True) -> Path:
    raw = Path(str(value))
    candidate = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
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
        or (path.parts and ":" in path.parts[0])
    ):
        raise ProjectConfigError(f"{field} должен быть безопасным относительным путём: {value!r}")
    return path


def _validate_project_config(path: Path, raw: Mapping[str, Any]) -> None:
    schema = raw.get("schema")
    if schema != PROJECT_SCHEMA:
        raise ProjectConfigError(f"{path}: ожидалась schema = {PROJECT_SCHEMA!r}")
    for key in ("name", "mod_root", "prefix"):
        if not isinstance(raw.get(key), str) or not str(raw[key]).strip():
            raise ProjectConfigError(f"{path}: обязательное строковое поле {key!r}")
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
    mod_root = _project_path(root, str(raw["mod_root"]), "mod_root", allow_absolute=False)
    if not mod_root.is_dir():
        raise NotADirectoryError(mod_root)
    build_root = _project_path(root, str(raw.get("build_root", ".srhd-build")), "build_root")
    cache_root = _project_path(root, str(raw.get("cache_root", ".srhd-cache")), "cache_root")
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
        overlay = project.root.joinpath(*overlay_rel.parts).resolve()
        if project.root not in overlay.parents:
            raise ProjectConfigError(f"overlay вышел за проект: {overlay}")
        for target in _copy_overlay(overlay, full_mod):
            overlaid.append(target.relative_to(full_mod).as_posix())

    for raw_pattern in _expand(list(variant.get("include", [])), variables, "variant.include"):
        pattern = str(_safe_relative(str(raw_pattern), "variant.include"))
        matches = sorted(glob.glob(str(project.root / pattern), recursive=True), key=str.casefold)
        for raw_match in matches:
            source = Path(raw_match)
            if not source.is_file() or source.is_symlink():
                continue
            relative = source.resolve().relative_to(project.root)
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
        inputs.extend(path for path in source.parent.rglob("*.rsm") if path.is_file())
    for field in ("lang_base",):
        value = artifact.get(field)
        if value:
            inputs.append(_project_path(project.root, str(value), field, allow_absolute=False))
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
        candidate = _project_path(project.root, str(value), "artifact.inputs", allow_absolute=False)
        if candidate.is_dir():
            inputs.extend(iter_files(candidate))
        elif candidate.is_file():
            inputs.append(candidate)
        else:
            raise FileNotFoundError(candidate)
    unique = {str(path.resolve()).casefold(): path.resolve() for path in inputs}
    return sorted(unique.values(), key=lambda item: str(item).casefold())


def _tool_fingerprints(toolchain: Toolchain, names: Iterable[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for name in sorted(set(names)):
        tool = toolchain.tools[name]
        if not tool.path.is_file():
            raise FileNotFoundError(f"Инструмент не найден: {tool.path}")
        result.append(
            {
                "name": name,
                "path": str(tool.path),
                "version": tool.version,
                "sha256": sha256_file(tool.path),
            }
        )
    return result


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
    engine_files = (
        package_root / "project.py",
        package_root / "toolchain.py",
        package_root / "scripts.py",
        package_root / "rsm.py",
        package_root / "blockpar.py",
    )
    payload = {
        "schema": CACHE_SCHEMA,
        "variant": variant,
        "artifact": artifact,
        "inputs": input_rows,
        "tools": list(tools),
        "engine": [
            {"path": item.name, "sha256": sha256_file(item)}
            for item in engine_files
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), payload


class ArtifactCache:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve() / "artifacts"

    def _entry(self, key: str) -> Path:
        return self.root / key[:2] / key

    def restore(self, key: str, destination_root: Path) -> tuple[Path, ...] | None:
        entry = self._entry(key)
        manifest_path = entry / "manifest.json"
        try:
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
        for cached, target in verified:
            try:
                _copy_file_verified(cached, target)
            except OSError:
                return None
            restored.append(target)
        return tuple(restored)

    def store(
        self,
        key: str,
        source_root: Path,
        outputs: Sequence[Path],
        fingerprint: Mapping[str, Any],
    ) -> None:
        entry = self._entry(key)
        if entry.is_dir() and (entry / "manifest.json").is_file():
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
    source = _project_path(project.root, str(artifact["source"]), f"artifact {identifier}.source", allow_absolute=False)
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

    outputs = _artifact_outputs(full_mod, artifact)
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
    source = _project_path(
        project.root,
        str(artifact["source"]),
        f"artifact {identifier}.source",
        allow_absolute=False,
    )
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
        for item in sorted(rsm_root.rglob("*.rsm"), key=lambda value: str(value).casefold()):
            targets.append(
                (
                    item,
                    full_mod
                    / "SOURCE"
                    / "ProjectBuild"
                    / identifier
                    / item.relative_to(rsm_root),
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
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


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
        "cache": {"enabled": use_cache, "root": str(project.cache_root), "hits": cache_hits, "misses": cache_misses},
        "tools": _project_tools_report(chain),
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
