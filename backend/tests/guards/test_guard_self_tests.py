"""Guard self-tests: prove each repo-local guard FAILS on a planted violation
and PASSES on clean input. A guard that cannot fail is not a guard.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import phase_guard
import pytest
import traceability

BACKEND = Path(__file__).resolve().parents[2]
REPO = BACKEND.parent
SPEC = REPO / "docs" / "PHASE1_TECHNICAL_SPEC.md"
LAYERING = REPO / "tools" / "validators" / "layering.py"


# ------------------------------------------------------------------ phase guard
@pytest.mark.parametrize(
    ("snippet", "rule"),
    [
        ("class PlanFinImporter: ...", "PLANFIN"),
        ("planfin_actuals = []", "PLANFIN"),
        ("def import_general_ledger(): ...", "GL"),
        ("gl_account = '5101'", "GL"),
        ("conn = open_ms_access('x.accdb')", "MSACCESS"),
        ("def create_po(pr): ...", "PO"),
        ("class PurchaseOrderService: ...", "PO"),
        ("def record_payment(): ...", "PAYMENT"),
        ("def write_back_to_hosxp(): ...", "HOSXP_WRITE"),
        ("def update_hosxp_pr(): ...", "HOSXP_WRITE"),
    ],
)
def test_phase_guard_catches_planted_out_of_scope_code(
    tmp_path: Path, snippet: str, rule: str
) -> None:
    (tmp_path / "module.py").write_text(snippet + "\n", encoding="utf-8")
    findings = phase_guard.scan([tmp_path])
    assert rule in {f.rule for f in findings}
    assert phase_guard.main([str(tmp_path)]) == 1


def test_phase_guard_catches_out_of_scope_file_names(tmp_path: Path) -> None:
    (tmp_path / "planfin").mkdir()
    (tmp_path / "planfin" / "__init__.py").write_text("", encoding="utf-8")
    assert {f.rule for f in phase_guard.scan([tmp_path])} == {"PLANFIN"}


def test_phase_guard_passes_clean_phase1_code(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text(
        "def confirm_ppr(pr_no): ...\nplan_ledger = []\nglobal_search = 1\npolicy = 2\n",
        encoding="utf-8",
    )
    assert phase_guard.scan([tmp_path]) == []
    assert phase_guard.main([str(tmp_path)]) == 0


@pytest.mark.spec("S-3", "S-46")
def test_phase_guard_passes_on_the_real_source_tree() -> None:
    assert phase_guard.scan(list(phase_guard.DEFAULT_ROOTS)) == []


# ------------------------------------------------------------------ layering (import-linter)
def _run_lint_imports(root: Path, config: Path) -> subprocess.CompletedProcess[str]:
    # Force UTF-8 both ways: import-linter prints box-drawing characters, and a Windows
    # console code page (e.g. cp874 on Thai Windows) cannot encode/decode them.
    env = dict(os.environ, PYTHONPATH=str(root), PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    return subprocess.run(
        [sys.executable, str(LAYERING), "--config", str(config), "--no-cache"],
        cwd=root,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _make_pkg(root: Path, files: dict[str, str]) -> None:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body), encoding="utf-8")


_SKELETON = {
    "ppr/__init__.py": "",
    "ppr/domain/__init__.py": "",
    "ppr/domain/rules.py": "X = 1\n",
    "ppr/domain/ledger.py": "",
    "ppr/application/__init__.py": "",
    "ppr/application/stock_services.py": "",
    "ppr/application/plan_services.py": "",
    "ppr/application/ppr_services.py": "",
    "ppr/application/sync_services.py": "",
    "ppr/ports.py": "",
    "ppr/api/__init__.py": "",
    "ppr/infrastructure/__init__.py": "",
    "ppr/integration/__init__.py": "",
    "ppr/integration/hosxp/__init__.py": "",
    "ppr/integration/hosxp/contracts.py": "",
    "ppr/integration/hosxp/gateway.py": "",
    "ppr/integration/hosxp/auth.py": "",
    "ppr/integration/hosxp/status.py": "",
    "ppr/integration/hosxp/mock/__init__.py": "",
    "ppr/integration/hosxp/real/__init__.py": "",
}


def _config_copy(tmp_path: Path) -> Path:
    """The REAL contracts from pyproject.toml, so the self-test exercises what `check` runs."""
    text = (BACKEND / "pyproject.toml").read_text(encoding="utf-8")
    section = text[text.index("[tool.importlinter]") :]
    cfg = tmp_path / "importlinter.toml"
    cfg.write_text(section, encoding="utf-8")
    return cfg


def test_layering_guard_passes_clean_skeleton(tmp_path: Path) -> None:
    _make_pkg(tmp_path, _SKELETON)
    res = _run_lint_imports(tmp_path, _config_copy(tmp_path))
    assert res.returncode == 0, res.stdout + res.stderr


@pytest.mark.parametrize(
    ("path", "body", "broken"),
    [
        ("ppr/domain/rules.py", "import ppr.integration.hosxp.contracts\n", "Domain is pure"),
        ("ppr/domain/rules.py", "import ppr.infrastructure\n", "Domain is pure"),
        ("ppr/domain/rules.py", "import ppr.integration.hosxp.mock\n", "Core never depends"),
        (
            "ppr/application/__init__.py",
            "import ppr.integration.hosxp.mock\n",
            "Core never depends",
        ),
        (
            "ppr/integration/hosxp/contracts.py",
            "import ppr.integration.hosxp.real\n",
            "Core never depends",
        ),
        (
            "ppr/integration/hosxp/real/__init__.py",
            "import ppr.integration.hosxp.mock\n",
            "independent",
        ),
        (
            "ppr/application/__init__.py",
            "import ppr.infrastructure\n",
            "Application and ports never touch",
        ),
        ("ppr/ports.py", "import sqlalchemy\n", "Application and ports never touch"),
        # D-39: stock movements are read for reports only.
        ("ppr/application/stock_services.py", "import ppr.domain.ledger\n", "Stock movements"),
        ("ppr/application/stock_services.py", "import ppr.ports\n", "Stock movements"),
        (
            "ppr/application/stock_services.py",
            "import ppr.application.ppr_services\n",
            "Stock movements",
        ),
    ],
)
def test_layering_guard_catches_planted_violation(
    tmp_path: Path, path: str, body: str, broken: str
) -> None:
    _make_pkg(tmp_path, {**_SKELETON, path: body})
    res = _run_lint_imports(tmp_path, _config_copy(tmp_path))
    assert res.returncode != 0
    assert "BROKEN" in res.stdout
    assert broken in res.stdout


# ------------------------------------------------------------------ traceability
def test_traceability_rejects_ids_not_in_the_spec(tmp_path: Path) -> None:
    (tmp_path / "test_x.py").write_text(
        "import pytest\n\n@pytest.mark.spec('AT-40.99.1', 'D-99')\ndef test_a():\n    pass\n",
        encoding="utf-8",
    )
    assert traceability.main(["--spec", str(SPEC), "--tests", str(tmp_path)]) == 1


def test_traceability_accepts_real_ids(tmp_path: Path) -> None:
    (tmp_path / "test_y.py").write_text(
        "import pytest\n\n@pytest.mark.spec('AT-40.2.1', 'D-01', 'S-13', 'G-1')\n"
        "def test_b():\n    pass\n",
        encoding="utf-8",
    )
    assert traceability.main(["--spec", str(SPEC), "--tests", str(tmp_path)]) == 0


def test_traceability_min_coverage_gate_can_fail(tmp_path: Path) -> None:
    (tmp_path / "test_z.py").write_text("def test_c():\n    pass\n", encoding="utf-8")
    args = ["--spec", str(SPEC), "--tests", str(tmp_path), "--min-acceptance", "100"]
    assert traceability.main(args) == 1


def test_traceability_ids_are_derived_from_the_spec() -> None:
    ids = traceability.parse_spec(SPEC)
    assert {"D-01", "D-02", "D-03", "D-04", "D-05", "D-06", "D-07", "D-08"} <= ids.decisions
    assert {"G-1", "G-2", "G-3", "G-4"} <= ids.gates
    assert ids.acceptance["AT-40.2.2"].startswith("Qty exceeds")


# ------------------------------------------------------------------ database gate
DB_READY = REPO / "tools" / "validators" / "db_ready.py"


@pytest.mark.parametrize(
    ("url", "expect"),
    [
        ("", "not set"),
        ("postgresql+psycopg://u:p@localhost:5432/ppr_dev", "_test"),
        ("postgresql+psycopg://u:p@127.0.0.1:1/ppr_test", "cannot connect"),
    ],
)
def test_database_gate_fails_when_misconfigured(url: str, expect: str) -> None:
    env = dict(os.environ, PPR_TEST_DATABASE_URL=url, PYTHONUTF8="1")
    env["PYTHONPATH"] = str(BACKEND / "src")
    res = subprocess.run(
        [sys.executable, str(DB_READY)],
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert res.returncode == 1
    assert "FAIL" in res.stdout and expect in res.stdout
    assert ":p@" not in res.stdout  # password never printed
