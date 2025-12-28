"""Running the dryer: draw wet, burn propane, put dry grain somewhere else.

The awkward part is the middle. Between drawing the wet bushels and putting the
dry ones away, the grain is in a machine and belongs to nobody's bin. A house
that leaves it in the wet bin until the run finishes reports inventory it does
not have, and one that moves it early reports a dry bin holding wet grain.

So the draw happens when the run is lit and the fill happens when it finishes,
and the run itself is where the bushels live in between. That is why the run is
stored and not just computed: it is a real place inventory can be.

The loss is posted as its own movement kind against the destination bin, so a
position report can say "the dryer took four hundred bushels this week" rather
than showing an unexplained shortfall.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from binyard.bushels import Bushels
from binyard.commodity import CropStandard
from binyard.drying.run import DryerRun, FuelUse, RunStatus
from binyard.errors import ValidationError
from binyard.grading.factors import FactorSample
from binyard.inventory.ledger import Movement, MovementKind
from binyard.percent import Percent
from binyard.shrink import ShrinkPolicy
from binyard.store.protocol import UnitOfWork

__all__ = ["DryingOutcome", "abandon_run", "finish_run", "start_run"]


@dataclass(frozen=True, slots=True)
class DryingOutcome:
    """A finished run, and what it did to the yard."""

    run: DryerRun
    posted_loss: Bushels
    dry_quality: FactorSample

    def describe(self, policy: ShrinkPolicy | None = None) -> dict[str, object]:
        return {
            "run": self.run.describe(policy),
            "posted_loss": str(self.posted_loss),
            "dry_quality": self.dry_quality.describe(),
        }


def start_run(
    work: UnitOfWork,
    *,
    identifier: str,
    source_bin: str,
    destination_bin: str,
    quantity: Bushels,
    at: datetime,
    by: str,
) -> DryerRun:
    """Draw the wet bushels out of their bin and light the dryer."""
    yard = work.yard.load()
    contents = yard.contents_of(source_bin)
    if contents.is_empty or contents.quality is None:
        raise ValidationError("that bin is empty", bin=source_bin)
    if contents.commodity_code is None:  # pragma: no cover - guarded by is_empty
        raise ValidationError("that bin holds nothing identifiable", bin=source_bin)

    run = DryerRun(
        identifier=identifier,
        commodity_code=contents.commodity_code,
        source_bin=source_bin,
        destination_bin=destination_bin,
        wet_bushels=quantity,
        wet_moisture=contents.quality.moisture,
        started_at=at,
    ).lit(at)

    yard = yard.ship(source_bin, quantity, at=at, reference=identifier, by=by)
    work.yard.save(yard)
    work.dryer.add(run)
    work.commit()
    return run


def finish_run(
    work: UnitOfWork,
    *,
    identifier: str,
    dry_bushels: Bushels,
    dry_moisture: Percent,
    standard: CropStandard,
    at: datetime,
    by: str,
    fuel: FuelUse | None = None,
) -> DryingOutcome:
    """Put the dry grain away and post what the dryer kept."""
    run = work.dryer.get(identifier)
    if run.status is not RunStatus.DRYING:
        raise ValidationError(
            "that run is not on the dryer", run=identifier, status=run.status.value
        )
    if run.commodity_code != standard.code:
        raise ValidationError(
            "that run is a different crop", run=identifier, crop=run.commodity_code
        )

    finished = run.finished(
        dry_bushels=dry_bushels, dry_moisture=dry_moisture, at=at, fuel=fuel
    )
    quality = _dry_quality(work, run, dry_moisture)

    # The wet bushels went into the run when it was lit, so the destination bin
    # takes the whole wet quantity back and the dryer's keep is posted against
    # it as its own movement. Receiving only the dry figure would leave the loss
    # nowhere, which is the shortfall nobody can explain in March.
    yard = work.yard.load()
    yard = yard.receive(
        run.destination_bin,
        run.commodity_code,
        run.wet_bushels,
        quality,
        at=at,
        reference=identifier,
        by=by,
    )
    loss = finished.actual_shrink
    if loss.is_positive():
        yard = yard.post(
            Movement(
                at=at,
                bin_identifier=run.destination_bin,
                kind=MovementKind.DRYER_LOSS,
                commodity_code=run.commodity_code,
                quantity=Bushels.zero() - loss,
                reference=identifier,
                by=by,
                note=f"dryer kept {loss} taking {finished.points_removed} out",
            )
        )

    work.yard.save(yard)
    work.dryer.replace(finished)
    work.commit()
    return DryingOutcome(run=finished, posted_loss=loss, dry_quality=quality)


def abandon_run(
    work: UnitOfWork,
    *,
    identifier: str,
    reason: str,
    dry_bushels: Bushels,
    dry_moisture: Percent,
    at: datetime,
    by: str,
) -> DryerRun:
    """Stop a run early. Whatever came out goes to the destination bin anyway."""
    run = work.dryer.get(identifier)
    stopped = run.abandoned(reason, dry_bushels=dry_bushels, dry_moisture=dry_moisture, at=at)
    quality = _dry_quality(work, run, dry_moisture)
    yard = work.yard.load()
    if dry_bushels.is_positive():
        yard = yard.receive(
            run.destination_bin,
            run.commodity_code,
            dry_bushels,
            quality,
            at=at,
            reference=identifier,
            by=by,
        )
    work.yard.save(yard)
    work.dryer.replace(stopped)
    work.commit()
    return stopped


def _dry_quality(work: UnitOfWork, run: DryerRun, dry_moisture: Percent) -> FactorSample:
    """The wet grain's factors with the new moisture on them.

    Drying moves moisture and leaves everything else where it was. It does add
    a little breakage, but the amount is not measurable per run and guessing at
    it would put a made up number into a bin's average.
    """
    yard = work.yard.load()
    contents = yard.contents_of(run.source_bin)
    if contents.quality is not None:
        return _with_moisture(contents.quality, dry_moisture)
    for entry in yard.ledger.for_reference(run.identifier):
        if entry.quality is not None:
            return _with_moisture(entry.quality, dry_moisture)
    raise ValidationError(
        "nothing left in the yard says what that run was drying", run=run.identifier
    )


def _with_moisture(sample: FactorSample, moisture: Percent) -> FactorSample:
    from dataclasses import replace

    return replace(sample, moisture=moisture)
