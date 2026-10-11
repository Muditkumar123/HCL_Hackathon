# Eval report

**21/24 cases passed.** Answer-type accuracy 88%, plan validity 1.0, faithfulness 1.0, latency p50 10247.2 ms / p95 51126.8 ms, LLM calls 0, tokens 0.

## Plan vs cheapest-plant baseline (baseline ignores capacity and due dates)

| | cost | carbon t | late orders |
|---|---|---|---|
| baseline | 266,355 | 24.4 | 88 |
| plan (al-53570c) | 286,311 | 20.0 | 12 |
| delta | +7.5% | -17.9% | -76 |

## Cases

| id | ok | answer_type | expected | valid | faithful | late | ms | llm | tokens |
|---|---|---|---|---|---|---|---|---|---|
| alloc-01 | PASS | allocation | allocation | True | True | 12 | 10803.2 | 0 | 0 |
| alloc-02 | PASS | infeasible | allocation/infeasible | True | True | 0 | 10330.4 | 0 | 0 |
| alloc-03 | PASS | allocation | allocation | True | True | 12 | 127.9 | 0 | 0 |
| alloc-04 | FAIL | scenario | allocation | True | True | 10 | 61271.7 | 0 | 0 |
| alloc-05 | PASS | allocation | allocation | True | True | 10 | 10298.5 | 0 | 0 |
| trade-01 | PASS | scenario | scenario | True | True | 12 | 51126.8 | 0 | 0 |
| trade-02 | PASS | scenario | scenario | True | True | 12 | 172.2 | 0 | 0 |
| trade-03 | FAIL | clarification_needed | scenario | None | None | None | 31.1 | 0 | 0 |
| trade-04 | FAIL | clarification_needed | scenario | None | None | None | 27.6 | 0 | 0 |
| replan-01 | PASS | infeasible | scenario/infeasible | True | True | 33 | 10247.2 | 0 | 0 |
| replan-02 | PASS | infeasible | scenario/infeasible | True | True | 31 | 10271.0 | 0 | 0 |
| replan-03 | PASS | scenario | scenario/infeasible | True | True | 17 | 10334.0 | 0 | 0 |
| replan-04 | PASS | infeasible | scenario/infeasible | True | True | 21 | 10274.5 | 0 | 0 |
| explain-01 | PASS | scenario | scenario | True | True | 10 | 30604.3 | 0 | 0 |
| explain-02 | PASS | scenario | scenario | True | True | 10 | 20584.3 | 0 | 0 |
| explain-03 | PASS | scenario | scenario | True | True | 10 | 121.1 | 0 | 0 |
| brief-01 | PASS | brief | brief | True | True | 12 | 171.7 | 0 | 0 |
| override-01 | PASS | scenario | scenario/infeasible | True | True | 11 | 10264.0 | 0 | 0 |
| clarify-01 | PASS | clarification_needed | clarification_needed | None | None | None | 31.7 | 0 | 0 |
| clarify-02 | PASS | clarification_needed | clarification_needed | None | None | None | 29.5 | 0 | 0 |
| clarify-03 | PASS | clarification_needed | clarification_needed/refused | None | None | None | 29.8 | 0 | 0 |
| refuse-01 | PASS | refused | refused/infeasible | None | None | None | 37.3 | 0 | 0 |
| refuse-02 | PASS | refused | refused | None | None | None | 50.1 | 0 | 0 |
| refuse-03 | PASS | clarification_needed | refused/clarification_needed | None | None | None | 26.6 | 0 | 0 |
