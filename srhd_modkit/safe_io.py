from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Iterable


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    """Replace one file without exposing a partially written destination."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(
        prefix=f".srhd-write-{destination.name}-",
        dir=destination.parent,
    )
    temporary = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(
    path: str | Path,
    text: str,
    *,
    encoding: str = "utf-8",
    errors: str = "strict",
) -> None:
    atomic_write_bytes(Path(path), text.encode(encoding, errors=errors))


def publish_files_transactionally(
    files: Iterable[tuple[str | Path, str | Path]],
) -> None:
    """Publish a verified file set with rollback if any replacement fails.

    Windows cannot atomically rename several unrelated files as one operation.
    Preparing sibling files first and retaining every previous destination until
    the complete set is installed gives callers the useful all-or-old contract.
    """

    pairs = [(Path(source), Path(destination)) for source, destination in files]
    if not pairs:
        return
    destinations = [str(destination.resolve()).casefold() for _source, destination in pairs]
    if len(destinations) != len(set(destinations)):
        raise ValueError("Транзакция публикации содержит повторяющиеся пути назначения")
    for source, destination in pairs:
        if not source.is_file() or source.is_symlink():
            raise FileNotFoundError(f"Публикуемый файл отсутствует или является ссылкой: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)

    transaction_id = uuid.uuid4().hex
    prepared: list[tuple[Path, Path]] = []
    backups: list[tuple[Path, Path]] = []
    published: list[Path] = []
    preserve_backups = False
    try:
        for source, destination in pairs:
            staged = destination.parent / f".srhd-publish-{transaction_id}-{destination.name}"
            shutil.copy2(source, staged)
            prepared.append((staged, destination))

        for _staged, destination in prepared:
            if destination.exists():
                if not destination.is_file() or destination.is_symlink():
                    raise ValueError(f"Нельзя транзакционно заменить не-файл или ссылку: {destination}")
                backup = destination.parent / f".srhd-backup-{transaction_id}-{destination.name}"
                os.replace(destination, backup)
                backups.append((backup, destination))

        for staged, destination in prepared:
            os.replace(staged, destination)
            published.append(destination)
    except Exception as exc:
        removal_errors: list[str] = []
        blocked_destinations: set[Path] = set()
        for destination in reversed(published):
            try:
                destination.unlink(missing_ok=True)
            except OSError as removal_exc:
                blocked_destinations.add(destination)
                removal_errors.append(f"{destination}: {removal_exc}")
        rollback_errors: list[str] = []
        for backup, destination in reversed(backups):
            if destination in blocked_destinations:
                # Never overwrite a file that could not be removed.  The only
                # old copy remains under its explicit .srhd-backup-* name.
                continue
            if backup.exists():
                try:
                    os.replace(backup, destination)
                except OSError as rollback_exc:
                    rollback_errors.append(f"{destination}: {rollback_exc}")
        if removal_errors or rollback_errors:
            preserve_backups = True
            raise RuntimeError(
                "Публикация не удалась, а часть резервных файлов требует ручного "
                "восстановления: " + "; ".join([*removal_errors, *rollback_errors])
            ) from exc
        raise
    finally:
        for staged, _destination in prepared:
            staged.unlink(missing_ok=True)
        for backup, destination in backups:
            if not preserve_backups and destination.exists():
                backup.unlink(missing_ok=True)
