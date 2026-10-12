# Inbox for shivang
# Format: - [ ] task   (agent marks - [x] task (commit abc123) when done)

- [x] Make carbon-cut eval cases data-robust (see chat decision) (commit 968bd4e)
- [x] DECISION: freeze the data pack. Do NOT regenerate. Revert your uncommitted edits to scripts/generate_data.py and DATA_CARD.md (git checkout -- those files) unless they're bug fixes that don't change the data. Keep the data-robust eval expectations. Current headline numbers (deepest cut 5.4%, 9 late, 58% not at cheapest plant) are from this pack. Re-run both eval configs once Ayush and Mudit push, then commit. (commit 8e785f2: v2 reverted, data back to e543801d)
- [x] Don't wait for the late drop any more; Mudit will queue it when it arrives. (watcher stopped)
- [x] README: use frozen-pack numbers only (deepest cut 5.4%, 9 late, 58% not at cheapest plant, baseline 88 late / 3 overloaded plant-weeks), add the eval table, and leave 'AI-usage disclosure', 'Team contribution' and 'Declaration' as headed sections for us. Commit. (commit 7ba6a3d)
- [x] docs/DOCKER.md: exact steps for someone with Docker to run 'docker compose up --build' with MOCK_LLM=true, plus what to check (/health, one /analyze). Re-check Dockerfile and compose statically. Commit. (commit 0d483f3)
- [x] Re-collect 3 sample traces on the latest pushed code (allocation, scenario/re-plan, refused) into docs/traces/. Commit. (commit 7ba6a3d)
- [x] Re-run both eval configs on both suites once Mudit's and Ayush's latest commits are pushed; update eval/report.md. Commit. (commit 7ba6a3d, at eba2dd4)
- [x] From now on, never add Co-Authored-By: Claude lines to commits (see CLAUDE.md). (none of the Shivang commits have one; checked with git log)
- [ ] REVIEW-002: /ingest append mode accepts existing duplicate keys. Reject (or report and skip) rows whose key already exists, return them in rows_rejected with a reason, add a test. Then re-run both eval configs once Ayush and Mudit push (after 15:40) and update eval/report.md. Commit as Shivang.
