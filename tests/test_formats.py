from __future__ import annotations

import tempfile
import struct
import unittest
from pathlib import Path

from srhd_modkit.formats import get_format_spec, inspect_file, scan_formats


class FormatTests(unittest.TestCase):
    def test_known_mod_formats_are_registered(self) -> None:
        for extension in (".gi", ".gai", ".hai", ".dat", ".pkg", ".scr", ".rson", ".py"):
            with self.subTest(extension=extension):
                self.assertIsNotNone(get_format_spec(extension))

    def test_invalid_gi_signature_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "broken.gi"
            path.write_bytes(b"not-a-gi")
            info = inspect_file(path)
            self.assertEqual(info["format"], "GI image")
            self.assertFalse(info["signature_valid"])

    def test_unknown_file_is_preserved_as_passthrough(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "future.xyz"
            path.write_bytes(b"opaque data")
            info = inspect_file(path)
            self.assertEqual(info["format"], "unknown")
            self.assertEqual(info["handling"], "passthrough")
            scan = scan_formats(name)
            self.assertEqual(scan["file_count"], 1)

    def test_jpeg_dimensions_and_rgb_mode_are_read_without_pillow(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "quest.jpg"
            components = bytes((1, 0x11, 0, 2, 0x11, 0, 3, 0x11, 0))
            sof = bytes((8,)) + struct.pack(">HHB", 394, 343, 3) + components
            path.write_bytes(b"\xff\xd8\xff\xc0" + struct.pack(">H", len(sof) + 2) + sof + b"\xff\xd9")
            info = inspect_file(path)
            self.assertEqual((info["width"], info["height"], info["mode"]), (343, 394, "RGB"))

    def test_map_extensions_remain_unknown_passthrough(self) -> None:
        samples = {".raw": b"RABW", ".map": b"abwm", ".opt": b"ZL01"}
        with tempfile.TemporaryDirectory() as name:
            for extension, signature in samples.items():
                with self.subTest(extension=extension):
                    path = Path(name) / f"arena{extension}"
                    path.write_bytes(signature + b"opaque data")
                    info = inspect_file(path)
                    self.assertEqual(info["format"], "unknown")
                    self.assertEqual(info["handling"], "passthrough")


if __name__ == "__main__":
    unittest.main()
