"""History: the Checkpoints of each project, kept in a git shadow repository.

Each project's History is a bare repository at `DataLayout.history_git(id)`
whose work tree is the project folder (ADR 0004). Git never writes inside the
project folder: it gets no `.git`, a repository the user keeps there is never
read or changed, and restored files are written through ProjectStore.

Git runs with no shell, no system or global configuration, no hooks and no
attribute from the project's own `.gitattributes`, so a Checkpoint holds every
file's bytes exactly as they are on disk (agent design §2.3, §6.5, §10.2).
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, Protocol, get_args

from prismtex.agent.types import FileState
from prismtex.errors import CheckpointNotFound, PathPolicyError, VersionConflict
from prismtex.workspace import (
    ProjectStore,
    check_relative_path,
    content_version,
    decode_text,
    file_kind,
)

__all__ = [
    "BUILD_SUFFIXES",
    "MAX_FILE_BYTES",
    "Checkpoint",
    "CheckpointReason",
    "GitShadowHistory",
    "History",
    "HistoryError",
    "NullHistory",
    "RestoreResult",
]

CheckpointReason = Literal["save", "accept", "auto_accept", "restore", "import"]
REASONS: Final = frozenset(get_args(CheckpointReason))

# Files larger than this stay out of History (agent design §2.3).
MAX_FILE_BYTES: Final = 20 * 2**20
# Files TeX and latexmk write next to the sources when a project is compiled
# in place, outside the app. Compiles in the app write to build/ instead.
BUILD_SUFFIXES: Final = (
    ".aux",
    ".bcf",
    ".blg",
    ".dvi",
    ".fdb_latexmk",
    ".fls",
    ".lof",
    ".log",
    ".lot",
    ".nav",
    ".out",
    ".run.xml",
    ".snm",
    ".synctex",
    ".synctex(busy)",
    ".synctex.gz",
    ".synctex.gz(busy)",
    ".toc",
    ".vrb",
    ".xdv",
)

_IDENTITY = ("PrismTeX", "prismtex@localhost")
# A full commit id: SHA-1, or SHA-256 in a repository that uses it.
_COMMIT_ID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_TRAILER_KEY = re.compile(r"[A-Za-z][A-Za-z0-9-]*")
_FILES_CHANGED = re.compile(rb"(\d+) files? changed")
_TURN_SEPARATOR = re.compile(r"[\s,]+")
# One record per commit: a record separator, then id, commit time and body.
_LOG_FORMAT = "--format=%x1e%H%x00%ct%x00%B%x00"
# $GIT_DIR/info/attributes outranks every .gitattributes in the work tree:
# no line-ending conversion, keyword expansion, filter or re-encoding.
_ATTRIBUTES = b"* -text -crlf -ident -filter -working-tree-encoding !eol !diff !merge\n"
_RESTORE_ATTEMPTS = 3
_GC_EVERY = 100


class HistoryError(RuntimeError):
    """A git command of a shadow repository failed or timed out."""


@dataclass(frozen=True)
class Checkpoint:
    """One saved version of a project's files (agent design §10.3).

    `id` is the commit id and `files_changed` counts the files changed since
    the Checkpoint before it. `proposal_id`, `turn_ids` and `model` come from
    the commit's trailers, which are the Provenance record of an accept.
    """

    id: str
    created_at: str
    reason: str
    message: str
    files_changed: int
    proposal_id: str | None = None
    turn_ids: tuple[str, ...] = ()
    model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": self.id,
            "created_at": self.created_at,
            "reason": self.reason,
            "message": self.message,
            "files_changed": self.files_changed,
        }
        if self.proposal_id is not None:
            body["proposal_id"] = self.proposal_id
        if self.turn_ids:
            body["turn_ids"] = list(self.turn_ids)
        if self.model is not None:
            body["model"] = self.model
        return body


@dataclass(frozen=True)
class RestoreResult:
    """The files a restore changed, with their new versions, sorted by path.

    `checkpoint_id` is the `restore` Checkpoint taken after writing, or the
    latest Checkpoint when the project already matched. A deleted file has
    version and text None; a file that is not UTF-8 source text has text None.
    """

    checkpoint_id: str
    files: tuple[FileState, ...]


class History(Protocol):
    """The Checkpoints of each project (agent design §2.3)."""

    async def checkpoint(
        self,
        project_id: str,
        *,
        reason: CheckpointReason,
        message: str,
        trailers: Mapping[str, str] | None = None,
    ) -> Checkpoint | None:
        """Save the project's files; None when nothing changed since the last one.

        The commit message is `message`, a blank line, `Reason: <reason>` and
        then each trailer as `Key: value`, one per line. The `Turns` trailer
        holds turn ids separated by commas.
        """
        ...

    async def list(
        self, project_id: str, *, limit: int = 50, before: str | None = None
    ) -> builtins.list[Checkpoint]:
        """Checkpoints newest first; `before` lists the ones older than that one."""
        ...

    async def diff(
        self,
        project_id: str,
        checkpoint_id: str,
        *,
        against: str | None = None,
        path: str | None = None,
    ) -> str:
        """A unified diff from `against` (default: the Checkpoint before) to the Checkpoint."""
        ...

    async def restore(
        self, project_id: str, checkpoint_id: str, *, paths: Sequence[str] | None = None
    ) -> RestoreResult:
        """Bring files back to how they were at a Checkpoint; `paths` limits it."""
        ...


class NullHistory(History):
    """History when git is not installed: nothing is saved and nothing is listed."""

    async def checkpoint(
        self,
        project_id: str,
        *,
        reason: CheckpointReason,
        message: str,
        trailers: Mapping[str, str] | None = None,
    ) -> Checkpoint | None:
        _commit_message(reason, message, trailers or {})
        return None

    async def list(
        self, project_id: str, *, limit: int = 50, before: str | None = None
    ) -> builtins.list[Checkpoint]:
        _check_limit(limit)
        if before is not None:
            raise CheckpointNotFound(before)
        return []

    async def diff(
        self,
        project_id: str,
        checkpoint_id: str,
        *,
        against: str | None = None,
        path: str | None = None,
    ) -> str:
        raise CheckpointNotFound(checkpoint_id)

    async def restore(
        self, project_id: str, checkpoint_id: str, *, paths: Sequence[str] | None = None
    ) -> RestoreResult:
        raise CheckpointNotFound(checkpoint_id)


@dataclass(frozen=True)
class _Repo:
    project_id: str
    gitdir: Path
    worktree: Path


class GitShadowHistory(History):
    """History in one bare git repository per project, driven by the git CLI.

    Every git call runs `git --git-dir=<history_git(id)> --work-tree=<project
    folder>` from inside the repository, under one asyncio.Lock per project.
    History holds the files ProjectStore lists, except symlinks, compile
    outputs (BUILD_SUFFIXES) and files over `max_file_bytes`. A repository is
    created by the first Checkpoint of its project.
    """

    def __init__(
        self,
        history_dir: Path,
        projects: ProjectStore,
        *,
        git: str,
        max_file_bytes: int = MAX_FILE_BYTES,
        timeout_s: float = 120.0,
    ) -> None:
        # history_dir is DataLayout.history_root; git gets it as HOME, so no
        # user configuration is found. Repositories live at history_git(id).
        self._projects = projects
        self._git = git
        self._max_file_bytes = max_file_bytes
        self._timeout_s = timeout_s
        self._env = _git_env(Path(history_dir))
        self._locks: dict[str, asyncio.Lock] = {}
        self._ready: set[str] = set()
        self._commits: dict[str, int] = {}

    @classmethod
    def detect(cls, history_dir: Path, projects: ProjectStore) -> GitShadowHistory | None:
        """A GitShadowHistory when a working `git` is on PATH, else None."""
        git = shutil.which("git")
        if git is None:
            return None
        try:
            subprocess.run(
                [git, "--version"],
                env=_git_env(Path(history_dir)),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=30,
                check=True,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return cls(history_dir, projects, git=git)

    # -- the History protocol -------------------------------------------------

    async def checkpoint(
        self,
        project_id: str,
        *,
        reason: CheckpointReason,
        message: str,
        trailers: Mapping[str, str] | None = None,
    ) -> Checkpoint | None:
        body = _commit_message(reason, message, trailers or {})
        worktree = self._projects.project_dir(project_id)
        async with self._lock(project_id):
            repo = await self._open(project_id, worktree, create=True)
            assert repo is not None
            return await self._commit(repo, body)

    async def list(
        self, project_id: str, *, limit: int = 50, before: str | None = None
    ) -> builtins.list[Checkpoint]:
        _check_limit(limit)
        worktree = self._projects.project_dir(project_id)
        async with self._lock(project_id):
            repo = await self._open(project_id, worktree, create=False)
            head = None if repo is None else await self._head(repo)
            if repo is None or head is None:
                if before is not None:
                    raise CheckpointNotFound(before)
                return []
            start, skip = head, 0
            if before is not None:
                start, skip = await self._resolve(repo, before), 1
            out = await self._out(
                repo,
                "log",
                "-z",
                "--root",
                "--no-renames",
                "--no-ext-diff",
                "--no-textconv",
                "--shortstat",
                _LOG_FORMAT,
                f"--max-count={limit}",
                f"--skip={skip}",
                start,
                "--",
            )
            return _parse_log(out)

    async def diff(
        self,
        project_id: str,
        checkpoint_id: str,
        *,
        against: str | None = None,
        path: str | None = None,
    ) -> str:
        rel = None if path is None else check_relative_path(path)
        worktree = self._projects.project_dir(project_id)
        async with self._lock(project_id):
            repo = await self._open(project_id, worktree, create=False)
            if repo is None:
                raise CheckpointNotFound(checkpoint_id)
            target = await self._resolve(repo, checkpoint_id)
            args = ["diff-tree", "-p", "-r", "--no-renames", "--no-ext-diff", "--no-textconv"]
            if against is None:
                args += ["--root", "--no-commit-id", target]
            else:
                args += [await self._resolve(repo, against), target]
            args += ["--"] if rel is None else ["--", rel]
            return (await self._out(repo, *args)).decode("utf-8", errors="replace")

    async def restore(
        self, project_id: str, checkpoint_id: str, *, paths: Sequence[str] | None = None
    ) -> RestoreResult:
        """Write the Checkpoint's files back, between a `save` and a `restore` Checkpoint.

        Files are written through ProjectStore, each only while its bytes are
        the ones the `save` Checkpoint holds, so nothing that was on disk is
        lost. A save that lands meanwhile is taken into History too, and the
        restore carries on. A file History does not hold (too large, or a
        compile output) is left alone. `paths` limits the restore to these
        files and folders.
        """
        if isinstance(paths, str):
            raise TypeError("paths is a sequence of paths, not one path")
        wanted = None if paths is None else tuple(dict.fromkeys(map(check_relative_path, paths)))
        worktree = self._projects.project_dir(project_id)
        async with self._lock(project_id):
            repo = await self._open(project_id, worktree, create=False)
            if repo is None:
                raise CheckpointNotFound(checkpoint_id)
            target = await self._resolve(repo, checkpoint_id)
            short = target[:12]
            saving = _commit_message("save", f"Changes before restoring checkpoint {short}", {})
            await self._commit(repo, saving)
            done: dict[str, FileState] = {}
            for attempt in range(_RESTORE_ATTEMPTS):
                try:
                    await self._apply(repo, target, wanted, done)
                    break
                except VersionConflict:
                    if attempt + 1 == _RESTORE_ATTEMPTS:
                        raise
                    await self._commit(repo, saving)
            restoring = _commit_message("restore", f"Restore checkpoint {short}", {})
            restored = await self._commit(repo, restoring)
            head = restored.id if restored is not None else await self._head(repo)
            if head is None:
                raise HistoryError("the history has no checkpoint after a restore")
            return RestoreResult(checkpoint_id=head, files=tuple(done[p] for p in sorted(done)))

    # -- repository ---------------------------------------------------------

    def _lock(self, project_id: str) -> asyncio.Lock:
        lock = self._locks.get(project_id)
        if lock is None:
            lock = self._locks[project_id] = asyncio.Lock()
        return lock

    async def _open(self, project_id: str, worktree: Path, *, create: bool) -> _Repo | None:
        """The project's repository, created first when `create`; None if absent."""
        gitdir = self._projects.layout.history_git(project_id)
        repo = _Repo(project_id, gitdir, worktree)
        exists = (gitdir / "HEAD").is_file()
        if exists and project_id in self._ready:
            return repo
        if not exists:
            if not create:
                return None
            gitdir.parent.mkdir(parents=True, exist_ok=True)
            # No template: the repository starts with no hooks and no excludes.
            init = ["init", "-q", "--bare", "--template=", str(gitdir)]
            await self._run(None, init, cwd=gitdir.parent)
        # The lock is held and this process is the repository's only user, so
        # lock files left by a crash or a killed git are stale.
        _remove_stale_locks(gitdir)
        attributes = gitdir / "info" / "attributes"
        if not attributes.is_file() or attributes.read_bytes() != _ATTRIBUTES:
            attributes.parent.mkdir(exist_ok=True)
            attributes.write_bytes(_ATTRIBUTES)
        self._ready.add(project_id)
        return repo

    async def _head(self, repo: _Repo) -> str | None:
        code, out = await self._run(
            repo, ["rev-parse", "-q", "--verify", "HEAD^{commit}"], cwd=repo.gitdir, check=False
        )
        if code == 0:
            return out.decode("ascii").strip()
        if code == 1:
            return None
        raise HistoryError(f"git rev-parse HEAD failed with exit code {code}")

    async def _resolve(self, repo: _Repo, checkpoint_id: str) -> str:
        """The full commit id of a Checkpoint, or CheckpointNotFound."""
        if not _COMMIT_ID.fullmatch(checkpoint_id):
            raise CheckpointNotFound(checkpoint_id)
        code, out = await self._run(
            repo,
            ["rev-parse", "-q", "--verify", f"{checkpoint_id}^{{commit}}"],
            cwd=repo.gitdir,
            check=False,
        )
        if code != 0:
            raise CheckpointNotFound(checkpoint_id)
        return out.decode("ascii").strip()

    async def _commit(self, repo: _Repo, body: str) -> Checkpoint | None:
        head = await self._head(repo)
        selected = await asyncio.to_thread(self._snapshot_paths, repo)
        tracked = _split_z(await self._out(repo, "ls-files", "-z"))
        gone = sorted(set(tracked).difference(selected))
        if gone:
            await self._out(
                repo, "update-index", "--force-remove", "-z", "--stdin", stdin=_join_z(gone)
            )
        if selected:
            await self._out(
                repo,
                "update-index",
                "--add",
                "--remove",
                "--replace",
                "-z",
                "--stdin",
                stdin=_join_z(selected),
            )
        base = head
        if base is None:  # the first Checkpoint is compared with the empty tree
            empty = await self._out(repo, "hash-object", "-t", "tree", "--stdin", stdin=b"")
            base = empty.decode("ascii").strip()
        stat = await self._out(repo, "diff-index", "--cached", "--shortstat", "--no-renames", base)
        files_changed = _files_changed(stat)
        if not files_changed:
            return None
        tree = (await self._out(repo, "write-tree")).decode("ascii").strip()
        seconds = int(time.time())
        date = f"@{seconds} +0000"
        parents = ["-p", head] if head else []
        commit = await self._out(
            repo,
            "commit-tree",
            tree,
            *parents,
            stdin=body.encode("utf-8"),
            env={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date},
        )
        commit_id = commit.decode("ascii").strip()
        # The old value makes this a compare-and-swap: "" means no commit yet.
        await self._out(repo, "update-ref", "HEAD", commit_id, head or "")
        await self._maybe_gc(repo)
        return _make_checkpoint(commit_id, seconds, body, files_changed)

    def _snapshot_paths(self, repo: _Repo) -> builtins.list[str]:
        """The files a Checkpoint holds: what ProjectStore lists, minus exclusions.

        A path the path policy would refuse is left out too, so that every
        file History holds can be restored through ProjectStore.
        """
        paths: builtins.list[str] = []
        for entry in self._projects.list_files(repo.project_id):
            if entry.size > self._max_file_bytes or entry.path.lower().endswith(BUILD_SUFFIXES):
                continue
            try:
                if check_relative_path(entry.path) != entry.path:
                    continue
                if (repo.worktree / entry.path).is_symlink():
                    continue
            except (PathPolicyError, UnicodeEncodeError, OSError):
                continue
            paths.append(entry.path)
        return paths

    async def _apply(
        self, repo: _Repo, target: str, wanted: tuple[str, ...] | None, done: dict[str, FileState]
    ) -> None:
        held = _parse_tree(await self._out(repo, "ls-tree", "-r", "-z", "--full-tree", "HEAD"))
        goal = _parse_tree(await self._out(repo, "ls-tree", "-r", "-z", "--full-tree", target))
        chosen: Callable[[str], bool] = (
            (lambda _path: True)
            if wanted is None
            else (lambda path: any(path == w or path.startswith(w + "/") for w in wanted))
        )
        deletes = sorted(p for p in held if p not in goal and chosen(p))
        writes = sorted(p for p, oid in goal.items() if held.get(p) != oid and chosen(p))
        oids = {goal[p] for p in writes} | {held[p] for p in [*deletes, *writes] if p in held}
        blobs = await self._blobs(repo, oids)
        await asyncio.to_thread(self._write_files, repo, held, goal, deletes, writes, blobs, done)

    def _write_files(
        self,
        repo: _Repo,
        held: dict[str, str],
        goal: dict[str, str],
        deletes: builtins.list[str],
        writes: builtins.list[str],
        blobs: dict[str, bytes],
        done: dict[str, FileState],
    ) -> None:
        # Deletes go first, so a path whose case or kind changed (a file that
        # became a folder) is free when it is written.
        pid = repo.project_id
        for path in deletes:
            self._projects.delete_file(pid, path, base_version=content_version(blobs[held[path]]))
            done[path] = FileState(path=path, version=None, text=None)
            _prune_empty_parents(repo.worktree, path)
        for path in writes:
            data = blobs[goal[path]]
            old = held.get(path)
            if old is None and os.path.lexists(repo.worktree / path):
                continue  # History holds no copy of what is there now
            expected = None if old is None else content_version(blobs[old])
            version = self._projects.replace_bytes(pid, path, data, expected_version=expected)
            done[path] = FileState(path=path, version=version, text=_source_text(path, data))

    async def _blobs(self, repo: _Repo, oids: set[str]) -> dict[str, bytes]:
        if not oids:
            return {}
        request = "".join(f"{oid}\n" for oid in sorted(oids)).encode("ascii")
        return _parse_batch(await self._out(repo, "cat-file", "--batch", stdin=request))

    async def _maybe_gc(self, repo: _Repo) -> None:
        # Commits are made with plumbing, which never packs loose objects, so
        # History asks for git's own housekeeping every so often.
        count = self._commits.get(repo.project_id, 0) + 1
        self._commits[repo.project_id] = count
        if count % _GC_EVERY == 0:
            await self._run(repo, ["gc", "--auto", "--quiet"], cwd=repo.gitdir, check=False)

    # -- running git ----------------------------------------------------------

    async def _out(
        self,
        repo: _Repo,
        *args: str,
        stdin: bytes | None = None,
        env: Mapping[str, str] | None = None,
    ) -> bytes:
        _code, out = await self._run(repo, list(args), cwd=repo.gitdir, stdin=stdin, env=env)
        return out

    async def _run(
        self,
        repo: _Repo | None,
        args: builtins.list[str],
        *,
        cwd: Path,
        stdin: bytes | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
    ) -> tuple[int, bytes]:
        argv = [self._git]
        if repo is not None:
            argv += [f"--git-dir={repo.gitdir}", f"--work-tree={repo.worktree}"]
        argv += _config_args(repo) + args
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env={**self._env, **env} if env else self._env,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(self._timeout_s):
                out, err = await proc.communicate(stdin)
        except BaseException as exc:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
            if repo is not None:
                self._ready.discard(repo.project_id)  # clear its lock files next time
            if isinstance(exc, TimeoutError):
                raise HistoryError(f"git {args[0]} timed out after {self._timeout_s:g} s") from None
            raise
        code = proc.returncode
        assert code is not None
        if check and code != 0:
            detail = err.decode("utf-8", errors="replace").strip()[-500:]
            raise HistoryError(f"git {args[0]} failed with exit code {code}: {detail}")
        return code, out


# -- helpers --------------------------------------------------------------------


def _git_env(home: Path) -> dict[str, str]:
    """The environment of every git call: no inherited GIT_* and no user config."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.pop("LANGUAGE", None)
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_LITERAL_PATHSPECS": "1",
            "LC_ALL": "C",  # untranslated output, which --shortstat parsing relies on
        }
    )
    return env


def _config_args(repo: _Repo | None) -> builtins.list[str]:
    settings = [
        "core.autocrlf=false",
        "core.safecrlf=false",
        "core.fileMode=false",
        f"core.hooksPath={os.devnull}",
        f"core.attributesFile={os.devnull}",
        f"core.excludesFile={os.devnull}",
        "core.fsmonitor=false",
        "core.longpaths=true",
        "core.quotePath=false",
        "commit.gpgSign=false",
        "gc.autoDetach=false",
        "maintenance.autoDetach=false",
        "init.defaultBranch=main",
        f"user.name={_IDENTITY[0]}",
        f"user.email={_IDENTITY[1]}",
    ]
    if repo is not None:
        settings += [
            f"safe.directory={repo.gitdir.as_posix()}",
            f"safe.directory={repo.worktree.as_posix()}",
        ]
    args: builtins.list[str] = []
    for setting in settings:
        args += ["-c", setting]
    return args


def _check_limit(limit: int) -> None:
    if limit < 1:
        raise ValueError("limit must be at least 1")


def _has_control(text: str, *, allow: str = "") -> bool:
    return any((ord(ch) < 32 or ord(ch) == 127) and ch not in allow for ch in text)


def _commit_message(reason: str, message: str, trailers: Mapping[str, str]) -> str:
    """`message`, a blank line, then `Reason: <reason>` and the trailers."""
    if reason not in REASONS:
        raise ValueError(f"unknown checkpoint reason {reason!r}")
    text = message.replace("\r\n", "\n").strip()
    if not text:
        raise ValueError("a checkpoint needs a message")
    if _has_control(text, allow="\n\t"):
        raise ValueError("a checkpoint message may not hold control characters")
    lines = [f"Reason: {reason}"]
    for key, value in trailers.items():
        if not _TRAILER_KEY.fullmatch(key) or key.lower() == "reason":
            raise ValueError(f"invalid trailer key {key!r}")
        if _has_control(value):
            raise ValueError(f"the {key} trailer may not hold control characters or newlines")
        lines.append(f"{key}: {value.strip()}")
    return text + "\n\n" + "\n".join(lines) + "\n"


def _parse_body(body: str) -> tuple[str, dict[str, str]]:
    """(message, trailers) of a commit body; trailers are its last paragraph."""
    text = body.rstrip("\n")
    message, sep, block = text.rpartition("\n\n")
    if not sep:
        return text, {}
    trailers: dict[str, str] = {}
    for line in block.split("\n"):
        key, colon, value = line.partition(":")
        if not colon or not _TRAILER_KEY.fullmatch(key):
            return text, {}
        trailers[key] = value.strip()
    return message, trailers


def _make_checkpoint(commit_id: str, seconds: int, body: str, files_changed: int) -> Checkpoint:
    message, trailers = _parse_body(body)
    return Checkpoint(
        id=commit_id,
        created_at=datetime.fromtimestamp(seconds, UTC).isoformat(),
        reason=trailers.get("Reason", ""),
        message=message,
        files_changed=files_changed,
        proposal_id=trailers.get("Proposal") or None,
        turn_ids=tuple(t for t in _TURN_SEPARATOR.split(trailers.get("Turns", "")) if t),
        model=trailers.get("Model") or None,
    )


def _files_changed(shortstat: bytes) -> int:
    match = _FILES_CHANGED.search(shortstat)
    return int(match.group(1)) if match else 0


def _parse_log(out: bytes) -> builtins.list[Checkpoint]:
    checkpoints: builtins.list[Checkpoint] = []
    for record in out.split(b"\x1e")[1:]:
        commit_id, seconds, body, rest = record.split(b"\x00", 3)
        checkpoints.append(
            _make_checkpoint(
                commit_id.decode("ascii"),
                int(seconds),
                body.decode("utf-8", errors="replace"),
                _files_changed(rest),
            )
        )
    return checkpoints


def _split_z(out: bytes) -> builtins.list[str]:
    return [item.decode("utf-8", errors="surrogateescape") for item in out.split(b"\x00") if item]


def _join_z(paths: Sequence[str]) -> bytes:
    return b"".join(path.encode("utf-8") + b"\x00" for path in paths)


def _parse_tree(out: bytes) -> dict[str, str]:
    """path -> blob id of the regular files in `git ls-tree -r -z` output."""
    entries: dict[str, str] = {}
    for record in out.split(b"\x00"):
        if not record:
            continue
        meta, _tab, path = record.partition(b"\t")
        mode, kind, oid = meta.split(b" ")
        if kind == b"blob" and mode in (b"100644", b"100755"):
            entries[path.decode("utf-8", errors="surrogateescape")] = oid.decode("ascii")
    return entries


def _parse_batch(out: bytes) -> dict[str, bytes]:
    """oid -> content from `git cat-file --batch` output."""
    blobs: dict[str, bytes] = {}
    pos = 0
    while pos < len(out):
        end = out.index(b"\n", pos)
        header = out[pos:end].split(b" ")
        if len(header) != 3:
            raise HistoryError(f"git cat-file could not read {header[0].decode(errors='replace')}")
        oid, _kind, size = header
        start = end + 1
        blobs[oid.decode("ascii")] = out[start : start + int(size)]
        pos = start + int(size) + 1
    return blobs


def _source_text(path: str, data: bytes) -> str | None:
    if file_kind(path) != "source":
        return None
    try:
        return decode_text(data)[0]
    except UnicodeDecodeError:
        return None


def _remove_stale_locks(gitdir: Path) -> None:
    candidates = [gitdir / "index.lock", gitdir / "HEAD.lock", gitdir / "packed-refs.lock"]
    refs = gitdir / "refs"
    if refs.is_dir():
        candidates += refs.rglob("*.lock")
    for lock in candidates:
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass


def _prune_empty_parents(root: Path, rel: str) -> None:
    """Remove the folders above a deleted file that it left empty."""
    parent = Path(rel).parent
    while parent != Path("."):
        folder = root / parent
        if folder.is_symlink():
            return
        try:
            folder.rmdir()
        except OSError:
            return
        parent = parent.parent
