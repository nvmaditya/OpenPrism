---
status: proposed
---

# Agent changes are proposals, compiled in a scratch copy and accepted per hunk

> Renamed to PrismTeX on 2026-11-25: read `openprism` as `prismtex` and `OPENPRISM_` as `PRISMTEX_`.

An agent turn never writes to the project folder.

- **During the turn.** Its edits build up in a staging overlay. Verification compiles that overlay in a scratch copy and compares the diagnostics with the pre-turn baseline, flagging only new errors. Up to two repair rounds follow.
- **At the end of the turn.** The result is a proposal: a list of hunks the user accepts or rejects against each file's base version. Accepting writes through the normal save path and creates a checkpoint.
- **Auto-accept.** An optional per-project setting auto-accepts proposals that compile with no new errors. It is off by default.

This replaces "write to disk, verify the absolute state, roll back on failure". That gate rolled back legitimate removals and renames. It reverted edits to documents that were already broken. It overwrote the user's concurrent saves. And it left half-finished edits when the browser disconnected. Staging also means the user's open buffer is never overwritten during a turn.

## Considered options

Writing to disk with a checkpoint and offering undo (the VS Code model) was the simpler alternative. We rejected it because unreviewed AI text would sit in the paper, and disk writes would race with the editor buffer.
