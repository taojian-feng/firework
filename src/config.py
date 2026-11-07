"""Runtime settings, read once from environment variables (and an optional .env file).

Everything tunable lives here so the CLI, the agent and the eval harness agree on defaults.
"""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

FIREWORKS_BASE_URL = "https://api.fireworks.ai/inference/v1"
DEFAULT_MODEL = "accounts/fireworks/models/kimi-k2p6"


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


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
