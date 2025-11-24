from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from binyard import commodity
from binyard.bushels import bushels
from binyard.errors import TicketStateError, ValidationError
from binyard.identity import TicketNumber
from binyard.percent import percent
from binyard.shrink import ShrinkPolicy
from binyard.tickets import ScaleWeights, TareSource, TicketStatus, split_weights
from binyard.units import pounds
from conftest import (
    HARVEST_DAY,
    HARVEST_NOON,
    LOCATION,
    graded_ticket,
    sample_for,
    ticket_for,
    weights_for,
)


class TestScaleWeights:
    def test_the_net_is_gross_less_tare(self):
        assert weights_for(net="48240").net == pounds("48240")

    def test_a_tare_heavier_than_the_gross_is_refused(self):
        with pytest.raises(ValidationError) as caught:
            ScaleWeights(gross=pounds("30000"), tare=pounds("30000"))
        assert "no load" in caught.value.message

    def test_a_negative_tare_is_refused(self):
        with pytest.raises(ValidationError):
            ScaleWeights(gross=pounds("48000"), tare=pounds("-1"))

    def test_a_gross_of_zero_is_refused(self):
        with pytest.raises(ValidationError):
            ScaleWeights(gross=pounds("0"), tare=pounds("0"))

    def test_a_weighed_tare_cannot_come_after_the_gross(self):
        with pytest.raises(ValidationError):
            ScaleWeights(
                gross=pounds("78420"),
                tare=pounds("30180"),
                gross_at=HARVEST_NOON,
                tare_at=HARVEST_NOON + timedelta(minutes=5),
            )

    def test_a_weighed_tare_needs_no_review(self):
        assert not weights_for().needs_review

    def test_a_book_tare_is_always_flagged(self):
        flagged = weights_for(tare_source=TareSource.BOOK)
        assert flagged.needs_review
        assert "fleet book" in flagged.review_reasons()[0]

    def test_a_declared_tare_is_always_flagged(self):
        assert weights_for(tare_source=TareSource.DECLARED).needs_review

    def test_a_fresh_stored_tare_is_fine(self):
        fresh = weights_for(
            tare_source=TareSource.STORED,
            gross_at=HARVEST_NOON,
            tare_at=HARVEST_NOON - timedelta(hours=3),
        )
        assert not fresh.needs_review

    def test_a_stale_stored_tare_is_flagged_with_its_age(self):
        stale = weights_for(
            tare_source=TareSource.STORED,
            gross_at=HARVEST_NOON,
            tare_at=HARVEST_NOON - timedelta(hours=20),
        )
        assert stale.tare_is_stale
        assert "20.0 hours" in stale.review_reasons()[0]

    def test_a_stored_tare_with_no_times_is_treated_as_stale(self):
        assert weights_for(tare_source=TareSource.STORED).tare_is_stale

    def test_re_taring_clears_the_flag(self):
        stale = weights_for(tare_source=TareSource.STORED)
        fixed = stale.with_tare(pounds("30200"), TareSource.WEIGHED, at=HARVEST_NOON)
        assert not fixed.needs_review
        assert fixed.net == pounds("48220")


class TestSplitting:
    def test_the_parts_add_back_to_the_whole(self):
        whole = weights_for(net="48240")
        parts = split_weights(whole, [2, 1])
        assert sum(p.net.hundredths for p in parts) == whole.net.hundredths

    def test_an_uneven_split_loses_no_hundredths(self):
        whole = weights_for(net="1000.01", tare="30000")
        parts = split_weights(whole, [1, 1, 1])
        assert sum(p.net.hundredths for p in parts) == whole.net.hundredths

    def test_the_tare_source_travels_with_every_part(self):
        whole = weights_for(tare_source=TareSource.BOOK)
        assert all(p.tare_source is TareSource.BOOK for p in split_weights(whole, [1, 1]))

    def test_shares_have_to_be_positive(self):
        with pytest.raises(ValidationError):
            split_weights(weights_for(), [1, 0])
        with pytest.raises(ValidationError):
            split_weights(weights_for(), [])


class TestTicketLifecycle:
    def test_a_fresh_ticket_is_weighed_and_open(self):
        ticket = ticket_for()
        assert ticket.status is TicketStatus.WEIGHED
        assert ticket.is_open
        assert ticket.net_bushels is None

    def test_a_ticket_needs_an_account(self):
        with pytest.raises(ValidationError):
            ticket_for(account="   ")

    def test_grading_fills_in_the_bushels(self):
        ticket = graded_ticket()
        assert ticket.status is TicketStatus.GRADED
        assert ticket.grade.grade == 1
        assert ticket.gross_bushels == bushels("861.43")
        assert ticket.net_bushels == bushels("857.12")

    def test_wet_corn_nets_fewer_bushels_than_it_weighed(self):
        wet = graded_ticket(sample=sample_for(commodity.CORN, moisture=percent("19.0")))
        dry = graded_ticket()
        assert wet.gross_bushels == dry.gross_bushels
        assert wet.net_bushels < dry.net_bushels

    def test_grading_against_the_wrong_standard_is_refused(self):
        with pytest.raises(ValidationError):
            ticket_for().graded_as(
                sample_for(commodity.SOYBEANS),
                commodity.SOYBEANS,
                ShrinkPolicy.market_default(commodity.SOYBEANS),
                at=HARVEST_NOON,
                by="scalehouse",
            )

    def test_grading_with_the_wrong_shrink_policy_is_refused(self):
        with pytest.raises(ValidationError):
            ticket_for().graded_as(
                sample_for(),
                commodity.CORN,
                ShrinkPolicy.market_default(commodity.SOYBEANS),
                at=HARVEST_NOON,
                by="scalehouse",
            )

    def test_applying_before_grading_is_refused(self):
        with pytest.raises(TicketStateError):
            ticket_for().applied(at=HARVEST_NOON, by="grain desk")

    def test_the_whole_run_to_settled(self):
        ticket = graded_ticket()
        ticket = ticket.applied(at=HARVEST_NOON, by="grain desk")
        ticket = ticket.settled(at=HARVEST_NOON, by="accounting")
        assert ticket.status is TicketStatus.SETTLED
        assert not ticket.is_open
        assert [event.became for event in ticket.history] == [
            TicketStatus.GRADED,
            TicketStatus.APPLIED,
            TicketStatus.SETTLED,
        ]

    def test_a_settled_ticket_is_finished(self):
        settled = (
            graded_ticket().applied(at=HARVEST_NOON, by="d").settled(at=HARVEST_NOON, by="a")
        )
        assert not settled.can_become(TicketStatus.VOID)
        with pytest.raises(TicketStateError):
            settled.voided("changed my mind", at=HARVEST_NOON, by="a")

    def test_grading_twice_is_refused(self):
        once = graded_ticket()
        with pytest.raises(TicketStateError):
            once.graded_as(
                sample_for(),
                commodity.CORN,
                ShrinkPolicy.market_default(commodity.CORN),
                at=HARVEST_NOON,
                by="scalehouse",
            )

    def test_transitions_do_not_touch_the_original(self):
        ticket = ticket_for()
        graded_ticket()
        assert ticket.status is TicketStatus.WEIGHED
        assert ticket.history == ()


class TestVoidingAndCorrecting:
    def test_voiding_needs_a_reason(self):
        with pytest.raises(ValidationError):
            ticket_for().voided("  ", at=HARVEST_NOON, by="manager")

    def test_a_voided_ticket_keeps_its_reason(self):
        void = ticket_for().voided("wrong account", at=HARVEST_NOON, by="manager")
        assert void.status is TicketStatus.VOID
        assert void.void_reason == "wrong account"
        assert not void.is_open

    def test_correcting_voids_the_original_and_hands_back_a_replacement(self):
        original = ticket_for()
        replacement_number = TicketNumber(LOCATION, 2025, 902)
        void, fresh = original.corrected_by(
            replacement_number, "wrong bin", at=HARVEST_NOON, by="manager"
        )
        assert void.status is TicketStatus.VOID
        assert str(replacement_number) in void.void_reason
        assert fresh.number == replacement_number
        assert fresh.voids == original.number
        assert fresh.status is TicketStatus.WEIGHED
        assert fresh.weights == original.weights

    def test_a_correction_needs_a_different_number(self):
        original = ticket_for()
        with pytest.raises(ValidationError):
            original.corrected_by(original.number, "typo", at=HARVEST_NOON, by="manager")

    def test_the_replacement_carries_the_reason_in_its_history(self):
        _, fresh = ticket_for().corrected_by(
            TicketNumber(LOCATION, 2025, 902), "wrong bin", at=HARVEST_NOON, by="manager"
        )
        assert "wrong bin" in fresh.history[0].note


class TestReview:
    def test_a_ticket_inherits_the_weights_review_flag(self):
        assert ticket_for(weights=weights_for(tare_source=TareSource.BOOK)).needs_review
        assert not ticket_for().needs_review


class TestDescription:
    def test_an_ungraded_ticket_renders_with_nulls(self):
        described = ticket_for().describe()
        assert described["grade"] is None
        assert described["shrink"] is None
        assert described["weighed_on"] == HARVEST_DAY.isoformat()

    def test_a_graded_ticket_renders_the_whole_story(self):
        described = graded_ticket().describe()
        assert described["grade"]["grade"] == 1
        assert described["shrink"]["net"] == "857.12 bu"
        assert described["history"][0]["by"] == "scalehouse"


def test_a_grade_for_the_wrong_crop_cannot_be_attached():
    corn = graded_ticket()
    with pytest.raises(ValidationError):
        ticket_for(standard=commodity.SOYBEANS, grade=corn.grade)


def test_soybeans_run_the_same_road():
    ticket = graded_ticket(standard=commodity.SOYBEANS)
    assert ticket.grade.commodity_code == "SOYBEANS"
    assert ticket.gross_bushels == bushels("804.00")


def test_a_ticket_number_sorts_by_sequence():
    numbers = [TicketNumber(LOCATION, 2025, n) for n in (3, 1, 2)]
    assert [n.sequence for n in sorted(numbers)] == [1, 2, 3]


def test_grading_at_a_later_time_is_recorded():
    later = datetime(2025, 10, 14, 16, 30)
    ticket = graded_ticket(at=later)
    assert ticket.history[-1].at == later
    assert ticket.grade is not None
    assert isinstance(ticket.weights.gross.pounds, Decimal)
