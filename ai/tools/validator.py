"""Plan validator: an independent re-check of any plan (solver output, baseline or a hand-made plan).

Deliberately does NOT import solver code or the cost model's pricing / capacity functions. It recomputes
eligibility, capacity, delivery dates, cost and carbon row by row from the raw tables, so a bug in the
vectorised cost model or the MIP cannot hide itself. Only date parsing helpers are shared.

Flags
    eligibility_ok  every order appears exactly once, each plant can make the product and ship to the
                    destination, and the plan's cost / carbon match the recomputed values (within 0.01)
    capacity_ok     per (plant[, product], week) load <= effective capacity
    delivery_ok     no order is late or unassigned (late orders are flagged, not hidden)
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, timedelta

import pandas as pd

from ai.tools.cost_model import first_week, has_week_column, monday, parse_week_column, to_date
from shared.models import CapacityChange, Dataset, OrderAssignment, ValidatorResult

log = logging.getLogger(__name__)
TOL = 0.01


def validate_plan(ds: Dataset, plan: list[OrderAssignment],
                  changes: Sequence[CapacityChange] = ()) -> ValidatorResult:
    """Check a plan against the data. Never raises: an internal error is reported as a violation."""
    try:
        return _validate(ds, plan, list(changes or []))
    except Exception as exc:
        log.exception("validate_plan failed")
        return ValidatorResult(capacity_ok=False, eligibility_ok=False, delivery_ok=False,
                               violations=[f"Validator error: {type(exc).__name__}: {exc}"])


def _validate(ds: Dataset, plan: list[OrderAssignment], changes: list[CapacityChange]) -> ValidatorResult:
    elig: list[str] = []
    cap_v: list[str] = []
    late: list[str] = []
    orders = {str(r.order_id): r for r in ds.orders.itertuples(index=False)}
    week0 = first_week(ds)
    plant_rows = _plant_rows(ds, week0)
    routes = _routes(ds)

    # ---- coverage: every order exactly once, no unknown orders
    seen: dict[str, int] = defaultdict(int)
    for a in plan:
        seen[a.order_id] += 1
    elig += [f"{o} appears {n} times in the plan" for o, n in seen.items() if n > 1]
    elig += [f"{o} is in the plan but not in the orders table" for o in seen if o not in orders]
    elig += [f"{o} is missing from the plan" for o in orders if o not in seen]

    load: dict[tuple, float] = defaultdict(float)
    for a in plan:
        o = orders.get(a.order_id)
        if o is None:
            continue
        if a.plant is None:
            late.append(f"{a.order_id} is unassigned (no capacity)")
            continue
        product, dest, qty = str(o.product), str(o.destination), float(o.qty)
        week = a.production_week
        if week is None:
            elig.append(f"{a.order_id} at plant {a.plant} has no production week")
            continue
        if week < week0:
            elig.append(f"{a.order_id} is produced in week {week} before planning starts ({week0})")
        prow = plant_rows.get((a.plant, product, week if has_week_column(ds) else None))
        if prow is None:
            elig.append(f"Plant {a.plant} cannot make {product} for {a.order_id}"
                        + (f" in week {week}" if has_week_column(ds) else ""))
            continue
        if routes is not None and (a.plant, dest) not in routes:
            elig.append(f"Plant {a.plant} has no transport route to {dest} for {a.order_id}")
            continue
        route = routes.get((a.plant, dest)) if routes is not None else None
        tc, transit, duty, tcarb = route if route else (0.0, 0.0, 0.0, 0.0)

        # recompute money and carbon independently
        unit_cost, kwh, ckwh, price = prow
        cost = qty * (unit_cost + kwh * price) * (1 + duty / 100) + qty * tc
        carbon = qty * (kwh * ckwh + tcarb)
        if abs(cost - a.cost) > TOL + 1e-9 * abs(cost):
            elig.append(f"{a.order_id} cost {a.cost:,.2f} does not match recomputed {cost:,.2f}")
        if abs(carbon - a.carbon_kg) > TOL + 1e-9 * abs(carbon):
            elig.append(f"{a.order_id} carbon {a.carbon_kg:,.2f} kg does not match recomputed {carbon:,.2f} kg")

        # recompute delivery
        arrival = week + timedelta(days=6 + math.ceil(transit))
        due = to_date(o.due)
        days_late = max(0, (arrival - due).days)
        if a.arrival is not None and a.arrival != arrival:
            elig.append(f"{a.order_id} arrival {a.arrival} does not match recomputed {arrival}")
        if a.on_time != (days_late == 0) or a.days_late != days_late:
            elig.append(f"{a.order_id} says on_time={a.on_time}, days_late={a.days_late} "
                        f"but recomputed days_late={days_late}")
        if days_late:
            late.append(f"{a.order_id} arrives {days_late} days late at {dest} (flagged)")

        load[(a.plant, product, week)] += qty

    # ---- capacity, recomputed from plants + capacity_events + scenario changes
    shared = _shared_plants(ds)
    used: dict[tuple, float] = defaultdict(float)
    for (plant, product, week), q in load.items():
        used[(plant, None if plant in shared else product, week)] += q
    for (plant, product, week), q in sorted(used.items(), key=lambda kv: (kv[0][0], kv[0][2], str(kv[0][1]))):
        limit = _capacity(ds, plant, product, week, changes, week0)
        if q > limit + 1e-6:
            what = f"plant {plant}" + (f" product {product}" if product else "")
            cap_v.append(f"{what} week {week}: load {q:,.0f} exceeds capacity {limit:,.0f}")

    violations = cap_v + elig + late
    result = ValidatorResult(capacity_ok=not cap_v, eligibility_ok=not elig, delivery_ok=not late,
                             violations=violations)
    log.info("validate_plan orders=%d capacity_ok=%s eligibility_ok=%s delivery_ok=%s violations=%d",
             len(plan), result.capacity_ok, result.eligibility_ok, result.delivery_ok, len(violations))
    return result


# ============================================================ independent table readers
def _plant_rows(ds: Dataset, week0: date) -> dict[tuple, tuple[float, float, float, float]]:
    """(plant, product, week|None) -> (unit_cost, energy_kwh_per_unit, carbon_kg_per_kwh, energy_price)."""
    p = ds.plants
    weeks = parse_week_column(p["week"], week0) if has_week_column(ds) else [None] * len(p)
    prices = p["energy_price_per_kwh"] if "energy_price_per_kwh" in p.columns else [0.0] * len(p)
    out = {}
    for r, w, price in zip(p.itertuples(index=False), weeks, prices):
        out[(str(r.plant), str(r.product), w)] = (float(r.unit_cost), float(r.energy_kwh_per_unit),
                                                 float(r.carbon_kg_per_kwh), 0.0 if pd.isna(price) else float(price))
    return out


def _routes(ds: Dataset) -> dict[tuple[str, str], tuple[float, float, float, float]] | None:
    """(plant, destination) -> (transport_cost_per_unit, transit_days, duty_pct, carbon_kg_per_unit);
    None when the transport table is empty (zero-cost assumption)."""
    t = ds.transport_duties
    if t is None or t.empty:
        return None
    return {(str(r.plant), str(r.destination)): (float(r.transport_cost_per_unit), float(r.transit_days),
                                                 float(r.duty_pct), float(r.carbon_kg_per_unit))
            for r in t.itertuples(index=False)}


def _shared_plants(ds: Dataset) -> set[str]:
    """Plants whose weekly_capacity is the same on all their product rows (per week if weeks are given)."""
    caps: dict[tuple, set[float]] = defaultdict(set)
    weeks = ds.plants["week"] if has_week_column(ds) else [None] * len(ds.plants)
    for plant, w, c in zip(ds.plants["plant"].astype(str), weeks, ds.plants["weekly_capacity"]):
        caps[(plant, w)].add(float(c))
    plants = {p for p, _ in caps}
    return {p for p in plants if all(len(v) == 1 for (q, _), v in caps.items() if q == p)}


def _capacity(ds: Dataset, plant: str, product: str | None, week: date, changes: list[CapacityChange],
              week0: date) -> float:
    p = ds.plants[ds.plants["plant"].astype(str) == plant]
    if has_week_column(ds):
        p = p[parse_week_column(ds.plants["week"], week0)[p.index] == monday(week)]
    if product is not None:
        p = p[p["product"].astype(str) == product]
    if p.empty:
        return 0.0
    cap = float(p["weekly_capacity"].iloc[0])
    week_end = week + timedelta(days=6)
    losses: list[tuple[float, date | None, date | None]] = []
    ev = ds.capacity_events
    if ev is not None and not ev.empty:
        for r in ev[ev["plant"].astype(str) == plant].itertuples(index=False):
            losses.append((float(r.capacity_loss_pct), None if pd.isna(r.from_date) else to_date(r.from_date),
                           None if pd.isna(r.to_date) else to_date(r.to_date)))
    losses += [(c.capacity_loss_pct, c.from_date, c.to_date) for c in changes if c.plant == plant]
    for pct, start, end in losses:
        if (start is None or start <= week_end) and (end is None or end >= week):
            cap *= 1 - pct / 100
    return cap
