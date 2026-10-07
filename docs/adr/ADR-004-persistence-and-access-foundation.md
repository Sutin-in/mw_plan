# ADR-004 — Persistence and access-control foundation (Wave 1B)

- **Status:** Accepted with Wave 1B (2026-09-28)
- **Scope:** schema foundation, migrations, append-only audit, RBAC skeleton, application sessions, local DB setup. No plan/PPR/ledger tables yet; no real HOSxP.

## Decisions

### 1. Schema and migrations
- SQLAlchemy Core tables in `ppr/infrastructure/db/schema.py`; Alembic migration `0001_foundation`.
- The database URL is never stored in `alembic.ini`; it comes from settings (`PPR_DATABASE_URL`) or is passed in by tests/CLI.
- Tests prove: upgrade → downgrade → upgrade round-trip, and **zero drift** between the models and the migrated schema.
- Tables: HOSxP mirrors (`hosxp_department`, `hosxp_item`, `hosxp_fund_source`, `hosxp_budget_category` with unbounded parent hierarchy), `plan_year`, `app_user`, `role` (8 seeded codes), `user_role`, `role_appointment` (HEAD_OF_PROCUREMENT only, effective range), `audit_log`, `sync_run`, `sync_change`.
- Mirrors are keyed by the HOSxP **source id**; column names are this system's, not HOSxP's.

### 2. Append-only audit (spec §36)
- A trigger rejects `UPDATE`, `DELETE` and `TRUNCATE` on `audit_log`; the application writer only inserts.
- Every audited change is written **in the same transaction** as the change (`UnitOfWork`); a failed audit write rolls the change back (tested).
- Audit rows record who (USER + id, or SYSTEM), action, entity, before/after JSON, reason, time.
- **Known limit (hardening, before production):** the table owner can disable triggers. Production must split a *migration owner* role from a *runtime* role that has only `INSERT, SELECT` on `audit_log` (and later the ledger). Recorded as a go-live item, not done in dev/test.

### 3. Authentication and sessions (spec §21)
- Credentials are verified only through `HosxpAuthenticationAdapter` (mock in Wave 1B). No HOSxP API, session or hashing scheme is assumed; **no password is stored** (tested).
- On success the app issues its **own** HMAC-SHA256 token carrying only local user id + expiry (`PPR_SESSION_SECRET`, default TTL 480 min). This is an application session, not a HOSxP one.
- Roles are **not** in the token; they are re-read from the database on every request, so revocation is immediate (tested).
- First ADMIN is bootstrapped with `python -m ppr.cli grant-role --username … --role ADMIN` (user must have logged in once), audited as SYSTEM with reason "bootstrap via CLI".

### 4. Server-side authorization (spec §22.9)
- `ppr/domain/permissions.py` maps each permission to roles. Every protected route declares one permission; a test fails if a route is added without being in the authorization matrix.
- The matrix test runs **every protected endpoint × all 8 roles**; unauthenticated/forged/expired tokens → 401, wrong role → 403.
- FINANCE/CFO/EXECUTIVE hold read permissions only. ADMIN manages roles but has **no** plan-management permission (spec §22.8, §39).

### 5. Layering
- `ppr.application` and `ppr.ports` may not import `ppr.infrastructure`, `ppr.api`, SQLAlchemy, Alembic or FastAPI (import-linter contract, with a guard self-test).

### 6. Test database safety
- Tests use `PPR_TEST_DATABASE_URL`; the database name **must end with `_test`**, otherwise tests refuse to run before any DDL.
- DB tests are **mandatory**: without a configured test DB they fail (never skip), and `check` has a separate `database` gate (PostgreSQL 16, reachable, `*_test`).
- Each test starts from an empty `public` schema migrated to head.

### 7. Local development database (Windows-first)
- `tools/dev/setup_local_db.py` (or `setup_local_db.bat`) creates only role `ppr_app` and databases `ppr_dev`, `ppr_test`; never drops anything; prompts for the superuser password without storing it; writes secrets only to git-ignored `.env`; writes a secret-free `_db_setup_result.txt`.
- On a shared dev/test server, `--name <programmer>` creates a separate role `ppr_<name>` with databases `ppr_<name>_dev` / `ppr_<name>_test`, and revokes `CONNECT` from `PUBLIC` on them, so programmers cannot reach each other's databases and test runs never collide.

## Open items carried forward
- Overlapping Head-of-Procurement appointments are not yet prevented at the DB level; the appointment rules and verification land in Wave 5 (spec §23, §40.9).
- A PR→PPR binding table with the unconditional unique constraint (D-03) lands with PPR persistence (Wave 3/4).
- Production role split (see §2) and backup/restore drill (spec §38.3) before go-live.
