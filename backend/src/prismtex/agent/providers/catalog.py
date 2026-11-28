"""The model catalog and the Provider registry (agent design §11.3, §11.6, §11.7).

`spec(ref)` gives the row for a catalogued model, or the fallback row of its Provider:
other `ollama:*` models and every OpenAI-compatible `<preset>:<model>`. `PRESETS`
describes the OpenAI-compatible endpoints.

`ProviderRegistry` is the production `ModelResolver`. It builds the adapter for a model
ref from `Settings`, applying the pinned `num_ctx` and the OpenAI-compatible window and
price on top of these rows; picks the default model; and reports health. Only `health`
with `probe=True` makes a request, and only to Ollama.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal

import httpx

from prismtex.agent.budget import LEDGER_ENV_VAR, Price, configured_ledger_path
from prismtex.agent.providers import (
    HealthIssue,
    HealthReport,
    ModelInfo,
    ModelPort,
    ModelResolver,
    ModelSpec,
    ModelUnavailable,
    ProviderError,
    ProviderHealth,
)
from prismtex.agent.providers.ollama import OllamaModel, list_models, show_capabilities
from prismtex.settings import Settings

__all__ = [
    "ANTHROPIC_LIKE",
    "CATALOG",
    "DEFAULT_ANTHROPIC",
    "FAKE_SPEC",
    "FAKE_SPECS",
    "HTTP_TIMEOUT",
    "OLLAMA_DEFAULT",
    "PRESETS",
    "PRICES_AS_OF",
    "RECOVER_TEXT_TOOL_CALLS",
    "SCRIPTED_SPEC",
    "UNKNOWN_MODEL",
    "Preset",
    "PresetName",
    "ProviderRegistry",
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
# The Anthropic default once a key is set; Haiku and Sonnet 5.5 are listed beside it.
DEFAULT_ANTHROPIC = "anthropic:claude-opus-5-5"

_OLLAMA_FALLBACK_WINDOW = 8_192
_OPENAI_COMPATIBLE_WINDOW = 32_768  # PRISMTEX_OPENAI_CONTEXT, applied by the registry
_AGENTIC_MIN_WINDOW = 32_768  # an OpenAI-compatible model below this runs guided

PresetName = Literal["openai", "openrouter", "vllm", "lmstudio"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Preset:
    """One kind of OpenAI-compatible endpoint (agent design §11.3).

    - `base_url` None: `PRISMTEX_OPENAI_BASE_URL` is required. `base_url_configurable`:
      that variable may set the URL; otherwise the key goes only to `base_url`.
    - The key is always `Settings.openai_api_key` (`PRISMTEX_OPENAI_API_KEY`);
      `key_required` False sends "unused" when it is unset.
    - `paid` models need a price and the cost ledger.
    - `parallel_tool_calls`: the endpoint accepts `parallel_tool_calls`, sent as False.
    """

    name: PresetName
    base_url: str | None
    base_url_configurable: bool
    key_required: bool
    paid: bool
    max_tokens_param: Literal["max_completion_tokens", "max_tokens"]
    parallel_tool_calls: bool
    quirks: frozenset[str] = frozenset()


# No Groq preset. `vllm` serves any other OpenAI-compatible endpoint; a price meters it.
PRESETS: Mapping[str, Preset] = MappingProxyType(
    {
        "openai": Preset(
            name="openai",
            base_url="https://api.openai.com/v1",
            base_url_configurable=False,
            key_required=True,
            paid=True,
            max_tokens_param="max_completion_tokens",
            parallel_tool_calls=True,
        ),
        "openrouter": Preset(
            name="openrouter",
            base_url="https://openrouter.ai/api/v1",
            base_url_configurable=False,
            key_required=True,
            paid=True,
            max_tokens_param="max_tokens",
            parallel_tool_calls=True,
        ),
        "vllm": Preset(
            name="vllm",
            base_url=None,
            base_url_configurable=True,
            key_required=False,
            paid=False,
            max_tokens_param="max_tokens",
            parallel_tool_calls=False,
        ),
        "lmstudio": Preset(
            name="lmstudio",
            base_url="http://127.0.0.1:1234/v1",
            base_url_configurable=True,
            key_required=False,
            paid=False,
            max_tokens_param="max_tokens",
            parallel_tool_calls=False,
            quirks=frozenset({RECOVER_TEXT_TOOL_CALLS}),
        ),
    }
)


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


def _openai_compatible(preset: Preset, model: str, *, window: int) -> ModelSpec:
    native_tools = True
    return ModelSpec(
        ref=f"{preset.name}:{model}",
        provider="openai_compatible",
        model=model,
        label=f"{model} ({preset.name})",
        context_window=window,
        input_cap=None,
        max_output=8_192,
        native_tools=native_tools,
        memory="records",
        prompt_caching=False,
        default_profile="agentic" if native_tools and window >= _AGENTIC_MIN_WINDOW else "guided",
        sampling={},  # no preset lists sampling parameters
        effort=None,
        num_ctx=None,
        price=None,
        paid=preset.paid,
        quirks=preset.quirks,
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
        preset = PRESETS.get(prefix)
        if preset is not None:
            return _openai_compatible(preset, model, window=_OPENAI_COMPATIBLE_WINDOW)
    raise ModelUnavailable(
        "unknown_model",
        f"Unknown model {ref!r}. Use ollama:<model>, anthropic:<model>, or "
        "<preset>:<model> with the preset openai, openrouter, vllm or lmstudio.",
    )


# -- the registry ----------------------------------------------------------------------

# Connect within about 10 s and allow about 120 s between chunks: a cold Ollama model can
# take over a minute to load before its first byte, and httpx's default is 5 s.
HTTP_TIMEOUT: Final = httpx.Timeout(120.0, connect=10.0)

_SCRIPTED_REF = SCRIPTED_SPEC.ref
_DEFAULT_SETTING = "PRISMTEX_DEFAULT_MODEL"
_OLLAMA_URL_SETTING = "PRISMTEX_OLLAMA_URL"
_ANTHROPIC_KEY_SETTING = "ANTHROPIC_API_KEY"
_ANTHROPIC_KEY_HINT = "Set ANTHROPIC_API_KEY (or CLAUDE_API_KEY) to use Claude."
_OPENAI_PRESET_SETTING = "PRISMTEX_OPENAI_PRESET"
_OPENAI_MODEL_SETTING = "PRISMTEX_OPENAI_MODEL"
_OPENAI_KEY_SETTING = "PRISMTEX_OPENAI_API_KEY"
_OPENAI_URL_SETTING = "PRISMTEX_OPENAI_BASE_URL"
_OPENAI_PRICE_SETTING = "PRISMTEX_OPENAI_PRICE"
_FAKE_SCRIPT_SETTING = "PRISMTEX_FAKE_SCRIPT"
_PRICE_HINT = f'Set {_OPENAI_PRICE_SETTING} to "input,output" in USD per million tokens.'
_NO_MODEL = (
    "No model is available. Start Ollama and run: ollama pull {ollama}; or set "
    "ANTHROPIC_API_KEY (or CLAUDE_API_KEY); or set PRISMTEX_OPENAI_PRESET and "
    "PRISMTEX_OPENAI_MODEL. PRISMTEX_DEFAULT_MODEL sets the model for every Project."
)


@dataclass(frozen=True, slots=True)
class _OllamaProbe:
    """What the last probe of Ollama saw; `detail` says why it was unreachable."""

    reachable: bool
    models: frozenset[str]
    capabilities: Mapping[str, frozenset[str]]
    detail: str | None


class ProviderRegistry(ModelResolver):
    """Builds the adapter for a model ref from `Settings` (agent design §11.3, §11.6, §11.7).

    - `resolve(ref, project_default)` serves `ref`, else the Project's default, else
      `Settings.default_model`, else the first available of: the Ollama catalog default
      if the last probe saw it pulled, `anthropic:claude-opus-5-5` if an Anthropic key is
      set, and the configured OpenAI-compatible `<preset>:<model>`. It raises
      `ModelUnavailable`: `unknown_model` for a ref nobody serves, else
      `model_unavailable` naming the setting that would fix it. A paid model needs a
      known price (`PRISMTEX_OPENAI_PRICE`) and the cost ledger (`PRISMTEX_LEDGER_PATH`).
    - Only the configured OpenAI-compatible model resolves, so its key reaches only its
      preset's base URL. `fake:scripted` exists only while `Settings.fake_script` names
      a script file.
    - `catalog()` and `health()` make no request. `health(probe=True)` asks Ollama for
      its models (`/api/tags`, and `/api/show` for those the catalog lacks) and keeps the
      answer for `resolve` and `catalog`; nothing contacts a hosted or paid endpoint.
    - `ledger_path` None means there is no ledger, so every paid model is refused;
      `from_env` supplies the configured one. The HTTP client, when not passed in, is
      built with `HTTP_TIMEOUT` and closed by `aclose`.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        ledger_path: Path | None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._ledger_path = ledger_path
        self._owns_http = http_client is None
        self._http = http_client if http_client is not None else _http_client()
        self._probe: _OllamaProbe | None = None

    @classmethod
    def from_env(
        cls,
        settings: Settings | None = None,
        *,
        ledger_path: Path | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> ProviderRegistry:
        """The registry over `settings` (`Settings()` when None).

        `ledger_path` overrides `Settings.ledger_path` (default
        `<data_dir>/state/ledger.jsonl`), for eval runs in temporary data directories.
        """
        settings = settings if settings is not None else Settings()
        ledger = ledger_path if ledger_path is not None else configured_ledger_path(settings)
        return cls(settings, ledger_path=ledger, http_client=http_client)

    async def aclose(self) -> None:
        """Close the HTTP client, unless the caller passed it in."""
        if self._owns_http:
            await self._http.aclose()

    # -- ModelResolver -----------------------------------------------------------------

    def resolve(self, ref: str | None, project_default: str | None) -> ModelPort:
        chosen = ref or project_default or self._settings.default_model or self._automatic()
        if chosen is None:
            raise ModelUnavailable(
                "model_unavailable",
                _NO_MODEL.format(ollama=OLLAMA_DEFAULT.removeprefix("ollama:")),
                env_var=_DEFAULT_SETTING,
            )
        model_spec = self._spec(chosen)
        self._check(model_spec)
        return self._build(model_spec)

    def catalog(self) -> Sequence[ModelInfo]:
        refs = [ref for ref, row in CATALOG.items() if row.provider == "ollama"]
        refs.extend(self._pulled_elsewhere())
        refs.extend(ref for ref, row in CATALOG.items() if row.provider == "anthropic")
        configured = self._configured_openai()
        if configured is not None:
            refs.append(configured)
        if self._script() is not None:
            refs.append(_SCRIPTED_REF)
        return tuple(
            ModelInfo.from_spec(self._spec(ref), available=self._available(ref)) for ref in refs
        )

    async def health(self, *, probe: bool = False) -> HealthReport:
        if probe:
            await self._probe_ollama()
        default = self._settings.default_model or self._automatic()
        providers = [self._ollama_health(), self._anthropic_health(), self._openai_health()]
        if self._settings.fake_script is not None:
            providers.append(self._fake_health())
        return HealthReport(
            ok=default is not None and self._available(default),
            default_model=default,
            providers=tuple(providers),
            issues=tuple(self._issues(default)),
        )

    # -- specs -------------------------------------------------------------------------

    def _spec(self, ref: str) -> ModelSpec:
        """The spec of `ref` with the settings applied; raises `unknown_model`."""
        if ref in FAKE_SPECS:
            if ref == _SCRIPTED_REF and self._settings.fake_script is not None:
                return SCRIPTED_SPEC
            raise ModelUnavailable("unknown_model", f"Unknown model {ref!r}.")
        row = spec(ref)
        if row.provider == "ollama":
            return self._ollama_spec(row)
        if row.provider == "openai_compatible" and ref not in CATALOG:
            return self._openai_spec(row)
        return row

    def _ollama_spec(self, row: ModelSpec) -> ModelSpec:
        num_ctx = self._settings.ollama_num_ctx or row.num_ctx or row.context_window
        if num_ctx != row.num_ctx or num_ctx != row.context_window:
            row = replace(row, num_ctx=num_ctx, context_window=num_ctx)
        if row.ref not in CATALOG and self._probe is not None:
            capabilities = self._probe.capabilities.get(row.model)
            if capabilities is not None:
                row = replace(row, native_tools="tools" in capabilities)
        return row

    def _openai_spec(self, row: ModelSpec) -> ModelSpec:
        window = self._settings.openai_context
        price = self._openai_price()
        agentic = row.native_tools and window >= _AGENTIC_MIN_WINDOW
        return replace(
            row,
            context_window=window,
            default_profile="agentic" if agentic else "guided",
            price=price,
            paid=row.paid or price is not None,
        )

    def _openai_price(self) -> Price | None:
        """`PRISMTEX_OPENAI_PRICE`, with cached input at the full input rate.

        The endpoint's cache rate is unknown, so the guard over-reserves rather than under.
        """
        if self._settings.openai_price is None:
            return None
        input_rate, output_rate = self._settings.openai_price
        return Price(
            input=input_rate, output=output_rate, cache_write=input_rate, cache_read=input_rate
        )

    # -- checks ------------------------------------------------------------------------

    def _check(self, model_spec: ModelSpec) -> None:
        """Raise `ModelUnavailable` naming the setting `model_spec` still needs."""
        if model_spec.provider == "ollama":
            if not _is_http_url(self._settings.ollama_url):
                raise ModelUnavailable(
                    "model_unavailable",
                    f"{_OLLAMA_URL_SETTING} must be an http or https URL.",
                    env_var=_OLLAMA_URL_SETTING,
                )
        elif model_spec.provider == "anthropic":
            if self._anthropic_key() is None:
                raise ModelUnavailable(
                    "model_unavailable",
                    f"{model_spec.ref} needs an Anthropic API key. {_ANTHROPIC_KEY_HINT}",
                    env_var=_ANTHROPIC_KEY_SETTING,
                )
            self._require_ledger(model_spec)
        elif model_spec.provider == "openai_compatible":
            self._openai_endpoint(model_spec)
        elif self._script() is None:
            raise ModelUnavailable(
                "model_unavailable",
                f"{_FAKE_SCRIPT_SETTING} must name a script file.",
                env_var=_FAKE_SCRIPT_SETTING,
            )

    def _require_ledger(self, model_spec: ModelSpec) -> Path:
        if self._ledger_path is None:
            raise ModelUnavailable(
                "model_unavailable",
                f"{model_spec.ref} is a paid model, and paid calls run only against the cost "
                f"ledger. Set {LEDGER_ENV_VAR}.",
                env_var=LEDGER_ENV_VAR,
            )
        return self._ledger_path

    def _openai_endpoint(self, model_spec: ModelSpec) -> tuple[Preset, str, str | None]:
        """The preset, base URL and key for `model_spec`; raises `ModelUnavailable`."""
        configured = self._configured_openai()
        if configured is None:
            raise ModelUnavailable(
                "model_unavailable",
                f"Set {_OPENAI_PRESET_SETTING} and {_OPENAI_MODEL_SETTING} to use "
                f"{model_spec.ref}.",
                env_var=_OPENAI_PRESET_SETTING,
            )
        if model_spec.ref != configured:
            raise ModelUnavailable(
                "model_unavailable",
                f"{model_spec.ref} is not the configured OpenAI-compatible model "
                f"({configured}). One endpoint is configured at a time: set "
                f"{_OPENAI_PRESET_SETTING} and {_OPENAI_MODEL_SETTING}.",
                env_var=_OPENAI_MODEL_SETTING,
            )
        preset = PRESETS[model_spec.ref.partition(":")[0]]
        url = self._openai_base_url(preset)
        if isinstance(url, HealthIssue):
            raise ModelUnavailable("model_unavailable", url.message, env_var=_OPENAI_URL_SETTING)
        key = self._openai_key()
        if preset.key_required and key is None:
            raise ModelUnavailable(
                "model_unavailable",
                f"The {preset.name} preset needs an API key. Set {_OPENAI_KEY_SETTING}.",
                env_var=_OPENAI_KEY_SETTING,
            )
        if model_spec.paid and model_spec.price is None:
            raise ModelUnavailable(
                "model_unavailable",
                f"{model_spec.ref} is a paid model with no known price, so its cost cannot "
                f"be metered. {_PRICE_HINT}",
                env_var=_OPENAI_PRICE_SETTING,
            )
        if model_spec.paid:
            self._require_ledger(model_spec)
        return preset, url, key

    def _openai_base_url(self, preset: Preset) -> str | HealthIssue:
        """The base URL the preset sends to, or the issue that stops it."""
        configured = self._settings.openai_base_url
        if preset.base_url_configurable and configured is not None:
            if not _is_http_url(configured):
                return HealthIssue(
                    "openai_base_url_missing",
                    f"{_OPENAI_URL_SETTING} must be an http or https URL.",
                )
            return configured.strip().rstrip("/")
        if preset.base_url is None:
            return HealthIssue(
                "openai_base_url_missing",
                f"The {preset.name} preset needs {_OPENAI_URL_SETTING}, the base URL of the "
                "endpoint, such as http://127.0.0.1:8001/v1.",
            )
        return preset.base_url

    def _available(self, ref: str) -> bool:
        """Whether `ref` can run now; an Ollama model must have been seen pulled."""
        try:
            model_spec = self._spec(ref)
            self._check(model_spec)
        except ModelUnavailable:
            return False
        return model_spec.provider != "ollama" or self._pulled(model_spec.model)

    # -- building ----------------------------------------------------------------------

    def _build(self, model_spec: ModelSpec) -> ModelPort:
        # The SDK adapters are imported on first use: each SDK takes a while to import,
        # and the fake module imports this one.
        if model_spec.provider == "ollama":
            return OllamaModel(
                model_spec,
                base_url=self._settings.ollama_url,
                client=self._http,
                num_ctx=model_spec.num_ctx or model_spec.context_window,
            )
        if model_spec.provider == "anthropic":
            from prismtex.agent.providers.anthropic import AnthropicModel

            key = self._anthropic_key()
            assert key is not None  # checked
            return AnthropicModel(
                model_spec,
                api_key=key,
                http_client=self._http,
                ledger_path=self._require_ledger(model_spec),
                effort=model_spec.effort or "medium",
                fallbacks=self._settings.anthropic_fallbacks == "default",
            )
        if model_spec.provider == "openai_compatible":
            from prismtex.agent.providers.openai_compat import OpenAICompatModel

            preset, url, key = self._openai_endpoint(model_spec)
            return OpenAICompatModel(
                model_spec,
                preset=preset,
                base_url=url,
                api_key=key,
                http_client=self._http,
                ledger_path=self._ledger_path,
            )
        from prismtex.agent.providers.fake import scripted_model

        script = self._script()
        assert script is not None  # checked
        try:
            return scripted_model(script)
        except ValueError as error:
            raise ModelUnavailable(
                "model_unavailable", str(error), env_var=_FAKE_SCRIPT_SETTING
            ) from None

    # -- the default model -------------------------------------------------------------

    def _automatic(self) -> str | None:
        """The first available of the Ollama default, Claude Opus 5.5 and the endpoint."""
        if self._pulled(OLLAMA_DEFAULT.removeprefix("ollama:")):
            return OLLAMA_DEFAULT
        if self._anthropic_key() is not None:
            return DEFAULT_ANTHROPIC
        return self._configured_openai()

    def _configured_openai(self) -> str | None:
        preset, model = self._settings.openai_preset, self._settings.openai_model
        return f"{preset}:{model}" if preset is not None and model is not None else None

    def _pulled(self, model: str) -> bool:
        probe = self._probe
        return probe is not None and probe.reachable and model in probe.models

    def _pulled_elsewhere(self) -> list[str]:
        """Refs of the pulled Ollama models the catalog lacks, `:latest` folded in."""
        if self._probe is None:
            return []
        names = self._probe.models
        return sorted(
            f"ollama:{name}"
            for name in names
            if f"ollama:{name}" not in CATALOG
            and not (name.endswith(":latest") and name.removesuffix(":latest") in names)
        )

    # -- settings ----------------------------------------------------------------------

    def _anthropic_key(self) -> str | None:
        secret = self._settings.anthropic_api_key
        return (secret.get_secret_value() or None) if secret is not None else None

    def _openai_key(self) -> str | None:
        secret = self._settings.openai_api_key
        return (secret.get_secret_value() or None) if secret is not None else None

    def _script(self) -> Path | None:
        path = self._settings.fake_script
        return path if path is not None and path.is_file() else None

    # -- health ------------------------------------------------------------------------

    async def _probe_ollama(self) -> None:
        url = self._settings.ollama_url
        if not _is_http_url(url):
            detail = f"{_OLLAMA_URL_SETTING} must be an http or https URL."
            self._probe = _OllamaProbe(False, frozenset(), {}, detail)
            return
        try:
            names = await list_models(url, self._http)
        except ProviderError as error:
            self._probe = _OllamaProbe(False, frozenset(), {}, error.message)
            return
        capabilities: dict[str, frozenset[str]] = {}
        for name in sorted(names):
            if name.endswith(":latest") or f"ollama:{name}" in CATALOG:
                continue  # a `:latest` name is read through its bare twin
            try:
                found = await show_capabilities(url, name, self._http)
            except ProviderError:
                continue
            capabilities[name] = found
            if f"{name}:latest" in names:
                capabilities[f"{name}:latest"] = found
        self._probe = _OllamaProbe(True, frozenset(names), capabilities, None)

    def _issues(self, default: str | None) -> list[HealthIssue]:
        """What stops the default model, and what is wrong with the configured endpoint."""
        issues: list[HealthIssue] = []
        probe = self._probe
        if probe is not None and (default is None or default.startswith("ollama:")):
            model = (default or OLLAMA_DEFAULT).removeprefix("ollama:")
            if not probe.reachable:
                detail = probe.detail or "Ollama could not be reached."
                issues.append(HealthIssue("ollama_unreachable", detail))
            elif model not in probe.models:
                detail = f"Ollama does not have {model}. Run: ollama pull {model}"
                issues.append(HealthIssue("ollama_model_missing", detail))
        if self._anthropic_key() is None and (default is None or default.startswith("anthropic:")):
            detail = f"No Anthropic API key is set. {_ANTHROPIC_KEY_HINT}"
            issues.append(HealthIssue("anthropic_key_missing", detail))
        preset_name = self._settings.openai_preset
        if preset_name is not None:
            preset = PRESETS[preset_name]
            url = self._openai_base_url(preset)
            if isinstance(url, HealthIssue):
                issues.append(url)
            if preset.paid and self._openai_price() is None:
                detail = (
                    f"The {preset.name} preset is paid and its model has no known price, so "
                    f"it is refused. {_PRICE_HINT}"
                )
                issues.append(HealthIssue("price_missing", detail))
        return issues

    def _ollama_health(self) -> ProviderHealth:
        url = self._settings.ollama_url
        probe = self._probe
        reachable = probe.reachable if probe is not None else None
        if not _is_http_url(url):
            detail = f"{_OLLAMA_URL_SETTING} must be an http or https URL."
            return ProviderHealth("ollama", configured=False, reachable=reachable, detail=detail)
        if probe is None:
            detail = f"{_display_url(url)}; not probed yet."
        elif not probe.reachable:
            detail = probe.detail
        else:
            count = len({name.removesuffix(":latest") for name in probe.models})
            detail = f"{_display_url(url)}; {count} model{'' if count == 1 else 's'} pulled."
        return ProviderHealth("ollama", configured=True, reachable=reachable, detail=detail)

    def _anthropic_health(self) -> ProviderHealth:
        if self._anthropic_key() is None:
            return ProviderHealth(
                "anthropic", configured=False, reachable=None, detail=_ANTHROPIC_KEY_HINT
            )
        detail = "Key set."
        if self._ledger_path is None:
            detail = f"Key set, but paid calls need the cost ledger: set {LEDGER_ENV_VAR}."
        return ProviderHealth("anthropic", configured=True, reachable=None, detail=detail)

    def _openai_health(self) -> ProviderHealth:
        configured = self._configured_openai()
        if configured is None:
            detail = (
                f"Set {_OPENAI_PRESET_SETTING} and {_OPENAI_MODEL_SETTING} to use an "
                "OpenAI-compatible endpoint."
            )
            return ProviderHealth(
                "openai_compatible", configured=False, reachable=None, detail=detail
            )
        try:
            _, url, _ = self._openai_endpoint(self._spec(configured))
        except ModelUnavailable as problem:
            return ProviderHealth(
                "openai_compatible", configured=False, reachable=None, detail=problem.message
            )
        detail = f"{configured} at {_display_url(url)}."
        return ProviderHealth("openai_compatible", configured=True, reachable=None, detail=detail)

    def _fake_health(self) -> ProviderHealth:
        script = self._script()
        detail = (
            f"Script {script.name}."
            if script is not None
            else f"{_FAKE_SCRIPT_SETTING} names no file."
        )
        return ProviderHealth("fake", configured=script is not None, reachable=None, detail=detail)


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT)


def _is_http_url(value: str) -> bool:
    try:
        url = httpx.URL(value.strip())
    except httpx.InvalidURL:
        return False
    return url.scheme in ("http", "https") and bool(url.host)


def _display_url(value: str) -> str:
    """`value` without credentials, query or fragment, for health details."""
    url = httpx.URL(value.strip())
    host = f"[{url.host}]" if ":" in url.host else url.host
    port = f":{url.port}" if url.port is not None else ""
    return f"{url.scheme}://{host}{port}{url.path.rstrip('/')}"
