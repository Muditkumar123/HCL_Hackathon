# REVIEW-003 — Solver and decision tools (current HEAD `b08990d`)

**Verdict: RED — blocks demo.** The requested portfolio/test/documentation changes are present, but the optimiser
still selects avoidable lateness and deferral, and the independent validator still accepts forged plans.

## Findings

### 1. RED — an available on-time plant loses to a one-day-late cheap plant

**File:** `ai/tools/solver.py:183-199`.

Exact one-order probe (qty 1, start `2026-10-05`, due `2026-10-13`): A costs 10 and arrives `10-14`; B costs 11
and arrives `10-13`. Default `Weights()` returned A, one day late, objective `0.71`, despite B being on time. The
committed test uses a 16-day delay, so it misses this edge.

**Fix:** make service lexicographic (unassigned, late orders/days, then cost/carbon), or prove a lateness coefficient
that dominates every possible saving. Add this exact one-day regression.

### 2. RED — defer penalty can leave a feasible order unassigned

**File:** `ai/tools/solver.py:345-349,414-446`.

Two 100-unit orders and two 100-unit slots: A is shared and makes P1/P2 cheaply; B makes P1 expensively; P2 can only
use A. The solver returned `OPTIMAL` with P1 at A and P2 unassigned (objective `0.82`). Forcing P1 to B returned a
validator-clean, all-on-time, fully assigned plan. An order-local penalty cannot price the knock-on move needed to
free its only slot.

**Fix:** minimise unassigned count first and fix that optimum before delivery/cost/carbon optimisation.

### 3. RED — heuristic frontier is still reported as a proof of infeasibility

**Files:** `ai/tools/scenarios.py:42-47,72-100`; `ai/graph.py:252-264`; `ai/generation/guards.py:644-649`.

On frozen pack `e543801d`, the frontier found a valid 5% plan and a best-found 5.4% point, then marked 10% as
`feasible=false`; the API says “not feasible” and “deepest feasible cut.” The minimum-carbon solve is only a bounded
heuristic/root-node result, so failure to find a deeper incumbent is not an infeasibility certificate.
`docs/solver.md:158-161` correctly calls 5.4% “best found” and “not proven,” but runtime wording contradicts it.

**Fix:** distinguish `not_found` from solver-certified `INFEASIBLE`; say “best found is 5.4%; a deeper cut is not
proven impossible” unless SCIP supplies a certificate.

### 4. RED — validator accepts forged/non-finite plan fields and split-week capacity

**File:** `ai/tools/validator.py:64-106`.

Exact bypasses against valid rows: `cost=NaN, carbon_kg=NaN` returns all flags true; `arrival=None` plus forged
`due=2099-01-01` also returns all flags true. With A capacity 1,000, two 600-unit rows dated Monday `10-05` and
Tuesday `10-06` return `capacity_ok=true`; putting both on Monday correctly reports 1,200 > 1,000.

**Fix:** reject non-finite values; require plan due/arrival to equal source/recomputed dates; require and normalise
production weeks to Monday; validate canonical fields on unassigned rows.

### 5. YELLOW — `0.0001` does not mathematically guarantee moves never outweigh lateness

**File:** `ai/tools/solver.py:68-71,195-198`; `docs/solver.md:81-83`.

The coefficient is exactly `0.0001`, as requested. But 101 plant moves cost `0.0101`, more than one late order-day
(`0.01`), so on this 120-order pack it can buy one day of lateness. The fixture test is useful but is not a proof.

**Fix:** make the move term strictly smaller than one late day over the maximum order count, or use lexicographic
delivery then moves. Add a 120-order tie construction.

### 6. YELLOW — same-plant week moves can disappear from the diff

**File:** `ai/tools/scenarios.py:134-152`.

Base `O-1=A/10-12/on_time=true` versus new `O-1=A/10-19/on_time=true` is skipped at line 137 although the reason
builder explicitly supports same-plant week changes.

**Fix:** skip only when plant, production week, and on-time status all match.

## Verified correct

- Validator has no solver import and independently recomputes the Annex A formulas. Three raw-pack checks matched:
  O-001/A/`10-12` cost `1,028.3847`, carbon `179.1366 kg`; O-005/B/`10-19` `2,712.00`, `163.2 kg`;
  O-008/D/`10-12` `2,965.92`, `106.632 kg`.
- Ineligible plants are excluded, ordinary weekly capacity is respected, and a dated 50% loss affects only its own
  week (`[800, 400, 800]`). Tool names/signatures match `default_deps()`; stubs require `USE_TOOL_STUBS=true`.
- Warm-start portfolio works: frozen-pack base reported best of 2 (`due,size`); B-30% re-plan best of 5, including
  two unseeded starts. The re-plan quality test now compares late order-days and counts a deferral as 12 days
  (`tests/tools/test_replan_quality.py:55-69`).
- Frozen-pack run: base 9 late/0 unassigned; B-30% re-plan 22 late/1 unassigned; frontier 5% feasible and best found
  5.4%. `docs/solver.md` correctly qualifies 5.4% as best found, not proven.
- Current source with generated frozen pack: full suite **248 passed**; Ruff passed.
