"""``python -m ppr.cli hosxp-verify`` - after IT connects HOSxP, check it (Wave 10B-2).

READ ONLY: it runs the site's queries (each in a read-only transaction, through the same
adapter the system uses) and reads the HOSxP account's own privileges from the database
catalogue. It never writes, never tries a write, and stores nothing.

Every check ends PASS / FAIL / OPEN / INFO (``ops_report``). What this command cannot verify
from HOSxP data is OPEN, never PASS (D-40):

* G-4 (which HOSxP status closes a PPR) - always OPEN in Phase 1;
* the PR cancellation status mapping (D-30) - OPEN, also once IT has mapped a status (shown as
  "configured ..., HOSxP evidence pending"): a mapping in a file is not proof; the evidence is
  go-live checklist item C2;
* stock issues / returns (D-39) - OPEN until IT records a verified ``[stock_movement]`` query,
  and OPEN while movements of unmapped kinds exist;
* the HOSxP login hash format (D-25) - OPEN: proven only by a real user signing in.

No HOSxP table, column or status is assumed here: everything comes from the site query file.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from ppr.application.stock_services import read_stock
from ppr.config.settings import Settings, SettingsError, redact_url
from ppr.domain.fiscal_year import FiscalYearConfig, fiscal_year_bounds, fiscal_year_label
from ppr.integration.hosxp.contracts import StockMovementKind
from ppr.integration.hosxp.errors import HosxpConfigurationError, HosxpError
from ppr.integration.hosxp.real.query_config import QueryConfig, load_config
from ppr.integration.hosxp.real.sql_gateway import SqlHosxpGateway
from ppr.integration.hosxp.status import HosxpPrStatus
from ppr.ops_report import CheckReport

# Allow-list (review of Wave 10B-2): anything a grant names beyond these is a FAIL - an
# unknown or new privilege is never assumed harmless.
READ_PRIVILEGES = {"SELECT", "SHOW VIEW", "USAGE"}


def _split_privileges(text_: str) -> list[str]:
    """``SELECT, UPDATE (`a`, `b`), INSERT`` -> ``["SELECT", "UPDATE", "INSERT"]``: commas
    inside a column list do not split, the column list is dropped."""
    out, depth, cur = [], 0, ""
    for ch in text_:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append(cur)
            cur = ""
            continue
        if depth == 0 and ch not in "()":
            cur += ch
    out.append(cur)
    return [re.sub(r"\s+", " ", p).strip().upper() for p in out if p.strip()]


def mysql_grant_problems(grants: Sequence[str]) -> list[str]:
    """Rights beyond reading in MySQL / MariaDB ``SHOW GRANTS`` lines (empty = read-only)."""
    found: list[str] = []
    for grant in grants:
        g = str(grant).strip()
        if not g.upper().startswith("GRANT "):
            continue  # e.g. MariaDB "SET DEFAULT ROLE ..."
        m = re.match(r"GRANT (.+?) ON .+? TO ", g, re.IGNORECASE | re.DOTALL)
        if m is None:
            # "GRANT <role> TO <user>": a role's rights are not visible here - not read-only
            # until shown otherwise.
            found.append("membership of a role (check that role's rights)")
            continue
        for priv in _split_privileges(m.group(1)):
            if priv not in READ_PRIVILEGES:
                found.append(priv)
        if "WITH GRANT OPTION" in g.upper():
            found.append("GRANT OPTION")
    return sorted(set(found))


def account_write_rights(engine: Engine) -> list[str]:
    """Rights of the HOSxP account beyond reading, from the database's own catalogue.
    Empty = read-only. Raises SQLAlchemyError if the catalogue cannot be read."""
    found: list[str] = []
    with engine.connect() as conn:
        if engine.dialect.name == "postgresql":
            # Role attributes are not inherited, but a member can SET ROLE to take them:
            # check them on every role the account belongs to, itself included (D-41).
            member = "pg_has_role(current_user, oid, 'MEMBER')"
            role = conn.execute(
                text(
                    "SELECT bool_or(rolsuper), bool_or(rolcreaterole), bool_or(rolcreatedb), "
                    f"bool_or(rolbypassrls) FROM pg_roles WHERE {member}"
                )
            ).one()
            for flag, name in zip(
                role, ("superuser", "CREATEROLE", "CREATEDB", "BYPASSRLS"), strict=True
            ):
                if flag:
                    found.append(name)
            # Predefined roles that write files, run programs or write every table.
            for name in conn.execute(
                text(
                    "SELECT rolname FROM pg_roles WHERE rolname IN ('pg_execute_server_program', "
                    f"'pg_write_server_files', 'pg_write_all_data') AND {member} ORDER BY 1"
                )
            ).scalars():
                found.append(f"member of {name}")
            # A role the account belongs to but does not inherit can still be taken with SET
            # ROLE; its rights are not counted below, so the membership itself fails (D-41).
            # PostgreSQL 16+ knows whether SET ROLE is allowed at all.
            pg16 = int(conn.execute(text("SHOW server_version_num")).scalar_one()) >= 160000
            can_take = "pg_has_role(current_user, oid, 'SET')" if pg16 else member
            switchable: int = conn.execute(
                text(
                    "SELECT count(*) FROM pg_roles WHERE rolname <> current_user "
                    f"AND {can_take} AND NOT pg_has_role(current_user, oid, 'USAGE')"
                )
            ).scalar_one()
            if switchable:
                found.append(f"membership of {switchable} role(s) it can SET ROLE to")
            # has_*_privilege counts rights through roles and PUBLIC too, not only direct grants.
            user_ns = (
                "n.nspname NOT IN ('pg_catalog', 'information_schema') "
                "AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%'"
            )
            relations: int = conn.execute(
                text(
                    "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    f"WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f') AND {user_ns} AND ("
                    "has_table_privilege(c.oid, 'INSERT') OR has_table_privilege(c.oid, 'UPDATE') "
                    "OR has_table_privilege(c.oid, 'DELETE') "
                    "OR has_table_privilege(c.oid, 'TRUNCATE') "
                    "OR has_table_privilege(c.oid, 'TRIGGER') "
                    "OR has_table_privilege(c.oid, 'REFERENCES'))"
                )
            ).scalar_one()
            if relations:
                found.append(f"write rights on {relations} table(s) / view(s)")
            columns: int = conn.execute(
                text(
                    "SELECT count(*) FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    f"WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f') AND {user_ns} "
                    "AND a.attnum > 0 AND NOT a.attisdropped AND ("
                    "has_column_privilege(c.oid, a.attnum, 'INSERT') "
                    "OR has_column_privilege(c.oid, a.attnum, 'UPDATE') "
                    "OR has_column_privilege(c.oid, a.attnum, 'REFERENCES'))"
                )
            ).scalar_one()
            if columns:
                found.append(f"write rights on {columns} column(s)")
            sequences: int = conn.execute(
                text(
                    "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    f"WHERE c.relkind = 'S' AND {user_ns} AND ("
                    "has_sequence_privilege(c.oid, 'UPDATE') "
                    "OR has_sequence_privilege(c.oid, 'USAGE'))"
                )
            ).scalar_one()
            if sequences:
                found.append(f"UPDATE/USAGE on {sequences} sequence(s)")
            creatable: int = conn.execute(
                text(
                    f"SELECT count(*) FROM pg_namespace n WHERE {user_ns} "
                    "AND has_schema_privilege(n.oid, 'CREATE')"
                )
            ).scalar_one()
            if creatable:
                found.append(f"CREATE in {creatable} schema(s)")
            if conn.execute(
                text("SELECT has_database_privilege(current_database(), 'CREATE')")
            ).scalar_one():
                found.append("CREATE on the database")
        else:  # MySQL / MariaDB
            found += mysql_grant_problems([str(g[0]) for g in conn.execute(text("SHOW GRANTS"))])
        conn.rollback()
    return sorted(set(found))


def run(
    settings: Settings,
    fy_config: FiscalYearConfig,
    prs: Sequence[str],
    out: Path | None,
    today: date,
    *,
    engine_factory: Callable[[str], Engine] | None = None,
) -> int:
    from ppr.bootstrap import hosxp_engine

    rep = CheckReport("hosxp-verify")
    try:
        url = settings.require_hosxp_database_url()
        cfg: QueryConfig = load_config(settings.hosxp_queries_file)
    except (SettingsError, HosxpConfigurationError) as exc:
        rep.add("FAIL", "setup", str(exc))
        return rep.finish(out)
    rep.add("INFO", "setup", f"HOSxP database {redact_url(url)}")
    rep.add("PASS", "query file", f"{settings.hosxp_queries_file} loads; every query is read-only")
    missing = cfg.missing_needed()
    rep.add(
        "FAIL" if missing else "PASS",
        "query file",
        "missing queries: " + (", ".join(missing) if missing else "none"),
    )
    engine = (engine_factory or hosxp_engine)(url)
    auth_url = settings.hosxp_auth_database_url
    try:
        _account(rep, engine)
        if auth_url:  # D-41: the sign-in queries read a separate login server
            rep.add("INFO", "login account", f"HOSxP login database {redact_url(auth_url)}")
            auth_engine = (engine_factory or hosxp_engine)(auth_url)
            try:
                _account(rep, auth_engine, "login account")
            finally:
                auth_engine.dispose()
        gw = SqlHosxpGateway(engine, cfg)
        masters = _masters(rep, gw)
        for pr_no in prs:
            _pr(rep, gw, pr_no, masters)
        if not prs:
            rep.add("OPEN", "PR", "no PR checked: give --pr <a real PR number> (e.g. PR 6907912)")
        _status_mapping(rep, cfg)
        rep.add(
            "OPEN",
            "G-4",
            "which HOSxP status closes a PPR is not decided (no PPR is closed automatically)",
        )
        rep.add(
            "OPEN",
            "login (D-25)",
            "HOSxP password-hash format: proven only when a real HOSxP user signs in",
        )
        _stock(rep, gw, fy_config, today, masters)
    finally:
        engine.dispose()
    return rep.finish(out)


def _account(rep: CheckReport, engine: Engine, area: str = "account") -> None:
    try:
        rights = account_write_rights(engine)
    except SQLAlchemyError as exc:
        rep.add("FAIL", area, f"cannot reach HOSxP or read its catalogue ({type(exc).__name__})")
        return
    if rights:
        rep.add("FAIL", area, "the HOSxP account can do more than read: " + ", ".join(rights))
    else:
        rep.add(
            "PASS",
            area,
            "no right beyond reading in the database catalogue "
            "(tables, views, columns, sequences, schemas, database, roles)",
        )


def _masters(rep: CheckReport, gw: SqlHosxpGateway) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for kind, fn in (
        ("departments", gw.get_departments),
        ("items", gw.get_items),
        ("fund sources", gw.get_fund_sources),
        ("budget categories", gw.get_budget_categories),
    ):
        try:
            rows: list[Any] = list(fn())
        except HosxpError as exc:
            rep.add("FAIL", "masters", f"{kind}: {exc}")
            out[kind] = set()
            continue
        ids = [r.source_id for r in rows]
        dup = len(ids) - len(set(ids))
        active = sum(1 for r in rows if r.active is True)
        if not rows:
            rep.add("FAIL", "masters", f"{kind}: no rows")
        elif dup:
            rep.add("FAIL", "masters", f"{kind}: {dup} duplicate source_id value(s)")
        else:
            rep.add("PASS", "masters", f"{kind}: {len(rows):,} rows ({active:,} active)")
        out[kind] = set(ids)
        if kind == "budget categories":
            parents = {r.parent_source_id for r in rows if r.parent_source_id}
            if lost := parents - set(ids):
                rep.add("FAIL", "masters", f"{kind}: {len(lost)} parent id(s) not in the list")
    return out


def _pr(rep: CheckReport, gw: SqlHosxpGateway, pr_no: str, masters: dict[str, set[str]]) -> None:
    area = f"PR {pr_no}"
    try:
        header = gw.get_pr(pr_no)
        items = list(gw.get_pr_items(pr_no)) if header is not None else []
    except HosxpError as exc:
        rep.add("FAIL", area, str(exc))
        return
    if header is None:
        rep.add("FAIL", area, "not found by get_pr")
        return
    rep.add("PASS", area, f"header found; {len(items)} item line(s)")
    if header.fiscal_year is None:
        rep.add("FAIL", area, "no fiscal_year (budget year): every PPR would be refused (D-18)")
    else:
        rep.add("PASS", area, f"fiscal year {header.fiscal_year}")
    for field_name, kind, value in (
        ("department_id", "departments", header.department_id),
        ("fund_source_id", "fund sources", header.fund_source_id),
        ("budget_category_id", "budget categories", header.budget_category_id),
    ):
        if value is None:
            rep.add("FAIL", area, f"{field_name} is empty")
        elif value not in masters.get(kind, set()):
            rep.add("FAIL", area, f"{field_name} is not an id of the {kind} list (code space)")
        else:
            rep.add("PASS", area, f"{field_name} matches the {kind} list")
    unknown = sum(1 for i in items if i.item_id not in masters.get("items", set()))
    if not items:
        rep.add("FAIL", area, "no item lines")
    elif unknown:
        rep.add("FAIL", area, f"{unknown} item line(s) with an item_id not in the items list")
    else:
        rep.add("PASS", area, "every item_id matches the items list")
    total = sum((i.amount for i in items), Decimal(0))
    if header.total_amount is None:
        rep.add("INFO", area, f"no header total; sum of item amounts {total}")
    elif header.total_amount == total:
        rep.add("PASS", area, f"header total {header.total_amount} = sum of item amounts")
    else:
        rep.add(
            "INFO",
            area,
            f"header total {header.total_amount} differs from the item sum {total}: such a PR "
            "is refused for a PPR (D-17); check it is this PR's data, not the query",
        )
    native = header.native_status
    if header.status is HosxpPrStatus.UNKNOWN:
        rep.add(
            "OPEN",
            area,
            f"native status {native!r} is not mapped in [status_map] (stays UNKNOWN)",
        )
    else:
        rep.add("INFO", area, f"native status {native!r} -> {header.status.value} ([status_map])")


def _status_mapping(rep: CheckReport, cfg: QueryConfig) -> None:
    mapped = cfg.status_mapping.native_to_normalized
    cancelled = sorted(n for n, s in mapped.items() if s is HosxpPrStatus.CANCELLED)
    if not cancelled:
        rep.add(
            "OPEN",
            "PR cancellation (D-30)",
            "no native status maps to CANCELLED: cancellations in HOSxP are not recognised",
        )
    else:
        rep.add(
            "OPEN",
            "PR cancellation (D-30)",
            f"configured {', '.join(repr(c) for c in cancelled)} -> CANCELLED; HOSxP evidence "
            "pending (a mapping in a file is not proof - go-live checklist C2)",
        )


def _stock(
    rep: CheckReport,
    gw: SqlHosxpGateway,
    fy_config: FiscalYearConfig,
    today: date,
    masters: dict[str, set[str]],
) -> None:
    area = "stock (D-39)"
    source = gw.stock_movement_source()
    if not source.usable:
        rep.add("OPEN", area, f"{source.problem}; the report shows ยังไม่มีข้อมูล")
        return
    v = source.verification
    assert v is not None
    kinds = Counter(source.kind_map.values())
    rep.add(
        "INFO",
        area,
        f"verification recorded on {v.verified_on} ({v.reference}); kinds mapped: "
        + ", ".join(f"{k.value} x{kinds[k]}" for k in StockMovementKind if kinds[k]),
    )
    start, end = fiscal_year_bounds(fiscal_year_label(today, fy_config), fy_config)
    end = min(end, today)
    got = read_stock(gw, start, end)
    if not got.available:
        rep.add("FAIL", area, f"read {start}..{end}: {got.reason}")
        return
    lines = len(got.totals) + len(got.unknown)
    rep.add("PASS", area, f"read {start}..{end}: {lines:,} item/department total(s)")
    if not got.returns_known:
        rep.add("OPEN", area, "no RETURN kind mapped: returns and net use show ยังไม่มีข้อมูล")
    if got.unmapped:
        rep.add(
            "OPEN",
            area,
            f"{got.unmapped:,} movement(s) of an unmapped kind: those figures show ยังไม่มีข้อมูล",
        )
    keys = set(got.totals) | set(got.unknown)
    bad_items = {i for i, _ in keys if i not in masters.get("items", set())}
    bad_depts = {d for _, d in keys if d not in masters.get("departments", set())}
    if bad_items or bad_depts:
        rep.add(
            "FAIL",
            area,
            f"ids not in the masters: {len(bad_items)} item(s), {len(bad_depts)} department(s)",
        )
    else:
        rep.add("PASS", area, "item and department ids match the masters")
