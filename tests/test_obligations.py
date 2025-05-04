from datetime import date

import pytest

from binyard.bushels import bushels
from binyard.errors import ObligationError, ValidationError
from binyard.money import Money, Price, dollars
from binyard.obligations import (
    Accrual,
    ObligationKind,
    ObligationStatus,
    Release,
    StorageObligation,
    StorageTerms,
    accrue,
)

OPENED = date(2025, 10, 15)


def obligation(kind: ObligationKind = ObligationKind.OPEN_STORAGE, **overrides):
    fields: dict[str, object] = {
        "identifier": "OS-1",
        "account": "HALVORSEN FARMS",
        "commodity_code": "CORN",
        "kind": kind,
        "quantity": bushels("10000"),
        "opened_on": OPENED,
    }
    fields.update(overrides)
    return StorageObligation(**fields)  # type: ignore[arg-type]


class TestKinds:
    def test_the_house_owns_what_it_bought_and_what_title_passed_on(self):
        assert ObligationKind.COMPANY_OWNED.house_owns
        assert ObligationKind.DEFERRED_PRICE.house_owns
        assert ObligationKind.PRICE_LATER.house_owns

    def test_the_house_does_not_own_stored_grain(self):
        assert not ObligationKind.OPEN_STORAGE.house_owns
        assert not ObligationKind.WAREHOUSE_RECEIPT.house_owns

    def test_only_stored_grain_accrues_storage(self):
        accruing = [k for k in ObligationKind if k.accrues_storage]
        assert set(accruing) == {
            ObligationKind.OPEN_STORAGE,
            ObligationKind.WAREHOUSE_RECEIPT,
        }

    def test_only_a_receipt_is_indivisible(self):
        indivisible = [k for k in ObligationKind if not k.divisible]
        assert indivisible == [ObligationKind.WAREHOUSE_RECEIPT]


class TestValidation:
    def test_an_obligation_needs_a_positive_quantity(self):
        with pytest.raises(ValidationError):
            obligation(quantity=bushels("0"))

    def test_an_obligation_needs_an_account(self):
        with pytest.raises(ValidationError):
            obligation(account=" ")

    def test_a_rate_on_a_kind_that_does_not_store_is_refused(self):
        with pytest.raises(ValidationError) as caught:
            obligation(ObligationKind.COMPANY_OWNED, storage_rate=Price.from_cents("4"))
        assert caught.value.detail["kind"] == "company_owned"

    def test_negative_free_days_are_refused(self):
        with pytest.raises(ValidationError):
            obligation(free_days=-1)

    def test_more_released_than_held_is_refused_at_construction(self):
        with pytest.raises(ObligationError):
            obligation(
                releases=(Release(OPENED, bushels("20000"), "LO-1"),),
            )

    def test_a_release_needs_a_reference_and_a_quantity(self):
        with pytest.raises(ValidationError):
            Release(OPENED, bushels("100"), "  ")
        with pytest.raises(ValidationError):
            Release(OPENED, bushels("0"), "LO-1")


class TestReleasing:
    def test_the_balance_falls_as_grain_goes_out(self):
        held = obligation().release(Release(date(2025, 12, 1), bushels("4000"), "LO-1"))
        assert held.balance == bushels("6000")
        assert held.released == bushels("4000")
        assert held.is_open

    def test_releasing_the_last_bushel_closes_it(self):
        held = obligation().release(Release(date(2025, 12, 1), bushels("10000"), "LO-1"))
        assert held.status is ObligationStatus.CLOSED
        assert not held.is_open

    def test_a_closed_obligation_refuses_another_release(self):
        held = obligation().release(Release(date(2025, 12, 1), bushels("10000"), "LO-1"))
        with pytest.raises(ObligationError):
            held.release(Release(date(2025, 12, 2), bushels("1"), "LO-2"))

    def test_releasing_more_than_the_balance_is_refused(self):
        with pytest.raises(ObligationError) as caught:
            obligation().release(Release(date(2025, 12, 1), bushels("10001"), "LO-1"))
        assert caught.value.detail["balance"] == "10000.00 bu"

    def test_releasing_before_it_opened_is_refused(self):
        with pytest.raises(ObligationError):
            obligation().release(Release(date(2025, 10, 1), bushels("100"), "LO-1"))

    def test_a_warehouse_receipt_cannot_be_part_released(self):
        receipt = obligation(ObligationKind.WAREHOUSE_RECEIPT, identifier="WR-1")
        with pytest.raises(ObligationError) as caught:
            receipt.release(Release(date(2025, 12, 1), bushels("4000"), "LO-1"))
        assert "cancel and reissue" in caught.value.message

    def test_a_warehouse_receipt_can_be_released_whole(self):
        receipt = obligation(ObligationKind.WAREHOUSE_RECEIPT, identifier="WR-1")
        closed = receipt.release(Release(date(2025, 12, 1), bushels("10000"), "LO-1"))
        assert closed.status is ObligationStatus.CLOSED

    def test_cancelling_a_receipt_can_name_its_replacement(self):
        receipt = obligation(ObligationKind.WAREHOUSE_RECEIPT, identifier="WR-1")
        cancelled = receipt.cancelled(superseded_by="WR-2")
        assert cancelled.status is ObligationStatus.CANCELLED
        assert cancelled.superseded_by == "WR-2"

    def test_a_cancelled_obligation_cannot_be_cancelled_again(self):
        cancelled = obligation().cancelled()
        with pytest.raises(ObligationError):
            cancelled.cancelled()


class TestPricing:
    def test_a_deferred_price_contract_starts_unpriced(self):
        contract = obligation(ObligationKind.DEFERRED_PRICE, identifier="DP-1")
        assert contract.needs_pricing
        assert not contract.is_priced

    def test_pricing_records_the_price_and_the_day(self):
        contract = obligation(ObligationKind.DEFERRED_PRICE, identifier="DP-1")
        priced = contract.priced(Price.of("4.5225"), date(2026, 2, 10))
        assert priced.is_priced
        assert not priced.needs_pricing
        assert priced.value() == dollars("45225.00")

    def test_an_unpriced_contract_has_no_value_yet(self):
        with pytest.raises(ObligationError):
            obligation(ObligationKind.DEFERRED_PRICE, identifier="DP-1").value()

    def test_open_storage_does_not_take_a_later_price(self):
        with pytest.raises(ObligationError):
            obligation().priced(Price.of("4.50"), date(2026, 2, 10))

    def test_pricing_before_the_contract_opened_is_refused(self):
        contract = obligation(ObligationKind.DEFERRED_PRICE, identifier="DP-1")
        with pytest.raises(ObligationError):
            contract.priced(Price.of("4.50"), date(2025, 1, 1))


class TestStorageAccrual:
    def _terms(self, **overrides):
        fields: dict[str, object] = {
            "rate_per_bushel_month": Price.from_cents("4"),
            "free_days": 30,
        }
        fields.update(overrides)
        return StorageTerms(**fields)  # type: ignore[arg-type]

    def test_nothing_accrues_inside_the_free_period(self):
        found = accrue(obligation(), self._terms(), date(2025, 11, 1))
        assert found.total == Money.zero()
        assert found.segments == ()

    def test_company_owned_grain_never_accrues(self):
        owned = obligation(ObligationKind.COMPANY_OWNED, identifier="CO-1")
        assert accrue(owned, self._terms(), date(2026, 6, 1)).total == Money.zero()

    def test_accruing_before_it_opened_is_refused(self):
        with pytest.raises(ValidationError):
            accrue(obligation(), self._terms(), date(2025, 10, 1))

    def test_a_month_started_is_a_month_charged(self):
        found = accrue(obligation(), self._terms(), date(2025, 12, 1))
        assert found.total == dollars("400.00")

    def test_per_day_prorates_the_same_stretch(self):
        terms = self._terms(accrual=Accrual.PER_DAY)
        found = accrue(obligation(), terms, date(2025, 12, 1))
        assert found.total < dollars("400.00")
        assert found.total > dollars("200.00")

    def test_a_release_splits_the_accrual_into_segments(self):
        held = obligation().release(Release(date(2026, 1, 15), bushels("4000"), "LO-1"))
        found = accrue(held, self._terms(), date(2026, 3, 1))
        assert len(found.segments) == 2
        assert found.segments[0].balance == bushels("10000")
        assert found.segments[1].balance == bushels("6000")

    def test_the_segments_add_up_to_the_total(self):
        held = obligation().release(Release(date(2026, 1, 15), bushels("4000"), "LO-1"))
        found = accrue(held, self._terms(), date(2026, 3, 1))
        total = Money.zero()
        for segment in found.segments:
            total = total + segment.charge
        assert total == found.total

    def test_a_release_outside_the_window_does_not_split_anything(self):
        held = obligation().release(Release(date(2026, 6, 1), bushels("4000"), "LO-1"))
        found = accrue(held, self._terms(), date(2026, 3, 1))
        assert len(found.segments) == 1

    def test_the_minimum_charge_lifts_a_tiny_accrual(self):
        small = obligation(quantity=bushels("10"))
        terms = self._terms(minimum_charge=dollars("25.00"))
        found = accrue(small, terms, date(2025, 12, 1))
        assert found.total == dollars("25.00")
        assert found.minimum_applied

    def test_the_minimum_does_not_apply_when_nothing_accrued(self):
        terms = self._terms(minimum_charge=dollars("25.00"))
        found = accrue(obligation(), terms, date(2025, 11, 1))
        assert found.total == Money.zero()
        assert not found.minimum_applied

    def test_the_obligations_own_rate_beats_the_posted_one(self):
        theirs = obligation(storage_rate=Price.from_cents("2"))
        found = accrue(theirs, self._terms(), date(2025, 12, 1))
        assert found.total == dollars("200.00")

    def test_the_obligations_own_free_days_beat_the_posted_ones(self):
        theirs = obligation(free_days=90)
        assert accrue(theirs, self._terms(), date(2025, 12, 1)).total == Money.zero()

    def test_negative_terms_are_refused(self):
        with pytest.raises(ValidationError):
            StorageTerms(rate_per_bushel_month=Price(-1))
        with pytest.raises(ValidationError):
            StorageTerms(rate_per_bushel_month=Price.zero(), free_days=-1)
        with pytest.raises(ValidationError):
            StorageTerms(rate_per_bushel_month=Price.zero(), minimum_charge=Money(-1))

    def test_the_assessment_renders_every_segment(self):
        held = obligation().release(Release(date(2026, 1, 15), bushels("4000"), "LO-1"))
        described = accrue(held, self._terms(), date(2026, 3, 1)).describe()
        assert described["obligation"] == "OS-1"
        assert len(described["segments"]) == 2
        assert described["segments"][1]["balance"] == "6000.00 bu"


def test_an_obligation_renders_for_the_examiner():
    described = obligation().describe()
    assert described["house_owns"] is False
    assert described["balance"] == "10000.00 bu"
    assert described["storage_starts_on"] == "2025-10-15"
