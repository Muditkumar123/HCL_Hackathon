"""Fixtures for ai/tools tests.

`trap_ds`: 6 orders x 3 plants, built so each trap in TASK-001 is present (planning starts Mon 2026-10-05):
  * cheapest plant is late    O-1 (P1 -> US): A is cheapest but A->US takes 25 days.
  * single-plant product      O-2 (P3): only plant C makes P3.
  * duty flips the cheapest   P2: B has the lower unit cost, but B->FR has 40% duty, so A wins O-3 (FR);
                              B still wins O-4 (DE).
  * low-carbon tiny capacity  C (0.05 kg/kWh) has 150 units/week, shared by P1 and P3.
  * capacity modes            A and C: shared plant capacity; B: per product (800 vs 600).
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, so plain `pytest tests/tools` works

from shared.models import Dataset  # noqa: E402

START = date(2026, 10, 5)  # Monday
DUE = date(2026, 10, 20)


@pytest.fixture
def trap_ds() -> Dataset:
    orders = pd.DataFrame({
        "order_id": ["O-1", "O-2", "O-3", "O-4", "O-5", "O-6"],
        "product": ["P1", "P3", "P2", "P2", "P1", "P1"],
        "qty": [100, 100, 200, 300, 400, 500],
        "destination": ["US", "DE", "FR", "DE", "DE", "FR"],
        "due": [DUE] * 6,
    })
    plants = pd.DataFrame({
        "plant": ["A", "A", "B", "B", "C", "C"],
        "product": ["P1", "P2", "P1", "P2", "P1", "P3"],
        "weekly_capacity": [1000, 1000, 800, 600, 150, 150],
        "unit_cost": [10, 20, 12, 15, 13, 30],
        "energy_kwh_per_unit": [2, 2, 2, 2, 2, 1],
        "carbon_kg_per_kwh": [0.5, 0.5, 0.6, 0.6, 0.05, 0.05],
    })
    routes = [  # plant, destination, cost/unit, transit days, duty %, carbon kg/unit
        ("A", "DE", 1.0, 2, 0, 0.1), ("A", "FR", 1.0, 3, 0, 0.1), ("A", "US", 2.0, 25, 0, 0.5),
        ("B", "DE", 1.5, 3, 0, 0.1), ("B", "FR", 1.5, 3, 40, 0.1), ("B", "US", 3.0, 5, 0, 0.4),
        ("C", "DE", 1.0, 2, 0, 0.05), ("C", "FR", 1.0, 3, 0, 0.05), ("C", "US", 2.0, 5, 0, 0.2),
    ]
    transport = pd.DataFrame(routes, columns=["plant", "destination", "transport_cost_per_unit", "transit_days",
                                              "duty_pct", "carbon_kg_per_unit"])
    events = pd.DataFrame(columns=["event_id", "plant", "from_date", "to_date", "capacity_loss_pct"])
    return Dataset(orders=orders, plants=plants, transport_duties=transport, capacity_events=events,
                   planning_start=START, data_version="trap")


def synthetic_ds(n_orders: int = 150, n_plants: int = 4, n_dest: int = 8, n_weeks: int = 4,
                 seed: int = 7) -> Dataset:
    """Seeded pack shaped like the real one (weekly carbon intensity, 2 capacity events) for timing tests."""
    rng = np.random.default_rng(seed)
    plants_ = [chr(ord("A") + i) for i in range(n_plants)]
    products = [f"P{i}" for i in range(1, 7)]
    dests = [f"D{i}" for i in range(1, n_dest + 1)]
    rows = []
    for p in plants_:
        makes = rng.choice(products, size=4, replace=False)
        cap = int(rng.integers(8, 14)) * 250
        for prod in makes:
            for w in range(1, n_weeks + 1):
                rows.append((p, prod, cap, float(rng.uniform(8, 20)), float(rng.uniform(1, 3)),
                             float(rng.uniform(0.05, 0.7)), w))
    rows.append((plants_[-1], "P7", 400, 25.0, 1.0, 0.1, 1))  # single-plant product
    plants = pd.DataFrame(rows, columns=["plant", "product", "weekly_capacity", "unit_cost", "energy_kwh_per_unit",
                                         "carbon_kg_per_kwh", "week"])
    plants = plants.drop_duplicates(["plant", "product", "week"])
    makeable = sorted(set(plants["product"]) - {"P7"})
    transport = pd.DataFrame([(p, d, float(rng.uniform(0.5, 4)), int(rng.integers(1, 12)),
                               float(rng.choice([0, 0, 5, 12, 25])), float(rng.uniform(0.05, 0.6)))
                              for p in plants_ for d in dests],
                             columns=["plant", "destination", "transport_cost_per_unit", "transit_days", "duty_pct",
                                      "carbon_kg_per_unit"])
    orders = pd.DataFrame({
        "order_id": [f"O-{i:03d}" for i in range(1, n_orders + 1)],
        "product": list(rng.choice(makeable, size=n_orders - 1)) + ["P7"],
        "qty": rng.integers(50, 400, size=n_orders),
        "destination": rng.choice(dests, size=n_orders),
        "due": [START + timedelta(days=int(d)) for d in rng.integers(9, 7 * n_weeks + 4, size=n_orders)],
    })
    events = pd.DataFrame([
        ("E-1", plants_[0], START + timedelta(days=7), START + timedelta(days=13), 40.0),
        ("E-2", plants_[1], START, START + timedelta(days=20), 20.0),
    ], columns=["event_id", "plant", "from_date", "to_date", "capacity_loss_pct"])
    return Dataset(orders=orders, plants=plants, transport_duties=transport, capacity_events=events,
                   planning_start=START, data_version=f"synthetic-{seed}")


@pytest.fixture
def big_ds() -> Dataset:
    return synthetic_ds()
