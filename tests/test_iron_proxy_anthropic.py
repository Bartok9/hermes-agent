"""Hermetic tests for Anthropic native (x-api-key) per-provider support.

Verifies build_proxy_config emits the right header-match rules, that the
opt-in default preserves existing Bearer-only behavior, and that the
uncovered/blocked discovery drops Anthropic when --with-anthropic is set.
Mirrors ``tests/test_iron_proxy.py`` style.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent.proxy_sources import iron_proxy as ip


@pytest.fixture
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.endswith("_API_KEY"):
            monkeypatch.delenv(key, raising=False)
    return monkeypatch


def _bearer_mapping(env="OPENROUTER_API_KEY", hosts=("openrouter.ai",)):
    return ip.TokenMapping(
        proxy_token=ip.mint_proxy_token("t"),
        real_env_name=env,
        upstream_hosts=hosts,
    )


def _xapikey_mapping():
    return ip.TokenMapping(
        proxy_token=ip.mint_proxy_token("anthropic"),
        real_env_name="ANTHROPIC_API_KEY",
        upstream_hosts=("api.anthropic.com",),
        auth_header="x-api-key",
    )


# ---------------------------------------------------------------------------
# build_proxy_config: header matching
# ---------------------------------------------------------------------------


def test_build_config_emits_both_bearer_and_xapikey_rules(tmp_path):
    cfg = ip.build_proxy_config(
        mappings=[_bearer_mapping(), _xapikey_mapping()],
        ca_cert=tmp_path / "ca.crt",
        ca_key=tmp_path / "ca.key",
    )
    secrets = cfg["transforms"][1]["config"]["secrets"]
    headers = {tuple(r["replace"]["match_headers"]) for r in secrets}
    assert ("Authorization",) in headers
    assert ("x-api-key",) in headers

    # The x-api-key rule targets api.anthropic.com and does NOT match query.
    xrule = next(
        r for r in secrets
        if r["replace"]["match_headers"] == ["x-api-key"]
    )
    assert xrule["source"] == {"type": "env", "var": "ANTHROPIC_API_KEY"}
    assert {h["host"] for h in xrule["rules"]} == {"api.anthropic.com"}
    assert xrule["replace"]["match_query"] is False


def test_build_config_default_emits_only_bearer(tmp_path):
    """Default behavior (no x-api-key mapping) is byte-for-byte Bearer."""
    cfg = ip.build_proxy_config(
        mappings=[_bearer_mapping()],
        ca_cert=tmp_path / "ca.crt",
        ca_key=tmp_path / "ca.key",
    )
    secrets = cfg["transforms"][1]["config"]["secrets"]
    assert len(secrets) == 1
    assert secrets[0]["replace"]["match_headers"] == ["Authorization"]
    assert secrets[0]["replace"]["match_query"] is True


# ---------------------------------------------------------------------------
# discover_xapikey_mappings: opt-in gating
# ---------------------------------------------------------------------------


def test_discover_xapikey_off_by_default(clean_env):
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert ip.discover_xapikey_mappings(with_anthropic=False) == []


def test_discover_xapikey_on_opt_in(clean_env):
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    mappings = ip.discover_xapikey_mappings(with_anthropic=True)
    assert len(mappings) == 1
    m = mappings[0]
    assert m.real_env_name == "ANTHROPIC_API_KEY"
    assert m.upstream_hosts == ("api.anthropic.com",)
    assert m.auth_header == "x-api-key"


def test_discover_xapikey_absent_env_no_mapping(clean_env):
    # Opt-in but no ANTHROPIC_API_KEY set -> nothing minted.
    assert ip.discover_xapikey_mappings(with_anthropic=True) == []


# ---------------------------------------------------------------------------
# uncovered / blocked discovery respects opt-in
# ---------------------------------------------------------------------------


def test_uncovered_drops_anthropic_when_opted_in(clean_env):
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert "ANTHROPIC_API_KEY" in ip.discover_uncovered_providers(
        with_anthropic=False)
    assert "ANTHROPIC_API_KEY" not in ip.discover_uncovered_providers(
        with_anthropic=True)


def test_blocked_drops_anthropic_when_opted_in(clean_env):
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert "ANTHROPIC_API_KEY" in ip.discover_blocked_providers(
        with_anthropic=False)
    assert "ANTHROPIC_API_KEY" not in ip.discover_blocked_providers(
        with_anthropic=True)


# ---------------------------------------------------------------------------
# mappings.json round-trips auth_header
# ---------------------------------------------------------------------------


def test_mappings_roundtrip_preserves_auth_header(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    (home / "proxy").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    ip.write_mappings([_bearer_mapping(), _xapikey_mapping()])
    loaded = ip.load_mappings()
    by_env = {m.real_env_name: m for m in loaded}
    assert by_env["OPENROUTER_API_KEY"].auth_header == "Authorization"
    assert by_env["ANTHROPIC_API_KEY"].auth_header == "x-api-key"


def test_mappings_backcompat_defaults_authorization(tmp_path, monkeypatch):
    """Old mappings.json without auth_header loads as Authorization."""
    import json
    home = tmp_path / "hermes"
    (home / "proxy").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "proxy" / "mappings.json").write_text(json.dumps({
        "version": 1,
        "tokens": [{
            "proxy_token": "hermes-proxy-abc",
            "env_name": "OPENAI_API_KEY",
            "upstream_hosts": ["api.openai.com"],
        }],
    }), encoding="utf-8")
    loaded = ip.load_mappings()
    assert loaded[0].auth_header == "Authorization"
