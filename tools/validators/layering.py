"""Run import-linter layering contracts.

``python -m importlinter.cli`` does not execute the command (it only imports the
module and exits 0), so this wrapper calls the real entry point. The guard
self-tests prove this wrapper fails on planted violations.

Usage:
    python tools/validators/layering.py --config backend/pyproject.toml
"""

from __future__ import annotations

import sys

from importlinter.cli import lint_imports_command

if __name__ == "__main__":
    sys.exit(lint_imports_command())
