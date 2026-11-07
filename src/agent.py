"""The text-to-SQL agent: a small, explicit tool-calling loop on a Fireworks-hosted open model.

One question flows like this:

    user question
      -> LLM (system prompt with the full grounded schema + recent conversation + question)
      -> tool calls: run_sql / get_column_values (optional exploration)
      -> submit_answer(sql)  -- executes the query; errors go back to the LLM to repair
      -> AgentResult (sql, rows, assumptions, timings, token usage)

Happy path is ONE LLM call (the model submits immediately), which is what keeps P50 latency low.
The loop stops when: submit_answer succeeds, the model replies in plain text (e.g. a clarifying
question), or the step budget (settings.max_steps) runs out.
"""

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI

from src.config import Settings
from src.db import QueryResult, connect_readonly
from src.prompts import FINAL_STEP_NUDGE, SYSTEM_PROMPT
from src.schema import build_schema_context
from src.tools import Tool, ToolContext, build_tools, call_tool


@dataclass
class Step:
    """One entry of the agent trace (shown with /trace in the CLI, saved by the eval)."""

    kind: str  # "llm", "tool"
    detail: str
    elapsed_s: float


@dataclass
class AgentResult:
    question: str
    sql: str | None = None
    result: QueryResult | None = None
    assumptions: str = ""
    message: str = ""  # plain-text reply (clarifying question, refusal, or failure reason)
    steps: list[Step] = field(default_factory=list)
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0
    llm_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.result is not None and self.result.ok

    def answer_text(self) -> str:
        """Short human-readable summary of the result (used for dev_answers.json)."""
        if not self.ok:
            return self.message or "No answer."
        return summarize_result(self.result)


def summarize_result(result: QueryResult, max_rows: int = 5) -> str:
    rows, cols = result.rows, result.columns
    if not rows:
        return "No rows returned."
    if len(rows) == 1 and len(cols) == 1:
        return f"{cols[0]} = {_fmt_value(rows[0][0])}"
    shown = "; ".join(
        ", ".join(f"{c}={_fmt_value(v)}" for c, v in zip(cols, row)) for row in rows[:max_rows]
    )
    if len(rows) == 1:
        return shown
    more = f" (showing first {max_rows})" if len(rows) > max_rows else ""
    return f"{len(rows)}{'+' if result.truncated else ''} rows{more}: {shown}"


def _fmt_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    return "NULL" if value is None else str(value)


_SQL_IN_TEXT = re.compile(r"```(?:sql)?\s*((?:SELECT|WITH)\b.*?)```", re.DOTALL | re.IGNORECASE)


class SQLAgent:
    def __init__(
        self,
        settings: Settings | None = None,
        client: OpenAI | None = None,
        conn: sqlite3.Connection | None = None,
        tools: dict[str, Tool] | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.conn = conn or connect_readonly(self.settings.db_path)
        self.client = client or OpenAI(
            api_key=self.settings.api_key, base_url=self.settings.base_url, timeout=60, max_retries=2
        )
        self.tools = tools or build_tools()
        # Built once per session. A stable system prompt also lets the server reuse its prompt cache.
        self.schema_text = build_schema_context(self.conn, self.settings.description_dir)
        self.system_prompt = SYSTEM_PROMPT.format(schema=self.schema_text)
        self.history: list[dict[str, Any]] = []  # compact Q/A pairs for follow-up questions

    def reset(self) -> None:
        self.history.clear()

    # -- main loop -------------------------------------------------------------------------------

    def ask(self, question: str) -> AgentResult:
        out = AgentResult(question=question)
        start = time.perf_counter()
        ctx = ToolContext(conn=self.conn, settings=self.settings)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            *self.history,
            {"role": "user", "content": question},
        ]

        for step in range(self.settings.max_steps):
            if step == self.settings.max_steps - 1:
                messages.append({"role": "user", "content": FINAL_STEP_NUDGE})
            message = self._call_llm(messages, out)
            if message is None:
                break

            if not message.tool_calls:
                # The model answered in prose. Some models put SQL in a code block instead of
                # calling the tool; treat that as a submit so the user still gets results.
                text = (message.content or "").strip()
                sql_match = _SQL_IN_TEXT.search(text)
                if sql_match:
                    messages.append({"role": "assistant", "content": text})
                    output = call_tool(self.tools, ctx, "submit_answer", _json_sql(sql_match.group(1)))
                    if self._accept(output, out):
                        break
                    messages.append({"role": "user", "content": output.content})
                    continue
                out.message = text or "The model returned an empty response."
                break

            messages.append(_assistant_message(message))
            for tool_call in message.tool_calls:
                t0 = time.perf_counter()
                output = call_tool(self.tools, ctx, tool_call.function.name, tool_call.function.arguments)
                out.steps.append(
                    Step("tool", f"{tool_call.function.name}({tool_call.function.arguments})", time.perf_counter() - t0)
                )
                messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": output.content})
                self._accept(output, out)
            if out.result is not None:
                break
        else:
            out.message = f"Stopped after {self.settings.max_steps} steps without a final answer."

        out.latency_s = time.perf_counter() - start
        self._remember(out)
        return out

    # -- helpers ---------------------------------------------------------------------------------

    def _call_llm(self, messages: list[dict[str, Any]], out: AgentResult):
        kwargs: dict[str, Any] = dict(
            model=self.settings.model,
            messages=messages,
            tools=[tool.spec() for tool in self.tools.values()],
            tool_choice="auto",
            temperature=self.settings.temperature,
            max_tokens=self.settings.max_tokens,
        )
        if self.settings.reasoning_effort:
            kwargs["reasoning_effort"] = self.settings.reasoning_effort
        t0 = time.perf_counter()
        try:
            response = self.client.chat.completions.create(**kwargs)
        except Exception as exc:  # network, auth, rate limit: report instead of crashing the CLI
            out.message = f"LLM request failed: {type(exc).__name__}: {exc}"
            return None
        elapsed = time.perf_counter() - t0
        out.llm_calls += 1
        out.llm_s += elapsed
        if response.usage:
            out.prompt_tokens += response.usage.prompt_tokens or 0
            out.completion_tokens += response.usage.completion_tokens or 0
        message = response.choices[0].message
        names = [tc.function.name for tc in message.tool_calls or []]
        out.steps.append(Step("llm", f"-> {', '.join(names) or 'text reply'}", elapsed))
        return message

    @staticmethod
    def _accept(output, out: AgentResult) -> bool:
        if output.final_result is None:
            return False
        out.result = output.final_result
        out.sql = output.final_result.sql
        out.assumptions = output.assumptions
        return True

    def _remember(self, out: AgentResult) -> None:
        """Keep a compact record of the turn (not the whole tool trace) for follow-up questions."""
        if out.ok:
            reply = f"SQL:\n{out.sql}\nResult:\n{out.result.preview(5)}"
        else:
            reply = out.message
        self.history += [
            {"role": "user", "content": out.question},
            {"role": "assistant", "content": reply},
        ]
        self.history = self.history[-2 * self.settings.history_turns :]


def _assistant_message(message) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": message.content or "",
        "tool_calls": [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in message.tool_calls
        ],
    }


def _json_sql(sql: str) -> str:
    return json.dumps({"sql": sql})
