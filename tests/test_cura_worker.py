import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from plugin_test_support import PLUGIN, ROOT, load_worker

worker = load_worker()


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.output, self.report = self.root / "output", self.root / "report.json"

    def report_fixture(self, *, center="disabled", operation="generate"):
        self.output.mkdir(exist_ok=True)
        keys = ["negative", "positive"] if operation == "generate" else list(worker.VIEWS)
        if center in {"generated", "cavity_fallback"}:
            keys.append("center")
        artifacts = {}
        for key in keys:
            file = self.output / (key + (".stl" if operation == "generate" else ".png"))
            file.write_bytes(b"synthetic-test-artifact")
            artifacts[key] = str(file)
        report = {"schema_version": 1, "status": "success", "operation": operation,
                  "center_insert": center, "warnings": [], "error": None,
                  "outputs" if operation == "generate" else "previews": artifacts}
        self.report.write_text(json.dumps(report), encoding="utf-8")
        return report

    def test_success_does_not_import_stale_center(self):
        self.report_fixture(center="skipped")
        (self.output / "center.stl").write_bytes(b"old-center")
        result = worker.read_report(self.report, self.output, "generate")
        self.assertEqual(set(result.artifacts), {"negative", "positive"})
        self.assertEqual(result.center_insert, "skipped")

    def test_generated_and_fallback_center_are_explicit(self):
        for center in ("generated", "cavity_fallback"):
            with self.subTest(center=center):
                self.report_fixture(center=center)
                self.assertIn("center", worker.read_report(self.report, self.output, "generate").artifacts)

    def test_outside_missing_duplicate_and_empty_artifacts_fail(self):
        for variant in ("outside", "missing", "duplicate", "empty"):
            with self.subTest(variant=variant):
                report = self.report_fixture()
                if variant == "outside":
                    outside = self.root / "outside.stl"
                    outside.write_bytes(b"private")
                    report["outputs"]["positive"] = str(outside)
                elif variant == "missing":
                    Path(report["outputs"]["positive"]).unlink()
                elif variant == "duplicate":
                    report["outputs"]["positive"] = report["outputs"]["negative"]
                else:
                    Path(report["outputs"]["positive"]).write_bytes(b"")
                self.report.write_text(json.dumps(report), encoding="utf-8")
                with self.assertRaises((ValueError, FileNotFoundError)):
                    worker.read_report(self.report, self.output, "generate")

    def test_inconsistent_center_schema_operation_and_partial_previews_fail(self):
        report = self.report_fixture()
        for changes in ({"center_insert": "generated"}, {"schema_version": 2}, {"operation": "preview"}, {"status": "unknown"}):
            with self.subTest(changes=changes):
                self.report.write_text(json.dumps(dict(report, **changes)), encoding="utf-8")
                with self.assertRaises(ValueError):
                    worker.read_report(self.report, self.output, "generate")
        report = self.report_fixture(operation="preview")
        self.assertEqual(len(worker.read_report(self.report, self.output, "preview").artifacts), 6)
        del report["previews"]["BACK"]
        self.report.write_text(json.dumps(report), encoding="utf-8")
        with self.assertRaises(ValueError):
            worker.read_report(self.report, self.output, "preview")

    def operation(self, code, timeout=3):
        return worker.BackendOperation([sys.executable, "-c", code], cwd=self.root,
                                       output_dir=self.output, report_path=self.report,
                                       operation="generate", timeout=timeout)

    def test_missing_report_and_nonzero_exit_fail_closed(self):
        for code in ("pass", "raise SystemExit(2)"):
            with self.subTest(code=code):
                result = self.operation(code).run()
                self.assertEqual(result.status, "failed")
                self.assertFalse(result.artifacts)
                for file in self.root.glob("*.log"):
                    file.unlink()

    def test_actual_child_success_is_report_driven(self):
        code = '''import json
from pathlib import Path
p=Path('output'); p.mkdir()
artifacts={}
for name in ('negative','positive'):
    f=(p/(name+'.stl')).resolve(); f.write_bytes(b'synthetic'); artifacts[name]=str(f)
Path('report.json').write_text(json.dumps(dict(schema_version=1,status='success',operation='generate',outputs=artifacts,center_insert='disabled',warnings=[],error=None)))
'''
        operation = self.operation(code)
        self.assertEqual(operation.run().status, "success")
        self.assertEqual(operation.run().status, "failed")

    def test_timeout_and_running_cancel_release_worker(self):
        started = time.monotonic()
        result = self.operation("import time; time.sleep(20)", timeout=0.15).run()
        self.assertEqual(result.status, "failed")
        self.assertIn("time limit", result.error)
        self.assertLess(time.monotonic() - started, 5)
        for file in self.root.glob("*.log"):
            file.unlink()
        operation = self.operation("import time; time.sleep(20)")
        results = []
        thread = threading.Thread(target=lambda: results.append(operation.run()))
        thread.start()
        time.sleep(0.15)
        operation.cancel()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0].status, "cancelled")

    def test_pre_cancel_and_existing_output_do_not_launch(self):
        operation = self.operation("raise AssertionError('must not run')")
        operation.cancel()
        self.assertEqual(operation.run().status, "cancelled")
        self.assertFalse(list(self.root.iterdir()))
        self.output.mkdir()
        result = self.operation("raise AssertionError('must not run')").run()
        self.assertIn("fresh", result.error)
        self.assertFalse(self.report.exists())

    def test_process_helper_has_exact_shared_bytes(self):
        self.assertEqual((PLUGIN / "process.py").read_bytes(), (ROOT / "src/e3p_mold/process.py").read_bytes())

    def test_child_environment_restores_apprun_values_without_mutating_parent(self):
        bundle = str(self.root / "frozen")
        original = {"APPDIR": bundle, "PATH": bundle + os.pathsep + "/system/bin",
                    "APPRUN_ORIGINAL_PATH": "/external/bin", "PYTHONPATH": bundle,
                    "PYTHONHOME": bundle, "QT_PLUGIN_PATH": bundle,
                    "LD_LIBRARY_PATH": bundle, "LD_LIBRARY_PATH_ORIG": bundle + "/apprun",
                    "APPRUN_ORIGINAL_LD_LIBRARY_PATH": "/original/lib",
                    "LD_PRELOAD": bundle + "/hooks.so", "APPRUN_ORIGINAL_LD_PRELOAD": "",
                    "KEPT_FIXTURE": "kept"}
        before = dict(original)
        env = worker.external_environment(original)
        self.assertEqual(original, before)
        self.assertEqual(env["PATH"], "/external/bin")
        self.assertEqual(env["LD_LIBRARY_PATH"], "/original/lib")
        self.assertEqual(env["KEPT_FIXTURE"], "kept")
        for key in ("APPDIR", "PYTHONHOME", "PYTHONPATH", "QT_PLUGIN_PATH", "LD_PRELOAD", "LD_LIBRARY_PATH_ORIG"):
            self.assertNotIn(key, env)
        self.assertFalse(any(name.startswith("APPRUN_") for name in env))

    def test_external_bootstrap_uses_isolated_python_and_preserves_arguments(self):
        launcher = self.root / "launcher.py"
        launcher.write_text("import json,sys\nfrom pathlib import Path\nPath('bootstrap.json').write_text(json.dumps({'isolated':sys.flags.isolated,'argv':sys.argv[1:]}))\n")
        operation = self.operation("pass")
        operation.command = [sys.executable, "-I", str(PLUGIN / "external_python.py"), str(launcher), "generate", "path with spaces.stl"]
        # There is deliberately no generation report; inspect only this bootstrap's output.
        operation.run()
        result = json.loads((self.root / "bootstrap.json").read_text())
        self.assertEqual(result, {"isolated": 1, "argv": ["generate", "path with spaces.stl"]})


if __name__ == "__main__":
    unittest.main()
