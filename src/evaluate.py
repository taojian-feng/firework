"""Evaluation harness: accuracy, latency and token cost on the dev questions.

    uv run eval                       # agent, default model -> dev_answers.json + results/
    uv run eval --baseline            # the customer's original one-line prompt, same model
    uv run eval --model accounts/fireworks/models/gpt-oss-120b --repeat 3
    uv run eval --price-in 0.6 --price-out 2.5   # $/1M tokens -> projected daily cost

Scoring is execution-based (what BIRD calls EX): run the predicted SQL and compare its result
set with the gold result. Comparing results instead of SQL text accepts any correct query.
  * strict:  same rows and same number of columns (row order matters only if gold has ORDER BY)
  * lenient: the gold columns can be found inside the predicted result (extra columns allowed,
             e.g. the model also returned district_id). Reported to show *how* things fail.
"""

import argparse
import itertools
import json
import re
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openai import OpenAI
from rich.console import Console
from rich.table import Table

from src.agent import SQLAgent, summarize_result
from src.config import Settings
from src.db import QueryResult, connect_readonly, execute_sql
from src.prompts import BASELINE_PROMPT

QUESTIONS_PATH = Path("data/dev_questions_with_answers.json")
RESULTS_DIR = Path("results")
DAILY_QUERIES = 30_000  # 1,000 users x 30 queries/day, from the customer email

console = Console()


# ---------------------------------------------------------------------------------------------
# Result comparison
# ---------------------------------------------------------------------------------------------


def _norm(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return round(float(value), 2)  # 4190.6642 and 4190.66 are the same answer
    if isinstance(value, str):
        return value.strip()
    return value


def _rows_equal(pred: list[tuple], gold: list[tuple], ordered: bool) -> bool:
    pred_n = [tuple(_norm(v) for v in row) for row in pred]
    gold_n = [tuple(_norm(v) for v in row) for row in gold]
    return pred_n == gold_n if ordered else Counter(pred_n) == Counter(gold_n)


def results_match(
    pred_rows: list[tuple], gold_rows: list[tuple], ordered: bool, lenient: bool = False
) -> bool:
    if len(pred_rows) != len(gold_rows):
        return False
    if not gold_rows:
        return True
    gold_width, pred_width = len(gold_rows[0]), len(pred_rows[0])
    if pred_width == gold_width and _rows_equal(pred_rows, gold_rows, ordered):
        return True
    if not lenient or pred_width < gold_width or pred_width > 8:
        return False
    # Try every way of picking gold_width columns out of the prediction (tiny for real queries).
    for cols in itertools.permutations(range(pred_width), gold_width):
        projected = [tuple(row[i] for i in cols) for row in pred_rows]
        if _rows_equal(projected, gold_rows, ordered):
            return True
    return False


def gold_rows(question: dict) -> list[tuple]:
    return [tuple(record.values()) for record in question["expected_result"]]


def is_ordered(sql: str) -> bool:
    return re.search(r"\border\s+by\b", sql, re.IGNORECASE) is not None


# ---------------------------------------------------------------------------------------------
# Runners: each returns a Prediction for one question
# ---------------------------------------------------------------------------------------------


@dataclass
class Prediction:
    qid: str
    sql: str | None
    result: QueryResult | None
    answer: str
    latency_s: float
    llm_calls: int
    prompt_tokens: int
    completion_tokens: int
    trace: list[str] = field(default_factory=list)
    strict: bool = False
    lenient: bool = False


def run_agent(agent: SQLAgent, question: dict) -> Prediction:
    agent.reset()  # dev questions are independent; no conversation carry-over
    out = agent.ask(question["question"])
    return Prediction(
        qid=question["id"],
        sql=out.sql,
        result=out.result,
        answer=out.answer_text(),
        latency_s=out.latency_s,
        llm_calls=out.llm_calls,
        prompt_tokens=out.prompt_tokens,
        completion_tokens=out.completion_tokens,
        trace=[f"{s.kind}: {s.detail} ({s.elapsed_s:.2f}s)" for s in out.steps],
    )


def extract_sql(text: str) -> str:
    """Pull SQL out of a free-form completion (what the customer's prototype had to do)."""
    fence = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        return fence.group(1).strip()
    start = re.search(r"\b(SELECT|WITH)\b", text, re.IGNORECASE)
    return text[start.start():].strip() if start else text.strip()


def run_baseline(client: OpenAI, settings: Settings, conn, question: dict) -> Prediction:
    start = time.perf_counter()
    prompt_tokens = completion_tokens = 0
    try:
        response = client.chat.completions.create(
            model=settings.model,
            messages=[{"role": "user", "content": BASELINE_PROMPT.format(question=question["question"])}],
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
        )
        text = response.choices[0].message.content or ""
        if response.usage:
            prompt_tokens, completion_tokens = response.usage.prompt_tokens, response.usage.completion_tokens
        sql = extract_sql(text)
        result = execute_sql(conn, sql, settings.max_rows, settings.query_timeout_s)
        answer = summarize_result(result) if result.ok else f"ERROR: {result.error}"
    except Exception as exc:
        sql, result, answer = None, None, f"LLM request failed: {exc}"
    return Prediction(
        qid=question["id"],
        sql=sql,
        result=result,
        answer=answer,
        latency_s=time.perf_counter() - start,
        llm_calls=1,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def score(pred: Prediction, question: dict) -> None:
    if pred.result is None or not pred.result.ok:
        return
    ordered = is_ordered(question["gold_sql"])
    gold = gold_rows(question)
    pred.strict = results_match(pred.result.rows, gold, ordered)
    pred.lenient = pred.strict or results_match(pred.result.rows, gold, ordered, lenient=True)


# ---------------------------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------------------------


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return ordered[index]


def summarize(preds: list[Prediction], n_questions: int, args: argparse.Namespace) -> dict[str, Any]:
    latencies = [p.latency_s for p in preds]
    runs = len(preds) / n_questions
    prompt_tok = statistics.mean(p.prompt_tokens for p in preds)
    completion_tok = statistics.mean(p.completion_tokens for p in preds)
    summary = {
        "mode": "baseline" if args.baseline else "agent",
        "model": args.model,
        "runs": runs,
        "accuracy_strict": sum(p.strict for p in preds) / len(preds),
        "accuracy_lenient": sum(p.lenient for p in preds) / len(preds),
        "executable_rate": sum(1 for p in preds if p.result is not None and p.result.ok) / len(preds),
        "latency_p50_s": round(percentile(latencies, 50), 3),
        "latency_p95_s": round(percentile(latencies, 95), 3),
        "avg_llm_calls": round(statistics.mean(p.llm_calls for p in preds), 2),
        "avg_prompt_tokens": round(prompt_tok),
        "avg_completion_tokens": round(completion_tok),
    }
    if args.price_in is not None and args.price_out is not None:
        per_query = (prompt_tok * args.price_in + completion_tok * args.price_out) / 1_000_000
        summary["cost_per_query_usd"] = round(per_query, 6)
        summary["cost_per_day_usd_at_30k"] = round(per_query * DAILY_QUERIES, 2)
    return summary


def print_report(preds: list[Prediction], summary: dict[str, Any]) -> None:
    table = Table(title=f"{summary['mode']} · {summary['model']}")
    for column in ["id", "strict", "lenient", "latency", "calls", "answer"]:
        table.add_column(column, overflow="fold")
    for p in preds:
        table.add_row(
            p.qid,
            "PASS" if p.strict else "fail",
            "PASS" if p.lenient else "fail",
            f"{p.latency_s:.2f}s",
            str(p.llm_calls),
            p.answer[:90],
        )
    console.print(table)
    console.print_json(data=summary)


def main(argv: list[str] | None = None) -> None:
    settings = Settings()
    parser = argparse.ArgumentParser(description="Evaluate on the dev questions.")
    parser.add_argument("--baseline", action="store_true", help="use the customer's one-line prompt")
    parser.add_argument("--model", default=settings.model)
    parser.add_argument("--repeat", type=int, default=1, help="runs per question (for stable latency)")
    parser.add_argument("--questions", help="comma-separated ids, e.g. q_001,q_009")
    parser.add_argument("--price-in", type=float, help="$ per 1M input tokens")
    parser.add_argument("--price-out", type=float, help="$ per 1M output tokens")
    parser.add_argument("--answers-out", default="dev_answers.json")
    args = parser.parse_args(argv)
    settings.model = args.model

    questions = json.loads(QUESTIONS_PATH.read_text())
    if args.questions:
        wanted = set(args.questions.split(","))
        questions = [q for q in questions if q["id"] in wanted]

    conn = connect_readonly(settings.db_path)
    client = OpenAI(api_key=settings.api_key, base_url=settings.base_url, timeout=60, max_retries=2)
    agent = None if args.baseline else SQLAgent(settings, client=client, conn=conn)

    preds: list[Prediction] = []
    for run in range(args.repeat):
        for question in questions:
            pred = run_baseline(client, settings, conn, question) if args.baseline else run_agent(agent, question)
            score(pred, question)
            preds.append(pred)
            console.print(
                f"[dim]run {run + 1} {pred.qid}: {'PASS' if pred.strict else 'fail'} {pred.latency_s:.2f}s[/dim]"
            )

    summary = summarize(preds, len(questions), args)
    print_report(preds[: len(questions)] if args.repeat == 1 else preds, summary)

    RESULTS_DIR.mkdir(exist_ok=True)
    slug = args.model.rsplit("/", 1)[-1]
    report_path = RESULTS_DIR / f"eval_{summary['mode']}_{slug}.json"
    report_path.write_text(
        json.dumps(
            {
                "summary": summary,
                "predictions": [
                    {
                        "id": p.qid,
                        "sql": p.sql,
                        "answer": p.answer,
                        "strict": p.strict,
                        "lenient": p.lenient,
                        "latency_s": round(p.latency_s, 3),
                        "llm_calls": p.llm_calls,
                        "prompt_tokens": p.prompt_tokens,
                        "completion_tokens": p.completion_tokens,
                        "trace": p.trace,
                    }
                    for p in preds
                ],
            },
            indent=2,
        )
    )
    console.print(f"Saved {report_path}")

    if not args.baseline and not args.questions:
        answers = {p.qid: {"sql": p.sql or "", "answer": p.answer} for p in preds[: len(questions)]}
        Path(args.answers_out).write_text(json.dumps(answers, indent=2) + "\n")
        console.print(f"Saved {args.answers_out}")


if __name__ == "__main__":
    main()
