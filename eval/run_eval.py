"""Run eval/cases.yaml against POST /analyze and compare configurations side by side.

    MOCK_LLM=true  python -m eval.run_eval --in-process --label mock   # rules router + template text
    MOCK_LLM=false python -m eval.run_eval --in-process --label llm    # qwen2.5:7b via Ollama
    python -m eval.run_eval --report mock llm                          # eval/report.md, side by side

Without --in-process the cases go to a running API (API_URL, default http://localhost:8000).
Each run writes eval/results_<label>.json. A case passes when its answer_type is expected, every condition
holds, and two rules from CLAUDE.md hold: a plan that is shown is valid, and the text is never unfaithful.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
PLAN_TYPES = {"allocation", "scenario", "brief"}


# ============================================================ cases
def normalise_case(c: dict) -> dict:
    """Accept both the flat `expect: [...]` format and `expected: {answer_type, conditions}`."""
    exp = c.get("expected", {})
    types = exp.get("answer_type", c.get("expect", []))
    return {"id": c["id"], "category": c.get("category", c["id"].rsplit("-", 1)[0]), "request": c["request"],
            "weights": c.get("weights"), "scenario": c.get("scenario"),
            "expect": [types] if isinstance(types, str) else list(types), "conditions": exp.get("conditions", [])}


def load_cases(path: Path) -> list[dict]:
    cases = [normalise_case(c) for c in yaml.safe_load(path.read_text())["cases"]]
    ids = [c["id"] for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case ids in cases.yaml")
    return cases


# ============================================================ condition evaluation
_MISSING = object()


def resolve(doc: Any, path: str) -> Any:
    cur = doc
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return _MISSING
    return cur


def _matches(item: dict, where: dict) -> bool:
    return all(item.get(k) == v for k, v in where.items())


def _project(items: list[dict], fields: list[str]) -> dict[Any, tuple]:
    key = fields[0]
    return {i.get(key): tuple(str(i.get(f)) for f in fields) for i in items}


def check_condition(cond: dict, doc: dict, others: dict[str, dict]) -> tuple[bool, str]:
    """Evaluate one condition against `doc` = {**response, baseline: {...}, trace: {...}}. Returns (ok, detail)."""
    path, op, want = cond["path"], cond["op"], cond.get("value")
    label = path + (f"[{cond['where']}]" if "where" in cond else "") + (f".{cond['field']}" if "field" in cond else "")
    if "compare_case" in cond:
        other = others.get(cond["compare_case"])
        if other is None:
            return False, f"{label}: compare_case {cond['compare_case']} has no response"
        a = _project(resolve(doc, path) or [], cond["projection"])
        b = _project(resolve(other, path) or [], cond["projection"])
        n = sum(a.get(k) != b.get(k) for k in set(a) | set(b))
        ok = n >= cond.get("min_changes", 1) if op == "differs" else n == 0
        return ok, f"{label} vs {cond['compare_case']}: {n} differ"
    val = resolve(doc, path)
    if "where" in cond:
        items = [i for i in (val if isinstance(val, list) else []) if _matches(i, cond["where"])]
        if "field" in cond:
            if not items:
                return False, f"{label}: no item matches"
            val = items[0].get(cond["field"], _MISSING)
        else:
            val = items
    if val is _MISSING and op not in ("is_null",):
        return False, f"{label}: missing"
    val = None if val is _MISSING else val
    ok = {
        "eq": lambda: val == want, "ne": lambda: val != want,
        "in": lambda: val in want, "not_in": lambda: val not in want,
        "is_null": lambda: val is None, "not_null": lambda: val is not None,
        "gt": lambda: val is not None and val > want, "gte": lambda: val is not None and val >= want,
        "lt": lambda: val is not None and val < want, "lte": lambda: val is not None and val <= want,
        "length_eq": lambda: len(val or []) == want, "length_gte": lambda: len(val or []) >= want,
    }.get(op)
    if ok is None:
        return False, f"{label}: unknown op {op!r}"
    shown = f"len={len(val)}" if isinstance(val, list) else repr(val)
    return bool(ok()), f"{label} {op} {want!r}: got {shown}"


# ============================================================ running
def make_client(in_process: bool, url: str) -> Any:
    if in_process:
        from fastapi.testclient import TestClient

        from backend.main import app

        return TestClient(app).__enter__()
    import httpx

    return httpx.Client(base_url=url, timeout=600)


def baseline_plan() -> list[dict]:
    """Cheapest-plant plan (ignores capacity and dates) on the same data: `baseline.allocation` in conditions."""
    try:
        from ai.tools.baseline import cheapest_plant_plan
        from backend import store

        store.init_db()
        return [a.model_dump(mode="json") for a in cheapest_plant_plan(store.load_dataset())]
    except Exception as exc:  # baseline is informative only
        log.warning("baseline unavailable: %s", exc)
        return []


def trace_signals(trace: dict) -> dict:
    """What the trace says about LLM use and fallbacks for one request."""
    steps = trace.get("steps", [])
    router = next((s for s in steps if s["tool"] == "parse_intent"), None)
    writers = [s for s in steps if s["agent"] == "writer"]
    final_writer = writers[-1]["tool"] if writers else None
    return {
        "parsed_by": (router.get("output") or {}).get("parsed_by") if router else None,
        "intent": (router.get("output") or {}).get("intent") if router else None,
        "writer": final_writer,
        "template_fallback": any(s["tool"] == "template_fallback" for s in steps),
        "guard_fails": sum(1 for s in steps if (s.get("check") or "").startswith("faithfulness: fail")),
        "write_attempts": sum(1 for s in writers),
        "llm_calls": trace.get("llm_calls", 0), "tokens": trace.get("tokens", 0),
    }


def run_case(client: Any, case: dict) -> tuple[dict, dict | None]:
    body = {"request": case["request"]}
    for key in ("weights", "scenario"):
        if case.get(key) is not None:
            body[key] = case[key]
    t0 = time.perf_counter()
    try:
        r = client.post("/analyze", json=body)
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        data = r.json()
    except Exception as exc:
        return {"id": case["id"], "category": case["category"], "ok": False, "error": str(exc)}, None
    out: dict[str, Any] = {"id": case["id"], "category": case["category"], "request": case["request"],
                           "http": r.status_code, "latency_ms": latency_ms, "expect": case["expect"]}
    if r.status_code != 200:
        out.update(ok=False, error=data.get("detail"))
        return out, None
    trace: dict = {}
    try:
        trace = client.get(f"/trace/{data['run_id']}").json()
    except Exception as exc:
        log.warning("trace fetch failed for %s: %s", data["run_id"], exc)
    v, k = data.get("validator"), data.get("kpis")
    out.update(
        run_id=data["run_id"], answer_type=data["answer_type"], type_ok=data["answer_type"] in case["expect"],
        valid=None if v is None else bool(v["capacity_ok"] and v["eligibility_ok"]),
        faithful=data.get("faithfulness_ok"), late_orders=k and k["late_orders"],
        cost=k and k["total_cost"], carbon_t=k and k["total_carbon_t"], **trace_signals(trace),
    )
    return out, {**data, "trace": trace}


def score(out: dict, case: dict, doc: dict | None, others: dict[str, dict]) -> None:
    """Fill in condition results and the pass/fail verdict."""
    if doc is None:
        return
    checks = [check_condition(c, doc, others) for c in case["conditions"]]
    if out["answer_type"] in PLAN_TYPES and out["valid"] is False:
        checks.append((False, "implicit: a plan that is shown must pass validate_plan"))
    if out["faithful"] is False:
        checks.append((False, "implicit: faithfulness_ok must not be false"))
    out["conditions_passed"] = sum(ok for ok, _ in checks)
    out["conditions_total"] = len(checks)
    out["failed_conditions"] = [d for ok, d in checks if not ok]
    out["ok"] = bool(out["type_ok"] and not out["failed_conditions"])


def run_all(cases: list[dict], client: Any) -> list[dict]:
    base = {"allocation": baseline_plan()}
    docs: dict[str, dict] = {}
    results = []
    for c in cases:
        out, data = run_case(client, c)
        if data is not None:
            docs[c["id"]] = {**data, "baseline": base}
        results.append(out)
        print(f"{c['id']:<18} {out.get('answer_type') or out.get('error')!s:<22} {out.get('latency_ms')} ms",
              flush=True)
    for out, c in zip(results, cases):  # after all runs, so compare_case can point forward or back
        score(out, c, docs.get(c["id"]), docs)
    return results


# ============================================================ summary + report
def percentile(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, round(p / 100 * (len(xs) - 1))))]


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 3) if den else None


def summarise(results: list[dict], label: str, llm_on: bool) -> dict:
    done = [r for r in results if "answer_type" in r]
    lat = [r["latency_ms"] for r in done]
    plans = [r for r in done if r["valid"] is not None and r["answer_type"] in PLAN_TYPES]
    faith = [r for r in done if r["faithful"] is not None]
    routed = [r for r in done if r["parsed_by"] is not None]
    written = [r for r in done if r["writer"] is not None]
    n = len(done) or 1
    return {
        "label": label, "llm_on": llm_on, "cases": len(results),
        "passed": sum(bool(r.get("ok")) for r in results),
        "answer_type_accuracy": _rate(sum(r["type_ok"] for r in done), len(results)),
        "conditions_pass_rate": _rate(sum(r.get("conditions_passed", 0) for r in done),
                                      sum(r.get("conditions_total", 0) for r in done)),
        "plan_validity_rate": _rate(sum(r["valid"] for r in plans), len(plans)),
        "faithfulness_rate": _rate(sum(r["faithful"] for r in faith), len(faith)),
        "router_llm_rate": _rate(sum(r["parsed_by"] == "llm" for r in routed), len(routed)),
        "router_fallback_rate": _rate(sum(r["parsed_by"] == "rules" for r in routed), len(routed)) if llm_on else None,
        "writer_template_rate": _rate(sum(r["writer"] == "template_summary" or r["template_fallback"]
                                          for r in written), len(written)),
        "faithfulness_retries": sum(r["guard_fails"] for r in done),
        "template_fallbacks_after_retry": sum(r["template_fallback"] for r in done),
        "latency_p50_ms": percentile(lat, 50), "latency_p95_ms": percentile(lat, 95),
        "llm_calls_per_request": round(sum(r["llm_calls"] for r in done) / n, 2),
        "tokens_per_request": round(sum(r["tokens"] for r in done) / n, 1),
        "tokens_total": sum(r["tokens"] for r in done),
    }


def _fmt(v: Any, kind: str = "") -> str:
    if v is None:
        return "n/a"
    if kind == "pct":
        return f"{v:.0%}"
    if kind == "ms":
        return f"{v / 1000:,.1f} s"
    return f"{v:,}" if isinstance(v, (int, float)) else str(v)


ROWS = [
    ("Cases passed", "passed", ""), ("Answer-type (intent) accuracy", "answer_type_accuracy", "pct"),
    ("Condition checks passed", "conditions_pass_rate", "pct"), ("Plan validity", "plan_validity_rate", "pct"),
    ("Faithfulness (number guard)", "faithfulness_rate", "pct"),
    ("Router parsed by LLM", "router_llm_rate", "pct"), ("Router fallback to rules", "router_fallback_rate", "pct"),
    ("Text from template", "writer_template_rate", "pct"),
    ("Faithfulness retries", "faithfulness_retries", ""),
    ("Template after 2 failed writes", "template_fallbacks_after_retry", ""),
    ("Latency p50", "latency_p50_ms", "ms"), ("Latency p95", "latency_p95_ms", "ms"),
    ("LLM calls per request", "llm_calls_per_request", ""), ("Tokens per request", "tokens_per_request", ""),
]


def report_md(runs: dict[str, dict]) -> str:
    labels = list(runs)
    sums = {lab: runs[lab]["summary"] for lab in labels}
    head = " | ".join(f"{lab} ({'qwen2.5:7b' if sums[lab]['llm_on'] else 'MOCK_LLM'})" for lab in labels)
    lines = ["# Eval report: two configurations", "",
             f"Same {sums[labels[0]]['cases']} cases (`eval/cases.yaml`), same data (`data/raw`, seed 42), "
             "same solver. Only the language layer changes.", "",
             f"| metric | {head} |", "|---|" + "---|" * len(labels)]
    for name, key, kind in ROWS:
        cells = []
        for lab in labels:
            v = sums[lab][key]
            cells.append(f"{v}/{sums[lab]['cases']}" if key == "passed" else _fmt(v, kind))
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines += ["", "Router fallback is n/a for MOCK_LLM: the rules router is the configuration, not a fallback.",
              "Plan validity counts only answers that present a plan (allocation, scenario, brief).", "",
              "## Per case", "", "| case | category | expected | " + " | ".join(labels) + " |",
              "|---|---|---|" + "---|" * len(labels)]
    by_id = {lab: {r["id"]: r for r in runs[lab]["results"]} for lab in labels}
    for r in runs[labels[0]]["results"]:
        cells = []
        for lab in labels:
            x = by_id[lab].get(r["id"], {})
            verdict = "PASS" if x.get("ok") else "FAIL"
            cells.append(f"{verdict} {x.get('answer_type', x.get('error', ''))}, {_fmt(x.get('latency_ms'), 'ms')}")
        lines.append(f"| {r['id']} | {r['category']} | {'/'.join(r.get('expect', []))} | " + " | ".join(cells) + " |")
    lines += ["", "## Failed conditions", ""]
    for lab in labels:
        fails = [r for r in runs[lab]["results"] if not r.get("ok")]
        lines.append(f"**{lab}** ({len(fails)} failing cases)")
        lines.append("")
        for r in fails:
            why = r.get("failed_conditions") or []
            if not r.get("type_ok", False):
                why = [f"answer_type {r.get('answer_type', r.get('error'))} not in {r.get('expect')}"] + why
            lines.append(f"- `{r['id']}`: " + "; ".join(why))
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser(description="Run the /analyze eval suite.")
    ap.add_argument("--cases", default=HERE / "cases.yaml", type=Path)
    ap.add_argument("--url", default=os.getenv("API_URL", "http://localhost:8000"))
    ap.add_argument("--in-process", action="store_true", help="use FastAPI TestClient instead of a running server")
    ap.add_argument("--label", default=None, help="results file suffix (default: mock or llm from MOCK_LLM)")
    ap.add_argument("--only", nargs="*", help="case ids to run")
    ap.add_argument("--report", nargs="+", metavar="LABEL", help="only build eval/report.md from saved results")
    args = ap.parse_args()

    if args.report:
        runs = {lab: json.loads((HERE / f"results_{lab}.json").read_text()) for lab in args.report}
        (HERE / "report.md").write_text(report_md(runs))
        print(f"wrote {HERE / 'report.md'}")
        return

    llm_on = os.getenv("MOCK_LLM", "false").lower() != "true"
    label = args.label or ("llm" if llm_on else "mock")
    cases = [c for c in load_cases(args.cases) if not args.only or c["id"] in args.only]
    results = run_all(cases, make_client(args.in_process, args.url))
    s = summarise(results, label, llm_on)
    out = HERE / f"results_{label}.json"
    out.write_text(json.dumps({"summary": s, "results": results}, indent=2, default=str))
    print(json.dumps(s, indent=2))
    print(f"wrote {out}")
    if s["passed"] < s["cases"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
