from __future__ import annotations

import json
import math
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from cura.CuraApplication import CuraApplication
from cura.Scene.CuraSceneNode import CuraSceneNode
from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, QUrl
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from UM.Application import Application
from UM.Extension import Extension
from UM.i18n import i18nCatalog
from UM.Job import Job
from UM.Logger import Logger
from UM.Message import Message
from UM.Scene.Iterator.DepthFirstIterator import DepthFirstIterator
from UM.Scene.Selection import Selection

from .worker import BackendOperation, OperationResult

catalog = i18nCatalog("cura")


PLUGIN_ID = "E3PBlenderMold"
DEFAULTS_FILE = "e3p_defaults.json"
PREF_PREFIX = "e3p_blender_mold"
DEFAULT_SPLIT_AXIS = "X"
DEFAULT_EXTERIOR_MODE = "FOOTPRINT_HULL"
DEFAULT_ALIGNMENT_KEYS = True
DEFAULT_CENTER_INSERT = False
DEFAULT_CENTER_INSERT_SOURCE = "CAVITY"
DEFAULT_CENTER_INSERT_SPAN = 8.0
DEFAULT_CLEARANCE = 0.4
SAFE_CLEARANCE_MAX = 0.5
DEFAULT_WALL = 6.0
DEFAULT_BOTTOM = 6.0
DEFAULT_TOP = 6.0
DEFAULT_PARTING_VIEW = "TOP"
DEFAULT_PARTING_ANGLE_DEG = 90.0
PARTING_VIEWS = ("TOP", "BOTTOM", "FRONT", "BACK", "LEFT", "RIGHT")
BACKEND_TIMEOUT = 180.0


def _keep_aspect_ratio_mode():
    return Qt.AspectRatioMode.KeepAspectRatio if hasattr(Qt, "AspectRatioMode") else Qt.KeepAspectRatio


def _smooth_transformation_mode():
    return Qt.TransformationMode.SmoothTransformation if hasattr(Qt, "TransformationMode") else Qt.SmoothTransformation


def _no_mouse_button_value():
    return Qt.MouseButton.NoButton if hasattr(Qt, "MouseButton") else Qt.NoButton


def _has_pressed_mouse_button(buttons) -> bool:
    try:
        return buttons != _no_mouse_button_value()
    except Exception:
        return bool(buttons)


@dataclass(slots=True)
class MoldJobRequest:
    source_path: Path
    output_dir: Path
    report_path: Path
    axis: str
    parting_view: str
    parting_angle_deg: float
    exterior_mode: str
    alignment_keys: bool
    center_insert: bool
    center_insert_source: str
    center_insert_span: float
    clearance: float
    wall: float
    bottom: float
    top: float
    export_negative_name: str
    export_positive_name: str
    export_center_name: str


class BlenderMoldJob(Job):
    """UM runs this worker off the UI thread; its finished signal returns to the UI."""
    def __init__(self, operation: BackendOperation, callback=None) -> None:
        super().__init__()
        self.operation = operation
        self.callback = callback
        self.result = OperationResult("failed", "Operation has not completed.")

    def run(self) -> None:
        self.result = self.operation.run()

    def request_cancel(self) -> None:
        # UM.Job.cancel only removes queued jobs; that would omit finished cleanup.
        self.operation.cancel()


class _SettingsDialog(QDialog):
    def __init__(self, *, project_root: str, blender_exe: str, python_exe: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("E3P Blender Mold Settings")

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        self.project_root_edit = QLineEdit(project_root)
        self.blender_exe_edit = QLineEdit(blender_exe)
        self.python_exe_edit = QLineEdit(python_exe)

        form.addRow("Project Root", self._path_row(self.project_root_edit, directory=True))
        form.addRow("Blender Executable", self._path_row(self.blender_exe_edit, directory=False))
        form.addRow("External Python (project venv if blank)", self._path_row(self.python_exe_edit, directory=False, optional=True))

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _path_row(self, line_edit: QLineEdit, *, directory: bool, optional: bool = False):
        row = QHBoxLayout()
        row.addWidget(line_edit)
        button = QPushButton("Browse")
        button.clicked.connect(lambda: self._browse(line_edit, directory=directory, optional=optional))
        row.addWidget(button)
        container = QVBoxLayout()
        container.addLayout(row)
        widget = QWidget()
        widget.setLayout(container)
        return widget

    def _browse(self, line_edit: QLineEdit, *, directory: bool, optional: bool) -> None:
        if directory:
            path = QFileDialog.getExistingDirectory(self, "Select Directory", line_edit.text().strip() or str(Path.home()))
        else:
            path, _ = QFileDialog.getOpenFileName(self, "Select File", line_edit.text().strip() or str(Path.home()))
        if path or not optional:
            line_edit.setText(path)


class _AngleLineEditor(QWidget):
    def __init__(self, angle_deg: float, parent=None) -> None:
        super().__init__(parent)
        self._angle_deg = float(angle_deg)
        self._pixmap = QPixmap()
        self._image_rect = QRectF()
        self.on_angle_changed = None
        self.setMinimumSize(320, 320)

    def setPixmap(self, pixmap: QPixmap) -> None:
        self._pixmap = pixmap
        self.update()

    def setAngle(self, angle_deg: float, *, emit: bool = True) -> None:
        normalized = float(angle_deg) % 180.0
        if abs(normalized - self._angle_deg) < 1e-6:
            return
        self._angle_deg = normalized
        self.update()
        if emit and callable(self.on_angle_changed):
            self.on_angle_changed(self._angle_deg)

    def angle(self) -> float:
        return self._angle_deg

    def _update_angle_from_pos(self, x: float, y: float) -> None:
        if not self._image_rect.contains(QPointF(x, y)):
            return
        center = self._image_rect.center()
        dx = x - center.x()
        dy = center.y() - y
        if abs(dx) < 1e-6 and abs(dy) < 1e-6:
            return
        angle_deg = math.degrees(math.atan2(dy, dx)) % 180.0
        self.setAngle(angle_deg)

    def mousePressEvent(self, event) -> None:  # pragma: no cover - Qt interaction
        point = event.position() if hasattr(event, "position") else event.pos()
        self._update_angle_from_pos(point.x(), point.y())

    def mouseMoveEvent(self, event) -> None:  # pragma: no cover - Qt interaction
        buttons = event.buttons()
        if _has_pressed_mouse_button(buttons):
            point = event.position() if hasattr(event, "position") else event.pos()
            self._update_angle_from_pos(point.x(), point.y())

    def paintEvent(self, _event) -> None:  # pragma: no cover - Qt interaction
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(244, 245, 247))

        if not self._pixmap.isNull():
            scaled = self._pixmap.scaled(
                self.size(),
                _keep_aspect_ratio_mode(),
                _smooth_transformation_mode(),
            )
            x = (self.width() - scaled.width()) * 0.5
            y = (self.height() - scaled.height()) * 0.5
            self._image_rect = QRectF(float(x), float(y), float(scaled.width()), float(scaled.height()))
            painter.drawPixmap(int(x), int(y), scaled)
        else:
            self._image_rect = QRectF(0.0, 0.0, float(self.width()), float(self.height()))

        center = self._image_rect.center()
        radius = max(self._image_rect.width(), self._image_rect.height()) * 0.6
        radians = math.radians(self._angle_deg)
        direction = QPointF(math.cos(radians), -math.sin(radians))
        p1 = QPointF(center.x() - (direction.x() * radius), center.y() - (direction.y() * radius))
        p2 = QPointF(center.x() + (direction.x() * radius), center.y() + (direction.y() * radius))

        antialias_hint = QPainter.RenderHint.Antialiasing if hasattr(QPainter, "RenderHint") else QPainter.Antialiasing
        painter.setRenderHint(antialias_hint, True)
        painter.setPen(QPen(QColor(196, 74, 28), 3))
        painter.drawLine(p1, p2)
        painter.setPen(QPen(QColor(42, 42, 42), 1))
        painter.setBrush(QColor(255, 255, 255))
        painter.drawEllipse(center, 4.0, 4.0)
        painter.end()


class _AnglePickerDialog(QDialog):
    def __init__(
        self,
        *,
        source_path: str,
        preview_paths: dict[str, Path],
        parting_view: str,
        parting_angle_deg: float,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Pick Parting Angle")
        self.selected_view = parting_view.upper()
        self.selected_angle_deg = float(parting_angle_deg)
        self._preview_paths = {}
        for name, path in preview_paths.items():
            if name in PARTING_VIEWS and path.is_file() and not QPixmap(str(path)).isNull():
                self._preview_paths[name] = path
        if not self._preview_paths:
            raise ValueError("No readable angle preview images were returned.")
        if self.selected_view not in self._preview_paths:
            self.selected_view = next(name for name in PARTING_VIEWS if name in self._preview_paths)
        self._view_buttons: dict[str, QPushButton] = {}

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Source STL: {source_path}"))

        button_grid = QGridLayout()
        layout.addLayout(button_grid)

        self.editor = _AngleLineEditor(self.selected_angle_deg)
        self.editor.on_angle_changed = self._on_editor_angle_changed
        layout.addWidget(self.editor)

        angle_row = QHBoxLayout()
        angle_row.addWidget(QLabel("Line Angle"))
        self.angle_spin = QDoubleSpinBox()
        self.angle_spin.setDecimals(1)
        self.angle_spin.setRange(0.0, 179.9)
        self.angle_spin.setSingleStep(1.0)
        self.angle_spin.setValue(self.selected_angle_deg)
        self.angle_spin.valueChanged.connect(self._on_spin_angle_changed)
        angle_row.addWidget(self.angle_spin)
        angle_row.addWidget(QLabel("deg"))
        layout.addLayout(angle_row)

        for index, view_name in enumerate(PARTING_VIEWS):
            button = QPushButton(view_name.title())
            button.setCheckable(True)
            preview_path = self._preview_paths.get(view_name)
            button.setEnabled(preview_path is not None)
            if preview_path and preview_path.exists():
                pixmap = QPixmap(str(preview_path))
                if not pixmap.isNull():
                    button.setIcon(QIcon(pixmap))
                    button.setIconSize(QSize(120, 120))
            button.clicked.connect(lambda _checked=False, value=view_name: self._set_active_view(value))
            self._view_buttons[view_name] = button
            button_grid.addWidget(button, index // 3, index % 3)

        self._set_active_view(self.selected_view)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept_selection)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _set_active_view(self, view_name: str) -> None:
        if view_name not in self._preview_paths:
            return
        self.selected_view = view_name
        for candidate, button in self._view_buttons.items():
            button.setChecked(candidate == view_name)
        preview_path = self._preview_paths.get(view_name)
        self.editor.setPixmap(QPixmap(str(preview_path)) if preview_path else QPixmap())

    def _on_editor_angle_changed(self, angle_deg: float) -> None:
        self.selected_angle_deg = angle_deg
        if abs(self.angle_spin.value() - angle_deg) >= 0.05:
            self.angle_spin.blockSignals(True)
            self.angle_spin.setValue(angle_deg)
            self.angle_spin.blockSignals(False)

    def _on_spin_angle_changed(self, value: float) -> None:
        self.selected_angle_deg = float(value)
        self.editor.setAngle(self.selected_angle_deg, emit=False)

    def _accept_selection(self) -> None:
        self.selected_angle_deg = float(self.angle_spin.value())
        self.accept()


class _GenerateDialog(QDialog):
    def __init__(
        self,
        *,
        source_path: str,
        parting_view: str,
        parting_angle_deg: float,
        exterior_mode: str,
        alignment_keys: bool,
        center_insert: bool,
        center_insert_source: str,
        center_insert_span: float,
        clearance: float,
        wall: float,
        bottom: float,
        top: float,
        preview_renderer,
        cancel_operation,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Generate Mold Halves")
        self._preview_renderer = preview_renderer
        self._cancel_operation = cancel_operation
        self._preview_busy = False
        self._closed = False

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        layout.addWidget(QLabel("Uses the original STL file. Cura movement, rotation, scale and mesh edits are NOT applied."))
        self.source_edit = QLineEdit(source_path)
        source_row = QHBoxLayout()
        source_row.addWidget(self.source_edit)
        self.source_button = source_button = QPushButton("Browse")
        source_button.clicked.connect(self._browse_source)
        source_row.addWidget(source_button)
        source_widget = QWidget()
        source_widget.setLayout(source_row)
        form.addRow("Source STL", source_widget)

        self.parting_view_combo = QComboBox()
        self.parting_view_combo.addItems(list(PARTING_VIEWS))
        self.parting_view_combo.setCurrentText(parting_view.upper())
        form.addRow("Parting View", self.parting_view_combo)

        angle_row = QHBoxLayout()
        self.parting_angle_spin = self._float_spin(parting_angle_deg, minimum=0.0, maximum=179.9)
        self.parting_angle_spin.setSingleStep(1.0)
        angle_row.addWidget(self.parting_angle_spin)
        self.pick_button = pick_button = QPushButton("Pick From Images")
        pick_button.clicked.connect(self._pick_parting_angle)
        angle_row.addWidget(pick_button)
        angle_widget = QWidget()
        angle_widget.setLayout(angle_row)
        form.addRow("Parting Angle (deg)", angle_widget)

        self.exterior_combo = QComboBox()
        self.exterior_combo.addItems(["FOOTPRINT_HULL", "BOX"])
        self.exterior_combo.setCurrentText(exterior_mode.upper())
        form.addRow("Exterior Mode", self.exterior_combo)

        self.alignment_keys_box = QCheckBox()
        self.alignment_keys_box.setChecked(bool(alignment_keys))
        form.addRow("Alignment Keys", self.alignment_keys_box)

        self.center_insert_box = QCheckBox()
        self.center_insert_box.setChecked(bool(center_insert))
        self.center_insert_box.setToolTip("Third-piece mode: the center insert carries the split-face shell and both outer halves relieve around it.")
        form.addRow("Center Insert", self.center_insert_box)

        self.center_insert_source_combo = QComboBox()
        self.center_insert_source_combo.addItems(["CAVITY", "PART"])
        self.center_insert_source_combo.setCurrentText(str(center_insert_source).strip().upper() or DEFAULT_CENTER_INSERT_SOURCE)
        self.center_insert_source_combo.setToolTip("Choose whether the center insert is derived from the cavity shell section or the part profile section.")
        form.addRow("Insert Source", self.center_insert_source_combo)

        self.center_insert_span_spin = self._float_spin(center_insert_span, minimum=1.0, maximum=50.0)
        self.center_insert_span_spin.setSingleStep(0.5)
        self.center_insert_span_spin.setToolTip("Total split-axis span of the center insert before socket clearance is applied.")
        form.addRow("Insert Span (mm)", self.center_insert_span_spin)

        self.clearance_spin = self._float_spin(min(clearance, SAFE_CLEARANCE_MAX), minimum=0.0, maximum=SAFE_CLEARANCE_MAX)
        self.clearance_spin.setSingleStep(0.05)
        self.clearance_spin.setToolTip("Conservative UI range: 0 to 0.5 mm. Fit and geometry still require inspection.")
        self.wall_spin = self._float_spin(wall, minimum=0.0)
        self.bottom_spin = self._float_spin(bottom, minimum=0.0)
        self.top_spin = self._float_spin(top, minimum=0.0)
        form.addRow("Clearance (mm)", self.clearance_spin)
        form.addRow("Wall (mm)", self.wall_spin)
        form.addRow("Bottom (mm)", self.bottom_spin)
        form.addRow("Top (mm)", self.top_spin)

        self.buttons = buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.preview_status = QLabel("")
        layout.addWidget(self.preview_status)
        self.cancel_preview_button = QPushButton("Cancel Preview")
        self.cancel_preview_button.clicked.connect(self._cancel_operation)
        self.cancel_preview_button.setEnabled(False)
        layout.addWidget(self.cancel_preview_button)

    def _float_spin(self, value: float, *, minimum: float, maximum: float = 1000.0) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setMinimum(minimum)
        spin.setMaximum(maximum)
        spin.setDecimals(2)
        spin.setValue(float(value))
        return spin

    def _browse_source(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select Source STL", self.source_edit.text().strip() or str(Path.home()), "STL Files (*.stl)")
        if path:
            self.source_edit.setText(path)

    def _set_preview_busy(self, busy: bool) -> None:
        self._preview_busy = busy
        for control in (self.pick_button, self.source_edit, self.source_button,
                        self.parting_view_combo, self.parting_angle_spin,
                        self.buttons.button(QDialogButtonBox.StandardButton.Ok)):
            control.setEnabled(not busy)
        self.cancel_preview_button.setEnabled(busy)
        self.preview_status.setText("Rendering previews in the background..." if busy else "")

    def _pick_parting_angle(self) -> None:
        if self._preview_busy:
            return
        source_path = Path(self.source_edit.text().strip()).expanduser()
        if not source_path.is_file() or source_path.suffix.lower() != ".stl":
            self.preview_status.setText("Pick an existing original STL file first.")
            return
        self._set_preview_busy(True)
        try:
            self._preview_renderer(source_path.resolve(), self._preview_finished)
        except Exception as exc:
            self._set_preview_busy(False)
            self.preview_status.setText(str(exc)[:600])

    def _preview_finished(self, result: OperationResult) -> None:
        self._set_preview_busy(False)
        if self._closed:
            return
        if result.status != "success":
            self.preview_status.setText(result.error or result.status)
            return
        try:
            dialog = _AnglePickerDialog(
                source_path=self.source_edit.text(), preview_paths=result.artifacts,
                parting_view=self.parting_view_combo.currentText(),
                parting_angle_deg=float(self.parting_angle_spin.value()), parent=self,
            )
            if dialog.exec() == int(QDialog.DialogCode.Accepted):
                self.parting_view_combo.setCurrentText(dialog.selected_view)
                self.parting_angle_spin.setValue(dialog.selected_angle_deg)
        except Exception as exc:
            self.preview_status.setText(str(exc)[:600])

    def accept(self) -> None:
        if not self._preview_busy:
            self._closed = True
            super().accept()

    def reject(self) -> None:
        self._closed = True
        if self._preview_busy:
            self._cancel_operation()
        super().reject()

    def closeEvent(self, event) -> None:
        self.reject()
        event.accept()


class E3PBlenderMold(Extension):
    def __init__(self) -> None:
        super().__init__()
        self.setMenuName(catalog.i18nc("@item:inmenu", "E3P"))
        self.addMenuItem(catalog.i18nc("@item:inmenu", "Generate Mold Halves"), self.generateMoldHalves)
        self.addMenuItem(catalog.i18nc("@item:inmenu", "Blender Mold Settings"), self.showSettings)
        self.addMenuItem(catalog.i18nc("@item:inmenu", "Cancel Active Operation"), self.cancelActiveOperation)

        self._preferences = Application.getInstance().getPreferences()
        self._defaults = self._load_defaults()
        self._progress_message: Optional[Message] = None
        self._active_job: Optional[BlenderMoldJob] = None
        self._dialog_open = False
        self._initialize_preferences()
        self._migrate_legacy_preferences()

    def _load_defaults(self) -> dict[str, Any]:
        defaults_path = Path(__file__).resolve().with_name(DEFAULTS_FILE)
        if not defaults_path.exists():
            return {}
        try:
            return json.loads(defaults_path.read_text(encoding="utf-8"))
        except Exception:
            Logger.logException("w", "Failed to load E3P Blender Mold defaults.")
            return {}

    def _initialize_preferences(self) -> None:
        self._preferences.addPreference(f"{PREF_PREFIX}/project_root", self._defaults.get("project_root", ""))
        self._preferences.addPreference(f"{PREF_PREFIX}/blender_exe", self._defaults.get("blender_exe", ""))
        self._preferences.addPreference(f"{PREF_PREFIX}/python_exe", self._defaults.get("python_exe", ""))
        self._preferences.addPreference(f"{PREF_PREFIX}/split_axis", DEFAULT_SPLIT_AXIS)
        self._preferences.addPreference(f"{PREF_PREFIX}/parting_view", DEFAULT_PARTING_VIEW)
        self._preferences.addPreference(f"{PREF_PREFIX}/parting_angle_deg", DEFAULT_PARTING_ANGLE_DEG)
        self._preferences.addPreference(f"{PREF_PREFIX}/exterior_mode", DEFAULT_EXTERIOR_MODE)
        self._preferences.addPreference(f"{PREF_PREFIX}/alignment_keys", DEFAULT_ALIGNMENT_KEYS)
        self._preferences.addPreference(f"{PREF_PREFIX}/center_insert", DEFAULT_CENTER_INSERT)
        self._preferences.addPreference(f"{PREF_PREFIX}/center_insert_source", DEFAULT_CENTER_INSERT_SOURCE)
        self._preferences.addPreference(f"{PREF_PREFIX}/center_insert_span", DEFAULT_CENTER_INSERT_SPAN)
        self._preferences.addPreference(f"{PREF_PREFIX}/clearance", DEFAULT_CLEARANCE)
        self._preferences.addPreference(f"{PREF_PREFIX}/wall", DEFAULT_WALL)
        self._preferences.addPreference(f"{PREF_PREFIX}/bottom", DEFAULT_BOTTOM)
        self._preferences.addPreference(f"{PREF_PREFIX}/top", DEFAULT_TOP)

    def _migrate_legacy_preferences(self) -> None:
        exterior_mode = (self._pref_text("exterior_mode") or DEFAULT_EXTERIOR_MODE).upper()
        clearance = self._pref_float("clearance", DEFAULT_CLEARANCE)
        legacy_combo = exterior_mode == "BOX" and clearance >= 0.95

        if legacy_combo:
            Logger.log("i", "E3P Blender Mold: migrating legacy BOX/1.0 defaults to FOOTPRINT_HULL/0.4.")
            self._preferences.setValue(f"{PREF_PREFIX}/exterior_mode", DEFAULT_EXTERIOR_MODE)
            self._preferences.setValue(f"{PREF_PREFIX}/clearance", DEFAULT_CLEARANCE)
            self._preferences.setValue(f"{PREF_PREFIX}/alignment_keys", DEFAULT_ALIGNMENT_KEYS)
            return

        if clearance > SAFE_CLEARANCE_MAX:
            Logger.log("w", "E3P Blender Mold: clamping unsafe saved clearance %.3f mm down to %.3f mm.", clearance, DEFAULT_CLEARANCE)
            self._preferences.setValue(f"{PREF_PREFIX}/clearance", DEFAULT_CLEARANCE)

    def _pref_text(self, key: str) -> str:
        return str(self._preferences.getValue(f"{PREF_PREFIX}/{key}") or "").strip()

    def _recommended_center_insert_source(self, raw_value: str) -> str:
        source = str(raw_value or "").strip().upper()
        if source not in {"CAVITY", "PART"}:
            return DEFAULT_CENTER_INSERT_SOURCE
        return source

    def _pref_bool(self, key: str, default: bool = False) -> bool:
        value = self._preferences.getValue(f"{PREF_PREFIX}/{key}")
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        return default

    def _pref_float(self, key: str, default: float) -> float:
        value = self._preferences.getValue(f"{PREF_PREFIX}/{key}")
        try:
            return float(value)
        except Exception:
            return float(default)

    def _normalized_parting_view(self, raw_value: str) -> str:
        view_name = str(raw_value or "").strip().upper()
        return view_name if view_name in PARTING_VIEWS else DEFAULT_PARTING_VIEW

    def _normalized_parting_angle(self, raw_value: float) -> float:
        try:
            angle_deg = float(raw_value)
        except Exception:
            angle_deg = DEFAULT_PARTING_ANGLE_DEG
        return min(max(angle_deg, 0.0), 179.9)

    def _split_axis_for_view(self, parting_view: str) -> str:
        return "Y" if self._normalized_parting_view(parting_view) in {"LEFT", "RIGHT"} else "X"

    def _resolve_project_root(self) -> Path:
        configured = self._pref_text("project_root")
        root = Path(configured).expanduser()
        if not configured or not (root / "run_blender_mold.py").is_file():
            raise RuntimeError("Set a valid E3P project root in Blender Mold Settings first.")
        return root.resolve()

    def _resolve_python(self, project_root: Path) -> Path:
        configured = self._pref_text("python_exe")
        candidates = [Path(configured).expanduser()] if configured else [
            project_root / ".venv" / "Scripts" / "python.exe",
            project_root / ".venv" / "bin" / "python",
            project_root / ".venv-blender" / "Scripts" / "python.exe",
            project_root / ".venv-blender" / "bin" / "python",
        ]
        for candidate in candidates:
            if candidate.is_file():
                # Keep a venv's executable symlink: resolving it selects the
                # system interpreter and loses the venv's installed dependencies.
                return candidate.absolute()
        raise RuntimeError("Set an external Python executable with the project dependencies installed, or create the project .venv. Cura's Python is not used.")

    def _resolve_blender(self) -> Path:
        configured = self._pref_text("blender_exe")
        blender_exe = Path(configured).expanduser()
        if not configured or not blender_exe.is_file():
            raise RuntimeError("Set a valid Blender executable in Blender Mold Settings first.")
        return blender_exe.resolve()

    def _new_run(self) -> Path:
        output_root = self._resolve_project_root() / "e3p_studio_runs" / "cura_blender_extension"
        output_root.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="run-", dir=str(output_root)))

    def _launcher_prefix(self, project_root: Path) -> list[str]:
        return [str(self._resolve_python(project_root)), "-I",
                str(Path(__file__).resolve().with_name("external_python.py")),
                str(project_root / "run_blender_mold.py")]

    def _render_angle_previews(self, source_path: Path, callback) -> None:
        if self._active_job is not None:
            raise RuntimeError("An E3P operation is already running. Wait or cancel it first.")
        project_root = self._resolve_project_root()
        blender_exe = self._resolve_blender()
        run_dir = self._new_run()
        output_dir, report_path = run_dir / "output", run_dir / "report.json"
        command = self._launcher_prefix(project_root) + ["preview", str(source_path), "-o", str(output_dir),
                   "--blender-exe", str(blender_exe), "--timeout", str(BACKEND_TIMEOUT),
                   "--report", str(report_path)]
        self._start_job(BackendOperation(command, cwd=project_root, output_dir=output_dir,
                                        report_path=report_path, operation="preview",
                                        timeout=BACKEND_TIMEOUT + 10), callback)

    def _start_job(self, operation: BackendOperation, callback=None) -> None:
        if self._active_job is not None:
            raise RuntimeError("An E3P operation is already running. Wait or cancel it first.")
        job = BlenderMoldJob(operation, callback)
        self._active_job = job
        job.finished.connect(self._onJobFinished)
        self._progress_message = Message(title="E3P", text="Running %s. Use E3P > Cancel Active Operation to stop." % operation.operation,
                                         progress=-1, lifetime=0)
        self._progress_message.show()
        try:
            job.start()
        except Exception:
            self._active_job = None
            self._progress_message.hide()
            self._progress_message = None
            raise

    def cancelActiveOperation(self) -> None:
        if self._active_job is not None:
            self._active_job.request_cancel()

    def _guess_source_from_selection(self) -> str:
        selected = list(Selection.getAllSelectedObjects())
        if len(selected) == 1:
            mesh = selected[0].getMeshData()
            file_name = mesh.getFileName() if mesh else None
            if file_name and str(file_name).lower().endswith(".stl") and Path(file_name).exists():
                return str(Path(file_name))

        open_files: set[str] = set()
        for node in DepthFirstIterator(Application.getInstance().getController().getScene().getRoot()):
            if not isinstance(node, CuraSceneNode):
                continue
            mesh = node.getMeshData()
            file_name = mesh.getFileName() if mesh else None
            if file_name and str(file_name).lower().endswith(".stl") and Path(file_name).exists():
                open_files.add(str(Path(file_name)))
        if len(open_files) == 1:
            return next(iter(open_files))
        return ""

    def showSettings(self) -> None:
        dialog = _SettingsDialog(
            project_root=self._pref_text("project_root"),
            blender_exe=self._pref_text("blender_exe"),
            python_exe=self._pref_text("python_exe"),
        )
        if dialog.exec() != int(QDialog.DialogCode.Accepted):
            return
        self._preferences.setValue(f"{PREF_PREFIX}/project_root", dialog.project_root_edit.text().strip())
        self._preferences.setValue(f"{PREF_PREFIX}/blender_exe", dialog.blender_exe_edit.text().strip())
        self._preferences.setValue(f"{PREF_PREFIX}/python_exe", dialog.python_exe_edit.text().strip())
        Message(
            title=catalog.i18nc("@info:title", "E3P"),
            text=catalog.i18nc("@info", "Blender mold settings saved."),
            lifetime=5,
        ).show()

    def generateMoldHalves(self) -> None:
        if self._active_job is not None or self._dialog_open:
            Message(title="E3P", text="An E3P operation or dialog is already active. Wait or cancel it first.", lifetime=6).show()
            return
        dialog = _GenerateDialog(
            source_path=self._guess_source_from_selection(),
            parting_view=self._normalized_parting_view(self._pref_text("parting_view") or DEFAULT_PARTING_VIEW),
            parting_angle_deg=self._normalized_parting_angle(self._pref_float("parting_angle_deg", DEFAULT_PARTING_ANGLE_DEG)),
            exterior_mode=self._pref_text("exterior_mode") or DEFAULT_EXTERIOR_MODE,
            alignment_keys=self._pref_bool("alignment_keys", DEFAULT_ALIGNMENT_KEYS),
            center_insert=self._pref_bool("center_insert", DEFAULT_CENTER_INSERT),
            center_insert_source=self._recommended_center_insert_source(self._pref_text("center_insert_source")),
            center_insert_span=self._pref_float("center_insert_span", DEFAULT_CENTER_INSERT_SPAN),
            clearance=self._pref_float("clearance", DEFAULT_CLEARANCE),
            wall=self._pref_float("wall", DEFAULT_WALL),
            bottom=self._pref_float("bottom", DEFAULT_BOTTOM),
            top=self._pref_float("top", DEFAULT_TOP),
            preview_renderer=self._render_angle_previews,
            cancel_operation=self.cancelActiveOperation,
        )
        self._dialog_open = True
        try:
            accepted = dialog.exec() == int(QDialog.DialogCode.Accepted)
        finally:
            self._dialog_open = False
        if not accepted:
            return

        source_path = Path(dialog.source_edit.text().strip()).expanduser()
        if not source_path.is_file() or source_path.suffix.lower() != ".stl":
            Message(
                title=catalog.i18nc("@info:title", "E3P"),
                text=catalog.i18nc("@info", "Pick a valid STL source file first."),
                lifetime=6,
                message_type=Message.MessageType.ERROR,
            ).show()
            return

        source_path = source_path.resolve()
        if dialog.clearance_spin.value() > SAFE_CLEARANCE_MAX:
            Message(
                title=catalog.i18nc("@info:title", "E3P"),
                text=catalog.i18nc(
                    "@info",
                    "This UI limits clearance to {0:.1f} mm. Inspect generated geometry and fit before use.",
                ).format(SAFE_CLEARANCE_MAX),
                lifetime=8,
                message_type=Message.MessageType.ERROR,
            ).show()
            return

        selected_view = self._normalized_parting_view(dialog.parting_view_combo.currentText())
        selected_angle_deg = self._normalized_parting_angle(dialog.parting_angle_spin.value())
        requested_alignment_keys = dialog.alignment_keys_box.isChecked()
        effective_alignment_keys = requested_alignment_keys and abs(selected_angle_deg - DEFAULT_PARTING_ANGLE_DEG) <= 0.05

        if requested_alignment_keys and not effective_alignment_keys:
            Message(
                title=catalog.i18nc("@info:title", "E3P"),
                text=catalog.i18nc(
                    "@info",
                    "Alignment keys are currently limited to the 90 deg parting angle path. "
                    "This run will continue without keys.",
                ),
                lifetime=8,
            ).show()

        self._preferences.setValue(f"{PREF_PREFIX}/split_axis", self._split_axis_for_view(selected_view))
        self._preferences.setValue(f"{PREF_PREFIX}/parting_view", selected_view)
        self._preferences.setValue(f"{PREF_PREFIX}/parting_angle_deg", selected_angle_deg)
        self._preferences.setValue(f"{PREF_PREFIX}/exterior_mode", dialog.exterior_combo.currentText().strip().upper())
        self._preferences.setValue(f"{PREF_PREFIX}/alignment_keys", requested_alignment_keys)
        self._preferences.setValue(f"{PREF_PREFIX}/center_insert", dialog.center_insert_box.isChecked())
        self._preferences.setValue(f"{PREF_PREFIX}/center_insert_source", dialog.center_insert_source_combo.currentText().strip().upper())
        self._preferences.setValue(f"{PREF_PREFIX}/center_insert_span", dialog.center_insert_span_spin.value())
        self._preferences.setValue(f"{PREF_PREFIX}/clearance", dialog.clearance_spin.value())
        self._preferences.setValue(f"{PREF_PREFIX}/wall", dialog.wall_spin.value())
        self._preferences.setValue(f"{PREF_PREFIX}/bottom", dialog.bottom_spin.value())
        self._preferences.setValue(f"{PREF_PREFIX}/top", dialog.top_spin.value())

        try:
            request = self._build_request(
                source_path=source_path,
                axis=self._split_axis_for_view(selected_view),
                parting_view=selected_view,
                parting_angle_deg=selected_angle_deg,
                exterior_mode=dialog.exterior_combo.currentText().strip().upper(),
                alignment_keys=effective_alignment_keys,
                center_insert=dialog.center_insert_box.isChecked(),
                center_insert_source=dialog.center_insert_source_combo.currentText().strip().upper(),
                center_insert_span=dialog.center_insert_span_spin.value(),
                clearance=dialog.clearance_spin.value(),
                wall=dialog.wall_spin.value(),
                bottom=dialog.bottom_spin.value(),
                top=dialog.top_spin.value(),
            )
            command = self._build_command(request)
        except Exception as exc:
            Message(
                title=catalog.i18nc("@info:title", "E3P"),
                text=str(exc),
                lifetime=10,
                message_type=Message.MessageType.ERROR,
            ).show()
            return

        try:
            self._start_job(BackendOperation(command, cwd=self._resolve_project_root(),
                                            output_dir=request.output_dir, report_path=request.report_path,
                                            operation="generate", timeout=BACKEND_TIMEOUT + 10))
        except Exception as exc:
            Message(title="E3P", text=str(exc), lifetime=10, message_type=Message.MessageType.ERROR).show()

    def _build_request(
        self,
        *,
        source_path: Path,
        axis: str,
        parting_view: str,
        parting_angle_deg: float,
        exterior_mode: str,
        alignment_keys: bool,
        center_insert: bool,
        center_insert_source: str,
        center_insert_span: float,
        clearance: float,
        wall: float,
        bottom: float,
        top: float,
    ) -> MoldJobRequest:
        run_dir = self._new_run()
        output_dir = run_dir / "output"
        return MoldJobRequest(
            source_path=source_path,
            output_dir=output_dir,
            report_path=run_dir / "report.json",
            axis=axis,
            parting_view=parting_view,
            parting_angle_deg=float(parting_angle_deg),
            exterior_mode=exterior_mode,
            alignment_keys=alignment_keys,
            center_insert=bool(center_insert),
            center_insert_source=str(center_insert_source).strip().upper(),
            center_insert_span=float(center_insert_span),
            clearance=float(clearance),
            wall=float(wall),
            bottom=float(bottom),
            top=float(top),
            export_negative_name=f"{source_path.stem}_mold_negative.stl",
            export_positive_name=f"{source_path.stem}_mold_positive.stl",
            export_center_name=f"{source_path.stem}_mold_center.stl",
        )

    def _build_command(self, request: MoldJobRequest) -> list[str]:
        project_root = self._resolve_project_root()
        blender_exe = self._resolve_blender()
        command = self._launcher_prefix(project_root) + [
            "generate",
            "--timeout",
            str(BACKEND_TIMEOUT),
            "--report",
            str(request.report_path),
            "--blender-exe",
            str(blender_exe),
            str(request.source_path),
            "-o",
            str(request.output_dir),
            "--split-axis",
            request.axis,
            "--parting-view",
            request.parting_view,
            "--parting-angle-deg",
            str(request.parting_angle_deg),
            "--exterior-mode",
            request.exterior_mode,
            "--clearance",
            str(request.clearance),
            "--wall",
            str(request.wall),
            "--bottom",
            str(request.bottom),
            "--top",
            str(request.top),
            "--export-negative",
            request.export_negative_name,
            "--export-positive",
            request.export_positive_name,
        ]
        command.append("--alignment-keys" if request.alignment_keys else "--no-alignment-keys")
        command.append("--center-insert" if request.center_insert else "--no-center-insert")
        if request.center_insert:
            command.extend(
                [
                    "--center-insert-source",
                    request.center_insert_source,
                    "--center-insert-total-span",
                    str(request.center_insert_span),
                    "--export-center",
                    request.export_center_name,
                ]
            )
        return command

    def _onJobFinished(self, job: BlenderMoldJob) -> None:
        # Ignore old/duplicate notifications; they must not clear another job's UI.
        if job is not self._active_job:
            return
        self._active_job = None
        if self._progress_message is not None:
            self._progress_message.hide()
            self._progress_message = None
        result = job.result
        # A late cancel after worker completion still prevents queued imports.
        if job.operation.cancel_event.is_set():
            result = OperationResult("cancelled", "Operation cancelled; no imports queued.")
        if job.callback is not None:
            job.callback(result)
            return
        if result.status != "success":
            Message(title="E3P", text=result.error or result.status, lifetime=0,
                    message_type=Message.MessageType.ERROR if result.status == "failed" else Message.MessageType.NEUTRAL).show()
            return
        try:
            for name in ("negative", "positive", "center"):
                if name in result.artifacts:
                    CuraApplication.getInstance().readLocalFile(QUrl.fromLocalFile(str(result.artifacts[name])), add_to_recent_files=False)
        except Exception as exc:
            Message(title="E3P", text="Could not queue all imports: %s. Verified files remain in %s." % (exc, job.operation.output_dir),
                    lifetime=0, message_type=Message.MessageType.ERROR).show()
            return
        text = "Queued %d verified mold files for Cura to import. Check Cura for import completion." % len(result.artifacts)
        if result.center_insert == "skipped":
            text += " The center insert was skipped."
        elif result.center_insert == "cavity_fallback":
            text += " The center insert uses the cavity fallback."
        if result.warnings:
            text += "\n" + "\n".join(result.warnings)[:1200]
        Message(title="E3P", text=text, lifetime=10).show()
