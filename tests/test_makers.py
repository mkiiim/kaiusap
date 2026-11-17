"""`sessions`' `context.model_maker`, who made the model of the last reply, from the shared table; null when
kaiusap can't tell, never the harness's maker."""
import pytest

from kaiusap.adapters import context_use, model_maker


@pytest.mark.parametrize("model,route,maker", [
    ("claude-opus-5-5", None, "anthropic"),
    ("us.anthropic.claude-sonnet-5-5-v1:0", "amazon-bedrock", "anthropic"),
    ("gpt-6.1-sol", None, "openai"),
    ("o3-mini", None, "openai"),
    ("codex-mini-latest", None, "openai"),
    ("gemini-4-carbon", "google", "google"),
    ("google/gemini-3.8-flash", "openrouter", "google"),
    ("big-pickle", "opencode", "opencode"),
    ("grok-5", "opencode", "xai"),
    ("glm-5", "opencode", "zhipu"),
    ("my-finetune", "google", "google"),      # not in the table: a route that is itself a maker
    ("my-finetune", "opencode", None),        # OpenCode's own route serves other makers' models
    ("my-finetune", "openrouter", None),
    ("ollama-local", None, None),
    ("gpto", None, None),                     # a prefix counts only up to a separator
    ("o3x", None, None),
    (None, "google", "google"),
    (None, None, None),
])
def test_model_maker(model, route, maker):
    assert model_maker(model, route) == maker


def test_context_carries_the_model_maker():
    assert context_use(10, None, "claude-opus-5-5", "2026-11-11T00:00:00Z")["model_maker"] == "anthropic"
    assert context_use(10, 100, "mystery", None)["model_maker"] is None
