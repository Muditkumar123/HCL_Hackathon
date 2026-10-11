"""eval/run_eval.py condition checks: the format in .coordination/reviews/EVAL-FORMAT.md, failing closed."""

from __future__ import annotations

from eval.run_eval import check_condition, load_cases, normalise_case, SUITES

DOC = {
    "answer_type": "allocation",
    "validator": {"capacity_ok": True},
    "allocation": [{"order_id": "O-1", "plant": "E", "on_time": True}, {"order_id": "O-2", "plant": None},
                   {"order_id": "O-3", "plant": "A"}, {"order_id": "O-3", "plant": "B"}],
    "limitations": ["2 orders are late"],
    "trace": {"llm_calls": 0},
    "baseline": {"allocation": [{"order_id": "O-1", "plant": "A", "on_time": False}]},
}


def ok(cond: dict, others: dict | None = None) -> bool:
    return check_condition(cond, DOC, others or {})[0]


def test_simple_paths_and_ops():
    assert ok({"path": "validator.capacity_ok", "op": "eq", "value": True})
    assert ok({"path": "trace.llm_calls", "op": "eq", "value": 0})
    assert ok({"path": "baseline.allocation", "where": {"order_id": "O-1"}, "field": "plant", "op": "eq", "value": "A"})
    assert ok({"path": "allocation", "where": {"order_id": "O-1"}, "field": "plant", "op": "not_in", "value": ["A"]})
    assert ok({"path": "allocation", "where": {"order_id": "O-2"}, "field": "plant", "op": "is_null"})
    assert ok({"path": "allocation", "where": {"plant": "E"}, "op": "length_eq", "value": 1})
    assert ok({"path": "limitations", "op": "length_gte", "value": 1})
    assert ok({"path": "answer_type", "op": "contains", "value": "alloc"})
    assert ok({"path": "answer_type", "op": "not_contains", "value": "brief"})


def test_fails_closed():
    assert not ok({"path": "kpis.late_orders", "op": "is_null"})                       # missing path
    assert not ok({"path": "allocation", "where": {"order_id": "O-9"}, "field": "plant", "op": "is_null"})  # no row
    assert not ok({"path": "allocation", "where": {"order_id": "O-3"}, "field": "plant", "op": "eq", "value": "A"})
    assert not ok({"path": "validator.capacity_ok", "op": "approximately", "value": True})  # unknown op
    assert not ok({"path": "answer_type", "op": "gt", "value": 3})                       # type error
    assert not ok({"compare_case": "nope", "path": "allocation", "projection": ["order_id", "plant"],
                   "op": "differs"})


def test_compare_case():
    other = {"allocation": [{"order_id": "O-1", "plant": "B"}]}
    cond = {"compare_case": "w1", "path": "allocation", "projection": ["order_id", "plant"], "op": "differs",
            "min_changes": 1}
    assert ok(cond, {"w1": other})
    assert not ok(cond, {"w1": {"allocation": DOC["allocation"]}})


def test_both_case_formats_load():
    assert normalise_case({"id": "a-1", "request": "x", "expect": ["allocation"]})["expect"] == ["allocation"]
    c = normalise_case({"id": "b", "category": "k", "request": "x",
                        "expected": {"answer_type": "refused", "conditions": [{"path": "p", "op": "is_null"}]}})
    assert c["expect"] == ["refused"] and len(c["conditions"]) == 1
    assert len(load_cases(SUITES["team"][1])) == 24
    assert len(load_cases(SUITES["auditor"][1])) >= 20


TRADE = {
    "answer_type": "infeasible",
    "summary": "A 10% carbon cut is not feasible; the deepest feasible cut is 5.4%.",
    "tradeoff": [{"carbon_cut_pct": 0.0, "cost": 100.0, "carbon_t": 20.0, "feasible": True},
                 {"carbon_cut_pct": 5.0, "cost": 104.0, "carbon_t": 19.0, "feasible": True},
                 {"carbon_cut_pct": 5.4, "cost": 104.0, "carbon_t": 18.9, "feasible": True},
                 {"carbon_cut_pct": 10.0, "cost": 0.0, "carbon_t": 18.0, "feasible": False}],
}


def _cut_rule(cut: float) -> dict:
    zero = {"path": "tradeoff", "where": {"carbon_cut_pct": 0.0}}
    row = {"path": "tradeoff", "where": {"carbon_cut_pct": cut}}
    return {"any_of": [
        [{**row, "field": "feasible", "op": "eq", "value": True},
         {**row, "field": "cost", "op": "gt", "value_from": {**zero, "field": "cost"}},
         {**row, "field": "carbon_t", "op": "lt", "value_from": {**zero, "field": "carbon_t"}}],
        [{"path": "answer_type", "op": "eq", "value": "infeasible"},
         {"path": "summary", "op": "contains", "value_from": {"path": "tradeoff", "where": {"feasible": True},
                                                              "field": "carbon_cut_pct", "agg": "max",
                                                              "format": "pct"}}],
    ]}


def test_any_of_carbon_cut_rule():
    assert check_condition(_cut_rule(5.0), TRADE, {})[0]               # feasible branch: costs more, emits less
    assert check_condition(_cut_rule(10.0), TRADE, {})[0]              # infeasible branch quotes "5.4%"
    wrong = {**TRADE, "summary": "A 10% carbon cut is not feasible."}
    ok, detail = check_condition(_cut_rule(10.0), wrong, {})
    assert not ok and "no branch passed" in detail
    cheaper = {**TRADE, "answer_type": "scenario",
               "tradeoff": [TRADE["tradeoff"][0], {**TRADE["tradeoff"][1], "cost": 99.0}]}
    assert not check_condition(_cut_rule(5.0), cheaper, {})[0]         # a "cut" that is cheaper is suspicious


def test_value_from_fails_closed():
    assert not check_condition({"path": "summary", "op": "contains",
                                "value_from": {"path": "tradeoff", "where": {"carbon_cut_pct": 99.0},
                                               "field": "cost"}}, TRADE, {})[0]
    assert not check_condition({"path": "summary", "op": "contains",
                                "value_from": {"path": "nope", "field": "x"}}, TRADE, {})[0]
    assert not check_condition({"path": "summary", "op": "contains",
                                "value_from": {"path": "tradeoff", "where": {"feasible": True},
                                               "field": "carbon_cut_pct", "agg": "max", "format": "{}"}},
                               TRADE, {})[0]
    assert not check_condition({"any_of": []}, TRADE, {})[0]
