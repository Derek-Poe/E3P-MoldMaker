"""Self-contained Cura-to-CLI boundary; does not import the external package."""
from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .process import run_process

VIEWS = ("TOP", "BOTTOM", "FRONT", "BACK", "LEFT", "RIGHT")
MAX_REPORT_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024


def external_environment(environ=None) -> dict[str, str]:
    """Undo frozen Cura/AppImage path injection for the child, never globally."""
    env = dict(os.environ if environ is None else environ)
    bundles = [value for value in (env.get("APPDIR"), getattr(sys, "_MEIPASS", None)) if value]

    def outside_bundle(value):
        path = os.path.normcase(os.path.abspath(value))
        return all(path != os.path.normcase(os.path.abspath(root)) and
                   not path.startswith(os.path.normcase(os.path.abspath(root)) + os.sep) for root in bundles)

    for name in ("PATH", "LD_LIBRARY_PATH", "LD_PRELOAD", "DYLD_LIBRARY_PATH", "LIBPATH"):
        original = "APPRUN_ORIGINAL_" + name
        if original in env:
            value = env[original]
        elif name + "_ORIG" in env:
            value = env[name + "_ORIG"]
        else:
            separator = " " if name == "LD_PRELOAD" else os.pathsep
            value = separator.join(entry for entry in env.get(name, "").split(separator)
                                   if entry and outside_bundle(entry))
        if value:
            env[name] = value
        else:
            env.pop(name, None)
    for name in list(env):
        if name.startswith(("PYTHON", "_PYI", "PYINSTALLER_", "APPRUN_")) or name in {
            "APPDIR", "APPIMAGE", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH",
            "QML2_IMPORT_PATH", "QML_IMPORT_PATH", "LD_LIBRARY_PATH_ORIG", "LD_PRELOAD_ORIG",
        }:
            env.pop(name, None)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


@dataclass
class OperationResult:
    status: str
    error: str = ""
    artifacts: dict[str, Path] = field(default_factory=dict)
    center_insert: str = "disabled"
    warnings: list[str] = field(default_factory=list)


def read_report(report_path: Path, output_dir: Path, operation: str) -> OperationResult:
    """Reject incomplete, inconsistent or out-of-directory success reports."""
    if report_path.is_symlink() or not report_path.is_file():
        raise ValueError("Backend did not write a regular outcome report.")
    if report_path.stat().st_size > MAX_REPORT_BYTES:
        raise ValueError("Backend outcome report exceeded its size budget.")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(report, dict) or report.get("schema_version") != 1:
        raise ValueError("Unsupported backend report schema.")
    if report.get("operation") != operation:
        raise ValueError("Backend reported a different operation.")
    status = report.get("status")
    if status not in {"success", "failed", "cancelled"}:
        raise ValueError("Invalid backend outcome status.")
    if status != "success":
        return OperationResult(status, str(report.get("error") or status)[:1600])
    if report.get("error"):
        raise ValueError("Successful backend report also contains an error.")
    warnings = report.get("warnings", [])
    if not isinstance(warnings, list) or not all(isinstance(w, str) for w in warnings):
        raise ValueError("Malformed backend warnings.")
    center = report.get("center_insert", "disabled")
    if center not in {"disabled", "generated", "cavity_fallback", "skipped"}:
        raise ValueError("Unknown center insert outcome.")
    raw = report.get("outputs" if operation == "generate" else "previews")
    required = {"negative", "positive"} if operation == "generate" else set(VIEWS)
    allowed = required | ({"center"} if operation == "generate" else set())
    if not isinstance(raw, dict) or not required <= raw.keys() or not raw.keys() <= allowed:
        raise ValueError("Backend report does not list the required artifacts.")
    if operation == "generate" and (("center" in raw) != (center in {"generated", "cavity_fallback"})):
        raise ValueError("Center insert outcome and artifact list disagree.")
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise ValueError("Backend output directory is missing or is a symbolic link.")
    root = output_dir.resolve()
    artifacts: dict[str, Path] = {}
    for name, value in raw.items():
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError("Backend artifact paths must be absolute.")
        path = Path(value)
        resolved = path.resolve(strict=True)
        if root not in resolved.parents or path.is_symlink() or any(p.is_symlink() for p in path.parents if p != root and root in p.parents):
            raise ValueError("Backend artifact escaped its output directory.")
        suffix = ".stl" if operation == "generate" else ".png"
        if not resolved.is_file() or resolved.suffix.lower() != suffix:
            raise ValueError("Backend artifact type is invalid.")
        if not 0 < resolved.stat().st_size <= MAX_ARTIFACT_BYTES:
            raise ValueError("Backend artifact is empty or exceeds its size budget.")
        if resolved in artifacts.values():
            raise ValueError("Backend reused one artifact for multiple outputs.")
        artifacts[name] = resolved
    return OperationResult("success", artifacts=artifacts, center_insert=center, warnings=warnings)


class BackendOperation:
    """One attempt, cancellable even before launch. The CLI owns geometry validation."""
    def __init__(self, command: list[str], *, cwd: Path, output_dir: Path,
                 report_path: Path, operation: str, timeout: float = 190.0):
        if operation not in {"generate", "preview"}:
            raise ValueError("Unknown operation.")
        if not 0 < timeout <= 3700:
            raise ValueError("Operation timeout must be positive and bounded.")
        self.command = list(command)
        self.cwd = cwd
        self.output_dir = output_dir
        self.report_path = report_path
        self.operation = operation
        self.timeout = timeout
        self.cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._started = False

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> OperationResult:
        with self._lock:
            if self._started:
                return OperationResult("failed", "An operation cannot be run twice.")
            self._started = True
        if self.cancel_event.is_set():
            return OperationResult("cancelled", "Operation cancelled.")
        try:
            if self.output_dir.exists() or self.output_dir.is_symlink() or self.report_path.exists() or self.report_path.is_symlink():
                raise ValueError("Operation requires fresh output and report paths.")
            completed = run_process(
                self.command, cwd=self.cwd, timeout=self.timeout,
                cancel_event=self.cancel_event,
                env=external_environment(),
                stdout_path=self.report_path.parent / "launcher-stdout.log",
                stderr_path=self.report_path.parent / "launcher-stderr.log",
            )
            if self.cancel_event.is_set() or completed.cancelled:
                return OperationResult("cancelled", "Operation cancelled; no imports queued.")
            if completed.timed_out:
                return OperationResult("failed", "Backend exceeded the operation time limit.")
            if completed.returncode != 0:
                return OperationResult("failed", "Backend failed (exit %s). See the bounded logs in %s." % (completed.returncode, self.report_path.parent))
            return read_report(self.report_path, self.output_dir, self.operation)
        except Exception as exc:
            return OperationResult("failed", str(exc)[:1600])
