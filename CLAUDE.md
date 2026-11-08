# Project notes for Claude Code

Text-to-SQL CLI on Fireworks (take-home for the Fireworks AI Field Engineer role). See README.md
for the design; this file is the short version for working in the code.

## Commands
- `uv run pytest` – offline tests (fake LLM client in tests/conftest.py). Run after every change.
- `uv run cli` – interactive CLI (needs FIREWORKS_API_KEY).
- `uv run eval [--baseline] [--model M] [--repeat N] [--questions q_001,q_002]` – dev-set eval.

## Layout
- src/agent.py – `SQLAgent.ask()` is the loop. Stops on: terminal tool result, plain-text reply, step budget.
- src/tools.py – tool registry. New tool = handler(ctx, **args) -> ToolOutput, plus a Tool(...) in build_tools().
- src/schema.py – schema text for the prompt (introspection + doc CSVs + enum values).
- src/db.py – the only place SQL is executed. Keep it read-only.
- src/evaluate.py – execution-match scoring, latency/token stats, writes dev_answers.json and results/.
- docs/ARCHITECTURE.md – diagrams; sources in docs/diagrams/*.mmd (update them when the loop changes).
- src/utils.py – helpers that came with the assignment (unchanged).

## Conventions
- Never execute model SQL except through `db.execute_sql`.
- SQL errors are returned as values (QueryResult.error), not raised, so the model can repair them.
- Keep prompt text in src/prompts.py.
- Don't tune prompts to the 10 dev questions' exact wording; keep rules general.
