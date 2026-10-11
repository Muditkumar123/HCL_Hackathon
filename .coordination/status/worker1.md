# Worker 1 Status (Ayush)

## Task: TEST-001
**Status:** DONE

## Task: TASK-001 decision tools (`ai/tools/`, `tests/tools/`, `docs/solver.md`)
**Status:** DONE (Step 1 + Step 2 + Shivang's eval fixes + round 2 below). Late data drop: tool ready, **waiting for
the real file** (dry run recorded below).

| Part | File | Status |
|---|---|---|
| Cost model (+ `ASSUMPTIONS`, `assumptions_for(ds)`, `plan_kpis`) | `ai/tools/cost_model.py` | DONE |
| Solver (pywraplp SCIP, MIP in docstring) | `ai/tools/solver.py` | DONE |
| Validator (independent re-check) | `ai/tools/validator.py` | DONE |
| Baseline `cheapest_plant_plan` | `ai/tools/baseline.py` | DONE |
| `tradeoff_frontier`, `compare_plans`, `counterfactual`, `share_not_at_cheapest_plant` | `ai/tools/scenarios.py` | DONE |
| Late-drop before/after report | `ai/tools/replan_report.py` | DONE |
| Model write-up + 5 judge Q&As (for README / judges) | `docs/solver.md` | DONE |
| Tests: 36 (trap fixture, synthetic 150 orders, real pack, graph end-to-end) | `tests/tools/` | PASSING |

Signatures match `ai/graph.py default_deps()` exactly. Extra keyword arguments are optional: `time_limit_s`,
`horizon_weeks`, `hint`, `portfolio`, `move_penalty` on the solver; `changes`/`forced` on `compare_plans`; `changes`,
`time_limit_s` on `counterfactual`.

**Run:** `MOCK_LLM=true pytest tests/tools -q` (36 passed, ~25 s) · `ruff check ai/tools tests/tools` clean.

**Data pack: v1 is frozen** (Shivang's revert 8e785f2). All numbers here and in `docs/solver.md` are from v1,
data_version e543801d. (I briefly moved them to v2 in eba2dd4 before seeing the revert; reverted.)

### Real pack (`data/raw`, data_version e543801d: 120 orders, 5 plants, 4 weeks, 1,592 options), weights 0.7/0.3
| | Result |
|---|---|
| Main plan | **1.4 s**, FEASIBLE (deterministic), within 3.1% of SCIP's proven lower bound; validator: capacity ok, eligibility ok |
| KPIs | cost 285,583 · carbon 19.8 t · **9 late** (13 late order-days) · 0 deferred · utilisation 86–96% |
| Baseline (cheapest plant) | cost 266,355 · carbon 24.4 t · **88 late** · **3 overloaded slots** (A wk 2 Nov: 13,469 units vs 1,275) |
| Orders NOT at their cheapest eligible plant | **58%** (70 of 120) |
| Trade-off frontier | **6.2 s**. Deepest achievable cut **5.4%**: +4,003 cost, 49 late, implied **3,727 per t** (money only). 10%+ not achievable |
| Re-plan "B loses 30%" (all weeks) | 3.9 s · 9 → **22 late** (was 67 before the fix) · 1 deferred · 32 plant changes · validator ok |
| "Why" counterfactual | 2.3 s for 3 alternative plants; 3–5 orders displaced each |

Numbers changed since round 1 (3.8% at 8,496/t, 59%, 11 late) because the search improved; see `docs/solver.md` §9.

### Late data drop
**Real drop: not received yet** (`data/raw/capacity_events.csv` and the DB still hold only E-1, E-2). When it lands:
`python -m ai.tools.replan_report <new capacity_events.csv> --mode replace` (or `--mode append` if the file holds only
the new events), then paste the table here. The live path stays Shivang's `scripts/late_drop.py`.

#### Dry run (not the real drop): Shivang's drill event E-3 appended (Plant E −50%, 12–25 Oct)
| Plan | Cost | Carbon (t) | Late orders | Deferred | Solve | Validator |
|---|---|---|---|---|---|---|
| Before | 285,583 | 19.8 | 9 (13 days) | 0 | 1.5 s | capacity_ok=True, eligibility_ok=True |
| After (re-plan) | 283,901 | 20.2 | 19 (35 days) | 0 | 2.4 s | capacity_ok=True, eligibility_ok=True |

Change: cost −1,682, carbon +0.4 t, late 9 → 19, 36 orders changed plant or on-time status. E-3 takes 800 units out of
the two earliest weeks, the bottleneck for orders due first. Cost falls because some orders that are now late anyway
stop paying for an expensive on-time route. A 30 s full search does no better (22 late), so this is not a weighting
problem (answer to Shivang's "lateness may be under-weighted" note).

### Round 2 changes
1. **Frontier always includes the deepest feasible cut** as its own point: the feasible point with the largest
   `carbon_cut_pct`. Every cut beyond it has `feasible=False`. Test: `test_frontier_always_has_deepest_feasible_point`.
2. **Warm-start portfolio** (parallel threads): main plan 2 starts; re-plans 5, including two that ignore the old plan
   and are re-scored with the move penalty, so a re-plan is never worse than a fresh plan. Base plan 11 → 9 late;
   E-3 re-plan 24 → 19 late.
3. **Move penalty** 0.001 → 0.0001 (100 moved orders = one late day). At 0.001, about 50 moves outweighed 5 late days.
   The "why" counterfactual uses 0.01 per move and one warm start, so its knock-on list shows only orders the force
   really displaces (was up to 26 unrelated reshuffles).
4. **Re-plan test corrected:** a bound on the NUMBER of late orders is not an invariant. On E-3 even a 30 s full search
   adds 14 late orders while only 6 are forced off, because the objective prefers several orders 1–2 days late over
   one deferred for weeks. The test now checks (a) objective no worse than deferring the forced-off orders and
   (b) late order-days (a deferral counted as 12) no worse than that. It runs on trap, synthetic, real pack B −30%,
   and E-3.

### Needs action from others
- **Mudit (`ai/graph.py`):** `replan` could call `compare(ds, base, new, changes=ch)` so each reason starts with
  "Plant B loses 30% capacity."; `allocate` could pass `forced=it.force_assignments`. Both are optional keyword args.
- **Mudit:** the infeasible-cut wording ("not feasible ... deepest feasible cut is 5.4%") is in your working copy and
  matches the frontier contract above; it is not committed yet.
- Resolved: plain `pytest tests/` now collects (201 passed); `latest_plan` filters by `data_version` (thanks Shivang).
