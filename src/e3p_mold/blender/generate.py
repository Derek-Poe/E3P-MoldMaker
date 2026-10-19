from __future__ import annotations

try:
    import bmesh
    import bpy
    from mathutils import Vector
except ModuleNotFoundError as exc:
    raise SystemExit(
        "generate.py requires Blender's Python API. "
        "Run it from Blender or from a Python environment with bpy available."
    ) from exc

import argparse
import json
import os
import sys
from dataclasses import dataclass
from math import ceil, isfinite, pi, sin
from pathlib import Path
from typing import Iterable, Sequence

# ========================================
# USER SETTINGS
# ========================================

CLEARANCE = 0.40          # cavity clearance around the part
WALL = 6.0                # wall thickness around the cavity footprint
BOTTOM = 6.0              # flat base thickness
TOP = 6.0                 # flat top thickness
SPLIT_AXIS = "X"          # supported: X or Y
SPLIT_OFFSET = 0.0        # 0 = split at footprint center
PARTING_VIEW = "TOP"      # orthographic view used for visual angle selection
PARTING_ANGLE_DEG = 90.0  # line angle in the selected view, degrees from image +X

# Exterior style: "BOX" or "FOOTPRINT_HULL"
EXTERIOR_MODE = "FOOTPRINT_HULL"

# Alignment keys: male pins on the positive split half, female sockets on the negative half
ADD_ALIGNMENT_KEYS = True
KEY_RADIUS = 3.0
KEY_CLEARANCE = 0.18
KEY_ENGAGEMENT = 8.0      # protrusion into the mating half
KEY_ROOT = 5.0            # anchor depth inside the male half
KEY_LATERAL_MARGIN = 5.0  # extra spacing beyond cavity bounds within the split plane
KEY_Z_MARGIN = 5.0        # extra spacing beyond cavity bounds near the top/bottom of the mold

# A continuous perimeter rib and matching socket around the split-face cavity
# opening. This acts like a cookie-cutter-style lock that helps alignment and
# contains injected material near the parting line.
ADD_PERIMETER_LOCK = True
PERIMETER_LOCK_WIDTH = 1.6
PERIMETER_LOCK_ENGAGEMENT = 1.8
PERIMETER_LOCK_ROOT = 1.2
PERIMETER_LOCK_CLEARANCE = 0.18

# Optional center insert: a third mold piece that carries the cavity-derived
# shell/key geometry on a small attached base. Both outer halves are relieved
# around this insert so it can nest between them.
ADD_CENTER_INSERT = False
CENTER_INSERT_SOURCE = "CAVITY"
CENTER_INSERT_TOTAL_SPAN = 8.0
CENTER_INSERT_CLEARANCE = 0.20
CENTER_INSERT_BASE_THICKNESS = 2.4

# Parting-plane channels. We now bring them in from the mold's flat cap face
# instead of the cylindrical wall so they are easier to fill and vent in use.
ADD_INJECTION_SITE = True
INJECTION_INNER_RADIUS = 1.8
INJECTION_OUTER_RADIUS = 3.1
ADD_VENT = True
VENT_RADIUS = 0.6
CHANNEL_BREAKTHROUGH = 1.2

# Export each half in a support-aware print posture. The old cavity-up
# orientation is kept as a preference when scores tie.
AUTO_ROTATE_CAVITY_UP = True

# Backwards-compatible alias for the earlier X-only draft.
KEY_Y_MARGIN = KEY_LATERAL_MARGIN

EXPORT_LEFT = "mold_left.stl"
EXPORT_RIGHT = "mold_right.stl"
EXPORT_CENTER = "mold_center.stl"
RENDER_PREVIEW = True
PREVIEW_IMAGE = "mold_preview.png"
PREVIEW_WIDTH = 1600
PREVIEW_HEIGHT = 1200
PREVIEW_SPLIT_GAP = 12.0
RENDER_ANGLE_PREVIEWS = False
ANGLE_PREVIEW_ONLY = False
ANGLE_PREVIEW_PREFIX = "parting_view"
ANGLE_PREVIEW_SIZE = 720
CLEAR_SCENE = True

# NOTE:
# This script preserves all cavities that appear in the STL. If a cavity is
# topologically enclosed and not actually moldable from the chosen split plane,
# this script cannot infer whether you intended a core insert or wanted the
# cavity ignored.

VALID_SPLIT_AXES = ("X", "Y")
VALID_EXTERIOR_MODES = ("BOX", "FOOTPRINT_HULL")
VALID_PARTING_VIEWS = ("TOP", "BOTTOM", "FRONT", "BACK", "LEFT", "RIGHT")
VALID_CENTER_INSERT_SOURCES = ("CAVITY", "PART")
DEFAULT_KEY_SAFE_PARTING_ANGLE_DEG = 90.0


# ========================================
# HELPERS
# ========================================
@dataclass(slots=True)
class Bounds3D:
    xmin: float
    xmax: float
    ymin: float
    ymax: float
    zmin: float
    zmax: float

    @property
    def xmid(self) -> float:
        return (self.xmin + self.xmax) * 0.5

    @property
    def ymid(self) -> float:
        return (self.ymin + self.ymax) * 0.5

    @property
    def zmid(self) -> float:
        return (self.zmin + self.zmax) * 0.5

    def axis_min(self, axis: str) -> float:
        return getattr(self, f"{axis.lower()}min")

    def axis_max(self, axis: str) -> float:
        return getattr(self, f"{axis.lower()}max")

    def axis_mid(self, axis: str) -> float:
        return (self.axis_min(axis) + self.axis_max(axis)) * 0.5


@dataclass(slots=True)
class MoldConfig:
    input_stl: Path
    output_dir: Path
    clearance: float
    wall: float
    bottom: float
    top: float
    split_axis: str
    split_offset: float
    parting_view: str | None
    parting_angle_deg: float
    exterior_mode: str
    add_alignment_keys: bool
    key_radius: float
    key_clearance: float
    key_engagement: float
    key_root: float
    key_lateral_margin: float
    key_z_margin: float
    add_perimeter_lock: bool
    perimeter_lock_width: float
    perimeter_lock_engagement: float
    perimeter_lock_root: float
    perimeter_lock_clearance: float
    add_center_insert: bool
    center_insert_source: str
    center_insert_total_span: float
    center_insert_clearance: float
    center_insert_base_thickness: float
    add_injection_site: bool
    injection_inner_radius: float
    injection_outer_radius: float
    add_vent: bool
    vent_radius: float
    channel_breakthrough: float
    auto_rotate_cavity_up: bool
    export_negative_name: str
    export_positive_name: str
    export_center_name: str
    render_preview: bool
    preview_image_name: str
    preview_width: int
    preview_height: int
    preview_split_gap: float
    render_angle_previews: bool
    angle_preview_only: bool
    angle_preview_prefix: str
    angle_preview_size: int
    clear_scene: bool
    artifact_manifest: Path | None = None
    voxel_size: float | None = None
    max_input_bytes: int = 64 * 1024 * 1024
    max_output_bytes: int = 256 * 1024 * 1024
    allow_mesh_repair: bool = False


class CenterInsertError(RuntimeError):
    """The requested insert could not be constructed; caller may explicitly retry."""


_ACTIVE_CONFIG: MoldConfig | None = None
_GEOMETRY_EVENTS: list[dict] = []
_WARNINGS: list[str] = []
_EXPORTED_BYTES = 0
MAX_VOXEL_CELLS = 64_000_000


def record_repair(kind: str, obj, **details) -> None:
    event = {"kind": kind, "object": obj.name, **details}
    if _ACTIVE_CONFIG is None or not _ACTIVE_CONFIG.allow_mesh_repair:
        raise RuntimeError(
            f"{obj.name} requires {kind}; refusing to change mesh intent. "
            "Inspect the input/parameters or explicitly enable --allow-mesh-repair."
        )
    _GEOMETRY_EVENTS.append(event)


def mesh_metrics(obj) -> dict:
    """Check topology independently of Blender modifier success."""
    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        finite = all(isfinite(float(value)) for vertex in bm.verts for value in vertex.co)
        remaining = set(bm.faces)
        components = 0
        while remaining:
            components += 1
            stack = [remaining.pop()]
            while stack:
                face = stack.pop()
                for edge in face.edges:
                    for linked in edge.link_faces:
                        if linked in remaining:
                            remaining.remove(linked)
                            stack.append(linked)
        closed = bool(bm.faces) and all(edge.is_manifold for edge in bm.edges) and all(v.is_manifold for v in bm.verts)
        consistent = closed and all(edge.is_contiguous for edge in bm.edges)
        volume = float(bm.calc_volume(signed=True)) if finite and bm.faces else 0.0
        bounds = get_world_bounds(obj)
        return {"vertices": len(bm.verts), "faces": len(bm.faces), "components": components,
                "finite": finite, "closed": closed, "consistent_winding": consistent,
                "signed_volume": volume, "bounds": [[bounds.xmin, bounds.ymin, bounds.zmin],
                                                     [bounds.xmax, bounds.ymax, bounds.zmax]]}
    finally:
        bm.free()


def require_solid(obj) -> dict:
    metrics = mesh_metrics(obj)
    if not (metrics["finite"] and metrics["closed"] and metrics["consistent_winding"]
            and metrics["components"] == 1 and metrics["signed_volume"] > 0):
        raise RuntimeError(f"{obj.name} must be one finite, consistently oriented, closed positive-volume solid.")
    return metrics


def write_artifact_manifest(cfg: MoldConfig, payload: dict) -> None:
    if cfg.artifact_manifest is None:
        return
    target = cfg.artifact_manifest
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps({"schema_version": 1, **payload}, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, target)


def successful_manifest(cfg: MoldConfig, *, outputs=None, previews=None, validation=None, features=None) -> None:
    files = list((outputs or {}).values()) + list((previews or {}).values())
    if sum(Path(path).stat().st_size for path in files) > cfg.max_output_bytes:
        raise RuntimeError("Generated artifacts exceeded --max-output-bytes.")
    if cfg.angle_preview_only:
        features = {name: False for name in ("alignment_keys", "perimeter_lock", "center_insert", "injection_site", "vent")}
    write_artifact_manifest(cfg, {
        "status": "success", "operation": "preview" if cfg.angle_preview_only else "generate",
        "outputs": outputs or {}, "previews": previews or {},
        "features": {"alignment_keys": cfg.add_alignment_keys, "perimeter_lock": cfg.add_perimeter_lock,
                     "center_insert": bool(outputs and "center" in outputs),
                     "injection_site": cfg.add_injection_site, "vent": cfg.add_vent, **(features or {})},
        "warnings": _WARNINGS,
        "geometry": {"voxel_size": cfg.voxel_size, "events": _GEOMETRY_EVENTS,
                     "event_coordinate_frame": "construction_before_print_orientation",
                     "quality": "original heuristic" if cfg.voxel_size is None else "explicit approximation"},
        "validation": validation or {}, "blender_version": bpy.app.version_string,
    })


def parting_angle_supports_alignment_keys(cfg: MoldConfig, *, tolerance_deg: float = 0.05) -> bool:
    if not cfg.parting_view:
        return True
    return abs(float(cfg.parting_angle_deg) - DEFAULT_KEY_SAFE_PARTING_ANGLE_DEG) <= tolerance_deg


def clear_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)


def import_stl(path: str | Path):
    bpy.ops.object.select_all(action="DESELECT")
    bpy.ops.wm.stl_import(filepath=str(path))
    imported = list(bpy.context.selected_objects)
    if not imported:
        raise RuntimeError(f"Blender did not import any objects from {path}.")

    if len(imported) == 1:
        obj = imported[0]
        bpy.context.view_layer.objects.active = obj
        return obj

    bpy.context.view_layer.objects.active = imported[0]
    bpy.ops.object.join()
    obj = bpy.context.active_object
    obj.name = Path(path).stem
    return obj


def apply_all_transforms(obj) -> None:
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    obj.select_set(False)


def duplicate_object(obj, name: str):
    dup = obj.copy()
    dup.data = obj.data.copy()
    dup.name = name
    bpy.context.collection.objects.link(dup)
    return dup


def delete_object(obj) -> None:
    existing = bpy.data.objects.get(obj.name)
    if existing is not None:
        bpy.data.objects.remove(existing, do_unlink=True)


def get_world_bounds(obj) -> Bounds3D:
    corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    xs = [c.x for c in corners]
    ys = [c.y for c in corners]
    zs = [c.z for c in corners]
    return Bounds3D(min(xs), max(xs), min(ys), max(ys), min(zs), max(zs))


def get_world_vertices(obj) -> list[tuple[float, float, float]]:
    mesh = obj.data
    return [tuple(obj.matrix_world @ vert.co) for vert in mesh.vertices]


def boolean_op(target, other, operation: str, solver: str = "EXACT") -> None:
    name = f"Boolean_{operation.title()}"
    mod = target.modifiers.new(name=name, type="BOOLEAN")
    mod.operation = operation
    mod.object = other
    mod.solver = solver
    bpy.context.view_layer.objects.active = target
    bpy.ops.object.modifier_apply(modifier=mod.name)


def clean_mesh_for_boolean(obj, *, merge_distance: float = 0.0001) -> None:
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bm = bmesh.from_edit_mesh(obj.data)
    bmesh.ops.remove_doubles(bm, verts=bm.verts[:], dist=merge_distance)
    bmesh.ops.dissolve_degenerate(bm, dist=merge_distance, edges=bm.edges[:])
    degenerate_faces = [face for face in bm.faces if face.calc_area() <= (merge_distance * merge_distance)]
    if degenerate_faces:
        bmesh.ops.delete(bm, geom=degenerate_faces, context="FACES")
    loose_edges = [edge for edge in bm.edges if not edge.link_faces]
    if loose_edges:
        bmesh.ops.delete(bm, geom=loose_edges, context="EDGES")
    loose_verts = [vert for vert in bm.verts if not vert.link_edges]
    if loose_verts:
        bmesh.ops.delete(bm, geom=loose_verts, context="VERTS")
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    bmesh.update_edit_mesh(obj.data)
    bpy.ops.object.mode_set(mode="OBJECT")
    obj.select_set(False)


def recommended_voxel_size(bounds: Bounds3D, *, clearance: float) -> float:
    max_span = max(bounds.xmax - bounds.xmin, bounds.ymax - bounds.ymin, bounds.zmax - bounds.zmin)
    return min(max(max_span / 260.0, clearance / 3.0, 0.08), 0.16)


def voxel_remesh_object(obj, voxel_size: float) -> None:
    if _ACTIVE_CONFIG is not None and _ACTIVE_CONFIG.voxel_size is not None:
        voxel_size = _ACTIVE_CONFIG.voxel_size
    bounds = get_world_bounds(obj)
    cells = 1
    for axis in ("x", "y", "z"):
        span = getattr(bounds, axis + "max") - getattr(bounds, axis + "min")
        cells *= max(1, ceil(span / voxel_size) + 3)
    if cells > MAX_VOXEL_CELLS:
        raise RuntimeError("Estimated voxel grid exceeds 64 million cells; use a larger --voxel-size or smaller model.")
    _GEOMETRY_EVENTS.append({"kind": "voxel_remesh", "object": obj.name,
                             "voxel_size": voxel_size, "estimated_grid_cells": cells})
    mod = obj.modifiers.new(name="VoxelRemesh", type="REMESH")
    mod.mode = "VOXEL"
    mod.voxel_size = float(voxel_size)
    if hasattr(mod, "adaptivity"):
        mod.adaptivity = 0.0
    if hasattr(mod, "use_remove_disconnected"):
        mod.use_remove_disconnected = False
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.modifier_apply(modifier=mod.name)


def mesh_has_faces(obj) -> bool:
    return bpy.data.objects.get(obj.name) is not None and len(obj.data.polygons) > 0


def mesh_is_closed_manifold(obj) -> bool:
    if bpy.data.objects.get(obj.name) is None:
        return False
    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        if not bm.faces:
            return False
        if any(edge.is_boundary for edge in bm.edges):
            return False
        if any(not edge.is_manifold for edge in bm.edges):
            return False
        if any(not vert.is_manifold for vert in bm.verts):
            return False
        return True
    finally:
        bm.free()


def replace_object_geometry(target, source) -> None:
    old_mesh = target.data
    target.data = source.data.copy()
    target.matrix_world = source.matrix_world.copy()
    if old_mesh.users == 0:
        bpy.data.meshes.remove(old_mesh)


def retain_largest_face_component(obj) -> None:
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()

    if not bm.faces:
        bpy.ops.object.mode_set(mode="OBJECT")
        obj.select_set(False)
        return

    remaining = set(bm.faces)
    components: list[list[bmesh.types.BMFace]] = []
    while remaining:
        seed = next(iter(remaining))
        stack = [seed]
        component: list[bmesh.types.BMFace] = []
        while stack:
            face = stack.pop()
            if face not in remaining:
                continue
            remaining.remove(face)
            component.append(face)
            for edge in face.edges:
                for linked in edge.link_faces:
                    if linked in remaining:
                        stack.append(linked)
        components.append(component)

    if len(components) > 1:
        largest = max(components, key=len)
        drop_faces = [face for component in components if component is not largest for face in component]
        if drop_faces:
            try:
                record_repair("drop_components", obj, components=len(components), dropped_faces=len(drop_faces))
            except RuntimeError:
                bpy.ops.object.mode_set(mode="OBJECT")
                obj.select_set(False)
                raise
            bmesh.ops.delete(bm, geom=drop_faces, context="FACES")

    bmesh.update_edit_mesh(obj.data)
    bpy.ops.object.mode_set(mode="OBJECT")
    obj.select_set(False)


def seal_boundary_holes(obj) -> None:
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bm = bmesh.from_edit_mesh(obj.data)
    boundary_edges = [edge for edge in bm.edges if edge.is_boundary]
    if boundary_edges:
        try:
            record_repair("fill_boundary_holes", obj, boundary_edges=len(boundary_edges))
        except RuntimeError:
            bpy.ops.object.mode_set(mode="OBJECT")
            obj.select_set(False)
            raise
        bmesh.ops.holes_fill(bm, edges=boundary_edges, sides=0)
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
        bmesh.update_edit_mesh(obj.data)
    bpy.ops.object.mode_set(mode="OBJECT")
    obj.select_set(False)


def finalize_mold_half(obj) -> None:
    retain_largest_face_component(obj)
    clean_mesh_for_boolean(obj)
    seal_boundary_holes(obj)
    clean_mesh_for_boolean(obj)


def combine_bounds(bounds: Iterable[Bounds3D]) -> Bounds3D:
    bounds_list = list(bounds)
    if not bounds_list:
        raise RuntimeError("Need at least one bounds object to combine.")
    return Bounds3D(
        xmin=min(bound.xmin for bound in bounds_list),
        xmax=max(bound.xmax for bound in bounds_list),
        ymin=min(bound.ymin for bound in bounds_list),
        ymax=max(bound.ymax for bound in bounds_list),
        zmin=min(bound.zmin for bound in bounds_list),
        zmax=max(bound.zmax for bound in bounds_list),
    )


def move_object_along_axis(obj, axis: str, distance: float) -> None:
    axis_index = 0 if axis == "X" else 1
    obj.location[axis_index] += distance
    bpy.context.view_layer.update()


def translate_object(obj, delta: Vector) -> None:
    obj.location += delta
    bpy.context.view_layer.update()


def center_object_xy_on_origin(obj) -> None:
    bounds = get_world_bounds(obj)
    translate_object(obj, Vector((-bounds.xmid, -bounds.ymid, 0.0)))
    apply_all_transforms(obj)


def drop_object_to_ground(obj) -> None:
    bounds = get_world_bounds(obj)
    translate_object(obj, Vector((0.0, 0.0, -bounds.zmin)))
    apply_all_transforms(obj)


def preferred_cavity_up_rotation(split_axis: str, keep_positive: bool) -> tuple[float, float, float]:
    if split_axis == "X":
        return (0.0, pi * 0.5 if keep_positive else -pi * 0.5, 0.0)
    return (-pi * 0.5 if keep_positive else pi * 0.5, 0.0, 0.0)


def cavity_open_direction(split_axis: str, keep_positive: bool) -> Vector:
    if split_axis == "X":
        return Vector((-1.0, 0.0, 0.0) if keep_positive else (1.0, 0.0, 0.0))
    return Vector((0.0, -1.0, 0.0) if keep_positive else (0.0, 1.0, 0.0))


def _cavity_access_score(open_dir_z: float) -> tuple[int, float]:
    if open_dir_z >= 0.75:
        band = 0
    elif open_dir_z >= 0.25:
        band = 1
    elif open_dir_z >= -0.25:
        band = 2
    else:
        band = 3
    return band, -open_dir_z


def _rotation_signature(rotation_euler: tuple[float, float, float]) -> tuple[int, int, int]:
    quarter_turn = pi * 0.5
    return tuple(int(round(value / quarter_turn)) % 4 for value in rotation_euler)


def candidate_print_rotations(split_axis: str, keep_positive: bool) -> list[tuple[str, tuple[float, float, float], bool]]:
    preferred = preferred_cavity_up_rotation(split_axis, keep_positive)
    candidates = [
        ("preferred_cavity_up", preferred, True),
        ("z_up", (0.0, 0.0, 0.0), False),
        ("z_down", (pi, 0.0, 0.0), False),
        ("x_up", (0.0, -pi * 0.5, 0.0), False),
        ("x_down", (0.0, pi * 0.5, 0.0), False),
        ("y_up", (pi * 0.5, 0.0, 0.0), False),
        ("y_down", (-pi * 0.5, 0.0, 0.0), False),
    ]
    unique: list[tuple[str, tuple[float, float, float], bool]] = []
    seen: set[tuple[int, int, int]] = set()
    for name, rotation, is_preferred in candidates:
        signature = _rotation_signature(rotation)
        if signature in seen:
            continue
        seen.add(signature)
        unique.append((name, rotation, is_preferred))
    return unique


def _support_score(obj, *, overhang_angle_deg: float = 45.0) -> tuple[float, float]:
    bounds = get_world_bounds(obj)
    bottom_z = bounds.zmin
    support_threshold = -sin(overhang_angle_deg * (pi / 180.0))
    bottom_clearance = max((bounds.zmax - bounds.zmin) * 0.01, 0.08)
    unsupported_area = 0.0
    contact_area = 0.0

    for polygon in obj.data.polygons:
        center_z = float(polygon.center.z)
        normal_z = float(polygon.normal.z)
        if center_z <= bottom_z + bottom_clearance:
            if normal_z < -0.85:
                contact_area += float(polygon.area)
            continue
        if normal_z < support_threshold:
            unsupported_area += float(polygon.area) * abs(normal_z)

    return unsupported_area, -contact_area


def orient_half_for_print(obj, split_axis: str, keep_positive: bool) -> str:
    best_name = ""
    best_rotation = (0.0, 0.0, 0.0)
    best_score: tuple[int, float, float, float, int] | None = None
    base_open_vector = cavity_open_direction(split_axis, keep_positive)

    for name, rotation, is_preferred in candidate_print_rotations(split_axis, keep_positive):
        trial = duplicate_object(obj, f"OrientationTrial_{name}")
        trial.rotation_euler = rotation
        bpy.context.view_layer.update()
        open_dir = trial.matrix_world.to_3x3() @ base_open_vector
        apply_all_transforms(trial)
        center_object_xy_on_origin(trial)
        drop_object_to_ground(trial)
        score = (*_cavity_access_score(float(open_dir.z)), *_support_score(trial), 0 if is_preferred else 1)
        delete_object(trial)

        if best_score is None or score < best_score:
            best_score = score
            best_name = name
            best_rotation = rotation

    obj.rotation_euler = best_rotation
    apply_all_transforms(obj)
    center_object_xy_on_origin(obj)
    drop_object_to_ground(obj)
    return best_name


def orient_object_for_print(obj) -> str:
    best_name = ""
    best_rotation = (0.0, 0.0, 0.0)
    best_score: tuple[float, float] | None = None

    for name, rotation, _is_preferred in candidate_print_rotations("X", True):
        trial = duplicate_object(obj, f"OrientationTrial_Generic_{name}")
        trial.rotation_euler = rotation
        bpy.context.view_layer.update()
        apply_all_transforms(trial)
        center_object_xy_on_origin(trial)
        drop_object_to_ground(trial)
        score = _support_score(trial)
        delete_object(trial)

        if best_score is None or score < best_score:
            best_score = score
            best_name = name
            best_rotation = rotation

    obj.rotation_euler = best_rotation
    apply_all_transforms(obj)
    center_object_xy_on_origin(obj)
    drop_object_to_ground(obj)
    return best_name


def canonical_split_axis_for_view(parting_view: str) -> str:
    return "Y" if parting_view in {"LEFT", "RIGHT"} else "X"


def parting_rotation_for_view(parting_view: str, angle_deg: float) -> tuple[float, float, float]:
    """Map a line measured from camera image +X into the construction split plane."""
    angle = (90.0 - angle_deg) * (pi / 180.0)
    if parting_view in {"BOTTOM", "FRONT", "LEFT"}:
        angle = -angle
    if parting_view in {"TOP", "BOTTOM"}:
        return (0.0, 0.0, angle)
    if parting_view in {"FRONT", "BACK"}:
        return (0.0, angle, 0.0)
    if parting_view in {"LEFT", "RIGHT"}:
        return (angle, 0.0, 0.0)
    raise ValueError("Unknown parting view")


def apply_parting_view_rotation(part, *, parting_view: str, parting_angle_deg: float) -> str:
    bounds = get_world_bounds(part)
    translate_object(part, Vector((-bounds.xmid, -bounds.ymid, -bounds.zmid)))
    apply_all_transforms(part)

    split_axis = canonical_split_axis_for_view(parting_view)
    for axis, angle in enumerate(parting_rotation_for_view(parting_view, float(parting_angle_deg))):
        part.rotation_euler[axis] += angle

    apply_all_transforms(part)
    return split_axis


def ensure_material(name: str, color: tuple[float, float, float, float]):
    material = bpy.data.materials.get(name)
    if material is None:
        material = bpy.data.materials.new(name=name)
    material.use_nodes = True
    material.blend_method = "BLEND" if color[3] < 0.999 else "OPAQUE"
    if hasattr(material, "shadow_method"):
        material.shadow_method = "HASHED" if color[3] < 0.999 else "OPAQUE"
    nodes = material.node_tree.nodes
    principled = nodes.get("Principled BSDF")
    if principled is None:
        nodes.clear()
        principled = nodes.new(type="ShaderNodeBsdfPrincipled")
        output = nodes.new(type="ShaderNodeOutputMaterial")
        material.node_tree.links.new(principled.outputs["BSDF"], output.inputs["Surface"])
    principled.inputs["Base Color"].default_value = color
    alpha_input = principled.inputs.get("Alpha")
    if alpha_input is not None:
        alpha_input.default_value = color[3]
    principled.inputs["Roughness"].default_value = 0.55
    specular_input = principled.inputs.get("Specular IOR Level") or principled.inputs.get("Specular")
    if specular_input is not None:
        specular_input.default_value = 0.35
    return material


def assign_material(obj, material) -> None:
    if obj.data.materials:
        obj.data.materials[0] = material
    else:
        obj.data.materials.append(material)


def add_preview_camera(bounds: Bounds3D, split_axis: str):
    center = Vector((bounds.xmid, bounds.ymid, bounds.zmid))
    span_x = bounds.xmax - bounds.xmin
    span_y = bounds.ymax - bounds.ymin
    span_z = bounds.zmax - bounds.zmin
    max_span = max(span_x, span_y, span_z, 1.0)

    # The pieces are laid out along X; look across that row, not along it.
    direction = Vector((0.45, -1.4, 1.5))
    direction.normalize()
    location = center + (direction * max_span * 2.8)

    bpy.ops.object.camera_add(location=location)
    camera = bpy.context.active_object
    camera.name = "PreviewCamera"
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = max(span_x, span_y, span_z * 0.9) * 1.2
    camera.rotation_euler = (center - location).to_track_quat("-Z", "Y").to_euler()
    return camera


def add_preview_lights(bounds: Bounds3D):
    center = Vector((bounds.xmid, bounds.ymid, bounds.zmid))
    span = max(bounds.xmax - bounds.xmin, bounds.ymax - bounds.ymin, bounds.zmax - bounds.zmin, 1.0)

    bpy.ops.object.light_add(type="SUN", location=center + Vector((span * 0.8, -span * 1.4, span * 1.8)))
    sun = bpy.context.active_object
    sun.name = "PreviewSun"
    sun.rotation_euler = (0.9, 0.0, 0.7)
    sun.data.energy = 2.8
    sun.data.angle = 0.45

    bpy.ops.object.light_add(type="AREA", location=center + Vector((-span * 0.7, span * 0.9, span * 1.1)))
    area = bpy.context.active_object
    area.name = "PreviewArea"
    area.rotation_euler = (1.05, 0.0, -0.75)
    area.data.energy = 1600.0
    area.data.shape = "RECTANGLE"
    area.data.size = span * 0.9
    area.data.size_y = span * 0.6
    return [sun, area]


def angle_preview_pose(view_name: str, distance: float):
    """Camera-relative position and XYZ Euler rotation used by the six actual previews."""
    if view_name == "TOP":
        location = (0.0, 0.0, distance)
        rotation = (0.0, 0.0, 0.0)
    elif view_name == "BOTTOM":
        location = (0.0, 0.0, -distance)
        rotation = (pi, 0.0, 0.0)
    elif view_name == "FRONT":
        location = (0.0, -distance, 0.0)
        rotation = (pi * 0.5, 0.0, 0.0)
    elif view_name == "BACK":
        location = (0.0, distance, 0.0)
        rotation = (pi * 0.5, 0.0, pi)
    elif view_name == "LEFT":
        location = (-distance, 0.0, 0.0)
        rotation = (pi * 0.5, 0.0, -pi * 0.5)
    elif view_name == "RIGHT":
        location = (distance, 0.0, 0.0)
        rotation = (pi * 0.5, 0.0, pi * 0.5)
    else:
        raise ValueError("Unknown preview view")
    return location, rotation


def add_angle_preview_camera(bounds: Bounds3D, view_name: str):
    center = Vector((bounds.xmid, bounds.ymid, bounds.zmid))
    span = max(bounds.xmax - bounds.xmin, bounds.ymax - bounds.ymin, bounds.zmax - bounds.zmin, 1.0)
    relative_location, rotation = angle_preview_pose(view_name, span * 2.8)
    location = center + Vector(relative_location)

    bpy.ops.object.camera_add(location=tuple(location), rotation=rotation)
    camera = bpy.context.active_object
    camera.name = f"AnglePreviewCamera_{view_name}"
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = span * 1.6
    return camera


def render_angle_previews(part, cfg: MoldConfig) -> dict[str, Path]:
    assign_material(part, ensure_material("AnglePreviewMaterial", (0.23, 0.48, 0.76, 1.0)))

    scene = bpy.context.scene
    hidden_states: dict[object, bool] = {}
    for obj in bpy.data.objects:
        hidden_states[obj] = bool(obj.hide_render)
        obj.hide_render = obj is not part

    if scene.world is None:
        scene.world = bpy.data.worlds.new("AnglePreviewWorld")
    scene.world.use_nodes = True
    world_nodes = scene.world.node_tree.nodes
    background = world_nodes.get("Background")
    if background is not None:
        background.inputs["Color"].default_value = (0.95, 0.96, 0.98, 1.0)
        background.inputs["Strength"].default_value = 0.9

    bounds = get_world_bounds(part)
    lights = add_preview_lights(bounds)
    render_engine_prop = scene.render.bl_rna.properties.get("engine")
    render_engine_items = {item.identifier for item in render_engine_prop.enum_items} if render_engine_prop is not None else set()
    scene.render.engine = "BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in render_engine_items else "BLENDER_EEVEE"
    scene.render.image_settings.file_format = "PNG"
    scene.render.resolution_x = cfg.angle_preview_size
    scene.render.resolution_y = cfg.angle_preview_size
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False
    if hasattr(scene, "eevee"):
        scene.eevee.taa_render_samples = 24

    output_paths: dict[str, Path] = {}
    for view_name in VALID_PARTING_VIEWS:
        camera = add_angle_preview_camera(bounds, view_name)
        scene.camera = camera
        output_path = (cfg.output_dir / f"{cfg.angle_preview_prefix}_{view_name.lower()}.png").resolve()
        scene.render.filepath = str(output_path)
        bpy.ops.render.render(write_still=True)
        output_paths[view_name] = output_path
        delete_object(camera)

    for obj, previous in hidden_states.items():
        if bpy.data.objects.get(obj.name) is not None:
            obj.hide_render = previous
    for light in lights:
        delete_object(light)
    return output_paths


def render_preview(part, negative_half, positive_half, cfg: MoldConfig, center_insert=None) -> Path:
    preview_negative = duplicate_object(negative_half, "PreviewNegative")
    preview_positive = duplicate_object(positive_half, "PreviewPositive")
    preview_center = duplicate_object(center_insert, "PreviewCenter") if center_insert is not None else None

    split_gap = max(cfg.preview_split_gap, 0.0)
    preview_axis = "X"
    if preview_center is not None:
        # Lay the three pieces out as a readable triptych.
        center_bounds = get_world_bounds(preview_center)
        move_object_along_axis(preview_center, preview_axis, -center_bounds.axis_mid(preview_axis))
        center_bounds = get_world_bounds(preview_center)

        negative_bounds = get_world_bounds(preview_negative)
        negative_target_max = center_bounds.axis_min(preview_axis) - (split_gap * 0.4)
        move_object_along_axis(preview_negative, preview_axis, negative_target_max - negative_bounds.axis_max(preview_axis))

        positive_bounds = get_world_bounds(preview_positive)
        positive_target_min = center_bounds.axis_max(preview_axis) + (split_gap * 0.4)
        move_object_along_axis(preview_positive, preview_axis, positive_target_min - positive_bounds.axis_min(preview_axis))
    else:
        negative_bounds = get_world_bounds(preview_negative)
        positive_bounds = get_world_bounds(preview_positive)
        move_object_along_axis(preview_negative, preview_axis, -split_gap * 0.5 - negative_bounds.xmax)
        move_object_along_axis(preview_positive, preview_axis, split_gap * 0.5 - positive_bounds.xmin)

    assign_material(preview_negative, ensure_material("PreviewNegativeMaterial", (0.83, 0.84, 0.86, 1.0)))
    assign_material(preview_positive, ensure_material("PreviewPositiveMaterial", (0.77, 0.37, 0.20, 1.0)))
    if preview_center is not None:
        assign_material(preview_center, ensure_material("PreviewCenterMaterial", (0.55, 0.58, 0.62, 1.0)))

    scene = bpy.context.scene
    hidden_states: dict[object, bool] = {}
    preview_objects = {preview_negative, preview_positive}
    if preview_center is not None:
        preview_objects.add(preview_center)
    for obj in bpy.data.objects:
        hidden_states[obj] = bool(obj.hide_render)
        obj.hide_render = obj not in preview_objects

    if scene.world is None:
        scene.world = bpy.data.worlds.new("PreviewWorld")
    scene.world.use_nodes = True
    world_nodes = scene.world.node_tree.nodes
    background = world_nodes.get("Background")
    if background is not None:
        background.inputs["Color"].default_value = (0.12, 0.14, 0.18, 1.0)
        background.inputs["Strength"].default_value = 0.35

    preview_bounds = combine_bounds(
        bound
        for bound in (
            get_world_bounds(preview_negative),
            get_world_bounds(preview_positive),
            get_world_bounds(preview_center) if preview_center is not None else None,
        )
        if bound is not None
    )
    preview_split_axis = canonical_split_axis_for_view(cfg.parting_view) if cfg.parting_view else cfg.split_axis
    camera = add_preview_camera(preview_bounds, preview_split_axis)
    lights = add_preview_lights(preview_bounds)
    lights[0].data.energy = 1.8
    scene.view_settings.exposure = -0.7

    scene.camera = camera
    render_engine_prop = scene.render.bl_rna.properties.get("engine")
    render_engine_items = {item.identifier for item in render_engine_prop.enum_items} if render_engine_prop is not None else set()
    scene.render.engine = "BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in render_engine_items else "BLENDER_EEVEE"
    scene.render.image_settings.file_format = "PNG"
    scene.render.resolution_x = cfg.preview_width
    scene.render.resolution_y = cfg.preview_height
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False
    if hasattr(scene, "eevee"):
        scene.eevee.taa_render_samples = 32
    preview_path = (cfg.output_dir / cfg.preview_image_name).resolve()
    scene.render.filepath = str(preview_path)
    bpy.ops.render.render(write_still=True)

    for obj, previous in hidden_states.items():
        if bpy.data.objects.get(obj.name) is not None:
            obj.hide_render = previous
    for helper in [preview_negative, preview_positive, preview_center, camera, *lights]:
        if helper is None:
            continue
        delete_object(helper)

    return preview_path


def export_stl(obj, filepath: str | Path) -> None:
    global _EXPORTED_BYTES
    estimate = 84 + 50 * sum(len(face.vertices) - 2 for face in obj.data.polygons)
    if _ACTIVE_CONFIG is not None and _EXPORTED_BYTES + estimate > _ACTIVE_CONFIG.max_output_bytes:
        raise RuntimeError("STL exports would exceed --max-output-bytes.")
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.wm.stl_export(filepath=str(filepath), export_selected_objects=True)
    _EXPORTED_BYTES += Path(filepath).stat().st_size


def add_cube(name: str, min_corner: tuple[float, float, float], max_corner: tuple[float, float, float]):
    cx = (min_corner[0] + max_corner[0]) * 0.5
    cy = (min_corner[1] + max_corner[1]) * 0.5
    cz = (min_corner[2] + max_corner[2]) * 0.5
    sx = max_corner[0] - min_corner[0]
    sy = max_corner[1] - min_corner[1]
    sz = max_corner[2] - min_corner[2]
    bpy.ops.mesh.primitive_cube_add(location=(cx, cy, cz))
    obj = bpy.context.active_object
    obj.name = name
    obj.scale = (sx * 0.5, sy * 0.5, sz * 0.5)
    apply_all_transforms(obj)
    return obj


def shrink_fatten_duplicate(obj, amount: float, name: str):
    dup = duplicate_object(obj, name)
    bpy.context.view_layer.objects.active = dup
    bpy.ops.object.select_all(action="DESELECT")
    dup.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.transform.shrink_fatten(value=amount)
    bpy.ops.object.mode_set(mode="OBJECT")
    dup.select_set(False)
    return dup


# ---------- 2D convex hull / offset ----------
def convex_hull_2d(points: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    pts = sorted(set((float(x), float(y)) for x, y in points))
    if len(pts) <= 1:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)

    upper: list[tuple[float, float]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)

    return lower[:-1] + upper[:-1]


def polygon_area(points: list[tuple[float, float]]) -> float:
    area = 0.0
    for i, (x1, y1) in enumerate(points):
        x2, y2 = points[(i + 1) % len(points)]
        area += x1 * y2 - x2 * y1
    return area * 0.5


def _line_intersection(
    p1: tuple[float, float],
    d1: tuple[float, float],
    p2: tuple[float, float],
    d2: tuple[float, float],
) -> tuple[float, float]:
    denom = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(denom) < 1e-9:
        # Parallel fallback: midpoint of endpoints.
        return ((p1[0] + p2[0]) * 0.5, (p1[1] + p2[1]) * 0.5)
    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    t = (dx * d2[1] - dy * d2[0]) / denom
    return (p1[0] + t * d1[0], p1[1] + t * d1[1])


def offset_convex_polygon(points: list[tuple[float, float]], distance: float) -> list[tuple[float, float]]:
    if len(points) < 3:
        raise RuntimeError("Need at least 3 points for a footprint hull.")

    pts = points[:]
    if polygon_area(pts) < 0:
        pts.reverse()

    shifted_lines: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for i, p0 in enumerate(pts):
        p1 = pts[(i + 1) % len(pts)]
        dx = p1[0] - p0[0]
        dy = p1[1] - p0[1]
        length = max((dx * dx + dy * dy) ** 0.5, 1e-9)
        outward = (dy / length, -dx / length)  # CCW polygon outward normal
        shifted_point = (p0[0] + outward[0] * distance, p0[1] + outward[1] * distance)
        direction = (dx / length, dy / length)
        shifted_lines.append((shifted_point, direction))

    out: list[tuple[float, float]] = []
    for i in range(len(shifted_lines)):
        prev_line = shifted_lines[i - 1]
        curr_line = shifted_lines[i]
        out.append(_line_intersection(prev_line[0], prev_line[1], curr_line[0], curr_line[1]))
    return out


def add_prism_from_polygon(name: str, polygon_xy: list[tuple[float, float]], zmin: float, zmax: float):
    mesh = bpy.data.meshes.new(name)
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)

    bm = bmesh.new()
    bottom_verts = [bm.verts.new((x, y, zmin)) for x, y in polygon_xy]
    top_verts = [bm.verts.new((x, y, zmax)) for x, y in polygon_xy]
    bm.verts.ensure_lookup_table()

    # Bottom face reversed so normal points down.
    bm.faces.new(list(reversed(bottom_verts)))
    bm.faces.new(top_verts)

    for i in range(len(polygon_xy)):
        j = (i + 1) % len(polygon_xy)
        bm.faces.new((bottom_verts[i], bottom_verts[j], top_verts[j], top_verts[i]))

    bm.normal_update()
    bm.to_mesh(mesh)
    bm.free()
    apply_all_transforms(obj)
    return obj


def build_mold_body_around_source(name: str, source_obj, cfg: MoldConfig):
    source_bounds = get_world_bounds(source_obj)
    if cfg.exterior_mode == "BOX":
        return add_cube(
            name,
            (
                source_bounds.xmin - cfg.wall,
                source_bounds.ymin - cfg.wall,
                source_bounds.zmin - cfg.bottom,
            ),
            (
                source_bounds.xmax + cfg.wall,
                source_bounds.ymax + cfg.wall,
                source_bounds.zmax + cfg.top,
            ),
        )

    world_vertices = get_world_vertices(source_obj)
    hull_xy = convex_hull_2d((x, y) for x, y, _z in world_vertices)
    outer_xy = offset_convex_polygon(hull_xy, cfg.wall)
    return add_prism_from_polygon(
        name,
        outer_xy,
        source_bounds.zmin - cfg.bottom,
        source_bounds.zmax + cfg.top,
    )


def add_prism_from_split_polygon(
    name: str,
    polygon_lz: list[tuple[float, float]],
    *,
    split_axis: str,
    axis_min: float,
    axis_max: float,
):
    mesh = bpy.data.meshes.new(name)
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)

    bm = bmesh.new()

    def _vertex(axis_value: float, lateral_value: float, z_value: float):
        if split_axis == "X":
            return bm.verts.new((axis_value, lateral_value, z_value))
        return bm.verts.new((lateral_value, axis_value, z_value))

    start_verts = [_vertex(axis_min, lateral, z_value) for lateral, z_value in polygon_lz]
    end_verts = [_vertex(axis_max, lateral, z_value) for lateral, z_value in polygon_lz]
    bm.verts.ensure_lookup_table()

    bm.faces.new(list(reversed(start_verts)))
    bm.faces.new(end_verts)

    for i in range(len(polygon_lz)):
        j = (i + 1) % len(polygon_lz)
        bm.faces.new((start_verts[i], start_verts[j], end_verts[j], end_verts[i]))

    bm.normal_update()
    bm.to_mesh(mesh)
    bm.free()
    apply_all_transforms(obj)
    return obj


def add_cylinder_between_points(name: str, start: tuple[float, float, float], end: tuple[float, float, float], radius: float):
    start_vec = Vector(start)
    end_vec = Vector(end)
    delta = end_vec - start_vec
    depth = max(delta.length, 0.001)
    direction = delta.normalized() if delta.length > 1e-9 else Vector((0.0, 0.0, 1.0))
    rotation = Vector((0.0, 0.0, 1.0)).rotation_difference(direction).to_euler()
    bpy.ops.mesh.primitive_cylinder_add(
        radius=radius,
        depth=depth,
        location=tuple((start_vec + end_vec) * 0.5),
        rotation=rotation,
    )
    obj = bpy.context.active_object
    obj.name = name
    apply_all_transforms(obj)
    return obj


def add_tapered_channel(
    name: str,
    start: tuple[float, float, float],
    end: tuple[float, float, float],
    *,
    start_radius: float,
    end_radius: float,
):
    start_vec = Vector(start)
    end_vec = Vector(end)
    delta = end_vec - start_vec
    depth = max(delta.length, 0.001)
    direction = delta.normalized() if delta.length > 1e-9 else Vector((0.0, 0.0, 1.0))
    rotation = Vector((0.0, 0.0, 1.0)).rotation_difference(direction).to_euler()
    bpy.ops.mesh.primitive_cone_add(
        radius1=start_radius,
        radius2=end_radius,
        depth=depth,
        location=tuple((start_vec + end_vec) * 0.5),
        rotation=rotation,
    )
    obj = bpy.context.active_object
    obj.name = name
    apply_all_transforms(obj)
    return obj


# ---------- splitting / keys ----------
def split_lateral_axis(split_axis: str) -> str:
    return "Y" if split_axis == "X" else "X"


def point_on_parting_plane(split_axis: str, axis_value: float, lateral_value: float, z_value: float) -> tuple[float, float, float]:
    if split_axis == "X":
        return (axis_value, lateral_value, z_value)
    return (lateral_value, axis_value, z_value)


def point_in_split_half(
    split_axis: str,
    *,
    axis_value: float,
    lateral_value: float,
    z_value: float,
) -> tuple[float, float, float]:
    if split_axis == "X":
        return (axis_value, lateral_value, z_value)
    return (lateral_value, axis_value, z_value)


def bisect_keep_side(obj, split_axis: str, split_plane: float, keep_positive: bool) -> None:
    axis = str(split_axis).upper()
    if axis == "X":
        plane_co = (split_plane, 0.0, 0.0)
        plane_no = (1.0, 0.0, 0.0)
    elif axis == "Y":
        plane_co = (0.0, split_plane, 0.0)
        plane_no = (0.0, 1.0, 0.0)
    elif axis == "Z":
        plane_co = (0.0, 0.0, split_plane)
        plane_no = (0.0, 0.0, 1.0)
    else:
        raise RuntimeError(f"Unsupported bisect axis {split_axis!r}.")
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")

    # We use fill here only on closed solids. Cavity geometry is split and
    # subtracted later, so this cap becomes the mold parting face rather than
    # sealing over cavity openings.
    bpy.ops.mesh.bisect(
        plane_co=plane_co,
        plane_no=plane_no,
        use_fill=True,
        clear_inner=keep_positive,
        clear_outer=not keep_positive,
        threshold=0.0001,
        xstart=0,
        xend=1,
        ystart=0,
        yend=1,
    )
    bpy.ops.object.mode_set(mode="OBJECT")
    obj.select_set(False)
    clean_mesh_for_boolean(obj)


def retain_nonlargest_face_components(obj) -> bool:
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()

    if not bm.faces:
        bpy.ops.object.mode_set(mode="OBJECT")
        obj.select_set(False)
        return False

    remaining = set(bm.faces)
    components: list[list[bmesh.types.BMFace]] = []
    while remaining:
        seed = next(iter(remaining))
        stack = [seed]
        component: list[bmesh.types.BMFace] = []
        while stack:
            face = stack.pop()
            if face not in remaining:
                continue
            remaining.remove(face)
            component.append(face)
            for edge in face.edges:
                for linked in edge.link_faces:
                    if linked in remaining:
                        stack.append(linked)
        components.append(component)

    if len(components) <= 1:
        bmesh.ops.delete(bm, geom=bm.faces[:], context="FACES")
    else:
        largest = max(components, key=len)
        bmesh.ops.delete(bm, geom=list(largest), context="FACES")

    bmesh.update_edit_mesh(obj.data)
    bpy.ops.object.mode_set(mode="OBJECT")
    obj.select_set(False)
    clean_mesh_for_boolean(obj)
    return mesh_has_faces(obj)


def make_cylindrical_key(
    name: str,
    center: tuple[float, float, float],
    radius: float,
    length: float,
    split_axis: str,
):
    rotation = (0.0, pi * 0.5, 0.0) if split_axis == "X" else (pi * 0.5, 0.0, 0.0)
    bpy.ops.mesh.primitive_cylinder_add(
        radius=radius,
        depth=length,
        location=center,
        rotation=rotation,
    )
    obj = bpy.context.active_object
    obj.name = name
    apply_all_transforms(obj)
    return obj


def choose_key_anchors(
    cavity_bounds: Bounds3D,
    mold_bounds: Bounds3D,
    split_axis: str,
    cfg: MoldConfig,
) -> list[tuple[float, float]]:
    lateral_axis = split_lateral_axis(split_axis)
    mold_lateral_min = mold_bounds.axis_min(lateral_axis)
    mold_lateral_max = mold_bounds.axis_max(lateral_axis)
    lateral_span = mold_lateral_max - mold_lateral_min
    cavity_half_span = (cavity_bounds.axis_max(lateral_axis) - cavity_bounds.axis_min(lateral_axis)) * 0.5
    center_lateral = mold_bounds.axis_mid(lateral_axis)
    center_z = mold_bounds.zmid
    safe_lateral_offset = max(
        cavity_half_span + cfg.key_lateral_margin,
        cfg.key_radius * 3.2,
        lateral_span * 0.18,
    )
    max_lateral_offset = max(cfg.key_radius * 1.8, (lateral_span * 0.5) - (cfg.key_radius * 1.8))
    lateral_offset = min(safe_lateral_offset, max_lateral_offset)

    z_span = mold_bounds.zmax - mold_bounds.zmin
    z_offset = max(cfg.key_radius * 2.6, z_span * 0.22)
    lower_z = max(mold_bounds.zmin + cfg.key_radius * 1.8, center_z - z_offset)
    upper_z = min(mold_bounds.zmax - cfg.key_radius * 1.8, center_z + z_offset)

    left_lateral = max(mold_lateral_min + cfg.key_radius * 1.8, center_lateral - lateral_offset)
    right_lateral = min(mold_lateral_max - cfg.key_radius * 1.8, center_lateral + lateral_offset)
    return [
        (left_lateral, lower_z),
        (right_lateral, upper_z),
    ]


def add_alignment_keys(
    negative_half,
    positive_half,
    split_axis: str,
    split_plane: float,
    cavity_bounds: Bounds3D,
    mold_bounds: Bounds3D,
    cfg: MoldConfig,
) -> None:
    pad_height = max(cfg.key_radius * 1.6, 4.0)
    pad_outboard = max(cfg.key_radius * 1.8, 4.0)
    pad_inboard = max(cfg.key_radius * 0.8, 1.6)
    pad_half_depth = max(cfg.key_root * 0.55, 2.2)
    key_width = max(cfg.key_radius * 2.0, 3.2)
    key_height = max(cfg.key_radius * 1.8, 3.0)
    z_min = mold_bounds.zmin + (pad_height * 0.65)
    z_max = mold_bounds.zmax - (pad_height * 0.65)
    z_ratios = (0.16, 0.38, 0.62, 0.84)
    z_centers = [z_min + ((z_max - z_min) * ratio) for ratio in z_ratios]
    # Keep the male/female alternation vertical on each outer side so the
    # clamps or wire loops can interleave instead of stacking two protrusions
    # on one edge and two sockets on the other.
    stations = [
        ("min", z_centers[0]),
        ("min", z_centers[1]),
        ("max", z_centers[2]),
        ("max", z_centers[3]),
    ]

    for index, (side, z_center) in enumerate(stations, start=1):
        positive_gets_male = (index % 2) == 1
        if split_axis == "X":
            if side == "min":
                lateral_min = mold_bounds.ymin - pad_outboard
                lateral_max = mold_bounds.ymin + pad_inboard
            else:
                lateral_min = mold_bounds.ymax - pad_inboard
                lateral_max = mold_bounds.ymax + pad_outboard

            positive_pad = add_cube(
                f"PositiveKeyPad_{index}",
                (split_plane, lateral_min, z_center - (pad_height * 0.5)),
                (split_plane + pad_half_depth, lateral_max, z_center + (pad_height * 0.5)),
            )
            negative_pad = add_cube(
                f"NegativeKeyPad_{index}",
                (split_plane - pad_half_depth, lateral_min, z_center - (pad_height * 0.5)),
                (split_plane, lateral_max, z_center + (pad_height * 0.5)),
            )
            male = add_cube(
                f"MaleKey_{index}",
                (
                    split_plane - cfg.key_engagement,
                    ((lateral_min + lateral_max) * 0.5) - (key_width * 0.5),
                    z_center - (key_height * 0.5),
                ),
                (
                    split_plane + cfg.key_root,
                    ((lateral_min + lateral_max) * 0.5) + (key_width * 0.5),
                    z_center + (key_height * 0.5),
                ),
            )
        else:
            if side == "min":
                lateral_min = mold_bounds.xmin - pad_outboard
                lateral_max = mold_bounds.xmin + pad_inboard
            else:
                lateral_min = mold_bounds.xmax - pad_inboard
                lateral_max = mold_bounds.xmax + pad_outboard

            positive_pad = add_cube(
                f"PositiveKeyPad_{index}",
                (lateral_min, split_plane, z_center - (pad_height * 0.5)),
                (lateral_max, split_plane + pad_half_depth, z_center + (pad_height * 0.5)),
            )
            negative_pad = add_cube(
                f"NegativeKeyPad_{index}",
                (lateral_min, split_plane - pad_half_depth, z_center - (pad_height * 0.5)),
                (lateral_max, split_plane, z_center + (pad_height * 0.5)),
            )
            male = add_cube(
                f"MaleKey_{index}",
                (
                    ((lateral_min + lateral_max) * 0.5) - (key_width * 0.5),
                    split_plane - cfg.key_engagement,
                    z_center - (key_height * 0.5),
                ),
                (
                    ((lateral_min + lateral_max) * 0.5) + (key_width * 0.5),
                    split_plane + cfg.key_root,
                    z_center + (key_height * 0.5),
                ),
            )

        boolean_op(positive_half, positive_pad, "UNION", solver="EXACT")
        clean_mesh_for_boolean(positive_half)
        boolean_op(negative_half, negative_pad, "UNION", solver="EXACT")
        clean_mesh_for_boolean(negative_half)
        delete_object(positive_pad)
        delete_object(negative_pad)

        female = (
            shrink_fatten_duplicate(male, cfg.key_clearance, f"FemaleSocket_{index}")
            if cfg.key_clearance > 1e-6
            else duplicate_object(male, f"FemaleSocket_{index}")
        )
        clean_mesh_for_boolean(female)
        if positive_gets_male:
            boolean_op(positive_half, male, "UNION", solver="EXACT")
            clean_mesh_for_boolean(positive_half)
            boolean_op(negative_half, female, "DIFFERENCE", solver="EXACT")
            clean_mesh_for_boolean(negative_half)
        else:
            boolean_op(negative_half, male, "UNION", solver="EXACT")
            clean_mesh_for_boolean(negative_half)
            boolean_op(positive_half, female, "DIFFERENCE", solver="EXACT")
            clean_mesh_for_boolean(positive_half)
        delete_object(female)
        delete_object(male)


def _channel_z(cavity_bounds: Bounds3D, ratio: float) -> float:
    return cavity_bounds.zmin + ((cavity_bounds.zmax - cavity_bounds.zmin) * ratio)


def _channel_points(
    split_axis: str,
    split_plane: float,
    *,
    outer_lateral: float,
    inner_lateral: float,
    z_value: float,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    start = point_on_parting_plane(split_axis, split_plane, outer_lateral, z_value)
    end = point_on_parting_plane(split_axis, split_plane, inner_lateral, z_value)
    return start, end


def _channel_lateral(cavity_bounds: Bounds3D, lateral_axis: str, ratio: float) -> float:
    return cavity_bounds.axis_min(lateral_axis) + ((cavity_bounds.axis_max(lateral_axis) - cavity_bounds.axis_min(lateral_axis)) * ratio)


def parting_profile_points(obj, split_axis: str, split_plane: float, *, tolerance: float = 0.001) -> list[tuple[float, float]]:
    axis_index = 0 if split_axis == "X" else 1
    lateral_index = 1 if split_axis == "X" else 0
    points: list[tuple[float, float]] = []
    for vertex in get_world_vertices(obj):
        if abs(vertex[axis_index] - split_plane) <= tolerance:
            points.append((vertex[lateral_index], vertex[2]))
    if not points and tolerance < 0.01:
        return parting_profile_points(obj, split_axis, split_plane, tolerance=0.01)

    unique_points: list[tuple[float, float]] = []
    seen: set[tuple[int, int]] = set()
    for lateral, z_value in points:
        key = (int(round(lateral * 1000.0)), int(round(z_value * 1000.0)))
        if key in seen:
            continue
        seen.add(key)
        unique_points.append((lateral, z_value))
    return unique_points


def parting_profile_hull(
    profile_sources: Iterable[object],
    split_axis: str,
    split_plane: float,
) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for source in profile_sources:
        if source is None:
            continue
        points.extend(parting_profile_points(source, split_axis, split_plane))
    if len(points) < 3:
        return []
    return convex_hull_2d(points)


def _clamped_perimeter_lock_width(
    profile_points: list[tuple[float, float]],
    mold_bounds: Bounds3D,
    split_axis: str,
    requested_width: float,
) -> float:
    if not profile_points:
        return 0.0

    lateral_axis = split_lateral_axis(split_axis)
    laterals = [point[0] for point in profile_points]
    z_values = [point[1] for point in profile_points]
    available_margin = min(
        min(laterals) - mold_bounds.axis_min(lateral_axis),
        mold_bounds.axis_max(lateral_axis) - max(laterals),
        min(z_values) - mold_bounds.zmin,
        mold_bounds.zmax - max(z_values),
    )
    safe_width = max(available_margin - 0.35, 0.0)
    return min(max(requested_width, 0.0), safe_width)


def split_region_profile_points(obj, split_axis: str) -> list[tuple[float, float]]:
    lateral_index = 1 if split_axis == "X" else 0
    return [(vertex[lateral_index], vertex[2]) for vertex in get_world_vertices(obj)]


def _perimeter_lock_axis_span(
    mold_bounds: Bounds3D,
    split_axis: str,
    split_plane: float,
    cfg: MoldConfig,
) -> tuple[float, float]:
    negative_available = max(split_plane - mold_bounds.axis_min(split_axis) - 0.35, 0.0)
    positive_available = max(mold_bounds.axis_max(split_axis) - split_plane - 0.35, 0.0)
    engagement = min(max(cfg.perimeter_lock_engagement, 0.25), negative_available)
    root = min(max(cfg.perimeter_lock_root, 0.25), positive_available)
    return split_plane - engagement, split_plane + root




def _axis_span_from_total(
    mold_bounds: Bounds3D,
    split_axis: str,
    split_plane: float,
    total_span: float,
) -> tuple[float, float]:
    negative_available = max(split_plane - mold_bounds.axis_min(split_axis) - 0.5, 0.0)
    positive_available = max(mold_bounds.axis_max(split_axis) - split_plane - 0.5, 0.0)
    half_span = min(max(total_span * 0.5, 0.0), negative_available, positive_available)
    if half_span <= 0.35:
        return split_plane, split_plane
    return split_plane - half_span, split_plane + half_span




def extract_plane_face_region(
    source_obj,
    *,
    split_axis: str,
    axis_value: float,
    name: str,
    tolerance: float = 0.001,
):
    axis_index = 0 if split_axis == "X" else 1
    cap = duplicate_object(source_obj, name)
    bpy.context.view_layer.objects.active = cap
    bpy.ops.object.select_all(action="DESELECT")
    cap.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bm = bmesh.from_edit_mesh(cap.data)
    bm.faces.ensure_lookup_table()

    cap_faces = []
    for face in bm.faces:
        if all(abs(float((cap.matrix_world @ vert.co)[axis_index]) - axis_value) <= tolerance for vert in face.verts):
            cap_faces.append(face)

    if not cap_faces:
        bpy.ops.object.mode_set(mode="OBJECT")
        cap.select_set(False)
        delete_object(cap)
        return None

    drop_faces = [face for face in bm.faces if face not in cap_faces]
    if drop_faces:
        bmesh.ops.delete(bm, geom=drop_faces, context="FACES")

    bm.verts.ensure_lookup_table()
    for vert in bm.verts:
        vert.co[axis_index] = axis_value

    bmesh.ops.remove_doubles(bm, verts=bm.verts[:], dist=0.0001)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    bmesh.update_edit_mesh(cap.data)
    bpy.ops.object.mode_set(mode="OBJECT")
    cap.select_set(False)
    clean_mesh_for_boolean(cap)
    return cap if mesh_has_faces(cap) else None


def extract_split_cap_region(
    source_obj,
    *,
    split_axis: str,
    split_plane: float,
    name: str,
    tolerance: float = 0.001,
):
    return extract_plane_face_region(
        source_obj,
        split_axis=split_axis,
        axis_value=split_plane,
        name=name,
        tolerance=tolerance,
    )


def _project_world_point_to_split_plane(point: Vector, split_axis: str) -> tuple[float, float]:
    if split_axis == "X":
        return (float(point.y), float(point.z))
    return (float(point.x), float(point.z))


def _polygon_contains_point(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    if len(polygon) < 3:
        return False
    x, y = point
    inside = False
    prev_x, prev_y = polygon[-1]
    for curr_x, curr_y in polygon:
        if ((curr_y > y) != (prev_y > y)) and (
            x < ((prev_x - curr_x) * (y - curr_y) / (prev_y - curr_y)) + curr_x
        ):
            inside = not inside
        prev_x, prev_y = curr_x, curr_y
    return inside


def _planar_region_boundary_loops(obj, *, split_axis: str) -> list[list[tuple[float, float]]]:
    if not mesh_has_faces(obj):
        return []

    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        boundary_edges = [edge for edge in bm.edges if len(edge.link_faces) == 1]
        if not boundary_edges:
            return []

        neighbors: dict[int, list[int]] = {}
        for edge in boundary_edges:
            a = edge.verts[0].index
            b = edge.verts[1].index
            neighbors.setdefault(a, []).append(b)
            neighbors.setdefault(b, []).append(a)

        visited_edges: set[tuple[int, int]] = set()
        loops: list[list[tuple[float, float]]] = []
        world = obj.matrix_world
        for edge in boundary_edges:
            start = edge.verts[0].index
            nxt = edge.verts[1].index
            edge_key = tuple(sorted((start, nxt)))
            if edge_key in visited_edges:
                continue

            loop_indices = [start]
            prev = start
            current = nxt
            visited_edges.add(edge_key)

            while True:
                loop_indices.append(current)
                if current == start:
                    break
                adjacent = neighbors.get(current, [])
                candidates = [index for index in adjacent if index != prev]
                if not candidates:
                    break
                next_index = None
                for candidate in candidates:
                    candidate_key = tuple(sorted((current, candidate)))
                    if candidate_key not in visited_edges:
                        next_index = candidate
                        break
                if next_index is None:
                    next_index = candidates[0]
                visited_edges.add(tuple(sorted((current, next_index))))
                prev, current = current, next_index
                if len(loop_indices) > (len(boundary_edges) * 2):
                    break

            if len(loop_indices) >= 4:
                if loop_indices[-1] == loop_indices[0]:
                    loop_indices = loop_indices[:-1]
                loop = [
                    _project_world_point_to_split_plane(world @ bm.verts[index].co, split_axis)
                    for index in loop_indices
                ]
                if len(loop) >= 3:
                    loops.append(loop)
        return loops
    finally:
        bm.free()


def remove_split_root_faces_within_footprint(
    obj,
    *,
    footprint_obj,
    split_axis: str,
    axis_min: float,
    axis_max: float,
    tolerance: float = 0.02,
) -> bool:
    loops = _planar_region_boundary_loops(footprint_obj, split_axis=split_axis)
    if not loops:
        return False

    axis_index = 0 if split_axis == "X" else 1
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()

    drop_faces = []
    for face in bm.faces:
        world_points = [obj.matrix_world @ vert.co for vert in face.verts]
        face_axis_min = min(float(point[axis_index]) for point in world_points)
        face_axis_max = max(float(point[axis_index]) for point in world_points)
        if face_axis_max < axis_min - tolerance or face_axis_min > axis_max + tolerance:
            continue
        center = Vector((0.0, 0.0, 0.0))
        for point in world_points:
            center += point
        center /= max(len(world_points), 1)
        projected_center = _project_world_point_to_split_plane(center, split_axis)
        if any(_polygon_contains_point(projected_center, loop) for loop in loops):
            drop_faces.append(face)

    if drop_faces:
        bmesh.ops.delete(bm, geom=drop_faces, context="FACES")
        bmesh.ops.remove_doubles(bm, verts=bm.verts[:], dist=0.0001)
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    bmesh.update_edit_mesh(obj.data)
    bpy.ops.object.mode_set(mode="OBJECT")
    obj.select_set(False)
    clean_mesh_for_boolean(obj)
    return bool(drop_faces)


def extrude_planar_region_along_axis(obj, *, split_axis: str, distance: float) -> None:
    if abs(distance) <= 1e-6:
        return

    delta = Vector((distance, 0.0, 0.0)) if split_axis == "X" else Vector((0.0, distance, 0.0))
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    geom = bmesh.ops.extrude_face_region(bm, geom=bm.faces[:])["geom"]
    verts = [element for element in geom if isinstance(element, bmesh.types.BMVert)]
    if verts:
        bmesh.ops.translate(bm, verts=verts, vec=delta)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    bmesh.update_edit_mesh(obj.data)
    bpy.ops.object.mode_set(mode="OBJECT")
    obj.select_set(False)
    clean_mesh_for_boolean(obj)


def extrude_plane_faces_in_place(
    obj,
    *,
    split_axis: str,
    axis_value: float,
    distance: float,
    tolerance: float = 0.01,
) -> bool:
    if abs(distance) <= 1e-6:
        return False

    axis_index = 0 if split_axis == "X" else 1
    delta = Vector((distance, 0.0, 0.0)) if split_axis == "X" else Vector((0.0, distance, 0.0))
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()

    faces = [
        face
        for face in bm.faces
        if all(abs(float((obj.matrix_world @ vert.co)[axis_index]) - axis_value) <= tolerance for vert in face.verts)
    ]
    if not faces:
        bpy.ops.object.mode_set(mode="OBJECT")
        obj.select_set(False)
        return False

    geom = bmesh.ops.extrude_face_region(bm, geom=faces)["geom"]
    verts = [element for element in geom if isinstance(element, bmesh.types.BMVert)]
    if verts:
        bmesh.ops.translate(bm, verts=verts, vec=delta)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    bmesh.update_edit_mesh(obj.data)
    bpy.ops.object.mode_set(mode="OBJECT")
    obj.select_set(False)
    clean_mesh_for_boolean(obj)
    return True


def outset_planar_region(obj, width: float, *, split_axis: str) -> None:
    if width <= 0.0:
        return

    bounds = get_world_bounds(obj)
    lateral_index = 1 if split_axis == "X" else 0
    lateral_span = (bounds.ymax - bounds.ymin) if split_axis == "X" else (bounds.xmax - bounds.xmin)
    z_span = bounds.zmax - bounds.zmin
    scale_lateral = ((lateral_span + (width * 2.0)) / lateral_span) if lateral_span > 1e-6 else 1.0
    scale_z = ((z_span + (width * 2.0)) / z_span) if z_span > 1e-6 else 1.0
    center_lateral = bounds.ymid if split_axis == "X" else bounds.xmid

    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bm = bmesh.from_edit_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    for vert in bm.verts:
        vert.co[lateral_index] = center_lateral + ((vert.co[lateral_index] - center_lateral) * scale_lateral)
        vert.co[2] = bounds.zmid + ((vert.co[2] - bounds.zmid) * scale_z)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    bmesh.update_edit_mesh(obj.data)
    bpy.ops.object.mode_set(mode="OBJECT")
    obj.select_set(False)
    clean_mesh_for_boolean(obj)


def solidify_planar_region(
    obj,
    *,
    split_axis: str,
    split_plane: float,
    axis_min: float,
    axis_max: float,
) -> None:
    thickness = axis_max - axis_min
    if thickness <= 0.0:
        return

    center_axis = (axis_min + axis_max) * 0.5
    move_object_along_axis(obj, split_axis, center_axis - split_plane)
    mod = obj.modifiers.new(name="PerimeterLockSolidify", type="SOLIDIFY")
    mod.thickness = float(thickness)
    mod.offset = 0.0
    if hasattr(mod, "use_even_offset"):
        mod.use_even_offset = True
    if hasattr(mod, "use_quality_normals"):
        mod.use_quality_normals = True
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.modifier_apply(modifier=mod.name)
    apply_all_transforms(obj)
    clean_mesh_for_boolean(obj)


def expand_object_bounds(
    obj,
    *,
    split_axis: str,
    axis_clearance: float,
    lateral_clearance: float,
    z_clearance: float,
) -> None:
    bounds = get_world_bounds(obj)
    axis_index = 0 if split_axis == "X" else 1
    lateral_index = 1 if split_axis == "X" else 0
    spans = (
        bounds.xmax - bounds.xmin,
        bounds.ymax - bounds.ymin,
        bounds.zmax - bounds.zmin,
    )
    centers = (bounds.xmid, bounds.ymid, bounds.zmid)
    clearances = [0.0, 0.0, max(z_clearance, 0.0)]
    clearances[axis_index] = max(axis_clearance, 0.0)
    clearances[lateral_index] = max(lateral_clearance, 0.0)
    scales = []
    for span, clearance in zip(spans, clearances):
        if span <= 1e-6 or clearance <= 0.0:
            scales.append(1.0)
        else:
            scales.append((span + (clearance * 2.0)) / span)

    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bm = bmesh.from_edit_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    for vert in bm.verts:
        for index in range(3):
            vert.co[index] = centers[index] + ((vert.co[index] - centers[index]) * scales[index])
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    bmesh.update_edit_mesh(obj.data)
    bpy.ops.object.mode_set(mode="OBJECT")
    obj.select_set(False)
    clean_mesh_for_boolean(obj)


def build_axis_span_slab(
    name: str,
    *,
    split_axis: str,
    axis_min: float,
    axis_max: float,
    mold_bounds: Bounds3D,
    pad: float = 1.0,
):
    if split_axis == "X":
        return add_cube(
            name,
            (axis_min, mold_bounds.ymin - pad, mold_bounds.zmin - pad),
            (axis_max, mold_bounds.ymax + pad, mold_bounds.zmax + pad),
        )
    return add_cube(
        name,
        (mold_bounds.xmin - pad, axis_min, mold_bounds.zmin - pad),
        (mold_bounds.xmax + pad, axis_max, mold_bounds.zmax + pad),
    )


def extract_half_volume_slice(
    source_obj,
    *,
    split_axis: str,
    axis_min: float,
    axis_max: float,
    mold_bounds: Bounds3D,
    name: str,
):
    volume = duplicate_object(source_obj, name)
    slab = build_axis_span_slab(
        f"{name}_Slab",
        split_axis=split_axis,
        axis_min=axis_min,
        axis_max=axis_max,
        mold_bounds=mold_bounds,
    )
    boolean_op(volume, slab, "INTERSECT", solver="EXACT")
    clean_mesh_for_boolean(volume)
    delete_object(slab)
    if not mesh_has_faces(volume):
        delete_object(volume)
        return None
    return volume


def build_centered_exact_silhouette_slab(
    source_obj,
    *,
    split_axis: str,
    split_plane: float,
    mold_bounds: Bounds3D,
    cfg: MoldConfig,
    reinforce: bool = False,
    axis_span_override: tuple[float, float] | None = None,
):
    if axis_span_override is None:
        axis_min, axis_max = _perimeter_lock_axis_span(mold_bounds, split_axis, split_plane, cfg)
    else:
        axis_min, axis_max = axis_span_override
    negative_depth = split_plane - axis_min
    positive_depth = axis_max - split_plane
    total_span = axis_max - axis_min
    if negative_depth <= 0.05 or positive_depth <= 0.05 or total_span <= 0.12:
        return None, 0.0, axis_min, axis_max

    cap = extract_split_cap_region(
        source_obj,
        split_axis=split_axis,
        split_plane=split_plane,
        name="ExactSilhouetteCap",
    )
    if cap is None:
        return None, 0.0, axis_min, axis_max

    positive_slab = duplicate_object(cap, "ExactSilhouettePositive")
    negative_slab = duplicate_object(cap, "ExactSilhouetteNegative")
    extrude_planar_region_along_axis(positive_slab, split_axis=split_axis, distance=positive_depth)
    extrude_planar_region_along_axis(negative_slab, split_axis=split_axis, distance=-negative_depth)
    boolean_op(positive_slab, negative_slab, "UNION", solver="MANIFOLD")
    clean_mesh_for_boolean(positive_slab)
    delete_object(negative_slab)

    if reinforce:
        reinforced_slab = duplicate_object(positive_slab, "ExactSilhouetteScaledDuplicate")
        reinforcement_clearance = max(cfg.perimeter_lock_clearance, 0.08)
        expand_object_bounds(
            reinforced_slab,
            split_axis=split_axis,
            axis_clearance=0.0,
            lateral_clearance=reinforcement_clearance,
            z_clearance=reinforcement_clearance,
        )
        boolean_op(positive_slab, reinforced_slab, "UNION", solver="MANIFOLD")
        clean_mesh_for_boolean(positive_slab)
        finalize_mold_half(positive_slab)
        delete_object(reinforced_slab)
    delete_object(cap)
    return positive_slab, total_span, axis_min, axis_max


def build_exact_split_cap_prism(
    source_obj,
    *,
    split_axis: str,
    split_plane: float,
    mold_bounds: Bounds3D,
    total_span: float,
):
    axis_min, axis_max = _axis_span_from_total(mold_bounds, split_axis, split_plane, total_span)
    if (axis_max - axis_min) <= 0.12:
        return None, 0.0

    cap = extract_split_cap_region(
        source_obj,
        split_axis=split_axis,
        split_plane=split_plane,
        name="ExactCenterInsertCap",
    )
    if cap is None:
        return None, 0.0

    loops = _planar_region_boundary_loops(cap, split_axis=split_axis)
    prisms: list[object] = []
    for index, loop in enumerate(loops, start=1):
        if len(loop) < 3:
            continue
        prism = add_prism_from_split_polygon(
            f"ExactCenterInsertPrism_{index}",
            loop,
            split_axis=split_axis,
            axis_min=axis_min,
            axis_max=axis_max,
        )
        prisms.append(prism)
    delete_object(cap)
    if not prisms:
        return None, 0.0

    exact_prism = prisms[0]
    for extra in prisms[1:]:
        boolean_op(exact_prism, extra, "UNION", solver="MANIFOLD")
        clean_mesh_for_boolean(exact_prism)
        delete_object(extra)

    finalize_mold_half(exact_prism)
    if not mesh_is_closed_manifold(exact_prism):
        voxel_remesh_object(
            exact_prism,
            min(recommended_voxel_size(get_world_bounds(exact_prism), clearance=0.4), 0.12),
        )
        clean_mesh_for_boolean(exact_prism)
        finalize_mold_half(exact_prism)
    if not mesh_is_closed_manifold(exact_prism):
        delete_object(exact_prism)
        return None, 0.0
    return exact_prism, (axis_max - axis_min)


def build_manual_center_insert_candidates(
    mold_body,
    cutter,
    silhouette_cap_source,
    silhouette_relief_source,
    *,
    split_axis: str,
    split_plane: float,
    mold_bounds: Bounds3D,
    cutter_bounds: Bounds3D,
    cfg: MoldConfig,
):

    reference_mold = duplicate_object(mold_body, "CenterInsertReferenceMold")
    boolean_op(reference_mold, cutter, "DIFFERENCE", solver="MANIFOLD")
    clean_mesh_for_boolean(reference_mold)
    reference_negative = duplicate_object(reference_mold, "CenterInsertReferenceNegative")
    reference_positive = duplicate_object(reference_mold, "CenterInsertReferencePositive")
    bisect_keep_side(reference_negative, split_axis, split_plane, keep_positive=False)
    bisect_keep_side(reference_positive, split_axis, split_plane, keep_positive=True)
    finalize_mold_half(reference_negative)
    finalize_mold_half(reference_positive)
    delete_object(reference_mold)

    cavity_floor_plane = cutter_bounds.zmin + max(min(cfg.clearance * 0.15, 0.08), 0.02)

    center_insert, span = build_exact_split_cap_prism(
        silhouette_cap_source,
        split_axis=split_axis,
        split_plane=split_plane,
        mold_bounds=mold_bounds,
        total_span=cfg.center_insert_total_span,
    )
    if center_insert is None or span <= 0.0:
        delete_object(reference_negative)
        delete_object(reference_positive)
        raise CenterInsertError(
            "Center insert mode was enabled, but the manual unsplit-mold center insert path failed. "
            f"Source={cfg.center_insert_source.lower()} span={cfg.center_insert_total_span:.3f} mm. "
            "Details: could not build the exact split-cap prism."
        )

    center_relief = duplicate_object(silhouette_relief_source, "CenterInsertRelief")
    boolean_op(center_insert, center_relief, "DIFFERENCE", solver="MANIFOLD")
    clean_mesh_for_boolean(center_insert)
    delete_object(center_relief)

    cavity_relief = duplicate_object(cutter, "CenterInsertCavityRelief")
    boolean_op(center_insert, cavity_relief, "DIFFERENCE", solver="MANIFOLD")
    clean_mesh_for_boolean(center_insert)
    delete_object(cavity_relief)
    finalize_mold_half(center_insert)
    if not mesh_is_closed_manifold(center_insert):
        voxel_remesh_object(
            center_insert,
            min(recommended_voxel_size(get_world_bounds(center_insert), clearance=cfg.clearance), 0.12),
        )
        clean_mesh_for_boolean(center_insert)
        finalize_mold_half(center_insert)
    if not mesh_is_closed_manifold(center_insert):
        delete_object(reference_negative)
        delete_object(reference_positive)
        delete_object(center_insert)
        raise CenterInsertError(
            "Center insert mode was enabled, but the manual unsplit-mold center insert path failed. "
            f"Source={cfg.center_insert_source.lower()} span={cfg.center_insert_total_span:.3f} mm. "
            "Details: exact silhouette shell could not be made watertight after model relief."
        )

    center_socket_source = duplicate_object(center_insert, "CenterInsertSeatShell")
    augmented_cutter = duplicate_object(cutter, "CenterInsertAugmentedCutter")
    boolean_op(augmented_cutter, center_insert, "UNION", solver="MANIFOLD")
    clean_mesh_for_boolean(augmented_cutter)
    finalize_mold_half(augmented_cutter)
    if not mesh_is_closed_manifold(augmented_cutter):
        voxel_remesh_object(
            augmented_cutter,
            min(recommended_voxel_size(get_world_bounds(augmented_cutter), clearance=cfg.clearance), 0.12),
        )
        clean_mesh_for_boolean(augmented_cutter)
        finalize_mold_half(augmented_cutter)
    if not mesh_is_closed_manifold(augmented_cutter):
        delete_object(reference_negative)
        delete_object(reference_positive)
        delete_object(augmented_cutter)
        delete_object(center_socket_source)
        delete_object(center_insert)
        raise CenterInsertError(
            "Center insert mode was enabled, but the manual unsplit-mold center insert path failed. "
            f"Source={cfg.center_insert_source.lower()} span={cfg.center_insert_total_span:.3f} mm. "
            "Details: augmented cavity cutter was not manifold."
        )

    mold_with_cavity = duplicate_object(mold_body, "CenterInsertUnsplitMold")
    boolean_op(mold_with_cavity, augmented_cutter, "DIFFERENCE", solver="MANIFOLD")
    clean_mesh_for_boolean(mold_with_cavity)
    finalize_mold_half(mold_with_cavity)
    delete_object(augmented_cutter)
    if not mesh_is_closed_manifold(mold_with_cavity):
        voxel_remesh_object(
            mold_with_cavity,
            min(recommended_voxel_size(get_world_bounds(mold_with_cavity), clearance=cfg.clearance), 0.12),
        )
        clean_mesh_for_boolean(mold_with_cavity)
        finalize_mold_half(mold_with_cavity)
    if not mesh_is_closed_manifold(mold_with_cavity):
        delete_object(reference_negative)
        delete_object(reference_positive)
        delete_object(mold_with_cavity)
        delete_object(center_socket_source)
        delete_object(center_insert)
        raise CenterInsertError(
            "Center insert mode was enabled, but the manual unsplit-mold center insert path failed. "
            f"Source={cfg.center_insert_source.lower()} span={cfg.center_insert_total_span:.3f} mm. "
            "Details: unsplit mold with cavity was not manifold."
        )

    inner_capture = duplicate_object(mold_with_cavity, "CenterInsertInnerCapture")
    bisect_keep_side(inner_capture, "Z", cavity_floor_plane, keep_positive=True)
    if retain_nonlargest_face_components(inner_capture):
        finalize_mold_half(inner_capture)
        if mesh_has_faces(inner_capture):
            boolean_op(center_insert, inner_capture, "UNION", solver="MANIFOLD")
            clean_mesh_for_boolean(center_insert)
        delete_object(inner_capture)
    else:
        delete_object(inner_capture)

    finalize_mold_half(center_insert)
    if not mesh_is_closed_manifold(center_insert):
        voxel_remesh_object(
            center_insert,
            min(recommended_voxel_size(get_world_bounds(center_insert), clearance=cfg.clearance), 0.12),
        )
        clean_mesh_for_boolean(center_insert)
        finalize_mold_half(center_insert)
    if not mesh_is_closed_manifold(center_insert):
        delete_object(reference_negative)
        delete_object(reference_positive)
        delete_object(mold_with_cavity)
        delete_object(center_socket_source)
        delete_object(center_insert)
        raise CenterInsertError(
            "Center insert mode was enabled, but the manual unsplit-mold center insert path failed. "
            f"Source={cfg.center_insert_source.lower()} span={cfg.center_insert_total_span:.3f} mm. "
            "Details: center insert could not be made watertight."
        )

    negative_candidate = duplicate_object(reference_negative, "CenterInsertNegative")
    positive_candidate = duplicate_object(reference_positive, "CenterInsertPositive")
    _apply_center_insert_socket_to_outer_halves(
        center_socket_source,
        negative_candidate,
        positive_candidate,
        split_axis=split_axis,
        split_plane=split_plane,
        cfg=cfg,
    )
    delete_object(center_socket_source)
    delete_object(mold_with_cavity)

    center_insert_issue = _center_insert_outer_half_balance_issue(
        reference_negative,
        reference_positive,
        negative_candidate,
        positive_candidate,
    )
    delete_object(reference_negative)
    delete_object(reference_positive)
    if center_insert_issue is not None:
        delete_object(negative_candidate)
        delete_object(positive_candidate)
        delete_object(center_insert)
        raise CenterInsertError(
            "Center insert mode was enabled, but the manual unsplit-mold center insert path failed. "
            f"Source={cfg.center_insert_source.lower()} span={cfg.center_insert_total_span:.3f} mm. "
            f"Details: {center_insert_issue}."
        )

    return negative_candidate, positive_candidate, center_insert, span, "manual-center-insert"


def _build_center_insert_hull_shell(
    *,
    split_axis: str,
    axis_min: float,
    axis_max: float,
    seat_margin: float,
    profile_points: list[tuple[float, float]],
) -> object | None:
    if len(profile_points) < 3:
        return None

    hull = convex_hull_2d(profile_points)
    if len(hull) < 3:
        return None

    outer_hull = offset_convex_polygon(hull, seat_margin)
    if len(outer_hull) < 3:
        return None

    outer_shell = add_prism_from_split_polygon(
        "CenterInsertHullOuter",
        outer_hull,
        split_axis=split_axis,
        axis_min=axis_min,
        axis_max=axis_max,
    )
    inner_shell = add_prism_from_split_polygon(
        "CenterInsertHullInner",
        hull,
        split_axis=split_axis,
        axis_min=axis_min - 0.02,
        axis_max=axis_max + 0.02,
    )
    boolean_op(outer_shell, inner_shell, "DIFFERENCE", solver="EXACT")
    clean_mesh_for_boolean(outer_shell)
    delete_object(inner_shell)
    finalize_mold_half(outer_shell)
    if not mesh_is_closed_manifold(outer_shell):
        voxel_remesh_object(
            outer_shell,
            min(recommended_voxel_size(get_world_bounds(outer_shell), clearance=0.4), 0.12),
        )
        clean_mesh_for_boolean(outer_shell)
        finalize_mold_half(outer_shell)
    if not mesh_is_closed_manifold(outer_shell):
        delete_object(outer_shell)
        return None
    return outer_shell


def build_center_insert(
    cavity_cap_source,
    *,
    split_axis: str,
    split_plane: float,
    mold_bounds: Bounds3D,
    cfg: MoldConfig,
    axis_span_override: tuple[float, float] | None = None,
):
    center_axis_span = (
        axis_span_override
        if axis_span_override is not None
        else _axis_span_from_total(
            mold_bounds,
            split_axis,
            split_plane,
            cfg.center_insert_total_span,
        )
    )
    axis_min, axis_max = center_axis_span
    negative_depth = split_plane - axis_min
    positive_depth = axis_max - split_plane
    total_span = axis_max - axis_min
    if negative_depth <= 0.05 or positive_depth <= 0.05 or total_span <= 0.12:
        return None, None, 0.0

    seat_cap = extract_split_cap_region(
        cavity_cap_source,
        split_axis=split_axis,
        split_plane=split_plane,
        name="CenterInsertSeatCap",
    )
    if seat_cap is None:
        return None, None, 0.0

    seat_margin = max(min(cfg.wall * 0.22, 2.2), 1.05)
    seat_loops = _planar_region_boundary_loops(seat_cap, split_axis=split_axis)
    outer_cap = duplicate_object(seat_cap, "CenterInsertOuterCap")
    expand_object_bounds(
        outer_cap,
        split_axis=split_axis,
        axis_clearance=0.0,
        lateral_clearance=seat_margin,
        z_clearance=seat_margin,
    )

    outer_positive = duplicate_object(outer_cap, "CenterInsertOuterPositive")
    outer_negative = duplicate_object(outer_cap, "CenterInsertOuterNegative")
    extrude_planar_region_along_axis(outer_positive, split_axis=split_axis, distance=positive_depth)
    extrude_planar_region_along_axis(outer_negative, split_axis=split_axis, distance=-negative_depth)
    boolean_op(outer_positive, outer_negative, "UNION", solver="MANIFOLD")
    clean_mesh_for_boolean(outer_positive)
    delete_object(outer_negative)

    inner_positive = duplicate_object(seat_cap, "CenterInsertInnerPositive")
    inner_negative = duplicate_object(seat_cap, "CenterInsertInnerNegative")
    extrude_planar_region_along_axis(inner_positive, split_axis=split_axis, distance=positive_depth + 0.02)
    extrude_planar_region_along_axis(inner_negative, split_axis=split_axis, distance=-(negative_depth + 0.02))
    boolean_op(inner_positive, inner_negative, "UNION", solver="MANIFOLD")
    clean_mesh_for_boolean(inner_positive)
    delete_object(inner_negative)

    boolean_op(outer_positive, inner_positive, "DIFFERENCE", solver="EXACT")
    clean_mesh_for_boolean(outer_positive)
    delete_object(inner_positive)
    delete_object(outer_cap)

    insert_shell = outer_positive

    if len(seat_loops) > 1:
        lateral_values = [point[0] for loop in seat_loops for point in loop]
        z_values = [point[1] for loop in seat_loops for point in loop]
        if lateral_values and z_values:
            rail_thickness = min(max(cfg.center_insert_base_thickness * 0.22, 0.8), 1.4)
            rail_gap = max(seat_margin * 0.45, 0.45)
            rail_lateral_min = max(lateral_values) + rail_gap
            rail_lateral_max = rail_lateral_min + rail_thickness
            rail_z_min = min(z_values) - max(rail_thickness * 0.45, 0.35)
            rail_z_max = max(z_values) + max(rail_thickness * 0.45, 0.35)
            if split_axis == "X":
                side_rail = add_cube(
                    "CenterInsertSideRail",
                    (axis_min, rail_lateral_min, rail_z_min),
                    (axis_max, rail_lateral_max, rail_z_max),
                )
            else:
                side_rail = add_cube(
                    "CenterInsertSideRail",
                    (rail_lateral_min, axis_min, rail_z_min),
                    (rail_lateral_max, axis_max, rail_z_max),
                )
            boolean_op(insert_shell, side_rail, "UNION", solver="EXACT")
            clean_mesh_for_boolean(insert_shell)
            delete_object(side_rail)

    delete_object(seat_cap)

    finalize_mold_half(insert_shell)
    if not mesh_is_closed_manifold(insert_shell):
        voxel_remesh_object(
            insert_shell,
            min(recommended_voxel_size(get_world_bounds(insert_shell), clearance=cfg.clearance), 0.12),
        )
        clean_mesh_for_boolean(insert_shell)
        finalize_mold_half(insert_shell)
    if not mesh_is_closed_manifold(insert_shell):
        delete_object(insert_shell)
        profile_points = [point for loop in seat_loops for point in loop]
        insert_shell = _build_center_insert_hull_shell(
            split_axis=split_axis,
            axis_min=axis_min,
            axis_max=axis_max,
            seat_margin=seat_margin,
            profile_points=profile_points,
        )
        if insert_shell is None:
            delete_object(seat_cap)
            return None, None, 0.0
        print("Center insert shell fallback: using hull-based open-center shell.")

    socket_source = duplicate_object(insert_shell, "CenterInsertSeatSocket")
    center_insert = insert_shell
    center_insert.name = "CenterInsert"
    return center_insert, socket_source, (axis_max - axis_min)


def _apply_center_insert_socket_to_outer_halves(
    socket_source,
    negative_half,
    positive_half,
    *,
    split_axis: str,
    split_plane: float,
    cfg: MoldConfig,
) -> None:
    center_socket = (
        shrink_fatten_duplicate(socket_source, cfg.center_insert_clearance, "CenterInsertSocket")
        if cfg.center_insert_clearance > 1e-6
        else duplicate_object(socket_source, "CenterInsertSocket")
    )
    clean_mesh_for_boolean(center_socket)
    # Give the seat cutter a tiny extra overlap along the split axis so the
    # socket subtraction doesn't leave a coplanar seam at the far insert face.
    expand_object_bounds(
        center_socket,
        split_axis=split_axis,
        axis_clearance=0.04,
        lateral_clearance=0.0,
        z_clearance=0.0,
    )
    clean_mesh_for_boolean(center_socket)
    negative_center_socket = duplicate_object(center_socket, "NegativeCenterInsertSocket")
    positive_center_socket = duplicate_object(center_socket, "PositiveCenterInsertSocket")
    bisect_keep_side(negative_center_socket, split_axis, split_plane, keep_positive=False)
    bisect_keep_side(positive_center_socket, split_axis, split_plane, keep_positive=True)
    boolean_op(negative_half, negative_center_socket, "DIFFERENCE", solver="EXACT")
    clean_mesh_for_boolean(negative_half)
    boolean_op(positive_half, positive_center_socket, "DIFFERENCE", solver="EXACT")
    clean_mesh_for_boolean(positive_half)
    delete_object(negative_center_socket)
    delete_object(positive_center_socket)
    delete_object(center_socket)
    for outer_half in (negative_half, positive_half):
        remesh_size = min(recommended_voxel_size(get_world_bounds(outer_half), clearance=cfg.clearance), 0.12)
        voxel_remesh_object(outer_half, remesh_size)
        clean_mesh_for_boolean(outer_half)
        finalize_mold_half(outer_half)
        if not mesh_is_closed_manifold(outer_half):
            voxel_remesh_object(outer_half, max(remesh_size * 0.75, 0.06))
            clean_mesh_for_boolean(outer_half)
            finalize_mold_half(outer_half)
        if not mesh_is_closed_manifold(outer_half):
            voxel_remesh_object(outer_half, 0.05)
            clean_mesh_for_boolean(outer_half)
            finalize_mold_half(outer_half)


def _center_insert_outer_half_balance_issue(
    reference_negative,
    reference_positive,
    candidate_negative,
    candidate_positive,
) -> str | None:
    negative_issue = _lock_candidate_geometry_issue(
        reference_negative,
        candidate_negative,
        require_closed_manifold=False,
    )
    if negative_issue:
        return f"negative {negative_issue}"

    positive_issue = _lock_candidate_geometry_issue(
        reference_positive,
        candidate_positive,
        require_closed_manifold=False,
    )
    if positive_issue:
        return f"positive {positive_issue}"

    negative_bounds = get_world_bounds(candidate_negative)
    positive_bounds = get_world_bounds(candidate_positive)
    axis_ratios: list[float] = []
    for axis in ("x", "y", "z"):
        negative_extent = getattr(negative_bounds, f"{axis}max") - getattr(negative_bounds, f"{axis}min")
        positive_extent = getattr(positive_bounds, f"{axis}max") - getattr(positive_bounds, f"{axis}min")
        largest = max(float(negative_extent), float(positive_extent), 1e-6)
        smallest = min(float(negative_extent), float(positive_extent))
        axis_ratios.append(smallest / largest)
    min_axis_ratio = min(axis_ratios) if axis_ratios else 1.0
    face_ratio = min(len(candidate_negative.data.polygons), len(candidate_positive.data.polygons)) / max(
        len(candidate_negative.data.polygons),
        len(candidate_positive.data.polygons),
        1,
    )
    min_face_ratio = 0.18 if min_axis_ratio >= 0.95 else 0.20
    if min_axis_ratio < 0.45 or face_ratio < min_face_ratio:
        return f"outer half balance too small (min_axis_ratio={min_axis_ratio:.3f} face_ratio={face_ratio:.3f})"
    return None


def build_manual_exact_perimeter_lock_candidates(
    mold_body,
    cutter,
    *,
    cavity_cap_source,
    split_axis: str,
    split_plane: float,
    mold_bounds: Bounds3D,
    cfg: MoldConfig,
    reinforce: bool = False,
):
    silhouette_slab, lock_width, axis_min, axis_max = build_centered_exact_silhouette_slab(
        cavity_cap_source,
        split_axis=split_axis,
        split_plane=split_plane,
        mold_bounds=mold_bounds,
        cfg=cfg,
        reinforce=reinforce,
    )
    if silhouette_slab is None or lock_width <= 0.0:
        if silhouette_slab is not None:
            delete_object(silhouette_slab)
        return None

    reinforced_male_source = duplicate_object(silhouette_slab, "ExactPerimeterReinforcedMaleSource") if reinforce else None
    if reinforced_male_source is not None:
        axis_overlap = max(cfg.clearance * 0.12, 0.03)
        expand_object_bounds(
            reinforced_male_source,
            split_axis=split_axis,
            axis_clearance=axis_overlap,
            lateral_clearance=0.0,
            z_clearance=0.0,
        )
    augmented_cutter = duplicate_object(cutter, "ExactPerimeterAugmentedCutter")
    boolean_op(augmented_cutter, silhouette_slab, "UNION", solver="MANIFOLD")
    clean_mesh_for_boolean(augmented_cutter)
    finalize_mold_half(augmented_cutter)
    if not mesh_is_closed_manifold(augmented_cutter):
        voxel_remesh_object(
            augmented_cutter,
            min(recommended_voxel_size(get_world_bounds(augmented_cutter), clearance=cfg.clearance), 0.12),
        )
        clean_mesh_for_boolean(augmented_cutter)
        finalize_mold_half(augmented_cutter)
    delete_object(silhouette_slab)

    if reinforce:
        mold_with_socket = duplicate_object(mold_body, "ExactPerimeterReinforcedMold")
        boolean_op(mold_with_socket, augmented_cutter, "DIFFERENCE", solver="MANIFOLD")
        clean_mesh_for_boolean(mold_with_socket)

        negative_candidate = duplicate_object(mold_with_socket, "ExactPerimeterNegative")
        positive_base = duplicate_object(mold_with_socket, "ExactPerimeterPositiveBase")
        bisect_keep_side(negative_candidate, split_axis, split_plane, keep_positive=False)
        bisect_keep_side(positive_base, split_axis, split_plane, keep_positive=True)
        finalize_mold_half(negative_candidate)
        finalize_mold_half(positive_base)

        delete_object(mold_with_socket)
        delete_object(augmented_cutter)

        reinforced_success = False
        if reinforced_male_source is not None:
            for union_solver in ("EXACT", "MANIFOLD"):
                positive_trial = duplicate_object(positive_base, f"ExactPerimeterPositivePrimary_{union_solver}")
                male_trial = duplicate_object(reinforced_male_source, f"ExactPerimeterMalePrimary_{union_solver}")
                male_cavity_relief = duplicate_object(cutter, f"ExactPerimeterMaleRelief_{union_solver}")
                boolean_op(male_trial, male_cavity_relief, "DIFFERENCE", solver="EXACT")
                clean_mesh_for_boolean(male_trial)
                delete_object(male_cavity_relief)
                finalize_mold_half(male_trial)
                if not mesh_is_closed_manifold(male_trial):
                    voxel_remesh_object(
                        male_trial,
                        min(recommended_voxel_size(get_world_bounds(male_trial), clearance=cfg.clearance), 0.12),
                    )
                    clean_mesh_for_boolean(male_trial)
                    finalize_mold_half(male_trial)
                if not mesh_is_closed_manifold(male_trial):
                    delete_object(male_trial)
                    delete_object(positive_trial)
                    continue

                boolean_op(positive_trial, male_trial, "UNION", solver=union_solver)
                clean_mesh_for_boolean(positive_trial)
                finalize_mold_half(positive_trial)
                delete_object(male_trial)
                if not mesh_is_closed_manifold(positive_trial):
                    voxel_remesh_object(
                        positive_trial,
                        min(recommended_voxel_size(get_world_bounds(positive_trial), clearance=cfg.clearance), 0.12),
                    )
                    clean_mesh_for_boolean(positive_trial)
                    finalize_mold_half(positive_trial)
                if not mesh_is_closed_manifold(positive_trial):
                    delete_object(positive_trial)
                    continue

                positive_candidate = positive_trial
                reinforced_success = True
                break

        if reinforced_male_source is not None:
            delete_object(reinforced_male_source)
            reinforced_male_source = None
        delete_object(positive_base)

        if not reinforced_success:
            delete_object(negative_candidate)
            return None
    else:
        mold_with_socket = duplicate_object(mold_body, "ExactPerimeterMold")
        boolean_op(mold_with_socket, augmented_cutter, "DIFFERENCE", solver="MANIFOLD")
        clean_mesh_for_boolean(mold_with_socket)

        negative_candidate = duplicate_object(mold_with_socket, "ExactPerimeterNegative")
        positive_candidate = duplicate_object(mold_with_socket, "ExactPerimeterPositive")
        bisect_keep_side(negative_candidate, split_axis, split_plane, keep_positive=False)
        bisect_keep_side(positive_candidate, split_axis, split_plane, keep_positive=True)
        delete_object(mold_with_socket)
        delete_object(augmented_cutter)

        positive_key_face = extract_plane_face_region(
            positive_candidate,
            split_axis=split_axis,
            axis_value=axis_max,
            name="ExactPerimeterPositiveKeyFace",
            tolerance=0.01,
        )
        if positive_key_face is None:
            delete_object(negative_candidate)
            delete_object(positive_candidate)
            return None

        extrude_planar_region_along_axis(
            positive_key_face,
            split_axis=split_axis,
            distance=-(axis_max - axis_min),
        )
        male_key = positive_key_face
        if cfg.perimeter_lock_clearance > 1e-6:
            clearanced_key = shrink_fatten_duplicate(
                male_key,
                -(cfg.perimeter_lock_clearance * 0.5),
                "ExactPerimeterPositiveKeyClearanced",
            )
            clean_mesh_for_boolean(clearanced_key)
            delete_object(male_key)
            male_key = clearanced_key
        boolean_op(positive_candidate, male_key, "UNION", solver="MANIFOLD")
        clean_mesh_for_boolean(positive_candidate)
        delete_object(male_key)

    finalize_mold_half(negative_candidate)
    finalize_mold_half(positive_candidate)
    return negative_candidate, positive_candidate, lock_width


def build_perimeter_lock_rib(
    *,
    split_axis: str,
    split_plane: float,
    mold_bounds: Bounds3D,
    profile_sources: Iterable[object],
    cfg: MoldConfig,
):
    profile_hull = parting_profile_hull(profile_sources, split_axis, split_plane)
    if len(profile_hull) < 3:
        return None, None

    lock_width = _clamped_perimeter_lock_width(profile_hull, mold_bounds, split_axis, cfg.perimeter_lock_width)
    if lock_width <= 0.12:
        return None, None

    axis_min, axis_max = _perimeter_lock_axis_span(mold_bounds, split_axis, split_plane, cfg)
    if axis_max - axis_min <= 0.18:
        return None, None

    outer_profile = offset_convex_polygon(profile_hull, lock_width)
    outer_prism = add_prism_from_split_polygon(
        "PerimeterLockOuter",
        outer_profile,
        split_axis=split_axis,
        axis_min=axis_min,
        axis_max=axis_max,
    )
    inner_prism = add_prism_from_split_polygon(
        "PerimeterLockInner",
        profile_hull,
        split_axis=split_axis,
        axis_min=axis_min - 0.25,
        axis_max=axis_max + 0.25,
    )
    boolean_op(outer_prism, inner_prism, "DIFFERENCE", solver="EXACT")
    clean_mesh_for_boolean(outer_prism)
    delete_object(inner_prism)
    return outer_prism, lock_width


def build_exact_cavity_perimeter_lock_rib(
    *,
    split_axis: str,
    split_plane: float,
    mold_bounds: Bounds3D,
    cavity_profile_sources: Iterable[object],
    negative_exact_source,
    positive_exact_source,
    cfg: MoldConfig,
):
    profile_hull = parting_profile_hull(cavity_profile_sources, split_axis, split_plane)
    if len(profile_hull) < 3:
        return None, None

    lock_width = _clamped_perimeter_lock_width(profile_hull, mold_bounds, split_axis, cfg.perimeter_lock_width)
    if lock_width <= 0.12:
        return None, None

    axis_min, axis_max = _perimeter_lock_axis_span(mold_bounds, split_axis, split_plane, cfg)
    if axis_max - axis_min <= 0.18:
        return None, None

    cavity_sources = [source for source in cavity_profile_sources if source is not None]
    if not cavity_sources:
        return None, None

    # Seed the feature from the actual cavity split face, not the mold shell.
    exact_plug = extract_split_cap_region(
        cavity_sources[-1],
        split_axis=split_axis,
        split_plane=split_plane,
        name="ExactCavityPerimeterLockCap",
    )
    if exact_plug is None and len(cavity_sources) > 1:
        exact_plug = extract_split_cap_region(
            cavity_sources[0],
            split_axis=split_axis,
            split_plane=split_plane,
            name="ExactCavityPerimeterLockCapFallback",
        )
    if exact_plug is None:
        return None, None

    solidify_planar_region(
        exact_plug,
        split_axis=split_axis,
        split_plane=split_plane,
        axis_min=axis_min,
        axis_max=axis_max,
    )

    for cavity_source in cavity_sources:
        boolean_op(exact_plug, cavity_source, "DIFFERENCE", solver="EXACT")
        clean_mesh_for_boolean(exact_plug)
        if not mesh_has_faces(exact_plug):
            delete_object(exact_plug)
            return None, None

    return exact_plug, lock_width


def build_exact_parting_cap_lip(
    *,
    split_axis: str,
    split_plane: float,
    mold_bounds: Bounds3D,
    negative_exact_source,
    positive_exact_source,
    cfg: MoldConfig,
):
    cap = extract_split_cap_region(
        positive_exact_source,
        split_axis=split_axis,
        split_plane=split_plane,
        name="ExactPartingLipCap",
    )
    if cap is None:
        cap = extract_split_cap_region(
            negative_exact_source,
            split_axis=split_axis,
            split_plane=split_plane,
            name="ExactPartingLipCapFallback",
        )
    if cap is None:
        return None

    axis_min, axis_max = _perimeter_lock_axis_span(mold_bounds, split_axis, split_plane, cfg)
    if axis_max - axis_min <= 0.18:
        delete_object(cap)
        return None

    # Use the true bisect cap as the profile, then extrude that exact constant
    # silhouette across the lock span. This mirrors the successful manual
    # Blender workflow much better than sampling a tapered half-volume slice.
    solidify_planar_region(
        cap,
        split_axis=split_axis,
        split_plane=split_plane,
        axis_min=axis_min,
        axis_max=axis_max,
    )
    return cap if mesh_has_faces(cap) else None


def build_exact_parting_slice_lip(
    *,
    split_axis: str,
    split_plane: float,
    mold_bounds: Bounds3D,
    negative_profile_source,
    positive_profile_source,
    cfg: MoldConfig,
):
    half_depth = min(max(cfg.perimeter_lock_width * 0.45, 0.3), 0.8)
    negative_axis_min = max(mold_bounds.axis_min(split_axis) + 0.15, split_plane - half_depth)
    positive_axis_max = min(mold_bounds.axis_max(split_axis) - 0.15, split_plane + half_depth)
    if split_plane - negative_axis_min <= 0.05 or positive_axis_max - split_plane <= 0.05:
        return None

    negative_slice = extract_half_volume_slice(
        negative_profile_source,
        split_axis=split_axis,
        axis_min=negative_axis_min,
        axis_max=split_plane,
        mold_bounds=mold_bounds,
        name="ExactPartingLipNegative",
    )
    positive_slice = extract_half_volume_slice(
        positive_profile_source,
        split_axis=split_axis,
        axis_min=split_plane,
        axis_max=positive_axis_max,
        mold_bounds=mold_bounds,
        name="ExactPartingLipPositive",
    )
    if negative_slice is None or positive_slice is None:
        for helper in (negative_slice, positive_slice):
            if helper is not None:
                delete_object(helper)
        return None

    boolean_op(positive_slice, negative_slice, "UNION", solver="EXACT")
    clean_mesh_for_boolean(positive_slice)
    delete_object(negative_slice)
    return positive_slice if mesh_has_faces(positive_slice) else None


def _lock_candidate_geometry_issue(
    reference_half,
    candidate_half,
    *,
    tolerance: float = 0.45,
    require_closed_manifold: bool = True,
) -> str | None:
    if not mesh_has_faces(candidate_half):
        return "candidate has no faces"
    if require_closed_manifold and not mesh_is_closed_manifold(candidate_half):
        return "candidate is not closed manifold"
    reference_bounds = get_world_bounds(reference_half)
    candidate_bounds = get_world_bounds(candidate_half)
    for axis in ("x", "y", "z"):
        ref_min = getattr(reference_bounds, f"{axis}min")
        ref_max = getattr(reference_bounds, f"{axis}max")
        candidate_min = getattr(candidate_bounds, f"{axis}min")
        candidate_max = getattr(candidate_bounds, f"{axis}max")
        if candidate_min > ref_min + tolerance:
            return f"{axis} min shrank too far ({candidate_min:.3f} > {ref_min + tolerance:.3f})"
        if candidate_max < ref_max - tolerance:
            return f"{axis} max shrank too far ({candidate_max:.3f} < {ref_max - tolerance:.3f})"
    reference_faces = max(len(reference_half.data.polygons), 1)
    candidate_faces = len(candidate_half.data.polygons)
    if candidate_faces < max(250, int(reference_faces * 0.08)):
        return f"candidate face count too small ({candidate_faces} vs {reference_faces})"
    return None


def _lock_candidate_geometry_is_sane(
    reference_half,
    candidate_half,
    *,
    tolerance: float = 0.45,
    require_closed_manifold: bool = True,
) -> bool:
    return (
        _lock_candidate_geometry_issue(
            reference_half,
            candidate_half,
            tolerance=tolerance,
            require_closed_manifold=require_closed_manifold,
        )
        is None
    )


def choose_channel_target_from_profile(
    profile_points: list[tuple[float, float]],
    *,
    lateral_ratio: float,
    z_ratio: float,
    inward_distance: float,
) -> tuple[float, float] | None:
    if not profile_points:
        return None

    lateral_values = [point[0] for point in profile_points]
    z_values = [point[1] for point in profile_points]
    target_lateral = min(lateral_values) + ((max(lateral_values) - min(lateral_values)) * lateral_ratio)
    target_z = min(z_values) + ((max(z_values) - min(z_values)) * z_ratio)
    candidate = min(
        profile_points,
        key=lambda point: ((point[0] - target_lateral) ** 2) + (((point[1] - target_z) * 1.35) ** 2),
    )
    centroid_lateral = sum(point[0] for point in profile_points) / len(profile_points)
    centroid_z = sum(point[1] for point in profile_points) / len(profile_points)
    delta_lateral = centroid_lateral - candidate[0]
    delta_z = centroid_z - candidate[1]
    length = max((delta_lateral * delta_lateral + delta_z * delta_z) ** 0.5, 1e-9)
    step = min(inward_distance, length * 0.7)
    return (
        candidate[0] + (delta_lateral / length) * step,
        candidate[1] + (delta_z / length) * step,
    )


def _cap_face_channel_points(
    split_axis: str,
    axis_value: float,
    *,
    outer_lateral: float,
    inner_lateral: float,
    outer_z: float,
    inner_z: float,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    start = point_in_split_half(split_axis, axis_value=axis_value, lateral_value=outer_lateral, z_value=outer_z)
    end = point_in_split_half(split_axis, axis_value=axis_value, lateral_value=inner_lateral, z_value=inner_z)
    return start, end


def _split_normal_channel_points(
    split_axis: str,
    *,
    outer_axis: float,
    inner_axis: float,
    lateral_value: float,
    z_value: float,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    start = point_in_split_half(
        split_axis,
        axis_value=outer_axis,
        lateral_value=lateral_value,
        z_value=z_value,
    )
    end = point_in_split_half(
        split_axis,
        axis_value=inner_axis,
        lateral_value=lateral_value,
        z_value=z_value,
    )
    return start, end


def subtract_feature_from_half(target_half, cutter) -> None:
    # Channels are narrow, sometimes nearly tangent features. Blender's
    # MANIFOLD solver is fast when every operand stays perfectly closed, but
    # EXACT is much more tolerant of real-world mesh offsets here.
    boolean_op(target_half, cutter, "DIFFERENCE", solver="EXACT")
    clean_mesh_for_boolean(target_half)
    delete_object(cutter)


def _channel_axis_value(
    mold_bounds: Bounds3D,
    split_axis: str,
    split_plane: float,
    *,
    side: str,
    radius: float,
    preferred_wall_distance: float,
) -> float:
    if side not in {"negative", "positive"}:
        raise ValueError(f"Unsupported half side: {side}")

    if side == "positive":
        outward_limit = mold_bounds.axis_max(split_axis)
        max_offset = max(outward_limit - split_plane - (radius * 1.25), radius * 0.6)
        offset = min(max(preferred_wall_distance, radius * 1.35), max_offset)
        return split_plane + offset

    outward_limit = mold_bounds.axis_min(split_axis)
    max_offset = max(split_plane - outward_limit - (radius * 1.25), radius * 0.6)
    offset = min(max(preferred_wall_distance, radius * 1.35), max_offset)
    return split_plane - offset


def add_parting_channels(
    negative_half,
    positive_half,
    split_axis: str,
    split_plane: float,
    cavity_bounds: Bounds3D,
    mold_bounds: Bounds3D,
    negative_profile_source,
    positive_profile_source,
    cfg: MoldConfig,
) -> None:
    lateral_axis = split_lateral_axis(split_axis)
    breakthrough = max(cfg.channel_breakthrough, cfg.clearance * 1.5, 0.4)
    top_face_z = mold_bounds.zmax + max(cfg.injection_outer_radius, cfg.vent_radius, breakthrough)
    preferred_wall_distance = max(cfg.clearance + 0.9, 1.2)
    lock_avoidance = max(
        breakthrough,
        (cfg.clearance * 2.0) + 0.8,
        (cfg.perimeter_lock_width + breakthrough + max(cfg.clearance, cfg.perimeter_lock_clearance))
        if (cfg.add_perimeter_lock or cfg.add_center_insert)
        else 0.0,
    )

    if cfg.add_injection_site:
        profile_points = parting_profile_points(positive_profile_source, split_axis, split_plane)
        injection_target = choose_channel_target_from_profile(
            profile_points,
            lateral_ratio=0.22,
            z_ratio=0.34,
            inward_distance=lock_avoidance + max(cfg.injection_inner_radius, cfg.clearance),
        )
        if injection_target is not None:
            injection_lateral, injection_z = injection_target
        else:
            injection_lateral = _channel_lateral(cavity_bounds, lateral_axis, 0.24)
            injection_z = min(_channel_z(cavity_bounds, 0.36), cavity_bounds.zmax - breakthrough)
        injection_start, injection_end = _cap_face_channel_points(
            split_axis,
            _channel_axis_value(
                mold_bounds,
                split_axis,
                split_plane,
                side="positive",
                radius=cfg.injection_outer_radius,
                preferred_wall_distance=preferred_wall_distance,
            ),
            outer_lateral=injection_lateral,
            inner_lateral=injection_lateral,
            outer_z=top_face_z,
            inner_z=injection_z,
        )
        injection = add_tapered_channel(
            "InjectionChannel",
            injection_start,
            injection_end,
            start_radius=cfg.injection_outer_radius,
            end_radius=cfg.injection_inner_radius,
        )
        _GEOMETRY_EVENTS.append({"kind": "channel", "half": "positive", "start": list(injection_start),
                                 "end": list(injection_end), "inner_radius": cfg.injection_inner_radius,
                                 "outer_radius": cfg.injection_outer_radius})
        subtract_feature_from_half(positive_half, injection)

    if cfg.add_vent:
        profile_points = parting_profile_points(negative_profile_source, split_axis, split_plane)
        vent_target = choose_channel_target_from_profile(
            profile_points,
            lateral_ratio=0.68,
            z_ratio=0.80,
            inward_distance=lock_avoidance + max(cfg.vent_radius, cfg.clearance),
        )
        if vent_target is not None:
            vent_lateral, vent_z = vent_target
        else:
            vent_lateral = _channel_lateral(cavity_bounds, lateral_axis, 0.76)
            vent_z = min(_channel_z(cavity_bounds, 0.82), cavity_bounds.zmax - breakthrough)
        vent_start, vent_end = _cap_face_channel_points(
            split_axis,
            _channel_axis_value(
                mold_bounds,
                split_axis,
                split_plane,
                side="negative",
                radius=cfg.vent_radius,
                preferred_wall_distance=max(cfg.clearance + 0.7, 0.9),
            ),
            outer_lateral=vent_lateral,
            inner_lateral=vent_lateral,
            outer_z=top_face_z,
            inner_z=vent_z,
        )
        vent = add_cylinder_between_points("VentChannel", vent_start, vent_end, cfg.vent_radius)
        _GEOMETRY_EVENTS.append({"kind": "channel", "half": "negative", "start": list(vent_start),
                                 "end": list(vent_end), "inner_radius": cfg.vent_radius,
                                 "outer_radius": cfg.vent_radius})
        subtract_feature_from_half(negative_half, vent)


def add_perimeter_lock(
    negative_half,
    positive_half,
    split_axis: str,
    split_plane: float,
    mold_bounds: Bounds3D,
    negative_cavity_source,
    positive_cavity_source,
    negative_exact_source,
    positive_exact_source,
    cfg: MoldConfig,
) -> float:
    # The manual exact silhouette path is attempted earlier in main().
    # This helper is now the conservative hull-based fallback.
    lock_variants = (("stable-hull", False),)
    for variant_name, require_closed_manifold in lock_variants:
        candidate_rib, lock_width = build_perimeter_lock_rib(
            split_axis=split_axis,
            split_plane=split_plane,
            mold_bounds=mold_bounds,
            profile_sources=(negative_cavity_source, positive_cavity_source),
            cfg=cfg,
        )

        if candidate_rib is None or lock_width is None:
            if candidate_rib is not None:
                delete_object(candidate_rib)
            continue

        # Ensure the finished collar never extends into the actual cavity.
        for cavity_source in (negative_cavity_source, positive_cavity_source):
            boolean_op(candidate_rib, cavity_source, "DIFFERENCE", solver="EXACT")
            clean_mesh_for_boolean(candidate_rib)
        if not mesh_has_faces(candidate_rib):
            delete_object(candidate_rib)
            continue

        positive_candidate = duplicate_object(positive_half, f"MoldPositive_{variant_name}")
        negative_candidate = duplicate_object(negative_half, f"MoldNegative_{variant_name}")
        boolean_op(positive_candidate, candidate_rib, "UNION", solver="EXACT")
        clean_mesh_for_boolean(positive_candidate)

        socket = (
            shrink_fatten_duplicate(candidate_rib, cfg.perimeter_lock_clearance, f"PerimeterLockSocket_{variant_name}")
            if cfg.perimeter_lock_clearance > 1e-6
            else duplicate_object(candidate_rib, f"PerimeterLockSocket_{variant_name}")
        )
        clean_mesh_for_boolean(socket)
        boolean_op(negative_candidate, socket, "DIFFERENCE", solver="EXACT")
        clean_mesh_for_boolean(negative_candidate)
        delete_object(socket)
        delete_object(candidate_rib)

        finalize_mold_half(negative_candidate)
        finalize_mold_half(positive_candidate)

        negative_issue = _lock_candidate_geometry_issue(
            negative_half,
            negative_candidate,
            require_closed_manifold=require_closed_manifold,
        )
        positive_issue = _lock_candidate_geometry_issue(
            positive_half,
            positive_candidate,
            require_closed_manifold=require_closed_manifold,
        )
        if negative_issue is None and positive_issue is None:
            replace_object_geometry(negative_half, negative_candidate)
            replace_object_geometry(positive_half, positive_candidate)
            delete_object(negative_candidate)
            delete_object(positive_candidate)
            print(f"Perimeter lock fallback: using {variant_name} variant for stability.")
            return lock_width

        delete_object(negative_candidate)
        delete_object(positive_candidate)

    print("Perimeter lock skipped: no stable lock variant survived boolean validation.")
    return 0.0


def _positive_float(raw_value: str) -> float:
    value = float(raw_value)
    if not isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return value


def _non_negative_float(raw_value: str) -> float:
    value = float(raw_value)
    if not isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("value must be zero or greater")
    return value


def _finite_float(raw_value: str) -> float:
    value = float(raw_value)
    if not isfinite(value):
        raise argparse.ArgumentTypeError("value must be finite")
    return value


def _voxel_size(raw_value: str) -> float:
    value = _positive_float(raw_value)
    if not 0.02 <= value <= 2.0:
        raise argparse.ArgumentTypeError("voxel size must be between 0.02 and 2.0 model units")
    return value


def _output_name(value: str) -> str:
    if (not value or value in {".", ".."} or any(c in value for c in '/\\:\0')
            or value.endswith((" ", "."))
            or value.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}):
        raise argparse.ArgumentTypeError("output names must be plain filenames")
    return value


def _positive_int(raw_value: str) -> int:
    value = int(raw_value)
    if value <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return value


def _normalized_axis(raw_value: str) -> str:
    axis = raw_value.upper()
    if axis not in VALID_SPLIT_AXES:
        raise argparse.ArgumentTypeError(f"split axis must be one of: {', '.join(VALID_SPLIT_AXES)}")
    return axis


def _normalized_exterior_mode(raw_value: str) -> str:
    mode = raw_value.upper()
    if mode not in VALID_EXTERIOR_MODES:
        raise argparse.ArgumentTypeError(f"exterior mode must be one of: {', '.join(VALID_EXTERIOR_MODES)}")
    return mode


def _normalized_center_insert_source(raw_value: str) -> str:
    source = raw_value.upper()
    if source not in VALID_CENTER_INSERT_SOURCES:
        raise argparse.ArgumentTypeError(
            f"center insert source must be one of: {', '.join(VALID_CENTER_INSERT_SOURCES)}"
        )
    return source


def _normalized_parting_view(raw_value: str) -> str:
    view = raw_value.upper()
    if view not in VALID_PARTING_VIEWS:
        raise argparse.ArgumentTypeError(f"parting view must be one of: {', '.join(VALID_PARTING_VIEWS)}")
    return view


def _script_argv(argv: Sequence[str] | None = None) -> list[str]:
    if argv is not None:
        return list(argv)
    if "--" in sys.argv:
        return sys.argv[sys.argv.index("--") + 1 :]

    executable = Path(sys.argv[0]).name.lower()
    looks_like_blender_launch = executable.startswith("blender") or any(
        arg in {"--background", "--factory-startup", "--python"} for arg in sys.argv[1:]
    )
    if looks_like_blender_launch:
        return []

    return sys.argv[1:]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a two-part mold around an STL using Blender booleans. "
            "When launching from Blender CLI, pass mold-script options after `--`."
        )
    )
    parser.add_argument(
        "input_path",
        help="Explicit path to the input STL.",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        required=True,
        help="Directory for the exported mold-half STL files.",
    )
    parser.add_argument(
        "--clearance",
        type=_non_negative_float,
        default=CLEARANCE,
        help="Cavity offset around the imported part.",
    )
    parser.add_argument(
        "--wall",
        type=_non_negative_float,
        default=WALL,
        help="Wall thickness around the cavity footprint.",
    )
    parser.add_argument(
        "--bottom",
        type=_non_negative_float,
        default=BOTTOM,
        help="Bottom thickness below the cavity.",
    )
    parser.add_argument(
        "--top",
        type=_non_negative_float,
        default=TOP,
        help="Top thickness above the cavity.",
    )
    parser.add_argument(
        "--split-axis",
        type=_normalized_axis,
        default=SPLIT_AXIS,
        help="Parting axis. `X` creates negative/positive X halves. `Y` does the same along Y.",
    )
    parser.add_argument(
        "--split-offset",
        type=_finite_float,
        default=SPLIT_OFFSET,
        help="Shift the parting plane away from the imported-part midpoint.",
    )
    parser.add_argument(
        "--parting-view",
        type=_normalized_parting_view,
        default=None,
        help="Orthographic view used for visual parting-angle selection.",
    )
    parser.add_argument(
        "--parting-angle-deg",
        type=_finite_float,
        default=PARTING_ANGLE_DEG,
        help="Parting-line angle in the selected view image, in degrees from image +X.",
    )
    parser.add_argument(
        "--exterior-mode",
        type=_normalized_exterior_mode,
        default=EXTERIOR_MODE,
        help="Choose BOX or FOOTPRINT_HULL exterior geometry.",
    )
    parser.add_argument(
        "--alignment-keys",
        action=argparse.BooleanOptionalAction,
        default=ADD_ALIGNMENT_KEYS,
        help="Enable or disable cylindrical alignment keys on the parting plane.",
    )
    parser.add_argument(
        "--key-radius",
        type=_positive_float,
        default=KEY_RADIUS,
        help="Radius of each cylindrical alignment key.",
    )
    parser.add_argument(
        "--key-clearance",
        type=_non_negative_float,
        default=KEY_CLEARANCE,
        help="Extra radial clearance added to the female sockets.",
    )
    parser.add_argument(
        "--key-engagement",
        type=_positive_float,
        default=KEY_ENGAGEMENT,
        help="How far each male key protrudes across the parting plane.",
    )
    parser.add_argument(
        "--key-root",
        type=_positive_float,
        default=KEY_ROOT,
        help="How far each male key anchors inside the positive split half.",
    )
    parser.add_argument(
        "--key-lateral-margin",
        type=_non_negative_float,
        default=KEY_LATERAL_MARGIN,
        help="Preferred side spacing between the cavity and alignment keys within the parting plane.",
    )
    parser.add_argument(
        "--key-z-margin",
        type=_non_negative_float,
        default=KEY_Z_MARGIN,
        help="Preferred top/bottom spacing between the cavity and alignment keys.",
    )
    parser.add_argument(
        "--perimeter-lock",
        action=argparse.BooleanOptionalAction,
        default=ADD_PERIMETER_LOCK,
        help="Add a continuous perimeter rib/socket around the split-face cavity outline.",
    )
    parser.add_argument(
        "--perimeter-lock-width",
        type=_positive_float,
        default=PERIMETER_LOCK_WIDTH,
        help="Outward width of the perimeter-lock rib around the cavity outline.",
    )
    parser.add_argument(
        "--perimeter-lock-engagement",
        type=_positive_float,
        default=PERIMETER_LOCK_ENGAGEMENT,
        help="How far the perimeter-lock rib protrudes across the parting plane.",
    )
    parser.add_argument(
        "--perimeter-lock-root",
        type=_positive_float,
        default=PERIMETER_LOCK_ROOT,
        help="How far the perimeter-lock rib anchors inside the positive half.",
    )
    parser.add_argument(
        "--perimeter-lock-clearance",
        type=_non_negative_float,
        default=PERIMETER_LOCK_CLEARANCE,
        help="Extra clearance added to the perimeter-lock socket.",
    )
    parser.add_argument(
        "--center-insert",
        action=argparse.BooleanOptionalAction,
        default=ADD_CENTER_INSERT,
        help="Export an optional third center insert carrying the cavity-derived shell/key geometry.",
    )
    parser.add_argument(
        "--center-insert-source",
        type=_normalized_center_insert_source,
        default=CENTER_INSERT_SOURCE,
        help="Choose whether the center insert is derived from the cavity shell or the part profile section.",
    )
    parser.add_argument(
        "--center-insert-total-span",
        type=_positive_float,
        default=CENTER_INSERT_TOTAL_SPAN,
        help="Total split-axis span of the center insert before socket clearance is applied.",
    )
    parser.add_argument(
        "--center-insert-clearance",
        type=_non_negative_float,
        default=CENTER_INSERT_CLEARANCE,
        help="Extra clearance around the removable center insert inside the outer halves.",
    )
    parser.add_argument(
        "--center-insert-base-thickness",
        type=_non_negative_float,
        default=CENTER_INSERT_BASE_THICKNESS,
        help="Thickness of the small base attached to the center insert.",
    )
    parser.add_argument(
        "--injection-site",
        action=argparse.BooleanOptionalAction,
        default=ADD_INJECTION_SITE,
        help="Cut a tapered injection channel down the parting plane.",
    )
    parser.add_argument(
        "--injection-inner-radius",
        type=_positive_float,
        default=INJECTION_INNER_RADIUS,
        help="Parting-face radius where the injection site meets the cavity.",
    )
    parser.add_argument(
        "--injection-outer-radius",
        type=_positive_float,
        default=INJECTION_OUTER_RADIUS,
        help="Outer mouth radius of the tapered injection site.",
    )
    parser.add_argument(
        "--vent",
        action=argparse.BooleanOptionalAction,
        default=ADD_VENT,
        help="Cut a smaller vent channel on the opposite side of the mold.",
    )
    parser.add_argument(
        "--vent-radius",
        type=_positive_float,
        default=VENT_RADIUS,
        help="Radius of the vent channel on the parting face.",
    )
    parser.add_argument(
        "--channel-breakthrough",
        type=_positive_float,
        default=CHANNEL_BREAKTHROUGH,
        help="How far each parting-face channel pushes into the cavity footprint.",
    )
    parser.add_argument(
        "--auto-rotate-cavity-up",
        action=argparse.BooleanOptionalAction,
        default=AUTO_ROTATE_CAVITY_UP,
        help="Auto-orient each finished half for printing by minimizing likely overhang support, with cavity-up used as the tie-break preference.",
    )
    parser.add_argument(
        "--export-negative",
        "--export-left",
        dest="export_negative_name",
        default=EXPORT_LEFT,
        help="Filename for the negative half export.",
    )
    parser.add_argument(
        "--export-positive",
        "--export-right",
        dest="export_positive_name",
        default=EXPORT_RIGHT,
        help="Filename for the positive half export.",
    )
    parser.add_argument(
        "--export-center",
        dest="export_center_name",
        default=EXPORT_CENTER,
        help="Filename for the optional center-insert export.",
    )
    parser.add_argument(
        "--render-preview",
        action=argparse.BooleanOptionalAction,
        default=RENDER_PREVIEW,
        help="Render a preview PNG of the generated mold halves.",
    )
    parser.add_argument(
        "--preview-image",
        dest="preview_image_name",
        default=PREVIEW_IMAGE,
        help="Filename for the preview PNG written into the output directory.",
    )
    parser.add_argument(
        "--preview-width",
        type=_positive_int,
        default=PREVIEW_WIDTH,
        help="Preview image width in pixels.",
    )
    parser.add_argument(
        "--preview-height",
        type=_positive_int,
        default=PREVIEW_HEIGHT,
        help="Preview image height in pixels.",
    )
    parser.add_argument(
        "--preview-split-gap",
        type=_non_negative_float,
        default=PREVIEW_SPLIT_GAP,
        help="Visual separation between the two halves in the preview render.",
    )
    parser.add_argument(
        "--render-angle-previews",
        action=argparse.BooleanOptionalAction,
        default=RENDER_ANGLE_PREVIEWS,
        help="Render orthographic preview images for the visual parting-angle picker.",
    )
    parser.add_argument(
        "--angle-preview-only",
        action=argparse.BooleanOptionalAction,
        default=ANGLE_PREVIEW_ONLY,
        help="Render the orthographic parting previews and exit without generating mold halves.",
    )
    parser.add_argument(
        "--angle-preview-prefix",
        default=ANGLE_PREVIEW_PREFIX,
        help="Filename prefix for orthographic parting preview images.",
    )
    parser.add_argument(
        "--angle-preview-size",
        type=_positive_int,
        default=ANGLE_PREVIEW_SIZE,
        help="Width and height of each orthographic parting preview image in pixels.",
    )
    parser.add_argument(
        "--clear-scene",
        action=argparse.BooleanOptionalAction,
        default=CLEAR_SCENE,
        help="Clear the current Blender scene before importing the STL.",
    )
    parser.add_argument("--artifact-manifest", type=Path, help="Machine-readable result manifest for the launcher.")
    parser.add_argument("--voxel-size", type=_voxel_size, default=None,
                        help="Override every remesh resolution (0.02–2.0). Larger values reduce size and detail; default preserves the original heuristic.")
    parser.add_argument("--max-input-bytes", type=_positive_int, default=64 * 1024 * 1024)
    parser.add_argument("--max-output-bytes", type=_positive_int, default=256 * 1024 * 1024)
    parser.add_argument("--allow-mesh-repair", action=argparse.BooleanOptionalAction, default=False,
                        help="Explicitly permit and report discarding extra components or filling boundary holes. Inspect resulting geometry.")
    return parser


def parse_config(argv: Sequence[str] | None = None) -> MoldConfig:
    parser = build_arg_parser()
    args = parser.parse_args(_script_argv(argv))

    input_stl = Path(args.input_path).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    if not input_stl.exists():
        raise SystemExit(f"Input STL was not found: {input_stl}")
    if input_stl.is_dir():
        raise SystemExit(f"Input STL must be a file, not a directory: {input_stl}")
    if output_dir.exists() and not output_dir.is_dir():
        raise SystemExit(f"Output path must be a directory: {output_dir}")
    if input_stl.suffix.lower() != ".stl" or input_stl.stat().st_size > args.max_input_bytes:
        parser.error("Input must be an STL within --max-input-bytes.")
    if max(args.preview_width, args.preview_height, args.angle_preview_size) > 2048:
        parser.error("Preview dimensions are limited to 2048 pixels.")
    names = [args.export_negative_name, args.export_positive_name, args.export_center_name,
             args.preview_image_name, args.angle_preview_prefix]
    for name in names:
        _output_name(name)
    if len({name.casefold() for name in names[:4]}) != 4:
        parser.error("Export and preview filenames must be distinct.")
    if any(Path(name).suffix.lower() != ".stl" for name in names[:3]) or Path(names[3]).suffix.lower() != ".png":
        parser.error("Mesh output names must end in .stl and assembly preview in .png.")
    expected_names = [] if args.angle_preview_only else names[:3] + ([names[3]] if args.render_preview else [])
    if args.angle_preview_only or args.render_angle_previews:
        expected_names += [f"{args.angle_preview_prefix}_{view.lower()}.png" for view in VALID_PARTING_VIEWS]
    if any((output_dir / name).exists() or (output_dir / name).is_symlink() for name in expected_names):
        parser.error("Use a fresh output directory; existing generated files are never overwritten.")
    if any((output_dir / name).resolve() == input_stl for name in names[:4]):
        parser.error("An output must not overwrite the input STL.")
    manifest = args.artifact_manifest.expanduser().resolve() if args.artifact_manifest else None
    if manifest is not None and (manifest == input_stl or manifest in [output_dir / name for name in expected_names]):
        parser.error("Artifact manifest must be distinct from the input and generated outputs.")
    if args.angle_preview_only:
        args.render_angle_previews = True

    if args.alignment_keys and args.key_root + args.key_engagement <= 0:
        raise SystemExit("Alignment keys need a positive combined key root and engagement length.")
    if args.injection_outer_radius < args.injection_inner_radius:
        raise SystemExit("Injection outer radius must be greater than or equal to the inner radius.")

    return MoldConfig(
        input_stl=input_stl,
        output_dir=output_dir,
        clearance=args.clearance,
        wall=args.wall,
        bottom=args.bottom,
        top=args.top,
        split_axis=args.split_axis,
        split_offset=args.split_offset,
        parting_view=args.parting_view,
        parting_angle_deg=args.parting_angle_deg,
        exterior_mode=args.exterior_mode,
        add_alignment_keys=args.alignment_keys,
        key_radius=args.key_radius,
        key_clearance=args.key_clearance,
        key_engagement=args.key_engagement,
        key_root=args.key_root,
        key_lateral_margin=args.key_lateral_margin,
        key_z_margin=args.key_z_margin,
        add_perimeter_lock=args.perimeter_lock,
        perimeter_lock_width=args.perimeter_lock_width,
        perimeter_lock_engagement=args.perimeter_lock_engagement,
        perimeter_lock_root=args.perimeter_lock_root,
        perimeter_lock_clearance=args.perimeter_lock_clearance,
        add_center_insert=args.center_insert,
        center_insert_source=args.center_insert_source,
        center_insert_total_span=args.center_insert_total_span,
        center_insert_clearance=args.center_insert_clearance,
        center_insert_base_thickness=args.center_insert_base_thickness,
        add_injection_site=args.injection_site,
        injection_inner_radius=args.injection_inner_radius,
        injection_outer_radius=args.injection_outer_radius,
        add_vent=args.vent,
        vent_radius=args.vent_radius,
        channel_breakthrough=args.channel_breakthrough,
        auto_rotate_cavity_up=args.auto_rotate_cavity_up,
        export_negative_name=args.export_negative_name,
        export_positive_name=args.export_positive_name,
        export_center_name=args.export_center_name,
        render_preview=args.render_preview,
        preview_image_name=args.preview_image_name,
        preview_width=args.preview_width,
        preview_height=args.preview_height,
        preview_split_gap=args.preview_split_gap,
        render_angle_previews=args.render_angle_previews,
        angle_preview_only=args.angle_preview_only,
        angle_preview_prefix=args.angle_preview_prefix,
        angle_preview_size=args.angle_preview_size,
        clear_scene=args.clear_scene,
        artifact_manifest=manifest,
        voxel_size=args.voxel_size,
        max_input_bytes=args.max_input_bytes,
        max_output_bytes=args.max_output_bytes,
        allow_mesh_repair=args.allow_mesh_repair,
    )


def _generate(cfg: MoldConfig) -> int:
    alignment_keys_disabled_reason = None
    perimeter_lock_width_used = 0.0
    center_insert = None
    center_insert_orientation = None
    angle_preview_paths = {}

    if cfg.add_alignment_keys and not parting_angle_supports_alignment_keys(cfg):
        cfg.add_alignment_keys = False
        alignment_keys_disabled_reason = (
            f"Alignment keys were disabled for {cfg.parting_view}@{cfg.parting_angle_deg:.3f}deg. "
            "The keyed arbitrary-angle path is still experimental; rerun at 90 deg for keyed molds."
        )

    if cfg.clear_scene:
        clear_scene()

    part = import_stl(cfg.input_stl)
    part.name = "Part"
    apply_all_transforms(part)
    if len(part.data.polygons) > 1_000_000:
        raise RuntimeError("Input exceeds one million faces.")
    require_solid(part)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)

    if cfg.render_angle_previews:
        angle_preview_paths = render_angle_previews(part, cfg)
        for view_name, output_path in angle_preview_paths.items():
            print(f"Angle preview {view_name}: {output_path}")
        if cfg.angle_preview_only:
            successful_manifest(cfg, previews={key: str(path) for key, path in angle_preview_paths.items()})
            return 0

    working_split_axis = cfg.split_axis
    if cfg.parting_view:
        working_split_axis = apply_parting_view_rotation(
            part,
            parting_view=cfg.parting_view,
            parting_angle_deg=cfg.parting_angle_deg,
        )

    part_bounds = get_world_bounds(part)

    # Build the cavity cutter by offsetting along normals rather than globally scaling.
    cutter = shrink_fatten_duplicate(part, cfg.clearance, "CavityCutter")
    clean_mesh_for_boolean(cutter)
    voxel_remesh_object(cutter, recommended_voxel_size(get_world_bounds(cutter), clearance=cfg.clearance))
    clean_mesh_for_boolean(cutter)
    cutter_bounds = get_world_bounds(cutter)

    # Build outer mold body.
    if cfg.exterior_mode == "BOX":
        mold_body = add_cube(
            "MoldBody",
            (
                part_bounds.xmin - cfg.wall - cfg.clearance,
                part_bounds.ymin - cfg.wall - cfg.clearance,
                part_bounds.zmin - cfg.bottom - cfg.clearance,
            ),
            (
                part_bounds.xmax + cfg.wall + cfg.clearance,
                part_bounds.ymax + cfg.wall + cfg.clearance,
                part_bounds.zmax + cfg.top + cfg.clearance,
            ),
        )
    else:
        # Flat-bottom / flat-top prism from the XY convex hull of the part footprint.
        world_vertices = get_world_vertices(part)
        hull_xy = convex_hull_2d((x, y) for x, y, _z in world_vertices)
        outer_xy = offset_convex_polygon(hull_xy, cfg.wall + cfg.clearance)
        mold_body = add_prism_from_polygon(
            "MoldBody",
            outer_xy,
            part_bounds.zmin - cfg.bottom - cfg.clearance,
            part_bounds.zmax + cfg.top + cfg.clearance,
        )

    mold_bounds = get_world_bounds(mold_body)

    # Split the closed solids first, then subtract the matching half-cutter
    # from each side. This prevents the bisect fill step from capping the
    # cavity loops on the parting face.
    split_plane = part_bounds.axis_mid(working_split_axis) + cfg.split_offset
    if not part_bounds.axis_min(working_split_axis) < split_plane < part_bounds.axis_max(working_split_axis):
        raise RuntimeError("The split plane must cross the interior of the input bounds.")
    negative_half = duplicate_object(mold_body, "MoldNegative")
    positive_half = duplicate_object(mold_body, "MoldPositive")
    negative_part = duplicate_object(part, "NegativePartProfile")
    positive_part = duplicate_object(part, "PositivePartProfile")
    negative_cutter = duplicate_object(cutter, "NegativeCavityCutter")
    positive_cutter = duplicate_object(cutter, "PositiveCavityCutter")

    bisect_keep_side(negative_half, working_split_axis, split_plane, keep_positive=False)
    bisect_keep_side(positive_half, working_split_axis, split_plane, keep_positive=True)
    bisect_keep_side(negative_part, working_split_axis, split_plane, keep_positive=False)
    bisect_keep_side(positive_part, working_split_axis, split_plane, keep_positive=True)
    bisect_keep_side(negative_cutter, working_split_axis, split_plane, keep_positive=False)
    bisect_keep_side(positive_cutter, working_split_axis, split_plane, keep_positive=True)

    boolean_op(negative_half, negative_cutter, "DIFFERENCE", solver="MANIFOLD")
    boolean_op(positive_half, positive_cutter, "DIFFERENCE", solver="MANIFOLD")
    if cfg.add_center_insert:
        center_cap_source = positive_cutter if cfg.center_insert_source == "CAVITY" else positive_part
        center_relief_source = part
        candidate_negative, candidate_positive, candidate_insert, candidate_span, center_insert_label = (
            build_manual_center_insert_candidates(
                mold_body,
                cutter,
                center_cap_source,
                center_relief_source,
                split_axis=working_split_axis,
                split_plane=split_plane,
                mold_bounds=mold_bounds,
                cutter_bounds=cutter_bounds,
                cfg=cfg,
            )
        )
        delete_object(negative_half)
        delete_object(positive_half)
        negative_half = candidate_negative
        positive_half = candidate_positive
        center_insert = candidate_insert
        center_insert_orientation = center_insert_label
        perimeter_lock_width_used = candidate_span
    elif cfg.add_perimeter_lock:
        accepted_exact = False
        for label, reinforce in (
            ("manual-exact reinforced", True),
            ("manual-exact", False),
        ):
            exact_candidates = build_manual_exact_perimeter_lock_candidates(
                mold_body,
                cutter,
                cavity_cap_source=positive_cutter,
                split_axis=working_split_axis,
                split_plane=split_plane,
                mold_bounds=mold_bounds,
                cfg=cfg,
                reinforce=reinforce,
            )
            if exact_candidates is None:
                continue
            exact_negative, exact_positive, exact_lock_width = exact_candidates
            negative_issue = _lock_candidate_geometry_issue(negative_half, exact_negative)
            positive_issue = _lock_candidate_geometry_issue(positive_half, exact_positive)
            if negative_issue is None and positive_issue is None:
                delete_object(negative_half)
                delete_object(positive_half)
                negative_half = exact_negative
                positive_half = exact_positive
                perimeter_lock_width_used = exact_lock_width
                print(f"Perimeter lock {label}: using cavity-derived silhouette sequence.")
                accepted_exact = True
                break

            print(
                f"Perimeter lock {label} rejected:"
                f" negative={negative_issue or 'ok'};"
                f" positive={positive_issue or 'ok'}"
            )
            delete_object(exact_negative)
            delete_object(exact_positive)

        if not accepted_exact:
            perimeter_lock_width_used = add_perimeter_lock(
                negative_half,
                positive_half,
                working_split_axis,
                split_plane,
                mold_bounds,
                negative_cutter,
                positive_cutter,
                negative_part,
                positive_part,
                cfg,
                )
    add_parting_channels(
        negative_half,
        positive_half,
        working_split_axis,
        split_plane,
        cutter_bounds,
        mold_bounds,
        negative_cutter,
        positive_cutter,
        cfg,
    )
    if cfg.add_alignment_keys:
        add_alignment_keys(negative_half, positive_half, working_split_axis, split_plane, cutter_bounds, mold_bounds, cfg)
        for keyed_half in (negative_half, positive_half):
            voxel_remesh_object(keyed_half, min(recommended_voxel_size(get_world_bounds(keyed_half), clearance=cfg.clearance), 0.12))
            clean_mesh_for_boolean(keyed_half)

    finalize_mold_half(negative_half)
    finalize_mold_half(positive_half)

    for helper in (cutter, mold_body, negative_part, positive_part, negative_cutter, positive_cutter):
        delete_object(helper)

    negative_orientation = None
    positive_orientation = None
    if cfg.auto_rotate_cavity_up:
        if center_insert is not None:
            center_insert_orientation = "assembly"
        else:
            negative_orientation = orient_half_for_print(negative_half, working_split_axis, keep_positive=False)
            positive_orientation = orient_half_for_print(positive_half, working_split_axis, keep_positive=True)
            if center_insert is not None:
                center_insert_orientation = orient_object_for_print(center_insert)

    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    negative_path = cfg.output_dir / cfg.export_negative_name
    positive_path = cfg.output_dir / cfg.export_positive_name
    center_path = cfg.output_dir / cfg.export_center_name if center_insert is not None else None
    validation = {"negative": require_solid(negative_half), "positive": require_solid(positive_half)}
    if center_insert is not None:
        validation["center"] = require_solid(center_insert)
    export_stl(negative_half, negative_path)
    export_stl(positive_half, positive_path)
    if center_path is not None:
        export_stl(center_insert, center_path)
    preview_path = render_preview(part, negative_half, positive_half, cfg, center_insert=center_insert) if cfg.render_preview else None

    print("Done.")
    print(f"Input: {cfg.input_stl}")
    print(f"Split plane: {working_split_axis}={split_plane:.3f}")
    if cfg.parting_view:
        print(f"Parting selection: {cfg.parting_view}@{cfg.parting_angle_deg:.3f}deg")
    print(f"Exterior mode: {cfg.exterior_mode}")
    if alignment_keys_disabled_reason:
        print(alignment_keys_disabled_reason)
    if cfg.add_center_insert:
        if perimeter_lock_width_used > 0.0:
            print(
                f"Center insert shell: enabled "
                f"(source={cfg.center_insert_source.lower()} span={perimeter_lock_width_used:.3f} mm)"
            )
        else:
            print("Center insert shell: skipped (not enough safe wall around the split profile)")
    elif cfg.add_perimeter_lock:
        if perimeter_lock_width_used > 0.0:
            print(f"Perimeter lock: enabled ({perimeter_lock_width_used:.3f} mm width)")
        else:
            print("Perimeter lock: skipped (not enough safe wall around the split profile)")
    if negative_orientation and positive_orientation:
        orientation_text = f"negative={negative_orientation}, positive={positive_orientation}"
        if center_insert_orientation:
            orientation_text += f", center={center_insert_orientation}"
        print(f"Print orientations: {orientation_text}")
    print(f"Negative half: {negative_path}")
    print(f"Positive half: {positive_path}")
    if center_path is not None:
        print(f"Center insert: {center_path}")
    if preview_path is not None:
        print(f"Preview image: {preview_path}")
    if alignment_keys_disabled_reason:
        _WARNINGS.append(alignment_keys_disabled_reason)
    outputs = {"negative": str(negative_path), "positive": str(positive_path)}
    if center_path is not None:
        outputs["center"] = str(center_path)
    previews = {key: str(path) for key, path in angle_preview_paths.items()}
    if preview_path is not None:
        previews["ASSEMBLY"] = str(preview_path)
    if cfg.add_perimeter_lock and center_insert is None and perimeter_lock_width_used <= 0:
        _WARNINGS.append("Requested perimeter lock was skipped: no valid safe-wall candidate survived.")
    successful_manifest(cfg, outputs=outputs, previews=previews, validation=validation,
                        features={"perimeter_lock": center_insert is None and perimeter_lock_width_used > 0})
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    global _ACTIVE_CONFIG, _EXPORTED_BYTES
    cfg = parse_config(argv)
    _ACTIVE_CONFIG = cfg
    _EXPORTED_BYTES = 0
    _GEOMETRY_EVENTS.clear()
    _WARNINGS.clear()
    if not cfg.angle_preview_only:
        _WARNINGS.append("Voxel remeshing approximates geometry; topology validation does not certify dimensional fit or demoldability.")
    try:
        return _generate(cfg)
    except Exception as exc:
        write_artifact_manifest(cfg, {"status": "failed", "operation": "preview" if cfg.angle_preview_only else "generate",
                                     "error_kind": "center_insert" if isinstance(exc, CenterInsertError) else "generator",
                                     "error": str(exc)[:2000], "outputs": {}, "previews": {},
                                     "warnings": _WARNINGS, "geometry": {"events": _GEOMETRY_EVENTS}})
        raise
    finally:
        _ACTIVE_CONFIG = None


if __name__ == "__main__":
    raise SystemExit(main())
