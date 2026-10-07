"""HOSxP PR status normalization (spec §24.5, D-02).

``HosxpPrStatus`` is the EXTERNAL, synchronized status. It is a separate field from
the internal ``PprState`` and must never be merged with it.

The native-code -> normalized mapping is TBD (spec §47). The mapping is therefore
configuration supplied at runtime; with no verified mapping every native value
normalizes to ``UNKNOWN``. No HOSxP native codes are defined in this codebase.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType


class HosxpPrStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    APPROVED = "APPROVED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PrStatusMapping:
    """Verified native-status -> normalized-status mapping. Empty until evidence exists."""

    native_to_normalized: Mapping[str, HosxpPrStatus] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def normalize(self, native: str | None) -> HosxpPrStatus:
        if native is None:
            return HosxpPrStatus.UNKNOWN
        return self.native_to_normalized.get(native, HosxpPrStatus.UNKNOWN)
