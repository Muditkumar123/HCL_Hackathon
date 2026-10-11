"""Re-plan quality and speed (from Shivang's eval: 'Plant B loses 30%' took late orders 12 -> 67).

The invariant: deferring exactly the orders a capacity loss pushes off its plant is always feasible, so a re-plan
must (a) score no worse than that repair on the solver objective and (b) carry no more lateness than it, measured in
late order-days with a deferred order counted as DEFER_DAYS. A bound on the NUMBER of late orders is not an
invariant: the objective prefers several orders a day or two late over one order deferred for weeks (on the real
pack, a 30 s full search after Shivang's E-3 event has 22 late orders vs 8 before, although E-3 forces only 6 off).
"""

from __future__ import annotations

import time
from collections import defaultdict
from pathlib import Path

import pandas as pd
import pytest
from conftest import synthetic_ds

from ai.tools.cost_model import capacity_key, capacity_modes, effective_capacity, unassigned
from ai.tools.scenarios import tradeoff_frontier
from ai.tools.solver import clear_cache, plan_objective, solve_allocation
from ai.tools.validator import validate_plan
from shared.models import CapacityChange, Dataset, Weights

RAW = Path(__file__).resolve().parents[2] / "data" / "raw"
DEFER_DAYS = 12  # one deferral = one more week late + the 0.05 margin (5 late days), see solver.defer


def forced_off(ds: Dataset, plan, changes) -> set[str]:
    """Fewest orders of `plan` that must leave their slot so every (plant, week) fits the reduced capacity."""
    qty = dict(zip(ds.orders["order_id"], ds.orders["qty"]))
    product = dict(zip(ds.orders["order_id"], ds.orders["product"]))
    modes = capacity_modes(ds)
    cap = {(r.plant, r.product, r.week_start): r.capacity for r in effective_capacity(ds, changes).itertuples()}
    slots = defaultdict(list)
    for a in plan:
        if a.plant is not None:
            slots[capacity_key(a.plant, product[a.order_id], a.production_week, modes)].append(a.order_id)
    out = set()
    for key, oids in slots.items():
        load = sum(qty[o] for o in oids)
        for o in sorted(oids, key=lambda o: -qty[o]):  # largest first = fewest orders
            if load <= cap.get(key, 0) + 1e-9:
                break
            out.add(o)
            load -= qty[o]
    return out


def late_or_deferred(plan) -> int:
    return sum(1 for a in plan if not a.on_time)


def lateness_burden(plan) -> int:
    """Late order-days, a deferred order counting DEFER_DAYS."""
    return sum(DEFER_DAYS if a.plant is None else a.days_late for a in plan)


def check_replan(ds: Dataset, changes, weights=Weights()):
    base = solve_allocation(ds, weights)
    new = solve_allocation(ds, weights, changes=changes, base_plan=base.assignments)
    off = forced_off(ds, base.assignments, changes)
    repair = [unassigned(a.order_id, a.due) if a.order_id in off else a for a in base.assignments]
    assert validate_plan(ds, new.assignments, changes).capacity_ok
    assert plan_objective(ds, new.assignments, weights) <= plan_objective(ds, repair, weights) + 1e-6
    assert lateness_burden(new.assignments) <= lateness_burden(repair), (
        f"re-plan carries {lateness_burden(new.assignments)} late order-days; deferring the {len(off)} orders the "
        f"loss forces off would carry {lateness_burden(repair)}")
    return base, new, off


def test_capacity_loss_without_overflow_adds_no_lateness(trap_ds):
    base, new, off = check_replan(trap_ds, [CapacityChange(plant="A", capacity_loss_pct=30)])
    assert not off and late_or_deferred(new.assignments) == late_or_deferred(base.assignments)


def test_capacity_loss_lateness_bounded_trap(trap_ds):
    _, _, off = check_replan(trap_ds, [CapacityChange(plant="A", capacity_loss_pct=70)])
    assert off


def test_capacity_loss_lateness_bounded_synthetic():
    check_replan(synthetic_ds(), [CapacityChange(plant="B", capacity_loss_pct=30)])


def test_lateness_outweighs_cost_savings(trap_ds):
    # make A's late route to US 13x cheaper per unit: still not worth 16 late days
    trap_ds.plants.loc[(trap_ds.plants["plant"] == "A") & (trap_ds.plants["product"] == "P1"), "unit_cost"] = 1
    o1 = next(a for a in solve_allocation(trap_ds, Weights()).assignments if a.order_id == "O-1")
    assert o1.plant != "A" and o1.on_time


def test_minimal_change_penalty_never_beats_lateness(trap_ds):
    ch = [CapacityChange(plant="A", capacity_loss_pct=70)]
    base = solve_allocation(trap_ds, Weights())
    fresh = solve_allocation(trap_ds, Weights(), changes=ch)
    sticky = solve_allocation(trap_ds, Weights(), changes=ch, base_plan=base.assignments)
    days = lambda r: sum(a.days_late for a in r.assignments)  # noqa: E731
    assert days(sticky) <= days(fresh)
    assert sticky.kpis.unassigned_orders <= fresh.kpis.unassigned_orders


def test_scenario_change_only_hits_its_weeks(trap_ds):
    ch = [CapacityChange(plant="B", capacity_loss_pct=50, from_date="2026-10-12", to_date="2026-10-18")]
    cap = effective_capacity(trap_ds, ch)
    b = cap[(cap["plant"] == "B") & (cap["product"] == "P1")].set_index("week_start")["capacity"]
    assert list(b) == [800, 400, 800][: len(b)]


@pytest.mark.skipif(not (RAW / "orders.csv").exists(), reason="data/raw pack not present")
def test_real_pack_speed_and_replan():
    from backend.store import planning_start

    f = {t: pd.read_csv(RAW / f"{t}.csv") for t in ("orders", "plants", "transport_duties", "capacity_events")}
    ds = Dataset(**f, planning_start=planning_start(f["orders"], f["plants"]), data_version="raw")
    clear_cache()
    t0 = time.perf_counter()
    base = solve_allocation(ds, Weights())
    assert time.perf_counter() - t0 < 3, "main plan must take < 3 s"
    assert validate_plan(ds, base.assignments).capacity_ok
    t0 = time.perf_counter()
    tradeoff_frontier(ds, Weights())
    assert time.perf_counter() - t0 < 10, "trade-off must take < 10 s"
    check_replan(ds, [CapacityChange(plant="B", capacity_loss_pct=30)])
    e3 = [CapacityChange(plant="E", capacity_loss_pct=50, from_date="2026-10-12", to_date="2026-10-25")]
    _, new, _ = check_replan(ds, e3)  # Shivang's late-drop dry run
    fresh = solve_allocation(ds, Weights(), changes=e3)
    assert plan_objective(ds, new.assignments, Weights()) <= plan_objective(ds, fresh.assignments, Weights()) + 1e-6
