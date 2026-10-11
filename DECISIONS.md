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
