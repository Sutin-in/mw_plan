# ADR-019 — Go-live tools, MOCK/DEMO data in production, production sign-in (Wave 10B-2)

- **Status:** Accepted - merged to `main` by fast-forward at `0f0c3d8` (2026-10-02); baseline tag `phase1-wave10b2-baseline` on the post-merge verified commit (recorded in the tag message and in the next documentation commit)
- **Spec:** D-40 (product owner, 2026-10-02), §40.14, §21, §24, §39; D-25, D-30, D-39, G-4.
  Builds on ADR-017 (production profile) and ADR-018 (stock movements). No HOSxP table, column,
  status or login detail was added or guessed; every HOSxP stand-in is the made-up `mockhx_*` /
  `MOCK-*` data. G-4, the PR cancellation mapping (D-30) and unverified stock movements stay OPEN.

## Decision

| D-40 | Implementation |
|---|---|
| Check reports | `ops_report.CheckReport`: one line per check, PASS / FAIL / WARN / OPEN / TODO / INFO; the overall result is `PASS` only when nothing else is reported, otherwise `NO FAIL - n WARN, n OPEN, n TODO (not PASS)` or `FAIL` (exit 1). `--out` saves the same as Markdown evidence with a separate "OPEN items (not PASS)" list. |
| `ppr.cli hosxp-verify [--pr N]... [--out F]` | Read only, through the system's own SQL adapter (read-only transactions): query file loads / needed queries present; the HOSxP account's rights read from the database catalogue on an **allow-list** (only SELECT / SHOW VIEW / USAGE pass; any other right, unknown or new, fails): PostgreSQL role flags (superuser, CREATEROLE, CREATEDB, BYPASSRLS), `has_table_privilege` on tables, views, materialized and foreign tables, `has_column_privilege`, `has_sequence_privilege`, `has_schema_privilege` and `has_database_privilege` (rights through roles and PUBLIC included); MySQL/MariaDB `SHOW GRANTS` (column lists, dynamic privileges, `WITH GRANT OPTION` and role membership fail) - nothing is ever written or tried; the 4 masters (rows, duplicates, parent ids); each PR given (found, item lines, fiscal year, department / fund / category / item ids in the masters' code space, header total vs item sum, native status mapped or OPEN); PR cancellation mapping (always OPEN: "no native status maps to CANCELLED", or "configured ... HOSxP evidence pending" - a mapping in a file is not proof, checklist C2); G-4 and D-25 always OPEN; stock movements (OPEN without a verified query, without a RETURN kind or with unmapped kinds; FAIL for ids outside the masters). Output: counts, column names, the PR numbers typed, native status codes, the stock verification date and reference - no names of people (not even who verified the stock query), no passwords, the URL redacted. |
| `ppr.cli first-start --check [--out F]` | Read only (settings, `.env`, query file, PPR database): production profile, database name not `*_dev` / `*_test`, schema owner and session secret set, user interface behind HTTPS (`PPR_UI_*`, `PPR_API_URL`, as `server.js` checks), `PPR_BACKUP_DIR`, `PPR_HOSXP_MODE=sql` and the query file, schema at the latest migration, least-privilege runtime account, MOCK/DEMO data (WARN), administrator / masters / plan year (TODO until done), backup drill and `hosxp-verify` (TODO), the OPEN items. |
| MOCK/DEMO in production = WARN | `infrastructure/db/mockdata.mock_data_summary` counts (never lists) demo users `demo_*` and `MOCK-*` masters, Plan Items, their ledger entries and PPRs. Production: start-up note `WARNING ...` (the API still starts), `ops-status` line `WARN` with `ops-status: PASS (with warnings)` when nothing failed, `status.ps1` repeats each WARN and ends `status: PASS with n warning(s)`, `/api/info.mock_data_warning` (counted at most once a minute) shown by the user interface on every page as an alert banner, `first-start --check` WARN, checklist B1. Development machines: INFO only (the demo data is expected there). |
| MOCK/DEMO never reaches the Plan Ledger in production | The production API and the nightly synchronization use `production_engine(engine)` (an engine execution option). `SqlLedgerRepository.append` - the one insert into `plan_ledger` - refuses, on such a connection, any entry on a Plan Item whose HOSxP item is `MOCK-*` (`ConflictError MOCK_DATA_IN_PRODUCTION`, HTTP 409; in the nightly run that PPR's ERROR, the run goes on). Development is unchanged. Production still refuses demonstration mode and the mock HOSxP (ADR-017). |
| Production sign-in through HOSxP only | Verified, not changed: `server.build` gives production the SQL HOSxP adapter and its MD5 login adapter (D-25) and nothing else - demo mode and `PPR_HOSXP_MODE=mock` are refused; `/api/auth/login` answers 401 for a wrong password and 503 `HOSXP_UNAVAILABLE` when HOSxP cannot be reached; there is no other authenticator to fall back to. Tests sign in a made-up HOSxP user, refuse `demo_*` users, and get 503 for both when HOSxP is down. |
| Hand-over documents | `docs/IT_HANDOFF.md` (Thai): the order of work, links to DEPLOY_PRODUCTION / HOSXP_CONNECTION / interface views (not repeated), the two check commands, first start, and the evidence IT sends back (PR 6907912 query, cancellation status, closing status, stock movements incl. how returns are recorded, login). `docs/GO_LIVE_CHECKLIST.md` (Thai): A - must PASS with evidence (10 items), B - WARN (MOCK/DEMO), C - OPEN (G-4, D-30, D-39, others reported OPEN) marked **OPEN**, never PASS, D - approval by the product owner (เจ้าของระบบ). A test keeps section C free of PASS and checks that every command the documents name exists. |

## §40.14 acceptance criteria

| ID | Criterion | Tests (`test_go_live_tools`) |
|---|---|---|
| AT-40.14.1 | `hosxp-verify` read-only, PASS / FAIL / OPEN, OPEN never PASS | verify on PostgreSQL + MariaDB, summary with OPEN, checklist section C |
| AT-40.14.2 | `first-start --check` changes nothing | row counts of every table before / after |
| AT-40.14.3 | MOCK/DEMO in production: warning everywhere, never a failure, never in the Plan Ledger | repository guard, production activation refused (409, ledger empty), `/api/info`, `ops-status`, start-up note |
| AT-40.14.4 | Production sign-in through HOSxP only, no demo fallback | HOSxP user signs in, `demo_*` refused, 503 when HOSxP is down, demo / mock HOSxP refused |

## Evidence

| Where | Commit | Result |
|---|---|---|
| Cloud production drill (`drill_production.py`, superuser) | branch head | 28/28 - with the new checks: ops-status WARN for the stand-in's MOCK masters without failing, `first-start --check` without FAIL on the drilled server, `hosxp-verify` PASS for the real read-only stand-in account and FAIL once that account gets INSERT through a view |
| Independent review | `a171e4f` | blocker fixed: the account check passed column / view / admin rights (deny-list) - now an allow-list, proven on real roles by the drill and by `test_postgresql_account_check_on_real_roles` (runs where the test account may create roles, e.g. CI); also fixed: D-30 OPEN even when configured, no verifier name in the output, no PASS next to a FAIL in first-start, production engine asserted for the API server and the nightly sync, `/api/info` waits a minute after a database error |
| Cloud (Linux, PostgreSQL 16.13; MariaDB for the SQL-adapter tests) | `293f6ba` | `tools/check.py --acceptance-report` 10/10 gates, 1,380 passed, 2 skipped (the role-split test of ADR-017 and `test_postgresql_account_check_on_real_roles`: the test account may not create roles - both run in CI, and the real-role account check also runs in the drill above); acceptance **84 PASS** (AT-40.14.1-.4 included) |
| Windows dev machine (PostgreSQL 16.15, Node 24.16, Python 3.12.9, Windows PowerShell 5.1) | `2ce387a` | production drill **28/28** (incl. ops-status WARN without failing, `first-start --check` without FAIL, `hosxp-verify` PASS for the real read-only account and FAIL with INSERT through a view); `tools/check.py --acceptance-report` **10/10 gates**, 1,355 passed, 27 skipped (MariaDB connector tests - no local MariaDB; the two role-creating tests; the POSIX-signal supervisor test); acceptance **84 PASS**; `status.ps1 -SkipTasks` under the production profile on `ppr_dev` shows the MOCK/DEMO line as WARN and fails only on the expected items (API/UI not running, development account); `first-start --check` on `ppr_dev` reports the expected FAILs of a development machine (database name, settings, HOSxP mode), the MOCK/DEMO WARN and the OPEN items - read only |

| Cloud, `main` after fast-forward merge | `749bfa7` | `tools/check.py --acceptance-report` 10/10 gates, 1,380 passed, 2 skipped; acceptance 84 PASS; production drill 28/28 (a first run hit a PostgreSQL outage of the cloud container and was repeated after restarting it) |
| Tag | `749bfa7` | `phase1-wave10b2-baseline` pushed from Windows by `_w10b2tag.bat` (2026-10-02) after its checks: working tree clean, the commit is `origin/main`'s head, the tag did not exist |

## Consequences

- IT can see, before go-live, exactly what is verified (PASS), wrong (FAIL), still to do (TODO)
  and still unproven (OPEN), with saved evidence for the product owner's sign-off.
- A production database that once held demonstration data keeps working but says so on every
  page; its MOCK Plan Items can never be activated, consumed or released in production.
- `hosxp-verify` cannot prove what the HOSxP data itself cannot show (closing status, login hash
  format): those remain OPEN until IT's evidence and the product owner's decisions arrive (Wave 9).
