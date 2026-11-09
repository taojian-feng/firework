"""Drill 4 (20 min): reuse verified question -> SQL pairs (few-shot memory).

Why: analysts ask the same kinds of questions; a verified query is faster and safer than a new one.

Spec:
- Settings has examples_path (env T2SQL_EXAMPLES_PATH, default "data/verified_queries.json"),
  a JSON list of {"question": ..., "sql": ...}. A missing file means no examples.
- Tool find_similar_queries(question) returns the top 3 pairs by word overlap with the question,
  best first, as text that includes each pair's SQL.
- Keep the dev questions OUT of that file, or the eval stops measuring anything.
- Discuss: when should the model call this tool? (Always first? Only when unsure?)
"""

import json

from src.config import Settings
from src.tools import ToolContext, build_tools, call_tool

PAIRS = [
    {"question": "How many gold cards are there?", "sql": "SELECT COUNT(*) FROM card WHERE type = 'gold'"},
    {"question": "Average loan amount per region", "sql": "SELECT d.A3, AVG(l.amount) FROM loan l JOIN account a USING(account_id) JOIN district d ON a.district_id = d.district_id GROUP BY d.A3"},
    {"question": "Number of clients by gender", "sql": "SELECT gender, COUNT(*) FROM client GROUP BY gender"},
]


def test_best_match_first(conn, tmp_path):
    path = tmp_path / "verified.json"
    path.write_text(json.dumps(PAIRS))
    ctx = ToolContext(conn=conn, settings=Settings(api_key="test", examples_path=str(path)))
    out = call_tool(build_tools(), ctx, "find_similar_queries", json.dumps({"question": "how many gold cards per region"}))
    assert "type = 'gold'" in out.content
    assert out.content.index("type = 'gold'") < out.content.index("AVG(l.amount)")


def test_missing_file_is_not_an_error(conn, tmp_path):
    ctx = ToolContext(conn=conn, settings=Settings(api_key="test", examples_path=str(tmp_path / "none.json")))
    out = call_tool(build_tools(), ctx, "find_similar_queries", json.dumps({"question": "anything"}))
    assert out.final_result is None and out.content  # a message, not a crash
