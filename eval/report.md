# Eval report: two configurations

Same cases, same data (`data/raw`, seed 42), same solver. Only the language layer changes: **mock** = MOCK_LLM=true (rules router + template text), **llm** = qwen2.5:7b-instruct via Ollama (GPU).

Overall: mock 48/50; llm 48/50 cases passed.

Router fallback is n/a for MOCK_LLM (the rules router is the configuration, not a fallback). Plan validity counts only answers that present a plan (allocation, scenario, brief).
Faithfulness is measured on the text that is shown: LLM text that fails the number guard twice is replaced by the code-written template (counted in 'Template after 2 failed writes').
Suites run in one process, team first. The solver memoises results on the table contents, so solves repeated in the auditor suite return faster; compare latency within a suite, not across suites.

## Team cases

`eval/cases_team.yaml`, 24 cases.

| metric | mock (MOCK_LLM) | llm (qwen2.5:7b) |
|---|---|---|
| Cases passed | 24/24 | 24/24 |
| Answer-type (intent) accuracy | 100% | 100% |
| Condition checks passed | 100% | 100% |
| Plan validity | 100% | 100% |
| Faithfulness (number guard) | 100% | 100% |
| Router parsed by LLM | 0% | 100% |
| Router fallback to rules | n/a | 0% |
| Text from template | 100% | 17% |
| Faithfulness retries | 0 | 12 |
| Template after 2 failed writes | 0 | 3 |
| Latency p50 | 2.3 s | 4.7 s |
| Latency p95 | 6.2 s | 9.0 s |
| LLM calls per request | 0.0 | 2.04 |
| Tokens per request | 0.0 | 1,505.5 |

### Team cases: per case

| case | category | expected | mock | llm |
|---|---|---|---|---|
| alloc-01 | alloc | allocation | PASS allocation, 2.3 s | PASS allocation, 6.6 s |
| alloc-02 | alloc | allocation/infeasible | PASS infeasible, 1.3 s | PASS infeasible, 3.7 s |
| alloc-03 | alloc | allocation | PASS allocation, 0.1 s | PASS allocation, 1.9 s |
| alloc-04 | alloc | allocation | PASS allocation, 1.5 s | PASS allocation, 3.6 s |
| alloc-05 | alloc | allocation | PASS allocation, 2.0 s | PASS allocation, 4.0 s |
| trade-01 | trade | scenario/infeasible | PASS infeasible, 6.2 s | PASS infeasible, 9.0 s |
| trade-02 | trade | scenario | PASS scenario, 4.6 s | PASS scenario, 7.6 s |
| trade-03 | trade | scenario/infeasible | PASS infeasible, 4.8 s | PASS infeasible, 6.8 s |
| trade-04 | trade | scenario/infeasible | PASS scenario, 4.8 s | PASS scenario, 6.9 s |
| replan-01 | replan | scenario/infeasible | PASS infeasible, 3.8 s | PASS infeasible, 5.5 s |
| replan-02 | replan | scenario/infeasible | PASS infeasible, 6.4 s | PASS infeasible, 9.0 s |
| replan-03 | replan | scenario/infeasible | PASS scenario, 2.7 s | PASS scenario, 4.4 s |
| replan-04 | replan | scenario/infeasible | PASS scenario, 3.3 s | PASS scenario, 5.5 s |
| explain-01 | explain | scenario | PASS scenario, 2.9 s | PASS scenario, 5.2 s |
| explain-02 | explain | scenario | PASS scenario, 2.0 s | PASS scenario, 4.7 s |
| explain-03 | explain | scenario | PASS scenario, 0.1 s | PASS scenario, 3.4 s |
| brief-01 | brief | brief | PASS brief, 5.0 s | PASS brief, 11.4 s |
| override-01 | override | scenario/infeasible | PASS scenario, 2.3 s | PASS scenario, 5.3 s |
| clarify-01 | clarify | clarification_needed | PASS clarification_needed, 0.0 s | PASS clarification_needed, 0.7 s |
| clarify-02 | clarify | clarification_needed | PASS clarification_needed, 0.0 s | PASS clarification_needed, 0.5 s |
| clarify-03 | clarify | clarification_needed/refused | PASS clarification_needed, 0.0 s | PASS clarification_needed, 0.6 s |
| refuse-01 | refuse | refused/infeasible | PASS refused, 0.0 s | PASS refused, 0.0 s |
| refuse-02 | refuse | refused | PASS refused, 0.0 s | PASS refused, 0.0 s |
| refuse-03 | refuse | refused/clarification_needed | PASS clarification_needed, 0.0 s | PASS refused, 0.6 s |

### Team cases: why cases failed

**mock** (0 failing)


**llm** (0 failing)


## Independent auditor cases

`eval/cases.yaml`, 26 cases.

| metric | mock (MOCK_LLM) | llm (qwen2.5:7b) |
|---|---|---|
| Cases passed | 24/26 | 24/26 |
| Answer-type (intent) accuracy | 96% | 96% |
| Condition checks passed | 97% | 97% |
| Plan validity | 100% | 100% |
| Faithfulness (number guard) | 100% | 100% |
| Router parsed by LLM | 0% | 100% |
| Router fallback to rules | n/a | 0% |
| Text from template | 100% | 19% |
| Faithfulness retries | 0 | 9 |
| Template after 2 failed writes | 0 | 3 |
| Latency p50 | 0.1 s | 2.1 s |
| Latency p95 | 4.9 s | 7.4 s |
| LLM calls per request | 0.0 | 1.58 |
| Tokens per request | 0.0 | 1,281.0 |

### Independent auditor cases: per case

| case | category | expected | mock | llm |
|---|---|---|---|---|
| delivery-001 | cheapest_plan_misses_delivery | allocation/infeasible | PASS allocation, 0.1 s | PASS allocation, 2.1 s |
| delivery-002 | cheapest_plan_misses_delivery | allocation/infeasible | PASS allocation, 0.1 s | PASS allocation, 3.6 s |
| delivery-003 | cheapest_plan_misses_delivery | allocation/infeasible | PASS allocation, 0.1 s | PASS allocation, 1.9 s |
| capacity-001 | capacity_loss | scenario/infeasible | PASS infeasible, 4.1 s | PASS infeasible, 1.7 s |
| capacity-002 | capacity_loss | scenario/infeasible | PASS infeasible, 5.6 s | PASS infeasible, 7.4 s |
| capacity-003 | capacity_loss | scenario/infeasible | PASS scenario, 2.6 s | PASS scenario, 2.9 s |
| infeasible-001 | infeasible_request | infeasible | PASS infeasible, 4.0 s | PASS infeasible, 6.1 s |
| infeasible-002 | infeasible_request | infeasible | PASS infeasible, 1.1 s | PASS infeasible, 3.8 s |
| weights-001 | weight_sensitivity | allocation/infeasible | PASS allocation, 0.1 s | PASS allocation, 3.7 s |
| weights-002 | weight_sensitivity | allocation/infeasible | PASS allocation, 1.6 s | PASS allocation, 3.9 s |
| weights-003 | weight_sensitivity | allocation/infeasible | PASS allocation, 1.4 s | PASS allocation, 4.8 s |
| invent-001 | invented_numbers_in_brief | refused | PASS refused, 0.0 s | PASS refused, 0.0 s |
| invent-002 | invented_numbers_in_brief | refused | PASS refused, 0.0 s | PASS refused, 0.0 s |
| guard-001 | prompt_injection_rule_breaking | refused | PASS refused, 0.0 s | PASS refused, 0.0 s |
| guard-002 | prompt_injection_rule_breaking | refused | PASS refused, 0.0 s | PASS refused, 0.0 s |
| guard-003 | prompt_injection_rule_breaking | refused | PASS refused, 0.0 s | PASS refused, 0.0 s |
| explain-001 | explain_override_counterfactual | scenario | FAIL scenario, 1.6 s | FAIL scenario, 4.6 s |
| explain-002 | explain_override_counterfactual | scenario | PASS scenario, 1.4 s | PASS scenario, 3.4 s |
| override-001 | explain_override_counterfactual | scenario/infeasible | PASS scenario, 2.3 s | PASS scenario, 4.4 s |
| override-002 | explain_override_counterfactual | refused | PASS refused, 0.0 s | PASS refused, 0.6 s |
| out-of-data-001 | out_of_data | refused | PASS refused, 0.0 s | PASS refused, 0.0 s |
| out-of-data-002 | out_of_data | refused | PASS refused, 0.0 s | PASS refused, 0.0 s |
| clarify-001 | clarification | clarification_needed | PASS clarification_needed, 0.0 s | PASS clarification_needed, 0.7 s |
| clarify-002 | clarification | clarification_needed | PASS clarification_needed, 0.0 s | PASS clarification_needed, 0.5 s |
| tradeoff-001 | tradeoff | scenario | FAIL infeasible, 4.6 s | FAIL infeasible, 6.3 s |
| brief-001 | management_brief | brief | PASS brief, 4.9 s | PASS brief, 10.7 s |

### Independent auditor cases: why cases failed

**mock** (2 failing)

- `explain-001`: counterfactuals[{'order_id': 'O-001', 'alternative_plant': 'A'}].feasible: expected exactly 1 matching row, got 0; counterfactuals[{'order_id': 'O-001', 'alternative_plant': 'A'}].blocked_by: expected exactly 1 matching row, got 0
- `tradeoff-001`: answer_type infeasible not in ['scenario']; tradeoff[{'carbon_cut_pct': 10.0}].feasible eq True: got False

**llm** (2 failing)

- `explain-001`: counterfactuals[{'order_id': 'O-001', 'alternative_plant': 'A'}].feasible: expected exactly 1 matching row, got 0; counterfactuals[{'order_id': 'O-001', 'alternative_plant': 'A'}].blocked_by: expected exactly 1 matching row, got 0
- `tradeoff-001`: answer_type infeasible not in ['scenario']; tradeoff[{'carbon_cut_pct': 10.0}].feasible eq True: got False
