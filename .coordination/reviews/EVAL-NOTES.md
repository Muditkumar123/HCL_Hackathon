# EVAL-NOTES: suggestions for the independent auditor's cases (from Shivang)

These are suggestions only. `eval/cases.yaml` is Codex's file, so I have not edited it.

## 1. Make `tradeoff-001` data-robust

**Problem.** `tradeoff-001` ("What does a 10% carbon cut cost?") requires `answer_type: scenario` and a feasible 10%
point. Whether a 10% cut fits in capacity depends on the data pack, not on the system's correctness. On the first
generated pack the deepest feasible cut was 3.8-5.4%, so the system correctly answered `infeasible` and quoted the
deepest cut, but the case failed.

**Team decision.** A carbon-cut answer is correct if EITHER:
- the requested cut is feasible, AND its cost is higher than the 0% point, AND its carbon is lower; OR
- `answer_type` is `infeasible`, AND the summary quotes the deepest feasible cut (the largest `carbon_cut_pct`
  with `feasible: true`, formatted like `5.4%`).

The team cases (`eval/cases_team.yaml`: trade-01, trade-03, trade-04) already use this rule.

**Suggested replacement** (needs the two extensions in section 2):

```yaml
  - id: tradeoff-001
    category: tradeoff
    request: "What does a 10% carbon cut cost?"
    expected:
      answer_type: [scenario, infeasible]
      conditions:
        - {path: tradeoff, where: {carbon_cut_pct: 10.0}, op: length_eq, value: 1}
        - any_of:
            - - {path: tradeoff, where: {carbon_cut_pct: 10.0}, field: feasible, op: eq, value: true}
              - {path: tradeoff, where: {carbon_cut_pct: 10.0}, field: cost, op: gt,
                 value_from: {path: tradeoff, where: {carbon_cut_pct: 0.0}, field: cost}}
              - {path: tradeoff, where: {carbon_cut_pct: 10.0}, field: carbon_t, op: lt,
                 value_from: {path: tradeoff, where: {carbon_cut_pct: 0.0}, field: carbon_t}}
            - - {path: answer_type, op: eq, value: infeasible}
              - {path: summary, op: contains,
                 value_from: {path: tradeoff, where: {feasible: true}, field: carbon_cut_pct, agg: max, format: pct}}
        - {path: validator.capacity_ok, op: eq, value: true}
        - {path: validator.eligibility_ok, op: eq, value: true}
        - {path: faithfulness_ok, op: eq, value: true}
```

## 2. Two small extensions to EVAL-FORMAT.md (already implemented in `eval/run_eval.py`)

Both keep the agreed rules: no `eval`, and missing paths or rows fail closed.

- **`value_from`** (instead of `value`): read the expected value from the same response.
  `{path, where?, field?, agg?: max|min, format?: pct}`. Without `agg`, `where` + `field` must match exactly one
  row, the same as a normal condition. `agg` takes the max or min over all matching rows; no rows fails.
  `format: pct` renders `5.4` as `5.4%`, the same as `ai/graph.py` writes `deepest_feasible_cut`.
- **`any_of`**: `[[cond, ...], [cond, ...]]` passes when every condition in at least one branch passes. An empty
  `any_of` fails. On failure, the report lists why each branch failed.

Tests: `tests/backend/test_eval_conditions.py`.

## 3. Data change that affects this case

The generated pack is getting moderate spare capacity, so that cuts of about 10-15% become feasible and 25% or
more stays infeasible (see `data/DATA_CARD.md`, pack v2). With the rule above, `tradeoff-001` passes on either pack
and checks real properties of the answer, not of the data.
