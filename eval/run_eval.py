"""Run the eval suites against POST /analyze and compare configurations side by side.

    MOCK_LLM=true  python -m eval.run_eval --in-process --label mock   # rules router + template text
    MOCK_LLM=false python -m eval.run_eval --in-process --label llm    # qwen2.5:7b via Ollama
    python -m eval.run_eval --report mock llm                          # eval/report.md, side by side

Two suites run by default:
  * team      eval/cases_team.yaml  (24 cases written by the team, answer_type only)
  * auditor   eval/cases.yaml       (26 independent cases written by Codex, with conditions;
                                     format agreed in .coordination/reviews/EVAL-FORMAT.md)

Without --in-process the cases go to a running API (API_URL, default http://localhost:8000).
Each run writes eval/results_<label>.json. A case passes when the response is HTTP 200 and a valid
AnalyzeResponse, its answer_type is expected, every condition holds, and two rules from CLAUDE.md hold:
a plan that is shown is valid, and the text is never unfaithful. Conditions fail closed and are never eval'd.
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
SUITES: dict[str, tuple[str, Path]] = {
    "team": ("Team cases", HERE / "cases_team.yaml"),
    "auditor": ("Independent auditor cases", HERE / "cases.yaml"),
}


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
        raise ValueError(f"duplicate case ids in {path.name}")
    return cases


# ============================================================ condition evaluation (fail closed)
_MISSING = object()


def resolve(doc: Any, path: str) -> Any:
    cur = doc
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return _MISSING
    return cur


def _matches(item: Any, where: dict) -> bool:
    return isinstance(item, dict) and all(k in item and item[k] == v for k, v in where.items())


def _project(items: list[dict], fields: list[str]) -> dict[Any, tuple]:
    key = fields[0]
    return {i.get(key): tuple(str(i.get(f)) for f in fields) for i in items}


def _apply_op(op: str, val: Any, want: Any) -> bool | None:
    """True/False for a known operator, None for an unknown one. Type errors count as False."""
    ops = {
        "eq": lambda: val == want, "ne": lambda: val != want,
        "in": lambda: val in want, "not_in": lambda: val not in want,
        "is_null": lambda: val is None, "not_null": lambda: val is not None,
        "gt": lambda: val > want, "gte": lambda: val >= want, "lt": lambda: val < want, "lte": lambda: val <= want,
        "contains": lambda: want in val, "not_contains": lambda: want not in val,
        "length_eq": lambda: len(val) == want, "length_gte": lambda: len(val) >= want,
    }
    if op not in ops:
        return None
    try:
        return bool(ops[op]())
    except TypeError:
        return False


def check_condition(cond: dict, doc: dict, others: dict[str, dict]) -> tuple[bool, str]:
    """Evaluate one condition against `doc` = {**response, baseline: {...}, trace: {...}}. Returns (ok, detail)."""
    path, op, want = cond.get("path", "?"), cond.get("op"), cond.get("value")
    label = path + (f"[{cond['where']}]" if "where" in cond else "") + (f".{cond['field']}" if "field" in cond else "")
    if "compare_case" in cond:
        other = others.get(cond["compare_case"])
        if other is None:
            return False, f"{label}: unknown or failed compare_case {cond['compare_case']!r}"
        a, b = resolve(doc, path), resolve(other, path)
        if not isinstance(a, list) or not isinstance(b, list):
            return False, f"{label} vs {cond['compare_case']}: path is not a list"
        pa, pb = _project(a, cond["projection"]), _project(b, cond["projection"])
        n = sum(pa.get(k) != pb.get(k) for k in set(pa) | set(pb))
        if op == "differs":
            return n >= cond.get("min_changes", 1), f"{label} vs {cond['compare_case']}: {n} differ"
        if op == "same":
            return n == 0, f"{label} vs {cond['compare_case']}: {n} differ"
        return False, f"{label}: unknown comparison op {op!r}"
    val = resolve(doc, path)
    if val is _MISSING:
        return False, f"{label}: path missing"
    if "where" in cond:
        if not isinstance(val, list):
            return False, f"{label}: `where` needs a list"
        items = [i for i in val if _matches(i, cond["where"])]
        if "field" in cond:
            if len(items) != 1:
                return False, f"{label}: expected exactly 1 matching row, got {len(items)}"
            if cond["field"] not in items[0]:
                return False, f"{label}: field missing"
            val = items[0][cond["field"]]
        else:
            val = items
    ok = _apply_op(op, val, want)
    if ok is None:
        return False, f"{label}: unknown op {op!r}"
    shown = f"len={len(val)}" if isinstance(val, list) else repr(val)
    return ok, f"{label} {op} {want!r}: got {shown}"


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
    except Exception as exc:  # conditions on baseline.* then fail closed
        log.warning("baseline unavailable: %s", exc)
        return []


def trace_signals(trace: dict) -> dict:
    """What the trace says about LLM use and fallbacks for one request."""
    steps = trace.get("steps", [])
    router = next((s for s in steps if s["tool"] == "parse_intent"), None)
    writers = [s for s in steps if s["agent"] == "writer"]
    return {
        "parsed_by": (router.get("output") or {}).get("parsed_by") if router else None,
        "intent": (router.get("output") or {}).get("intent") if router else None,
        "writer": writers[-1]["tool"] if writers else None,
        "template_fallback": any(s["tool"] == "template_fallback" for s in steps),
        "guard_fails": sum(1 for s in steps if (s.get("check") or "").startswith("faithfulness: fail")),
        "llm_calls": trace.get("llm_calls", 0), "tokens": trace.get("tokens", 0),
    }


def run_case(client: Any, case: dict) -> tuple[dict, dict | None]:
    from pydantic import ValidationError

    from shared.models import AnalyzeResponse

    body = {"request": case["request"]}
    for key in ("weights", "scenario"):
        if case.get(key) is not None:
            body[key] = case[key]
    out: dict[str, Any] = {"id": case["id"], "category": case["category"], "request": case["request"],
                           "expect": case["expect"], "ok": False}
    t0 = time.perf_counter()
    try:
        r = client.post("/analyze", json=body)
        out["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        data = r.json()
    except Exception as exc:
        out["error"] = f"request failed: {exc}"
        return out, None
    out["http"] = r.status_code
    if r.status_code != 200:
        out["error"] = f"HTTP {r.status_code}: {data.get('detail') if isinstance(data, dict) else data}"
        return out, None
    try:
        AnalyzeResponse.model_validate(data)
    except ValidationError as exc:
        out["error"] = f"response is not a valid AnalyzeResponse: {exc.error_count()} errors"
        return out, None
    trace: dict = {}
    try:
        trace = client.get(f"/trace/{data['run_id']}").json()
    except Exception as exc:  # trace.* conditions then fail closed
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


def run_suite(cases: list[dict], client: Any, baseline: list[dict]) -> list[dict]:
    docs: dict[str, dict] = {}
    results = []
    for c in cases:
        out, data = run_case(client, c)
        if data is not None:
            docs[c["id"]] = {**data, "baseline": {"allocation": baseline}}
        results.append(out)
        print(f"  {c['id']:<18} {out.get('answer_type') or out.get('error')!s:<22} {out.get('latency_ms')} ms",
              flush=True)
    for out, c in zip(results, cases):  # after the whole suite, so compare_case can point forward or back
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


def summarise(results: list[dict], llm_on: bool) -> dict:
    done = [r for r in results if "answer_type" in r]
    lat = [r["latency_ms"] for r in done]
    plans = [r for r in done if r["valid"] is not None and r["answer_type"] in PLAN_TYPES]
    faith = [r for r in done if r["faithful"] is not None]
    routed = [r for r in done if r["parsed_by"] is not None]
    written = [r for r in done if r["writer"] is not None]
    n = len(done) or 1
    return {
        "cases": len(results), "passed": sum(bool(r.get("ok")) for r in results),
        "errors": sum("error" in r for r in results),
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


def _suite_md(suite: str, runs: dict[str, dict]) -> list[str]:
    labels = [lab for lab in runs if suite in runs[lab]["suites"]]
    title, path = SUITES[suite]
    sums = {lab: runs[lab]["suites"][suite]["summary"] for lab in labels}
    res = {lab: runs[lab]["suites"][suite]["results"] for lab in labels}
    head = " | ".join(f"{lab} ({'qwen2.5:7b' if runs[lab]['llm_on'] else 'MOCK_LLM'})" for lab in labels)
    lines = [f"## {title}", "", f"`eval/{path.name}`, {sums[labels[0]]['cases']} cases.", "",
             f"| metric | {head} |", "|---|" + "---|" * len(labels)]
    for name, key, kind in ROWS:
        cells = [f"{sums[lab][key]}/{sums[lab]['cases']}" if key == "passed" else _fmt(sums[lab][key], kind)
                 for lab in labels]
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines += ["", f"### {title}: per case", "", "| case | category | expected | " + " | ".join(labels) + " |",
              "|---|---|---|" + "---|" * len(labels)]
    by_id = {lab: {r["id"]: r for r in res[lab]} for lab in labels}
    for r in res[labels[0]]:
        cells = []
        for lab in labels:
            x = by_id[lab].get(r["id"], {})
            what = x.get("answer_type") or "error"
            cells.append(f"{'PASS' if x.get('ok') else 'FAIL'} {what}, {_fmt(x.get('latency_ms'), 'ms')}")
        lines.append(f"| {r['id']} | {r['category']} | {'/'.join(r.get('expect', []))} | " + " | ".join(cells) + " |")
    lines += ["", f"### {title}: why cases failed", ""]
    for lab in labels:
        fails = [r for r in res[lab] if not r.get("ok")]
        lines += [f"**{lab}** ({len(fails)} failing)", ""]
        for r in fails:
            why = list(r.get("failed_conditions") or [])
            if "error" in r:
                why = [r["error"]]
            elif not r.get("type_ok", False):
                why = [f"answer_type {r.get('answer_type')} not in {r.get('expect')}"] + why
            lines.append(f"- `{r['id']}`: " + "; ".join(why))
        lines.append("")
    return lines


CONFIG_NAMES = {True: "qwen2.5:7b-instruct via Ollama (GPU)", False: "MOCK_LLM=true (rules router + template text)"}


def report_md(runs: dict[str, dict]) -> str:
    labels = list(runs)
    suites = [s for s in SUITES if any(s in runs[lab]["suites"] for lab in labels)]
    total = {lab: (sum(runs[lab]["suites"][s]["summary"]["passed"] for s in runs[lab]["suites"]),
                   sum(runs[lab]["suites"][s]["summary"]["cases"] for s in runs[lab]["suites"])) for lab in labels}
    lines = ["# Eval report: two configurations", "",
             "Same cases, same data (`data/raw`, seed 42), same solver. Only the language layer changes: "
             + ", ".join(f"**{lab}** = {CONFIG_NAMES[runs[lab]['llm_on']]}" for lab in labels) + ".", "",
             "Overall: " + "; ".join(f"{lab} {p}/{n}" for lab, (p, n) in total.items()) + " cases passed.", "",
             "Router fallback is n/a for MOCK_LLM (the rules router is the configuration, not a fallback). "
             "Plan validity counts only answers that present a plan (allocation, scenario, brief).", ""]
    for s in suites:
        lines += _suite_md(s, runs)
    return "\n".join(lines)


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser(description="Run the /analyze eval suites.")
    ap.add_argument("--suite", nargs="+", choices=list(SUITES), default=list(SUITES))
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
    client = make_client(args.in_process, args.url)
    baseline = baseline_plan()
    out: dict[str, Any] = {"label": label, "llm_on": llm_on, "suites": {}}
    for suite in args.suite:
        cases = [c for c in load_cases(SUITES[suite][1]) if not args.only or c["id"] in args.only]
        print(f"== {SUITES[suite][0]} ({len(cases)})", flush=True)
        results = run_suite(cases, client, baseline)
        out["suites"][suite] = {"summary": summarise(results, llm_on), "results": results}
        print(json.dumps(out["suites"][suite]["summary"], indent=2))
    path = HERE / f"results_{label}.json"
    path.write_text(json.dumps(out, indent=2, default=str))
    print(f"wrote {path}")
    if any(s["summary"]["passed"] < s["summary"]["cases"] for s in out["suites"].values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
