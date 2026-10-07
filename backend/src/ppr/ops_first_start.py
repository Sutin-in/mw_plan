"""``python -m ppr.cli first-start --check`` - is this server ready for its first start in
production? (Wave 10B-2)

READ ONLY: it reads the settings (environment and ``.env``), the site query file and the
PPR database; it changes nothing and connects to nothing else (HOSxP is checked by
``hosxp-verify``). Each line ends PASS / FAIL / WARN / OPEN / TODO / INFO (``ops_report``):

* FAIL - the server would refuse to start, or would run unsafely;
* WARN - MOCK/DEMO data in the production database (D-40): allowed, but must be dealt with;
* TODO - a first-start step still to do (migration aside, which is a FAIL until done);
* OPEN - a decision or HOSxP evidence that does not exist yet (G-4, D-30, D-39) - never PASS.

The steps themselves are in ``docs/IT_HANDOFF.md``; the evidence goes to
``docs/GO_LIVE_CHECKLIST.md``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import SQLAlchemyError

from ppr.config.settings import Settings, SettingsError, database_name, redact_url
from ppr.infrastructure.db.mockdata import mock_data_summary
from ppr.infrastructure.db.privileges import runtime_account_issues
from ppr.infrastructure.db.schema import hosxp_department, plan_year, user_role
from ppr.integration.hosxp.errors import HosxpConfigurationError
from ppr.integration.hosxp.real.query_config import QueryConfig, load_config
from ppr.integration.hosxp.status import HosxpPrStatus
from ppr.ops_report import CheckReport

LOCAL_API = re.compile(r"^http://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def run(settings: Settings, env: Mapping[str, str], out: Path | None) -> int:
    from ppr.server import schema_head  # composition level: the migration scripts

    rep = CheckReport("first-start --check")
    _settings(rep, settings, env)
    cfg = _hosxp_settings(rep, settings)
    try:
        url = settings.require_database_url()
    except SettingsError as exc:
        rep.add("FAIL", "database", str(exc))
        return rep.finish(out)
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            head = schema_head()
            if current == head:
                rep.add("PASS", "database", f"{redact_url(url)}: schema {current} (latest)")
            else:
                rep.add(
                    "FAIL",
                    "database",
                    f"schema {current or 'none'}, latest {head}: run python -m ppr.cli migrate "
                    "(it uses the schema owner, PPR_MIGRATION_DATABASE_URL)",
                )
            issues = runtime_account_issues(conn)
            rep.add(
                "FAIL" if issues else "PASS",
                "database",
                "runtime account least-privilege" + (": " + "; ".join(issues) if issues else ""),
            )
            mock = mock_data_summary(conn)
            admins = conn.execute(
                select(func.count()).select_from(user_role).where(user_role.c.role_code == "ADMIN")
            ).scalar_one()
            masters = conn.execute(select(func.count()).select_from(hosxp_department)).scalar_one()
            years = conn.execute(select(func.count()).select_from(plan_year)).scalar_one()
            conn.rollback()
    except SQLAlchemyError as exc:
        rep.add("FAIL", "database", f"{redact_url(url)} does not answer ({type(exc).__name__})")
        return rep.finish(out)
    finally:
        engine.dispose()

    if mock.found:
        rep.add(
            "WARN",
            "MOCK/DEMO (D-40)",
            f"{mock.describe()}: not real HOSxP data; the system starts, warns on every page and "
            "never posts MOCK items to the Plan Ledger - use a clean database for go-live",
        )
    else:
        rep.add("PASS", "MOCK/DEMO (D-40)", "no MOCK/DEMO data in the database")
    rep.add(
        "PASS" if admins else "TODO",
        "first start",
        f"{admins} administrator(s)"
        if admins
        else "no administrator yet: after the first HOSxP sign-in run "
        "python -m ppr.cli grant-role --username <user> --role ADMIN",
    )
    rep.add(
        "PASS" if masters else "TODO",
        "first start",
        "HOSxP masters synchronized"
        if masters
        else "HOSxP masters not synchronized yet (administrator: synchronize masters)",
    )
    rep.add(
        "INFO" if years else "TODO",
        "first start",
        f"{years} plan year(s)" if years else "no plan year yet (Plan Officer creates it)",
    )
    rep.add(
        "TODO",
        "backup",
        "first backup and restore drill (DEPLOY_PRODUCTION.md §7) - evidence in the checklist",
    )
    rep.add("TODO", "HOSxP", "run python -m ppr.cli hosxp-verify --pr <real PR> --out <file>")
    _open_items(rep, cfg)
    return rep.finish(out)


def _settings(rep: CheckReport, settings: Settings, env: Mapping[str, str]) -> None:
    rep.add(
        "PASS" if settings.production else "FAIL",
        "settings",
        "PPR_PROFILE=production"
        if settings.production
        else f"PPR_PROFILE is {settings.profile!r}: set PPR_PROFILE=production on this server",
    )
    try:
        url = settings.require_database_url()
        name = database_name(url).lower()
        bad = name.endswith(("_dev", "_test"))
        rep.add(
            "FAIL" if bad else "PASS",
            "settings",
            f"database {name!r}" + (" is a development / test one" if bad else ""),
        )
    except SettingsError as exc:
        rep.add("FAIL", "settings", str(exc))
    if settings.production:
        try:
            settings.migration_url()
            rep.add("PASS", "settings", "schema owner account (PPR_MIGRATION_DATABASE_URL) is set")
        except SettingsError as exc:
            rep.add("FAIL", "settings", str(exc))
        rep.add("INFO", "settings", "demo mode is refused in production (start without --demo)")
    try:
        settings.require_session_secret()
        rep.add("PASS", "settings", "PPR_SESSION_SECRET is set (long enough)")
    except SettingsError as exc:
        rep.add("FAIL", "settings", str(exc))

    def get(name: str) -> str:
        return (env.get(name) or "").strip()

    ui = []
    if get("PPR_UI_SECURE_COOKIES") != "1":
        ui.append("PPR_UI_SECURE_COOKIES=1 (HTTPS)")
    proxy = get("PPR_UI_TRUST_PROXY")
    if not proxy:
        ui.append("PPR_UI_TRUST_PROXY (the reverse proxy)")
    if proxy == "loopback" and get("PPR_UI_HOST") not in LOCAL_HOSTS:
        ui.append("PPR_UI_HOST=127.0.0.1 (proxy on this server)")
    api = get("PPR_API_URL") or "http://127.0.0.1:8000"
    if not LOCAL_API.match(api):
        ui.append("PPR_API_URL on this server (http://127.0.0.1:<port>)")
    rep.add(
        "FAIL" if ui else "PASS",
        "settings",
        "user interface behind HTTPS" + (": set " + "; ".join(ui) if ui else ""),
    )
    backup = get("PPR_BACKUP_DIR")
    rep.add(
        "PASS" if backup else "TODO",
        "settings",
        f"backups to {backup}"
        if backup
        else "set PPR_BACKUP_DIR (copied off the server per policy)",
    )


def _hosxp_settings(rep: CheckReport, settings: Settings) -> QueryConfig | None:
    if settings.hosxp_mode != "sql":
        rep.add("FAIL", "HOSxP", "PPR_HOSXP_MODE must be sql (the real HOSxP)")
    try:
        settings.require_hosxp_database_url()
        rep.add("PASS", "HOSxP", "PPR_HOSXP_DATABASE_URL is set")
    except SettingsError as exc:
        rep.add("FAIL", "HOSxP", str(exc))
    rep.add(
        "INFO",
        "HOSxP",
        "sign-in reads "
        + (
            "a separate login server (PPR_HOSXP_AUTH_DATABASE_URL, D-41)"
            if settings.hosxp_auth_database_url
            else "the same HOSxP database (PPR_HOSXP_AUTH_DATABASE_URL not set)"
        ),
    )
    try:
        cfg = load_config(settings.hosxp_queries_file)
    except HosxpConfigurationError as exc:
        rep.add("FAIL", "HOSxP", str(exc))
        return None
    missing = cfg.missing_needed()
    rep.add(
        "FAIL" if missing else "PASS",
        "HOSxP",
        f"{settings.hosxp_queries_file}: "
        + ("missing " + ", ".join(missing) if missing else "every needed query is configured"),
    )
    return cfg


def _open_items(rep: CheckReport, cfg: QueryConfig | None) -> None:
    rep.add("OPEN", "G-4", "which HOSxP status closes a PPR is not decided")
    if cfg is None:
        rep.add("OPEN", "PR cancellation (D-30)", "query file not loaded")
        rep.add("OPEN", "stock (D-39)", "query file not loaded")
        return
    cancelled = [
        n
        for n, s in cfg.status_mapping.native_to_normalized.items()
        if s is HosxpPrStatus.CANCELLED
    ]
    if cancelled:
        rep.add(
            "OPEN",
            "PR cancellation (D-30)",
            "a CANCELLED mapping is configured; HOSxP evidence pending (checklist C2)",
        )
    else:
        rep.add("OPEN", "PR cancellation (D-30)", "no native status maps to CANCELLED")
    if cfg.stock.usable:
        rep.add("INFO", "stock (D-39)", "verified [stock_movement] query configured")
    else:
        rep.add("OPEN", "stock (D-39)", f"{cfg.stock.problem}; the report shows ยังไม่มีข้อมูล")
