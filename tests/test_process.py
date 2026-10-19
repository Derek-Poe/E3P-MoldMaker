import ctypes
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from e3p_mold.process import LOG_LIMIT, TAIL_LIMIT, run_process


def running(pid):
    if os.name == "nt":
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        api.OpenProcess.restype = wintypes.HANDLE
        api.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        api.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(api.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            api.CloseHandle(handle)
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_bounded_stream_capture_and_exit_status(self):
        result = run_process([sys.executable, "-c", f"import sys;sys.stdout.write('x'*{LOG_LIMIT + 321});sys.stderr.write('fixture error');sys.exit(7)"],
                             cwd=self.root, timeout=10, stdout_path=self.root / "out.log", stderr_path=self.root / "err.log")
        self.assertEqual(result.returncode, 7)
        self.assertEqual(len(result.stdout), TAIL_LIMIT)
        self.assertEqual((self.root / "out.log").stat().st_size, LOG_LIMIT)
        self.assertEqual(result.stderr, "fixture error")
        self.assertTrue(result.logs_truncated)

    def test_timeout_and_prelaunch_cancellation(self):
        result = run_process([sys.executable, "-c", "import time;time.sleep(30)"], cwd=self.root, timeout=0.25)
        self.assertTrue(result.timed_out)
        self.assertLess(result.elapsed_seconds, 6)
        event = threading.Event()
        event.set()
        result = run_process([sys.executable, "-c", "raise AssertionError('must not launch')"], cwd=self.root, timeout=2, cancel_event=event)
        self.assertTrue(result.cancelled)
        self.assertIsNone(result.returncode)

    def test_explicit_child_environment_does_not_change_parent(self):
        inherited = "E3P_TEST_PARENT_ENVIRONMENT"
        replaced = "E3P_TEST_CHILD_ENVIRONMENT"
        with patch.dict(os.environ, {inherited: "parent-only"}):
            child_environment = dict(os.environ)
            child_environment.pop(inherited)
            child_environment[replaced] = "child-only"
            code = f"import os;print(os.environ.get({inherited!r}, 'absent'));print(os.environ[{replaced!r}])"
            result = run_process([sys.executable, "-c", code], cwd=self.root, timeout=5,
                                 env=child_environment)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.splitlines(), ["absent", "child-only"])
            self.assertEqual(os.environ[inherited], "parent-only")
            self.assertNotIn(replaced, os.environ)

    def test_cancel_kills_owned_descendant_but_preserves_unrelated_process(self):
        marker = self.root / "child.pid"
        event = threading.Event()
        sentinel = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: sentinel.poll() is None and sentinel.kill())
        code = ("import os,subprocess,sys,time;from pathlib import Path;"
                "options={'creationflags':subprocess.CREATE_NEW_PROCESS_GROUP} if os.name=='nt' else {'start_new_session':True};"
                "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],**options);"
                f"Path({str(marker)!r}).write_text(str(child.pid));time.sleep(30)")
        def cancel_when_child_exists():
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            event.set()
        thread = threading.Thread(target=cancel_when_child_exists)
        thread.start()
        result = run_process([sys.executable, "-c", code], cwd=self.root, timeout=8, cancel_event=event)
        thread.join()
        self.assertTrue(marker.exists(), "child was never launched")
        self.assertTrue(result.cancelled)
        child = int(marker.read_text())
        deadline = time.monotonic() + 2
        while running(child) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(running(child), "owned child survived cancellation")
        self.assertIsNone(sentinel.poll(), "unrelated process was terminated")
        sentinel.terminate()
        sentinel.wait(timeout=5)

    def test_output_budget_terminates_writer(self):
        code = f"import time;from pathlib import Path;Path({str(self.root / 'large.bin')!r}).write_bytes(b'x'*200000);time.sleep(30)"
        result = run_process([sys.executable, "-c", code], cwd=self.root, timeout=5,
                             output_directory=self.root, max_output_bytes=10000)
        self.assertTrue(result.budget_exceeded)

    def test_log_write_failure_does_not_hang_or_overwrite(self):
        target = self.root / "existing.log"
        target.write_text("preserve")
        with self.assertRaises(OSError):
            run_process([sys.executable, "-c", "import time;time.sleep(30)"], cwd=self.root,
                        timeout=2, stdout_path=target)
        self.assertEqual(target.read_text(), "preserve")
