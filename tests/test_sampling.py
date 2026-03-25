from decimal import Decimal

import pytest

from binyard.errors import ValidationError
from binyard.grading import (
    MINIMUM_PROBES,
    FactorName,
    FactorSample,
    Position,
    Probe,
    build_composite,
)
from binyard.grading.factors import Odor
from binyard.percent import percent


def reading(moisture="15.0", test_weight="57.0", **overrides):
    fields: dict[str, object] = {
        "moisture": percent(moisture),
        "test_weight": Decimal(test_weight),
    }
    fields.update(overrides)
    return FactorSample(**fields)  # type: ignore[arg-type]


def probe(position=Position.FRONT, grams=400, **overrides):
    return Probe(position=position, grams=grams, reading=reading(**overrides))


def three_probes(**overrides):
    return [
        probe(Position.FRONT, **overrides),
        probe(Position.MIDDLE, **overrides),
        probe(Position.REAR, **overrides),
    ]


class TestProbes:
    def test_a_probe_that_pulled_nothing_is_refused(self):
        with pytest.raises(ValidationError):
            probe(grams=0)

    def test_only_the_pup_positions_are_a_pup(self):
        assert Position.PUP_FRONT.is_pup
        assert not Position.MIDDLE.is_pup


class TestBuilding:
    def test_no_probes_at_all_is_refused(self):
        with pytest.raises(ValidationError):
            build_composite([])

    def test_three_full_probes_are_representative(self):
        composite = build_composite(three_probes())
        assert composite.representative
        assert composite.reasons == ()
        assert composite.grams == 1200

    def test_too_few_probes_is_not_representative(self):
        composite = build_composite([probe(Position.FRONT), probe(Position.REAR)])
        assert not composite.representative
        assert f"{MINIMUM_PROBES} is the minimum" in composite.reasons[0]

    def test_probing_the_same_place_three_times_is_not_representative(self):
        composite = build_composite([probe(Position.MIDDLE) for _ in range(3)])
        assert not composite.representative
        assert "same place" in composite.reasons[0]

    def test_a_short_probe_is_called_out(self):
        probes = three_probes()
        probes[1] = probe(Position.MIDDLE, grams=120)
        composite = build_composite(probes)
        assert not composite.representative
        assert "short of 200 grams" in composite.reasons[0]

    def test_a_pup_counts_as_its_own_positions(self):
        composite = build_composite(
            [
                probe(Position.FRONT),
                probe(Position.REAR),
                probe(Position.PUP_FRONT),
                probe(Position.PUP_REAR),
            ]
        )
        assert composite.representative
        assert len(composite.positions) == 4


class TestWeighting:
    def test_the_composite_is_weighted_by_grams_not_averaged(self):
        heavy = Probe(Position.FRONT, 900, reading(moisture="14.0"))
        light = Probe(Position.REAR, 100, reading(moisture="24.0"))
        third = Probe(Position.MIDDLE, 500, reading(moisture="14.0"))
        composite = build_composite([heavy, light, third])
        # A straight mean of the three readings would be over 17%.
        assert composite.sample.moisture < percent("16.0")

    def test_identical_probes_average_to_themselves(self):
        composite = build_composite(three_probes(moisture="15.4"))
        assert composite.sample.moisture == percent("15.4")

    def test_the_composite_lands_between_the_probes(self):
        probes = [
            probe(Position.FRONT, moisture="13.0"),
            probe(Position.MIDDLE, moisture="16.0"),
            probe(Position.REAR, moisture="19.0"),
        ]
        composite = build_composite(probes)
        assert percent("13.0") <= composite.sample.moisture <= percent("19.0")

    def test_test_weight_is_weighted_too(self):
        probes = [
            probe(Position.FRONT, grams=900, test_weight="58.0"),
            probe(Position.MIDDLE, grams=300, test_weight="54.0"),
            probe(Position.REAR, grams=300, test_weight="54.0"),
        ]
        composite = build_composite(probes)
        assert Decimal("56.0") < composite.sample.test_weight < Decimal("58.0")

    def test_an_odor_in_one_probe_reaches_the_composite(self):
        probes = three_probes()
        probes[2] = Probe(Position.REAR, 400, reading(odor=Odor.SOUR))
        assert build_composite(probes).sample.odor is Odor.SOUR

    def test_infestation_in_one_probe_reaches_the_composite(self):
        probes = three_probes()
        probes[0] = Probe(Position.FRONT, 400, reading(infested=True))
        assert build_composite(probes).sample.infested


class TestSpread:
    def test_a_tight_load_needs_no_second_look(self):
        probes = [
            probe(Position.FRONT, moisture="15.0"),
            probe(Position.MIDDLE, moisture="15.4"),
            probe(Position.REAR, moisture="15.2"),
        ]
        composite = build_composite(probes)
        assert composite.spread(FactorName.MOISTURE) == Decimal("0.40")
        assert not composite.needs_a_look

    def test_a_load_wet_at_one_end_is_flagged(self):
        probes = [
            probe(Position.FRONT, moisture="14.0"),
            probe(Position.MIDDLE, moisture="15.5"),
            probe(Position.REAR, moisture="19.0"),
        ]
        composite = build_composite(probes)
        assert composite.spread(FactorName.MOISTURE) == Decimal("5.00")
        assert composite.needs_a_look

    def test_the_spread_reads_on_any_factor(self):
        probes = [
            probe(Position.FRONT, test_weight="58.0"),
            probe(Position.MIDDLE, test_weight="54.0"),
            probe(Position.REAR, test_weight="56.0"),
        ]
        assert build_composite(probes).spread(FactorName.TEST_WEIGHT) == Decimal("4.0")

    def test_one_probe_has_no_spread(self):
        composite = build_composite([probe(Position.FRONT)])
        assert composite.spread(FactorName.MOISTURE) == Decimal("0.00")
        assert not composite.needs_a_look


def test_the_composite_renders_for_a_stub():
    described = build_composite(three_probes()).describe()
    assert described["probes"] == 3
    assert described["representative"] is True
    assert described["sample"]["test_weight"] == "57.0 lb/bu"
