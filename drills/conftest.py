"""Fixtures for the practice drills. Reuses the offline FakeClient from tests/conftest.py."""

import threading

from tests.conftest import conn, llm_response, tool_call  # noqa: F401  (conn is a fixture)


class RepeatingClient:
    """Fake Fireworks client that returns the same response forever. Thread-safe, for load tests."""

    def __init__(self, response):
        self.response = response
        self.calls = 0
        self._lock = threading.Lock()
        self.chat = type("Chat", (), {})()
        self.chat.completions = type("Completions", (), {"create": self._create})()

    def _create(self, **kwargs):
        with self._lock:
            self.calls += 1
        return self.response
