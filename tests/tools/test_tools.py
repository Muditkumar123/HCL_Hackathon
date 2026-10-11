"""Tests for ai/tools: cost model, solver, validator, baseline. Run: python -m pytest tests/tools -q"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

import pandas as pd
import pytest

from ai.tools.baseline import cheapest_plant_plan
from ai.tools.cost_model import assumptions_for, build_options, capacity_modes, effective_capacity
from ai.tools.solver import solve_allocation
from ai.tools.validator import validate_plan
from shared.models import CapacityChange, OrderAssignment, Weights


def plants_of(res) -> dict[str, str | None]:
    return {a.order_id: a.plant for a in res.assignments}


# ---------------------------------------------------------------- cost model
def test_landed_cost_and_carbon_formulas(trap_ds):
    o = build_options(trap_ds).set_index(["order_id", "plant", "week_start"])
    row = o.loc[("O-3", "B", date(2026, 10, 5))]
    assert row.landed_cost == pytest.approx(200 * 15 * 1.40 + 200 * 1.5)  # duty on ex-works value only
    assert row.carbon_kg == pytest.approx(200 * (2 * 0.6 + 0.1))
    assert row.arrival == date(2026, 10, 14)  # Mon + 6 + 3 transit


def test_duty_flips_cheapest_plant(trap_ds):
    o = build_options(trap_ds).groupby(["order_id", "plant"])["unit_landed_cost"].min()
    assert o[("O-3", "A")] < o[("O-3", "B")]   # FR: B's 40% duty makes A cheaper
    assert o[("O-4", "B")] < o[("O-4", "A")]   # DE: no duty, B's lower unit cost wins


def test_only_eligible_options(trap_ds):
    o = build_options(trap_ds)
    assert set(o.loc[o["order_id"] == "O-2", "plant"]) == {"C"}
    assert set(o.loc[o["order_id"] == "O-3", "plant"]) == {"A", "B"}


def test_capacity_modes_and_assumptions(trap_ds):
    assert capacity_modes(trap_ds) == {"A": "shared", "B": "per_product", "C": "shared"}
    text = " ".join(assumptions_for(trap_ds))
    assert "shared capacity for plant(s) A, C" in text and "per-product capacity for plant(s) B" in text


def test_effective_capacity_applies_events_and_changes(trap_ds):
    trap_ds.capacity_events = pd.DataFrame([("E-1", "A", "2026-10-12", "2026-10-14", 50)],
                                           columns=["event_id", "plant", "from_date", "to_date", "capacity_loss_pct"])
    cap = effective_capacity(trap_ds, [CapacityChange(plant="A", capacity_loss_pct=0.2)])  # 0.2 -> 20%
    a = cap[cap["plant"] == "A"].set_index("week_start")["capacity"]
    assert a[date(2026, 10, 5)] == pytest.approx(800)
    assert a[date(2026, 10, 12)] == pytest.approx(1000 * 0.5 * 0.8)


def test_week_column_varies_carbon_intensity(trap_ds):
    p = trap_ds.plants
    trap_ds.plants = pd.concat([p.assign(week=1), p.assign(week=2, carbon_kg_per_kwh=p["carbon_kg_per_kwh"] * 2)])
    o = build_options(trap_ds)
    c = o[(o["order_id"] == "O-5") & (o["plant"] == "A")].set_index("week_start")["carbon_kg"]
    assert list(c.index) == [date(2026, 10, 5), date(2026, 10, 12)]
    assert c.iloc[1] == pytest.approx(400 * (2 * 1.0 + 0.1))


# ---------------------------------------------------------------- solver
def test_cheapest_late_goes_elsewhere(trap_ds):
    res = solve_allocation(trap_ds, Weights())
    assert res.status == "OPTIMAL"
    o1 = next(a for a in res.assignments if a.order_id == "O-1")
    assert o1.plant in ("B", "C") and o1.on_time
    assert cheapest_plant_plan(trap_ds)[0].plant == "A"  # the naive plan falls into the trap


def test_only_eligible_plant(trap_ds):
    res = solve_allocation(trap_ds, Weights())
    assert plants_of(res)["O-2"] == "C"
    forced = solve_allocation(trap_ds, Weights(), forced={"O-2": "A"})
    assert forced.status == "INFEASIBLE" and "eligibility" in forced.notes[0]


def test_capacity_respected(trap_ds):
    res = solve_allocation(trap_ds, Weights())
    qty = dict(zip(trap_ds.orders["order_id"], trap_ds.orders["qty"]))
    load: dict[tuple, float] = defaultdict(float)
    for a in res.assignments:
        load[(a.plant, a.production_week)] += qty[a.order_id]
    assert all(q <= 1000 for (p, _), q in load.items() if p == "A")
    assert all(q <= 150 for (p, _), q in load.items() if p == "C")
    v = validate_plan(trap_ds, res.assignments)
    assert v.capacity_ok and v.eligibility_ok and v.delivery_ok, v.violations


def test_capacity_loss_moves_orders(trap_ds):
    from ai.tools.scenarios import compare_plans

    base = solve_allocation(trap_ds, Weights())
    change = [CapacityChange(plant="A", capacity_loss_pct=70)]
    new = solve_allocation(trap_ds, Weights(), changes=change, base_plan=base.assignments)
    diff = compare_plans(trap_ds, base, new, change)
    assert any(c.from_plant == "A" and c.to_plant != "A" for c in diff)
    assert all("Plant A loses 70% capacity" in c.reason for c in diff if c.from_plant == "A")
    assert any(a.changed for a in new.assignments)
    assert validate_plan(trap_ds, new.assignments, change).capacity_ok
    assert not validate_plan(trap_ds, base.assignments, change).capacity_ok  # old plan breaks the new limit


def test_unassigned_when_capacity_short(trap_ds):
    res = solve_allocation(trap_ds, Weights(), changes=[CapacityChange(plant="C", capacity_loss_pct=100)])
    assert plants_of(res)["O-2"] is None
    assert res.kpis.unassigned_orders == 1 and any("cannot be produced" in n for n in res.notes)


def test_require_all_on_time_and_lateness(trap_ds):
    trap_ds.orders.loc[trap_ds.orders["order_id"] == "O-1", "due"] = date(2026, 10, 12)  # nobody reaches US by then
    res = solve_allocation(trap_ds, Weights(), require_all_on_time=True)
    assert res.status == "OPTIMAL" and any("no on-time option" in n for n in res.notes)
    assert res.kpis.late_orders == 1


def test_carbon_cap_and_forced(trap_ds):
    base = solve_allocation(trap_ds, Weights(cost=1, carbon=0))
    capped = solve_allocation(trap_ds, Weights(cost=1, carbon=0), carbon_cap_kg=base.kpis.total_carbon_t * 900)
    assert capped.kpis.total_carbon_t <= base.kpis.total_carbon_t * 0.9 + 1e-9
    assert capped.kpis.total_cost >= base.kpis.total_cost
    forced = solve_allocation(trap_ds, Weights(), forced={"O-6": "B"})
    assert plants_of(forced)["O-6"] == "B"


def test_solver_never_raises(trap_ds):
    trap_ds.plants = trap_ds.plants.drop(columns=["unit_cost"])
    res = solve_allocation(trap_ds, Weights())
    assert res.status == "ERROR" and res.notes


# ---------------------------------------------------------------- validator
def test_validator_catches_overload(trap_ds):
    good = solve_allocation(trap_ds, Weights()).assignments
    wk1 = date(2026, 10, 5)
    o5 = next(a for a in good if a.order_id == "O-5")
    bad = [a if a.order_id != "O-5" else a.model_copy(update={"production_week": wk1, "arrival": date(2026, 10, 13)})
           for a in good]
    v = validate_plan(trap_ds, bad)
    assert o5.production_week != wk1
    assert not v.capacity_ok and "plant A week 2026-10-05" in v.violations[0]


def test_validator_catches_bad_numbers_and_coverage(trap_ds):
    plan = solve_allocation(trap_ds, Weights()).assignments
    lied = [plan[0].model_copy(update={"cost": plan[0].cost - 100})] + plan[1:4] + [plan[3]]
    v = validate_plan(trap_ds, lied)
    text = " | ".join(v.violations)
    assert not v.eligibility_ok
    assert "does not match recomputed" in text and "appears 2 times" in text and "missing" in text


def test_validator_catches_ineligible_and_late(trap_ds):
    a = OrderAssignment(order_id="O-2", plant="A", production_week=date(2026, 10, 5), cost=1, carbon_kg=1,
                        due=date(2026, 10, 20), on_time=True)
    v = validate_plan(trap_ds, [a])
    assert not v.eligibility_ok and any("cannot make P3" in x for x in v.violations)
    base = cheapest_plant_plan(trap_ds)
    v2 = validate_plan(trap_ds, base)
    assert not v2.delivery_ok and any("O-1 arrives" in x for x in v2.violations)
