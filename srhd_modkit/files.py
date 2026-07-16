from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
import tempfile
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from .models import ModRecord
from .module_info import find_module_info


DEFAULT_EXCLUDE_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
DEFAULT_EXCLUDE_DIRS = {".git", ".hg", ".svn", "__pycache__", ".pytest_cache"}


def sha256_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def iter_files(root: str | Path, *, exclude: Iterable[str] = ()) -> list[Path]:
    root = Path(root).resolve()
    patterns = tuple(exclude)
    result: list[Path] = []
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(
            [
                d
                for d in dirs
                if d.casefold() not in DEFAULT_EXCLUDE_DIRS
                and not d.casefold().startswith(".srhd-")
            ],
            key=str.casefold,
        )
        current_path = Path(current)
        for name in sorted(files, key=str.casefold):
            path = current_path / name
            if path.is_symlink() or name.casefold() in DEFAULT_EXCLUDE_NAMES:
                continue
            rel = path.relative_to(root).as_posix()
            if any(fnmatch.fnmatch(rel, pattern) for pattern in patterns):
                continue
            result.append(path)
    return result


def build_manifest(root: str | Path, *, exclude: Iterable[str] = ()) -> dict[str, Any]:
    root = Path(root).resolve()
    entries: list[dict[str, Any]] = []
    for path in iter_files(root, exclude=exclude):
        stat = path.stat()
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": sha256_file(path),
            }
        )
    return {
        "schema": "srhd-modkit-manifest-v1",
        "root_name": root.name,
        "file_count": len(entries),
        "total_size": sum(item["size"] for item in entries),
        "files": entries,
    }


def _tree_map(root: Path) -> dict[str, Path]:
    return {path.relative_to(root).as_posix().casefold(): path for path in iter_files(root)}


def compare_trees(left: str | Path, right: str | Path) -> dict[str, Any]:
    left = Path(left).resolve()
    right = Path(right).resolve()
    left_map = _tree_map(left)
    right_map = _tree_map(right)
    added: list[str] = []
    removed: list[str] = []
    changed: list[str] = []
    identical: list[str] = []

    for key in sorted(left_map.keys() | right_map.keys()):
        left_path = left_map.get(key)
        right_path = right_map.get(key)
        if left_path is None:
            added.append(right_path.relative_to(right).as_posix())
        elif right_path is None:
            removed.append(left_path.relative_to(left).as_posix())
        elif left_path.stat().st_size != right_path.stat().st_size:
            changed.append(left_path.relative_to(left).as_posix())
        elif sha256_file(left_path) == sha256_file(right_path):
            identical.append(left_path.relative_to(left).as_posix())
        else:
            changed.append(left_path.relative_to(left).as_posix())
    return {
        "left": str(left),
        "right": str(right),
        "added": added,
        "removed": removed,
        "changed": changed,
        "identical": identical,
        "summary": {
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
            "identical": len(identical),
        },
    }


def find_duplicates(root: str | Path, *, min_size: int = 1) -> list[dict[str, Any]]:
    root = Path(root).resolve()
    by_size: dict[int, list[Path]] = defaultdict(list)
    for path in iter_files(root):
        size = path.stat().st_size
        if size >= min_size:
            by_size[size].append(path)

    groups: list[dict[str, Any]] = []
    for size, paths in by_size.items():
        if len(paths) < 2:
            continue
        by_hash: dict[str, list[Path]] = defaultdict(list)
        for path in paths:
            by_hash[sha256_file(path)].append(path)
        for digest, matches in by_hash.items():
            if len(matches) < 2:
                continue
            groups.append(
                {
                    "sha256": digest,
                    "size": size,
                    "copies": len(matches),
                    "recoverable": size * (len(matches) - 1),
                    "paths": [str(path) for path in sorted(matches, key=lambda item: str(item).casefold())],
                }
            )
    return sorted(groups, key=lambda group: (-group["recoverable"], group["paths"][0].casefold()))


def find_collisions(
    mods: list[ModRecord],
    *,
    data_only: bool = False,
    hash_files: bool = False,
) -> list[dict[str, Any]]:
    owners: dict[str, list[tuple[ModRecord, Path, str]]] = defaultdict(list)
    for mod in mods:
        for path in iter_files(mod.root):
            rel = path.relative_to(mod.root).as_posix()
            if data_only and not rel.casefold().startswith("data/"):
                continue
            owners[rel.casefold()].append((mod, path, rel))

    collisions: list[dict[str, Any]] = []
    for entries in owners.values():
        if len(entries) < 2:
            continue
        hashes = [sha256_file(path) for _, path, _ in entries] if hash_files else []
        collisions.append(
            {
                "path": entries[0][2],
                "mods": [mod.name for mod, _, _ in entries],
                "files": [str(path) for _, path, _ in entries],
                "identical": len(set(hashes)) == 1 if hashes else None,
                "sha256": hashes if hashes else None,
            }
        )
    return sorted(collisions, key=lambda item: item["path"].casefold())


def pack_mod(
    mod_dir: str | Path,
    output: str | Path,
    *,
    prefix: str | None = None,
    exclude: Iterable[str] = (),
) -> dict[str, Any]:
    mod_dir = Path(mod_dir).resolve()
    output = Path(output).resolve()
    if find_module_info(mod_dir) is None:
        raise FileNotFoundError(f"ModuleInfo.txt not found in {mod_dir}")
    output.parent.mkdir(parents=True, exist_ok=True)
    prefix = prefix or mod_dir.name
    temp = output.with_name(output.name + ".tmp")
    if temp.exists():
        temp.unlink()

    files = [path for path in iter_files(mod_dir, exclude=exclude) if path not in {output, temp}]
    try:
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in files:
                rel = path.relative_to(mod_dir).as_posix()
                archive_name = f"{prefix}/{rel}" if prefix else rel
                info = zipfile.ZipInfo(archive_name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                with path.open("rb") as source, archive.open(info, "w") as target:
                    while chunk := source.read(1024 * 1024):
                        target.write(chunk)
        os.replace(temp, output)
    except Exception:
        if temp.exists():
            temp.unlink()
        raise
    return {
        "output": str(output),
        "sha256": sha256_file(output),
        "file_count": len(files),
        "archive_size": output.stat().st_size,
        "prefix": prefix,
    }


def stage_tree(source: str | Path, destination: str | Path) -> dict[str, Any]:
    """Create a verified, byte-for-byte working copy of an arbitrary mod tree.

    Unknown and proprietary formats are copied without reinterpretation.  The
    destination must not exist, which makes a partial or accidental merge
    impossible.
    """
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if not source.is_dir():
        raise NotADirectoryError(source)
    if destination.exists():
        raise FileExistsError(f"Папка назначения уже существует: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    files = iter_files(source)
    with tempfile.TemporaryDirectory(prefix=f".{destination.name}.stage-", dir=destination.parent) as temp_name:
        temp = Path(temp_name)
        total_size = 0
        for path in files:
            relative = path.relative_to(source)
            target = temp / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            if path.stat().st_size != target.stat().st_size or sha256_file(path) != sha256_file(target):
                raise OSError(f"Проверка копии не пройдена: {relative}")
            total_size += target.stat().st_size
        os.replace(temp, destination)
    return {
        "source": str(source),
        "destination": str(destination),
        "file_count": len(files),
        "total_size": total_size,
        "verified": True,
    }
