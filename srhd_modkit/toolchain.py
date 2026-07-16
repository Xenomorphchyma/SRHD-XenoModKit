from __future__ import annotations

import errno
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from .files import sha256_file
from .formats import inspect_file
from .blockpar import load_blockpar
from .scripts import inspect_scr, load_rson
from .runtime_lint import lint_rson_runtime
from .hidden_process import run_on_hidden_desktop
from .legacy_manifest import ensure_legacy_codepage_executable


EMPTY_RSCRIPT_LANG_DAT = b"\xff\xfe"


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
            "gi-to-png": Tool(
                "gi-to-png",
                self.tools_root / "RangerTools" / "gi-to-png_ranger-tools.exe",
                "Проверенное автоматическое преобразование GI в PNG",
                True,
            ),
            "png-to-gi": Tool(
                "png-to-gi",
                self.tools_root / "RangerTools" / "png-to-gi_ranger-tools.exe",
                "Проверенное автоматическое преобразование PNG в GI",
                True,
            ),
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
                "Headless-проверка, конвертация и компиляция RSON/SVR/SCR 4.10f",
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

        tool = self.require({"gi-png": "gi-to-png", "png-gi": "png-to-gi"}[direction])
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        converted: list[ConversionItem] = []
        with tempfile.TemporaryDirectory(prefix=".srhd-convert-", dir=output_dir.parent) as temp_name:
            stage_root = Path(temp_name)
            staged: list[tuple[Path, Path, Path]] = []
            for source, destination in destinations:
                relative = destination.relative_to(output_dir)
                stage_dir = (stage_root / relative.parent)
                stage_dir.mkdir(parents=True, exist_ok=True)
                command = [str(tool.path), "-o", str(stage_dir)]
                if direction == "png-gi":
                    command.extend(["-t", gi_mode])
                command.append(str(source))
                completed = subprocess.run(
                    command,
                    cwd=tool.path.parent,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=300,
                    check=False,
                )
                stage_file = stage_dir / source.with_suffix(target_ext).name
                if completed.returncode != 0 or not stage_file.is_file():
                    details = (completed.stderr or completed.stdout).strip()
                    raise RuntimeError(f"{tool.name} не создал корректный результат для {source}: {details}")
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

    def compile_rson(
        self,
        source: str | Path,
        scr_output: str | Path,
        lang_output: str | Path,
        *,
        overwrite: bool = False,
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
        tool = self.require("rscript")
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
                timeout=300,
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
            "runtime_warnings": [
                issue.as_dict() for issue in runtime_issues if issue.severity == "warning"
            ],
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
