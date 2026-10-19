import os
import tempfile
import unittest
from pathlib import Path

from mold_fixtures import FACES, VERTICES, png, stl

from e3p_mold.validation import confined_file, validate_png, validate_stl


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "fixture.stl"
        stl(self.path)

    def test_analytic_volume_bounds_and_unchanged_bytes(self):
        original = self.path.read_bytes()
        result = validate_stl(self.path, max_bytes=10000)
        self.assertAlmostEqual(result["volume"], 1 / 6)
        self.assertEqual(result["bounds"], [[0, 0, 0], [1, 1, 1]])
        self.assertEqual((result["bodies"], result["faces"]), (1, 4))
        self.assertFalse(result["modified"])
        self.assertEqual(self.path.read_bytes(), original)

    def test_open_inverted_inconsistent_degenerate_and_nonfinite_rejected(self):
        cases = [(VERTICES, FACES[:-1]), (VERTICES, [face[::-1] for face in FACES]),
                 (VERTICES, [FACES[0][::-1], *FACES[1:]]),
                 (VERTICES, [(0, 0, 1), *FACES[1:]]),
                 ([(float("nan"), 0, 0), *VERTICES[1:]], FACES)]
        for vertices, faces in cases:
            with self.subTest(vertices=vertices, faces=faces):
                stl(self.path, vertices, faces)
                with self.assertRaises(ValueError):
                    validate_stl(self.path, max_bytes=10000)

    def test_separate_and_point_touching_closed_solids_rejected(self):
        for second in [[(x + 3, y, z) for x, y, z in VERTICES],
                       [(-x, -y, -z) for x, y, z in VERTICES]]:
            inverted = second[0] == (0, 0, 0)
            faces = FACES + [tuple(index + 4 for index in (face[::-1] if inverted else face)) for face in FACES]
            stl(self.path, VERTICES + second, faces)
            with self.assertRaisesRegex(ValueError, "one face-connected body"):
                validate_stl(self.path, max_bytes=10000)

    def test_truncation_budget_and_cooperative_deadline(self):
        with self.assertRaises(ValueError):
            validate_stl(self.path, max_bytes=100)
        def expired():
            raise TimeoutError("fixture deadline")
        with self.assertRaises(TimeoutError):
            validate_stl(self.path, max_bytes=10000, check=expired)
        self.path.write_bytes(self.path.read_bytes()[:-1])
        with self.assertRaises(ValueError):
            validate_stl(self.path, max_bytes=10000)

    def test_png_validated_with_crc_and_complete_image_data(self):
        path = self.root / "view.png"
        png(path)
        self.assertEqual(validate_png(path, max_bytes=10000)["width"], 1)
        original = path.read_bytes()
        for data in (original[:-4], original[:45] + bytes([original[45] ^ 1]) + original[46:], b"not an image"):
            path.write_bytes(data)
            with self.assertRaises(ValueError):
                validate_png(path, max_bytes=10000)

    def test_artifact_paths_confined_and_links_rejected(self):
        self.assertEqual(confined_file(self.root, str(self.path), suffix=".stl", max_bytes=10000), self.path)
        with self.assertRaises(ValueError):
            confined_file(self.root, "fixture.stl", suffix=".stl", max_bytes=10000)
        other = self.root / "alias.stl"
        os.link(self.path, other)
        with self.assertRaises(ValueError):
            confined_file(self.root, str(other), suffix=".stl", max_bytes=10000)
