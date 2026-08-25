from __future__ import annotations

import hashlib
import errno
import json
import os
import shutil
import tempfile
import zipfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator, Sequence

from .audit import AuditProfile, AuditReport, audit_mod
from .files import build_manifest, pack_mod, safe_archive_name, sha256_file, stage_tree
from .safe_io import atomic_write_text, publish_files_transactionally


RELEASE_SCHEMA = "srhd-modkit-release-v1"
DEPLOY_SCHEMA = "srhd-modkit-deploy-v1"
DEPLOY_PLAN_SCHEMA = "srhd-modkit-deploy-plan-v1"
DEPLOY_TRANSACTION_SCHEMA = "srhd-modkit-deploy-transaction-v1"

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
    "srhd-modkit.toml",
    "**/srhd-modkit.toml",
    "srhd-modkit.local.toml",
    "**/srhd-modkit.local.toml",
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

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "srhd-modkit-release-blocked-v1",
            "blocked": True,
            "warnings_as_errors": self.warnings_as_errors,
            "message": str(self),
            "audit": self.report.as_dict(),
        }


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


@dataclass(frozen=True, slots=True)
class DeployPlan:
    source: Path
    destination_root: Path
    destination: Path
    prefix: str
    added: tuple[str, ...]
    changed: tuple[str, ...]
    removed: tuple[str, ...]
    identical: tuple[str, ...]
    excluded: tuple[str, ...]
    exclude: tuple[str, ...]
    strip_sources: bool
    file_count: int
    total_size: int
    report: AuditReport
    blocked: bool
    source_manifest: dict[str, Any]
    target_manifest: dict[str, Any]
    schema: str = DEPLOY_PLAN_SCHEMA

    def as_dict(self, *, include_audit: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema": self.schema,
            "source": str(self.source),
            "destination_root": str(self.destination_root),
            "destination": str(self.destination),
            "prefix": self.prefix,
            "blocked": self.blocked,
            "strip_sources": self.strip_sources,
            "exclude": list(self.exclude),
            "file_count": self.file_count,
            "total_size": self.total_size,
            "summary": {
                "added": len(self.added),
                "changed": len(self.changed),
                "removed": len(self.removed),
                "identical": len(self.identical),
                "excluded": len(self.excluded),
            },
            "added": list(self.added),
            "changed": list(self.changed),
            "removed": list(self.removed),
            "identical": list(self.identical),
            "excluded": list(self.excluded),
        }
        if include_audit:
            value["audit_report"] = self.report.as_dict()
        return value


def _safe_archive_name(name: str) -> PurePosixPath:
    return safe_archive_name(name)


def verify_release_archive(
    archive_path: str | Path,
    manifest: dict[str, Any],
    *,
    prefix: str,
) -> dict[str, Any]:
    archive_path = Path(archive_path).resolve()
    expected: dict[str, dict[str, Any]] = {}
    expected_folded: set[str] = set()
    safe_prefix = _safe_archive_name(prefix) if prefix else None
    for item in manifest.get("files", []):
        if not isinstance(item, dict):
            raise ValueError("Манифест содержит некорректную запись файла")
        relative = _safe_archive_name(str(item["path"]))
        archive_name = (
            (safe_prefix / relative).as_posix()
            if safe_prefix is not None
            else relative.as_posix()
        )
        _safe_archive_name(archive_name)
        if archive_name in expected:
            raise ValueError(f"Манифест содержит точный дубль пути: {archive_name}")
        folded_name = archive_name.casefold()
        if folded_name in expected_folded:
            raise ValueError(
                f"Манифест содержит пути, различающиеся только регистром: {archive_name}"
            )
        expected[archive_name] = item
        expected_folded.add(folded_name)

    with zipfile.ZipFile(archive_path, "r") as archive:
        all_infos = archive.infolist()
        normalized_names: list[str] = []
        for info in all_infos:
            normalized_names.append(_safe_archive_name(info.filename).as_posix())
        if len(normalized_names) != len(set(normalized_names)):
            raise ValueError("ZIP содержит точные дубли путей, включая каталоги")
        normalized_folded = [name.casefold() for name in normalized_names]
        if len(normalized_folded) != len(set(normalized_folded)):
            raise ValueError("ZIP содержит пути, различающиеся только регистром")

        infos = [info for info in all_infos if not info.is_dir()]
        names = [info.filename for info in infos]
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
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


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


def _manifest_index(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item["path"]).casefold(): {
            "path": str(item["path"]),
            "size": int(item["size"]),
            "sha256": str(item["sha256"]),
        }
        for item in manifest.get("files", [])
    }


def _deployment_diff(
    source_manifest: dict[str, Any],
    target_manifest: dict[str, Any],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    source = _manifest_index(source_manifest)
    target = _manifest_index(target_manifest)
    added: list[str] = []
    changed: list[str] = []
    removed: list[str] = []
    identical: list[str] = []
    for key in sorted(source.keys() | target.keys()):
        left = source.get(key)
        right = target.get(key)
        if right is None and left is not None:
            added.append(left["path"])
        elif left is None and right is not None:
            removed.append(right["path"])
        elif left is not None and right is not None:
            if (left["size"], left["sha256"]) == (right["size"], right["sha256"]):
                identical.append(left["path"])
            else:
                changed.append(left["path"])
    return tuple(added), tuple(changed), tuple(removed), tuple(identical)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_transaction(path: Path, value: dict[str, Any]) -> None:
    value["updated_at"] = _utc_now()
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


@contextmanager
def _deployment_lock(destination: Path) -> Iterator[Path]:
    lock_path = destination.parent / f".{destination.name}.srhd-deploy.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    stream = lock_path.open("a+b")
    locked = False
    try:
        stream.seek(0)
        if stream.read(1) == b"":
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise RuntimeError(
                f"Другая сборка уже изменяет эту папку: {destination}"
            ) from exc
        stream.seek(0)
        stream.truncate()
        stream.write(
            json.dumps(
                {"pid": os.getpid(), "destination": str(destination), "started_at": _utc_now()},
                ensure_ascii=False,
            ).encode("utf-8")
        )
        stream.flush()
        yield lock_path
    finally:
        if locked:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            finally:
                stream.close()
                lock_path.unlink(missing_ok=True)
        else:
            stream.close()


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _assert_no_link_components(path: Path) -> None:
    absolute = path.absolute()
    for component in reversed((absolute, *absolute.parents)):
        if _is_link_or_junction(component):
            raise ValueError(f"Путь развёртывания проходит через ссылку или junction: {component}")


def _deploy_destination(destination_root: Path, prefix: str) -> Path:
    safe_prefix = _safe_archive_name(prefix)
    raw_destination = destination_root.joinpath(*safe_prefix.parts)
    _assert_no_link_components(raw_destination)
    destination_root = destination_root.resolve()
    destination = raw_destination.resolve()
    if destination == destination_root or destination_root not in destination.parents:
        raise ValueError(f"Путь развёртывания вышел за каталог назначения: {destination}")
    return destination


def plan_deploy(
    mod_dir: str | Path,
    destination_root: str | Path,
    *,
    prefix: str | None = None,
    exclude: Sequence[str] = (),
    strip_sources: bool = True,
    tools_root: str | Path | None = None,
    allow: Sequence[str] = (),
    warnings_as_errors: bool = False,
) -> DeployPlan:
    mod_dir = Path(mod_dir).resolve()
    destination_root = Path(destination_root).absolute()
    _assert_no_link_components(destination_root)
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
    effective_exclude = _distribution_excludes(
        mod_dir,
        exclude,
        strip_sources=strip_sources,
    )
    full_manifest = build_manifest(mod_dir)
    source_manifest = build_manifest(mod_dir, exclude=effective_exclude)
    target_manifest = build_manifest(destination) if destination.is_dir() else {"files": []}
    added, changed, removed, identical = _deployment_diff(source_manifest, target_manifest)
    included_keys = set(_manifest_index(source_manifest))
    excluded_files = tuple(
        item["path"]
        for item in full_manifest.get("files", [])
        if str(item["path"]).casefold() not in included_keys
    )
    return DeployPlan(
        source=mod_dir,
        destination_root=destination_root,
        destination=destination,
        prefix=archive_prefix,
        added=added,
        changed=changed,
        removed=removed,
        identical=identical,
        excluded=excluded_files,
        exclude=effective_exclude,
        strip_sources=strip_sources,
        file_count=len(source_manifest.get("files", [])),
        total_size=sum(int(item["size"]) for item in source_manifest.get("files", [])),
        report=report,
        blocked=bool(report.blocking_issues(warnings_as_errors=warnings_as_errors)),
        source_manifest=source_manifest,
        target_manifest=target_manifest,
    )


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

        publish_files_transactionally(
            (
                (temp_manifest, manifest_path),
                (temp_audit, audit_path),
                (temp_archive, output),
            )
        )
        published_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        published_audit = json.loads(audit_path.read_text(encoding="utf-8"))
        if published_manifest != manifest or published_audit != report.as_dict():
            raise RuntimeError("Опубликованные sidecar-файлы не совпали с проверенным staging")
        published_verification = verify_release_archive(output, manifest, prefix=archive_prefix)
        if published_verification["sha256"] != verification["sha256"]:
            raise RuntimeError("SHA-256 ZIP изменился во время публикации")

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

    plan = plan_deploy(
        mod_dir,
        destination_root,
        prefix=prefix,
        exclude=exclude,
        strip_sources=strip_sources,
        tools_root=tools_root,
        allow=allow,
        warnings_as_errors=warnings_as_errors,
    )
    if plan.blocked:
        raise ReleaseBlockedError(plan.report, warnings_as_errors=warnings_as_errors)

    mod_dir = plan.source
    destination_root = plan.destination_root
    archive_prefix = plan.prefix
    destination = plan.destination
    effective_exclude = plan.exclude
    destination_root.mkdir(parents=True, exist_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _deployment_lock(destination):
        if destination.exists() and not overwrite:
            raise FileExistsError(
                f"Папка назначения уже существует: {destination}. "
                "Для полной проверенной замены используйте --overwrite"
            )
        if destination.exists() and (not destination.is_dir() or _is_link_or_junction(destination)):
            raise ValueError(f"Нельзя заменить не-каталог или ссылку: {destination}")

        replaced_existing = destination.exists()
        old_manifest = build_manifest(destination) if replaced_existing else {"files": []}
        transaction_id = uuid.uuid4().hex
        transaction = destination.parent / f".{destination.name}.srhd-deploy-{transaction_id}"
        transaction.mkdir()
        staged = transaction / "new"
        backup = transaction / "previous"
        metadata_path = transaction / "transaction.json"
        metadata: dict[str, Any] = {
            "schema": DEPLOY_TRANSACTION_SCHEMA,
            "id": transaction_id,
            "state": "preparing",
            "created_at": _utc_now(),
            "pid": os.getpid(),
            "source": str(mod_dir),
            "destination_root": str(destination_root),
            "destination": str(destination),
            "prefix": archive_prefix,
            "staged": str(staged),
            "backup": str(backup),
            "strip_sources": strip_sources,
            "expected_file_count": plan.file_count,
            "expected_total_size": plan.total_size,
        }
        _write_transaction(metadata_path, metadata)
        published = False
        previous_moved = False
        try:
            stage_tree(mod_dir, staged, exclude=effective_exclude)
            staged_manifest = build_manifest(staged)
            expected_files = _manifest_files(staged_manifest)
            if expected_files != _manifest_files(plan.source_manifest):
                raise OSError("Состав исходного мода изменился после dry-run-плана; повторите сборку")
            metadata["state"] = "staged"
            metadata["staged_manifest_sha256"] = hashlib.sha256(
                json.dumps(expected_files, sort_keys=True).encode("utf-8")
            ).hexdigest()
            _write_transaction(metadata_path, metadata)
            if replaced_existing:
                os.replace(destination, backup)
                previous_moved = True
                metadata["state"] = "previous-moved"
                _write_transaction(metadata_path, metadata)
            try:
                os.replace(staged, destination)
                published = True
                metadata["state"] = "published"
                _write_transaction(metadata_path, metadata)
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
                    metadata["state"] = "rolled-back"
                    _write_transaction(metadata_path, metadata)
                except Exception as rollback_error:
                    metadata["state"] = "rollback-failed"
                    metadata["rollback_error"] = str(rollback_error)
                    try:
                        _write_transaction(metadata_path, metadata)
                    except OSError:
                        pass
                    raise RuntimeError(
                        "Развёртывание не завершено и автоматический откат не удался; "
                        f"резервная копия сохранена в {backup}"
                    ) from rollback_error
                raise

            metadata["state"] = "verified"
            _write_transaction(metadata_path, metadata)
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
                excluded_file_count=len(plan.excluded),
                exclude=effective_exclude,
                strip_sources=strip_sources,
                report=plan.report,
            )
            if backup.exists():
                shutil.rmtree(backup)
                previous_moved = False
            metadata["state"] = "complete"
            _write_transaction(metadata_path, metadata)
            return result
        finally:
            # Never delete the only preserved copy when Windows prevented rollback.
            if transaction.exists() and not (previous_moved and backup.exists()):
                shutil.rmtree(transaction, ignore_errors=True)


def _find_transaction_dirs(root: Path) -> list[Path]:
    root = root.resolve()
    if not root.exists():
        return []
    result: list[Path] = []
    for current, dirs, _files in os.walk(root, followlinks=False):
        current_path = Path(current)
        matches = [
            name
            for name in dirs
            if name.startswith(".") and ".srhd-deploy-" in name
        ]
        for name in matches:
            result.append(current_path / name)
        dirs[:] = [name for name in dirs if name not in matches and not name.startswith(".srhd-")]
    return sorted(result, key=lambda item: str(item).casefold())


def _read_transaction(directory: Path) -> dict[str, Any]:
    metadata_path = directory / "transaction.json"
    metadata: dict[str, Any]
    try:
        raw = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata = raw if isinstance(raw, dict) else {}
        metadata_error = None
    except (OSError, json.JSONDecodeError) as exc:
        metadata = {}
        metadata_error = str(exc)
    backup = Path(str(metadata.get("backup") or directory / "previous"))
    staged = Path(str(metadata.get("staged") or directory / "new"))
    destination_value = metadata.get("destination")
    destination = Path(str(destination_value)) if destination_value else None
    return {
        "path": str(directory),
        "metadata": str(metadata_path),
        "metadata_valid": metadata_error is None,
        "metadata_error": metadata_error,
        "id": metadata.get("id"),
        "state": metadata.get("state", "unknown"),
        "created_at": metadata.get("created_at"),
        "updated_at": metadata.get("updated_at"),
        "pid": metadata.get("pid"),
        "source": metadata.get("source"),
        "destination": str(destination) if destination is not None else None,
        "destination_exists": bool(destination is not None and destination.exists()),
        "backup": str(backup),
        "backup_exists": backup.is_dir(),
        "staged": str(staged),
        "staged_exists": staged.is_dir(),
        "recovery_available": backup.is_dir(),
    }


def inspect_deployments(root: str | Path) -> dict[str, Any]:
    root = Path(root).resolve()
    transactions = [_read_transaction(path) for path in _find_transaction_dirs(root)]
    locks = sorted(
        str(path)
        for path in root.rglob(".*.srhd-deploy.lock")
        if path.is_file()
    ) if root.exists() else []
    return {
        "schema": "srhd-modkit-deployment-audit-v1",
        "root": str(root),
        "transactions": transactions,
        "locks": locks,
        "summary": {
            "transactions": len(transactions),
            "recoverable": sum(bool(item["recovery_available"]) for item in transactions),
            "locks": len(locks),
        },
    }


def _candidate_transactions_for_target(target: Path) -> list[Path]:
    prefix = f".{target.name}.srhd-deploy-"
    candidates = [
        path
        for path in target.parent.glob(f"{prefix}*")
        if path.is_dir()
    ]
    return sorted(candidates, key=lambda item: item.stat().st_mtime_ns, reverse=True)


def rollback_deployment(target: str | Path) -> dict[str, Any]:
    raw_target = Path(target).absolute()
    _assert_no_link_components(raw_target)
    target = raw_target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    candidates = _candidate_transactions_for_target(target)
    selected: Path | None = None
    selected_info: dict[str, Any] | None = None
    for candidate in candidates:
        info = _read_transaction(candidate)
        recorded_destination = info.get("destination")
        destination_matches = (
            recorded_destination is None
            or Path(str(recorded_destination)).resolve() == target
        )
        if info["backup_exists"] and destination_matches:
            selected = candidate
            selected_info = info
            break
    if selected is None or selected_info is None:
        raise FileNotFoundError(f"Для {target} не найдена сохранённая deploy-копия")

    metadata_path = selected / "transaction.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            metadata = {}
    except (OSError, json.JSONDecodeError):
        metadata = {}
    backup = Path(selected_info["backup"])
    displaced = selected / f"displaced-current-{uuid.uuid4().hex}"
    current_moved = False
    with _deployment_lock(target):
        try:
            if target.exists():
                if not target.is_dir() or _is_link_or_junction(target):
                    raise ValueError(f"Нельзя отвести не-каталог или ссылку: {target}")
                os.replace(target, displaced)
                current_moved = True
            os.replace(backup, target)
        except Exception:
            if current_moved and displaced.exists() and not target.exists():
                os.replace(displaced, target)
            raise
        metadata.update(
            {
                "schema": DEPLOY_TRANSACTION_SCHEMA,
                "state": "rolled-back-manual",
                "destination": str(target),
                "backup": str(backup),
                "displaced_current": str(displaced) if current_moved else None,
                "manual_rollback_at": _utc_now(),
            }
        )
        _write_transaction(metadata_path, metadata)
    return {
        "schema": "srhd-modkit-deployment-rollback-v1",
        "target": str(target),
        "transaction": str(selected),
        "restored": True,
        "displaced_current": str(displaced) if current_moved else None,
        "cleanup_required": True,
    }


def _pid_state(pid: object) -> str:
    """Return ``alive``, ``dead``, ``unknown`` or ``absent``.

    Access denied is deliberately not treated as a dead process: cleanup must
    preserve a possibly active deployment owned by another account/session.
    """

    if not isinstance(pid, int) or pid <= 0:
        return "absent"
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            return "dead" if error == 87 else "unknown"
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return "unknown"
            return "alive" if exit_code.value == still_active else "dead"
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return "alive"
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "unknown"
    except OSError as exc:
        return "dead" if exc.errno == errno.ESRCH else "unknown"


def _pid_is_running(pid: object) -> bool:
    """Backward-compatible boolean helper for callers that only need alive."""

    return _pid_state(pid) == "alive"


def cleanup_deployments(
    root: str | Path,
    *,
    apply: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    root = Path(root).resolve()
    inspected = inspect_deployments(root)
    removed: list[str] = []
    refused: list[dict[str, str]] = []
    planned: list[str] = []
    for item in inspected["transactions"]:
        path = Path(item["path"])
        pid = item.get("pid")
        pid_state = _pid_state(pid)
        active_states = {"preparing", "staged", "previous-moved", "published", "verified"}
        if pid_state in {"alive", "unknown"} and item.get("state") in active_states:
            refused.append(
                {
                    "path": str(path),
                    "reason": (
                        f"Транзакция принадлежит активному процессу PID {pid}"
                        if pid_state == "alive"
                        else f"Нельзя доказать завершение процесса PID {pid}; транзакция сохранена"
                    ),
                }
            )
            continue
        destination_text = item.get("destination")
        if not destination_text and not force:
            refused.append(
                {
                    "path": str(path),
                    "reason": "Нельзя доказать цель транзакции; используйте --force только после ручной проверки",
                }
            )
            continue
        if item["backup_exists"] and not force:
            refused.append(
                {
                    "path": str(path),
                    "reason": "Содержит единственную резервную копию; сначала выполните rollback либо добавьте --force",
                }
            )
            continue
        planned.append(str(path))
        if apply:
            if destination_text:
                destination = Path(str(destination_text)).absolute()
                _assert_no_link_components(destination)
                try:
                    with _deployment_lock(destination.resolve()):
                        shutil.rmtree(path)
                except RuntimeError as exc:
                    refused.append({"path": str(path), "reason": str(exc)})
                    planned.pop()
                    continue
            else:
                shutil.rmtree(path)
            removed.append(str(path))
    return {
        "schema": "srhd-modkit-deployment-cleanup-v1",
        "root": str(root),
        "apply": apply,
        "force": force,
        "planned": planned,
        "removed": removed,
        "refused": refused,
        "summary": {
            "planned": len(planned),
            "removed": len(removed),
            "refused": len(refused),
        },
    }
