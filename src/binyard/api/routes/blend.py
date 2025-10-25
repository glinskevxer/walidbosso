"""Planning a blend over HTTP.

``POST /blends`` is a read that happens to take a body. It computes a draw list
and writes nothing, because the desk asks this question repeatedly while a
buyer is on the phone and none of those questions should move grain.

An infeasible blend is a 422 rather than a 400. The request was well formed and
the yard genuinely cannot make the spec, which is a different thing from a
typo, and a caller building a UI needs to tell them apart without reading
prose.
"""

from __future__ import annotations

from decimal import Decimal

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from binyard.api.app import work_for
from binyard.api.parsing import Reader, reader_for
from binyard.blend.planner import Objective, plan_blend
from binyard.blend.spec import BlendSpec, SpecLimit, sources_from
from binyard.errors import ValidationError
from binyard.grading.factors import Direction, FactorName, direction_of
from binyard.observability import metrics

__all__ = ["routes", "spec_from"]


def routes() -> list[Route]:
    return [Route("/blends", plan, methods=["POST"], name="plan_blend")]


def _limits_from(reader: Reader, name: str) -> tuple[SpecLimit, ...]:
    raw = reader._raw(name, {})  # noqa: SLF001 - the reader is ours
    if raw in (None, {}):
        return ()
    if not isinstance(raw, dict):
        raise ValidationError("expected an object of factor to limit", field=name)
    limits: list[SpecLimit] = []
    for key, value in raw.items():
        try:
            factor = FactorName(str(key))
        except ValueError as exc:
            raise ValidationError(
                "no such factor",
                field=f"{name}.{key}",
                known=[item.value for item in FactorName],
            ) from exc
        limits.append(SpecLimit(factor=factor, limit=Decimal(str(value))))
    return tuple(limits)


def spec_from(reader: Reader, commodity_code: str) -> BlendSpec:
    """Build the spec, letting explicit limits tighten a grade's."""
    quantity = reader.bushels("quantity")
    grade = reader._raw("grade", None)  # noqa: SLF001 - the reader is ours
    limits: dict[FactorName, SpecLimit] = {}
    if grade is not None:
        from_grade = BlendSpec.for_grade(commodity_code, quantity, int(grade))
        limits = {limit.factor: limit for limit in from_grade.limits}

    for limit in _limits_from(reader, "limits"):
        limits[limit.factor] = _tighter(limits.get(limit.factor), limit)

    if not limits:
        raise ValidationError("a blend needs a grade or at least one limit")
    return BlendSpec(
        commodity_code=commodity_code,
        quantity=quantity,
        minimum_grade=None if grade is None else int(grade),
        limits=tuple(limits[factor] for factor in sorted(limits, key=lambda f: f.value)),
        reject_worse_than=_limits_from(reader, "reject_worse_than"),
    )


async def plan(request: Request) -> JSONResponse:
    reader = reader_for(await request.json())
    standard = reader.commodity("commodity")
    spec = spec_from(reader, standard.code)
    objective = reader.choice("objective", Objective, Objective.SHIP_WORST_FIRST)
    if objective is Objective.EXPLICIT_COST:
        raise ValidationError("explicit costs are not offered over http")
    locked = frozenset(reader.strings("lock", []))
    minimum = reader.bushels(
        "minimum_draw", str(request.app.state.settings.minimum_bin_draw)
    )

    work = work_for(request)
    try:
        sources = sources_from(
            work.yard.load().states(),
            standard.code,
            minimum_draw=minimum,
            locked=locked,
        )
    finally:
        work.close()

    with metrics.timed("blend_seconds", commodity=standard.code):
        found = plan_blend(spec, sources, objective=objective)
    metrics.counter("blends_planned_total", commodity=standard.code)

    payload = found.describe()
    payload["spec"] = spec.describe()
    payload["offered"] = [source.describe() for source in sources]
    return JSONResponse(payload)


def _tighter(existing: SpecLimit | None, offered: SpecLimit) -> SpecLimit:
    if existing is None:
        return offered
    if direction_of(offered.factor) is Direction.HIGHER_IS_BETTER:
        return existing if existing.limit >= offered.limit else offered
    return existing if existing.limit <= offered.limit else offered
