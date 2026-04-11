"""Building a composite out of what the probe actually pulled.

A grade is only as good as the sample, and the sample is not one scoop. A
truck is probed in several places because grain segregates in transit: the
fines and the light kernels migrate to the middle and the heavy ones to the
sides, so a single probe down the centre of a hopper reads several points
different from the load.

Two rules that houses argue about and that are settled here:

**Enough probes, spread far enough apart.** A load probed twice, both times in
the same third of the trailer, is not a load that has been sampled. The
adequacy check counts probes and positions, and it refuses to call the result
representative rather than quietly producing a grade nobody could defend.

**The composite is weighted by what each probe pulled**, not averaged across
probes. A probe that came up half full contributed half as much grain and gets
half the weight, and a house that averages the readings instead is letting a
short probe count as much as a full one.

The spread between probes is kept as well as the average, because a load with
14% at one end and 19% at the other is a load somebody should look at before it
goes in a bin, whatever its average says.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from binyard.errors import ValidationError
from binyard.grading.factors import FactorName, FactorSample, Odor, weighted_sample
from binyard.percent import Percent

__all__ = [
    "MINIMUM_POSITIONS",
    "MINIMUM_PROBES",
    "Composite",
    "Position",
    "Probe",
    "build_composite",
]

#: A load probed fewer times than this is not a sampled load.
MINIMUM_PROBES = 3
#: And they have to be in at least this many places along the trailer.
MINIMUM_POSITIONS = 2

#: Points of moisture spread that puts a load in front of somebody.
SPREAD_TO_REVIEW = Percent.of_points("3.0")


class Position(StrEnum):
    """Where along the load a probe was taken."""

    FRONT = "front"
    MIDDLE = "middle"
    REAR = "rear"
    #: A second trailer. Counts as its own set of positions.
    PUP_FRONT = "pup_front"
    PUP_REAR = "pup_rear"

    @property
    def is_pup(self) -> bool:
        return self in {Position.PUP_FRONT, Position.PUP_REAR}


@dataclass(frozen=True, slots=True)
class Probe:
    """One draw of the probe, and what it read."""

    position: Position
    #: Grams pulled. A short probe counts for less in the composite.
    grams: int
    reading: FactorSample

    def __post_init__(self) -> None:
        if self.grams <= 0:
            raise ValidationError(
                "a probe that pulled nothing is not a probe", position=self.position.value
            )


@dataclass(frozen=True, slots=True)
class Composite:
    """The sample the grade is taken on, and how good it is."""

    sample: FactorSample
    probes: tuple[Probe, ...]
    representative: bool
    reasons: tuple[str, ...] = ()

    @property
    def grams(self) -> int:
        return sum(probe.grams for probe in self.probes)

    @property
    def positions(self) -> tuple[Position, ...]:
        seen: list[Position] = []
        for probe in self.probes:
            if probe.position not in seen:
                seen.append(probe.position)
        return tuple(seen)

    def spread(self, factor: FactorName) -> Decimal:
        """The gap between the highest and lowest probe on one factor."""
        readings = [probe.reading.reading(factor) for probe in self.probes]
        return max(readings) - min(readings)

    @property
    def needs_a_look(self) -> bool:
        return self.spread(FactorName.MOISTURE) > SPREAD_TO_REVIEW.points

    def describe(self) -> dict[str, object]:
        return {
            "probes": len(self.probes),
            "grams": self.grams,
            "positions": [item.value for item in self.positions],
            "representative": self.representative,
            "reasons": list(self.reasons),
            "moisture_spread": str(self.spread(FactorName.MOISTURE)),
            "needs_a_look": self.needs_a_look,
            "sample": self.sample.describe(),
        }


def build_composite(probes: list[Probe]) -> Composite:
    """Weight the probes into one sample and say whether it is representative."""
    if not probes:
        raise ValidationError("a composite needs at least one probe")

    sample = weighted_sample([(probe.reading, probe.grams) for probe in probes])
    reasons: list[str] = []

    if len(probes) < MINIMUM_PROBES:
        reasons.append(f"{len(probes)} probes taken, {MINIMUM_PROBES} is the minimum")

    positions = {probe.position for probe in probes}
    if len(positions) < MINIMUM_POSITIONS:
        reasons.append(
            "every probe came from the same place along the load"
            if len(positions) == 1
            else f"probed in {len(positions)} places, {MINIMUM_POSITIONS} is the minimum"
        )

    thin = [probe for probe in probes if probe.grams < 200]
    if thin:
        reasons.append(f"{len(thin)} probes came up short of 200 grams")

    if any(probe.reading.odor is not Odor.NONE for probe in probes) and (
        sample.odor is Odor.NONE
    ):  # pragma: no cover - commingle takes the worst, so this cannot happen
        reasons.append("an odor was found in a probe but not carried into the composite")

    return Composite(
        sample=sample,
        probes=tuple(probes),
        representative=not reasons,
        reasons=tuple(reasons),
    )
