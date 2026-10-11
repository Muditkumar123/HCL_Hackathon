# The allocation model (solver, trade-off, re-plan, "why")

Owner: Ayush (Worker 1). Code: `ai/tools/`. Every number a user sees comes from this code, never from the LLM.

**In one paragraph.** For each order we list every way it could be made: which plant, in which week. Each way has a
landed cost, a carbon footprint and an arrival date, all computed from the data tables. A mixed-integer program
(OR-Tools, SCIP solver) picks exactly one way per order so that no plant-week exceeds its capacity, and minimises a
weighted sum of cost, carbon and lateness. A separate validator re-checks the chosen plan from the raw tables before
anyone sees it.

Numbers marked **[pack]** come from the frozen data pack v1: `data/raw`, `data_version e543801d` (120 orders, 6
products, 10 destinations, 5 plants, 4 production weeks from 12 Oct 2026, capacity events E-1 and E-2). They change
when the data changes. Everything else is a property of the model.

---

## 1. Inputs: options

`cost_model.build_options` joins the tables into one row per **option** (order, plant, production week), only where
the plant makes the product (in that week) and has a transport route to the destination. **[pack]** 1,592 options.

| Per option | Formula |
|---|---|
| landed cost | `qty × (unit_cost + energy_kWh/unit × energy_price) × (1 + duty%/100) + qty × transport_cost/unit` (duty on ex-works value) |
| carbon (kg) | `qty × (energy_kWh/unit × carbon_kg/kWh[plant, week] + transport_carbon/unit[plant, destination])` |
| arrival | Monday of the production week + 6 days + transit days |
| days late | `max(0, arrival − due)` |

**Capacity** (`effective_capacity`): a plant whose `weekly_capacity` is the same on all its product rows has one
shared weekly capacity; otherwise each (plant, product) has its own. **[pack]** A, C, D and E are shared; B is per
product. A capacity event multiplies capacity by `(1 − loss%/100)` in every week that overlaps its dates. A scenario
change without dates (e.g. "Plant B loses 30%") applies to every week.

**Horizon:** every week up to the last one from which the latest due date can still be met by the fastest route,
plus one week for late production, and only weeks the plants table covers. **[pack]** 4 weeks.

All formulas and fallbacks are in `cost_model.ASSUMPTIONS`. `assumptions_for(ds)` adds the per-dataset choices for
the API response.

## 2. Decision variables

- `x[o,p,w] ∈ {0,1}`: order *o* is produced at plant *p* in week *w* (one per option).
- `u[o] ∈ {0,1}`: order *o* is **deferred**: it cannot be produced inside the horizon. It shows as "unassigned" in
  the plan and is always reported, never hidden.

Orders are not split: one order, one plant, one week.

## 3. Constraints

| Name | Constraint | Meaning |
|---|---|---|
| assign once | `Σ_{p,w} x[o,p,w] + u[o] = 1` | every order is planned exactly once or explicitly deferred |
| capacity | `Σ qty·x ≤ effective_capacity` for each (plant[, product], week) | no plant-week overload |
| eligibility | only eligible options exist as variables | a plant can't make what it can't make |
| carbon cap (trade-off only) | `Σ carbon·x + Σ min_carbon[o]·u[o] ≤ cap` | a deferred order still emits when it is made later, so deferring can't fake a carbon cut |
| on time (optional) | late options removed for orders that have an on-time option | "cheapest plan that is on time" |
| forced (optional) | `Σ_w x[o,p_forced,w] = 1` | manager override, and the counterfactual "why" |

## 4. Objective (minimise)

```
  w_cost   · Σ cost·x   / C_ref          C_ref = Σ_o (cheapest option cost of o)
+ w_carbon · Σ carbon·x / K_ref          K_ref = Σ_o (lowest option carbon of o)
+ w_late   · 0.01 · Σ days_late·x
+ Σ defer[o] · u[o]
[+ move_penalty · (orders moved off their base-plan plant)]      re-plans only
```

- **Normalisation.** Dividing by `C_ref` and `K_ref` makes both terms about 1.0, so the weights mean what they say:
  0.7/0.3 (the default) really is "cost matters about twice as much as carbon", whatever the units. The trade-off
  table reports the implied carbon price, so management sees the default's meaning in money per tonne.
- **Lateness dominates.** One late order-day costs 0.01, which is 1% of the normalised cost term at weight 1.
  **[pack]** That is about 3,800 of money per late day (C_ref = 266,355; weight 0.7). An average order costs about
  2,400 in total, so the plan never makes an order late to save money on it.
- **Deferral penalty, and why it changed.** The first version charged a flat 1,000 per deferred order (a "big-M").
  That equals about 100,000 late order-days, so the solver would make dozens of orders late just to squeeze one more
  order into the horizon. Shivang's eval caught it: "Plant B loses 30%" pushed late orders from 12 to 67. Now
  `defer[o]` = o's worst option inside the horizon + one more week of lateness + 0.05 (about 5 late days). Deferring is
  always worse than producing late, but only by about what it really means: the order ships at least a week later.
  The same "B loses 30%" re-plan now has **[pack]** 22 late orders and 1 deferral.
- **Minimal change.** When re-planning, every order moved off its old plant costs 0.0001, so 100 moves weigh as much
  as one late day. It only breaks near-ties in favour of a stable plan; it never buys stability with lateness. The
  "why" counterfactual uses 0.01 per move instead (section 7).
- A 1e-7 per-week tie-break prefers earlier production among exact ties, so equal plans come out the same every time.

## 5. How it is solved

| Setting | Value | Why |
|---|---|---|
| Solver | OR-Tools `pywraplp`, SCIP backend | open-source MIP solver, gives a proven lower bound |
| MIP gap | 0.5% | stop once within 0.5% of the proven optimum |
| Time limit | 10 s main plan; 3 s per trade-off point and per counterfactual | latency targets: plan < 3 s, trade-off < 10 s |
| Warm start | greedy plan: forced orders, then old-plan placements that still fit, then the rest at their best option that fits | gives SCIP a good plan at once |
| Large models (> 600 options) | stop after SCIP's root node, then local search | measured **[pack]**: a 30 s full search improves the objective by only about 1%, at 20x the time |
| Portfolio | 2 warm starts (by due date, largest order first), 5 when re-planning (+ keep old placements, + two that ignore the old plan), run in parallel threads; keep the best | a re-plan seeded only from the old plan got stuck next to it |
| Local search | relocate one order; or move an order into a full slot by ejecting another order to its best free option (or deferring it). Only strictly improving moves; respects capacity, forced orders and the carbon cap | packing at 89% utilisation is where MIP heuristics stall |
| Memoisation | results cached on (table contents hash, arguments) | the graph and the trade-off ask for the same base plan |

Everything is **deterministic**: the root-node stop is a node limit, not a clock, so the same request gives the same
plan. When the plan isn't proven optimal, status is `FEASIBLE` and `notes` says how far it can be from the optimum,
e.g. **[pack]** "within 3.1% of the solver's proven lower bound". The validator checks every plan independently.

**[pack]** Why it is hard: 18,464 units of demand for 20,830 units of capacity (89%) in 4 weeks, orders of 41–258
units into 28 capacity slots, with due dates. That is bin packing.

## 6. Trade-off frontier (`scenarios.tradeoff_frontier`)

Epsilon-constraint: solve once with no carbon cap (the 0% point), then re-solve with `carbon ≤ (1 − c/100) × carbon_0`
for each cut *c*.

1. A minimum-carbon plan (carbon only; lateness allowed; no extra deferrals) estimates the deepest achievable cut.
2. All requested cuts, plus that deepest cut, are solved **in parallel**, warm-started from the minimum-carbon plan
   (3 s each). **[pack]** 6.2 s in total.
3. Each point keeps the best plan among its own solve and every deeper feasible one, so the curve is monotone.
4. The **deepest feasible cut is always an explicit point** (the feasible point with the largest cut). Every cut
   beyond it has `feasible=False`, so the answer can be "a 10% cut isn't achievable within capacity; the best is
   X% at +Y and Z late orders".
5. `implied_carbon_price_per_t = (cost_c − cost_0) / (carbon_0 − carbon_c)` in tonnes, versus the 0% point.
   It counts money only; the point also reports its late orders.

## 7. Re-plan and "why" (counterfactual)

- **Re-plan** (`solve_allocation(..., changes=..., base_plan=old)`) re-solves the whole plan with the new capacity,
  preferring the old placements; `compare_plans` lists every order whose plant or on-time status changed, with a
  reason built by code (capacity loss, load in its old week before → after, cost and carbon change, lateness).
- **Why is order X at plant P?** For each other eligible plant Q we re-solve the **whole** plan with X forced to Q.
  We report X's own cost difference and the whole-plan cost and carbon difference, plus the knock-on: which other
  orders move and which become late. In a capacity-limited plan the honest answer is often "Q is cheaper for X, but
  putting X there pushes other orders out and the plan as a whole gets worse". Each other order moved costs 0.01
  (one late day), so the knock-on list shows what the force really displaces, not unrelated reshuffles.
  `blocked_by` is `eligibility` (Q can't make or ship it), `capacity` (no plan fits it, or other orders would be
  deferred) or `delivery` (Q can only deliver it late, while the current plan is on time).

## 8. Validator (`validator.validate_plan`)

Independent: it doesn't import the solver or the cost model's pricing and capacity code. It recomputes each order's
eligibility, route, cost, carbon (within 0.01) and arrival from the raw tables, and re-adds every capacity loss. It
flags every order missing or duplicated, every overload, and every late or deferred order (late orders are flagged,
never hidden).

## 9. Headline numbers [pack]

Default weights (cost 0.7, carbon 0.3, lateness 1.0).

| | Optimised plan | Naive "cheapest plant" plan |
|---|---|---|
| Total cost | 285,583 | 266,355 |
| Carbon | 19.8 t | 24.4 t |
| Late orders | **9** (13 late order-days) | **88** |
| Capacity | all plant-weeks within capacity | **3 overloaded slots**: A wk 2 Nov 13,469 vs 1,275 units; B/P2 wk 12 Oct 2,274 vs 800; D wk 19 Oct 2,259 vs 900 |
| Validator | capacity ok, eligibility ok | fails capacity |

The naive plan looks about 19,000 cheaper only because it isn't a real plan: it ignores capacity and due dates.

- **58% of orders are NOT at their cheapest eligible plant** (70 of 120). For those, "it was the cheapest option"
  would be a wrong explanation; that is why "why" answers re-solve the whole plan. (59% in the earlier run of the
  same pack; the plan improved when the warm-start portfolio was added.)
- **Carbon: the deepest achievable cut is 5.4%** (19.8 → 18.7 t) at +4,003 cost and 49 late orders: an implied
  **3,727 per tonne** (money only). 10% or more is not achievable within this pack's capacity. An earlier search
  found only 3.8% at 8,496 per tonne; the better search found a deeper, cheaper cut, so these are "best found"
  figures (lower bounds on what is achievable), not proven limits.
- **Speed:** main plan 1.4 s, trade-off frontier 6.2 s, re-plan 3.9 s, "why" for 3 alternative plants 2.3 s
  (timings vary by about ±0.2 s from run to run; plans and all other numbers are deterministic).
- **"Plant B loses 30%" for all 4 weeks:** 9 → 22 late orders, 1 deferred, 32 plant changes, cost 285,583 →
  282,841, validator ok. The loss takes 2,160 units out of a pack that only had 2,366 spare, so lateness rises.
- **Late-drop drill** (E-3: Plant E −50%, 12–25 Oct; a dry run, not the real drop): 9 → 19 late, cost −1,682,
  carbon +0.4 t. Cost can fall when capacity is lost: some orders that are now late anyway no longer pay for an
  expensive on-time route. A 30 s full search does no better (22 late orders).

## 10. Likely judge questions

**Q1. Why a MIP and not a greedy rule or an LLM?** Every order competes for the same plant-weeks, so the best plant
for one order depends on all the others. The naive cheapest-plant rule produces 88 late orders and puts 13,469 units
into a week that holds 1,275 **[pack]**. A MIP sees all orders at once, guarantees capacity and eligibility by
construction, and tells us how far from optimal it can be. The LLM only reads the request and writes the
explanation; it never produces a number.

**Q2. Your plan says FEASIBLE, not OPTIMAL. Is it any good?** Yes, and we say how good. SCIP proves a lower bound;
the plan is **[pack]** within 3.1% of it on the weighted objective. A 30 s full search improves the main plan by
only about 1% (8 instead of 9 late orders). We trade that last 1% for a deterministic answer in about 1.5 s, and
the validator checks every plan independently.

**Q3. How did you choose the weights, and what does 0.7/0.3 mean?** Cost and carbon are normalised so each term is
about 1.0, which makes the weights a real 70/30 preference. The trade-off table turns that into money: here a 5%
carbon cut has an implied price of about 3,727 per tonne **[pack]**. Lateness is set so that one late day
(about 3,800) outweighs the whole cost of an average order (about 2,400). Users can change all three weights per
request.

**Q4. "Plant B loses 30%": why do some orders move that were never at Plant B?** Capacity is shared: when B's orders
move to A or D, they take slots other orders used, and those orders move in turn. Each change carries a code-built
reason, for example **[pack]** "O-014: Plant B loses 30% capacity. Plant B load in week 2026-10-26: 1,931 -> 1,330
units. Order cost -226, carbon +162 kg." The re-plan prefers the old placements (every move costs a little), so only
necessary moves happen. Our test checks that a re-plan is never worse, in cost
or in late order-days, than simply deferring the orders the loss forces off.

**Q5. How do you explain why an order went to a plant, honestly?** We don't compare per-order costs, because in a
capacity-limited plan that is often wrong: 58% of orders aren't at their cheapest plant **[pack]**. We force the
order to each alternative and re-solve the whole plan (a counterfactual). The answer is concrete: **[pack]** "Plant A
would be 287 cheaper for O-001, but it can't deliver O-001 on time, and two other orders (O-110, O-112) would also
become late". Every number comes
from that re-solve.
