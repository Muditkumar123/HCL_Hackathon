# DEBATE-001: response to REVIEW-001 (Mudit's files)

Every RED/YELLOW finding has a regression test in `tests/test_graph_guards.py` (`test_review1_*`) that uses Codex's
exact input; 58 of them failed before the fix and pass after. This file records where the fix differs from what
REVIEW-001 suggested, and the two parts I dispute.

## #2 (partial dispute): API `weights` should not always beat a grounded `weights_override`
- Agreed and fixed: every ungrounded solver field is dropped (plant, %, order, date, forced move, carbon cut,
  on-time, weights), and the explicit API `scenario` always wins over parsed text.
- Disputed: "explicit API weights must take precedence over LLM output". After the fix, `weights_override` survives
  G2 only when the request text itself says "cheapest", "cost only", "greenest" or names weights, so it is the
  user's words, not an LLM invention. The Streamlit UI always sends the slider weights (default 70/30), so the API
  cannot tell a deliberate value from an untouched default. With Codex's rule, "Give me the cheapest plan that is on
  time" from the UI would silently solve at 70/30, breaking a guide capability.
- Kept: grounded text override > API weights > default. Alternative if we must follow Codex: the UI sends `weights`
  only when the user moves the slider; then API weights can win. Needs Mudit's decision.

## #6 (partial dispute): a valid plan with late orders labelled `infeasible`
- Agreed and fixed: a plan that fails `validate_plan` (capacity/eligibility) or has solver status INFEASIBLE/ERROR is
  no longer returned at all: no allocation, KPIs, changes, counterfactuals or recommendation.
- Disputed: "any late order -> infeasible". TASK-003 line 105 says so; the code marks a plan `infeasible` only for
  unassigned orders or when the user asked for everything on time. A late order is a delivery trade-off, not a broken
  plan: the validator reports `delivery_ok=false`, late ids are listed in limitations, the UI shows late rows in red,
  and the writer must mention them. On the real data pack the solver's plan has 11-12 late orders (REVIEW-001
  "Verified correct"; eval alloc-01), so Codex's rule would label the main demo answer `infeasible`, and eval case
  alloc-01 (expects `allocation`) would fail. Whether those orders could be on time at another plant is not checked
  here; "cheapest plan that is on time" already returns `infeasible` when they cannot. Not changed; this is the open "item 7" decision for Mudit:
  either update TASK-003 line 105 to match the code, or change the code.

## #1 (fixed, differently): LLM prose is gated, not replaced
- Fixed: request numbers are no longer trusted; ids, plants and dates are identifiers, not numbers; every number must
  match a FACTS value of the same unit (%, t, kg, plain) and sign; ranges, million/billion/lakh/crore, number words,
  fractions, `.5` and scientific notation are parsed; unknown plants/ids/dates fail; plant ranking, move, capacity,
  plan-wide and "no late orders" claims must be backed by a FACTS record. Failure -> retry once -> template.
- Different: REVIEW-001 suggests code-rendering all rankings/reasons. Structured outputs already are code-built
  (`recommendations`, `changes[].reason`, counterfactual table, explanation sentence). The LLM summary stays, but
  only as text that passes the gate; a property test checks the gate accepts text copied from FACTS.
- Residual risk (honest): claims with no number, plant, id or listed keyword (e.g. "costs fell sharply") are not
  semantically verified. Mitigation: template fallback, `faithfulness_ok` in the response, trace shows the check.
  The fail-closed gate may raise the template rate with the real LLM; measure with eval `writer_template_rate`.

## #3 (fixed): consequence for Ayush
`CapacityChange.capacity_loss_pct` is now always a percentage; only the API `Scenario` converts a share (0.3 -> 30).
`tests/tools/test_tools.py::test_effective_capacity_applies_events_and_changes` builds `CapacityChange(0.2)` and
expects 20%, i.e. it asserts the behaviour REVIEW-001 #3 flags. It needs `capacity_loss_pct=20` (Ayush's file).

## #7 (fixed): consequence for Ayush
`tests/tools/test_scenarios.py::test_graph_end_to_end` expects `scenario` for "What does a 10% carbon cut cost?" on
`trap_ds`, where the deepest feasible cut is 3.6%. It passed only because the old code printed the sentinel
("costs -23,650 more"). The answer is now `infeasible` with "the deepest feasible cut is 3.6%". The test should
accept `infeasible` for that phrasing (Ayush's file).

## #8: eval part not mine
The unused `statistics` import in `eval/run_eval.py` is Shivang's file; repo-wide `ruff check .` already passes.
