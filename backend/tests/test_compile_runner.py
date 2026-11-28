"""run_hardened: no shell, closed stdin, exact env, output cap, process-tree kill."""

from __future__ import annotations

import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

from prismtex.compile import ExecutableNotFound, run_hardened


def _env(**extra: str) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH") or os.defpath}
    # Python on Windows needs these to start.
    for name in ("SYSTEMROOT", "SystemRoot", "TEMP", "TMP"):
        if name in os.environ:
            env[name] = os.environ[name]
    return {**env, **extra}


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", textwrap.dedent(code)]


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        # A killed process whose parent is gone may linger as a zombie
        # until init reaps it; it is not running.
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return True
    return state != "Z"


def _wait_gone(pid: int, within_s: float = 10.0) -> bool:
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def test_captures_stdout_and_stderr(tmp_path: Path) -> None:
    outcome = run_hardened(
        _py("import sys; print('to stdout'); print('to stderr', file=sys.stderr)"),
        cwd=tmp_path,
        env=_env(),
        timeout_s=30,
    )
    assert outcome.exit_code == 0
    assert "to stdout" in outcome.output
    assert "to stderr" in outcome.output
    assert not outcome.timed_out
    assert not outcome.truncated
    assert outcome.duration_ms >= 0


def test_reports_the_exit_code(tmp_path: Path) -> None:
    outcome = run_hardened(_py("raise SystemExit(3)"), cwd=tmp_path, env=_env(), timeout_s=30)
    assert outcome.exit_code == 3


def test_stdin_is_closed(tmp_path: Path) -> None:
    outcome = run_hardened(
        _py("import sys; print(repr(sys.stdin.read()))"), cwd=tmp_path, env=_env(), timeout_s=30
    )
    assert outcome.output.strip() == "''"


def test_runs_in_cwd(tmp_path: Path) -> None:
    outcome = run_hardened(
        _py("import os; print(os.getcwd())"), cwd=tmp_path, env=_env(), timeout_s=30
    )
    assert Path(outcome.output.strip()).resolve() == tmp_path.resolve()


def test_child_sees_exactly_the_given_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRISMTEX_SERVER_SECRET", "server-only")
    outcome = run_hardened(
        _py("import os; print(os.environ.get('GIVEN'), os.environ.get('PRISMTEX_SERVER_SECRET'))"),
        cwd=tmp_path,
        env=_env(GIVEN="yes"),
        timeout_s=30,
    )
    assert outcome.output.split() == ["yes", "None"]


def test_arguments_are_not_interpreted_by_a_shell(tmp_path: Path) -> None:
    outcome = run_hardened(
        [*_py("import sys; print(sys.argv[1:])"), "$(touch pwned)", "; touch pwned2", "`id`"],
        cwd=tmp_path,
        env=_env(),
        timeout_s=30,
    )
    assert "$(touch pwned)" in outcome.output
    assert list(tmp_path.iterdir()) == []


def test_missing_executable(tmp_path: Path) -> None:
    with pytest.raises(ExecutableNotFound):
        run_hardened(["prismtex-no-such-tool"], cwd=tmp_path, env=_env(), timeout_s=30)


def test_executable_is_looked_up_on_the_given_path(tmp_path: Path) -> None:
    name = Path(sys.executable).name
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    with pytest.raises(ExecutableNotFound):
        run_hardened([name, "-c", "pass"], cwd=tmp_path, env=_env(PATH=str(empty)), timeout_s=30)
    with pytest.raises(ExecutableNotFound):
        run_hardened([name, "-c", "pass"], cwd=tmp_path, env={}, timeout_s=30)
    found = run_hardened(
        [name, "-c", "print('found')"],
        cwd=tmp_path,
        env=_env(PATH=str(Path(sys.executable).parent)),
        timeout_s=30,
    )
    assert found.output.strip() == "found"


def test_output_is_capped_and_the_rest_drained(tmp_path: Path) -> None:
    # 8 MiB would block the child forever if nobody drained the pipe.
    code = "import sys\nfor _ in range(128): sys.stdout.write('x' * 65536)\nprint('end')"
    outcome = run_hardened(_py(code), cwd=tmp_path, env=_env(), timeout_s=60, max_output_bytes=1000)
    assert outcome.exit_code == 0
    assert outcome.truncated
    assert outcome.output == "x" * 1000


def test_output_is_decoded_with_replacement(tmp_path: Path) -> None:
    outcome = run_hardened(
        _py("import sys; sys.stdout.buffer.write(b'ok \\xff\\xfe end')"),
        cwd=tmp_path,
        env=_env(),
        timeout_s=30,
    )
    assert outcome.output == "ok �� end"


GRANDCHILD = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print(child.pid, flush=True)
time.sleep(60)
"""


def test_timeout_kills_the_whole_process_tree(tmp_path: Path) -> None:
    started = time.monotonic()
    outcome = run_hardened(_py(GRANDCHILD), cwd=tmp_path, env=_env(), timeout_s=2)
    assert outcome.timed_out
    assert outcome.exit_code is None
    assert time.monotonic() - started < 20
    grandchild = int(outcome.output.split()[0])
    assert _wait_gone(grandchild), "the grandchild survived the timeout"


LEAVES_A_PIPE_HOLDER = """
import subprocess, sys
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
print(child.pid, flush=True)
"""


def test_returns_when_a_leftover_process_holds_the_pipe(tmp_path: Path) -> None:
    started = time.monotonic()
    outcome = run_hardened(_py(LEAVES_A_PIPE_HOLDER), cwd=tmp_path, env=_env(), timeout_s=30)
    assert outcome.exit_code == 0
    assert not outcome.timed_out
    assert time.monotonic() - started < 15
    leftover = int(outcome.output.split()[0])
    if sys.platform != "win32":
        # POSIX kills what is left of the process group.
        assert _wait_gone(leftover)
