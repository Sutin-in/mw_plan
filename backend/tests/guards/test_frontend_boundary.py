"""The user interface stays a thin presentation layer (D-24).

The Express application must not read the database or HOSxP itself: every business
rule and permission lives behind the Python API. These checks fail if a database
driver, a database URL or a direct HOSxP connection appears in the frontend.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
FRONTEND = REPO / "frontend"

DB_PACKAGES = re.compile(
    r"^(pg|pg-promise|postgres|mysql2?|mariadb|knex|sequelize|typeorm|prisma|@prisma/client|"
    r"mssql|oracledb|sqlite3?|better-sqlite3|mongodb|mongoose|redis|ioredis)$"
)
DIRECT_ACCESS = re.compile(
    r"postgres(ql)?://|mysql://|mariadb://|PPR_DATABASE_URL|PPR_HOSXP_DATABASE_URL|"
    r"\bSELECT\s+.+\s+FROM\b|require\(['\"](pg|mysql2?|mariadb)['\"]\)",
    re.IGNORECASE,
)


def _sources() -> list[Path]:
    return [
        p
        for d in ("src", "views", "public")
        for p in (FRONTEND / d).rglob("*")
        if p.is_file() and p.suffix in {".js", ".ejs", ".css", ".html"}
    ]


@pytest.mark.spec("D-24")
def test_frontend_has_no_database_or_hosxp_driver() -> None:
    pkg = json.loads((FRONTEND / "package.json").read_text(encoding="utf-8"))
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    assert not [d for d in deps if DB_PACKAGES.match(d)]
    assert set(pkg.get("dependencies", {})) == {"express", "ejs", "cookie-parser"}


@pytest.mark.spec("D-24")
def test_frontend_code_talks_only_to_the_api() -> None:
    files = _sources()
    assert files, "frontend sources not found"
    offenders = [
        str(p.relative_to(REPO))
        for p in files
        if DIRECT_ACCESS.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []


@pytest.mark.spec("D-24")
def test_the_guard_catches_a_planted_violation() -> None:
    assert DIRECT_ACCESS.search("const pool = require('pg')")
    assert DIRECT_ACCESS.search("const url = 'postgresql://u:p@db/ppr'")
    assert DB_PACKAGES.match("mysql2")
