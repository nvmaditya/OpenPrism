"""The model catalog (agent design §11.6)."""

from __future__ import annotations

import pytest

from prismtex.agent.budget import Price
from prismtex.agent.providers import ModelInfo, ModelUnavailable
from prismtex.agent.providers.catalog import CATALOG, PRICES_AS_OF, spec
from prismtex.agent.providers.fake import anthropic_like, fake_spec


def test_catalog_specs_match_the_capability_table() -> None:
    assert PRICES_AS_OF == "2026-11-24"
    assert set(CATALOG) == {
        "ollama:qwen3:4b-instruct-2507-q4_K_M",
        "ollama:llama3.1:8b",
        "anthropic:claude-haiku-5-5",
        "anthropic:claude-sonnet-5-5",
        "anthropic:claude-opus-5-5",
    }

    qwen = spec("ollama:qwen3:4b-instruct-2507-q4_K_M")
    assert (qwen.provider, qwen.model) == ("ollama", "qwen3:4b-instruct-2507-q4_K_M")
    assert (qwen.context_window, qwen.num_ctx, qwen.input_cap, qwen.max_output) == (
        16_384,
        16_384,
        None,
        1_024,
    )
    assert qwen.default_profile == "guided" and qwen.memory == "records" and qwen.native_tools
    assert qwen.sampling == {"temperature": 0.7, "top_p": 0.8, "top_k": 20}
    assert (qwen.price, qwen.paid, qwen.effort, qwen.prompt_caching) == (None, False, None, False)
    assert qwen.quirks == {"recover_text_tool_calls"} and qwen.chars_per_token == 3.0

    llama = spec("ollama:llama3.1:8b")
    assert (llama.context_window, llama.num_ctx, llama.max_output) == (12_288, 12_288, 2_048)
    assert llama.default_profile == "guided" and llama.native_tools
    assert llama.quirks == {"recover_text_tool_calls", "unknown_model"}

    haiku = spec("anthropic:claude-haiku-5-5")
    assert (haiku.provider, haiku.model) == ("anthropic", "claude-haiku-5-5")
    assert (haiku.context_window, haiku.input_cap, haiku.max_output) == (1_000_000, 100_000, 16_000)
    assert haiku.price == Price(0.10, 0.50, 0.125, 0.01) and haiku.paid
    assert haiku.sampling == {} and haiku.effort == "medium" and haiku.num_ctx is None
    assert haiku.memory == "transcript" and haiku.default_profile == "agentic"
    assert haiku.prompt_caching and haiku.native_tools and haiku.quirks == frozenset()
    assert spec("anthropic:claude-sonnet-5-5").price == Price(2.00, 10.00, 2.50, 0.10)
    assert spec("anthropic:claude-opus-5-5").price == Price(4.00, 20.00, 5.00, 0.20)
    # Sampling is never sent to Anthropic.
    assert all(not s.sampling for s in CATALOG.values() if s.provider == "anthropic")


def test_uncatalogued_refs_use_their_provider_fallback_row() -> None:
    other = spec("ollama:mistral:7b")
    assert (other.model, other.context_window, other.num_ctx, other.max_output) == (
        "mistral:7b",
        8_192,
        8_192,
        2_048,
    )
    assert not other.native_tools  # read from /api/show
    assert (other.default_profile, other.memory, other.sampling, other.paid) == (
        "guided",
        "records",
        {},
        False,
    )

    gpt = spec("openai:gpt-test")
    assert (gpt.provider, gpt.model, gpt.context_window, gpt.max_output) == (
        "openai_compatible",
        "gpt-test",
        32_768,
        8_192,
    )
    assert gpt.native_tools and gpt.default_profile == "agentic" and gpt.memory == "records"
    assert gpt.paid and gpt.price is None and gpt.sampling == {}
    assert spec("openrouter:vendor/model").paid and not spec("vllm:qwen").paid
    assert spec("lmstudio:qwen").quirks == {"recover_text_tool_calls"}
    assert spec("vllm:qwen").quirks == frozenset()


@pytest.mark.parametrize(
    "ref",
    [
        "groq:llama-3.3-70b",
        "anthropic:claude-2",
        "fake:nope",
        "ollama:",
        "openai: ",
        "main.tex",
        "",
    ],
)
def test_unknown_refs_raise_unknown_model(ref: str) -> None:
    with pytest.raises(ModelUnavailable) as caught:
        spec(ref)
    assert caught.value.code == "unknown_model" and caught.value.env_var is None


def test_fake_specs_resolve_through_the_catalog_for_replay() -> None:
    assert spec("fake:m") is fake_spec
    assert spec("fake:anthropic-like") is anthropic_like
    assert spec("fake:scripted").ref == "fake:scripted"
    assert (fake_spec.default_profile, fake_spec.memory, fake_spec.native_tools) == (
        "agentic",
        "records",
        True,
    )
    assert (fake_spec.context_window, fake_spec.max_output, fake_spec.paid) == (
        32_768,
        1_024,
        False,
    )
    assert (anthropic_like.memory, anthropic_like.paid) == ("transcript", True)
    assert (anthropic_like.context_window, anthropic_like.input_cap, anthropic_like.max_output) == (
        1_000_000,
        100_000,
        16_000,
    )
    assert anthropic_like.price == spec("anthropic:claude-haiku-5-5").price
    info = ModelInfo.from_spec(anthropic_like, available=False)
    assert (info.ref, info.context_window, info.price, info.available) == (
        "fake:anthropic-like",
        1_000_000,
        anthropic_like.price,
        False,
    )
