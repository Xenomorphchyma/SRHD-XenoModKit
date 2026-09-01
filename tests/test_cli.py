from __future__ import annotations

import io
import json
import struct
import tempfile
import unittest
import zlib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from srhd_modkit.cli import main


class CliTests(unittest.TestCase):
    def test_json_flag_keeps_machine_readable_error_contract(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            missing = Path(name) / "missing.txt"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["dat", "tree", str(missing), "--json"])
            self.assertEqual(code, 1)
            self.assertEqual(stderr.getvalue(), "")
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["schema"], "srhd-modkit-error-v1")
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["error"]["type"], "FileNotFoundError")

    def test_resource_verify_returns_nonzero_for_nonstandard_pkg_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "unsafe.pkg"
            payload = bytes(70_000)
            compressed = zlib.compress(payload, level=9)
            chunk = b"ZL02" + struct.pack("<I", len(payload)) + compressed
            block = struct.pack("<I", 4 + len(chunk)) + struct.pack("<I", len(chunk)) + chunk
            data_offset = 344
            data = bytearray(data_offset)
            struct.pack_into("<III", data, 4, 170, 1, 158)
            data[24:28] = b"MODS"
            data[87:91] = b"Mods"
            struct.pack_into("<II", data, 174, 170, 1)
            name_offset = 194
            struct.pack_into("<II", data, name_offset - 8, len(block), len(payload))
            data[name_offset : name_offset + 8] = b"FRAME.GI"
            data[name_offset + 63 : name_offset + 71] = b"Frame.gi"
            struct.pack_into("<I", data, name_offset + 142, data_offset)
            path.write_bytes(bytes(data) + block)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(["resource", "verify", str(path), "--json"])

            result = json.loads(stdout.getvalue())
            self.assertEqual(code, 1)
            self.assertTrue(result["structurally_valid"])
            self.assertEqual(
                result["compatibility_issues"][0]["code"],
                "pkg-zl02-chunk-exceeds-game-compatible-size",
            )


if __name__ == "__main__":
    unittest.main()
