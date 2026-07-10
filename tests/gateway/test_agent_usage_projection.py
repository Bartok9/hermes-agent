"""Tests for gateway.agent_usage.project_agent_usage (#42477)."""
from types import SimpleNamespace

from gateway.agent_usage import project_agent_usage


def test_project_none_agent_defaults():
    usage = project_agent_usage(None)
    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 0
    assert usage["cache_read_tokens"] == 0
    assert usage["estimated_cost_usd"] == 0.0
    assert usage["cost_status"] == "unknown"
    assert usage["cost_source"] == "none"
    assert usage["model"] is None


def test_prefers_session_input_tokens_over_prompt_tokens():
    agent = SimpleNamespace(
        context_compressor=SimpleNamespace(last_prompt_tokens=9000, context_length=128000),
        session_input_tokens=1200,
        session_prompt_tokens=8000,
        session_completion_tokens=300,
        session_cache_read_tokens=6800,
        session_cache_write_tokens=50,
        session_estimated_cost_usd=0.42,
        session_cost_status="estimated",
        session_cost_source="openrouter",
        model="anthropic/claude-sonnet-4",
    )
    usage = project_agent_usage(agent)
    assert usage["input_tokens"] == 1200
    assert usage["output_tokens"] == 300
    assert usage["cache_read_tokens"] == 6800
    assert usage["cache_write_tokens"] == 50
    assert usage["estimated_cost_usd"] == 0.42
    assert usage["cost_status"] == "estimated"
    assert usage["cost_source"] == "openrouter"
    assert usage["last_prompt_tokens"] == 9000
    assert usage["context_length"] == 128000
    assert usage["model"] == "anthropic/claude-sonnet-4"


def test_falls_back_to_session_prompt_tokens_when_input_zero():
    agent = SimpleNamespace(
        context_compressor=None,
        session_input_tokens=0,
        session_prompt_tokens=555,
        session_completion_tokens=10,
        session_cache_read_tokens=0,
        session_cache_write_tokens=0,
        session_estimated_cost_usd=0.0,
        session_cost_status="unknown",
        session_cost_source="none",
        model="local",
    )
    usage = project_agent_usage(agent)
    assert usage["input_tokens"] == 555
    assert usage["last_prompt_tokens"] == 0
