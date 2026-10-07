"""Database readiness gate for `check`.

PASS only if PPR_TEST_DATABASE_URL (environment or .env) is set, names a ``*_test``
database, is reachable, and runs PostgreSQL 16. Never prints the password.
"""

from __future__ import annotations

import sys

from sqlalchemy import create_engine, text

from ppr.config.settings import Settings, SettingsError, redact_url, require_test_database

REQUIRED_MAJOR = 16


def main() -> int:
    url = Settings.load().test_database_url
    if not url:
        print("db_ready: FAIL - PPR_TEST_DATABASE_URL is not set (run tools/dev/setup_local_db.py)")
        return 1
    try:
        require_test_database(url)
    except SettingsError as exc:
        print(f"db_ready: FAIL - {exc}")
        return 1
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            version = str(conn.execute(text("SHOW server_version")).scalar_one())
            db = conn.execute(text("SELECT current_database()")).scalar_one()
    except Exception as exc:
        print(f"db_ready: FAIL - cannot connect to {redact_url(url)}: {type(exc).__name__}")
        return 1
    finally:
        engine.dispose()
    major = int(version.split(".")[0])
    if major != REQUIRED_MAJOR:
        print(f"db_ready: FAIL - PostgreSQL {version} found, {REQUIRED_MAJOR}.x required")
        return 1
    print(f"db_ready: PASS - {db} on PostgreSQL {version} ({redact_url(url)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
