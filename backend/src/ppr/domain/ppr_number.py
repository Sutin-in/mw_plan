"""PPR numbering (spec §7, D-06).

Format ``PPR-<fiscal-year>-<6-digit sequence>``, e.g. ``PPR-2570-000001``.
Numbers are unique, system-generated and fiscal-year scoped; the sequence resets
each fiscal year. Gapless numbering is NOT required (D-06).

``FiscalYearSequencer`` is an in-memory reference allocator used by domain tests;
the persistent allocator (Wave 4) must satisfy the same contract.
"""

from __future__ import annotations

import re

MAX_SEQUENCE = 999_999
_PATTERN = re.compile(r"^PPR-(\d{4})-(\d{6})$")


def format_ppr_number(fiscal_year: int, sequence: int) -> str:
    if not 1000 <= fiscal_year <= 9999:
        raise ValueError(f"fiscal year must be 4 digits, got {fiscal_year}")
    if not 1 <= sequence <= MAX_SEQUENCE:
        raise ValueError(f"sequence must be 1..{MAX_SEQUENCE}, got {sequence}")
    return f"PPR-{fiscal_year}-{sequence:06d}"


def parse_ppr_number(value: str) -> tuple[int, int]:
    m = _PATTERN.match(value)
    if not m:
        raise ValueError(f"not a PPR number: {value!r}")
    fiscal_year, sequence = int(m.group(1)), int(m.group(2))
    if sequence == 0:
        raise ValueError(f"sequence 000000 is not valid: {value!r}")
    return fiscal_year, sequence


class FiscalYearSequencer:
    """Reference allocator: independent, monotonically increasing sequence per FY."""

    def __init__(self) -> None:
        self._last: dict[int, int] = {}

    def next_number(self, fiscal_year: int) -> str:
        nxt = self._last.get(fiscal_year, 0) + 1
        number = format_ppr_number(fiscal_year, nxt)  # validates before committing
        self._last[fiscal_year] = nxt
        return number
