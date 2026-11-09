"""Drill 2 (15 min): an ask_user tool so the model can ask a clarifying question.

Spec:
- build_tools() includes "ask_user" with one required string argument "question".
- It is terminal: calling it ends the turn with AgentResult.message == the question, no SQL.
"""

from src.agent import SQLAgent
from src.config import Settings
from src.tools import build_tools
from tests.conftest import FakeClient, llm_response, tool_call


def test_ask_user_is_registered_and_terminal():
    tools = build_tools()
    assert "ask_user" in tools and tools["ask_user"].terminal
    assert tools["ask_user"].parameters["required"] == ["question"]


def test_ask_user_ends_the_turn_with_the_question(conn):
    client = FakeClient([llm_response(tool_calls=[tool_call("ask_user", question="Which year?")])])
    agent = SQLAgent(Settings(api_key="test"), client=client, conn=conn)
    out = agent.ask("Show me the loan trend")
    assert not out.ok and out.sql is None
    assert out.message == "Which year?" and out.llm_calls == 1
    sent = [t["function"]["name"] for t in client.requests[0]["tools"]]
    assert "ask_user" in sent
