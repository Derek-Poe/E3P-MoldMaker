"""Repository demo helpers: invoke the same public CLI used by callers."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]


def generate(source: Path, output: Path, blender: str, options: list[str], *, timeout: float = 180):
    if output.exists():
        raise FileExistsError("Use a fresh generation output directory.")
    report = output / "report.json"
    command = [sys.executable, "-m", "e3p_mold", "generate", str(source), "-o", str(output),
               "--blender-exe", blender, "--timeout", str(timeout), "--report", str(report), *options]
    started = time.perf_counter()
    with output.with_name(output.name + ".stdout.log").open("xb") as stdout, output.with_name(output.name + ".stderr.log").open("xb") as stderr:
        process = subprocess.run(command, cwd=PACKAGE, stdout=stdout, stderr=stderr,
                                 timeout=timeout + 30, check=False)
    result = json.loads(report.read_text(encoding="utf-8")) if report.is_file() else {}
    return process.returncode, result, time.perf_counter() - started
