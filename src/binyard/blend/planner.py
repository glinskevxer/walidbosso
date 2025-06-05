"""Turning a spec and a yard full of bins into a draw list.

The program is small and the shape is always the same. One variable per usable
bin, holding the bushels drawn from it. One equality saying the draws add to
the order. One row per spec limit, written so it says nothing about individual
bins and everything about the average:

    sum over bins of  x_i * (reading_i - limit)  <=  0

which is the average constraint with the denominator cleared out. That
rearrangement is the whole trick, and it is why a bin over the limit can still
go into a blend as long as enough bins under it come too.

Two things the program cannot express, handled outside it:

**Minimum draw is not linear.** "Take nothing from this bin, or at least five
hundred bushels" is a disjunction, and an LP has no way to say it. The planner
solves, drops any bin whose draw came back under its floor, and solves again
without those bins. It repeats until the answer is clean or there is nothing
left to drop. That terminates because each pass removes at least one bin.

**Bushels are not continuous.** The solver works in exact rationals and the
draw list has to be hundredths. Rounding each draw independently would miss the
order by a few hundredths, so the largest draw absorbs the difference.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction

from binyard.blend.simplex import Constraint, LinearProgram, Sense, Status, solve
from binyard.blend.spec import BlendSource, BlendSpec, SpecLimit
from binyard.bushels import Bushels
from binyard.errors import InfeasibleBlend, ValidationError
from binyard.grading.factors import FactorSample
from binyard.inventory.bin import commingle

__all__ = ["Objective", "Draw", "BlendPlan", "plan_blend"]


class Objective(StrEnum):
    """What the planner is trying to do beyond satisfying the spec."""

    #: Ship the poorest grain that still makes the spec. Keeps the good grain.
    SHIP_WORST_FIRST = "ship_worst_first"
    #: Ship the best grain. Used when a later order is tighter than this one.
    SHIP_BEST_FIRST = "ship_best_first"
    #: Use the cost supplied per bin. Anything the desk wants to weigh.
    EXPLICIT_COST = "explicit_cost"


@dataclass(frozen=True, slots=True)
class Draw:
    """How much comes out of one bin."""

    identifier: str
    quantity: Bushels

    def describe(self) -> dict[str, str]:
        return {"bin": self.identifier, "quantity": str(self.quantity)}


@dataclass(frozen=True, slots=True)
class BlendPlan:
    """A draw list that satisfies the spec, and the quality it produces."""

    spec: BlendSpec
    draws: tuple[Draw, ...]
    quality: FactorSample
    objective: Objective
    passes: int = 1
    notes: tuple[str, ...] = ()

    @property
    def quantity(self) -> Bushels:
        total = Bushels.zero()
        for draw in self.draws:
            total = total + draw.quantity
        return total

    @property
    def bins_touched(self) -> int:
        return len(self.draws)

    def draw_from(self, identifier: str) -> Bushels:
        for draw in self.draws:
            if draw.identifier == identifier:
                return draw.quantity
        return Bushels.zero()

    def margin_on(self, limit: SpecLimit) -> Decimal:
        """How much room is left under a ceiling, or above a floor."""
        reading = self.quality.reading(limit.factor)
        return limit.limit - reading if not limit.is_minimum else reading - limit.limit

    def describe(self) -> dict[str, object]:
        return {
            "quantity": str(self.quantity),
            "objective": self.objective.value,
            "bins_touched": self.bins_touched,
            "passes": self.passes,
            "draws": [draw.describe() for draw in self.draws],
            "quality": self.quality.describe(),
            "margins": {
                limit.factor.value: str(self.margin_on(limit)) for limit in self.spec.limits
            },
            "notes": list(self.notes),
        }


def plan_blend(
    spec: BlendSpec,
    sources: tuple[BlendSource, ...],
    *,
    objective: Objective = Objective.SHIP_WORST_FIRST,
    costs: dict[str, Decimal] | None = None,
) -> BlendPlan:
    """Find a draw list that fills the order and makes the spec."""
    usable = [
        source
        for source in sources
        if source.usable and spec.admits(source.quality)
    ]
    if not usable:
        raise InfeasibleBlend(
            "no bin is both available and inside the reject limits",
            offered=len(sources),
        )
    if objective is Objective.EXPLICIT_COST and not costs:
        raise ValidationError("an explicit cost objective needs costs")

    notes: list[str] = []
    passes = 0
    while usable:
        passes += 1
        solution = _solve_for(spec, usable, objective, costs)
        if solution is None:
            raise InfeasibleBlend(
                "no combination of these bins makes the spec",
                bins=[source.identifier for source in usable],
                wanted=str(spec.quantity),
            )
        draws = _to_draws(usable, solution, spec.quantity)
        short = [
            source
            for source, draw in zip(usable, draws, strict=True)
            if draw.quantity.is_positive() and draw.quantity < source.minimum_draw
        ]
        if not short:
            kept = tuple(draw for draw in draws if draw.quantity.is_positive())
            return BlendPlan(
                spec=spec,
                draws=kept,
                quality=_blended_quality(usable, kept),
                objective=objective,
                passes=passes,
                notes=tuple(notes),
            )
        dropped = {source.identifier for source in short}
        notes.append(
            "dropped " + ", ".join(sorted(dropped)) + " for coming in under the minimum draw"
        )
        usable = [source for source in usable if source.identifier not in dropped]

    raise InfeasibleBlend(
        "every bin that could help came in under its minimum draw",
        wanted=str(spec.quantity),
    )


def _solve_for(
    spec: BlendSpec,
    sources: list[BlendSource],
    objective: Objective,
    costs: dict[str, Decimal] | None,
) -> tuple[Fraction, ...] | None:
    width = len(sources)
    rows: list[Constraint] = [
        Constraint(
            coefficients=tuple(Fraction(1) for _ in sources),
            sense=Sense.EQ,
            rhs=Fraction(spec.quantity.hundredths),
        )
    ]
    for index, source in enumerate(sources):
        coefficients = [Fraction(0)] * width
        coefficients[index] = Fraction(1)
        rows.append(
            Constraint(
                coefficients=tuple(coefficients),
                sense=Sense.LE,
                rhs=Fraction(source.available.hundredths),
            )
        )
    for limit in spec.limits:
        coefficients = tuple(
            Fraction(source.reading(limit.factor)) - Fraction(limit.limit)
            for source in sources
        )
        rows.append(
            Constraint(
                coefficients=coefficients,
                sense=Sense.GE if limit.is_minimum else Sense.LE,
                rhs=Fraction(0),
            )
        )

    program = LinearProgram(
        objective=_costs_for(spec, sources, objective, costs), constraints=tuple(rows)
    )
    solution = solve(program)
    if solution.status is not Status.OPTIMAL:
        return None
    return solution.values


def _costs_for(
    spec: BlendSpec,
    sources: list[BlendSource],
    objective: Objective,
    costs: dict[str, Decimal] | None,
) -> tuple[Fraction, ...]:
    if objective is Objective.EXPLICIT_COST:
        assert costs is not None
        return tuple(
            Fraction(costs.get(source.identifier, Decimal(0))) for source in sources
        )
    sign = Fraction(-1) if objective is Objective.SHIP_WORST_FIRST else Fraction(1)
    return tuple(sign * _defect_score(spec, source) for source in sources)


def _defect_score(spec: BlendSpec, source: BlendSource) -> Fraction:
    """How far from the spec a bin sits, summed over the limited factors.

    Positive means worse than the spec asks for. Ship-worst-first makes this a
    negative cost, so the solver prefers the bins the elevator wants rid of.
    """
    score = Fraction(0)
    for limit in spec.limits:
        reading = Fraction(source.reading(limit.factor))
        edge = Fraction(limit.limit)
        score += (edge - reading) if limit.is_minimum else (reading - edge)
    return score


def _to_draws(
    sources: list[BlendSource], values: tuple[Fraction, ...], wanted: Bushels
) -> list[Draw]:
    hundredths = [int(value) for value in values]
    shortfall = wanted.hundredths - sum(hundredths)
    if shortfall:
        widest = max(range(len(hundredths)), key=lambda i: hundredths[i])
        hundredths[widest] += shortfall
    return [
        Draw(identifier=source.identifier, quantity=Bushels(amount))
        for source, amount in zip(sources, hundredths, strict=True)
    ]


def _blended_quality(sources: list[BlendSource], draws: tuple[Draw, ...]) -> FactorSample:
    by_identifier = {source.identifier: source for source in sources}
    running: FactorSample | None = None
    weight = 0
    for draw in draws:
        source = by_identifier[draw.identifier]
        if running is None:
            running = source.quality
            weight = draw.quantity.hundredths
            continue
        running = commingle((running, weight), (source.quality, draw.quantity.hundredths))
        weight += draw.quantity.hundredths
    assert running is not None
    return running
