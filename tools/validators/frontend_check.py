"""Frontend gate (Wave 5A, D-24): the Express user interface builds and its tests pass.

Steps:
  1. Node.js >= 20 is installed;
  2. dependencies are installed exactly as locked (``npm ci`` if node_modules is missing
     or older than package-lock.json);
  3. every JavaScript file parses (``node --check``);
  4. the tests pass (``node --test``).

Usage:  python tools/validators/frontend_check.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"
MIN_NODE_MAJOR = 20


def _run(cmd: list[str]) -> int:
    print("$ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=FRONTEND, check=False).returncode


def main() -> int:
    node = shutil.which("node")
    npm = shutil.which("npm")
    if node is None or npm is None:
        print("frontend: FAIL - Node.js (node and npm) is not installed; install Node.js 20 LTS+")
        return 1
    version = subprocess.run([node, "--version"], capture_output=True, text=True, check=False)
    major = int(version.stdout.strip().lstrip("v").split(".")[0] or 0)
    if major < MIN_NODE_MAJOR:
        print(f"frontend: FAIL - Node.js {version.stdout.strip()} is too old; need >= 20")
        return 1

    lock = FRONTEND / "package-lock.json"
    modules = FRONTEND / "node_modules" / ".package-lock.json"
    stale = not modules.exists() or modules.stat().st_mtime < lock.stat().st_mtime
    if stale and _run([npm, "ci", "--no-audit", "--no-fund"]) != 0:
        print("frontend: FAIL - npm ci failed")
        return 1

    sources = sorted(
        str(p.relative_to(FRONTEND)) for d in ("src", "test") for p in (FRONTEND / d).rglob("*.js")
    )
    for src in sources:
        if subprocess.run([node, "--check", src], cwd=FRONTEND, check=False).returncode != 0:
            print(f"frontend: FAIL - {src} does not parse")
            return 1

    env = dict(os.environ)
    env.pop("NODE_OPTIONS", None)
    code = subprocess.run([node, "--test"], cwd=FRONTEND, env=env, check=False).returncode
    if code != 0:
        print("frontend: FAIL - tests failed")
        return 1
    print(f"frontend: PASS - Node.js {version.stdout.strip()}, {len(sources)} file(s) checked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
