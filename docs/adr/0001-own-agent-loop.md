---
status: proposed
---

# Own agent loop instead of LangChain and LangGraph

> Renamed to PrismTeX on 2026-11-25: read `openprism` as `prismtex` and `OPENPRISM_` as `PRISMTEX_`.

The agent runs on a small loop we own, over a provider port with three production adapters: Anthropic SDK, OpenAI-compatible SDK, and Ollama's native `/api/chat`. A fake adapter covers tests. We dropped LangGraph's `create_react_agent`, which was deprecated in LangGraph 1.0.

The deciding facts:

- **No Ollama context control.** The framework path gave us no way to set Ollama's context size (`num_ctx`). The OpenAI-compatible `/v1` endpoint ignores it, and the context silently fell to 4,096 tokens.
- **Broken Claude requests.** It sent `temperature` to Claude models that reject it, and fell back to `max_tokens=4096` for the 5.5 models.
- **History edits.** Its client-side history editing conflicts with the preserved thinking blocks of Claude 5.5 models.
- **No new dependencies.** The `anthropic`, `openai` and `httpx` packages we need are already installed.
- **Full control.** Owning the loop gives us token streaming, cancellation, budgets and offline tests with a recording fake.

## Considered options

- **LangChain v1 `create_agent` with middleware.** It has rich limits and summarization. But it adds `langchain`, needs a LangGraph upgrade, and its summarization conflicts with preserved thinking.
- **PydanticAI.** It has the best test models, but needs major upgrades of both SDKs and still reaches Ollama only through `/v1`.

## Consequences

We own about 4,000 lines of agent code (review, verification, budgets and recovery included), and its tests become the main regression net. Adding a provider means writing an adapter of roughly 150 lines.
