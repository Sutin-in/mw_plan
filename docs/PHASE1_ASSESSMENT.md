# PHASE1_ASSESSMENT.md

## Hospital Plan & PPR Control System — Greenfield Repository Assessment

**Date:** 2026-09-28
**Canonical spec:** `docs/PHASE1_TECHNICAL_SPEC.md` — v1.3-draft (D-01 … D-10) when this assessment was approved; **current authoritative version is v1.12 (Decision Record D-01 … D-31, design gates G-1 … G-4)**. Where this assessment differs from the spec, the spec governs. Current wave status is in `docs/HANDOFF.md`.
**Status:** **Approved 2026-09-28 with corrections** (recorded below and in `docs/adr/`). Wave 0 + Wave 1A approved to proceed.

---

## A. Repository assessment

| Item | Finding |
|---|---|
| Repository root | `C:\Project_AI\Plan_Claude` (confirmed by user as greenfield root) |
| Git | Initialized on 2026-09-28, branch `main`, **no commits yet** |
| Contents | `docs/PHASE1_TECHNICAL_SPEC.md` only |
| Source code / tests / migrations / config | None |
| Orchestra / DA / UX-UI | **Not in this repo.** They live in a separate repository and may be used later as *external* development and validation tooling. They are not copied or recreated here unless approved. |
| Toolchain on the Windows host | **Not verified.** The inspection shell runs in a sandbox that mounts the folder; it cannot see what is installed on Windows (Python, Node, PostgreSQL, Docker). This must be confirmed before Wave 0. |

Conclusion: there is nothing to reuse or conflict with inside this repo. Every capability in the spec is currently missing. The main early risks are unmade technology decisions, not existing code.

---

## B. Existing capabilities relevant to the spec

| Capability | Where | How it may be used |
|---|---|---|
| Phase 1 spec v1.1 | `docs/` | Source of Truth |
| Orchestra (task orchestration, evidence-isolated verification, claim gating) | Separate repo | Optional external driver for wave execution and evidence checks. It is **not** a runtime dependency of the app. |
| DA skills | Separate repo | Optional later use for report and dashboard design (Wave 8) |
| UX/UI skills | Separate repo | Optional later use for screen design (Wave 5+) |

Integration boundary: the app repo must build and test on its own. External tooling reads the repo and its test evidence. Nothing in `src/` imports external tooling.

---

## C. Missing capabilities (everything in the spec)

| Work package (spec §43) | Status |
|---|---|
| M01 Foundation: DB, migrations, config, fiscal year, domain structure | Missing |
| M02 Identity & Role: HOSxP auth adapter, RBAC, annual appointment | Missing |
| M03 HOSxP adapter: contracts, mock gateway, status normalization | Missing |
| M04 Plan Core: year, budget envelope, plan items, 3 plan types, export | Missing |
| M05 PPR Core: pre-validation, draft, confirm, lock, header allocation, print | Missing |
| M06 Plan Ledger: append-only ledger, derived remaining, concurrency, release | Missing |
| M07 Governance: amendment, unlock/revision, HoP verification | Missing |
| M08 Sync & Audit: manual and nightly sync, diff, cancellation, logs | Missing |
| M09 UI / dashboards | Missing |
| M10 Reports, alerts, Excel, acceptance/regression, backup drill | Missing |
| Repo-local guardrails (lint, types, architecture and phase guards) | Missing |

---

## D. Proposed architecture mapped to this repository

### D.1 Technology stack (APPROVED 2026-09-28 — see ADR-001)

| Concern | Proposal | Why |
|---|---|---|
| Language / API | Python 3.12 + FastAPI + Pydantic v2 | Typed contracts for the HOSxP boundary; fast to test |
| ORM / migrations | SQLAlchemy 2.x + Alembic | Explicit transactions; versioned migrations |
| Database | **PostgreSQL** (approved Phase 1 target; native install must remain possible — Docker optional for dev only) | `SELECT … FOR UPDATE`, SERIALIZABLE isolation, CHECK constraints, triggers to block UPDATE/DELETE on ledger and audit tables, recursive CTE for category hierarchy |
| Tests | pytest + hypothesis (ledger invariants) + real PostgreSQL for integration and concurrency tests | SQLite cannot prove the §19 concurrency requirement |
| Lint / types / architecture | ruff, mypy (strict for `domain/`), import-linter | Enforces the layering below |
| Excel export | openpyxl | Export only, System → Excel (§4.3) |
| Scheduler | CLI command (`ppr sync --mode nightly`) triggered by an external scheduler | Keeps the app stateless; schedule stays configurable (§25.3) |
| Frontend | **Node.js + Express, server-rendered pages calling the API (D-24, ADR-009)** | Decided 2026-09-29 for Wave 5 |
| Print (PPR document) | HTML print view first; PDF generation later if required | Final layout is TBD (§47) |

The app runs its own signed session **after** a successful `HosxpAuthenticationAdapter.authenticate()`. The app session is owned by this system. No HOSxP session or token format is assumed. No passwords are stored.

### D.2 Layering (dependency rule: arrows point inward only)

```text
api (FastAPI routers, authz dependencies)
   │
application (use cases: prevalidate_pr, confirm_ppr, unlock_ppr, run_sync, amend_plan …)
   │                        │
domain (pure: rules, state machine,    ports (Protocols): HosxpGateway, HosxpAuthenticationAdapter,
 ledger math, validation results)       Clock, repositories, AuditWriter
   ▲                        ▲
infrastructure/db (SQLAlchemy models, repositories, migrations)
integration/hosxp/mock (MockHosxpGateway, MockHosxpAuth)
integration/hosxp/real (EMPTY until Wave 9 evidence)
```

Rules enforced by import-linter:

- `domain` imports nothing from `infrastructure`, `integration`, or `api`.
- `application` depends on `integration.hosxp.contracts` and ports only, never on `mock` or `real`.
- Nothing outside `integration/hosxp/real/` may contain HOSxP-native identifiers.
- No module, table, or symbol named `planfin`, and no GL, PO-creation, or payment modules (phase guard).

### D.3 Directory layout

```text
Plan_Claude/
├─ docs/
│  ├─ PHASE1_TECHNICAL_SPEC.md
│  ├─ PHASE1_ASSESSMENT.md
│  ├─ adr/                      # architecture decision records (stack, DB, D-01..D-04, open questions)
│  └─ traceability.md           # generated: spec §40 acceptance IDs → tests
├─ backend/
│  ├─ pyproject.toml
│  ├─ alembic.ini
│  ├─ migrations/versions/
│  ├─ src/ppr/
│  │  ├─ config/
│  │  ├─ domain/
│  │  │  ├─ fiscal_year.py      # FY states, PPR number format, closed-year rule
│  │  │  ├─ plan.py             # plan item, plan types, envelope rule
│  │  │  ├─ ledger.py           # event types, derived remaining (D-01 qty-only stock events)
│  │  │  ├─ ppr_state.py        # 7-state machine (D-02)
│  │  │  ├─ prevalidation.py    # all-or-none, qty AND amount hard control
│  │  │  └─ allocation.py       # header-level category allocation (D-04)
│  │  ├─ application/
│  │  ├─ ports/
│  │  ├─ integration/hosxp/
│  │  │  ├─ contracts.py        # normalized DTOs (§24) + HosxpPrStatus enum
│  │  │  ├─ gateway.py          # HosxpGateway Protocol (§42)
│  │  │  ├─ auth.py             # HosxpAuthenticationAdapter Protocol (§21)
│  │  │  ├─ mock/               # in-memory fixtures + scenario builders
│  │  │  └─ real/               # placeholder, README only, until Wave 9
│  │  ├─ auth/                  # RBAC, policies, appointment checks
│  │  ├─ audit/
│  │  ├─ sync/
│  │  ├─ reporting/
│  │  ├─ infrastructure/db/
│  │  └─ api/
│  └─ tests/
│     ├─ unit/ integration/ acceptance/ concurrency/ architecture/
│     └─ conftest.py
├─ frontend/                    # created in Wave 5 after the stack decision
├─ tools/validators/            # phase_guard.py, traceability.py
├─ docker-compose.yml           # OPTIONAL dev PostgreSQL (Wave 1B); native PostgreSQL must also work
├─ tools/check.py               # `check` = lint + format + types + layering + guards + traceability + tests (cross-platform)
└─ README.md
```

### D.4 How the four decisions map to the design

| Decision | Design consequence |
|---|---|
| D-01 stock issue/return affects qty only | `plan_ledger` has `qty_delta` and `amount_delta`. A CHECK constraint forces `amount_delta = 0` for `CENTRAL_STOCK_*` events. Remaining amount is computed without stock events. |
| D-02 separate states | `ppr.state` (7-value enum, changed only through the state machine) and `ppr.hosxp_pr_status` (6-value enum, written only by sync). Two columns, two enums, no shared values table. |
| D-03 one confirmed PPR per PR, ever | The PR→PPR binding created at `CONFIRMED_LOCKED` has an unconditional `UNIQUE (hosxp_pr_id)`. The cancelled PPR row is kept. |
| D-05 drafts don't bind | Drafts are unnumbered; at most one working draft per PR; discard (audited) frees the PR for a new draft until first confirmation. |
| D-06 numbering | Unique per fiscal year, resets yearly; gaps allowed; no gapless serialization. |
| D-07 `CLOSED` | In the enum; no inbound transition and no manual close action until HOSxP terminal status is verified (G-4). |
| D-08 fiscal year | Boundaries/labeling are configuration; HOSxP `fiscal_year` field not assumed. |
| D-04 header allocation | **PPR 1 → N `ppr_category_allocation` rows** `(ppr_id, budget_category_id, amount)`, `UNIQUE (ppr_id, budget_category_id)`. Σ amount = required PPR amount; every category under the PR's fund source. Allocation rows never post to `plan_ledger`; plan usage stays PR Item → Plan Item → Plan Ledger. |

---

## E. Dependency graph

```text
                 ┌──────────────────────┐
                 │ W0 Skeleton + guards │
                 └──────────┬───────────┘
          ┌─────────────────┼──────────────────┐
          ▼                 ▼                  ▼
  M01 Foundation     M03 HOSxP contracts   Audit foundation
  (FY, config, DB)   + Mock gateway        (append-only)
          │                 │                  │
          ├──────► M02 Identity/RBAC ◄─────────┤
          │         (auth adapter, roles,      │
          │          appointment)              │
          ▼                 │                  │
     M04 Plan Core ◄────────┘ (masters)        │
          │                                    │
          ▼                                    │
     M06 Plan Ledger ◄─────────────────────────┤
          │                                    │
          ▼                                    │
     M05 PPR Core (prevalidate → draft → confirm/lock, allocation)
          │
   ┌──────┼───────────────┬──────────────────┐
   ▼      ▼               ▼                  ▼
 M07   M08 Sync        Search/Timeline     M07 Amendment
 HoP   (diff, cancel,  (W5)                + Central Pool /
 verify release-once)                      Assigned Purchase (W7)
   └──────┴───────┬───────┴──────────────────┘
                  ▼
     M09 UI / dashboards  →  M10 Reports, alerts, Excel
                  ▼
     W9 Real HOSxP adapter (blocked on evidence) → W10 Full verification
```

Critical path: W0 → M01 → M03 → M04 → M06 → M05 confirm. Everything after that can run partly in parallel.

---

## F. Recommended implementation waves

This follows spec §44, with one added **Wave 0** and Wave 1 split into two parts so the first change is small and reversible.

| Wave | Content | Exit criteria |
|---|---|---|
| **0 Skeleton & guardrails** | pyproject, lint, types, import-linter contracts, phase guard, traceability script, `check` task, README, ADRs (Alembic baseline moved to 1B: no persistence in W0/1A) | `check` passes on an empty codebase; guard self-tests prove they catch violations |
| **1A Pure domain + HOSxP contracts** | Fiscal-year rules, PPR number format, PPR state machine (D-02), ledger arithmetic (D-01), qty/amount hard-control function, all-or-none prevalidation result type, header allocation validator (D-04); HOSxP DTOs, `HosxpGateway` and `HosxpAuthenticationAdapter` Protocols, `MockHosxpGateway`, status normalizer defaulting to `UNKNOWN` | Unit and property tests cover §13, §17, §18.3, §28 and D-01 to D-04 without a DB |
| **1B Persistence foundation** | Schema for HOSxP mirrors, plan_year, users/roles, audit_log (append-only trigger), sync_run; Alembic round-trip; RBAC skeleton with server-side policy | Migration up/down/up works; audit immutability test; unauthorized API test |
| 2 Plan Core | plan_budget, plan_item, plan_item_demand, envelope validation before activation, plan_ledger + initial `PLAN_ACTIVATED` entry (shipped as `PLAN_APPROVED`, renamed by migration 0003) | §9.1 envelope, remaining = ledger |
| 3 PR retrieval & pre-PPR | Live retrieval via gateway, item→plan matching, qty+amount checks, no record on failure, HOSxP-down refusal | §40.1, §40.2, §40.6 (downtime) |
| 4 Confirm / lock / print | Atomic confirm, per-FY sequence (unique, gaps allowed — D-06), PR snapshot, ledger posting, lock/version, header allocation (1:N), print view; required allocation amount = sum of PR item amounts (D-17, resolves G-2) | §40.3, §40.4, §40.5 incl. concurrency |
| 5 Procurement / search / timeline | Appointment-aware HoP verification, queue, search with role scope, timeline; frontend stack decided | §40.9, §40.10 |
| 6 Sync / change / cancel | Manual and nightly sync, field diff, `PR_CHANGED_REVIEW_REQUIRED`, cancellation with release-once | §40.6 to §40.8 |
| 7 Amendment / Central / Assigned | **Design gates G-1 and G-3 must be resolved first.** Governed amendment, Central Pool with qty-only stock events (D-01), Assigned Purchase demand states | §40.11, §40.12 |
| 8 Dashboards / reports / alerts | Role dashboards, §37 reports, Excel, alert engine (30-day default) | Report reconciliation tests |
| 9 Real HOSxP adapter | **Blocked until verified HOSxP evidence exists** | Contract tests pass against both mock and real |
| 10 Full verification | Full acceptance/regression, concurrency, authz matrix, sync idempotency, backup/restore drill | Go-live review |

---

## G. Files and directories expected to change

**Already done (this step):** `.git/` initialized, `docs/PHASE1_TECHNICAL_SPEC.md`, `docs/PHASE1_ASSESSMENT.md`.

**Wave 0 (create):** `README.md`, `.gitignore`, `.editorconfig`, `backend/pyproject.toml`, `backend/src/ppr/__init__.py` (plus empty package dirs from D.3), `tools/check.py`, `tools/validators/phase_guard.py`, `tools/validators/traceability.py`, `tools/validators/layering.py`, `backend/tests/guards/*` (guard self-tests), `docs/adr/ADR-001-technology-stack.md`, `docs/adr/ADR-002-phase1-decisions.md`, `docs/adr/ADR-003-design-gates-and-assumptions.md`. Alembic files move to Wave 1B.

**Wave 1A (create):** `backend/src/ppr/domain/{fiscal_year,ledger,ppr_state,prevalidation,allocation,plan}.py`, `backend/src/ppr/integration/hosxp/{contracts,gateway,auth,status}.py`, `backend/src/ppr/integration/hosxp/mock/{gateway,auth,fixtures}.py`, `backend/src/ppr/integration/hosxp/real/README.md`, `backend/tests/unit/domain/test_*.py`, `backend/tests/unit/integration/test_mock_gateway.py`.

**Never in Phase 1:** anything named `planfin`, GL/Access connectors, PO creation, payment, or HOSxP write-back.

---

## H. Risks and open questions

No existing-code conflicts (greenfield). Risks are decisions and spec gaps.

### Resolved on 2026-09-28

| Item | Resolution |
|---|---|
| H-1 Stack / DB | **Approved**: Python, FastAPI, Pydantic v2, SQLAlchemy 2.x, Alembic, PostgreSQL, pytest, Hypothesis, ruff, mypy, import-linter. Frontend not locked. Docker optional; native PostgreSQL must remain possible (ADR-001). |
| H-2 Abandoned drafts | **Decided** → spec D-05. Draft unnumbered, one working draft per PR, discardable; binding starts at `CONFIRMED_LOCKED`. |
| H-5 Numbering | **Decided** → spec D-06. Unique, FY-scoped, yearly reset; gapless **not** required. |
| H-6 `CLOSED` | **Corrected** → spec D-07. No manual close action; no automatic mapping; transition is integration-TBD (G-4). |
| H-7 Fiscal year | **Approved with caution** → spec D-08. Boundaries are config; HOSxP `fiscal_year` field not assumed. |

### Deferred design gates (spec §0, ADR-003)

| Gate | Topic | Must be resolved before |
|---|---|---|
| G-1 (H-3) | Central Pool qty double-counting. Until then: D-01 preserved, contracts only, no Central Pool posting. | Wave 7 |
| G-2 (H-4) | Header total vs item sum. **Resolved → spec D-17** (2026-09-29). | — |
| G-3 (H-8) | Assigned Purchase matching/posting. | Wave 7 |
| G-4 | Terminal HOSxP status → `CLOSED`. | Wave 6/9 |

### Still open

- **H-9 Windows host toolchain.** Not yet confirmed (Python version, PostgreSQL). Wave 0/1A checks were run in the cloud workspace on Python 3.12; the same `python tools/check.py` must be re-run on the Windows host.

### Other risks

- **H-10 Personal data.** Requester names and HOSxP identities are personal data. Audit and export need PDPA-appropriate access control and retention. This is a hosting concern (§47).
- **H-11 HOSxP evidence dependency.** Wave 9 and go-live are blocked until real HOSxP details are verified. The mock must not drift into implying real field names.

---

## I. Tests and validators after each wave

One command, `check`, runs everything below. It is required to pass before a wave is marked complete (§45).

| Gate | W0 | 1A | 1B | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 10 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| ruff + format check | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| mypy (strict on domain) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| import-linter layering | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| phase guard (no PlanFin/GL/PO/payment) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| unit tests (domain) | | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| property tests (ledger never negative; remaining = Σ ledger) | | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Alembic up/down/up + model-drift check | ✓ | | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| DB constraint tests (unique PR, ledger/audit immutable, D-01 CHECK) | | | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| authz matrix (direct API calls per role) | | | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| acceptance tests tagged to §40 IDs | | | | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| concurrency test (real PostgreSQL, parallel confirms) | | | | | | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| sync idempotency (repeat runs, release-once) | | | | | | | | ✓ | ✓ | ✓ | ✓ |
| report reconciliation (report totals = ledger) | | | | | | | | | | ✓ | ✓ |
| traceability report (§40 coverage) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 100% |
| backup/restore drill | | | | | | | | | | | ✓ |

Each wave report will list the exact changed files and the exact `check` output (§49.8). Orchestra can optionally consume that evidence externally.

---

## J. Recommended smallest safe first wave

**Wave 0 + Wave 1A together, with no database and no UI.**

Why this is the safest start:
- **No persistence, no network, no HOSxP assumptions.** It is pure Python plus tests. It is fully reversible.
- **It locks in the highest-risk business rules first**, as executable tests: all-or-none, the qty **and** amount hard control, the D-01 qty-only stock events, the D-02 state machine that has no `PR_APPROVED`/`ACTIVE` states, D-03 uniqueness as a domain rule, D-04 header allocation Σ = total within one fund source, and PPR-2570-000001 formatting with per-FY reset.
- **The HOSxP boundary is fixed early** as Protocols plus a mock. The status normalizer maps anything unknown to `UNKNOWN`. The `real/` package stays empty.
- **Guardrails exist before business code grows**: layering, phase guard and traceability.

Acceptance for this first wave: `check` passes, and the guard self-tests show they fail on a planted violation. Unit and property tests will cover these spec items without a DB: §13 (all four examples), §17 (all rules), §18.3 (formulas with D-01), §28 (all allowed and forbidden transitions) and §40.2 (all eight cases).

Approved 2026-09-28. Scope exclusions for this wave: no persistence/domain tables, no UI, no real HOSxP adapter, no PlanFin/GL/PO creation/payment, no Central Pool ledger behavior.
