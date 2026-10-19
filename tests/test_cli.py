import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from mold_fixtures import png, stl

from e3p_mold.cli import VIEWS, build_parser, run_job
from e3p_mold.process import ProcessResult


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.input = self.root / "input.stl"
        stl(self.input)
        self.calls = []

    def arguments(self, *extra, operation="generate"):
        return build_parser().parse_args([operation, str(self.input), "-o", str(self.root / "output"),
                                          "--blender-exe", sys.executable, *extra])

    def success(self, command, **kwargs):
        self.calls.append((command, kwargs))
        path = Path(command[command.index("--artifact-manifest") + 1])
        preview = "--angle-preview-only" in command
        center = "--center-insert" in command
        artifact = {"schema_version": 1, "status": "success", "operation": "preview" if preview else "generate",
                    "outputs": {}, "previews": {}, "features": {"center_insert": center}, "warnings": [],
                    "geometry": {"events": [{"kind": "fixture"}]}}
        if preview:
            for name in VIEWS:
                image = path.parent / f"{name}.png"
                png(image)
                artifact["previews"][name] = str(image)
        else:
            for name in ["negative", "positive", *(["center"] if center else [])]:
                mesh = path.parent / f"{name}.stl"
                stl(mesh)
                artifact["outputs"][name] = str(mesh)
        path.write_text(json.dumps(artifact))
        return ProcessResult(0, "historical center insert failure text must be ignored", "", False, False, 0.01, False)

    def center_failure(self, command, **kwargs):
        self.calls.append((command, kwargs))
        path = Path(command[command.index("--artifact-manifest") + 1])
        path.write_text(json.dumps({"schema_version": 1, "status": "failed", "operation": "generate",
                                    "error_kind": "center_insert", "error": "fixture failure"}))
        return ProcessResult(23, "", "fixture center failure", False, False, 0.01, False)

    def test_success_uses_verified_manifest_and_preserves_input(self):
        before = self.input.read_bytes()
        report = run_job(self.arguments(), runner=self.success)
        self.assertEqual(report["status"], "success")
        self.assertEqual(set(report["outputs"]), {"negative", "positive"})
        self.assertEqual(report["center_insert"], "disabled")
        self.assertEqual(report["geometry"]["events"][0]["kind"], "fixture")
        self.assertEqual(self.input.read_bytes(), before)
        self.assertTrue(all(Path(path).is_file() for path in report["outputs"].values()))
        self.assertFalse(list((self.root / "output").glob(".attempt-*")))
        command = self.calls[0][0]
        for flag in ("--disable-autoexec", "--factory-startup", "--background", "--python-exit-code"):
            self.assertIn(flag, command)
        self.assertEqual(command[command.index("--python-exit-code") + 1], "23")
        self.assertEqual(json.loads((self.root / "output/report.json").read_text())["status"], "success")

    def test_part_to_cavity_retry_ignores_old_failure_text_and_isolated_paths(self):
        def runner(command, **kwargs):
            return self.center_failure(command, **kwargs) if not self.calls else self.success(command, **kwargs)
        report = run_job(self.arguments("--center-insert", "--center-insert-source", "PART"), runner=runner)
        self.assertEqual(report["status"], "success")
        self.assertEqual(report["center_insert"], "cavity_fallback")
        self.assertIn("center", report["outputs"])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(report["attempts"][0]["error_kind"], "center_insert")
        self.assertEqual(report["attempts"][0]["error"], "fixture failure")
        self.assertNotEqual(self.calls[0][1]["cwd"], self.calls[1][1]["cwd"])
        self.assertLessEqual(self.calls[1][1]["timeout"], self.calls[0][1]["timeout"])

    def test_center_skip_is_explicit_and_cannot_import_stale_center(self):
        def runner(command, **kwargs):
            if len(self.calls) < 2:
                result = self.center_failure(command, **kwargs)
                stl(Path(kwargs["cwd"]) / "center.stl")
                return result
            return self.success(command, **kwargs)
        report = run_job(self.arguments("--center-insert", "--center-insert-source", "PART"), runner=runner)
        self.assertEqual(report["status"], "success")
        self.assertEqual(report["center_insert"], "skipped")
        self.assertNotIn("center", report["outputs"])
        self.assertTrue(any("skipped" in warning for warning in report["warnings"]))
        self.assertEqual(len(self.calls), 3)

    def test_missing_manifest_never_uses_stdout_or_unverified_stl(self):
        def runner(command, **kwargs):
            self.calls.append(command)
            stl(Path(kwargs["cwd"]) / "negative.stl")
            return ProcessResult(0, "Negative half: negative.stl\nPositive half: positive.stl", "", False, False, 0.01, False)
        report = run_job(self.arguments("--center-insert"), runner=runner)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["outputs"], {})
        self.assertEqual(len(self.calls), 1)

    def test_missing_or_external_artifact_fails_closed(self):
        for mutation in ("missing", "external", "inverted"):
            with self.subTest(mutation=mutation):
                args = self.arguments()
                args.output_dir = self.root / mutation
                def runner(command, **kwargs):
                    result = self.success(command, **kwargs)
                    path = Path(command[command.index("--artifact-manifest") + 1])
                    artifact = json.loads(path.read_text())
                    output = Path(artifact["outputs"]["negative"])
                    if mutation == "missing":
                        output.unlink()
                    elif mutation == "external":
                        artifact["outputs"]["negative"] = str(self.input)
                    else:
                        from mold_fixtures import FACES
                        stl(output, faces=[face[::-1] for face in FACES])
                    path.write_text(json.dumps(artifact))
                    return result
                report = run_job(args, runner=runner)
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report["outputs"], {})
                self.assertFalse((args.output_dir / "artifacts").exists())

    def test_existing_output_and_report_are_never_overwritten(self):
        args = self.arguments()
        args.output_dir.mkdir()
        sentinel = args.output_dir / "preserve.txt"
        sentinel.write_text("preserve")
        self.assertEqual(run_job(args, runner=self.success)["status"], "failed")
        self.assertEqual(list(args.output_dir.iterdir()), [sentinel])
        args.output_dir = self.root / "unused"
        args.report = self.input
        before = self.input.read_bytes()
        self.assertEqual(run_job(args, runner=self.success)["status"], "failed")
        self.assertEqual(self.input.read_bytes(), before)
        self.assertFalse(self.calls)

    def test_cancel_timeout_and_output_budget_never_publish(self):
        for kind in ("cancelled", "timed_out", "budget_exceeded"):
            with self.subTest(kind=kind):
                args = self.arguments()
                args.output_dir = self.root / kind
                def runner(command, **kwargs):
                    return ProcessResult(1, "", "", kind == "timed_out", kind == "cancelled", .01, False, kind == "budget_exceeded")
                report = run_job(args, runner=runner)
                self.assertEqual(report["status"], "cancelled" if kind == "cancelled" else "failed")
                self.assertEqual(report["outputs"], {})
        event = threading.Event()
        event.set()
        self.assertEqual(run_job(self.arguments(), runner=self.success, cancel_event=event)["status"], "cancelled")

    def test_preview_requires_six_verified_images(self):
        report = run_job(self.arguments(operation="preview"), runner=self.success)
        self.assertEqual(report["status"], "success")
        self.assertEqual(set(report["previews"]), VIEWS)
        self.assertEqual(report["outputs"], {})
        args = self.arguments(operation="preview")
        args.output_dir = self.root / "incomplete"
        def incomplete(command, **kwargs):
            result = self.success(command, **kwargs)
            path = Path(command[command.index("--artifact-manifest") + 1])
            artifact = json.loads(path.read_text())
            del artifact["previews"]["TOP"]
            path.write_text(json.dumps(artifact))
            return result
        self.assertEqual(run_job(args, runner=incomplete)["status"], "failed")

    def test_input_budget_prevents_launch_and_fallback_can_be_disabled(self):
        report = run_job(self.arguments("--max-input-bytes", "1"), runner=self.success)
        self.assertEqual(report["status"], "failed")
        self.assertFalse(self.calls)
        args = self.arguments("--center-insert", "--no-center-fallback")
        report = run_job(args, runner=self.center_failure)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(report["error"], "fixture failure")
