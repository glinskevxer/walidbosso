"""Gross, tare and the several ways a truck gets weighed.

The simple case is a truck that drives over the scale full, dumps, and drives
over it empty. Gross minus tare is the load and there is nothing to argue
about.

Harvest is not the simple case. A truck may be tared once in the morning and
run on that figure all day, which is a **stored tare** and is a commercial
decision with a limit on how long it stays good. A semi with a pup may weigh
in as one unit and split into two dumps against two different bins. A driver
may forget to tare at all, and the elevator has to use the fleet's book tare
and flag the ticket so someone looks at it.

Which of those happened is not a detail. A stored tare that has gone stale is
the single most common source of a bin that will not reconcile, so the source
travels with the weights and is printed on the ticket.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from binyard.errors import ValidationError
from binyard.units import Weight

__all__ = ["STORED_TARE_MAX_AGE", "ScaleWeights", "TareSource", "split_weights"]

#: A tare taken more than this long before the gross has to be re-weighed.
STORED_TARE_MAX_AGE = timedelta(hours=12)


class TareSource(StrEnum):
    """Where the tare weight came from."""

    #: Weighed empty at this visit. The only kind that needs no supervision.
    WEIGHED = "weighed"
    #: Weighed empty earlier today and reused.
    STORED = "stored"
    #: The fleet's registered empty weight, used because nothing else was available.
    BOOK = "book"
    #: The driver gave a number. Always flagged.
    DECLARED = "declared"

    @property
    def needs_review(self) -> bool:
        return self in {TareSource.BOOK, TareSource.DECLARED}


@dataclass(frozen=True, slots=True)
class ScaleWeights:
    """One weighing, with enough context to defend the net."""

    gross: Weight
    tare: Weight
    tare_source: TareSource = TareSource.WEIGHED
    gross_at: datetime | None = None
    tare_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.gross, Weight) or not isinstance(self.tare, Weight):
            raise ValidationError("gross and tare must both be weights")
        if not self.gross.is_positive():
            raise ValidationError("a gross weight has to be positive", gross=str(self.gross))
        if self.tare.hundredths < 0:
            raise ValidationError("a tare cannot be negative", tare=str(self.tare))
        if self.tare >= self.gross:
            raise ValidationError(
                "the tare is not lighter than the gross, so there is no load",
                gross=str(self.gross),
                tare=str(self.tare),
            )
        if (
            self.gross_at is not None
            and self.tare_at is not None
            and self.tare_source is TareSource.WEIGHED
            and self.tare_at > self.gross_at
        ):
            raise ValidationError("a weighed tare cannot be later than the gross it belongs to")

    @property
    def net(self) -> Weight:
        return self.gross - self.tare

    @property
    def tare_age(self) -> timedelta | None:
        if self.gross_at is None or self.tare_at is None:
            return None
        return abs(self.gross_at - self.tare_at)

    @property
    def tare_is_stale(self) -> bool:
        """True when a stored tare is older than the house allows."""
        if self.tare_source is not TareSource.STORED:
            return False
        age = self.tare_age
        return age is None or age > STORED_TARE_MAX_AGE

    @property
    def needs_review(self) -> bool:
        return self.tare_source.needs_review or self.tare_is_stale

    def review_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.tare_source is TareSource.BOOK:
            reasons.append("tare taken from the fleet book rather than the scale")
        if self.tare_source is TareSource.DECLARED:
            reasons.append("tare declared by the driver")
        if self.tare_is_stale:
            age = self.tare_age
            said = (
                "unknown age" if age is None else f"{age.total_seconds() / 3600:.1f} hours old"
            )
            reasons.append(f"stored tare is {said}")
        return tuple(reasons)

    def with_tare(
        self, tare: Weight, source: TareSource, at: datetime | None = None
    ) -> ScaleWeights:
        """Re-tare, which is what happens when a driver comes back to the scale."""
        return ScaleWeights(
            gross=self.gross,
            tare=tare,
            tare_source=source,
            gross_at=self.gross_at,
            tare_at=at if at is not None else self.tare_at,
        )

    def describe(self) -> dict[str, str]:
        return {
            "gross": str(self.gross),
            "tare": str(self.tare),
            "net": str(self.net),
            "tare_source": self.tare_source.value,
        }


def split_weights(weights: ScaleWeights, shares: list[int]) -> tuple[ScaleWeights, ...]:
    """Divide one weighing into several by integer shares, losing nothing.

    A semi and pup weighed as one unit, dumped into two bins. The tare is split
    the same way as the gross so each part's net is a true share of the whole,
    and the remainder hundredths land on the first parts so the pieces add back
    to the original exactly.
    """
    if not shares or any(share <= 0 for share in shares):
        raise ValidationError("split shares must be a non empty list of positives")
    total = sum(shares)
    gross_parts = _divide(weights.gross, shares, total)
    tare_parts = _divide(weights.tare, shares, total)
    parts: list[ScaleWeights] = []
    for gross, tare in zip(gross_parts, tare_parts, strict=True):
        parts.append(
            ScaleWeights(
                gross=gross,
                tare=tare,
                tare_source=weights.tare_source,
                gross_at=weights.gross_at,
                tare_at=weights.tare_at,
            )
        )
    return tuple(parts)


def _divide(weight: Weight, shares: list[int], total: int) -> list[Weight]:
    base = [weight.hundredths * share // total for share in shares]
    remainder = weight.hundredths - sum(base)
    for index in range(remainder):
        base[index % len(base)] += 1
    return [Weight(value) for value in base]
