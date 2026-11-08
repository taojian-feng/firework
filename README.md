# Text-to-SQL CLI on Fireworks

A proof of concept for GitLab's "agentic BI CLI": ask a database questions in plain English from the
terminal, see the SQL and the results, and ask follow-ups. It runs on open models served by
Fireworks and ships with an eval harness that measures accuracy, latency and token cost against the
dev questions and against the customer's original one-line prompt.

The original assignment is kept in [docs/ASSIGNMENT.md](docs/ASSIGNMENT.md). The email to the
customer is [EMAIL.md](EMAIL.md).

## Quick start

Requirements: Python 3.11+, [uv](https://github.com/astral-sh/uv), `curl`, `unzip`, `sqlite3`
(the last three only for `setup.sh`).

```bash
./setup.sh                      # downloads data/financial.sqlite (skip if it is already there) and runs uv sync
cp .env.example .env            # then put your key in .env, or:
export FIREWORKS_API_KEY=fw_...

uv run cli                      # interactive CLI   (same as: python -m src.cli)
uv run eval                     # 10 dev questions -> dev_answers.json + results/
uv run pytest                   # offline unit tests, no API key needed
```

### Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `FIREWORKS_API_KEY` | yes | | Fireworks API key |
| `T2SQL_MODEL` | no | `accounts/fireworks/models/kimi-k3` | Model used by the agent |
| `T2SQL_DB_PATH` | no | `data/financial.sqlite` | SQLite database |
| `T2SQL_DESCRIPTION_DIR` | no | `data/database_description` | Column-doc CSVs (optional) |
| `T2SQL_MAX_STEPS` | no | `6` | LLM calls allowed per question |
| `T2SQL_REASONING_EFFORT` | no | unset | `low`/`medium`/`high` for reasoning models that accept it |

All settings live in [src/config.py](src/config.py).

### CLI usage

Illustrative session (real measurements are under [Results](#results)):

```
$ uv run cli
ask> How many female clients live in districts where the average salary is greater than 10,000?
╭─ SQL ─────────────────────────────────────────────────────────────╮
│ SELECT COUNT(DISTINCT c.client_id) AS female_clients FROM client c │
│ JOIN district d ON c.district_id = d.district_id                   │
│ WHERE c.gender = 'F' AND d.A11 > 10000                             │
╰────────────────────────────────────────────────────────────────────╯
┏━━━━━━━━━━━━━━━━┓
┃ female_clients ┃
┡━━━━━━━━━━━━━━━━┩
│ 636            │
└────────────────┘
1.4s total (1.3s model, 1 call(s)) · 2,950 in / 85 out tokens
ask> now only in Prague
...
```

Commands: `/schema` (the context the model sees), `/trace` (LLM and tool steps behind the last
answer), `/reset` (new topic), `/help`, `exit` / `quit`.

Other databases: `uv run cli --db path/to/any.sqlite` (column docs are optional, add
`--descriptions dir/` if you have them). Other models: `uv run cli --model accounts/fireworks/models/gpt-oss-120b`.

## How it works

```
question ──► SQLAgent.ask()  (src/agent.py)
               │  system prompt = rules + grounded schema (src/schema.py), built once per session
               │  + last 5 question/SQL pairs (follow-ups) + the new question
               ▼
             LLM on Fireworks (OpenAI-compatible API, tool calling)
               │
               ├─ get_column_values(table, column)  ─┐  optional exploration,
               ├─ run_sql(sql)                       ─┘  results go back to the model
               │
               └─ submit_answer(sql, assumptions)  ──►  executes read-only (src/db.py)
                       error / 0 rows ──► back to the model to repair
                       rows           ──► AgentResult: SQL, rows, assumptions, timings, tokens
```

| File | What it does |
|---|---|
| [src/cli.py](src/cli.py) | REPL, rendering, slash commands |
| [src/agent.py](src/agent.py) | The tool-calling loop and stop conditions, conversation memory |
| [src/tools.py](src/tools.py) | Tool registry: `submit_answer`, `run_sql`, `get_column_values` |
| [src/schema.py](src/schema.py) | Introspects any SQLite DB + optional doc CSVs into prompt text |
| [src/db.py](src/db.py) | Read-only connection, SQL guard, row cap, timeout |
| [src/prompts.py](src/prompts.py) | System prompt and the customer's baseline prompt |
| [src/evaluate.py](src/evaluate.py) | Eval harness: execution accuracy, latency P50/P95, tokens, cost |
| [tests/](tests/) | Offline tests with a scripted fake LLM client |

### Design decisions

**Ground the model in the real schema, docs and values.** The customer's failures (hallucinated
columns, wrong joins) come from the model guessing. The prompt contains every table and column with
its type, keys, the CSV documentation (so it knows `district.A3` is the region and `A11` is average
salary), and the actual distinct values of low-cardinality text columns. The values matter: the docs
say `disp.type` can be `USER` but the data only has `OWNER`/`DISPONENT`, and region names are spelled
`south Moravia`. The whole schema is about 2k tokens, so it goes in full. For a customer database
with hundreds of tables the same per-table text becomes the documents for embedding retrieval
(e.g. `qwen3-embedding-8b`), and only the relevant tables are sent.

**A small agent loop, designed so the common case is one LLM call.** The model can call
`submit_answer` straight away, which executes the query and ends the turn. The results are shown to
the user as a table, with no second LLM call just to phrase them, which keeps latency down. Exploration
tools (`run_sql`, `get_column_values`) are there for when the model is unsure. The loop stops when
`submit_answer` succeeds, when the model replies in plain text (a clarifying question, or "this data
isn't in the schema"), or when the step budget runs out (`T2SQL_MAX_STEPS`, also a latency cap).

**Self-repair.** SQL errors and a first empty result are returned to the model as tool results, so
it fixes its own query (wrong column, bad filter value) instead of failing in front of the user.

**Trust: show the SQL and the assumptions.** Every answer shows the exact SQL, and the model must
state interpretations it made (e.g. "good standing" → `status = 'C'`). A BI user can check it.

**Safe by construction.** Generated SQL is untrusted. The connection is opened `mode=ro` with
`PRAGMA query_only`, only a single `SELECT`/`WITH` statement is accepted, results are capped at
1,000 rows, and queries time out after 10s.

**Follow-ups.** The agent replays the last 5 question/SQL/result-preview pairs, not the full tool
trace, so "now only for women" can modify the previous SQL while prompts stay small. `/reset`
starts a new topic.

**Adding a tool** means writing a handler and adding one `Tool(...)` entry in
[src/tools.py](src/tools.py). The loop does not change. Mark it `terminal=True` if it should end the turn.

## Validation

`uv run eval` runs the 10 dev questions and scores them by **execution match**: the predicted SQL
is executed and its result set compared with the gold result, so any correct query passes, not just
one written the same way. Floats compare to 2 decimals, and row order matters only when the gold
query has `ORDER BY`. Two scores are reported:

* **strict**: same rows, same number of columns
* **lenient**: the gold columns appear in the result, extra columns allowed. The gap between the
  two shows answers that are right but shaped differently, as opposed to answers that are wrong.

It also reports executable rate, latency P50/P95, LLM calls per question and tokens per question,
and with `--price-in/--price-out` ($ per 1M tokens) the cost per query and per day at 30k queries.
`--baseline` runs the customer's prompt (`Convert this question to SQL: {question}`) through
the same scorer for a like-for-like comparison. Per-question SQL, answers and traces are saved
to `results/`.

The scorer itself is tested: `tests/test_evaluate.py` checks that all 10 gold queries score as
matches.

```bash
uv run eval                                   # agent -> dev_answers.json
uv run eval --baseline                        # customer prompt, same model
uv run eval --model accounts/fireworks/models/gpt-oss-120b --repeat 3   # model comparison
```

### Results

<!-- RESULTS:START -->
_To be filled in from `results/` after the eval run._
<!-- RESULTS:END -->

### Known limitations

* 10 dev questions is too few to claim an accuracy number. It is a smoke test. The next step is a
  customer-specific eval set of 100+ questions (see EMAIL.md).
* SQLite only. Other databases need a SQLAlchemy connection layer plus a read-only DB role, and
  dialect hints in the prompt.
* The full schema goes into the prompt. Very wide schemas need retrieval first (above).
* Follow-up memory is a short window and does not summarize older turns.

## How I used AI tools

I used **Claude Code** (Anthropic's coding agent) as a pair programmer for this take-home: reading
the assignment, exploring the database, scaffolding the modules, writing the unit tests and the fake
LLM client, and drafting the README and email. I reviewed and edited all of it, I made the design
calls (schema grounding with real values, one-call fast path, execution-based scoring), and I can
walk through every file. [CLAUDE.md](CLAUDE.md) holds the project notes Claude Code reads, which
also makes it quick to extend live.

Dataset: BIRD benchmark `financial` database, see [BIRD_LICENSE.md](BIRD_LICENSE.md).
