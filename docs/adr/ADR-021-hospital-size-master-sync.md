# ADR-021 — Hospital-size master sync; passwords never shown (Wave 11B)

- **Status:** Accepted - approved by the product owner (2026-10-06) and merged to `main` by fast-forward at `233541a`; baseline tag `phase1-wave11b-baseline` on that post-merge verified commit
- **Spec:** §40.16, §25.2, §25.4, §36. No HOSxP table, column, status or login detail changes.

## Context

At the hospital (2026-10-06) the first manual master sync (run 16) failed with
`OperationalError` and stored nothing. HOSxP answered every master query (13 fund sources, 476
categories, 450 departments, 43,707 items); the failure was in the PPR database: the master
upsert sent one multi-row `INSERT` with one parameter per value (43,707 items x 5 values),
and PostgreSQL's client protocol takes at most 65,535 parameters. Reproduced in the cloud
with the same message ("number of parameters must be between 0 and 65535").

Separately, `hosxp-check` printed the stock account's password: the password held a raw `#`,
which URL parsing reads as the start of a fragment, so `redact_url` found no password to hide.

## Decision

| Problem | Fix |
|---|---|
| Master upsert above the parameter limit | `SqlMasterRepository.upsert` sends batches of `MASTER_UPSERT_BATCH` (2,000) rows, at most 2,000 x 5 = 10,000 parameters; all batches stay in the caller's one transaction, so a sync is still all or nothing (a later batch failing undoes the earlier ones) and the created / updated counts are unchanged. |
| An id HOSxP returns twice (one statement refused it; batches would hide it) | Checked before writing: the run fails with `DUPLICATE_SOURCE_ID` (409), its error names the kind, the count and up to five ids, so IT can fix the site query; the repository refuses a repeat too. |
| Password shown when it holds `#` (or `@`, `/`, `?`) | `redact_url` no longer parses the URL: it hides everything from the first `:` after `scheme://` up to the last `@`, and a `password=` query parameter. When in doubt it hides more, never less. Every tool uses this one function. |
| Same parsing in `database_name` (review) | With `/`, `#` or `?` in the password it returned part of the password, or nothing - printed by first-start, used in backup file names, and an empty name skipped the production `_dev` / `_test` refusal. It now reads the name as SQLAlchemy does (`make_url`). |

## §40.16 acceptance criteria

| ID | Criterion | Tests |
|---|---|---|
| AT-40.16.1 | Hospital-size master sync in one transaction; repeat changes nothing; failure keeps the masters | `test_plan_core::test_a_hospital_size_catalogue_syncs_in_one_transaction`, `test_plan_core::test_a_failing_later_batch_keeps_every_master_as_it_was`, `test_plan_core::test_a_repeated_source_id_is_named_and_keeps_the_masters` |
| AT-40.16.2 | A database password is hidden whatever characters it holds | `test_session_and_settings::test_a_password_is_hidden_whatever_it_holds`, `test_session_and_settings::test_the_database_name_is_read_whatever_the_password_holds` |

## Consequences

- The hospital can sync its 43,707 items. IT should still change the HOSxP account's
  password that was shown on screen, and write special characters URL-encoded in `.env`
  (`#` as `%23`, `@` as `%40`).
- Known and not changed in this wave: a master query that HOSxP refuses (a query error, not
  an outage) leaves its sync run in `RUNNING`; to be decided separately.

## Evidence

| Where | Commit | Result |
|---|---|---|
| Independent review | `55d17fc`, re-review `3cfed55` | no blocker; fixed: `database_name` had the same parsing fault (part of the password as the name, or an empty name skipping the production `_dev` / `_test` refusal), a repeated HOSxP id gave a bare 500 (now `DUPLICATE_SOURCE_ID` naming the kind and ids), no test proved a later failing batch undoes the earlier ones, `password=` query parameter, a site account name in a test; left (nits): a raw `&` inside a `password=` query value, `sslpassword=`, an `@` in a query string without a password over-hides the host |
| Cloud (Linux, PostgreSQL 16.13; MariaDB for the SQL-adapter tests) | `3cfed55` | `tools/check.py --acceptance-report` 10/10 gates, 1,404 passed, 2 skipped (the two role-creating tests); acceptance **91 PASS** (AT-40.16.1-.2 included) |
| Cloud production drill (`drill_production.py`, superuser) | `3cfed55` | 28/28 |
| Windows dev machine (PostgreSQL 16.15, Node 24.16, Python 3.12.9) | `b447e42` | production drill **28/28**; `tools/check.py --acceptance-report` **10/10 gates**, 1,379 passed, 27 skipped (MariaDB connector tests - no local MariaDB; the two role-creating tests; the POSIX-signal supervisor test); acceptance **91 PASS** incl. AT-40.16.1-.2 (`_verify11b.bat`, 2026-10-06) |
| Cloud, `main` after fast-forward merge | `233541a` | `tools/check.py --acceptance-report` 10/10 gates, 1,404 passed, 2 skipped; acceptance 91 PASS (a first run failed because the container's PostgreSQL had stopped; rerun after restarting it) |
| Tag | `233541a` | `phase1-wave11b-baseline` pushed from Windows by `_w11btag.bat` (2026-10-06) after its checks: working tree clean, the commit is `origin/main`'s head, the tag did not exist |
| Hospital (IT machine) | `233541a` (ZIP) | delivered as `Plan_Claude_wave11b.zip` (GitHub is blocked there); sign-in with a HOSxP account and a live PR read (PR 7000156) seen on a screenshot 2026-10-07; manual master sync run 19 (2026-10-07 09:29) SUCCEEDED: 44,655 records checked, 44,655 created, 0 updated (screenshot) |
