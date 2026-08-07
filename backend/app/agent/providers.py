"""Provider-pluggable chat model factory (PRISM_LLM_* settings)."""

from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from app.settings import Settings, get_settings

# Documented provider ids (switch via PRISM_LLM_PROVIDER only — no source edits).
SUPPORTED_PROVIDERS = frozenset(
    {"openai", "azure", "ollama", "anthropic", "custom"}
)


class ProviderError(ValueError):
    """Invalid or unsupported LLM provider configuration."""


def get_chat_model(settings: Settings | None = None) -> BaseChatModel:
    """
    Build a LangChain chat model from settings.

    Switching ``PRISM_LLM_PROVIDER`` (and related env vars) changes which
    backend is constructed without editing application source.
    """
    settings = settings or get_settings()
    provider = (settings.prism_llm_provider or "openai").strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ProviderError(
            f"unsupported PRISM_LLM_PROVIDER={provider!r}; "
            f"expected one of {sorted(SUPPORTED_PROVIDERS)}"
        )

    model = settings.prism_llm_model or "gpt-4o"
    api_key = settings.prism_llm_api_key or "not-set"
    base_url = (settings.prism_llm_base_url or "").strip() or None

    if provider in ("openai", "custom"):
        return _openai_compatible(
            model=model,
            api_key=api_key,
            base_url=base_url,
            provider_label=provider,
        )

    if provider == "ollama":
        # OpenAI-compatible Ollama endpoint (default local)
        ollama_base = base_url or "http://127.0.0.1:11434/v1"
        return _openai_compatible(
            model=model or "llama3",
            api_key=api_key or "ollama",
            base_url=ollama_base,
            provider_label="ollama",
        )

    if provider == "azure":
        return _azure(
            model=model,
            api_key=api_key,
            base_url=base_url,
        )

    if provider == "anthropic":
        return _anthropic(model=model, api_key=api_key, base_url=base_url)

    raise ProviderError(f"unhandled provider: {provider}")


def _openai_compatible(
    *,
    model: str,
    api_key: str,
    base_url: str | None,
    provider_label: str,
) -> BaseChatModel:
    from langchain_openai import ChatOpenAI

    kwargs: dict[str, Any] = {
        "model": model,
        "api_key": api_key,
        "temperature": 0,
    }
    if base_url:
        kwargs["base_url"] = base_url
    chat = ChatOpenAI(**kwargs)
    # Stash label for describe_chat_model / tests (not a live network field)
    object.__setattr__(chat, "_prism_provider", provider_label)
    return chat


def _azure(*, model: str, api_key: str, base_url: str | None) -> BaseChatModel:
    from langchain_openai import AzureChatOpenAI

    # base_url is the Azure endpoint, e.g. https://{resource}.openai.azure.com/
    kwargs: dict[str, Any] = {
        "azure_deployment": model,
        "api_key": api_key,
        "api_version": "2024-08-01-preview",
        "temperature": 0,
    }
    if base_url:
        kwargs["azure_endpoint"] = base_url
    else:
        # Placeholder endpoint so the object can be constructed without live calls
        kwargs["azure_endpoint"] = "https://example.openai.azure.com/"
    chat = AzureChatOpenAI(**kwargs)
    object.__setattr__(chat, "_prism_provider", "azure")
    return chat


def _anthropic(*, model: str, api_key: str, base_url: str | None) -> BaseChatModel:
    from langchain_anthropic import ChatAnthropic

    kwargs: dict[str, Any] = {
        "model": model or "claude-3-5-sonnet-latest",
        "api_key": api_key,
        "temperature": 0,
    }
    if base_url:
        kwargs["base_url"] = base_url
    chat = ChatAnthropic(**kwargs)
    object.__setattr__(chat, "_prism_provider", "anthropic")
    return chat


def describe_chat_model(model: BaseChatModel) -> dict[str, Any]:
    """Observable construction facts for tests (no network)."""
    provider = getattr(model, "_prism_provider", None) or type(model).__name__
    info: dict[str, Any] = {
        "class": type(model).__name__,
        "provider": provider,
        "module": type(model).__module__,
    }

    # OpenAI-compatible
    for attr in ("model_name", "model"):
        if hasattr(model, attr):
            val = getattr(model, attr)
            if val is not None:
                info["model"] = val
                break

    # base URL / endpoint (langchain-openai field names vary by version)
    for attr in (
        "openai_api_base",
        "base_url",
        "azure_endpoint",
        "anthropic_api_url",
    ):
        if hasattr(model, attr):
            val = getattr(model, attr)
            if val:
                info["base_url"] = str(val)
                break

    # Try nested client if present
    if "base_url" not in info:
        client = getattr(model, "root_client", None) or getattr(model, "client", None)
        if client is not None:
            base = getattr(client, "base_url", None)
            if base is not None:
                info["base_url"] = str(base)

    return info
