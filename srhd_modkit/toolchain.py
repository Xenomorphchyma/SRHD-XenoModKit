from __future__ import annotations

import errno
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from .files import sha256_file
from .formats import inspect_file
from .image_codec import read_gi, read_png, write_gi, write_png
from .blockpar import (
    BlockParDocument,
    BlockParNode,
    BlockParParameter,
    load_blockpar,
    parse_blockpar,
)
from .scripts import inspect_scr, load_rson
from .runtime_lint import (
    compare_storage_schemas,
    dialog_semantic_map,
    lint_rson_runtime,
)
from .script_artifacts import lint_script_dialog_language
from .game_text import lint_blockpar_display_text, lint_game_text
from .textio import read_text
from .rsm import RsmProject, inspect_rsm_project
from .hidden_process import HiddenControlAction, HiddenProcessTimeout, run_on_hidden_desktop
from .legacy_manifest import ensure_legacy_codepage_executable
from .executable_version import ExecutableVersion, detect_executable_version


EMPTY_RSCRIPT_LANG_DAT = b"\xff\xfe"
MIN_RSCRIPT_ADAPTIVE_TIMEOUT = 600.0
_DECOMPILE_CANONICALIZATION_SENSITIVE_RUNTIME_CODES = frozenset(
    {"runtime-turn-direct-world-access"}
)


def _decompiled_runtime_issue(issue: Any) -> dict[str, Any]:
    """Tag lint evidence recovered from SCR instead of authored RSON."""

    return {
        **issue.as_dict(),
        "analysis_origin": "decompiled-rson",
        "canonicalization_sensitive": (
            issue.code in _DECOMPILE_CANONICALIZATION_SENSITIVE_RUNTIME_CODES
        ),
    }


def _rscript_timeout_policy(
    source: Path,
    operation: str,
    requested: float | None,
) -> tuple[float | None, dict[str, Any]]:
    """Choose size-aware total and no-progress deadlines.

    Small projects keep a 60-second stalled-process window.  RScript can remain
    quiet longer while rebuilding a large graph, so both that window and the
    total deadline grow with proven project size.  The formulas are deliberately
    uncapped.  The Windows Job Object still owns and cleans the editor process
    if the caller or agent terminates.

    ``requested=None`` selects the adaptive deadlines, ``0`` disables both,
    and a positive value remains an explicit operator limit.
    """

    if requested is not None and requested < 0:
        raise ValueError("Таймаут не может быть отрицательным; 0 отключает оба ограничения")
    size_mib = source.stat().st_size / (1024 * 1024) if source.is_file() else 0.0
    code_lines = 0
    objects = 0
    if source.suffix.casefold() == ".rson" and source.is_file():
        try:
            summary = load_rson(source).summary()
            code_lines = int(summary.get("code_lines", 0))
            objects = int(summary.get("objects", 0))
        except Exception:
            pass
    if operation in {"compile", "roundtrip"}:
        adaptive = max(
            MIN_RSCRIPT_ADAPTIVE_TIMEOUT,
            180.0 + code_lines * 0.35 + objects * 1.5 + size_mib * 30.0,
        )
        adaptive_progress = max(
            60.0,
            30.0 + code_lines * 0.02 + objects * 0.25 + size_mib * 10.0,
        )
    else:
        adaptive = max(
            MIN_RSCRIPT_ADAPTIVE_TIMEOUT,
            300.0 + size_mib * 180.0,
        )
        adaptive_progress = max(60.0, 60.0 + size_mib * 60.0)
    adaptive = round(adaptive, 3)
    adaptive_progress = round(adaptive_progress, 3)
    if requested is None:
        selected = adaptive
        progress_timeout = adaptive_progress
        mode = "adaptive"
    elif requested == 0:
        selected = None
        progress_timeout = None
        mode = "disabled"
    else:
        selected = float(requested)
        progress_timeout = min(adaptive_progress, selected)
        mode = "explicit"
    return selected, {
        "mode": mode,
        "seconds": selected,
        "hard_seconds": selected,
        "adaptive_seconds": adaptive,
        "progress_seconds": progress_timeout,
        "adaptive_progress_seconds": adaptive_progress,
        "progress_resets_on": ["expected-output", "process-io", "control-action"],
        "operation": operation,
        "source_size": source.stat().st_size if source.is_file() else None,
        "objects": objects or None,
        "code_lines": code_lines or None,
    }


def _rscript_failure_diagnostic(
    exc: Exception,
    *,
    operation: str | None = None,
) -> dict[str, Any] | None:
    """Extract stable machine-readable facts from legacy modal diagnostics."""

    message = str(exc)
    folded = message.casefold()
    match = re.search(
        r"TFileEC\.Open\.\s*FileName=(.+?\.txt)\.",
        message,
        re.IGNORECASE,
    )
    if match:
        temp_path = Path(match.group(1)).resolve()
        exists = temp_path.exists()
        readable = False
        detected_encoding = None
        if temp_path.is_file():
            try:
                prefix = temp_path.read_bytes()[:4]
                readable = True
                if prefix.startswith(b"\xff\xfe"):
                    detected_encoding = "utf-16le-bom"
                elif prefix.startswith(b"\xfe\xff"):
                    detected_encoding = "utf-16be-bom"
                elif prefix.startswith(b"\xef\xbb\xbf"):
                    detected_encoding = "utf-8-bom"
                else:
                    detected_encoding = "legacy-ansi-or-unknown"
            except OSError:
                readable = False
        return {
            "code": "decompile-lang-import-tfileec-open",
            "message": message,
            "temp_path": str(temp_path),
            "exists": exists,
            "is_file": temp_path.is_file(),
            "readable": readable,
            "detected_encoding": detected_encoding,
            "lock_status": "unknown",
            "suggested_retry": "Повторите без --lang-dat или явно разрешите --fallback-without-lang",
        }
    if (
        operation == "compile"
        and isinstance(exc, TimeoutError)
        and "rscript" in folded
        and "build" in folded
        and ("dat files params" in folded or "script params" in folded)
        and (
            "подтверждённого прогресса" in folded
            or "аварийный лимит" in folded
            or "timeout" in folded
        )
    ):
        return {
            "code": "rscript-build-silent-main-window-stall",
            "message": message,
            "compiler_output_created": False,
            "classification": (
                "RScript вернулся к главному окну Build, не создал SCR и не "
                "показывал подтверждённого прогресса"
            ),
            "suggested_retry": (
                "Сравните RSON с последней успешно собранной версией; validate "
                "и runtime-lint не доказывают компилируемость внешним RScript"
            ),
        }
    if any(marker in folded for marker in ("скрытое окно", "контролы диалога", "окно ошибки")):
        return {
            "code": "rscript-modal-error",
            "message": message,
            "suggested_retry": "Если сбой возник при импорте Lang.dat, повторите без --lang-dat или явно разрешите --fallback-without-lang",
        }
    return None


class ScriptBuildFailure(ValueError):
    """Machine-readable failure from the headless RScript build workflow."""

    def __init__(self, message: str, report: dict[str, Any]):
        super().__init__(message)
        self.report = report

    def as_dict(self) -> dict[str, Any]:
        return self.report


class RsmBuildFailure(ValueError):
    """Machine-readable failure from the standalone rsmc workflow."""

    def __init__(self, message: str, report: dict[str, Any]):
        super().__init__(message)
        self.report = report

    def as_dict(self) -> dict[str, Any]:
        return self.report


def _project_graph_sha256(project: Any) -> str:
    payload = {
        "Visual.Objects": project.data.get("Visual.Objects"),
        "Visual.Links": project.data.get("Visual.Links"),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _cleanup_stale_decompile_transactions(parent: Path, *, older_than_seconds: float = 86400.0) -> list[str]:
    """Remove only marked ModKit transactions left by an interrupted process."""

    removed: list[str] = []
    now = time.time()
    for candidate in parent.glob(".srhd-decompile-*"):
        marker = candidate / ".srhd-transaction"
        if not candidate.is_dir() or not marker.is_file():
            continue
        try:
            if now - marker.stat().st_mtime < older_than_seconds:
                continue
            shutil.rmtree(candidate)
            removed.append(str(candidate))
        except OSError:
            continue
    return removed


def is_empty_rscript_lang_dat(path: str | Path) -> bool:
    """Return true only for DATA/Script/Lang.dat containing an empty UTF-16 BOM."""
    candidate = Path(path).resolve()
    folded = [part.casefold() for part in candidate.parts]
    if len(folded) < 3 or folded[-3:] != ["data", "script", "lang.dat"]:
        return False
    return (
        candidate.is_file()
        and candidate.stat().st_size == len(EMPTY_RSCRIPT_LANG_DAT)
        and candidate.read_bytes() == EMPTY_RSCRIPT_LANG_DAT
    )


def _replace_cross_device_safe(staged: Path, destination: Path) -> None:
    """Atomically publish a staged file even when outputs are on another volume."""
    try:
        os.replace(staged, destination)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV and getattr(exc, "winerror", None) != 17:
            raise

    destination.parent.mkdir(parents=True, exist_ok=True)
    local_stage = destination.parent / f".{destination.name}.stage-{uuid.uuid4().hex}"
    try:
        shutil.copy2(staged, local_stage)
        os.replace(local_stage, destination)
        staged.unlink()
    finally:
        local_stage.unlink(missing_ok=True)


@dataclass(frozen=True)
class RScriptLangFragment:
    """Validated UTF-16 dialog fragment emitted by RScript 4.10f.

    The compiler output is a flat ``number=value`` fragment, not a complete
    BlockPar document and therefore not a game-ready ``Lang.dat`` by itself.
    ``incomplete`` means that RScript exported CT/code placeholders recovered
    without dialog text rather than actual localized strings. ``invalid``
    identifies replacement characters, controls, or text outside the game's
    CP1251 language contract.
    """

    path: str
    status: str
    entries: tuple[tuple[str, str], ...]
    placeholder_keys: tuple[str, ...] = ()
    empty_keys: tuple[str, ...] = ()
    invalid_text_keys: tuple[str, ...] = ()
    referenced_ct_keys: tuple[str, ...] = ()

    @property
    def usable_for_game_language(self) -> bool:
        return self.status in {"complete", "empty"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "srhd-modkit-rscript-lang-fragment-v1",
            "path": self.path,
            "format": "rscript-dialog-fragment",
            "encoding": "utf-16-le-bom",
            "status": self.status,
            "entries": len(self.entries),
            "keys": [key for key, _value in self.entries],
            "placeholder_keys": list(self.placeholder_keys),
            "empty_keys": list(self.empty_keys),
            "invalid_text_keys": list(self.invalid_text_keys),
            "referenced_ct_keys": list(self.referenced_ct_keys),
            "usable_for_game_language": self.usable_for_game_language,
            "game_dat_ready": False,
        }


_RSCRIPT_LANG_PLACEHOLDER_RE = re.compile(
    r"(?:"
    r"Script\.[A-Za-z0-9_.-]+\.\d+"
    r"|CT\s*\(.*\)\s*;?"
    r"|(?:DAnswer|DText|Format)\s*\(.*\)\s*;?"
    r")",
    re.IGNORECASE,
)
_SCRIPT_CT_KEY_RE = re.compile(r"Script\.([A-Za-z0-9_.-]+)\.(\d+)", re.IGNORECASE)
_SCRIPT_LANG_CODE_STUB_RE = re.compile(
    r"^\s*(?:DAnswer|DText|CT|Format)\s*\(",
    re.IGNORECASE,
)


def inspect_rscript_lang_fragment(path: str | Path) -> RScriptLangFragment:
    """Parse and classify the flat language fragment produced by RScript."""

    source = Path(path).resolve()
    raw = source.read_bytes()
    if not raw.startswith(EMPTY_RSCRIPT_LANG_DAT):
        raise ValueError("Языковой вывод RScript не имеет обязательного UTF-16LE BOM FF FE")
    if len(raw) % 2:
        raise ValueError("Языковой вывод RScript имеет нечётную длину UTF-16LE")
    try:
        text = raw[len(EMPTY_RSCRIPT_LANG_DAT) :].decode("utf-16-le")
    except UnicodeDecodeError as exc:
        raise ValueError("Языковой вывод RScript повреждён как UTF-16LE") from exc

    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    placeholders: list[str] = []
    empty: list[str] = []
    invalid_text: list[str] = []
    referenced: set[str] = set()
    for number, line in enumerate(text.splitlines(), start=1):
        if not line:
            continue
        if "=" not in line:
            raise ValueError(f"Некорректная строка языкового фрагмента RScript #{number}: нет '='")
        key, value = line.split("=", 1)
        if not re.fullmatch(r"\d+", key):
            raise ValueError(
                f"Некорректный ключ языкового фрагмента RScript в строке {number}: {key!r}"
            )
        if key in seen:
            raise ValueError(f"Дублирующийся ключ языкового фрагмента RScript: {key}")
        seen.add(key)
        entries.append((key, value))
        stripped = value.strip()
        if not stripped:
            empty.append(key)
        try:
            value.encode("cp1251", errors="strict")
        except UnicodeEncodeError:
            invalid_text.append(key)
        else:
            if "\ufffd" in value or any(
                ord(character) < 0x20 and character not in {"\t"}
                for character in value
            ):
                invalid_text.append(key)
        if _RSCRIPT_LANG_PLACEHOLDER_RE.fullmatch(stripped):
            placeholders.append(key)
        for match in _SCRIPT_CT_KEY_RE.finditer(value):
            referenced.add(f"Script.{match.group(1)}.{match.group(2)}")

    status = (
        "empty"
        if not entries
        else "invalid"
        if invalid_text
        else "incomplete"
        if placeholders or empty
        else "complete"
    )
    return RScriptLangFragment(
        str(source),
        status,
        tuple(entries),
        tuple(placeholders),
        tuple(empty),
        tuple(invalid_text),
        tuple(sorted(referenced, key=str.casefold)),
    )


def _reject_invalid_rscript_lang_fragment(fragment: RScriptLangFragment) -> None:
    if fragment.status != "invalid":
        return
    preview = ", ".join(fragment.invalid_text_keys[:12])
    suffix = "…" if len(fragment.invalid_text_keys) > 12 else ""
    raise ValueError(
        "Языковой вывод RScript содержит повреждённый или не совместимый с "
        f"CP1251 текст в ключах {preview}{suffix}"
    )


def _iter_string_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _iter_string_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_string_values(child)


def _project_script_language_keys(project: Any, script_name: str) -> set[str]:
    """Return numeric Script.<name> CT keys referenced anywhere in RSON."""

    result: set[str] = set()
    target = script_name.casefold()
    for value in _iter_string_values(project.data):
        for match in _SCRIPT_CT_KEY_RE.finditer(value):
            if match.group(1).casefold() == target:
                result.add(match.group(2))
    return result


def _blockpar_inline_comment_risk(document: BlockParDocument) -> tuple[str, str] | None:
    """Return the first value that BlockParEditor would truncate at ``//``."""

    def walk(node: BlockParNode, prefix: str) -> tuple[str, str] | None:
        location = f"{prefix}/{node.name}" if prefix else node.name
        for parameter in node.parameters:
            if "//" in parameter.value:
                return f"{location}/{parameter.key}", parameter.value
        for child in node.children:
            found = walk(child, location)
            if found is not None:
                return found
        return None

    for root in document.roots:
        found = walk(root, "")
        if found is not None:
            return found
    return None


@dataclass(frozen=True)
class Tool:
    name: str
    path: Path
    purpose: str
    automatic: bool
    version: str | None = None
    compatibility: str | None = None

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["path"] = str(self.path)
        value["available"] = self.path.is_file()
        value["size"] = self.path.stat().st_size if self.path.is_file() else None
        return value


@dataclass(frozen=True)
class ConversionRecommendation:
    code: str
    message: str


@dataclass(frozen=True)
class ConversionItem:
    source: Path
    destination: Path
    source_sha256: str
    destination_sha256: str
    destination_size: int
    recommendations: tuple[ConversionRecommendation, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["source"] = str(self.source)
        value["destination"] = str(self.destination)
        return value


def _items_useless_destination(path: Path) -> bool:
    folded = tuple(part.casefold() for part in path.parts)
    return any(
        folded[index : index + 2] == ("data", "itemsuseless")
        for index in range(len(folded) - 1)
    )


class Toolchain:
    def __init__(self, tools_root: str | Path | None = None):
        if tools_root is None:
            tools_root = Path(__file__).resolve().parents[2]
        self.tools_root = Path(tools_root).resolve()
        blockpar_root = self.tools_root / "BlockParEditor"
        blockpar_original = blockpar_root / "BlockParEditor.exe"
        blockpar_codec = blockpar_root / "BlockParEditor.Legacy.exe"
        self.blockpar_version = detect_executable_version(blockpar_original)
        if blockpar_original.is_file() and not (
            self.blockpar_version is not None
            and self.blockpar_version.at_least(2, 0)
        ):
            ensure_legacy_codepage_executable(blockpar_original, blockpar_codec)
        blockpar_path = (
            blockpar_original
            if self.blockpar_version is not None
            and self.blockpar_version.at_least(2, 0)
            else blockpar_codec
        )
        rscript_path = self.tools_root / "RScript" / "RScript.exe"
        self.rscript_version = detect_executable_version(rscript_path)
        rscript410_path = self.tools_root / "RScript410" / "RScript.exe"
        self.rscript410_version = detect_executable_version(rscript410_path)
        rscript_label = self._rscript_version_label()
        blockpar_label = self._blockpar_version_label()
        self.tools = {
            "blockpar": Tool(
                "blockpar",
                blockpar_path,
                (
                    f"DAT/BlockPar {blockpar_label} без GUI"
                    + (
                        " с нативной Unicode-поддержкой"
                        if self.blockpar_version is not None
                        and self.blockpar_version.at_least(2, 0)
                        else " с локальной CP1251-совместимостью"
                    )
                ),
                True,
                blockpar_label,
                (
                    "native-unicode"
                    if self.blockpar_version is not None
                    and self.blockpar_version.at_least(2, 0)
                    else "legacy-cp1251"
                ),
            ),
            "reseditor": Tool(
                "reseditor",
                self.tools_root / "ResEditor" / "ResEditor_hai128.exe",
                "Редактирование GAI, HAI, PKG и ресурсов",
                False,
            ),
            "rscript": Tool(
                "rscript",
                rscript_path,
                (
                    "Headless-проверка, декомпиляция и компиляция "
                    f"RSON/SCR через RScript {rscript_label}"
                ),
                True,
                rscript_label,
                self._rscript_cli_profile(),
            ),
            "rsmc": Tool(
                "rsmc",
                self.tools_root / "RSMCompiler" / "rsmc.exe",
                "Консольная сборка модульных RSM-проектов в SCR",
                True,
                None,
                "rsm-build",
            ),
            "rscript410": Tool(
                "rscript410",
                rscript410_path,
                "Необязательный legacy-конвертер RSON/SVR через RScript 4.10f",
                True,
                (
                    f"{self.rscript410_version.major}.{self.rscript410_version.minor}f"
                    if self.rscript410_version is not None
                    else None
                ),
                "legacy-svr-convert",
            ),
            "shipviewer": Tool(
                "shipviewer",
                self.tools_root / "ShipViewer" / "RShip.exe",
                "Просмотр кораблей и связанных ресурсов",
                False,
            ),
        }

    def status(self) -> list[dict[str, Any]]:
        return [tool.as_dict() for tool in self.tools.values()]

    def require(self, name: str) -> Tool:
        tool = self.tools[name]
        if not tool.path.is_file():
            raise FileNotFoundError(f"Инструмент не найден: {tool.path}")
        return tool

    @staticmethod
    def _collect(inputs: Iterable[str | Path], extension: str) -> list[tuple[Path, Path]]:
        result: list[tuple[Path, Path]] = []
        for raw in inputs:
            path = Path(raw).resolve()
            if path.is_file():
                if path.suffix.casefold() != extension:
                    raise ValueError(f"Ожидался файл {extension}: {path}")
                result.append((path, Path(path.name)))
            elif path.is_dir():
                matches = sorted(
                    (item for item in path.rglob("*") if item.is_file() and item.suffix.casefold() == extension),
                    key=lambda item: item.relative_to(path).as_posix().casefold(),
                )
                result.extend((item, item.relative_to(path)) for item in matches)
            else:
                raise FileNotFoundError(path)
        if not result:
            raise ValueError(f"Не найдено файлов {extension}")
        return result

    def convert(
        self,
        inputs: Iterable[str | Path],
        output_dir: str | Path,
        *,
        direction: str,
        gi_mode: str = "0_32",
        overwrite: bool = False,
    ) -> list[ConversionItem]:
        if direction not in {"gi-png", "png-gi"}:
            raise ValueError(f"Неизвестное направление: {direction}")
        if gi_mode not in {"0_32", "0_16", "2"}:
            raise ValueError("Режим GI должен быть 0_32, 0_16 или 2")
        source_ext, target_ext = (".gi", ".png") if direction == "gi-png" else (".png", ".gi")
        sources = self._collect(inputs, source_ext)
        output_dir = Path(output_dir).resolve()
        destinations = [(source, output_dir / relative.with_suffix(target_ext)) for source, relative in sources]
        normalized = [os.path.normcase(str(destination)) for _, destination in destinations]
        if len(normalized) != len(set(normalized)):
            raise FileExistsError("Несколько входных файлов дают один и тот же путь результата")
        existing = [destination for _, destination in destinations if destination.exists()]
        if existing and not overwrite:
            preview = ", ".join(str(path) for path in existing[:3])
            raise FileExistsError(f"Результат уже существует (используйте --overwrite): {preview}")

        output_dir.parent.mkdir(parents=True, exist_ok=True)
        converted: list[ConversionItem] = []
        with tempfile.TemporaryDirectory(prefix=".srhd-convert-", dir=output_dir.parent) as temp_name:
            stage_root = Path(temp_name)
            staged: list[tuple[Path, Path, Path]] = []
            for source, destination in destinations:
                relative = destination.relative_to(output_dir)
                stage_dir = (stage_root / relative.parent)
                stage_dir.mkdir(parents=True, exist_ok=True)
                stage_file = stage_dir / source.with_suffix(target_ext).name
                try:
                    if direction == "gi-png":
                        source_image = read_gi(source)
                        write_png(source_image, stage_file)
                        if read_png(stage_file) != source_image:
                            raise RuntimeError("PNG не прошёл пиксельную обратную проверку")
                    else:
                        source_image = read_png(source)
                        write_gi(source_image, stage_file, gi_mode)
                        rebuilt = read_gi(stage_file)
                        if (rebuilt.width, rebuilt.height) != (source_image.width, source_image.height):
                            raise RuntimeError("GI изменил размер изображения при обратной проверке")
                        if gi_mode == "0_32" and rebuilt != source_image:
                            raise RuntimeError("GI 0_32 не прошёл пиксельную обратную проверку")
                except Exception as exc:
                    raise RuntimeError(f"Нативный GI/PNG-кодек не обработал {source}: {exc}") from exc
                inspected = inspect_file(stage_file)
                if inspected["signature_valid"] is False:
                    raise RuntimeError(f"Неверная сигнатура результата: {stage_file}")
                staged.append((source, destination, stage_file))

            # Commit only after the entire batch has converted and validated.
            for source, destination, stage_file in staged:
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(stage_file, destination)
                recommendations: tuple[ConversionRecommendation, ...] = ()
                if (
                    direction == "png-gi"
                    and gi_mode != "2"
                    and _items_useless_destination(destination)
                ):
                    recommendations = (
                        ConversionRecommendation(
                            "gi-items-useless-mode-2-recommended",
                            "Для предметной иконки в DATA\\ItemsUseless рекомендуется "
                            "--mode 2: штатная трёхслойная упаковка лучше совместима "
                            "с уменьшенными карточками интерфейса. Выбранный режим "
                            f"{gi_mode} сохранён; редкие форматы не запрещены.",
                        ),
                    )
                converted.append(
                    ConversionItem(
                        source=source,
                        destination=destination,
                        source_sha256=sha256_file(source),
                        destination_sha256=sha256_file(destination),
                        destination_size=destination.stat().st_size,
                        recommendations=recommendations,
                    )
                )
        return converted

    def open_editor(self, path: str | Path, *, allow_gui: bool = False) -> dict[str, str]:
        if not allow_gui or os.environ.get("SRHD_MODKIT_ALLOW_GUI") != "1":
            raise PermissionError(
                "GUI отключён. Для осознанного ручного запуска нужны одновременно "
                "--allow-gui и SRHD_MODKIT_ALLOW_GUI=1"
            )
        path = Path(path).resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        extension = path.suffix.casefold()
        tool_name = {
            ".dat": "blockpar",
            ".gai": "reseditor",
            ".hai": "reseditor",
            ".pkg": "reseditor",
            ".gi": "reseditor",
            ".scr": "rscript",
        }.get(extension)
        if tool_name is None:
            raise ValueError(f"Для {extension or 'файла без расширения'} штатный редактор не назначен")
        tool = self.require(tool_name)
        subprocess.Popen([str(tool.path), str(path)], cwd=tool.path.parent)
        note = None
        if extension == ".dat":
            note = "Если файл не открылся автоматически: нажмите Open dat, затем раскрывайте блоки стрелкой слева."
        return {"file": str(path), "tool": tool.name, "executable": str(tool.path), "note": note}

    def convert_dat(
        self,
        source: str | Path,
        destination: str | Path,
        *,
        overwrite: bool = False,
        verify: bool = True,
    ) -> dict[str, Any]:
        source = Path(source).resolve()
        destination = Path(destination).resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        expected = {".dat": ".txt", ".txt": ".dat"}.get(source.suffix.casefold())
        if expected is None or destination.suffix.casefold() != expected:
            raise ValueError("BlockPar конвертируется только DAT -> TXT или TXT -> DAT")
        if destination.exists() and not overwrite:
            raise FileExistsError(f"Результат уже существует: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)

        if source.suffix.casefold() == ".dat" and is_empty_rscript_lang_dat(source):
            destination.write_bytes(b"")
            return {
                "source": str(source),
                "destination": str(destination),
                "source_sha256": sha256_file(source),
                "destination_sha256": sha256_file(destination),
                "verified": True,
                "format": "rscript-empty-lang-dat",
                "encoding": "utf-16-le-bom",
            }

        tool = self.require("blockpar")

        source_document = load_blockpar(source) if source.suffix.casefold() == ".txt" else None
        if source_document is not None:
            inline_comment = _blockpar_inline_comment_risk(source_document)
            if inline_comment is not None:
                location, value = inline_comment
                raise ValueError(
                    "blockpar-inline-comment-truncation-risk: BlockParEditor 1.9/2.1 "
                    "считает // началом комментария и обрежет значение при TXT -> DAT; "
                    f"{location}={value!r}"
                )
        with tempfile.TemporaryDirectory(prefix=".srhd-dat-", dir=destination.parent) as temp_name:
            temp = Path(temp_name)
            staged_source = temp / source.name
            staged_destination = temp / destination.name
            if source_document is not None:
                # BlockPar 2.1 is Unicode-native and must receive Unicode text;
                # feeding it the legacy CP1251 transport changes Russian values.
                # BlockPar 1.9 remains on the isolated CP1251 compatibility EXE.
                # In both cases the game-facing payload is still limited to
                # Windows-1251, so reject unrepresentable glyphs first.
                transport_encoding = (
                    "utf-8"
                    if self.blockpar_version is not None
                    and self.blockpar_version.at_least(2, 0)
                    else "cp1251"
                )
                game_text = source_document.to_text(include_raw=False)
                try:
                    game_text.encode("cp1251")
                except UnicodeEncodeError as exc:
                    bad = game_text[exc.start : max(exc.end, exc.start + 1)]
                    raise ValueError(
                        "BlockPar-текст нельзя передать игре как Windows-1251: "
                        f"{bad!r} (U+{ord(bad[0]):04X})"
                    ) from exc
                try:
                    source_document.save(
                        staged_source,
                        encoding=transport_encoding,
                        include_raw=False,
                        bom=False,
                    )
                except UnicodeEncodeError as exc:
                    bad = game_text[exc.start : max(exc.end, exc.start + 1)]
                    raise ValueError(
                        "BlockPar-текст нельзя передать игре как Windows-1251: "
                        f"{bad!r} (U+{ord(bad[0]):04X})"
                    ) from exc
            else:
                shutil.copy2(source, staged_source)

            completed = run_on_hidden_desktop(
                tool.path,
                ["--cli", "--convert", str(staged_source), str(staged_destination)],
                cwd=tool.path.parent,
                expected_outputs=[staged_destination],
                timeout=30,
                settle_seconds=0.5,
                abort_window_patterns=("Run-time error", "Runtime error", "Overflow"),
            )
            if not staged_destination.is_file():
                raise RuntimeError(f"BlockParEditor CLI не создал результат (код {completed.exit_code})")

            verified = False
            if source.suffix.casefold() == ".dat":
                load_blockpar(staged_destination)
                verified = True
            elif verify:
                check_txt = temp / f"{destination.stem}.verified.txt"
                check = run_on_hidden_desktop(
                    tool.path,
                    ["--cli", "--convert", str(staged_destination), str(check_txt)],
                    cwd=tool.path.parent,
                    expected_outputs=[check_txt],
                    timeout=30,
                    settle_seconds=0.5,
                    abort_window_patterns=("Run-time error", "Runtime error", "Overflow"),
                )
                if not check_txt.is_file():
                    raise RuntimeError("Не удалось проверить собранный DAT обратной конвертацией")
                if load_blockpar(check_txt).canonical_semantic() != source_document.canonical_semantic():
                    raise RuntimeError("Собранный DAT не совпал с исходным деревом BlockPar")
                verified = True
            os.replace(staged_destination, destination)

        return {
            "source": str(source),
            "destination": str(destination),
            "source_sha256": sha256_file(source),
            "destination_sha256": sha256_file(destination),
            "verified": verified,
        }

    def _compile_rson_with_rscript(
        self,
        source: Path,
        scr_output: Path,
        lang_output: Path,
        *,
        timeout: float | None = None,
    ) -> tuple[Any, dict[str, Any], dict[str, Any]]:
        """Run the RScript compiler after callers perform their own policy checks."""

        tool, cli_profile = self._require_supported_rscript("compile")
        timeout_seconds, timeout_policy = _rscript_timeout_policy(source, "compile", timeout)
        scr_output.parent.mkdir(parents=True, exist_ok=True)
        lang_output.parent.mkdir(parents=True, exist_ok=True)
        common_parent = scr_output.parent
        with tempfile.TemporaryDirectory(prefix=".srhd-script-", dir=common_parent) as temp_name:
            temp = Path(temp_name)
            staged_source = temp / source.name
            staged_scr = temp / scr_output.name
            staged_lang = temp / lang_output.name
            shutil.copy2(source, staged_source)
            process_result = None
            compiler_started = time.monotonic()
            try:
                arguments = (
                    [
                        "--cli",
                        "-b",
                        str(staged_source),
                        str(staged_scr),
                        str(staged_lang),
                        "--full",
                    ]
                    if cli_profile == "modern-cli"
                    else [
                        "--cli",
                        "--build",
                        "--full",
                        str(staged_source),
                        str(staged_scr),
                        str(staged_lang),
                    ]
                )
                process_result = run_on_hidden_desktop(
                    tool.path,
                    arguments,
                    cwd=tool.path.parent,
                    timeout=timeout_seconds,
                    expected_outputs=[staged_scr, staged_lang],
                    progress_timeout=timeout_policy["progress_seconds"],
                    abort_window_patterns=("Run-time error", "Runtime error", "Error", "Ошибка"),
                )
                if not staged_scr.is_file():
                    raise RuntimeError(
                        f"RScript CLI не создал SCR (код {process_result.exit_code})"
                    )
                if not staged_lang.is_file():
                    raise RuntimeError(
                        "RScript CLI не создал языковой фрагмент "
                        f"(код {process_result.exit_code})"
                    )
                lang_fragment = inspect_rscript_lang_fragment(staged_lang)
                _reject_invalid_rscript_lang_fragment(lang_fragment)
                scr_info = inspect_scr(staged_scr)
                if not scr_info["supported_version"]:
                    raise RuntimeError(
                        f"RScript создал SCR неподдерживаемой версии {scr_info['version']}"
                    )
                scr_info["lang_fragment"] = lang_fragment.as_dict()
                _replace_cross_device_safe(staged_scr, scr_output)
                _replace_cross_device_safe(staged_lang, lang_output)
            except Exception as exc:
                diagnostic = _rscript_failure_diagnostic(exc, operation="compile")
                if diagnostic is None:
                    if isinstance(exc, TimeoutError):
                        code = "rscript-build-timeout"
                    elif not staged_scr.is_file():
                        code = "rscript-build-output-missing"
                    else:
                        code = "rscript-build-output-invalid"
                    diagnostic = {"code": code, "message": str(exc)}
                process_failure = (
                    exc.as_dict()
                    if callable(getattr(exc, "as_dict", None))
                    else None
                )
                if process_failure is not None:
                    diagnostic["process"] = process_failure
                elapsed = (
                    getattr(process_result, "elapsed_seconds", None)
                    if process_result is not None
                    else getattr(
                        exc,
                        "elapsed_seconds",
                        time.monotonic() - compiler_started,
                    )
                )
                report = {
                    "schema": "srhd-modkit-script-build-v1",
                    "status": "failed",
                    "source": str(source),
                    "source_sha256": sha256_file(source),
                    "preflight_passed": True,
                    "compiler_started": True,
                    "compiler_output_created": staged_scr.is_file(),
                    "language_output_created": staged_lang.is_file(),
                    "published_outputs": False,
                    "compiler": {
                        "name": "RScript",
                        "version": self._rscript_version_label(),
                        "cli_profile": cli_profile,
                        "executable": str(tool.path),
                        "exit_code": (
                            getattr(process_result, "exit_code", None)
                            if process_result is not None
                            else getattr(exc, "exit_code", None)
                        ),
                        "seconds": round(float(elapsed), 3),
                        "progress_updates": (
                            getattr(process_result, "progress_updates", None)
                            if process_result is not None
                            else getattr(exc, "progress_updates", None)
                        ),
                        "last_progress_seconds": (
                            round(
                                float(
                                    getattr(
                                        process_result,
                                        "last_progress_seconds",
                                        0.0,
                                    )
                                ),
                                3,
                            )
                            if process_result is not None
                            else round(
                                float(getattr(exc, "last_progress_seconds", 0.0)),
                                3,
                            )
                        ),
                        "timeout": timeout_policy,
                    },
                    "failure": diagnostic,
                }
                raise ScriptBuildFailure(str(exc), report) from exc
        return process_result, scr_info, timeout_policy

    def _load_script_lang_base(
        self,
        source: Path,
        workspace: Path,
    ) -> tuple[BlockParDocument, Path]:
        if not source.is_file():
            raise FileNotFoundError(source)
        if source.suffix.casefold() == ".txt":
            return load_blockpar(source), source
        if source.suffix.casefold() != ".dat":
            raise ValueError("Базовый язык должен быть BlockPar TXT или Lang.dat")
        decoded = workspace / "lang-base.decoded.txt"
        self.convert_dat(source, decoded)
        return load_blockpar(decoded), decoded

    @staticmethod
    def _script_lang_node(document: BlockParDocument, script_name: str) -> BlockParNode:
        try:
            return document.find_node(f"Script/{script_name}")
        except KeyError as exc:
            raise ValueError(
                f"Базовый язык не содержит узел Script/{script_name}"
            ) from exc

    def _prepare_script_lang_dat(
        self,
        project: Any,
        fragment: RScriptLangFragment,
        destination: Path,
        workspace: Path,
        *,
        base: Path | None,
    ) -> dict[str, Any]:
        """Create or preserve a verified game Lang.dat in staging."""

        script_name = str(project.summary().get("name", "")).strip()
        if not script_name or re.search(r"[\\/\r\n{}]", script_name):
            raise ValueError(f"Некорректное имя скрипта для Lang.dat: {script_name!r}")

        base_document: BlockParDocument | None = None
        base_source: Path | None = None
        if base is not None:
            base_document, base_source = self._load_script_lang_base(base, workspace)

        if fragment.status == "invalid":
            preview = ", ".join(fragment.invalid_text_keys[:12])
            suffix = "…" if len(fragment.invalid_text_keys) > 12 else ""
            raise ValueError(
                "RScript создал языковой фрагмент с повреждённым или не совместимым "
                f"с CP1251 текстом в ключах {preview}{suffix}; игровой Lang.dat не создан"
            )

        if fragment.status == "incomplete":
            if base_document is None or base is None:
                raise ValueError(
                    "RScript создал неполный языковой фрагмент с CT/code-заглушками. "
                    "Для игрового Lang.dat передайте --lang-base с проверенным прежним "
                    "Lang.dat/TXT либо сначала восстановите RSON с импортом диалогов"
                )
            node = self._script_lang_node(base_document, script_name)
            available = {parameter.key for parameter in node.parameters}
            required = _project_script_language_keys(project, script_name)
            required.update(
                key
                for key, value in fragment.entries
                if _SCRIPT_LANG_CODE_STUB_RE.match(value)
                and not _SCRIPT_CT_KEY_RE.search(value)
            )
            missing = sorted(required - available, key=lambda value: int(value))
            if missing:
                preview = ", ".join(missing[:12])
                suffix = "…" if len(missing) > 12 else ""
                raise ValueError(
                    f"Базовый Lang.dat не покрывает Script/{script_name}: "
                    f"отсутствуют ключи {preview}{suffix}"
                )
            code_stubs = sorted(
                (
                    parameter.key
                    for parameter in node.parameters
                    if parameter.key.isdecimal()
                    and _SCRIPT_LANG_CODE_STUB_RE.match(parameter.value)
                ),
                key=int,
            )
            if code_stubs:
                preview = ", ".join(code_stubs[:12])
                suffix = "…" if len(code_stubs) > 12 else ""
                raise ValueError(
                    f"Базовый Lang.dat содержит RScript-код вместо видимого текста "
                    f"в Script/{script_name}: ключи {preview}{suffix}"
                )
            if base.suffix.casefold() == ".dat":
                shutil.copy2(base, destination)
            else:
                self.convert_dat(base_source, destination)
            return {
                "status": "verified",
                "mode": "preserved-base",
                "script_node": f"Script/{script_name}",
                "entries": len(node.parameters),
                "required_keys": len(required),
                "base": str(base),
                "base_sha256": sha256_file(base),
                "reason": "rson-dialog-text-not-imported",
            }

        if fragment.status == "empty":
            if base_document is not None and base is not None:
                # A no-dialog rebuild must not erase unrelated language data.
                if base.suffix.casefold() == ".dat":
                    shutil.copy2(base, destination)
                else:
                    self.convert_dat(base_source, destination)
                return {
                    "status": "verified",
                    "mode": "preserved-base",
                    "script_node": f"Script/{script_name}",
                    "entries": 0,
                    "required_keys": 0,
                    "base": str(base),
                    "base_sha256": sha256_file(base),
                    "reason": "rscript-empty-dialog-fragment",
                }

        required = _project_script_language_keys(project, script_name)
        fragment_keys = {key for key, _value in fragment.entries}
        missing_fragment = sorted(required - fragment_keys, key=lambda value: int(value))
        if missing_fragment:
            preview = ", ".join(missing_fragment[:12])
            suffix = "…" if len(missing_fragment) > 12 else ""
            raise ValueError(
                f"Языковой фрагмент RScript не покрывает Script/{script_name}: "
                f"отсутствуют ключи {preview}{suffix}. Используйте импорт диалогов "
                "или проверенный --lang-base"
            )

        if base_document is None:
            base_document = parse_blockpar(
                f"Script ^{{\r\n    {script_name} ~{{\r\n    }}\r\n}}\r\n",
                encoding="cp1251",
            )
            node = base_document.find_node(f"Script/{script_name}")
            mode = "generated-empty" if fragment.status == "empty" else "generated"
        else:
            try:
                node = base_document.find_node(f"Script/{script_name}")
            except KeyError:
                try:
                    base_document.find_node("Script")
                except KeyError:
                    base_document.ensure_node("Script", operator="^")
                node = base_document.add_node("Script", script_name, operator="~")
            mode = "merged-base"

        node.entries = [
            entry
            for entry in node.entries
            if not isinstance(entry, BlockParParameter) or not entry.key.isdecimal()
        ]
        indent = node.indent + "    "
        node.entries.extend(
            BlockParParameter(key=key, value=value, indent=indent, modified=True)
            for key, value in fragment.entries
        )
        blockpar_source = workspace / "lang.generated.txt"
        base_document.save(
            blockpar_source,
            encoding="cp1251",
            include_raw=False,
            bom=False,
        )
        self.convert_dat(blockpar_source, destination, verify=True)
        return {
            "status": "verified",
            "mode": mode,
            "script_node": f"Script/{script_name}",
            "entries": len(fragment.entries),
            "required_keys": len(required),
            "base": str(base) if base is not None else None,
            "base_sha256": sha256_file(base) if base is not None else None,
            "reason": (
                "script-has-no-exported-dialog-text"
                if fragment.status == "empty"
                else "complete-rscript-dialog-fragment"
            ),
        }

    def _rscript_version_label(self) -> str:
        if self.rscript_version is None:
            return "неизвестной версии"
        return f"{self.rscript_version.major}.{self.rscript_version.minor}f"

    def _blockpar_version_label(self) -> str:
        if self.blockpar_version is None:
            return "неизвестной версии"
        return self.blockpar_version.dotted(components=2)

    def _rscript_cli_profile(self) -> str:
        version = self.rscript_version
        if version is None:
            return "undetected-cli"
        if version.parts[:2] == (4, 15):
            return "modern-cli"
        if version.parts[:2] == (4, 10):
            return "legacy-cli"
        return "unsupported-cli"

    def _require_supported_rscript(self, operation: str) -> tuple[Tool, str]:
        tool = self.require("rscript")
        profile = self._rscript_cli_profile()
        if profile in {"unsupported-cli", "undetected-cli"}:
            raise RuntimeError(
                f"RScript {self._rscript_version_label()} не входит в проверенную "
                "матрицу CLI. Поддерживаются точно 4.10f и 4.15f. "
                "Установите штатную 4.15f либо сохраните 4.10f."
            )
        if operation in {"export-rsm", "cli-decompile"} and profile not in {
            "modern-cli",
        }:
            raise RuntimeError(
                f"Операция {operation} требует RScript 4.15f; обнаружен "
                f"{self._rscript_version_label()}. RScript 4.10f остаётся "
                "поддержан для RSON/SCR-сборки и legacy-декомпиляции."
            )
        return tool, profile

    def compile_rson(
        self,
        source: str | Path,
        scr_output: str | Path,
        lang_output: str | Path,
        *,
        lang_dat_output: str | Path | None = None,
        lang_base: str | Path | None = None,
        overwrite: bool = False,
        timeout: float | None = None,
        check_custom_factions: bool = True,
    ) -> dict[str, Any]:
        source = Path(source).resolve()
        scr_output = Path(scr_output).resolve()
        requested_lang_output = Path(lang_output).resolve()
        requested_lang_dat = Path(lang_dat_output).resolve() if lang_dat_output is not None else None
        lang_base_path = Path(lang_base).resolve() if lang_base is not None else None
        if requested_lang_output.suffix.casefold() == ".dat":
            if requested_lang_dat is not None and requested_lang_dat != requested_lang_output:
                raise ValueError("Нельзя задать два разных игровых Lang.dat через --lang и --lang-dat")
            requested_lang_dat = requested_lang_output
            fragment_output: Path | None = None
        else:
            fragment_output = requested_lang_output
        if requested_lang_dat is not None and requested_lang_dat.suffix.casefold() != ".dat":
            raise ValueError("Игровой языковой результат --lang-dat должен иметь расширение .dat")
        if lang_base_path is not None and requested_lang_dat is None:
            raise ValueError("--lang-base используется только вместе с игровым --lang-dat")
        if source.suffix.casefold() != ".rson":
            raise ValueError("Компилятор принимает проект .rson")
        project = load_rson(source)
        issues = project.validate()
        errors = [issue for issue in issues if issue.severity == "error"]
        if errors:
            raise ValueError("RSON не прошёл проверку: " + "; ".join(issue.message for issue in errors[:5]))
        runtime_issues = lint_rson_runtime(
            project,
            check_custom_factions=check_custom_factions,
        )
        runtime_errors = [issue for issue in runtime_issues if issue.severity == "error"]
        if runtime_errors:
            raise ValueError(
                "RSON не прошёл runtime-lint: "
                + "; ".join(f"{issue.code}: {issue.message}" for issue in runtime_errors[:5])
            )
        destinations = [scr_output]
        if fragment_output is not None:
            destinations.append(fragment_output)
        if requested_lang_dat is not None:
            destinations.append(requested_lang_dat)
        folded_destinations = [str(path).casefold() for path in destinations]
        if len(set(folded_destinations)) != len(folded_destinations):
            raise ValueError("SCR, языковой фрагмент и Lang.dat должны иметь разные пути")
        existing = [path for path in destinations if path.exists()]
        if existing and not overwrite:
            raise FileExistsError(f"Результат уже существует: {existing[0]}")
        scr_output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".srhd-script-build-",
            dir=scr_output.parent,
        ) as temp_name:
            transaction = Path(temp_name)
            staged_scr = transaction / "compiled.scr"
            staged_fragment = transaction / "compiled.lang.txt"
            process_result, _scr_info, timeout_policy = self._compile_rson_with_rscript(
                source,
                staged_scr,
                staged_fragment,
                timeout=timeout,
            )
            fragment = inspect_rscript_lang_fragment(staged_fragment)
            _reject_invalid_rscript_lang_fragment(fragment)
            fragment_sha256 = sha256_file(staged_fragment)
            game_lang: dict[str, Any] | None = None
            staged_lang_dat: Path | None = None
            if requested_lang_dat is not None:
                staged_lang_dat = transaction / "compiled.lang.dat"
                game_lang = self._prepare_script_lang_dat(
                    project,
                    fragment,
                    staged_lang_dat,
                    transaction,
                    base=lang_base_path,
                )

            # Nothing reaches caller-visible paths until every requested
            # language artifact has been parsed and, for DAT, round-tripped.
            _replace_cross_device_safe(staged_scr, scr_output)
            if fragment_output is not None:
                fragment_output.parent.mkdir(parents=True, exist_ok=True)
                _replace_cross_device_safe(staged_fragment, fragment_output)
            if requested_lang_dat is not None and staged_lang_dat is not None:
                requested_lang_dat.parent.mkdir(parents=True, exist_ok=True)
                _replace_cross_device_safe(staged_lang_dat, requested_lang_dat)

        fragment_result = fragment.as_dict()
        fragment_result["path"] = str(fragment_output) if fragment_output is not None else None
        fragment_result["sha256"] = fragment_sha256
        if game_lang is not None and requested_lang_dat is not None:
            game_lang["path"] = str(requested_lang_dat)
            game_lang["sha256"] = sha256_file(requested_lang_dat)
            game_lang["size"] = requested_lang_dat.stat().st_size
        legacy_lang = fragment_output or requested_lang_dat
        assert legacy_lang is not None
        language_warnings: list[dict[str, Any]] = []
        if (
            requested_lang_dat is not None
            and [part.casefold() for part in requested_lang_dat.parts[-3:]]
            == ["data", "script", "lang.dat"]
            and fragment.entries
        ):
            language_warnings.append(
                {
                    "severity": "warning",
                    "code": "rscript-lang-dat-nonruntime-path",
                    "message": (
                        "DATA/Script/Lang.dat пригоден как артефакт сборки или "
                        "импорта RScript, но не доказывает загрузку текста игрой. "
                        "Для статических TDialogAnswer опубликуйте проверенные "
                        "ключи Script/<имя>/<номер> в CFG/<язык>/Lang.dat"
                    ),
                    "path": str(requested_lang_dat),
                }
            )
        if fragment.status == "incomplete":
            language_warnings.append(
                {
                    "severity": "warning",
                    "code": "rscript-lang-fragment-incomplete",
                    "message": (
                        "RSON не содержит импортированный текст части диалогов: RScript "
                        "вывел CT/code-заглушки. SCR собран, но языковой фрагмент нельзя "
                        "превращать в игровой Lang.dat без проверенной базы"
                    ),
                    "placeholder_keys": list(fragment.placeholder_keys),
                    "empty_keys": list(fragment.empty_keys),
                }
            )
        return {
            "schema": "srhd-modkit-script-build-v1",
            "status": "passed",
            "source": str(source),
            "scr": str(scr_output),
            "lang": str(legacy_lang),
            "scr_size": scr_output.stat().st_size,
            "scr_sha256": sha256_file(scr_output),
            "lang_sha256": sha256_file(legacy_lang),
            "language": {
                "fragment": fragment_result,
                "game_dat": game_lang,
                "warnings": language_warnings,
            },
            "compiler": {
                "name": "RScript",
                "version": self._rscript_version_label(),
                "cli_profile": self._rscript_cli_profile(),
                "executable": str(self.tools["rscript"].path),
            },
            "compiler_exit_code": process_result.exit_code,
            "preflight_passed": True,
            "compiler_started": True,
            "compiler_output_created": True,
            "language_output_created": True,
            "published_outputs": True,
            "compiler_was_waiting_after_output": process_result.forced_after_outputs,
            "compiler_seconds": round(process_result.elapsed_seconds, 3),
            "compiler_queue_seconds": round(getattr(process_result, "queue_seconds", 0.0), 3),
            "compiler_progress_updates": getattr(process_result, "progress_updates", 0),
            "compiler_last_progress_seconds": round(
                getattr(process_result, "last_progress_seconds", 0.0), 3
            ),
            "compiler_timeout": timeout_policy,
            "runtime_warnings": [
                issue.as_dict() for issue in runtime_issues if issue.severity == "warning"
            ],
        }

    def _recover_scr_with_rscript(
        self,
        source: Path,
        recovered: Path,
        *,
        lang_dat: Path | None,
        timeout: float | None,
    ) -> tuple[Any, dict[str, Any]]:
        """Recover RSON through the version-appropriate headless backend."""

        tool, cli_profile = self._require_supported_rscript("decompile")
        timeout_seconds, timeout_policy = _rscript_timeout_policy(source, "decompile", timeout)
        timeout_policy = {**timeout_policy, "backend": cli_profile}
        recovered.parent.mkdir(parents=True, exist_ok=True)
        if cli_profile == "modern-cli":
            arguments = ["--cli", "-d", str(source), str(recovered)]
            if lang_dat is not None:
                arguments.extend(["--langdat", str(lang_dat)])
            process_result = run_on_hidden_desktop(
                tool.path,
                arguments,
                cwd=tool.path.parent,
                expected_outputs=[recovered],
                timeout=timeout_seconds,
                progress_timeout=timeout_policy["progress_seconds"],
                abort_window_patterns=(
                    "Run-time error",
                    "Runtime error",
                    "Application Error",
                    "Access violation",
                    "Error",
                    "Ошибка",
                ),
            )
            if not recovered.is_file():
                raise RuntimeError(
                    "RScript modern CLI не создал восстановленный RSON "
                    f"(код {process_result.exit_code})"
                )
            return process_result, timeout_policy

        stem = f"_srhd_{uuid.uuid4().hex}"
        staged_scr = tool.path.parent / f"{stem}.scr"
        try:
            shutil.copy2(source, staged_scr)
            control_actions: list[HiddenControlAction] = []
            if lang_dat is not None:
                control_actions.extend(
                    [
                        HiddenControlAction(
                            parent_title="SCR decompilation",
                            button_text="Import dialogs from Lang.dat",
                            button_class="TCheckBox",
                            delay_seconds=3.0,
                        ),
                        HiddenControlAction(
                            parent_title="SCR decompilation",
                            button_class="TsFilenameEdit",
                            type_text=str(lang_dat),
                            delay_seconds=0.5,
                        ),
                    ]
                )
                save_delay = 0.5
            else:
                save_delay = 3.0
            control_actions.extend(
                [
                    HiddenControlAction(
                        parent_title="SCR decompilation",
                        button_text="Save RSON",
                        force_enable=True,
                        delay_seconds=save_delay,
                        confirm_parent_class="#32770",
                        retry_seconds=1.0,
                    ),
                    HiddenControlAction(
                        parent_class="#32770",
                        button_control_id=1001,
                        button_class="Edit",
                        type_text=str(recovered),
                        delay_seconds=0.5,
                    ),
                    HiddenControlAction(
                        parent_class="#32770",
                        button_control_id=1,
                        button_class="Button",
                        delay_seconds=0.5,
                    ),
                ]
            )
            process_result = run_on_hidden_desktop(
                tool.path,
                [staged_scr.name],
                cwd=tool.path.parent,
                expected_outputs=[recovered],
                timeout=timeout_seconds,
                progress_timeout=timeout_policy["progress_seconds"],
                abort_window_patterns=(
                    "Run-time error",
                    "Runtime error",
                    "Application Error",
                    "Access violation",
                ),
                control_actions=control_actions,
            )
            if not recovered.is_file():
                raise RuntimeError("RScript не создал восстановленный RSON")
            return process_result, timeout_policy
        finally:
            staged_scr.unlink(missing_ok=True)

    def decompile_scr(
        self,
        source: str | Path,
        destination: str | Path,
        *,
        lang_dat: str | Path | None = None,
        overwrite: bool = False,
        decompile_timeout: float | None = None,
        roundtrip_timeout: float | None = None,
        keep_unverified: str | Path | None = None,
        deep_roundtrip: bool = False,
        fallback_without_lang: bool = False,
    ) -> dict[str, Any]:
        """Recover RSON and publish it only after a fail-closed round trip."""

        source = Path(source).resolve()
        destination = Path(destination).resolve()
        if source.suffix.casefold() != ".scr":
            raise ValueError("Декомпилятор принимает только .scr")
        if destination.suffix.casefold() != ".rson":
            raise ValueError("Результат декомпиляции должен иметь расширение .rson")
        if not source.is_file():
            raise FileNotFoundError(source)
        if destination.exists() and not overwrite:
            raise FileExistsError(f"Результат уже существует: {destination}")
        unverified_destination = Path(keep_unverified).resolve() if keep_unverified is not None else None
        if unverified_destination is not None:
            if unverified_destination.suffix.casefold() != ".rson":
                raise ValueError("--keep-unverified должен указывать отдельный .rson")
            if unverified_destination == destination:
                raise ValueError("Непроверенный RSON нельзя сохранять по пути штатного результата")
            if unverified_destination.exists() and not overwrite:
                raise FileExistsError(f"Непроверенный результат уже существует: {unverified_destination}")
        requested_lang = Path(lang_dat).resolve() if lang_dat is not None else None
        resolved_lang = requested_lang
        lang_dat_skip_reason = None
        if requested_lang is not None:
            if requested_lang.suffix.casefold() != ".dat":
                raise ValueError("Файл диалогов должен иметь расширение .dat")
            if not requested_lang.is_file():
                raise FileNotFoundError(requested_lang)
            # An explicitly supplied dialog DAT containing only the UTF-16LE
            # BOM is semantically empty regardless of its staging path.  The
            # stricter DATA/Script/Lang.dat path rule remains in
            # is_empty_rscript_lang_dat() for generic DAT validation.
            if requested_lang.read_bytes() == EMPTY_RSCRIPT_LANG_DAT:
                resolved_lang = None
                lang_dat_skip_reason = "empty-rscript-lang-dat"
        source_info = inspect_scr(source)
        if not source_info["supported_version"]:
            raise ValueError(
                f"RScript {self._rscript_version_label()} не поддерживает "
                f"SCR версии {source_info['version']}"
            )

        destination.parent.mkdir(parents=True, exist_ok=True)
        stale_transactions_removed = _cleanup_stale_decompile_transactions(destination.parent)
        transaction = destination.parent / f".srhd-decompile-{uuid.uuid4().hex}"
        transaction.mkdir()
        (transaction / ".srhd-transaction").write_text("decompile-v1\n", encoding="ascii")
        recovered = transaction / "recovered.rson"
        phases: list[dict[str, Any]] = [
            {"name": "inspect-source", "status": "passed", "seconds": 0.0}
        ]
        if lang_dat_skip_reason is not None:
            phases.append(
                {
                    "name": "import-dialogs",
                    "status": "skipped",
                    "reason": lang_dat_skip_reason,
                    "seconds": 0.0,
                }
            )
        project = None
        summary: dict[str, Any] | None = None
        runtime_issues: list[Any] = []
        process_result = None
        rebuild_result = None
        rebuilt_sha256 = None
        exact_binary_match = False
        roundtrip_policy: dict[str, Any] | None = None
        decompile_policy: dict[str, Any] | None = None
        dialogs_imported = resolved_lang is not None
        lang_fallback_used = False
        lang_import_error: dict[str, Any] | None = None

        def preserve_unverified() -> str | None:
            if unverified_destination is None or not recovered.is_file():
                return None
            unverified_destination.parent.mkdir(parents=True, exist_ok=True)
            _replace_cross_device_safe(recovered, unverified_destination)
            return str(unverified_destination)

        def failure_result(
            exc: Exception,
            *,
            operational: bool,
            validation_issues: list[Any] | None = None,
        ) -> dict[str, Any]:
            kept = preserve_unverified()
            reported_summary = dict(summary) if summary is not None else None
            if reported_summary is not None:
                reported_summary["path"] = kept
            diagnostic = _rscript_failure_diagnostic(exc)
            return {
                "schema": "srhd-modkit-decompile-v1",
                "status": "failed" if operational else "unverified",
                "verified": False,
                "operational_failure": operational,
                "source": str(source),
                "requested_destination": str(destination),
                "destination": None,
                "unverified_path": kept,
                "source_sha256": sha256_file(source),
                "source_version": source_info["version"],
                "decompiler": {
                    "name": "RScript",
                    "version": self._rscript_version_label(),
                    "cli_profile": self._rscript_cli_profile(),
                    "executable": str(self.tools["rscript"].path),
                },
                "lang_dat": str(requested_lang) if requested_lang is not None else None,
                "dialogs_imported": dialogs_imported,
                "lang_dat_skip_reason": lang_dat_skip_reason,
                "lang_import": {
                    "status": (
                        "failed-fallback" if lang_fallback_used else "failed"
                    ) if requested_lang is not None else "not-requested",
                    "fallback_used": lang_fallback_used,
                    "diagnostic": lang_import_error or diagnostic,
                },
                "recovered_project": reported_summary,
                "phases": phases,
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "diagnostic": diagnostic,
                },
                "validation_issues": [item.as_dict() for item in (validation_issues or [])],
                "runtime_analysis": {
                    "origin": "decompiled-rson",
                    "canonicalization_sensitive_codes": sorted(
                        _DECOMPILE_CANONICALIZATION_SENSITIVE_RUNTIME_CODES
                    ),
                },
                "runtime_issues": [_decompiled_runtime_issue(issue) for issue in runtime_issues],
                "timeouts": {
                    "decompile": decompile_policy,
                    "roundtrip": roundtrip_policy,
                },
                "stale_transactions_removed": stale_transactions_removed,
            }

        try:
            phase_started = time.monotonic()
            _selected_decompile_timeout, decompile_policy = _rscript_timeout_policy(
                source,
                "decompile",
                decompile_timeout,
            )
            try:
                process_result, decompile_policy = self._recover_scr_with_rscript(
                    source,
                    recovered,
                    lang_dat=resolved_lang,
                    timeout=decompile_timeout,
                )
                phases.append(
                    {
                        "name": "recover-rson",
                        "status": "passed",
                        "seconds": round(time.monotonic() - phase_started, 3),
                        "exit_code": process_result.exit_code,
                        "queue_seconds": round(getattr(process_result, "queue_seconds", 0.0), 3),
                    }
                )
            except Exception as exc:
                phases.append(
                    {
                        "name": "recover-rson",
                        "status": "failed",
                        "seconds": round(time.monotonic() - phase_started, 3),
                        "error": str(exc),
                    }
                )
                if resolved_lang is None or not fallback_without_lang:
                    return failure_result(exc, operational=True)
                lang_import_error = _rscript_failure_diagnostic(exc) or {
                    "code": "decompile-lang-import-failed",
                    "message": str(exc),
                    "suggested_retry": "RSON будет восстановлен без диалогов Lang.dat по явно разрешённому fallback",
                }
                dialogs_imported = False
                lang_fallback_used = True
                recovered.unlink(missing_ok=True)
                fallback_started = time.monotonic()
                try:
                    process_result, decompile_policy = self._recover_scr_with_rscript(
                        source,
                        recovered,
                        lang_dat=None,
                        timeout=decompile_timeout,
                    )
                    phases.append(
                        {
                            "name": "recover-rson-without-lang",
                            "status": "passed",
                            "seconds": round(time.monotonic() - fallback_started, 3),
                            "reason": "explicit-fallback-after-lang-import-failure",
                            "exit_code": process_result.exit_code,
                            "queue_seconds": round(getattr(process_result, "queue_seconds", 0.0), 3),
                        }
                    )
                except Exception as fallback_exc:
                    phases.append(
                        {
                            "name": "recover-rson-without-lang",
                            "status": "failed",
                            "seconds": round(time.monotonic() - fallback_started, 3),
                            "error": str(fallback_exc),
                        }
                    )
                    return failure_result(fallback_exc, operational=True)

            phase_started = time.monotonic()
            try:
                project = load_rson(recovered)
                summary = project.summary()
                validation_issues = project.validate()
            except Exception as exc:
                phases.append(
                    {
                        "name": "validate-rson",
                        "status": "failed",
                        "seconds": round(time.monotonic() - phase_started, 3),
                        "error": str(exc),
                    }
                )
                return failure_result(exc, operational=False)
            validation_errors = [issue for issue in validation_issues if issue.severity == "error"]
            if validation_errors:
                exc = RuntimeError(
                    "Восстановленный RSON не прошёл проверку: "
                    + "; ".join(issue.message for issue in validation_errors[:5])
                )
                phases.append(
                    {
                        "name": "validate-rson",
                        "status": "failed",
                        "seconds": round(time.monotonic() - phase_started, 3),
                        "issues": len(validation_errors),
                    }
                )
                return failure_result(exc, operational=False, validation_issues=validation_issues)
            phases.append(
                {
                    "name": "validate-rson",
                    "status": "passed",
                    "seconds": round(time.monotonic() - phase_started, 3),
                    "issues": len(validation_issues),
                }
            )

            phase_started = time.monotonic()
            project.path = destination
            summary = project.summary()
            try:
                runtime_issues = lint_rson_runtime(project)
            except Exception as exc:
                phases.append(
                    {
                        "name": "lint-runtime",
                        "status": "failed",
                        "seconds": round(time.monotonic() - phase_started, 3),
                        "error": str(exc),
                    }
                )
                return failure_result(exc, operational=True)
            phases.append(
                {
                    "name": "lint-runtime",
                    "status": "passed",
                    "seconds": round(time.monotonic() - phase_started, 3),
                    "errors": sum(issue.severity == "error" for issue in runtime_issues),
                    "warnings": sum(issue.severity == "warning" for issue in runtime_issues),
                }
            )

            rebuilt_scr = transaction / "roundtrip.scr"
            rebuilt_lang = transaction / "roundtrip.txt"
            phase_started = time.monotonic()
            _selected_roundtrip_timeout, roundtrip_policy = _rscript_timeout_policy(
                recovered,
                "roundtrip",
                roundtrip_timeout,
            )
            try:
                rebuild_result, rebuilt_info, roundtrip_policy = self._compile_rson_with_rscript(
                    recovered,
                    rebuilt_scr,
                    rebuilt_lang,
                    timeout=roundtrip_timeout,
                )
                if source_info["version"] != rebuilt_info["version"]:
                    raise RuntimeError(
                        "После SCR -> RSON -> SCR изменилась версия формата: "
                        f"{source_info['version']} -> {rebuilt_info['version']}"
                    )
                if source_info["event_signatures"] != rebuilt_info["event_signatures"]:
                    raise RuntimeError("После SCR -> RSON -> SCR изменились сигнатуры событий")
                rebuilt_sha256 = sha256_file(rebuilt_scr)
                exact_binary_match = sha256_file(source) == rebuilt_sha256
                phases.append(
                    {
                        "name": "compile-roundtrip",
                        "status": "passed",
                        "seconds": round(time.monotonic() - phase_started, 3),
                        "exit_code": rebuild_result.exit_code,
                    }
                )
            except Exception as exc:
                phases.append(
                    {
                        "name": "compile-roundtrip",
                        "status": "failed",
                        "seconds": round(time.monotonic() - phase_started, 3),
                        "error": str(exc),
                    }
                )
                return failure_result(exc, operational=False)

            deep_result: dict[str, Any] | None = None
            if deep_roundtrip:
                phase_started = time.monotonic()
                deep_rson = transaction / "deep-roundtrip.rson"
                try:
                    deep_process, deep_policy = self._recover_scr_with_rscript(
                        rebuilt_scr,
                        deep_rson,
                        lang_dat=None,
                        timeout=decompile_timeout,
                    )
                    deep_project = load_rson(deep_rson)
                    deep_errors = [issue for issue in deep_project.validate() if issue.severity == "error"]
                    if deep_errors:
                        raise RuntimeError(
                            "Повторно восстановленный RSON не прошёл проверку: "
                            + "; ".join(issue.message for issue in deep_errors[:5])
                        )
                    deep_summary = deep_project.summary()
                    stable_fields = ("file_version", "objects", "links", "code_lines", "types")
                    structural_match = all(summary[field] == deep_summary[field] for field in stable_fields)
                    if not structural_match:
                        raise RuntimeError("Глубокий SCR -> RSON -> SCR -> RSON изменил структуру проекта")
                    deep_result = {
                        "verified": True,
                        "project": {**deep_summary, "path": None},
                        "canonical_graph_match": _project_graph_sha256(project) == _project_graph_sha256(deep_project),
                        "decompiler_exit_code": deep_process.exit_code,
                        "decompiler_progress_updates": getattr(deep_process, "progress_updates", 0),
                        "timeout": deep_policy,
                    }
                    phases.append(
                        {
                            "name": "deep-roundtrip",
                            "status": "passed",
                            "seconds": round(time.monotonic() - phase_started, 3),
                        }
                    )
                except Exception as exc:
                    phases.append(
                        {
                            "name": "deep-roundtrip",
                            "status": "failed",
                            "seconds": round(time.monotonic() - phase_started, 3),
                            "error": str(exc),
                        }
                    )
                    return failure_result(exc, operational=False)

            phase_started = time.monotonic()
            try:
                _replace_cross_device_safe(recovered, destination)
            except Exception as exc:
                phases.append(
                    {
                        "name": "publish",
                        "status": "failed",
                        "seconds": round(time.monotonic() - phase_started, 3),
                        "error": str(exc),
                    }
                )
                return failure_result(exc, operational=True)
            phases.append(
                {
                    "name": "publish",
                    "status": "passed",
                    "seconds": round(time.monotonic() - phase_started, 3),
                }
            )
        finally:
            shutil.rmtree(transaction, ignore_errors=True)

        return {
            "schema": "srhd-modkit-decompile-v1",
            "status": "verified",
            "source": str(source),
            "destination": str(destination),
            "requested_destination": str(destination),
            "unverified_path": None,
            "source_sha256": sha256_file(source),
            "destination_sha256": sha256_file(destination),
            "source_version": source_info["version"],
            "decompiler": {
                "name": "RScript",
                "version": self._rscript_version_label(),
                "cli_profile": self._rscript_cli_profile(),
                "executable": str(self.tools["rscript"].path),
            },
            "lang_dat": str(requested_lang) if requested_lang is not None else None,
            "dialogs_imported": dialogs_imported,
            "lang_dat_skip_reason": lang_dat_skip_reason,
            "lang_import": {
                "status": (
                    "failed-fallback"
                    if lang_fallback_used
                    else "passed" if dialogs_imported else "skipped" if lang_dat_skip_reason else "not-requested"
                ),
                "fallback_used": lang_fallback_used,
                "diagnostic": lang_import_error,
            },
            "objects": summary["objects"],
            "recovered_project": summary,
            "verified": True,
            "operational_failure": False,
            "roundtrip": {
                "scr_sha256": rebuilt_sha256,
                "exact_binary_match": exact_binary_match,
                "event_signatures_match": True,
                "compiler_exit_code": rebuild_result.exit_code,
                "compiler_seconds": round(rebuild_result.elapsed_seconds, 3),
                "compiler_queue_seconds": round(getattr(rebuild_result, "queue_seconds", 0.0), 3),
                "compiler_progress_updates": getattr(rebuild_result, "progress_updates", 0),
            },
            "deep_roundtrip": deep_result,
            "decompiler_exit_code": process_result.exit_code,
            "decompiler_was_waiting_after_output": process_result.forced_after_outputs,
            "decompiler_seconds": round(process_result.elapsed_seconds, 3),
            "decompiler_queue_seconds": round(getattr(process_result, "queue_seconds", 0.0), 3),
            "decompiler_progress_updates": getattr(process_result, "progress_updates", 0),
            "decompiler_last_progress_seconds": round(
                getattr(process_result, "last_progress_seconds", 0.0), 3
            ),
            "timeouts": {
                "decompile": decompile_policy,
                "roundtrip": roundtrip_policy,
            },
            "phases": phases,
            "stale_transactions_removed": stale_transactions_removed,
            "runtime_analysis": {
                "origin": "decompiled-rson",
                "canonicalization_sensitive_codes": sorted(
                    _DECOMPILE_CANONICALIZATION_SENSITIVE_RUNTIME_CODES
                ),
            },
            "runtime_issues": [_decompiled_runtime_issue(issue) for issue in runtime_issues],
        }

    def compare_scr(
        self,
        left: str | Path,
        right: str | Path,
        *,
        left_lang_dat: str | Path | None = None,
        right_lang_dat: str | Path | None = None,
        decompile_timeout: float | None = None,
        roundtrip_timeout: float | None = None,
        deep_roundtrip: bool = False,
        fallback_without_lang: bool = False,
        max_diff_lines: int = 200,
    ) -> dict[str, Any]:
        """Compare two SCR projects through verified temporary RSON recovery."""

        left = Path(left).resolve()
        right = Path(right).resolve()
        if max_diff_lines < 0:
            raise ValueError("max_diff_lines должен быть неотрицательным")
        with tempfile.TemporaryDirectory(prefix="srhd-scr-compare-") as temp_name:
            temp = Path(temp_name)
            left_rson = temp / "left.rson"
            right_rson = temp / "right.rson"
            left_result = self.decompile_scr(
                left,
                left_rson,
                lang_dat=left_lang_dat,
                decompile_timeout=decompile_timeout,
                roundtrip_timeout=roundtrip_timeout,
                deep_roundtrip=deep_roundtrip,
                fallback_without_lang=fallback_without_lang,
            )
            right_result = self.decompile_scr(
                right,
                right_rson,
                lang_dat=right_lang_dat,
                decompile_timeout=decompile_timeout,
                roundtrip_timeout=roundtrip_timeout,
                deep_roundtrip=deep_roundtrip,
                fallback_without_lang=fallback_without_lang,
            )

            def side(result: dict[str, Any]) -> dict[str, Any]:
                value = {
                    key: result.get(key)
                    for key in (
                        "source",
                        "status",
                        "verified",
                        "operational_failure",
                        "source_sha256",
                        "source_version",
                        "lang_dat",
                        "dialogs_imported",
                        "lang_import",
                        "recovered_project",
                        "roundtrip",
                        "deep_roundtrip",
                        "runtime_analysis",
                        "runtime_issues",
                        "phases",
                        "error",
                        "timeouts",
                    )
                }
                if isinstance(value.get("recovered_project"), dict):
                    value["recovered_project"] = dict(value["recovered_project"])
                    value["recovered_project"]["path"] = None
                return value

            verified = bool(left_result["verified"] and right_result["verified"])
            changed_blocks: list[dict[str, Any]] = []
            metadata_match = False
            event_signatures_match: bool | None = None
            runtime_changes = {"added": [], "resolved": [], "unchanged": []}
            storage_compatibility: dict[str, Any] | None = None
            dialog_semantics: dict[str, Any] | None = None
            update_issues: list[dict[str, Any]] = []
            if verified:
                left_project = load_rson(left_rson)
                right_project = load_rson(right_rson)
                storage_compatibility = compare_storage_schemas(left_project, right_project)
                left_dialogs = dialog_semantic_map(left_project)
                right_dialogs = dialog_semantic_map(right_project)
                dialog_semantics = {
                    "match": left_dialogs == right_dialogs,
                    "left": left_dialogs,
                    "right": right_dialogs,
                }
                stable_fields = ("file_version", "objects", "links", "code_lines", "types")
                left_summary = left_project.summary()
                right_summary = right_project.summary()
                left_scr_info = inspect_scr(left)
                right_scr_info = inspect_scr(right)
                left_script_name = str(left_summary.get("name", "")).strip()
                right_script_name = str(right_summary.get("name", "")).strip()
                left_sha256 = left_result.get("source_sha256")
                right_sha256 = right_result.get("source_sha256")
                if (
                    left_script_name
                    and right_script_name
                    and left_script_name.casefold() == right_script_name.casefold()
                    and isinstance(left_sha256, str)
                    and isinstance(right_sha256, str)
                    and left_sha256 != right_sha256
                ):
                    update_issues.append(
                        {
                            "severity": "warning",
                            "code": "runtime-saved-script-cache-update-shadow",
                            "message": (
                                f"SCR изменился, но runtime-имя {right_script_name!r} "
                                "осталось прежним. Активный экземпляр скрипта и его "
                                "код могут быть сериализованы в SAV, поэтому замена "
                                "файла на диске не доказывает обновление уже "
                                "загруженного сохранения. Для несовместимого "
                                "runtime-исправления используйте новый epoch/"
                                "ScriptName и проверенную миграцию"
                            ),
                            "script_name": right_script_name,
                            "left_sha256": left_sha256,
                            "right_sha256": right_sha256,
                            "evidence": (
                                f"{left.name} {left_sha256} -> "
                                f"{right.name} {right_sha256}"
                            ),
                        }
                    )
                event_signatures_match = (
                    left_scr_info["event_signatures"] == right_scr_info["event_signatures"]
                )
                metadata_match = (
                    left_scr_info["version"] == right_scr_info["version"]
                    and event_signatures_match
                    and all(left_summary[field] == right_summary[field] for field in stable_fields)
                )

                def code_blocks(project: Any) -> dict[str, list[str]]:
                    blocks: dict[str, list[str]] = {}
                    for item in project.iter_objects():
                        object_id = item.get("#")
                        for field, value in item.items():
                            if field in {"Code", "ActCode", "LinkCode"} and isinstance(value, list):
                                blocks[f"#{object_id} {field}"] = [str(line) for line in value]
                            elif field.casefold().endswith("code") and isinstance(value, str):
                                blocks[f"#{object_id} {field}"] = value.splitlines()
                    return blocks

                left_blocks = code_blocks(left_project)
                right_blocks = code_blocks(right_project)
                remaining = max_diff_lines
                for key in sorted(set(left_blocks) | set(right_blocks)):
                    before = left_blocks.get(key, [])
                    after = right_blocks.get(key, [])
                    if before == after:
                        continue
                    diff = list(
                        difflib.unified_diff(
                            before,
                            after,
                            fromfile=f"left {key}",
                            tofile=f"right {key}",
                            lineterm="",
                        )
                    )
                    emitted = diff[:remaining]
                    remaining -= len(emitted)
                    changed_blocks.append(
                        {
                            "block": key,
                            "left_lines": len(before),
                            "right_lines": len(after),
                            "diff": emitted,
                            "diff_truncated": len(emitted) < len(diff),
                        }
                    )

                def issue_map(result: dict[str, Any]) -> dict[tuple[Any, ...], dict[str, Any]]:
                    mapped: dict[tuple[Any, ...], dict[str, Any]] = {}
                    for issue in result.get("runtime_issues", []):
                        signature = tuple(issue.get(field) for field in ("severity", "code", "message", "location", "evidence"))
                        mapped[signature] = {key: value for key, value in issue.items() if key != "path"}
                    return mapped

                left_issues = issue_map(left_result)
                right_issues = issue_map(right_result)
                runtime_changes = {
                    "added": [right_issues[key] for key in sorted(set(right_issues) - set(left_issues), key=repr)],
                    "resolved": [left_issues[key] for key in sorted(set(left_issues) - set(right_issues), key=repr)],
                    "unchanged": [right_issues[key] for key in sorted(set(left_issues) & set(right_issues), key=repr)],
                }

            return {
                "schema": "srhd-modkit-scr-compare-v1",
                "verified": verified,
                "operational_failure": bool(
                    left_result.get("operational_failure") or right_result.get("operational_failure")
                ),
                "left": side(left_result),
                "right": side(right_result),
                "comparison": {
                    "metadata_match": metadata_match if verified else None,
                    "event_signatures_match": event_signatures_match,
                    "code_changed": bool(changed_blocks) if verified else None,
                    "changed_blocks": changed_blocks,
                    "runtime_issues": runtime_changes,
                    "update_issues": update_issues,
                    "storage_compatibility": storage_compatibility,
                    "dialog_semantics": dialog_semantics,
                    "temporary_projects_persisted": False,
                },
            }

    def export_rsm(
        self,
        source: str | Path,
        destination: str | Path,
        *,
        split: bool = False,
        overwrite: bool = False,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Export a validated RSON project through RScript 4.15f's CLI."""

        source = Path(source).resolve()
        destination = Path(destination).resolve()
        if source.suffix.casefold() != ".rson" or not source.is_file():
            raise ValueError("Экспорт в RSM принимает существующий файл .rson")
        errors = [issue for issue in load_rson(source).validate() if issue.severity == "error"]
        if errors:
            raise ValueError(f"RSON не прошёл проверку: {errors[0].message}")
        if not split and destination.suffix.casefold() != ".rsm":
            raise ValueError("Одиночный RSM должен иметь расширение .rsm")
        if destination.exists() and not overwrite:
            raise FileExistsError(f"Результат уже существует: {destination}")
        tool, _profile = self._require_supported_rscript("export-rsm")
        timeout_seconds, timeout_policy = _rscript_timeout_policy(source, "export-rsm", timeout)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".srhd-rsm-export-", dir=destination.parent
        ) as temp_name:
            temp = Path(temp_name)
            staged_argument = temp / "project.rsm"
            arguments = ["--cli", "-x", str(source), str(staged_argument)]
            if split:
                arguments.append("--split")
            expected = [] if split else [staged_argument]
            process = run_on_hidden_desktop(
                tool.path,
                arguments,
                cwd=tool.path.parent,
                expected_outputs=expected,
                timeout=timeout_seconds,
                progress_timeout=timeout_policy["progress_seconds"],
                abort_window_patterns=("Run-time error", "Runtime error", "Error", "Ошибка"),
            )
            generated = staged_argument.with_suffix("") if split else staged_argument
            entry = generated / "main.rsm" if split else generated
            if process.exit_code != 0 or not entry.is_file():
                raise RuntimeError(
                    "RScript 4.15f не создал RSM "
                    f"(код {process.exit_code}; ожидался {entry})"
                )
            project = inspect_rsm_project(entry)
            if not project.valid:
                first = next(issue for issue in project.issues if issue.severity == "error")
                raise RuntimeError(f"Экспортированный RSM не прошёл проверку: {first.message}")
            if split:
                backup = destination.parent / f".srhd-rsm-backup-{uuid.uuid4().hex}"
                had_destination = destination.exists()
                if had_destination:
                    os.replace(destination, backup)
                try:
                    os.replace(generated, destination)
                except Exception:
                    if had_destination and backup.exists() and not destination.exists():
                        os.replace(backup, destination)
                    raise
                finally:
                    if backup.exists():
                        shutil.rmtree(backup, ignore_errors=True)
                published_entry = destination / "main.rsm"
            else:
                _replace_cross_device_safe(generated, destination)
                published_entry = destination
        published = inspect_rsm_project(published_entry)
        return {
            "schema": "srhd-modkit-rsm-export-v1",
            "status": "passed",
            "source": str(source),
            "destination": str(destination),
            "split": split,
            "script_name": published.script_name,
            "modules": [module.as_dict() for module in published.modules],
            "source_sha256": sha256_file(source),
            "compiler": {
                "name": "RScript",
                "version": self._rscript_version_label(),
                "cli_profile": self._rscript_cli_profile(),
                "executable": str(tool.path),
                "exit_code": process.exit_code,
            },
            "timeout": timeout_policy,
        }

    def _rsm_language_document(
        self,
        script_name: str,
        base: Path | None,
        workspace: Path,
        label: str,
    ) -> BlockParDocument:
        if base is None:
            document = parse_blockpar("Script ^{\n}\n", encoding="utf-8")
        elif base.suffix.casefold() == ".txt":
            document = load_blockpar(base)
        elif base.suffix.casefold() == ".dat":
            decoded = workspace / f"{label}.base.txt"
            self.convert_dat(base, decoded)
            document = load_blockpar(decoded)
        else:
            raise ValueError("Языковая база RSM должна быть Lang.txt или Lang.dat")
        document.ensure_node(f"Script/{script_name}")
        return document

    def _prepare_rsm_language_target(
        self,
        script_name: str,
        target: Path,
        *,
        base: Path | None,
        workspace: Path,
        binary: bool,
        label: str,
    ) -> None:
        document = self._rsm_language_document(script_name, base, workspace, label)
        seed = workspace / f"{label}.seed.txt"
        document.save(seed, encoding="utf-8", include_raw=False, bom=False)
        if binary:
            self.convert_dat(seed, target, verify=True)
        else:
            shutil.copy2(seed, target)

    def _audit_rsm_language(
        self,
        project: Any,
        script_name: str,
        *,
        lang_txt: Path | None,
        lang_dat: Path | None,
        workspace: Path,
    ) -> tuple[list[dict[str, Any]], Path | None, dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        documents: list[tuple[str, BlockParDocument]] = []
        audit_lang_dat: Path | None = None
        txt_document: BlockParDocument | None = None
        dat_document: BlockParDocument | None = None

        if lang_txt is not None:
            txt_document = load_blockpar(lang_txt)
            documents.append((str(workspace / "CFG" / "Rus" / "Lang.dat"), txt_document))
            for issue in lint_game_text(
                read_text(lang_txt),
                lang_txt,
                require_cp1251_representable=True,
            ):
                issues.append(issue.as_dict())
            issues.extend(issue.as_dict() for issue in lint_blockpar_display_text(txt_document, lang_txt))
            audit_lang_dat = workspace / "lang-audit" / "Lang.dat"
            audit_lang_dat.parent.mkdir(parents=True, exist_ok=True)
            self.convert_dat(lang_txt, audit_lang_dat, verify=True)

        if lang_dat is not None:
            decoded = workspace / "lang-dat.decoded.txt"
            self.convert_dat(lang_dat, decoded)
            dat_document = load_blockpar(decoded)
            documents.append((str(workspace / "CFG" / "Rus" / "Lang.dat"), dat_document))
            for issue in lint_game_text(
                read_text(decoded),
                lang_dat,
                require_cp1251_representable=True,
            ):
                issues.append(issue.as_dict())
            issues.extend(issue.as_dict() for issue in lint_blockpar_display_text(dat_document, lang_dat))
            semantic_probe = workspace / "lang-dat-roundtrip" / "Lang.dat"
            semantic_probe.parent.mkdir(parents=True, exist_ok=True)
            self.convert_dat(decoded, semantic_probe, verify=True)
            audit_lang_dat = lang_dat

        if txt_document is not None and dat_document is not None:
            if txt_document.canonical_semantic() != dat_document.canonical_semantic():
                issues.append(
                    {
                        "severity": "error",
                        "code": "rsm-language-outputs-mismatch",
                        "message": "Lang.txt и Lang.dat после rsmc содержат разные деревья BlockPar",
                        "path": str(lang_dat),
                        "location": f"Script/{script_name}",
                        "evidence": str(lang_txt),
                    }
                )

        selected = documents[:1]
        issues.extend(
            issue.as_dict()
            for issue in lint_script_dialog_language(
                [project],
                selected,
                checked_scripts=[script_name],
            )
        )
        return issues, audit_lang_dat, {
            "txt": str(lang_txt) if lang_txt is not None else None,
            "dat": str(lang_dat) if lang_dat is not None else None,
            "documents": len(documents),
        }

    def _compile_and_audit_rsm(
        self,
        project: RsmProject,
        workspace: Path,
        *,
        lang_txt: Path | None,
        lang_dat: Path | None,
        timeout: float | None,
        deep_roundtrip: bool,
    ) -> dict[str, Any]:
        rsmc = self.require("rsmc")
        staged_scr = workspace / "compiled.scr"
        arguments = ["build", str(project.entry), "-o", str(staged_scr)]
        if lang_txt is not None:
            arguments.extend(["--lang-txt", str(lang_txt)])
        if lang_dat is not None:
            arguments.extend(["--lang-dat", str(lang_dat)])
        timeout_seconds, timeout_policy = _rscript_timeout_policy(
            project.entry, "rsm-build", timeout
        )
        try:
            process = run_on_hidden_desktop(
                rsmc.path,
                arguments,
                cwd=project.entry.parent,
                expected_outputs=[staged_scr],
                timeout=timeout_seconds,
                progress_timeout=timeout_policy["progress_seconds"],
                abort_window_patterns=("Run-time error", "Runtime error", "Error", "Ошибка"),
                no_console=True,
            )
        except HiddenProcessTimeout as exc:
            report = {
                "schema": "srhd-modkit-rsm-build-v1",
                "status": "failed",
                "verified": False,
                "source": str(project.entry),
                "script_name": project.script_name,
                "compiler_output_created": staged_scr.is_file(),
                "published_outputs": False,
                "failure": {
                    "code": "rsmc-build-timeout",
                    "message": str(exc),
                    "diagnostic": exc.as_dict(),
                },
                "compiler": {
                    "name": "rsmc",
                    "executable": str(rsmc.path),
                    "executable_sha256": sha256_file(rsmc.path),
                    "exit_code": exc.exit_code,
                },
                "timeout": timeout_policy,
            }
            raise RsmBuildFailure(str(exc), report) from exc
        if process.exit_code != 0 or not staged_scr.is_file():
            code = (
                "rsmc-language-merge-failed"
                if staged_scr.is_file() and (lang_txt is not None or lang_dat is not None)
                else "rsmc-build-failed"
            )
            report = {
                "schema": "srhd-modkit-rsm-build-v1",
                "status": "failed",
                "verified": False,
                "source": str(project.entry),
                "script_name": project.script_name,
                "compiler_output_created": staged_scr.is_file(),
                "published_outputs": False,
                "failure": {
                    "code": code,
                    "message": (
                        "rsmc завершился с ошибкой после создания SCR; обычно это "
                        "означает, что языковой файл не существовал заранее или "
                        "не содержал Script/<ScriptName>"
                        if code == "rsmc-language-merge-failed"
                        else "rsmc не создал SCR"
                    ),
                },
                "compiler": {
                    "name": "rsmc",
                    "executable": str(rsmc.path),
                    "executable_sha256": sha256_file(rsmc.path),
                    "exit_code": process.exit_code,
                },
                "timeout": timeout_policy,
            }
            raise RsmBuildFailure(report["failure"]["message"], report)

        scr_info = inspect_scr(staged_scr)
        if not scr_info["supported_version"]:
            raise RsmBuildFailure(
                "rsmc создал неподдерживаемую версию SCR",
                {
                    "schema": "srhd-modkit-rsm-build-v1",
                    "status": "failed",
                    "verified": False,
                    "source": str(project.entry),
                    "compiler_output_created": True,
                    "published_outputs": False,
                    "failure": {
                        "code": "rsmc-scr-version",
                        "message": f"Неподдерживаемая версия SCR {scr_info['version']}",
                    },
                },
            )

        language_issues: list[dict[str, Any]] = []
        audit_lang_dat: Path | None = None
        language_report: dict[str, Any] = {"txt": None, "dat": None, "documents": 0}
        recovered = workspace / "recovered.rson"
        # Recover code without importing language. RScript 4.15f can raise
        # EInOutError 105 while importing an otherwise valid merged Lang.dat.
        # The exact packaged BlockPar is audited immediately afterwards against
        # the recovered graph, so no dialogue key is trusted or skipped.
        decompile = self.decompile_scr(
            staged_scr,
            recovered,
            deep_roundtrip=deep_roundtrip,
        )
        if decompile.get("verified"):
            recovered_project = load_rson(recovered)
            language_issues, audit_lang_dat, language_report = self._audit_rsm_language(
                recovered_project,
                project.script_name or recovered_project.name,
                lang_txt=lang_txt,
                lang_dat=lang_dat,
                workspace=workspace,
            )
        runtime_issues = list(decompile.get("runtime_issues", ()))
        validation_issues = list(decompile.get("validation_issues", ()))
        issues = [*project.as_dict()["issues"], *validation_issues, *runtime_issues, *language_issues]
        errors = [issue for issue in issues if issue.get("severity") == "error"]
        verified = bool(decompile.get("verified") and not errors)
        failure: dict[str, Any] | None = None
        if not decompile.get("verified"):
            failure = {
                "code": "rsm-postbuild-scr-audit-failed",
                "message": "rsmc создал SCR, но обязательный SCR round-trip не прошёл",
                "diagnostic": decompile.get("error"),
            }
        elif errors:
            failure = {
                "code": "rsm-postbuild-audit-issues",
                "message": "SCR собран, но runtime/language-аудит обнаружил ошибки",
                "issue_codes": sorted({issue.get("code") for issue in errors if issue.get("code")}),
            }
        return {
            "schema": "srhd-modkit-rsm-build-v1",
            "status": "passed" if verified else "issues" if decompile.get("verified") else "failed",
            "verified": verified,
            "source": str(project.entry),
            "script_name": project.script_name,
            "modules": [module.as_dict() for module in project.modules],
            "scr": str(staged_scr),
            "scr_size": staged_scr.stat().st_size,
            "scr_sha256": sha256_file(staged_scr),
            "scr_info": scr_info,
            "language": language_report,
            "issues": issues,
            "runtime_issues": runtime_issues,
            "language_issues": language_issues,
            "failure": failure,
            "decompile_audit": decompile,
            "compiler_output_created": True,
            "published_outputs": False,
            "compiler": {
                "name": "rsmc",
                "executable": str(rsmc.path),
                "executable_sha256": sha256_file(rsmc.path),
                "exit_code": process.exit_code,
                "seconds": round(process.elapsed_seconds, 3),
            },
            "timeout": timeout_policy,
        }

    def validate_rsm(
        self,
        entry: str | Path,
        *,
        lang_base: str | Path | None = None,
        timeout: float | None = None,
        deep_roundtrip: bool = False,
    ) -> dict[str, Any]:
        project = inspect_rsm_project(entry)
        if not project.valid:
            return {**project.as_dict(), "status": "issues", "verified": False}
        base = Path(lang_base).resolve() if lang_base is not None else None
        if base is not None and not base.is_file():
            raise FileNotFoundError(base)
        with tempfile.TemporaryDirectory(prefix="srhd-rsm-validate-") as temp_name:
            workspace = Path(temp_name)
            lang_txt: Path | None = None
            lang_dat: Path | None = None
            if base is not None:
                if base.suffix.casefold() == ".dat":
                    lang_dat = workspace / "CFG" / "Rus" / "Lang.dat"
                    lang_dat.parent.mkdir(parents=True, exist_ok=True)
                    self._prepare_rsm_language_target(
                        project.script_name or "",
                        lang_dat,
                        base=base,
                        workspace=workspace,
                        binary=True,
                        label="validate-lang-dat",
                    )
                else:
                    lang_txt = workspace / "CFG" / "Rus" / "Lang.txt"
                    lang_txt.parent.mkdir(parents=True, exist_ok=True)
                    self._prepare_rsm_language_target(
                        project.script_name or "",
                        lang_txt,
                        base=base,
                        workspace=workspace,
                        binary=False,
                        label="validate-lang-txt",
                    )
            result = self._compile_and_audit_rsm(
                project,
                workspace,
                lang_txt=lang_txt,
                lang_dat=lang_dat,
                timeout=timeout,
                deep_roundtrip=deep_roundtrip,
            )
            result["temporary_outputs_persisted"] = False
            result["scr"] = None
            result["scr_sha256"] = result.get("scr_sha256")
            return result

    def build_rsm(
        self,
        entry: str | Path,
        scr_output: str | Path,
        *,
        lang_txt_output: str | Path | None = None,
        lang_dat_output: str | Path | None = None,
        lang_base: str | Path | None = None,
        overwrite: bool = False,
        timeout: float | None = None,
        deep_roundtrip: bool = False,
    ) -> dict[str, Any]:
        project = inspect_rsm_project(entry)
        if not project.valid:
            report = {**project.as_dict(), "status": "issues", "verified": False, "published_outputs": False}
            raise RsmBuildFailure("RSM не прошёл статическую проверку", report)
        scr_output = Path(scr_output).resolve()
        if scr_output.suffix.casefold() != ".scr":
            raise ValueError("Результат build-rsm должен иметь расширение .scr")
        lang_txt_target = Path(lang_txt_output).resolve() if lang_txt_output is not None else None
        lang_dat_target = Path(lang_dat_output).resolve() if lang_dat_output is not None else None
        if lang_txt_target is not None and lang_txt_target.suffix.casefold() != ".txt":
            raise ValueError("--lang-txt должен указывать на .txt")
        if lang_dat_target is not None and lang_dat_target.suffix.casefold() != ".dat":
            raise ValueError("--lang-dat должен указывать на .dat")
        targets = [path for path in (scr_output, lang_txt_target, lang_dat_target) if path is not None]
        existing = [path for path in targets if path.exists()]
        if existing and not overwrite:
            raise FileExistsError(f"Результат уже существует: {existing[0]}")
        explicit_base = Path(lang_base).resolve() if lang_base is not None else None
        if explicit_base is not None and not explicit_base.is_file():
            raise FileNotFoundError(explicit_base)
        scr_output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".srhd-rsm-build-", dir=scr_output.parent) as temp_name:
            workspace = Path(temp_name)
            staged_txt: Path | None = None
            staged_dat: Path | None = None
            if lang_txt_target is not None:
                staged_txt = workspace / "CFG" / "Rus" / "Lang.txt"
                staged_txt.parent.mkdir(parents=True, exist_ok=True)
                txt_base = explicit_base or (lang_txt_target if lang_txt_target.is_file() else None)
                self._prepare_rsm_language_target(
                    project.script_name or "",
                    staged_txt,
                    base=txt_base,
                    workspace=workspace,
                    binary=False,
                    label="build-lang-txt",
                )
            if lang_dat_target is not None:
                staged_dat = workspace / "CFG" / "Rus" / "Lang.dat"
                staged_dat.parent.mkdir(parents=True, exist_ok=True)
                dat_base = explicit_base or (lang_dat_target if lang_dat_target.is_file() else None)
                self._prepare_rsm_language_target(
                    project.script_name or "",
                    staged_dat,
                    base=dat_base,
                    workspace=workspace,
                    binary=True,
                    label="build-lang-dat",
                )
            result = self._compile_and_audit_rsm(
                project,
                workspace,
                lang_txt=staged_txt,
                lang_dat=staged_dat,
                timeout=timeout,
                deep_roundtrip=deep_roundtrip,
            )
            if not result["verified"]:
                raise RsmBuildFailure("RSM-сборка не прошла обязательный аудит", result)
            staged_scr = Path(result["scr"])
            _replace_cross_device_safe(staged_scr, scr_output)
            if staged_txt is not None and lang_txt_target is not None:
                lang_txt_target.parent.mkdir(parents=True, exist_ok=True)
                _replace_cross_device_safe(staged_txt, lang_txt_target)
            if staged_dat is not None and lang_dat_target is not None:
                lang_dat_target.parent.mkdir(parents=True, exist_ok=True)
                _replace_cross_device_safe(staged_dat, lang_dat_target)
        result["scr"] = str(scr_output)
        result["scr_sha256"] = sha256_file(scr_output)
        result["scr_info"] = inspect_scr(scr_output)
        result["language"] = {
            **result.get("language", {}),
            "txt": str(lang_txt_target) if lang_txt_target is not None else None,
            "dat": str(lang_dat_target) if lang_dat_target is not None else None,
        }
        result["published_outputs"] = True
        return result

    def convert_script_project(
        self,
        source: str | Path,
        destination: str | Path,
        *,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        source = Path(source).resolve()
        destination = Path(destination).resolve()
        mapping = {".rson": ("svr", ".svr"), ".svr": ("rson", ".rson")}
        target = mapping.get(source.suffix.casefold())
        if target is None or destination.suffix.casefold() != target[1]:
            raise ValueError("Поддерживается только RSON -> SVR или SVR -> RSON")
        if not source.is_file():
            raise FileNotFoundError(source)
        if destination.exists() and not overwrite:
            raise FileExistsError(f"Результат уже существует: {destination}")
        if source.suffix.casefold() == ".rson":
            errors = [item for item in load_rson(source).validate() if item.severity == "error"]
            if errors:
                raise ValueError(f"RSON не прошёл проверку: {errors[0].message}")
        tool, cli_profile = self._require_supported_rscript("convert")
        if cli_profile == "modern-cli":
            fallback = self.tools["rscript410"]
            fallback_version = self.rscript410_version
            if (
                not fallback.path.is_file()
                or fallback_version is None
                or fallback_version.parts[:2] != (4, 10)
            ):
                raise RuntimeError(
                    "RScript 4.15f больше не публикует RSON/SVR-конвертацию через "
                    "CLI. Штатный setup сохраняет прежнюю 4.10f в RScript410; "
                    "без неё применяйте RSON или RSM напрямую. GUI ModKit не запускает."
                )
            tool = fallback
            cli_profile = "legacy-cli"
        destination.parent.mkdir(parents=True, exist_ok=True)
        timeout_seconds, timeout_policy = _rscript_timeout_policy(source, "convert", None)
        # RScript 4.10f crashes with Runtime error 217 for absolute paths and
        # even relative paths containing a directory. Only a bare filename in
        # its own working directory is reliable. A UUID prevents collisions.
        stem = f"_srhd_{uuid.uuid4().hex}"
        staged_source = tool.path.parent / f"{stem}{source.suffix.casefold()}"
        generated = tool.path.parent / f"{stem}{target[1]}"
        try:
            shutil.copy2(source, staged_source)
            process_result = run_on_hidden_desktop(
                tool.path,
                ["--cli", "--convert", target[0], staged_source.name],
                cwd=tool.path.parent,
                expected_outputs=[generated],
                timeout=timeout_seconds,
                progress_timeout=timeout_policy["progress_seconds"],
                abort_window_patterns=("Run-time error", "Runtime error", "Error", "Ошибка"),
            )
            if not generated.is_file():
                raise RuntimeError("RScript CLI не создал результат конвертации")
            if generated.suffix.casefold() == ".rson":
                issues = load_rson(generated).validate()
                if any(item.severity == "error" for item in issues):
                    raise RuntimeError(f"Полученный RSON не прошёл проверку: {issues[0].message}")
            with tempfile.TemporaryDirectory(prefix=".srhd-script-output-", dir=destination.parent) as output_name:
                staged_output = Path(output_name) / destination.name
                shutil.copy2(generated, staged_output)
                os.replace(staged_output, destination)
        finally:
            staged_source.unlink(missing_ok=True)
            generated.unlink(missing_ok=True)
        return {
            "source": str(source),
            "destination": str(destination),
            "sha256": sha256_file(destination),
            "compiler_exit_code": process_result.exit_code,
            "compiler_was_waiting_after_output": process_result.forced_after_outputs,
            "compiler_queue_seconds": round(getattr(process_result, "queue_seconds", 0.0), 3),
            "compiler_progress_updates": getattr(process_result, "progress_updates", 0),
            "compiler_timeout": timeout_policy,
        }
