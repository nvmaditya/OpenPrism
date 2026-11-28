"""The model catalog: capabilities and prices per model ref (agent design §11.6).

`spec(ref)` gives the row for a catalogued model, or the fallback row of its Provider:
other `ollama:*` models and every OpenAI-compatible `<preset>:<model>`. Settings such as
the pinned `num_ctx` or an OpenAI-compatible window and price are applied by the
resolver on top of these rows.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from prismtex.agent.budget import Price
from prismtex.agent.providers import ModelSpec, ModelUnavailable

__all__ = [
    "ANTHROPIC_LIKE",
    "CATALOG",
    "FAKE_SPEC",
    "FAKE_SPECS",
    "OLLAMA_DEFAULT",
    "PRICES_AS_OF",
    "RECOVER_TEXT_TOOL_CALLS",
    "SCRIPTED_SPEC",
    "UNKNOWN_MODEL",
    "spec",
]

# Anthropic prices, USD per million tokens. Cache writes are the 5-minute rate. Haiku
# 5.5 prices hold for prompts up to 100K tokens, which its input cap keeps to.
PRICES_AS_OF = "2026-11-24"
_HAIKU_PRICE = Price(input=0.10, output=0.50, cache_write=0.125, cache_read=0.01)
_SONNET_PRICE = Price(input=2.00, output=10.00, cache_write=2.50, cache_read=0.10)
_OPUS_PRICE = Price(input=4.00, output=20.00, cache_write=5.00, cache_read=0.20)

RECOVER_TEXT_TOOL_CALLS = "recover_text_tool_calls"
UNKNOWN_MODEL = "unknown_model"

OLLAMA_DEFAULT = "ollama:qwen3:4b-instruct-2507-q4_K_M"

_OLLAMA_FALLBACK_WINDOW = 8_192
_OPENAI_COMPATIBLE_WINDOW = 32_768  # PRISMTEX_OPENAI_CONTEXT, applied by the resolver
# OpenAI-compatible presets (agent design §11.3): whether each is paid, and its quirks.
_PRESET_PAID: Mapping[str, bool] = {
    "openai": True,
    "openrouter": True,
    "vllm": False,
    "lmstudio": False,
}
_PRESET_QUIRKS: Mapping[str, frozenset[str]] = {"lmstudio": frozenset({RECOVER_TEXT_TOOL_CALLS})}


def _ollama(
    model: str,
    *,
    label: str,
    window: int,
    max_output: int,
    native_tools: bool,
    sampling: Mapping[str, float],
    quirks: frozenset[str] = frozenset(),
) -> ModelSpec:
    return ModelSpec(
        ref=f"ollama:{model}",
        provider="ollama",
        model=model,
        label=label,
        context_window=window,
        input_cap=None,
        max_output=max_output,
        native_tools=native_tools,
        memory="records",
        prompt_caching=False,
        default_profile="guided",
        sampling=sampling,
        effort=None,
        num_ctx=window,
        price=None,
        paid=False,
        quirks=frozenset({RECOVER_TEXT_TOOL_CALLS}) | quirks,
    )


def _claude(model: str, *, label: str, price: Price) -> ModelSpec:
    return ModelSpec(
        ref=f"anthropic:{model}",
        provider="anthropic",
        model=model,
        label=label,
        context_window=1_000_000,
        input_cap=100_000,
        max_output=16_000,
        native_tools=True,
        memory="transcript",
        prompt_caching=True,
        default_profile="agentic",
        sampling={},  # never sent to Anthropic
        effort="medium",
        num_ctx=None,
        price=price,
        paid=True,
    )


def _openai_compatible(preset: str, model: str) -> ModelSpec:
    window = _OPENAI_COMPATIBLE_WINDOW
    native_tools = True
    return ModelSpec(
        ref=f"{preset}:{model}",
        provider="openai_compatible",
        model=model,
        label=f"{model} ({preset})",
        context_window=window,
        input_cap=None,
        max_output=8_192,
        native_tools=native_tools,
        memory="records",
        prompt_caching=False,
        default_profile="agentic" if native_tools and window >= 32_768 else "guided",
        sampling={},
        effort=None,
        num_ctx=None,
        price=None,
        paid=_PRESET_PAID[preset],
        quirks=_PRESET_QUIRKS.get(preset, frozenset()),
    )


def _fake(ref: str, *, label: str) -> ModelSpec:
    return ModelSpec(
        ref=ref,
        provider="fake",
        model=ref.removeprefix("fake:"),
        label=label,
        context_window=32_768,
        input_cap=None,
        max_output=1_024,
        native_tools=True,
        memory="records",
        prompt_caching=False,
        default_profile="agentic",
        sampling={},
        effort=None,
        num_ctx=None,
        price=None,
        paid=False,
    )


_ROWS = (
    _ollama(
        "qwen3:4b-instruct-2507-q4_K_M",
        label="Qwen3 4B Instruct 2507 (Ollama)",
        window=16_384,
        max_output=1_024,  # agentic Steps; a guided rewrite reserves up to 4,096
        native_tools=True,
        sampling={"temperature": 0.7, "top_p": 0.8, "top_k": 20},  # the Qwen model card
    ),
    _ollama(
        "llama3.1:8b",
        label="Llama 3.1 8B (Ollama)",
        window=12_288,
        max_output=2_048,
        native_tools=True,
        sampling={"temperature": 0.6, "top_p": 0.9},  # the Llama 3.1 model card
        quirks=frozenset({UNKNOWN_MODEL}),  # usable, not a supported default
    ),
    _claude("claude-haiku-5-5", label="Claude Haiku 5.5", price=_HAIKU_PRICE),
    _claude("claude-sonnet-5-5", label="Claude Sonnet 5.5", price=_SONNET_PRICE),
    _claude("claude-opus-5-5", label="Claude Opus 5.5", price=_OPUS_PRICE),
)
CATALOG: Mapping[str, ModelSpec] = MappingProxyType({row.ref: row for row in _ROWS})

# Fakes for tests, evals and the end-to-end lane (agent design §11.5).
FAKE_SPEC = _fake("fake:m", label="Fake model")
SCRIPTED_SPEC = _fake("fake:scripted", label="Scripted fake model")
ANTHROPIC_LIKE = ModelSpec(
    ref="fake:anthropic-like",
    provider="fake",
    model="anthropic-like",
    label="Fake model with Anthropic-like memory and prices",
    context_window=1_000_000,
    input_cap=100_000,
    max_output=16_000,
    native_tools=True,
    memory="transcript",
    prompt_caching=True,
    default_profile="agentic",
    sampling={},
    effort="medium",
    num_ctx=None,
    price=_HAIKU_PRICE,
    paid=True,
)
FAKE_SPECS: Mapping[str, ModelSpec] = MappingProxyType(
    {row.ref: row for row in (FAKE_SPEC, SCRIPTED_SPEC, ANTHROPIC_LIKE)}
)


def spec(ref: str) -> ModelSpec:
    """The capabilities of `ref`; raises `ModelUnavailable("unknown_model")`."""
    known = CATALOG.get(ref) or FAKE_SPECS.get(ref)
    if known is not None:
        return known
    prefix, _, model = ref.partition(":")
    if model and not model.isspace():
        if prefix == "ollama":
            return _ollama(
                model,
                label=f"{model} (Ollama)",
                window=_OLLAMA_FALLBACK_WINDOW,
                max_output=2_048,
                native_tools=False,  # read from /api/show by the resolver
                sampling={},
                quirks=frozenset({UNKNOWN_MODEL}),
            )
        if prefix in _PRESET_PAID:
            return _openai_compatible(prefix, model)
    raise ModelUnavailable(
        "unknown_model",
        f"Unknown model {ref!r}. Use ollama:<model>, anthropic:<model>, or "
        "<preset>:<model> with the preset openai, openrouter, vllm or lmstudio.",
    )
