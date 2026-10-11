"""Scenario tools built on the solver: trade-off frontier, plan diff and counterfactual 'why' answers.

All numbers come from solver runs; every PlanChange.reason is assembled by code from plan facts.
"""

from __future__ import annotations

import logging
import math
import os
from collections import defaultdict
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor

from ai.tools.cost_model import build_options
from ai.tools.solver import plan_objective, solve_allocation
from shared.models import (
    KPIs, CapacityChange, CounterfactualResult, Dataset, OrderAssignment, PlanChange, SolveResult, TradeoffPoint, Weights,
)

log = logging.getLogger(__name__)
OK_STATUSES = ("OPTIMAL", "FEASIBLE")
# A 'why' answer must isolate the knock-on of the forced order: every other order it moves has to be worth at least
# one late order-day, otherwise the heuristic search reshuffles unrelated orders and the diff becomes noise.
COUNTERFACTUAL_MOVE_PENALTY = 0.01


def _signed(x: float) -> str:
    return f"{round(x):+,d}"


# ============================================================ trade-off frontier (epsilon-constraint)
def _carbon_kg(r: SolveResult) -> float:
    return sum(a.carbon_kg for a in r.assignments)


def tradeoff_frontier(ds: Dataset, weights: Weights, cuts: Sequence[float] = (0, 5, 10, 15, 20, 30),
                      changes: Sequence[CapacityChange] = (), time_limit_s: float = 3.0) -> list[TradeoffPoint]:
    """Cost of cutting carbon by each percentage versus the unconstrained (0%) plan.

    For each cut c: re-solve with Σ carbon·x ≤ (1 - c/100) · carbon_0 (epsilon-constraint).
    * First a minimum-carbon plan (carbon only, lateness allowed, no deferral beyond the 0% plan) estimates the
      deepest achievable cut. Then every cut, and that deepest cut, is solved in parallel, warm-started from it.
    * The result ALWAYS contains the deepest feasible cut as an explicit point: the largest cut any of these plans
      reaches, rounded down to 0.1%. Read it as the feasible point with the largest carbon_cut_pct (a requested
      cut between the rounded and the exact value is feasible too). Every point beyond it has feasible=False, so
      the answer can say "a 10% cut is not achievable; the best is X% at +Y cost and Z late orders".
    * A cut is also infeasible if it would leave more orders unassigned than the 0% plan.
    * Monotone by construction: a plan that meets a deeper cut also meets every shallower one, so each point keeps
      the best plan (solver objective) among its own solve and every deeper feasible solve.
    implied_carbon_price_per_t = (cost_c - cost_0) / (carbon_0_t - carbon_c_t).
    """
    wanted = sorted({float(c) for c in cuts})
    base = solve_allocation(ds, weights, changes)  # same call as the graph's allocate step, so usually cached
    if base.status not in OK_STATUSES or base.kpis is None:
        log.warning("tradeoff_frontier: base solve %s", base.status)
        return [TradeoffPoint(carbon_cut_pct=c, cost=0.0, carbon_t=0.0, late_orders=0, feasible=False)
                for c in wanted]
    k0_kg = _carbon_kg(base)
    max_unassigned = base.kpis.unassigned_orders
    tol_kg = 0.01 * len(base.assignments) + 1e-6  # assignments carry carbon rounded to 0.01 kg
    green = Weights(cost=0.0001, carbon=1, lateness=0)  # deepest cut: lateness allowed, deferral still penalised

    def ok(r: SolveResult | None, cap_kg: float) -> bool:
        return (r is not None and r.status in OK_STATUSES and r.kpis is not None
                and r.kpis.unassigned_orders <= max_unassigned and _carbon_kg(r) <= cap_kg + tol_kg)

    def capped(cut: float, hint: list[OrderAssignment] | None = None) -> SolveResult:
        return solve_allocation(ds, weights, changes, carbon_cap_kg=k0_kg * (1 - cut / 100),
                                time_limit_s=time_limit_s, hint=hint, portfolio=hint is None)

    min_carbon = solve_allocation(ds, green, changes, time_limit_s=time_limit_s)
    k_min_kg = _carbon_kg(min_carbon) if ok(min_carbon, float("inf")) else None
    hint = min_carbon.assignments if k_min_kg is not None else None
    todo = [c for c in wanted if c > 0]
    if k_min_kg is not None and k0_kg > 0:
        max_cut = math.floor((1 - k_min_kg / k0_kg) * 1000 + 1e-9) / 10
        if max_cut > 0 and max_cut not in todo:
            todo.append(max_cut)
    results: dict[float, SolveResult | None] = {0.0: base}
    with ThreadPoolExecutor(max_workers=max(1, min(len(todo), os.cpu_count() or 1, 8))) as pool:
        futures = {c: pool.submit(capped, c, hint) for c in todo}
        results.update({c: f.result() for c, f in futures.items()})
    candidates = [r for r in [*results.values(), min_carbon] if r is not None]
    reached = [(1 - _carbon_kg(r) / k0_kg) * 100 for r in candidates if ok(r, float("inf"))] if k0_kg > 0 else []
    deepest = max(0.0, math.floor(max(reached, default=0.0) * 10 + 1e-9) / 10)
    if deepest > 0:
        results.setdefault(deepest, None)

    objective = {id(r): plan_objective(ds, r.assignments, weights) for r in candidates if r.kpis is not None}
    best: dict[float, KPIs | None] = {}
    for cut in sorted(results):
        feasible = [r for r in candidates if ok(r, k0_kg * (1 - cut / 100))]
        best[cut] = min(feasible, key=lambda r: objective[id(r)]).kpis if feasible else None
    ref = best[0.0] or base.kpis
    points: list[TradeoffPoint] = []
    for cut, k in best.items():
        if k is None:
            points.append(TradeoffPoint(carbon_cut_pct=cut, cost=0.0, carbon_t=round(k0_kg * (1 - cut / 100) / 1000, 3),
                                        late_orders=0, feasible=False))
            continue
        d_carbon = ref.total_carbon_t - k.total_carbon_t
        price = round((k.total_cost - ref.total_cost) / d_carbon, 2) if cut > 0 and d_carbon > 1e-9 else None
        points.append(TradeoffPoint(carbon_cut_pct=cut, cost=k.total_cost, carbon_t=k.total_carbon_t,
                                    late_orders=k.late_orders, implied_carbon_price_per_t=price))
    if 0.0 not in wanted:
        points = [p for p in points if p.carbon_cut_pct != 0]
    log.info("tradeoff_frontier points=%d feasible=%d deepest_cut=%.1f%%", len(points),
             sum(p.feasible for p in points), deepest)
    return points


# ============================================================ plan diff
def _week_loads(plan: Sequence[OrderAssignment], qty: dict[str, float]) -> dict[tuple, float]:
    load: dict[tuple, float] = defaultdict(float)
    for a in plan:
        if a.plant is not None:
            load[(a.plant, a.production_week)] += qty.get(a.order_id, 0.0)
    return load


def compare_plans(ds: Dataset, base: SolveResult, new: SolveResult, changes: Sequence[CapacityChange] = (),
                  forced: dict[str, str] | None = None) -> list[PlanChange]:
    """Orders whose plant changed (or whose on-time status changed) between two plans, with a code-built reason."""
    qty = dict(zip(ds.orders["order_id"].astype(str), ds.orders["qty"].astype(float)))
    base_by = {a.order_id: a for a in base.assignments}
    base_load, new_load = _week_loads(base.assignments, qty), _week_loads(new.assignments, qty)
    lost = defaultdict(list)
    for c in changes:
        span = "" if c.from_date is None and c.to_date is None else f" ({c.from_date or '...'} to {c.to_date or '...'})"
        lost[c.plant].append(f"Plant {c.plant} loses {c.capacity_loss_pct:g}% capacity{span}.")
    forced = forced or {}

    out: list[PlanChange] = []
    for n in new.assignments:
        b = base_by.get(n.order_id)
        if b is None or (b.plant == n.plant and b.on_time == n.on_time):
            continue
        parts: list[str] = []
        if n.order_id in forced:
            parts.append(f"{n.order_id} is forced to plant {forced[n.order_id]}.")
        if b.plant is not None and b.plant != n.plant:
            parts += lost.get(b.plant, [])
            key = (b.plant, b.production_week)
            parts.append(f"Plant {b.plant} load in week {b.production_week}: "
                         f"{base_load.get(key, 0):,.0f} -> {new_load.get(key, 0):,.0f} units.")
        if n.plant is None:
            parts.append("No eligible plant has capacity left for it in the new plan.")
        elif b.plant is None:
            parts.append(f"Capacity is now available at plant {n.plant}.")
        elif b.plant == n.plant:
            parts.append(f"Same plant {n.plant}, production week {b.production_week} -> {n.production_week}.")
        cost_d, carbon_d = n.cost - b.cost, n.carbon_kg - b.carbon_kg
        if n.plant is not None and b.plant is not None:
            parts.append(f"Order cost {_signed(cost_d)}, carbon {round(carbon_d):+,d} kg.")
        if b.on_time and not n.on_time and n.plant is not None:
            parts.append(f"Now {n.days_late} day{'s' if n.days_late != 1 else ''} late.")
        elif not b.on_time and n.on_time and b.plant is not None:
            parts.append("Now on time.")
        out.append(PlanChange(order_id=n.order_id, from_plant=b.plant, to_plant=n.plant, cost_delta=round(cost_d, 2),
                              carbon_delta_kg=round(carbon_d, 2), reason=" ".join(parts)))
    log.info("compare_plans changes=%d", len(out))
    return out


# ============================================================ counterfactual
def counterfactual(ds: Dataset, base: SolveResult, order_id: str, plant: str,
                   changes: Sequence[CapacityChange] = (), time_limit_s: float = 3.0) -> CounterfactualResult:
    """'What if order_id went to plant instead?' Re-solves the WHOLE plan with forced={order_id: plant}.
    The forced solve starts from the base plan and charges COUNTERFACTUAL_MOVE_PENALTY per other order moved, so
    displaced_orders are the orders the force really pushes out, not unrelated reshuffles.

    blocked_by:
      eligibility  the plant cannot make the product or ship to the destination
      capacity     no feasible plan puts the order there, or doing so leaves other orders unproduced
      delivery     the plant can only deliver this order late while the current plan delivers it on time
    """
    base_a = next((a for a in base.assignments if a.order_id == order_id), None)
    chosen = base_a.plant if base_a else None
    res = CounterfactualResult(order_id=order_id, chosen_plant=chosen, alternative_plant=plant, feasible=False)
    opts = build_options(ds)
    mine = opts[(opts["order_id"] == order_id) & (opts["plant"] == plant)]
    if mine.empty:
        res.blocked_by = "eligibility"
        return res

    new = solve_allocation(ds, base.weights_used, changes, forced={order_id: plant}, base_plan=base.assignments,
                           time_limit_s=time_limit_s, portfolio=False, move_penalty=COUNTERFACTUAL_MOVE_PENALTY)
    base_unassigned = base.kpis.unassigned_orders if base.kpis else 0
    if new.status not in OK_STATUSES or new.kpis is None or new.kpis.unassigned_orders > base_unassigned:
        res.blocked_by = "capacity"
        if new.kpis is None:
            res.order_cost_delta = round(float(mine["landed_cost"].min()) - (base_a.cost if base_a else 0.0), 2)
            return res

    new_a = next(a for a in new.assignments if a.order_id == order_id)
    res.order_cost_delta = round(new_a.cost - (base_a.cost if base_a else 0.0), 2)
    if base.kpis is not None:
        res.plan_cost_delta = round(new.kpis.total_cost - base.kpis.total_cost, 2)
        res.plan_carbon_delta_kg = round((new.kpis.total_carbon_t - base.kpis.total_carbon_t) * 1000, 2)
    base_on_time = {a.order_id: a.on_time for a in base.assignments}
    res.new_late_orders = [a.order_id for a in new.assignments
                           if a.plant is not None and not a.on_time and base_on_time.get(a.order_id, False)]
    res.displaced_orders = [c for c in compare_plans(ds, base, new, changes, forced={order_id: plant})
                            if c.order_id != order_id]
    if res.blocked_by is None:
        if base_a is not None and base_a.on_time and not bool(mine["on_time"].any()):
            res.blocked_by = "delivery"
        else:
            res.feasible = True
    log.info("counterfactual %s -> %s feasible=%s blocked_by=%s plan_cost_delta=%.0f displaced=%d",
             order_id, plant, res.feasible, res.blocked_by, res.plan_cost_delta, len(res.displaced_orders))
    return res


# ============================================================ evaluation headline
def share_not_at_cheapest_plant(ds: Dataset, plan: Sequence[OrderAssignment]) -> float:
    """Share of assigned orders NOT at their cheapest eligible plant (by landed cost, any week).

    These are the orders where a per-order 'it was cheapest' explanation would be wrong.
    """
    opts = build_options(ds)
    if opts.empty:
        return 0.0
    cheapest = opts.groupby("order_id")["landed_cost"].min()
    best_plants = {o: set(g.loc[g["landed_cost"] <= cheapest[o] + 1e-6, "plant"]) for o, g in opts.groupby("order_id")}
    assigned = [a for a in plan if a.plant is not None]
    if not assigned:
        return 0.0
    return round(sum(a.plant not in best_plants.get(a.order_id, set()) for a in assigned) / len(assigned), 4)
