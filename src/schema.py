"""Builds the schema context that grounds the model.

Hallucinated tables/columns and wrong filter values were the customer's main quality problem, so
the model gets, for every table:
  * real column names and types, primary and foreign keys (read from the database itself),
  * the human documentation from the description CSVs (when the customer has them),
  * actual values for low-cardinality text columns, e.g. region names or status codes.

The last point matters a lot: the docs say disp.type can be "USER", but the data only contains
OWNER and DISPONENT; the question says "Prague region" and the value lives in district.A3.

This works for any SQLite file. Descriptions are optional, so "point it at a database and go"
still works, just with less help. For a schema with hundreds of tables the same text would be
chunked per table and retrieved with embeddings instead of sent in full (see README).
"""

import csv
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

# Text columns with at most this many distinct values get all of their values listed.
MAX_ENUM_VALUES = 25
SAMPLE_VALUES = 3


@dataclass
class Column:
    name: str
    type: str
    is_pk: bool = False
    description: str = ""
    values: list[str] = field(default_factory=list)  # every distinct value (enum-like columns)
    examples: list[str] = field(default_factory=list)  # a few sample values (other columns)


@dataclass
class Table:
    name: str
    row_count: int
    columns: list[Column]
    foreign_keys: list[str]  # "column -> other_table.other_column"


# Table names that are SQL keywords must be quoted in queries (the sample DB has a table "order").
RESERVED_WORDS = {"order", "group", "select", "table", "where", "from", "index", "user", "limit"}


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _display_name(identifier: str) -> str:
    return _quote(identifier) if identifier.lower() in RESERVED_WORDS else identifier


def load_descriptions(description_dir: str | None) -> dict[str, dict[str, str]]:
    """{table: {column: "readable name: description. values"}} from the BIRD-style CSVs."""
    if not description_dir or not Path(description_dir).is_dir():
        return {}
    result: dict[str, dict[str, str]] = {}
    for csv_path in sorted(Path(description_dir).glob("*.csv")):
        with csv_path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
            for row in csv.DictReader(handle):
                column = (row.get("original_column_name") or "").strip()
                if not column:
                    continue
                parts = []
                readable = " ".join((row.get("column_name") or "").split())
                desc = " ".join((row.get("column_description") or "").split())
                values = " ".join((row.get("value_description") or "").split())
                if readable and readable.lower() != column.lower():
                    parts.append(readable)
                if desc and desc.lower() != readable.lower():
                    parts.append(desc)
                if values:
                    parts.append(f"values: {values}")
                result.setdefault(csv_path.stem, {})[column] = "; ".join(parts)
    return result


def introspect(conn: sqlite3.Connection, description_dir: str | None = None) -> list[Table]:
    docs = load_descriptions(description_dir)
    table_names = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    tables = []
    for name in table_names:
        q = _quote(name)
        row_count = conn.execute(f"SELECT COUNT(*) FROM {q}").fetchone()[0]
        foreign_keys = [
            f"{fk[3]} -> {fk[2]}.{fk[4]}" for fk in conn.execute(f"PRAGMA foreign_key_list({q})")
        ]
        columns = []
        for _cid, col_name, col_type, _notnull, _default, pk in conn.execute(f"PRAGMA table_info({q})"):
            column = Column(
                name=col_name,
                type=col_type or "",
                is_pk=bool(pk),
                description=docs.get(name, {}).get(col_name, ""),
            )
            _attach_values(conn, q, column)
            columns.append(column)
        tables.append(Table(name, row_count, columns, foreign_keys))
    return tables


def _attach_values(conn: sqlite3.Connection, table_sql: str, column: Column) -> None:
    col = _quote(column.name)
    if "TEXT" in column.type.upper() or "CHAR" in column.type.upper():
        # LIMIT n+1 lets us tell "small enum" from "free text" without a full DISTINCT count.
        values = [
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT {col} FROM {table_sql} WHERE {col} IS NOT NULL LIMIT {MAX_ENUM_VALUES + 1}"
            )
        ]
        if len(values) <= MAX_ENUM_VALUES:
            column.values = [str(v) for v in values]
            return
    sample = conn.execute(
        f"SELECT DISTINCT {col} FROM {table_sql} WHERE {col} IS NOT NULL LIMIT {SAMPLE_VALUES}"
    )
    column.examples = [str(r[0]) for r in sample]


def render_schema(tables: list[Table]) -> str:
    """Render tables as compact, commented DDL-like text, the format models read best."""
    blocks = []
    for table in tables:
        lines = [f"TABLE {_display_name(table.name)}  -- {table.row_count:,} rows"]
        for c in table.columns:
            notes = []
            if c.is_pk:
                notes.append("PRIMARY KEY")
            if c.description:
                notes.append(c.description)
            if c.values:
                notes.append("ALL VALUES: " + ", ".join(repr(v) for v in c.values))
            elif c.examples:
                notes.append("e.g. " + ", ".join(repr(v) for v in c.examples))
            lines.append(f"  {c.name} {c.type}" + (f"  -- {' | '.join(notes)}" if notes else ""))
        for fk in table.foreign_keys:
            lines.append(f"  FOREIGN KEY {fk}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def build_schema_context(conn: sqlite3.Connection, description_dir: str | None = None) -> str:
    return render_schema(introspect(conn, description_dir))
