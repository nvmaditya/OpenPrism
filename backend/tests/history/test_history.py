"""History: the git shadow repository of each project (agent design §2.3, §6.5, §10.2)."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from prismtex.agent.types import FileState
from prismtex.errors import CheckpointNotFound, ProjectNotFound, VersionConflict
from prismtex.history import Checkpoint, GitShadowHistory, NullHistory
from prismtex.workspace import FileVersion, ProjectStore, content_version

Project = tuple[ProjectStore, str]
Put = Callable[[ProjectStore, str, str, str], FileVersion]


def _env() -> dict[str, str]:
    # The test's own git calls ignore the developer's config as well.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, LC_ALL="C")
    return env


def git_bytes(*args: str, cwd: Path | None = None) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=_env(), capture_output=True, check=True
    ).stdout


def git(*args: str, cwd: Path | None = None) -> str:
    return git_bytes(*args, cwd=cwd).decode("utf-8").replace("\r\n", "\n")


def gitdir_of(store: ProjectStore, pid: str) -> str:
    return f"--git-dir={store.layout.history_git(pid)}"


def git_path() -> str:
    found = shutil.which("git")
    assert found is not None
    return found


@pytest.fixture
def history(blank_project: Project) -> GitShadowHistory:
    store, _pid = blank_project
    found = GitShadowHistory.detect(store.layout.history_root, store)
    assert found is not None
    return found


async def save(history: GitShadowHistory, pid: str, message: str = "Saved edits") -> Checkpoint:
    cp = await history.checkpoint(pid, reason="save", message=message)
    assert cp is not None
    return cp


@pytest.mark.anyio
async def test_checkpoint_commits_with_trailers_and_ignores_the_users_own_git(
    blank_project: Project, history: GitShadowHistory
) -> None:
    store, pid = blank_project
    proj = store.project_dir(pid)
    git("init", "-q", cwd=proj)
    git(
        "-c", "user.name=u", "-c", "user.email=u@example.invalid",
        "commit", "-q", "--allow-empty", "-m", "mine",
        cwd=proj,
    )  # fmt: skip
    cp = await history.checkpoint(
        pid,
        reason="accept",
        message="Accept proposal t_1: 1 change in main.tex",
        trailers={
            "Proposal": "t_1",
            "Turns": "t_1",
            "Model": "fake:m",
            "Profile": "agentic",
            "Hunks": "h0123456789ab",
        },
    )
    assert cp is not None and cp.reason == "accept" and cp.proposal_id == "t_1"
    assert cp.model == "fake:m"
    gitdir = gitdir_of(store, pid)
    body = git(gitdir, "log", "-1", "--format=%B")
    assert "Proposal: t_1\n" in body and "Model: fake:m\n" in body
    tracked = git(gitdir, "ls-tree", "-r", "--name-only", "HEAD").split()
    assert not any(p.startswith(".git/") for p in tracked)
    assert git("log", "--format=%s", cwd=proj) == "mine\n"
    assert await history.checkpoint(pid, reason="save", message="again") is None


@pytest.mark.anyio
async def test_list_diff_and_restore_round_trip_with_paths(
    blank_project: Project, history: GitShadowHistory, put: Put
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", "one\n")
    put(store, pid, "sections/a.tex", "A\n")
    first = await save(history, pid, "First")
    put(store, pid, "main.tex", "two\n")
    put(store, pid, "b.tex", "B\n")
    store.delete_file(pid, "sections/a.tex")
    second = await save(history, pid, "Second")
    assert (first.files_changed, second.files_changed) == (2, 3)

    assert await history.list(pid) == [second, first]
    assert await history.list(pid, limit=1) == [second]
    assert await history.list(pid, before=second.id) == [first]
    assert await history.list(pid, before=first.id) == []

    made = await history.diff(pid, second.id)
    assert "-one\n+two\n" in made and "+B\n" in made and "deleted file mode" in made
    only_main = await history.diff(pid, second.id, path="main.tex")
    assert "a/main.tex" in only_main and "b.tex" not in only_main and "a.tex" not in only_main
    assert "-two\n+one\n" in await history.diff(pid, first.id, against=second.id, path="main.tex")
    assert "+one\n" in await history.diff(pid, first.id, path="main.tex")

    partial = await history.restore(pid, first.id, paths=["main.tex"])
    assert [f.path for f in partial.files] == ["main.tex"]
    assert store.read(pid, "main.tex").text == "one\n"
    assert store.read(pid, "b.tex").text == "B\n"

    full = await history.restore(pid, first.id)
    assert {f.path: f.version for f in full.files} == {
        "b.tex": None,
        "sections/a.tex": content_version(b"A\n"),
    }
    assert [e.path for e in store.list_files(pid)] == ["main.tex", "sections/a.tex"]
    assert full.checkpoint_id == (await history.list(pid, limit=1))[0].id
    assert not (store.project_dir(pid) / ".git").exists()


@pytest.mark.anyio
async def test_restore_takes_a_save_checkpoint_first_then_a_restore_checkpoint(
    blank_project: Project, history: GitShadowHistory, put: Put
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", "saved\n")
    first = await save(history, pid)
    put(store, pid, "main.tex", "not yet in history\n")

    result = await history.restore(pid, first.id)
    latest, saved, original = await history.list(pid)
    assert [latest.reason, saved.reason, original.reason] == ["restore", "save", "save"]
    assert original == first and result.checkpoint_id == latest.id
    assert "+not yet in history\n" in await history.diff(pid, saved.id)
    assert "+saved\n" in await history.diff(pid, latest.id)
    assert first.id[:12] in saved.message and first.id[:12] in latest.message
    assert store.read(pid, "main.tex").text == "saved\n"

    # Restoring a state the project already has adds no Checkpoint.
    again = await history.restore(pid, first.id)
    assert again.files == () and again.checkpoint_id == latest.id
    assert len(await history.list(pid)) == 3


@pytest.mark.anyio
async def test_crlf_bytes_are_stored_and_restored_unchanged(
    blank_project: Project, history: GitShadowHistory
) -> None:
    store, pid = blank_project
    proj = store.project_dir(pid)
    originals = {
        "main.tex": b"\\documentclass{article}\r\n% $Id: kept $\r\n\\begin{document}\r\nHi\r\n",
        "mixed.tex": b"one\r\ntwo\nthree\r\n",
        "refs.bib": b"@book{k, author={Ren\xe9}}\r\n",  # Latin-1, not UTF-8
        "fig.png": b"\x89PNG\r\n\x1a\n\x00\r\n\xff",
    }
    # The project's own attributes and ignores do not change what History stores.
    (proj / ".gitattributes").write_bytes(b"* text=auto eol=lf\n*.tex ident filter=lfs\n")
    (proj / ".gitignore").write_bytes(b"*.bib\n*.png\n")
    for path, data in originals.items():
        (proj / path).write_bytes(data)
    cp = await history.checkpoint(pid, reason="import", message="Imported")
    assert cp is not None and cp.files_changed == 6
    for path, data in originals.items():
        assert git_bytes(gitdir_of(store, pid), "cat-file", "blob", f"HEAD:{path}") == data

    for path in originals:
        (proj / path).write_bytes(b"changed\n")
    await history.restore(pid, cp.id)
    for path, data in originals.items():
        assert (proj / path).read_bytes() == data


@pytest.mark.anyio
async def test_null_history_returns_none_and_empty_lists() -> None:
    history = NullHistory()
    assert await history.checkpoint("p", reason="accept", message="Accept") is None
    assert await history.list("p") == []
    with pytest.raises(CheckpointNotFound):
        await history.list("p", before="0" * 40)
    with pytest.raises(CheckpointNotFound):
        await history.diff("p", "0" * 40)
    with pytest.raises(CheckpointNotFound):
        await history.restore("p", "0" * 40)


def test_detect_returns_none_when_git_is_missing(
    blank_project: Project, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store, _pid = blank_project
    assert GitShadowHistory.detect(store.layout.history_root, store) is not None
    monkeypatch.setattr(shutil, "which", lambda *_args, **_kwargs: None)
    assert GitShadowHistory.detect(store.layout.history_root, store) is None
    broken = str(tmp_path / "no-such-git")
    monkeypatch.setattr(shutil, "which", lambda *_args, **_kwargs: broken)
    assert GitShadowHistory.detect(store.layout.history_root, store) is None


@pytest.mark.anyio
async def test_concurrent_checkpoints_on_one_project_are_serialized(
    blank_project: Project, history: GitShadowHistory, put: Put, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, pid = blank_project
    running = 0
    peak = 0
    run = history._run

    async def counting(*args: Any, **kwargs: Any) -> tuple[int, bytes]:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        try:
            await asyncio.sleep(0)  # give the other calls a chance to interleave
            return await run(*args, **kwargs)
        finally:
            running -= 1

    monkeypatch.setattr(history, "_run", counting)
    put(store, pid, "main.tex", "edited\n")
    results = await asyncio.gather(
        *(history.checkpoint(pid, reason="save", message=f"Save {i}") for i in range(5))
    )
    assert peak == 1
    assert sum(cp is not None for cp in results) == 1
    assert len(await history.list(pid)) == 1


@pytest.mark.anyio
async def test_restore_result_lists_file_states_with_new_versions(
    blank_project: Project, history: GitShadowHistory, put: Put
) -> None:
    store, pid = blank_project
    proj = store.project_dir(pid)
    put(store, pid, "a.tex", "alpha\n")
    (proj / "win.tex").write_bytes(b"x\r\ny\r\n")
    (proj / "fig.png").write_bytes(b"\x89PNG one")
    cp = await save(history, pid)
    put(store, pid, "a.tex", "changed\n")
    store.delete_file(pid, "win.tex")
    (proj / "fig.png").write_bytes(b"\x89PNG two")
    put(store, pid, "c.tex", "new\n")

    result = await history.restore(pid, cp.id)
    assert result.files == (
        FileState(path="a.tex", version=content_version(b"alpha\n"), text="alpha\n"),
        FileState(path="c.tex", version=None, text=None),
        FileState(path="fig.png", version=content_version(b"\x89PNG one"), text=None),
        FileState(path="win.tex", version=content_version(b"x\r\ny\r\n"), text="x\ny\n"),
    )
    for state in result.files:
        if state.version is not None:
            assert store.read_bytes(pid, state.path)[1] == state.version
    assert not (proj / "c.tex").exists()


@pytest.mark.anyio
async def test_the_users_git_config_environment_and_hooks_change_nothing(
    blank_project: Project, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, put: Put
) -> None:
    store, pid = blank_project
    proj = store.project_dir(pid)
    marker = tmp_path / "hook-ran"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for name in ("reference-transaction", "pre-commit", "post-commit", "post-index-change"):
        hook = hooks / name
        hook.write_text(f"#!/bin/sh\necho {name} >> '{marker.as_posix()}'\n", newline="\n")
        hook.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    config = home / ".gitconfig"
    config.write_text(
        f"[core]\n\tautocrlf = true\n\thooksPath = {hooks.as_posix()}\n"
        "[commit]\n\tgpgSign = true\n[user]\n\tname = Someone Else\n",
        newline="\n",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_DIR", str(proj / ".git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "index"))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Someone Else")
    history = GitShadowHistory.detect(store.layout.history_root, store)
    assert history is not None

    (proj / "main.tex").write_bytes(b"a\r\nb\r\n")
    await save(history, pid)
    gitdir = gitdir_of(store, pid)
    assert git(gitdir, "log", "-1", "--format=%an <%ae>|%cn <%ce>") == (
        "PrismTeX <prismtex@localhost>|PrismTeX <prismtex@localhost>\n"
    )
    assert git_bytes(gitdir, "cat-file", "blob", "HEAD:main.tex") == b"a\r\nb\r\n"
    assert not (proj / ".git").exists() and not (tmp_path / "index").exists()

    # Hooks placed in the shadow repository itself do not run either.
    shutil.copytree(hooks, store.layout.history_git(pid) / "hooks")
    put(store, pid, "main.tex", "c\n")
    await save(history, pid)
    assert not marker.exists()


@pytest.mark.anyio
async def test_large_files_and_compile_outputs_stay_out_and_restore_leaves_them_alone(
    blank_project: Project,
) -> None:
    store, pid = blank_project
    proj = store.project_dir(pid)
    history = GitShadowHistory(store.layout.history_root, store, git=git_path(), max_file_bytes=64)
    (proj / "big.png").write_bytes(b"x" * 65)
    (proj / "small.png").write_bytes(b"x" * 64)
    (proj / "main.aux").write_bytes(b"\\relax\n")
    (proj / "sub").mkdir()
    (proj / "sub" / "main.synctex.gz").write_bytes(b"gz")
    (proj / "main.tex").write_bytes(b"short\n")
    cp = await save(history, pid)
    tracked = set(git(gitdir_of(store, pid), "ls-tree", "-r", "--name-only", "HEAD").split())
    assert tracked == {"main.tex", "small.png"}

    # A file that grows past the limit leaves History; restore does not overwrite it.
    (proj / "small.png").write_bytes(b"y" * 65)
    grown = await save(history, pid)
    assert grown.files_changed == 1
    result = await history.restore(pid, cp.id)
    assert result.files == () and result.checkpoint_id == grown.id
    assert (proj / "small.png").read_bytes() == b"y" * 65


@pytest.mark.anyio
async def test_a_save_that_lands_during_a_restore_is_kept_in_history(
    blank_project: Project, history: GitShadowHistory, put: Put, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", "checkpointed\n")
    cp = await save(history, pid)
    put(store, pid, "main.tex", "before the restore\n")
    replace_bytes = store.replace_bytes
    calls = 0

    def racing(project_id: str, path: str, data: bytes, *, expected_version: str | None) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            put(store, project_id, path, "saved during the restore\n")
        return replace_bytes(project_id, path, data, expected_version=expected_version)

    monkeypatch.setattr(store, "replace_bytes", racing)
    await history.restore(pid, cp.id)
    assert store.read(pid, "main.tex").text == "checkpointed\n"
    listed = await history.list(pid)
    assert [c.reason for c in listed] == ["restore", "save", "save", "save"]
    assert "+saved during the restore\n" in await history.diff(pid, listed[1].id)
    assert "+before the restore\n" in await history.diff(pid, listed[2].id)

    # A restore that keeps losing the race gives up without overwriting anything.
    def always_racing(project_id: str, path: str, data: bytes, **kwargs: Any) -> str:
        put(store, project_id, path, f"saved again {calls}\n")
        raise VersionConflict(path, None)

    monkeypatch.setattr(store, "replace_bytes", always_racing)
    with pytest.raises(VersionConflict):
        await history.restore(pid, listed[2].id)
    assert store.read(pid, "main.tex").text.startswith("saved again")


@pytest.mark.anyio
async def test_a_lock_file_left_by_a_crash_does_not_block_the_next_checkpoint(
    blank_project: Project, history: GitShadowHistory, put: Put
) -> None:
    store, pid = blank_project
    await save(history, pid)
    gitdir = store.layout.history_git(pid)
    (gitdir / "index.lock").write_bytes(b"")
    (gitdir / "refs" / "heads" / "main.lock").write_bytes(b"")
    put(store, pid, "main.tex", "after the crash\n")
    restarted = GitShadowHistory(store.layout.history_root, store, git=git_path())
    assert await restarted.checkpoint(pid, reason="save", message="After restart") is not None
    assert not (gitdir / "index.lock").exists()


@pytest.mark.anyio
async def test_trailers_round_trip_and_bad_input_is_refused_before_git_runs(
    blank_project: Project, history: GitShadowHistory
) -> None:
    store, pid = blank_project
    bad: list[dict[str, Any]] = [
        {"reason": "oops", "message": "x"},
        {"reason": "save", "message": "  \n"},
        {"reason": "save", "message": "a\x00b"},
        {"reason": "save", "message": "x", "trailers": {"Model": "a\nb"}},
        {"reason": "save", "message": "x", "trailers": {"Bad Key": "x"}},
        {"reason": "save", "message": "x", "trailers": {"Reason": "accept"}},
    ]
    for kwargs in bad:
        with pytest.raises(ValueError):
            await history.checkpoint(pid, **kwargs)
    assert not store.layout.history_git(pid).exists()

    cp = await history.checkpoint(
        pid,
        reason="auto_accept",
        message="Accept proposal t_2: 2 changes in main.tex\n\nSecond paragraph.",
        trailers={"Proposal": "t_2", "Turns": "t_1, t_2", "Model": "ollama:qwen3:4b"},
    )
    assert cp is not None and cp.turn_ids == ("t_1", "t_2")
    assert cp.message == "Accept proposal t_2: 2 changes in main.tex\n\nSecond paragraph."
    assert await history.list(pid) == [cp]
    assert cp.to_dict() == {
        "id": cp.id,
        "created_at": cp.created_at,
        "reason": "auto_accept",
        "message": cp.message,
        "files_changed": 1,
        "proposal_id": "t_2",
        "turn_ids": ["t_1", "t_2"],
        "model": "ollama:qwen3:4b",
    }
    assert set(Checkpoint("c", "t", "save", "m", 0).to_dict()) == {
        "id",
        "created_at",
        "reason",
        "message",
        "files_changed",
    }


@pytest.mark.anyio
async def test_unknown_and_malformed_checkpoint_ids_are_not_found(
    blank_project: Project, history: GitShadowHistory
) -> None:
    store, pid = blank_project
    cp = await save(history, pid)
    blob = git(gitdir_of(store, pid), "rev-parse", "HEAD:main.tex").strip()
    for bad in ["0" * 40, "HEAD", "--output=x", cp.id[:12], cp.id.upper(), blob]:
        with pytest.raises(CheckpointNotFound):
            await history.diff(pid, bad)
        with pytest.raises(CheckpointNotFound):
            await history.diff(pid, cp.id, against=bad)
        with pytest.raises(CheckpointNotFound):
            await history.restore(pid, bad)
        with pytest.raises(CheckpointNotFound):
            await history.list(pid, before=bad)
    assert await history.list(pid) == [cp]

    other = store.create(kind="blank").id
    assert await history.list(other) == []
    with pytest.raises(CheckpointNotFound):
        await history.restore(other, cp.id)
    assert not store.layout.history_git(other).exists()
    for unknown in ["0" * 32, "../" + pid]:
        with pytest.raises(ProjectNotFound):
            await history.checkpoint(unknown, reason="save", message="x")
        with pytest.raises(ProjectNotFound):
            await history.list(unknown)
    with pytest.raises(ValueError):
        await history.list(pid, limit=0)
    with pytest.raises(TypeError):
        await history.restore(pid, cp.id, paths=cast(Any, "main.tex"))
