"""Drill 8 (30 min): a load test harness.

Spec:
- New module src/loadtest.py with
      run_load(make_agent, questions, concurrency, duration_s) -> dict
  make_agent() returns a fresh SQLAgent; call it once per worker (each worker needs its own
  connection and history). Workers loop until duration_s passes: reset(), ask a random question,
  record latency and outcome.
- The dict has: concurrency, requests, qps, p50_s, p95_s, error_rate, avg_llm_calls, avg_tokens.
- Then: a main() / `uv run loadtest` script entry that sweeps --concurrency 1,4,8 against the real
  API and prints one row per level. Run it on your Mac with gpt-oss-120b.
- Discuss: closed loop (N users) vs open loop (fixed arrival rate); what 429/503s mean on
  serverless; why P95 matters more than P50 here.
"""

from src.agent import SQLAgent
from src.config import Settings
from src.loadtest import run_load
from tests.conftest import llm_response, tool_call
from drills.conftest import RepeatingClient

QUESTIONS = [{"question": "How many cards?"}, {"question": "How many loans?"}]


def test_run_load(conn):
    client = RepeatingClient(llm_response(tool_calls=[tool_call("submit_answer", sql="SELECT COUNT(*) FROM card")]))
    made = []

    def make_agent():
        agent = SQLAgent(Settings(api_key="test"), client=client)  # own connection per worker
        made.append(agent)
        return agent

    report = run_load(make_agent, QUESTIONS, concurrency=4, duration_s=0.5)
    assert len(made) == 4 and len({id(a.conn) for a in made}) == 4
    assert report["concurrency"] == 4 and report["requests"] > 0 and report["error_rate"] == 0
    assert report["requests"] == client.calls  # one LLM call per question on the happy path
    for key in ("qps", "p50_s", "p95_s", "avg_llm_calls", "avg_tokens"):
        assert key in report
    assert report["p50_s"] <= report["p95_s"]
