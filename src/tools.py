"""Tools the agent can call, and the registry that wires them into the agent loop.

Adding a tool = write a handler function + add a Tool(...) entry to build_tools(). The agent
loop does not need to change.

Stopping: only a tool marked terminal=True can end the turn, and only when its output says so
(ToolOutput.ends_turn): either an accepted SQL result (final_result) or a reply for the user
(final_message, e.g. a clarifying question). A terminal tool can also return neither, as
submit_answer does when the query fails, and the loop continues so the model can repair it.
"""

import json
import sqlite3
from dataclasses import dataclass
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


@dataclass
class ToolOutput:
    content: str  # what the model sees as the tool result
    final_result: QueryResult | None = None  # set only when a terminal tool accepted an answer
    assumptions: str = ""
    final_message: str | None = None  # a terminal tool ending the turn with a reply instead of SQL

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


# ---------------------------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------------------------


def build_tools() -> dict[str, Tool]:
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
