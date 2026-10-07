# ADR-020 — Sign-in on a separate HOSxP server; the PPR's department is the PR's (Wave 11A)

- **Status:** Accepted - approved by the product owner and merged to `main` by fast-forward at `3ad5c4a` (2026-10-06; governance discard by Plan Officer and Admin confirmed); baseline tag `phase1-wave11a-baseline` on the post-merge verified commit
- **Spec:** D-41 (product owner, 2026-10-05), §21, §22.1, §40.15; amends D-09; keeps D-25 and
  D-40. No HOSxP table, column, status or login detail is written in code: the hospital's
  login query stays in its site query file.

## Context

IT reported (2026-10-05) that at the hospital the HOSxP user accounts are on one PostgreSQL
server (login) and the stock / PR data on another (inventory); the two use different
department codes; the login table has no department, and the stock server's user tables are
empty, so HOSxP cannot say which stock department a user belongs to. Until now the system
read everything through one HOSxP connection and allowed a requester to act only for PRs of
their own department (D-09) - nobody could sign in, and nobody would have had a department.

## Decision

| D-41 | Implementation |
|---|---|
| Second read-only connection | `PPR_HOSXP_AUTH_DATABASE_URL` (optional). `server.build` gives the HOSxP sign-in adapter its own `SqlHosxpGateway` on that URL with the same site query file; the PR / master / stock gateway keeps `PPR_HOSXP_DATABASE_URL`. Unset = one connection, as before. Login server down = `HOSXP_UNAVAILABLE` (503), no fallback (D-40). |
| No department needed | The sign-in queries may return no department (the contract already allowed it). Roles are still granted one by one by an ADMIN. |
| PPR department = PR department | `prevalidate_pr` and `prepare_draft` no longer compare the PR's department with the user's (`check_department` and the `PR_OTHER_DEPARTMENT` / `NO_DEPARTMENT` refusals are removed). The PPR is created with the PR's department as before, so plan matching, CENTRAL / ASSIGNED purchaser rules and every hard control are unchanged. |
| Only the creator acts | `_require_requester_of(department)` became `_require_creator(rec)`: REQUESTER role and `ppr.created_by_user_id = caller` for allocations, coverage, discard, confirm and reconfirm (`NOT_THE_CREATOR`, 403). Plan Officer / Admin unlock unchanged. `PprOut.created_by_user_id` lets the user interface offer these actions to the creator only. |
| Abandoned draft | A draft keeps its PR from every other PPR (D-05) and only its creator could discard it; a `PLAN_OFFICER` or `ADMIN` (the unlocking roles) may now discard another user's draft with a required reason (`REASON_REQUIRED`, 422), audited as `PPR_DRAFT_DISCARDED` with `by_governance`, the creator and the reason. The route takes any signed-in user; the service refuses everyone else. A requester who also holds one of these roles discards their own draft as its creator. |
| Requester reads own PPRs | `Caller.department_scope` became `Caller.ppr_scope: PprScope | None`; `PprScope(created_by_user_id)` for a user whose only role is REQUESTER, `None` for every other role. The PPR repository (`get`, `search`, `find`, `count`, `count_by_state`) and the sync observations of a run filter on `created_by_user_id`; one PPR of another user answers 404. Dashboards say "PPR เฉพาะที่คุณสร้าง"; PPR reports carry the scope line "เฉพาะ PPR ที่คุณสร้าง". Plan data keeps its department scope (none when HOSxP gives none). |
| Check tools | `hosxp-check` prints the login database (redacted), connects to it, and `--user NAME` checks the sign-in queries for one account: found, source id, active, department yes/none, whether the stored value has the MD5 form, profile found - never the hash, the password or a person's name. `hosxp-verify` runs the read-only account check on the login account too (`[login account]`). The PostgreSQL account check now also reads role attributes (superuser, CREATEROLE, CREATEDB, BYPASSRLS) over every role the account belongs to, fails membership of `pg_execute_server_program`, `pg_write_server_files`, `pg_write_all_data`, and fails a membership it does not inherit but can take with `SET ROLE` (PostgreSQL 16+: the `SET` option), since `has_*_privilege` does not count those. |
| Documents | `.env.example`, `hosxp_queries.example.toml` (login on another server; ids as TEXT - `AS CHAR` is one character in PostgreSQL; no invented default values), HOSXP_CONNECTION, IT_HANDOFF, DEPLOY_PRODUCTION, GO_LIVE_CHECKLIST A3 / A5. |

## §40.15 acceptance criteria

| ID | Criterion | Tests |
|---|---|---|
| AT-40.15.1 | Sign-in reads only the login server; down = 503, no fallback | `test_login_server::test_sign_in_reads_only_the_login_server` |
| AT-40.15.2 | A requester without a department makes a PPR for any department's PR; it draws on that department's plan | `test_ppr_api::test_a_requester_without_a_department_makes_a_ppr_for_any_departments_pr`, `test_prevalidation_api::test_a_requester_validates_a_pr_of_any_department` |
| AT-40.15.3 | Only the creator edits, discards, confirms, reconfirms; Planning / Admin discard an abandoned draft with a reason | `test_ppr_api::test_only_the_requester_who_created_a_ppr_confirms_it`, `test_ppr_api::test_an_abandoned_draft_is_discarded_by_planning_with_a_reason`, `frontend/test/app.test.js` (D-41 creator-only actions) |
| AT-40.15.4 | A REQUESTER-only user reads only their own PPRs everywhere | `test_isolation::test_a_requester_never_reads_a_ppr_someone_else_created`, the full route sweep, `test_ppr_api::test_only_the_requester_who_created_a_ppr_confirms_it` |
| AT-40.15.5 | Both connections tested and verified read-only | `test_login_server::test_hosxp_check_tests_the_login_server_without_showing_secrets`, `test_login_server::test_hosxp_verify_checks_the_login_account_too`, `test_go_live_tools::test_postgresql_account_check_on_real_roles` |

## Consequences

- The hospital can sign in with HOSxP accounts kept on the login server, with no department.
- Control moves from "the requester's department" to "the PR's department + the creator +
  Head of Procurement verification + audit": any requester can prepare a PPR that draws on
  any department's plan. This is the product owner's choice (D-41, option ก); the audit
  trail records who prepared each PPR.
- D-25 stays OPEN until a real HOSxP user signs in (hash form, encoding, active flag).

## Evidence

| Where | Commit | Result |
|---|---|---|
| Independent review | `75342c2`, re-review `6aef7b7` | no blocker; fixed: abandoned draft could block a PR for ever (governance discard with a reason), PostgreSQL check missed role attributes / predefined roles / SET-able roles through memberships, UI offered actions only the creator may take, isolation test widened to every PPR route, report and export; left: a failing sign-in query answers 500 (as before this wave) |
| Cloud (Linux, PostgreSQL 16.13; MariaDB for the SQL-adapter tests) | `4c6d0f4` | `tools/check.py --acceptance-report` 10/10 gates, 1,386 passed, 2 skipped (the two role-creating tests: the test account may not create roles; `test_postgresql_account_check_on_real_roles` was also run as a local superuser on `6aef7b7` - passed, the account-check code is unchanged since); acceptance **89 PASS** (AT-40.15.1-.5 included); UI tests 37/37 |
| Cloud production drill (`drill_production.py`, superuser) | `4c6d0f4` | 28/28 - the stricter account check still passes the real read-only stand-in account and fails INSERT through a view |
| Windows dev machine (PostgreSQL 16.15, Node 24.16, Python 3.12.9) | `0b57f7f` | production drill **28/28** (incl. the stricter `hosxp-verify` account check: the real read-only account passes, INSERT through a view fails); `tools/check.py --acceptance-report` **10/10 gates**, 1,361 passed, 27 skipped (MariaDB connector tests - no local MariaDB; the two role-creating tests; the POSIX-signal supervisor test); acceptance **89 PASS** incl. AT-40.15.1-.5 (`_verify11a.bat`, 2026-10-06) |
| Cloud, `main` after fast-forward merge | `d8b22d5` | `tools/check.py --acceptance-report` 10/10 gates, 1,386 passed, 2 skipped; acceptance 89 PASS |
| Tag | `d8b22d5` | `phase1-wave11a-baseline` pushed from Windows by `_w11atag.bat` (2026-10-06) after its checks: working tree clean, the commit is `origin/main`'s head, the tag did not exist (a first run stopped at `git tag` on a quoting error in the batch file; nothing was created) |
