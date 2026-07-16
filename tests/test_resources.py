from __future__ import annotations

import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from srhd_modkit.resources import (
    ResourceFormatError,
    extract_resource,
    inspect_gai,
    inspect_hai,
    inspect_pkg,
)


def _gi(width: int, height: int, marker: int = 0) -> bytes:
    return struct.pack("<4sIIIII", b"gi\0\0", 1, 0, marker, width, height)


def _gai(frames: list[bytes], width: int, height: int) -> bytes:
    header = bytearray(48 + len(frames) * 8)
    struct.pack_into("<4sI", header, 0, b"gai\0", 1)
    struct.pack_into("<III", header, 16, width, height, len(frames))
    offset = len(header)
    for index, frame in enumerate(frames):
        struct.pack_into("<II", header, 48 + index * 8, offset, len(frame))
        offset += len(frame)
    return bytes(header) + b"".join(frames)


def _pkg(payload: bytes, name: str = "Frame.gi") -> bytes:
    compressed = zlib.compress(payload, level=9)
    chunk = b"ZL02" + struct.pack("<I", len(payload)) + compressed
    block = struct.pack("<I", 4 + len(chunk)) + struct.pack("<I", len(chunk)) + chunk
    data_offset = 344
    data = bytearray(data_offset)
    struct.pack_into("<I", data, 0, 0)  # one directory record follows
    struct.pack_into("<III", data, 4, 170, 1, 158)
    data[24 : 24 + 4] = b"MODS"
    data[87 : 87 + 4] = b"Mods"
    struct.pack_into("<II", data, 174, 170, 1)
    name_offset = 194
    struct.pack_into("<II", data, name_offset - 8, len(block), len(payload))
    data[name_offset : name_offset + len(name.upper())] = name.upper().encode("ascii")
    data[name_offset + 63 : name_offset + 63 + len(name)] = name.encode("ascii")
    struct.pack_into("<I", data, name_offset + 142, data_offset)
    return bytes(data) + block


class ResourceTests(unittest.TestCase):
    def test_gai_lists_and_extracts_embedded_gi_frames(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "anim.gai"
            frames = [_gi(10, 20), _gi(12, 22, 1)]
            source.write_bytes(_gai(frames, 32, 32))
            info = inspect_gai(source)
            self.assertEqual((info.width, info.height), (32, 32))
            self.assertEqual([(item.width, item.height) for item in info.frames], [(10, 20), (12, 22)])
            result = extract_resource(source, root / "out")
            self.assertEqual(result["files"], 2)
            self.assertEqual((root / "out" / "anim_0001.gi").read_bytes(), frames[1])

    def test_gai_rejects_broken_frame_offset(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "bad.gai"
            data = bytearray(_gai([_gi(1, 1)], 1, 1))
            struct.pack_into("<I", data, 48, 999)
            path.write_bytes(data)
            with self.assertRaises(ResourceFormatError):
                inspect_gai(path)

    def test_hai_reports_standard_raw_frame_stride(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "ship.hai"
            header = struct.pack("<13I", 0x04210420, 2, 2, 2, 3, 8, 1, 8, 0, 0, 0, 0, 4)
            path.write_bytes(header + bytes(3 * 8))
            info = inspect_hai(path)
            self.assertTrue(info.standard_layout)
            self.assertEqual(info.physical_frame_size, 8)
            self.assertEqual(len(info.frames), 3)

    def test_pkg_lists_verifies_and_extracts_zl02(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            payload = _gi(7, 9)
            source = root / "one.pkg"
            source.write_bytes(_pkg(payload))
            info = inspect_pkg(source)
            self.assertEqual(info.folders, ("Mods",))
            self.assertEqual(info.entries[0].name, "Frame.gi")
            self.assertEqual(info.decompress(info.entries[0]), payload)
            self.assertEqual(info.verify()["verified_files"], 1)
            extract_resource(source, root / "unpacked")
            self.assertEqual((root / "unpacked" / "Mods" / "Frame.gi").read_bytes(), payload)


if __name__ == "__main__":
    unittest.main()
