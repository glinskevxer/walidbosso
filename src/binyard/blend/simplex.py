"""A small exact simplex, in rationals.

Blending to a spec is a linear program and there is no way around that. The
constraints are averages, the objective is money, and greedy heuristics get
close and then quietly ship a load that misses the moisture ceiling by a tenth.

This is a two phase tableau simplex over :class:`fractions.Fraction`. Rational
arithmetic rather than floating point, for two reasons that both matter here.
Degenerate vertices are common when several bins hold the same quality, and a
float pivot on a degenerate vertex can produce a tiny negative reduced cost
that sends the method round a cycle forever. And a blend plan is a number
somebody loads a train against, so "48,000 bushels give or take a rounding
error" is not an acceptable answer.

Bland's rule is used for pivoting. It is slower than steepest edge and it
cannot cycle, which is the whole point. Problems here are a few dozen columns
and under ten rows, so the difference is microseconds.

The module knows nothing about grain. It takes a program and returns a vertex.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction

from binyard.errors import ValidationError

__all__ = ["Constraint", "LinearProgram", "Sense", "Solution", "Status", "solve"]

_ZERO = Fraction(0)


class Sense(StrEnum):
    """Which way a constraint runs."""

    LE = "<="
    GE = ">="
    EQ = "=="


class Status(StrEnum):
    OPTIMAL = "optimal"
    INFEASIBLE = "infeasible"
    UNBOUNDED = "unbounded"


@dataclass(frozen=True, slots=True)
class Constraint:
    """One row: ``coefficients . x  sense  rhs``."""

    coefficients: tuple[Fraction, ...]
    sense: Sense
    rhs: Fraction

    def normalised(self) -> Constraint:
        """Flip the row if the right hand side is negative, so it can be based."""
        if self.rhs >= 0:
            return self
        flipped = {Sense.LE: Sense.GE, Sense.GE: Sense.LE, Sense.EQ: Sense.EQ}
        return Constraint(
            coefficients=tuple(-c for c in self.coefficients),
            sense=flipped[self.sense],
            rhs=-self.rhs,
        )


@dataclass(frozen=True, slots=True)
class LinearProgram:
    """Minimise ``objective . x`` subject to ``constraints``, with ``x >= 0``."""

    objective: tuple[Fraction, ...]
    constraints: tuple[Constraint, ...]

    def __post_init__(self) -> None:
        width = len(self.objective)
        if width == 0:
            raise ValidationError("a program needs at least one variable")
        for index, constraint in enumerate(self.constraints):
            if len(constraint.coefficients) != width:
                raise ValidationError(
                    "a constraint has the wrong number of coefficients",
                    row=index,
                    expected=width,
                    got=len(constraint.coefficients),
                )

    @property
    def variables(self) -> int:
        return len(self.objective)


@dataclass(frozen=True, slots=True)
class Solution:
    """A vertex, or the reason there is not one."""

    status: Status
    values: tuple[Fraction, ...] = ()
    objective_value: Fraction = _ZERO
    iterations: int = 0

    @property
    def is_optimal(self) -> bool:
        return self.status is Status.OPTIMAL


def solve(program: LinearProgram, *, iteration_limit: int = 10_000) -> Solution:
    """Solve to optimality, or say why it cannot be done."""
    rows = [constraint.normalised() for constraint in program.constraints]
    if not rows:
        # With no constraints the minimum is zero unless a cost is negative.
        if any(cost < 0 for cost in program.objective):
            return Solution(status=Status.UNBOUNDED)
        return Solution(
            status=Status.OPTIMAL,
            values=tuple(_ZERO for _ in program.objective),
            objective_value=_ZERO,
        )

    structural = program.variables
    slack_of: list[int | None] = []
    artificial_of: list[int | None] = []

    extra = 0
    for row in rows:
        if row.sense is Sense.LE or row.sense is Sense.GE:
            slack_of.append(extra)
            extra += 1
            artificial_of.append(None)
        else:
            slack_of.append(None)
            artificial_of.append(None)

    artificial_start = structural + extra
    artificial_count = 0
    for index, row in enumerate(rows):
        if row.sense in {Sense.GE, Sense.EQ}:
            artificial_of[index] = artificial_count
            artificial_count += 1

    width = structural + extra + artificial_count
    tableau: list[list[Fraction]] = []
    basis: list[int] = []

    for index, row in enumerate(rows):
        line = [Fraction(0)] * (width + 1)
        for column, coefficient in enumerate(row.coefficients):
            line[column] = Fraction(coefficient)
        slack = slack_of[index]
        if slack is not None:
            line[structural + slack] = Fraction(1) if row.sense is Sense.LE else Fraction(-1)
        artificial = artificial_of[index]
        if artificial is not None:
            line[artificial_start + artificial] = Fraction(1)
            basis.append(artificial_start + artificial)
        else:
            assert slack is not None
            basis.append(structural + slack)
        line[width] = Fraction(row.rhs)
        tableau.append(line)

    iterations = 0

    if artificial_count:
        phase_one = [Fraction(0)] * (width + 1)
        for column in range(artificial_start, width):
            phase_one[column] = Fraction(1)
        objective_row = _price_out(phase_one, tableau, basis, width)
        status, iterations = _pivot(tableau, objective_row, basis, width, iteration_limit)
        if status is Status.UNBOUNDED:  # pragma: no cover - phase one is bounded below
            return Solution(status=Status.INFEASIBLE, iterations=iterations)
        if -objective_row[width] != 0:
            return Solution(status=Status.INFEASIBLE, iterations=iterations)
        _drive_out_artificials(tableau, basis, width, artificial_start)

    phase_two = [Fraction(0)] * (width + 1)
    for column, cost in enumerate(program.objective):
        phase_two[column] = Fraction(cost)
    for column in range(artificial_start, width):
        phase_two[column] = Fraction(0)
    objective_row = _price_out(phase_two, tableau, basis, width)
    for column in range(artificial_start, width):
        objective_row[column] = Fraction(0)

    status, more = _pivot(
        tableau, objective_row, basis, width, iteration_limit, blocked=artificial_start
    )
    iterations += more
    if status is Status.UNBOUNDED:
        return Solution(status=Status.UNBOUNDED, iterations=iterations)

    values = [Fraction(0)] * structural
    for row, column in enumerate(basis):
        if column < structural:
            values[column] = tableau[row][width]
    return Solution(
        status=Status.OPTIMAL,
        values=tuple(values),
        objective_value=-objective_row[width],
        iterations=iterations,
    )


def _price_out(
    costs: list[Fraction], tableau: list[list[Fraction]], basis: list[int], width: int
) -> list[Fraction]:
    """Make the objective row consistent with the current basis."""
    row = list(costs)
    for index, column in enumerate(basis):
        factor = row[column]
        if factor != 0:
            for position in range(width + 1):
                row[position] -= factor * tableau[index][position]
    return row


def _pivot(
    tableau: list[list[Fraction]],
    objective: list[Fraction],
    basis: list[int],
    width: int,
    limit: int,
    *,
    blocked: int | None = None,
) -> tuple[Status, int]:
    """Run Bland's rule until nothing improves."""
    ceiling = width if blocked is None else blocked
    iterations = 0
    while iterations < limit:
        entering = -1
        for column in range(ceiling):
            if objective[column] < 0:
                entering = column
                break
        if entering < 0:
            return Status.OPTIMAL, iterations

        leaving = -1
        best: Fraction | None = None
        for index, line in enumerate(tableau):
            if line[entering] <= 0:
                continue
            ratio = line[width] / line[entering]
            if (
                best is None
                or ratio < best
                or (ratio == best and basis[index] < basis[leaving])
            ):
                best = ratio
                leaving = index
        if leaving < 0:
            return Status.UNBOUNDED, iterations

        _eliminate(tableau, objective, basis, width, leaving, entering)
        iterations += 1
    raise ValidationError("the simplex did not finish inside its iteration limit")


def _eliminate(
    tableau: list[list[Fraction]],
    objective: list[Fraction],
    basis: list[int],
    width: int,
    row: int,
    column: int,
) -> None:
    pivot = tableau[row][column]
    tableau[row] = [value / pivot for value in tableau[row]]
    for index, line in enumerate(tableau):
        if index == row:
            continue
        factor = line[column]
        if factor != 0:
            tableau[index] = [
                value - factor * pivot_value
                for value, pivot_value in zip(line, tableau[row], strict=True)
            ]
    factor = objective[column]
    if factor != 0:
        for position in range(width + 1):
            objective[position] -= factor * tableau[row][position]
    basis[row] = column


def _drive_out_artificials(
    tableau: list[list[Fraction]], basis: list[int], width: int, artificial_start: int
) -> None:
    """Pivot any artificial left in the basis at zero out of it."""
    for row, column in enumerate(basis):
        if column < artificial_start:
            continue
        for candidate in range(artificial_start):
            if tableau[row][candidate] != 0:
                objective = [Fraction(0)] * (width + 1)
                _eliminate(tableau, objective, basis, width, row, candidate)
                break
