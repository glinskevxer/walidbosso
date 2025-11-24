from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from binyard import commodity
from binyard.bushels import bushels
from binyard.errors import (
    BinCapacityExceeded,
    CommodityMismatch,
    InsufficientInventory,
    NotFound,
    ValidationError,
)
from binyard.grading.factors import FactorSample, Odor
from binyard.inventory import (
    BinContents,
    BinKind,
    Ledger,
    Movement,
    MovementKind,
    Yard,
    commingle,
)
from binyard.percent import percent
from conftest import HARVEST_NOON, bin_for, sample_for, yard_with

MORNING = datetime(2025, 10, 14, 8, 0)
NOON = HARVEST_NOON
EVENING = datetime(2025, 10, 14, 18, 0)


def wet(moisture: str = "20.0", test_weight: str = "53.0") -> FactorSample:
    return FactorSample(moisture=percent(moisture), test_weight=Decimal(test_weight))


def dry(moisture: str = "14.0", test_weight: str = "57.0") -> FactorSample:
    return FactorSample(moisture=percent(moisture), test_weight=Decimal(test_weight))


class TestBinDefinition:
    def test_a_bin_needs_a_positive_capacity(self):
        with pytest.raises(ValidationError):
            bin_for(capacity="0")

    def test_a_bin_needs_an_identifier(self):
        with pytest.raises(ValidationError):
            bin_for(identifier="  ")

    def test_only_a_pile_carries_an_allowance(self):
        with pytest.raises(ValidationError):
            bin_for(pile_allowance=percent("0.5"))
        pile = bin_for(kind=BinKind.PILE, pile_allowance=percent("0.5"))
        assert pile.kind.is_temporary

    def test_a_bin_restricted_to_one_crop_refuses_the_rest(self):
        restricted = bin_for(allowed_commodities=frozenset({"SOYBEANS"}))
        assert restricted.accepts("SOYBEANS")
        assert not restricted.accepts("CORN")

    def test_an_unrestricted_bin_accepts_anything(self):
        assert bin_for().accepts("CORN")
        assert bin_for().accepts("OATS")


class TestContents:
    def test_an_empty_bin_has_no_quality(self):
        empty = BinContents.empty()
        assert empty.is_empty
        assert empty.quality is None
        assert empty.commodity_code is None

    def test_an_empty_bin_cannot_carry_a_quality(self):
        with pytest.raises(ValidationError):
            BinContents(commodity_code="CORN", quantity=bushels(0), quality=dry())

    def test_grain_in_a_bin_needs_a_crop_and_a_quality(self):
        with pytest.raises(ValidationError):
            BinContents(quantity=bushels("100"))

    def test_the_first_load_sets_the_quality(self):
        structure = bin_for()
        held = BinContents.empty().add(structure, "CORN", bushels("1000"), dry())
        assert held.quality == dry()
        assert held.quantity == bushels("1000")

    def test_a_second_load_averages_by_bushels(self):
        structure = bin_for()
        held = BinContents.empty().add(structure, "CORN", bushels("9000"), dry())
        held = held.add(structure, "CORN", bushels("1000"), wet())
        assert held.quality.moisture == percent("14.6")
        assert held.quality.test_weight == Decimal("56.6")

    def test_overfilling_is_refused_and_says_by_how_much(self):
        structure = bin_for(capacity="1000")
        held = BinContents.empty().add(structure, "CORN", bushels("900"), dry())
        with pytest.raises(BinCapacityExceeded) as caught:
            held.add(structure, "CORN", bushels("200"), dry())
        assert caught.value.detail["capacity"] == "1000.00 bu"

    def test_filling_to_the_brim_is_allowed(self):
        structure = bin_for(capacity="1000")
        held = BinContents.empty().add(structure, "CORN", bushels("1000"), dry())
        assert held.quantity == structure.capacity

    def test_a_second_crop_in_the_same_bin_is_refused(self):
        structure = bin_for()
        held = BinContents.empty().add(structure, "CORN", bushels("1000"), dry())
        with pytest.raises(CommodityMismatch):
            held.add(structure, "SOYBEANS", bushels("100"), dry())

    def test_a_crop_the_bin_is_not_set_up_for_is_refused(self):
        structure = bin_for(allowed_commodities=frozenset({"SOYBEANS"}))
        with pytest.raises(CommodityMismatch):
            BinContents.empty().add(structure, "CORN", bushels("100"), dry())

    def test_drawing_leaves_the_quality_alone(self):
        structure = bin_for()
        held = BinContents.empty().add(structure, "CORN", bushels("1000"), dry())
        remaining, drawn = held.draw(bushels("400"))
        assert remaining.quantity == bushels("600")
        assert remaining.quality == drawn == dry()

    def test_drawing_the_last_bushel_empties_the_bin(self):
        structure = bin_for()
        held = BinContents.empty().add(structure, "CORN", bushels("1000"), dry())
        remaining, _ = held.draw(bushels("1000"))
        assert remaining.is_empty
        assert remaining.commodity_code is None

    def test_drawing_more_than_is_there_is_refused(self):
        structure = bin_for()
        held = BinContents.empty().add(structure, "CORN", bushels("1000"), dry())
        with pytest.raises(InsufficientInventory):
            held.draw(bushels("1001"))

    def test_adding_nothing_is_refused(self):
        with pytest.raises(ValidationError):
            BinContents.empty().add(bin_for(), "CORN", bushels("0"), dry())


class TestCommingling:
    def test_the_average_sits_between_the_two(self):
        blended = commingle((dry("14.0"), 9000), (wet("20.0"), 1000))
        assert percent("14.0") < blended.moisture < percent("20.0")

    def test_infestation_spreads(self):
        clean = dry()
        infested = FactorSample(
            moisture=percent("14.0"), test_weight=Decimal("57.0"), infested=True
        )
        assert commingle((clean, 100000), (infested, 1)).infested

    def test_odor_takes_the_worse_of_the_two(self):
        sour = FactorSample(
            moisture=percent("14.0"), test_weight=Decimal("57.0"), odor=Odor.SOUR
        )
        musty = FactorSample(
            moisture=percent("14.0"), test_weight=Decimal("57.0"), odor=Odor.MUSTY
        )
        assert commingle((musty, 5000), (sour, 5000)).odor is Odor.SOUR
        assert commingle((dry(), 5000), (musty, 5000)).odor is Odor.MUSTY

    def test_heat_damage_stays_inside_total_damage(self):
        one = FactorSample(
            moisture=percent("14"),
            test_weight=Decimal("57"),
            total_damage=percent("5.0"),
            heat_damage=percent("0.5"),
        )
        two = FactorSample(
            moisture=percent("14"),
            test_weight=Decimal("57"),
            total_damage=percent("1.0"),
            heat_damage=percent("1.0"),
        )
        blended = commingle((one, 3000), (two, 7000))
        assert blended.heat_damage <= blended.total_damage

    def test_commingling_nothing_is_refused(self):
        with pytest.raises(ValidationError):
            commingle((dry(), 0), (wet(), 0))


class TestLedger:
    def _receipt(self, at, quantity="1000", identifier="N1", reference="WBS-25-000001"):
        return Movement(
            at=at,
            bin_identifier=identifier,
            kind=MovementKind.RECEIPT,
            commodity_code="CORN",
            quantity=bushels(quantity),
            reference=reference,
            by="scalehouse",
            quality=dry(),
        )

    def test_a_movement_of_nothing_is_refused(self):
        with pytest.raises(ValidationError):
            self._receipt(NOON, quantity="0")

    def test_grain_going_in_needs_a_quality(self):
        with pytest.raises(ValidationError):
            Movement(
                at=NOON,
                bin_identifier="N1",
                kind=MovementKind.RECEIPT,
                commodity_code="CORN",
                quantity=bushels("100"),
                reference="x",
                by="me",
            )

    def test_entries_are_kept_in_time_order_however_they_arrive(self):
        ledger = Ledger().post(self._receipt(EVENING)).post(self._receipt(MORNING))
        assert [entry.at for entry in ledger] == [MORNING, EVENING]

    def test_reading_history_cuts_at_a_moment(self):
        ledger = Ledger().post_all([self._receipt(MORNING), self._receipt(EVENING)])
        assert len(ledger.up_to(NOON)) == 1
        assert len(ledger.up_to(EVENING)) == 2

    def test_the_balance_nets_ins_and_outs(self):
        ledger = Ledger().post(self._receipt(MORNING, quantity="1000"))
        ledger = ledger.post(
            Movement(
                at=NOON,
                bin_identifier="N1",
                kind=MovementKind.SHIPMENT,
                commodity_code="CORN",
                quantity=bushels("-400"),
                reference="LO-1",
                by="loadout",
            )
        )
        assert ledger.balance("N1") == bushels("600")

    def test_a_reversal_cancels_the_original(self):
        original = self._receipt(MORNING)
        ledger = Ledger().post(original).post(original.reversal(NOON, "manager", "void"))
        assert ledger.balance("N1") == bushels("0")
        assert "reverses" in ledger.entries[-1].note

    def test_movements_can_be_found_by_reference(self):
        ledger = Ledger().post_all(
            [self._receipt(MORNING, reference="A"), self._receipt(NOON, reference="B")]
        )
        assert len(ledger.for_reference("A")) == 1

    def test_the_ledger_lists_the_bins_it_touched(self):
        ledger = Ledger().post_all(
            [self._receipt(MORNING, identifier="N1"), self._receipt(NOON, identifier="N2")]
        )
        assert ledger.bins() == ("N1", "N2")


class TestYard:
    def test_two_bins_cannot_share_an_identifier(self):
        with pytest.raises(ValidationError):
            Yard(bins=(bin_for("N1"), bin_for("N1")))

    def test_asking_for_a_bin_that_is_not_there(self):
        with pytest.raises(NotFound):
            yard_with("N1").bin("N9")

    def test_receiving_fills_the_bin(self):
        yard = yard_with("N1").receive(
            "N1", "CORN", bushels("1000"), dry(), at=MORNING, reference="T1", by="s"
        )
        assert yard.contents_of("N1").quantity == bushels("1000")
        assert yard.state_of("N1").headroom == bushels("119000")

    def test_receiving_past_capacity_is_refused_and_posts_nothing(self):
        yard = yard_with("N1", capacity="1000")
        with pytest.raises(BinCapacityExceeded):
            yard.receive(
                "N1", "CORN", bushels("1001"), dry(), at=MORNING, reference="T1", by="s"
            )
        assert len(yard.ledger) == 0

    def test_shipping_draws_the_bin_down(self):
        yard = yard_with("N1").receive(
            "N1", "CORN", bushels("1000"), dry(), at=MORNING, reference="T1", by="s"
        )
        yard = yard.ship("N1", bushels("400"), at=NOON, reference="LO-1", by="l")
        assert yard.contents_of("N1").quantity == bushels("600")

    def test_shipping_from_an_empty_bin_is_refused(self):
        with pytest.raises(InsufficientInventory):
            yard_with("N1").ship("N1", bushels("1"), at=NOON, reference="LO-1", by="l")

    def test_history_reads_back_at_a_moment(self):
        yard = yard_with("N1").receive(
            "N1", "CORN", bushels("1000"), dry(), at=MORNING, reference="T1", by="s"
        )
        yard = yard.ship("N1", bushels("400"), at=EVENING, reference="LO-1", by="l")
        assert yard.contents_of("N1", when=NOON).quantity == bushels("1000")
        assert yard.contents_of("N1", when=EVENING).quantity == bushels("600")
        assert yard.contents_of("N1", when=MORNING - timedelta(hours=1)).is_empty

    def test_a_transfer_moves_quality_with_the_grain(self):
        yard = yard_with("N1", "N2").receive(
            "N1", "CORN", bushels("1000"), wet(), at=MORNING, reference="T1", by="s"
        )
        yard = yard.transfer("N1", "N2", bushels("400"), at=NOON, reference="TR-1", by="o")
        assert yard.contents_of("N1").quantity == bushels("600")
        assert yard.contents_of("N2").quantity == bushels("400")
        assert yard.contents_of("N2").quality == wet()

    def test_a_transfer_to_the_same_bin_is_refused(self):
        yard = yard_with("N1").receive(
            "N1", "CORN", bushels("1000"), dry(), at=MORNING, reference="T1", by="s"
        )
        with pytest.raises(ValidationError):
            yard.transfer("N1", "N1", bushels("100"), at=NOON, reference="TR", by="o")

    def test_a_measure_up_shortfall_is_posted_not_hidden(self):
        yard = yard_with("N1").receive(
            "N1", "CORN", bushels("10000"), dry(), at=MORNING, reference="T1", by="s"
        )
        yard = yard.adjust(
            "N1", bushels("-400"), "annual measure up", at=EVENING, reference="MU-25", by="m"
        )
        assert yard.contents_of("N1").quantity == bushels("9600")
        assert yard.ledger.entries[-1].kind is MovementKind.ADJUSTMENT
        assert yard.ledger.entries[-1].note == "annual measure up"

    def test_an_adjustment_cannot_conjure_grain_into_an_empty_bin(self):
        with pytest.raises(ValidationError):
            yard_with("N1").adjust(
                "N1", bushels("100"), "found it", at=NOON, reference="MU", by="m"
            )

    def test_totals_run_across_every_bin_holding_the_crop(self):
        yard = yard_with("N1", "N2", "N3")
        for name in ("N1", "N2"):
            yard = yard.receive(
                name, "CORN", bushels("1000"), dry(), at=MORNING, reference="T", by="s"
            )
        yard = yard.receive(
            "N3", "SOYBEANS", bushels("500"), dry(), at=MORNING, reference="T", by="s"
        )
        assert yard.total_of("CORN") == bushels("2000")
        assert yard.total_of("SOYBEANS") == bushels("500")
        assert len(yard.holding("CORN")) == 2

    def test_utilisation_reads_as_a_fraction(self):
        yard = yard_with("N1", capacity="1000").receive(
            "N1", "CORN", bushels("250"), dry(), at=MORNING, reference="T", by="s"
        )
        assert yard.state_of("N1").utilisation == Decimal("0.25")

    def test_a_bin_state_renders_for_a_board(self):
        yard = yard_with("N1").receive(
            "N1", "CORN", bushels("1000"), dry(), at=MORNING, reference="T", by="s"
        )
        described = yard.state_of("N1").describe()
        assert described["contents"]["commodity"] == "CORN"
        assert described["headroom"] == "119000.00 bu"


def test_a_full_harvest_day_reconciles():
    yard = yard_with("N1", "N2")
    at = MORNING
    for index in range(20):
        yard = yard.receive(
            "N1" if index % 2 == 0 else "N2",
            "CORN",
            bushels("860.71"),
            sample_for(commodity.CORN),
            at=at,
            reference=f"WBS-25-{index:06d}",
            by="scalehouse",
        )
        at += timedelta(minutes=12)
    assert yard.total_of("CORN") == bushels("17214.20")
    assert yard.ledger.balance("N1") + yard.ledger.balance("N2") == bushels("17214.20")
