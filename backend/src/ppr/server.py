"""Composition root for the running API server (Wave 5A, D-24 .. D-26).

Builds ``AppDeps`` from settings and starts uvicorn. Like ``ppr.bootstrap`` it sits
outside the core, so it may choose the concrete HOSxP adapters:

* ``sql`` mode: the read-only SQL gateway and the MD5 login adapter (D-25), both driven
  by the site query file;
* demo mode (D-26): mock HOSxP with demo users and data; refused in ``sql`` mode, and
  refused on a ``*_test`` database (tests wipe it).

The server refuses to start when the database schema is not at the latest migration,
so it never runs against a half-migrated database.

Production profile (``PPR_PROFILE=production``, Wave 10A) also refuses: demo mode, a
development or test database, a runtime database account that is not least-privilege
(``privileges.runtime_account_issues``) and listening beyond this machine (the API is reached
only by the user interface on the same server; HTTPS ends at the reverse proxy).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from ppr import demo as demo_mode
from ppr.api.app import AppDeps, create_app
from ppr.auth.session import SessionSigner
from ppr.bootstrap import hosxp_engine
from ppr.config.fiscal_year import load_fiscal_year_config
from ppr.config.settings import Settings, SettingsError, database_name, load_environment
from ppr.domain.fiscal_year import fiscal_year_label
from ppr.infrastructure.db.mockdata import mock_data_summary, production_engine
from ppr.infrastructure.db.privileges import runtime_account_issues
from ppr.integration.hosxp.auth import HosxpAuthenticationAdapter
from ppr.integration.hosxp.errors import HosxpConfigurationError
from ppr.integration.hosxp.gateway import HosxpGateway
from ppr.integration.hosxp.real.query_config import QueryConfig, load_config
from ppr.integration.hosxp.real.sql_auth import SqlHosxpAuthenticationAdapter
from ppr.integration.hosxp.real.sql_gateway import SqlHosxpGateway

BACKEND = Path(__file__).resolve().parents[2]
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


@dataclass(frozen=True)
class Built:
    app: FastAPI
    engine: Engine
    demo: bool
    notes: tuple[str, ...]


def schema_head() -> str:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    if head is None:
        raise SettingsError("no migrations found")
    return head


def require_current_schema(engine: Engine) -> None:
    head = schema_head()
    try:
        with engine.connect() as conn:
            current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except SQLAlchemyError:
        current = None
    if current != head:
        raise SettingsError(
            f"database schema is at {current or 'nothing'}, expected {head}: "
            "run  python -m ppr.cli migrate  first"
        )


def _site_queries(settings: Settings) -> QueryConfig:
    try:
        queries = load_config(settings.hosxp_queries_file)
    except HosxpConfigurationError as exc:
        raise SettingsError(str(exc)) from None
    if missing := queries.missing_needed():
        raise SettingsError(
            f"{settings.hosxp_queries_file} lacks query/queries: {', '.join(missing)} "
            "(test with: python -m ppr.cli hosxp-check)"
        )
    settings.require_hosxp_database_url()
    return queries


def build(settings: Settings, *, demo: bool, env: dict[str, str] | None = None) -> Built:
    env = env if env is not None else load_environment()
    url = settings.require_database_url()
    secret = settings.require_session_secret()
    cfg = load_fiscal_year_config(env)
    notes: list[str] = []

    gateway: HosxpGateway
    auth: HosxpAuthenticationAdapter
    if settings.production:
        if demo:
            raise SettingsError("production: demo mode is not allowed (PPR_PROFILE=production)")
        name = database_name(url).lower()
        if name.endswith(("_test", "_dev")):
            raise SettingsError(f"production: database {name!r} is a development or test one")
    if demo:
        if settings.hosxp_mode != "mock":
            raise SettingsError("demo mode runs only with the mock HOSxP (PPR_HOSXP_MODE=mock)")
        test_url = settings.test_database_url
        if database_name(url).lower().endswith("_test") or (test_url and url == test_url):
            raise SettingsError("demo mode must not use a *_test database (tests wipe it)")
    elif settings.hosxp_mode == "mock":
        raise SettingsError(
            "the mock HOSxP has no users: start with --demo, or set PPR_HOSXP_MODE=sql"
        )

    # sql mode: the site query file is checked before anything connects.
    queries = None if demo else _site_queries(settings)

    engine = create_engine(url, pool_pre_ping=True)
    if settings.production:
        engine = production_engine(engine)  # D-40: MOCK data never reaches the Plan Ledger
    try:
        require_current_schema(engine)
        if settings.production:
            with engine.connect() as conn:
                issues = runtime_account_issues(conn)
                mock = mock_data_summary(conn)
            if issues:
                raise SettingsError(
                    "production: PPR_DATABASE_URL must be the least-privilege runtime account: "
                    + "; ".join(issues)
                )
            if mock.found:  # D-40: a warning, not a refusal
                notes.append(
                    "WARNING: the production database holds MOCK/DEMO data ("
                    + mock.describe()
                    + "); users see a warning on every page and MOCK items are never posted "
                    "to the Plan Ledger - see first-start --check"
                )
        if demo:
            today = date.today()  # noqa: DTZ011 - server local date, as AppDeps.today
            mock_gw = demo_mode.demo_gateway(today, fiscal_year_label(today, cfg))
            mock_auth = demo_mode.demo_auth()
            report = demo_mode.seed(engine, mock_auth, mock_gw, cfg)
            gateway, auth = mock_gw, mock_auth
            notes.append(
                f"DEMO MODE (D-26): mock HOSxP, fiscal year {report.fiscal_year}, "
                f"{report.users} demo users (password {demo_mode.DEMO_PASSWORD}); "
                + ("plan seeded" if report.plan_created else "plan already present")
            )
        else:
            assert queries is not None
            sql_gw = SqlHosxpGateway(hosxp_engine(settings.require_hosxp_database_url()), queries)
            # D-41: the sign-in queries may read a separate HOSxP login server.
            auth_gw = (
                SqlHosxpGateway(hosxp_engine(settings.hosxp_auth_database_url), queries)
                if settings.hosxp_auth_database_url
                else sql_gw
            )
            gateway, auth = sql_gw, SqlHosxpAuthenticationAdapter(auth_gw)
            notes.append(f"HOSxP: read-only SQL, queries from {settings.hosxp_queries_file}")
            if auth_gw is not sql_gw:
                notes.append("HOSxP sign-in: separate login server (PPR_HOSXP_AUTH_DATABASE_URL)")
    except Exception:
        engine.dispose()
        raise

    deps = AppDeps(
        engine,
        auth,
        SessionSigner(secret, timedelta(minutes=settings.session_ttl_minutes)),
        cfg,
        gateway,
        demo=demo,
        production=settings.production,
    )
    return Built(create_app(deps), engine, demo, tuple(notes))


def is_loopback(host: str) -> bool:
    import ipaddress

    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def serve(*, demo: bool, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    import uvicorn

    settings = Settings.load()
    if settings.production and not is_loopback(host):
        raise SettingsError(
            f"production: the API listens on this machine only (got --host {host}); the user "
            "interface on the same server calls it, and HTTPS ends at the reverse proxy"
        )
    built = build(settings, demo=demo)
    for note in built.notes:
        print(note)  # noqa: T201 - start-up banner
    print(f"API listening on http://{host}:{port}  (health: /api/health)")  # noqa: T201
    try:
        uvicorn.run(built.app, host=host, port=port, log_level="info")
    finally:
        built.engine.dispose()
