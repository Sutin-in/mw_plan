"""Wave 10B-1: stock issues and returns can never be posted to the Plan Ledger (D-39).

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-02

D-39 (product owner, 2026-10-02): stock issues and returns are read-only HOSxP data shown in
the *Central Plan Usage* report, never Plan Ledger transactions; they change neither plan
quantity nor plan amount. The application refuses ``CENTRAL_STOCK_ISSUE`` /
``CENTRAL_STOCK_RETURN`` (``domain/ledger.py``); this revision makes the database refuse
them too:

* ``ck_plan_ledger_known_event`` no longer lists the two event types;
* ``ck_plan_ledger_event_signs`` drops their sign rules (D-01, superseded);
* ``ck_plan_ledger_no_stock_movement`` names the rule explicitly.

No row is changed or deleted. The two event types were never posted (D-38 C-3 kept them
unused); should a database hold one anyway, the upgrade stops with a message instead of
touching the append-only ledger, so the case is investigated rather than erased.
Migrations 0001-0009 are unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STOCK = "'CENTRAL_STOCK_ISSUE', 'CENTRAL_STOCK_RETURN'"
_EVENTS = "'PLAN_ACTIVATED', 'PPR_CONFIRMED', 'PPR_RELEASED', 'PLAN_AMENDMENT'"
_SIGNS = (
    "(event_type <> 'PLAN_ACTIVATED' OR (qty_delta >= 0 AND amount_delta >= 0)) AND "
    "(event_type <> 'PPR_CONFIRMED' OR (qty_delta < 0 AND amount_delta <= 0)) AND "
    "(event_type <> 'PPR_RELEASED' OR (qty_delta >= 0 AND amount_delta >= 0 "
    "AND (qty_delta > 0 OR amount_delta > 0))) AND "
    "(event_type <> 'PLAN_AMENDMENT' OR (qty_delta <> 0 OR amount_delta <> 0))"
)
_STOCK_SIGNS = (
    " AND (event_type <> 'CENTRAL_STOCK_ISSUE' OR (qty_delta < 0 AND amount_delta = 0)) AND "
    "(event_type <> 'CENTRAL_STOCK_RETURN' OR (qty_delta > 0 AND amount_delta = 0))"
)


def upgrade() -> None:
    found = op.get_bind().execute(
        sa.text(f"SELECT count(*) FROM plan_ledger WHERE event_type IN ({_STOCK})")
    ).scalar_one()
    if found:
        raise RuntimeError(
            f"plan_ledger holds {found} stock issue/return entr(y/ies); D-39 forbids them. "
            "Nothing was changed: investigate these rows before upgrading (they are never "
            "deleted by a migration)."
        )
    op.execute("ALTER TABLE plan_ledger DROP CONSTRAINT ck_plan_ledger_known_event")
    op.execute("ALTER TABLE plan_ledger DROP CONSTRAINT ck_plan_ledger_event_signs")
    op.execute(
        "ALTER TABLE plan_ledger ADD CONSTRAINT ck_plan_ledger_known_event "
        f"CHECK (event_type IN ({_EVENTS}))"
    )
    op.execute(f"ALTER TABLE plan_ledger ADD CONSTRAINT ck_plan_ledger_event_signs CHECK ({_SIGNS})")
    op.execute(
        "ALTER TABLE plan_ledger ADD CONSTRAINT ck_plan_ledger_no_stock_movement "
        f"CHECK (event_type NOT IN ({_STOCK}))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE plan_ledger DROP CONSTRAINT ck_plan_ledger_no_stock_movement")
    op.execute("ALTER TABLE plan_ledger DROP CONSTRAINT ck_plan_ledger_known_event")
    op.execute("ALTER TABLE plan_ledger DROP CONSTRAINT ck_plan_ledger_event_signs")
    op.execute(
        "ALTER TABLE plan_ledger ADD CONSTRAINT ck_plan_ledger_known_event "
        f"CHECK (event_type IN ({_EVENTS}, {_STOCK}))"
    )
    op.execute(
        "ALTER TABLE plan_ledger ADD CONSTRAINT ck_plan_ledger_event_signs "
        f"CHECK ({_SIGNS}{_STOCK_SIGNS})"
    )
