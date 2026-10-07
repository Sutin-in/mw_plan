"""Create the local development/test PostgreSQL databases and write ``.env``.

Safe by design:
* creates ONLY role ``ppr_app`` and databases ``ppr_dev`` and ``ppr_test`` - or, with
  ``--name somchai`` on a shared server, role ``ppr_somchai`` and databases
  ``ppr_somchai_dev`` / ``ppr_somchai_test`` (one set per programmer, so test runs,
  which recreate the schema, never collide);
* never drops or alters any other role or database;
* the superuser password is prompted (not echoed) and never stored;
* ``ppr_app``'s password and the session secret are random and written only to ``.env``
  (git-ignored). An existing ``.env`` password is reused so re-running is idempotent;
* writes ``_db_setup_result.txt`` with versions and connection checks, WITHOUT secrets.

Usage (from the repository root):
    python tools/dev/setup_local_db.py [--name NAME] [--host localhost] [--port 5432]
                                       [--superuser postgres]
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import secrets
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import psycopg
from psycopg import sql

REPO = Path(__file__).resolve().parents[2]
ENV_FILE = REPO / ".env"
RESULT_FILE = REPO / "_db_setup_result.txt"
REQUIRED_MAJOR = 16
_NAME = re.compile(r"^[a-z][a-z0-9_]{1,30}$")


def names_for(dev_name: str | None) -> tuple[str, str, str]:
    """(role, dev database, test database). Test database always ends with _test."""
    if dev_name is None:
        return "ppr_app", "ppr_dev", "ppr_test"
    if not _NAME.match(dev_name):
        raise SystemExit("--name must be 2-31 chars: lowercase letters, digits, underscore")
    return f"ppr_{dev_name}", f"ppr_{dev_name}_dev", f"ppr_{dev_name}_test"


def find_psql() -> str | None:
    found = shutil.which("psql")
    if found:
        return found
    for base in (Path(r"C:\Program Files\PostgreSQL"), Path(r"C:\Program Files (x86)\PostgreSQL")):
        for candidate in sorted(base.glob("*/bin/psql.exe"), reverse=True):
            return str(candidate)
    return None


def existing_app_password() -> str | None:
    if not ENV_FILE.is_file():
        return None
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("PPR_DATABASE_URL="):
            pw = urlsplit(line.split("=", 1)[1].strip()).password
            return pw or None
    return None


def existing_session_secret() -> str | None:
    if not ENV_FILE.is_file():
        return None
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("PPR_SESSION_SECRET="):
            value = line.split("=", 1)[1].strip()
            return value if len(value) >= 32 else None
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=5432)
    ap.add_argument("--superuser", default="postgres")
    ap.add_argument("--name", default=None, help="per-programmer names on a shared server")
    args = ap.parse_args()
    app_role, dev_db, test_db = names_for(args.name)
    databases = (dev_db, test_db)

    report: list[str] = [f"setup_local_db run at {datetime.now(UTC).isoformat()}"]

    def log(msg: str) -> None:
        print(msg)
        report.append(msg)

    ok = True
    try:
        psql = find_psql()
        if psql:
            out = subprocess.run([psql, "--version"], capture_output=True, text=True, check=False)
            log(f"psql: {out.stdout.strip() or out.stderr.strip()} ({psql})")
        else:
            log("psql: NOT FOUND on PATH or under Program Files (server check continues)")

        su_pw = getpass.getpass(f"Password for PostgreSQL superuser '{args.superuser}': ")
        su_conninfo = dict(
            host=args.host, port=args.port, user=args.superuser, password=su_pw, dbname="postgres"
        )
        app_pw = existing_app_password() or secrets.token_urlsafe(24)

        with psycopg.connect(**su_conninfo, autocommit=True) as conn:
            version = conn.execute("SHOW server_version").fetchone()
            server_version = str(version[0]) if version else "?"
            major = int(server_version.split(".")[0])
            log(f"server: PostgreSQL {server_version} at {args.host}:{args.port}")
            if major != REQUIRED_MAJOR:
                log(f"WARNING: expected PostgreSQL {REQUIRED_MAJOR}, found {major}")

            exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (app_role,))
            if exists.fetchone():
                conn.execute(
                    sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                        sql.Identifier(app_role), sql.Literal(app_pw)
                    )
                )
                log(f"role {app_role}: existed, password set to the value in .env")
            else:
                conn.execute(
                    sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD {}").format(
                        sql.Identifier(app_role), sql.Literal(app_pw)
                    )
                )
                log(f"role {app_role}: created")

            for db in databases:
                row = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db,)).fetchone()
                if row:
                    log(f"database {db}: already exists (left unchanged)")
                else:
                    conn.execute(
                        sql.SQL("CREATE DATABASE {} OWNER {} ENCODING 'UTF8'").format(
                            sql.Identifier(db), sql.Identifier(app_role)
                        )
                    )
                    log(f"database {db}: created, owner {app_role}")
                # Only the owner (and superusers) may connect - matters on a shared server.
                conn.execute(
                    sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(sql.Identifier(db))
                )

        secret = existing_session_secret() or secrets.token_urlsafe(48)
        pw_q = quote(app_pw, safe="")
        base = f"postgresql+psycopg://{app_role}:{pw_q}@{args.host}:{args.port}"
        ENV_FILE.write_text(
            "# Written by tools/dev/setup_local_db.py - git-ignored, do not commit.\n"
            f"PPR_DATABASE_URL={base}/{dev_db}\n"
            f"PPR_TEST_DATABASE_URL={base}/{test_db}\n"
            f"PPR_SESSION_SECRET={secret}\n"
            "PPR_SESSION_TTL_MINUTES=480\n",
            encoding="utf-8",
        )
        log(f".env: written to {ENV_FILE} (secrets not shown)")

        for db in databases:
            with psycopg.connect(
                host=args.host, port=args.port, user=app_role, password=app_pw, dbname=db
            ) as conn:
                row = conn.execute(
                    "SELECT current_database(), current_user, split_part(version(), ' ', 2)"
                ).fetchone()
                assert row is not None
                log(f"connect OK as {row[1]} to {row[0]} (PostgreSQL {row[2]})")
    except Exception as exc:  # report every failure, without secrets
        ok = False
        log(f"ERROR: {type(exc).__name__}: {exc}")

    log("RESULT: OK" if ok else "RESULT: FAILED")
    RESULT_FILE.write_text("\n".join(report) + "\n", encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(main())
