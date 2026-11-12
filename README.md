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
1.6s total (1.5s model, 1 call(s)) · 3,050 in / 120 out tokens
ask> now only in Prague
...
```

Commands: `/schema` (the context the model sees), `/trace` (LLM and tool steps behind the last
answer), `/reset` (new topic), `/help`, `exit` / `quit`.

Other databases: `uv run cli --db path/to/any.sqlite` (column docs are optional, add
`--descriptions dir/` if you have them). Other models: `uv run cli --model accounts/fireworks/models/gpt-oss-120b`.

## Finance assistant: web search, guardrail, stop conditions

The CLI is now a finance-only assistant with two sources. Questions about the bank's own data
go to SQL as before; market and general finance questions go to the web; everything else is
refused.

```
question
  │
  ├─ 1. guardrail (src/agent.py: _is_finance)  one short LLM call: FINANCE or OTHER
  │       OTHER ──► "I can only help with finance questions"   (no tools run, nothing remembered)
  │
  └─ 2. agent loop, one set of tools, the model picks:
          bank data    run_sql / get_column_values ─► submit_answer(sql)       route = "sql"
          prices       get_quote(ticker)            ─┐
          market data  web_search(query) ≤ 3 times ─┴► final_answer(answer, sources)  route = "web"
```

**Routing.** There is no hand-written router. The guardrail only decides *in scope or not*. Inside
scope, the model chooses the tool, guided by the tool descriptions ("never use web_search for the
bank's own clients, accounts, loans or cards") and one rule in the system prompt ("if the answer is
in the database, use SQL; otherwise search, never answer market questions from memory"). Adding a
third source later is one more tool, not a new branch in the code.

**Why a separate guardrail call** instead of only a prompt rule: it is cheap (a few hundred tokens,
can run on a small model via `T2SQL_GUARD_MODEL`), it is testable on its own, and an off-topic or
prompt-injection question never reaches the tools. It sees the previous question, so follow-ups like
"and for 1997?" stay in scope. If the classifier call fails it lets the question through (fail
open), because the main prompt also refuses non-finance questions; a stricter deployment would fail
closed. Cost: one extra call per question (about +0.3 to 0.5 s). The next optimisation is to run it
in parallel with the first agent call and drop that answer if the verdict is OTHER.

**Prices come from data, not search.** A first version answered "today NVIDIA stock price" from
search snippets and reported an after-hours price from an earlier day as today's: snippets carry no
reliable timestamp. `get_quote` (yfinance) returns the latest price *with its timestamp*, and the
prompt says never to take a price from snippets. web_search stays for news, earnings and context.
In production this would be a licensed market-data API; the tool interface stays the same.

**Fresh results.** Today's date is in the system prompt, and the model is told to put the year
(or month and year) in time-sensitive searches and to say how recent its information is. Without
it, the model searches as if it were its training cutoff and returns year-old prices.

**Stop conditions.** One question can't hang or run up cost:

| Stop | Where | Default |
|---|---|---|
| `submit_answer` succeeds or `final_answer` is called (terminal tools) | `tools.py`, `_accept` | |
| Plain-text reply (clarifying question, or an answer after searching) | `ask()` | |
| Step budget: max LLM calls per question | `T2SQL_MAX_STEPS` | 6 |
| Time budget, checked before every LLM call after the first | `T2SQL_MAX_SECONDS` | 30 s |
| Timeout per LLM request (client also retries twice) | `T2SQL_LLM_TIMEOUT_S` | 60 s |
| Web searches per question; then the tool says "limit reached, answer now" | `T2SQL_MAX_SEARCHES` | 3 |
| Timeout per web search | `T2SQL_SEARCH_TIMEOUT_S` | 10 s |

A failed search (timeout, rate limit) comes back to the model as text, like a SQL error, so it can
try another query or answer with what it has. `AgentResult.error` is set when a turn stops on a
budget or an API error, which is what the load test counts as an error.

**Try it**

```bash
uv run cli                       # "How many clients live in Prague?" · "How did NVIDIA do last quarter?" · "Weather in London?"
uv run cli --no-web --no-guardrail   # the original text-to-SQL behaviour
uv run finance-eval              # 13 questions: 4 SQL, 5 web (1 price), 4 refuse -> per-route accuracy and latency
uv run loadtest --concurrency 1,4,8 --duration 30            # p50/p95, qps, error rate per level
uv run loadtest --route sql --concurrency 1,8,16 --model accounts/fireworks/models/gpt-oss-120b
```

**Eval** ([evals/finance_questions.json](evals/finance_questions.json),
[src/finance_eval.py](src/finance_eval.py)). A question passes when it takes the expected route and
the answer checks out: SQL results match the gold query (execution match), web answers come with
at least one source, refusals come from the guardrail. Web answer *content* isn't graded
automatically because it changes daily; the saved report has every answer and trace to read.

**Load test** ([src/loadtest.py](src/loadtest.py)). Closed loop: N workers, each with its own
agent and DB connection, ask random questions back to back for a fixed time. It reports requests,
throughput (qps), p50/p95/max latency, error rate with sample errors, LLM calls and tokens per
question. Watch how p95 and errors move with concurrency: 429s mean a rate limit, 503s mean load
shedding or a deployment scaling up. DuckDuckGo rate-limits quickly, so `--route sql` measures the
model and the database without it.

## How it works

Detailed diagrams (control flow and a step-by-step sequence) are in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).


Short version:

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
| [src/tools.py](src/tools.py) | Tool registry: `submit_answer`, `run_sql`, `get_column_values`, `web_search`, `get_quote`, `final_answer` |
| [src/schema.py](src/schema.py) | Introspects any SQLite DB + optional doc CSVs into prompt text |
| [src/db.py](src/db.py) | Read-only connection, SQL guard, row cap, timeout |
| [src/prompts.py](src/prompts.py) | System prompt and the customer's baseline prompt |
| [src/evaluate.py](src/evaluate.py) | Eval harness: execution accuracy, latency P50/P95, tokens, cost |
| [src/finance_eval.py](src/finance_eval.py) | Routing eval for the finance assistant (SQL / web / refuse) |
| [src/loadtest.py](src/loadtest.py) | Concurrency benchmark: p50/p95, throughput, error rate |
| [tests/](tests/) | Offline tests. `FakeClient` in [tests/conftest.py](tests/conftest.py) stands in for the Fireworks API and returns pre-written model replies, so tests run without a key or network |

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
isn't in the schema"), or when the step budget runs out (`T2SQL_MAX_STEPS`, also a latency cap). A
plain-text reply that ran no SQL is sent back once with "call submit_answer if this is a data
question", because models sometimes repeat a number from earlier in the conversation instead of
querying. That way every data answer comes with SQL the user can check.

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
Measured on 2026-11-08 on Fireworks serverless. Agent runs repeat each question 3 times (30 answers);
the baseline ran once. Raw per-question SQL, answers and traces are in [results/](results/).
Cost uses Fireworks list prices per 1M tokens (Kimi K3 $3 in / $15 out, gpt-oss-120b $0.15 /
$0.60), uncached. Prompt caching of the fixed schema prefix would lower the input share.

| Setup | Correct | SQL executes | P50 | P95 | LLM calls | Tokens in / out | $ / query | $ / day at 30k |
|---|---|---|---|---|---|---|---|---|
| Customer prompt, kimi-k3 (baseline) | 5/10 | 5/10 | 8.9s | 24.0s | 1.0 | 46 / 590 | 0.0090 | 270 |
| Agent, kimi-k3 | 30/30 | 30/30 | 4.8s | 19.3s | 1.03 | 3,108 / 366 | 0.0148 | 444 |
| Agent, kimi-k3, `reasoning_effort=low` | 30/30 | 30/30 | 2.6s | 9.9s | 1.0 | 3,057 / 122 | 0.0110 | 330 |
| **Agent, gpt-oss-120b** | **30/30** | **30/30** | **1.6s** | **4.0s** | 1.5 | 4,519 / 281 | **0.00085** | **25** |

What the numbers say:

* **Grounding fixes the quality problem.** The baseline failed exactly the way the customer
  described: `FROM loans`, `district_name`, `transaction_date`, `birth_number`, and ANSI `DATE '1997-01-01'`
  syntax that SQLite rejects. All five failures were invented names or wrong dialect, and none
  of them survive once the model sees the real schema.
* **gpt-oss-120b meets the latency and cost targets** at the same dev-set accuracy: 1.6s P50, about
  17x cheaper than Kimi K3. It needed the fallback in 6 of 30 answers: it ran the correct query
  with `run_sql` and then answered in prose instead of calling `submit_answer`. Before that fallback
  (commit 6fcce86) it scored 8/10.
* **Kimi K3 is accurate but too slow by default.** With reasoning on it's 4.8s P50 and has long-tail outliers
  (one call took 40s). `reasoning_effort=low` halves latency and output tokens at no accuracy cost here.
* `dev_answers.json` comes from a single kimi-k3 run (10/10, the CLI default).

Scorer note: the first low-effort run scored 27/30 because q_008 has two districts tied at 53 owners, and
SQLite returned them in the other order. Tie order is undefined in SQL, so the scorer now
requires only the sort column to match in sequence. All saved runs were re-scored with
`uv run eval --rescore results/*.json`, which makes no LLM calls.
<!-- RESULTS:END -->

### Known limitations

* 10 dev questions is too few to claim an accuracy number, and 30/30 on all three agent setups means
  this set no longer separates the models. It is a smoke test. The next step is a
  customer-specific eval set of 100+ questions (see EMAIL.md).
* SQLite only. Other databases need a SQLAlchemy connection layer plus a read-only DB role, and
  dialect hints in the prompt.
* The full schema goes into the prompt. Very wide schemas need retrieval first (above).
* Follow-up memory is a short window and does not summarize older turns.

## How I used AI tools

I used **Claude Code** (Anthropic's coding agent) as a pair programmer for this take-home: reading
the assignment, exploring the database, scaffolding the modules, writing the unit tests, and drafting the README and email. I reviewed and edited all of it, I made the design
calls (schema grounding with real values, one-call fast path, execution-based scoring), and I can
walk through every file. [CLAUDE.md](CLAUDE.md) holds the project notes Claude Code reads, which
also makes it quick to extend live.

Dataset: BIRD benchmark `financial` database, see [BIRD_LICENSE.md](BIRD_LICENSE.md).
