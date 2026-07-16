from __future__ import annotations

import errno
import difflib
import hashlib
import json
import os
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
from .blockpar import load_blockpar
from .scripts import inspect_scr, load_rson
from .runtime_lint import lint_rson_runtime
from .hidden_process import HiddenControlAction, run_on_hidden_desktop
from .legacy_manifest import ensure_legacy_codepage_executable


EMPTY_RSCRIPT_LANG_DAT = b"\xff\xfe"


def _rscript_timeout_policy(
    source: Path,
    operation: str,
    requested: float | None,
) -> tuple[float | None, dict[str, Any]]:
    """Choose a generous size-aware limit without imposing a maximum size.

    ``requested=None`` means adaptive, ``0`` disables the total deadline, and
    a positive value is an explicit operator policy.  The adaptive formula is
    intentionally uncapped: a large project receives more time instead of
    becoming impossible to build merely because it crossed a fixed threshold.
    """

    if requested is not None and requested < 0:
        raise ValueError("Таймаут не может быть отрицательным; 0 отключает общий лимит")
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
        adaptive = max(600.0, 180.0 + code_lines * 0.35 + objects * 1.5 + size_mib * 30.0)
    else:
        adaptive = max(600.0, 300.0 + size_mib * 180.0)
    adaptive = round(adaptive, 3)
    if requested is None:
        selected = adaptive
        mode = "adaptive"
    elif requested == 0:
        selected = None
        mode = "disabled"
    else:
        selected = float(requested)
        mode = "explicit"
    return selected, {
        "mode": mode,
        "seconds": selected,
        "adaptive_seconds": adaptive,
        "source_size": source.stat().st_size if source.is_file() else None,
        "objects": objects or None,
        "code_lines": code_lines or None,
    }


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
class Tool:
    name: str
    path: Path
    purpose: str
    automatic: bool

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["path"] = str(self.path)
        value["available"] = self.path.is_file()
        value["size"] = self.path.stat().st_size if self.path.is_file() else None
        return value


@dataclass(frozen=True)
class ConversionItem:
    source: Path
    destination: Path
    source_sha256: str
    destination_sha256: str
    destination_size: int

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["source"] = str(self.source)
        value["destination"] = str(self.destination)
        return value


class Toolchain:
    def __init__(self, tools_root: str | Path | None = None):
        if tools_root is None:
            tools_root = Path(__file__).resolve().parents[2]
        self.tools_root = Path(tools_root).resolve()
        blockpar_root = self.tools_root / "BlockParEditor"
        blockpar_original = blockpar_root / "BlockParEditor.exe"
        blockpar_codec = blockpar_root / "BlockParEditor.Legacy.exe"
        if blockpar_original.is_file():
            ensure_legacy_codepage_executable(blockpar_original, blockpar_codec)
        self.tools = {
            "blockpar": Tool(
                "blockpar",
                blockpar_codec,
                "DAT/BlockPar 1.9 без GUI с локальной CP1251-совместимостью",
                True,
            ),
            "reseditor": Tool(
                "reseditor",
                self.tools_root / "ResEditor" / "ResEditor_hai128.exe",
                "Редактирование GAI, HAI, PKG и ресурсов",
                False,
            ),
            "rscript": Tool(
                "rscript",
                self.tools_root / "RScript" / "RScript.exe",
                "Headless-проверка, декомпиляция, конвертация и компиляция RSON/SVR/SCR 4.10f",
                True,
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
                converted.append(
                    ConversionItem(
                        source=source,
                        destination=destination,
                        source_sha256=sha256_file(source),
                        destination_sha256=sha256_file(destination),
                        destination_size=destination.stat().st_size,
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
        with tempfile.TemporaryDirectory(prefix=".srhd-dat-", dir=destination.parent) as temp_name:
            temp = Path(temp_name)
            staged_source = temp / source.name
            staged_destination = temp / destination.name
            if source_document is not None:
                # The VB6 frontend corrupts Unicode on systems whose global ACP
                # is UTF-8. The private codec process is explicitly CP1251, which
                # also matches the byte payload consumed by the game. Feeding it
                # UTF-8 can round-trip through the editor while producing mojibake
                # in SRHD, so unrepresentable Unicode must fail before conversion.
                try:
                    source_document.save(
                        staged_source,
                        encoding="cp1251",
                        include_raw=False,
                        bom=False,
                    )
                except UnicodeEncodeError as exc:
                    bad = source_document.to_text(include_raw=False)[exc.start : max(exc.end, exc.start + 1)]
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

        tool = self.require("rscript")
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
            process_result = run_on_hidden_desktop(
                tool.path,
                [
                    "--cli",
                    "--build",
                    "--full",
                    str(staged_source),
                    str(staged_scr),
                    str(staged_lang),
                ],
                cwd=tool.path.parent,
                timeout=timeout_seconds,
                expected_outputs=[staged_scr, staged_lang],
                abort_window_patterns=("Run-time error", "Runtime error", "Error", "Ошибка"),
            )
            if not staged_scr.is_file():
                raise RuntimeError(f"RScript CLI не создал SCR (код {process_result.exit_code})")
            if not staged_lang.exists():
                staged_lang.write_text("", encoding="utf-8")
            scr_info = inspect_scr(staged_scr)
            if not scr_info["supported_version"]:
                raise RuntimeError(f"RScript создал SCR неподдерживаемой версии {scr_info['version']}")
            _replace_cross_device_safe(staged_scr, scr_output)
            _replace_cross_device_safe(staged_lang, lang_output)
        return process_result, scr_info, timeout_policy

    def compile_rson(
        self,
        source: str | Path,
        scr_output: str | Path,
        lang_output: str | Path,
        *,
        overwrite: bool = False,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        source = Path(source).resolve()
        scr_output = Path(scr_output).resolve()
        lang_output = Path(lang_output).resolve()
        if source.suffix.casefold() != ".rson":
            raise ValueError("Компилятор принимает проект .rson")
        project = load_rson(source)
        issues = project.validate()
        errors = [issue for issue in issues if issue.severity == "error"]
        if errors:
            raise ValueError("RSON не прошёл проверку: " + "; ".join(issue.message for issue in errors[:5]))
        runtime_issues = lint_rson_runtime(project)
        runtime_errors = [issue for issue in runtime_issues if issue.severity == "error"]
        if runtime_errors:
            raise ValueError(
                "RSON не прошёл runtime-lint: "
                + "; ".join(f"{issue.code}: {issue.message}" for issue in runtime_errors[:5])
            )
        existing = [path for path in (scr_output, lang_output) if path.exists()]
        if existing and not overwrite:
            raise FileExistsError(f"Результат уже существует: {existing[0]}")
        process_result, _scr_info, timeout_policy = self._compile_rson_with_rscript(
            source,
            scr_output,
            lang_output,
            timeout=timeout,
        )
        return {
            "source": str(source),
            "scr": str(scr_output),
            "lang": str(lang_output),
            "scr_size": scr_output.stat().st_size,
            "scr_sha256": sha256_file(scr_output),
            "lang_sha256": sha256_file(lang_output),
            "compiler_exit_code": process_result.exit_code,
            "compiler_was_waiting_after_output": process_result.forced_after_outputs,
            "compiler_seconds": round(process_result.elapsed_seconds, 3),
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
        """Automate RScript's hidden decompiler and always remove its staged SCR."""

        tool = self.require("rscript")
        timeout_seconds, timeout_policy = _rscript_timeout_policy(source, "decompile", timeout)
        stem = f"_srhd_{uuid.uuid4().hex}"
        staged_scr = tool.path.parent / f"{stem}.scr"
        recovered.parent.mkdir(parents=True, exist_ok=True)
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
        resolved_lang = Path(lang_dat).resolve() if lang_dat is not None else None
        if resolved_lang is not None:
            if resolved_lang.suffix.casefold() != ".dat":
                raise ValueError("Файл диалогов должен иметь расширение .dat")
            if not resolved_lang.is_file():
                raise FileNotFoundError(resolved_lang)
        source_info = inspect_scr(source)
        if not source_info["supported_version"]:
            raise ValueError(f"RScript 4.10f не поддерживает SCR версии {source_info['version']}")

        destination.parent.mkdir(parents=True, exist_ok=True)
        stale_transactions_removed = _cleanup_stale_decompile_transactions(destination.parent)
        transaction = destination.parent / f".srhd-decompile-{uuid.uuid4().hex}"
        transaction.mkdir()
        (transaction / ".srhd-transaction").write_text("decompile-v1\n", encoding="ascii")
        recovered = transaction / "recovered.rson"
        phases: list[dict[str, Any]] = [
            {"name": "inspect-source", "status": "passed", "seconds": 0.0}
        ]
        project = None
        summary: dict[str, Any] | None = None
        runtime_issues: list[Any] = []
        process_result = None
        rebuild_result = None
        rebuilt_sha256 = None
        exact_binary_match = False
        roundtrip_policy: dict[str, Any] | None = None
        decompile_policy: dict[str, Any] | None = None

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
                "lang_dat": str(resolved_lang) if resolved_lang is not None else None,
                "dialogs_imported": resolved_lang is not None,
                "recovered_project": reported_summary,
                "phases": phases,
                "error": {"type": type(exc).__name__, "message": str(exc)},
                "validation_issues": [item.as_dict() for item in (validation_issues or [])],
                "runtime_issues": [issue.as_dict() for issue in runtime_issues],
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
                return failure_result(exc, operational=True)

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
            "lang_dat": str(resolved_lang) if resolved_lang is not None else None,
            "dialogs_imported": resolved_lang is not None,
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
            },
            "deep_roundtrip": deep_result,
            "decompiler_exit_code": process_result.exit_code,
            "decompiler_was_waiting_after_output": process_result.forced_after_outputs,
            "decompiler_seconds": round(process_result.elapsed_seconds, 3),
            "timeouts": {
                "decompile": decompile_policy,
                "roundtrip": roundtrip_policy,
            },
            "phases": phases,
            "stale_transactions_removed": stale_transactions_removed,
            "runtime_issues": [issue.as_dict() for issue in runtime_issues],
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
            )
            right_result = self.decompile_scr(
                right,
                right_rson,
                lang_dat=right_lang_dat,
                decompile_timeout=decompile_timeout,
                roundtrip_timeout=roundtrip_timeout,
                deep_roundtrip=deep_roundtrip,
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
                        "recovered_project",
                        "roundtrip",
                        "deep_roundtrip",
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
            if verified:
                left_project = load_rson(left_rson)
                right_project = load_rson(right_rson)
                stable_fields = ("file_version", "objects", "links", "code_lines", "types")
                left_summary = left_project.summary()
                right_summary = right_project.summary()
                left_scr_info = inspect_scr(left)
                right_scr_info = inspect_scr(right)
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
                    "temporary_projects_persisted": False,
                },
            }

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
        tool = self.require("rscript")
        destination.parent.mkdir(parents=True, exist_ok=True)
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
                timeout=120,
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
        }
