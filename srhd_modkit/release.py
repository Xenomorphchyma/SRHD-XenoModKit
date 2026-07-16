from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence

from .audit import AuditProfile, AuditReport, audit_mod
from .files import build_manifest, pack_mod, sha256_file, stage_tree


RELEASE_SCHEMA = "srhd-modkit-release-v1"


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

    report = audit_mod(
        mod_dir,
        profile=AuditProfile.RELEASE,
        tools_root=tools_root,
        allow=allow,
    )
    if report.blocking_issues(warnings_as_errors=warnings_as_errors):
        raise ReleaseBlockedError(report, warnings_as_errors=warnings_as_errors)

    output.parent.mkdir(parents=True, exist_ok=True)
    archive_prefix = prefix if prefix is not None else mod_dir.name
    _safe_archive_name(archive_prefix)
    with tempfile.TemporaryDirectory(prefix=".srhd-release-", dir=output.parent) as temp_name:
        temp = Path(temp_name)
        staged = temp / "staged" / mod_dir.name
        stage_result = stage_tree(mod_dir, staged)
        raw_manifest = build_manifest(staged, exclude=exclude)
        files = [
            {"path": item["path"], "size": item["size"], "sha256": item["sha256"]}
            for item in raw_manifest["files"]
        ]
        manifest: dict[str, Any] = {
            "schema": RELEASE_SCHEMA,
            "source": str(mod_dir),
            "root_name": mod_dir.name,
            "prefix": archive_prefix,
            "exclude": list(exclude),
            "file_count": len(files),
            "total_size": sum(item["size"] for item in files),
            "stage_verified": bool(stage_result["verified"]),
            "files": files,
        }

        temp_archive = temp / output.name
        pack_result = pack_mod(staged, temp_archive, prefix=archive_prefix, exclude=exclude)
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
        tuple(exclude),
    )

