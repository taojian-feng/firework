# How the agent works

Two views of the same system. The Mermaid source is in [diagrams/](diagrams/) and renders on
GitHub. PNG and SVG exports are next to it for slides or offline viewing
([flow PNG](diagrams/agent_flow.png), [sequence PNG](diagrams/agent_sequence.png)).

## 1. Control flow of one question

The whole loop lives in `SQLAgent.ask()` ([src/agent.py](../src/agent.py)). Every query, from any
tool, goes through the single read-only executor in [src/db.py](../src/db.py) (green box).

```mermaid
flowchart TD
    subgraph START["Session start · once"]
        direction LR
        DB[("SQLite DB<br/>opened read-only<br/>db.connect_readonly")]
        CSV["Column-doc CSVs<br/>optional"]
        INTRO["schema.build_schema_context<br/>tables · types · PK/FK<br/>column docs · ALL VALUES of enum columns"]
        SYS["System prompt<br/>prompts.SYSTEM_PROMPT + schema<br/>~3k tokens, identical every call → cacheable"]
        DB --> INTRO
        CSV --> INTRO
        INTRO --> SYS
    end

    U(["User types a question<br/>cli.main"]) --> CMD{"/schema · /trace<br/>/reset · exit?"}
    CMD -- "yes" --> CMDH["Handled locally<br/>no LLM call"]
    CMD -- "no" --> MSG["Build messages<br/>system prompt + last 5 Q/SQL/result pairs + question<br/>agent.SQLAgent.ask"]
    SYS -.-> MSG

    MSG --> BUDGET{"Last step?<br/>step = max_steps − 1"}
    BUDGET -- "yes" --> NUDGE["Append nudge:<br/>call submit_answer now"] --> LLM
    BUDGET -- "no" --> LLM["LLM call on Fireworks<br/>OpenAI-compatible API<br/>tools = submit_answer, run_sql, get_column_values"]

    LLM -- "API error" --> FAIL["Show error message"]
    LLM --> TC{"Tool calls<br/>in the reply?"}

    TC -- "run_sql" --> RS["tools.run_sql<br/>exploratory query, rows go back to the model<br/>remembers last successful result"]
    TC -- "get_column_values" --> GV["tools.get_column_values<br/>distinct values + counts"]
    TC -- "submit_answer" --> SUB["tools.submit_answer<br/>runs the final query"]

    RS --> EXEC
    GV --> EXEC
    SUB --> EXEC
    EXEC["db.execute_sql<br/>1 statement · SELECT/WITH only<br/>mode=ro + query_only<br/>1,000-row cap · 10 s timeout<br/>errors returned as values"]

    EXEC --> OUTRS{"Which tool?"}
    OUTRS -- "run_sql / get_column_values" --> BACK["Tool result appended<br/>to messages"]
    OUTRS -- "submit_answer" --> CHK{"Result?"}
    CHK -- "SQL error" --> REPAIR["Error text back to the model:<br/>fix and resubmit"] --> BACK
    CHK -- "0 rows, first time" --> EMPTY["Ask the model to double-check<br/>filter values, then resubmit"] --> BACK
    CHK -- "rows, or 0 rows again" --> DONE

    BACK --> MORE{"Steps left?"}
    MORE -- "yes" --> BUDGET
    MORE -- "no" --> STOP["Stopped after max_steps"]

    TC -- "no tools: plain text" --> TXT{"SQL in a<br/>code block?"}
    TXT -- "yes" --> SUB
    TXT -- "no" --> FB{"run_sql already<br/>returned rows?"}
    FB -- "yes · fallback" --> DONE
    FB -- "no" --> NUDGED{"Already nudged,<br/>or last step?"}
    NUDGED -- "no" --> TNUDGE["Ask once: call submit_answer<br/>if this is a data question"] --> BUDGET
    NUDGED -- "yes" --> CLAR["Clarifying question<br/>or 'not in this data'"]

    DONE(["AgentResult<br/>SQL · rows · assumptions<br/>latency · LLM calls · tokens"])
    DONE --> RENDER["cli.render<br/>SQL panel · assumption · results table · timing"]
    CLAR --> RENDER
    STOP --> RENDER
    FAIL --> RENDER
    RENDER --> HIST["Save Q + SQL + 5-row preview<br/>to history for follow-ups<br/>agent._remember"]
    HIST --> NEXT(["Wait for the next question"])

    classDef llm fill:#ede7f6,stroke:#5e35b1,color:#1a1a1a
    classDef tool fill:#e3f2fd,stroke:#1e88e5,color:#1a1a1a
    classDef safe fill:#e8f5e9,stroke:#43a047,color:#1a1a1a
    classDef stop fill:#fff3e0,stroke:#fb8c00,color:#1a1a1a
    class LLM,NUDGE llm
    class RS,GV,SUB tool
    class EXEC,DB safe
    class DONE,CLAR,STOP,FAIL stop
```

### Reading the diagram

| Stage | Where | What happens | Why |
|---|---|---|---|
| Session start | `schema.build_schema_context` | Introspect tables, types, keys; merge the doc CSVs; list every value of low-cardinality text columns | Stops hallucinated names and wrong filter values (e.g. `'south Moravia'`, loan status `'C'`) |
| Build messages | `SQLAgent.ask` | System prompt + last 5 question/SQL/result-preview pairs + the new question | Follow-ups ("now only women") can edit the previous SQL; prompt stays small |
| LLM call | `SQLAgent._call_llm` | OpenAI-compatible call to Fireworks with 3 tool specs, temperature 0 | One API shape for any Fireworks model; `--model` switches it |
| Exploration | `tools.run_sql`, `tools.get_column_values` | Model checks data or values before answering; rows go back to the model only | Lets the model verify when unsure, without the user seeing scratch work |
| Final answer | `tools.submit_answer` | Executes the final SQL. An error or a first empty result goes back to the model | Self-repair: broken SQL is fixed before the user sees it |
| Safety | `db.execute_sql` | One `SELECT`/`WITH` statement, `mode=ro` + `query_only`, 1,000-row cap, 10s timeout, errors returned as values | Model output is untrusted; the database can't be modified |
| Stop conditions | `SQLAgent.ask` | (1) a terminal tool ends the turn (`submit_answer` accepted, or a terminal tool returns a reply for the user), (2) plain-text reply (asked once to call `submit_answer` instead, so a data answer always comes with SQL; a real clarifying question comes back as text again), (3) `max_steps` reached (the last step adds a "submit now" nudge), (4) API error | Bounded latency and cost per question |
| Fallbacks | `SQLAgent.ask` | SQL in a code block → executed as a submit. Prose after a successful `run_sql` → that result is used | Some models (gpt-oss-120b) skip the final tool call: 8/10 → 10/10 |
| Output | `cli.render`, `AgentResult` | SQL panel, assumptions, results table, latency, LLM calls, tokens. `/trace` shows each step | Trust: users can check the SQL and the interpretation |

The happy path is **one LLM call**: the model calls `submit_answer` straight away and the results
are rendered directly, with no second call to phrase them. Over 30 runs, Kimi K3 averaged 1.03 calls
per question and gpt-oss-120b averaged 1.5.

## 2. A concrete session

The first question takes the fast path. The follow-up shows the repair loop. The `c.sex` typo
was made up to illustrate it; the gender counts and the final answers (18 and 22) are the real
values in the database.

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant CLI as cli.py
    participant Agent as agent.SQLAgent
    participant LLM as Fireworks LLM
    participant Tools as tools.py
    participant DB as db.execute_sql (read-only)

    Note over Agent: Startup: schema.py introspects the DB once,<br/>adds column docs and enum values to the system prompt

    User->>CLI: How many running loans in good standing<br/>belong to male owners in north Bohemia?
    CLI->>Agent: ask(question)
    Agent->>LLM: system prompt (schema) + history + question + tool specs
    Note over LLM: Fast path: confident, so it submits right away
    LLM-->>Agent: submit_answer(sql, assumptions="good standing = status 'C'")
    Agent->>Tools: call_tool("submit_answer", args)
    Tools->>DB: execute_sql(sql)
    DB-->>Tools: QueryResult(rows=[(18,)])
    Tools-->>Agent: ToolOutput(final_result)
    Agent-->>CLI: AgentResult (1 LLM call)
    CLI-->>User: SQL panel, assumption, table: 18

    User->>CLI: now only female owners
    CLI->>Agent: ask(follow-up)
    Agent->>LLM: system prompt + previous Q/SQL/result + follow-up
    LLM-->>Agent: submit_answer(sql with a typo: c.sex = 'F')
    Agent->>Tools: call_tool("submit_answer")
    Tools->>DB: execute_sql
    DB-->>Tools: error: no such column c.sex
    Tools-->>Agent: "Your query failed, fix it..."
    Note over Agent,LLM: Self-repair (illustrative typo): the error goes back as a tool result
    Agent->>LLM: messages + tool error
    LLM-->>Agent: get_column_values(client, gender)
    Agent->>Tools: call_tool("get_column_values")
    Tools->>DB: SELECT gender, COUNT(*) ... GROUP BY gender
    DB-->>Agent: F | 2645, M | 2724
    Agent->>LLM: messages + values
    LLM-->>Agent: submit_answer(sql with c.gender = 'F')
    Agent->>Tools: call_tool("submit_answer")
    Tools->>DB: execute_sql
    DB-->>Agent: rows
    Agent-->>CLI: AgentResult (3 LLM calls)
    CLI-->>User: corrected SQL, table: 22
```

## Where to extend it (Stage 2)

* **New tool:** write `handler(ctx, **args) -> ToolOutput` and add a `Tool(...)` entry in
  `build_tools()` ([src/tools.py](../src/tools.py)). Only `terminal=True` tools can end the turn, and
  they do it by returning `final_result` (an accepted query) or `final_message` (a reply such as a
  clarifying question). The loop doesn't change.
* **Routing:** `_call_llm` is the single place where model choice happens. A router (a cheap model for easy
  questions, a stronger one for hard ones) goes there.
* **Load testing:** the `AgentResult` timings (`latency_s`, `llm_s`, `llm_calls`, tokens) are what
  a concurrent load harness would aggregate. `src/evaluate.py` already reports P50/P95.

## 3. Finance assistant: guardrail and two sources

Added after the take-home. A guardrail call runs before the loop above, and two web tools join the
same tool list. The loop itself is unchanged apart from the time budget and treating a plain-text
reply after a search as the answer. (The PNG/SVG exports above predate this section.)

```mermaid
flowchart TD
    Q(["Question"]) --> G{"Guardrail LLM call<br/>FINANCE or OTHER?<br/>sees previous question"}
    G -- "OTHER" --> R["Refusal message<br/>no tools, not remembered"]
    G -- "FINANCE (or classifier failed)" --> L["Agent loop<br/>system prompt has today's date"]
    L --> T{"Model picks a tool"}
    T -- "bank data" --> S["run_sql / get_column_values<br/>→ submit_answer(sql)"]
    T -- "markets, general finance" --> W["web_search (ddgs)<br/>≤ max_searches per question"]
    W --> F["final_answer(answer, sources)<br/>or plain-text answer"]
    S --> D(["route = sql: SQL + table"])
    F --> E(["route = web: answer + sources"])
    L -. "step budget · time budget · LLM timeout" .-> X(["Stopped with error"])
```
