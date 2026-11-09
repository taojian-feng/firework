"""Drill 3 (15 min): fetch the schema on demand instead of putting all of it in the prompt.

Why: GitLab's warehouse has hundreds of tables; the full schema won't fit in every prompt.

Spec:
- Settings has schema_mode: "full" (default, today's behaviour) or "names"
  (env T2SQL_SCHEMA_MODE). In "names" mode the system prompt lists table names (with row counts)
  but no column details.
- A tool get_table_schema(table) returns the full rendered block for one table (columns, docs,
  ALL VALUES, foreign keys). An unknown table returns a message listing the real table names.
- Afterwards: run `uv run eval` in both modes and compare prompt tokens and accuracy.
"""

import json

from src.agent import SQLAgent
from src.config import Settings
from src.tools import ToolContext, build_tools, call_tool
from tests.conftest import FakeClient


def test_names_mode_prompt_has_no_column_details(conn):
    agent = SQLAgent(Settings(api_key="test", schema_mode="names"), client=FakeClient([]), conn=conn)
    assert "loan" in agent.system_prompt and "district" in agent.system_prompt
    assert "'POPLATEK MESICNE'" not in agent.system_prompt  # no enum values in names mode
    full = SQLAgent(Settings(api_key="test"), client=FakeClient([]), conn=conn)
    assert "'POPLATEK MESICNE'" in full.system_prompt


def test_get_table_schema(conn):
    ctx = ToolContext(conn=conn, settings=Settings(api_key="test"))
    out = call_tool(build_tools(), ctx, "get_table_schema", json.dumps({"table": "loan"}))
    assert "status" in out.content and "ALL VALUES" in out.content and "FOREIGN KEY" in out.content
    missing = call_tool(build_tools(), ctx, "get_table_schema", json.dumps({"table": "loans"}))
    assert "loan" in missing.content and "district" in missing.content
