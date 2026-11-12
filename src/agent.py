"""The finance assistant: a small, explicit tool-calling loop on a Fireworks-hosted open model.

One question flows like this:

    user question
      -> guardrail: one small LLM call, FINANCE or OTHER. OTHER -> polite refusal, no tools run.
      -> LLM (system prompt with today's date + the full grounded schema + recent conversation)
      -> bank data:   run_sql / get_column_values (optional) -> submit_answer(sql)
         market data: get_quote (prices) / web_search (capped) -> final_answer(answer, sources)
      -> AgentResult (route, sql + rows or answer + sources, timings, token usage)

Happy path for a database question is guardrail + ONE agent call. The loop stops when:
  1. a terminal tool ends the turn (submit_answer succeeds, or final_answer),
  2. the model replies in plain text (a clarifying question, or an answer after searching),
  3. the step budget (settings.max_steps) runs out,
  4. the time budget (settings.max_seconds) runs out, checked before each LLM call.
Each LLM request also has its own timeout, and web_search refuses after settings.max_searches.
"""

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from openai import OpenAI

from src.config import Settings
from src.db import QueryResult, connect_readonly
from src.prompts import FINAL_STEP_NUDGE, GUARDRAIL_PROMPT, REFUSAL_MESSAGE, SYSTEM_PROMPT, TEXT_REPLY_NUDGE
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
    message: str = ""  # plain-text reply (web answer, clarifying question, refusal, or failure reason)
    route: str = ""  # "sql", "web", "refused", or "text" (clarifying question / no answer)
    sources: list[str] = field(default_factory=list)  # URLs behind a web answer
    error: str = ""  # set when the turn failed: LLM error, step budget or time budget
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
        search_fn=None,
        quote_fn=None,
        today: date | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.conn = conn or connect_readonly(self.settings.db_path)
        self.client = client or OpenAI(
            api_key=self.settings.api_key,
            base_url=self.settings.base_url,
            timeout=self.settings.llm_timeout_s,
            max_retries=2,
        )
        self.tools = tools or build_tools(web=self.settings.web_search)
        self.search_fn = search_fn  # None = real web search (ddgs); tests pass a fake
        self.quote_fn = quote_fn  # None = real quotes (yfinance); tests pass a fake
        # Built once per session. A stable system prompt also lets the server reuse its prompt cache.
        # Today's date is in it so the model searches for current data, not its training cutoff.
        self.schema_text = build_schema_context(self.conn, self.settings.description_dir)
        self.system_prompt = SYSTEM_PROMPT.format(
            schema=self.schema_text,
            today=(today or date.today()).strftime("%A %d %B %Y"),
            max_searches=self.settings.max_searches,
        )
        if not self.settings.web_search:
            self.system_prompt += "\nWeb search is turned off: answer only from the database, and say so for market questions.\n"
        self.history: list[dict[str, Any]] = []  # compact Q/A pairs for follow-up questions

    def reset(self) -> None:
        self.history.clear()

    # -- main loop -------------------------------------------------------------------------------

    def ask(self, question: str) -> AgentResult:
        out = AgentResult(question=question)
        start = time.perf_counter()
        if self.settings.guardrail and not self._is_finance(question, out):
            out.route, out.message = "refused", REFUSAL_MESSAGE
            out.latency_s = time.perf_counter() - start
            return out  # not added to history: an off-topic turn shouldn't steer follow-ups
        ctx = ToolContext(conn=self.conn, settings=self.settings, search_fn=self.search_fn, quote_fn=self.quote_fn)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            *self.history,
            {"role": "user", "content": question},
        ]

        text_nudged = False
        for step in range(self.settings.max_steps):
            if step > 0 and time.perf_counter() - start > self.settings.max_seconds:
                out.error = out.message = (
                    f"Stopped: the {self.settings.max_seconds:.0f}s time budget ran out before a final answer."
                )
                break
            if step == self.settings.max_steps - 1:
                messages.append({"role": "user", "content": FINAL_STEP_NUDGE})
            message = self._call_llm(messages, out)
            if message is None:
                break

            if not message.tool_calls:
                # The model answered in prose. Some models put SQL in a code block instead of
                # calling the tool; treat that as a submit so the user still gets results.
                text = (message.content or "").strip()
                if (ctx.searches or ctx.quotes) and text:
                    # Answered from web results in prose instead of calling final_answer: accept it.
                    out.message, out.sources = text, ctx.sources[:3]
                    break
                sql_match = _SQL_IN_TEXT.search(text)
                if sql_match:
                    messages.append({"role": "assistant", "content": text})
                    output = call_tool(self.tools, ctx, "submit_answer", _json_sql(sql_match.group(1)))
                    if self._accept(output, out):
                        break
                    messages.append({"role": "user", "content": output.content})
                    continue
                if ctx.last_ok_result is not None:
                    # The model explored with run_sql, got rows, then answered in prose instead of
                    # calling submit_answer (seen with gpt-oss-120b). Use that query's result.
                    out.result, out.sql = ctx.last_ok_result, ctx.last_ok_result.sql
                    out.assumptions = text[:300]
                    out.steps.append(Step("fallback", "used last successful run_sql result", 0.0))
                    break
                if not text_nudged and step < self.settings.max_steps - 1:
                    # No SQL at all, e.g. the model repeated a result from the conversation history.
                    # Ask once for submit_answer; a genuine clarifying question comes back as text.
                    text_nudged = True
                    messages += [{"role": "assistant", "content": text}, {"role": "user", "content": TEXT_REPLY_NUDGE}]
                    out.steps.append(Step("nudge", "plain-text reply: asked for submit_answer", 0.0))
                    continue
                out.message = text or "The model returned an empty response."
                break

            messages.append(_assistant_message(message))
            done = False
            for tool_call in message.tool_calls:
                name = tool_call.function.name
                t0 = time.perf_counter()
                output = call_tool(self.tools, ctx, name, tool_call.function.arguments)
                out.steps.append(Step("tool", f"{name}({tool_call.function.arguments})", time.perf_counter() - t0))
                messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": output.content})
                # Only a terminal tool can end the turn; the first one that does wins.
                tool = self.tools.get(name)
                if not done and tool is not None and tool.terminal:
                    done = self._accept(output, out)
            if done:
                break
        else:
            out.error = out.message = f"Stopped after {self.settings.max_steps} steps without a final answer."

        if out.ok:
            out.route = "sql"
        elif (ctx.searches or ctx.quotes) and not out.error:
            out.route = "web"
        else:
            out.route = "text"
        out.latency_s = time.perf_counter() - start
        self._remember(out)
        return out

    # -- helpers ---------------------------------------------------------------------------------

    def _is_finance(self, question: str, out: AgentResult) -> bool:
        """Guardrail: one short classifier call. Fails open (the system prompt also refuses)."""
        previous = next((m["content"] for m in reversed(self.history) if m["role"] == "user"), "(none)")
        kwargs: dict[str, Any] = dict(
            model=self.settings.guard_model or self.settings.model,
            messages=[{"role": "user", "content": GUARDRAIL_PROMPT.format(previous=previous, question=question)}],
            temperature=0.0,
            max_tokens=self.settings.max_tokens,
        )
        if self.settings.reasoning_effort:
            kwargs["reasoning_effort"] = self.settings.reasoning_effort
        t0 = time.perf_counter()
        try:
            response = self.client.chat.completions.create(**kwargs)
        except Exception as exc:
            out.steps.append(Step("guard", f"classifier failed, allowing: {type(exc).__name__}", time.perf_counter() - t0))
            return True
        self._record_usage(out, response, time.perf_counter() - t0)
        words = re.findall(r"\b(FINANCE|OTHER)\b", (response.choices[0].message.content or "").upper())
        verdict = words[-1] if words else "FINANCE"  # unparseable -> allow
        out.steps.append(Step("guard", verdict, time.perf_counter() - t0))
        return verdict == "FINANCE"

    @staticmethod
    def _record_usage(out: AgentResult, response, elapsed: float) -> None:
        out.llm_calls += 1
        out.llm_s += elapsed
        if response.usage:
            out.prompt_tokens += response.usage.prompt_tokens or 0
            out.completion_tokens += response.usage.completion_tokens or 0

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
        except Exception as exc:  # network, auth, rate limit, timeout: report instead of crashing
            out.error = out.message = f"LLM request failed: {type(exc).__name__}: {exc}"
            return None
        elapsed = time.perf_counter() - t0
        self._record_usage(out, response, elapsed)
        message = response.choices[0].message
        names = [tc.function.name for tc in message.tool_calls or []]
        out.steps.append(Step("llm", f"-> {', '.join(names) or 'text reply'}", elapsed))
        return message

    @staticmethod
    def _accept(output, out: AgentResult) -> bool:
        """Record a terminal tool's output on the result. Returns True if it ends the turn."""
        if not output.ends_turn:
            return False
        if output.final_result is not None:
            out.result = output.final_result
            out.sql = output.final_result.sql
            out.assumptions = output.assumptions
        else:
            out.message = output.final_message
            out.sources = output.sources
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
