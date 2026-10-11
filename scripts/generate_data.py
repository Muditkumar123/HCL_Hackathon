"""Seeded synthetic data pack (Annex B schema) with the planning traps built in on purpose.

    python -m scripts.generate_data --out data/raw [--seed 42]

Writes orders.csv, plants.csv, transport_duties.csv, capacity_events.csv and refreshes data/DATA_CARD.md.

Traps (each one is described in the data card):
  * cheapest plant is late  P1 orders to US/BR with tight due dates: Plant A is cheapest but ships 22-26 days.
  * single-plant product    P6 is made only by Plant D.
  * duty trap               Plant B has the lowest P2 unit cost, but UK/TR charge 25-30% duty on B's goods.
  * low-carbon, low-capacity Plant C runs on hydro power (~0.03 kg/kWh) but makes only 220 units a week.
  * weekly carbon intensity plants.week: grid intensity moves week to week (wind, solar, maintenance).
  * capacity events         B loses 40% in week 2; A loses 25% in weeks 3-4.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

START = date(2026, 10, 12)          # Monday; first production week
N_WEEKS = 4
N_ORDERS = 120
DESTINATIONS = ["DE", "FR", "UK", "PL", "IT", "ES", "TR", "US", "BR", "IN"]
CARD = Path("data/DATA_CARD.md")

# plant -> (products, unit cost per product, capacity, energy kWh/unit, base kg CO2e/kWh, energy price/kWh, home)
# capacity: int => shared across the plant's products; dict => per product.
PLANTS: dict[str, dict] = {
    "A": {"products": {"P1": 9.0, "P2": 14.0, "P3": 11.0, "P4": 16.0}, "capacity": 1700,
          "kwh": 2.2, "carbon": 0.62, "price": 0.09, "region": "east"},
    "B": {"products": {"P1": 11.0, "P2": 12.0, "P5": 18.0}, "capacity": {"P1": 700, "P2": 800, "P5": 500},
          "kwh": 2.0, "carbon": 0.38, "price": 0.14, "region": "central"},
    "C": {"products": {"P1": 13.5, "P3": 14.0, "P4": 19.0}, "capacity": 220,
          "kwh": 2.4, "carbon": 0.03, "price": 0.07, "region": "north"},
    "D": {"products": {"P2": 15.0, "P4": 17.0, "P5": 17.5, "P6": 24.0}, "capacity": 900,
          "kwh": 1.8, "carbon": 0.45, "price": 0.12, "region": "west"},
    "E": {"products": {"P1": 12.5, "P3": 13.0, "P5": 19.0}, "capacity": 800,
          "kwh": 1.9, "carbon": 0.40, "price": 0.11, "region": "americas"},
}

# (region, destination) -> (transit days, cost/unit, kg CO2e/unit). Unlisted pairs use the defaults below.
TRANSIT: dict[str, dict[str, tuple[int, float, float]]] = {
    "east": {"PL": (2, 0.6, 0.05), "DE": (3, 0.8, 0.07), "TR": (4, 1.0, 0.09), "IN": (16, 1.6, 0.35),
             "US": (24, 1.4, 0.45), "BR": (26, 1.5, 0.5)},
    "central": {"DE": (1, 0.5, 0.03), "FR": (2, 0.7, 0.05), "IT": (2, 0.7, 0.05), "PL": (2, 0.7, 0.05),
                "US": (14, 2.2, 0.38), "BR": (17, 2.4, 0.42)},
    "north": {"DE": (2, 0.7, 0.05), "UK": (2, 0.8, 0.06), "PL": (3, 0.8, 0.06), "US": (13, 2.3, 0.36)},
    "west": {"FR": (1, 0.5, 0.03), "ES": (2, 0.6, 0.04), "UK": (2, 0.8, 0.05), "IT": (3, 0.8, 0.06),
             "US": (11, 2.0, 0.33), "BR": (14, 2.2, 0.38)},
    "americas": {"US": (3, 0.8, 0.08), "BR": (6, 1.2, 0.15), "UK": (10, 2.4, 0.34), "ES": (11, 2.5, 0.36)},
}
DEFAULT_TRANSIT = (7, 1.6, 0.2)
# Import duty (% of unit cost) by destination; B's goods face the punitive UK/TR rates (the duty trap).
DUTY = {"UK": 4.0, "TR": 6.0, "US": 5.0, "BR": 12.0, "IN": 10.0}
DUTY_OVERRIDE = {("B", "UK"): 25.0, ("B", "TR"): 30.0}


def make_plants(rng: np.random.Generator) -> pd.DataFrame:
    rows = []
    for w in range(N_WEEKS):
        week = START + timedelta(weeks=w)
        for plant, spec in PLANTS.items():
            # weekly grid intensity: +/-15% around the plant's base (C stays hydro-low)
            intensity = round(spec["carbon"] * float(rng.uniform(0.85, 1.15)), 3)
            for product, cost in spec["products"].items():
                cap = spec["capacity"][product] if isinstance(spec["capacity"], dict) else spec["capacity"]
                rows.append({"plant": plant, "product": product, "weekly_capacity": cap, "unit_cost": cost,
                             "energy_kwh_per_unit": spec["kwh"], "carbon_kg_per_kwh": intensity,
                             "energy_price_per_kwh": spec["price"], "week": week.isoformat()})
    return pd.DataFrame(rows)


def make_transport() -> pd.DataFrame:
    rows = []
    for plant, spec in PLANTS.items():
        for dest in DESTINATIONS:
            days, cost, carbon = TRANSIT[spec["region"]].get(dest, DEFAULT_TRANSIT)
            rows.append({"plant": plant, "destination": dest, "transport_cost_per_unit": cost, "transit_days": days,
                         "duty_pct": DUTY_OVERRIDE.get((plant, dest), DUTY.get(dest, 0.0)),
                         "carbon_kg_per_unit": carbon})
    return pd.DataFrame(rows)


def earliest_arrival(product: str, dest: str) -> date:
    """First-week production (Monday + 6 days) plus the fastest transit from any plant that makes `product`."""
    days = min(TRANSIT[spec["region"]].get(dest, DEFAULT_TRANSIT)[0]
               for spec in PLANTS.values() if product in spec["products"])
    return START + timedelta(days=6 + days)


def make_orders(rng: np.random.Generator) -> pd.DataFrame:
    """Every order has at least one on-time option (capacity permitting): due >= earliest arrival + 1 day."""
    def due(product: str, dest: str) -> date:
        return earliest_arrival(product, dest) + timedelta(days=int(rng.integers(1, 7 * N_WEEKS - 4)))

    rows = []
    # Trap orders first, so their ids are stable across seeds.
    for i, dest in enumerate(["US", "US", "BR", "US"]):           # cheapest plant (A) is late, E/D are on time
        rows.append(("P1", int(rng.integers(80, 160)), dest, START + timedelta(days=16 + i)))
    for dest in ["UK", "TR", "UK"]:                                # duty trap: B cheapest before duty
        rows.append(("P2", int(rng.integers(150, 300)), dest, due("P2", dest)))
    for dest in ["FR", "ES", "DE"]:                                # single-plant product
        rows.append(("P6", int(rng.integers(60, 140)), dest, due("P6", dest)))
    products = ["P1", "P2", "P3", "P4", "P5"]
    weights = [0.32, 0.22, 0.18, 0.14, 0.14]
    while len(rows) < N_ORDERS:
        product, dest = str(rng.choice(products, p=weights)), str(rng.choice(DESTINATIONS))
        rows.append((product, int(rng.integers(40, 260)), dest, due(product, dest)))
    df = pd.DataFrame(rows, columns=["product", "qty", "destination", "due"])
    df.insert(0, "order_id", [f"O-{i:03d}" for i in range(1, len(df) + 1)])
    df["priority"] = rng.choice(["normal", "normal", "normal", "high"], size=len(df))
    df["customer"] = [f"C-{int(c):02d}" for c in rng.integers(1, 25, size=len(df))]
    return df


def make_events() -> pd.DataFrame:
    return pd.DataFrame([
        {"event_id": "E-1", "plant": "B", "from_date": START + timedelta(weeks=1),
         "to_date": START + timedelta(weeks=1, days=6), "capacity_loss_pct": 40.0},
        {"event_id": "E-2", "plant": "A", "from_date": START + timedelta(weeks=2),
         "to_date": START + timedelta(weeks=3, days=6), "capacity_loss_pct": 25.0},
    ])


def generate(seed: int = 42) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    return {"plants": make_plants(rng), "transport_duties": make_transport(), "orders": make_orders(rng),
            "capacity_events": make_events()}


def data_card(tables: dict[str, pd.DataFrame], seed: int) -> str:
    o, p, t = tables["orders"], tables["plants"], tables["transport_duties"]
    counts = "\n".join(f"| {name} | {len(df)} |" for name, df in tables.items())
    return f"""# DATA CARD: synthetic multi-plant order pack

**Source:** 100% synthetic, generated by `python -m scripts.generate_data --seed {seed}`. No real company,
customer or plant data. Re-running with the same seed gives byte-identical CSVs.
**Planning start:** {START.isoformat()} (Monday), horizon {N_WEEKS} production weeks.

## Row counts
`python -m backend.validate_data` checks the loaded database against this table.

| table | rows |
|---|---|
{counts}

## Tables and units
| table | columns | units |
|---|---|---|
| orders | order_id, product, qty, destination, due, *priority, customer* | qty in units; due is the delivery date |
| plants | plant, product, weekly_capacity, unit_cost, energy_kwh_per_unit, carbon_kg_per_kwh, *energy_price_per_kwh, week* | capacity in units/week; cost in money/unit; carbon in kg CO2e per kWh; one row per (plant, product, week) |
| transport_duties | plant, destination, transport_cost_per_unit, transit_days, duty_pct, carbon_kg_per_unit | money/unit; days; % of unit cost; kg CO2e/unit |
| capacity_events | event_id, plant, from_date, to_date, capacity_loss_pct | % of weekly capacity lost between the dates (inclusive) |

Columns in *italics* are optional extras. Capacity: a plant with the same `weekly_capacity` on every product row
shares it across products (A, C, D, E); B has a separate capacity per product.

## Shape
- {len(o)} orders, {o['product'].nunique()} products, {o['destination'].nunique()} destinations
  ({', '.join(sorted(o['destination'].unique()))}), total demand {int(o['qty'].sum())} units.
- {p['plant'].nunique()} plants; {len(t)} plant-destination routes (full matrix).
- Grid intensity range {p['carbon_kg_per_kwh'].min():.3f}-{p['carbon_kg_per_kwh'].max():.3f} kg CO2e/kWh.

## Built-in traps (what a good plan must handle)
| trap | where |
|---|---|
| Cheapest plant is late | O-001..O-004: P1 to US/BR due in 16-19 days. Plant A is cheapest but ships 24-26 days; E and B are on time. |
| Single-plant product | P6 (O-008..O-010) is made only by Plant D. |
| Duty flips the cheapest plant | O-005..O-007: P2 to UK/TR. B has the lowest unit cost, but 25-30% duty on B's goods. |
| Low-carbon, low-capacity plant | Plant C: about 0.03 kg CO2e/kWh (hydro), only 220 units/week. |
| Weekly carbon intensity | `plants.week`: intensity moves +/-15% week to week. |
| Capacity events | E-1: B -40% in week 2. E-2: A -25% in weeks 3-4. |

## Known limitations
- Costs and intensities are plausible but invented; they are not calibrated to any real grid or tariff.
- One transport mode per route; no minimum batch sizes or changeover times.
"""


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="data/raw", type=Path)
    ap.add_argument("--seed", default=42, type=int)
    ap.add_argument("--card", default=CARD, type=Path)
    args = ap.parse_args()
    tables = generate(args.seed)
    args.out.mkdir(parents=True, exist_ok=True)
    for name, df in tables.items():
        df.to_csv(args.out / f"{name}.csv", index=False)
        log.info("wrote %s (%d rows)", args.out / f"{name}.csv", len(df))
    args.card.parent.mkdir(parents=True, exist_ok=True)
    args.card.write_text(data_card(tables, args.seed))
    log.info("wrote %s", args.card)


if __name__ == "__main__":
    main()
