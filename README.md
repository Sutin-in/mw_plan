# Hospital Plan & PPR Control System — Phase 1

Controls which approved annual plan each HOSxP Purchase Requisition (PR) consumes, through a
**PPR (คำขอใช้แผนประกอบ PR)**, without modifying or replacing HOSxP.

- **Source of Truth:** [`docs/PHASE1_TECHNICAL_SPEC.md`](docs/PHASE1_TECHNICAL_SPEC.md) (v1.12, Decision Record D-01 … D-31, design gates G-1 … G-4)
- **Assessment & wave plan:** [`docs/PHASE1_ASSESSMENT.md`](docs/PHASE1_ASSESSMENT.md)
- **Decisions:** [`docs/adr/`](docs/adr/)
- **Traceability (generated):** [`docs/traceability.md`](docs/traceability.md)
- **New to the project? Start here:** [`docs/HANDOFF.md`](docs/HANDOFF.md) · server setup: [`docs/SERVER_SETUP_WINDOWS.md`](docs/SERVER_SETUP_WINDOWS.md)

Phase 1 = Plan + PPR + HOSxP integration + control + tracking + audit.
**PlanFin, GL, PO creation, payment and HOSxP write-back are out of scope** and blocked by `phase_guard`.

## Status

| Wave | Content | Status |
|---|---|---|
| 0 | Skeleton, guardrails, check command, ADRs | Done |
| 1A | Pure domain rules + HOSxP contracts/Protocols + mock | Done (commit 1d5d395) |
| 1B | Persistence foundation: PostgreSQL schema + Alembic, append-only audit, sessions, server-side RBAC, API skeleton | Done (commit 9e668d5) |
| 2 | Plan Core: master sync (mock), budgets, Plan Items, envelope validation, activation with `PLAN_ACTIVATED` ledger posting, D-15 corrections | Done (commit 5a16809) |
| pre-3 | Hardening: ledger event `PLAN_APPROVED` → `PLAN_ACTIVATED` (migration 0003), doc refresh, GitHub Actions CI | Done (merged, commit c3efc67) |
| 3 | PR retrieval / Pre-PPR validation (live, read-only; D-16, D-17, D-18) — ADR-006 | Done (merged) |
| 4 | PPR draft, allocation, confirm/lock, numbering, ledger, unlock/reconfirm, versions, A4 print (D-19 … D-23) — ADR-008 | Done (merged, commit 297a1ec; tag `phase1-wave4-baseline`) |
| 5A | User interface (Node.js + Express, D-24), API server, HOSxP MD5 login compatibility adapter (D-25, provisional until real-HOSxP evidence), demonstration mode (D-26) — ADR-009 | Done (merged, commit 4860920; tag `phase1-wave5a-baseline`) |
| 5B | Head of Procurement appointment and verification (D-27, D-28), procurement queue, search, timeline — ADR-010 | Done (merged, commit 88075e5; tag `phase1-wave5b-baseline`) |
| 6 | PR synchronization, change detection, HOSxP cancellation (empty mapping until IT evidence), invalid-evidence rules (D-29 … D-31) — ADR-011 | Done (merged, commit 26b9b9f; tag `phase1-wave6-baseline`) |
| 8A | Role dashboards, derived in-app alerts with ADMIN-set thresholds, plan management screens (D-32 … D-34); migration 0007 — ADR-012 | Done (merged, commit 1bfbbfa) |
| 8B | Nine §37 reports on screen and as Excel (`.xlsx` from the API, exports audited) (D-35) — ADR-013 | Done (merged, commit f4751ba; verified on Windows, see ADR-013) |
| 7A | Plan Amendment: Director-approved changes to an ACTIVE plan with append-only before/after history, `PLAN_AMENDMENT` ledger entries, preview, screens and the Plan Amendment report (D-37); migration 0008 — ADR-014 | Done (merged, commit d36ed48; verified on Windows, see ADR-014) |
| 7B-1 | Central Pool (D-38): central-store PRs draw on `CENTRAL` items, ambiguity fails closed, purchaser-only visibility, Central Plan Usage report — ADR-015 | Done (merged, commit 5b36ebc; verified on Windows, see ADR-015) |
| 7B-2 | Assigned Purchase (D-38): coverage per demand department, derived demand states, blocking while demand is open, demand amendment; migration 0009 — ADR-016 | Done (merged, commit 8e6b695; verified on Windows, see ADR-016) |
| 10A | Production readiness (no real HOSxP): concurrency / idempotency / isolation suites, §40 acceptance report, production profile and role split, supervisor and scheduled tasks, backup/restore drill — ADR-017, `docs/DEPLOY_PRODUCTION.md` | Done (merged, commit 71f7a11; verified on Windows, see ADR-017) |
| 10B-1 | Stock issues / returns are HOSxP data for the *Central Plan Usage* report only (D-39): refused by the Plan Ledger in the application and the database (migration 0010), read live through a verified site query, never stored, never split by fund, own-department view for other departments — ADR-018 | Done (merged, commit 6b825a5; verified on Windows, see ADR-018; baseline tag `phase1-wave10b1-baseline`) |
| 10B-2 | IT hand-over: `docs/IT_HANDOFF.md`, `ppr.cli hosxp-verify`, `ppr.cli first-start --check`, `docs/GO_LIVE_CHECKLIST.md` (OPEN items never PASS, product-owner sign-off); MOCK/DEMO data in production warned everywhere and never posted to the Plan Ledger; production sign-in through HOSxP only (D-40) — ADR-019 | Done (merged, commit 0f0c3d8; verified on Windows, see ADR-019; baseline tag `phase1-wave10b2-baseline`) |
| 11A | Sign-in on a separate HOSxP login server (`PPR_HOSXP_AUTH_DATABASE_URL`, read-only); the PPR takes the PR's department; only the requester who created a PPR edits / confirms it and a REQUESTER-only user reads only their own PPRs; Planning / Admin discard an abandoned draft with a reason; `hosxp-check --user`, `hosxp-verify` checks the login account (D-41) — ADR-020 | Done (merged, commit 3ad5c4a; verified on Windows, see ADR-020; baseline tag `phase1-wave11a-baseline`) |
| 11B | Hospital-size master sync (batched upsert under PostgreSQL's 65,535-parameter limit, one transaction); database passwords hidden whatever characters they hold (`redact_url`) — ADR-021 | Done (merged, commit 233541a; verified on Windows, see ADR-021; baseline tag `phase1-wave11b-baseline`) |
| 12A-1 | Plan imported from Excel (D-42): template with HOSxP code lists, preview, all-or-nothing import into a DRAFT year, rows without a HOSxP item until bound, removal with a reason while DRAFT; migration 0011 — ADR-022 | Done (merged, commit b710106; verified on Windows, see ADR-022; baseline tag `phase1-wave12a1-baseline`) |
| 12A-2 | The requester chooses the plan row of every PR line on every PPR (D-43): rows offered by department, fund source and unit, no automatic match, nothing remembered; choices kept with the PPR and changed while DRAFT / unlocked; plan rows without a HOSxP item (template without item column; code optional on screen and by amendment); migration 0012 — ADR-023 | Done (merged, commit fe72a75; verified on Windows, see ADR-023; baseline tag `phase1-wave12a2-baseline`) |
| HOSxP | Real HOSxP connection: read-only SQL adapter configured per site (ADR-007, `docs/HOSXP_CONNECTION.md`) | Built and tested on PostgreSQL + MariaDB; needs the hospital's read-only account and query file |

## Layout

```text
backend/
  src/ppr/
    domain/              pure rules (no frameworks, no I/O)
    integration/hosxp/   normalized contracts, Protocols, status mapping
      mock/              MockHosxpGateway, MockHosxpAuthenticationAdapter, sample MOCK-* masters
      real/              generic read-only SQL adapter; HOSxP names only in the site file (ADR-007)
    config/              settings (.env / environment) and fiscal-year boundaries
    ports.py             interfaces the application layer depends on
    application/         use cases (login, roles, plan year, plan core) - no SQL/HTTP imports
    infrastructure/db/   SQLAlchemy schema + repositories (PostgreSQL)
    auth/                application session tokens
    api/                 FastAPI app; server-side authorization on every route
    cli.py               migrate, bootstrap first ADMIN
  migrations/            Alembic migrations
  tests/
    unit/                domain, integration, config, auth
    db/                  PostgreSQL + API tests (mandatory)
    guards/              self-tests proving every guard can fail
tools/
  check.py               the single repo-local gate
  validators/            phase_guard.py, traceability.py, layering.py, db_ready.py
  dev/                   setup_local_db.py (+ .bat for Windows)
docs/
```

## Local database (PostgreSQL 16)

Docker is **not** required. Install PostgreSQL 16 natively, then from the repository root:

```powershell
.venv\Scripts\python -m pip install -e "backend[dev]"
tools\dev\setup_local_db.bat          # or: .venv\Scripts\python tools\dev\setup_local_db.py
```

On a shared server add `--name <programmer>` so each person gets their own role and
databases (`ppr_<name>`, `ppr_<name>_dev`, `ppr_<name>_test`).

This creates only role `ppr_app` and databases `ppr_dev` and `ppr_test`, writes connection
strings and a session secret to `.env` (git-ignored), and writes a secret-free report to
`_db_setup_result.txt`. Then migrate the development database and bootstrap the first admin:

```powershell
.venv\Scripts\python -m ppr.cli migrate
.venv\Scripts\python -m ppr.cli grant-role --username <hosxp-username> --role ADMIN
```

Test runs use `ppr_test` only; tests refuse any database whose name does not end in `_test`.

## Running the check

Requires Python 3.12+ and the test database above.

```powershell
# Windows (PowerShell), from the repository root
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e "backend[dev]"
.venv\Scripts\python tools\check.py
```

```bash
# Linux/macOS
python3.12 -m venv .venv
.venv/bin/python -m pip install -e "backend[dev]"
.venv/bin/python tools/check.py
```

`check` runs nine gates and exits non-zero if any fails: ruff lint, ruff format, mypy (strict on
`ppr.domain` and `ppr.integration`), import-linter layering, `phase_guard`, database readiness,
traceability, frontend (Node.js ≥ 20; the Express UI parses and its tests pass), pytest (including
mandatory PostgreSQL and API tests).
Add `--write-traceability` to regenerate `docs/traceability.md`.

## Traceability convention

Tests declare what they verify: `@pytest.mark.spec("AT-40.2.2", "D-01", "S-13")`.

- `AT-40.x.y` — y-th bullet of spec §40.x (acceptance baseline)
- `D-0n` — decision record entry; `G-n` — design gate; `S-n[.m]` — spec section

IDs are parsed from the spec itself; a reference to an ID that does not exist fails the check.

**Evidence level:** in Waves 0–1A, `AT-*` tags are *rule-level* evidence (pure domain functions).
Full acceptance for the same IDs also needs persistence, API and authorization tests in later
waves; the traceability report counts references, not end-to-end completeness.

## Non-negotiables (spec §46)

1. No invented HOSxP APIs, tables, fields, status codes or authentication details. Mock ids are `MOCK-*`; real HOSxP table/column names live only in the site file `hosxp_queries.toml` (ADR-007).
2. Hard controls are quantity **and** amount; unit price and Q1–Q4 never block.
3. Remaining is derived from the append-only Plan Ledger — never overwritten.
4. PPR state (7 internal states) ≠ HOSxP PR status (separate, normalized field).
5. One confirmed PPR per HOSxP PR number, for life; drafts don't bind.
6. Authorization is server-side on every route; roles are re-read per request.
7. The audit log and the Plan Ledger are append-only (database triggers); audit is written in the same transaction as the change.
8. Plans change freely in DRAFT, only as documented corrections in APPROVED (D-15), and only by Plan Amendment after ACTIVE.
