"""Regression tests for #34171 — xai-oauth auxiliary client must use
chat.completions, NOT the Responses API.

xAI OAuth tokens are scoped to /v1/chat/completions only. Wrapping the
OpenAI client in CodexAuxiliaryClient (which translates calls to
/v1/responses) caused every auxiliary task (compression, vision,
web_extract) to fail with HTTP 403.
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest


def test_xai_oauth_aux_returns_plain_openai_client_not_codex_wrapper():
    """The fix: _build_xai_oauth_aux_client returns a plain OpenAI client
    that hits /v1/chat/completions, NOT a CodexAuxiliaryClient that would
    route to /v1/responses (where xAI OAuth tokens are not authorized)."""
    from agent.auxiliary_client import (
        _build_xai_oauth_aux_client,
        CodexAuxiliaryClient,
    )
    from openai import OpenAI

    with patch(
        "agent.auxiliary_client._resolve_xai_oauth_for_aux",
        return_value=("xai-oauth-token-fake", "https://api.x.ai/v1"),
    ):
        client, model = _build_xai_oauth_aux_client("grok-4.3")

    assert client is not None
    assert model == "grok-4.3"
    # NOT wrapped in CodexAuxiliaryClient — that's the #34171 bug.
    assert not isinstance(client, CodexAuxiliaryClient), (
        "xai-oauth aux client must be a plain OpenAI client (chat.completions), "
        "not a CodexAuxiliaryClient (responses). See #34171."
    )
    # IS an OpenAI client.
    assert isinstance(client, OpenAI)


def test_xai_oauth_aux_returns_none_when_no_model():
    """Defensive: missing model returns (None, None) with a warning."""
    from agent.auxiliary_client import _build_xai_oauth_aux_client

    client, model = _build_xai_oauth_aux_client("")
    assert client is None
    assert model is None


def test_xai_oauth_aux_returns_none_when_unauthenticated():
    """When xAI OAuth resolution fails, return (None, None) cleanly."""
    from agent.auxiliary_client import _build_xai_oauth_aux_client

    with patch(
        "agent.auxiliary_client._resolve_xai_oauth_for_aux",
        return_value=None,
    ):
        client, model = _build_xai_oauth_aux_client("grok-4.3")
    assert client is None
    assert model is None


def test_xai_oauth_aux_uses_correct_base_url():
    """The plain OpenAI client must hit the xAI base URL, not OpenAI's."""
    from agent.auxiliary_client import _build_xai_oauth_aux_client

    expected_base = "https://api.x.ai/v1"
    with patch(
        "agent.auxiliary_client._resolve_xai_oauth_for_aux",
        return_value=("xai-token", expected_base),
    ):
        client, _ = _build_xai_oauth_aux_client("grok-4.3")
    # OpenAI 1.x stores base_url on the client. The exact attribute name has
    # varied across SDK versions; check the most likely candidates.
    base = getattr(client, "base_url", None) or getattr(client, "_base_url", None)
    assert base is not None
    # OpenAI SDK normalizes base_url to URL object → str() it.
    assert "x.ai" in str(base)
