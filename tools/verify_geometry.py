"""Actual-CLI synthetic geometry regression; requires the project's validation dependencies."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import trimesh
from _pipeline import generate
from make_demo import triangles, write_facets, write_fixture


def solid(path):
    mesh = trimesh.load_mesh(path, force="mesh")
    assert np.isfinite(mesh.vertices).all(), "Nonfinite output vertex"
    assert mesh.is_watertight and mesh.is_winding_consistent and mesh.is_volume, "Output is not an oriented closed solid"
    parents = list(range(len(mesh.faces)))

    def find(face):
        while parents[face] != face:
            parents[face] = parents[parents[face]]
            face = parents[face]
        return face

    edges = {}
    for face_index, face in enumerate(mesh.faces):
        for a, b in zip(face, (face[1], face[2], face[0])):
            key = (min(int(a), int(b)), max(int(a), int(b)))
            previous = edges.get(key)
            if previous is None:
                edges[key] = face_index
            else:
                parents[find(face_index)] = find(previous)
    assert len({find(face) for face in range(len(mesh.faces))}) == 1, "Expected exactly one nonempty edge-connected body"
    return mesh


def ray_distances(mesh, start, direction):
    """Independent vectorized Moller-Trumbore intersection, without an rtree dependency."""
    start = np.asarray(start, dtype=float)
    direction = np.asarray(direction, dtype=float)
    triangles = mesh.triangles
    edge1 = triangles[:, 1] - triangles[:, 0]
    edge2 = triangles[:, 2] - triangles[:, 0]
    pvec = np.cross(np.broadcast_to(direction, edge2.shape), edge2)
    determinant = np.einsum("ij,ij->i", edge1, pvec)
    valid = np.abs(determinant) > 1e-10
    inverse = np.zeros_like(determinant)
    inverse[valid] = 1 / determinant[valid]
    tvec = start - triangles[:, 0]
    u = np.einsum("ij,ij->i", tvec, pvec) * inverse
    qvec = np.cross(tvec, edge1)
    v = np.einsum("j,ij->i", direction, qvec) * inverse
    distance = np.einsum("ij,ij->i", edge2, qvec) * inverse
    valid &= (u >= -1e-8) & (v >= -1e-8) & (u + v <= 1 + 1e-8) & (distance > 1e-6)
    return distance[valid]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blender-exe", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    fixture = root / "synthetic-box.stl"
    write_fixture(fixture, "box")
    common = ["--voxel-size", "0.4", "--exterior-mode", "BOX", "--no-alignment-keys",
              "--no-perimeter-lock", "--no-render-preview", "--no-auto-rotate-cavity-up", "--no-allow-mesh-repair"]
    code, baseline, baseline_seconds = generate(fixture, root / "analytic", args.blender_exe,
        [*common, "--clearance", "0", "--no-injection-site", "--no-vent"], timeout=args.timeout)
    assert code == 0 and baseline.get("status") == "success", "Analytic CLI generation failed"
    meshes = {name: solid(path) for name, path in baseline["outputs"].items()}
    ideal_volume = (32 * 28 * 36 - 20 * 16 * 24) / 2
    expected = {"negative": [[-16, -14, -18], [0, 14, 18]],
                "positive": [[0, -14, -18], [16, 14, 18]]}
    assert set(meshes) == set(expected), "Expected exactly two mold halves"
    for name, mesh in meshes.items():
        assert np.allclose(mesh.bounds, expected[name], atol=1e-4), "Known outer bounds changed"
        assert abs(mesh.volume - ideal_volume) < 0.5, "Known zero-clearance cavity volume changed"
    baseline_inner = min(ray_distances(meshes["positive"], [3, 0, 0], [0, 1, 0]))
    assert abs(baseline_inner - 8) < 1e-4, "Baseline cavity wall is misplaced"

    code, channels, channel_seconds = generate(fixture, root / "channels", args.blender_exe,
        [*common, "--clearance", "0.4", "--injection-site", "--vent"], timeout=args.timeout)
    assert code == 0 and channels.get("status") == "success", "Channel CLI generation failed"
    channel_meshes = {name: solid(path) for name, path in channels["outputs"].items()}
    cavity_wall = min(ray_distances(channel_meshes["positive"], [3, 0, 0], [0, 1, 0]))
    assert 8.05 < cavity_wall < 8.9, "Requested clearance did not expand the sampled cavity wall"
    # This intentionally verifies a sampled wall; it does not claim a global offset bound.
    events = channels.get("geometry", {}).get("events", [])
    channel_events = [event for event in events if event.get("kind") == "channel"]
    assert len(channel_events) == 2, "Expected two reported channel paths"
    for event in channel_events:
        start, end = np.array(event["start"]), np.array(event["end"])
        assert np.all(np.abs(end) < np.array([10, 8, 12])), "Channel does not terminate inside known cavity"
        hits = ray_distances(channel_meshes[event["half"]], start, end - start)
        assert not np.any(hits < 1 - 1e-5), "Channel centerline is blocked by output solid"
        control_hits = ray_distances(meshes[event["half"]], start, end - start)
        assert np.any(control_hits < 1 - 1e-5), "Control mold should block the uncut channel path"

    code, budget, budget_seconds = generate(fixture, root / "output-budget", args.blender_exe,
        [*common, "--clearance", "0", "--no-injection-site", "--no-vent", "--max-output-bytes", "100"], timeout=args.timeout)
    assert code != 0 and budget.get("status") == "failed" and not budget.get("outputs"), "Output budget must fail closed"
    separate = root / "two-separate-boxes.stl"
    facets = triangles("box")
    write_facets(separate, facets + [[(x + 40, y, z) for x, y, z in face] for face in facets])
    code, multi, multi_seconds = generate(separate, root / "multiple-bodies", args.blender_exe,
        [*common, "--no-injection-site", "--no-vent"], timeout=args.timeout)
    assert code != 0 and multi.get("status") == "failed" and not multi.get("outputs"), "Multiple input bodies must not be silently discarded"
    code, grid, grid_seconds = generate(fixture, root / "voxel-budget", args.blender_exe,
        [*common, "--voxel-size", "0.02", "--no-injection-site", "--no-vent"], timeout=args.timeout)
    assert code != 0 and grid.get("status") == "failed" and not grid.get("outputs"), "Oversized voxel grid must fail before remeshing"
    result = {"passed": True, "scope": "Synthetic analytic box and sampled clearance/channel tests; no fit or physical casting certification",
              "cases": [{"name": "analytic_box", "elapsed_seconds": baseline_seconds,
                         "expected_half_volume": ideal_volume,
                         "measured_half_volumes": {name: float(mesh.volume) for name, mesh in meshes.items()}},
                        {"name": "clearance_and_channels", "elapsed_seconds": channel_seconds,
                         "sampled_cavity_wall_y": float(cavity_wall), "verified_channel_paths": len(channel_events)},
                        {"name": "output_budget_rejection", "elapsed_seconds": budget_seconds},
                        {"name": "multiple_input_bodies_rejection", "elapsed_seconds": multi_seconds},
                        {"name": "voxel_grid_budget_rejection", "elapsed_seconds": grid_seconds}]}
    (root / "geometry-verification.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
