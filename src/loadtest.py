"""Concurrency benchmark: latency percentiles, throughput and errors under load.

    uv run loadtest                                   # 1, 4, 8 concurrent users, 30s each
    uv run loadtest --concurrency 1,8,16 --duration 60 --model accounts/fireworks/models/gpt-oss-120b
    uv run loadtest --route sql                       # only database questions (no web search)

Closed loop: N workers, each with its own agent (own DB connection and history), ask a random
question, wait for the answer, ask the next, until the duration is up. So "concurrency" is the
number of simultaneous users, and throughput (qps) is what the system sustained for them.

An error is a turn that failed (LLM error such as 429/503 or a timeout, step or time budget hit)
or raised. Web search goes to DuckDuckGo, which rate-limits quickly; use --route sql to measure
the model and the database alone.
"""

import argparse
import json
import random
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from rich.console import Console
from rich.table import Table

from src.agent import SQLAgent
from src.config import Settings
from src.evaluate import percentile

QUESTIONS_PATH = Path("evals/finance_questions.json")
RESULTS_DIR = Path("results")

console = Console()


def run_load(make_agent: Callable[[], SQLAgent], questions: list[dict], concurrency: int, duration_s: float) -> dict:
    agents = [make_agent() for _ in range(concurrency)]  # created up front: schema load isn't load
    records: list[dict] = []
    lock = threading.Lock()
    deadline = time.perf_counter() + duration_s

    def worker(agent: SQLAgent, seed: int) -> None:
        rng = random.Random(seed)
        while time.perf_counter() < deadline:
            question = rng.choice(questions)["question"]
            agent.reset()
            t0 = time.perf_counter()
            try:
                out = agent.ask(question)
                record = {
                    "latency_s": time.perf_counter() - t0,
                    "error": out.error,
                    "route": out.route,
                    "llm_calls": out.llm_calls,
                    "tokens": out.prompt_tokens + out.completion_tokens,
                }
            except Exception as exc:  # the agent shouldn't raise; count it if it does
                record = {"latency_s": time.perf_counter() - t0, "error": repr(exc), "route": "", "llm_calls": 0, "tokens": 0}
            with lock:
                records.append(record)

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for i, agent in enumerate(agents):
            pool.submit(worker, agent, i)
    elapsed = time.perf_counter() - started

    latencies = [r["latency_s"] for r in records] or [0.0]
    errors = [r["error"] for r in records if r["error"]]
    return {
        "concurrency": concurrency,
        "requests": len(records),
        "qps": round(len(records) / elapsed, 3) if elapsed else 0.0,
        "p50_s": round(percentile(latencies, 50), 3),
        "p95_s": round(percentile(latencies, 95), 3),
        "max_s": round(max(latencies), 3),
        "error_rate": round(len(errors) / len(records), 3) if records else 0.0,
        "errors_sample": sorted(set(e[:120] for e in errors))[:3],
        "avg_llm_calls": round(statistics.mean(r["llm_calls"] for r in records), 2) if records else 0.0,
        "avg_tokens": round(statistics.mean(r["tokens"] for r in records)) if records else 0,
    }


def main(argv: list[str] | None = None) -> None:
    settings = Settings()
    parser = argparse.ArgumentParser(description="Concurrency benchmark for the finance assistant.")
    parser.add_argument("--concurrency", default="1,4,8", help="comma-separated levels, e.g. 1,4,8")
    parser.add_argument("--duration", type=float, default=30.0, help="seconds per level")
    parser.add_argument("--model", default=settings.model)
    parser.add_argument("--route", choices=["sql", "web", "refused"], help="only questions with this route")
    args = parser.parse_args(argv)
    settings.model = args.model

    questions = json.loads(QUESTIONS_PATH.read_text())
    if args.route:
        questions = [q for q in questions if q["route"] == args.route]

    reports = []
    for level in [int(x) for x in args.concurrency.split(",")]:
        console.print(f"[dim]concurrency {level} for {args.duration:.0f}s ...[/dim]")
        reports.append(run_load(lambda: SQLAgent(settings), questions, level, args.duration))

    table = Table(title=f"load test · {args.model} · {args.route or 'mixed'} questions")
    for column in ["users", "requests", "qps", "p50", "p95", "max", "errors", "calls/q", "tokens/q"]:
        table.add_column(column)
    for r in reports:
        table.add_row(
            str(r["concurrency"]), str(r["requests"]), f"{r['qps']:.2f}", f"{r['p50_s']:.2f}s",
            f"{r['p95_s']:.2f}s", f"{r['max_s']:.2f}s", f"{r['error_rate']:.0%}",
            f"{r['avg_llm_calls']:.1f}", str(r["avg_tokens"]),
        )
    console.print(table)
    for r in reports:
        if r["errors_sample"]:
            console.print(f"[red]errors at {r['concurrency']} users:[/red] {r['errors_sample']}")

    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / f"loadtest_{args.model.rsplit('/', 1)[-1]}.json"
    path.write_text(json.dumps({"model": args.model, "route": args.route, "levels": reports}, indent=2))
    console.print(f"Saved {path}")


if __name__ == "__main__":
    main()
