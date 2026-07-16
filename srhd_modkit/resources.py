from __future__ import annotations

import struct
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


GAI_MAGIC = b"gai\0"
GI_MAGIC = b"gi\0\0"
HAI_MAGIC = 0x04210420


class ResourceFormatError(ValueError):
    """A binary resource is truncated or contradicts its own index."""


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
            "capabilities": ["inspect", "list", "extract-gi"],
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

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PkgInfo:
    path: Path
    size: int
    directory_depth: int
    folders: tuple[str, ...]
    normalized_folders: tuple[str, ...]
    entries: tuple[PkgEntry, ...]

    @property
    def uncompressed_size(self) -> int:
        return sum(entry.uncompressed_size for entry in self.entries)

    def summary(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "format": "resource package",
            "size": self.size,
            "directory_depth": self.directory_depth,
            "folders": list(self.folders),
            "normalized_folders": list(self.normalized_folders),
            "file_count": len(self.entries),
            "uncompressed_size": self.uncompressed_size,
            "valid": True,
            "capabilities": ["inspect", "list", "verify", "extract"],
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
    directory_depth = _u32(data, 0, "PKG directory depth")
    if directory_depth > 128:
        raise ResourceFormatError(f"PKG: неправдоподобная глубина каталогов {directory_depth}")

    position = 4
    folders: list[str] = []
    normalized_folders: list[str] = []
    for index in range(directory_depth + 1):
        if position + 170 > len(data):
            raise ResourceFormatError("PKG: таблица каталогов обрезана")
        record_size, kind, payload_size = struct.unpack_from("<III", data, position)
        if (record_size, kind, payload_size) != (170, 1, 158):
            raise ResourceFormatError(
                f"PKG: каталог {index} имеет заголовок {(record_size, kind, payload_size)}, ожидался (170, 1, 158)"
            )
        normalized = _safe_component(_fixed_name(data, position + 20, 63, "PKG folder"), "PKG folder")
        display = _safe_component(_fixed_name(data, position + 83, 63, "PKG folder"), "PKG folder")
        normalized_folders.append(normalized)
        folders.append(display)
        position += record_size

    if _u32(data, position, "PKG table marker") != 170:
        raise ResourceFormatError("PKG: отсутствует маркер таблицы файлов 170")
    file_count = _u32(data, position + 4, "PKG file count")
    if not file_count or file_count > 1_000_000:
        raise ResourceFormatError(f"PKG: неправдоподобное число файлов {file_count}")
    first_name = position + 20
    table_end = first_name + (file_count - 1) * 158 + 146
    if table_end > len(data):
        raise ResourceFormatError("PKG: таблица файлов обрезана")

    entries: list[PkgEntry] = []
    for index in range(file_count):
        name_offset = first_name + index * 158
        compressed_size, uncompressed_size = struct.unpack_from("<II", data, name_offset - 8)
        normalized = _safe_component(_fixed_name(data, name_offset, 63, "PKG file"), "PKG file")
        display = _safe_component(_fixed_name(data, name_offset + 63, 63, "PKG file"), "PKG file")
        data_offset = _u32(data, name_offset + 142, "PKG data offset")
        if not compressed_size:
            raise ResourceFormatError(f"PKG: {display} имеет нулевой сжатый размер")
        entries.append(PkgEntry(index, display, normalized, data_offset, compressed_size, uncompressed_size))

    folded_names = [entry.name.casefold() for entry in entries]
    if len(folded_names) != len(set(folded_names)):
        raise ResourceFormatError("PKG: повторяющиеся имена файлов без учёта регистра")
    for index, entry in enumerate(entries):
        expected_end = entries[index + 1].offset if index + 1 < len(entries) else len(data)
        if entry.offset < table_end or entry.offset + entry.compressed_size != expected_end:
            raise ResourceFormatError(
                f"PKG: индекс {entry.name} противоречит границам блока"
            )
    return PkgInfo(
        path,
        len(data),
        directory_depth,
        tuple(folders),
        tuple(normalized_folders),
        tuple(entries),
    )


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
        outputs = _prepare_outputs((base / entry.name for entry in info.entries), overwrite)
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
