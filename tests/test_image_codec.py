from __future__ import annotations

import binascii
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from srhd_modkit.image_codec import (
    GI_MAGIC,
    PNG_MAGIC,
    ImageFormatError,
    RgbaImage,
    UnsupportedImageFormat,
    decode_gi,
    decode_png,
    encode_gi,
    encode_png,
    inspect_gi,
    verify_gi,
)


def _chunk(kind: bytes, payload: bytes) -> bytes:
    crc = binascii.crc32(kind)
    crc = binascii.crc32(payload, crc) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def _png(
    width: int,
    height: int,
    bit_depth: int,
    color_type: int,
    raw: bytes,
    *,
    palette: bytes | None = None,
    transparency: bytes | None = None,
    interlace: int = 0,
) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, interlace)
    chunks = [_chunk(b"IHDR", header)]
    if palette is not None:
        chunks.append(_chunk(b"PLTE", palette))
    if transparency is not None:
        chunks.append(_chunk(b"tRNS", transparency))
    chunks.extend((_chunk(b"IDAT", zlib.compress(raw)), _chunk(b"IEND", b"")))
    return PNG_MAGIC + b"".join(chunks)


def _sample() -> RgbaImage:
    pixels = bytes(
        component
        for pixel in (
            (255, 0, 0, 255),
            (0, 255, 0, 192),
            (0, 0, 255, 128),
            (1, 2, 3, 0),
            (12, 34, 56, 78),
            (90, 87, 65, 43),
            (22, 44, 66, 88),
            (99, 111, 123, 135),
        )
        for component in pixel
    )
    return RgbaImage(4, 2, pixels)


def _paeth(left: int, above: int, upper_left: int) -> int:
    prediction = left + above - upper_left
    distances = (abs(prediction - left), abs(prediction - above), abs(prediction - upper_left))
    return (left, above, upper_left)[distances.index(min(distances))]


def _filtered_rows(image: RgbaImage) -> bytes:
    stride = image.width * 4
    output = bytearray()
    previous = bytes(stride)
    for y in range(image.height):
        filter_type = y % 5
        row = image.pixels[y * stride : (y + 1) * stride]
        output.append(filter_type)
        for index, value in enumerate(row):
            left = row[index - 4] if index >= 4 else 0
            above = previous[index]
            upper_left = previous[index - 4] if index >= 4 else 0
            predictor = (
                0
                if filter_type == 0
                else left
                if filter_type == 1
                else above
                if filter_type == 2
                else (left + above) // 2
                if filter_type == 3
                else _paeth(left, above, upper_left)
            )
            output.append((value - predictor) & 0xFF)
        previous = row
    return bytes(output)


class PngCodecTests(unittest.TestCase):
    def test_rgba_roundtrip_is_exact_and_deterministic(self) -> None:
        image = _sample()
        first = encode_png(image)
        second = encode_png(image)
        self.assertEqual(first, second)
        self.assertEqual(decode_png(first), image)

    def test_palette_subbyte_and_transparency_are_decoded(self) -> None:
        data = _png(
            2,
            1,
            1,
            3,
            b"\0\x40",
            palette=bytes((255, 0, 0, 0, 255, 0)),
            transparency=bytes((255, 128)),
        )
        self.assertEqual(
            decode_png(data).pixels,
            bytes((255, 0, 0, 255, 0, 255, 0, 128)),
        )

    def test_sixteen_bit_grayscale_is_scaled(self) -> None:
        data = _png(2, 1, 16, 0, b"\0\0\0\xff\xff")
        self.assertEqual(
            decode_png(data).pixels,
            bytes((0, 0, 0, 255, 255, 255, 255, 255)),
        )

    def test_adam7_single_pixel_is_decoded(self) -> None:
        data = _png(1, 1, 8, 6, b"\0\x01\x02\x03\x04", interlace=1)
        self.assertEqual(decode_png(data), RgbaImage(1, 1, bytes((1, 2, 3, 4))))

    def test_all_png_filters_are_decoded(self) -> None:
        image = RgbaImage(
            3,
            5,
            bytes((index * 37 + 11) & 0xFF for index in range(3 * 5 * 4)),
        )
        data = _png(image.width, image.height, 8, 6, _filtered_rows(image))
        self.assertEqual(decode_png(data), image)

    def test_adam7_places_every_pass(self) -> None:
        width = height = 8
        pixels = bytes(
            component
            for y in range(height)
            for x in range(width)
            for component in (x * 23, y * 29, (x + y) * 13, 255 - x * 7)
        )
        image = RgbaImage(width, height, pixels)
        raw = bytearray()
        for start_x, start_y, step_x, step_y in (
            (0, 0, 8, 8),
            (4, 0, 8, 8),
            (0, 4, 4, 8),
            (2, 0, 4, 4),
            (0, 2, 2, 4),
            (1, 0, 2, 2),
            (0, 1, 1, 2),
        ):
            for y in range(start_y, height, step_y):
                raw.append(0)
                for x in range(start_x, width, step_x):
                    offset = (y * width + x) * 4
                    raw.extend(pixels[offset : offset + 4])
        data = _png(width, height, 8, 6, bytes(raw), interlace=1)
        self.assertEqual(decode_png(data), image)

    def test_broken_crc_is_rejected(self) -> None:
        data = bytearray(encode_png(RgbaImage(1, 1, bytes((1, 2, 3, 4)))))
        data[-1] ^= 1
        with self.assertRaises(ImageFormatError):
            decode_png(data)


class GiCodecTests(unittest.TestCase):
    def test_mode_0_32_is_pixel_exact_and_deterministic(self) -> None:
        image = _sample()
        first = encode_gi(image, "0_32")
        self.assertEqual(first, encode_gi(image, "0_32"))
        self.assertEqual(decode_gi(first), image)

    def test_mode_0_16_is_rgb565_and_opaque(self) -> None:
        image = RgbaImage(2, 1, bytes((255, 255, 255, 1, 17, 33, 65, 0)))
        decoded = decode_gi(encode_gi(image, "0_16"))
        self.assertEqual(decoded.pixel(0, 0), (248, 252, 248, 255))
        self.assertEqual(decoded.pixel(1, 0)[3], 255)

    def test_mode_2_handles_opaque_translucent_and_transparent_pixels(self) -> None:
        image = _sample()
        encoded = encode_gi(image, "2")
        self.assertEqual(encoded, encode_gi(image, "2"))
        decoded = decode_gi(encoded)
        self.assertEqual(decoded.pixel(0, 0), (248, 0, 0, 255))
        self.assertEqual(decoded.pixel(3, 0), (0, 0, 0, 0))
        self.assertEqual(decoded.pixel(1, 0)[3], 192)
        self.assertEqual(decoded.pixel(2, 0)[3], 128)

    def test_mode_2_all_transparent_uses_empty_layers(self) -> None:
        encoded = encode_gi(RgbaImage(3, 2, bytes(3 * 2 * 4)), "2")
        self.assertEqual(len(encoded), 160)
        self.assertEqual(decode_gi(encoded).pixels, bytes(3 * 2 * 4))

    def test_legacy_frame_type_is_inspectable_but_not_decodable(self) -> None:
        header = struct.pack(
            "<4sIiiiiIIIIII4I",
            GI_MAGIC,
            1,
            0,
            0,
            1,
            1,
            0xF800,
            0x07E0,
            0x001F,
            0,
            1,
            1,
            0,
            0,
            0,
            0,
        )
        data = header + bytes(32)
        with self.assertRaises(UnsupportedImageFormat):
            decode_gi(data)
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "legacy.gi"
            path.write_bytes(data)
            self.assertEqual(inspect_gi(path).frame_type, 1)
            with self.assertRaises(UnsupportedImageFormat):
                verify_gi(path)

    def test_invalid_layer_offset_is_rejected(self) -> None:
        data = bytearray(encode_gi(RgbaImage(1, 1, bytes((1, 2, 3, 4))), "0_32"))
        struct.pack_into("<I", data, 64, len(data) + 100)
        with self.assertRaises(ImageFormatError):
            decode_gi(data)

    def test_mode_2_rejects_alpha_outside_six_bits(self) -> None:
        data = bytearray(encode_gi(RgbaImage(1, 1, bytes((1, 2, 3, 128))), "2"))
        alpha_offset = struct.unpack_from("<I", data, 128)[0]
        data[alpha_offset + 17] = 64
        with self.assertRaises(ImageFormatError):
            decode_gi(data)
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "broken-alpha.gi"
            path.write_bytes(data)
            with self.assertRaises(ImageFormatError):
                verify_gi(path)


if __name__ == "__main__":
    unittest.main()
