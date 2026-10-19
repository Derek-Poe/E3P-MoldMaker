# Cura extension

The extension adds **Extensions → E3P** with generation, settings, and cancellation commands. It keeps the original interactive angle picker: choose one of six rendered views, drag the parting line, or enter its angle numerically.

## Install explicitly

Use the source checkout and the external Python environment described in the main README. Cura's bundled Python does not need NumPy, trimesh, or this project's package installed. The adapter uses Qt and Uranium from Cura and launches a separate configured Python interpreter.

Close Cura. Choose the intended Cura version's user plugin directory yourself, then install into its **E3PBlenderMold** child directory:

```powershell
.venv\Scripts\python.exe tools/install_cura_extension.py --destination "C:\path\to\Cura\plugins\E3PBlenderMold"
```

On Linux, use `.venv/bin/python` and an explicit Linux destination. The parent directory must already exist. The installer does not search for a profile, create one, copy local defaults, or overwrite an existing plugin by default. To replace a previous installation, add `--replace`; it first writes and verifies a sibling ZIP backup containing the old plugin's files. If installation fails after moving the old directory, it attempts to restore that directory. A concurrent installer is unsupported; keep Cura closed during installation. Symlinked destinations and junctions are rejected.

Restart Cura, open **E3P → Blender Mold Settings**, and set:

- **Project Root:** the checkout containing `run_blender_mold.py`.
- **Blender Executable:** the full path to the separately installed Blender executable.
- **External Python:** the project environment's Python executable. If blank, the adapter checks `.venv` and `.venv-blender` in the project root. It never falls back to Cura's interpreter.

Keep that checkout and environment available after installing the extension. On Linux, select the virtual environment's `bin/python` path; the adapter preserves its symlink so Python uses that environment's dependencies.

The child interpreter starts in Python's isolated mode. The adapter removes Cura/AppImage Python and Qt path injection from the child's environment and restores pre-AppImage library paths. A small child-only bootstrap clears inherited Windows DLL search overrides before running the project shim; it does not change Cura's process-global DLL search state.

## Input and job behavior

**The input is the original STL on disk.** Cura-side translation, rotation, scaling, duplication, and mesh edits are not exported or applied. A selected model may suggest its original filename; always check the displayed source. To use edited geometry, export it explicitly to a new STL and choose that file.

Both **Pick From Images** and generation run in background jobs. Only one operation may run at a time. Preview disables source/angle changes and acceptance while it runs; use **Cancel Preview**, close its dialog, or choose **E3P → Cancel Active Operation** to stop. The backend has a 180-second timeout; the adapter allows 10 additional seconds for process cleanup. Cancellation targets the owned process tree, with Windows Job Objects and Linux descendant tracking. Other POSIX platforms have only session-level termination and are untested.

Each operation creates a fresh run directory below `e3p_studio_runs/cura_blender_extension/`, with its outcome report, output directory, and bounded logs. Each subprocess log stream is capped at 1 MiB on disk; diagnostic tails are bounded separately. Reports and artifact paths are checked before the UI consumes them. Missing or unreadable preview views cannot be selected. A failed, timed-out, or cancelled job queues no imports.

Generation imports only the artifacts explicitly named by a successful structured report. A center insert omitted after a fallback is never inferred from an existing file. The success message says **queued for import**: Cura loads those files asynchronously and may still report an import failure. Check the scene and inspect the resulting geometry before slicing or manufacturing.

The UI retains a conservative 0–0.5 mm clearance range and disables alignment keys for non-90-degree parting angles. It does not opt into mesh repair. Models requiring explicit repair must use the CLI's `--allow-mesh-repair` option and have the resulting geometry inspected. These controls are not guarantees of fit, demoldability, or safe machining. The CLI exposes additional geometry parameters; consult the main README for geometric validation and limitations.

## Compatibility evidence

The manifest declares SDK `8.12.0`, the SDK in [Cura 5.12.1](https://github.com/Ultimaker/Cura/blob/5.12.1/cura/ApplicationMetadata.py). Cura treats this as a minimum minor version within SDK major 8, rather than an exact version lock. Later Cura releases require their own compatibility check.

The adapter uses Python 3.10-compatible syntax and PyQt6. Automated checks cover real offscreen Qt widgets with mocked Cura services, report validation, real child-process timeout/cancellation, single-job cleanup, stale-center exclusion, interactive angle synchronization, and installer preservation/rollback. The shared process helper is checked byte-for-byte against the CLI's copy. Those widget tests alone do not establish a full Cura application test or physical print result.

**Live Cura compatibility remains unverified for this release.** Isolated launches of the official Cura 5.12.1 Linux runtime did not reach plugin initialization in the validation environment. No successful full Cura registration, scene import, or Windows Cura application run is claimed. The CLI's actual Blender geometry checks and the adapter's automated widget/process checks are separate evidence. The SDK declaration identifies the intended integration target; try the extension in a separate Cura profile before relying on it for a working setup.
