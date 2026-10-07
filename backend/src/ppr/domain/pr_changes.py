"""Field-level PR change classification (spec §16, §40.7; D-10, D-21, D-22).

ONE classifier decides which differences between two versions of a HOSxP PR are
critical (control-sensitive) and which are metadata:
* before confirmation, a critical change since the draft blocks confirmation (D-21);
* after confirmation, the Wave 6 sync uses the same rules to decide whether a PPR moves
  to PR_CHANGED_REVIEW_REQUIRED (D-10).

Inputs are plain JSON-like mappings (the normalized PR contract as dumped to JSON), so
this module stays pure. Critical, per D-10 and D-21/D-22:
* PR item composition / identity (lines added, removed, item id changed);
* quantity, item amount;
* unit price only where it changes amount/control semantics (the same line's quantity
  or amount changed too);
* PR total, fund source, budget category;
* requesting department and fiscal year (identity-boundary fields, always critical);
* normalized PR status where it affects validity (a change to or from CANCELLED).
Everything else (names, unit labels, requester, dates, the native status text, a price
change that leaves quantity and amount unchanged) is non-critical.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

IDENTITY_FIELDS = ("fiscal_year", "department_id", "fund_source_id")
CRITICAL_HEADER_FIELDS = frozenset({*IDENTITY_FIELDS, "budget_category_id", "total_amount"})
CANCELLED = "CANCELLED"
_NUMERIC = frozenset({"qty", "unit_price", "amount", "total_amount"})
_IGNORED_HEADER = frozenset({"pr_no"})  # the lookup key itself


@dataclass(frozen=True)
class FieldChange:
    scope: str  # "header" or "line"
    subject: str | None  # PR item id for line changes
    field: str
    before: Any
    after: Any
    critical: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "subject": self.subject,
            "field": self.field,
            "before": self.before,
            "after": self.after,
            "critical": self.critical,
        }


def _norm(field: str, value: Any) -> Any:
    if value is None or field not in _NUMERIC:
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return value


def _differs(field: str, a: Any, b: Any) -> bool:
    return bool(_norm(field, a) != _norm(field, b))


def diff_pr(
    before_header: Mapping[str, Any],
    before_lines: Sequence[Mapping[str, Any]],
    after_header: Mapping[str, Any],
    after_lines: Sequence[Mapping[str, Any]],
) -> tuple[FieldChange, ...]:
    """Every field-level difference, each classified critical or not."""
    out: list[FieldChange] = []
    for field in sorted((set(before_header) | set(after_header)) - _IGNORED_HEADER):
        a, b = before_header.get(field), after_header.get(field)
        if not _differs(field, a, b):
            continue
        # Status matters only where it affects validity (to or from CANCELLED).
        critical = CANCELLED in (a, b) if field == "status" else field in CRITICAL_HEADER_FIELDS
        out.append(FieldChange("header", None, field, a, b, critical))

    old = {str(line["pr_item_id"]): line for line in before_lines}
    new = {str(line["pr_item_id"]): line for line in after_lines}
    for pid in sorted(old.keys() - new.keys()):
        out.append(FieldChange("line", pid, "line", "present", "removed", True))
    for pid in sorted(new.keys() - old.keys()):
        out.append(FieldChange("line", pid, "line", "absent", "added", True))
    for pid in sorted(old.keys() & new.keys()):
        a_line, b_line = old[pid], new[pid]
        changed = {
            f
            for f in (set(a_line) | set(b_line)) - {"pr_item_id"}
            if _differs(f, a_line.get(f), b_line.get(f))
        }
        control = bool(changed & {"item_id", "qty", "amount"})
        for f in sorted(changed):
            if f in ("item_id", "qty", "amount"):
                critical = True
            elif f == "unit_price":
                critical = control  # only where it changes amount/control semantics
            else:
                critical = False
            out.append(FieldChange("line", pid, f, a_line.get(f), b_line.get(f), critical))
    return tuple(out)


def critical_only(changes: Sequence[FieldChange]) -> tuple[FieldChange, ...]:
    return tuple(c for c in changes if c.critical)


def identity_changes(changes: Sequence[FieldChange]) -> tuple[FieldChange, ...]:
    """Changes to the identity-boundary fields of an existing PPR (D-22)."""
    return tuple(c for c in changes if c.scope == "header" and c.field in IDENTITY_FIELDS)
