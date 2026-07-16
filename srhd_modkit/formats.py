from __future__ import annotations

import struct
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from .files import iter_files, sha256_file


@dataclass(frozen=True)
class FormatSpec:
    name: str
    extensions: tuple[str, ...]
    category: str
    handling: str
    editor: str | None = None
    automatic_conversion: str | None = None
    signature: bytes | None = None
    signature_offset: int = 0

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["signature"] = self.signature.hex() if self.signature is not None else None
        return value


# "handling" is deliberately conservative.  A binary format is not marked as
# convertible unless the local toolchain has been exercised successfully.
FORMAT_SPECS: tuple[FormatSpec, ...] = (
    FormatSpec("GI image", (".gi",), "image", "convertible", "RangerTools", "png", b"gi\0\0"),
    FormatSpec("GAI animation", (".gai",), "animation", "headless", "SRHD ModKit", "gi", b"gai\0"),
    FormatSpec(
        "HAI animation/image",
        (".hai",),
        "animation",
        "inspectable",
        "SRHD ModKit + ResEditor",
        signature=b"\x20\x04\x21\x04",
    ),
    FormatSpec("BlockPar DAT", (".dat",), "structured-data", "headless", "SRHD ModKit + BlockParEditor CLI", "txt"),
    FormatSpec("resource package", (".pkg",), "package", "headless", "SRHD ModKit", "extract"),
    FormatSpec("compiled Ranger script", (".scr",), "script-binary", "inspectable", "SRHD ModKit + RScript"),
    FormatSpec("RSON script project", (".rson",), "structured-text", "headless", "SRHD ModKit + RScript", "scr/svr"),
    FormatSpec("SVR script project", (".svr",), "script-project", "headless", "RScript", "rson"),
    FormatSpec("PNG image", (".png",), "image", "convertible", automatic_conversion="gi", signature=b"\x89PNG\r\n\x1a\n"),
    FormatSpec("JPEG image", (".jpg", ".jpeg"), "image", "standard", signature=b"\xff\xd8\xff"),
    FormatSpec("BMP image", (".bmp",), "image", "standard", signature=b"BM"),
    FormatSpec("Photoshop image", (".psd",), "image-source", "standard", signature=b"8BPS"),
    FormatSpec("WAVE audio", (".wav",), "audio", "standard", signature=b"RIFF"),
    FormatSpec("Space Rangers video", (".vdo",), "video", "passthrough"),
    FormatSpec("ZIP archive", (".zip",), "archive", "standard", signature=b"PK\x03\x04"),
    FormatSpec("7-Zip archive", (".7z",), "archive", "standard", signature=b"7z\xbc\xaf\x27\x1c"),
    FormatSpec("Windows executable", (".exe",), "binary", "passthrough", signature=b"MZ"),
    FormatSpec("Windows library", (".dll",), "binary", "passthrough", signature=b"MZ"),
    FormatSpec("plain text", (".txt", ".ini", ".cfg", ".csv", ".md", ".log"), "text", "text"),
    FormatSpec("JSON", (".json",), "structured-text", "text"),
)

FORMAT_BY_EXTENSION = {
    extension: spec for spec in FORMAT_SPECS for extension in spec.extensions
}


def get_format_spec(path_or_extension: str | Path) -> FormatSpec | None:
    raw = str(path_or_extension)
    extension = raw.casefold() if raw.startswith(".") and "/" not in raw and "\\" not in raw else Path(raw).suffix.casefold()
    return FORMAT_BY_EXTENSION.get(extension)


def _dimensions(path: Path, spec: FormatSpec | None) -> dict[str, int]:
    if spec is None:
        return {}
    with path.open("rb") as stream:
        header = stream.read(32)
    if spec.name == "PNG image" and len(header) >= 24 and header.startswith(b"\x89PNG\r\n\x1a\n"):
        width, height = struct.unpack(">II", header[16:24])
        return {"width": width, "height": height}
    if spec.name == "GI image" and len(header) >= 24 and header.startswith(b"gi\0\0"):
        width, height = struct.unpack("<II", header[16:24])
        return {"width": width, "height": height}
    if spec.name == "GAI animation" and len(header) >= 28 and header.startswith(b"gai\0"):
        width, height, frame_count = struct.unpack("<III", header[16:28])
        return {"width": width, "height": height, "frame_count": frame_count}
    if spec.name == "HAI animation/image" and len(header) >= 20 and header.startswith(b"\x20\x04\x21\x04"):
        width, height, _canvas_size, frame_count = struct.unpack("<IIII", header[4:20])
        return {"width": width, "height": height, "frame_count": frame_count}
    return {}


def inspect_file(path: str | Path, *, include_hash: bool = False) -> dict[str, Any]:
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    spec = get_format_spec(path)
    signature_valid: bool | None = None
    if spec and spec.signature is not None:
        with path.open("rb") as stream:
            stream.seek(spec.signature_offset)
            actual = stream.read(max(len(spec.signature), 4))
            if spec.name == "ZIP archive":
                signature_valid = actual[:4] in {b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"}
            else:
                signature_valid = actual[: len(spec.signature)] == spec.signature
    result: dict[str, Any] = {
        "path": str(path),
        "extension": path.suffix.casefold(),
        "size": path.stat().st_size,
        "format": spec.name if spec else "unknown",
        "category": spec.category if spec else "unknown",
        "handling": spec.handling if spec else "passthrough",
        "editor": spec.editor if spec else None,
        "automatic_conversion": spec.automatic_conversion if spec else None,
        "signature_valid": signature_valid,
    }
    result.update(_dimensions(path, spec))
    if include_hash:
        result["sha256"] = sha256_file(path)
    return result


def scan_formats(root: str | Path, *, include_hash: bool = False) -> dict[str, Any]:
    root = Path(root).resolve()
    counts: Counter[str] = Counter()
    sizes: Counter[str] = Counter()
    invalid_signatures: list[dict[str, Any]] = []
    files = iter_files(root) if root.is_dir() else [root]
    for path in files:
        info = inspect_file(path, include_hash=include_hash)
        key = info["extension"] or "<без расширения>"
        counts[key] += 1
        sizes[key] += info["size"]
        if info["signature_valid"] is False:
            invalid_signatures.append(info)
    extensions = [
        {
            "extension": extension,
            "count": counts[extension],
            "size": sizes[extension],
            "format": (FORMAT_BY_EXTENSION.get(extension).name if extension in FORMAT_BY_EXTENSION else "unknown"),
            "handling": (FORMAT_BY_EXTENSION.get(extension).handling if extension in FORMAT_BY_EXTENSION else "passthrough"),
        }
        for extension in sorted(counts, key=lambda item: (-counts[item], item))
    ]
    return {
        "root": str(root),
        "file_count": sum(counts.values()),
        "total_size": sum(sizes.values()),
        "extensions": extensions,
        "invalid_signatures": invalid_signatures,
    }


def format_catalog() -> Iterable[FormatSpec]:
    return FORMAT_SPECS
