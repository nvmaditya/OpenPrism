#!/usr/bin/env python3
"""
OpenPrism auto-ship (Stop / optional manual).

On genuine end_turn in the OpenPrism repo:
  1) Ensure tests green (marker or pytest)
  2) If dirty: append docs/SHIPLOG.md, stage, commit
  3) If ahead of origin/main: git push origin main
  4) Block the stop only if tests or push fail

Designed to run unattended every time the agent finishes a turn.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.flush()


def block(reason: str) -> int:
    emit({"decision": "block", "reason": reason})
    return 0


def git(root: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def is_openprism(root: Path) -> bool:
    if not (root / ".git").is_dir() and not (root / ".git").is_file():
        return False
    r = git(root, "remote", "get-url", "origin")
    if r.returncode != 0:
        return False
    return "openprism" in (r.stdout or "").lower()


def resolve_python(root: Path) -> Path | None:
    for rel in (".venv/Scripts/python.exe", ".venv/bin/python", ".venv/bin/python3"):
        p = root / rel
        if p.is_file():
            return p
    return None


def tests_green(root: Path) -> tuple[bool, str]:
    tests = root / "backend" / "tests"
    if not tests.is_dir():
        return True, "no backend/tests (skipped)"

    marker = root / ".grok" / "hooks" / "state" / "last_pytest_ok"
    if marker.is_file():
        try:
            age = time.time() - marker.stat().st_mtime
            if age < 3600:
                return True, f"pytest marker ok ({int(age)}s ago)"
        except OSError:
            pass

    py = resolve_python(root)
    if py is None:
        return False, "no project venv python to run pytest"

    proc = subprocess.run(
        [str(py), "-m", "pytest", str(tests), "-q", "--tb=line"],
        cwd=str(root / "backend"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    tail = ((proc.stdout or "") + "\n" + (proc.stderr or ""))[-1200:]
    if proc.returncode == 0:
        # refresh marker
        state = root / ".grok" / "hooks" / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "last_pytest_ok").write_text(
            json.dumps({"ts": time.time(), "via": "auto_ship"}, indent=2),
            encoding="utf-8",
        )
        return True, tail.strip() or "pytest passed"
    return False, tail


def porcelain(root: Path) -> str:
    r = git(root, "status", "--porcelain")
    return (r.stdout or "").strip()


def ahead_count(root: Path) -> int:
    r = git(root, "rev-list", "--left-right", "--count", "origin/main...HEAD")
    if r.returncode == 0:
        parts = (r.stdout or "").strip().split()
        if len(parts) >= 2:
            return int(parts[1])
    r2 = git(root, "status", "-sb")
    line = (r2.stdout or "").splitlines()[0] if r2.stdout else ""
    m = re.search(r"ahead\s+(\d+)", line)
    return int(m.group(1)) if m else 0


def shortstat(root: Path) -> str:
    r = git(root, "diff", "--stat")
    r2 = git(root, "diff", "--cached", "--stat")
    return ((r.stdout or "") + (r2.stdout or "")).strip()[:800]


def update_docs(root: Path, dirty_summary: str) -> list[Path]:
    """Append ship log + stamp PLAN.md header. Returns paths touched."""
    touched: list[Path] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    branch = (git(root, "branch", "--show-current").stdout or "main").strip()

    # docs/SHIPLOG.md — always safe progressive docs
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    shiplog = docs / "SHIPLOG.md"
    if not shiplog.exists():
        shiplog.write_text(
            "# OpenPrism ship log\n\n"
            "Auto-updated by Grok `.grok/hooks` after tests pass "
            "(feature complete → docs stamp → commit → push).\n\n",
            encoding="utf-8",
        )
    entry = (
        f"## {now}\n\n"
        f"- Branch: `{branch}`\n"
        f"- Auto-ship after agent turn (tests green).\n"
    )
    if dirty_summary:
        entry += f"- Working tree changes:\n\n```\n{dirty_summary}\n```\n\n"
    else:
        entry += "- Working tree clean (push-only or no file diffs).\n\n"
    with shiplog.open("a", encoding="utf-8") as f:
        f.write(entry)
    touched.append(shiplog)

    # PLAN.md — update status line if present
    plan = root / "PLAN.md"
    if plan.is_file():
        text = plan.read_text(encoding="utf-8")
        stamp = f"> **Last auto-ship:** {now} · docs/SHIPLOG.md"
        if "**Last auto-ship:**" in text:
            text = re.sub(
                r"> \*\*Last auto-ship:\*\*[^\n]*",
                stamp,
                text,
                count=1,
            )
        else:
            # insert after first status blockquote if any
            if text.startswith(">"):
                lines = text.splitlines(keepends=True)
                # find end of leading blockquote run
                i = 0
                while i < len(lines) and (
                    lines[i].startswith(">") or lines[i].strip() == ""
                ):
                    i += 1
                lines.insert(i, stamp + "\n")
                text = "".join(lines)
            else:
                text = stamp + "\n\n" + text
        plan.write_text(text, encoding="utf-8")
        touched.append(plan)

    return touched


def commit_all(root: Path, message: str) -> tuple[bool, str]:
    git(root, "add", "-A")
    # nothing staged?
    r = git(root, "diff", "--cached", "--quiet")
    if r.returncode == 0:
        return True, "nothing to commit"
    # ensure identity (local only if missing)
    email = git(root, "config", "user.email")
    if email.returncode != 0 or not (email.stdout or "").strip():
        git(root, "config", "user.email", "openprism-bot@local")
        git(root, "config", "user.name", "OpenPrism Auto-Ship")
    c = git(root, "commit", "-m", message)
    if c.returncode != 0:
        return False, (c.stdout or "") + (c.stderr or "")
    return True, (c.stdout or "").strip()


def push_main(root: Path) -> tuple[bool, str]:
    p = git(root, "push", "origin", "HEAD:main")
    if p.returncode != 0:
        # try -u origin main
        p2 = git(root, "push", "-u", "origin", "main")
        if p2.returncode != 0:
            return False, ((p.stdout or "") + (p.stderr or "") + (p2.stdout or "") + (p2.stderr or ""))[
                -1500:
            ]
        return True, (p2.stdout or p2.stderr or "pushed").strip()
    return True, (p.stdout or p.stderr or "pushed").strip()


def main() -> int:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0

    # Only genuine turn ends (not session-end observe fire)
    reason = data.get("reason")
    if reason is not None and reason != "end_turn":
        return 0

    root = Path(
        data.get("workspaceRoot")
        or data.get("cwd")
        or os.environ.get("GROK_WORKSPACE_ROOT")
        or os.getcwd()
    ).resolve()

    if not is_openprism(root):
        return 0

    ok, detail = tests_green(root)
    if not ok:
        return block(
            "OpenPrism auto-ship: tests failed. Fix tests before docs/push.\n\n"
            + detail
        )

    dirty = porcelain(root)
    actions: list[str] = []

    if dirty:
        summary = shortstat(root) or dirty[:500]
        update_docs(root, summary)
        msg = (
            "chore: auto-ship after feature tests\n\n"
            "Automated by .grok/hooks (docs stamp + commit + push)."
        )
        cok, cout = commit_all(root, msg)
        if not cok:
            return block(f"OpenPrism auto-ship: commit failed.\n{cout}")
        actions.append(f"committed: {cout or 'ok'}")
    else:
        actions.append("tree clean")

    ahead = ahead_count(root)
    if ahead > 0 or dirty:
        # re-check ahead after commit
        ahead = ahead_count(root)
    if ahead > 0:
        pok, pout = push_main(root)
        if not pok:
            return block(
                "OpenPrism auto-ship: git push failed. Fix remote/auth, then push.\n\n"
                + pout
            )
        actions.append(f"pushed ({ahead} commit(s)): {pout[:200]}")
    else:
        actions.append("already synced with origin/main")

    # Allow stop; optional non-blocking context for the UI
    emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "Stop",
                "additionalContext": "Auto-ship: " + "; ".join(actions),
            }
        }
    )
    return 0


if __name__ == "__main__":
    try:
        # Default reason for CLI testing when no stdin envelope
        if sys.stdin.isatty():
            # Manual run: force end_turn path with cwd workspace
            sys.stdin = open(os.devnull)  # type: ignore
            payload = {
                "reason": "end_turn",
                "workspaceRoot": str(Path.cwd()),
            }
            # re-exec logic with synthetic data
            os.environ.setdefault("GROK_WORKSPACE_ROOT", str(Path.cwd()))
            # fall through by writing temp — simpler to call main with mock:
            class _S:
                def read(self):
                    return json.dumps(payload)

            sys.stdin = _S()  # type: ignore
        raise SystemExit(main())
    except Exception as exc:
        sys.stderr.write(f"auto_ship error (fail-open): {exc}\n")
        raise SystemExit(0)
