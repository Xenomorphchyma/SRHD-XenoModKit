from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .blockpar import load_blockpar
from .files import iter_files, stage_tree
from .module_info import find_module_info, parse_module_info
from .native_loader import validate_native_mod
from .project import (
    ArtifactCache,
    ModProject,
    ProjectConfigError,
    _apply_variant_files,
    _artifact_cache_key,
    _artifact_inputs,
    _artifact_tool_names,
    _expand,
    _effective_artifact_source,
    _external_build_issues,
    _external_build_report,
    _is_link_or_junction,
    _project_path,
    _resolve_variant,
    _safe_relative,
    _selected_artifacts,
    _validate_effective_artifact_outputs,
    _tool_fingerprints,
    load_project,
    resolve_project_target,
)
from .release import _distribution_excludes
from .rsm import inspect_rsm_project
from .scripts import load_rson
from .toolchain import Toolchain


PROJECT_INIT_SCHEMA = "srhd-modkit-project-init-v1"
PROJECT_PLAN_SCHEMA = "srhd-modkit-project-plan-v1"
PROJECT_DOCTOR_SCHEMA = "srhd-modkit-project-doctor-v1"
PROJECT_CLEAN_SCHEMA = "srhd-modkit-project-clean-v1"

_WORKSPACE_PREFIXES = (
    ".srhd-project-build-",
    ".srhd-release-",
    ".srhd-script-",
    ".srhd-script-build-",
    ".srhd-script-output-",
    ".srhd-scr-roundtrip-",
    ".srhd-decompile-",
    ".srhd-dat-",
    ".srhd-convert-",
    ".srhd-rsm-",
    ".srhd-gai-",
    ".srhd-pkg-",
    ".srhd-manifest-",
    ".srhd-project-plan-",
)
_STALE_SECONDS = 24 * 60 * 60


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _identifier(value: str, fallback: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-").casefold()
    return normalized or fallback


def _default_prefix(mod_root: Path) -> str:
    if mod_root.parent.name.casefold() == "othermods":
        return f"OtherMods/{mod_root.name}"
    return mod_root.name


def _source_cfg_target(mod_root: Path, source: Path) -> Path | None:
    parts = source.relative_to(mod_root).parts
    folded = [part.casefold() for part in parts]
    try:
        source_index = next(index for index, part in enumerate(folded) if part in {"source", "sources"})
        cfg_index = next(
            index
            for index in range(source_index + 1, len(parts))
            if folded[index] in {"cfg", "config"}
        )
    except StopIteration:
        return None
    tail = Path(*parts[cfg_index + 1 :])
    return Path("CFG") / tail.with_suffix(".dat")


def _matching_binary(mod_root: Path, directory: str, stem: str, suffix: str) -> Path:
    expected = mod_root / directory / f"{stem}{suffix}"
    if expected.is_file():
        return expected.relative_to(mod_root)
    matches = [
        path.relative_to(mod_root)
        for path in iter_files(mod_root)
        if path.name.casefold() == f"{stem}{suffix}".casefold()
    ]
    return matches[0] if len(matches) == 1 else Path(directory) / f"{stem}{suffix}"


def _render_project_toml(
    *,
    name: str,
    mod_root: str,
    prefix: str,
    artifacts: Sequence[Mapping[str, Any]],
    external_builds: Sequence[Mapping[str, Any]] = (),
) -> str:
    lines = [
        'schema = "srhd-modkit-project-v1"',
        f"name = {_toml_string(name)}",
        f"mod_root = {_toml_string(mod_root)}",
        f"prefix = {_toml_string(prefix)}",
        'default_variant = "release"',
        'build_root = ".srhd-build"',
        'cache_root = ".srhd-cache"',
        "",
        "[variants.release]",
        "",
    ]
    ordered_fields = (
        "id",
        "kind",
        "source",
        "output",
        "lang_fragment",
        "lang_txt",
        "lang_dat",
        "lang_base",
    )
    for artifact in artifacts:
        lines.append("[[artifacts]]")
        for field in ordered_fields:
            value = artifact.get(field)
            if value:
                lines.append(f"{field} = {_toml_string(str(value))}")
        lines.append("")
    for external in external_builds:
        lines.append("[[external_builds]]")
        for field in ("id", "kind", "project", "mode"):
            value = external.get(field)
            if value:
                lines.append(f"{field} = {_toml_string(str(value))}")
        outputs = [str(value) for value in external.get("outputs", [])]
        rendered_outputs = ", ".join(_toml_string(value) for value in outputs)
        lines.append(f"outputs = [{rendered_outputs}]")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _make_artifact_ids_unique(artifacts: list[dict[str, Any]]) -> None:
    used: set[str] = set()
    for artifact in artifacts:
        base = str(artifact["id"])
        candidate = base
        number = 2
        while candidate.casefold() in used:
            candidate = f"{base}-{number}"
            number += 1
        artifact["id"] = candidate
        used.add(candidate.casefold())


def _solution_project_paths(solution: Path) -> set[Path]:
    """Return C#/C++ project files referenced by an MSBuild solution."""

    try:
        text = solution.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return set()
    result: set[Path] = set()
    for raw in re.findall(r'Project\([^\r\n]*?=\s*"[^"]*",\s*"([^"]+\.(?:csproj|vcxproj))"', text, re.I):
        candidate = (solution.parent / Path(raw.replace("\\", "/"))).resolve()
        result.add(candidate)
    return result


def initialize_project(
    mod: str | Path,
    *,
    output: str | Path | None = None,
    name: str | None = None,
    prefix: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Create a conservative project draft around an existing mod tree."""

    mod_root = Path(mod).resolve()
    if not mod_root.is_dir():
        raise NotADirectoryError(mod_root)
    info_path = find_module_info(mod_root)
    if info_path is None:
        raise FileNotFoundError(f"ModuleInfo.txt не найден в {mod_root}")
    module = parse_module_info(info_path)
    destination = Path(output).resolve() if output is not None else mod_root.parent / "srhd-modkit.toml"
    if destination.exists() and not overwrite:
        raise FileExistsError(f"Конфигурация уже существует: {destination}")
    try:
        relative_mod = mod_root.relative_to(destination.parent).as_posix()
    except ValueError as exc:
        raise ProjectConfigError(
            "srhd-modkit.toml должен находиться в каталоге-предке существующего мода"
        ) from exc

    issues: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    claimed_outputs: set[str] = set()
    ambiguous_outputs: set[str] = set()
    files = iter_files(mod_root)

    primary_projects = [
        path
        for path in files
        if path.suffix.casefold() in {".csproj", ".vcxproj"}
        or path.name.casefold() == "cmakelists.txt"
        or (
            path.name.casefold() == "build.ps1"
            and "native" in {part.casefold() for part in path.relative_to(mod_root).parts}
            and path.relative_to(mod_root).parts[0].casefold() in {"source", "sources"}
        )
    ]
    runtime_binaries = [
        path
        for path in files
        if path.suffix.casefold() in {".dll", ".exe"}
        and path.relative_to(mod_root).parts[0].casefold() not in {"source", "sources"}
    ]
    known_primary = {path.resolve() for path in primary_projects}
    primary_runtime_stems = {
        path.stem.casefold()
        for path in primary_projects
        if path.name.casefold() != "cmakelists.txt"
    }
    solutions: list[Path] = []
    for solution in (path for path in files if path.suffix.casefold() == ".sln"):
        references = _solution_project_paths(solution)
        has_uncovered_project = bool(references - known_primary)
        has_unique_runtime = any(
            binary.stem.casefold() == solution.stem.casefold()
            and binary.stem.casefold() not in primary_runtime_stems
            for binary in runtime_binaries
        )
        # A solution that merely wraps projects already represented below is
        # intentionally deduplicated.  Independent/unresolved solution graphs
        # and a solution-owned runtime remain explicit build requirements.
        if not primary_projects or has_uncovered_project or has_unique_runtime or not references:
            solutions.append(solution)
    project_candidates = [*primary_projects, *solutions]
    external_builds: list[dict[str, Any]] = []
    claimed_runtime: set[str] = set()
    for external_project in project_candidates:
        kind = (
            "dotnet"
            if external_project.suffix.casefold() == ".csproj"
            else "msbuild-cpp"
            if external_project.suffix.casefold() == ".vcxproj"
            else "cmake"
            if external_project.name.casefold() == "cmakelists.txt"
            else "xeno-native-plugin"
            if external_project.name.casefold() == "build.ps1"
            else "msbuild-solution"
        )
        if kind == "xeno-native-plugin":
            matching_outputs = [
                binary
                for binary in runtime_binaries
                if binary.name.casefold().endswith(".xenoplugin.dll")
                and "native" in {
                    part.casefold() for part in binary.relative_to(mod_root).parts[:-1]
                }
            ]
        else:
            matching_outputs = [
                binary
                for binary in runtime_binaries
                if binary.stem.casefold() == external_project.stem.casefold()
                or (
                    binary.name.casefold().endswith(".xenoplugin.dll")
                    and binary.name[: -len(".XenoPlugin.dll")].casefold()
                    == external_project.stem.casefold()
                )
            ]
        for binary in matching_outputs:
            claimed_runtime.add(str(binary).casefold())
        external_builds.append(
            {
                "id": _identifier(external_project.stem, f"external-{len(external_builds) + 1}"),
                "kind": kind,
                "project": external_project.relative_to(destination.parent).as_posix(),
                "mode": "unconfigured",
                "outputs": [
                    binary.relative_to(destination.parent).as_posix()
                    for binary in matching_outputs
                ],
            }
        )
        issues.append(
            {
                "severity": "warning",
                "code": "project-init-external-build-unconfigured",
                "message": (
                    f"Обнаружен внешний проект {external_project.name}. ModKit не запускает "
                    "непроверенные C++/C# build-команды автоматически; подтвердите outputs "
                    "и переключите mode на prebuilt после отдельной сборки"
                ),
                "path": str(external_project),
            }
        )
    for binary in runtime_binaries:
        if str(binary).casefold() in claimed_runtime:
            continue
        relative_binary = binary.relative_to(destination.parent).as_posix()
        native_plugin = binary.name.casefold().endswith(".xenoplugin.dll")
        external_builds.append(
            {
                "id": _identifier(binary.stem, f"runtime-{len(external_builds) + 1}"),
                "kind": "xeno-native-plugin" if native_plugin else "prebuilt-binary",
                "project": relative_binary,
                "mode": "unconfigured",
                "outputs": [relative_binary],
            }
        )
        issues.append(
            {
                "severity": "warning",
                "code": (
                    "project-init-native-plugin-build-unconfirmed"
                    if native_plugin
                    else "project-init-runtime-binary-unconfirmed"
                ),
                "message": (
                    f"Поставляемый runtime-бинарник {binary.name} не связан с найденным "
                    "проектом сборки. Подтвердите его как prebuilt output либо укажите "
                    "реальный C++/C# project вручную"
                ),
                "path": str(binary),
            }
        )

    for source in files:
        if source.suffix.casefold() != ".txt":
            continue
        target = _source_cfg_target(mod_root, source)
        if target is None:
            continue
        try:
            load_blockpar(source)
        except Exception:
            continue
        key = target.as_posix().casefold()
        if key in ambiguous_outputs:
            continue
        if key in claimed_outputs:
            artifacts = [
                artifact
                for artifact in artifacts
                if str(artifact.get("output", "")).casefold() != key
            ]
            claimed_outputs.discard(key)
            ambiguous_outputs.add(key)
            issues.append(
                {
                    "severity": "warning",
                    "code": "project-init-ambiguous-dat-source",
                    "message": f"Несколько TXT претендуют на {target}; все спорные связи исключены из черновика",
                    "path": str(source),
                }
            )
            continue
        claimed_outputs.add(key)
        artifacts.append(
            {
                "id": _identifier(target.stem, f"dat-{len(artifacts) + 1}"),
                "kind": "dat",
                "source": source.relative_to(destination.parent).as_posix(),
                "output": target.as_posix(),
            }
        )

    rson_sources: list[tuple[Path, str]] = []
    for source in files:
        if source.suffix.casefold() != ".rson":
            continue
        try:
            script_name = str(load_rson(source).summary().get("name") or source.stem)
        except Exception as exc:
            issues.append(
                {
                    "severity": "warning",
                    "code": "project-init-invalid-rson",
                    "message": f"RSON не добавлен автоматически: {exc}",
                    "path": str(source),
                }
            )
            continue
        rson_sources.append((source, script_name))

    declared_languages = module.languages
    language_files = [
        path
        for path in files
        if path.name.casefold() == "lang.dat" and "cfg" in {part.casefold() for part in path.parts}
    ]
    shared_language = len(rson_sources) > 1 and bool(language_files)
    if shared_language:
        issues.append(
            {
                "severity": "warning",
                "code": "project-init-shared-lang-output-unresolved",
                "message": (
                    "Несколько RSON используют общий Lang.dat. Черновик оставляет существующий DAT "
                    "побайтно и не угадывает порядок слияния; задайте языковую сборку явно"
                ),
                "path": str(mod_root),
            }
        )
    for source, script_name in rson_sources:
        output_rel = _matching_binary(mod_root, "DATA/Script", script_name, ".scr")
        key = output_rel.as_posix().casefold()
        if key in ambiguous_outputs:
            continue
        if key in claimed_outputs:
            artifacts = [
                artifact
                for artifact in artifacts
                if str(artifact.get("output", "")).casefold() != key
            ]
            claimed_outputs.discard(key)
            ambiguous_outputs.add(key)
            issues.append(
                {
                    "severity": "warning",
                    "code": "project-init-ambiguous-script-output",
                    "message": f"Несколько исходников претендуют на {output_rel}; все спорные связи исключены",
                    "path": str(source),
                }
            )
            continue
        claimed_outputs.add(key)
        artifact: dict[str, Any] = {
            "id": _identifier(script_name, f"script-{len(artifacts) + 1}"),
            "kind": "rson",
            "source": source.relative_to(destination.parent).as_posix(),
            "output": output_rel.as_posix(),
        }
        fragment = source.with_name(f"{script_name}.lang.txt")
        if fragment.is_file():
            artifact["lang_fragment"] = (Path("SOURCE") / "ProjectBuild" / fragment.name).as_posix()
        if len(rson_sources) == 1 and language_files:
            selected_lang = None
            for language in declared_languages:
                selected_lang = next(
                    (
                        path
                        for path in language_files
                        if language.casefold() in {part.casefold() for part in path.parts}
                    ),
                    None,
                )
                if selected_lang is not None:
                    break
            selected_lang = selected_lang or language_files[0]
            artifact["lang_dat"] = selected_lang.relative_to(mod_root).as_posix()
            artifact["lang_base"] = selected_lang.relative_to(destination.parent).as_posix()
        artifacts.append(artifact)

    known_scripts = {script_name.casefold() for _source, script_name in rson_sources}
    for source in files:
        if source.suffix.casefold() != ".rsm":
            continue
        try:
            project = inspect_rsm_project(source)
        except Exception:
            continue
        script_name = str(project.script_name or "").strip()
        if not script_name or script_name.casefold() in known_scripts:
            continue
        output_rel = _matching_binary(mod_root, "DATA/Script", script_name, ".scr")
        key = output_rel.as_posix().casefold()
        if key in claimed_outputs:
            continue
        claimed_outputs.add(key)
        known_scripts.add(script_name.casefold())
        artifacts.append(
            {
                "id": _identifier(script_name, f"rsm-{len(artifacts) + 1}"),
                "kind": "rsm",
                "source": source.relative_to(destination.parent).as_posix(),
                "output": output_rel.as_posix(),
            }
        )

    _make_artifact_ids_unique(artifacts)
    _make_artifact_ids_unique(external_builds)
    native_report = validate_native_mod(mod_root)
    issues.extend(
        {
            "severity": item.severity,
            "code": item.code,
            "message": item.message,
            "path": item.path,
        }
        for item in native_report.issues
    )
    selected_name = name or module.name or mod_root.name
    selected_prefix = prefix or _default_prefix(mod_root)
    rendered = _render_project_toml(
        name=selected_name,
        mod_root=relative_mod,
        prefix=selected_prefix,
        artifacts=artifacts,
        external_builds=external_builds,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".srhd-project-init-",
        suffix=".toml",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(rendered, encoding="utf-8")
        # Prove that the generated draft is loadable before replacing a prior
        # configuration or reporting success.
        load_project(temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "schema": PROJECT_INIT_SCHEMA,
        "mod": str(mod_root),
        "output": str(destination),
        "name": selected_name,
        "prefix": selected_prefix,
        "artifacts": artifacts,
        "external_builds": external_builds,
        "native_loader": native_report.as_dict() if native_report.detected else None,
        "issues": issues,
        "summary": {
            "artifacts": len(artifacts),
            "external_builds": len(external_builds),
            "warnings": sum(item["severity"] == "warning" for item in issues),
        },
    }


def _fingerprint_changes(previous: Mapping[str, Any] | None, current: Mapping[str, Any]) -> list[str]:
    if previous is None:
        return ["no-previous-cache-entry"]
    changes: list[str] = []
    previous_inputs = {str(item.get("path")): item for item in previous.get("inputs", [])}
    current_inputs = {str(item.get("path")): item for item in current.get("inputs", [])}
    for path in sorted(previous_inputs.keys() | current_inputs.keys(), key=str.casefold):
        if path not in previous_inputs:
            changes.append(f"input-added:{path}")
        elif path not in current_inputs:
            changes.append(f"input-removed:{path}")
        elif previous_inputs[path] != current_inputs[path]:
            changes.append(f"input-changed:{path}")
    for field in ("artifact", "tools", "engine"):
        if previous.get(field) != current.get(field):
            changes.append(f"{field}-changed")
    return changes or ["cache-entry-invalid-or-missing"]


def _expected_artifact_outputs(
    project: ModProject,
    artifact: Mapping[str, Any],
    full_mod: Path,
) -> list[str]:
    output = _safe_relative(str(artifact["output"]), "artifact.output")
    source = _effective_artifact_source(project, artifact, full_mod)
    if str(artifact["kind"]).casefold() == "copy" and source.is_dir():
        values = [
            (Path(*output.parts) / path.relative_to(source)).as_posix()
            for path in iter_files(source)
        ]
    else:
        values = [output.as_posix()]
    for field in ("lang_dat", "lang_txt", "lang_fragment"):
        if artifact.get(field):
            values.append(_safe_relative(str(artifact[field]), f"artifact.{field}").as_posix())
    return list(dict.fromkeys(values))


def plan_project(
    path: str | Path = ".",
    *,
    variant: str | None = None,
    tools_root: str | Path | None = None,
) -> dict[str, Any]:
    project = load_project(path)
    variant_name, variant_config, variables = _resolve_variant(project, variant)
    artifacts = _selected_artifacts(project, variant_name, variables)
    selected_tools_root = Path(tools_root).resolve() if tools_root is not None else project.tools_root
    required_tools = {
        name
        for artifact in artifacts
        for name in _artifact_tool_names(artifact)
    }
    toolchain = Toolchain(selected_tools_root) if required_tools else None
    cache = ArtifactCache(project.cache_root)
    issues: list[dict[str, Any]] = []
    issues.extend(_external_build_issues(project))
    artifact_rows: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix=".srhd-project-plan-", dir=project.root) as temp_name:
        full_mod = Path(temp_name) / project.mod_root.name
        stage_tree(project.mod_root, full_mod)
        _apply_variant_files(project, full_mod, variant_config, variables)
        try:
            _validate_effective_artifact_outputs(project, artifacts, full_mod)
        except ProjectConfigError as exc:
            issues.append(
                {
                    "severity": "error",
                    "code": "project-plan-output-overlap",
                    "message": str(exc),
                }
            )
        for artifact in artifacts:
            identifier = str(artifact["id"])
            row: dict[str, Any] = {
                "id": identifier,
                "kind": str(artifact["kind"]),
                "outputs": _expected_artifact_outputs(project, artifact, full_mod),
            }
            try:
                source = _effective_artifact_source(project, artifact, full_mod)
                inputs = _artifact_inputs(project, artifact, source, full_mod)
                artifact_tools = _artifact_tool_names(artifact)
                if artifact_tools and toolchain is None:
                    raise RuntimeError("toolchain не инициализирован для артефакта")
                tools = (
                    _tool_fingerprints(toolchain, artifact_tools)
                    if toolchain is not None
                    else []
                )
                key, fingerprint = _artifact_cache_key(
                    project,
                    variant_name,
                    artifact,
                    inputs,
                    tools,
                    full_mod,
                )
                current = cache.probe(key)
                latest = cache.latest(variant_name, identifier)
                row.update(
                    {
                        "cache_key": key,
                        "cache": "hit" if current is not None else "miss",
                        "rebuild": current is None,
                        "reasons": []
                        if current is not None
                        else _fingerprint_changes(
                            latest.get("fingerprint") if latest is not None else None,
                            fingerprint,
                        ),
                        "inputs": fingerprint["inputs"],
                        "tools": tools,
                    }
                )
            except Exception as exc:
                row.update({"cache": "unavailable", "rebuild": True, "reasons": [str(exc)]})
                issues.append(
                    {
                        "severity": "error",
                        "code": "project-plan-artifact-unavailable",
                        "message": f"Артефакт {identifier}: {exc}",
                    }
                )
            artifact_rows.append(row)

        strip_sources = bool(variant_config.get("strip_sources", project.raw.get("strip_sources", True)))
        excludes = _distribution_excludes(full_mod, (), strip_sources=strip_sources)
        existing: list[str] = []
        excluded: list[str] = []
        for file in iter_files(full_mod):
            relative = file.relative_to(full_mod).as_posix()
            if any(fnmatch.fnmatch(relative.casefold(), pattern.casefold()) for pattern in excludes):
                excluded.append(relative)
            else:
                existing.append(relative)
        outputs = sorted(
            set(existing).union(*(row["outputs"] for row in artifact_rows)),
            key=str.casefold,
        )
    prefix = _safe_relative(
        str(_expand(variant_config.get("prefix", project.prefix), variables, "variant.prefix")),
        "variant.prefix",
    ).as_posix()
    targets: list[dict[str, Any]] = []
    for target_name in sorted(project.targets, key=str.casefold):
        try:
            target = resolve_project_target(project, target_name, variant=variant_name)
            target_prefix = _safe_relative(target.prefix, "target.prefix")
            targets.append(
                {
                    **target.as_dict(),
                    "destination": str(target.root.joinpath(*target_prefix.parts)),
                    "status": "configured",
                }
            )
        except ProjectConfigError as exc:
            targets.append(
                {"name": target_name, "status": "unconfigured", "reason": str(exc)}
            )
    publish_config = project.raw.get("publish", {})
    publish_output = None
    if isinstance(publish_config, Mapping) and isinstance(publish_config.get("output"), str):
        publish_text = str(_expand(publish_config["output"], variables, "publish.output"))
        publish_output = str(_project_path(project.root, publish_text, "publish.output"))
    return {
        "schema": PROJECT_PLAN_SCHEMA,
        "project": str(project.path),
        "variant": variant_name,
        "prefix": prefix,
        "blocked": any(item["severity"] == "error" for item in issues),
        "destinations": {
            "build": str(
                project.build_root
                / variant_name
                / Path(*_safe_relative(prefix, "prefix").parts)
            ),
            "release": publish_output,
            "targets": targets,
        },
        "artifacts": artifact_rows,
        "external_builds": _external_build_report(project),
        "files": {
            "game": outputs,
            "release": [f"{prefix.rstrip('/')}/{item}" for item in outputs],
            "excluded": sorted(excluded, key=str.casefold),
        },
        "issues": issues,
        "summary": {
            "artifacts": len(artifact_rows),
            "cache_hits": sum(item.get("cache") == "hit" for item in artifact_rows),
            "rebuilds": sum(bool(item.get("rebuild")) for item in artifact_rows),
            "files": len(outputs),
            "errors": sum(item["severity"] == "error" for item in issues),
        },
    }


def _path_usage(path: Path) -> tuple[int, int]:
    if not path.exists() or path.is_symlink():
        return 0, 0
    files = iter_files(path) if path.is_dir() else [path]
    return len(files), sum(item.stat().st_size for item in files)


def _workspace_rows(project: ModProject) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    now = time.time()
    skip = {project.build_root.resolve(), project.cache_root.resolve()}
    for current, directories, _files in os.walk(project.root, followlinks=False):
        current_path = Path(current)
        kept: list[str] = []
        for name in directories:
            raw_candidate = current_path / name
            if raw_candidate.is_symlink():
                continue
            candidate = raw_candidate.resolve()
            if project.root != candidate and project.root not in candidate.parents:
                continue
            if candidate in skip or name in {".git", "__pycache__", ".pytest_cache"}:
                continue
            if any(name.startswith(prefix) for prefix in _WORKSPACE_PREFIXES):
                try:
                    age = max(0.0, now - candidate.stat().st_mtime)
                except OSError:
                    age = 0.0
                files, size = _path_usage(candidate)
                result.append(
                    {
                        "path": str(candidate),
                        "files": files,
                        "bytes": size,
                        "age_seconds": round(age, 3),
                        "stale": age >= _STALE_SECONDS,
                    }
                )
                continue
            kept.append(name)
        directories[:] = kept
    return sorted(result, key=lambda item: str(item["path"]).casefold())


def doctor_project(
    path: str | Path = ".",
    *,
    variant: str | None = None,
    tools_root: str | Path | None = None,
) -> dict[str, Any]:
    project = load_project(path)
    issues: list[dict[str, Any]] = []
    try:
        plan = plan_project(project.path, variant=variant, tools_root=tools_root)
        issues.extend(plan["issues"])
    except Exception as exc:
        plan = None
        issues.append(
            {
                "severity": "error",
                "code": "project-doctor-plan-failed",
                "message": str(exc),
            }
        )

    selected_tools_root = Path(tools_root).resolve() if tools_root is not None else project.tools_root
    doctor_variant, _doctor_config, doctor_variables = _resolve_variant(project, variant)
    doctor_artifacts = _selected_artifacts(project, doctor_variant, doctor_variables)
    required = {
        name
        for artifact in doctor_artifacts
        for name in _artifact_tool_names(artifact)
    }
    chain = Toolchain(selected_tools_root) if required else None
    tools = [item for item in chain.status() if item["name"] in required] if chain else []
    for item in tools:
        if not item.get("available"):
            issues.append(
                {
                    "severity": "error",
                    "code": "project-doctor-tool-missing",
                    "message": f"Требуемый инструмент {item['name']} не найден: {item['path']}",
                }
            )

    targets: list[dict[str, Any]] = []
    for name in project.targets:
        try:
            target = resolve_project_target(project, name, variant=variant)
            targets.append(target.as_dict())
        except Exception as exc:
            issues.append(
                {
                    "severity": "error",
                    "code": "project-doctor-target-invalid",
                    "message": f"Цель {name}: {exc}",
                }
            )

    for label, candidate in (("build_root", project.build_root), ("cache_root", project.cache_root)):
        if not candidate.name.casefold().startswith(".srhd-"):
            issues.append(
                {
                    "severity": "warning",
                    "code": "project-doctor-nonservice-derived-root",
                    "message": f"{label} не имеет служебного имени .srhd-*: {candidate}",
                }
            )
    cache_files, cache_bytes = _path_usage(project.cache_root)
    workspaces = _workspace_rows(project)
    return {
        "schema": PROJECT_DOCTOR_SCHEMA,
        "project": project.as_dict(),
        "healthy": not any(item["severity"] == "error" for item in issues),
        "plan": plan,
        "tools": tools,
        "targets": targets,
        "cache": {
            "root": str(project.cache_root),
            "files": cache_files,
            "bytes": cache_bytes,
        },
        "workspaces": workspaces,
        "issues": issues,
        "summary": {
            "errors": sum(item["severity"] == "error" for item in issues),
            "warnings": sum(item["severity"] == "warning" for item in issues),
            "stale_workspaces": sum(bool(item["stale"]) for item in workspaces),
        },
    }


def clean_project(
    path: str | Path = ".",
    *,
    apply: bool = False,
    build: bool = False,
    cache: bool = False,
) -> dict[str, Any]:
    project = load_project(path)
    candidates: list[dict[str, Any]] = []
    for workspace in _workspace_rows(project):
        if workspace["stale"]:
            candidates.append({**workspace, "kind": "stale-workspace"})
    for enabled, kind, candidate in (
        (build, "build-root", project.build_root),
        (cache, "cache-root", project.cache_root),
    ):
        if not enabled or not candidate.exists():
            continue
        if not candidate.name.casefold().startswith(".srhd-"):
            raise ProjectConfigError(
                f"Отказ очистки {kind}: каталог должен иметь служебное имя .srhd-*: {candidate}"
            )
        files, size = _path_usage(candidate)
        candidates.append(
            {"path": str(candidate), "kind": kind, "files": files, "bytes": size, "stale": True}
        )

    # Collapse nested candidates so one operation never reports or deletes the
    # same bytes twice.
    ordered = sorted(candidates, key=lambda item: len(Path(item["path"]).parts))
    selected: list[dict[str, Any]] = []
    for item in ordered:
        candidate = Path(item["path"])
        if any(Path(parent["path"]) in candidate.parents for parent in selected):
            continue
        selected.append(item)

    removed: list[str] = []
    if apply:
        for item in selected:
            candidate = Path(item["path"])
            if _is_link_or_junction(candidate) or not candidate.is_dir():
                raise ProjectConfigError(
                    f"Отказ очистки {item['kind']}: путь стал ссылкой, junction или не-каталогом: "
                    f"{candidate}"
                )
            verified = _project_path(
                project.root,
                candidate,
                f"project clean {item['kind']}",
                reject_links=True,
            )
            shutil.rmtree(verified)
            removed.append(str(verified))
    return {
        "schema": PROJECT_CLEAN_SCHEMA,
        "project": str(project.path),
        "apply": apply,
        "planned": selected,
        "removed": removed,
        "summary": {
            "candidates": len(selected),
            "files": sum(int(item["files"]) for item in selected),
            "bytes": sum(int(item["bytes"]) for item in selected),
            "removed": len(removed),
        },
    }


__all__ = [
    "initialize_project",
    "plan_project",
    "doctor_project",
    "clean_project",
]
