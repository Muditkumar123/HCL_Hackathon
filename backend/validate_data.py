"""Data-quality checks run before planning (shown in the demo and in GET /health).

    python -m backend.validate_data          # prints a PASS/FAIL table, exit code 1 on any FAIL
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pandas as pd

from shared.models import Dataset

log = logging.getLogger(__name__)

DATA_CARD = Path("data/DATA_CARD.md")
# Grid electricity intensity in kg CO2e per kWh. Real grids sit between ~0.01 (hydro/nuclear) and ~1.1 (lignite).
CARBON_KWH_RANGE = (0.0, 1.2)
_COUNT_ROW = re.compile(r"^\|\s*`?(orders|plants|transport_duties|capacity_events)`?\s*\|\s*(\d+)\s*\|", re.M)


def _check(name: str, bad: list[str], ok_detail: str, total: int | None = None) -> dict:
    if not bad:
        return {"check": name, "passed": True, "detail": ok_detail}
    shown = ", ".join(map(str, bad[:5])) + (" ..." if len(bad) > 5 else "")
    of = f" of {total}" if total is not None else ""
    return {"check": name, "passed": False, "detail": f"{len(bad)}{of} failing: {shown}"}


def check_products_makeable(ds: Dataset) -> dict:
    makeable = set(ds.plants["product"].astype(str))
    bad = ds.orders.loc[~ds.orders["product"].astype(str).isin(makeable), "order_id"].astype(str).tolist()
    return _check("every order's product is made by at least one plant", bad,
                  f"{len(ds.orders)} orders, {len(makeable)} makeable products", len(ds.orders))


def check_routes(ds: Dataset) -> dict:
    """Every (eligible plant, order destination) pair needs a transport_duties row."""
    elig = ds.orders[["product", "destination"]].merge(ds.plants[["plant", "product"]], on="product")
    needed = set(zip(elig["plant"].astype(str), elig["destination"].astype(str)))
    have = set(zip(ds.transport_duties["plant"].astype(str), ds.transport_duties["destination"].astype(str)))
    bad = sorted(f"{p}->{d}" for p, d in needed - have)
    return _check("every (plant, destination) pair has a transport row", bad,
                  f"{len(needed)} pairs needed, all present", len(needed))


def check_capacity(ds: Dataset) -> dict:
    cap = pd.to_numeric(ds.plants["weekly_capacity"], errors="coerce")
    rows = ds.plants.loc[~(cap > 0)]
    bad = [f"{r.plant}/{r.product}" for r in rows.itertuples()]
    return _check("weekly_capacity > 0 (units per week)", bad,
                  f"min {cap.min():g} units/week" if len(cap) else "no plants", len(ds.plants))


def check_due_after_start(ds: Dataset) -> dict:
    due = pd.to_datetime(ds.orders["due"], errors="coerce")
    bad = ds.orders.loc[~(due > pd.Timestamp(ds.planning_start)), "order_id"].astype(str).tolist()
    return _check(f"due > planning_start ({ds.planning_start})", bad,
                  f"earliest due {due.min().date() if len(due) else 'n/a'}", len(ds.orders))


def check_carbon_range(ds: Dataset) -> dict:
    lo, hi = CARBON_KWH_RANGE
    c = pd.to_numeric(ds.plants["carbon_kg_per_kwh"], errors="coerce")
    rows = ds.plants.loc[~c.between(lo, hi)]
    bad = [f"{r.plant}/{r.product}={r.carbon_kg_per_kwh}" for r in rows.itertuples()]
    return _check(f"carbon intensity in [{lo}, {hi}] kg CO2e/kWh", bad,
                  f"range {c.min():g}-{c.max():g} kg CO2e/kWh" if len(c) else "no plants", len(ds.plants))


def expected_counts(card: Path = DATA_CARD) -> dict[str, int]:
    """Row counts from the '| table | rows |' table in DATA_CARD.md."""
    if not card.exists():
        return {}
    return {t: int(n) for t, n in _COUNT_ROW.findall(card.read_text())}


def check_row_counts(ds: Dataset, card: Path = DATA_CARD) -> dict:
    exp = expected_counts(card)
    if not exp:
        return {"check": "row counts match DATA_CARD.md", "passed": False,
                "detail": f"no row-count table found in {card}"}
    actual = {"orders": len(ds.orders), "plants": len(ds.plants),
              "transport_duties": len(ds.transport_duties), "capacity_events": len(ds.capacity_events)}
    bad = [f"{t}: card {n} vs db {actual[t]}" for t, n in exp.items() if actual[t] != n]
    return _check("row counts match DATA_CARD.md", bad, ", ".join(f"{t}={actual[t]}" for t in exp))


CHECKS = (check_products_makeable, check_routes, check_capacity, check_due_after_start, check_carbon_range,
          check_row_counts)


def run_checks(ds: Dataset) -> list[dict]:
    """Run all checks; a check that crashes is reported as failed, never raised."""
    out = []
    for fn in CHECKS:
        try:
            out.append(fn(ds))
        except Exception as exc:  # e.g. a table is empty and a column is missing
            log.warning("check %s failed to run: %s", fn.__name__, exc)
            out.append({"check": fn.__name__, "passed": False, "detail": f"could not run: {exc}"})
    return out


def format_table(results: list[dict]) -> str:
    w = max(len(r["check"]) for r in results)
    lines = [f"{'CHECK':<{w}}  RESULT  DETAIL", "-" * (w + 40)]
    lines += [f"{r['check']:<{w}}  {'PASS' if r['passed'] else 'FAIL':<6}  {r['detail']}" for r in results]
    return "\n".join(lines)


def main() -> None:
    from backend import store

    logging.basicConfig(level=logging.WARNING)
    store.init_db()
    results = run_checks(store.load_dataset())
    print(format_table(results))
    if not all(r["passed"] for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
