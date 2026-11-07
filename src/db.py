"""Safe, read-only SQL execution.

Model-generated SQL is untrusted input, so it goes through three layers before touching data:
  1. a cheap static check (one statement, SELECT/WITH only),
  2. a read-only SQLite connection (mode=ro + PRAGMA query_only), which is the real guarantee,
  3. a row cap and a wall-clock timeout so a bad cross join cannot hang the CLI.
"""

import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class UnsafeSQLError(ValueError):
    """Raised when a query is rejected before it reaches the database."""


@dataclass
class QueryResult:
    sql: str
    columns: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    error: str | None = None
    truncated: bool = False
    elapsed_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None

    def as_records(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row)) for row in self.rows]

    def preview(self, max_rows: int = 20) -> str:
        """Compact text rendering that is sent back to the model as a tool result."""
        if self.error:
            return f"ERROR: {self.error}"
        lines = [" | ".join(self.columns)]
        lines += [" | ".join(_fmt(v) for v in row) for row in self.rows[:max_rows]]
        shown = min(len(self.rows), max_rows)
        more = "+" if self.truncated else ""
        lines.append(f"({len(self.rows)}{more} rows total, {shown} shown)")
        return "\n".join(lines)


def _fmt(value: Any) -> str:
    return "NULL" if value is None else str(value)


_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)


def normalize_sql(sql: str) -> str:
    """Strip markdown fences, comments, whitespace and a trailing semicolon."""
    sql = sql.strip()
    fence = re.search(r"```(?:sql)?\s*(.*?)```", sql, re.DOTALL | re.IGNORECASE)
    if fence:
        sql = fence.group(1)
    sql = _COMMENT_RE.sub(" ", sql).strip()
    return sql.rstrip(";").strip()


def check_sql_is_safe(sql: str) -> str:
    """Return the normalized SQL, or raise UnsafeSQLError."""
    cleaned = normalize_sql(sql)
    if not cleaned:
        raise UnsafeSQLError("Empty query.")
    # A ';' left after removing the trailing one means several statements. This is
    # conservative (a ';' inside a string literal is also rejected), which is fine here.
    if ";" in cleaned:
        raise UnsafeSQLError("Only a single SQL statement is allowed.")
    first_word = cleaned.split(None, 1)[0].upper()
    if first_word not in {"SELECT", "WITH"}:
        raise UnsafeSQLError(f"Only read-only SELECT/WITH queries are allowed (got {first_word}).")
    return cleaned


def connect_readonly(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}. Run ./setup.sh first.")
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, check_same_thread=False)
    conn.execute("PRAGMA query_only = ON")
    return conn


def execute_sql(
    conn: sqlite3.Connection,
    sql: str,
    max_rows: int = 1000,
    timeout_s: float = 10.0,
) -> QueryResult:
    """Run one read-only query. Never raises for SQL problems: errors come back in QueryResult.error
    so the agent can show them to the model and let it repair the query."""
    start = time.perf_counter()
    try:
        cleaned = check_sql_is_safe(sql)
    except UnsafeSQLError as exc:
        return QueryResult(sql=sql, error=str(exc))

    deadline = start + timeout_s
    # SQLite calls this every N VM instructions; returning non-zero aborts the query.
    conn.set_progress_handler(lambda: int(time.perf_counter() > deadline), 10_000)
    try:
        cursor = conn.execute(cleaned)
        columns = [d[0] for d in cursor.description or []]
        rows = cursor.fetchmany(max_rows + 1)
        truncated = len(rows) > max_rows
        return QueryResult(
            sql=cleaned,
            columns=columns,
            rows=[tuple(r) for r in rows[:max_rows]],
            truncated=truncated,
            elapsed_s=time.perf_counter() - start,
        )
    except sqlite3.OperationalError as exc:
        message = str(exc)
        if message == "interrupted":
            message = f"Query exceeded the {timeout_s:.0f}s timeout. Add filters or aggregate earlier."
        return QueryResult(sql=cleaned, error=message, elapsed_s=time.perf_counter() - start)
    except sqlite3.Error as exc:
        return QueryResult(sql=cleaned, error=str(exc), elapsed_s=time.perf_counter() - start)
    finally:
        conn.set_progress_handler(None, 0)
