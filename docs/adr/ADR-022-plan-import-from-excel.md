# ADR-022 — Plan imported from Excel; rows without a HOSxP item (Wave 12A-1)

- **Status:** Accepted - merged to `main` by fast-forward at `b710106` (2026-10-07); baseline tag `phase1-wave12a1-baseline` on that post-merge verified commit. D-42 approved by the product owner (2026-10-07: pending bindings usable by other PRs, permanent bindings changed by amendment, the wave split into 12A-1 import and 12A-2 binding, budget lines entered before import, `ASSIGNED` not imported)
- **Superseded in part by ADR-023 (D-43, Wave 12A-2):** no item column, no pre-binding and no binding review - the requester chooses the plan row on every PPR.
- **Spec:** D-42, §40.17; amends D-16 (a row without an item matches no PR line until Wave 12A-2 binds it). Migration `0011` (0001-0010 unchanged).

## Context

The hospital's 2570 plan is kept by the planning team in Excel per department (some 4,000
rows): item names and units as the team writes them, quantities, prices, amounts and
quarters, but **no HOSxP item codes**. Every Plan Item so far had to name a HOSxP item, so the
plan could only be typed in row by row with a code (planning team files, 2026-10-06).

## Decision

| Topic | Implementation |
|---|---|
| Template | `GET /api/plan-years/{fy}/import-template`: sheets `หัวข้อ` (heading: plan type, owner / purchasing department, fund source, budget category - HOSxP `source_id`s), `รายการ` (rows), the synchronized active departments, fund sources and budget categories, and the year's budget lines. Columns are found by title. |
| Reading | `api/plan_import_xlsx.py`: at most 10 MB, a real `.xlsx` (ZIP with `xl/workbook.xml`, unpacked size capped), openpyxl read-only with cached values (formulas never evaluated), at most 20,000 rows with values and 200,000 rows scanned per sheet. The user interface reads the upload with its own small multipart reader (`frontend/src/multipart.js`, no new package). |
| Checking | `domain/plan_import.py` (pure): exact codes only, active records only, `DEPARTMENT` / `CENTRAL` only (`ASSIGNED` needs per-department demand - entered on screen), CENTRAL needs a purchaser, the budget line must exist (never created from the file), numbers read as Excel holds them - a number cell to Excel's 15 significant digits, so a formula result such as 3 x 1.1 (stored as 3.3000000000000003) reads 3.3 as on the sheet (from 1e10 up, where 15 digits could cut real digits, the float's own digits are kept and refused as too precise); text numbers only as plain digits with thousands commas in groups of three ("1,5", Thai digits, "1e3" are refused) - and **never rounded** (more decimals than stored, or more digits than the column holds, = error; digits counted with `adjusted()`, so 1E+20 is caught), quantity 0 with an amount = error, quantity and amount 0 = skipped and listed, amount ≠ quantity x price = warning (as for manual items). A pre-bound row's HOSxP item must be active and counted in the row's unit; pre-bound rows may not create a second match for one item, department and fund (D-16 / D-38). |
| All or nothing | `POST .../imports/preview` writes nothing; `POST .../imports` re-reads and re-checks the file (optionally the previewed SHA-256) and writes every row or none: `IMPORT_HAS_ERRORS` (409) with the errors. DRAFT years only (`PLAN_YEAR_NOT_DRAFT`). |
| Records | `plan_import` (file name, SHA-256, rows imported / skipped, total, who / when; a live import of the same SHA-256 in a year is unique). `plan_item.plan_import_id`, `source_sheet`, `source_row`; `item_source_id` / `item_code_snapshot` may be empty only for an imported row (`ck_plan_item_code_or_import`). One audit entry `PLAN_IMPORTED` per file. |
| Removal | `POST /api/plan-imports/{id}/remove` with a reason, DRAFT years only (the record is read again under the year lock and marked only while live, so a double click removes once): the import's Plan Items are deleted (a DRAFT plan has no ledger entry and no PPR), the record is marked `REMOVED` with who / when / why, audit `PLAN_IMPORT_REMOVED`. The same file may then be imported again. |
| Rows without an item | Never match a PR line (`NOT_IN_PLAN`), are left out of the duplicate-match checks (plan validation and amendments), count in the budget envelope, are activated into the Plan Ledger like any Plan Item and can be amended in their values. In DRAFT the Plan Officer may edit them and may pre-bind one (unit must match: `UNIT_MISMATCH`; the row then takes the HOSxP name, the plan's name stays in the audit entry); binding by amendment is refused (`UNBOUND_ROW`) - that is Wave 12A-2's reviewed binding. A manual Plan Item still needs a HOSxP item (`ITEM_REQUIRED`). The Central Plan usage report shows an unbound central row's issue figures as unknown, with a note, never 0. |
| User interface | Plan year page: "นำเข้าจาก Excel" → template download, upload and check, result (budget comparison, errors / warnings / skipped rows with sheet, row and column), confirm (the checked copy is kept in the interface's memory for 30 minutes, one per session, bound to that session, used once; the upload is read only for a signed-in session, at most 10 MB; a larger file is read and discarded up to 30 MB so the browser gets the "file too large" page, and only an upload beyond that is cut off), import history with removal. Unbound rows are marked "ยังไม่ผูกรหัส HOSxP". |

## §40.17 acceptance criteria

| ID | Criterion | Tests |
|---|---|---|
| AT-40.17.1 | Template lists codes; preview reports everything and writes nothing | `test_plan_import_api::test_the_template_lists_the_hosxp_codes_and_budget_lines`, `test_plan_import_api::test_a_valid_file_is_previewed_then_imported_once` |
| AT-40.17.2 | Any error imports nothing | `test_plan_import_api::test_any_error_imports_nothing_and_every_error_is_reported`, `test_plan_import_api::test_a_file_that_is_not_the_template_is_refused`, `test_plan_import_api::test_numbers_beyond_the_columns_and_loose_commas_are_errors`, `test_plan_import_api::test_a_pre_bound_row_may_not_duplicate_a_planned_item`, `test_plan_import_rules` |
| AT-40.17.3 | DRAFT only; source kept; same file once; one audit entry | `test_plan_import_api::test_a_valid_file_is_previewed_then_imported_once`, `test_plan_import_api::test_unbound_rows_are_activated_but_never_match_a_pr`, `test_migration_0011` (old rows kept, no item only for imports, downgrade refused once an import exists), `frontend/test/app.test.js` (D-42 upload, confirm once, session-bound, 10 MB) |
| AT-40.17.4 | Unbound rows: no PR match, no duplicate check, envelope and ledger as any item; pre-bound rows ordinary | `test_plan_import_api::test_unbound_rows_are_activated_but_never_match_a_pr`, `test_plan_import_api::test_an_imported_row_is_edited_in_draft_and_pre_bound_only_in_its_unit`, `test_plan_import_api::test_every_plan_view_and_report_shows_unbound_rows`, `test_plan_import_api::test_an_unbound_row_is_amended_in_value_and_its_history_kept` |
| AT-40.17.5 | Removal with a reason while DRAFT; record and audit stay | `test_plan_import_api::test_an_import_is_removed_with_a_reason_while_draft`, `test_plan_import_api::test_the_import_audit_stays_after_removal` |

## Consequences

- The planning team fills the template (headings get their HOSxP codes once) and imports the
  whole plan in one step; the envelope check at approval still compares it with the real
  approved budget.
- Until Wave 12A-2, a PR line whose item is planned only in an unbound row is `NOT_IN_PLAN`:
  import the plan now, start PPRs from imported rows after 12A-2 (or pre-bind rows).
- After the update, `ppr.cli migrate` (owner account) applies `0011` and grants the runtime
  account the new table, as for every update.

## Evidence

| Where | Commit | Result |
|---|---|---|
| Independent review | `d8fc079`, re-review `20802f0` | no blocker; fixed: digits counted from the text form let 1E+20 pass (now `adjusted()`), Excel formula results (3 x 1.1) refused as too precise (now read at Excel's 15 digits; above 1e10 never shortened), "1,5" read as 15 (strict text numbers), a double-clicked removal could overwrite the first (re-read under the lock, guarded update), "None" shown as an item code in the amendment report, the upload buffered before sign-in, an endless upload, one waiting file per session, blank-row limit, template text prefix, downgrade dropping removed records; left (nits): the confirm tick box is checked in the browser only, refreshing the result page uploads again, an oversize upload may show a connection reset in some browsers |
| Planning team's data (dry run) | `90d2d40` | the 4,036 rows already prepared (`แผนนำเข้า_PPR_2570.xlsx`) converted to this template and checked by `check_import` with stand-in codes: no number or precision error; 23 rows lack a unit or quantity / price / amount and must be filled; 38 rows of quantity and amount 0 are skipped |
| Cloud (Linux, PostgreSQL 16.13; MariaDB for the SQL-adapter tests) | `90d2d40` | `tools/check.py --acceptance-report` 10/10 gates, 1,479 passed, 2 skipped (the two role-creating tests); acceptance **96 PASS** (AT-40.17.1-.5 included); UI tests 41/41 |
| Cloud production drill (`drill_production.py`, superuser) | `90d2d40` | 28/28 (migrates to `0011`) |
| Windows dev machine (PostgreSQL 16.15, Node 24.16, Python 3.12.9) | `ed19d9e` | production drill **28/28** (schema 0011); pytest 1,454 passed, 27 skipped; acceptance **96 PASS**; **frontend gate FAILED** (`TypeError: fetch failed` in the UI tests): the oversize-upload test - the interface closed the connection while the client was still sending, which Windows reports as a network error. Fixed: an oversize upload is drained (up to 30 MB) before the answer |
| Cloud, after the upload fix | `29c56f7` | `tools/check.py --acceptance-report` 10/10 gates, 1,479 passed, 2 skipped; acceptance 96 PASS; UI tests 41/41 |
| Windows dev machine (PostgreSQL 16.15, Node 24.16, Python 3.12.9) | `29c56f7` | `tools/check.py --acceptance-report` **10/10 gates**, 1,454 passed, 27 skipped (MariaDB connector tests - no local MariaDB; the two role-creating tests; the POSIX-signal supervisor test); acceptance **96 PASS** incl. AT-40.17.1-.5; UI tests exit 0 (`_verify12a1b.bat`); production drill **28/28** at schema 0011 (`_drill12a1b.bat`, 2026-10-07 - a first drill in `_verify12a1b.bat` stopped before starting because the typed password could not be read, Thai keyboard layout) |
| Cloud, `main` after fast-forward merge | `b710106` | `tools/check.py --acceptance-report` 10/10 gates, 1,479 passed, 2 skipped; acceptance 96 PASS |
| Tag | `b710106` | `phase1-wave12a1-baseline` pushed from Windows by `_w12a1tag.bat` (2026-10-07) after its checks: working tree clean, the commit is `origin/main`'s head, the tag did not exist |
