from __future__ import annotations

from pathlib import Path

from .models import ModuleEntry, ModuleInfo
from .textio import read_text


INFO_FILE_NAME = "moduleinfo.txt"


def find_module_info(mod_dir: str | Path) -> Path | None:
    mod_dir = Path(mod_dir)
    try:
        for child in mod_dir.iterdir():
            if child.is_file() and child.name.casefold() == INFO_FILE_NAME:
                return child
    except OSError:
        return None
    return None


def parse_module_info(path: str | Path) -> ModuleInfo:
    path = Path(path).resolve()
    decoded = read_text(path)
    result = ModuleInfo(path=path, encoding=decoded.encoding)
    current_entry: ModuleEntry | None = None

    for line_number, line in enumerate(decoded.text.splitlines(), start=1):
        raw = line.strip().lstrip("\ufeff")
        if not raw or raw.startswith(("#", ";", "//")):
            continue
        if "=" in raw:
            key, value = raw.split("=", 1)
            key = key.strip()
            if not key:
                result.malformed_lines.append((line_number, line))
                current_entry = None
                continue
            current_entry = ModuleEntry(key=key, value=value.strip(), line=line_number)
            result.entries.append(current_entry)
            continue
        if current_entry is not None:
            replacement = ModuleEntry(
                key=current_entry.key,
                value=current_entry.value + "\n" + raw,
                line=current_entry.line,
            )
            result.entries[-1] = replacement
            current_entry = replacement
        else:
            result.malformed_lines.append((line_number, line))

    return result

