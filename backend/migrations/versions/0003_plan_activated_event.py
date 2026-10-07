"""Rename the initial ledger event PLAN_APPROVED -> PLAN_ACTIVATED.

Revision ID: 0003
Revises: 0002

Why: the initial Plan Ledger entry is posted when a plan year moves APPROVED -> ACTIVE
(D-12), not when the Director approves the plan. The event is renamed so that

* Director approval       = plan-year state APPROVED
* Operational activation  = plan-year state ACTIVE
* Initial ledger event    = PLAN_ACTIVATED
* Audit action            = PLAN_ACTIVATED

Data: existing PLAN_APPROVED rows are preserved and relabelled PLAN_ACTIVATED; their
activation idempotency key ``approve:<plan_item_id>`` becomes ``activate:<plan_item_id>``
(the format the application now uses). Nothing else in a row changes (id, deltas,
actor, timestamps are kept).

Append-only guarantee: the row-level UPDATE/DELETE guard trigger is disabled only for
the relabelling UPDATE and re-enabled before the migration ends. PostgreSQL DDL is
transactional and Alembic runs this revision in one transaction, so a failure rolls
back to the original state with the trigger enabled. The trigger and its function are
never dropped. ``audit_log`` is not touched.

The CHECK constraints and the partial unique index that name the event are recreated
with the new name. Migrations 0001 and 0002 are unchanged.
"""

from __future__ import annotations

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels = None
depends_on = None

_OTHER_EVENTS = (
    "'PPR_CONFIRMED', 'PPR_RELEASED', 'PLAN_AMENDMENT', "
    "'CENTRAL_STOCK_ISSUE', 'CENTRAL_STOCK_RETURN'"
)

_SIGNS_TAIL = (
    "(event_type <> 'PPR_CONFIRMED' OR (qty_delta < 0 AND amount_delta <= 0)) AND "
    "(event_type <> 'PPR_RELEASED' OR (qty_delta >= 0 AND amount_delta >= 0 "
    "AND (qty_delta > 0 OR amount_delta > 0))) AND "
    "(event_type <> 'PLAN_AMENDMENT' OR (qty_delta <> 0 OR amount_delta <> 0)) AND "
    "(event_type <> 'CENTRAL_STOCK_ISSUE' OR (qty_delta < 0 AND amount_delta = 0)) AND "
    "(event_type <> 'CENTRAL_STOCK_RETURN' OR (qty_delta > 0 AND amount_delta = 0))"
)


def _swap(old: str, new: str, old_prefix: str, new_prefix: str, old_ix: str, new_ix: str) -> None:
    op.execute(f"DROP INDEX {old_ix}")
    op.execute("ALTER TABLE plan_ledger DROP CONSTRAINT ck_plan_ledger_known_event")
    op.execute("ALTER TABLE plan_ledger DROP CONSTRAINT ck_plan_ledger_event_signs")

    op.execute("ALTER TABLE plan_ledger DISABLE TRIGGER plan_ledger_no_update_delete")
    op.execute(
        f"UPDATE plan_ledger SET event_type = '{new}', "
        f"idempotency_key = CASE WHEN idempotency_key = '{old_prefix}' || plan_item_id "
        f"THEN '{new_prefix}' || plan_item_id ELSE idempotency_key END "
        f"WHERE event_type = '{old}'"
    )
    op.execute("ALTER TABLE plan_ledger ENABLE TRIGGER plan_ledger_no_update_delete")

    op.execute(
        "ALTER TABLE plan_ledger ADD CONSTRAINT ck_plan_ledger_known_event "
        f"CHECK (event_type IN ('{new}', {_OTHER_EVENTS}))"
    )
    op.execute(
        "ALTER TABLE plan_ledger ADD CONSTRAINT ck_plan_ledger_event_signs CHECK ("
        f"(event_type <> '{new}' OR (qty_delta >= 0 AND amount_delta >= 0)) AND "
        + _SIGNS_TAIL
        + ")"
    )
    op.execute(
        f"CREATE UNIQUE INDEX {new_ix} ON plan_ledger (plan_item_id) WHERE event_type = '{new}'"
    )


def upgrade() -> None:
    _swap(
        "PLAN_APPROVED",
        "PLAN_ACTIVATED",
        "approve:",
        "activate:",
        "uq_plan_ledger_one_approval",
        "uq_plan_ledger_one_activation",
    )


def downgrade() -> None:
    _swap(
        "PLAN_ACTIVATED",
        "PLAN_APPROVED",
        "activate:",
        "approve:",
        "uq_plan_ledger_one_activation",
        "uq_plan_ledger_one_approval",
    )
