"""Least-privilege runtime database account (spec §39; ADR-004 hardening; Wave 10A).

Production uses two PostgreSQL accounts:

* the *schema owner* - runs the migrations (``ppr.cli migrate`` with
  ``PPR_MIGRATION_DATABASE_URL``) and owns every table;
* the *runtime* account - the application's ``PPR_DATABASE_URL``. It may read and write rows,
  but only SELECT and INSERT on the append-only evidence tables (audit, ledger, versions,
  bindings, verifications, observations, amendment history), so not even a defect could change
  evidence through it; and it owns nothing, so it cannot disable the protecting triggers or
  change the schema.

The append-only tables are found from the database itself (the tables with a
``*_no_update_delete`` trigger), so a new evidence table is covered by its migration alone.
"""

from __future__ import annotations

from sqlalchemy import Connection, text

SCHEMA = "public"
_APPEND_ONLY_SQL = """
    SELECT DISTINCT c.relname
    FROM pg_trigger t
    JOIN pg_class c ON c.oid = t.tgrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE NOT t.tgisinternal AND n.nspname = :s AND t.tgname LIKE '%\\_no\\_update\\_delete'
    ORDER BY 1
"""
_TABLES_SQL = "SELECT tablename FROM pg_tables WHERE schemaname = :s ORDER BY 1"


def append_only_tables(conn: Connection) -> list[str]:
    return [r[0] for r in conn.execute(text(_APPEND_ONLY_SQL), {"s": SCHEMA})]


def _ident(name: str) -> str:
    """A quoted identifier (role names are checked by the settings, table names come from
    the catalog)."""
    return '"' + name.replace('"', '""') + '"'


def grant_statements(role: str, tables: list[str], append_only: list[str]) -> list[str]:
    r = _ident(role)
    out = [
        f"REVOKE ALL ON ALL TABLES IN SCHEMA {SCHEMA} FROM {r}",
        f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {SCHEMA} FROM {r}",
        f"GRANT USAGE ON SCHEMA {SCHEMA} TO {r}",
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {SCHEMA} TO {r}",
    ]
    for t in tables:
        if t == "alembic_version":
            rights = "SELECT"  # the server checks the schema version; never changes it
        elif t in append_only:
            rights = "SELECT, INSERT"
        else:
            rights = "SELECT, INSERT, UPDATE, DELETE"
        out.append(f"GRANT {rights} ON {SCHEMA}.{_ident(t)} TO {r}")
    return out


def grant_runtime(conn: Connection, role: str) -> list[str]:
    """Give ``role`` exactly the runtime rights (idempotent; run as the schema owner after
    every migration). Returns the statements run."""
    tables = [r[0] for r in conn.execute(text(_TABLES_SQL), {"s": SCHEMA})]
    stmts = grant_statements(role, tables, append_only_tables(conn))
    for stmt in stmts:
        conn.execute(text(stmt))
    return stmts


def runtime_account_issues(conn: Connection) -> list[str]:
    """Why the connected account is NOT a least-privilege runtime account (empty = fine)."""
    issues: list[str] = []
    user, superuser = conn.execute(
        text("SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user")
    ).one()
    if superuser:
        issues.append(f"{user} is a superuser")
    # Rights reached through role membership count too (SET ROLE, even without INHERIT).
    for (role,) in conn.execute(
        text(
            "SELECT r.rolname FROM pg_roles r WHERE r.rolname <> current_user "
            "AND pg_has_role(current_user, r.oid, 'MEMBER') "
            "AND (r.rolsuper OR r.rolcreaterole OR r.rolcreatedb OR r.rolbypassrls "
            "OR r.rolname IN (SELECT tableowner FROM pg_tables WHERE schemaname = :s))"
        ),
        {"s": SCHEMA},
    ):
        issues.append(f"{user} is a member of {role}, which owns tables or has admin rights")
    attrs = conn.execute(
        text(
            "SELECT rolcreaterole, rolcreatedb, rolbypassrls FROM pg_roles "
            "WHERE rolname = current_user"
        )
    ).one()
    for flag, name in zip(attrs, ("CREATEROLE", "CREATEDB", "BYPASSRLS"), strict=True):
        if flag:
            issues.append(f"{user} has {name}")
    owned: int = conn.execute(
        text("SELECT count(*) FROM pg_tables WHERE schemaname = :s AND tableowner = current_user"),
        {"s": SCHEMA},
    ).scalar_one()
    if owned:
        issues.append(f"{user} owns {owned} table(s) (it could disable the evidence triggers)")
    if conn.execute(
        text("SELECT has_schema_privilege(current_user, :s, 'CREATE')"), {"s": SCHEMA}
    ).scalar_one():
        issues.append(f"{user} may create objects in schema {SCHEMA}")
    if conn.execute(
        text("SELECT has_database_privilege(current_user, current_database(), 'CREATE')")
    ).scalar_one():
        issues.append(f"{user} may create schemas in this database")
    append_only = set(append_only_tables(conn))
    if not append_only:
        issues.append("no append-only tables found (is the schema migrated?)")
    for t in sorted(append_only):
        for right in ("UPDATE", "DELETE", "TRUNCATE"):
            if conn.execute(
                text("SELECT has_table_privilege(current_user, :t, :r)"),
                {"t": f"{SCHEMA}.{t}", "r": right},
            ).scalar_one():
                issues.append(f"{user} has {right} on append-only table {t}")
    for (t,) in conn.execute(text(_TABLES_SQL), {"s": SCHEMA}):
        needed = ("SELECT",) if t == "alembic_version" else ("SELECT", "INSERT")
        for right in needed:
            if not conn.execute(
                text("SELECT has_table_privilege(current_user, :t, :r)"),
                {"t": f"{SCHEMA}.{t}", "r": right},
            ).scalar_one():
                issues.append(f"{user} lacks {right} on {t} (run: ppr.cli migrate)")
    return issues
