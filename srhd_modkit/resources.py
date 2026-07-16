from __future__ import annotations

import hashlib
import os
import struct
import tempfile
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .files import sha256_file


GAI_MAGIC = b"gai\0"
GI_MAGIC = b"gi\0\0"
HAI_MAGIC = 0x04210420


class ResourceFormatError(ValueError):
    """A binary resource is truncated or contradicts its own index."""


class UnsupportedResourceFormat(ResourceFormatError):
    """The file uses a known alternative layout that is preserved as passthrough."""


def _u32(data: bytes | memoryview, offset: int, label: str) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise ResourceFormatError(f"{label}: нет 4 байт по смещению {offset}")
    return struct.unpack_from("<I", data, offset)[0]


def _fixed_name(data: bytes, offset: int, size: int, label: str) -> str:
    if offset < 0 or offset + size > len(data):
        raise ResourceFormatError(f"{label}: строка выходит за границы файла")
    raw = data[offset : offset + size].split(b"\0", 1)[0]
    try:
        return raw.decode("cp1251")
    except UnicodeDecodeError as exc:
        raise ResourceFormatError(f"{label}: имя не декодируется как Windows-1251") from exc


def _safe_component(value: str, label: str) -> str:
    if not value or value in {".", ".."} or any(char in value for char in "\\/:\0"):
        raise ResourceFormatError(f"{label}: небезопасное имя {value!r}")
    return value


@dataclass(frozen=True)
class GaiFrame:
    index: int
    offset: int
    size: int
    width: int
    height: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class GaiInfo:
    path: Path
    size: int
    version: int
    width: int
    height: int
    auxiliary_offset: int
    auxiliary_size: int
    frames: tuple[GaiFrame, ...]

    def summary(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "format": "GAI animation",
            "size": self.size,
            "version": self.version,
            "width": self.width,
            "height": self.height,
            "frame_count": len(self.frames),
            "auxiliary_offset": self.auxiliary_offset,
            "auxiliary_size": self.auxiliary_size,
            "valid": True,
            "capabilities": ["inspect", "list", "extract-gi", "build"],
        }

    def listing(self) -> dict[str, Any]:
        value = self.summary()
        value["frames"] = [frame.as_dict() for frame in self.frames]
        return value


def inspect_gai(path: str | Path) -> GaiInfo:
    path = Path(path).resolve()
    data = path.read_bytes()
    if len(data) < 56:
        raise ResourceFormatError(f"GAI слишком короткий: {len(data)} байт")
    if data[:4] != GAI_MAGIC:
        raise ResourceFormatError("GAI: неверная сигнатура")

    version = _u32(data, 4, "GAI version")
    width = _u32(data, 16, "GAI width")
    height = _u32(data, 20, "GAI height")
    frame_count = _u32(data, 24, "GAI frame count")
    auxiliary_offset = _u32(data, 32, "GAI auxiliary offset")
    auxiliary_size = _u32(data, 36, "GAI auxiliary size")
    if version != 1:
        raise ResourceFormatError(f"GAI: неподдерживаемая версия {version}")
    if not width or not height or not frame_count:
        raise ResourceFormatError("GAI: нулевой размер или число кадров")
    if frame_count > 1_000_000:
        raise ResourceFormatError(f"GAI: неправдоподобное число кадров {frame_count}")

    table_end = 48 + frame_count * 8
    if table_end > len(data):
        raise ResourceFormatError("GAI: таблица кадров обрезана")
    if auxiliary_offset == 0:
        if auxiliary_size != 0:
            raise ResourceFormatError("GAI: размер вспомогательного блока задан без смещения")
        expected_offset = table_end
    else:
        if auxiliary_offset != table_end:
            raise ResourceFormatError(
                f"GAI: вспомогательный блок начинается с {auxiliary_offset}, ожидалось {table_end}"
            )
        expected_offset = auxiliary_offset + auxiliary_size
        if expected_offset > len(data):
            raise ResourceFormatError("GAI: вспомогательный блок выходит за границы файла")

    frames: list[GaiFrame] = []
    for index in range(frame_count):
        offset, size = struct.unpack_from("<II", data, 48 + index * 8)
        if not size:
            raise ResourceFormatError(f"GAI: кадр {index} имеет нулевой размер")
        if offset != expected_offset:
            raise ResourceFormatError(
                f"GAI: кадр {index} начинается с {offset}, ожидалось {expected_offset}"
            )
        end = offset + size
        if end > len(data):
            raise ResourceFormatError(f"GAI: кадр {index} выходит за границы файла")
        if data[offset : offset + 4] != GI_MAGIC or size < 24:
            raise ResourceFormatError(f"GAI: кадр {index} не является вложенным GI")
        frame_width, frame_height = struct.unpack_from("<II", data, offset + 16)
        if not frame_width or not frame_height:
            raise ResourceFormatError(f"GAI: кадр {index} имеет нулевой размер GI")
        frames.append(GaiFrame(index, offset, size, frame_width, frame_height))
        expected_offset = end
    if expected_offset != len(data):
        raise ResourceFormatError(f"GAI: после кадров осталось {len(data) - expected_offset} байт")
    return GaiInfo(path, len(data), version, width, height, auxiliary_offset, auxiliary_size, tuple(frames))


@dataclass(frozen=True)
class HaiFrame:
    index: int
    offset: int
    size: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class HaiInfo:
    path: Path
    size: int
    width: int
    height: int
    canvas_size: int
    frame_count: int
    nominal_frame_size: int
    physical_frame_size: int
    palette_size: int
    frames: tuple[HaiFrame, ...]

    @property
    def standard_layout(self) -> bool:
        return self.nominal_frame_size == self.physical_frame_size

    def summary(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "format": "HAI animation/image",
            "size": self.size,
            "width": self.width,
            "height": self.height,
            "canvas_size": self.canvas_size,
            "frame_count": self.frame_count,
            "nominal_frame_size": self.nominal_frame_size,
            "physical_frame_size": self.physical_frame_size,
            "extra_bytes_per_frame": self.physical_frame_size - self.nominal_frame_size,
            "palette_size": self.palette_size,
            "standard_layout": self.standard_layout,
            "valid": True,
            "capabilities": ["inspect", "list"],
        }

    def listing(self) -> dict[str, Any]:
        value = self.summary()
        value["frames"] = [frame.as_dict() for frame in self.frames]
        return value


def inspect_hai(path: str | Path) -> HaiInfo:
    path = Path(path).resolve()
    data = path.read_bytes()
    if len(data) < 52:
        raise ResourceFormatError(f"HAI слишком короткий: {len(data)} байт")
    fields = struct.unpack_from("<13I", data)
    magic, width, height, canvas_size, frame_count, nominal_size = fields[:6]
    palette_size = fields[12]
    if magic != HAI_MAGIC:
        raise ResourceFormatError(f"HAI: неверный маркер 0x{magic:08x}")
    if not width or not height or not canvas_size or not frame_count or not nominal_size:
        raise ResourceFormatError("HAI: нулевой размер или число кадров")
    payload_size = len(data) - 52
    if payload_size % frame_count:
        raise ResourceFormatError("HAI: данные не делятся на целое число кадров")
    physical_size = payload_size // frame_count
    if physical_size < nominal_size:
        raise ResourceFormatError("HAI: физический кадр меньше заявленного")
    frames = tuple(
        HaiFrame(index, 52 + index * physical_size, physical_size) for index in range(frame_count)
    )
    return HaiInfo(
        path,
        len(data),
        width,
        height,
        canvas_size,
        frame_count,
        nominal_size,
        physical_size,
        palette_size,
        frames,
    )


@dataclass(frozen=True)
class PkgEntry:
    index: int
    name: str
    normalized_name: str
    offset: int
    compressed_size: int
    uncompressed_size: int
    folders: tuple[str, ...] = ()
    normalized_folders: tuple[str, ...] = ()
    kind: int = 2

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["path"] = self.relative_path.as_posix()
        return value

    @property
    def relative_path(self) -> Path:
        return Path(*self.folders, self.name)


@dataclass(frozen=True)
class PkgInfo:
    path: Path
    size: int
    directory_depth: int
    folders: tuple[str, ...]
    normalized_folders: tuple[str, ...]
    entries: tuple[PkgEntry, ...]
    directory_count: int = 0

    @property
    def uncompressed_size(self) -> int:
        return sum(entry.uncompressed_size for entry in self.entries)

    def summary(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "format": "resource package",
            "size": self.size,
            "header_value": self.directory_depth,
            "directory_depth": max((len(entry.folders) for entry in self.entries), default=0),
            "directory_count": self.directory_count,
            "folders": list(self.folders),
            "normalized_folders": list(self.normalized_folders),
            "file_count": len(self.entries),
            "uncompressed_size": self.uncompressed_size,
            "valid": True,
            "capabilities": ["inspect", "list", "verify", "extract", "build"],
        }

    def listing(self) -> dict[str, Any]:
        value = self.summary()
        value["files"] = [entry.as_dict() for entry in self.entries]
        return value

    def _block(self, entry: PkgEntry) -> bytes:
        with self.path.open("rb") as stream:
            stream.seek(entry.offset)
            block = stream.read(entry.compressed_size)
        if len(block) != entry.compressed_size:
            raise ResourceFormatError(f"PKG: блок {entry.name} обрезан")
        return block

    def decompress(self, entry: PkgEntry) -> bytes:
        block = self._block(entry)
        if entry.kind == 1:
            if len(block) < 4 or _u32(block, 0, "PKG raw block size") != len(block) - 4:
                raise ResourceFormatError(f"PKG: неверный размер несжатого блока {entry.name}")
            payload = block[4:]
            if len(payload) != entry.uncompressed_size:
                raise ResourceFormatError(
                    f"PKG: {entry.name} содержит {len(payload)} байт вместо {entry.uncompressed_size}"
                )
            return payload
        if len(block) < 14 or _u32(block, 0, "PKG block size") != len(block) - 4:
            raise ResourceFormatError(f"PKG: неверный размер блока {entry.name}")
        position = 4
        output = bytearray()
        while position < len(block):
            chunk_size = _u32(block, position, "PKG chunk size")
            chunk_start = position + 4
            chunk_end = chunk_start + chunk_size
            if chunk_size < 10 or chunk_end > len(block):
                raise ResourceFormatError(f"PKG: блок {entry.name}, неверная длина ZL02")
            chunk = block[chunk_start:chunk_end]
            if chunk[:4] != b"ZL02":
                raise ResourceFormatError(f"PKG: блок {entry.name}, отсутствует ZL02")
            expected = _u32(chunk, 4, "PKG raw chunk size")
            decoder = zlib.decompressobj()
            try:
                decoded = decoder.decompress(chunk[8:]) + decoder.flush()
            except zlib.error as exc:
                raise ResourceFormatError(f"PKG: zlib-ошибка в {entry.name}: {exc}") from exc
            if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                raise ResourceFormatError(f"PKG: ZL02-поток {entry.name} имеет лишние или неполные данные")
            if len(decoded) != expected:
                raise ResourceFormatError(
                    f"PKG: ZL02 {entry.name} дал {len(decoded)} байт вместо {expected}"
                )
            output.extend(decoded)
            position = chunk_end
        if position != len(block) or len(output) != entry.uncompressed_size:
            raise ResourceFormatError(
                f"PKG: {entry.name} распакован в {len(output)} байт вместо {entry.uncompressed_size}"
            )
        return bytes(output)

    def verify(self) -> dict[str, Any]:
        for entry in self.entries:
            self.decompress(entry)
        value = self.summary()
        value["verified_files"] = len(self.entries)
        value["verified_uncompressed_size"] = self.uncompressed_size
        return value


def inspect_pkg(path: str | Path) -> PkgInfo:
    path = Path(path).resolve()
    data = path.read_bytes()
    if len(data) < 32:
        raise ResourceFormatError(f"PKG слишком короткий: {len(data)} байт")
    if data[:4] == b"XPKG":
        raise UnsupportedResourceFormat("PKG использует альтернативный контейнер XPKG")
    header_value = _u32(data, 0, "PKG header")
    entries: list[PkgEntry] = []
    visited_tables: set[int] = set()
    active_tables: set[int] = set()
    directory_count = 0

    def parse_table(
        position: int,
        folders: tuple[str, ...],
        normalized_folders: tuple[str, ...],
    ) -> None:
        nonlocal directory_count
        if position in active_tables:
            raise ResourceFormatError(f"PKG: цикл таблиц по смещению {position}")
        if position in visited_tables:
            raise ResourceFormatError(f"PKG: таблица {position} используется несколькими каталогами")
        if position < 4 or position + 12 > len(data):
            raise ResourceFormatError(f"PKG: таблица по смещению {position} выходит за границы")
        record_size, count, payload_size = struct.unpack_from("<III", data, position)
        if record_size != 170 or payload_size not in {0, 158}:
            if position == 4:
                raise UnsupportedResourceFormat(
                    f"PKG использует неизвестный заголовок {(record_size, count, payload_size)}"
                )
            raise ResourceFormatError(
                f"PKG: таблица {position} имеет заголовок {(record_size, count, payload_size)}"
            )
        if not count or count > 1_000_000:
            raise ResourceFormatError(f"PKG: неправдоподобное число записей {count}")
        table_end = position + 12 + count * 158
        if table_end > len(data):
            raise ResourceFormatError(f"PKG: таблица {position} обрезана")
        visited_tables.add(position)
        active_tables.add(position)
        folded_names: set[str] = set()
        for table_index in range(count):
            entry_offset = position + 12 + table_index * 158
            compressed_size, uncompressed_size = struct.unpack_from("<II", data, entry_offset)
            normalized = _safe_component(
                _fixed_name(data, entry_offset + 8, 63, "PKG entry"), "PKG entry"
            )
            display = _safe_component(
                _fixed_name(data, entry_offset + 71, 63, "PKG entry"), "PKG entry"
            )
            first_kind, second_kind = struct.unpack_from("<II", data, entry_offset + 134)
            target = _u32(data, entry_offset + 150, "PKG entry offset")
            folded = display.casefold()
            if folded in folded_names:
                raise ResourceFormatError(
                    f"PKG: повторяющееся имя {display} в {'/'.join(folders) or '<root>'}"
                )
            folded_names.add(folded)
            is_directory = (first_kind, second_kind) == (3, 3)
            is_file = (first_kind, second_kind) == (2, 2)
            # Real archives also use (1, 1) and other file flags. Sizes are
            # the stable discriminator: directory entries always have zeroes.
            if compressed_size:
                is_file = True
                is_directory = False
            elif not compressed_size and not uncompressed_size:
                is_directory = True
                is_file = False
            # Old single-path fixtures did not populate the kind fields.
            legacy_kind = (first_kind, second_kind) == (0, 0)
            if not is_directory and not is_file and (first_kind, second_kind) == (0, 0):
                is_file = bool(compressed_size)
                is_directory = not is_file
            if is_directory:
                if compressed_size or uncompressed_size:
                    raise ResourceFormatError(f"PKG: каталог {display} содержит размеры файла")
                if legacy_kind and target == 0:
                    target = table_end
                directory_count += 1
                parse_table(
                    target,
                    folders + (display,),
                    normalized_folders + (normalized,),
                )
            elif is_file:
                if not compressed_size:
                    raise ResourceFormatError(f"PKG: {display} имеет нулевой сжатый размер")
                entries.append(
                    PkgEntry(
                        len(entries),
                        display,
                        normalized,
                        target,
                        compressed_size,
                        uncompressed_size,
                        folders,
                        normalized_folders,
                        first_kind if first_kind == second_kind else 0,
                    )
                )
            else:
                raise UnsupportedResourceFormat(
                    f"PKG: неизвестный тип записи {(first_kind, second_kind)} у {display}"
                )
        active_tables.remove(position)

    parse_table(4, (), ())
    if not entries:
        raise ResourceFormatError("PKG не содержит файлов")

    by_offset = sorted(entries, key=lambda item: item.offset)
    for index, entry in enumerate(by_offset):
        expected_end = by_offset[index + 1].offset if index + 1 < len(by_offset) else len(data)
        if entry.offset < 4 or entry.offset + entry.compressed_size > expected_end:
            raise ResourceFormatError(
                f"PKG: индекс {entry.name} противоречит границам блока"
            )
    common_folders = list(entries[0].folders)
    common_normalized = list(entries[0].normalized_folders)
    for entry in entries[1:]:
        while common_folders and tuple(common_folders) != entry.folders[: len(common_folders)]:
            common_folders.pop()
            common_normalized.pop()
    return PkgInfo(
        path,
        len(data),
        header_value,
        tuple(common_folders),
        tuple(common_normalized),
        tuple(entries),
        directory_count,
    )


def _publish_resource(staged: Path, output: Path, overwrite: bool) -> None:
    if output.exists() and not overwrite:
        raise FileExistsError(f"Результат уже существует: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staged, output)


def _collect_gi_frames(frames: Iterable[str | Path]) -> list[Path]:
    result: list[Path] = []
    for raw in frames:
        path = Path(raw).resolve()
        if path.is_dir():
            result.extend(
                sorted(
                    (item for item in path.iterdir() if item.is_file() and item.suffix.casefold() == ".gi"),
                    key=lambda item: item.name.casefold(),
                )
            )
        elif path.is_file() and path.suffix.casefold() == ".gi":
            result.append(path)
        else:
            raise ValueError(f"Ожидался GI-файл или каталог GI: {path}")
    if not result:
        raise ValueError("Не найдено ни одного GI-кадра")
    return result


def build_gai(
    frames: Iterable[str | Path],
    output: str | Path,
    *,
    template: str | Path | None = None,
    width: int | None = None,
    height: int | None = None,
    auxiliary: bytes | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    frame_paths = _collect_gi_frames(frames)
    output = Path(output).resolve()
    if output.suffix.casefold() != ".gai":
        raise ValueError("Результат GAI должен иметь расширение .gai")
    if output.exists() and not overwrite:
        raise FileExistsError(f"Результат уже существует: {output}")

    template_info: GaiInfo | None = None
    template_data = b""
    if template is not None:
        template_path = Path(template).resolve()
        template_info = inspect_gai(template_path)
        template_data = template_path.read_bytes()

    payloads: list[bytes] = []
    frame_dimensions: list[tuple[int, int]] = []
    for path in frame_paths:
        data = path.read_bytes()
        if len(data) < 24 or data[:4] != GI_MAGIC:
            raise ResourceFormatError(f"GAI: {path} не является корректным GI-кадром")
        frame_width, frame_height = struct.unpack_from("<II", data, 16)
        if not frame_width or not frame_height:
            raise ResourceFormatError(f"GAI: {path} содержит нулевой размер GI")
        payloads.append(data)
        frame_dimensions.append((frame_width, frame_height))

    canvas_width = width or (template_info.width if template_info else max(item[0] for item in frame_dimensions))
    canvas_height = height or (template_info.height if template_info else max(item[1] for item in frame_dimensions))
    if canvas_width <= 0 or canvas_height <= 0:
        raise ValueError("Размер холста GAI должен быть положительным")
    if any(frame_width > canvas_width or frame_height > canvas_height for frame_width, frame_height in frame_dimensions):
        raise ValueError("GI-кадр не помещается в заданный холст GAI")

    if auxiliary is None:
        if template_info and template_info.auxiliary_offset:
            start = template_info.auxiliary_offset
            auxiliary_payload = template_data[start : start + template_info.auxiliary_size]
        else:
            auxiliary_payload = b""
    else:
        auxiliary_payload = bytes(auxiliary)

    header = bytearray(template_data[:48] if template_info else bytes(48))
    struct.pack_into("<4sI", header, 0, GAI_MAGIC, 1)
    struct.pack_into("<III", header, 16, canvas_width, canvas_height, len(payloads))
    table_end = 48 + len(payloads) * 8
    auxiliary_offset = table_end if auxiliary_payload else 0
    struct.pack_into("<II", header, 32, auxiliary_offset, len(auxiliary_payload))
    table = bytearray(len(payloads) * 8)
    offset = table_end + len(auxiliary_payload)
    for index, payload in enumerate(payloads):
        struct.pack_into("<II", table, index * 8, offset, len(payload))
        offset += len(payload)

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".srhd-gai-", dir=output.parent) as name:
        staged = Path(name) / output.name
        staged.write_bytes(bytes(header) + bytes(table) + auxiliary_payload + b"".join(payloads))
        rebuilt = inspect_gai(staged)
        rebuilt_data = staged.read_bytes()
        for expected, frame in zip(payloads, rebuilt.frames):
            if rebuilt_data[frame.offset : frame.offset + frame.size] != expected:
                raise ResourceFormatError(f"GAI: кадр {frame.index} изменился при сборке")
        _publish_resource(staged, output, overwrite)
    return {
        "output": str(output),
        "format": "GAI animation",
        "frames": len(payloads),
        "width": canvas_width,
        "height": canvas_height,
        "auxiliary_size": len(auxiliary_payload),
        "size": output.stat().st_size,
        "sha256": sha256_file(output),
        "verified": True,
    }


def _pkg_name_bytes(value: str, label: str) -> bytes:
    _safe_component(value, label)
    try:
        encoded = value.encode("cp1251")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} нельзя представить в Windows-1251: {value!r}") from exc
    if len(encoded) >= 63:
        raise ValueError(f"{label} длиннее 62 байт Windows-1251: {value!r}")
    return encoded + bytes(63 - len(encoded))


@dataclass(slots=True)
class _PkgBuildFile:
    source: Path
    display: str
    normalized: str
    block: Path | None = None
    compressed_size: int = 0
    uncompressed_size: int = 0
    data_offset: int = 0


@dataclass(slots=True)
class _PkgBuildNode:
    display: str = ""
    normalized: str = ""
    dirs: dict[str, _PkgBuildNode] = field(default_factory=dict)
    files: dict[str, _PkgBuildFile] = field(default_factory=dict)
    table_offset: int = 0


def _pkg_add_directory(node: _PkgBuildNode, display: str) -> _PkgBuildNode:
    folded = display.casefold()
    if folded in node.files:
        raise ResourceFormatError(f"PKG: файл и каталог имеют одно имя: {display}")
    current = node.dirs.get(folded)
    if current is not None:
        if current.display != display:
            raise ResourceFormatError(f"PKG: каталоги различаются только регистром: {display}")
        return current
    current = _PkgBuildNode(display, display.upper())
    node.dirs[folded] = current
    return current


def _pkg_add_file(node: _PkgBuildNode, source: Path, display: str) -> _PkgBuildFile:
    folded = display.casefold()
    if folded in node.dirs or folded in node.files:
        raise ResourceFormatError(f"PKG: повторяющийся путь без учёта регистра: {display}")
    value = _PkgBuildFile(source, display, display.upper())
    node.files[folded] = value
    return value


def _pkg_entries(node: _PkgBuildNode) -> list[_PkgBuildNode | _PkgBuildFile]:
    return sorted(
        [*node.dirs.values(), *node.files.values()],
        key=lambda item: (item.display.casefold(), item.display),
    )


def _encode_pkg_block(source: Path, output: Path, chunk_size: int) -> tuple[int, int]:
    uncompressed = 0
    with source.open("rb") as input_stream, output.open("wb") as target:
        target.write(bytes(4))
        while raw := input_stream.read(chunk_size):
            uncompressed += len(raw)
            compressed = zlib.compress(raw, level=9)
            chunk = b"ZL02" + struct.pack("<I", len(raw)) + compressed
            target.write(struct.pack("<I", len(chunk)))
            target.write(chunk)
        if uncompressed == 0:
            compressed = zlib.compress(b"", level=9)
            chunk = b"ZL02" + struct.pack("<I", 0) + compressed
            target.write(struct.pack("<I", len(chunk)))
            target.write(chunk)
        size = target.tell()
        target.seek(0)
        target.write(struct.pack("<I", size - 4))
    return size, uncompressed


def build_pkg(
    source_dir: str | Path,
    output: str | Path,
    *,
    package_folders: Sequence[str] | None = None,
    template: str | Path | None = None,
    chunk_size: int = 1024 * 1024,
    overwrite: bool = False,
) -> dict[str, Any]:
    source_dir = Path(source_dir).resolve()
    output = Path(output).resolve()
    if not source_dir.is_dir():
        raise NotADirectoryError(source_dir)
    if output.suffix.casefold() != ".pkg":
        raise ValueError("Результат PKG должен иметь расширение .pkg")
    if output.exists() and not overwrite:
        raise FileExistsError(f"Результат уже существует: {output}")
    if chunk_size <= 0:
        raise ValueError("chunk_size должен быть положительным")

    template_info = inspect_pkg(template) if template is not None else None
    folders = tuple(package_folders) if package_folders is not None else (
        template_info.folders if template_info and template_info.folders else (source_dir.name,)
    )
    if not folders:
        raise ValueError("PKG должен иметь хотя бы один корневой каталог")
    for folder_name in folders:
        _pkg_name_bytes(folder_name, "PKG folder")

    source_files = sorted(
        (path for path in source_dir.rglob("*") if path.is_file()),
        key=lambda path: (path.relative_to(source_dir).as_posix().casefold(), path.relative_to(source_dir).as_posix()),
    )
    if not source_files:
        raise ValueError("Каталог PKG не содержит файлов")

    root = _PkgBuildNode()
    prefix_node = root
    for folder_name in folders:
        prefix_node = _pkg_add_directory(prefix_node, folder_name)
    build_files: list[_PkgBuildFile] = []
    for source in source_files:
        relative = source.relative_to(source_dir)
        node = prefix_node
        for component in relative.parts[:-1]:
            _pkg_name_bytes(component, "PKG folder")
            node = _pkg_add_directory(node, component)
        _pkg_name_bytes(relative.name, "PKG file")
        build_files.append(_pkg_add_file(node, source, relative.name))

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".srhd-pkg-", dir=output.parent) as name:
        temp = Path(name)
        for index, item in enumerate(build_files):
            block = temp / f"block-{index:06d}.bin"
            item.compressed_size, item.uncompressed_size = _encode_pkg_block(
                item.source, block, chunk_size
            )
            item.block = block

        nodes: list[_PkgBuildNode] = []
        cursor = 4

        def allocate(node: _PkgBuildNode) -> None:
            nonlocal cursor
            entries = _pkg_entries(node)
            if not entries:
                raise ResourceFormatError(f"PKG: пустой каталог {node.display!r}")
            node.table_offset = cursor
            nodes.append(node)
            cursor += 12 + len(entries) * 158
            for entry in entries:
                if isinstance(entry, _PkgBuildNode):
                    allocate(entry)

        allocate(root)
        data_cursor = cursor + 4
        ordered_files: list[_PkgBuildFile] = []
        for node in nodes:
            ordered_files.extend(
                entry for entry in _pkg_entries(node) if isinstance(entry, _PkgBuildFile)
            )
        for item in ordered_files:
            item.data_offset = data_cursor
            data_cursor += item.compressed_size

        header = bytearray(cursor + 4)
        header_value = template_info.directory_depth if template_info else 4
        struct.pack_into("<I", header, 0, header_value)
        for node in nodes:
            entries = _pkg_entries(node)
            struct.pack_into("<III", header, node.table_offset, 170, len(entries), 158)
            for index, entry in enumerate(entries):
                base = node.table_offset + 12 + index * 158
                header[base + 8 : base + 71] = _pkg_name_bytes(entry.normalized, "PKG normalized name")
                header[base + 71 : base + 134] = _pkg_name_bytes(entry.display, "PKG display name")
                if isinstance(entry, _PkgBuildNode):
                    struct.pack_into("<II", header, base + 134, 3, 3)
                    struct.pack_into("<I", header, base + 150, entry.table_offset)
                else:
                    struct.pack_into(
                        "<II", header, base, entry.compressed_size, entry.uncompressed_size
                    )
                    struct.pack_into("<II", header, base + 134, 2, 2)
                    struct.pack_into("<I", header, base + 150, entry.data_offset)

        staged = temp / output.name
        with staged.open("wb") as archive:
            archive.write(header)
            for item in ordered_files:
                if item.block is None:
                    raise AssertionError("PKG block was not encoded")
                with item.block.open("rb") as block:
                    while chunk := block.read(1024 * 1024):
                        archive.write(chunk)

        rebuilt = inspect_pkg(staged)
        rebuilt.verify()
        expected = {
            (Path(*folders) / path.relative_to(source_dir)).as_posix().casefold(): sha256_file(path)
            for path in source_files
        }
        actual = {
            entry.relative_path.as_posix().casefold(): hashlib.sha256(rebuilt.decompress(entry)).hexdigest()
            for entry in rebuilt.entries
        }
        if actual != expected:
            raise ResourceFormatError("PKG: проверка decode → encode → decode не совпала")
        _publish_resource(staged, output, overwrite)

    return {
        "output": str(output),
        "format": "resource package",
        "folders": list(folders),
        "files": len(source_files),
        "uncompressed_size": sum(path.stat().st_size for path in source_files),
        "size": output.stat().st_size,
        "sha256": sha256_file(output),
        "verified": True,
    }


def inspect_resource(path: str | Path, *, listing: bool = False) -> dict[str, Any]:
    path = Path(path).resolve()
    extension = path.suffix.casefold()
    if extension == ".gai":
        info = inspect_gai(path)
    elif extension == ".hai":
        info = inspect_hai(path)
    elif extension == ".pkg":
        info = inspect_pkg(path)
    else:
        raise ValueError("Поддерживаются ресурсы GAI, HAI и PKG")
    return info.listing() if listing else info.summary()


def verify_resource(path: str | Path) -> dict[str, Any]:
    path = Path(path).resolve()
    if path.suffix.casefold() == ".pkg":
        return inspect_pkg(path).verify()
    value = inspect_resource(path)
    value["verified"] = True
    return value


def _prepare_outputs(paths: Iterable[Path], overwrite: bool) -> list[Path]:
    outputs = list(paths)
    folded = [str(path).casefold() for path in outputs]
    if len(folded) != len(set(folded)):
        raise ResourceFormatError("Ресурс создаёт повторяющиеся пути")
    if not overwrite:
        existing = next((path for path in outputs if path.exists()), None)
        if existing is not None:
            raise FileExistsError(f"Результат уже существует: {existing}")
    return outputs


def extract_resource(path: str | Path, output: str | Path, *, overwrite: bool = False) -> dict[str, Any]:
    path = Path(path).resolve()
    output = Path(output).resolve()
    extension = path.suffix.casefold()
    if extension == ".gai":
        info = inspect_gai(path)
        data = path.read_bytes()
        outputs = _prepare_outputs(
            (output / f"{path.stem}_{frame.index:04d}.gi" for frame in info.frames), overwrite
        )
        output.mkdir(parents=True, exist_ok=True)
        for destination, frame in zip(outputs, info.frames):
            destination.write_bytes(data[frame.offset : frame.offset + frame.size])
        return {
            "source": str(path),
            "output": str(output),
            "format": "GAI animation",
            "files": len(outputs),
            "written_size": sum(frame.size for frame in info.frames),
        }
    if extension == ".pkg":
        info = inspect_pkg(path)
        # Verify the complete source before creating any output path.
        info.verify()
        base = output.joinpath(*info.folders)
        outputs = _prepare_outputs(
            (output.joinpath(*entry.folders, entry.name) for entry in info.entries), overwrite
        )
        resolved_output = output.resolve()
        for destination in outputs:
            if resolved_output != destination and resolved_output not in destination.parents:
                raise ResourceFormatError(f"PKG пытается выйти из каталога назначения: {destination}")
        written_size = 0
        for destination, entry in zip(outputs, info.entries):
            payload = info.decompress(entry)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
            written_size += len(payload)
        return {
            "source": str(path),
            "output": str(output),
            "package_root": str(base),
            "format": "resource package",
            "files": len(outputs),
            "written_size": written_size,
            "verified": True,
        }
    if extension == ".hai":
        raise ValueError("HAI пока поддерживается только для inspect/list/verify: схема пиксельных слоёв ещё не доказана")
    raise ValueError("Извлечение поддерживается для GAI и PKG")
