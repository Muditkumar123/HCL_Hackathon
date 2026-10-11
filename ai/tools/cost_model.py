"""Cost model: turns the four data tables into priced allocation options.

Pure functions: a `Dataset` goes in, DataFrames / contract models come out. No DB, no HTTP, no LLM.
Every formula and fallback is listed in `ASSUMPTIONS` so the API can show it to the user.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from typing import Any

import pandas as pd

from shared.models import KPIs, CapacityChange, Dataset, OrderAssignment

log = logging.getLogger(__name__)

ASSUMPTIONS: list[str] = [
    "Weeks start on Monday; the first production week is the week that contains planning_start.",
    "An order is produced at one plant in one week (orders are not split).",
    "Production finishes at the end of its week: arrival = week_start + 6 days + transit_days.",
    "energy_price_per_kwh is used if the plants table has it, else 0 (unit_cost is assumed to include energy).",
    "Duty is charged on the ex-works value (production + energy), not on transport.",
    "landed_cost = qty * (unit_cost + energy_kwh_per_unit * energy_price) * (1 + duty_pct/100)"
    " + qty * transport_cost_per_unit.",
    "carbon_kg = qty * (energy_kwh_per_unit * carbon_kg_per_kwh[plant, week]"
    " + carbon_kg_per_unit[plant, destination]).",
    "Capacity mode: a plant whose weekly_capacity is identical on all its product rows has one shared weekly "
    "capacity; otherwise capacity is per (plant, product).",
    "A capacity loss applies in full to every week that overlaps [from_date, to_date]; "
    "several losses in the same week multiply.",
    "A (plant, destination) pair with no transport row cannot ship there. If the transport table is empty, "
    "transport cost, transit days, duty and transport carbon are all 0.",
    "The horizon runs to the last week from which the latest due date can still be met (fastest route), plus one "
    "week for late production; with a 'week' column it is also limited to the weeks the plants table covers.",
    "An order that cannot be produced inside the horizon is 'unassigned' (deferred): it costs as much lateness as "
    "producing it in the week after the horizon, plus a margin, so the plan never defers an order to save cost.",
]

LATE_WEEKS = 1  # production weeks kept after the last week that can still meet the latest due date

OPTION_COLUMNS = [
    "order_id", "plant", "week_start", "qty", "landed_cost", "carbon_kg", "arrival", "due", "days_late",
    "on_time", "evidence_refs",
]


# ============================================================ dates and weeks
def to_date(value: Any) -> date:
    """Parse a CSV / pandas / python date value into a `date`."""
    return pd.Timestamp(value).date()


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def first_week(ds: Dataset) -> date:
    return monday(to_date(ds.planning_start))


def has_week_column(ds: Dataset) -> bool:
    return "week" in ds.plants.columns and ds.plants["week"].notna().any()


def parse_week_column(weeks: pd.Series, week0: date) -> pd.Series:
    """Map the plants 'week' column to Monday dates.

    Dates are snapped to their Monday. Integers starting at 0 or 1 are an index from `week0`;
    other integers are read as ISO week numbers in the planning year.
    """
    num = pd.to_numeric(weeks, errors="coerce")
    if num.notna().all():
        n = num.astype(int)
        if n.min() in (0, 1):
            offset = n - n.min()
        else:
            offset = (n - week0.isocalendar()[1]) % 52
        return offset.map(lambda k: week0 + timedelta(weeks=int(k)))
    return pd.to_datetime(weeks).dt.date.map(monday)


def horizon(ds: Dataset, horizon_weeks: int | None = None) -> list[date]:
    """Production weeks (Mondays) the solver may use: every week up to the last one from which the latest due
    date can still be met (fastest route), plus LATE_WEEKS more so a capacity shortfall shows up as lateness."""
    w0 = first_week(ds)
    last_useful = w0
    if len(ds.orders):
        t = _transport(ds)
        fastest = 0 if t is None else int(math.ceil(t["transit_days"].min()))
        last_due = max(to_date(d) for d in ds.orders["due"])
        last_useful = max(w0, monday(last_due - timedelta(days=6 + fastest)))
    end = last_useful + timedelta(weeks=LATE_WEEKS)
    if has_week_column(ds):
        weeks = sorted(w for w in set(parse_week_column(ds.plants["week"], w0)) if w0 <= w <= end)
    else:
        weeks = [w0 + timedelta(weeks=k) for k in range((end - w0).days // 7 + 1)]
    return weeks[:horizon_weeks] if horizon_weeks else weeks


# ============================================================ table preparation
def _plants_by_week(ds: Dataset, horizon_weeks: int | None = None) -> pd.DataFrame:
    """plants with a `week_start` column: one row per (plant, product, week) in the horizon."""
    weeks = horizon(ds, horizon_weeks)
    p = ds.plants.copy()
    p["plant"], p["product"] = p["plant"].astype(str), p["product"].astype(str)
    for col in ("weekly_capacity", "unit_cost", "energy_kwh_per_unit", "carbon_kg_per_kwh"):
        p[col] = pd.to_numeric(p[col])
    p["energy_price"] = (pd.to_numeric(p["energy_price_per_kwh"]).fillna(0.0)
                         if "energy_price_per_kwh" in p.columns else 0.0)
    if has_week_column(ds):
        p["week_start"] = parse_week_column(p["week"], first_week(ds))
        return p[p["week_start"].isin(set(weeks))].reset_index(drop=True)
    return p.merge(pd.DataFrame({"week_start": weeks}), how="cross")


def _transport(ds: Dataset) -> pd.DataFrame | None:
    """Transport rows with numeric columns, or None when the table is empty (zero-cost assumption)."""
    t = ds.transport_duties
    if t is None or t.empty:
        return None
    t = t.copy()
    t["plant"], t["destination"] = t["plant"].astype(str), t["destination"].astype(str)
    for col in ("transport_cost_per_unit", "transit_days", "duty_pct", "carbon_kg_per_unit"):
        t[col] = pd.to_numeric(t[col])
    return t.drop_duplicates(["plant", "destination"], keep="last")


def capacity_modes(ds: Dataset) -> dict[str, str]:
    """{plant: 'shared' | 'per_product'} following the capacity-mode rule in ASSUMPTIONS."""
    p = ds.plants.assign(plant=ds.plants["plant"].astype(str))
    keys = ["plant", "week"] if has_week_column(ds) else ["plant"]
    distinct = p.groupby(keys)["weekly_capacity"].nunique().groupby(level="plant").max()
    return {plant: "shared" if n <= 1 else "per_product" for plant, n in distinct.items()}


def assumptions_for(ds: Dataset) -> list[str]:
    """ASSUMPTIONS plus the data-dependent choices made for this dataset."""
    out = list(ASSUMPTIONS)
    modes = capacity_modes(ds)
    for mode in ("shared", "per_product"):
        plants = sorted(p for p, m in modes.items() if m == mode)
        if plants:
            out.append(f"Capacity mode used: {mode.replace('_', '-')} capacity for plant(s) {', '.join(plants)}.")
    if "energy_price_per_kwh" not in ds.plants.columns:
        out.append("No energy_price_per_kwh column: energy price taken as 0.")
    if has_week_column(ds):
        out.append("plants has a 'week' column: capacity, cost and carbon intensity vary by week.")
    if _transport(ds) is None:
        out.append("transport_duties is empty: transport cost, transit, duty and transport carbon taken as 0.")
    return out


# ============================================================ capacity
def _capacity_losses(ds: Dataset, changes: Iterable[CapacityChange]) -> list[tuple[str, float, date | None,
                                                                                    date | None, str]]:
    """(plant, loss_pct, from, to, ref) for every capacity_events row plus every scenario change."""
    out = []
    ev = ds.capacity_events
    if ev is not None and not ev.empty:
        for r in ev.itertuples(index=False):
            out.append((str(r.plant), float(r.capacity_loss_pct),
                        None if pd.isna(r.from_date) else to_date(r.from_date),
                        None if pd.isna(r.to_date) else to_date(r.to_date),
                        f"capacity_events.csv:{r.event_id}"))
    for c in changes:
        ref = f"scenario:{c.event_id}" if c.event_id else f"scenario:{c.plant}-{c.capacity_loss_pct:g}%"
        out.append((c.plant, c.capacity_loss_pct, c.from_date, c.to_date, ref))
    return out


def _overlaps(week_start: date, start: date | None, end: date | None) -> bool:
    week_end = week_start + timedelta(days=6)
    return (start is None or start <= week_end) and (end is None or end >= week_start)


def effective_capacity(ds: Dataset, changes: Sequence[CapacityChange] = (),
                       horizon_weeks: int | None = None) -> pd.DataFrame:
    """cols: plant, product|None, week_start, capacity (+ nominal, events). Applies capacity_events + scenario
    changes (capacity * (1 - loss_pct/100) for weeks overlapping [from_date, to_date]).

    `product` is None for plants in shared-capacity mode (one limit for all their products).
    """
    p = _plants_by_week(ds, horizon_weeks)
    modes = capacity_modes(ds)
    p["product"] = [None if modes.get(pl) == "shared" else pr for pl, pr in zip(p["plant"], p["product"])]
    cap = (p.groupby(["plant", "product", "week_start"], dropna=False, sort=False)["weekly_capacity"].first()
           .reset_index().rename(columns={"weekly_capacity": "nominal"}))
    cap["product"] = cap["product"].astype(object).where(cap["product"].notna(), None)
    cap = cap.sort_values(["plant", "week_start"], kind="stable").reset_index(drop=True)
    losses = _capacity_losses(ds, changes)
    capacity, events = [], []
    for plant, week, nominal in zip(cap["plant"], cap["week_start"], cap["nominal"]):
        factor, refs = 1.0, []
        for lp, pct, start, end, ref in losses:
            if lp == plant and _overlaps(week, start, end):
                factor *= 1 - pct / 100
                refs.append(ref)
        capacity.append(float(nominal) * factor)
        events.append(refs)
    cap["capacity"], cap["events"] = capacity, events
    return cap[["plant", "product", "week_start", "capacity", "nominal", "events"]]


def capacity_key(plant: str, product: str, week_start: date, modes: dict[str, str]) -> tuple:
    """The (plant, product|None, week_start) row in effective_capacity that an option draws on."""
    return (plant, None if modes.get(plant) == "shared" else product, week_start)


# ============================================================ options
def build_options(ds: Dataset, horizon_weeks: int | None = None) -> pd.DataFrame:
    """One row per (order_id, plant, week_start) for ELIGIBLE plants only.
    cols: order_id, plant, week_start, qty, landed_cost, carbon_kg, arrival, due, days_late, on_time,
          evidence_refs  (+ product, destination, unit_landed_cost, transit_days)

    Eligible = the plant has a plants row for the product (in that week) and a transport row to the destination.
    """
    o = ds.orders.copy()
    if o.empty:
        return pd.DataFrame(columns=OPTION_COLUMNS + ["product", "destination"])
    o["order_id"], o["product"] = o["order_id"].astype(str), o["product"].astype(str)
    o["destination"], o["qty"] = o["destination"].astype(str), pd.to_numeric(o["qty"])
    o["due"] = o["due"].map(to_date)
    o = o[["order_id", "product", "qty", "destination", "due"]]

    df = o.merge(_plants_by_week(ds, horizon_weeks), on="product", how="inner")
    t = _transport(ds)
    if t is None:
        for col in ("transport_cost_per_unit", "transit_days", "duty_pct", "carbon_kg_per_unit"):
            df[col] = 0.0
    else:
        df = df.merge(t[["plant", "destination", "transport_cost_per_unit", "transit_days", "duty_pct",
                         "carbon_kg_per_unit"]], on=["plant", "destination"], how="inner")
    if df.empty:
        return pd.DataFrame(columns=OPTION_COLUMNS + ["product", "destination"])

    prod_value = df["qty"] * (df["unit_cost"] + df["energy_kwh_per_unit"] * df["energy_price"])
    df["landed_cost"] = prod_value * (1 + df["duty_pct"] / 100) + df["qty"] * df["transport_cost_per_unit"]
    df["carbon_kg"] = df["qty"] * (df["energy_kwh_per_unit"] * df["carbon_kg_per_kwh"] + df["carbon_kg_per_unit"])
    df["unit_landed_cost"] = df["landed_cost"] / df["qty"]
    transit = df["transit_days"].map(math.ceil).astype(int)
    df["arrival"] = [w + timedelta(days=6 + int(d)) for w, d in zip(df["week_start"], transit)]
    df["days_late"] = [max(0, (a - d).days) for a, d in zip(df["arrival"], df["due"])]
    df["on_time"] = df["days_late"] == 0
    week_tag = has_week_column(ds)
    df["evidence_refs"] = [
        [f"orders.csv:{oid}", f"plants.csv:{pl}:{pr}" + (f":{w.isoformat()}" if week_tag else "")]
        + ([f"transport_duties.csv:{pl}:{de}"] if t is not None else [])
        for oid, pl, pr, w, de in zip(df["order_id"], df["plant"], df["product"], df["week_start"], df["destination"])
    ]
    cols = OPTION_COLUMNS + ["product", "destination", "unit_landed_cost", "transit_days"]
    return df[cols].sort_values(["order_id", "plant", "week_start"]).reset_index(drop=True)


# ============================================================ plan helpers shared by solver / baseline / scenarios
def to_assignment(row: Any, changed: bool = False, extra_refs: Sequence[str] = ()) -> OrderAssignment:
    """OrderAssignment from one build_options row (a namedtuple or Series)."""
    return OrderAssignment(
        order_id=row.order_id, plant=row.plant, production_week=row.week_start,
        cost=round(float(row.landed_cost), 2), carbon_kg=round(float(row.carbon_kg), 2),
        arrival=row.arrival, due=row.due, on_time=bool(row.on_time), days_late=int(row.days_late),
        changed=changed, evidence_refs=list(row.evidence_refs) + list(extra_refs),
    )


def unassigned(order_id: str, due: Any, changed: bool = False) -> OrderAssignment:
    return OrderAssignment(order_id=str(order_id), plant=None, cost=0.0, carbon_kg=0.0, due=to_date(due),
                           on_time=False, changed=changed, evidence_refs=[f"orders.csv:{order_id}"])


def plan_kpis(ds: Dataset, plan: Sequence[OrderAssignment], changes: Sequence[CapacityChange] = (),
              horizon_weeks: int | None = None) -> KPIs:
    """KPIs of a plan. Utilisation = assigned qty / effective capacity summed over the horizon, per plant."""
    qty = dict(zip(ds.orders["order_id"].astype(str), pd.to_numeric(ds.orders["qty"])))
    dest = dict(zip(ds.orders["order_id"].astype(str), ds.orders["destination"].astype(str)))
    cap = effective_capacity(ds, changes, horizon_weeks).groupby("plant")["capacity"].sum()
    load: dict[str, float] = {}
    late_by_dest: dict[str, int] = {}
    for a in plan:
        if a.plant is not None:
            load[a.plant] = load.get(a.plant, 0.0) + float(qty.get(a.order_id, 0))
            if not a.on_time:
                d = dest.get(a.order_id, "?")
                late_by_dest[d] = late_by_dest.get(d, 0) + 1
    util = {p: round(min(1.0, load.get(p, 0.0) / c), 4) if c > 0 else 0.0 for p, c in cap.items()}
    return KPIs(
        total_cost=round(sum(a.cost for a in plan), 2),
        total_carbon_t=round(sum(a.carbon_kg for a in plan) / 1000, 3),
        late_orders=sum(1 for a in plan if a.plant is not None and not a.on_time),
        unassigned_orders=sum(1 for a in plan if a.plant is None),
        plant_utilisation=util,
        late_by_destination=dict(sorted(late_by_dest.items())),
    )
