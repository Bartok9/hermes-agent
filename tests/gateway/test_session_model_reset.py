"""Tests that /new (and its /reset alias) clears session-scoped overrides."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    runner._pending_model_notes = {}
    runner._background_tasks = set()

    session_key = build_session_key(_make_source())
    session_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.reset_session.return_value = session_entry
    runner.session_store._entries = {session_key: session_entry}
    runner.session_store._generate_session_key.return_value = session_key
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._agent_cache_lock = None  # disables _evict_cached_agent lock path
    runner._is_user_authorized = lambda _source: True
    runner._format_session_info = lambda: ""

    return runner


@pytest.mark.asyncio
async def test_new_command_clears_session_model_override():
    """/new must remove the session-scoped model override for that session."""
    runner = _make_runner()
    session_key = build_session_key(_make_source())

    # Simulate a prior /model switch stored as a session override
    runner._session_model_overrides[session_key] = {
        "model": "gpt-4o",
        "provider": "openai",
        "api_key": "***",
        "base_url": "",
        "api_mode": "openai",
    }
    runner._session_reasoning_overrides[session_key] = {"enabled": True, "effort": "high"}
    runner._pending_model_notes[session_key] = "[Note: switched to gpt-4o.]"

    await runner._handle_reset_command(_make_event("/new"))

    assert session_key not in runner._session_model_overrides
    assert session_key not in runner._session_reasoning_overrides
    assert session_key not in runner._pending_model_notes


@pytest.mark.asyncio
async def test_new_command_no_override_is_noop():
    """/new with no prior model override must not raise."""
    runner = _make_runner()
    session_key = build_session_key(_make_source())

    assert session_key not in runner._session_model_overrides
    assert session_key not in runner._session_reasoning_overrides

    await runner._handle_reset_command(_make_event("/new"))

    assert session_key not in runner._session_model_overrides
    assert session_key not in runner._session_reasoning_overrides


@pytest.mark.asyncio
async def test_new_command_only_clears_own_session():
    """/new must only clear the override for the session that triggered it."""
    runner = _make_runner()
    session_key = build_session_key(_make_source())
    other_key = "other_session_key"

    runner._session_model_overrides[session_key] = {
        "model": "gpt-4o",
        "provider": "openai",
        "api_key": "sk-test",
        "base_url": "",
        "api_mode": "openai",
    }
    runner._session_model_overrides[other_key] = {
        "model": "claude-sonnet-4-6",
        "provider": "anthropic",
        "api_key": "***",
        "base_url": "",
        "api_mode": "anthropic",
    }
    runner._session_reasoning_overrides[session_key] = {"enabled": True, "effort": "high"}
    runner._session_reasoning_overrides[other_key] = {"enabled": True, "effort": "low"}
    runner._pending_model_notes[session_key] = "[Note: switched to gpt-4o.]"
    runner._pending_model_notes[other_key] = "[Note: switched to claude-sonnet-4-6.]"

    await runner._handle_reset_command(_make_event("/new"))

    assert session_key not in runner._session_model_overrides
    assert other_key in runner._session_model_overrides
    assert session_key not in runner._session_reasoning_overrides
    assert other_key in runner._session_reasoning_overrides
    assert session_key not in runner._pending_model_notes
    assert other_key in runner._pending_model_notes


@pytest.mark.asyncio
async def test_new_command_completes_when_pinned_model_invalid_during_cleanup():
    """Regression for #6426.

    When a session was pinned to a model that the user has since removed
    from ``config.yaml`` / ``.env``, ``shutdown_memory_provider`` (called
    on /new before reset_session) makes a summarisation LLM call to that
    invalid model and the SDK raises a ``BadRequestError``-class
    exception.  Previously this exception was silently swallowed by a
    broad ``except Exception: pass`` in ``_cleanup_agent_resources`` and,
    when combined with the upstream timeout/deadlock surface, left the
    user with no signal that their long-term memory wasn't summarized.

    This test confirms two guarantees:

    1. The reset still completes (returns a reply, the session_store
       reset_session call still fires).
    2. The user is told *why* the summary was skipped — the warning text
       is prepended to the reset response so the user knows the previous
       model is no longer available and can fix their config.
    """
    import threading

    runner = _make_runner()
    runner._agent_cache_lock = threading.Lock()

    # Synthetic exception class whose ``__name__`` matches what the
    # OpenAI / Anthropic SDKs actually raise on an unknown model.  We
    # don't import the real SDKs — ``_cleanup_agent_resources`` matches
    # by class-name string for exactly that reason (see the
    # ``_PINNED_MODEL_EXC_NAMES`` frozenset).
    class BadRequestError(Exception):
        pass

    mock_agent = MagicMock()

    def _raise_invalid_model(*args, **kwargs):
        raise BadRequestError("Invalid model: gpt-4o-deleted")

    mock_agent.shutdown_memory_provider = MagicMock(side_effect=_raise_invalid_model)
    mock_agent._session_messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi back"},
    ]
    # ``close()`` is exercised after the memory-provider step in the
    # cleanup function — make it a no-op so we test the memory-shutdown
    # path in isolation.
    mock_agent.close = MagicMock()

    session_key = build_session_key(_make_source())
    runner._agent_cache = {session_key: mock_agent}

    reply = await runner._handle_reset_command(_make_event("/new"))

    # Reply is an EphemeralReply (str subclass) so we can substring-check.
    text = str(reply)
    assert "Could not summarize" in text, (
        f"Reset response should include the user-visible warning explaining "
        f"why long-term memory wasn't summarized, got: {text!r}"
    )
    assert "no longer available" in text, (
        f"Warning should explain the pinned model is no longer available, "
        f"got: {text!r}"
    )
    assert "Session reset" in text or "New session" in text, (
        f"Reset must still surface the normal reset header so the user "
        f"sees the operation completed, got: {text!r}"
    )

    # The cleanup call was actually attempted (not skipped) — the broken
    # memory provider was invoked exactly once.
    mock_agent.shutdown_memory_provider.assert_called_once_with(
        mock_agent._session_messages
    )

    # The reset itself still happened — session_store.reset_session was
    # called for this session, so the user is no longer pinned to the
    # broken model on the next message.
    runner.session_store.reset_session.assert_called_once_with(session_key)


@pytest.mark.asyncio
async def test_new_command_silent_when_cleanup_succeeds():
    """Companion to the #6426 regression: when cleanup succeeds the reset
    response must NOT include the warning prefix.  Otherwise every /new
    on a healthy session would scare the user.
    """
    import threading

    runner = _make_runner()
    runner._agent_cache_lock = threading.Lock()

    mock_agent = MagicMock()
    mock_agent.shutdown_memory_provider = MagicMock(return_value=None)
    mock_agent._session_messages = []
    mock_agent.close = MagicMock()

    session_key = build_session_key(_make_source())
    runner._agent_cache = {session_key: mock_agent}

    reply = await runner._handle_reset_command(_make_event("/new"))
    text = str(reply)

    assert "Could not summarize" not in text
    assert "no longer available" not in text
    assert "Session reset" in text or "New session" in text


@pytest.mark.asyncio
async def test_new_command_silent_for_non_model_cleanup_errors():
    """Companion: unrelated cleanup failures (network blip, plugin bug)
    must NOT produce the model-specific warning — those stay in debug
    logs only, matching the prior best-effort cleanup contract.
    """
    import threading

    runner = _make_runner()
    runner._agent_cache_lock = threading.Lock()

    mock_agent = MagicMock()
    mock_agent.shutdown_memory_provider = MagicMock(
        side_effect=RuntimeError("plugin internal failure")
    )
    mock_agent._session_messages = []
    mock_agent.close = MagicMock()

    session_key = build_session_key(_make_source())
    runner._agent_cache = {session_key: mock_agent}

    reply = await runner._handle_reset_command(_make_event("/new"))
    text = str(reply)

    # No model-specific warning for generic exceptions
    assert "Could not summarize" not in text
    assert "no longer available" not in text
    # But the reset still completes
    assert "Session reset" in text or "New session" in text
