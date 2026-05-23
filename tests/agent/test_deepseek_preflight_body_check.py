"""Regression tests for DeepSeek preflight body-size check (#30771).

DeepSeek silently returns HTTP 400 on request bodies over ~880 KB.
The check must raise a descriptive ValueError before the round-trip
when the base_url points at api.deepseek.com, and be a no-op for
every other provider.
"""

import json
import pytest
from unittest.mock import MagicMock

from agent.chat_completion_helpers import (
    _DEEPSEEK_BODY_LIMIT_BYTES,
    _deepseek_preflight_body_check,
)


def _make_agent(base_url: str) -> MagicMock:
    agent = MagicMock()
    agent.base_url = base_url
    return agent


def _oversized_kwargs() -> dict:
    """Build api_kwargs whose serialised body exceeds the DeepSeek limit."""
    big_message = "x" * (_DEEPSEEK_BODY_LIMIT_BYTES + 1_000)
    return {"messages": [{"role": "user", "content": big_message}], "model": "deepseek-v3"}


def _undersized_kwargs() -> dict:
    return {"messages": [{"role": "user", "content": "hi"}], "model": "deepseek-v3"}


class TestDeepSeekPreflightBodyCheck:
    def test_raises_on_oversized_body_for_deepseek(self):
        agent = _make_agent("https://api.deepseek.com/v1")
        with pytest.raises(ValueError, match="exceeds DeepSeek"):
            _deepseek_preflight_body_check(agent, _oversized_kwargs())

    def test_no_raise_on_undersized_body_for_deepseek(self):
        agent = _make_agent("https://api.deepseek.com/v1")
        # Must not raise
        _deepseek_preflight_body_check(agent, _undersized_kwargs())

    def test_no_raise_for_openai(self):
        agent = _make_agent("https://api.openai.com/v1")
        _deepseek_preflight_body_check(agent, _oversized_kwargs())

    def test_no_raise_for_openrouter(self):
        agent = _make_agent("https://openrouter.ai/api/v1")
        _deepseek_preflight_body_check(agent, _oversized_kwargs())

    def test_no_raise_for_anthropic(self):
        agent = _make_agent("https://api.anthropic.com/v1")
        _deepseek_preflight_body_check(agent, _oversized_kwargs())

    def test_no_raise_for_local_endpoint(self):
        agent = _make_agent("http://127.0.0.1:11434/v1")
        _deepseek_preflight_body_check(agent, _oversized_kwargs())

    def test_no_raise_when_base_url_is_none(self):
        agent = _make_agent(None)
        _deepseek_preflight_body_check(agent, _oversized_kwargs())

    def test_error_message_includes_byte_count_and_remedy(self):
        agent = _make_agent("https://api.deepseek.com/v1")
        with pytest.raises(ValueError) as exc_info:
            _deepseek_preflight_body_check(agent, _oversized_kwargs())
        msg = str(exc_info.value)
        assert "bytes" in msg
        assert "compress" in msg.lower() or "Compress" in msg

    def test_limit_constant_is_sensible(self):
        assert 800_000 <= _DEEPSEEK_BODY_LIMIT_BYTES <= 920_000
