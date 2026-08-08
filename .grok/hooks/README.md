# OpenPrism Grok hooks — automatic ship

## Behavior (every agent turn end)

On **`Stop`** with `reason=end_turn` in this repo:

1. **Tests** — green required (uses recent pytest marker or runs `backend` pytest).
2. **Docs** — appends `docs/SHIPLOG.md` and stamps `PLAN.md` “Last auto-ship”.
3. **Commit** — `git add -A` + commit if the tree is dirty (`.env` stays ignored).
4. **Push** — `git push origin main` if local is ahead of `origin/main`.

If tests or push fail, the hook **blocks** stop and feeds the error back to the agent.

If everything is already clean and pushed, the turn ends normally.

## Files

| File | Role |
|------|------|
| `feature-ship.json` | Registers PostToolUse + Stop |
| `bin/auto_ship.py` | Auto docs → commit → push |
| `bin/post_test_mark.py` | Marks successful pytest for the gate |
| `bin/stop_ship_gate.py` | Thin wrapper → `auto_ship.py` |

## Enable

1. Work in `C:\Code\prism`
2. `/hooks-trust` (once) if project hooks are not trusted
3. `/hooks` → `r` to reload

## Manual dry-run

```powershell
cd C:\Code\prism
.\.venv\Scripts\python.exe .grok\hooks\bin\auto_ship.py
# or with envelope:
echo '{"reason":"end_turn","workspaceRoot":"C:\\Code\\prism"}' | .\.venv\Scripts\python.exe .grok\hooks\bin\auto_ship.py
```
