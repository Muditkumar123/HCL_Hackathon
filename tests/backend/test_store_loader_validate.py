# ruff: noqa: F811  (test arguments shadow the fixtures imported from backend_fixtures, which is how pytest finds them)
"""Unit tests for backend.store, backend.loader and backend.validate_data."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from backend import loader, validate_data
from backend_fixtures import CSVS, db, loaded  # noqa: F401 (pytest fixtures, imported by name)
from shared.models import KPIs, OrderAssignment, SolveResult, TraceRecord, Weights


def test_load_dataset_types_and_cache(loaded):
    ds = loaded.load_dataset()
    assert ds is loaded.load_dataset()                       # cached
    assert isinstance(ds.orders["due"].iloc[0], date)
    assert ds.capacity_events["from_date"].iloc[0] == date(2026, 10, 12)
    assert ds.planning_start == date(2026, 10, 5)            # from PLANNING_START
    assert ds.transport_duties["transit_days"].dtype.kind == "i"


def test_data_version_changes_on_write(loaded):
    v1 = loaded.load_dataset().data_version
    loader.load_csv("orders", (CSVS["orders"] + "O-9,P1,5,DE,2026-10-25\n").encode())
    assert loaded.load_dataset().data_version != v1


def test_planning_start_fallbacks(monkeypatch):
    from backend.store import planning_start

    monkeypatch.delenv("PLANNING_START", raising=False)
    orders = pd.DataFrame({"due": [date(2026, 10, 30), date(2026, 10, 22)]})
    assert planning_start(orders) == date(2026, 10, 5)       # Monday on/before 22 Oct - 14 days
    plants = pd.DataFrame({"week": ["2026-10-14", "2026-10-21"]})
    assert planning_start(orders, plants) == date(2026, 10, 12)


def test_trace_roundtrip(db):
    t = TraceRecord(run_id="al-abc123", request="hi", tokens=5)
    db.save_trace(t)
    assert db.get_trace("al-abc123").tokens == 5
    assert db.get_trace("missing") is None


def test_latest_plan_roundtrip(db):
    assert db.latest_plan() is None
    a = OrderAssignment(order_id="O-1", plant="A", cost=10, carbon_kg=1, due=date(2026, 10, 20), on_time=True)
    res = SolveResult(status="OPTIMAL", assignments=[a], solve_s=0.1, weights_used=Weights(),
                      kpis=KPIs(total_cost=10, total_carbon_t=0.001, late_orders=0, plant_utilisation={"A": 0.1}))
    db.save_plan("al-1", res)
    assert db.latest_plan().assignments[0].plant == "A"


def test_loader_rejects_bad_values(db):
    loader.load_csv("plants", CSVS["plants"].encode())
    text = ("event_id,plant,from_date,to_date,capacity_loss_pct\n"
            "E-1,B,2026-10-12,2026-10-18,130\nE-2,B,2026-10-18,2026-10-12,10\nE-3,A,2026-10-12,2026-10-18,20\n")
    res = loader.load_csv("capacity_events", text.encode())
    assert res.status == "partial" and res.rows_loaded == 1
    assert "> 100" in res.errors[0] and "before from_date" in res.errors[1]


def test_loader_keeps_extra_columns(loaded):
    assert "priority" in loaded.load_dataset().orders.columns


def test_loader_caps_errors_at_20(db):
    text = "order_id,product,qty,destination,due\n" + "".join(f"O-{i},P1,-1,DE,2026-10-20\n" for i in range(30))
    res = loader.load_csv("orders", text.encode())
    assert res.status == "error" and res.rows_rejected == 30 and len(res.errors) == 20


def test_load_dir(db, tmp_path: Path):
    for table, text in CSVS.items():
        (tmp_path / f"{table}.csv").write_text(text)
    (tmp_path / "notes.csv").write_text("a\n1\n")
    results = loader.load_dir(tmp_path)
    assert [r.table for r in results] == list(loader.LOAD_ORDER)
    assert all(r.status == "ok" for r in results)


def test_validate_checks_pass_and_fail(loaded, tmp_path: Path):
    ds = loaded.load_dataset()
    card = tmp_path / "card.md"
    card.write_text("| table | rows |\n|---|---|\n| orders | 4 |\n| plants | 6 |\n")
    assert validate_data.check_row_counts(ds, card)["passed"]
    card.write_text("| orders | 99 |\n")
    assert not validate_data.check_row_counts(ds, card)["passed"]
    results = {r["check"]: r["passed"] for r in validate_data.run_checks(ds)}
    assert all(v for k, v in results.items() if "DATA_CARD" not in k)


def test_validate_detects_problems(loaded):
    ds = loaded.load_dataset()
    ds.orders.loc[0, "product"] = "P9"
    ds.plants.loc[0, "weekly_capacity"] = 0
    ds.plants.loc[1, "carbon_kg_per_kwh"] = 5.0
    ds.orders.loc[1, "due"] = date(2026, 10, 1)
    ds.transport_duties.drop(index=0, inplace=True)          # A->US route gone
    failed = {r["check"] for r in validate_data.run_checks(ds) if not r["passed"]}
    assert len(failed) >= 5


def test_generate_data_meets_spec():
    from scripts.generate_data import DESTINATIONS, generate

    t = generate(42)
    assert len(t["orders"]) >= 100 and t["orders"]["destination"].nunique() >= 8
    makers = t["plants"].groupby("product")["plant"].nunique()
    assert (makers == 1).any()                               # single-plant product
    assert "week" in t["plants"] and t["plants"].groupby("plant")["carbon_kg_per_kwh"].nunique().gt(1).any()
    assert len(t["capacity_events"]) == 2
    assert len(t["transport_duties"]) == t["plants"]["plant"].nunique() * len(DESTINATIONS)
    assert generate(42)["orders"].equals(t["orders"])        # seeded
