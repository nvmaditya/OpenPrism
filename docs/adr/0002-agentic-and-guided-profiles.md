---
status: proposed
---

# Two agent profiles, with LaTeX plumbing done by the host

> Renamed to PrismTeX on 2026-11-25: read `openprism` as `prismtex` and `OPENPRISM_` as `PRISMTEX_`.

One turn interface runs one of two profiles, chosen from the model's capabilities (users can override the choice).

- **Agentic** (frontier models such as Claude): the model drives a tool loop, with `str_replace`-style edits on numbered, ranged views.
- **Guided** (small local models such as qwen3 4B):
  - The host chooses the region (the selection, or a section from the outline).
  - The model rewrites that region as plain text inside delimiters, so LaTeX never passes through JSON escaping.
  - The host applies the result and checks it.

In both profiles the host does the structural LaTeX work:

- adding packages;
- inserting content after a heading or anchor;
- wiring bibliography entries and `\cite` keys;
- stripping stray `\begin{document}` and `\end{document}` from fragments.

We decided this because of the live runs on 2026-11-24:

- **JSON tool chains fail on small models.** qwen3 4B failed multi-step JSON tool chains: it couldn't write `\\` plus a newline inside JSON, and it repeated identical failing calls.
- **Surgical edits work once the harness is fixed.** With a correct context size and tolerant edits, qwen3 4B passed surgical edits in 8–16 s.
- **Plumbing is where it still failed:** placing content, `\usepackage` lines and bibliography wiring.
- **llama3.1 8B is not viable.** It failed most tasks even with the fixes, so it isn't a supported default.
