"""Tests for the proactive loop synthesis engine."""

from __future__ import annotations

import json
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from hermes_cli.proactive_loop import (
    ProactiveLoop,
    SynthesisResult,
    _build_synthesis_prompt,
    _parse_synthesis_response,
    _THRESHOLD_SCORES,
    register_threshold,
)


# ──────────────────────────────────────────────────────────────────────
# Unit tests — prompt builder
# ──────────────────────────────────────────────────────────────────────


def test_build_synthesis_prompt_contains_history():
    prompt = _build_synthesis_prompt("user: hello\nassistant: hi", "(none)")
    assert "user: hello" in prompt
    assert "assistant: hi" in prompt


def test_build_synthesis_prompt_contains_already_sent():
    prompt = _build_synthesis_prompt("history", "already sent: xyz")
    assert "already sent: xyz" in prompt


def test_build_synthesis_prompt_requests_json():
    prompt = _build_synthesis_prompt("h", "n")
    assert '"should_send"' in prompt
    assert '"novelty"' in prompt
    assert '"message"' in prompt


# ──────────────────────────────────────────────────────────────────────
# Unit tests — response parser
# ──────────────────────────────────────────────────────────────────────


def test_parse_valid_json():
    raw = json.dumps({
        "should_send": True,
        "message": "Hey, found something.",
        "novelty": 0.8,
        "relevance": 0.9,
        "reasoning": "New finding.",
        "candidates": ["option a"],
    })
    result = _parse_synthesis_response(raw)
    assert result["should_send"] is True
    assert result["message"] == "Hey, found something."
    assert result["novelty"] == pytest.approx(0.8)


def test_parse_json_with_markdown_fence():
    raw = "```json\n{\"should_send\": false, \"message\": null, \"novelty\": 0.1, \"relevance\": 0.2, \"reasoning\": \"nothing\", \"candidates\": []}\n```"
    result = _parse_synthesis_response(raw)
    assert result["should_send"] is False


def test_parse_malformed_json_returns_no_send():
    result = _parse_synthesis_response("this is not json at all!!")
    assert result["should_send"] is False
    assert result["message"] is None
    assert result["novelty"] == pytest.approx(0.0)
    assert "parse failure" in result["reasoning"]


def test_parse_empty_string_returns_no_send():
    result = _parse_synthesis_response("")
    assert result["should_send"] is False


# ──────────────────────────────────────────────────────────────────────
# Unit tests — threshold scores
# ──────────────────────────────────────────────────────────────────────


def test_conservative_threshold_higher_than_eager():
    assert _THRESHOLD_SCORES["conservative"] > _THRESHOLD_SCORES["eager"]


def test_balanced_between_conservative_and_eager():
    assert _THRESHOLD_SCORES["eager"] < _THRESHOLD_SCORES["balanced"] < _THRESHOLD_SCORES["conservative"]


# ──────────────────────────────────────────────────────────────────────
# Unit tests — custom threshold registration
# ──────────────────────────────────────────────────────────────────────


def test_register_threshold_works():
    @register_threshold("test_always_send")
    class AlwaysSend:
        def should_send(self, result: SynthesisResult) -> bool:
            return True

    from hermes_cli.proactive_loop import _registered_thresholds
    assert "test_always_send" in _registered_thresholds
    assert _registered_thresholds["test_always_send"].should_send(
        SynthesisResult(False, None, "", 0.0, 0.0, 0.0)
    ) is True


def test_register_threshold_never_send():
    @register_threshold("test_never_send")
    class NeverSend:
        def should_send(self, result: SynthesisResult) -> bool:
            return False

    from hermes_cli.proactive_loop import _registered_thresholds
    assert _registered_thresholds["test_never_send"].should_send(
        SynthesisResult(True, "hi", "r", 1.0, 1.0, 1.0)
    ) is False


# ──────────────────────────────────────────────────────────────────────
# Unit tests — ProactiveLoop error handling
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_synthesis_returns_no_send_on_exception():
    """If synthesis fails, it must return a no-send result, never raise."""
    db = MagicMock()
    db.get_messages_since.side_effect = RuntimeError("db exploded")
    db.get_proactive_sent.return_value = []
    cfg = MagicMock()
    cfg.get.return_value = "conservative"

    loop = ProactiveLoop(session_db=db, config=cfg)
    result = await loop.run_synthesis("session-1")

    assert result.should_send is False
    assert result.message is None
    # Either a synthesis error or no history — both are valid no-send paths
    assert not result.should_send


@pytest.mark.asyncio
async def test_run_synthesis_returns_no_send_when_no_history():
    """Empty history window → no send."""
    db = MagicMock()
    db.get_messages_since.return_value = []
    db.get_proactive_sent.return_value = []
    cfg = MagicMock()
    cfg.get.return_value = "conservative"

    loop = ProactiveLoop(session_db=db, config=cfg)
    result = await loop.run_synthesis("session-empty")

    assert result.should_send is False
    assert "no history" in result.reasoning


# ──────────────────────────────────────────────────────────────────────
# Integration-style test — full synthesis path with mock LLM
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_synthesis_conservative_above_threshold():
    """High novelty + relevance → sends with conservative threshold."""
    db = MagicMock()
    db.get_messages_since.return_value = [
        {"role": "user", "content": "can you check the logs for errors?"},
        {"role": "assistant", "content": "On it, running the scan now."},
    ]
    db.get_proactive_sent.return_value = []
    cfg = MagicMock()
    cfg.get.return_value = "conservative"

    loop = ProactiveLoop(session_db=db, config=cfg)

    high_score_response = json.dumps({
        "should_send": True,
        "message": "Hey — finished the log scan. Found something: errors appear every 4h at :15. Almost certainly a cron job.",
        "novelty": 0.9,
        "relevance": 0.85,
        "reasoning": "Completed background task user asked about.",
        "candidates": ["log scan result"],
    })

    with patch.object(loop, "_call_synthesis_model", new=AsyncMock(return_value=high_score_response)):
        result = await loop.run_synthesis("session-logs")

    assert result.should_send is True
    assert result.message is not None
    assert "log" in result.message.lower() or "error" in result.message.lower()
    assert result.novelty_score == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_full_synthesis_below_threshold_does_not_send():
    """Low scores → no send even if model says should_send."""
    db = MagicMock()
    db.get_messages_since.return_value = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    db.get_proactive_sent.return_value = []
    cfg = MagicMock()
    cfg.get.return_value = "conservative"  # threshold = 0.75

    loop = ProactiveLoop(session_db=db, config=cfg)

    low_score_response = json.dumps({
        "should_send": True,  # model says yes — but scores are below threshold
        "message": "Just checking in!",
        "novelty": 0.2,
        "relevance": 0.3,
        "reasoning": "low novelty greeting",
        "candidates": [],
    })

    with patch.object(loop, "_call_synthesis_model", new=AsyncMock(return_value=low_score_response)):
        result = await loop.run_synthesis("session-low")

    # combined = 0.6*0.2 + 0.4*0.3 = 0.12 + 0.12 = 0.24 < 0.75
    assert result.should_send is False
    assert result.message is None
