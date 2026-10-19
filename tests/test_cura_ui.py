"""Real offscreen Qt widgets with a small UM/Cura harness, not a Cura acceptance test."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

from plugin_test_support import load_ui, load_worker

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PyQt6.QtCore import QRectF
    from PyQt6.QtGui import QColor, QPixmap
    from PyQt6.QtWidgets import QApplication, QDialogButtonBox
except ImportError:
    QApplication = None


@unittest.skipIf(QApplication is None, "Install the optional PyQt6 test dependency for widget checks.")
class CuraWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.ui = load_ui()
        cls.worker = load_worker()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "original.stl"
        self.source.write_text("synthetic source")
        self.ui.Application.imports.clear()
        self.ui.Message.messages.clear()

    def dialog(self, renderer, cancel=lambda: None):
        dialog = self.ui._GenerateDialog(source_path=str(self.source), parting_view="TOP",
                                        parting_angle_deg=90, exterior_mode="BOX", alignment_keys=True,
                                        center_insert=False, center_insert_source="CAVITY", center_insert_span=8,
                                        clearance=0.4, wall=6, bottom=6, top=6,
                                        preview_renderer=renderer, cancel_operation=cancel)
        self.addCleanup(dialog.close)
        return dialog

    def operation(self, operation="generate"):
        return self.worker.BackendOperation(["unused"], cwd=self.root, output_dir=self.root / "output",
                                            report_path=self.root / "report.json", operation=operation)

    def test_preview_returns_while_busy_and_blocks_accept_until_callback(self):
        pending = []
        dialog = self.dialog(lambda source, callback: pending.append((source, callback)))
        dialog._pick_parting_angle()
        self.assertEqual(len(pending), 1)
        self.assertTrue(dialog._preview_busy)
        self.assertFalse(dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled())
        self.assertFalse(dialog.source_edit.isEnabled())
        self.assertTrue(dialog.cancel_preview_button.isEnabled())
        dialog.accept()
        self.assertFalse(dialog._closed)
        dialog._pick_parting_angle()
        self.assertEqual(len(pending), 1)
        pending[0][1](self.worker.OperationResult("failed", "injected failure"))
        self.assertFalse(dialog._preview_busy)
        self.assertTrue(dialog.source_edit.isEnabled())
        self.assertEqual(dialog.preview_status.text(), "injected failure")

    def test_close_during_preview_cancels_and_late_success_does_not_open_picker(self):
        pending, cancellations = [], []
        dialog = self.dialog(lambda source, callback: pending.append(callback), lambda: cancellations.append(True))
        dialog._pick_parting_angle()
        dialog.reject()
        self.assertEqual(cancellations, [True])
        pending[0](self.worker.OperationResult("success", artifacts={}))
        self.assertTrue(dialog._closed)
        self.assertFalse(dialog._preview_busy)
        self.assertEqual(dialog.preview_status.text(), "")

    def test_preview_launch_exception_resets_controls(self):
        def fail(*_):
            raise RuntimeError("unconfigured runtime")
        dialog = self.dialog(fail)
        dialog._pick_parting_angle()
        self.assertFalse(dialog._preview_busy)
        self.assertTrue(dialog.pick_button.isEnabled())
        self.assertEqual(dialog.preview_status.text(), "unconfigured runtime")

    def test_missing_and_unreadable_views_disabled_no_stale_view_switch(self):
        image = self.root / "top.png"
        pixmap = QPixmap(20, 20)
        pixmap.fill(QColor("white"))
        pixmap.save(str(image))
        corrupt = self.root / "back.png"
        corrupt.write_text("not a png")
        dialog = self.ui._AnglePickerDialog(source_path=str(self.source), preview_paths={"TOP": image, "BACK": corrupt},
                                            parting_view="BACK", parting_angle_deg=45)
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.selected_view, "TOP")
        self.assertTrue(dialog._view_buttons["TOP"].isEnabled())
        self.assertFalse(dialog._view_buttons["BACK"].isEnabled())
        self.assertFalse(dialog._view_buttons["LEFT"].isEnabled())
        dialog._set_active_view("LEFT")
        self.assertEqual(dialog.selected_view, "TOP")
        self.assertFalse(dialog.editor._pixmap.isNull())

    def test_angle_mouse_mapping_and_spin_remain_bidirectional(self):
        editor = self.ui._AngleLineEditor(90)
        editor._image_rect = QRectF(0, 0, 100, 100)
        angles = []
        editor.on_angle_changed = angles.append
        editor._update_angle_from_pos(100, 50)
        self.assertAlmostEqual(editor.angle(), 0)
        editor._update_angle_from_pos(75, 25)
        self.assertAlmostEqual(editor.angle(), 45)
        editor._update_angle_from_pos(50, 50)
        editor._update_angle_from_pos(101, 50)
        self.assertAlmostEqual(editor.angle(), 45)
        editor.setAngle(225)
        self.assertEqual(angles, [0, 45])
        image = self.root / "top.png"
        pixmap = QPixmap(20, 20)
        pixmap.fill(QColor("white"))
        pixmap.save(str(image))
        dialog = self.ui._AnglePickerDialog(source_path=str(self.source), preview_paths={"TOP": image},
                                            parting_view="TOP", parting_angle_deg=45)
        self.addCleanup(dialog.close)
        dialog.angle_spin.setValue(70)
        self.assertEqual(dialog.editor.angle(), 70)
        dialog.editor.setAngle(30)
        self.assertEqual(dialog.angle_spin.value(), 30)

    def test_one_active_job_and_stale_finished_signal_preserves_progress(self):
        extension = self.ui.E3PBlenderMold()
        extension._start_job(self.operation())
        active = extension._active_job
        progress = extension._progress_message
        self.assertTrue(active.started)
        with self.assertRaisesRegex(RuntimeError, "already running"):
            extension._start_job(self.operation("preview"))
        extension._onJobFinished(self.ui.BlenderMoldJob(self.operation()))
        self.assertIs(extension._active_job, active)
        self.assertFalse(progress.hidden)
        extension.cancelActiveOperation()
        self.assertTrue(active.operation.cancel_event.is_set())
        extension._onJobFinished(active)
        self.assertIsNone(extension._active_job)
        self.assertTrue(progress.hidden)

    def test_success_queues_only_reported_files_and_honest_message(self):
        extension = self.ui.E3PBlenderMold()
        extension._start_job(self.operation())
        job = extension._active_job
        job.result = self.worker.OperationResult("success", artifacts={"negative": self.root / "negative.stl", "positive": self.root / "positive.stl"}, center_insert="skipped")
        (self.root / "center.stl").write_text("stale")
        extension._onJobFinished(job)
        self.assertEqual(len(self.ui.Application.imports), 2)
        self.assertNotIn(str(self.root / "center.stl"), self.ui.Application.imports)
        self.assertIn("Queued 2", self.ui.Message.messages[-1].kwargs["text"])
        self.assertIn("Check Cura", self.ui.Message.messages[-1].kwargs["text"])

    def test_cancel_after_worker_success_prevents_import(self):
        extension = self.ui.E3PBlenderMold()
        extension._start_job(self.operation())
        job = extension._active_job
        job.result = self.worker.OperationResult("success", artifacts={"negative": self.source})
        extension.cancelActiveOperation()
        extension._onJobFinished(job)
        self.assertEqual(self.ui.Application.imports, [])

    def test_preview_completion_uses_callback_never_imports(self):
        extension, results = self.ui.E3PBlenderMold(), []
        extension._start_job(self.operation("preview"), results.append)
        job = extension._active_job
        job.result = self.worker.OperationResult("success", artifacts={"TOP": self.root / "top.png"})
        extension._onJobFinished(job)
        self.assertEqual(results, [job.result])
        self.assertEqual(self.ui.Application.imports, [])
        self.assertIsNone(extension._active_job)

    def test_explicit_runtime_missing_fails_without_cura_python_fallback(self):
        extension = self.ui.E3PBlenderMold()
        extension._preferences.setValue("e3p_blender_mold/python_exe", str(self.root / "missing-python"))
        with self.assertRaisesRegex(RuntimeError, "external Python"):
            extension._resolve_python(self.root)
        extension._preferences.setValue("e3p_blender_mold/project_root", "")
        with self.assertRaises(RuntimeError):
            extension._resolve_project_root()

    def test_venv_python_symlink_is_not_resolved_to_system_interpreter(self):
        extension = self.ui.E3PBlenderMold()
        extension._preferences.setValue("e3p_blender_mold/python_exe", "")
        python = self.root / ".venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        try:
            python.symlink_to(sys.executable)
        except OSError:
            self.skipTest("Creating symlinks is not available to this account.")
        self.assertEqual(extension._resolve_python(self.root), python)
        self.assertNotEqual(extension._resolve_python(self.root), python.resolve())


if __name__ == "__main__":
    unittest.main()
