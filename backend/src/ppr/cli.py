"""Administrative command line.

    python -m ppr.cli migrate                               # upgrade the dev DB to head
    python -m ppr.cli grant-role --username NAME --role ADMIN
    python -m ppr.cli hosxp-schema --match pr,purchase,request --out hosxp_schema.md
    python -m ppr.cli hosxp-check [--pr PR_NO] [--user NAME] # test the site HOSxP queries
    python -m ppr.cli serve [--demo] [--host H] [--port P]  # run the API server (Wave 5A)
    python -m ppr.cli sync-prs --mode NIGHTLY               # PR synchronization (Wave 6)

``sync-prs`` is what the hospital's scheduler (Windows Task Scheduler) runs; the schedule
itself (e.g. 02:00, only an example) belongs to that scheduler and is set by hospital IT
(D-29). It reads HOSxP through the site query file (PPR_HOSXP_MODE=sql). Exit code 0 =
SUCCEEDED, 1 = PARTIAL or FAILED (see the run in the application), 2 = configuration
problem, 3 = another full synchronization is still running.

``grant-role`` exists to bootstrap the first ADMIN (nobody can grant roles through the
API before an admin exists). The user must have logged in once. The grant is written
to the audit log as a SYSTEM action with the reason "bootstrap via CLI".
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url

from ppr.bootstrap import hosxp_engine
from ppr.config.settings import Settings, SettingsError, database_name, redact_url
from ppr.domain.roles import Role
from ppr.infrastructure.db.privileges import grant_runtime
from ppr.infrastructure.db.repositories import unit_of_work
from ppr.integration.hosxp.errors import HosxpError
from ppr.integration.hosxp.gateway import HosxpGateway
from ppr.ports import SYSTEM_ACTOR, AuditEntry

BACKEND = Path(__file__).resolve().parents[2]


def alembic_config(url: str) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    cfg.attributes["url"] = url
    return cfg


def _migrate(settings: Settings) -> int:
    """Upgrade as the schema owner, then give the runtime role exactly its rights."""
    try:
        url = settings.migration_url()
        if settings.production and not settings.runtime_role:
            raise SettingsError("production: set PPR_RUNTIME_ROLE (the runtime account's role)")
    except SettingsError as exc:
        print(f"error: {exc}")
        return 2
    print(f"migrating {redact_url(url)}")
    command.upgrade(alembic_config(url), "head")
    if settings.runtime_role:
        engine = create_engine(url)
        try:
            with engine.begin() as conn:
                stmts = grant_runtime(conn, settings.runtime_role)
        finally:
            engine.dispose()
        print(f"granted runtime rights to role {settings.runtime_role} ({len(stmts)} statements)")
    return 0


def _backup_command(args: argparse.Namespace, settings: Settings) -> int:
    from ppr import ops_backup

    try:
        if args.cmd == "backup":
            res = ops_backup.backup(settings.migration_url(), Path(args.out))
            m = res.manifest
            rows = sum(t["rows"] for t in m["tables"].values())
            print(f"backup: {res.dump} ({res.dump.stat().st_size} bytes)")
            print(
                f"manifest: {res.manifest_file}: schema {m['schema_version']}, "
                f"{len(m['tables'])} tables, {rows} rows, evidence {ops_backup.ledger_digest(m)}"
            )
            return 0
        owner = settings.migration_url()
        target = make_url(owner).set(database=args.target_db).render_as_string(hide_password=False)
        if args.cmd == "restore":
            live = {database_name(u) for u in (settings.database_url, owner) if u}
            ops_backup.restore(
                Path(args.dump),
                target,
                source_url=settings.database_url,
                replace=args.replace,
                protected=tuple(live) if settings.production else (),
            )
            print(f"restored {args.dump} into {redact_url(target)}")
            return 0
        problems = ops_backup.verify(Path(args.manifest), target)
    except SettingsError as exc:
        print(f"error: {exc}")
        return 2
    for p in problems:
        print(f"FAIL  {p}")
    print("verify-restore: " + ("FAIL" if problems else "PASS - identical to the backup"))
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ppr.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser(
        "migrate",
        help="upgrade the database to the latest schema (as the schema owner) and grant the "
        "runtime role its rights (PPR_RUNTIME_ROLE)",
    )
    b = sub.add_parser("backup", help="dump the database and its manifest (spec 38.3)")
    b.add_argument("--out", required=True, help="folder for the .dump and .manifest.json")
    r = sub.add_parser("restore", help="load a dump into another, empty database")
    r.add_argument("--dump", required=True)
    r.add_argument(
        "--target-db",
        required=True,
        help="name of the target database on the same server; the schema owner's account "
        "(PPR_MIGRATION_DATABASE_URL) is used, so no password is typed on the command line",
    )
    r.add_argument(
        "--replace",
        action="store_true",
        help="replace a non-empty *_test / *_drill / *_restore target database",
    )
    vr = sub.add_parser("verify-restore", help="compare a restored database with the manifest")
    vr.add_argument("--manifest", required=True)
    vr.add_argument("--target-db", required=True, help="name of the restored database")
    o = sub.add_parser(
        "ops-status",
        help="operations check: database, schema version, last nightly and master sync",
    )
    o.add_argument(
        "--max-nightly-age-hours",
        type=float,
        default=26.0,
        help="fail when the last successful nightly sync is older (default 26)",
    )
    g = sub.add_parser("grant-role", help="bootstrap a role for an existing user")
    g.add_argument("--username", required=True)
    g.add_argument("--role", required=True, choices=[r.value for r in Role])
    s = sub.add_parser(
        "hosxp-schema",
        help="list HOSxP table/column NAMES (no row data) matching keywords, to a file",
    )
    s.add_argument("--match", default="", help="comma-separated keywords; empty = all")
    s.add_argument("--schema", default=None, help="database schema (default: the URL's)")
    s.add_argument("--out", default="hosxp_schema.md")
    c = sub.add_parser("hosxp-check", help="run the site HOSxP queries and show the result")
    c.add_argument("--pr", default=None, help="a PR number to retrieve")
    c.add_argument(
        "--user",
        default=None,
        help="a HOSxP user name: check the sign-in queries for it (no password asked or shown)",
    )
    v = sub.add_parser("serve", help="run the API server (use --demo to try it, D-26)")
    v.add_argument("--demo", action="store_true", help="mock HOSxP + demo users and plan")
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=8000)
    hv = sub.add_parser(
        "hosxp-verify",
        help="after IT connects HOSxP: read-only checks, each PASS / FAIL / OPEN (Wave 10B-2)",
    )
    hv.add_argument(
        "--pr", action="append", default=[], help="a real PR number to check (repeatable)"
    )
    hv.add_argument("--out", default=None, help="also write the report as Markdown evidence")
    fs = sub.add_parser(
        "first-start",
        help="production server ready for its first start? read-only checks (Wave 10B-2)",
    )
    fs.add_argument(
        "--check", action="store_true", required=True, help="check only (changes nothing)"
    )
    fs.add_argument("--out", default=None, help="also write the report as Markdown evidence")
    y = sub.add_parser("sync-prs", help="synchronize confirmed PPRs with their HOSxP PRs")
    y.add_argument("--mode", default="NIGHTLY", choices=["NIGHTLY"])
    args = ap.parse_args(argv)

    if args.cmd == "sync-prs":
        try:
            settings = Settings.load()
        except SettingsError as exc:
            print(f"error: {exc}")
            return 2
        return _sync_prs(settings)

    if args.cmd == "serve":
        from ppr.server import serve

        try:
            serve(demo=args.demo, host=args.host, port=args.port)
        except (SettingsError, HosxpError) as exc:
            print(f"error: {exc}")
            return 2
        return 0

    try:
        settings = Settings.load()
    except SettingsError as exc:
        print(f"error: {exc}")
        return 2
    if args.cmd in ("hosxp-schema", "hosxp-check"):
        try:
            return _hosxp_command(args, settings)
        except SettingsError as exc:
            print(f"error: {exc}")
            return 2
    if args.cmd == "migrate":
        return _migrate(settings)
    if args.cmd in ("backup", "restore", "verify-restore"):
        return _backup_command(args, settings)
    if args.cmd == "hosxp-verify":
        from datetime import date

        from ppr.config.fiscal_year import load_fiscal_year_config
        from ppr.config.settings import load_environment
        from ppr.ops_hosxp_verify import run as hosxp_verify

        out = Path(args.out) if args.out else None
        cfg = load_fiscal_year_config(load_environment())
        return hosxp_verify(settings, cfg, args.pr, out, date.today())  # noqa: DTZ011
    if args.cmd == "first-start":
        from ppr.config.settings import load_environment
        from ppr.ops_first_start import run as first_start

        return first_start(settings, load_environment(), Path(args.out) if args.out else None)
    if args.cmd == "ops-status":
        from ppr.ops_status import run as ops_status

        return ops_status(settings, args.max_nightly_age_hours)
    url = settings.require_database_url()

    engine = create_engine(url)
    try:
        with unit_of_work(engine) as uow:
            user = uow.users.get_by_username(args.username)
            if user is None:
                print(f"user {args.username!r} not found - they must log in once first")
                return 1
            role = Role(args.role)
            if uow.users.grant_role(user.id, role, None):
                uow.audit.write(
                    AuditEntry(
                        SYSTEM_ACTOR,
                        "ROLE_GRANTED",
                        "app_user",
                        str(user.id),
                        before={"roles": sorted(r.value for r in user.roles)},
                        after={"roles": sorted({*(r.value for r in user.roles), role.value})},
                        reason="bootstrap via CLI",
                    )
                )
                print(f"granted {role.value} to {args.username}")
            else:
                print(f"{args.username} already has {role.value}")
    finally:
        engine.dispose()
    return 0


def _sync_prs(settings: Settings) -> int:
    from ppr.application.errors import ConflictError
    from ppr.integration.hosxp.errors import HosxpConfigurationError
    from ppr.integration.hosxp.real.query_config import load_config
    from ppr.integration.hosxp.real.sql_gateway import SqlHosxpGateway
    from ppr.server import require_current_schema

    try:
        if settings.hosxp_mode != "sql":
            raise SettingsError("PR synchronization reads the real HOSxP: set PPR_HOSXP_MODE=sql")
        queries = load_config(settings.hosxp_queries_file)
        if missing := queries.missing_needed():
            raise SettingsError(f"{settings.hosxp_queries_file} lacks: {', '.join(missing)}")
        url = settings.require_database_url()
        hosxp_url = settings.require_hosxp_database_url()
    except (SettingsError, HosxpConfigurationError) as exc:
        print(f"error: {exc}")
        return 2
    engine = create_engine(url, pool_pre_ping=True)
    if settings.production:
        from ppr.infrastructure.db.mockdata import production_engine

        engine = production_engine(engine)  # D-40: MOCK data never reaches the Plan Ledger
    hx = hosxp_engine(hosxp_url)
    try:
        try:
            require_current_schema(engine)
        except SettingsError as exc:
            print(f"error: {exc}")
            return 2
        try:
            gateway = SqlHosxpGateway(hx, queries)
        except HosxpConfigurationError as exc:
            print(f"error: {exc}")
            return 2
        return run_nightly(engine, gateway)
    except ConflictError as exc:
        print(f"refused: {exc}")
        return 3
    finally:
        hx.dispose()
        engine.dispose()


def run_nightly(engine: Engine, gateway: HosxpGateway) -> int:
    """One scheduled PR synchronization of every open confirmed PPR (D-29)."""
    from ppr.application.sync_services import NIGHTLY, run_sync
    from ppr.domain.pr_sync import SyncScope

    result = run_sync(
        lambda: unit_of_work(engine),
        gateway,
        mode=NIGHTLY,
        scope=SyncScope.PPRS,
        requested_by=None,
    )
    r = result.run
    print(
        f"sync run {r.id}: {r.status} - checked {r.records_checked}, moved to review "
        f"{r.records_changed}, cancelled {r.records_cancelled}"
    )
    return 0 if r.status == "SUCCEEDED" else 1


def _hosxp_command(args: argparse.Namespace, settings: Settings) -> int:
    from ppr.integration.hosxp.errors import HosxpError
    from ppr.integration.hosxp.real.query_config import load_config
    from ppr.integration.hosxp.real.schema_discovery import discover, to_markdown
    from ppr.integration.hosxp.real.sql_gateway import SqlHosxpGateway

    url = settings.require_hosxp_database_url()
    engine = hosxp_engine(url)
    try:
        if args.cmd == "hosxp-schema":
            words = [w.strip() for w in args.match.split(",") if w.strip()]
            tables = discover(engine, words, schema=args.schema)
            title = f"HOSxP schema ({engine.dialect.name}; match: {', '.join(words) or 'all'})"
            Path(args.out).write_text(to_markdown(tables, title=title), encoding="utf-8")
            print(f"wrote {len(tables)} table(s)/view(s) to {args.out} (names only, no data)")
            return 0

        cfg = load_config(settings.hosxp_queries_file)
        gw = SqlHosxpGateway(engine, cfg)
        print(f"HOSxP database: {redact_url(url)}")
        auth_url = settings.hosxp_auth_database_url
        print(
            "HOSxP login database (D-41): "
            + (redact_url(auth_url) if auth_url else "the same HOSxP database")
        )
        print(f"query file: {settings.hosxp_queries_file}")
        missing = cfg.missing_needed()
        print("missing queries: " + (", ".join(missing) if missing else "none"))
        ok = not missing
        for name, fn in (
            ("get_departments", gw.get_departments),
            ("get_items", gw.get_items),
            ("get_fund_sources", gw.get_fund_sources),
            ("get_budget_categories", gw.get_budget_categories),
        ):
            if name in missing:
                continue
            try:
                print(f"{name}: {len(fn())} row(s) OK")
            except HosxpError as exc:
                ok = False
                print(f"{name}: FAILED - {exc}")
        # D-39: optional, report only - shown for IT, never part of the result.
        stock = cfg.stock
        if stock.usable and stock.verification is not None:
            v = stock.verification
            print(
                f"stock movements (optional): verified by {v.verified_by} on {v.verified_on} "
                f"({v.reference}); kinds: {', '.join(sorted(set(stock.kind_map.values())))}"
            )
        else:
            print(f"stock movements (optional): not used - {stock.problem}")
        if not _login_check(args, auth_url, engine, cfg):
            ok = False
        if args.pr:
            try:
                header = gw.get_pr(args.pr)
                if header is None:
                    print(f"PR {args.pr}: not found")
                else:
                    items = gw.get_pr_items(args.pr)
                    out = {
                        "header": header.model_dump(mode="json"),
                        "items": [i.model_dump(mode="json") for i in items],
                        "sum_of_item_amounts": str(sum(i.amount for i in items)),
                    }
                    print(json.dumps(out, ensure_ascii=False, indent=2))
            except HosxpError as exc:
                ok = False
                print(f"PR {args.pr}: FAILED - {exc}")
        print("RESULT: OK" if ok else "RESULT: PROBLEMS FOUND")
        return 0 if ok else 1
    except HosxpError as exc:
        print(f"error: {exc}")
        return 1
    finally:
        engine.dispose()


def _login_check(args: argparse.Namespace, auth_url: str | None, engine: Engine, cfg: Any) -> bool:
    """D-41: the sign-in queries' database - reachable, and (``--user``) the mapping of one
    account. Read only; never asks for, reads out or prints a password or its hash."""
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    from ppr.integration.hosxp.real.sql_auth import SqlHosxpAuthenticationAdapter
    from ppr.integration.hosxp.real.sql_gateway import SqlHosxpGateway

    auth_engine = hosxp_engine(auth_url) if auth_url else engine
    ok = True
    try:
        if auth_url:
            try:
                with auth_engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                print("login database: connected OK")
            except SQLAlchemyError as exc:
                print(f"login database: FAILED - cannot connect ({type(exc).__name__})")
                return False
        if args.user:
            auth = SqlHosxpAuthenticationAdapter(SqlHosxpGateway(auth_engine, cfg))
            try:
                c = auth.check_user(args.user)
            except HosxpError as exc:
                print(f"user {args.user}: FAILED - {exc}")
                return False
            if not c.found:
                print(f"user {args.user}: not found by get_login_user")
                ok = False
            else:
                form = "32 hex characters (MD5 form)" if c.hash_looks_md5 else "NOT MD5 form"
                print(
                    f"user {args.user}: found (source_id {c.source_id}); active: {c.active}; "
                    f"department: {'yes' if c.has_department else 'none'}; password hash: "
                    f"{form}; get_user_profile: {'found' if c.profile_found else 'NOT found'}"
                )
                ok = bool(c.hash_looks_md5 and c.profile_found)
    finally:
        if auth_engine is not engine:
            auth_engine.dispose()
    return ok


if __name__ == "__main__":
    sys.exit(main())
