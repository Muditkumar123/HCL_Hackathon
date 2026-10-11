# Worker 1 Status (Ayush)

## Task: TEST-001
**Status:** DONE

## Task: TASK-001 decision tools (`ai/tools/`, `tests/tools/`)
**Status:** DONE (Step 1 + Step 2), including the fixes from Shivang's eval (speed, re-plan lateness).

| Part | File | Status |
|---|---|---|
| Cost model (+ `ASSUMPTIONS`, `assumptions_for(ds)`, `plan_kpis`) | `ai/tools/cost_model.py` | DONE |
| Solver (pywraplp SCIP, MIP in docstring) | `ai/tools/solver.py` | DONE |
| Validator (independent re-check) | `ai/tools/validator.py` | DONE |
| Baseline `cheapest_plant_plan` | `ai/tools/baseline.py` | DONE |
| `tradeoff_frontier`, `compare_plans`, `counterfactual`, `share_not_at_cheapest_plant` | `ai/tools/scenarios.py` | DONE |
| Tests: 34 (trap fixture, synthetic 150 orders, real pack, graph end-to-end) | `tests/tools/` | PASSING |

Signatures match `ai/graph.py default_deps()` exactly; extra keyword arguments are optional
(`time_limit_s`, `horizon_weeks`, `hint` on the solver; `changes`/`forced` on `compare_plans`; `changes`, `time_limit_s` on
`counterfactual`).

**Run:** `MOCK_LLM=true python -m pytest tests/tools -q` (34 passed, ~16 s) · `ruff check ai/tools tests/tools` clean.

### Real pack (`data/raw`: 120 orders, 5 plants, 4 weeks, 1,592 options), default weights 0.7/0.3
| | Result |
|---|---|
| Main plan | **1.2 s**, FEASIBLE (root node + local search, deterministic), within 4.7% of SCIP's proven lower bound; validator: capacity ok, eligibility ok |
| KPIs | cost 285,685 · carbon 19.7 t · 11 late (16 late order-days) · 0 unassigned · utilisation 86–96% |
| Baseline (cheapest plant) | cost 266,355 · carbon 24.4 t · **88 late** · 3 capacity overloads |
| Orders NOT at their cheapest eligible plant | **59%** (a per-order "it was cheapest" explanation would be wrong for these) |
| Trade-off frontier | **5.9 s**. Deepest achievable cut **3.8%**: +6,414 cost, 61 late, implied **8,496 per t**. 5%+ cuts are infeasible within capacity (checked with a 15 s full search: needs deferring orders) |
| Re-plan "B loses 30%" (all weeks) | 3.2 s · 20 late (was 67) · 3 deferred · 20 plant moves. The loss forces 12 orders off B; late+deferred rose by 12 |
| Counterfactual | ~1.2 s per alternative plant (3 s limit) |

### What changed after Shivang's eval
1. **Speed.** MIP gap 0.5%; horizon limited to the weeks due dates need (+1 late week); greedy warm start; big models stop after
   the SCIP root node (full branch-and-bound did not beat it in 20 s), then deterministic local search; 3 s limit for
   trade-off points and counterfactuals; trade-off cuts run in parallel threads; results memoised on table contents.
2. **Re-plan 12 -> 67 late.** The cause was the flat 1000 "unassigned" penalty, not the lateness weight: one deferred order
   was worth about 100,000 late order-days, so the solver made dozens of orders late to fit one more order into the
   horizon. Now a deferred order costs its worst in-horizon option + one more week of lateness + 0.05. Answers to the
   eval's questions:
   - Loss only in the event's weeks? `capacity_events` rows and dated `CapacityChange`s apply only to overlapping weeks.
     A `CapacityChange` with no dates applies to every week (contract: "None => whole horizon"); "Plant B loses 30%"
     parses that way.
   - Lateness vs cost: one late order-day = 1% of total normalised cost ≈ 3,800 here, more than an average order's
     whole cost (~2,400). Test: `test_lateness_outweighs_cost_savings`.
   - Minimal-change penalty: 0.001 per moved order = one tenth of one late day. Test:
     `test_minimal_change_penalty_never_beats_lateness`.
   - New test `test_replan_quality.py`: a re-plan must score no worse than deferring exactly the orders the loss forces
     off, and must not add more late-or-deferred orders than that (trap, synthetic and real pack).

### Needs action from others
- **Mudit (`ai/generation/guards.py` template):** an infeasible `TradeoffPoint` has `cost=0`, so the template says
  "A 10% carbon cut costs -285,685 more". Please check `feasible` and use the extra deepest-cut point (e.g. 3.8%) to say
  "a 10% cut is not possible within capacity; the most is 3.8% at +6,414 and 61 late orders".
- **Mudit (`ai/graph.py`):** `replan` could pass the changes to `compare(ds, base, new, changes=ch)` so each reason says
  "Plant B loses 30% capacity"; `allocate` could pass `forced=it.force_assignments`. Both keyword args are optional.
- **Mudit:** `pytest tests/` fails at collection (`No module named 'ai'`); `python -m pytest tests/` works. A root
  `conftest.py` (or `pythonpath = .` in a pytest config) fixes it.
- **Shivang (`backend/store.latest_plan`):** it returns the last saved plan even after the data changes (I saw a stored
  plan whose order ids did not match the current data). Please filter by `data_version`, otherwise explain/re-plan
  compare against a stale plan.
- Assumptions for the response: `ai.tools.cost_model.assumptions_for(ds)` returns formulas + capacity mode per plant.
