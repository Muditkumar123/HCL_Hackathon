## D-001 LLM: qwen2.5:7b-instruct via Ollama on local A100
- Tried: our cached Qwen3-8B (Hugging Face). This Ollama version cannot convert the Qwen3ForCausalLM architecture.
- Chose: qwen2.5:7b-instruct, which is listed in the guide, needs no thinking-mode workaround, and is constrained to our JSON schema.
- Why a 7B model is enough: the LLM only parses intent and words the answer; every number comes from the solver.
- Verified: router parsed "Plant B loses 30% capacity" to REPLAN / B / 30 (parsed_by=llm, 382 tokens).

## D-002 Contract: `HealthResponse.validation_passed: bool | None = None`
- Why: `/health` already returned `validation_passed` as an extra key added by hand, so the response did not match the contract.
- Change: optional field, default `None` (= checks not run), so existing callers and serialised responses stay valid.
- Effect: `backend/main.py` can pass `validation_passed=...` into `HealthResponse(...)` instead of adding the key by hand.
- Requested by: eval run (Shivang); applied by Mudit.

## D-003 LLM: keep the model loaded (`keep_alive=-1`), Ollama on GPU at port 11435
- Why: Ollama unloads an idle model after 5 minutes by default, so the first request after a pause (e.g. in the demo)
  would pay the model load time again.
- Change: `ai/llm.get_llm()` sets `keep_alive=-1` for Ollama. The URL stays in `.env` (`OLLAMA_BASE_URL=http://localhost:11435`).

## D-004 Contract: `CapacityChange.capacity_loss_pct` is always a percentage (REVIEW-001 #3)
- Why: the share-to-percent conversion ran on every `CapacityChange`, so text "Plant A loses 0.3%" became 30% and
  "1%" became 100%.
- Change: the conversion (0 < v <= 1 -> v * 100) now runs only in the API `Scenario` validator, where the guide's
  "share of capacity lost" applies. Router, LLM output and CSV rows are taken as percentages.
- Impact: Ayush's `test_effective_capacity_applies_events_and_changes` relied on the old conversion and needs
  `capacity_loss_pct=20`.

## D-005 Weights precedence: explicit API weights beat parsed text (REVIEW-001 #2)
- Rule: `AnalyzeRequest.weights` > grounded `weights_override` from the request text ("cheapest", "greenest",
  "cost only", "weights") > default 70/30. Consistent with "the API scenario wins over parsed text".
- UI: `frontend/app.py` sends `weights` only when the cost slider differs from 70% or the lateness weight from 1.0,
  so with untouched sliders "Give me the cheapest plan that is on time" still runs cost-only.
- Tests: `test_api_weights_beat_grounded_override`, `test_frontend_sends_weights_only_when_changed`.

## D-006 Late orders do not make a valid plan `infeasible` (REVIEW-001 #6)
- Rule: `answer_type = infeasible` only for unassigned orders, an all-on-time request that cannot be met, or a plan
  that fails validation (a failed plan is not shown). Late orders in a valid plan keep `allocation`/`scenario`; they
  are listed in `limitations`, flagged by `validator.delivery_ok=false`, and the writer must mention them.
- Why: the guide's own example returns `answer_type: "allocation"` with 2 late orders.
- TASK-003 line 105 updated to match.

## D-007 Contract: `TradeoffPoint.plan` (internal, never serialised); a requested cut is never re-solved
- Why: the graph re-solved a requested carbon cut on its own. That solve could differ from the frontier's (no
  warm start, different candidate), so the table said "5% feasible" while the answer said "infeasible" (eval trade-04).
- Change: `TradeoffPoint.plan: SolveResult | None` (excluded from JSON and the schema). The graph answers a requested
  cut from the frontier's own point: if `plan` is attached it is validated and shown; if not, the answer is the
  point's numbers only (no allocation); if the point is infeasible the answer is `infeasible`. No separate solve.
- Ask for Ayush: in `tradeoff_frontier`, attach the chosen `SolveResult` per point (`plan=...`).

## D-008 Wording: "no feasible plan found within the time limit; best found X%" (REVIEW-002)
- Why: the solver stops at a time limit, so a missing plan is not proof that none exists.
- Rule: infeasible carbon cuts (facts, template, limitations, chart legend, writer prompt) say
  "no feasible plan found within the time limit; best found X%". "Impossible", "not possible", "cannot be achieved"
  are rejected by G4. Only data-proven facts use "cannot" (a plant that cannot make the product: eligibility).
