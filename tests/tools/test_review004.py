"""REVIEW-004: Codex's two solver-correctness scenarios (avoidable lateness, avoidable deferral), exact inputs.

Both are OPTIMAL under the current weighted objective, so no search fix can change them: the objective must. Two
narrow objective changes each fixed its case but made the real 120-order plan worse, so they were reverted and the
cases stay here as strict xfails (they start failing loudly once a lexicographic two-stage solve fixes them).
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from ai.tools.solver import solve_allocation
from ai.tools.validator import validate_plan
from shared.models import Dataset, Weights

START = date(2026, 10, 5)  # Monday
ROUTE_COLS = ["plant", "destination", "transport_cost_per_unit", "transit_days", "duty_pct", "carbon_kg_per_unit"]
EVENT_COLS = ["event_id", "plant", "from_date", "to_date", "capacity_loss_pct"]


def _ds(orders: list[tuple], plants: list[tuple], routes: list[tuple]) -> Dataset:
    return Dataset(
        orders=pd.DataFrame(orders, columns=["order_id", "product", "qty", "destination", "due"]),
        plants=pd.DataFrame(plants, columns=["plant", "product", "weekly_capacity", "unit_cost", "energy_kwh_per_unit",
                                             "carbon_kg_per_kwh", "week"]),
        transport_duties=pd.DataFrame(routes, columns=ROUTE_COLS),
        capacity_events=pd.DataFrame(columns=EVENT_COLS), planning_start=START, data_version="review-004")


@pytest.mark.xfail(strict=True, reason="REVIEW-004 #1 known limitation (docs/solver.md section 11): a per-order "
                   "lateness floor fixed this case but made the 120-order plan worse (13 -> 14 late days alone, "
                   "19 with the deferral fix)")
def test_one_day_late_cheap_plant_loses_to_on_time_plant():
    """qty 1, due 10-13: A costs 10 and arrives 10-14 (1 day late); B costs 11 and arrives 10-13 (on time)."""
    ds = _ds(orders=[("O-1", "P1", 1, "X", date(2026, 10, 13))],
             plants=[("A", "P1", 1000, 10, 1, 0.5, "2026-10-05"), ("B", "P1", 1000, 11, 1, 0.5, "2026-10-05")],
             routes=[("A", "X", 0, 3, 0, 0), ("B", "X", 0, 2, 0, 0)])
    a = solve_allocation(ds, Weights()).assignments[0]
    assert (a.plant, a.on_time, a.cost) == ("B", True, 11)


@pytest.mark.xfail(strict=True, reason="REVIEW-004 #2 known limitation (docs/solver.md section 11): adding the plan's "
                   "cost/carbon spread to the defer penalty fixed this case but made the 120-order plan worse "
                   "(9 -> 12 late orders, gap 3.1% -> 11.5%)")
def test_feasible_order_is_not_deferred_to_save_cost():
    """Two 100-unit orders, two 100-unit slots: A (shared) makes P1 and P2 cheaply, B makes P1 expensively,
    P2 can only use A. The only full plan is P1 at B, P2 at A."""
    ds = _ds(orders=[("O-1", "P1", 100, "X", date(2026, 10, 20)), ("O-2", "P2", 100, "X", date(2026, 10, 20))],
             plants=[("A", "P1", 100, 10, 1, 0.5, "2026-10-05"), ("A", "P2", 100, 10, 1, 0.5, "2026-10-05"),
                     ("B", "P1", 100, 30, 1, 0.5, "2026-10-05")],
             routes=[("A", "X", 0, 2, 0, 0), ("B", "X", 0, 2, 0, 0)])
    res = solve_allocation(ds, Weights())
    plants = {a.order_id: a.plant for a in res.assignments}
    assert plants == {"O-1": "B", "O-2": "A"} and res.kpis.unassigned_orders == 0
    v = validate_plan(ds, res.assignments)
    assert v.capacity_ok and v.eligibility_ok and v.delivery_ok
