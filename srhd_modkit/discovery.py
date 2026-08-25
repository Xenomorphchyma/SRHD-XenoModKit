from __future__ import annotations

import os
from pathlib import Path

from .models import ModRecord
from .module_info import INFO_FILE_NAME, find_module_info, parse_module_info


SKIP_DIRS = {".git", ".hg", ".svn", "__pycache__", ".pytest_cache"}


def _keep_directory(parent: Path, name: str) -> bool:
    path = parent / name
    return (
        name.casefold() not in SKIP_DIRS
        and not name.casefold().startswith(".srhd-")
        and not path.is_symlink()
        and not bool(getattr(path, "is_junction", lambda: False)())
    )


def _measure_tree(root: Path) -> tuple[int, int]:
    count = 0
    size = 0
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        dirs[:] = [d for d in dirs if _keep_directory(current_path, d)]
        for name in files:
            path = current_path / name
            if name.casefold().startswith(".srhd-") or path.is_symlink():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            count += 1
            size += stat.st_size
    return count, size


def load_mod(mod_dir: str | Path, *, collection_root: str | Path | None = None) -> ModRecord:
    mod_dir = Path(mod_dir).resolve()
    info_path = find_module_info(mod_dir)
    if info_path is None:
        raise FileNotFoundError(f"ModuleInfo.txt not found in {mod_dir}")
    collection = Path(collection_root).resolve() if collection_root else mod_dir.parent
    try:
        relative = mod_dir.relative_to(collection)
    except ValueError:
        relative = Path(mod_dir.name)
    file_count, total_size = _measure_tree(mod_dir)
    return ModRecord(
        root=mod_dir,
        relative_path=relative,
        module=parse_module_info(info_path),
        has_cfg=(mod_dir / "CFG").is_dir(),
        has_data=(mod_dir / "DATA").is_dir(),
        file_count=file_count,
        total_size=total_size,
    )


def discover_mods(root: str | Path, *, max_depth: int | None = None) -> list[ModRecord]:
    root = Path(root).resolve()
    direct = find_module_info(root)
    if direct is not None:
        return [load_mod(root, collection_root=root.parent)]

    found: list[ModRecord] = []
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        dirs[:] = sorted(
            [d for d in dirs if _keep_directory(current_path, d) and not d.startswith(".")],
            key=str.casefold,
        )
        depth = len(current_path.relative_to(root).parts)
        if max_depth is not None and depth >= max_depth:
            dirs[:] = []
        if any(name.casefold() == INFO_FILE_NAME for name in files):
            found.append(load_mod(current_path, collection_root=root))

    return sorted(found, key=lambda mod: mod.relative_path.as_posix().casefold())
