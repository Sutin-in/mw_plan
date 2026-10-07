"""The ONE canonical serializer of a live HOSxP PR (D-21, D-22, D-29).

Drafts (``ppr.draft_pr``), confirmed version snapshots (``pr`` / ``pr_items``) and PR
synchronization observations (``ppr_sync_observation.live_pr``) all store the PR in
this form, so every comparison is made between like and like by
``domain.pr_changes``. It is the normalized contract only: no connection data, no raw
rows, nothing that is not part of the PR contract.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ppr.integration.hosxp.contracts import PrHeader, PrItem


def header_json(header: PrHeader) -> dict[str, Any]:
    return header.model_dump(mode="json")


def items_json(items: Sequence[PrItem]) -> list[dict[str, Any]]:
    return [i.model_dump(mode="json") for i in items]


def pr_snapshot(header: PrHeader, items: Sequence[PrItem]) -> dict[str, Any]:
    """``{"header": ..., "items": [...]}`` - the PR exactly as retrieved."""
    return {"header": header_json(header), "items": items_json(items)}
