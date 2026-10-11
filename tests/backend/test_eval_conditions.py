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
