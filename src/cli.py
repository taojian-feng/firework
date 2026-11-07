"""Interactive CLI. Run with: uv run cli  (or: python -m src.cli)

    uv run cli                                  # sample banking database
    uv run cli --db path/to/other.sqlite        # any SQLite database (column docs are optional)
    uv run cli --model accounts/fireworks/models/gpt-oss-20b
"""

import argparse
import sys

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from src.agent import AgentResult, SQLAgent
from src.config import Settings

DISPLAY_ROWS = 50

HELP = """\
Ask a question in plain English, e.g. "How many accounts are in Prague?"
Follow-ups work: "now only for female clients", "sort that by amount".

Commands:
  /schema   show the schema context the model sees
  /trace    show the steps (LLM calls, tool calls) behind the last answer
  /reset    forget the conversation (start a new topic)
  /help     show this help
  exit      quit (or: quit, Ctrl-D)
"""

console = Console()


def render(answer: AgentResult) -> None:
    if answer.sql:
        console.print(Panel(Syntax(answer.sql, "sql", word_wrap=True), title="SQL", border_style="cyan"))
    if answer.assumptions:
        console.print(f"[yellow]Assumption:[/yellow] {answer.assumptions}")
    if answer.ok:
        result = answer.result
        table = Table(show_lines=False, header_style="bold")
        for column in result.columns:
            table.add_column(column)
        for row in result.rows[:DISPLAY_ROWS]:
            table.add_row(*["NULL" if v is None else str(v) for v in row])
        console.print(table)
        if len(result.rows) > DISPLAY_ROWS or result.truncated:
            console.print(f"[dim]Showing {DISPLAY_ROWS} of {len(result.rows)}{'+' if result.truncated else ''} rows.[/dim]")
    elif answer.message:
        console.print(Panel(answer.message, border_style="magenta"))
    console.print(
        f"[dim]{answer.latency_s:.2f}s total ({answer.llm_s:.2f}s model, {answer.llm_calls} call(s)) · "
        f"{answer.prompt_tokens:,} in / {answer.completion_tokens:,} out tokens[/dim]"
    )


def render_trace(answer: AgentResult | None) -> None:
    if answer is None:
        console.print("[dim]No question asked yet.[/dim]")
        return
    for i, step in enumerate(answer.steps, 1):
        console.print(f"[dim]{i:>2}. {step.elapsed_s:5.2f}s[/dim] [bold]{step.kind}[/bold] {step.detail}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ask your database questions in plain English.")
    parser.add_argument("--db", help="Path to a SQLite database (default: data/financial.sqlite)")
    parser.add_argument("--descriptions", help="Folder of column-description CSVs (optional)")
    parser.add_argument("--model", help="Fireworks model id")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    settings = Settings()
    if args.db:
        settings.db_path = args.db
        settings.description_dir = args.descriptions or ""
    elif args.descriptions:
        settings.description_dir = args.descriptions
    if args.model:
        settings.model = args.model
    if not settings.api_key:
        console.print("[red]FIREWORKS_API_KEY is not set.[/red] export it or put it in a .env file.")
        sys.exit(1)

    try:
        agent = SQLAgent(settings)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(1)

    console.print(
        Panel.fit(
            f"[bold]Text-to-SQL CLI[/bold]  ·  db: {settings.db_path}  ·  model: {settings.model}\n"
            "Type a question, /help for commands, exit to quit.",
            border_style="green",
        )
    )

    last: AgentResult | None = None
    while True:
        try:
            question = console.input("[bold green]ask>[/bold green] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        if not question:
            continue
        command = question.lower()
        if command in {"exit", "quit", "/exit", "/quit"}:
            break
        if command == "/help":
            console.print(HELP)
        elif command == "/schema":
            console.print(agent.schema_text, markup=False, highlight=False)
        elif command == "/trace":
            render_trace(last)
        elif command == "/reset":
            agent.reset()
            console.print("[dim]Conversation cleared.[/dim]")
        else:
            with console.status("Thinking..."):
                last = agent.ask(question)
            render(last)
    console.print("Bye!")


if __name__ == "__main__":
    main()
