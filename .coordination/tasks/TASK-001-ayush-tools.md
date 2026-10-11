# TASK-001 — Ayush — Decision tools: cost model, solver, validator (`ai/tools/`)

**Owner:** Ayush (Worker 1) **Writes only to:** `ai/tools/`, `tests/tools/`
**Contract:** `shared/models.py` (do not edit; ask the Orchestrator)
**Why this role changed:** there are no documents to retrieve in this track. Your "retrieval" becomes
*retrieving the facts from the data and the solver*. You own the code behind the 25 + 15 + 15 points
(valid allocation, trade-off, re-plan). Rename `ai/retrieval/` to `ai/tools/`.

**Rule:** every function is **pure**. It takes a `Dataset` (pandas DataFrames) and returns contract models.
No DB, no HTTP, no LLM. That keeps it unit-testable and lets Shivang and Mudit work in parallel.

---

## Files and signatures (Step 1, target: about 90 minutes)

### `ai/tools/cost_model.py`
```python
def effective_capacity(ds: Dataset, changes: list[CapacityChange]) -> pd.DataFrame:
    """cols: plant, product|None, week_start, capacity. Applies capacity_events + scenario changes
    (capacity * (1 - loss_pct/100) for weeks overlapping [from_date, to_date])."""

def build_options(ds: Dataset, horizon_weeks: int | None = None) -> pd.DataFrame:
    """One row per (order_id, plant, week_start) for ELIGIBLE plants only.
    cols: order_id, plant, week_start, qty, landed_cost, carbon_kg, arrival, due, days_late, on_time,
          evidence_refs"""
```
Formulas (put each assumption in a module-level `ASSUMPTIONS: list[str]`):
- `energy_price` = `plants.energy_price_per_kwh` if that column exists, else 0 (assumption: unit_cost already includes energy).
- `prod_value = qty * (unit_cost + energy_kwh_per_unit * energy_price)`
- `landed_cost = prod_value * (1 + duty_pct/100) + qty * transport_cost_per_unit` (assumption: duty is charged on ex-works value)
- `carbon_kg = qty * (energy_kwh_per_unit * carbon_kg_per_kwh[plant, week] + carbon_kg_per_unit[plant, dest])`
  (use the `week` column in plants if present, because carbon intensity changes by week)
- `arrival = week_start + 6 days + transit_days`; `days_late = max(0, arrival - due)`
- **Capacity mode:** if a plant's `weekly_capacity` is identical on all of its product rows, treat it as shared
  plant capacity; otherwise treat it as per (plant, product). Record which mode was used in `ASSUMPTIONS`.

### `ai/tools/solver.py` (OR-Tools `pywraplp`, SCIP backend, `time_limit_s=10`)
```python
def solve_allocation(ds: Dataset, weights: Weights, changes: list[CapacityChange] = [],
                     carbon_cap_kg: float | None = None, require_all_on_time: bool = False,
                     base_plan: list[OrderAssignment] | None = None,
                     forced: dict[str, str] | None = None) -> SolveResult:
```
**`ai/graph.py` (already written) calls these exact names and keyword arguments, so match them exactly.**
`forced = {"O-077": "A"}` means order O-077 must go to plant A (add `Σ_w x[O-077, A, w] = 1`). It's used for manager
overrides and for counterfactual "why" answers.
MIP formulation (document it in the docstring, because judges will ask):
- `x[o,p,w] ∈ {0,1}` for each option row; `u[o] ∈ {0,1}` = order unassigned
- each order is assigned exactly once: `Σ_{p,w} x[o,p,w] + u[o] = 1`
- capacity: `Σ qty·x ≤ effective_capacity[p(,product),w]`
- objective: `w.cost·Σcost·x / C_ref + w.carbon·Σcarbon·x / K_ref + w.lateness·0.01·Σdays_late·x + 1000·Σu`
  where `C_ref = Σ_o min cost` and `K_ref = Σ_o min carbon`, so both terms are about 1.0.
  (Normalising makes the weights meaningful. One late order-day costs as much as 1% of total cost.)
- `carbon_cap_kg` adds `Σcarbon·x ≤ cap`, used for the trade-off frontier.
- `require_all_on_time` forbids late options for any order that has at least one on-time option.
- Stretch: if `base_plan` is given, add `+0.001·(number of orders moved)` for minimal-change re-planning.
- Status INFEASIBLE or any `u[o]=1` → put a note in `notes`. Never raise to the caller.

### `ai/tools/validator.py` (independent: do **not** import solver code)
```python
def validate_plan(ds: Dataset, plan: list[OrderAssignment],
                  changes: list[CapacityChange] = []) -> ValidatorResult:
```
Checks: every order appears exactly once; plant can make the product; per-(plant, week) load ≤ effective capacity;
recomputed arrival vs due; recomputed cost and carbon match within 0.01. Each failure is added to `violations`.

### `ai/tools/baseline.py`
```python
def cheapest_plant_plan(ds: Dataset) -> list[OrderAssignment]:
    """Naive: min landed cost per order, ignoring capacity and dates. Shows the flaws (capability level 2)
    and is the 'simple plan' the evaluation compares against."""
```

## Step 2 (after the E2E allocate path works)
`ai/tools/scenarios.py`:
```python
def tradeoff_frontier(ds, weights, cuts=(0, 5, 10, 15, 20, 30)) -> list[TradeoffPoint]  # epsilon-constraint
def compare_plans(ds, base: SolveResult, new: SolveResult) -> list[PlanChange]          # reason built by code
def counterfactual(ds, base: SolveResult, order_id: str, plant: str) -> CounterfactualResult
    # OUR DIFFERENTIATOR. Re-solve the WHOLE plan with forced={order_id: plant}, then compare_plans(base, new).
    # Fill: order_cost_delta (this order's own cost at `plant` minus at its chosen plant),
    #       plan_cost_delta, plan_carbon_delta_kg (whole plan, new minus base),
    #       displaced_orders (other orders that moved), new_late_orders,
    #       feasible=False + blocked_by="eligibility"|"capacity"|"delivery" if the forced solve fails.
```
Also compute, for the evaluation report, the **share of assignments that are NOT at the order's cheapest eligible plant**.
Those are the orders where a simple per-order explanation would be wrong. That share is a headline number in the demo.
`implied_carbon_price_per_t = Δcost / Δcarbon_t` versus the 0% point. This is the number that defends the weights.

To check that your tools fit the graph, run Mudit's tests and an end-to-end call:
`MOCK_LLM=true pytest tests/ -q`, then `ai.graph.run(AnalyzeRequest(request="Allocate this week's orders"), ds)`.

## Tests (`tests/tools/`, pytest, fixture of about 6 orders × 3 plants)
Build the fixture so it contains these traps: the cheapest plant is late; a product only one plant can make;
a duty that flips which plant is cheapest; a low-carbon plant with tiny capacity.
- `test_cheapest_late_goes_elsewhere` · `test_only_eligible_plant` · `test_capacity_respected`
- `test_validator_catches_overload` (hand-craft a bad plan) · `test_capacity_loss_moves_orders`

## Done when
`pytest tests/tools` passes, `solve_allocation` on the real pack runs in under 10 s, and its plan passes
`validate_plan`. Write DONE plus the run time and KPIs into `.coordination/status/worker1.md`.
