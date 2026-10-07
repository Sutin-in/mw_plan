# ADR-017 — Production readiness without the real HOSxP (Wave 10A)

- **Status:** Accepted - merged to `main` at `71f7a11` (2026-10-01)
- **Spec:** §19, §22.1, §25.5, §36, §38, §39, §40, §44 Wave 10; ADR-004 (go-live items).
  Real HOSxP work (Wave 9) and the IT hand-over (Wave 10B) are not part of this wave.
  G-4 and the HOSxP cancellation native-status mapping stay OPEN; no native HOSxP status was
  added or guessed (the drill's HOSxP stand-in is the existing made-up `mockhx_*` fixture).

## Decision

| Area | What exists now |
|---|---|
| Concurrency (§19, §38) | `tests/db/test_concurrency.py`: double-submitted confirmation confirms once (one version, binding, ledger entry); parallel confirmations get distinct consecutive numbers; a confirmation waits for an amendment's plan-year lock and is judged against the amended plan; two amendments from one form record one; two purchases never cover one demand beyond its need; verification and cancellation of PPRs covering the same demands run in parallel without deadlock (3 rounds). Lock waits are proven from `pg_stat_activity`, not by timing. With the earlier ones (confirmation vs confirmation, verification vs verification / appointment, synchronization vs confirmation, two cancellations). |
| Idempotency (§25.5, §38.2) | `tests/db/test_idempotency.py`: a second run over unchanged PRs changes nothing (ledger, states, business audit); a PR change seen again is not recorded again; a retried run releases a cancellation once (retrying again changes nothing); demand states change once however often a cancellation is seen. |
| Authorization / isolation (§22.1, §39) | `tests/db/test_isolation.py` calls **every GET route of the API** (read from the application, so a new route fails until it is given a target) as a requester of an unrelated department, pointing each path parameter at other departments' records and running every report on screen and as Excel; answers must be refusals or free of their data. A department filter never widens the scope. Positive control: the owner and the purchaser do see their records. The write side is the existing authorization matrix (every route, every role). |
| Acceptance (§40) | The traceability gate now requires **100 %** of §40 criteria to be referenced by tests. `python tools/check.py --acceptance-report` adds a 10th gate that reports each criterion with its tests and how they ended in that run (`docs/acceptance_report.md`). Tests were added for AT-40.12.3 (demand preserved), AT-40.12.4 (a fulfilled demand is not bought again without an approved change) and AT-40.13.3 (an old year's PPR stays searchable). **AT-40.12.1 / 40.12.2 (stock issue / return in the ledger) are DEFERRED** with their reason in `docs/acceptance_deferred.txt`: no verified HOSxP query exists and D-38 C-3 shows stock issues only - to be decided in Wave 10B. |
| Production profile (§39) | `PPR_PROFILE=production` refuses: demo mode, `*_dev` / `*_test` databases, a runtime database account that is not least-privilege, an API listening beyond 127.0.0.1, a user interface without HTTPS settings (`PPR_UI_SECURE_COOKIES=1`, a named `PPR_UI_TRUST_PROXY`, a local API, and with a local proxy a local listening address). |
| Role split (ADR-004) | `ppr.cli migrate` runs as the schema owner (`PPR_MIGRATION_DATABASE_URL`) and grants the runtime role (`PPR_RUNTIME_ROLE`) exactly: SELECT/INSERT/UPDATE/DELETE on ordinary tables, **SELECT/INSERT only** on the 8 append-only evidence tables (found from their `*_no_update_delete` triggers), SELECT on `alembic_version`, sequence USAGE. The least-privilege check also refuses superuser, CREATEROLE / CREATEDB / BYPASSRLS, schema or database CREATE, owning tables and membership of a role that owns tables or has admin rights. |
| Health and monitoring | `GET /api/health` 200 only when the database answers (503 otherwise); UI `GET /healthz` (liveness) and `/healthz?deep=1` (reaches the API); `ppr.cli ops-status` (schema version, least-privilege account in production, last successful nightly sync within 26 h; exit code for monitoring); `tools/ops/status.ps1` adds the scheduled tasks. |
| Running unattended | `tools/ops/supervise.py` (standard library): restarts a service that exits (5 s doubling to 60 s) or stops answering its health check 3 times; dated logs with daily pruning; one supervisor per service (OS file lock, so stale files or reused process ids never block a start); stops its service first when terminated; services in their own process group. `tools/ops/register_tasks.ps1` registers 4 tasks in `\PPR\`: API and UI at start-up plus a 5-minute watchdog trigger (ignored while running), nightly sync and daily backup at times IT chooses. No new dependency. |
| HTTPS | Vendor-neutral requirements in `docs/DEPLOY_PRODUCTION.md` §4 (certificate in IT's store, TLS 1.2+, redirect, forward only to the UI, forwarding headers, body size and timeouts, firewall). The UI trusts `X-Forwarded-*` only from the named proxy and logs the real client address per request (no query strings). No certificate, key or credential is in the repository. |
| Backup / restore (§38.3) | `ppr.cli backup` exports one REPEATABLE READ snapshot, computes a manifest in it (row count and row hash of every table in a fixed text form - UTC, ISO dates, "C" order - sequences, triggers, schema version; no row data) and runs `pg_dump --snapshot` on the same snapshot. `restore --target-db` loads into another empty database on the same server with the owner's credentials (no password on any command line), checks the dump against its manifest, refuses the live database (recognised by server start time and database oid, whatever host name is used) and, in production, never replaces the configured databases. `verify-restore` recomputes and compares the manifest. |

## Evidence

| Where | Commit | Result |
|---|---|---|
| Cloud (Linux, PostgreSQL 16.13) | `32f63b1` | `tools/check.py --acceptance-report` 10/10 gates, 1,299 passed, 1 skipped (role-split test: the test account may not create roles; run as a superuser it passes, 3/3); acceptance 75 PASS, 2 DEFERRED |
| Cloud production drill (`tools/dev/drill_production.py`, superuser) | `32f63b1` | 24/24: role split and refusals by PostgreSQL itself, production refusals, API under the supervisor with the SQL HOSxP stand-in, sign-in, master sync, kill → restart, nightly + ops-status, UI refusal / deep health, backup → restore elsewhere → identical, live database refused under another host name |
| Windows dev machine (PostgreSQL 16.15, Node 24.16, Python 3.12.9) | `2c33ee8` | production drill 24/24; `tools/check.py --acceptance-report` 10/10 gates, 1,278 passed, 22 skipped (20 MariaDB connector tests - no local MariaDB; the role-split test, as on the cloud; the POSIX-signal supervisor test, Linux only); acceptance 75 PASS, 2 DEFERRED; `register_tasks.ps1` failed under Windows PowerShell 5.1 (no `$PSScriptRoot` in parameter defaults) |
| Windows, scripts re-checked | `76471fb` | Windows PowerShell 5.1.26100: `register_tasks.ps1 -PrintOnly` builds the 4 tasks; `status.ps1 -SkipTasks` runs and reports correctly (API/UI down on that machine as expected). The printed nightly/backup time showed UTC (18:30 for 01:30); the display was fixed afterwards - the trigger itself is created in local time |
| CI (GitHub Actions; its database account may create roles) | branch head | runs the role-split test that the cloud and Windows test accounts skip |
| Cloud, `main` after fast-forward merge | `71f7a11` | `tools/check.py --acceptance-report` 10/10 gates, 1,299 passed, 1 skipped; acceptance 75 PASS, 2 DEFERRED |
| Windows, supplemental (did not block the merge) | `9fca37e` | `register_tasks.ps1 -PrintOnly` shows the nightly and backup tasks at 01:30 and 03:00 (server local time) - the display fix holds; `status.ps1 -SkipTasks` runs (API/UI not running and no nightly run yet on that machine: those FAIL lines are expected; schema 0009 PASS) |
| Tag | `71f7a11` | `phase1-wave10a-baseline` pushed from Windows (2026-10-02) |

## Consequences

- Development machines are unchanged (`development` profile): one account, demo mode, mock HOSxP.
- Production needs two PostgreSQL accounts, the `.env` settings of DEPLOY §3 and the HTTPS proxy
  of §4 before the API or the UI will start.
- The test suite takes about a minute longer (concurrency, isolation sweep, backup round trip).
- Open for Wave 10B: the IT hand-over pack (HOSxP query guide, post-connection check, first
  start-up, go-live checklist) and the stock-issue question (AT-40.12.1/2) - the latter decided
  by D-39 and built in Wave 10B-1 (ADR-018).
