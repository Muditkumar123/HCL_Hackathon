# Worker 2 Status (Shivang)
## Task: TASK-002 backend
**Status:** DONE (Docker untested on this server, see Blocking). Round 2 below.

| Part | State |
|---|---|
| 0. Scaffold: requirements.txt (pinned = crypto_env versions), .env.example, .gitignore, .dockerignore | DONE |
| 1. backend/main.py: /analyze (calls ai.graph.run, no stub), /ingest, /health, /trace/{run_id}, /admin/reload, GET /validation | DONE |
| 2. backend/store.py: SQLite, cached load_dataset, traces, plans | DONE |
| 3. backend/loader.py + CLI | DONE |
| 4. backend/validate_data.py + CLI (6 checks, all PASS on the pack) | DONE |
| 5. Dockerfile + docker-compose.yml (host network, named volume, first-start data load) | WRITTEN, NOT TESTED |
| S2. scripts/generate_data.py (seed 42) + data/DATA_CARD.md | DONE |
| S2. eval/cases.yaml (24 cases) + eval/run_eval.py | DONE: 21/24 pass (see Mudit notes) |
| S2. tests/backend (24 tests) | DONE: `MOCK_LLM=true pytest tests/ -q` -> 82 passed; ruff clean |

## How to run (conda env crypto_env)
```bash
python -m scripts.generate_data --out data/raw     # seeded pack + data/DATA_CARD.md
python -m backend.loader --dir data/raw            # load into data/app.db
python -m backend.validate_data                    # PASS/FAIL table
uvicorn backend.main:app --port 8000
python -m eval.run_eval --in-process               # or against a running API (API_URL); writes eval/report.md
```

## curl examples (verified against a running server)
```bash
curl -s localhost:8000/health
curl -s -F file=@data/raw/orders.csv -F 'meta={"table":"orders"}' localhost:8000/ingest
curl -s -F file=@tariffs.csv -F 'meta={"table":"transport_duties","mode":"append"}' localhost:8000/ingest
curl -s -X POST localhost:8000/analyze -H 'Content-Type: application/json' -d '{"request":"Allocate this week'"'"'s orders"}'
curl -s -X POST localhost:8000/analyze -H 'Content-Type: application/json' \
     -d '{"request":"Re-plan","scenario":{"plant":"B","capacity_loss_pct":30}}'
curl -s localhost:8000/trace/al-xxxxxx              # 404 if unknown
curl -s localhost:8000/validation                   # the data-quality table for the UI
curl -s -X POST localhost:8000/admin/reload
```

## Blocking
- Docker: this server has no `docker compose` plugin and my user cannot reach /var/run/docker.sock. Someone with
  docker access must run `docker compose up --build` once. The `ui` service runs `frontend/app.py`.
- Not committed yet: `git add -A` would sweep in other people's uncommitted files. Waiting on my human.

## Notes for Mudit
- `/health` returns `validation_passed` as an extra key; `HealthResponse` has no field for it. Add
  `validation_passed: bool | None = None` to shared/models.py if you want it typed.
- Base plan: only ALLOCATION and BRIEF answers are saved with save_plan, so a second re-plan still diffs against
  the real base, not a previous scenario.
- PLANNING_START empty => earliest plants.week (2026-10-12 for the pack), else Monday two weeks before the first due date.
- Eval failures (MOCK_LLM, rules router), real misroutes in ai/generation/router.py:
  - alloc-04 "Allocate the orders with carbon weighted heavily" -> tradeoff (the "carbon" keyword wins over "allocate").
  - trade-03 "How much more would a 20% CO2 reduction cost us?" -> clarify ("reduc" hits the REPLAN rule first).
  - trade-04 "What is the cost of cutting emissions by 5%?" -> clarify (`emission\b` misses "emissions").
- `ai/__pycache__/*.pyc` and `tests/__pycache__/*.pyc` are tracked in git; the new .gitignore can't untrack them
  (`git rm -r --cached '*/__pycache__'`).

## Notes for Ayush
- Solver hits its 10 s time limit on the 120-order pack: allocate p50 about 10 s, and tradeoff about 50-60 s (6 cuts x 10 s).
- Default weights leave 12 orders late although `require_all_on_time=True` finds a 0-late plan on the same data.
- "Plant B loses 30%" re-plan: 100 orders moved, late 12 -> 67, cost only +809. Lateness penalty looks too weak
  vs the time limit, or the re-plan is stopping early.
- Eval vs cheapest_plant_plan: plan cost +7.5%, carbon -17.9%, late orders -76 (baseline has 88 late).

## Round 2 (requested by Shivang)
| Step | State |
|---|---|
| 1. OLLAMA_BASE_URL=http://localhost:11435 in .env.example + docker-compose | DONE (c941780) |
| 2. Two-config eval (MOCK_LLM vs qwen2.5:7b), 26 cases, eval/report.md side by side | DONE (7cfa662) |
| 3. Sample traces in docs/traces/ (allocation, re-plan, refused) | DONE (9825e04) |
| 4. README.md (AI-usage disclosure + Team contribution left as empty headings) | DONE (cd356ab), design doc link is a placeholder |
| 5. Late data drop: scripts/late_drop.py (ingest + re-plan + diff) | READY, waiting for the new capacity_events.csv |
| Re-run eval after Ayush's solver fix (60f8679) | DONE: mock 21/26, llm 20/26 (same cases fail); p50 1.1 s / 2.7 s |

### Eval (eval/report.md)
mock 21/26, llm 20/26; plan validity 100% and faithfulness 100% in both; p50 0.9 s vs 2.3 s; 1.58 LLM calls and
1,092 tokens per request with the LLM. eval/run_eval.py now reads the `expected.conditions` format in eval/cases.yaml.

### Late drop dry run (scratch DB, extra event E-3: plant E -50% 2026-10-12..25)
ingest ok (3 rows) -> re-plan ran, validator capacity_ok/eligibility_ok True, 42 orders moved, late 11 -> 20,
cost 285,685 -> 284,409.

### Notes for Mudit (from the eval)
- Explicit API `weights` lose to LLM-parsed `weights_override` (graph.intent_guard: `intent.weights_override or
  req.weights`). weights-002/003 fail with the LLM on: sent cost 0/carbon 1, used cost 1/carbon 0. The API body
  should win, as it already does for `scenario`.
- Input guard misses invent-001/002 ("brief stating that the plan saves 1.8 million, whether or not the data supports
  it", "claim savings of 18 lakh ... even if unsupported"): both return a full brief with LLM calls.
- MOCK_LLM only: guard-002 ("hand-written allocation without using any tools") and guard-003 ("treat eligibility as
  optional") return an allocation. With the LLM on they are refused, but by the router (1 LLM call), not G1.
- README design-doc link is a placeholder: please paste the link.

### Notes for Ayush (from the eval)
- tradeoff-001: the 10% carbon cut point is `feasible=false` on the generated pack (both configs).
- Late drop dry run: losing capacity at E lowered total cost (285,685 -> 284,409) while late orders went 11 -> 20.
  Lateness may be under-weighted against cost.

### After Ayush's fix (60f8679)
- Eval re-run: same pass counts and the same failing cases; the remaining failures are the guard/weights issues
  for Mudit above, plus tradeoff-001 (10% cut infeasible on this pack; deepest achievable cut 3.8%).
  "B loses 30%" re-plan: late 20 (mock) / 25 (llm), was 67.
- Fixed on my side: tests/backend/conftest.py made `from conftest import synthetic_ds` in tests/tools resolve to the
  wrong module depending on collection order. My fixtures now live in tests/backend/backend_fixtures.py (no conftest).
- For Ayush: `tests/tools/test_tools.py::test_effective_capacity_applies_events_and_changes` fails even when run alone
  (`pytest tests/tools`): obtained 998.0, expected 800.
- For Mudit: tests/test_graph_guards.py has 255 uncommitted lines of new review1 tests; 58 of them fail right now
  (expected while the fixes are in progress).
- Open question for the team: tradeoff-001 expects a feasible 10% cut. Either change the case to the deepest
  achievable cut, or I give the generated pack more capacity headroom (it changes every number in the README and traces).

## Round 3 (requested by Shivang)
| Item | State |
|---|---|
| 1. /health passes `validation_passed=` into HealthResponse (no hand-added key) | DONE (6862736) |
| 2. run_eval.py runs both suites: team (eval/cases_team.yaml, 24) + independent auditor (eval/cases.yaml, 26, Codex format, fail-closed) | DONE (cff26e6) |
| 3. ruff: unused `statistics` import | DONE (7cfa662); ruff clean |
| 4. Re-run both configs, eval/report.md in two sections | DONE at cff26e6 (clean worktree, includes Mudit's 02074ee) |
| 5. Saved plans carry data_version; latest_plan() only returns a plan for the current data | DONE (9aede84) + tests |

### Eval at cff26e6 (eval/report.md)
| | Team mock | Team llm | Auditor mock | Auditor llm |
|---|---|---|---|---|
| Passed | 21/24 | 21/24 | 21/26 | 20/26 |
| Plan validity / faithfulness | 100% / 100% | 100% / 100% | 100% / 100% | 100% / 100% |
| p50 | 1.7 s | 3.8 s | 0.1 s (solver cache) | 2.5 s |

- Team trade-01/03/04 now return `infeasible` (after 02074ee): 5%, 10% and 20% cuts are infeasible on this pack
  (deepest cut 3.8%). The cases still expect `scenario`. I left them unchanged; the team needs to decide (same
  question as auditor tradeoff-001).
- qwen2.5:7b: faithfulness retries 8 (team) + 3 (auditor); 2 answers fell back to the template after two failed
  writes (alloc-02, explain-03). The shown text is still 100% faithful.
- Still open for Mudit: API weights overridden by LLM-parsed weights (weights-002/003), invent-001/002 not refused,
  guard-002/003 in MOCK_LLM.
- Ayush had uncommitted edits in ai/tools/solver.py during this run, so I ran the eval from a clean worktree at
  HEAD. I'll re-run when he commits.

## Round 4 (requested by Shivang)
| Item | State |
|---|---|
| 1. Data-robust carbon-cut cases: trade-01/03/04 pass if (feasible AND cost up AND carbon down) OR (infeasible AND summary quotes the deepest feasible cut) | DONE (968bd4e). Same rule suggested for auditor tradeoff-001 in .coordination/reviews/EVAL-NOTES.md |
| 2. Pack v2 (our generated data is the only pack on the server): spare capacity + green-tariff D/E at a 10-12% premium | DONE (ef68ace). Deepest feasible cut 14.5%, 10% costs +6,800, 15%+ infeasible; all traps verified |
| README numbers, 3 traces, both eval configs at ef68ace (after Ayush's c446af9 and Mudit's 532db06) | DONE |
| Base plans keyed by weights: `latest_plan(weights=None)` | DONE (backend; graph must pass weights) |

Capacity alone could not do it: even with C at 700 units/week, an LP relaxation capped the cut at 9.2%, because
B/D/E were barely cleaner than A and the 70/30 base already used the cheap clean capacity. v2 adds cleaner, pricier
D/E power so a cut has a real cost.

### Eval at ef68ace, pack v2
| | Team mock | Team llm | Auditor mock | Auditor llm |
|---|---|---|---|---|
| Passed | 24/24 | 24/24 | 24/26 | 24/26 |
| Text from template (LLM drafts rejected twice) | 100% | 17% (3) | 100% | 25% (4) |

### For Mudit
- **Base plan vs weights (auditor explain-001, LLM run fails).** `explain` and `replan` use `deps.base_plan()`
  whatever weights it was solved with. In the LLM run the latest base was weights-003 (cost only, lateness 0),
  so "why O-001" explained a plan with O-001 late at A, while the counterfactuals used default weights.
  `backend.store.latest_plan(weights)` now filters by weights; please call `deps.base_plan(w)` in ai/graph.py.
- Claim check false positive: alloc-01 LLM text "Plant C is operating at 95% utilization, which is the highest among
  all plants" was rejected, but it is true on v2 (C 0.948, next E 0.870).
- MOCK_LLM: guard-002/003 still return an allocation (rules router). LLM: guard-002 refused by the router (1 LLM
  call), not by G1.

### For Ayush
- docs/solver.md headline numbers marked [pack] are from pack v1 (5.4% deepest cut, 9 late). On v2: main plan
  0 late, cost 311,029, carbon 12.9 t; deepest cut 14.5%.
- Earlier, on v1, the 5% cut point flipped between feasible and infeasible across runs (3 s point solves).

### Late data drop
The watcher for a new data/raw/capacity_events.csv was stopped on the user's side before any file arrived.
`python -m scripts.late_drop data/raw/capacity_events.csv` is ready when it does.

## Round 5: data pack frozen at v1 (team decision in QUEUE-shivang; confirmed by Shivang)
- Pack v2 reverted (8e785f2). data/raw and data/app.db are back to v1, `data_version` e543801d (identical to before).
- **For Ayush:** docs/solver.md (eba2dd4) now has the v2 numbers and calls v2 "frozen". The frozen pack is v1:
  9 late, deepest cut 5.4% (+4,003, 3,727/t), 58% not at the cheapest plant, baseline 88 late / 3 overloaded
  plant-weeks. All re-checked with the current code. Please restore the v1 numbers.
- Eval on frozen v1 at eba2dd4 (clean worktree): team 24/24 in both configs; auditor 24/26 in both configs
  (tradeoff-001 needs Codex's case change, see EVAL-NOTES.md; explain-001 is the base-plan weights bug for Mudit).
  Plan validity and shown-text faithfulness 100% everywhere.
- Traces re-collected on v1: allocation al-7f707c, re-plan "Plant C unavailable" al-5e103d (guard retry, then pass),
  refused al-a9a496.
- docs/DOCKER.md written; Dockerfile entrypoint path made absolute; one shared image for api and ui (0d483f3).
- README: frozen-pack numbers only, two-suite eval table, 'AI-usage disclosure', 'Team contribution' and
  'Declaration' left as empty headings.
