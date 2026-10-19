# Validation and limits

The author has successfully printed molds and cast silicone using the original
workflow. This maintenance release has software validation on generated models;
no new physical cast or dimensional measurement is claimed.

## Reproduce the geometry checks

```sh
python -m pip install -r requirements.txt .
python tools/verify_geometry.py --blender-exe /path/to/blender --output verification-output
python tools/make_demo.py --blender-exe /path/to/blender --output demo-output
```

Both commands invoke the packaged launcher and a real Blender subprocess.
Choose new output directories. They create their own synthetic STL inputs, so no
private models or downloaded assets are needed. The geometry regression uses
Blender 5.2.0 and writes a machine-readable `geometry-verification.json`.

| Check | Evidence and boundary |
| --- | --- |
| Analytic box | A 20 × 16 × 24 mm box with zero clearance and features disabled produces two closed halves with expected bounds and exactly 12,288 mm³ volume each. No repair is enabled. |
| Clearance | A requested 0.4 mm normal offset moves a sampled cavity wall from 8.0 to approximately 8.231 mm. This confirms movement in the intended direction, and demonstrates why the setting is not an exact distance guarantee. |
| Channels | Independent ray/triangle intersections check both injection and vent centerlines through exported triangles, with blocked paths in the uncut control. This is a sampled check, not exhaustive channel certification. |
| Output budget | A deliberately insufficient byte budget produces a failed result with no reported success artifacts. |
| Input and remesh rejection | Disconnected input bodies and an excessive estimated voxel grid produce failed results with no reported success artifacts. |
| Feature demonstration | A generated ellipsoid exercises alignment keys, perimeter locking, channels, final STL validation and rendering. The demo explicitly permits repair and records removed geometry; it does not establish physical fit. |

The launcher independently reads the exported binary STL. It checks finite
coordinates, nondegenerate triangles, closed consistently oriented edges,
one face-connected body, connected incident-face fans and positive signed volume.
It performs no repair. These checks do not detect every self-intersection or
prove wall thickness, clearance, channel connectivity or demoldability.

## Ordinary regression suite

```sh
python -m pip install -r requirements-dev.txt -e .
python -m ruff check .
python -m unittest discover -s tests -v
python -m build
python -m pip check
```

The suite covers polygon geometry, malformed artifact rejection, retry ownership,
missing/stale output, output budgets, timeout/cancellation, process descendants,
bounded logs, Cura worker state, Qt controls and explicit-destination installation.
Failure-injection tests use controlled child programs instead of requiring a
full Blender run. Cura harness tests supply application services; see the separate
[Cura validation scope](cura.md) for checks against the application itself.

GitHub Actions runs the ordinary suite on Windows and Linux with Python 3.12 and
3.14. A separate Linux job downloads a digest-verified Blender 5.2.0 and runs the
analytic geometry regression through the installed package. The feature render
is a reproducible local demonstration, not part of the hosted geometry job.

## Geometry and resource tradeoffs

Voxel remeshing and normal offsets are approximations. Destructive repair can
remove fragments or seal holes and thereby change intended features. It requires
explicit opt-in, and repair events remain in the result report. A successful
topology check cannot establish that the chosen repair was appropriate.

The launcher defaults to a 180-second operation timeout, 64 MiB input and 256 MiB
output limits. Log streams are capped at 1 MiB each. Output-directory size is
polled, so temporary disk overshoot is possible. The generator estimates a
64-million-cell remesh ceiling; this is not a hard memory limit on Blender or
its Boolean algorithms. Choose a coarser voxel size or simpler model when needed.

Center-insert fallback is observable in the result, and an omitted insert is
never inferred from an old file. Its retry paths have failure-injection tests;
the current real-Blender matrix does not include a successful center-insert case.
Alignment keys at unsupported parting angles
are disabled with a warning. Inspect every new model and perform a physical fit
and casting trial before relying on it.
