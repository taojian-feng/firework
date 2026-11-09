"""Drill 5 (15 min): a glossary tool over the column documentation.

Why: business words ("in debt", "region", "owner") don't match cryptic column names.

Spec:
- Tool search_glossary(term) searches the description CSVs (settings.description_dir) for the term,
  case-insensitive, in column names, descriptions and value descriptions.
- Each hit is one line "table.column: description | value description".
- No hits returns a message saying so.
- Discuss: when should the model pick this over get_column_values? (Meaning vs. spelling.)
"""

import json

from src.config import Settings
from src.tools import ToolContext, build_tools, call_tool


def _search(conn, term):
    ctx = ToolContext(conn=conn, settings=Settings(api_key="test"))
    return call_tool(build_tools(), ctx, "search_glossary", json.dumps({"term": term})).content


def test_finds_cryptic_column(conn):
    assert "district.A3" in _search(conn, "REGION")


def test_finds_value_meaning(conn):
    assert "loan.status" in _search(conn, "client in debt")


def test_no_hits(conn):
    assert "district" not in _search(conn, "zzzz-not-a-term")
