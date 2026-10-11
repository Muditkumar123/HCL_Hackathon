# TASK-003 — Mudit — LangGraph flow, intent router, writer, guardrails, Streamlit (`ai/generation/`, `ai/graph.py`, `ai/llm.py`, `frontend/`)

**Owner:** Mudit (Orchestrator)

> **Status:** `ai/llm.py`, `ai/generation/*` (guards, router, writer, prompts), `ai/graph.py` and
> `tests/test_graph_guards.py` (18 passing) are already drafted. **Still to do:** review and own that code,
> add `ai/tools/stubs.py` behind `USE_TOOL_STUBS=true` so the flow runs before Ayush's solver lands, then build `frontend/app.py`.
**Principle:** the LLM handles language in (parse intent) and language out (summary/brief). **It never creates,
edits or computes a plan.** At most 2 LLM calls per request.

---

## Step 1 files

### `ai/llm.py`
```python
def get_llm() -> BaseChatModel | None
    # langchain init_chat_model(LLM_MODEL, model_provider=LLM_PROVIDER, temperature=0); None if MOCK_LLM=true
def health() -> str                 # "mock" | "ok:<provider>/<model>" | "error: ..."
def call_structured(prompt, schema) -> tuple[BaseModel | None, int]   # (parsed, tokens); 1 retry, then None
def call_text(prompt) -> tuple[str | None, int]
```

### `ai/generation/router.py`
```python
def parse_intent(req: AnalyzeRequest) -> ParsedIntent
```
Order of operations: **(1)** deterministic refusal rules (regex for "ignore capacity", "even though it can't make",
"write the allocation yourself", "make up", "next year price") → REFUSE; **(2)** LLM structured output with the
`ParsedIntent` schema; **(3)** if the LLM fails or MOCK_LLM is on, fall back to keyword rules (regex for `O-\d+` / `order \d+`,
`plant [A-C]`, `\d+%`); **(4)** if `req.scenario` is present, it overrides the parsed capacity changes. Missing plant or % for a
re-plan → CLARIFY.

### `ai/generation/prompts.py`
```text
ROUTER_SYSTEM = """You classify requests for a multi-plant order allocation system (plants A, B, C).
You do NOT create allocations, costs, carbon numbers or schedules. You only extract parameters.
Return JSON matching the schema. intent is one of:
- allocate: build or show a plan ("allocate this week's orders", "cheapest plan that is on time" -> require_all_on_time=true, weights_override cost=1 carbon=0)
- tradeoff: cost/carbon trade-off ("what does a 10% carbon cut cost" -> carbon_cut_pct=10)
- replan: a plant loses capacity ("Plant B loses 30% capacity" -> capacity_changes=[{plant:"B", capacity_loss_pct:30}])
- explain_order: why an order went where it did (extract order_id exactly as written, e.g. "O-077" or "77")
- brief: a management/leadership brief
- clarify: a required detail is missing (re-plan without plant or %, explain without order id)
- refuse: asks to break capacity or eligibility rules, asks YOU to write or edit the plan, or asks for facts
  not in the data (future prices, other companies)
Put a one-line justification in `reason`. Never invent values that the user did not state."""

WRITER_SYSTEM = """You write for a manufacturing leadership team.
HARD RULES:
1. Use ONLY numbers that appear in FACTS, copied exactly as written there. Do not calculate, round,
   add, subtract, convert units or estimate. If you need a number that is not in FACTS, say "not available".
2. Do not use numbered or bulleted lists with digits.
3. Late or unassigned orders must be mentioned if FACTS lists any.
4. End with: "This plan is a proposal and needs management approval."
Tone: plain, direct, no jargon."""

SUMMARY_USER = """REQUEST: {request}
ANSWER TYPE: {answer_type}
FACTS (computed by the solver and validator):
{facts_json}
Write at most 3 sentences that answer the request."""

BRIEF_USER = """FACTS (computed by the solver and validator):
{facts_json}
Write a one-page brief (max 250 words) with these headings: Recommendation, Cost and Carbon,
Delivery Risk, Trade-off, Decision Needed."""

RETRY_SUFFIX = """Your previous answer used numbers that are not in FACTS: {bad_numbers}.
Rewrite it using only numbers from FACTS."""
```
**Important technique:** code pre-formats every number in `facts_json` as a display string (`"1,840,000"`,
`"410.2 t"`, `"12.5%"`, plus code-computed deltas). The LLM then copies numbers instead of computing them,
and the guard can match exactly.

### `ai/generation/guards.py` (the number-faithfulness guardrail)
```python
def extract_numbers(text: str) -> list[float]       # handles 1,840,000 · 1.84M · 410 t · 12.5% · 3k
def allowed_numbers(facts: dict, request: str) -> set[float]   # all numeric leaves + request numbers + rounding variants
def check_faithfulness(text: str, facts: dict, request: str) -> tuple[bool, list[float]]
def template_summary(answer_type: AnswerType, facts: dict) -> str   # deterministic fallback, no LLM
```
Flow: write, then check. On failure, retry once with RETRY_SUFFIX. If it fails again, use `template_summary`. Record
`faithfulness_ok` and a trace step `{"agent": "guard", "check": "faithfulness: pass (n/n)"}`.

### `ai/generation/writer.py`
```python
def write_summary(req: AnalyzeRequest, answer_type: AnswerType, facts: dict) -> tuple[str, bool, int]
def write_brief(facts: dict) -> tuple[str, bool, int]   # (text, faithful, tokens)
```

### `ai/graph.py` (LangGraph `StateGraph`, a fixed DAG, not a ReAct agent)
```
parse_intent ─► route ─┬─► allocate  (optimiser: solve_allocation)        ─┐
                       ├─► tradeoff  (scenario: tradeoff_frontier)         │
                       ├─► replan    (optimiser x2 + scenario: compare)    ├─► validate (checker) ─► write (writer) ─► guard ─► finalize
                       ├─► explain   (scenario: explain_order)             │
                       ├─► brief     (optimiser + tradeoff → write_brief) ─┘
                       ├─► clarify ───────────────────────────────────────────────────────────────► finalize
                       └─► refuse  ───────────────────────────────────────────────────────────────► finalize
def run(req: AnalyzeRequest, ds: Dataset) -> tuple[AnalyzeResponse, TraceRecord]
```
Every node appends a `TraceStep` (agent, tool, input summary, output summary, duration_ms). **Gate:** if
`validate_plan` returns violations, the plan is not shown as valid. The answer becomes `infeasible`, and the violations are listed.
Any order late or unassigned → `answer_type = infeasible` with the closest plan (the soft solve). Recommendations
(`requires_approval=True`) are built by code from KPIs and changes. Until Ayush's tools land, use stub functions with the same signatures.

### `frontend/app.py` (Streamlit, calls the API only via httpx)
- **Sidebar:** API health light · cost/carbon weight slider (default 70/30) · lateness weight · capacity-loss form
  (plant, %, from, to) · CSV upload → `/ingest` with a result toast · validation table
- **Main:** chat input plus 6 example buttons (one per capability in guide §2.1) · answer-type badge · summary ·
  KPI tiles (cost, carbon t, late orders, utilisation) · **trade-off chart** (plotly: x = carbon t, y = cost,
  point label = late orders, highlight the chosen plan) · allocation table (late rows in red) · "What moved" table ·
  validator badge · assumptions and limitations expanders · "Show trace" expander (`GET /trace/{run_id}`) · brief panel with an approval note

## Done when
"Allocate this week's orders" goes through UI → API → graph → solver → validator → writer → guard and back,
with a trace in under 10 s in MOCK_LLM mode and with the real LLM.
