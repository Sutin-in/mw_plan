"""Database test fixtures (PostgreSQL).

* The test database comes from PPR_TEST_DATABASE_URL (environment or .env).
* Its name MUST end with ``_test``; anything else is refused before any DDL runs.
* If it is not configured, every DB test FAILS (never silently skipped), so the
  `check` gate cannot pass without real database evidence.
* Each test starts from an empty ``public`` schema migrated to head.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, text

from ppr.cli import alembic_config
from ppr.config.settings import Settings, SettingsError, require_test_database


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "/tests/db/" in item.nodeid.replace("\\", "/") or item.nodeid.startswith("tests/db/"):
            item.add_marker(pytest.mark.db)


@pytest.fixture(scope="session")
def db_url() -> str:
    url = Settings.load().test_database_url
    if not url:
        pytest.fail(
            "PPR_TEST_DATABASE_URL is not configured. Run tools/dev/setup_local_db.py "
            "(writes .env) or set the variable. Database tests are mandatory.",
            pytrace=False,
        )
    try:
        return require_test_database(url)
    except SettingsError as exc:
        pytest.fail(str(exc), pytrace=False)


@pytest.fixture(scope="session")
def engine(db_url: str) -> Iterator[Engine]:
    eng = create_engine(db_url, future=True)
    yield eng
    eng.dispose()


def reset_schema(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))


@pytest.fixture
def empty_db(engine: Engine, db_url: str) -> Engine:
    """Empty schema, NOT migrated (for migration tests)."""
    reset_schema(engine)
    return engine


@pytest.fixture
def db(empty_db: Engine, db_url: str) -> Engine:
    """Empty schema migrated to head."""
    command.upgrade(alembic_config(db_url), "head")
    return empty_db
