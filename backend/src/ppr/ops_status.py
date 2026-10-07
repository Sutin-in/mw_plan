"""``python -m ppr.cli ops-status`` - is the installation working? (Wave 10A, operations)

Prints one line per check and exits 0 only when every check passes, so a scheduler, a
monitoring tool or a person can run it:

* the runtime database answers and its schema is at the latest migration;
* in production, the runtime account is least-privilege (``privileges.runtime_account_issues``);
* the last nightly PR synchronization finished successfully within ``max_age`` hours;
* when the last HOSxP master synchronization ran (information only);
* MOCK / DEMO data in the database (D-40): ``WARN`` in production (it never fails the check;
  the warning stays until the data is dealt with), ``INFO`` on a development machine.

It reads only; it prints no secret and no business record.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from ppr.config.settings import Settings, SettingsError, redact_url
from ppr.infrastructure.db.mockdata import mock_data_summary
from ppr.infrastructure.db.privileges import runtime_account_issues

OK, FAIL, INFO, WARN = "PASS", "FAIL", "INFO", "WARN"


def _line(state: str, what: str) -> None:
    print(f"{state:<5} {what}")  # noqa: T201 - a command-line report


def run(settings: Settings, max_age_hours: float, now: datetime | None = None) -> int:
    from ppr.server import schema_head  # composition level: the migration scripts

    try:
        url = settings.require_database_url()
    except SettingsError as exc:
        _line(FAIL, str(exc))
        return 1
    now = now or datetime.now(UTC)
    failed = False
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            head = schema_head()
            ok = current == head
            failed |= not ok
            _line(
                OK if ok else FAIL, f"database {redact_url(url)}: schema {current} (latest {head})"
            )
            if settings.production:
                issues = runtime_account_issues(conn)
                failed |= bool(issues)
                _line(
                    FAIL if issues else OK,
                    "runtime account least-privilege"
                    + (": " + "; ".join(issues) if issues else ""),
                )
            nightly = conn.execute(
                text(
                    "SELECT status, finished_at FROM sync_run WHERE mode = 'NIGHTLY' "
                    "AND finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1"
                )
            ).first()
            good = conn.execute(
                text(
                    "SELECT max(finished_at) FROM sync_run WHERE mode = 'NIGHTLY' "
                    "AND status = 'SUCCEEDED'"
                )
            ).scalar()
            mock = mock_data_summary(conn)
            masters = conn.execute(
                text(
                    "SELECT status, finished_at FROM sync_run WHERE scope = 'MASTERS' "
                    "AND finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1"
                )
            ).first()
    except SQLAlchemyError as exc:
        _line(FAIL, f"database {redact_url(url)} does not answer ({type(exc).__name__})")
        return 1
    finally:
        engine.dispose()

    if good is None:
        failed = True
        _line(FAIL, "nightly PR synchronization: no successful run yet")
    else:
        age = now - good
        fresh = age <= timedelta(hours=max_age_hours)
        failed |= not fresh
        _line(
            OK if fresh else FAIL,
            f"nightly PR synchronization: last success {good.isoformat(timespec='minutes')} "
            f"({age.total_seconds() / 3600:.1f} h ago, limit {max_age_hours:g} h)",
        )
    if nightly is not None and nightly[0] != "SUCCEEDED":
        _line(INFO, f"the latest nightly run ended {nightly[0]} at {nightly[1]:%Y-%m-%d %H:%M}")
    if mock.found:
        _line(
            WARN if settings.production else INFO,
            "MOCK/DEMO data in this database: "
            + mock.describe()
            + (
                " - not real HOSxP data; never posted to the Plan Ledger in production (D-40)"
                if settings.production
                else ""
            ),
        )
    elif settings.production:
        _line(OK, "no MOCK/DEMO data in the production database")
    if masters is None:
        _line(INFO, "HOSxP master synchronization: never run")
    else:
        _line(INFO, f"HOSxP master synchronization: {masters[0]} at {masters[1]:%Y-%m-%d %H:%M}")
    warned = settings.production and mock.found
    print(  # noqa: T201
        "ops-status: " + ("FAIL" if failed else "PASS" + (" (with warnings)" if warned else ""))
    )
    return 1 if failed else 0
