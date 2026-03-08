from datetime import date

import pytest

from binyard.analysis import build_exposure, check_books_balance
from binyard.bushels import bushels
from binyard.contracts import PriceKind, PurchaseContract
from binyard.contracts.contract import Delivery
from binyard.errors import ValidationError
from binyard.money import Price, dollars
from binyard.obligations import ObligationKind, Release, StorageObligation

OPENED = date(2025, 10, 15)
TODAY = date(2025, 12, 1)


def obligation(identifier, kind, quantity, **overrides):
    fields: dict[str, object] = {
        "identifier": identifier,
        "account": "HALVORSEN FARMS",
        "commodity_code": "CORN",
        "kind": kind,
        "quantity": bushels(quantity),
        "opened_on": OPENED,
    }
    fields.update(overrides)
    return StorageObligation(**fields)  # type: ignore[arg-type]


def basis_contract(identifier="C-1", delivered="4000", fixed=None):
    contract = PurchaseContract(
        identifier=identifier,
        account="HALVORSEN FARMS",
        commodity_code="CORN",
        quantity=bushels("5000"),
        delivery_from=date(2025, 10, 1),
        delivery_to=date(2025, 12, 31),
        price_kind=PriceKind.BASIS,
        basis=Price.of("-0.30"),
        futures_month="CZ25",
    ).deliver(Delivery(OPENED, "T-1", bushels(delivered)))
    return contract if fixed is None else contract.fix_futures(Price.of(fixed))


class TestBooks:
    def test_stored_grain_is_not_the_houses(self):
        report = build_exposure(
            on=TODAY,
            on_hand={"CORN": bushels("10000")},
            obligations=[obligation("OS-1", ObligationKind.OPEN_STORAGE, "10000")],
            purchases=[],
        )
        corn = report.for_commodity("CORN")
        assert corn.customer_owned == bushels("10000")
        assert corn.owned == bushels("0")
        assert corn.flat_price_position == bushels("0")

    def test_bought_grain_is_a_long_position(self):
        report = build_exposure(
            on=TODAY,
            on_hand={"CORN": bushels("10000")},
            obligations=[obligation("CO-1", ObligationKind.COMPANY_OWNED, "10000")],
            purchases=[],
        )
        corn = report.for_commodity("CORN")
        assert corn.owned == bushels("10000")
        assert corn.is_long
        assert not corn.is_short

    def test_deferred_price_grain_is_owned_but_unpriced(self):
        report = build_exposure(
            on=TODAY,
            on_hand={"CORN": bushels("10000")},
            obligations=[obligation("DP-1", ObligationKind.DEFERRED_PRICE, "10000")],
            purchases=[],
        )
        corn = report.for_commodity("CORN")
        assert corn.owned_unpriced == bushels("10000")
        assert corn.owned_priced == bushels("0")

    def test_pricing_a_deferred_contract_moves_it_across(self):
        priced = obligation("DP-1", ObligationKind.DEFERRED_PRICE, "10000").priced(
            Price.of("4.5225"), date(2025, 11, 1)
        )
        report = build_exposure(
            on=TODAY, on_hand={"CORN": bushels("10000")}, obligations=[priced], purchases=[]
        )
        corn = report.for_commodity("CORN")
        assert corn.owned_priced == bushels("10000")
        assert corn.owned_unpriced == bushels("0")
        assert corn.average_cost == Price.of("4.5225")

    def test_a_cancelled_obligation_is_in_no_book(self):
        report = build_exposure(
            on=TODAY,
            on_hand={"CORN": bushels("0")},
            obligations=[obligation("OS-1", ObligationKind.OPEN_STORAGE, "10000").cancelled()],
            purchases=[],
        )
        corn = report.for_commodity("CORN")
        assert corn.customer_owned == bushels("0")

    def test_an_obligation_opened_later_is_not_in_a_book_yet(self):
        later = obligation(
            "OS-1", ObligationKind.OPEN_STORAGE, "10000", opened_on=date(2026, 1, 1)
        )
        report = build_exposure(
            on=TODAY, on_hand={"CORN": bushels("0")}, obligations=[later], purchases=[]
        )
        assert report.for_commodity("CORN").customer_owned == bushels("0")

    def test_releases_before_the_date_reduce_the_book(self):
        drawn = obligation("OS-1", ObligationKind.OPEN_STORAGE, "10000").release(
            Release(date(2025, 11, 15), bushels("4000"), "LO-1")
        )
        report = build_exposure(
            on=TODAY, on_hand={"CORN": bushels("6000")}, obligations=[drawn], purchases=[]
        )
        assert report.for_commodity("CORN").customer_owned == bushels("6000")


class TestPosition:
    def test_selling_more_than_is_owned_is_a_short(self):
        report = build_exposure(
            on=TODAY,
            on_hand={"CORN": bushels("10000")},
            obligations=[obligation("CO-1", ObligationKind.COMPANY_OWNED, "10000")],
            purchases=[],
            sales={"CORN": bushels("15000")},
        )
        corn = report.for_commodity("CORN")
        assert corn.is_short
        assert corn.flat_price_position == bushels("-5000")
        assert corn.unhedged == bushels("5000")
        assert report.shorts == (corn,)

    def test_a_matched_book_is_flat(self):
        report = build_exposure(
            on=TODAY,
            on_hand={"CORN": bushels("10000")},
            obligations=[obligation("CO-1", ObligationKind.COMPANY_OWNED, "10000")],
            purchases=[],
            sales={"CORN": bushels("10000")},
        )
        corn = report.for_commodity("CORN")
        assert not corn.is_long
        assert not corn.is_short
        assert corn.unhedged == bushels("0")

    def test_the_position_marks_to_a_board_price(self):
        report = build_exposure(
            on=TODAY,
            on_hand={"CORN": bushels("10000")},
            obligations=[obligation("CO-1", ObligationKind.COMPANY_OWNED, "10000")],
            purchases=[],
        )
        assert report.for_commodity("CORN").value_at(Price.of("4.50")) == dollars("45000.00")

    def test_a_short_marks_to_a_negative_value(self):
        report = build_exposure(
            on=TODAY,
            on_hand={"CORN": bushels("0")},
            obligations=[],
            purchases=[],
            sales={"CORN": bushels("5000")},
        )
        assert report.for_commodity("CORN").value_at(Price.of("4.50")) == dollars("-22500.00")


class TestBasis:
    def test_an_unfixed_basis_contract_shows_as_basis_exposure(self):
        report = build_exposure(
            on=TODAY,
            on_hand={"CORN": bushels("4000")},
            obligations=[],
            purchases=[basis_contract()],
        )
        assert report.for_commodity("CORN").basis_position == bushels("4000")

    def test_fixing_the_futures_leg_clears_it(self):
        report = build_exposure(
            on=TODAY,
            on_hand={"CORN": bushels("4000")},
            obligations=[],
            purchases=[basis_contract(fixed="4.70")],
        )
        assert report.for_commodity("CORN").basis_position == bushels("0")

    def test_a_flat_contract_is_never_basis_exposure(self):
        flat = PurchaseContract(
            identifier="C-FLAT",
            account="HALVORSEN FARMS",
            commodity_code="CORN",
            quantity=bushels("5000"),
            delivery_from=date(2025, 10, 1),
            delivery_to=date(2025, 12, 31),
            price_kind=PriceKind.FLAT,
            flat_price=Price.of("4.52"),
        ).deliver(Delivery(OPENED, "T-1", bushels("4000")))
        report = build_exposure(
            on=TODAY, on_hand={"CORN": bushels("4000")}, obligations=[], purchases=[flat]
        )
        assert report.for_commodity("CORN").basis_position == bushels("0")


class TestBooksBalance:
    def test_a_consistent_yard_balances(self):
        report = build_exposure(
            on=TODAY,
            on_hand={"CORN": bushels("10000")},
            obligations=[
                obligation("OS-1", ObligationKind.OPEN_STORAGE, "6000"),
                obligation("CO-1", ObligationKind.COMPANY_OWNED, "4000"),
            ],
            purchases=[],
        )
        check_books_balance(report.for_commodity("CORN"))

    def test_a_bushel_missing_from_the_books_is_caught(self):
        report = build_exposure(
            on=TODAY,
            on_hand={"CORN": bushels("10000")},
            obligations=[obligation("OS-1", ObligationKind.OPEN_STORAGE, "6000")],
            purchases=[],
        )
        with pytest.raises(ValidationError) as caught:
            check_books_balance(report.for_commodity("CORN"))
        assert caught.value.detail["booked"] == "6000.00 bu"


def test_the_report_renders_and_names_the_shorts():
    report = build_exposure(
        on=TODAY,
        on_hand={"CORN": bushels("1000"), "SOYBEANS": bushels("0")},
        obligations=[obligation("CO-1", ObligationKind.COMPANY_OWNED, "1000")],
        purchases=[],
        sales={"SOYBEANS": bushels("2000")},
    )
    described = report.describe()
    assert described["short_in"] == ["SOYBEANS"]
    assert len(described["exposures"]) == 2
