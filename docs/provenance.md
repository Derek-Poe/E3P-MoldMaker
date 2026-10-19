# Provenance and release changes

This project was extracted from Derek Poe's E3P 3D-printing workspace. The active
source was `create_mold_v2.py`, the Python launcher, and the `E3PBlenderMold` Cura
extension. Higher-numbered experimental scripts were not used as the release baseline.

Preserved and adapted work includes hull/offset geometry, split-solid cavity
construction, keyed and locking interfaces, center-insert candidates, channels,
parting-view selection and print-orientation scoring. The original workflow has
been used for printed molds and silicone casting, as reported by the author.
No physical cast from this maintenance release is represented as newly tested.

Release maintenance separates the runtime from unrelated printer/web/device
integrations, adds explicit dependencies and versioned result manifests, corrects
retry/output ownership, adds cancellation and time limits, and makes geometric
repair and resource tradeoffs visible. New checks cover injected failures and
generated geometry rather than relying on source-string assertions.

Private source snapshots are retained outside this repository. Original model
assets, machine-specific defaults, deployment settings and runtime logs were not
copied into the public package. The demo fixture is generated from primitives.

The repository contains this project's source under MIT. It calls separately
installed Blender and Cura; it does not redistribute either application or claim
their geometry APIs as original implementation. Runtime dependencies retain
their respective licenses.
