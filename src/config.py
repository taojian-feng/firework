"""Runtime settings, read once from environment variables (and an optional .env file).

Everything tunable lives here so the CLI, the agent and the eval harness agree on defaults.
"""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

FIREWORKS_BASE_URL = "https://api.fireworks.ai/inference/v1"
DEFAULT_MODEL = "accounts/fireworks/models/kimi-k3"


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value else default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return value.strip().lower() not in {"0", "false", "no", "off"} if value else default


@dataclass
class Settings:
    api_key: str = field(default_factory=lambda: os.getenv("FIREWORKS_API_KEY", ""))
    base_url: str = field(default_factory=lambda: os.getenv("FIREWORKS_BASE_URL", FIREWORKS_BASE_URL))
    model: str = field(default_factory=lambda: os.getenv("T2SQL_MODEL", DEFAULT_MODEL))
    db_path: str = field(default_factory=lambda: os.getenv("T2SQL_DB_PATH", "data/financial.sqlite"))
    description_dir: str = field(
        default_factory=lambda: os.getenv("T2SQL_DESCRIPTION_DIR", "data/database_description")
    )
    # Agent loop limits: every LLM call costs ~1s, so the step budget is also a latency budget.
    max_steps: int = field(default_factory=lambda: _env_int("T2SQL_MAX_STEPS", 6))
    # Wall-clock budget for one question, checked before every LLM call after the first.
    max_seconds: float = field(default_factory=lambda: _env_float("T2SQL_MAX_SECONDS", 30.0))
    # Finance assistant: refuse non-finance questions before the agent runs (one small LLM call).
    guardrail: bool = field(default_factory=lambda: _env_bool("T2SQL_GUARDRAIL", True))
    # Model for the guardrail classifier. Empty = same as `model`; a small model is cheaper.
    guard_model: str = field(default_factory=lambda: os.getenv("T2SQL_GUARD_MODEL", ""))
    # Web search tool (DuckDuckGo via the ddgs package) for market / general finance questions.
    web_search: bool = field(default_factory=lambda: _env_bool("T2SQL_WEB_SEARCH", True))
    max_searches: int = field(default_factory=lambda: _env_int("T2SQL_MAX_SEARCHES", 3))
    search_results: int = field(default_factory=lambda: _env_int("T2SQL_SEARCH_RESULTS", 5))
    search_timeout_s: int = field(default_factory=lambda: _env_int("T2SQL_SEARCH_TIMEOUT_S", 10))
    # Timeout for one LLM request (the OpenAI client also retries twice).
    llm_timeout_s: float = field(default_factory=lambda: _env_float("T2SQL_LLM_TIMEOUT_S", 60.0))
    # How many previous question/answer pairs are replayed for follow-up questions.
    history_turns: int = field(default_factory=lambda: _env_int("T2SQL_HISTORY_TURNS", 5))
    # SQL execution guardrails.
    max_rows: int = field(default_factory=lambda: _env_int("T2SQL_MAX_ROWS", 1000))
    query_timeout_s: int = field(default_factory=lambda: _env_int("T2SQL_QUERY_TIMEOUT_S", 10))
    # Rows of each result shown to the model (keeps prompts small).
    preview_rows: int = field(default_factory=lambda: _env_int("T2SQL_PREVIEW_ROWS", 20))
    temperature: float = 0.0
    max_tokens: int = field(default_factory=lambda: _env_int("T2SQL_MAX_TOKENS", 2048))
    # Optional: "low" / "medium" / "high" for reasoning models that accept it (e.g. gpt-oss).
    reasoning_effort: str | None = field(default_factory=lambda: os.getenv("T2SQL_REASONING_EFFORT") or None)
