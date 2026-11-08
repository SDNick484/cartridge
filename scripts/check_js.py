"""Syntax-check each static JS file with `node --check` (as an ES module). Stdlib only."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src" / "cartridge" / "static"


def main() -> int:
    node = shutil.which("node")
    if node is None:
        print("node is not on PATH; install Node.js to syntax-check the browser JS.", file=sys.stderr)
        return 2
    failed = False
    for path in sorted(STATIC.glob("*.js")):
        result = subprocess.run(
            [node, "--input-type=module", "--check"],
            input=path.read_bytes(),
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            print(f"ok {path.name}")
        else:
            failed = True
            print(f"FAIL {path.name}\n{result.stderr.decode(errors='replace')}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
