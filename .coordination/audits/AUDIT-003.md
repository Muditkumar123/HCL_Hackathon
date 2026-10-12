# AUDIT-003: Ayush's solver and tools (commit 60f8679)
Scope: ai/tools/ (cost_model, solver, validator, scenarios, baseline), tests/tools/, tests/test_replan_quality.py.
Focus:
1. Is validator.py independent of solver code (no imports from solver)?
2. Verify on small hand-checkable cases: an order whose cheapest plant is late goes to an on-time plant; an ineligible plant is never used; weekly capacity is respected; a capacity loss with dates hits only its own weeks.
3. Check the claim "max achievable carbon cut is 3.8%; 5% or more is infeasible within capacity": try to find a feasible 5% plan and report what you find.
4. The new unplanned-order penalty: can it make the solver defer orders that could be made on time?
5. Landed cost = production + energy + transport + duty, and carbon = plant energy + transport, as in Annex A. Recompute 3 orders by hand and compare.
6. Function names and keyword arguments match what ai/graph.py default_deps() imports.
Write findings to .coordination/reviews/REVIEW-003.md with verdict GREEN / YELLOW / RED, most severe first, each with file:line, failing input and fix.
