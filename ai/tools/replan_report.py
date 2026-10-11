"""Before/after re-plan report for a late data drop (e.g. a new capacity_events.csv).

    python -m ai.tools.replan_report data/raw/late_capacity_events.csv [--data data/raw] [--mode append|replace]

The before plan is solved on the current tables; the after plan re-plans on the tables with the new file loaded,
starting from the before plan (minimal change). Both plans pass validate_plan. Nothing is written to the backend DB:
this is for checking numbers (worker1.md, demo), while the live path is scripts/late_drop.py -> /ingest -> /analyze.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import replace
from pathlib import Path

import pandas as pd

from ai.tools.scenarios import compare_plans
from ai.tools.solver import solve_allocation
from ai.tools.validator import validate_plan
from shared.models import Dataset, PlanChange, SolveResult, ValidatorResult, Weights

log = logging.getLogger(__name__)
TABLES = ("orders", "plants", "transport_duties", "capacity_events")


def replan_report(before: Dataset, after: Dataset, weights: Weights | None = None) -> dict:
    """Solve `before`, re-plan `after` from it, validate both. Returns KPIs, validator results and moved orders."""
    w = weights or Weights()
    old = solve_allocation(before, w)
    new = solve_allocation(after, w, base_plan=old.assignments)
    return {"before": old, "after": new,
            "validator_before": validate_plan(before, old.assignments),
            "validator_after": validate_plan(after, new.assignments),
            "changes": compare_plans(after, old, new)}


def to_markdown(rep: dict, title: str) -> str:
    old, new = rep["before"], rep["after"]
    vb, va = rep["validator_before"], rep["validator_after"]
    changes: list[PlanChange] = rep["changes"]

    def row(name: str, r: SolveResult, v: ValidatorResult) -> str:
        k = r.kpis
        if k is None:
            return f"| {name} | {r.status} | | | | | |"
        days = sum(a.days_late for a in r.assignments)
        return (f"| {name} | {k.total_cost:,.0f} | {k.total_carbon_t:,.1f} | {k.late_orders} ({days} days) | "
                f"{k.unassigned_orders} | {r.solve_s:.1f} s | capacity_ok={v.capacity_ok}, eligibility_ok={v.eligibility_ok} |")

    lines = [f"### {title}", "", "| Plan | Cost | Carbon (t) | Late orders | Deferred | Solve | Validator |",
             "|---|---|---|---|---|---|---|", row("Before", old, vb), row("After (re-plan)", new, va)]
    if old.kpis and new.kpis:
        lines += ["", f"Change: cost {new.kpis.total_cost - old.kpis.total_cost:+,.0f}, carbon "
                      f"{new.kpis.total_carbon_t - old.kpis.total_carbon_t:+,.1f} t, late orders "
                      f"{old.kpis.late_orders} -> {new.kpis.late_orders}, {len(changes)} orders changed plant or "
                      f"on-time status."]
    lines += [f"- {c.order_id}: {c.from_plant} -> {c.to_plant}. {c.reason}" for c in changes[:8]]
    return "\n".join(lines)


def _load(data: Path) -> dict[str, pd.DataFrame]:
    return {t: pd.read_csv(data / f"{t}.csv") if (data / f"{t}.csv").exists() else pd.DataFrame() for t in TABLES}


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser(description="Before/after re-plan for a late data drop.")
    ap.add_argument("file", type=Path, help="the late CSV; its table is the file name without .csv unless --table")
    ap.add_argument("--table", default=None, choices=TABLES)
    ap.add_argument("--data", type=Path, default=Path("data/raw"), help="folder with the current tables")
    ap.add_argument("--mode", choices=("replace", "append"), default="replace", help="as /ingest: replace or append")
    args = ap.parse_args()
    from backend.store import planning_start  # same planning_start as the API (CLI only; the tools stay pure)

    frames = _load(args.data)
    start = planning_start(frames["orders"], frames["plants"])
    before = Dataset(**frames, planning_start=start, data_version="before")
    table = args.table or args.file.stem
    if table not in TABLES:
        raise SystemExit(f"cannot tell the table from {args.file.name}; pass --table")
    new = pd.read_csv(args.file)
    after_frames = {**frames, table: pd.concat([frames[table], new], ignore_index=True)
                    if args.mode == "append" else new}
    after = replace(before, **{table: after_frames[table]}, data_version="after")
    print(to_markdown(replan_report(before, after), f"Late data drop: {args.file.name} ({table}, {args.mode})"))


if __name__ == "__main__":
    main()
