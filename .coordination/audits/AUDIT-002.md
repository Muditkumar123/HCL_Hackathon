# AUDIT-002: Re-check REVIEW-001 fixes + Shivang's backend
START CONDITION: only begin when `git log --oneline` shows a commit containing "Fix REVIEW-001". Until then, do AUDIT-003.

Part A: Re-verify REVIEW-001 (Mudit's files: ai/generation/, ai/graph.py, ai/llm.py, shared/, tests/test_graph_guards.py)
1. Re-run EVERY bypass input from REVIEW-001 exactly as written. For each: still bypasses? (yes/no + evidence).
2. Check the new intent-guard rule: require_all_on_time / weights_override only allowed when the request's own words justify them. Try 3 new phrasings that might sneak them through.
3. Check the rule router: "20% CO2 reduction" -> tradeoff, "cutting emissions by 5%" -> tradeoff, "Plant B loses 30%, what happens to carbon?" -> replan.
4. Check that infeasible carbon cuts are reported as not achievable (never as a negative extra cost).
5. Update REVIEW-001 findings to FIXED / STILL OPEN / DISPUTED in REVIEW-002.

Part B: Shivang's backend (backend/, scripts/, eval/, data/DATA_CARD.md, Dockerfile, docker-compose.yml)
1. API contract vs guide section 6: POST /analyze body {request, weights?, scenario?}; POST /ingest multipart CSV + JSON table name -> rows_loaded, rows_rejected, status; GET /health; GET /trace/{run_id}. Test each with curl, including bad inputs (missing column, wrong table name, empty file, unknown run_id): no raw tracebacks.
2. Loader: rejects bad rows with reasons, keeps extra columns, row counts match DATA_CARD.md. The 6 validation checks run and are honest.
3. latest_plan(): does it ever return a plan saved under older data (data_version mismatch)?
4. Eval runner: runs both Shivang's cases and your eval/cases.yaml; results reproducible; no expected values hard-coded from the system's own output.
5. Secrets: .env not tracked, no keys in Git history. Docker files: review statically (no Docker on this server); flag anything that would fail on `docker compose up --build`.
6. Disqualification check: any hard-coded answers anywhere in backend or eval.

Write findings to .coordination/reviews/REVIEW-002.md with verdict GREEN / YELLOW / RED, most severe first, each with file:line, failing input and fix.
