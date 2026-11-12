"""Tools the agent can call, and the registry that wires them into the agent loop.

Adding a tool = write a handler function + add a Tool(...) entry to build_tools(). The agent
loop does not need to change.

Stopping: only a tool marked terminal=True can end the turn, and only when its output says so
(ToolOutput.ends_turn): either an accepted SQL result (final_result) or a reply for the user
(final_message, e.g. a clarifying question). A terminal tool can also return neither, as
submit_answer does when the query fails, and the loop continues so the model can repair it.

Two sources, one loop: submit_answer / run_sql / get_column_values answer from the bank's database,
web_search + final_answer answer market and general finance questions. The tool descriptions (and
one rule in the system prompt) are the router; the loop does not know which source is which.
"""

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable

from src.config import Settings
from src.db import QueryResult, execute_sql


@dataclass
class ToolContext:
    """What a tool handler can touch. Also carries small bits of per-question state."""

    conn: sqlite3.Connection
    settings: Settings
    empty_result_warned: bool = False
    # Last successful exploratory query, used if the model forgets to call submit_answer.
    last_ok_result: QueryResult | None = None
    # Web search: the search function (swapped for a fake in tests), searches used, URLs seen.
    search_fn: Callable[..., list[dict]] | None = None
    searches: int = 0
    sources: list[str] = field(default_factory=list)


@dataclass
class ToolOutput:
    content: str  # what the model sees as the tool result
    final_result: QueryResult | None = None  # set only when a terminal tool accepted an answer
    assumptions: str = ""
    final_message: str | None = None  # a terminal tool ending the turn with a reply instead of SQL
    sources: list[str] = field(default_factory=list)  # URLs behind a final_message from the web

    @property
    def ends_turn(self) -> bool:
        return self.final_result is not None or self.final_message is not None


Handler = Callable[..., ToolOutput]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema
    handler: Handler
    terminal: bool = False

    def spec(self) -> dict[str, Any]:
        """OpenAI-compatible function-calling spec, which Fireworks accepts as-is."""
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


# ---------------------------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------------------------


def run_sql(ctx: ToolContext, sql: str) -> ToolOutput:
    result = execute_sql(ctx.conn, sql, ctx.settings.max_rows, ctx.settings.query_timeout_s)
    if result.ok and result.rows:
        ctx.last_ok_result = result
    return ToolOutput(result.preview(ctx.settings.preview_rows))


def get_column_values(ctx: ToolContext, table: str, column: str, contains: str = "") -> ToolOutput:
    """Distinct values with counts, so the model can pick an exact filter value."""
    table_q = '"' + table.replace('"', '""') + '"'
    column_q = '"' + column.replace('"', '""') + '"'
    sql = f"SELECT {column_q} AS value, COUNT(*) AS n FROM {table_q}"
    if contains:
        escaped = contains.replace("'", "''")
        sql += f" WHERE CAST({column_q} AS TEXT) LIKE '%{escaped}%'"
    sql += f" GROUP BY {column_q} ORDER BY n DESC LIMIT 50"
    result = execute_sql(ctx.conn, sql, ctx.settings.max_rows, ctx.settings.query_timeout_s)
    return ToolOutput(result.preview(50))


def submit_answer(ctx: ToolContext, sql: str, assumptions: str = "") -> ToolOutput:
    """Execute the final query. Errors (and a first empty result) go back to the model to fix."""
    result = execute_sql(ctx.conn, sql, ctx.settings.max_rows, ctx.settings.query_timeout_s)
    if not result.ok:
        return ToolOutput(f"Your query failed, fix it and call submit_answer again.\n{result.preview()}")
    if not result.rows and not ctx.empty_result_warned:
        # Empty results are the classic symptom of a wrong filter value. Ask once, then accept.
        ctx.empty_result_warned = True
        return ToolOutput(
            "The query returned 0 rows. Double-check filter values (get_column_values) and join "
            "conditions. If 0 rows is really correct, call submit_answer again with the same query."
        )
    return ToolOutput(result.preview(ctx.settings.preview_rows), final_result=result, assumptions=assumptions)


def ddgs_search(query: str, max_results: int, timeout_s: int, timelimit: str | None = None) -> list[dict]:
    """DuckDuckGo (and other engines) through the ddgs package. Returns [{title, href, body}]."""
    from ddgs import DDGS  # imported here so the SQL-only path never needs it

    return DDGS(timeout=timeout_s).text(query, max_results=max_results, timelimit=timelimit)


def web_search(ctx: ToolContext, query: str, timelimit: str = "") -> ToolOutput:
    """Search the web. Capped per question; failures come back as text so the model can carry on."""
    cap = ctx.settings.max_searches
    if ctx.searches >= cap:
        return ToolOutput(f"Search limit reached ({cap} per question). Call final_answer now with what you found.")
    ctx.searches += 1
    search = ctx.search_fn or ddgs_search
    try:
        results = search(query, ctx.settings.search_results, ctx.settings.search_timeout_s, timelimit or None)
    except Exception as exc:  # network error, rate limit, timeout: tell the model, don't crash
        return ToolOutput(f"Web search failed ({type(exc).__name__}: {exc}). Try a different query or answer.")
    if not results:
        return ToolOutput("No results. Try a broader query.")
    lines = []
    for i, r in enumerate(results, 1):
        url = r.get("href") or r.get("url") or ""
        if url and url not in ctx.sources:
            ctx.sources.append(url)
        lines.append(f"[{i}] {r.get('title', '')}\n{url}\n{(r.get('body') or '')[:400]}")
    return ToolOutput("\n\n".join(lines))


def final_answer(ctx: ToolContext, answer: str, sources: list[str] | None = None) -> ToolOutput:
    """End the turn with a written answer (used for web answers)."""
    if not answer.strip():
        return ToolOutput("The answer was empty. Call final_answer again with the answer text.")
    return ToolOutput("Answer delivered.", final_message=answer.strip(), sources=list(sources or ctx.sources[:3]))


# ---------------------------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------------------------


def build_tools(web: bool = True) -> dict[str, Tool]:
    tools = [
        Tool(
            name="submit_answer",
            description=(
                "Run the FINAL SQLite query that answers the user's question and show it to the user. "
                "Call this as soon as you are confident."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "A single read-only SQLite SELECT query."},
                    "assumptions": {
                        "type": "string",
                        "description": "One short sentence on any interpretation you made. Empty if none.",
                    },
                },
                "required": ["sql"],
            },
            handler=submit_answer,
            terminal=True,
        ),
        Tool(
            name="run_sql",
            description=(
                "Run an exploratory read-only SQLite query and see up to 20 rows. Use it to check data "
                "before answering. It is NOT shown to the user: you must still call submit_answer at the end."
            ),
            parameters={
                "type": "object",
                "properties": {"sql": {"type": "string"}},
                "required": ["sql"],
            },
            handler=run_sql,
        ),
        Tool(
            name="get_column_values",
            description="List the distinct values (with counts) of a column, optionally filtered by a substring.",
            parameters={
                "type": "object",
                "properties": {
                    "table": {"type": "string"},
                    "column": {"type": "string"},
                    "contains": {"type": "string", "description": "Optional case-insensitive substring filter."},
                },
                "required": ["table", "column"],
            },
            handler=get_column_values,
        ),
    ]
    if web:
        tools += [
            Tool(
                name="web_search",
                description=(
                    "Search the web for market and general finance information that is NOT in the bank's "
                    "database: stock prices, company earnings, interest rates, the economy, how financial "
                    "products work. Never use it for the bank's own clients, accounts, loans or cards. "
                    "Include the current year in time-sensitive queries."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "A short web search query."},
                        "timelimit": {
                            "type": "string",
                            "enum": ["d", "w", "m", "y"],
                            "description": "Optional: only results from the last day, week, month or year.",
                        },
                    },
                    "required": ["query"],
                },
                handler=web_search,
            ),
            Tool(
                name="final_answer",
                description=(
                    "Give the user your FINAL written answer to a market or general finance question, "
                    "based on web_search results. Not for database questions (use submit_answer)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "answer": {"type": "string", "description": "A short answer, with how recent the data is."},
                        "sources": {"type": "array", "items": {"type": "string"}, "description": "URLs used."},
                    },
                    "required": ["answer"],
                },
                handler=final_answer,
                terminal=True,
            ),
        ]
    return {tool.name: tool for tool in tools}


def call_tool(tools: dict[str, Tool], ctx: ToolContext, name: str, raw_arguments: str) -> ToolOutput:
    """Dispatch one tool call. Bad tool names or malformed JSON become messages the model can fix."""
    tool = tools.get(name)
    if tool is None:
        return ToolOutput(f"Unknown tool '{name}'. Available tools: {', '.join(tools)}.")
    try:
        arguments = json.loads(raw_arguments or "{}")
    except json.JSONDecodeError as exc:
        return ToolOutput(f"Arguments were not valid JSON ({exc}). Call {name} again with valid JSON.")
    try:
        return tool.handler(ctx, **arguments)
    except TypeError as exc:  # missing / unexpected arguments
        return ToolOutput(f"Bad arguments for {name}: {exc}")
