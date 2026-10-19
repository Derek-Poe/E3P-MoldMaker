"""Bounded, owned subprocesses. Stdlib only; also bundled with the Cura extension."""

from __future__ import annotations

import math
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

LOG_LIMIT = 1_048_576  # Per stream, on disk.
TAIL_LIMIT = 16_384  # Per stream, returned to the caller.


@dataclass(frozen=True)
class ProcessResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    cancelled: bool
    elapsed_seconds: float
    logs_truncated: bool
    budget_exceeded: bool = False


class _WindowsJob:
    """Terminate the owned process and descendants, including nested jobs."""

    def __init__(self, process):
        import ctypes
        from ctypes import wintypes

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        self.api.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        self.api.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.api.CloseHandle(self.handle)
            self.handle = None
            raise error

        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                        ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", ctypes.c_uint64 * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

        limits = ExtendedLimits()
        limits.basic.flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        self.api.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def resume(self, process):
        """Resume the sole initial thread after assigning the suspended child."""
        import ctypes
        from ctypes import wintypes

        class ThreadEntry(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                        ("thread_id", wintypes.DWORD), ("owner_pid", wintypes.DWORD),
                        ("base_priority", wintypes.LONG), ("delta_priority", wintypes.LONG), ("flags", wintypes.DWORD)]

        self.api.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        self.api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        self.api.Thread32First.argtypes = (wintypes.HANDLE, ctypes.POINTER(ThreadEntry))
        self.api.Thread32Next.argtypes = (wintypes.HANDLE, ctypes.POINTER(ThreadEntry))
        self.api.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self.api.OpenThread.restype = wintypes.HANDLE
        self.api.ResumeThread.argtypes = (wintypes.HANDLE,)
        self.api.ResumeThread.restype = wintypes.DWORD
        snapshot = self.api.CreateToolhelp32Snapshot(4, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            valid = self.api.Thread32First(snapshot, ctypes.byref(entry))
            while valid:
                if entry.owner_pid == process.pid:
                    handle = self.api.OpenThread(2, False, entry.thread_id)  # THREAD_SUSPEND_RESUME
                    if not handle:
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        if self.api.ResumeThread(handle) == 0xFFFFFFFF:
                            raise ctypes.WinError(ctypes.get_last_error())
                        return
                    finally:
                        self.api.CloseHandle(handle)
                valid = self.api.Thread32Next(snapshot, ctypes.byref(entry))
            raise RuntimeError("suspended child initial thread was not found")
        finally:
            self.api.CloseHandle(snapshot)

    def close(self):
        if self.handle:
            self.api.TerminateJobObject(self.handle, 1)
            self.api.CloseHandle(self.handle)
            self.handle = None


def _linux_identity(pid):
    try:
        # comm may contain spaces and parentheses. Fields after its final ')'
        # start with state (field 3), ppid (4), and starttime at index 19 (22).
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[1]), fields[19]
    except (OSError, ValueError, IndexError):
        return None


def _kill_posix_tree(process):
    # A CLI can itself launch Blender in a new session. On Linux freeze the
    # owned ancestor and then its descendants before killing them, including
    # those nested sessions. PID start times prevent killing reused PIDs.
    identities = {}
    if Path("/proc/self/stat").exists() and process.poll() is None:
        first = _linux_identity(process.pid)
        if first:
            identities[process.pid] = first[1]
            try:
                os.kill(process.pid, signal.SIGSTOP)
            except ProcessLookupError:
                pass
            for _ in range(64):
                discovered = []
                for item in Path("/proc").iterdir():
                    if not item.name.isdigit():
                        continue
                    pid = int(item.name)
                    identity = _linux_identity(pid)
                    if identity and identity[0] in identities and pid not in identities:
                        identities[pid] = identity[1]
                        discovered.append(pid)
                        try:
                            os.kill(pid, signal.SIGSTOP)
                        except ProcessLookupError:
                            pass
                if not discovered:
                    break
    for pid, started in reversed(list(identities.items())):
        current = _linux_identity(pid)
        if current and current[1] == started:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class _Capture:
    def __init__(self, pipe, destination):
        self.pipe = pipe
        self.destination = destination
        self.tail = bytearray()
        self.total = 0
        self.error = None

    def drain(self):
        output = None
        try:
            if self.destination is not None:
                output = Path(self.destination).open("xb")
            while chunk := self.pipe.read(8192):
                if output and self.total < LOG_LIMIT:
                    output.write(chunk[: LOG_LIMIT - self.total])
                self.total += len(chunk)
                self.tail.extend(chunk)
                if len(self.tail) > TAIL_LIMIT:
                    del self.tail[:-TAIL_LIMIT]
        except Exception as exc:
            self.error = exc
        finally:
            if output:
                output.close()
            self.pipe.close()


def run_process(command, *, cwd, timeout, cancel_event=None, stdout_path=None,
                stderr_path=None, output_directory=None, max_output_bytes=None, env=None):
    """Run a trusted command without a shell, with bounded capture and lifetime.

    Windows uses an owned Job Object. On Linux, cancellation while the ancestor
    is live also discovers nested child sessions; after normal ancestor exit,
    only the original session is guaranteed. Commands must not daemonize into
    detached sessions and exit. Other POSIX systems terminate the owned session
    only. Output-directory
    budgets are polled, so a temporary overshoot before termination is possible.
    An explicit environment applies to this child only; None inherits the caller.
    No global process names, credentials, or unrelated processes are inspected.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    if not command or isinstance(command, (str, bytes)) or any(not isinstance(arg, str) or "\0" in arg for arg in command):
        raise ValueError("command must be a nonempty sequence of strings")
    if max_output_bytes is not None and (type(max_output_bytes) is not int or max_output_bytes <= 0 or output_directory is None):
        raise ValueError("output budget requires a directory and a positive byte limit")
    started = time.monotonic()
    if cancel_event is not None and cancel_event.is_set():
        return ProcessResult(None, "", "", False, True, 0.0, False)
    # CREATE_SUSPENDED closes the spawn-before-job-assignment race on Windows.
    options = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004} if os.name == "nt" else {"start_new_session": True}
    process = subprocess.Popen(list(command), cwd=cwd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, **options)
    job = None
    captures = [_Capture(process.stdout, stdout_path), _Capture(process.stderr, stderr_path)]
    threads = [threading.Thread(target=capture.drain, daemon=True) for capture in captures]
    timed_out = cancelled = budget_exceeded = False
    try:
        if os.name == "nt":
            job = _WindowsJob(process)
        for thread in threads:
            thread.start()
        if job is not None:
            job.resume(process)
        while process.poll() is None:
            cancelled = cancel_event is not None and cancel_event.is_set()
            timed_out = time.monotonic() - started >= timeout
            if output_directory is not None and max_output_bytes is not None:
                total = 0
                for path in Path(output_directory).rglob("*"):
                    try:
                        if path.is_file() and not path.is_symlink():
                            total += path.stat().st_size
                    except FileNotFoundError:
                        pass
                    if total > max_output_bytes:
                        budget_exceeded = True
                        break
            if cancelled or timed_out or budget_exceeded or any(capture.error for capture in captures):
                break
            time.sleep(0.02)
    finally:
        if job is not None:
            job.close()
        elif os.name != "nt":
            _kill_posix_tree(process)
        elif process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=5)
            else:
                captures[threads.index(thread)].pipe.close()
        if any(thread.is_alive() for thread in threads):
            raise RuntimeError("owned subprocess log streams did not close")
    if any(capture.error for capture in captures):
        raise OSError("failed to capture subprocess logs") from next(c.error for c in captures if c.error)
    return ProcessResult(process.returncode, *(bytes(c.tail).decode("utf-8", "replace") for c in captures),
                         timed_out, cancelled, time.monotonic() - started,
                         any(c.total > LOG_LIMIT for c in captures), budget_exceeded)
