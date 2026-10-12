# REVIEW-002 — REVIEW-001 re-check and backend (HEAD `b08990d`)

**Verdict: RED — disqualification risk remains.** The exact REVIEW-001 regressions pass and the API/backend are
mostly sound, but new G2/G4 phrasings still let LLM output change solver controls or publish unsupported claims.

## Findings

### 1. RED — intent grounding checks words, not whether the user actually requested the control

**File:** `ai/generation/guards.py:163-165,192-232`.

All three simulated LLM outputs survived G2 unchanged:

- `Allocate orders; they do not have to be on time.` + `require_all_on_time=true`.
- `Allocate orders and show the objective weights being used.` + carbon-only `weights_override`.
- `Allocate orders; do not use cost-only weighting.` + cost-only `weights_override`.

The regexes see “on time”/“weights”/“cost-only” but ignore negation and mention-vs-instruction semantics. This lets
LLM text alter the optimiser, which is a disqualification risk.

**Fix:** derive these controls deterministically from explicit positive request patterns/API fields; never retain an
LLM-provided boolean/weight merely because its keyword occurs. Add the three exact regressions.

### 2. RED — G4 accepts unsupported allocation, delivery, and capacity claims via synonyms

**File:** `ai/generation/guards.py:453-483,577-616`.

With FACTS containing two late orders and no backing plan-wide/capacity claim, all returned
`faithfulness_ok=true`: `The schedule is punctual.`, `Every delivery meets its promised date.`,
`Plant B has room for every order.`, and `Send the entire portfolio to Plant B.`

**Fix:** replace open-ended claim allowlisting with code-written templates for plan/delivery/capacity assertions, or
add semantic claim categories that fail closed when a plant/order-wide claim lacks a matching structured fact.

### 3. YELLOW — append ingest accepts duplicate keys already present in the table

**Files:** `backend/loader.py:89-125`; `backend/store.py:95-105`.

After loading 120 orders, appending a valid CSV row with existing id `O-001` returned `status=ok`,
`rows_loaded=1`, `rows_rejected=0`; the table then had 121 rows and two `O-001`s. The loader checks duplicates only
inside the incoming frame, while the store concatenates with existing rows afterward.

**Fix:** validate key uniqueness against the combined table and reject collisions atomically (or enforce database
unique constraints). Run the six data checks before making an appended version active.

## REVIEW-001 status

| # | Status | Evidence |
|---|---|---|
| 1 G4 invented numbers/claims | **STILL OPEN** | Every exact old bypass is now rejected, including million/lakh/ranges/signs/rankings; the four synonym bypasses above remain. |
| 2 G2 ungrounded solver fields | **STILL OPEN** | Exact old ungrounded fields are dropped; negation/mention phrasings above retain solver controls. |
| 3 decimal percentages | **FIXED** | `0.3% -> 0.3`, `1% -> 1`; API share `0.3 -> 30`. |
| 4 router reason bypasses G4 | **FIXED** | LLM reason remains trace-only; fixed response text is shown. |
| 5 G1/PII | **FIXED for tested inputs** | Every exact REVIEW-001 G1 input plus the two auditor phrases is refused pre-LLM; PII is masked. |
| 6 rejected/late plan handling | **FIXED / DISPUTED** | Validator-rejected plans are hidden. Ordinary late plans being flagged rather than always `infeasible` is consistent with Annex A. |
| 7 infeasible cut negative cost | **FIXED** | 90% request says not achievable and never emits the old negative sentinel cost. |
| 8 exception boundaries/Ruff | **FIXED** | Graph/LLM errors are bounded and sanitized; Ruff passes. |

`tests/test_graph_guards.py` contains every exact REVIEW-001 bypass and the new weights-cache regressions. Required router outputs were
exact: `20% CO2 reduction -> tradeoff(20)`, `cutting emissions by 5% -> tradeoff(5)`, and
`Plant B loses 30%, what happens to carbon? -> replan(B,30)`.

## Backend/eval verified correct

- API contract matched `shared/models.py`. Curl checks: valid `/analyze`, `/ingest`, `/health`, and `/trace/{id}`
  returned typed responses; missing-column/empty CSV and wrong table returned 422; unknown trace returned 404; no
  raw traceback was exposed. Extra CSV columns survive and bad rows include line-specific reasons.
- Frozen pack `e543801d`: all six data checks passed; counts 68 plants, 50 routes, 120 orders, 2 events.
  `latest_plan()` correctly rejects older `data_version`s. HEAD `b08990d` also passes resolved weights into the
  lookup for re-plan/explain; the 103-order stale-weight mismatch no longer reproduces.
- Eval runner defaults to both suites, validates `AnalyzeResponse`, evaluates conditions fail-closed without Python
  `eval`, and contains no system-output cost constants. After making auditor `tradeoff-001` data-robust, mock results
  are team **24/24** and auditor **26/26** in mock mode at current HEAD.
- No hard-coded backend answers or default tool stubs found. `USE_TOOL_STUBS` is false by default.
- `.env` is ignored/untracked; history/worktree scans found no API keys or private keys. Docker review passed
  statically: absolute entrypoint, shared image, generated+loaded first-start data, `.dockerignore` excludes secrets.
  `.env` is required, and both README and `docs/DOCKER.md` explicitly require copying `.env.example` first.
- Current HEAD with the generated frozen pack: full suite **248 passed** and Ruff passed.
