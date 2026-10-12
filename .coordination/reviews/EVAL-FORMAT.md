# Evaluation YAML format — agreement for `eval/run_eval.py`

`eval/cases.yaml` uses `schema_version: 1`. The current runner reads legacy top-level `expect`; Shivang should update
it to the format below before using this suite.

Each case contains:

- `id`: unique stable id.
- `category`: reporting bucket.
- `request`: POST `/analyze` text.
- optional `weights` and `scenario`: copied unchanged into the request body.
- `expected.answer_type`: one exact type, or a list when both a valid closest plan and an explicitly infeasible
  result are contractually acceptable.
- `expected.conditions`: machine-readable assertions.

Normal condition:

```yaml
path: allocation
where: {order_id: O-001} # optional exact-match array filter
field: on_time           # optional field from the one filtered item
op: eq
value: true
```

Supported operators: `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `in`, `not_in`, `is_null`, `not_null`, `contains`,
`not_contains`, `length_eq`, and `length_gte`. Dotted paths address the response. `trace.*` addresses the fetched
trace. `baseline.*` addresses `cheapest_plant_plan` on the same dataset.

Cross-case plan comparison:

```yaml
compare_case: weights-001
path: allocation
projection: [order_id, plant, production_week]
op: differs
min_changes: 1
```

The runner must always require HTTP 200 and validate JSON with `AnalyzeResponse.model_validate`. Missing paths,
missing or duplicate filtered rows, unknown operators, and unknown comparison ids fail closed. Run cross-case checks
after all selected cases finish. Never execute condition text with Python `eval`.

