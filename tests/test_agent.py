from src.agent import SQLAgent
from src.config import Settings

from tests.conftest import FakeClient, llm_response, tool_call


def make_agent(conn, responses, **overrides):
    settings = Settings(api_key="test", **overrides)
    client = FakeClient(responses)
    return SQLAgent(settings, client=client, conn=conn), client


def test_happy_path_is_one_llm_call(conn):
    agent, client = make_agent(
        conn, [llm_response(tool_calls=[tool_call("submit_answer", sql="SELECT COUNT(*) AS n FROM loan")])]
    )
    out = agent.ask("How many loans?")
    assert out.ok and out.result.rows == [(682,)]
    assert out.llm_calls == 1 and out.answer_text() == "n = 682"
    assert "TABLE loan" in client.requests[0]["messages"][0]["content"]


def test_sql_error_is_repaired(conn):
    agent, client = make_agent(
        conn,
        [
            llm_response(tool_calls=[tool_call("submit_answer", sql="SELECT COUNT(*) FROM loans")]),
            llm_response(tool_calls=[tool_call("submit_answer", call_id="c2", sql="SELECT COUNT(*) FROM loan")]),
        ],
    )
    out = agent.ask("How many loans?")
    assert out.ok and out.llm_calls == 2
    tool_message = client.requests[1]["messages"][-1]
    assert tool_message["role"] == "tool" and "no such table" in tool_message["content"]


def test_empty_result_is_questioned_once(conn):
    empty = "SELECT COUNT(*) FROM district WHERE A3 = 'Praha' GROUP BY A3"
    agent, _ = make_agent(
        conn,
        [
            llm_response(tool_calls=[tool_call("submit_answer", sql=empty)]),
            llm_response(tool_calls=[tool_call("submit_answer", call_id="c2", sql=empty)]),
        ],
    )
    out = agent.ask("How many districts in Praha?")
    assert out.ok and out.result.rows == [] and out.llm_calls == 2


def test_plain_text_reply_is_a_message(conn):
    agent, _ = make_agent(conn, [llm_response(content="Which year do you mean?")])
    out = agent.ask("Show me the trend")
    assert not out.ok and out.message == "Which year do you mean?"


def test_sql_in_text_is_executed(conn):
    agent, _ = make_agent(conn, [llm_response(content="```sql\nSELECT COUNT(*) FROM card\n```")])
    assert agent.ask("How many cards?").result.rows == [(892,)]


def test_step_budget(conn):
    explore = llm_response(tool_calls=[tool_call("run_sql", sql="SELECT 1")])
    agent, _ = make_agent(conn, [explore, explore], max_steps=2)
    out = agent.ask("loop forever")
    assert not out.ok and "Stopped after 2 steps" in out.message


def test_follow_up_sees_previous_turn(conn):
    agent, client = make_agent(
        conn,
        [
            llm_response(tool_calls=[tool_call("submit_answer", sql="SELECT COUNT(*) FROM client")]),
            llm_response(tool_calls=[tool_call("submit_answer", sql="SELECT COUNT(*) FROM client WHERE gender='F'")]),
        ],
    )
    agent.ask("How many clients?")
    agent.ask("only women")
    messages = client.requests[1]["messages"]
    assert messages[1]["content"] == "How many clients?"
    assert "SELECT COUNT(*) FROM client" in messages[2]["content"]


def test_text_reply_after_run_sql_falls_back_to_that_result(conn):
    agent, _ = make_agent(
        conn,
        [
            llm_response(tool_calls=[tool_call("run_sql", sql="SELECT COUNT(*) AS n FROM card")]),
            llm_response(content="There are 892 cards."),
        ],
    )
    out = agent.ask("How many cards?")
    assert out.ok and out.result.rows == [(892,)] and out.sql.startswith("SELECT COUNT(*)")
    assert out.steps[-1].kind == "fallback"


def _extra_tool(name, handler, terminal):
    from src.tools import Tool

    return Tool(name=name, description=name, parameters={"type": "object", "properties": {}}, handler=handler,
                terminal=terminal)


def test_terminal_tool_can_end_turn_with_a_message(conn):
    from src.tools import ToolOutput, build_tools

    tools = build_tools()
    tools["ask_user"] = _extra_tool(
        "ask_user", lambda ctx, question: ToolOutput("Asked.", final_message=question), terminal=True
    )
    settings = Settings(api_key="test")
    client = FakeClient([llm_response(tool_calls=[tool_call("ask_user", question="Which year?")])])
    agent = SQLAgent(settings, client=client, conn=conn, tools=tools)
    out = agent.ask("Show me the trend")
    assert not out.ok and out.message == "Which year?" and out.llm_calls == 1
    assert agent.history[-1]["content"] == "Which year?"  # a follow-up answer sees the question


def test_non_terminal_tool_cannot_end_turn(conn):
    from src.db import execute_sql
    from src.tools import ToolOutput, build_tools

    def sneaky(ctx):
        return ToolOutput("rows", final_result=execute_sql(ctx.conn, "SELECT 1"))

    tools = build_tools()
    tools["sneaky"] = _extra_tool("sneaky", sneaky, terminal=False)
    client = FakeClient(
        [
            llm_response(tool_calls=[tool_call("sneaky")]),
            llm_response(tool_calls=[tool_call("submit_answer", call_id="c2", sql="SELECT COUNT(*) FROM card")]),
        ]
    )
    agent = SQLAgent(Settings(api_key="test"), client=client, conn=conn, tools=tools)
    out = agent.ask("How many cards?")
    assert out.ok and out.result.rows == [(892,)] and out.llm_calls == 2
