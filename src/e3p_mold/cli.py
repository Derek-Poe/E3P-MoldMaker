"""Fresh-output orchestration around the Blender geometry engine."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import signal
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path

from .process import run_process
from .validation import confined_file, validate_png, validate_stl

VIEWS = {"TOP", "BOTTOM", "FRONT", "BACK", "LEFT", "RIGHT"}
INPUT_BUDGET = 64 * 1024 * 1024
OUTPUT_BUDGET = 256 * 1024 * 1024


class Cancelled(Exception):
    pass


def _finite(text):
    value = float(text)
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError("value must be finite")
    return value


def _positive(text):
    value = _finite(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return value


def _bytes(text):
    value = int(text)
    if not 0 < value <= 1024 * 1024 * 1024:
        raise argparse.ArgumentTypeError("byte budget must be between 1 and 1073741824")
    return value


def build_parser():
    parser = argparse.ArgumentParser(description="Generate and validate STL mold geometry with headless Blender")
    commands = parser.add_subparsers(dest="operation", required=True)
    for operation in ("generate", "preview"):
        sub = commands.add_parser(operation)
        sub.add_argument("input", type=Path)
        sub.add_argument("-o", "--output-dir", required=True, type=Path)
        sub.add_argument("--blender-exe", default=os.environ.get("E3P_BLENDER_EXE", "blender"))
        sub.add_argument("--timeout", type=_positive, default=180.0)
        sub.add_argument("--max-input-bytes", type=_bytes, default=INPUT_BUDGET)
        sub.add_argument("--max-output-bytes", type=_bytes, default=OUTPUT_BUDGET)
        sub.add_argument("--report", type=Path)
        sub.add_argument("--center-fallback", action=argparse.BooleanOptionalAction, default=True)
        geometry = sub.add_argument_group("geometry options (forwarded to Blender)")
        for name in ("clearance", "wall", "bottom", "top", "split-offset", "parting-angle-deg", "voxel-size",
                     "key-radius", "key-clearance", "key-engagement", "key-root", "key-lateral-margin", "key-z-margin",
                     "perimeter-lock-width", "perimeter-lock-engagement", "perimeter-lock-root", "perimeter-lock-clearance",
                     "center-insert-total-span", "center-insert-clearance", "center-insert-base-thickness",
                     "injection-inner-radius", "injection-outer-radius", "vent-radius", "channel-breakthrough", "preview-split-gap"):
            geometry.add_argument("--" + name, type=_finite, default=None)
        for name, choices in (("split-axis", ("X", "Y")), ("parting-view", sorted(VIEWS)),
                              ("exterior-mode", ("BOX", "FOOTPRINT_HULL")), ("center-insert-source", ("CAVITY", "PART"))):
            geometry.add_argument("--" + name, choices=choices, default=None)
        for name in ("alignment-keys", "center-insert", "perimeter-lock", "injection-site", "vent",
                     "auto-rotate-cavity-up", "render-preview", "render-angle-previews", "clear-scene", "allow-mesh-repair"):
            geometry.add_argument("--" + name, action=argparse.BooleanOptionalAction, default=None)
        for name in ("preview-width", "preview-height", "angle-preview-size"):
            geometry.add_argument("--" + name, type=int, default=None)
        for name in ("export-negative", "export-positive", "export-center", "preview-image", "angle-preview-prefix"):
            geometry.add_argument("--" + name, default=None)
        sub.set_defaults(geometry_names=[action.dest for action in geometry._group_actions])
    return parser


def _geometry_arguments(args, center_mode):
    values = {name: getattr(args, name) for name in args.geometry_names}
    if args.operation == "preview":
        values["render_angle_previews"] = True
    if center_mode == "disabled":
        values["center_insert"] = False
    elif center_mode in ("PART", "CAVITY"):
        values["center_insert"] = True
        values["center_insert_source"] = center_mode
    result = []
    for name, value in values.items():
        if value is None:
            continue
        option = name.replace("_", "-")
        if type(value) is bool:
            result.append("--" + ("" if value else "no-") + option)
        else:
            result.extend(["--" + option, str(value)])
    if args.operation == "preview":
        result.append("--angle-preview-only")
    return result


def _manifest(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 131072:
        raise ValueError("Blender did not provide a bounded artifact manifest")
    data = json.loads(path.read_text(encoding="utf-8"))
    json.dumps(data, allow_nan=False)  # Reject nonfinite numbers, including metadata.
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("unsupported artifact manifest")
    if data.get("status") not in ("success", "failed"):
        raise ValueError("artifact manifest has no explicit outcome")
    return data


def _validated_artifacts(artifact, attempt, args, center_mode, check):
    if artifact.get("operation") != args.operation or artifact.get("status") != "success":
        raise ValueError("artifact operation/outcome does not match the request")
    outputs = artifact.get("outputs", {})
    previews = artifact.get("previews", {})
    if not isinstance(outputs, dict) or not isinstance(previews, dict):
        raise ValueError("artifact paths must be objects")
    features = artifact.get("features")
    if not isinstance(features, dict) or type(features.get("center_insert")) is not bool:
        raise ValueError("artifact manifest must identify enabled center-insert state")
    if args.operation == "generate":
        expected = {"negative", "positive"} | ({"center"} if center_mode != "disabled" else set())
        if set(outputs) != expected or features["center_insert"] != (center_mode != "disabled"):
            raise ValueError("artifact does not contain exactly the requested mold pieces")
        if not set(previews) <= VIEWS | {"ASSEMBLY"}:
            raise ValueError("unexpected preview names")
    elif outputs or set(previews) != VIEWS:
        raise ValueError("preview operation must return exactly six views and no mold outputs")
    paths, statistics = {}, {}
    total = 0
    for group, mapping, extension, validate in (("outputs", outputs, ".stl", validate_stl), ("previews", previews, ".png", validate_png)):
        paths[group], statistics[group] = {}, {}
        for name, value in mapping.items():
            check()
            if not isinstance(value, str):
                raise ValueError("artifact path must be a string")
            path = confined_file(attempt, value, suffix=extension, max_bytes=args.max_output_bytes)
            if path in [p for existing in paths.values() for p in existing.values()]:
                raise ValueError("artifact paths must be distinct")
            total += path.stat().st_size
            if total > args.max_output_bytes:
                raise ValueError("combined artifacts exceed the output budget")
            kwargs = {"max_bytes": args.max_output_bytes}
            if group == "outputs":
                kwargs["check"] = check
            statistics[group][name] = validate(path, **kwargs)
            paths[group][name] = path
    warnings = artifact.get("warnings", [])
    if not isinstance(warnings, list) or len(warnings) > 100 or any(not isinstance(w, str) or len(w) > 2000 for w in warnings):
        raise ValueError("invalid artifact warnings")
    return paths, statistics, warnings


def run_job(args, *, runner=run_process, cancel_event=None):
    started = time.monotonic()
    deadline = started + args.timeout
    cancelled = cancel_event or threading.Event()
    report = {"schema_version": 1, "status": "failed", "operation": args.operation,
              "outputs": {}, "previews": {}, "center_insert": "disabled", "warnings": [],
              "attempts": [], "error": None, "validation": {}}
    output = args.output_dir.expanduser().absolute()
    report_path = args.report.expanduser().absolute() if args.report else output / "report.json"
    reserved = False

    def check():
        if cancelled.is_set():
            raise Cancelled("Operation cancelled")
        if time.monotonic() >= deadline:
            raise TimeoutError("Overall operation timeout exceeded")

    try:
        check()
        if report_path.exists() or report_path.is_symlink():
            raise ValueError("report path already exists; refusing to overwrite it")
        source = args.input.expanduser().resolve(strict=True)
        if not source.is_file() or source.suffix.lower() != ".stl" or not 0 < source.stat().st_size <= args.max_input_bytes:
            raise ValueError("input must be a nonempty STL within the input byte budget")
        blender = shutil.which(args.blender_exe) or args.blender_exe
        blender = Path(blender).expanduser().resolve(strict=True)
        if not blender.is_file():
            raise ValueError("Blender executable must be a file")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.mkdir()  # Exclusive reservation; existing directories are never reused.
        reserved = True
        output = output.resolve()
        initial = (args.center_insert_source or "CAVITY") if args.center_insert and args.operation == "generate" else "disabled"
        modes = [initial]
        if args.center_fallback and initial != "disabled":
            if initial == "PART":
                modes.append("CAVITY")
            modes.append("disabled")
        script = Path(__file__).parent / "blender" / "generate.py"
        for index, mode in enumerate(modes, 1):
            check()
            with tempfile.TemporaryDirectory(prefix=f".attempt-{index}-", dir=output) as temporary:
                attempt = Path(temporary)
                artifact_path = attempt / "artifacts.json"
                command = [str(blender), "--background", "--factory-startup", "--disable-autoexec",
                           "--python-exit-code", "23", "--python", str(script), "--", str(source),
                           "-o", str(attempt), "--artifact-manifest", str(artifact_path),
                           "--max-input-bytes", str(args.max_input_bytes), "--max-output-bytes", str(args.max_output_bytes),
                           *_geometry_arguments(args, mode)]
                result = runner(command, cwd=str(attempt), timeout=max(0.001, deadline - time.monotonic()),
                                cancel_event=cancelled, stdout_path=output / f"attempt-{index}.stdout.log",
                                stderr_path=output / f"attempt-{index}.stderr.log",
                                output_directory=output, max_output_bytes=args.max_output_bytes)
                entry = {key: value for key, value in asdict(result).items() if key not in ("stdout", "stderr")}
                entry["kind"] = mode.lower()
                report["attempts"].append(entry)
                if result.cancelled or cancelled.is_set():
                    raise Cancelled("Operation cancelled")
                if result.timed_out:
                    raise TimeoutError("Overall operation timeout exceeded")
                if result.budget_exceeded:
                    raise ValueError("attempt exceeded the output byte budget")
                check()
                artifact = _manifest(artifact_path)
                entry["artifact_status"] = artifact["status"]
                if result.returncode != 0 or artifact["status"] != "success":
                    entry["error_kind"] = str(artifact.get("error_kind", "generator"))[:80]
                    entry["error"] = str(artifact.get("error", "Blender failed; inspect this attempt's bounded logs"))[:2000]
                    if (result.returncode != 0 and artifact["status"] == "failed"
                            and artifact.get("operation") == args.operation and artifact.get("error_kind") == "center_insert"
                            and mode != "disabled" and index < len(modes)):
                        report["warnings"].append(f"Center insert from {mode} failed; retrying with {modes[index]}.")
                        continue
                    raise ValueError(entry["error"])
                paths, statistics, warnings = _validated_artifacts(artifact, attempt, args, mode, check)
                staging = attempt / "publish"
                staging.mkdir()
                published = output / "artifacts"
                for group, mapping in paths.items():
                    for name, path in mapping.items():
                        filename = f"{name.lower()}{path.suffix.lower()}"
                        path.rename(staging / filename)
                        report[group][name] = str(published / filename)
                check()
                staging.rename(published)
                report["status"] = "success"
                report["validation"] = statistics
                report["geometry"] = artifact.get("geometry", {})
                report["features"] = artifact["features"]
                report["warnings"].extend(warnings)
                report["center_insert"] = ("disabled" if initial == "disabled" else "skipped" if mode == "disabled"
                                           else "cavity_fallback" if initial == "PART" and mode == "CAVITY" else "generated")
                if report["center_insert"] == "skipped":
                    report["warnings"].append("Center insert was skipped; only the two validated outer halves were generated.")
                break
        else:
            raise ValueError("No generation attempt succeeded")
    except Cancelled as exc:
        report["status"], report["error"] = "cancelled", str(exc)
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        report["error"] = str(exc)[:2000]
    if report["status"] != "success":
        report["outputs"], report["previews"], report["validation"] = {}, {}, {}
    report["elapsed_seconds"] = time.monotonic() - started
    # An explicit report can record an early validation failure. A default report
    # is written only inside the directory this invocation successfully reserved.
    if args.report is not None or reserved:
        try:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            with report_path.open("x", encoding="utf-8") as stream:
                json.dump(report, stream, indent=2, allow_nan=False)
                stream.write("\n")
        except OSError as exc:
            report["status"], report["error"] = "failed", f"Could not write fresh report: {exc}"
            report["outputs"], report["previews"], report["validation"] = {}, {}, {}
    return report


def main(argv=None):
    args = build_parser().parse_args(argv)
    cancelled = threading.Event()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            if hasattr(signal, name):
                sig = getattr(signal, name)
                previous[sig] = signal.signal(sig, lambda *_: cancelled.set())
    try:
        report = run_job(args, cancel_event=cancelled)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    print(json.dumps(report, allow_nan=False))
    return 0 if report["status"] == "success" else 130 if report["status"] == "cancelled" else 2
