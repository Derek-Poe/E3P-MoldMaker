"""Source-checkout entry point for Cura and the former launcher arguments."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from e3p_mold.cli import main

if __name__ == "__main__":
    arguments = sys.argv[1:]
    if not arguments or arguments[0] not in ("generate", "preview"):
        arguments.insert(0, "generate")
    raise SystemExit(main(arguments))
