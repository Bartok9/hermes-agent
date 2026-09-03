"""Regression tests for Vertex in PROVIDER_REGISTRY (#61852).

Auxiliary tasks go through resolve_provider_client, which needs
PROVIDER_REGISTRY auth_type=vertex plus alias normalization.
"""

from unittest.mock import MagicMock, patch


class TestVertexProviderRegistry:
    def test_vertex_in_registry(self):
        from hermes_cli.auth import PROVIDER_REGISTRY

        assert "vertex" in PROVIDER_REGISTRY

    def test_vertex_auth_type(self):
        from hermes_cli.auth import PROVIDER_REGISTRY

        assert PROVIDER_REGISTRY["vertex"].auth_type == "vertex"

    def test_vertex_has_no_api_key_env_vars(self):
        from hermes_cli.auth import PROVIDER_REGISTRY

        assert PROVIDER_REGISTRY["vertex"].api_key_env_vars == ()


class TestResolveProviderClientVertexPath:
    def test_resolve_vertex_uses_adapter_and_returns_client(self):
        from agent.auxiliary_client import resolve_provider_client

        fake_client = MagicMock(name="OpenAIClient")
        with (
            patch(
                "agent.vertex_adapter.has_vertex_credentials",
                return_value=True,
            ),
            patch(
                "agent.vertex_adapter.get_vertex_config",
                return_value=(
                    "fake-token",
                    "https://us-central1-aiplatform.googleapis.com/v1/"
                    "projects/p/locations/us-central1/endpoints/openapi",
                ),
            ),
            patch("openai.OpenAI", return_value=fake_client) as openai_ctor,
        ):
            client, model = resolve_provider_client(
                "vertex", "google/gemini-3-flash-preview"
            )

        assert client is fake_client
        assert model
        openai_ctor.assert_called_once()
        call_kwargs = openai_ctor.call_args.kwargs
        assert call_kwargs["api_key"] == "fake-token"
        assert "aiplatform.googleapis.com" in call_kwargs["base_url"]

    def test_resolve_vertex_without_credentials_returns_none(self):
        from agent.auxiliary_client import resolve_provider_client

        with patch(
            "agent.vertex_adapter.has_vertex_credentials",
            return_value=False,
        ):
            client, model = resolve_provider_client(
                "vertex", "google/gemini-3-flash-preview"
            )

        assert client is None
        assert model is None


class TestVertexAuxAliases:
    def test_normalize_aliases(self):
        from agent.auxiliary_client import _normalize_aux_provider

        for alias in ("google-vertex", "vertex-ai", "gcp-vertex", "vertexai", "Vertex", "VERTEX"):
            assert _normalize_aux_provider(alias) == "vertex"
