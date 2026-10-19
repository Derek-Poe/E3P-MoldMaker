"""Copy the adapter only to an explicit plugin destination; never find a profile."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

PLUGIN_NAME = "E3PBlenderMold"
FILES = ("__init__.py", "E3PBlenderMold.py", "plugin.json", "worker.py", "process.py", "external_python.py")
SOURCE = Path(__file__).resolve().parents[1] / "cura_extension" / PLUGIN_NAME


def install(destination: Path, *, replace: bool = False, source: Path = SOURCE) -> dict:
    """Stage a fixed source allowlist, preserve an existing plugin before replacement."""
    destination = Path(os.path.abspath(destination.expanduser()))
    if destination.name != PLUGIN_NAME:
        raise ValueError("Destination must be the explicit E3PBlenderMold directory, not a Cura profile or plugins parent.")
    # Do not traverse junctions/symlinks into an unintended profile or filesystem.
    if any(p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()) for p in (destination, *destination.parents)):
        raise ValueError("Destination and its parents must not be symbolic links or junctions.")
    if not destination.parent.is_dir():
        raise ValueError("Destination parent must already exist; no profile is created automatically.")
    if source.resolve() == destination.resolve() or source.resolve() in destination.resolve().parents:
        raise ValueError("Destination must be separate from the source plugin.")
    if destination.exists():
        if not replace:
            raise FileExistsError("Destination exists. Use --replace to preserve it as a sibling backup and replace it.")
        if not destination.is_dir() or not (destination / "plugin.json").is_file():
            raise ValueError("Refusing to replace a destination that is not an existing plugin directory.")
    for name in FILES:
        candidate = source / name
        if not candidate.is_file() or candidate.is_symlink():
            raise ValueError("Missing or linked packaged plugin file: " + name)
    metadata = json.loads((source / "plugin.json").read_text(encoding="utf-8"))
    if metadata.get("name") != "E3P Blender Mold" or metadata.get("version") != "0.2.0":
        raise ValueError("Unexpected source plugin metadata.")
    stage = Path(tempfile.mkdtemp(prefix=".e3p-install-", dir=destination.parent))
    backup = None
    previous = None
    installed = False
    try:
        for name in FILES:
            shutil.copy2(source / name, stage / name)
        if destination.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = destination.with_name(PLUGIN_NAME + ".backup-" + stamp + ".zip")
            if backup.exists():
                raise FileExistsError("Backup destination already exists.")
            entries = sorted(destination.rglob("*"))
            if any(p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()) for p in entries):
                raise ValueError("Existing plugin contains links; back it up manually before installation.")
            # A ZIP backup cannot be discovered as a second installed extension.
            with zipfile.ZipFile(backup, "x", compression=zipfile.ZIP_DEFLATED) as archive:
                for file in entries:
                    archive.write(file, str(file.relative_to(destination)))
            with zipfile.ZipFile(backup) as archive:
                if archive.testzip() is not None:
                    raise OSError("Backup archive verification failed.")
                for file in entries:
                    if file.is_file() and archive.read(str(file.relative_to(destination)).replace("\\", "/")) != file.read_bytes():
                        raise OSError("Backup archive bytes differ from the existing plugin.")
            previous = stage / "previous"
            destination.rename(previous)
        try:
            # os.rename refuses an existing directory on Windows; on POSIX guard
            # against replacing a concurrently created empty destination as well.
            if destination.exists() or destination.is_symlink():
                raise FileExistsError("Destination appeared during installation.")
            # Keep the rollback directory out of the installed plugin.
            if previous is None:
                stage.rename(destination)
            else:
                payload = stage / "payload"
                payload.mkdir()
                for name in FILES:
                    (stage / name).rename(payload / name)
                payload.rename(destination)
            installed = True
        except Exception:
            if previous is not None and not destination.exists() and not destination.is_symlink():
                previous.rename(destination)
            raise
        return {"destination": str(destination), "backup": str(backup) if backup else None,
                "files": list(FILES), "version": metadata["version"]}
    finally:
        # Never discard the old directory if a concurrent change blocked rollback.
        if stage.exists() and (installed or previous is None or not previous.exists()):
            shutil.rmtree(stage)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True,
                        help="Exact new plugin directory ending in E3PBlenderMold; parent must already exist.")
    parser.add_argument("--replace", action="store_true", help="Back up an existing plugin before replacement.")
    args = parser.parse_args(argv)
    try:
        result = install(args.destination, replace=args.replace)
    except (OSError, ValueError) as exc:
        parser.exit(2, "Install failed: %s\n" % exc)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
