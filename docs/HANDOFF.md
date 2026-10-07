# HANDOFF — Hospital Plan & PPR Control System (Phase 1)

Read this first, then `docs/PHASE1_TECHNICAL_SPEC.md`. This document explains how the
project is run so work can continue without losing its guarantees.

**Product owner / approver:** Dr. Worrawate Rojjaratpaisarn. Every wave ends with a report
and **stops for approval** before the next wave starts.

---

## 1. Sources of truth (in order of precedence)

1. `docs/PHASE1_TECHNICAL_SPEC.md` (v1.12) **§0 Decision Record** (D-01 … D-31) and design gates (G-1 … G-4)
2. The rest of `docs/PHASE1_TECHNICAL_SPEC.md`
3. `docs/adr/` (how decisions are realized in code)
4. `docs/PHASE1_ASSESSMENT.md` (architecture and wave plan)

If code and spec disagree, the spec wins. A spec change is made **only** by adding or amending
an entry in §0 with the product owner's approval — never silently in code.

## 2. Where the project stands

| Wave | Content | Commit |
|---|---|---|
| 0 + 1A | Skeleton, guardrails, pure domain rules, HOSxP contracts + mock | `1d5d395` |
| 1B | PostgreSQL schema + Alembic, append-only audit, sessions, server-side RBAC, API skeleton | `9e668d5` |
| 2 | Plan Core: master sync (mock), budgets, Plan Items, envelope validation, activation + initial ledger posting, D-15 corrections (ADR-005) | `5a16809` |
| pre-3 | Hardening: initial ledger event renamed `PLAN_APPROVED` → `PLAN_ACTIVATED` (migration 0003), stale docs refreshed, GitHub Actions CI (`.github/workflows/phase1-check.yml`), A-1/A-9 promoted to D-16 | `c3efc67` (merged; baseline tag `phase1-wave2-baseline`) |
| 3 | PR retrieval / Pre-PPR validation: live, read-only, D-16/D-17/D-18 (ADR-006) | `21c5eac` (merged) |
| HOSxP | Real HOSxP connection: generic read-only SQL adapter + site query file + `hosxp-schema` / `hosxp-check` tools (ADR-007, `docs/HOSXP_CONNECTION.md`) | `cfbf2d5`, `7685353` (merged); needs the site read-only account and query file |
| 4 | PPR draft, allocation (D-19), atomic confirm with row locks, numbering, lifetime binding, ledger, unlock/reconfirm, versions, A4 print (D-20); draft/reconfirm PR-change rules and precision (D-21 … D-23) — ADR-008 | `297a1ec` (merged; baseline tag `phase1-wave4-baseline`) |
| 5A | User interface (Node.js + Express, Thai, D-24), runnable API server, HOSxP MD5 login adapter (D-25), demonstration mode (D-26), 9th check gate `frontend` — ADR-009 | `4860920` (merged; baseline tag `phase1-wave5a-baseline`) |
| 5B | Head of Procurement appointment (D-27) and verification (D-28), procurement queue, search, timeline, their screens; migration 0005 — ADR-010 | `88075e5` (merged; baseline tag `phase1-wave5b-baseline`) |
| 6 | PR synchronization (manual, nightly command, retry, one PPR), change detection → review, HOSxP cancellation with one-time release (empty mapping until IT evidence), invalid-evidence rules, governance re-check of cancelled PPRs, "observed in HOSxP" timeline, sync screens; migration 0006 — D-29 … D-31, ADR-011 | `26b9b9f` (merged; baseline tag `phase1-wave6-baseline`) |
| 8A | Role dashboards (Requester, Plan Control Center, Executive), derived in-app alerts (D-33) with ADMIN-set thresholds, plan management screens (D-34); migration 0007 — D-32 … D-34, ADR-012. Built before Wave 7 (order 8 → 7 → 9 → 10, D-32) | `1bfbbfa` (merged) |
| 7A | Plan Amendment (D-37): Plan Officer records a Director-approved amendment (document number and date) on an ACTIVE year; whole-amendment rules (balanced lines, never below used, closing unused items, data changes only while unused, fund source fixed), append-only history, `PLAN_AMENDMENT` ledger entries, `PLAN_AMENDED` audit, preview, screens and the *Plan Amendment* report; migration 0008 — ADR-014 | `d36ed48` (merged; baseline tag `phase1-wave7a-baseline`; used items keep category / purchaser — ADR-014) |
| 7B-1 | Central Pool (D-38 G-1): PRs of the purchasing department match `CENTRAL` items, ambiguity fails closed (refused already at activation / amendment), purchaser required, purchaser-only visibility, *Central Plan Usage* report (stock issues: no data until IT confirms a query) — ADR-015 | `5b36ebc` (merged; baseline tag `phase1-wave7b1-baseline`) |
| 7B-2 | Assigned Purchase (D-38 G-3): purchaser's PRs match `ASSIGNED` items, coverage per demand department checked at confirmation, derived demand states (planned → included → fulfilled, back to planned on cancellation), blocking of departments with open demand, demand amended by Plan Amendment; migration 0009 — ADR-016 | `8e6b695` (merged; baseline tag `phase1-wave7b2-baseline`; one demand, one purchase line — ADR-016) |
| 10A | Production readiness without real HOSxP: concurrency, idempotency and every-route isolation tests; 100 % §40 acceptance gate with per-criterion report; production profile (owner vs least-privilege runtime role, loopback API, HTTPS proxy settings), health checks, ops-status, supervisor + Task Scheduler, backup/restore with manifest — ADR-017, docs/DEPLOY_PRODUCTION.md | `71f7a11` (merged; baseline tag `phase1-wave10a-baseline`) |
| 10B-1 | D-39 stock boundary: `CENTRAL_STOCK_ISSUE` / `CENTRAL_STOCK_RETURN` refused by the ledger domain and by `ck_plan_ledger_no_stock_movement` (migration 0010); no stock term in remaining; optional `[stock_movement]` site query used only with a verification record and kind mapping; *Central Plan Usage* shows issued / returned / net used / bought-not-issued per item and department, read live, nothing stored, no fund split; other departments see only their own consumption — ADR-018 | `6b825a5` (merged; baseline tag `phase1-wave10b1-baseline` on the post-merge verified commit, see ADR-018) |
| 10B-2 | IT hand-over (D-40): `IT_HANDOFF.md`, `hosxp-verify` and `first-start --check` (read-only, PASS / FAIL / WARN / OPEN / TODO, OPEN never PASS, Markdown evidence), `GO_LIVE_CHECKLIST.md` signed by the product owner; MOCK/DEMO in production = WARN at start-up / ops-status / every page, never in the Plan Ledger (`production_engine` guard); production sign-in through HOSxP only — ADR-019 | `0f0c3d8` (merged; baseline tag `phase1-wave10b2-baseline` on the post-merge verified commit, see ADR-019) |
| 11A | Login server split and PPR department from the PR (D-41): second read-only HOSxP connection for sign-in only; requesters prepare PPRs for any department's PR, act on and read only their own PPRs (`PprScope`, `NOT_THE_CREATOR`, `PPR_DISCARD`); stricter PostgreSQL account check — ADR-020 | `3ad5c4a` (merged; baseline tag `phase1-wave11a-baseline` on the post-merge verified commit, see ADR-020) |
| 11B | Master sync of 43,707 items failed at the hospital (one INSERT above 65,535 parameters): batched upsert in one transaction; `redact_url` hides passwords with `#` / `@` / `/` / `?` — ADR-021 | `233541a` (merged; baseline tag `phase1-wave11b-baseline`, see ADR-021) |
| 12A-1 | Excel plan import (D-42): `plan_import`, imported Plan Items may have no HOSxP item (`ck_plan_item_code_or_import`), checked by `domain/plan_import.py`, read by `api/plan_import_xlsx.py`, UI upload without a new package — ADR-022 | `b710106` (merged; baseline tag `phase1-wave12a1-baseline`, see ADR-022) |
| 12A-2 | Requester chooses the plan row per PR line (D-43): `domain/prevalidation.py` (`_offered`, `PLAN_ROW_NOT_CHOSEN` / `PLAN_ROW_NOT_ALLOWED`), `ppr_line_choice` (0012), `ppr_services.set_choices` / `choice_report`, `PUT/GET /api/pprs/{id}/choices`, `?choice=` on the PR check; import without item column; Plan Items without a HOSxP item (name + unit) — ADR-023 | `fe72a75` (merged; baseline tag `phase1-wave12a2-baseline`, see ADR-023) |
| 8B | Reports of §37 (Plan Summary, Plan Remaining, Department / Category Utilization, PPR Register, Pending, Cancelled, PR Changed, Audit) on screen and as `.xlsx` from the API; each download audited (`REPORT_EXPORTED`); new dependency `openpyxl` — D-35, ADR-013 | `f4751ba` (merged; baseline tag `phase1-wave8b-baseline`; evidence and the Windows isolation caveat in ADR-013) |

Verified: 8/8 `check` gates on Linux + PostgreSQL 16 and on the Windows dev machine (PostgreSQL 16.15) up to Wave 4 (`297a1ec`); on Windows the 10 MySQL/MariaDB connector tests are skipped (no local MariaDB) and run in CI.
GitHub Actions runs the same `python tools/check.py` on every push and pull request (PostgreSQL 16, Python 3.12, CI-only secrets).

Ledger naming (since pre-3 hardening): Director approval = plan-year state `APPROVED`; operational activation = plan-year state `ACTIVE`; the initial ledger event and the audit action are both `PLAN_ACTIVATED`.

## 3. Non-negotiable rules

1. **Phase 1 only.** No PlanFin, GL / MS Access, PO creation or modification, payment, or writing to HOSxP. `phase_guard` fails the build if these appear in code or migrations.
2. **Never invent HOSxP details** (API endpoints, tables, field names, status codes, authentication). Use the contracts in `integration/hosxp/contracts.py` and the mock. The real adapter (`integration/hosxp/real/`) is generic: HOSxP table/column names live only in the site file `hosxp_queries.toml`, written from the real schema (ADR-007).
3. **Hard control = quantity AND amount.** Unit price is an estimate; Q1–Q4 are monitoring only.
4. **Remaining is derived** from the append-only Plan Ledger. No code path may set "remaining" directly.
5. **PPR state (7 internal states) ≠ HOSxP PR status** (separate normalized field). `CLOSED` has no inbound transition yet (D-07).
6. **One confirmed PPR per HOSxP PR number, for life**; drafts don't bind (D-03, D-05). Only a REQUESTER reconfirms after revision (D-09).
7. **Authorization is server-side** on every route; every new route must be added to the authorization matrix test (the test fails otherwise).
8. **Audit is append-only** and written in the **same transaction** as the change.
9. **No secrets in git.** `.env` is git-ignored; never commit passwords, connection strings with passwords, or real patient/HOSxP data. Do not copy real HOSxP data into dev/test databases.
10. **Stock movements are HOSxP reporting data only (D-39, ADR-018).** Store issues / returns are read-only from HOSxP for the *Central Plan Usage* report; they are never posted to the Plan Ledger (application and database refuse `CENTRAL_STOCK_ISSUE` / `CENTRAL_STOCK_RETURN`) and never stored. **Unknown ≠ zero:** a figure HOSxP does not determine is shown as "ยังไม่มีข้อมูล", never 0. **No allocation by inference:** consumption is never split by fund, purchaser or plan line by guess. **MOCK / DEMO stay apart from production:** made-up `MOCK-*` data and the demo's stand-in verification exist only in mock/demo mode; a real site shows stock figures only after IT records its own verification.

## 4. How every wave is done

1. Read the spec sections for the wave and any related §0 decisions/gates. If a gate blocks the wave, stop and ask.
2. List open questions **before** coding. Do not guess business rules — ask the product owner.
3. Write tests first or alongside code. Tag tests with the spec items they prove: `@pytest.mark.spec("AT-40.2.2", "D-01", "S-13")`.
4. Keep the layering (enforced by import-linter):
   - `domain/` — pure rules, no frameworks, no I/O
   - `application/` + `ports.py` — use cases; no SQLAlchemy/FastAPI imports
   - `infrastructure/db/` — SQL implementations of the ports
   - `api/` — HTTP, authorization, composition
5. Schema changes: new Alembic migration (never edit an applied one); the drift test must stay green.
6. Run `python tools/check.py` → must be **9/9** (8/8 before Wave 5A). A wave is not done if any gate fails.
7. Update docs (spec §0 only with approval, ADRs, README status, `python tools/check.py --write-traceability`).
8. Report: exact changed files, exact `check` output, open assumptions/TBDs, confirmation that out-of-scope items did not enter. Then **stop for approval**.
9. Commit with a clear message; push to GitHub.

## 5. Open design gates and assumptions

| Gate | Must be resolved before | Topic |
|---|---|---|
| G-1 | — | Resolved by D-38 (2026-10-01); built in Wave 7B-1 (ADR-015). |
| ~~G-2~~ | — | **Resolved → D-17** (2026-09-29): required amount = sum of PR item amounts; header total must match exactly, otherwise the PPR cannot be confirmed. |
| G-3 | — | Resolved by D-38 (2026-10-01); built in Wave 7B-2 (ADR-016). |
| G-4 | Wave 6/9 | Which verified terminal HOSxP status closes a PPR. |

Stock movements (D-39, ADR-018): IT supplies the `[stock_movement]` query with its verification record and kind mapping (including how HOSxP records a return); until then the *Central Plan Usage* report shows "ยังไม่มีข้อมูล" for them.

Working assumptions A-1 … A-12 are listed in `docs/adr/ADR-003-design-gates-and-assumptions.md`.
Go-live items: production DB role split, backup/restore drill and HTTPS are built and drilled in Wave 10A (ADR-017, `docs/DEPLOY_PRODUCTION.md`) — the hospital's own servers still need them set up and drilled once; and the **D-25 real-HOSxP login evidence** — query/table/column mapping, stored hash format, password encoding before hashing, account-active semantics (spec D-25, ADR-009). The current MD5 login adapter is provisional compatibility behaviour, not verified HOSxP authentication.

## 6. Wave 8 — dashboards, alerts, plan screens, reports

Wave 8B (merged `f4751ba`; ADR-013, D-35; Windows evidence caveat recorded there): nine reports share
one row builder for screen and Excel; plan reports reuse the 8A balances, PPR reports the PPR
search and scope; the date range of PPR reports is the first confirmation (version 1). After
pulling, run `pip install -e backend` again (new dependency `openpyxl`). The same branch holds
D-36: a PR-sync failure alert clears only when every failed PPR of the run was resolved by
later retries / one-PPR syncs (the count still outstanding is shown); failures and retry
results are audited (`PR_SYNC_FINISHED.failed_ppr_ids`, `PR_SYNC_RETRY_RESULT`). End-to-end
check on a development machine: `python tools/dev/verify_wave8b.py` (demo mode, `*_dev` DB).

Wave 8A (merged `1bfbbfa`; ADR-012, spec D-32 … D-34). Dashboards and alerts only
read (no ledger posting, no state change, no HOSxP call) and use the §22.1 scopes. Alerts are
derived on every request (no alert table, no acknowledgement) and shown in the application
only; LINE is Phase 2. Thresholds (30 days, 20 %) live in `alert_setting` (migration 0007),
changed by ADMIN with a reason (audited); the procurement queue uses the same inactivity
threshold. Plan screens call the Wave 2 API only. Plan Amendment and Central Plan Usage reports move
to Wave 7. G-1, G-3 (Wave 7) and G-4 stay
open. Wave 7A (Plan Amendment) is merged (ADR-014); Wave 7B waits for G-1 / G-3.

Wave 6 (merged, ADR-011, D-29 … D-31): HOSxP is read outside every database lock; only an
IT-mapped native status cancels (the mapping ships empty: REAL-HOSXP EVIDENCE REQUIRED, see
ADR-011 §7); NOT_FOUND and unreliable evidence never change state; control-evidence failures
send locked/verified PPRs to review.

To try the system now: `run_demo.bat` (Windows) or ADR-009 "Running"; Wave 5B demo users
`demo_head` (can verify) and `demo_head_old` (last year's head, cannot).

## 7. Code map

```text
backend/src/ppr/
  domain/            fiscal_year, ppr_number, hard_control, prevalidation, pr_binding,
                     ppr_state, ledger, allocation, roles, permissions, violations,
                     pr_changes (the one PR classifier), pr_sync (D-29 … D-31 rules),
                     alerts (derived alert rules, D-33)
  integration/hosxp/ contracts, gateway (Protocol), auth (Protocol), status, mock/,
                     real/ (SQL adapter: query_config, sql_gateway, schema_discovery,
                     sql_auth = MD5 login through site queries, D-25)
  bootstrap.py       chooses mock or real HOSxP from settings (PPR_HOSXP_MODE)
  server.py          composition root of the running API (serve, schema check, D-24..D-26)
  demo.py            demonstration mode: mock PRs/users and idempotent seed (D-26)
  ports.py           UnitOfWork, repositories, AuditWriter (Protocols)
  application/       services.py (login, roles, plan year), plan_services.py (Plan Core),
                     pr_services.py (PR retrieval + Pre-PPR validation),
                     ppr_services.py (PPR draft/confirm/unlock),
                     procurement_services.py (appointments, verify, queue, search,
                     timeline), sync_services.py (PR synchronization, Wave 6),
                     dashboard_services.py (dashboards, alerts, thresholds, Wave 8A),
                     report_services.py (the §37 reports, Wave 8B),
                     pr_snapshot.py (the one canonical PR serializer), errors.py
  infrastructure/db/ schema.py, repositories.py, plan_repositories.py
  auth/session.py    application session tokens
  api/app.py         FastAPI app, auth, plan-year/audit/role routes
  api/plan_routes.py Plan Core routes (sync, masters, budgets, items, validation, balance)
  api/pr_routes.py   Pre-PPR validation route (GET /api/prs/{pr_no}/prevalidation)
  api/ppr_routes.py  PPR routes; api/ppr_print.py the A4 print page
  api/sync_routes.py PR synchronization routes (Wave 6)
  api/dashboard_routes.py dashboards, alerts, alert settings, plan balances (Wave 8A)
  api/report_routes.py, api/report_xlsx.py reports and their Excel files (Wave 8B)
  cli.py             migrate, grant-role, hosxp-schema, hosxp-check, serve [--demo],
                     sync-prs --mode NIGHTLY (run by Windows Task Scheduler)
frontend/            Express UI (D-24): src/app.js routes, src/api.js API client,
                     src/labels.js Thai labels, views/*.ejs, public/style.css, test/
run_demo.bat         Windows: migrate, start API (demo) + UI, open the browser
backend/migrations/  Alembic (0001_foundation, 0002_plan_core, 0003_plan_activated_event, 0004_ppr_core,
                     0005_procurement_verification, 0006_pr_sync, 0007_alert_setting,
                     0008_plan_amendment, 0009_assigned_purchase)
backend/tests/       unit/, db/ (PostgreSQL, mandatory), guards/ (self-tests)
tools/check.py       the 9-gate check (frontend gate: tools/validators/frontend_check.py)
tools/dev/           setup_local_db.py/.bat (per-programmer databases with --name)
```

## 8. Getting set up

See `docs/SERVER_SETUP_WINDOWS.md`.
