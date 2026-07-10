"""Helpers to project AIAgent session counters into gateway return dicts."""
from __future__ import annotations

from typing import Any, Dict, Optional


def project_agent_usage(agent: Optional[Any]) -> Dict[str, Any]:
    """Project an AIAgent's in-memory usage/cost counters into a plain dict.

    Gateway platforms (Telegram/Discord/etc.) used to read only
    ``session_prompt_tokens`` / ``session_completion_tokens`` and drop cache +
    cost fields, which made Telegram sessions look like $0/0 tokens in
    state/UI surfaces that consume the gateway return payload (#42477).

    Prefers ``session_input_tokens`` (canonical billed input) and falls back to
    ``session_prompt_tokens`` when the canonical counter was never advanced.
    """
    out: Dict[str, Any] = {
        "last_prompt_tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "context_length": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "estimated_cost_usd": 0.0,
        "cost_status": "unknown",
        "cost_source": "none",
        "model": None,
    }
    if agent is None:
        return out
    compressor = getattr(agent, "context_compressor", None)
    if compressor is not None:
        out["last_prompt_tokens"] = int(getattr(compressor, "last_prompt_tokens", 0) or 0)
        out["context_length"] = int(getattr(compressor, "context_length", 0) or 0)
    input_tokens = int(getattr(agent, "session_input_tokens", 0) or 0)
    if not input_tokens:
        input_tokens = int(getattr(agent, "session_prompt_tokens", 0) or 0)
    out["input_tokens"] = input_tokens
    out["output_tokens"] = int(getattr(agent, "session_completion_tokens", 0) or 0)
    out["cache_read_tokens"] = int(getattr(agent, "session_cache_read_tokens", 0) or 0)
    out["cache_write_tokens"] = int(getattr(agent, "session_cache_write_tokens", 0) or 0)
    out["estimated_cost_usd"] = float(getattr(agent, "session_estimated_cost_usd", 0.0) or 0.0)
    out["cost_status"] = getattr(agent, "session_cost_status", "unknown") or "unknown"
    out["cost_source"] = getattr(agent, "session_cost_source", "none") or "none"
    out["model"] = getattr(agent, "model", None)
    return out
