# AUDIT-001: Graph, guardrails and contract
Scope: ai/graph.py, ai/generation/ (guards.py, router.py, writer.py, prompts.py), ai/llm.py, shared/models.py, tests/test_graph_guards.py.
Focus:
1. Can any path show an LLM-made number or an unvalidated plan?
2. Try to bypass G1, G2 and G4 with real inputs and report each bypass with the exact input.
3. Does every response field match guide section 6 and shared/models.py?
4. Does run() ever leak a traceback?
Then start eval/cases.yaml.
