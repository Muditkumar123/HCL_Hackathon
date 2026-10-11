"""Development stand-ins for Ayush's ai/tools (solver, validator, scenarios). Switched on by USE_TOOL_STUBS=true.

Same names and signatures as ai/tools/{solver,validator,scenarios}.py, so ai.graph.default_deps() can swap them in
and the whole flow runs before the real solver lands. Every number is computed from the Dataset with the TASK-001
cost model (landed cost, carbon, arrival); nothing is hard-coded. The allocation is a greedy heuristic, not the MIP,
so it is never labelled OPTIMAL and every result carries STUB_NOTE. Must be OFF for the demo (CLAUDE.md rule 2).

Cost model (same formulas as TASK-001):
  prod_value  = qty * (unit_cost + energy_kwh_per_unit * energy_price_per_kwh)   (energy price 0 if no column)
  landed_cost = prod_value * (1 + duty_pct/100) + qty * transport_cost_per_unit
  carbon_kg   = qty * (energy_kwh_per_unit * carbon_kg_per_kwh + carbon_kg_per_unit)
  arrival     = week_start + 6 days + transit_days;  days_late = max(0, arrival - due)
Greedy: forced orders first, then by due date and regret (best vs second-best score); each order takes its
lowest-score option that still has capacity, where score is the per-option term of the MIP objective.
A carbon cap is met by a repair pass that makes the cheapest moves per kg of carbon saved.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pandas as pd

from shared.models import (
    CapacityChange, CounterfactualResult, Dataset, KPIs, OrderAssignment, PlanChange, SolveResult, TradeoffPoint,
    ValidatorResult, Weights,
)

log = logging.getLogger(__name__)

STUB_NOTE = "Development stub (USE_TOOL_STUBS=true): greedy heuristic, not the MIP solver. Turn off for the demo."
MAX_WEEKS = 8
TOL = 0.01
CapKey = tuple[str, str | None, date]   # (plant, product or None when capacity is shared, week_start)


# ============================================================ cost model
@dataclass(frozen=True)
class Option:
    order_id: str
    plant: str
    product: str
    destination: str
    week: date
    qty: float
    cost: float
    carbon_kg: float
    arrival: date
    due: date
    days_late: int

    @property
    def on_time(self) -> bool:
        return self.days_late == 0


def _as_date(v: Any) -> date:
    return pd.Timestamp(v).date()


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _num(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(f) else f


def _weeks(ds: Dataset) -> list[date]:
    start = _monday(ds.planning_start)
    last_due = max((_as_date(d) for d in ds.orders["due"]), default=start)
    n = min(MAX_WEEKS, max(1, (_monday(last_due) - start).days // 7 + 1))
    return [start + timedelta(weeks=i) for i in range(n)]


def _plant_rows(ds: Dataset) -> pd.DataFrame:
    """One row per (plant, product). If plants has a `week` column, values are averaged over weeks (stub limit)."""
    p = ds.plants.copy()
    p["plant"], p["product"] = p["plant"].astype(str), p["product"].astype(str)
    cols = [c for c in ("weekly_capacity", "unit_cost", "energy_kwh_per_unit", "carbon_kg_per_kwh",
                        "energy_price_per_kwh") if c in p.columns]
    return p.groupby(["plant", "product"], as_index=False)[cols].mean()


def _lanes(ds: Dataset) -> dict[tuple[str, str], dict]:
    td = ds.transport_duties
    if td is None or td.empty:
        return {}
    return {(str(r["plant"]), str(r["destination"])): r for r in td.to_dict("records")}


def build_options(ds: Dataset) -> tuple[dict[str, list[Option]], list[str]]:
    """Every (order, eligible plant, week) option with its landed cost, carbon and lateness."""
    plants, lanes, weeks = _plant_rows(ds), _lanes(ds), _weeks(ds)
    notes: set[str] = set()
    if "week" in ds.plants.columns:
        notes.add("Stub averages weekly carbon intensity per plant and product.")
    out: dict[str, list[Option]] = {}
    for o in ds.orders.to_dict("records"):
        oid, product, dest = str(o["order_id"]), str(o["product"]), str(o["destination"])
        qty, due = _num(o["qty"]), _as_date(o["due"])
        out[oid] = []
        for r in plants[plants["product"] == product].to_dict("records"):
            lane = lanes.get((r["plant"], dest))
            if lane is None:
                notes.add(f"No transport row for {r['plant']}->{dest}; transport, duty and transit taken as 0.")
                lane = {}
            prod_value = qty * (_num(r["unit_cost"]) + _num(r["energy_kwh_per_unit"]) * _num(r.get("energy_price_per_kwh")))
            cost = prod_value * (1 + _num(lane.get("duty_pct")) / 100) + qty * _num(lane.get("transport_cost_per_unit"))
            carbon = qty * (_num(r["energy_kwh_per_unit"]) * _num(r["carbon_kg_per_kwh"])
                            + _num(lane.get("carbon_kg_per_unit")))
            transit = int(_num(lane.get("transit_days")))
            for w in weeks:
                arrival = w + timedelta(days=6 + transit)
                out[oid].append(Option(oid, r["plant"], product, dest, w, qty, round(cost, 2), round(carbon, 2),
                                       arrival, due, max(0, (arrival - due).days)))
    return out, sorted(notes)


# ============================================================ capacity
def _events(ds: Dataset) -> list[CapacityChange]:
    ev = ds.capacity_events
    if ev is None or ev.empty:
        return []
    return [CapacityChange(**{k: (None if pd.isna(v) else v) for k, v in r.items()}) for r in ev.to_dict("records")]


def _overlaps(c: CapacityChange, week: date) -> bool:
    return (c.from_date is None or c.from_date <= week + timedelta(days=6)) and (c.to_date is None or c.to_date >= week)


def effective_capacity(ds: Dataset, changes: list[CapacityChange]) -> tuple[dict[CapKey, float], dict[str, bool]]:
    """Capacity per (plant, product|None, week) after capacity_events and scenario changes.
    A plant whose weekly_capacity is identical on all its product rows has one shared capacity (product None)."""
    plants, weeks, events = _plant_rows(ds), _weeks(ds), _events(ds) + list(changes)
    cap: dict[CapKey, float] = {}
    shared: dict[str, bool] = {}
    for plant, g in plants.groupby("plant"):
        shared[plant] = g["weekly_capacity"].nunique() == 1
        rows = [(None, g["weekly_capacity"].iloc[0])] if shared[plant] else list(zip(g["product"], g["weekly_capacity"]))
        for product, c in rows:
            for w in weeks:
                factor = math.prod(1 - e.capacity_loss_pct / 100 for e in events if e.plant == plant and _overlaps(e, w))
                cap[(plant, product, w)] = float(c) * factor
    return cap, shared


def _key(o: Option, shared: dict[str, bool]) -> CapKey:
    return (o.plant, None if shared.get(o.plant, True) else o.product, o.week)


# ============================================================ solver stub
def solve_allocation(ds: Dataset, weights: Weights, changes: list[CapacityChange] = [],
                     carbon_cap_kg: float | None = None, require_all_on_time: bool = False,
                     base_plan: list[OrderAssignment] | None = None,
                     forced: dict[str, str] | None = None) -> SolveResult:
    t0 = time.perf_counter()
    try:
        return _solve(ds, weights, list(changes), carbon_cap_kg, require_all_on_time, base_plan or [], forced or {}, t0)
    except Exception as exc:  # never raise to the graph
        log.exception("stub solver failed")
        return SolveResult(status="ERROR", assignments=[], solve_s=round(time.perf_counter() - t0, 3),
                           weights_used=weights, notes=[STUB_NOTE, f"Stub solver error: {exc}"])


def _solve(ds: Dataset, w: Weights, changes: list[CapacityChange], carbon_cap_kg: float | None,
           require_all_on_time: bool, base_plan: list[OrderAssignment], forced: dict[str, str],
           t0: float) -> SolveResult:
    opts, notes = build_options(ds)
    notes = [STUB_NOTE, *notes]
    left, shared = effective_capacity(ds, changes)
    total_cap = dict(left)
    c_ref = sum(min(o.cost for o in os) for os in opts.values() if os) or 1.0
    k_ref = sum(min(o.carbon_kg for o in os) for os in opts.values() if os) or 1.0
    base = {a.order_id: (a.plant, a.production_week) for a in base_plan}

    def score(o: Option) -> float:
        moved = o.order_id in base and base[o.order_id] != (o.plant, o.week)
        return (w.cost * o.cost / c_ref + w.carbon * o.carbon_kg / k_ref + w.lateness * 0.01 * o.days_late
                + 0.001 * moved)

    cands: dict[str, list[Option]] = {}
    for oid, os in opts.items():
        if oid in forced:
            os = [o for o in os if o.plant == forced[oid]]
            if not os:
                notes.append(f"Forced plant {forced[oid]} cannot make order {oid}.")
        if require_all_on_time and any(o.on_time for o in os):
            os = [o for o in os if o.on_time]
        cands[oid] = sorted(os, key=score)

    def regret(oid: str) -> float:
        s = [score(o) for o in cands[oid][:2]]
        return s[1] - s[0] if len(s) == 2 else math.inf

    due = {oid: os[0].due for oid, os in opts.items() if os}
    order = sorted(cands, key=lambda oid: (oid not in forced, due.get(oid, date.max), -regret(oid)))
    chosen: dict[str, Option | None] = {}
    for oid in order:
        chosen[oid] = next((o for o in cands[oid] if left[_key(o, shared)] >= o.qty - 1e-9), None)
        if chosen[oid] is not None:
            left[_key(chosen[oid], shared)] -= chosen[oid].qty

    status = "FEASIBLE"
    if carbon_cap_kg is not None and not _repair_carbon(chosen, cands, left, shared, score, carbon_cap_kg):
        status = "INFEASIBLE"
        notes.append(f"Carbon cap of {carbon_cap_kg / 1000:,.1f} t cannot be met within capacity.")
    unassigned = [oid for oid, o in chosen.items() if o is None]
    if unassigned:
        notes.append(f"{len(unassigned)} orders could not be assigned within capacity: {', '.join(unassigned[:10])}.")

    assignments = [_assignment(ds, oid, chosen[oid], base) for oid in opts]
    return SolveResult(status=status, assignments=assignments, kpis=_kpis(ds, assignments, total_cap, left),
                       solve_s=round(time.perf_counter() - t0, 3), weights_used=w, notes=notes,
                       objective=round(sum(score(o) for o in chosen.values() if o) + 1000 * len(unassigned), 6))


def _repair_carbon(chosen: dict[str, Option | None], cands: dict[str, list[Option]], left: dict[CapKey, float],
                   shared: dict[str, bool], score: Any, cap_kg: float) -> bool:
    """Move orders to lower-carbon options, cheapest score increase per kg saved first, until under the cap."""
    for _ in range(3):
        total = sum(o.carbon_kg for o in chosen.values() if o)
        if total <= cap_kg + 1e-6:
            return True
        moves = [((score(n) - score(cur)) / (cur.carbon_kg - n.carbon_kg), oid, cur, n)
                 for oid, cur in chosen.items() if cur
                 for n in cands[oid] if n.carbon_kg < cur.carbon_kg]
        moves.sort(key=lambda m: (m[0], m[1]))
        for _ratio, oid, cur, n in moves:
            if total <= cap_kg + 1e-6:
                break
            if chosen[oid] is not cur or left[_key(n, shared)] < n.qty - 1e-9:
                continue
            left[_key(cur, shared)] += cur.qty
            left[_key(n, shared)] -= n.qty
            chosen[oid], total = n, total - cur.carbon_kg + n.carbon_kg
    return sum(o.carbon_kg for o in chosen.values() if o) <= cap_kg + 1e-6


def _assignment(ds: Dataset, oid: str, o: Option | None, base: dict) -> OrderAssignment:
    if o is None:
        row = ds.orders.loc[ds.orders["order_id"].astype(str) == oid].iloc[0]
        return OrderAssignment(order_id=oid, plant=None, cost=0.0, carbon_kg=0.0, due=_as_date(row["due"]),
                               on_time=False, changed=oid in base and base[oid][0] is not None)
    return OrderAssignment(order_id=oid, plant=o.plant, production_week=o.week, cost=o.cost, carbon_kg=o.carbon_kg,
                           arrival=o.arrival, due=o.due, on_time=o.on_time, days_late=o.days_late,
                           changed=oid in base and base[oid] != (o.plant, o.week),
                           evidence_refs=[f"plants.csv:{o.plant}:{o.product}",
                                          f"transport_duties.csv:{o.plant}:{o.destination}"])


def _kpis(ds: Dataset, plan: list[OrderAssignment], total_cap: dict[CapKey, float],
          left: dict[CapKey, float]) -> KPIs:
    dest = dict(zip(ds.orders["order_id"].astype(str), ds.orders["destination"].astype(str)))
    late = [a for a in plan if a.plant and not a.on_time]
    util: dict[str, float] = {}
    for plant in sorted({k[0] for k in total_cap}):
        cap = sum(v for k, v in total_cap.items() if k[0] == plant)
        used = cap - sum(v for k, v in left.items() if k[0] == plant)
        util[plant] = round(used / cap, 4) if cap > 0 else 0.0
    late_by_dest: dict[str, int] = {}
    for a in late:
        late_by_dest[dest[a.order_id]] = late_by_dest.get(dest[a.order_id], 0) + 1
    return KPIs(total_cost=round(sum(a.cost for a in plan), 2),
                total_carbon_t=round(sum(a.carbon_kg for a in plan) / 1000, 3), late_orders=len(late),
                unassigned_orders=sum(a.plant is None for a in plan), plant_utilisation=util,
                late_by_destination=late_by_dest)


# ============================================================ validator stub
def validate_plan(ds: Dataset, plan: list[OrderAssignment], changes: list[CapacityChange] = []) -> ValidatorResult:
    """Re-checks the plan against the data. Shares only the cost model with the stub solver, not its assignment code."""
    opts, _ = build_options(ds)
    cap, shared = effective_capacity(ds, list(changes))
    by_key = {(o.order_id, o.plant, o.week): o for os in opts.values() for o in os}
    cap_v, elig_v, other_v = [], [], []
    seen: dict[str, int] = {}
    load: dict[CapKey, float] = {}
    late = False
    for a in plan:
        seen[a.order_id] = seen.get(a.order_id, 0) + 1
        if a.order_id not in opts:
            other_v.append(f"Order {a.order_id} is not in the data.")
            continue
        if a.plant is None:
            continue
        if not any(o.plant == a.plant for o in opts[a.order_id]):
            elig_v.append(f"Plant {a.plant} cannot make the product of order {a.order_id}.")
            continue
        o = by_key.get((a.order_id, a.plant, a.production_week))
        if o is None:
            other_v.append(f"Order {a.order_id} has no production week inside the horizon.")
            continue
        load[_key(o, shared)] = load.get(_key(o, shared), 0.0) + o.qty
        if abs(o.cost - a.cost) > TOL or abs(o.carbon_kg - a.carbon_kg) > TOL:
            other_v.append(f"Order {a.order_id}: cost or carbon does not match the cost model.")
        if o.on_time != a.on_time:
            other_v.append(f"Order {a.order_id}: on-time flag does not match arrival {o.arrival} vs due {o.due}.")
        late = late or not o.on_time
    for oid in opts:
        if seen.get(oid, 0) != 1:
            other_v.append(f"Order {oid} appears {seen.get(oid, 0)} times (expected once).")
    for k, used in load.items():
        if used > cap.get(k, 0.0) + 1e-6:
            cap_v.append(f"Plant {k[0]} week {k[2]} load {used:,.0f} exceeds capacity {cap.get(k, 0.0):,.0f}.")
    return ValidatorResult(capacity_ok=not cap_v, eligibility_ok=not elig_v, delivery_ok=not late,
                           violations=cap_v + elig_v + other_v)


# ============================================================ scenario stubs
def tradeoff_frontier(ds: Dataset, weights: Weights, cuts: tuple = (0, 5, 10, 15, 20, 30)) -> list[TradeoffPoint]:
    """Epsilon-constraint: re-solve with carbon <= (1 - cut) * carbon of the unconstrained plan."""
    base = solve_allocation(ds, weights)
    if base.kpis is None:
        return []
    c0, k0 = base.kpis.total_cost, base.kpis.total_carbon_t
    points = []
    for cut in cuts:
        res = base if cut == 0 else solve_allocation(ds, weights, carbon_cap_kg=k0 * 1000 * (1 - cut / 100))
        if res.kpis is None:
            continue
        saved_t = k0 - res.kpis.total_carbon_t
        points.append(TradeoffPoint(
            carbon_cut_pct=float(cut), cost=res.kpis.total_cost, carbon_t=res.kpis.total_carbon_t,
            late_orders=res.kpis.late_orders, feasible=res.status in ("OPTIMAL", "FEASIBLE"),
            implied_carbon_price_per_t=round((res.kpis.total_cost - c0) / saved_t, 2) if cut and saved_t > 0 else None,
        ))
    return points


def _signed(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}{abs(x):,.0f}"


def _reason(old: OrderAssignment, new: OrderAssignment) -> str:
    if new.plant is None:
        return f"No eligible plant has capacity left, so the order is unassigned (was Plant {old.plant})."
    if old.plant is None:
        return f"Capacity is now available in Plant {new.plant}."
    if old.plant == new.plant:
        text = f"Rescheduled in Plant {new.plant} from week {old.production_week} to week {new.production_week}"
    else:
        text = (f"Moved from Plant {old.plant} to Plant {new.plant}: cost {_signed(new.cost - old.cost)}, "
                f"carbon {_signed(new.carbon_kg - old.carbon_kg)} kg")
    return text + (f"; now {new.days_late} days late." if not new.on_time else ".")


def compare_plans(ds: Dataset, base: SolveResult, new: SolveResult) -> list[PlanChange]:
    old = {a.order_id: a for a in base.assignments}
    out = []
    for a in new.assignments:
        b = old.get(a.order_id)
        if b is None or (b.plant, b.production_week) == (a.plant, a.production_week):
            continue
        out.append(PlanChange(order_id=a.order_id, from_plant=b.plant, to_plant=a.plant,
                              cost_delta=round(a.cost - b.cost, 2), carbon_delta_kg=round(a.carbon_kg - b.carbon_kg, 2),
                              reason=_reason(b, a)))
    return out


def counterfactual(ds: Dataset, base: SolveResult, order_id: str, plant: str) -> CounterfactualResult:
    """Re-solve the WHOLE plan with order_id forced to plant, then compare with the base plan."""
    b = {a.order_id: a for a in base.assignments}
    chosen = b[order_id].plant if order_id in b else None
    product = ds.orders.loc[ds.orders["order_id"].astype(str) == order_id, "product"].iloc[0]
    makers = set(ds.plants.loc[ds.plants["product"].astype(str) == str(product), "plant"].astype(str))
    if plant not in makers:
        return CounterfactualResult(order_id=order_id, chosen_plant=chosen, alternative_plant=plant, feasible=False,
                                    blocked_by="eligibility")
    new = solve_allocation(ds, base.weights_used, forced={order_id: plant}, base_plan=base.assignments)
    a = next((x for x in new.assignments if x.order_id == order_id), None)
    if new.status in ("INFEASIBLE", "ERROR") or a is None or a.plant != plant or not base.kpis or not new.kpis:
        return CounterfactualResult(order_id=order_id, chosen_plant=chosen, alternative_plant=plant, feasible=False,
                                    blocked_by="capacity")
    was_on_time = order_id in b and b[order_id].on_time
    return CounterfactualResult(
        order_id=order_id, chosen_plant=chosen, alternative_plant=plant,
        feasible=a.on_time or not was_on_time, blocked_by="delivery" if was_on_time and not a.on_time else None,
        order_cost_delta=round(a.cost - b[order_id].cost, 2) if order_id in b else 0.0,
        plan_cost_delta=round(new.kpis.total_cost - base.kpis.total_cost, 2),
        plan_carbon_delta_kg=round((new.kpis.total_carbon_t - base.kpis.total_carbon_t) * 1000, 2),
        new_late_orders=[x.order_id for x in new.assignments
                         if x.plant and not x.on_time and x.order_id in b and b[x.order_id].on_time],
        displaced_orders=[c for c in compare_plans(ds, base, new) if c.order_id != order_id],
    )
