#!/usr/bin/env python3
"""
PostToolUse helper: if a shell command ran pytest successfully, mark it
so the Stop ship gate can skip a full re-run for a while.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0

    tool = (data.get("toolName") or "").lower()
    if tool not in (
        "run_terminal_command",
        "bash",
        "shell",
    ) and "terminal" not in tool and "bash" not in tool:
        # still allow if command looks like pytest
        pass

    tin = data.get("toolInput") or {}
    cmd = ""
    if isinstance(tin, dict):
        cmd = str(tin.get("command") or tin.get("cmd") or "")
    else:
        cmd = str(tin)

    if "pytest" not in cmd.lower():
        return 0

    # Success heuristic: no failure in toolResult if present
    result = data.get("toolResult") or data.get("tool_result") or ""
    result_s = result if isinstance(result, str) else json.dumps(result)
    lower = result_s.lower()
    failed = (
        "failed" in lower
        and "passed" not in lower.split("failed")[0][-40:]  # weak
    )
    if "exit code" in lower and "exit code: 0" not in lower and "exit_code\": 0" not in lower:
        # common failure markers
        if "failed" in lower or "error" in lower:
            return 0
    if "=== " in result_s and " failed" in lower:
        return 0
    if "failed," in lower or " failed " in lower:
        # "1 failed" style
        if "0 failed" not in lower and "passed" in lower and "failed" in lower:
            # e.g. "2 failed, 3 passed"
            import re

            m = re.search(r"(\d+)\s+failed", lower)
            if m and int(m.group(1)) > 0:
                return 0

    root = Path(
        data.get("workspaceRoot")
        or os.environ.get("GROK_WORKSPACE_ROOT")
        or os.getcwd()
    ).resolve()
    state = root / ".grok" / "hooks" / "state"
    state.mkdir(parents=True, exist_ok=True)
    marker = state / "last_pytest_ok"
    marker.write_text(
        json.dumps({"ts": time.time(), "cmd": cmd[:500]}, indent=2),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        sys.stderr.write(f"post_test_mark error (fail-open): {exc}\n")
        raise SystemExit(0)
