# REVIEW-004 — RED finding replay (HEAD `6ef4369`)

**Verdict: RED — two solver correctness findings remain open.** No new audit was performed.

| Prior finding | Status | Fix commit / current evidence |
|---|---|---|
| REVIEW-002 #1 — G2 negation/mention controls (3 inputs) | **FIXED** | `d24a5ca`. All exact inputs now discard the unrequested LLM control: lateness remains allowed and both weight overrides fall back to 70/30. |
| REVIEW-002 #2 — G4 synonym claims (4 inputs) | **FIXED** | `d24a5ca`. All four exact unsupported statements now return `faithfulness_ok=false`. |
| REVIEW-003 #1 — one-day-late cheap plant beats on-time plant | **STILL OPEN** | No fixing commit. Exact replay still returns A, arriving `2026-10-14` one day late, objective `0.71`; on-time B remains available. |
| REVIEW-003 #2 — avoidable deferral trap | **STILL OPEN** | No fixing commit. Exact replay still assigns O-1 to A and leaves O-2 unassigned, objective `0.82`; forcing O-1 to B yields both orders assigned on time and validator-clean. |
| REVIEW-003 #3 — heuristic frontier presented as proof | **FIXED** | `d24a5ca` fixed runtime wording; `6ef4369` added the deferral cap/certified-infeasibility path. Frozen pack `e543801d`, exact 10% request now says: “no feasible plan found within the time limit; best found 5.4%,” never “not feasible,” “impossible,” or “deepest feasible.” |
| REVIEW-003 #4 — validator NaN, forged dates, split-week capacity (3 inputs) | **FIXED** | `6ef4369`. The exact NaN and forged-date rows now fail eligibility; the Monday/Tuesday 600+600 split now fails capacity and flags the non-Monday date. |

Verification: the 10 exact G2/G4/validator regressions passed (`10 passed`), and both solver scenarios plus the frozen-pack 10% request were replayed directly under `crypto_env`.
