from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import zipfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence

from .audit import AuditProfile, AuditReport, audit_mod
from .files import build_manifest, pack_mod, sha256_file, stage_tree


RELEASE_SCHEMA = "srhd-modkit-release-v1"
DEPLOY_SCHEMA = "srhd-modkit-deploy-v1"

_SOURCE_ROOT_NAMES = {"source", "sources"}
_SOURCE_FILE_PATTERNS = (
    "*.rson",
    "**/*.rson",
    "*.rsm",
    "**/*.rsm",
    "*.svr",
    "**/*.svr",
    "*.source.txt",
    "**/*.source.txt",
    "*.lang.txt",
    "**/*.lang.txt",
)


class ReleaseBlockedError(RuntimeError):
    def __init__(self, report: AuditReport, *, warnings_as_errors: bool = False) -> None:
        self.report = report
        self.warnings_as_errors = warnings_as_errors
        issues = report.blocking_issues(warnings_as_errors=warnings_as_errors)
        first = issues[0] if issues else None
        message = "Релиз заблокирован аудитом"
        if first:
            message += f" ({first.code}): {first.message}"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ReleaseResult:
    output: Path
    manifest_path: Path
    audit_path: Path
    sha256: str
    archive_size: int
    file_count: int
    prefix: str
    verified: bool
    report: AuditReport
    exclude: tuple[str, ...] = ()
    strip_sources: bool = False
    schema: str = RELEASE_SCHEMA

    def as_dict(self, *, include_audit: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema": self.schema,
            "output": str(self.output),
            "manifest": str(self.manifest_path),
            "audit": str(self.audit_path),
            "sha256": self.sha256,
            "archive_size": self.archive_size,
            "file_count": self.file_count,
            "prefix": self.prefix,
            "verified": self.verified,
            "exclude": list(self.exclude),
            "strip_sources": self.strip_sources,
        }
        if include_audit:
            value["audit_report"] = self.report.as_dict()
        return value


@dataclass(frozen=True, slots=True)
class DeployResult:
    destination: Path
    file_count: int
    total_size: int
    prefix: str
    verified: bool
    replaced_existing: bool
    stale_files_removed: int
    excluded_file_count: int
    exclude: tuple[str, ...]
    strip_sources: bool
    report: AuditReport
    modcfg_modified: bool = False
    schema: str = DEPLOY_SCHEMA

    def as_dict(self, *, include_audit: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema": self.schema,
            "destination": str(self.destination),
            "file_count": self.file_count,
            "total_size": self.total_size,
            "prefix": self.prefix,
            "verified": self.verified,
            "replaced_existing": self.replaced_existing,
            "stale_files_removed": self.stale_files_removed,
            "excluded_file_count": self.excluded_file_count,
            "exclude": list(self.exclude),
            "strip_sources": self.strip_sources,
            "modcfg_modified": self.modcfg_modified,
        }
        if include_audit:
            value["audit_report"] = self.report.as_dict()
        return value


def _safe_archive_name(name: str) -> PurePosixPath:
    if not name or "\\" in name or "\0" in name:
        raise ValueError(f"Небезопасный путь ZIP: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"Небезопасный путь ZIP: {name!r}")
    if path.parts and ":" in path.parts[0]:
        raise ValueError(f"Небезопасный путь ZIP: {name!r}")
    return path


def verify_release_archive(
    archive_path: str | Path,
    manifest: dict[str, Any],
    *,
    prefix: str,
) -> dict[str, Any]:
    archive_path = Path(archive_path).resolve()
    expected: dict[str, dict[str, Any]] = {}
    for item in manifest.get("files", []):
        relative = PurePosixPath(str(item["path"]))
        archive_name = (PurePosixPath(prefix) / relative).as_posix() if prefix else relative.as_posix()
        expected[archive_name] = item

    with zipfile.ZipFile(archive_path, "r") as archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        names = [info.filename for info in infos]
        for name in names:
            _safe_archive_name(name)
        if len(names) != len(set(names)):
            raise ValueError("ZIP содержит точные дубли путей")
        folded = [name.casefold() for name in names]
        if len(folded) != len(set(folded)):
            raise ValueError("ZIP содержит пути, различающиеся только регистром")
        if set(names) != set(expected):
            missing = sorted(set(expected) - set(names))
            extra = sorted(set(names) - set(expected))
            raise ValueError(f"Состав ZIP не совпал с манифестом: missing={missing[:3]}, extra={extra[:3]}")
        if archive.testzip() is not None:
            raise ValueError("ZIP не прошёл проверку CRC")

        for info in infos:
            item = expected[info.filename]
            digest = hashlib.sha256()
            size = 0
            with archive.open(info, "r") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
            if size != int(item["size"]) or digest.hexdigest() != item["sha256"]:
                raise ValueError(f"ZIP не совпал с SHA-256-манифестом: {info.filename}")
    return {
        "archive": str(archive_path),
        "verified": True,
        "file_count": len(expected),
        "sha256": sha256_file(archive_path),
    }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sidecar_paths(output: Path) -> tuple[Path, Path]:
    return output.with_suffix(".manifest.json"), output.with_suffix(".audit.json")


def _ensure_outside_mod(mod_dir: Path, paths: Iterable[Path]) -> None:
    for path in paths:
        if path == mod_dir or mod_dir in path.parents:
            raise ValueError(f"Релиз и отчёты должны находиться вне дерева мода: {path}")


def _distribution_excludes(
    mod_dir: Path,
    exclude: Sequence[str],
    *,
    strip_sources: bool,
) -> tuple[str, ...]:
    patterns = list(exclude)
    if strip_sources:
        for child in mod_dir.iterdir():
            if child.is_dir() and child.name.casefold() in _SOURCE_ROOT_NAMES:
                patterns.append(f"{child.name}/**")
        patterns.extend(_SOURCE_FILE_PATTERNS)
    # Preserve order for reports while avoiding duplicate work.
    return tuple(dict.fromkeys(patterns))


def _manifest_files(manifest: dict[str, Any]) -> dict[str, tuple[int, str]]:
    return {
        str(item["path"]): (int(item["size"]), str(item["sha256"]))
        for item in manifest.get("files", [])
    }


def _deploy_destination(destination_root: Path, prefix: str) -> Path:
    safe_prefix = _safe_archive_name(prefix)
    destination = destination_root.joinpath(*safe_prefix.parts).resolve()
    if destination == destination_root or destination_root not in destination.parents:
        raise ValueError(f"Путь развёртывания вышел за каталог назначения: {destination}")
    return destination


def build_release(
    mod_dir: str | Path,
    output: str | Path,
    *,
    prefix: str | None = None,
    exclude: Sequence[str] = (),
    tools_root: str | Path | None = None,
    allow: Sequence[str] = (),
    warnings_as_errors: bool = False,
    overwrite: bool = False,
    strip_sources: bool = False,
) -> ReleaseResult:
    mod_dir = Path(mod_dir).resolve()
    output = Path(output).resolve()
    if output.suffix.casefold() != ".zip":
        raise ValueError("Релиз должен иметь расширение .zip")
    manifest_path, audit_path = _sidecar_paths(output)
    _ensure_outside_mod(mod_dir, (output, manifest_path, audit_path))
    destinations = (output, manifest_path, audit_path)
    if not overwrite:
        existing = next((path for path in destinations if path.exists()), None)
        if existing:
            raise FileExistsError(f"Результат уже существует: {existing}")

    archive_prefix = prefix if prefix is not None else mod_dir.name
    _safe_archive_name(archive_prefix)
    report = audit_mod(
        mod_dir,
        profile=AuditProfile.RELEASE,
        tools_root=tools_root,
        install_subpath=archive_prefix,
        allow=allow,
    )
    if report.blocking_issues(warnings_as_errors=warnings_as_errors):
        raise ReleaseBlockedError(report, warnings_as_errors=warnings_as_errors)

    effective_exclude = _distribution_excludes(
        mod_dir,
        exclude,
        strip_sources=strip_sources,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".srhd-release-", dir=output.parent) as temp_name:
        temp = Path(temp_name)
        staged = temp / "staged" / mod_dir.name
        stage_result = stage_tree(mod_dir, staged, exclude=effective_exclude)
        raw_manifest = build_manifest(staged)
        files = [
            {"path": item["path"], "size": item["size"], "sha256": item["sha256"]}
            for item in raw_manifest["files"]
        ]
        manifest: dict[str, Any] = {
            "schema": RELEASE_SCHEMA,
            "source": str(mod_dir),
            "root_name": mod_dir.name,
            "prefix": archive_prefix,
            "exclude": list(effective_exclude),
            "strip_sources": strip_sources,
            "file_count": len(files),
            "total_size": sum(item["size"] for item in files),
            "stage_verified": bool(stage_result["verified"]),
            "files": files,
        }

        temp_archive = temp / output.name
        pack_result = pack_mod(staged, temp_archive, prefix=archive_prefix)
        verification = verify_release_archive(temp_archive, manifest, prefix=archive_prefix)
        manifest["archive"] = {
            "name": output.name,
            "size": temp_archive.stat().st_size,
            "sha256": verification["sha256"],
            "verified": True,
        }
        temp_manifest = temp / manifest_path.name
        temp_audit = temp / audit_path.name
        _write_json(temp_manifest, manifest)
        _write_json(temp_audit, report.as_dict())

        os.replace(temp_manifest, manifest_path)
        os.replace(temp_audit, audit_path)
        os.replace(temp_archive, output)

    return ReleaseResult(
        output,
        manifest_path,
        audit_path,
        str(pack_result["sha256"]),
        output.stat().st_size,
        len(files),
        archive_prefix,
        True,
        report,
        effective_exclude,
        strip_sources,
    )


def deploy_mod(
    mod_dir: str | Path,
    destination_root: str | Path,
    *,
    prefix: str | None = None,
    exclude: Sequence[str] = (),
    strip_sources: bool = True,
    tools_root: str | Path | None = None,
    allow: Sequence[str] = (),
    warnings_as_errors: bool = False,
    overwrite: bool = False,
) -> DeployResult:
    """Audit and publish an exact game-ready mod directory without merging.

    The destination is staged next to the final directory, verified by SHA-256,
    and then swapped into place.  Existing contents are moved out first, so
    files removed from the source cannot survive a repeated deployment.
    ModCFG.txt is never read or changed by this operation.
    """

    mod_dir = Path(mod_dir).resolve()
    destination_root = Path(destination_root).resolve()
    if not mod_dir.is_dir():
        raise NotADirectoryError(mod_dir)
    archive_prefix = prefix if prefix is not None else mod_dir.name
    destination = _deploy_destination(destination_root, archive_prefix)
    if destination == mod_dir or destination in mod_dir.parents or mod_dir in destination.parents:
        raise ValueError("Исходный мод и папка развёртывания не должны быть вложены друг в друга")

    report = audit_mod(
        mod_dir,
        profile=AuditProfile.RELEASE,
        tools_root=tools_root,
        install_subpath=archive_prefix,
        allow=allow,
    )
    if report.blocking_issues(warnings_as_errors=warnings_as_errors):
        raise ReleaseBlockedError(report, warnings_as_errors=warnings_as_errors)

    effective_exclude = _distribution_excludes(
        mod_dir,
        exclude,
        strip_sources=strip_sources,
    )
    source_file_count = len(build_manifest(mod_dir)["files"])
    destination_root.mkdir(parents=True, exist_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not overwrite:
        raise FileExistsError(
            f"Папка назначения уже существует: {destination}. "
            "Для полной проверенной замены используйте --overwrite"
        )
    if destination.exists() and (not destination.is_dir() or destination.is_symlink()):
        raise ValueError(f"Нельзя заменить не-каталог или ссылку: {destination}")

    replaced_existing = destination.exists()
    old_manifest = build_manifest(destination) if replaced_existing else {"files": []}
    transaction = destination.parent / f".{destination.name}.srhd-deploy-{uuid.uuid4().hex}"
    transaction.mkdir()
    staged = transaction / "new"
    backup = transaction / "previous"
    published = False
    previous_moved = False
    try:
        stage_tree(mod_dir, staged, exclude=effective_exclude)
        staged_manifest = build_manifest(staged)
        expected_files = _manifest_files(staged_manifest)
        if replaced_existing:
            os.replace(destination, backup)
            previous_moved = True
        try:
            os.replace(staged, destination)
            published = True
            deployed_manifest = build_manifest(destination)
            if _manifest_files(deployed_manifest) != expected_files:
                raise OSError("Проверка развёрнутой папки по SHA-256 не пройдена")
        except Exception:
            try:
                if published and destination.exists():
                    os.replace(destination, staged)
                    published = False
                if previous_moved and backup.exists():
                    os.replace(backup, destination)
                    previous_moved = False
            except Exception as rollback_error:
                raise RuntimeError(
                    "Развёртывание не завершено и автоматический откат не удался; "
                    f"резервная копия сохранена в {backup}"
                ) from rollback_error
            raise

        old_files = set(_manifest_files(old_manifest))
        new_files = set(expected_files)
        result = DeployResult(
            destination=destination,
            file_count=len(expected_files),
            total_size=sum(size for size, _digest in expected_files.values()),
            prefix=archive_prefix,
            verified=True,
            replaced_existing=replaced_existing,
            stale_files_removed=len(old_files - new_files),
            excluded_file_count=source_file_count - len(expected_files),
            exclude=effective_exclude,
            strip_sources=strip_sources,
            report=report,
        )
        if backup.exists():
            shutil.rmtree(backup)
            previous_moved = False
        return result
    finally:
        # Never delete the only preserved copy when Windows prevented rollback.
        if transaction.exists() and not (previous_moved and backup.exists()):
            shutil.rmtree(transaction, ignore_errors=True)
