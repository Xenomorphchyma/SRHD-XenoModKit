from __future__ import annotations

import ctypes
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from srhd_modkit.cli import main
from srhd_modkit.hidden_process import inspect_hidden_processes, run_on_hidden_desktop


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "private desktops and Job Objects are Windows-only")
class HiddenProcessTests(unittest.TestCase):
    def assert_clean(self) -> None:
        report = inspect_hidden_processes()
        self.assertEqual(report["desktop_count"], 0, report)
        self.assertEqual(report["process_count"], 0, report)

    def test_normal_exit_and_timeout_leave_no_desktop(self) -> None:
        self.assert_clean()
        completed = run_on_hidden_desktop(
            sys.executable,
            ["-B", "-c", "pass"],
            cwd=ROOT,
            timeout=5,
        )
        self.assertEqual(completed.exit_code, 0)
        with self.assertRaises(TimeoutError):
            run_on_hidden_desktop(
                sys.executable,
                ["-B", "-c", "import time;time.sleep(30)"],
                cwd=ROOT,
                timeout=0.2,
            )
        self.assert_clean()

    def test_parent_termination_closes_job_and_child(self) -> None:
        self.assert_clean()
        with tempfile.TemporaryDirectory(prefix="srhd-job-test-") as temp_name:
            marker = Path(temp_name) / "child.pid"
            grandchild_marker = Path(temp_name) / "grandchild.pid"
            grandchild_code = (
                "import os,time;from pathlib import Path;"
                f"Path({str(grandchild_marker)!r}).write_text(str(os.getpid()),encoding='ascii');"
                "time.sleep(60)"
            )
            child_code = (
                "import os,subprocess,sys,time;from pathlib import Path;"
                f"Path({str(marker)!r}).write_text(str(os.getpid()),encoding='ascii');"
                f"subprocess.Popen([sys.executable,'-B','-c',{grandchild_code!r}]);"
                "time.sleep(60)"
            )
            parent_code = (
                "from pathlib import Path;import sys;"
                "from srhd_modkit.hidden_process import run_on_hidden_desktop;"
                f"run_on_hidden_desktop(sys.executable,['-B','-c',{child_code!r}],"
                "cwd=Path.cwd(),timeout=30)"
            )
            parent = subprocess.Popen(
                [sys.executable, "-B", "-c", parent_code],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            child_pid = None
            grandchild_pid = None
            try:
                deadline = time.monotonic() + 8
                while (
                    time.monotonic() < deadline
                    and parent.poll() is None
                    and not (marker.is_file() and grandchild_marker.is_file())
                ):
                    time.sleep(0.05)
                if marker.is_file():
                    child_pid = int(marker.read_text(encoding="ascii"))
                if grandchild_marker.is_file():
                    grandchild_pid = int(grandchild_marker.read_text(encoding="ascii"))
                if child_pid is None or grandchild_pid is None:
                    _stdout, stderr = parent.communicate(timeout=1) if parent.poll() is not None else ("", "<running>")
                    self.fail(f"hidden process tree did not start; parent stderr: {stderr}")

                parent.kill()
                parent.wait(timeout=5)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and _process_alive(child_pid):
                    time.sleep(0.05)
                self.assertFalse(_process_alive(child_pid), f"child PID {child_pid} survived its parent")
                self.assertFalse(
                    _process_alive(grandchild_pid),
                    f"grandchild PID {grandchild_pid} survived the Job Object",
                )
                self.assert_clean()
            finally:
                if parent.poll() is None:
                    parent.kill()
                try:
                    parent.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    parent.kill()
                    parent.communicate(timeout=5)
                if child_pid is not None and _process_alive(child_pid):
                    os.kill(child_pid, 15)
                if grandchild_pid is not None and _process_alive(grandchild_pid):
                    os.kill(grandchild_pid, 15)

    def test_named_mutex_serializes_parallel_runs(self) -> None:
        self.assert_clean()
        parent_code = (
            "from pathlib import Path;import sys;"
            "from srhd_modkit.hidden_process import run_on_hidden_desktop;"
            "result=run_on_hidden_desktop(sys.executable,"
            "['-B','-c','import time;time.sleep(.4)'],cwd=Path.cwd(),timeout=5);"
            "print(result.queue_seconds)"
        )
        parents = [
            subprocess.Popen(
                [sys.executable, "-B", "-c", parent_code],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        try:
            maximum_desktops = 0
            deadline = time.monotonic() + 10
            while any(parent.poll() is None for parent in parents) and time.monotonic() < deadline:
                maximum_desktops = max(maximum_desktops, int(inspect_hidden_processes()["desktop_count"]))
                time.sleep(0.03)
            self.assertTrue(all(parent.poll() is not None for parent in parents), "parallel parents timed out")
            completed = [parent.communicate(timeout=1) for parent in parents]
            self.assertEqual(
                [parent.returncode for parent in parents],
                [0, 0],
                [stderr for _stdout, stderr in completed],
            )
            queue_seconds = [float(stdout.strip()) for stdout, _stderr in completed]
            self.assertEqual(maximum_desktops, 1)
            self.assertGreater(max(queue_seconds), 0.2)
            self.assert_clean()
        finally:
            for parent in parents:
                if parent.poll() is None:
                    parent.kill()
                try:
                    parent.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    parent.kill()
                    parent.communicate(timeout=5)

    def test_doctor_processes_reports_clean_json(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["doctor", "processes", "--json"])
        self.assertEqual(exit_code, 0)
        self.assertIn('"schema": "srhd-modkit-process-audit-v1"', output.getvalue())
        self.assertIn('"status": "passed"', output.getvalue())


def _process_alive(process_id: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel32.WaitForSingleObject.restype = ctypes.c_ulong
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(0x00100000, False, process_id)  # SYNCHRONIZE
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x00000102  # WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
