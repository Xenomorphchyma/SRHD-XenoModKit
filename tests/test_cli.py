from __future__ import annotations

import io
import json
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
