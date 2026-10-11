"""Naive baseline plan, used to show what the optimiser fixes and as the evaluation's 'simple plan'."""

from __future__ import annotations

import logging

from ai.tools.cost_model import build_options, to_assignment, unassigned
from shared.models import Dataset, OrderAssignment

log = logging.getLogger(__name__)


def cheapest_plant_plan(ds: Dataset) -> list[OrderAssignment]:
    """Naive: min landed cost per order, ignoring capacity and dates. Shows the flaws (capability level 2)
    and is the 'simple plan' the evaluation compares against.

    Each order goes to its cheapest eligible (plant, week) (ties: lower carbon, earlier week, plant name), so the
    plan can overload plants and ship late. validate_plan reports both.
    """
    opts = build_options(ds)
    best = {r.order_id: r for r in opts.sort_values(["order_id", "landed_cost", "carbon_kg", "week_start", "plant"])
            .drop_duplicates("order_id").itertuples(index=False)}
    plan = [to_assignment(best[str(o)]) if str(o) in best else unassigned(o, due)
            for o, due in zip(ds.orders["order_id"], ds.orders["due"])]
    log.info("cheapest_plant_plan orders=%d unassigned=%d", len(plan), sum(a.plant is None for a in plan))
    return plan
