from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .blockpar import BlockParDocument


@dataclass(frozen=True)
class ScriptArtifactIssue:
    severity: str
    code: str
    message: str
    path: str | None = None
    location: str | None = None
    evidence: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _script_parameters(document: BlockParDocument) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for node in document.roots:
        if node.name.casefold() != "script":
            continue
        result.extend((parameter.key, parameter.value) for parameter in node.parameters)
    return result


def _path_parts(value: str) -> list[str]:
    return [part for part in value.replace("/", "\\").split("\\") if part]


def lint_script_cache(
    mod_root: str | Path,
    scripts: Sequence[str | Path],
    registrations: Mapping[str, Sequence[str]],
    cache_documents: Sequence[tuple[str | Path, BlockParDocument]],
) -> list[ScriptArtifactIssue]:
    """Cross-check local SCR files, Main registrations and CacheData mappings.

    Extra CacheData entries are intentionally allowed: merge-style patch mods
    can legally reference a script owned by a dependency. Every *local and
    registered* SCR, however, must have a self-consistent local cache mapping.
    """
    root = Path(mod_root).resolve()
    issues: list[ScriptArtifactIssue] = []
    normalized_registrations = {key.casefold(): values for key, values in registrations.items()}
    local_scripts = {Path(path).stem.casefold(): Path(path).name for path in scripts}

    if len(cache_documents) > 1:
        baseline_path, baseline = cache_documents[0]
        baseline_semantic = baseline.canonical_semantic()
        for path, document in cache_documents[1:]:
            if document.canonical_semantic() != baseline_semantic:
                issues.append(
                    ScriptArtifactIssue(
                        "error",
                        "cachedata-source-binary-mismatch",
                        "Исходный и собранный CacheData содержат разные ссылки; игра может загрузить не тот ресурс",
                        str(Path(path).resolve()),
                        evidence=f"не совпадает с {Path(baseline_path).resolve()}",
                    )
                )

    if not cache_documents:
        return issues

    for cache_path, document in cache_documents:
        resolved_cache = str(Path(cache_path).resolve())
        parameters = _script_parameters(document)
        by_key: dict[str, list[tuple[str, str]]] = {}
        for key, value in parameters:
            by_key.setdefault(key.casefold(), []).append((key, value))

        for folded_stem, filename in sorted(local_scripts.items()):
            if folded_stem not in normalized_registrations:
                continue
            mappings = by_key.get(folded_stem, [])
            if not mappings:
                issues.append(
                    ScriptArtifactIssue(
                        "error",
                        "cache-script-missing",
                        f"Локальный зарегистрированный {filename} отсутствует в узле Script файла CacheData",
                        resolved_cache,
                        "Script",
                    )
                )
                continue

            expected_tail = [root.name.casefold(), "data", "script", filename.casefold()]
            for key, value in mappings:
                parts = _path_parts(value)
                basename = parts[-1] if parts else ""
                if Path(basename).stem.casefold() != key.casefold():
                    issues.append(
                        ScriptArtifactIssue(
                            "error",
                            "cache-script-key-path-mismatch",
                            f"Ключ CacheData {key} указывает на другой SCR: {basename or 'пустой путь'}",
                            resolved_cache,
                            f"Script/{key}",
                            value,
                        )
                    )
                folded_tail = [part.casefold() for part in parts[-4:]]
                if folded_tail != expected_tail:
                    expected = f"Mods\\<раздел>\\{root.name}\\DATA\\Script\\{filename}"
                    issues.append(
                        ScriptArtifactIssue(
                            "error",
                            "cache-script-local-path-mismatch",
                            f"CacheData для {filename} должен ссылаться на локальный SCR этого мода",
                            resolved_cache,
                            f"Script/{key}",
                            f"получено: {value}; ожидается: {expected}",
                        )
                    )
    return issues

