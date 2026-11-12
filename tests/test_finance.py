"""Finance assistant: guardrail, web_search routing, and the stop conditions. All offline."""

from datetime import date

from src.agent import SQLAgent
from src.config import Settings
from tests.conftest import FakeClient, llm_response, tool_call

RESULTS = [
    {"title": "NVIDIA Q2 FY2027 results", "href": "https://example.com/nvda", "body": "Revenue was ..."},
    {"title": "Earnings recap", "href": "https://example.com/recap", "body": "Shares rose ..."},
]


class FakeSearch:
    def __init__(self, results=RESULTS, fail=False):
        self.results, self.fail, self.queries = results, fail, []

    def __call__(self, query, max_results, timeout_s, timelimit=None):
        self.queries.append(query)
        if self.fail:
            raise TimeoutError("search timed out")
        return self.results


def make_agent(conn, responses, search=None, **overrides):
    settings = Settings(api_key="test", **overrides)
    client = FakeClient(responses)
    agent = SQLAgent(settings, client=client, conn=conn, search_fn=search or FakeSearch(), today=date(2026, 11, 12))
    return agent, client


def test_off_topic_question_is_refused_without_tools(conn):
    agent, client = make_agent(conn, [llm_response(content="OTHER")], guardrail=True)
    out = agent.ask("What's the weather in London?")
    assert out.route == "refused" and "finance" in out.message.lower()
    assert out.llm_calls == 1 and "tools" not in client.requests[0]
    assert agent.history == []  # refused turns don't steer follow-ups


def test_database_question_goes_to_sql(conn):
    agent, client = make_agent(
        conn,
        [
            llm_response(content="FINANCE"),
            llm_response(tool_calls=[tool_call("submit_answer", sql="SELECT COUNT(*) AS n FROM loan")]),
        ],
        guardrail=True,
    )
    out = agent.ask("How many loans are there?")
    assert out.route == "sql" and out.result.rows == [(682,)] and out.llm_calls == 2
    assert [s.kind for s in out.steps][0] == "guard"


def test_guardrail_sees_previous_question_for_follow_ups(conn):
    agent, client = make_agent(
        conn,
        [
            llm_response(content="FINANCE"),
            llm_response(tool_calls=[tool_call("submit_answer", sql="SELECT COUNT(*) FROM loan")]),
            llm_response(content="FINANCE"),
            llm_response(tool_calls=[tool_call("submit_answer", sql="SELECT COUNT(*) FROM card")]),
        ],
        guardrail=True,
    )
    agent.ask("How many loans are there?")
    agent.ask("and cards?")
    assert "How many loans are there?" in client.requests[2]["messages"][0]["content"]


def test_market_question_goes_to_web(conn):
    search = FakeSearch()
    agent, client = make_agent(
        conn,
        [
            llm_response(content="FINANCE"),
            llm_response(tool_calls=[tool_call("web_search", query="NVIDIA Q2 2026 earnings")]),
            llm_response(
                tool_calls=[
                    tool_call("final_answer", call_id="c2", answer="Revenue grew ...", sources=["https://example.com/nvda"])
                ]
            ),
        ],
        search=search,
        guardrail=True,
    )
    out = agent.ask("How did NVIDIA do in Q2?")
    assert out.route == "web" and out.message == "Revenue grew ..." and out.sources == ["https://example.com/nvda"]
    assert search.queries == ["NVIDIA Q2 2026 earnings"] and out.sql is None
    tool_message = client.requests[2]["messages"][-1]
    assert tool_message["role"] == "tool" and "https://example.com/recap" in tool_message["content"]


def test_system_prompt_has_todays_date(conn):
    agent, _ = make_agent(conn, [])
    assert "Thursday 12 November 2026" in agent.system_prompt


def test_search_cap(conn):
    search = FakeSearch()
    searches = [llm_response(tool_calls=[tool_call("web_search", call_id=f"c{i}", query=f"q{i}")]) for i in range(2)]
    final = llm_response(tool_calls=[tool_call("final_answer", call_id="f", answer="Best effort answer.")])
    agent, client = make_agent(conn, [*searches, final], search=search, max_searches=1)
    out = agent.ask("Latest ECB rate?")
    assert search.queries == ["q0"] and out.route == "web"
    assert "limit" in client.requests[2]["messages"][-1]["content"].lower()
    assert out.sources == ["https://example.com/nvda", "https://example.com/recap"]  # defaults to URLs seen


def test_search_failure_is_returned_to_the_model(conn):
    agent, client = make_agent(
        conn,
        [
            llm_response(tool_calls=[tool_call("web_search", query="gold price today")]),
            llm_response(content="I couldn't reach the web right now."),
        ],
        search=FakeSearch(fail=True),
    )
    out = agent.ask("Gold price today?")
    assert "search failed" in client.requests[1]["messages"][-1]["content"].lower()
    assert out.message == "I couldn't reach the web right now."


def test_text_reply_after_search_is_the_answer(conn):
    agent, _ = make_agent(
        conn,
        [
            llm_response(tool_calls=[tool_call("web_search", query="credit vs debit card")]),
            llm_response(content="A credit card borrows money; a debit card spends your own."),
        ],
    )
    out = agent.ask("Credit vs debit card?")
    assert out.route == "web" and out.message.startswith("A credit card") and out.llm_calls == 2
    assert out.sources  # the URLs that were seen


def test_time_budget(conn):
    agent, _ = make_agent(conn, [llm_response(tool_calls=[tool_call("web_search", query="x")])], max_seconds=0)
    out = agent.ask("anything")
    assert out.error and "time budget" in out.message and out.llm_calls == 1


def test_guardrail_fails_open(conn):
    class Broken(FakeClient):
        def _create(self, **kwargs):
            if "tools" not in kwargs:
                raise ConnectionError("down")
            return super()._create(**kwargs)

    client = Broken([llm_response(tool_calls=[tool_call("submit_answer", sql="SELECT COUNT(*) FROM card")])])
    agent = SQLAgent(Settings(api_key="test", guardrail=True), client=client, conn=conn)
    out = agent.ask("How many cards?")
    assert out.route == "sql" and "classifier failed" in out.steps[0].detail


def test_web_tools_can_be_turned_off(conn):
    agent, _ = make_agent(conn, [], web_search=False)
    assert "web_search" not in agent.tools and "final_answer" not in agent.tools


def test_load_benchmark_reports_percentiles(conn):
    import threading

    from src.loadtest import run_load

    class RepeatingClient(FakeClient):
        lock = threading.Lock()

        def _create(self, **kwargs):
            with self.lock:
                self.requests.append(kwargs)
            return llm_response(tool_calls=[tool_call("submit_answer", sql="SELECT COUNT(*) FROM card")])

    client = RepeatingClient([])
    made = []

    def make_agent():
        made.append(SQLAgent(Settings(api_key="test"), client=client))  # own DB connection per worker
        return made[-1]

    report = run_load(make_agent, [{"question": "How many cards?"}], concurrency=3, duration_s=0.3)
    assert len(made) == 3 and len({id(a.conn) for a in made}) == 3
    assert report["requests"] == len(client.requests) > 0 and report["error_rate"] == 0
    assert report["p50_s"] <= report["p95_s"] and report["qps"] > 0
