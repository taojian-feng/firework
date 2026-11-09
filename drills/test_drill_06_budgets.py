"""Drill 6 (10 min): more stop conditions, so one question can't run up cost or latency.

Spec:
- Settings.max_seconds (float, env T2SQL_MAX_SECONDS, default 30): before every LLM call except
  the first, stop if the question has run longer than this. AgentResult.message mentions
  "time budget".
- Settings.max_calls_per_tool (int, env T2SQL_MAX_CALLS_PER_TOOL, default 4): once a tool has been
  called that many times for one question, further calls are not executed and the model is told
  the limit was reached (the tool message contains "limit").
"""

from src.agent import SQLAgent
from src.config import Settings
from tests.conftest import FakeClient, llm_response, tool_call


def test_time_budget(conn):
    client = FakeClient([llm_response(tool_calls=[tool_call("run_sql", sql="SELECT 1")])])
    agent = SQLAgent(Settings(api_key="test", max_seconds=0), client=client, conn=conn)
    out = agent.ask("anything")
    assert not out.ok and out.llm_calls == 1 and "time budget" in out.message


def test_per_tool_cap(conn):
    explore = [llm_response(tool_calls=[tool_call("run_sql", call_id=f"c{i}", sql="SELECT 1")]) for i in range(3)]
    submit = llm_response(tool_calls=[tool_call("submit_answer", call_id="s", sql="SELECT COUNT(*) FROM card")])
    client = FakeClient([*explore, submit])
    agent = SQLAgent(Settings(api_key="test", max_calls_per_tool=2), client=client, conn=conn)
    out = agent.ask("How many cards?")
    assert out.ok
    third_run_sql_result = client.requests[3]["messages"][-1]
    assert third_run_sql_result["role"] == "tool" and "limit" in third_run_sql_result["content"].lower()
