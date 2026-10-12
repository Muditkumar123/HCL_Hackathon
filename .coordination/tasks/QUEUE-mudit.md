# Inbox for mudit
# Format: - [ ] task   (agent marks - [x] task (commit abc123) when done)

- [x] From now on, never add Co-Authored-By: Claude or Claude-Session lines to commits (see CLAUDE.md). (no commit: process rule, applied to every commit after ed4ae1e)
- [x] In ai/graph.py, call deps.base_plan(w) with the resolved weights (Shivang's latest_plan(weights) now filters by weights) so 'why' and re-plan never reuse a base plan solved with different weights. Fixes eval explain-001. Add a test. pytest, commit, push. (commit b08990d)
- [x] REVIEW-002 (RED) is in .coordination/reviews/REVIEW-002.md. Fix every G2 and G4 bypass Codex lists, with tests using the exact inputs. Also change infeasible-cut wording everywhere (template, facts, summary prompt) to 'no feasible plan found within the time limit; best found X%'. Never say 'impossible' unless proven. Timebox: done by 15:40. pytest, commit, push. (commit d24a5ca; REVIEW-002 #3 duplicate keys on append ingest is Shivang's backend/loader.py, not mine)
