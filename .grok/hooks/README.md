# OpenPrism Grok hooks

## What they do

| Event | Script | Behavior |
|-------|--------|----------|
| `PostToolUse` (shell) | `bin/post_test_mark.py` | If a `pytest` command looks successful, writes `state/last_pytest_ok` |
| `Stop` (`end_turn`) | `bin/stop_ship_gate.py` | **Ship gate** for this repo only (origin contains `OpenPrism`) |

### Ship gate (Stop)

When the agent tries to finish a turn and this is the OpenPrism workspace:

1. **Tests** — must be green (recent pytest marker or runs `backend` pytest).
2. **Docs + commit** — if the working tree is dirty, blocks stop and requires:
   - update `PLAN.md` / `AGENTS.md` when the product or agent surface changed
   - `git commit`
3. **Push** — if local commits are ahead of `origin/main`, blocks stop until:
   - `git push origin main`

Hooks **fail open** on crashes (Grok default). An explicit `decision: block` is required to keep the agent working.

## Enable in this project

1. Open the project in Grok: `C:\Code\prism`
2. Trust hooks: `/hooks-trust` (or launch with `--trust`)
3. Reload hooks: `/hooks` → `r`, or restart the session
4. Confirm **feature-ship** is listed under Project hooks

## Required agent sequence (after a feature)

```
implement → pytest green → update PLAN.md/AGENTS.md if needed → commit → git push origin main → then stop
```
