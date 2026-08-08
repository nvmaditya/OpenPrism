#!/usr/bin/env python3
"""
OpenPrism Stop gate: after a genuine end_turn, require ship hygiene.

If this workspace is the OpenPrism repo and there is unfinished ship work
(tests failing, dirty tree, or unpushed commits), block the stop and tell
the agent to: update docs → commit → git push origin main.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def out(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def is_openprism(root: Path) -> bool:
    if not (root / ".git").exists():
        return False
    r = git(root, "remote", "get-url", "origin")
    if r.returncode != 0:
        return False
    url = (r.stdout or "").strip().lower()
    return "openprism" in url


def resolve_python(root: Path) -> Path | None:
    for rel in (".venv/Scripts/python.exe", ".venv/bin/python", ".venv/bin/python3"):
        p = root / rel
        if p.is_file():
            return p
    return None


def tests_ok(root: Path) -> tuple[bool, str]:
    """Run backend pytest when present. Skip (ok) if no tests dir."""
    tests = root / "backend" / "tests"
    if not tests.is_dir():
        return True, "no backend/tests"
    py = resolve_python(root)
    if py is None:
        return True, "no venv python; skipped pytest"
    # Prefer marker from a successful PostToolUse pytest this session
    marker = root / ".grok" / "hooks" / "state" / "last_pytest_ok"
    if marker.is_file():
        try:
            age = __import__("time").time() - marker.stat().st_mtime
            if age < 3600:  # 1h
                return True, f"recent pytest ok marker ({int(age)}s ago)"
        except OSError:
            pass
    proc = subprocess.run(
        [str(py), "-m", "pytest", str(tests), "-q", "--tb=no"],
        cwd=str(root / "backend"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    tail = ((proc.stdout or "") + (proc.stderr or ""))[-800:]
    return proc.returncode == 0, tail


def ahead_count(root: Path) -> int:
    r = git(root, "rev-list", "--left-right", "--count", "origin/main...HEAD")
    if r.returncode != 0:
        # try origin/HEAD
        r = git(root, "status", "-sb")
        line = (r.stdout or "").splitlines()[0] if r.stdout else ""
        if "ahead" in line:
            # ## main...origin/main [ahead 2]
            import re

            m = re.search(r"ahead\s+(\d+)", line)
            return int(m.group(1)) if m else 0
        return 0
    parts = (r.stdout or "").strip().split()
    if len(parts) >= 2:
        return int(parts[1])
    return 0


def dirty(root: Path) -> bool:
    r = git(root, "status", "--porcelain")
    return bool((r.stdout or "").strip())


def main() -> int:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return 0

    # Only genuine turn completions
    if data.get("reason") != "end_turn":
        return 0

    root = Path(
        data.get("workspaceRoot")
        or data.get("cwd")
        or os.environ.get("GROK_WORKSPACE_ROOT")
        or os.getcwd()
    ).resolve()

    if not is_openprism(root):
        return 0

    # Avoid infinite loops: after several blocks, allow stop with warning in reason
    if data.get("stopHookActive"):
        # Still enforce unpushed commits once more; then allow
        if not dirty(root) and ahead_count(root) == 0:
            return 0

    ok, test_detail = tests_ok(root)
    if not ok:
        out(
            {
                "decision": "block",
                "reason": (
                    "OpenPrism ship gate: tests are not green.\n"
                    "Fix failures, re-run pytest, then update PLAN.md / AGENTS.md "
                    "if the product or agent surface changed, commit, and "
                    "`git push origin main`.\n\n"
                    f"{test_detail}"
                ),
            }
        )
        return 0

    if dirty(root):
        out(
            {
                "decision": "block",
                "reason": (
                    "OpenPrism ship gate: uncommitted work remains after testing.\n"
                    "Required ship sequence:\n"
                    "1) Update documentation (PLAN.md phase status; AGENTS.md only if "
                    "agent/API surface changed — product-facing docs, not drive-by fluff).\n"
                    "2) git add + commit with a clear message.\n"
                    "3) git push origin main (https://github.com/nvmaditya/OpenPrism).\n"
                    "Do not claim the feature done until origin/main has the commit."
                ),
            }
        )
        return 0

    ahead = ahead_count(root)
    if ahead > 0:
        out(
            {
                "decision": "block",
                "reason": (
                    f"OpenPrism ship gate: {ahead} local commit(s) not on origin/main.\n"
                    "Run: git push origin main\n"
                    "Confirm remote has the commit before finishing."
                ),
            }
        )
        return 0

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # fail-open
        sys.stderr.write(f"stop_ship_gate error (fail-open): {exc}\n")
        raise SystemExit(0)
