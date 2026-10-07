"""HOSxP boundary: contracts, status mapping, mock gateway, mock auth.

Spec §21, §24, §26, §42.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import MappingProxyType

import pytest
from pydantic import ValidationError

from ppr.integration.hosxp.auth import AuthFailure, HosxpAuthenticationAdapter
from ppr.integration.hosxp.contracts import (
    BudgetCategory,
    HosxpUserProfile,
    PrHeader,
    PrItem,
    StockMovement,
    StockMovementKind,
    StockMovementSource,
    StockVerification,
)
from ppr.integration.hosxp.errors import HosxpConfigurationError, HosxpUnavailableError
from ppr.integration.hosxp.gateway import HosxpGateway
from ppr.integration.hosxp.mock import (
    MockHosxpAuthenticationAdapter,
    MockHosxpData,
    MockHosxpGateway,
)
from ppr.integration.hosxp.status import HosxpPrStatus, PrStatusMapping

D = Decimal


def _pr(pr_no: str = "MOCK-PR-1", **kw: object) -> PrHeader:
    return PrHeader(pr_id=f"id-{pr_no}", pr_no=pr_no, **kw)  # type: ignore[arg-type]


def _item(pid: str, qty: int = 1) -> PrItem:
    return PrItem(
        pr_item_id=pid, item_id="MOCK-ITEM-A", qty=D(qty), unit_price=D(10), amount=D(10 * qty)
    )


# ------------------------------------------------------------------ contracts
@pytest.mark.spec("S-24.2", "D-08")
def test_pr_header_does_not_require_unverified_fields() -> None:
    h = _pr()
    assert h.fiscal_year is None  # existence in HOSxP not assumed (D-08)
    assert h.status is HosxpPrStatus.UNKNOWN


def test_contracts_are_immutable_and_reject_unknown_fields() -> None:
    h = _pr()
    with pytest.raises(ValidationError):
        h.pr_no = "X"
    with pytest.raises(ValidationError):
        PrHeader(pr_id="1", pr_no="1", made_up_hosxp_field="x")  # type: ignore[call-arg]


@pytest.mark.spec("S-8.1")
def test_budget_category_hierarchy_has_no_fixed_depth() -> None:
    chain = [BudgetCategory(source_id="MOCK-C0", code="C0", name="root", active=True)]
    for i in range(1, 6):
        chain.append(
            BudgetCategory(
                source_id=f"MOCK-C{i}",
                code=f"C{i}",
                name=f"level {i}",
                parent_source_id=f"MOCK-C{i - 1}",
                active=True,
            )
        )
    assert chain[-1].parent_source_id == "MOCK-C4"


def _move(i: int, d: date, qty: int | str = 1, kind: str = "MOCK-ISSUE") -> StockMovement:
    return StockMovement(
        movement_id=f"MOCK-SM-{i}",
        movement_date=d,
        department_id="MOCK-DEP-WARD-A",
        item_id="MOCK-ITEM-A",
        qty=D(qty),
        native_kind=kind,
    )


@pytest.mark.spec("D-39", "S-27")
def test_stock_movement_contract_needs_positive_qty_and_native_kind() -> None:
    assert _move(1, date(2026, 10, 2), 100).qty == D(100)
    for bad in (0, -1):
        with pytest.raises(ValidationError):
            _move(1, date(2026, 10, 2), bad)
    with pytest.raises(ValidationError):
        _move(1, date(2026, 10, 2), 1, kind="")


# ------------------------------------------------------------------ status
@pytest.mark.spec("D-02", "S-24.5")
def test_without_verified_mapping_every_native_status_is_unknown() -> None:
    m = PrStatusMapping()
    for native in ("A", "1", "approved", "", None):
        assert m.normalize(native) is HosxpPrStatus.UNKNOWN


@pytest.mark.spec("D-02")
def test_mapping_is_configuration_supplied() -> None:
    m = PrStatusMapping(MappingProxyType({"MOCK-NATIVE-9": HosxpPrStatus.CANCELLED}))
    assert m.normalize("MOCK-NATIVE-9") is HosxpPrStatus.CANCELLED
    assert m.normalize("MOCK-NATIVE-X") is HosxpPrStatus.UNKNOWN


@pytest.mark.spec("D-02", "S-24.5")
def test_normalized_hosxp_statuses_are_exactly_the_spec_list() -> None:
    assert {s.value for s in HosxpPrStatus} == {
        "DRAFT",
        "ACTIVE",
        "APPROVED",
        "CANCELLED",
        "COMPLETED",
        "UNKNOWN",
    }


# ------------------------------------------------------------------ mock gateway
def _gateway() -> MockHosxpGateway:
    data = MockHosxpData()
    data.prs["MOCK-PR-1"] = _pr()
    data.pr_items["MOCK-PR-1"] = [_item("L1"), _item("L2", 3)]
    return MockHosxpGateway(data)


@pytest.mark.spec("S-42")
def test_mock_satisfies_the_gateway_port() -> None:
    assert isinstance(_gateway(), HosxpGateway)
    assert isinstance(MockHosxpAuthenticationAdapter(), HosxpAuthenticationAdapter)


@pytest.mark.spec("AT-40.1.1", "S-12")
def test_mock_returns_header_and_all_items() -> None:
    g = _gateway()
    header = g.get_pr("MOCK-PR-1")
    items = g.get_pr_items("MOCK-PR-1")
    assert header is not None and header.pr_no == "MOCK-PR-1"
    assert [i.pr_item_id for i in items] == ["L1", "L2"]
    assert g.calls == ["get_pr", "get_pr_items"]


def test_mock_unknown_pr_returns_none() -> None:
    assert _gateway().get_pr("MOCK-PR-404") is None


@pytest.mark.spec("S-26")
def test_mock_downtime_raises_instead_of_serving_stale_data() -> None:
    g = _gateway()
    g.available = False
    for call in (
        lambda: g.get_pr("MOCK-PR-1"),
        lambda: g.get_pr_items("MOCK-PR-1"),
        g.get_departments,
        g.get_items,
        g.get_fund_sources,
        g.get_budget_categories,
        lambda: g.get_budget_snapshot("F", "C"),
        lambda: g.get_stock_movements(date(2026, 10, 1), date(2026, 10, 31)),
    ):
        with pytest.raises(HosxpUnavailableError):
            call()


def test_mock_can_simulate_pr_change_in_hosxp() -> None:
    g = _gateway()
    g.replace_pr(_pr(status=HosxpPrStatus.CANCELLED), [_item("L1", 5)])
    h = g.get_pr("MOCK-PR-1")
    assert h is not None and h.status is HosxpPrStatus.CANCELLED
    assert g.get_pr_items("MOCK-PR-1")[0].qty == D(5)


VERIFIED = StockMovementSource(
    problem=None,
    verification=StockVerification("MOCK IT", date(2026, 10, 1), "MOCK-REF-1"),
    kind_map=MappingProxyType({"MOCK-ISSUE": StockMovementKind.ISSUE}),
)


@pytest.mark.spec("AT-40.12.5", "D-39")
def test_mock_stock_movements_need_a_verified_source() -> None:
    data = MockHosxpData(stock_movements=[_move(1, date(2026, 10, 1))])
    g = MockHosxpGateway(data)
    assert not g.stock_movement_source().usable  # a new site has no verified query
    with pytest.raises(HosxpConfigurationError, match="ยังไม่ได้ตั้งค่า"):
        g.get_stock_movements(date(2026, 10, 1), date(2026, 10, 31))
    data.stock_source = VERIFIED
    assert [m.movement_id for m in g.get_stock_movements(date(2026, 10, 1), date(2026, 10, 1))]


def test_mock_stock_movements_by_date_range() -> None:
    data = MockHosxpData(
        stock_movements=[_move(i, date(2026, 10, d)) for i, d in enumerate((1, 2, 3))],
        stock_source=VERIFIED,
    )
    g = MockHosxpGateway(data)
    got = g.get_stock_movements(date(2026, 10, 2), date(2026, 10, 3))
    assert [m.movement_id for m in got] == ["MOCK-SM-1", "MOCK-SM-2"]


# ------------------------------------------------------------------ mock auth
@pytest.mark.spec("S-21", "S-39")
def test_mock_auth_never_stores_plaintext_password() -> None:
    a = MockHosxpAuthenticationAdapter()
    secret = "s3cret-Password!"
    a.add_user(HosxpUserProfile(source_id="MOCK-U1", username="nurse1"), secret)
    assert all(secret.encode() not in blob for blob in a.stored_secrets())
    assert a.authenticate("nurse1", secret).ok
    assert a.authenticate("nurse1", "wrong").failure is AuthFailure.INVALID_CREDENTIALS
    assert a.authenticate("nobody", secret).failure is AuthFailure.INVALID_CREDENTIALS


def test_mock_auth_inactive_and_unavailable() -> None:
    a = MockHosxpAuthenticationAdapter()
    a.add_user(HosxpUserProfile(source_id="MOCK-U2", username="old", active=False), "pw")
    assert a.authenticate("old", "pw").failure is AuthFailure.ACCOUNT_INACTIVE
    a.available = False
    with pytest.raises(HosxpUnavailableError):
        a.authenticate("old", "pw")
    with pytest.raises(HosxpUnavailableError):
        a.get_user_profile("MOCK-U2")
