"""Write a small deterministic synthetic STL; no downloaded model is used."""
from __future__ import annotations

import argparse
import math
import struct
from pathlib import Path


def triangles(shape: str = "ellipsoid"):
    if shape == "box":
        vertices = [(x, y, z) for z in (-12, 12) for y in (-8, 8) for x in (-10, 10)]
        quads = [(0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4),
                 (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)]
        return [[vertices[a], vertices[b], vertices[c]] for a, b, c, d in quads
                for a, b, c in ((a, b, c), (a, c, d))]
    golden = (1 + math.sqrt(5)) / 2
    vertices = [(-1, golden, 0), (1, golden, 0), (-1, -golden, 0), (1, -golden, 0),
                (0, -1, golden), (0, 1, golden), (0, -1, -golden), (0, 1, -golden),
                (golden, 0, -1), (golden, 0, 1), (-golden, 0, -1), (-golden, 0, 1)]

    def unit(point):
        length = math.sqrt(sum(x * x for x in point))
        return tuple(x / length for x in point)

    vertices = [unit(v) for v in vertices]
    faces = [(0, 11, 5), (0, 5, 1), (0, 1, 7), (0, 7, 10), (0, 10, 11),
             (1, 5, 9), (5, 11, 4), (11, 10, 2), (10, 7, 6), (7, 1, 8),
             (3, 9, 4), (3, 4, 2), (3, 2, 6), (3, 6, 8), (3, 8, 9),
             (4, 9, 5), (2, 4, 11), (6, 2, 10), (8, 6, 7), (9, 8, 1)]
    for _ in range(2):
        midpoints = {}

        def midpoint(a, b):
            key = tuple(sorted((a, b)))
            if key not in midpoints:
                midpoints[key] = len(vertices)
                vertices.append(unit(tuple((x + y) / 2 for x, y in zip(vertices[a], vertices[b]))))
            return midpoints[key]

        refined = []
        for a, b, c in faces:
            ab, bc, ca = midpoint(a, b), midpoint(b, c), midpoint(c, a)
            refined.extend(((a, ab, ca), (b, bc, ab), (c, ca, bc), (ab, bc, ca)))
        faces = refined
    vertices = [(x * 10, y * 7.5, z * 15) for x, y, z in vertices]
    return [[vertices[a], vertices[b], vertices[c]] for a, b, c in faces]



def write_fixture(path: Path, shape: str = "ellipsoid") -> None:
    write_facets(path, triangles(shape))


def write_facets(path: Path, faces) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(b"E3P MoldMaker synthetic fixture".ljust(80, b"\0"))
        stream.write(struct.pack("<I", len(faces)))
        for triangle in faces:
            stream.write(struct.pack("<12fH", 0, 0, 0, *(v for point in triangle for v in point), 0))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Fresh directory for synthetic input and actual CLI results.")
    parser.add_argument("--blender-exe", required=True)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    source = args.output / "synthetic-ellipsoid.stl"
    write_fixture(source)
    from _pipeline import generate
    code, report, elapsed = generate(source, args.output / "generated", args.blender_exe,
                                    ["--voxel-size", "0.25", "--allow-mesh-repair", "--render-preview",
                                     "--preview-width", "1200", "--preview-height", "900"], timeout=args.timeout)
    if code != 0 or report.get("status") != "success":
        raise SystemExit("Demo failed; inspect generated/report.json and CLI logs.")
    print(f"Generated synthetic mold and render in {args.output / 'generated'} ({elapsed:.2f}s).")
