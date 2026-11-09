"""Drill 7 (15 min): a cheap model first, a stronger model when it fails.

Why: gpt-oss-120b is ~17x cheaper than Kimi K3 at the same dev accuracy; pay for the big model
only when needed.

Spec:
- Settings.fallback_model (env T2SQL_FALLBACK_MODEL, default None).
- If set and the first attempt has no result (out.ok is False), retry the question once with
  fallback_model. Token and LLM-call counts add up across both attempts.
- AgentResult.model records which model produced the final answer.
- Discuss: should a clarifying question escalate? How do you keep history from getting the failed
  turn twice?
"""

from src.agent import SQLAgent
from src.config import Settings
from tests.conftest import FakeClient, llm_response, tool_call


def test_escalates_after_failure(conn):
    client = FakeClient(
        [
            llm_response(tool_calls=[tool_call("run_sql", sql="SELECT 1")]),  # cheap model runs out of steps
            llm_response(tool_calls=[tool_call("submit_answer", call_id="s", sql="SELECT COUNT(*) FROM card")]),
        ]
    )
    settings = Settings(api_key="test", model="cheap", fallback_model="strong", max_steps=1)
    out = SQLAgent(settings, client=client, conn=conn).ask("How many cards?")
    assert out.ok and out.model == "strong" and out.llm_calls == 2
    assert [r["model"] for r in client.requests] == ["cheap", "strong"]


def test_no_escalation_when_first_model_succeeds(conn):
    client = FakeClient([llm_response(tool_calls=[tool_call("submit_answer", sql="SELECT 1")])])
    settings = Settings(api_key="test", model="cheap", fallback_model="strong")
    out = SQLAgent(settings, client=client, conn=conn).ask("one")
    assert out.ok and out.model == "cheap" and len(client.requests) == 1
