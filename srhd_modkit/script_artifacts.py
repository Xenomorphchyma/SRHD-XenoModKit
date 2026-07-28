from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .blockpar import BlockParDocument
from .scripts import RsonProject


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


@dataclass(frozen=True)
class _DialogAnswerLanguageRef:
    script_name: str
    object_id: int | None
    object_name: str
    answer_number: int | None
    message: str
    dialog_names: tuple[str, ...]

    @property
    def label(self) -> str:
        object_label = (
            f"TDialogAnswer #{self.object_id}"
            if self.object_id is not None
            else "TDialogAnswer (номер объекта недоступен)"
        )
        answer_label = (
            f", AMsg.Num={self.answer_number}"
            if self.answer_number is not None
            else ""
        )
        dialogs = (
            f", диалог {', '.join(self.dialog_names)}"
            if self.dialog_names
            else ""
        )
        return f"{object_label}{answer_label}{dialogs}"


_SCRIPT_DIALOG_KEY_RE = re.compile(
    r"\bScript\.([A-Za-z0-9_.-]+)\.(\d+)\b",
    re.IGNORECASE,
)
_DIALOG_EDGE_CALL_RE = re.compile(
    r"\b(DChange|DAdd)\s*\(\s*(\d+)\s*\)",
    re.IGNORECASE,
)
_DANSWER_LITERAL_RE = re.compile(
    r"^\s*DAnswer\s*\(\s*(['\"])(.*?)\1\s*\)\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)
_LANG_CODE_STUB_RE = re.compile(
    r"^\s*(?:DAnswer|DText|CT|Format)\s*\(",
    re.IGNORECASE,
)


def _dialog_message_value(value: str) -> str:
    """Normalize source/fragment syntax without changing visible text."""
    return value.strip().removesuffix(";").rstrip()


def _answer_value_is_visible(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    match = _DANSWER_LITERAL_RE.fullmatch(stripped)
    if not match:
        return True
    payload = match.group(2)
    if "~" in payload:
        return bool(payload.rsplit("~", 1)[1].strip())
    return payload.strip().casefold() not in {"", "exit", "fastexit", "restart"}


def _answer_value_is_code_stub(value: str) -> bool:
    return bool(_LANG_CODE_STUB_RE.match(value))


def _object_code(item: Mapping[str, Any]) -> str:
    lines: list[str] = []
    for field, value in item.items():
        if field in {"Code", "ActCode", "LinkCode"} and isinstance(value, list):
            lines.extend(str(line) for line in value if isinstance(line, str))
        elif field.casefold().endswith("code") and isinstance(value, str):
            lines.append(value)
    return "\n".join(lines)


def _dialog_names_by_answer(project: RsonProject) -> dict[int, tuple[str, ...]]:
    """Resolve dialog ownership through graph links plus DChange/DAdd edges."""
    objects = list(project.iter_objects())
    by_id = {
        item.get("#"): item
        for item in objects
        if isinstance(item.get("#"), int)
    }
    messages = {
        int(item["DMsg.Num"]): item["#"]
        for item in objects
        if item.get("Type") == "TDialogMsg"
        and isinstance(item.get("#"), int)
        and str(item.get("DMsg.Num", "")).strip().isdigit()
    }
    answers = {
        int(item["AMsg.Num"]): item["#"]
        for item in objects
        if item.get("Type") == "TDialogAnswer"
        and isinstance(item.get("#"), int)
        and str(item.get("AMsg.Num", "")).strip().isdigit()
    }
    outgoing: dict[int, set[int]] = {}
    links = project.data.get("Visual.Links", [])
    if isinstance(links, list):
        for link in links:
            if not isinstance(link, dict):
                continue
            begin = link.get("Begin")
            end = link.get("End")
            if isinstance(begin, int) and isinstance(end, int):
                outgoing.setdefault(begin, set()).add(end)
    for item in objects:
        object_id = item.get("#")
        parent = item.get("Parent")
        if (
            isinstance(object_id, int)
            and isinstance(parent, int)
            and parent in by_id
            and parent != object_id
        ):
            outgoing.setdefault(parent, set()).add(object_id)
        if not isinstance(object_id, int):
            continue
        for call, raw_number in _DIALOG_EDGE_CALL_RE.findall(_object_code(item)):
            number = int(raw_number)
            target = messages.get(number) if call.casefold() == "dchange" else answers.get(number)
            if target is not None:
                outgoing.setdefault(object_id, set()).add(target)

    result: dict[int, list[str]] = {}
    for dialog in objects:
        if dialog.get("Type") != "TDialog" or not isinstance(dialog.get("#"), int):
            continue
        name = str(dialog.get("Name", "")).strip() or f"TDialog #{dialog['#']}"
        pending = [dialog["#"]]
        visited: set[int] = set()
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            item = by_id.get(current)
            if item is not None and item.get("Type") == "TDialogAnswer":
                result.setdefault(current, []).append(name)
            pending.extend(outgoing.get(current, ()))
    return {
        object_id: tuple(dict.fromkeys(names))
        for object_id, names in result.items()
    }


def _dialog_answer_language_refs(project: RsonProject) -> list[_DialogAnswerLanguageRef]:
    dialog_names = _dialog_names_by_answer(project)
    result: list[_DialogAnswerLanguageRef] = []
    for item in project.iter_objects():
        if item.get("Type") != "TDialogAnswer":
            continue
        message = str(item.get("Msg", "")).strip()
        if not message:
            continue
        raw_number = str(item.get("AMsg.Num", "")).strip()
        number = int(raw_number) if raw_number.isdigit() else None
        object_id = item.get("#") if isinstance(item.get("#"), int) else None
        result.append(
            _DialogAnswerLanguageRef(
                project.name,
                object_id,
                str(item.get("Name", "")).strip(),
                number,
                message,
                dialog_names.get(object_id, ()) if object_id is not None else (),
            )
        )
    return result


def _script_node_parameters(
    document: BlockParDocument,
    script_name: str,
) -> dict[str, str] | None:
    try:
        node = document.find_node(f"Script/{script_name}")
    except KeyError:
        return None
    return {parameter.key: parameter.value for parameter in node.parameters}


def _is_data_script_lang(path: Path) -> bool:
    return [part.casefold() for part in path.parts[-3:]] == [
        "data",
        "script",
        "lang.dat",
    ]


def _is_cfg_language_lang(path: Path) -> bool:
    return (
        len(path.parts) >= 3
        and path.parts[-3].casefold() == "cfg"
        and path.parts[-1].casefold() == "lang.dat"
    )


def _expected_answer_keys(
    project: RsonProject,
    references: Sequence[_DialogAnswerLanguageRef],
    generated_entries: Sequence[tuple[str, str]],
) -> dict[_DialogAnswerLanguageRef, tuple[str, ...]]:
    """Map answers to exact keys only when RSON or compiler output proves them."""
    result: dict[_DialogAnswerLanguageRef, list[str]] = {}
    available = [
        (key, value)
        for key, value in generated_entries
        if key.isdecimal() and value.strip()
    ]
    used: set[int] = set()
    for reference in references:
        keys: list[str] = []
        for match in _SCRIPT_DIALOG_KEY_RE.finditer(reference.message):
            if match.group(1).casefold() == project.name.casefold():
                keys.append(match.group(2))
        normalized = _dialog_message_value(reference.message)
        for index, (key, value) in enumerate(available):
            if index in used:
                continue
            if _dialog_message_value(value) == normalized:
                keys.append(key)
                used.add(index)
                break
        result[reference] = list(dict.fromkeys(keys))
    return {key: tuple(values) for key, values in result.items()}


def lint_script_dialog_language(
    projects: Sequence[RsonProject],
    packaged_languages: Sequence[tuple[str | Path, BlockParDocument | None]],
    generated_fragments: Mapping[
        str,
        tuple[str | Path, Sequence[tuple[str, str]]],
    ] | None = None,
    *,
    checked_scripts: Sequence[str] | None = None,
    binary_scripts: Sequence[Mapping[str, Any]] = (),
) -> list[ScriptArtifactIssue]:
    """Cross-check visible dialog answers against shipped script language data.

    RScript may read or create DATA/Script/Lang.dat as a project/build artifact,
    but SRHD loads active-language overrides from CFG/<language>/Lang.dat.  The
    former is still inspected for malformed generated values, but it is never
    accepted as proof that a static TDialogAnswer label reaches the game.
    Exact numeric keys are required only when a canonical Script.<name>.<number>
    reference or RScript's adjacent language fragment proves the mapping.
    """
    checked_projects = list(projects)
    project_names = {project.name.casefold() for project in checked_projects}
    for script in binary_scripts:
        script_path = Path(str(script.get("path", ""))).resolve()
        by_name: dict[str, set[str]] = {}
        for item in script.get("dialog_language_keys", ()):
            if not isinstance(item, Mapping):
                continue
            script_name = str(item.get("script_name", "")).strip()
            key = str(item.get("key", "")).strip()
            if script_name and key.isdecimal():
                by_name.setdefault(script_name, set()).add(key)
        for script_name, keys in by_name.items():
            if script_name.casefold() in project_names:
                continue
            checked_projects.append(
                RsonProject(
                    {
                        "ScriptName": script_name,
                        "Visual.Objects": [
                            {
                                "Dialogs": [
                                    {
                                        "Type": "TDialogAnswer",
                                        "Name": "",
                                        "Parent": -1,
                                        "Msg": (
                                            "DAnswer(CT("
                                            f"'Script.{script_name}.{key}'"
                                            "));"
                                        ),
                                    }
                                    for key in sorted(keys, key=int)
                                ]
                            }
                        ],
                    },
                    script_path,
                )
            )
            project_names.add(script_name.casefold())

    fragments = {
        name.casefold(): (Path(path).resolve(), tuple(entries))
        for name, (path, entries) in (generated_fragments or {}).items()
    }
    active = {name.casefold() for name in checked_scripts or ()}
    documents = [
        (Path(path).resolve(), document)
        for path, document in packaged_languages
    ]
    issues: list[ScriptArtifactIssue] = []
    reported_code_stubs: set[tuple[str, str, str]] = set()

    for project in checked_projects:
        references = _dialog_answer_language_refs(project)
        if not references:
            continue
        fragment_info = fragments.get(project.name.casefold())
        if active and project.name.casefold() not in active and fragment_info is None:
            continue
        fragment_path: Path | None = None
        fragment_entries: tuple[tuple[str, str], ...] = ()
        if fragment_info is not None:
            fragment_path, fragment_entries = fragment_info
        expected_keys = _expected_answer_keys(project, references, fragment_entries)
        expected_preview = sorted(
            {
                key
                for keys in expected_keys.values()
                for key in keys
            },
            key=int,
        )
        references_label = "; ".join(reference.label for reference in references)
        expected_path = f"Script/{project.name}"

        runtime_documents = [
            (path, document)
            for path, document in documents
            if _is_cfg_language_lang(path)
        ]
        build_documents = [
            (path, document)
            for path, document in documents
            if _is_data_script_lang(path)
        ]

        if not documents:
            key_text = (
                f"/{','.join(expected_preview)}"
                if expected_preview
                else "/<ключи RScript>"
            )
            issues.append(
                ScriptArtifactIssue(
                    "error",
                    "script-dialog-lang-dat-missing",
                    f"{references_label}: локализуемые варианты ответа не имеют "
                    "игрового Lang.dat. Ожидались непустые записи "
                    f"{expected_path}{key_text} в CFG/<язык>/Lang.dat",
                    str(project.path) if project.path else None,
                    f"{expected_path}{key_text}",
                    f"проверен проект {project.name}; Lang.dat не найден",
                )
            )
            continue

        if not runtime_documents:
            supplied = ", ".join(str(path) for path, _document in build_documents)
            key_text = (
                f"/{','.join(expected_preview)}"
                if expected_preview
                else "/<ключи RScript>"
            )
            issues.append(
                ScriptArtifactIssue(
                    "error",
                    "script-dialog-lang-runtime-dat-missing",
                    f"{references_label}: найден только DATA/Script/Lang.dat, "
                    "который является артефактом сборки/импорта RScript и не "
                    "доказывает загрузку подписи игрой. Опубликуйте "
                    f"{expected_path}{key_text} в CFG/<язык>/Lang.dat",
                    str(project.path) if project.path else None,
                    f"{expected_path}{key_text}",
                    f"проверены: {supplied or 'Lang.dat вне CFG/<язык>'}",
                )
            )
        selected_documents = runtime_documents or build_documents
        if not selected_documents:
            continue

        nodes: list[tuple[Path, dict[str, str]]] = []
        for path, document in selected_documents:
            parameters = (
                _script_node_parameters(document, project.name)
                if document is not None
                else None
            )
            if parameters is None:
                issues.append(
                    ScriptArtifactIssue(
                        "error",
                        "script-dialog-lang-node-missing",
                        f"{references_label}: в поставляемом языке отсутствует узел "
                        f"{expected_path}",
                        str(path),
                        expected_path,
                        f"проверен {path}",
                    )
                )
                continue
            nodes.append((path, parameters))

        for path, parameters in nodes:
            for reference in references:
                for key in expected_keys.get(reference, ()):
                    value = parameters.get(key)
                    if value is None:
                        issues.append(
                            ScriptArtifactIssue(
                                "error",
                                "script-dialog-lang-key-missing",
                                f"{reference.label}: отсутствует языковой ключ "
                                f"{expected_path}/{key}",
                                str(path),
                                f"{expected_path}/{key}",
                                f"Msg={reference.message}",
                            )
                        )
                    elif _answer_value_is_code_stub(value):
                        reported_code_stubs.add(
                            (str(path).casefold(), project.name.casefold(), key)
                        )
                        issues.append(
                            ScriptArtifactIssue(
                                "error",
                                "script-dialog-lang-value-code-stub",
                                f"{reference.label}: {expected_path}/{key} содержит "
                                "RScript-код вместо видимой подписи варианта ответа",
                                str(path),
                                f"{expected_path}/{key}",
                                f"value={value!r}",
                            )
                        )
                    elif not _answer_value_is_visible(value):
                        issues.append(
                            ScriptArtifactIssue(
                                "error",
                                "script-dialog-lang-value-empty",
                                f"{reference.label}: {expected_path}/{key} не содержит "
                                "видимой подписи варианта ответа",
                                str(path),
                                f"{expected_path}/{key}",
                                f"value={value!r}",
                            )
                        )

        if fragment_path is None or not fragment_entries:
            continue
        nonempty_generated = {
            key: value
            for key, value in fragment_entries
            if key.isdecimal() and value.strip()
        }
        unpublished: dict[str, list[str]] = {}
        for path, parameters in nodes:
            mismatched = [
                key
                for key in nonempty_generated
                if key not in parameters
            ]
            if mismatched:
                unpublished[str(path)] = sorted(mismatched, key=int)
        if unpublished:
            evidence = "; ".join(
                f"{path}: {','.join(keys)}"
                for path, keys in unpublished.items()
            )
            issues.append(
                ScriptArtifactIssue(
                    "error",
                    "script-generated-lang-unpublished",
                    f"RScript создал непустой языковой фрагмент для {project.name}, "
                    "но поставляемый Lang.dat не содержит соответствующие ключи",
                    str(fragment_path),
                    expected_path,
                    evidence,
                )
            )

    # SCR without RSON cannot expose TDialogAnswer object numbers, but a code
    # expression stored directly in Script/<name>/<key> is never a visible
    # localized label. Check this independently of exact graph recovery.
    script_names = (
        {
            str(name).strip()
            for name in checked_scripts or ()
            if str(name).strip()
        }
        or {project.name for project in checked_projects}
    )
    for script_name in sorted(script_names, key=str.casefold):
        nodes = [
            (path, parameters)
            for path, document in documents
            if document is not None
            and (parameters := _script_node_parameters(document, script_name)) is not None
        ]
        for path, parameters in nodes:
            for key, value in parameters.items():
                marker = (str(path).casefold(), script_name.casefold(), key)
                if marker in reported_code_stubs or not _answer_value_is_code_stub(value):
                    continue
                issues.append(
                    ScriptArtifactIssue(
                        "error",
                        "script-dialog-lang-value-code-stub",
                        f"Script/{script_name}/{key} содержит RScript-код вместо "
                        "видимого текста; имя диалога и номер TDialogAnswer "
                        "недоступны без точной RSON/SCR-ссылки",
                        str(path),
                        f"Script/{script_name}/{key}",
                        f"value={value!r}",
                    )
                )
    return issues


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
