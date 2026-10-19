"""Run the original-derived pure algorithms without importing Blender or executing a scene."""
import argparse
import ast
import math
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src/e3p_mold/blender/generate.py"
NAMES = {"convex_hull_2d", "polygon_area", "_line_intersection", "offset_convex_polygon",
         "_polygon_contains_point", "_positive_float", "_non_negative_float", "_finite_float",
         "_voxel_size", "_output_name", "angle_preview_pose", "parting_rotation_for_view", "canonical_split_axis_for_view"}
module = ast.parse(SOURCE.read_text(encoding="utf-8"))
definitions = [node for node in module.body if isinstance(node, ast.FunctionDef) and node.name in NAMES]
namespace = {"argparse": argparse, "isfinite": math.isfinite, "pi": math.pi, "Iterable": list}
exec(compile(ast.Module(body=definitions, type_ignores=[]), str(SOURCE), "exec"), namespace)


class GeometryMathTests(unittest.TestCase):
    def test_all_camera_views_map_drawn_line_into_split_plane(self):
        def rotate(vector, angles):
            # Blender XYZ Euler: apply X, then Y, then Z, using independent elementary rotations.
            x, y, z = vector
            rx, ry, rz = angles
            y, z = y * math.cos(rx) - z * math.sin(rx), y * math.sin(rx) + z * math.cos(rx)
            x, z = x * math.cos(ry) + z * math.sin(ry), -x * math.sin(ry) + z * math.cos(ry)
            x, y = x * math.cos(rz) - y * math.sin(rz), x * math.sin(rz) + y * math.cos(rz)
            return x, y, z

        for view in ("TOP", "BOTTOM", "FRONT", "BACK", "LEFT", "RIGHT"):
            location, camera_rotation = namespace["angle_preview_pose"](view, 1.0)
            forward = rotate((0, 0, -1), camera_rotation)
            self.assertAlmostEqual(sum(a * b for a, b in zip(forward, location)), -1.0)
            for angle in (-170, -30, 0, 30, 60, 90, 150, 180):
                with self.subTest(view=view, angle=angle):
                    alpha = math.radians(angle)
                    line_world = rotate((math.cos(alpha), math.sin(alpha), 0), camera_rotation)
                    rotated = rotate(line_world, namespace["parting_rotation_for_view"](view, angle))
                    split_index = 0 if namespace["canonical_split_axis_for_view"](view) == "X" else 1
                    self.assertAlmostEqual(rotated[split_index], 0.0, places=12)

    def test_rotated_polygon_and_winding(self):
        polygon = [(0, 2), (2, 0), (0, -2), (-2, 0)]
        for winding in (polygon, polygon[::-1]):
            for point in ((0.2, 0.1), (-0.2, -0.1), (0, 0)):
                self.assertTrue(namespace["_polygon_contains_point"](point, winding))
            for point in ((3, 0.1), (-3, -0.1), (0.1, 3)):
                self.assertFalse(namespace["_polygon_contains_point"](point, winding))

    def test_concave_polygon(self):
        polygon = [(0, 0), (3, 0), (3, 1), (1, 1), (1, 3), (0, 3)]
        for winding in (polygon, polygon[::-1]):
            self.assertTrue(namespace["_polygon_contains_point"]((0.5, 2), winding))
            self.assertFalse(namespace["_polygon_contains_point"]((2, 2), winding))

    def test_hull_duplicates_collinear_and_area(self):
        points = [(0, 0), (2, 0), (1, 0), (2, 2), (0, 2), (1, 1), (0, 0)]
        hull = namespace["convex_hull_2d"](points)
        self.assertEqual(set(hull), {(0, 0), (2, 0), (2, 2), (0, 2)})
        self.assertAlmostEqual(namespace["polygon_area"](hull), 4.0)
        self.assertEqual(len(namespace["convex_hull_2d"]([(0, 0), (1, 1), (2, 2)])), 2)

    def test_offset_square_known_bounds_both_windings(self):
        square = [(0, 0), (2, 0), (2, 2), (0, 2)]
        for winding in (square, square[::-1]):
            self.assertEqual(set(namespace["offset_convex_polygon"](winding, 0.5)),
                             {(-0.5, -0.5), (2.5, -0.5), (2.5, 2.5), (-0.5, 2.5)})
        with self.assertRaises(RuntimeError):
            namespace["offset_convex_polygon"]([(0, 0), (1, 1)], 1)

    def test_numeric_parameters_reject_nonfinite(self):
        for name in ("_positive_float", "_non_negative_float", "_finite_float", "_voxel_size"):
            for value in ("nan", "inf", "-inf", "1e999"):
                with self.subTest(name=name, value=value), self.assertRaises(argparse.ArgumentTypeError):
                    namespace[name](value)
        for value in ("0.019", "2.01"):
            with self.assertRaises(argparse.ArgumentTypeError):
                namespace["_voxel_size"](value)
        self.assertEqual(namespace["_voxel_size"]("0.4"), 0.4)

    def test_output_names_cannot_escape(self):
        for name in ("../a.stl", "a/b.stl", "a\\b.stl", "C:a.stl", "..", "a\0.stl", "a."):
            with self.subTest(name=name), self.assertRaises(argparse.ArgumentTypeError):
                namespace["_output_name"](name)
        self.assertEqual(namespace["_output_name"]("mold_left.stl"), "mold_left.stl")


if __name__ == "__main__":
    unittest.main()
