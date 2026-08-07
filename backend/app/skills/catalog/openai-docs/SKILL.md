---
name: openai-docs
description: Official OpenAI product and API guidance. Use for API implementation, model selection, prompt upgrades, and Codex surface choice.
---

# openai-docs

The `openai-docs` skill provides current, official guidance for OpenAI products and APIs. Use it for API implementation questions, model selection, model or prompt upgrades, and help choosing an appropriate Codex surface.

Answers must be grounded in official OpenAI documentation. For broad questions about Codex itself, prefer official manuals; any web fallback is restricted to official OpenAI sources (e.g. platform.openai.com, developers.openai.com).

## Workflow

1. Restate the product/API surface in question.
2. Prefer cached/local reference notes under this skill's `references/` when present.
3. Do not invent non-existent endpoints or model names.
