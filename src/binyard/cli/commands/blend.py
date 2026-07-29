"""``binyard blend``: work out a draw list for a shipping spec.

The command never writes. It is what the grain desk runs at ten in the morning
to find out whether the order they are about to take is one the yard can
actually make, and running it should be free.

A spec can be given two ways. ``--grade 2`` pulls the published limits for that
grade out of the table, which is what a contract usually says. ``--max`` and
``--min`` add or tighten individual factors, which is what the buyer's fax
usually says on top of it. The tighter of the two always wins, because a spec
that quietly relaxed a grade limit would ship grain that fails on arrival.
"""

from __future__ import annotations

import argparse
from decimal import Decimal

from binyard.blend.planner import Objective, plan_blend
from binyard.blend.spec import BlendSpec, SpecLimit, sources_from
from binyard.bushels import bushels
from binyard.cli.context import Context
from binyard.commodity import lookup
from binyard.errors import ValidationError
from binyard.grading.factors import Direction, FactorName, direction_of
from binyard.reporting.reports import blend_table
from binyard.reporting.table import Column, Table

__all__ = ["build_spec", "handle", "register"]


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register this subcommand's parser and its handler."""
    parser = subparsers.add_parser("blend", help="plan a draw list against a spec")
    parser.add_argument("--crop", required=True)
    parser.add_argument("--quantity", required=True, help="bushels wanted")
    parser.add_argument("--grade", type=int, help="take the limits from this grade")
    parser.add_argument(
        "--max",
        dest="maximums",
        action="append",
        default=[],
        metavar="FACTOR=VALUE",
        help="ceiling on one factor, repeatable",
    )
    parser.add_argument(
        "--min",
        dest="minimums",
        action="append",
        default=[],
        metavar="FACTOR=VALUE",
        help="floor on one factor, repeatable",
    )
    parser.add_argument(
        "--reject",
        dest="rejects",
        action="append",
        default=[],
        metavar="FACTOR=VALUE",
        help="keep any bin worse than this out entirely, repeatable",
    )
    parser.add_argument(
        "--objective",
        choices=[item.value for item in Objective if item is not Objective.EXPLICIT_COST],
        default=Objective.SHIP_WORST_FIRST.value,
    )
    parser.add_argument(
        "--lock",
        dest="locked",
        action="append",
        default=[],
        metavar="BIN",
        help="hold a bin out of the blend, repeatable",
    )
    parser.add_argument("--minimum-draw", default=None, help="bushels, per bin")
    parser.set_defaults(handler=handle)


def build_spec(arguments: argparse.Namespace, commodity_code: str) -> BlendSpec:
    """Assemble the spec, letting explicit limits tighten a grade's."""
    quantity = bushels(arguments.quantity)
    if arguments.grade is not None:
        spec = BlendSpec.for_grade(commodity_code, quantity, arguments.grade)
        limits = {limit.factor: limit for limit in spec.limits}
    else:
        limits = {}

    for text in arguments.maximums:
        limit = _parse_limit(text, Direction.LOWER_IS_BETTER)
        limits[limit.factor] = _tighter(limits.get(limit.factor), limit)
    for text in arguments.minimums:
        limit = _parse_limit(text, Direction.HIGHER_IS_BETTER)
        limits[limit.factor] = _tighter(limits.get(limit.factor), limit)

    rejects = tuple(_parse_limit(text, None) for text in arguments.rejects)
    if not limits:
        raise ValidationError("a blend needs at least one limit or a grade")
    return BlendSpec(
        commodity_code=commodity_code,
        quantity=quantity,
        minimum_grade=arguments.grade,
        limits=tuple(limits[factor] for factor in sorted(limits, key=lambda f: f.value)),
        reject_worse_than=rejects,
    )


def handle(context: Context, arguments: argparse.Namespace) -> int:
    """Run the command and return the exit code."""
    standard = lookup(arguments.crop)
    spec = build_spec(arguments, standard.code)
    work = context.work()
    minimum = (
        bushels(arguments.minimum_draw)
        if arguments.minimum_draw is not None
        else bushels(context.settings.minimum_bin_draw)
    )
    sources = sources_from(
        work.yard.load().states(),
        standard.code,
        minimum_draw=minimum,
        locked=frozenset(arguments.locked),
    )
    plan = plan_blend(spec, sources, objective=Objective(arguments.objective))
    context.emit(_spec_table(spec))
    context.emit(blend_table(plan))
    for note in plan.notes:
        context.say(note)
    return 0


def _spec_table(spec: BlendSpec) -> Table:
    rows = [limit.describe() for limit in spec.limits]
    for limit in spec.reject_worse_than:
        described = limit.describe()
        described["sense"] = f"{described['sense']} (reject)"
        rows.append(described)
    return Table(
        title=f"Spec for {spec.quantity} of {spec.commodity_code}",
        columns=(
            Column("factor", "Factor"),
            Column("sense", "Sense"),
            Column.number("limit", "Limit"),
        ),
        rows=tuple(rows),
    )


def _parse_limit(text: str, expected: Direction | None) -> SpecLimit:
    if "=" not in text:
        raise ValidationError("a limit is written FACTOR=VALUE", got=text)
    name, _, value = text.partition("=")
    try:
        factor = FactorName(name.strip().lower())
    except ValueError as exc:
        raise ValidationError(
            "no such factor", got=name, known=[item.value for item in FactorName]
        ) from exc
    if expected is not None and direction_of(factor) is not expected:
        sense = "a maximum" if expected is Direction.LOWER_IS_BETTER else "a minimum"
        raise ValidationError(f"{factor.value} does not take {sense}", factor=factor.value)
    return SpecLimit(factor=factor, limit=Decimal(value.strip()))


def _tighter(existing: SpecLimit | None, offered: SpecLimit) -> SpecLimit:
    if existing is None:
        return offered
    if direction_of(offered.factor) is Direction.HIGHER_IS_BETTER:
        return existing if existing.limit >= offered.limit else offered
    return existing if existing.limit <= offered.limit else offered
