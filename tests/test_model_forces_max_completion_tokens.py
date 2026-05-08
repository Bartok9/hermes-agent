"""Tests for utils.model_forces_max_completion_tokens (issue #13901)."""

from utils import model_forces_max_completion_tokens


def test_model_forces_max_completion_tokens():
    cases = [
        # Direct matches
        ("gpt-4o", True),
        ("gpt-4o-mini", True),
        ("gpt-4o-2024-11-20", True),
        ("gpt-4.1", True),
        ("gpt-4.1-mini", True),
        ("gpt-5", True),
        ("gpt-5.4", True),
        ("gpt-5.4-mini", True),
        ("o1", True),
        ("o1-mini", True),
        ("o1-preview", True),
        ("o3", True),
        ("o3-mini", True),
        ("o4", True),
        ("o4-mini", True),
        # Vendor-prefixed (OpenRouter style)
        ("openai/gpt-5.4", True),
        ("openai/gpt-4o-mini", True),
        ("openai/o3-mini", True),
        # Legacy / non-OpenAI models should NOT match
        ("gpt-3.5-turbo", False),
        ("gpt-4", False),
        ("gpt-4-turbo", False),
        ("llama3", False),
        ("llama3:70b", False),
        ("claude-3-opus", False),
        ("mistral-7b", False),
        ("qwen3:8b", False),
        ("deepseek-r1:14b", False),
        # Edge cases
        ("", False),
        (None, False),
        ("   ", False),
    ]
    for model, expected in cases:
        result = model_forces_max_completion_tokens(model)
        assert result is expected, (
            f"model_forces_max_completion_tokens({model!r}) returned {result}, "
            f"expected {expected}"
        )
