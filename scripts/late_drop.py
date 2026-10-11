"""Late data drop: load a new CSV through POST /ingest and re-plan through POST /analyze.

    python -m scripts.late_drop data/raw/capacity_events.csv [--table capacity_events] [--api http://localhost:8000]
    python -m scripts.late_drop data/raw/capacity_events.csv --in-process

Steps: (1) solve the current plan, (2) ingest the file, (3) solve again on the new data, (4) print what changed.
Both plans come from the solver; this script only compares them. Capacity events in the table are applied by the
solver itself, so they are NOT also sent as a scenario (that would count the loss twice).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)
REQUEST = "Allocate this week's orders"


def _client(in_process: bool, api: str) -> Any:
    if in_process:
        from fastapi.testclient import TestClient

        from backend.main import app

        return TestClient(app).__enter__()
    import httpx

    return httpx.Client(base_url=api, timeout=600)


def _analyze(client: Any) -> dict:
    r = client.post("/analyze", json={"request": REQUEST})
    r.raise_for_status()
    return r.json()


def diff_plans(before: dict, after: dict) -> list[dict]:
    old = {a["order_id"]: a for a in before["allocation"]}
    moved = []
    for a in after["allocation"]:
        b = old.get(a["order_id"])
        if b and (b["plant"], b["production_week"]) != (a["plant"], a["production_week"]):
            moved.append({"order_id": a["order_id"], "from": f"{b['plant']} wk {b['production_week']}",
                          "to": f"{a['plant']} wk {a['production_week']}", "on_time": a["on_time"]})
    return moved


def kpi_line(resp: dict) -> str:
    k, v = resp.get("kpis") or {}, resp.get("validator") or {}
    return (f"{resp['answer_type']}: cost {k.get('total_cost', 0):,.0f}, carbon {k.get('total_carbon_t', 0):,.1f} t, "
            f"late {k.get('late_orders')}, unassigned {k.get('unassigned_orders')}, "
            f"validator capacity_ok={v.get('capacity_ok')} eligibility_ok={v.get('eligibility_ok')} "
            f"(run {resp['run_id']})")


def run(path: Path, table: str, client: Any) -> dict:
    before = _analyze(client)
    with path.open("rb") as fh:
        r = client.post("/ingest", files={"file": (path.name, fh, "text/csv")}, data={"meta": json.dumps({"table": table})})
    ingest = r.json()
    if r.status_code != 200:
        return {"ingest": ingest, "error": "ingest failed; plan not re-run"}
    after = _analyze(client)
    return {"ingest": ingest, "before": kpi_line(before), "after": kpi_line(after), "moved": diff_plans(before, after),
            "limitations": after.get("limitations", [])}


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser(description="Ingest a late CSV and re-plan.")
    ap.add_argument("file", type=Path)
    ap.add_argument("--table", default=None, help="default: the file name without .csv")
    ap.add_argument("--api", default=os.getenv("API_URL", "http://localhost:8000"))
    ap.add_argument("--in-process", action="store_true")
    args = ap.parse_args()
    out = run(args.file, args.table or args.file.stem, _client(args.in_process, args.api))
    ing = out["ingest"]
    print(f"ingest {ing.get('table')}: {ing.get('status')} loaded={ing.get('rows_loaded')} "
          f"rejected={ing.get('rows_rejected')} data_version={ing.get('data_version')}")
    for e in ing.get("errors", []):
        print(f"    {e}")
    if "error" in out:
        raise SystemExit(out["error"])
    print(f"before  {out['before']}\nafter   {out['after']}\nmoved   {len(out['moved'])} orders")
    for m in out["moved"][:15]:
        print(f"    {m['order_id']}: {m['from']} -> {m['to']} on_time={m['on_time']}")


if __name__ == "__main__":
    main()
