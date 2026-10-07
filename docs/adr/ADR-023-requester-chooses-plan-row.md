# ADR-023 — The requester chooses the plan row of every PR line (Wave 12A-2)

- **Status:** Accepted - merged to `main` by fast-forward at `fe72a75` (2026-10-07); baseline tag `phase1-wave12a2-baseline` on that post-merge verified commit. D-43 approved by the product owner (2026-10-07: no HOSxP codes in the plan, the template drops the item column; the requester chooses on every PPR and nothing is remembered - no automatic match even for rows that carry a code; a wrong choice is corrected through a Plan Officer unlock; the HOSxP code of a manually added row is optional). Product owner, 2026-10-07, after review: a requester may see the offered rows and their remaining balance of the PR's department (accepted); Assigned Purchase items keep a HOSxP item (D-38 A-1); several rows may carry the same HOSxP item for one department and fund source - to be implemented in Wave 12A-3
- **Spec:** D-43, §40.18; supersedes the D-16 automatic match and the binding / review parts of D-42; amends D-41 (what a requester sees of the plan). Migration `0012` (0001-0011 unchanged).

## Context

After Wave 12A-1 the plan is imported from Excel without HOSxP item codes. D-42 planned a
binding of a HOSxP item to a plan row at the first PPR, reviewed by the Head of Procurement and
then remembered. The product owner chose instead an explicit choice on every PPR: the plan
stays in the planning team's names and units, and the person who knows what is bought says,
for each PR line, which plan row it uses (2026-10-07).

## Decision

| Topic | Implementation |
|---|---|
| Rows offered | `domain/prevalidation.py`: for each PR line, the current ACTIVE year's Plan Items that answer the PR's department (owner of a DEPARTMENT row, purchaser of a CENTRAL / ASSIGNED row, D-38) and fund source, **counted in the line's unit** (the PR line's unit, else the HOSxP item's; exact text, outer spaces ignored; unknown unit = nothing offered). The HOSxP item of a row is never used to match. |
| Results | none offered `NOT_IN_PLAN`; no choice `PLAN_ROW_NOT_CHOSEN`; a choice outside the rows offered `PLAN_ROW_NOT_ALLOWED`; otherwise the hard control applies to the chosen row, lines choosing the same row in aggregate (unchanged). `AMBIGUOUS_PLAN_MATCH` is no longer raised. D-38 A-1 blocking is unchanged for Assigned Purchase rows that carry a HOSxP item. |
| Check page | `GET /api/prs/{pr_no}/prevalidation?choice=<pr_item_id>:<plan_item_id>` (repeatable; malformed = 400 `BAD_CHOICE`): each line lists `offered`, and `options` gives every offered row's name, unit, HOSxP item / code (if any), plan type and remaining balance. Read-only as before. |
| Choices kept | Table `ppr_line_choice` (PPR, PR line, plan row, the row's name and unit as chosen, who, when; deleted with the PPR). `POST /api/pprs` takes `{pr_no, choices}`: a draft is created only when every line passes with them (a line the PR does not have: `CHOICE_NOT_A_PR_LINE`). `PUT /api/pprs/{id}/choices`: the creator only, DRAFT or UNLOCKED_FOR_REVISION only, the PR read live; the lines named change, the others keep their choice (a kept choice the line may no longer use is dropped); a line the PR does not have (`CHOICE_NOT_A_PR_LINE`) or a row not offered - also to a line offered nothing - (`PLAN_ROW_NOT_ALLOWED`) is refused; other checks wait for confirmation (as allocations); audit `PPR_CHOICES_UPDATED` (before / after). Working Assigned Purchase coverage of a row no longer chosen is dropped. A draft's lines follow the new choices at once; an unlocked PPR keeps the lines of its confirmed version - whose usage reconfirmation releases - until reconfirmed. `GET /api/pprs/{id}/choices[?choice=...]`: the stored choices and the check as confirmation would run it. |
| Confirmation | Confirm / reconfirm evaluate with the stored choices (both evaluations, under the same locks as before); usage is posted to the chosen rows; the version snapshot carries each line's plan row name and unit. A PR line whose unit changed in HOSxP after the choice fails closed (`PLAN_ROW_NOT_ALLOWED`); a row renamed or re-united since it was chosen (amendment while only drafts chose it) is refused (`PLAN_ROW_CHANGED`) until the requester chooses it again. Drafts and unlocked PPRs existing before `0012` have no stored choice: their requester chooses before confirming (`PLAN_ROW_NOT_CHOSEN`). |
| Nothing remembered | Each PPR's choices belong to it; the next PR of the same item is offered the rows again. |
| Plan rows without an item | The import template has no item column (an item column left from an older template is not read: warning `ITEM_COLUMN_IGNORED`); `ck_plan_item_code_or_import` dropped by `0012`. A Plan Item entered on screen may have no HOSxP item and then needs its own name and unit (`NAME_AND_UNIT_REQUIRED`); its name and unit change only while no PPR has ever drawn on it (`NAME_CHANGE_AFTER_USE`). An amendment may add rows without an item (name and unit) and rename a row without one that no PPR has ever drawn on, even if released since (`DATA_CHANGE_AFTER_USE`); a row with a HOSxP item takes the item's name and unit (`NAME_FROM_HOSXP`). An Assigned Purchase item still needs a HOSxP item (`ITEM_REQUIRED` / `ASSIGNED_WITHOUT_ITEM`: D-38 A-1 recognizes the demand by it). The rule that a HOSxP item is planned once per department and fund source applies only to rows that carry one (unchanged). |
| User interface | PR check page: a select per line (offered rows with unit, code, plan type, remaining), "ตรวจกับรายการแผนที่เลือก" (GET, nothing stored), then "สร้างฉบับร่าง" posts exactly the checked rows. PPR page: each line with its plan row; while DRAFT / unlocked, the creator changes the rows (others see them). Print: plan row name and unit per line. Plan item and amendment forms: code optional with name and unit. No client JavaScript (CSP unchanged). |
| Migration `0012` | creates `ppr_line_choice`, drops `ck_plan_item_code_or_import`; downgrade refuses while a choice is stored (who chose which row would be lost) or a Plan Item has neither a HOSxP item nor an import (the restored check would refuse it). |

## §40.18 acceptance criteria

| ID | Criterion | Tests |
|---|---|---|
| AT-40.18.1 | Rows offered by department, fund and unit; not chosen / not allowed; no automatic match | `test_prevalidation` (D-43 tests), `test_line_choice_api::test_a_line_offered_nothing_never_keeps_a_choice`, `test_line_choice_api::test_a_draft_needs_a_chosen_row_for_every_line_and_usage_goes_to_it`, `test_line_choice_api::test_a_unit_changed_in_hosxp_after_the_draft_fails_closed`, `test_plan_import_api::test_imported_rows_are_activated_and_chosen_by_the_requester`, `test_central_api` (both rows offered, fails closed) |
| AT-40.18.2 | Draft only with valid choices; hard control per chosen row in aggregate; usage to the chosen row | `test_line_choice_api::test_a_draft_needs_a_chosen_row_for_every_line_and_usage_goes_to_it`, `test_prevalidation::test_lines_of_different_items_choosing_one_row_are_checked_in_aggregate` |
| AT-40.18.3 | Choices changed by the creator while DRAFT / unlocked, audited; reconfirmation moves usage; nothing remembered | `test_line_choice_api::test_choices_change_only_by_the_creator_while_editable_and_are_audited`, `test_line_choice_api::test_a_row_renamed_after_it_was_chosen_is_chosen_again`, `test_sync_api::test_cancellation_after_reconfirmation_releases_what_is_still_held`, `frontend/test/app.test.js` (D-43) |
| AT-40.18.4 | PPR page and print show each line's plan row | `test_line_choice_api::test_a_draft_needs_a_chosen_row_for_every_line_and_usage_goes_to_it`, `frontend/test/app.test.js` (D-43) |
| AT-40.18.5 | Rows without an item on screen and by amendment; name / unit fixed once used; no item column | `test_line_choice_api::test_rows_without_an_item_are_added_and_renamed_by_amendment_only_while_unused`, `test_plan_import_api::test_an_imported_row_is_edited_in_draft_and_given_an_item_only_in_its_unit`, `test_plan_import_api::test_an_item_column_from_the_old_template_is_not_read`, `test_migration_0012` |

## Consequences

- Every PPR needs one choice per PR line; the requester sees the offered rows' names, units and
  remaining balances (D-41 amended). A row the planning team wrote in a unit other than the one
  departments use in HOSxP is offered to no line: the Plan Officer renames / re-units it while it
  is unused (DRAFT edit or amendment).
- The Head of Procurement verifies as before and sees each line's plan row on the PPR and print.
- After the update, `ppr.cli migrate` (owner account) applies `0012` and grants the runtime
  account the new table, as for every update.
- Open: G-4, D-25, D-30, D-39 unchanged.

## Evidence

| Where | Commit | Result |
|---|---|---|
| Independent review (read-only, working tree) | before commit | no blocker; fixed: a choice for a line offered no row was stored unchecked (now `PLAN_ROW_NOT_ALLOWED`, ids bounded), a row renamed by amendment while only drafts had chosen it was used without a new choice (now `PLAN_ROW_CHANGED`), "used" for the name rule meant outstanding usage (now any PPR ever), a partial change dropped the other lines' choices (now only the lines named change), unknown lines handled differently at creation, downgrade dropping choices, a misleading message on the PPR page; the check page showing every offered row's remaining balance of the PR's department to any requester was accepted by the product owner (D-41 / D-43) |
| Cloud (Linux, PostgreSQL 16.13; MariaDB for the SQL-adapter tests) | `787de35` | `tools/check.py --acceptance-report` 10/10 gates, 1,511 passed, 2 skipped (the two role-creating tests); acceptance **101 PASS** (AT-40.18.1-.5 included); UI tests 43/43 |
| Cloud production drill (`drill_production.py`, superuser) | `787de35` | 28/28 (migrates to `0012`) |
| Windows dev machine (PostgreSQL 16.15, Node 24.16, Python 3.12.9) | `bf19776` | `_verify12a2.bat` (2026-10-07): production drill **28/28** at schema 0012; `tools/check.py --acceptance-report` **10/10 gates**, 1,486 passed, 27 skipped (MariaDB connector tests - no local MariaDB; the two role-creating tests; the POSIX-signal supervisor test); acceptance **101 PASS** incl. AT-40.18.1-.5; UI tests exit 0 |
| Cloud, `main` after fast-forward merge | `fe72a75` | `tools/check.py --acceptance-report` 10/10 gates, 1,511 passed, 2 skipped; acceptance 101 PASS |
| Tag | `fe72a75` | `phase1-wave12a2-baseline` pushed from Windows by `_w12a2tag.bat` (2026-10-07) after its checks: working tree clean, the commit is `origin/main`'s head, the tag did not exist |
