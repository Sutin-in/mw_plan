"""Repo-local check: the single gate every wave must pass (spec §45).

Runs, in order, and reports PASS/FAIL per gate:

  1. ruff lint
  2. ruff format --check
  3. mypy (strict on ppr.domain / ppr.integration)
  4. import-linter layering contracts
  5. phase_guard (no PlanFin / GL / PO creation / payment / HOSxP write-back)
  6. database (test DB configured, named *_test, reachable, PostgreSQL 16)
  7. traceability (every spec reference in tests exists in the spec, and every acceptance
     criterion of spec §40 is referenced by a test - Wave 10)
  8. frontend (Node.js >= 20; Express user interface parses and its tests pass, D-24)
  9. pytest (unit, property, guard self-tests, database/API tests)

Usage (from the repository root, Windows or Linux):
    python tools/check.py
    python tools/check.py --write-traceability   # also regenerate docs/traceability.md
    python tools/check.py --acceptance-report    # also: 10. acceptance - per-criterion result
                                                 # of this run in docs/acceptance_report.md
                                                 # (or --acceptance-report=PATH)

Exit code 0 only if every gate passes.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
PY = sys.executable


JUNIT = BACKEND / ".pytest_tmp" / "junit.xml"


def _gates(write_trace: bool, acceptance: Path | None) -> list[tuple[str, list[str], Path]]:
    # Wave 10: every acceptance criterion (spec §40) must be referenced by a test.
    trace = [PY, str(REPO / "tools" / "validators" / "traceability.py"), "--min-acceptance", "100"]
    if write_trace:
        trace += ["--write", str(REPO / "docs" / "traceability.md")]
    tools = str(REPO / "tools")
    pytest_cmd = [
        PY,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        # Explicit --basetemp avoids pytest's shared %TEMP%\\pytest-of-<user>\\pytest-current
        # link, whose cleanup fails with WinError 5 on some Windows machines.
        f"--basetemp={BACKEND / '.pytest_tmp'}",
    ]
    if acceptance is not None:
        pytest_cmd.append(f"--junitxml={JUNIT}")
    gates = [
        ("ruff lint", [PY, "-m", "ruff", "check", "src", "tests", tools], BACKEND),
        ("ruff format", [PY, "-m", "ruff", "format", "--check", "src", "tests", tools], BACKEND),
        ("mypy", [PY, "-m", "mypy"], BACKEND),
        (
            "import-linter",
            [PY, str(REPO / "tools" / "validators" / "layering.py"), "--config", "pyproject.toml"],
            BACKEND,
        ),
        ("phase_guard", [PY, str(REPO / "tools" / "validators" / "phase_guard.py")], REPO),
        ("database", [PY, str(REPO / "tools" / "validators" / "db_ready.py")], REPO),
        ("traceability", trace, REPO),
        ("frontend", [PY, str(REPO / "tools" / "validators" / "frontend_check.py")], REPO),
        ("pytest", pytest_cmd, BACKEND),
    ]
    if acceptance is not None:
        # One pytest run, reported per acceptance criterion (spec §40).
        gates.append(
            (
                "acceptance",
                [
                    PY,
                    str(REPO / "tools" / "validators" / "acceptance_report.py"),
                    "--junit",
                    str(JUNIT),
                    "--write",
                    str(acceptance),
                ],
                REPO,
            )
        )
    return gates


def main(argv: list[str]) -> int:
    write_trace = "--write-traceability" in argv
    acceptance: Path | None = None
    for a in argv:
        if a == "--acceptance-report":
            acceptance = REPO / "docs" / "acceptance_report.md"
        elif a.startswith("--acceptance-report="):
            acceptance = Path(a.split("=", 1)[1]).resolve()
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(BACKEND / "src"), env.get("PYTHONPATH", "")]).rstrip(
        os.pathsep
    )
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")

    results: list[tuple[str, bool, float]] = []
    JUNIT.unlink(missing_ok=True)  # the acceptance gate reads only this run's results
    for name, cmd, cwd in _gates(write_trace, acceptance):
        print(f"\n=== {name} ===", flush=True)
        t0 = time.perf_counter()
        proc = subprocess.run(cmd, cwd=cwd, env=env, check=False)
        results.append((name, proc.returncode == 0, time.perf_counter() - t0))

    print("\n=== summary ===")
    for name, ok, secs in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<14} ({secs:.1f}s)")
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} gates passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
