# Stage 2 practice drills

Timed exercises for the live-coding round. Each drill has failing acceptance tests in this folder.
Make them pass, using Claude Code the way you would in the interview, and **talk out loud** while
you do it: who the user is, what the change is for, how the model will decide to use it, and what
stops the loop.

These tests are not part of `uv run pytest` (that still runs only `tests/`), so the main suite
stays green while you practise.

## How to run one

```bash
git checkout -b drill-02 origin/claude/project-thread-wommpq   # a scratch branch per drill
uv run pytest drills/test_drill_02_ask_user.py                    # red
# ...start a timer, implement, explain out loud...
uv run pytest drills/test_drill_02_ask_user.py tests              # green, and nothing else broke
```

Start each drill from a clean branch so they stay independent. Drill 7 is easier after drill 6.

## The drills

| # | Time | Drill | What it practises |
|---|---|---|---|
| 1 | done | `terminal` flag ends the turn (PR #1) | Read it again: `_accept` and the tool loop in `ask()` |
| 2 | 15 min | `ask_user` terminal tool | Adding a tool; a tool that stops the loop without SQL |
| 3 | 15 min | Schema on demand: `schema_mode="names"` + `get_table_schema` | Scaling to a 1,000-table warehouse; prompt tokens vs extra calls |
| 4 | 20 min | `find_similar_queries` over verified question/SQL pairs | Retrieval as a tool; keeping the eval honest (no dev questions in the file) |
| 5 | 15 min | `search_glossary` over the column docs | Routing between sources: meaning (glossary) vs spelling (`get_column_values`) |
| 6 | 10 min | Time budget and per-tool call cap | Stop conditions; bounding cost and P95 latency |
| 7 | 15 min | Router: cheap model, escalate on failure | Cost vs quality; where model choice lives (`_call_llm`) |
| 8 | 30 min | `src/loadtest.py` + `uv run loadtest` | The performance framework they will ask for |
| 9 | 5 min | No code: explain the agent from the user's outcome | "Raul's analysts get correct numbers in under 3s" before any code |

The exact spec for each drill is in the docstring at the top of its test file. Each one also has a
"Discuss" question: have an answer ready, because that is what the interviewer will ask next.

## Things to say for any new tool

1. **Who needs it and why:** the user problem, in one sentence.
2. **Routing:** the tool description is the router. Say when the model should and shouldn't call it.
3. **Stopping:** is it terminal? What bounds it (step budget, per-tool cap, timeout)?
4. **Safety:** does it touch data? Anything reaching the database goes through `db.execute_sql`.
5. **Proof:** a `FakeClient` test for the loop, then `uv run eval` for real accuracy and latency.

## After drill 8: run it for real (on your Mac)

```bash
uv run loadtest --concurrency 1,4,8 --duration 60 --model accounts/fireworks/models/gpt-oss-120b
```

Look at how P95 and the error rate change with concurrency, and be ready to explain 429s (rate
limit) vs 503s (load shedding or a deployment scaling up).

## Reference solutions

`solutions/reference.patch` has one way to do drills 2 to 8. Only open it after your own attempt.
To compare: `git apply drills/solutions/reference.patch` on a fresh branch, then
`uv run pytest drills tests` (44 pass).
