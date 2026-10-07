"""Backup, restore and the restore check (spec §38.3; Wave 10A, operations).

    python -m ppr.cli backup  --out <folder>
    python -m ppr.cli restore --dump <file.dump> --target-url <url> [--replace]
    python -m ppr.cli verify-restore --manifest <file.manifest.json> --url <url>

* **backup** opens a REPEATABLE READ transaction, exports its snapshot, computes a *manifest*
  of the database inside that snapshot and runs ``pg_dump --snapshot`` on the same snapshot -
  so the dump and the manifest describe exactly the same moment, even while users work. The
  manifest holds, per table, the row count and a hash of the rows (no row data), the sequence
  values, the schema version and the evidence triggers. It runs as the schema owner in
  production (``PPR_MIGRATION_DATABASE_URL``).
* **restore** loads a dump into another, empty database (``pg_restore --no-owner``). It never
  touches the database the dump came from, and replaces a non-empty one only with
  ``--replace`` and only when its name ends with ``_test``, ``_drill`` or ``_restore``.
* **verify-restore** recomputes the manifest on the restored database and compares: every
  table's count and row hash (so every plan-ledger entry, hence every derived balance, every
  audit row and every PPR version), the sequences (new PPR numbers continue after the last
  one), the schema version and the evidence triggers.

Passwords reach ``pg_dump`` / ``pg_restore`` only through the ``PGPASSWORD`` environment of
the child process, never on a command line or in a file. ``PPR_PG_BIN`` names the folder of
the PostgreSQL client tools when they are not on PATH (on Windows the newest
``C:\\Program Files\\PostgreSQL\\<n>\\bin`` is used).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import make_url

from ppr.config.settings import SettingsError, database_name, redact_url

REPLACEABLE = ("_test", "_drill", "_restore")


# ------------------------------------------------------------------ PostgreSQL client tools
def pg_tool(name: str) -> str:
    folder = os.environ.get("PPR_PG_BIN")
    exe = name + (".exe" if os.name == "nt" else "")
    if folder:
        path = Path(folder) / exe
        if path.is_file():
            return str(path)
        raise SettingsError(f"PPR_PG_BIN={folder} has no {exe}")
    found = shutil.which(name)
    if found:
        return found
    if os.name == "nt":
        roots = sorted(
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"), "PostgreSQL").glob("*/bin"),
            key=lambda p: int(p.parent.name) if p.parent.name.isdigit() else 0,
        )
        for root in reversed(roots):
            if (root / exe).is_file():
                return str(root / exe)
    raise SettingsError(f"{name} not found: install the PostgreSQL client tools or set PPR_PG_BIN")


def _libpq(url: str) -> tuple[list[str], dict[str, str]]:
    """Connection arguments and environment for a client tool (password in the env only)."""
    u = make_url(url)
    args = ["--host", u.host or "localhost", "--port", str(u.port or 5432)]
    if u.username:
        args += ["--username", u.username]
    env = {**os.environ}
    if u.password:
        env["PGPASSWORD"] = str(u.password)
    return args, env


# ------------------------------------------------------------------ manifest
def manifest(conn: Connection) -> dict[str, Any]:
    """What a restored copy must reproduce exactly (counts and hashes only, no row data).
    Rows are hashed in one fixed text form (UTC, ISO dates, byte order "C") whatever the
    session settings or the database collation, so a copy on another server compares equal."""
    conn.execute(text("SET LOCAL TIME ZONE 'UTC'"))
    conn.execute(text("SET LOCAL DateStyle = 'ISO, YMD'"))
    conn.execute(text("SET LOCAL IntervalStyle = 'iso_8601'"))
    conn.execute(text("SET LOCAL extra_float_digits = 3"))
    conn.execute(text("SET LOCAL bytea_output = 'hex'"))
    tables = [
        r[0]
        for r in conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")
        )
    ]
    out_tables: dict[str, dict[str, Any]] = {}
    for t in tables:
        q = f"SELECT count(*), md5(COALESCE(string_agg(x::text, E'\\n' ORDER BY x::text COLLATE \"C\"), '')) FROM public.\"{t}\" x"  # noqa: E501 - catalog name
        count, digest = conn.execute(text(q)).one()
        out_tables[t] = {"rows": int(count), "hash": digest}
    sequences = {
        r[0]: int(r[1]) if r[1] is not None else None
        for r in conn.execute(
            text(
                "SELECT sequencename, last_value FROM pg_sequences "
                "WHERE schemaname = 'public' ORDER BY 1"
            )
        )
    }
    triggers = [
        r[0]
        for r in conn.execute(
            text(
                "SELECT c.relname || '.' || t.tgname FROM pg_trigger t "
                "JOIN pg_class c ON c.oid = t.tgrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE NOT t.tgisinternal AND n.nspname = 'public' ORDER BY 1"
            )
        )
    ]
    version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    return {
        "schema_version": version,
        "tables": out_tables,
        "sequences": sequences,
        "triggers": triggers,
    }


def compare(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Every difference between two manifests (empty = the copy is exact)."""
    problems: list[str] = []
    if expected["schema_version"] != actual["schema_version"]:
        problems.append(
            f"schema version {actual['schema_version']}, expected {expected['schema_version']}"
        )
    for t, e in expected["tables"].items():
        a = actual["tables"].get(t)
        if a is None:
            problems.append(f"table {t} is missing")
        elif a["rows"] != e["rows"]:
            problems.append(f"table {t}: {a['rows']} rows, expected {e['rows']}")
        elif a["hash"] != e["hash"]:
            problems.append(f"table {t}: same row count, different content")
    problems += [
        f"unexpected table {t}" for t in sorted(set(actual["tables"]) - set(expected["tables"]))
    ]
    for s, v in expected["sequences"].items():
        if actual["sequences"].get(s) != v:
            problems.append(f"sequence {s} at {actual['sequences'].get(s)}, expected {v}")
    missing = sorted(set(expected["triggers"]) - set(actual["triggers"]))
    problems += [f"trigger {t} is missing" for t in missing]
    return problems


def ledger_digest(m: dict[str, Any]) -> str:
    """A short fingerprint of the evidence tables, for the report."""
    keys = ("plan_ledger", "audit_log", "ppr_version", "ppr_binding")
    raw = "|".join(
        f"{k}:{m['tables'][k]['rows']}:{m['tables'][k]['hash']}" for k in keys if k in m["tables"]
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ------------------------------------------------------------------ backup
@dataclass(frozen=True)
class BackupResult:
    dump: Path
    manifest_file: Path
    manifest: dict[str, Any]


def backup(url: str, out_dir: Path, now: datetime | None = None) -> BackupResult:
    now = now or datetime.now(UTC)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{database_name(url)}-{now:%Y%m%d-%H%M%S}"
    dump = out_dir / f"{stem}.dump"
    mfile = out_dir / f"{stem}.manifest.json"
    engine = create_engine(url, isolation_level="REPEATABLE READ")
    try:
        with engine.connect() as conn, conn.begin():
            conn.execute(text("SET TRANSACTION READ ONLY"))
            snapshot: str = conn.execute(text("SELECT pg_export_snapshot()")).scalar_one()
            m = manifest(conn)
            args, env = _libpq(url)
            proc = subprocess.run(
                [
                    pg_tool("pg_dump"),
                    *args,
                    "--format=custom",
                    f"--snapshot={snapshot}",
                    f"--file={dump}",
                    database_name(url),
                ],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                raise SettingsError(f"pg_dump failed: {proc.stderr.strip()[-500:]}")
    finally:
        engine.dispose()
    record = {
        "database": redact_url(url),
        "taken_at": now.isoformat(),
        "dump": dump.name,
        "dump_sha256": _sha256(dump),
        **m,
    }
    mfile.write_text(json.dumps(record, indent=1, sort_keys=True), encoding="utf-8")
    return BackupResult(dump, mfile, record)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ restore
def _identity(url: str) -> tuple[str, int] | None:
    """Which database this really is: the server instance (its start time) and the database
    oid - the same whatever host name or address the URL uses. None if it does not answer."""
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            started, oid = conn.execute(
                text(
                    "SELECT pg_postmaster_start_time()::text, oid FROM pg_database "
                    "WHERE datname = current_database()"
                )
            ).one()
            return str(started), int(oid)
    except Exception:  # unreachable: compared by name below
        return None
    finally:
        engine.dispose()


def _same_place(a: str, b: str) -> bool:
    ua, ub = make_url(a), make_url(b)

    def host(h: str | None) -> str:
        return "127.0.0.1" if h in (None, "", "localhost", "::1") else h

    return (host(ua.host), ua.port or 5432, ua.database) == (
        host(ub.host),
        ub.port or 5432,
        ub.database,
    )


def restore(
    dump: Path,
    target_url: str,
    *,
    source_url: str | None,
    replace: bool,
    protected: tuple[str, ...] = (),
) -> None:
    """``protected``: database names that are never replaced (the live ones, in production)."""
    target = make_url(target_url)
    if source_url is not None:
        live, there = _identity(source_url), _identity(target_url)
        if _same_place(source_url, target_url) or (live is not None and live == there):
            raise SettingsError("refusing to restore over the database the system runs on")
    if replace and (target.database or "") in protected:
        raise SettingsError(
            f"refusing to replace {target.database!r}: it is the database the system uses"
        )
    mfile = dump.with_suffix("").with_suffix(".manifest.json")
    if mfile.is_file():
        expected = json.loads(mfile.read_text(encoding="utf-8")).get("dump_sha256")
        if expected and expected != _sha256(dump):
            raise SettingsError(f"{dump.name} does not match its manifest (damaged or replaced)")
    engine = create_engine(target_url)
    try:
        with engine.begin() as conn:
            n: int = conn.execute(
                text("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
            ).scalar_one()
            if n:
                name = database_name(target_url).lower()
                if not (replace and name.endswith(REPLACEABLE)):
                    raise SettingsError(
                        f"target database {name!r} is not empty; restore into an empty database "
                        f"(or a *{'/*'.join(REPLACEABLE)} one with --replace)"
                    )
                conn.execute(text("DROP SCHEMA public CASCADE"))
                conn.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()
    args, env = _libpq(target_url)
    proc = subprocess.run(
        [
            pg_tool("pg_restore"),
            *args,
            "--no-owner",
            "--no-privileges",
            "--exit-on-error",
            f"--dbname={target.database}",
            str(dump),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SettingsError(f"pg_restore failed: {proc.stderr.strip()[-800:]}")


def verify(manifest_file: Path, url: str) -> list[str]:
    expected = json.loads(manifest_file.read_text(encoding="utf-8"))
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            actual = manifest(conn)
    finally:
        engine.dispose()
    return compare(expected, actual)
