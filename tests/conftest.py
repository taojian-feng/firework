"""Shared fixtures. Tests run offline: the LLM is replaced by a scripted fake client."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.db import connect_readonly

DB_PATH = Path("data/financial.sqlite")


@pytest.fixture(scope="session")
def conn():
    if not DB_PATH.exists():
        pytest.skip("data/financial.sqlite missing, run ./setup.sh")
    return connect_readonly(str(DB_PATH))


def tool_call(name: str, call_id: str = "call_1", **arguments):
    return SimpleNamespace(
        id=call_id, type="function", function=SimpleNamespace(name=name, arguments=json.dumps(arguments))
    )


def llm_response(content: str = "", tool_calls=None, prompt_tokens: int = 100, completion_tokens: int = 20):
    message = SimpleNamespace(content=content, tool_calls=tool_calls or None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


class FakeClient:
    """Mimics openai.OpenAI().chat.completions.create, returning scripted responses in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        # Snapshot the message list: the agent keeps appending to the same list object.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)
