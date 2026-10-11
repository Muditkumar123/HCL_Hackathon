"""Run eval/cases.yaml against POST /analyze and report quality, cost/carbon vs a naive baseline, and latency.

    python -m eval.run_eval                         # against API_URL (default http://localhost:8000)
    python -m eval.run_eval --in-process            # no server needed: FastAPI TestClient on the local DB

Writes eval/results.json (per case) and eval/report.md (summary). Exit code 1 if any case fails.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import time
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
PLAN_TYPES = {"allocation", "scenario", "brief", "infeasible"}


def load_cases(path: Path) -> list[dict]:
    cases = yaml.safe_load(path.read_text())["cases"]
    ids = [c["id"] for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case ids in cases.yaml")
    return cases


def make_client(in_process: bool, url: str) -> Any:
    if in_process:
        from fastapi.testclient import TestClient

        from backend.main import app

        return TestClient(app).__enter__()
    import httpx

    return httpx.Client(base_url=url, timeout=300)


def baseline() -> dict[str, float] | None:
    """Cheapest-plant plan (ignores capacity and dates) on the same data, for comparison."""
    try:
        from ai.tools.baseline import cheapest_plant_plan
        from backend import store

        store.init_db()
        plan = cheapest_plant_plan(store.load_dataset())
    except Exception as exc:  # baseline is informative only
        log.warning("baseline unavailable: %s", exc)
        return None
    return {"cost": sum(a.cost for a in plan), "carbon_t": sum(a.carbon_kg for a in plan) / 1000,
            "late_orders": sum(not a.on_time for a in plan)}


def run_case(client: Any, case: dict) -> dict:
    body = {"request": case["request"]}
    for key in ("weights", "scenario"):
        if key in case:
            body[key] = case[key]
    t0 = time.perf_counter()
    try:
        r = client.post("/analyze", json=body)
        latency_ms = (time.perf_counter() - t0) * 1000
        data = r.json()
    except Exception as exc:
        return {"id": case["id"], "ok": False, "http": None, "error": str(exc), "latency_ms": None}
    out: dict[str, Any] = {"id": case["id"], "request": case["request"], "http": r.status_code,
                           "latency_ms": round(latency_ms, 1), "expect": case["expect"]}
    if r.status_code != 200:
        out.update(ok=False, error=data.get("detail"))
        return out
    at = data["answer_type"]
    v = data.get("validator")
    k = data.get("kpis")
    out.update(
        run_id=data["run_id"], answer_type=at, type_ok=at in case["expect"],
        valid=None if v is None else bool(v["capacity_ok"] and v["eligibility_ok"]),
        faithful=data.get("faithfulness_ok"),
        cost=k and k["total_cost"], carbon_t=k and k["total_carbon_t"], late_orders=k and k["late_orders"],
        unassigned=k and k["unassigned_orders"],
    )
    # A plan shown to the user must be valid (rule 4); an "infeasible" answer is allowed to say otherwise.
    plan_ok = not (at in PLAN_TYPES - {"infeasible"} and k is not None and out["valid"] is False)
    out["ok"] = bool(out["type_ok"] and plan_ok and out["faithful"] is not False)
    try:
        tr = client.get(f"/trace/{data['run_id']}").json()
        out.update(llm_calls=tr.get("llm_calls", 0), tokens=tr.get("tokens", 0), trace_ms=tr.get("latency_ms"))
    except Exception as exc:
        log.warning("trace fetch failed for %s: %s", data["run_id"], exc)
    return out


def percentile(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    i = min(len(xs) - 1, max(0, round(p / 100 * (len(xs) - 1))))
    return xs[i]


def summarise(results: list[dict], base: dict[str, float] | None) -> dict:
    lat = [r["latency_ms"] for r in results if r.get("latency_ms") is not None]
    plans = [r for r in results if r.get("valid") is not None]
    faith = [r for r in results if r.get("faithful") is not None]
    alloc = next((r for r in results if r.get("answer_type") == "allocation" and r.get("cost") is not None), None)
    s: dict[str, Any] = {
        "cases": len(results),
        "passed": sum(r["ok"] for r in results),
        "answer_type_accuracy": round(sum(bool(r.get("type_ok")) for r in results) / len(results), 3),
        "plan_validity_rate": round(sum(r["valid"] for r in plans) / len(plans), 3) if plans else None,
        "faithfulness_rate": round(sum(r["faithful"] for r in faith) / len(faith), 3) if faith else None,
        "latency_p50_ms": percentile(lat, 50), "latency_p95_ms": percentile(lat, 95),
        "llm_calls_total": sum(r.get("llm_calls", 0) for r in results),
        "tokens_total": sum(r.get("tokens", 0) for r in results),
        "baseline_cheapest_plant": base,
    }
    if alloc and base:
        s["plan_vs_baseline"] = {
            "run_id": alloc["run_id"], "plan_cost": alloc["cost"], "plan_carbon_t": alloc["carbon_t"],
            "plan_late_orders": alloc["late_orders"],
            "cost_delta_pct": round((alloc["cost"] - base["cost"]) / base["cost"] * 100, 2),
            "carbon_delta_pct": round((alloc["carbon_t"] - base["carbon_t"]) / base["carbon_t"] * 100, 2),
            "late_orders_delta": alloc["late_orders"] - base["late_orders"],
        }
    return s


def report_md(results: list[dict], s: dict) -> str:
    lines = ["# Eval report", "", f"**{s['passed']}/{s['cases']} cases passed.** "
             f"Answer-type accuracy {s['answer_type_accuracy']:.0%}, plan validity {s['plan_validity_rate']}, "
             f"faithfulness {s['faithfulness_rate']}, latency p50 {s['latency_p50_ms']} ms / "
             f"p95 {s['latency_p95_ms']} ms, LLM calls {s['llm_calls_total']}, tokens {s['tokens_total']}.", ""]
    if "plan_vs_baseline" in s:
        b, pv = s["baseline_cheapest_plant"], s["plan_vs_baseline"]
        lines += ["## Plan vs cheapest-plant baseline (baseline ignores capacity and due dates)", "",
                  "| | cost | carbon t | late orders |", "|---|---|---|---|",
                  f"| baseline | {b['cost']:,.0f} | {b['carbon_t']:,.1f} | {b['late_orders']} |",
                  f"| plan ({pv['run_id']}) | {pv['plan_cost']:,.0f} | {pv['plan_carbon_t']:,.1f} | "
                  f"{pv['plan_late_orders']} |",
                  f"| delta | {pv['cost_delta_pct']:+.1f}% | {pv['carbon_delta_pct']:+.1f}% | "
                  f"{pv['late_orders_delta']:+d} |", ""]
    lines += ["## Cases", "", "| id | ok | answer_type | expected | valid | faithful | late | ms | llm | tokens |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['id']} | {'PASS' if r['ok'] else 'FAIL'} | {r.get('answer_type', r.get('error'))} | "
                     f"{'/'.join(r.get('expect', []))} | {r.get('valid')} | {r.get('faithful')} | "
                     f"{r.get('late_orders')} | {r.get('latency_ms')} | {r.get('llm_calls')} | {r.get('tokens')} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser(description="Run the /analyze eval suite.")
    ap.add_argument("--cases", default=HERE / "cases.yaml", type=Path)
    ap.add_argument("--url", default=os.getenv("API_URL", "http://localhost:8000"))
    ap.add_argument("--in-process", action="store_true", help="use FastAPI TestClient instead of a running server")
    ap.add_argument("--only", nargs="*", help="case ids to run")
    args = ap.parse_args()

    cases = [c for c in load_cases(args.cases) if not args.only or c["id"] in args.only]
    client = make_client(args.in_process, args.url)
    results = []
    for c in cases:
        r = run_case(client, c)
        print(f"{r['id']:<12} {'PASS' if r['ok'] else 'FAIL'}  {r.get('answer_type') or r.get('error')}  "
              f"{r.get('latency_ms')} ms", flush=True)
        results.append(r)
    s = summarise(results, baseline())
    (HERE / "results.json").write_text(json.dumps({"summary": s, "results": results}, indent=2, default=str))
    (HERE / "report.md").write_text(report_md(results, s))
    print(json.dumps(s, indent=2, default=str))
    if s["passed"] < s["cases"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
