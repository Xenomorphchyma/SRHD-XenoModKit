from __future__ import annotations

import binascii
import struct
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
GI_MAGIC = b"gi\0\0"
_MAX_DECODED_BYTES = 512 * 1024 * 1024
_GI_HEADER_SIZE = 64
_GI_LAYER_SIZE = 32


class ImageFormatError(ValueError):
    """An image is truncated or contradicts its own binary structure."""


class UnsupportedImageFormat(ImageFormatError):
    """An identified image variant is preserved but cannot be decoded safely."""


@dataclass(frozen=True)
class RgbaImage:
    width: int
    height: int
    pixels: bytes

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("Размер изображения должен быть положительным")
        expected = self.width * self.height * 4
        if len(self.pixels) != expected:
            raise ValueError(f"RGBA содержит {len(self.pixels)} байт вместо {expected}")

    def pixel(self, x: int, y: int) -> tuple[int, int, int, int]:
        if not 0 <= x < self.width or not 0 <= y < self.height:
            raise IndexError((x, y))
        offset = (y * self.width + x) * 4
        return tuple(self.pixels[offset : offset + 4])  # type: ignore[return-value]


@dataclass(frozen=True)
class GiLayerInfo:
    index: int
    offset: int
    size: int
    start_x: int
    start_y: int
    finish_x: int
    finish_y: int
    unknown1: int
    unknown2: int

    @property
    def width(self) -> int:
        return self.finish_x - self.start_x

    @property
    def height(self) -> int:
        return self.finish_y - self.start_y

    def as_dict(self) -> dict[str, int]:
        value = asdict(self)
        value["width"] = self.width
        value["height"] = self.height
        return value


@dataclass(frozen=True)
class GiInfo:
    path: Path | None
    size: int
    version: int
    start_x: int
    start_y: int
    finish_x: int
    finish_y: int
    red_mask: int
    green_mask: int
    blue_mask: int
    alpha_mask: int
    frame_type: int
    layers: tuple[GiLayerInfo, ...]

    @property
    def width(self) -> int:
        return self.finish_x - self.start_x

    @property
    def height(self) -> int:
        return self.finish_y - self.start_y

    @property
    def supported(self) -> bool:
        return self.frame_type in {0, 2} and self.width > 0 and self.height > 0

    def summary(self) -> dict[str, Any]:
        return {
            "path": str(self.path) if self.path is not None else None,
            "format": "GI image",
            "size": self.size,
            "version": self.version,
            "width": self.width,
            "height": self.height,
            "frame_type": self.frame_type,
            "layer_count": len(self.layers),
            "supported": self.supported,
            "capabilities": ["inspect", "decode", "encode"] if self.supported else ["inspect"],
        }

    def listing(self) -> dict[str, Any]:
        value = self.summary()
        value["layers"] = [layer.as_dict() for layer in self.layers]
        return value


def _checked_image_size(width: int, height: int, channels: int = 4) -> None:
    if width <= 0 or height <= 0:
        raise ImageFormatError("Изображение имеет нулевой размер")
    if width > 0x7FFFFFFF or height > 0x7FFFFFFF:
        raise ImageFormatError("Размер изображения не помещается в координаты формата")
    if width * height * channels > _MAX_DECODED_BYTES:
        raise ImageFormatError(
            f"Распакованное изображение превышает безопасный лимит {_MAX_DECODED_BYTES} байт"
        )


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    crc = binascii.crc32(kind)
    crc = binascii.crc32(payload, crc) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def _paeth(left: int, above: int, upper_left: int) -> int:
    prediction = left + above - upper_left
    left_distance = abs(prediction - left)
    above_distance = abs(prediction - above)
    upper_left_distance = abs(prediction - upper_left)
    if left_distance <= above_distance and left_distance <= upper_left_distance:
        return left
    if above_distance <= upper_left_distance:
        return above
    return upper_left


def encode_png(image: RgbaImage) -> bytes:
    """Encode a deterministic non-interlaced RGBA8 PNG using only the standard library."""

    _checked_image_size(image.width, image.height)
    rows = bytearray()
    stride = image.width * 4
    for y in range(image.height):
        row = image.pixels[y * stride : (y + 1) * stride]
        # Filter 0 keeps encoding linear-time in pure Python. Compression still
        # happens in the native zlib implementation, and the result is stable.
        rows.append(0)
        rows.extend(row)
    header = struct.pack(">IIBBBBB", image.width, image.height, 8, 6, 0, 0, 0)
    compressed = zlib.compress(bytes(rows), level=9)
    return PNG_MAGIC + _png_chunk(b"IHDR", header) + _png_chunk(b"IDAT", compressed) + _png_chunk(b"IEND", b"")


def _unfilter_png_rows(raw: bytes, width: int, height: int, bits_per_pixel: int) -> list[bytes]:
    row_bytes = (width * bits_per_pixel + 7) // 8
    filter_bpp = max(1, (bits_per_pixel + 7) // 8)
    expected = height * (row_bytes + 1)
    if len(raw) != expected:
        raise ImageFormatError(f"PNG IDAT содержит {len(raw)} байт вместо {expected}")
    result: list[bytes] = []
    position = 0
    previous = bytes(row_bytes)
    for _ in range(height):
        filter_type = raw[position]
        position += 1
        encoded = raw[position : position + row_bytes]
        position += row_bytes
        if filter_type > 4:
            raise ImageFormatError(f"PNG использует неизвестный фильтр {filter_type}")
        if filter_type == 0:
            result.append(encoded)
            previous = encoded
            continue
        row = bytearray(row_bytes)
        for index, value in enumerate(encoded):
            left = row[index - filter_bpp] if index >= filter_bpp else 0
            above = previous[index]
            upper_left = previous[index - filter_bpp] if index >= filter_bpp else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = above
            elif filter_type == 3:
                predictor = (left + above) // 2
            else:
                predictor = _paeth(left, above, upper_left)
            row[index] = (value + predictor) & 0xFF
        value = bytes(row)
        result.append(value)
        previous = value
    return result


def _samples(row: bytes, count: int, bit_depth: int) -> list[int]:
    if bit_depth == 8:
        if len(row) < count:
            raise ImageFormatError("PNG-строка обрезана")
        return list(row[:count])
    if bit_depth == 16:
        if len(row) < count * 2:
            raise ImageFormatError("16-битная PNG-строка обрезана")
        return [struct.unpack_from(">H", row, index * 2)[0] for index in range(count)]
    mask = (1 << bit_depth) - 1
    values: list[int] = []
    for index in range(count):
        bit_offset = index * bit_depth
        shift = 8 - bit_depth - (bit_offset % 8)
        values.append((row[bit_offset // 8] >> shift) & mask)
    return values


def _scale_sample(value: int, bit_depth: int) -> int:
    maximum = (1 << bit_depth) - 1
    return (value * 255 + maximum // 2) // maximum


def _decode_png_row(
    row: bytes,
    width: int,
    color_type: int,
    bit_depth: int,
    palette: tuple[tuple[int, int, int], ...],
    transparency: bytes | None,
) -> bytes:
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    if color_type == 6 and bit_depth == 8:
        expected = width * 4
        if len(row) < expected:
            raise ImageFormatError("PNG-строка RGBA8 обрезана")
        return row[:expected]
    values = _samples(row, width * channels, bit_depth)
    output = bytearray(width * 4)
    transparent_gray = struct.unpack(">H", transparency)[0] if color_type == 0 and transparency else None
    transparent_rgb = struct.unpack(">HHH", transparency) if color_type == 2 and transparency else None
    for x in range(width):
        source = x * channels
        target = x * 4
        if color_type == 0:
            gray_raw = values[source]
            gray = _scale_sample(gray_raw, bit_depth)
            alpha = 0 if transparent_gray == gray_raw else 255
            output[target : target + 4] = bytes((gray, gray, gray, alpha))
        elif color_type == 2:
            raw_rgb = tuple(values[source : source + 3])
            rgb = tuple(_scale_sample(item, bit_depth) for item in raw_rgb)
            alpha = 0 if transparent_rgb == raw_rgb else 255
            output[target : target + 4] = bytes((*rgb, alpha))
        elif color_type == 3:
            index = values[source]
            if index >= len(palette):
                raise ImageFormatError(f"PNG ссылается на отсутствующий цвет палитры {index}")
            alpha = transparency[index] if transparency and index < len(transparency) else 255
            output[target : target + 4] = bytes((*palette[index], alpha))
        elif color_type == 4:
            gray = _scale_sample(values[source], bit_depth)
            alpha = _scale_sample(values[source + 1], bit_depth)
            output[target : target + 4] = bytes((gray, gray, gray, alpha))
        else:
            output[target : target + 4] = bytes(
                _scale_sample(values[source + channel], bit_depth) for channel in range(4)
            )
    return bytes(output)


def _adam7_passes(width: int, height: int) -> Iterable[tuple[int, int, int, int, int, int]]:
    for start_x, start_y, step_x, step_y in (
        (0, 0, 8, 8),
        (4, 0, 8, 8),
        (0, 4, 4, 8),
        (2, 0, 4, 4),
        (0, 2, 2, 4),
        (1, 0, 2, 2),
        (0, 1, 1, 2),
    ):
        pass_width = max(0, (width - start_x + step_x - 1) // step_x)
        pass_height = max(0, (height - start_y + step_y - 1) // step_y)
        if pass_width and pass_height:
            yield start_x, start_y, step_x, step_y, pass_width, pass_height


def decode_png(data: bytes | bytearray | memoryview) -> RgbaImage:
    """Decode standard PNG color types, bit depths and Adam7 interlacing to RGBA8."""

    data = bytes(data)
    if not data.startswith(PNG_MAGIC):
        raise ImageFormatError("PNG: неверная сигнатура")
    position = len(PNG_MAGIC)
    header: tuple[int, int, int, int, int, int, int] | None = None
    palette: tuple[tuple[int, int, int], ...] = ()
    transparency: bytes | None = None
    compressed = bytearray()
    saw_iend = False
    while position < len(data):
        if position + 12 > len(data):
            raise ImageFormatError("PNG: заголовок chunk обрезан")
        size = struct.unpack_from(">I", data, position)[0]
        kind = data[position + 4 : position + 8]
        end = position + 12 + size
        if end > len(data):
            raise ImageFormatError(f"PNG: chunk {kind!r} выходит за границы файла")
        payload = data[position + 8 : position + 8 + size]
        actual_crc = struct.unpack_from(">I", data, position + 8 + size)[0]
        expected_crc = binascii.crc32(kind)
        expected_crc = binascii.crc32(payload, expected_crc) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise ImageFormatError(f"PNG: неверная CRC у chunk {kind.decode('ascii', 'replace')}")
        if kind == b"IHDR":
            if header is not None or position != len(PNG_MAGIC) or size != 13:
                raise ImageFormatError("PNG: неверное расположение или размер IHDR")
            header = struct.unpack(">IIBBBBB", payload)
        elif kind == b"PLTE":
            if not size or size % 3 or size > 768:
                raise ImageFormatError("PNG: неверный размер PLTE")
            palette = tuple(
                (payload[index], payload[index + 1], payload[index + 2])
                for index in range(0, size, 3)
            )
        elif kind == b"tRNS":
            transparency = payload
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            if size:
                raise ImageFormatError("PNG: IEND должен быть пустым")
            saw_iend = True
            position = end
            break
        elif kind and 65 <= kind[0] <= 90:
            raise UnsupportedImageFormat(f"PNG: неизвестный обязательный chunk {kind!r}")
        position = end
    if not saw_iend or position != len(data):
        raise ImageFormatError("PNG: отсутствует IEND или после него есть лишние данные")
    if header is None or not compressed:
        raise ImageFormatError("PNG: отсутствует IHDR или IDAT")
    width, height, bit_depth, color_type, compression, filtering, interlace = header
    _checked_image_size(width, height)
    allowed_depths = {
        0: {1, 2, 4, 8, 16},
        2: {8, 16},
        3: {1, 2, 4, 8},
        4: {8, 16},
        6: {8, 16},
    }
    if color_type not in allowed_depths or bit_depth not in allowed_depths[color_type]:
        raise UnsupportedImageFormat(
            f"PNG: неподдерживаемая комбинация color_type={color_type}, bit_depth={bit_depth}"
        )
    if compression != 0 or filtering != 0 or interlace not in {0, 1}:
        raise UnsupportedImageFormat("PNG: неподдерживаемый метод сжатия, фильтрации или interlace")
    if color_type == 3 and not palette:
        raise ImageFormatError("PNG с палитрой не содержит PLTE")
    if color_type == 3 and len(palette) > 1 << bit_depth:
        raise ImageFormatError("PNG: палитра больше допустимого числа индексов")
    expected_trns = {0: 2, 2: 6}
    if transparency is not None:
        if color_type in expected_trns and len(transparency) != expected_trns[color_type]:
            raise ImageFormatError("PNG: неверный размер tRNS")
        if color_type == 3 and len(transparency) > len(palette):
            raise ImageFormatError("PNG: tRNS длиннее палитры")
        if color_type in {4, 6}:
            raise ImageFormatError("PNG: tRNS недопустим для изображения с альфа-каналом")

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    bits_per_pixel = channels * bit_depth
    if interlace == 0:
        expected_raw_size = height * (1 + (width * bits_per_pixel + 7) // 8)
    else:
        expected_raw_size = sum(
            pass_height * (1 + (pass_width * bits_per_pixel + 7) // 8)
            for _x, _y, _dx, _dy, pass_width, pass_height in _adam7_passes(width, height)
        )
    if expected_raw_size > _MAX_DECODED_BYTES:
        raise ImageFormatError("PNG: распакованный IDAT превышает безопасный лимит")
    decoder = zlib.decompressobj()
    try:
        raw = decoder.decompress(bytes(compressed), expected_raw_size + 1)
        if decoder.unconsumed_tail or len(raw) > expected_raw_size:
            raise ImageFormatError("PNG: распакованный IDAT длиннее ожидаемого")
        raw += decoder.flush()
    except zlib.error as exc:
        raise ImageFormatError(f"PNG: ошибка zlib: {exc}") from exc
    if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ImageFormatError("PNG: поток IDAT неполный или содержит лишние данные")
    if len(raw) != expected_raw_size:
        raise ImageFormatError(
            f"PNG: IDAT распакован в {len(raw)} байт вместо {expected_raw_size}"
        )

    output = bytearray(width * height * 4)
    if interlace == 0:
        rows = _unfilter_png_rows(raw, width, height, bits_per_pixel)
        for y, row in enumerate(rows):
            decoded = _decode_png_row(row, width, color_type, bit_depth, palette, transparency)
            output[y * width * 4 : (y + 1) * width * 4] = decoded
    else:
        raw_position = 0
        for start_x, start_y, step_x, step_y, pass_width, pass_height in _adam7_passes(width, height):
            row_bytes = (pass_width * bits_per_pixel + 7) // 8
            pass_size = pass_height * (row_bytes + 1)
            pass_raw = raw[raw_position : raw_position + pass_size]
            if len(pass_raw) != pass_size:
                raise ImageFormatError("PNG: Adam7-проход обрезан")
            raw_position += pass_size
            rows = _unfilter_png_rows(pass_raw, pass_width, pass_height, bits_per_pixel)
            for pass_y, row in enumerate(rows):
                decoded = _decode_png_row(
                    row, pass_width, color_type, bit_depth, palette, transparency
                )
                y = start_y + pass_y * step_y
                for pass_x in range(pass_width):
                    x = start_x + pass_x * step_x
                    source = pass_x * 4
                    target = (y * width + x) * 4
                    output[target : target + 4] = decoded[source : source + 4]
        if raw_position != len(raw):
            raise ImageFormatError("PNG: после Adam7-проходов остались лишние данные")
    return RgbaImage(width, height, bytes(output))


def read_png(path: str | Path) -> RgbaImage:
    return decode_png(Path(path).read_bytes())


def write_png(image: RgbaImage, path: str | Path) -> None:
    Path(path).write_bytes(encode_png(image))


def _parse_gi(data: bytes, path: Path | None = None) -> GiInfo:
    if len(data) < _GI_HEADER_SIZE:
        raise ImageFormatError(f"GI слишком короткий: {len(data)} байт")
    if data[:4] != GI_MAGIC:
        raise ImageFormatError("GI: неверная сигнатура")
    version = struct.unpack_from("<I", data, 4)[0]
    start_x, start_y, finish_x, finish_y = struct.unpack_from("<iiii", data, 8)
    red, green, blue, alpha, frame_type, layer_count = struct.unpack_from("<IIIIII", data, 24)
    unknown = struct.unpack_from("<IIII", data, 48)
    if version != 1:
        raise UnsupportedImageFormat(f"GI: неподдерживаемая версия {version}")
    width = finish_x - start_x
    height = finish_y - start_y
    if width < 0 or height < 0:
        raise ImageFormatError("GI: конечные координаты меньше начальных")
    if width and height:
        _checked_image_size(width, height)
    elif width or height:
        raise ImageFormatError("GI: только одна координата холста имеет нулевой размер")
    if any(unknown):
        raise UnsupportedImageFormat(f"GI: неизвестные поля заголовка {unknown}")
    if not layer_count or layer_count > 64:
        raise ImageFormatError(f"GI: неправдоподобное число слоёв {layer_count}")
    table_end = _GI_HEADER_SIZE + layer_count * _GI_LAYER_SIZE
    if table_end > len(data):
        raise ImageFormatError("GI: таблица слоёв обрезана")
    layers: list[GiLayerInfo] = []
    occupied: list[tuple[int, int, int]] = []
    for index in range(layer_count):
        values = struct.unpack_from("<IIiiiiii", data, _GI_HEADER_SIZE + index * _GI_LAYER_SIZE)
        offset, size, layer_start_x, layer_start_y, layer_finish_x, layer_finish_y, u1, u2 = values
        layer = GiLayerInfo(
            index,
            offset,
            size,
            layer_start_x,
            layer_start_y,
            layer_finish_x,
            layer_finish_y,
            u1,
            u2,
        )
        strict_layers = frame_type in {0, 2} and width > 0 and height > 0
        if size == 0:
            if strict_layers and (offset != 0 or any(values[2:])):
                raise ImageFormatError(f"GI: пустой слой {index} содержит ненулевые поля")
        else:
            if strict_layers and (layer.width <= 0 or layer.height <= 0):
                raise ImageFormatError(f"GI: слой {index} имеет неверные координаты")
            if strict_layers and (
                layer.start_x < start_x
                or layer.start_y < start_y
                or layer.finish_x > finish_x
                or layer.finish_y > finish_y
            ):
                raise ImageFormatError(f"GI: слой {index} выходит за границы холста")
            if offset < table_end or offset + size > len(data):
                raise ImageFormatError(f"GI: данные слоя {index} выходят за границы файла")
            occupied.append((offset, offset + size, index))
        layers.append(layer)
    occupied.sort()
    for previous, current in zip(occupied, occupied[1:]):
        if previous[1] > current[0]:
            raise ImageFormatError(f"GI: данные слоёв {previous[2]} и {current[2]} пересекаются")
    return GiInfo(
        path,
        len(data),
        version,
        start_x,
        start_y,
        finish_x,
        finish_y,
        red,
        green,
        blue,
        alpha,
        frame_type,
        tuple(layers),
    )


def inspect_gi(path: str | Path) -> GiInfo:
    path = Path(path).resolve()
    return _parse_gi(path.read_bytes(), path)


def _rgb565_decode(raw: bytes) -> tuple[int, int, int]:
    if len(raw) != 2:
        raise ImageFormatError("GI: RGB565-пиксель обрезан")
    value = raw[0] | raw[1] << 8
    return ((value >> 11) & 31) << 3, ((value >> 5) & 63) << 2, (value & 31) << 3


def _rgb565_encode_round(red: int, green: int, blue: int) -> bytes:
    value = round(red / 255 * 31) << 11
    value |= round(green / 255 * 63) << 5
    value |= round(blue / 255 * 31)
    return struct.pack("<H", value)


def _rgb565_encode_layer(red: int, green: int, blue: int) -> bytes:
    # Type 2 uses the bit-exact quantizer of the established SRHD converter.
    green &= 251
    value = (red >> 3) << 11 | (green >> 2) << 5 | blue >> 3
    return struct.pack("<H", value)


def _layer_data(data: bytes, layer: GiLayerInfo) -> bytes:
    return data[layer.offset : layer.offset + layer.size] if layer.size else b""


def _decode_gi_type0(data: bytes, info: GiInfo) -> RgbaImage:
    if len(info.layers) != 1:
        raise ImageFormatError(f"GI type 0 должен иметь один слой, найдено {len(info.layers)}")
    layer = info.layers[0]
    output = bytearray(info.width * info.height * 4)
    payload = _layer_data(data, layer)
    masks = (info.alpha_mask, info.red_mask, info.green_mask, info.blue_mask)
    if masks == (0xFF000000, 0x00FF0000, 0x0000FF00, 0x000000FF):
        pixel_size = 4
        decoder: Callable[[bytes], tuple[int, int, int, int]] | None = None
    elif (info.red_mask, info.green_mask, info.blue_mask) == (0xF800, 0x07E0, 0x001F) and not info.alpha_mask:
        pixel_size = 2
        decoder = lambda raw: (*_rgb565_decode(raw), 255)
    else:
        raise UnsupportedImageFormat(
            "GI type 0 использует неподдерживаемые маски "
            f"A/R/G/B={masks!r}"
        )
    expected = layer.width * layer.height * pixel_size
    if len(payload) != expected:
        raise ImageFormatError(f"GI type 0: слой содержит {len(payload)} байт вместо {expected}")
    position = 0
    for y in range(layer.start_y, layer.finish_y):
        target = (
            (y - info.start_y) * info.width + layer.start_x - info.start_x
        ) * 4
        if decoder is None:
            source_row = payload[position : position + layer.width * 4]
            row = bytearray(len(source_row))
            row[0::4] = source_row[2::4]
            row[1::4] = source_row[1::4]
            row[2::4] = source_row[0::4]
            row[3::4] = source_row[3::4]
            output[target : target + len(row)] = row
            position += len(source_row)
        else:
            for _ in range(layer.width):
                output[target : target + 4] = bytes(decoder(payload[position : position + 2]))
                position += 2
                target += 4
    return RgbaImage(info.width, info.height, bytes(output))


def _decode_rle_layer(
    payload: bytes,
    layer: GiLayerInfo,
    info: GiInfo,
    pixel_size: int,
    apply: Callable[[int, bytes], None] | None,
    maximum: int | None = None,
) -> None:
    if not payload:
        return
    if len(payload) < 16:
        raise ImageFormatError(f"GI type 2: заголовок слоя {layer.index} обрезан")
    encoded_size, width, height, reserved = struct.unpack_from("<IIII", payload)
    if encoded_size != len(payload) - 16:
        raise ImageFormatError(
            f"GI type 2: слой {layer.index} заявляет {encoded_size} байт вместо {len(payload) - 16}"
        )
    if (width, height) != (layer.width, layer.height) or reserved:
        raise ImageFormatError(f"GI type 2: заголовок слоя {layer.index} не совпадает с таблицей")
    stream = memoryview(payload)[16:]
    position = 0
    x = 0
    y = 0
    while position < len(stream):
        command = stream[position]
        position += 1
        if command in {0, 128}:
            if x > width or y >= height:
                raise ImageFormatError(f"GI type 2: неверный конец строки слоя {layer.index}")
            x = 0
            y += 1
            continue
        count = command & 0x7F
        if not count or y >= height or x + count > width:
            raise ImageFormatError(f"GI type 2: RLE-выход за слой {layer.index}")
        if command > 128:
            byte_count = count * pixel_size
            if position + byte_count > len(stream):
                raise ImageFormatError(f"GI type 2: пиксели слоя {layer.index} обрезаны")
            if maximum is not None and any(
                value > maximum for value in stream[position : position + byte_count]
            ):
                raise ImageFormatError(
                    f"GI type 2: слой {layer.index} содержит значение больше {maximum}"
                )
            if apply is None:
                position += byte_count
                x += count
            else:
                for _ in range(count):
                    canvas_x = layer.start_x - info.start_x + x
                    canvas_y = layer.start_y - info.start_y + y
                    target = (canvas_y * info.width + canvas_x) * 4
                    apply(target, bytes(stream[position : position + pixel_size]))
                    position += pixel_size
                    x += 1
        else:
            x += count
    if y != height or x:
        raise ImageFormatError(
            f"GI type 2: слой {layer.index} завершён в ({x}, {y}), ожидалось (0, {height})"
        )


def _decode_gi_type2(data: bytes, info: GiInfo) -> RgbaImage:
    if len(info.layers) != 3:
        raise ImageFormatError(f"GI type 2 должен иметь три слоя, найдено {len(info.layers)}")
    if (info.red_mask, info.green_mask, info.blue_mask, info.alpha_mask) != (0xF800, 0x07E0, 0x001F, 0):
        raise UnsupportedImageFormat("GI type 2 использует неизвестные цветовые маски")
    output = bytearray(info.width * info.height * 4)

    def apply_color(target: int, raw: bytes) -> None:
        output[target : target + 4] = bytes((*_rgb565_decode(raw), 255))

    def apply_alpha(target: int, raw: bytes) -> None:
        alpha = (63 - raw[0]) << 2
        red, green, blue = output[target : target + 3]
        if alpha not in {0, 255}:
            red = min(255, round(red / alpha * 63) << 2)
            green = min(255, round(green / alpha * 63) << 2)
            blue = min(255, round(blue / alpha * 63) << 2)
        output[target : target + 4] = bytes((red, green, blue, alpha))

    _decode_rle_layer(_layer_data(data, info.layers[0]), info.layers[0], info, 2, apply_color)
    _decode_rle_layer(_layer_data(data, info.layers[1]), info.layers[1], info, 2, apply_color)
    _decode_rle_layer(
        _layer_data(data, info.layers[2]), info.layers[2], info, 1, apply_alpha, 63
    )
    return RgbaImage(info.width, info.height, bytes(output))


def decode_gi(data: bytes | bytearray | memoryview) -> RgbaImage:
    data = bytes(data)
    info = _parse_gi(data)
    if not info.width or not info.height:
        raise UnsupportedImageFormat("GI с нулевым холстом нельзя представить как PNG")
    if info.frame_type == 0:
        return _decode_gi_type0(data, info)
    if info.frame_type == 2:
        return _decode_gi_type2(data, info)
    raise UnsupportedImageFormat(
        f"GI frame type {info.frame_type} пока доступен только для inspect/passthrough"
    )


def read_gi(path: str | Path) -> RgbaImage:
    return decode_gi(Path(path).read_bytes())


def _gi_header(image: RgbaImage, frame_type: int, layers: int, alpha32: bool = False) -> bytes:
    if alpha32:
        masks = (0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
    else:
        masks = (0xF800, 0x07E0, 0x001F, 0)
    return struct.pack(
        "<4sIiiiiIIIIII4I",
        GI_MAGIC,
        1,
        0,
        0,
        image.width,
        image.height,
        *masks,
        frame_type,
        layers,
        0,
        0,
        0,
        0,
    )


def _gi_layer_descriptor(offset: int, payload: bytes, bounds: tuple[int, int, int, int]) -> bytes:
    if not payload:
        return bytes(_GI_LAYER_SIZE)
    return struct.pack("<IIiiiiii", offset, len(payload), *bounds, 0, 0)


def _bbox(image: RgbaImage, predicate: Callable[[int], bool]) -> tuple[int, int, int, int] | None:
    min_x = image.width
    min_y = image.height
    max_x = -1
    max_y = -1
    for y in range(image.height):
        for x in range(image.width):
            alpha = image.pixels[(y * image.width + x) * 4 + 3]
            if predicate(alpha):
                min_x = min(min_x, x)
                min_y = min(min_y, y)
                max_x = max(max_x, x)
                max_y = max(max_y, y)
    if max_x < 0:
        return None
    return min_x, min_y, max_x + 1, max_y + 1


def _encode_rle(
    image: RgbaImage,
    bounds: tuple[int, int, int, int] | None,
    predicate: Callable[[int], bool],
    pixel: Callable[[int], bytes],
) -> bytes:
    if bounds is None:
        return b""
    start_x, start_y, finish_x, finish_y = bounds
    width = finish_x - start_x
    height = finish_y - start_y
    output = bytearray()
    for y in range(start_y, finish_y):
        x = start_x
        if not any(
            predicate(image.pixels[(y * image.width + current) * 4 + 3])
            for current in range(start_x, finish_x)
        ):
            output.append(128)
            continue
        while x < finish_x:
            index = (y * image.width + x) * 4
            active = predicate(image.pixels[index + 3])
            run_start = x
            while x < finish_x and predicate(
                image.pixels[(y * image.width + x) * 4 + 3]
            ) == active and x - run_start < 127:
                x += 1
            count = x - run_start
            if active:
                output.append(128 + count)
                for current in range(run_start, x):
                    output.extend(pixel((y * image.width + current) * 4))
            else:
                output.append(count)
        output.append(0)
    return struct.pack("<IIII", len(output), width, height, 0) + bytes(output)


def _encode_gi_type0(image: RgbaImage, mode: str) -> bytes:
    if mode == "0_32":
        payload = bytearray(len(image.pixels))
        payload[0::4] = image.pixels[2::4]
        payload[1::4] = image.pixels[1::4]
        payload[2::4] = image.pixels[0::4]
        payload[3::4] = image.pixels[3::4]
        header = _gi_header(image, 0, 1, alpha32=True)
    else:
        payload = bytearray()
        for index in range(0, len(image.pixels), 4):
            payload.extend(_rgb565_encode_round(*image.pixels[index : index + 3]))
        header = _gi_header(image, 0, 1)
    offset = _GI_HEADER_SIZE + _GI_LAYER_SIZE
    descriptor = _gi_layer_descriptor(offset, bytes(payload), (0, 0, image.width, image.height))
    return header + descriptor + bytes(payload)


def _encode_gi_type2(image: RgbaImage) -> bytes:
    opaque = lambda alpha: alpha == 255
    translucent = lambda alpha: alpha not in {0, 255}
    opaque_bounds = _bbox(image, opaque)
    translucent_bounds = _bbox(image, translucent)

    def opaque_pixel(index: int) -> bytes:
        return _rgb565_encode_layer(*image.pixels[index : index + 3])

    def translucent_pixel(index: int) -> bytes:
        red, green, blue, alpha = image.pixels[index : index + 4]
        return _rgb565_encode_layer(red * alpha >> 8, green * alpha >> 8, blue * alpha >> 8)

    def alpha_pixel(index: int) -> bytes:
        return bytes(((255 - image.pixels[index + 3]) >> 2,))

    payloads = (
        _encode_rle(image, opaque_bounds, opaque, opaque_pixel),
        _encode_rle(image, translucent_bounds, translucent, translucent_pixel),
        _encode_rle(image, translucent_bounds, translucent, alpha_pixel),
    )
    bounds = (
        opaque_bounds or (0, 0, 0, 0),
        translucent_bounds or (0, 0, 0, 0),
        translucent_bounds or (0, 0, 0, 0),
    )
    offset = _GI_HEADER_SIZE + 3 * _GI_LAYER_SIZE
    descriptors = bytearray()
    for payload, layer_bounds in zip(payloads, bounds):
        descriptors.extend(_gi_layer_descriptor(offset if payload else 0, payload, layer_bounds))
        offset += len(payload)
    return _gi_header(image, 2, 3) + bytes(descriptors) + b"".join(payloads)


def encode_gi(image: RgbaImage, mode: str = "0_32") -> bytes:
    """Encode deterministic SRHD GI in modes 0_32, 0_16 or 2."""

    _checked_image_size(image.width, image.height)
    if mode not in {"0_32", "0_16", "2"}:
        raise ValueError("Режим GI должен быть 0_32, 0_16 или 2")
    if mode in {"0_32", "0_16"}:
        return _encode_gi_type0(image, mode)
    return _encode_gi_type2(image)


def write_gi(image: RgbaImage, path: str | Path, mode: str = "0_32") -> None:
    Path(path).write_bytes(encode_gi(image, mode))


def verify_gi(path: str | Path) -> dict[str, Any]:
    path = Path(path).resolve()
    data = path.read_bytes()
    info = _parse_gi(data, path)
    if not info.supported:
        if info.frame_type in {0, 2} and (not info.width or not info.height):
            reason = "GI с нулевым холстом нельзя представить как PNG"
        else:
            reason = f"GI frame type {info.frame_type} пока доступен только для inspect/passthrough"
        raise UnsupportedImageFormat(
            reason
        )
    if info.frame_type == 0:
        if len(info.layers) != 1:
            raise ImageFormatError(
                f"GI type 0 должен иметь один слой, найдено {len(info.layers)}"
            )
        masks = (info.alpha_mask, info.red_mask, info.green_mask, info.blue_mask)
        if masks == (0xFF000000, 0x00FF0000, 0x0000FF00, 0x000000FF):
            pixel_size = 4
        elif (
            (info.red_mask, info.green_mask, info.blue_mask) == (0xF800, 0x07E0, 0x001F)
            and not info.alpha_mask
        ):
            pixel_size = 2
        else:
            raise UnsupportedImageFormat(
                "GI type 0 использует неподдерживаемые маски "
                f"A/R/G/B={masks!r}"
            )
        layer = info.layers[0]
        expected = layer.width * layer.height * pixel_size
        if layer.size != expected:
            raise ImageFormatError(
                f"GI type 0: слой содержит {layer.size} байт вместо {expected}"
            )
    else:
        if len(info.layers) != 3:
            raise ImageFormatError(
                f"GI type 2 должен иметь три слоя, найдено {len(info.layers)}"
            )
        if (info.red_mask, info.green_mask, info.blue_mask, info.alpha_mask) != (
            0xF800,
            0x07E0,
            0x001F,
            0,
        ):
            raise UnsupportedImageFormat("GI type 2 использует неизвестные цветовые маски")
        for index, pixel_size in enumerate((2, 2, 1)):
            _decode_rle_layer(
                _layer_data(data, info.layers[index]),
                info.layers[index],
                info,
                pixel_size,
                None,
                63 if index == 2 else None,
            )
    value = info.summary()
    value["decoded_bytes"] = info.width * info.height * 4
    value["verified"] = True
    return value
