"""Routing eval for the finance assistant: does each question go to the right place?

    uv run finance-eval                        # all 12 questions, default model
    uv run finance-eval --model accounts/fireworks/models/gpt-oss-120b --repeat 3
    uv run finance-eval --ids ref_01,web_02

Each question in evals/finance_questions.json has an expected route:
  * sql      -> answered with SQL, and the result matches the gold query (lenient execution match)
  * web      -> answered after at least one web_search, with at least one source URL
  * refused  -> stopped by the guardrail
A question passes when the route is right AND the answer checks out. The web answers' content is
not graded automatically (it changes daily); read them in the saved report.
"""

import argparse
import json
import statistics
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.agent import AgentResult, SQLAgent
from src.config import Settings
from src.db import execute_sql
from src.evaluate import percentile, results_match

QUESTIONS_PATH = Path("evals/finance_questions.json")
RESULTS_DIR = Path("results")

console = Console()


def check(agent: SQLAgent, question: dict, out: AgentResult) -> bool:
    if out.route != question["route"]:
        return False
    if out.route == "sql":
        gold = execute_sql(agent.conn, question["gold_sql"], agent.settings.max_rows, agent.settings.query_timeout_s)
        return results_match(out.result.rows, gold.rows, ordered=False, lenient=True)
    if out.route == "web":
        return bool(out.sources) and bool(out.message)
    return True  # refused


def main(argv: list[str] | None = None) -> None:
    settings = Settings()
    parser = argparse.ArgumentParser(description="Finance assistant routing eval.")
    parser.add_argument("--model", default=settings.model)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--ids", help="comma-separated question ids")
    args = parser.parse_args(argv)
    settings.model = args.model

    questions = json.loads(QUESTIONS_PATH.read_text())
    if args.ids:
        questions = [q for q in questions if q["id"] in set(args.ids.split(","))]
    agent = SQLAgent(settings)

    rows = []
    for _ in range(args.repeat):
        for q in questions:
            agent.reset()  # independent questions
            out = agent.ask(q["question"])
            passed = check(agent, q, out)
            rows.append(
                {
                    "id": q["id"],
                    "expected": q["route"],
                    "route": out.route,
                    "pass": passed,
                    "latency_s": round(out.latency_s, 3),
                    "llm_calls": out.llm_calls,
                    "tokens": out.prompt_tokens + out.completion_tokens,
                    "answer": out.answer_text() if out.route == "sql" else out.message,
                    "sql": out.sql,
                    "sources": out.sources,
                    "trace": [f"{s.kind}: {s.detail} ({s.elapsed_s:.2f}s)" for s in out.steps],
                }
            )
            console.print(f"[dim]{q['id']}: {out.route} {'PASS' if passed else 'fail'} {out.latency_s:.2f}s[/dim]")

    table = Table(title=f"finance routing · {args.model}")
    for column in ["id", "expected", "got", "result", "latency", "calls", "answer"]:
        table.add_column(column, overflow="fold")
    for r in rows:
        table.add_row(
            r["id"], r["expected"], r["route"], "PASS" if r["pass"] else "fail",
            f"{r['latency_s']:.2f}s", str(r["llm_calls"]), (r["answer"] or "")[:80],
        )
    console.print(table)

    summary = {"model": args.model, "accuracy": round(sum(r["pass"] for r in rows) / len(rows), 3)}
    for route in ("sql", "web", "refused"):
        subset = [r for r in rows if r["expected"] == route]
        if subset:
            latencies = [r["latency_s"] for r in subset]
            summary[route] = {
                "accuracy": round(sum(r["pass"] for r in subset) / len(subset), 3),
                "p50_s": round(percentile(latencies, 50), 2),
                "p95_s": round(percentile(latencies, 95), 2),
                "avg_llm_calls": round(statistics.mean(r["llm_calls"] for r in subset), 2),
            }
    console.print_json(data=summary)

    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / f"finance_eval_{args.model.rsplit('/', 1)[-1]}.json"
    path.write_text(json.dumps({"summary": summary, "predictions": rows}, indent=2))
    console.print(f"Saved {path}")


if __name__ == "__main__":
    main()
