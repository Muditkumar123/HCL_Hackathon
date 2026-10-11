"""LangGraph flow: a fixed DAG with guard nodes and one bounded retry loop.

    START
      │
    input_guard (G1) ──blocked──────────────────────────────────────────────┐
      │ ok                                                                   │
    parse_intent (LLM #1, constrained JSON; rules fallback)                  │
      │                                                                      │
    intent_guard (G2) ──blocked─────────────────────────────────────────────┤
      │ ok, routed by intent                                                 │
    allocate | tradeoff | replan | explain | brief    (solver tools only)    │
      │                                                                      │
    checker (G3: validate_plan gate, builds FACTS from tool outputs)         │
      │                                                                      │
    write (LLM #2) ◄──retry once with offending numbers──┐                   │
      │                                                   │                  │
    output_guard (G4: number faithfulness) ──fail────────┘                   │
      │ pass            └──fail twice──► template_fallback ──┐               │
      ▼                                                       ▼              ▼
    finalize (G5: approval flags, recommendations, trace) ◄──────────────────┘

Why LangGraph and not a plain function chain: conditional routing, a bounded retry cycle and a typed
shared state, with every node writing to the trace. Why not a ReAct agent: every step except language
is deterministic, so letting an LLM choose tools would add failure modes and cost with no benefit.
"""

from __future__ import annotations

import logging
import operator
import os
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Annotated, Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from ai.generation import guards, router, writer
from shared.models import (
    AnalyzeRequest, AnalyzeResponse, AnswerType, CounterfactualResult, Dataset, Intent, ParsedIntent,
    PlanChange, Recommendation, SolveResult, TraceRecord, TraceStep, TradeoffPoint, ValidatorResult,
    Weights, new_run_id,
)

log = logging.getLogger(__name__)
MAX_WRITE_ATTEMPTS = 2
DEFAULT_CUTS = (0.0, 5.0, 10.0, 15.0, 20.0, 30.0)


# ============================================================ dependencies (injected => testable)
@dataclass
class Deps:
    solve: Callable[..., SolveResult]
    validate: Callable[..., ValidatorResult]
    frontier: Callable[..., list[TradeoffPoint]]
    compare: Callable[..., list[PlanChange]]
    counterfactual: Callable[..., CounterfactualResult]
    parse_intent: Callable[[str], tuple[ParsedIntent, int, bool]] = router.parse_intent
    write: Callable[..., tuple[str | None, int]] = writer.write
    base_plan: Callable[[], SolveResult | None] = lambda: None   # backend.store.latest_plan
    _graph: Any = field(default=None, init=False, repr=False, compare=False)

    def graph(self) -> Any:
        """Compiled once per Deps. Nodes read deps attributes at call time, so later edits still apply."""
        if self._graph is None:
            self._graph = build_graph(self)
        return self._graph


def use_tool_stubs() -> bool:
    return os.getenv("USE_TOOL_STUBS", "false").lower() == "true"


def default_deps() -> Deps:
    if use_tool_stubs():  # development only (CLAUDE.md rule 2): must be off for the demo
        log.warning("USE_TOOL_STUBS=true: using ai/generation/tool_stubs.py instead of ai/tools")
        from ai.generation.tool_stubs import (
            compare_plans, counterfactual, solve_allocation, tradeoff_frontier, validate_plan,
        )
    else:
        from ai.tools.scenarios import compare_plans, counterfactual, tradeoff_frontier
        from ai.tools.solver import solve_allocation
        from ai.tools.validator import validate_plan

    try:
        from backend.store import latest_plan
    except ImportError:
        latest_plan = lambda: None  # noqa: E731
    return Deps(solve=solve_allocation, validate=validate_plan, frontier=tradeoff_frontier,
                compare=compare_plans, counterfactual=counterfactual, base_plan=latest_plan)


@lru_cache(maxsize=1)
def _default() -> Deps:
    """The process-wide Deps used by run() when the caller passes none. default_deps() stays a fresh factory,
    so callers that modify the Deps they get (tests) never change this shared one."""
    return default_deps()


# ============================================================ state
class State(TypedDict, total=False):
    req: AnalyzeRequest
    ds: Dataset
    run_id: str
    cleaned: str
    intent: ParsedIntent
    weights: Weights
    answer_type: AnswerType
    blocked_reason: str
    result: SolveResult
    base: SolveResult
    tradeoff: list[TradeoffPoint]
    changes: list[PlanChange]
    counterfactuals: list[CounterfactualResult]
    validator: ValidatorResult
    facts: dict
    mode: str
    text: str
    bad_numbers: list[float]
    write_attempts: int
    faithful: bool
    used_template: bool
    assumptions: Annotated[list[str], operator.add]
    limitations: Annotated[list[str], operator.add]
    steps: Annotated[list[TraceStep], operator.add]
    llm_calls: Annotated[int, operator.add]
    tokens: Annotated[int, operator.add]


def _step(agent: str, tool: str, t0: float, **kw: Any) -> list[TraceStep]:
    return [TraceStep(agent=agent, tool=tool, duration_ms=round((time.perf_counter() - t0) * 1000, 1), **kw)]


# ============================================================ formatting (code formats, LLM copies)
def money(x: float) -> str:
    return f"{x:,.0f}"


def signed_money(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}{abs(x):,.0f}"


def tonnes(kg: float) -> str:
    return f"{kg / 1000:,.1f} t"


def pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _scenario_numbers(req: AnalyzeRequest) -> set[float]:
    if req.scenario is None:
        return set()
    nums = set(guards.extract_numbers(req.scenario.model_dump_json()))
    return nums | {round(n * 100, 4) for n in nums if 0 < n <= 1}


def _order_plant(res: SolveResult, order_id: str) -> str | None:
    return next((a.plant for a in res.assignments if a.order_id == order_id), None)


def _eligible_plants(ds: Dataset, order_id: str) -> list[str]:
    product = ds.orders.loc[ds.orders["order_id"] == order_id, "product"].iloc[0]
    return sorted(set(ds.plants.loc[ds.plants["product"] == product, "plant"].astype(str)))


# ============================================================ graph
def build_graph(deps: Deps):
    # ---------------- G1
    def input_guard(s: State) -> dict:
        t0 = time.perf_counter()
        v = guards.input_guard(s["req"].request)
        out: dict = {"steps": _step("guard", "input_guard", t0, check=v.check), "limitations": v.notes}
        if v.ok:
            out["cleaned"] = v.cleaned
        else:
            out.update(answer_type=v.answer_type, blocked_reason=v.reason)
        return out

    def parse_intent(s: State) -> dict:
        t0 = time.perf_counter()
        intent, tokens, used_llm = deps.parse_intent(s["cleaned"])
        req = s["req"]
        if req.scenario is not None:                       # explicit API scenario beats parsed text
            if req.scenario.capacity_changes:
                intent.capacity_changes = list(req.scenario.capacity_changes)
                if intent.intent in (Intent.ALLOCATE, Intent.CLARIFY):
                    intent.intent = Intent.REPLAN
            if req.scenario.carbon_cut_pct is not None:
                intent.carbon_cut_pct = req.scenario.carbon_cut_pct
        return {"intent": intent, "llm_calls": int(used_llm), "tokens": tokens,
                "steps": _step("router", "parse_intent", t0,
                               output={"intent": intent.intent.value, "parsed_by": intent.parsed_by,
                                       "reason": intent.reason})}

    # ---------------- G2
    def intent_guard(s: State) -> dict:
        t0 = time.perf_counter()
        v, intent = guards.intent_guard(s["intent"], s["req"].request, _scenario_numbers(s["req"]), s["ds"])
        weights = intent.weights_override or s["req"].weights or Weights()
        out: dict = {"intent": intent, "weights": weights, "assumptions": v.notes,
                     "steps": _step("guard", "intent_guard", t0, check=v.check)}
        if not v.ok:
            out.update(answer_type=v.answer_type, blocked_reason=v.reason)
        return out

    def route_after_input(s: State) -> str:
        return "finalize" if s.get("blocked_reason") else "parse_intent"

    def route_after_intent(s: State) -> str:
        if s.get("blocked_reason"):
            return "finalize"
        return {Intent.ALLOCATE: "allocate", Intent.TRADEOFF: "tradeoff", Intent.REPLAN: "replan",
                Intent.EXPLAIN: "explain", Intent.BRIEF: "brief"}[s["intent"].intent]

    # ---------------- tool nodes (all numbers come from here)
    def allocate(s: State) -> dict:
        t0, ds, it, w = time.perf_counter(), s["ds"], s["intent"], s["weights"]
        res = deps.solve(ds, w, require_all_on_time=it.require_all_on_time)
        steps = _step("optimiser", "solve_allocation", t0,
                      input={"weights": w.model_dump(), "require_all_on_time": it.require_all_on_time},
                      output={"status": res.status, "solve_s": res.solve_s})
        if not it.force_assignments:
            return {"result": res, "answer_type": AnswerType.ALLOCATION, "steps": steps, "mode": "summary"}
        # manager override: price it with a forced re-solve instead of refusing
        t1 = time.perf_counter()
        forced = deps.solve(ds, w, forced=it.force_assignments, base_plan=res.assignments)
        changes = deps.compare(ds, res, forced)
        steps += _step("scenario", "solve_allocation(forced)+compare_plans", t1,
                       input={"forced": it.force_assignments},
                       output={"status": forced.status, "orders_moved": len(changes)})
        return {"base": res, "result": forced, "changes": changes, "answer_type": AnswerType.SCENARIO,
                "steps": steps, "mode": "summary"}

    def tradeoff(s: State) -> dict:
        t0, ds, w = time.perf_counter(), s["ds"], s["weights"]
        res = deps.solve(ds, w)
        cut = s["intent"].carbon_cut_pct
        cuts = tuple(sorted(set(DEFAULT_CUTS) | ({cut} if cut is not None else set())))
        points = deps.frontier(ds, w, cuts=cuts)
        return {"result": res, "tradeoff": points, "answer_type": AnswerType.SCENARIO, "mode": "summary",
                "steps": _step("scenario", "tradeoff_frontier", t0, input={"cuts": list(cuts)},
                               output={"points": len(points)})}

    def replan(s: State) -> dict:
        t0, ds, w, ch = time.perf_counter(), s["ds"], s["weights"], s["intent"].capacity_changes
        base = deps.base_plan() or deps.solve(ds, w)
        new = deps.solve(ds, w, changes=ch, base_plan=base.assignments)
        changes = deps.compare(ds, base, new)
        return {"base": base, "result": new, "changes": changes, "answer_type": AnswerType.SCENARIO,
                "mode": "summary",
                "steps": _step("optimiser", "solve_allocation(capacity_loss)+compare_plans", t0,
                               input={"capacity_changes": [c.model_dump(mode="json") for c in ch]},
                               output={"status": new.status, "orders_moved": len(changes)})}

    def explain(s: State) -> dict:
        t0, ds, w, oid = time.perf_counter(), s["ds"], s["weights"], s["intent"].order_id
        base = deps.base_plan() or deps.solve(ds, w)
        chosen = _order_plant(base, oid)
        cfs = [deps.counterfactual(ds, base, oid, p) for p in _eligible_plants(ds, oid) if p != chosen]
        return {"result": base, "counterfactuals": cfs, "answer_type": AnswerType.SCENARIO, "mode": "summary",
                "steps": _step("scenario", "counterfactual", t0, input={"order_id": oid, "chosen": chosen},
                               output={"alternatives_tested": len(cfs)})}

    def brief(s: State) -> dict:
        t0, ds, w = time.perf_counter(), s["ds"], s["weights"]
        res = deps.solve(ds, w)
        points = deps.frontier(ds, w, cuts=DEFAULT_CUTS)
        return {"result": res, "tradeoff": points, "answer_type": AnswerType.BRIEF, "mode": "brief",
                "steps": _step("optimiser", "solve_allocation+tradeoff_frontier", t0,
                               output={"status": res.status, "points": len(points)})}

    # ---------------- G3: plan gate + facts
    def checker(s: State) -> dict:
        t0, ds, res = time.perf_counter(), s["ds"], s["result"]
        changes_in = s["intent"].capacity_changes
        v = deps.validate(ds, res.assignments, changes_in)
        at, lim = s["answer_type"], []
        if res.status in ("INFEASIBLE", "ERROR") or not (v.capacity_ok and v.eligibility_ok):
            at = AnswerType.INFEASIBLE
            lim.append("The plan failed validation and is NOT a valid plan: " + "; ".join(v.violations[:5]))
        k = res.kpis
        if k and k.unassigned_orders:
            at = AnswerType.INFEASIBLE
            lim.append(f"{k.unassigned_orders} orders cannot be produced within capacity.")
        if k and k.late_orders:
            if s["intent"].require_all_on_time:
                at = AnswerType.INFEASIBLE
            late_ids = [a.order_id for a in res.assignments if not a.on_time][:10]
            lim.append(f"{k.late_orders} orders cannot meet their due date in this plan: {', '.join(late_ids)}.")
        return {"validator": v, "answer_type": at, "limitations": lim + res.notes, "facts": _facts(s, v),
                "steps": _step("checker", "validate_plan", t0, output=v.model_dump())}

    # ---------------- writer + G4 with bounded retry
    def write(s: State) -> dict:
        t0 = time.perf_counter()
        attempts = s.get("write_attempts", 0) + 1
        text, tokens = deps.write(s["req"].request, s["answer_type"], s["facts"], s["mode"], s.get("bad_numbers"))
        if text is None:  # MOCK_LLM or LLM down: deterministic text, faithful by construction
            return {"text": guards.template_summary(s["answer_type"], s["facts"]), "used_template": True,
                    "write_attempts": attempts, "steps": _step("writer", "template_summary", t0)}
        return {"text": guards.clean_llm_text(text), "used_template": False, "write_attempts": attempts,
                "llm_calls": 1, "tokens": tokens,
                "steps": _step("writer", f"write_{s['mode']}", t0, input={"attempt": attempts})}

    def output_guard(s: State) -> dict:
        t0 = time.perf_counter()
        if s.get("used_template"):
            return {"faithful": True, "steps": _step("guard", "faithfulness", t0, check="template: pass by construction")}
        ok, bad, n = guards.check_faithfulness(s["text"], s["facts"], s["req"].request)
        check = f"faithfulness: {'pass' if ok else 'fail'} ({n - len(bad)}/{n} numbers matched)"
        return {"faithful": ok, "bad_numbers": bad, "steps": _step("guard", "faithfulness", t0,
                                                                     check=check, output={"unmatched": bad})}

    def route_after_output(s: State) -> str:
        if s["faithful"]:
            return "finalize"
        return "write" if s.get("write_attempts", 0) < MAX_WRITE_ATTEMPTS else "template_fallback"

    def template_fallback(s: State) -> dict:
        t0 = time.perf_counter()
        return {"text": guards.template_summary(s["answer_type"], s["facts"]), "faithful": True,
                "used_template": True,
                "limitations": ["The LLM text failed the number check twice, so a code-generated summary is shown."],
                "steps": _step("guard", "template_fallback", t0, check="LLM text rejected; template used")}

    def finalize(s: State) -> dict:  # G5 happens in run() when the response is assembled
        return {}

    g = StateGraph(State)
    for name, fn in [("input_guard", input_guard), ("parse_intent", parse_intent), ("intent_guard", intent_guard),
                     ("allocate", allocate), ("tradeoff", tradeoff), ("replan", replan), ("explain", explain),
                     ("brief", brief), ("checker", checker), ("write", write), ("output_guard", output_guard),
                     ("template_fallback", template_fallback), ("finalize", finalize)]:
        g.add_node(name, fn)
    g.add_edge(START, "input_guard")
    g.add_conditional_edges("input_guard", route_after_input, ["parse_intent", "finalize"])
    g.add_edge("parse_intent", "intent_guard")
    g.add_conditional_edges("intent_guard", route_after_intent,
                            ["allocate", "tradeoff", "replan", "explain", "brief", "finalize"])
    for n in ("allocate", "tradeoff", "replan", "explain", "brief"):
        g.add_edge(n, "checker")
    g.add_edge("checker", "write")
    g.add_edge("write", "output_guard")
    g.add_conditional_edges("output_guard", route_after_output, ["write", "template_fallback", "finalize"])
    g.add_edge("template_fallback", "finalize")
    g.add_edge("finalize", END)
    return g.compile()


# ============================================================ facts: the ONLY thing the LLM sees
def _facts(s: State, v: ValidatorResult) -> dict:
    res: SolveResult = s["result"]
    f: dict[str, Any] = {"answer_type": s["answer_type"].value,
                         "weights_used": {"cost": pct(s["weights"].cost), "carbon": pct(s["weights"].carbon)},
                         "validator": {"capacity_ok": v.capacity_ok, "eligibility_ok": v.eligibility_ok}}
    if res.kpis:
        k = res.kpis
        f["kpis"] = {"total_cost": money(k.total_cost), "total_carbon_t": f"{k.total_carbon_t:,.1f} t",
                     "late_orders": k.late_orders, "unassigned_orders": k.unassigned_orders,
                     "plant_utilisation": {p: pct(u) for p, u in k.plant_utilisation.items()}}
        f["late_order_ids"] = [a.order_id for a in res.assignments if not a.on_time][:10]
    if s.get("changes") is not None and s.get("base") is not None and s["base"].kpis and res.kpis:
        b = s["base"].kpis
        f["changes_count"] = len(s["changes"])
        f["cost_delta"] = signed_money(res.kpis.total_cost - b.total_cost)
        f["carbon_delta_t"] = f"{res.kpis.total_carbon_t - b.total_carbon_t:+,.1f} t"
        f["late_orders_before_after"] = f"{b.late_orders} -> {res.kpis.late_orders}"
        f["moved"] = [{"order_id": c.order_id, "from": c.from_plant, "to": c.to_plant, "reason": c.reason}
                      for c in s["changes"][:10]]
    if s.get("tradeoff"):
        pts = s["tradeoff"]
        zero = next((p for p in pts if p.carbon_cut_pct == 0), pts[0])
        rows = [{"carbon_cut_pct": f"{p.carbon_cut_pct:g}%", "cost": money(p.cost),
                 "cost_delta": signed_money(p.cost - zero.cost), "carbon": f"{p.carbon_t:,.1f} t",
                 "late_orders": p.late_orders, "feasible": p.feasible,
                 "implied_carbon_price_per_t": (money(p.implied_carbon_price_per_t)
                                                if p.implied_carbon_price_per_t is not None else "n/a")}
                for p in pts]
        f["tradeoff"] = rows
        cut = s["intent"].carbon_cut_pct
        if cut is not None:
            f["tradeoff_row"] = next((r for r, p in zip(rows, pts) if p.carbon_cut_pct == cut), None)
    if s.get("counterfactuals"):
        oid = s["intent"].order_id
        chosen = _order_plant(res, oid)
        cfs = []
        for c in s["counterfactuals"]:
            cfs.append({"alternative_plant": c.alternative_plant, "feasible": c.feasible, "blocked_by": c.blocked_by,
                        "order_cost_delta": signed_money(c.order_cost_delta),
                        "plan_cost_delta": signed_money(c.plan_cost_delta),
                        "plan_carbon_delta": tonnes(c.plan_carbon_delta_kg),
                        "new_late_orders": c.new_late_orders,
                        "displaced_orders": [d.order_id for d in c.displaced_orders]})
        f["order_id"], f["chosen_plant"], f["counterfactuals"] = oid, chosen, cfs
        f["explanation"] = _explain_sentence(oid, chosen, s["counterfactuals"])
    return f


def _explain_sentence(oid: str, chosen: str | None, cfs: list[CounterfactualResult]) -> str:
    """Code-built 'why'. The LLM may rephrase it; the numbers stay checked."""
    parts = [f"{oid} is in Plant {chosen}."]
    for c in cfs:
        if not c.feasible:
            parts.append(f"Plant {c.alternative_plant} is not possible ({c.blocked_by}).")
            continue
        local = "cheaper" if c.order_cost_delta < 0 else "more expensive"
        parts.append(
            f"Plant {c.alternative_plant} would be {local} for this order ({signed_money(c.order_cost_delta)}), "
            f"but re-solving the whole plan changes total cost by {signed_money(c.plan_cost_delta)}"
            + (f", moves {len(c.displaced_orders)} other orders" if c.displaced_orders else "")
            + (f" and makes {', '.join(c.new_late_orders)} late" if c.new_late_orders else "") + "."
        )
    return " ".join(parts)


# ============================================================ entry point (called by backend /analyze)
def run(req: AnalyzeRequest, ds: Dataset, deps: Deps | None = None) -> tuple[AnalyzeResponse, TraceRecord]:
    t0 = time.perf_counter()
    graph = (deps or _default()).graph()
    run_id = new_run_id()
    try:
        s: State = graph.invoke({"req": req, "ds": ds, "run_id": run_id, "steps": [], "llm_calls": 0,
                                 "tokens": 0, "assumptions": [], "limitations": []})
    except Exception as exc:  # never leak a traceback to the user; the trace keeps the error
        log.exception("graph failed run_id=%s", run_id)
        s = {"answer_type": AnswerType.INFEASIBLE, "blocked_reason": f"Internal error: {type(exc).__name__}. See trace.",
             "steps": [TraceStep(agent="system", tool="graph", check=f"error: {exc}")], "llm_calls": 0, "tokens": 0,
             "assumptions": [], "limitations": []}
    resp = _assemble(s, run_id, req)
    trace = TraceRecord(run_id=run_id, request=req.request, answer_type=resp.answer_type, steps=s.get("steps", []),
                        llm_calls=s.get("llm_calls", 0), tokens=s.get("tokens", 0),
                        latency_ms=round((time.perf_counter() - t0) * 1000, 1), data_version=ds.data_version,
                        approval_status=resp.approval_status)
    return resp, trace


def _assemble(s: State, run_id: str, req: AnalyzeRequest) -> AnalyzeResponse:
    """G5: code (not the LLM) assembles every structured field; approval is always required for plans."""
    at: AnswerType = s.get("answer_type", AnswerType.CLARIFICATION_NEEDED)
    res: SolveResult | None = s.get("result")
    is_plan = res is not None and at != AnswerType.REFUSED
    text = s.get("blocked_reason") or s.get("text", "")
    recs: list[Recommendation] = []
    if is_plan and res.kpis:
        v = s.get("validator")
        valid = bool(v and v.capacity_ok and v.eligibility_ok)
        action = {"allocation": "Approve the proposed allocation",
                  "scenario": f"Review the scenario: {len(s.get('changes') or [])} orders move",
                  "brief": "Approve the plan described in the brief",
                  "infeasible": "Decide how to handle orders that cannot be met"}.get(at.value, "Review the plan")
        recs.append(Recommendation(
            action=action,
            rationale=f"Solver status {res.status}; validator {'passed' if valid else 'FAILED'}; "
                      f"{res.kpis.late_orders} late, {res.kpis.unassigned_orders} unassigned orders.",
            evidence_refs=[f"trace:{run_id}:solve_allocation", f"trace:{run_id}:validate_plan"],
            confidence="high" if valid and not res.kpis.late_orders else ("medium" if valid else "low"),
            estimated_impact=f"Total cost {money(res.kpis.total_cost)}, carbon {res.kpis.total_carbon_t:,.1f} t",
            requires_approval=True,
        ))
    weights = s.get("weights") or req.weights or Weights()
    return AnalyzeResponse(
        run_id=run_id, answer_type=at, summary=text if at != AnswerType.BRIEF else text.split("\n")[0][:300],
        allocation=res.assignments if is_plan else [], kpis=res.kpis if is_plan else None,
        tradeoff=s.get("tradeoff", []), changes=s.get("changes") or [], weights_used=weights,
        validator=s.get("validator"), recommendations=recs, counterfactuals=s.get("counterfactuals", []),
        brief=text if at == AnswerType.BRIEF else None,
        faithfulness_ok=s.get("faithful"), approval_status="pending" if is_plan else "not_required",
        assumptions=[f"Weights: cost {pct(weights.cost)}, carbon {pct(weights.carbon)}, lateness x{weights.lateness:g}"]
                    + s.get("assumptions", []),
        limitations=s.get("limitations", []),
    )
