"""Tests for ai/tools/scenarios.py and the end-to-end fit with ai/graph.py."""

from __future__ import annotations

import os
import time

from ai.tools.baseline import cheapest_plant_plan
from ai.tools.scenarios import compare_plans, counterfactual, share_not_at_cheapest_plant, tradeoff_frontier
from ai.tools.solver import solve_allocation
from ai.tools.validator import validate_plan
from shared.models import Weights


def test_tradeoff_frontier_is_monotone(trap_ds):
    pts = tradeoff_frontier(trap_ds, Weights(cost=1, carbon=0), cuts=(0, 5, 10, 20))
    cuts = [p.carbon_cut_pct for p in pts]
    assert cuts == sorted(cuts) and {0, 5, 10, 20} <= set(cuts)
    assert pts[0].implied_carbon_price_per_t is None
    feas = [p for p in pts if p.feasible]
    assert all(a.cost <= b.cost + 1e-6 and a.carbon_t >= b.carbon_t - 1e-9 for a, b in zip(feas, feas[1:]))
    for p in feas[1:]:
        assert p.carbon_t <= pts[0].carbon_t * (1 - p.carbon_cut_pct / 100) + 1e-6
        if p.implied_carbon_price_per_t is not None:
            expect = (p.cost - pts[0].cost) / (pts[0].carbon_t - p.carbon_t)
            assert abs(p.implied_carbon_price_per_t - expect) < 0.01


def test_tradeoff_infeasible_cut_is_flagged(trap_ds):
    pts = tradeoff_frontier(trap_ds, Weights(), cuts=(0, 90))
    by_cut = {p.carbon_cut_pct: p for p in pts}
    assert by_cut[90].feasible is False
    extra = [p for p in pts if p.carbon_cut_pct not in (0, 90)]  # deepest achievable cut is added
    assert len(extra) == 1 and extra[0].feasible and 0 < extra[0].carbon_cut_pct < 90
    assert extra[0].carbon_t <= by_cut[0].carbon_t * (1 - extra[0].carbon_cut_pct / 100) + 1e-6


def test_frontier_always_has_deepest_feasible_point(trap_ds):
    pts = tradeoff_frontier(trap_ds, Weights(), cuts=(0, 1, 50))
    feasible = [p for p in pts if p.feasible]
    deepest = max(feasible, key=lambda p: p.carbon_cut_pct)
    assert deepest.carbon_cut_pct not in (0, 1, 50)          # added even though nobody asked for it
    assert 1 < deepest.carbon_cut_pct < 50
    assert all(not p.feasible for p in pts if p.carbon_cut_pct > deepest.carbon_cut_pct)
    assert deepest.carbon_t <= pts[0].carbon_t * (1 - deepest.carbon_cut_pct / 100) + 1e-6
    assert deepest.implied_carbon_price_per_t is not None


def test_counterfactual_eligibility_and_knock_on(trap_ds):
    base = solve_allocation(trap_ds, Weights())
    cf = counterfactual(trap_ds, base, "O-2", "A")
    assert not cf.feasible and cf.blocked_by == "eligibility"

    # O-1 is in C (cheap, low carbon, tiny). Forcing O-1 to B is feasible but costs carbon.
    cf_b = counterfactual(trap_ds, base, "O-1", "B")
    assert cf_b.feasible and cf_b.chosen_plant == "C"
    assert cf_b.plan_carbon_delta_kg > 0
    assert cf_b.plan_cost_delta == cf_b.order_cost_delta  # nothing else had to move


def test_counterfactual_delivery_block(trap_ds):
    base = solve_allocation(trap_ds, Weights())
    cf = counterfactual(trap_ds, base, "O-1", "A")  # A -> US takes 25 days: always late
    assert not cf.feasible and cf.blocked_by == "delivery"
    assert cf.order_cost_delta < 0 and "O-1" in cf.new_late_orders  # cheaper for the order, but late


def test_counterfactual_capacity_block(trap_ds):
    trap_ds.orders.loc[trap_ds.orders["order_id"] == "O-6", "qty"] = 900  # bigger than C's 150/week
    base = solve_allocation(trap_ds, Weights())
    cf = counterfactual(trap_ds, base, "O-6", "C")
    assert not cf.feasible and cf.blocked_by == "capacity"


def test_counterfactual_displaces_other_orders(trap_ds):
    # A's only on-time weeks for DE are 1 and 2; week 1 is full with the FR orders (O-3 200 + O-6 500).
    trap_ds.orders["qty"] = trap_ds.orders["order_id"].map({"O-4": 500, "O-5": 550}).fillna(trap_ds.orders["qty"])
    base = solve_allocation(trap_ds, Weights())
    assert {a.order_id: a.plant for a in base.assignments}["O-4"] == "B"
    cf = counterfactual(trap_ds, base, "O-4", "A")  # A has no room for 500 + 550 in week 2
    assert cf.feasible
    # cheapest knock-on under default weights: push O-6 to week 2 (1 day late) rather than move an order to B
    assert cf.new_late_orders == ["O-6"]
    assert [(d.order_id, d.from_plant, d.to_plant) for d in cf.displaced_orders] == [("O-6", "A", "A")]
    assert "Now 1 day late" in cf.displaced_orders[0].reason
    lateness_first = counterfactual(trap_ds, solve_allocation(trap_ds, Weights(lateness=100)), "O-4", "A")
    # with lateness expensive, the cheapest way to make room is O-3 (200 units, FR) A -> B: +300
    assert {d.order_id: (d.from_plant, d.to_plant) for d in lateness_first.displaced_orders} == {"O-3": ("A", "B")}
    assert lateness_first.plan_cost_delta == lateness_first.order_cost_delta + 300  # knock-on the local view misses
    assert not lateness_first.new_late_orders
    assert "Plant A load in week 2026-10-05" in lateness_first.displaced_orders[0].reason


def test_compare_plans_reason_is_built_from_facts(trap_ds):
    base = solve_allocation(trap_ds, Weights())
    new = solve_allocation(trap_ds, Weights(), forced={"O-6": "B"}, base_plan=base.assignments)
    diff = compare_plans(trap_ds, base, new, forced={"O-6": "B"})
    o6 = next(c for c in diff if c.order_id == "O-6")
    assert o6.from_plant == "A" and o6.to_plant == "B"
    assert "forced to plant B" in o6.reason and "Order cost +" in o6.reason


def test_share_not_at_cheapest_plant(trap_ds):
    res = solve_allocation(trap_ds, Weights())
    share = share_not_at_cheapest_plant(trap_ds, res.assignments)
    assert 0 < share < 1          # O-1 is not at its cheapest plant (A is late)
    assert share_not_at_cheapest_plant(trap_ds, cheapest_plant_plan(trap_ds)) == 0


def test_synthetic_pack_under_10s(big_ds):
    t0 = time.perf_counter()
    res = solve_allocation(big_ds, Weights())
    elapsed = time.perf_counter() - t0
    assert res.status in ("OPTIMAL", "FEASIBLE") and elapsed < 10
    if res.status == "FEASIBLE":  # large model: root-node stop, the gap to the lower bound is reported
        assert any("proven lower bound" in n for n in res.notes)
    v = validate_plan(big_ds, res.assignments)
    assert v.capacity_ok and v.eligibility_ok, v.violations[:5]


def test_graph_end_to_end(trap_ds, monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "true")
    from ai.graph import default_deps, run
    from shared.models import AnalyzeRequest, AnswerType

    deps = default_deps()
    deps.base_plan = lambda: None  # don't read whatever plan the backend DB has stored
    cut_ok = next(p.feasible for p in tradeoff_frontier(trap_ds, Weights(), cuts=(0, 10)) if p.carbon_cut_pct == 10)
    for text, expected in [("Allocate this week's orders", {AnswerType.ALLOCATION}),
                           # a cut beyond the deepest feasible one may be answered as INFEASIBLE (graph's choice)
                           ("What does a 10% carbon cut cost?",
                            {AnswerType.SCENARIO} if cut_ok else {AnswerType.SCENARIO, AnswerType.INFEASIBLE}),
                           ("Plant A loses 30% capacity. Re-plan.", {AnswerType.SCENARIO}),
                           ("Why was order O-1 built in Plant C?", {AnswerType.SCENARIO})]:
        resp, trace = run(AnalyzeRequest(request=text), trap_ds, deps)
        assert resp.answer_type in expected, (text, resp.summary, resp.limitations)
        assert resp.validator is not None and resp.validator.capacity_ok, (text, resp.validator)
        assert os.environ["MOCK_LLM"] == "true" and trace.steps
