"""Source trees, scratch compiles and the shared compile cache (agent design §2.3, §7.1, §7.2).

A `SourceTree` is a Project as a compile would see it: the Project folder
with an overlay of text on top (staged edits, an unsaved editor buffer), plus
a digest of everything the compile reads. A `TreeCompiler` compiles trees and
caches each result under sha256(toolchain, digest), so the same content is
compiled once whoever asks.

`ScratchTreeCompiler` is the TeX one. A Baseline or Proposal tree is written
to `build/<project_id>/scratch/<turn_id>/<digest>/src/` and compiled into the
`out/` folder beside it, through `Compiler.compile_dir`, which takes no
per-Project lock; the Project folder is never written. A user tree (the
Compile button: no overlay) goes through `Compiler.compile`, under the lock,
into `build/<project_id>/out/`.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import stat
import sys
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Literal, Protocol

from prismtex.cancel import CancelToken
from prismtex.compile.compiler import CompileBackend, Compiler, CompileResult
from prismtex.compile.logparse import Diagnostic
from prismtex.errors import PathPolicyError, RootFileMissing
from prismtex.layout import DataLayout
from prismtex.settings import Settings
from prismtex.workspace import (
    Engine,
    Eol,
    ProjectStore,
    check_relative_path,
    content_version,
    detect_eol,
    encode_text,
    is_internal,
    resolve_in,
)

__all__ = [
    "Purpose",
    "ScratchTreeCompiler",
    "SourceTree",
    "TreeCompiler",
    "source_tree",
    "tree_digest",
]

Purpose = Literal["user", "baseline", "proposal"]

_EMPTY: Mapping[str, str | None] = MappingProxyType({})
_TURN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
# Statuses worth keeping: a timeout can pass on a quieter machine, and an
# unavailable toolchain can be installed.
_CACHED_STATUSES = frozenset({"ok", "failed"})
# TeX on Windows can hold a file open for a moment after it exits.
_RMTREE_ATTEMPTS = 10 if sys.platform == "win32" else 1
_RMTREE_DELAY_S = 0.2


@dataclass(frozen=True)
class SourceTree:
    """A Project with an overlay of text, as one compile input.

    `overlay` maps a path to LF text, or None for a file deleted in the tree;
    `eols` the line ending each overlaid text is written with; `inputs` each
    Source file's content version of its effective bytes; `assets` each
    Asset's (size, mtime_ns) in the Project folder. `digest` covers the Root
    file, the engine, `inputs` and `assets`.
    """

    project_id: str
    root_file: str
    engine: Engine
    overlay: Mapping[str, str | None]
    eols: Mapping[str, Eol]
    inputs: Mapping[str, str]
    digest: str
    assets: Mapping[str, tuple[int, int]] = field(default_factory=lambda: MappingProxyType({}))


def tree_digest(
    root_file: str,
    engine: Engine,
    inputs: Mapping[str, str],
    assets: Mapping[str, tuple[int, int]],
) -> str:
    """sha256 over the Root file, the engine, the sorted inputs and the Assets' sizes and mtimes."""
    digest = hashlib.sha256()
    digest.update(f"root {root_file}\nengine {engine}\n".encode())
    for path in sorted(inputs):
        digest.update(f"source {path}:{inputs[path]}\n".encode())
    for path in sorted(assets):
        size, mtime_ns = assets[path]
        digest.update(f"asset {path}:{size}:{mtime_ns}\n".encode())
    return digest.hexdigest()


def source_tree(
    projects: ProjectStore,
    project_id: str,
    *,
    overlay: Mapping[str, str | None] = _EMPTY,
    eols: Mapping[str, Eol] | None = None,
    root_file: str | None = None,
    engine: Engine | None = None,
) -> SourceTree:
    """The tree of `project_id` with `overlay` on top of its Project folder.

    The Root file and engine come from the project settings unless given
    (the Compile button's per-run overrides); RootFileMissing when there is
    no Root file. The Root file is not validated here: the compile does that.
    An overlaid path missing from `eols` keeps the line ending of the file on
    disk, or "lf" for a new file. Overlay paths go through the path policy.
    """
    settings = projects.settings(project_id)
    chosen_root = root_file if root_file is not None else settings.root_file
    if chosen_root is None:
        raise RootFileMissing()
    chosen_engine: Engine = engine if engine is not None else settings.engine
    project_dir = projects.project_dir(project_id)
    given_eols = eols or {}

    texts: dict[str, str | None] = {}
    for raw, text in overlay.items():
        rel = check_relative_path(raw)
        if is_internal(rel):
            raise PathPolicyError(
                raw, "internal", "This folder is reserved and cannot be read or changed."
            )
        texts[rel] = text
    given: dict[str, Eol] = {check_relative_path(raw): eol for raw, eol in given_eols.items()}

    inputs: dict[str, str] = {}
    assets: dict[str, tuple[int, int]] = {}
    for entry in projects.list_files(project_id):
        if entry.path in texts:
            continue
        try:
            target = resolve_in(project_dir, entry.path)
            if entry.kind == "source":
                inputs[entry.path] = content_version(target.read_bytes())
            else:
                info = target.stat()
                assets[entry.path] = (info.st_size, info.st_mtime_ns)
        except (OSError, PathPolicyError):
            continue  # removed since it was listed

    resolved_eols: dict[str, Eol] = {}
    for rel, text in texts.items():
        if text is None:
            continue
        eol = given.get(rel) or _disk_eol(project_dir, rel)
        resolved_eols[rel] = eol
        inputs[rel] = content_version(encode_text(text, eol))

    return SourceTree(
        project_id=project_id,
        root_file=chosen_root,
        engine=chosen_engine,
        overlay=MappingProxyType(texts),
        eols=MappingProxyType(resolved_eols),
        inputs=MappingProxyType(inputs),
        digest=tree_digest(chosen_root, chosen_engine, inputs, assets),
        assets=MappingProxyType(assets),
    )


def _disk_eol(project_dir: Path, rel: str) -> Eol:
    try:
        return detect_eol(resolve_in(project_dir, rel).read_bytes())
    except (OSError, PathPolicyError):
        return "lf"


class TreeCompiler(Protocol):
    """Compiles SourceTrees, caching results by digest (ScratchTreeCompiler, FakeTreeCompiler)."""

    async def compile(
        self,
        tree: SourceTree,
        *,
        purpose: Purpose,
        cancel: CancelToken,
        timeout_s: float = 120.0,
        turn_id: str | None = None,
    ) -> CompileResult:
        """The result for `tree`, from the cache or a new compile.

        Baseline and Proposal compiles need `turn_id`, which names the
        scratch folder, and may reuse any cached result. A user compile has
        an empty overlay and reuses only an earlier user compile's result.
        Raises Cancelled when `cancel` is set before or during the compile.
        """
        ...

    def peek(self, digest: str) -> CompileResult | None:
        """The cached result a Baseline or Proposal compile of `digest` would reuse."""
        ...


@dataclass(frozen=True)
class _Entry:
    result: CompileResult
    purpose: Purpose
    pdf_stamp: tuple[int, int] | None  # the PDF's (size, mtime_ns) when it was cached


class _CompileCache:
    """An in-process LRU of compile results with the user-purpose rule (agent design §7.1).

    A Baseline or Proposal compile may reuse any entry. A user compile reuses
    only a user compile's entry whose PDF in `build/<id>/out/` is still the
    one that compile wrote: a scratch result's PDF lives in a folder that is
    deleted when its Turn ends.
    """

    def __init__(self, size: int) -> None:
        self._size = max(1, size)
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._guard = threading.Lock()

    def get(self, key: str, purpose: Purpose) -> CompileResult | None:
        with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if purpose == "user" and (
                entry.purpose != "user" or _pdf_stamp(entry.result.pdf_path) != entry.pdf_stamp
            ):
                return None
            self._entries.move_to_end(key)
            return entry.result

    def put(self, key: str, purpose: Purpose, result: CompileResult) -> None:
        if result.status not in _CACHED_STATUSES:
            return
        entry = _Entry(result, purpose, _pdf_stamp(result.pdf_path))
        with self._guard:
            self._entries[key] = entry
            self._entries.move_to_end(key)
            while len(self._entries) > self._size:
                self._entries.popitem(last=False)


class _KeyLocks:
    """One asyncio lock per cache key, dropped when nobody holds or waits for it."""

    def __init__(self) -> None:
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        lock, users = self._locks.get(key, (None, 0))
        if lock is None:
            lock = asyncio.Lock()
        self._locks[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock, users = self._locks[key]
            if users <= 1:
                del self._locks[key]
            else:
                self._locks[key] = (lock, users - 1)


class ScratchTreeCompiler:
    """The TeX TreeCompiler: scratch folders for Turns, the Compiler for the user.

    Concurrent Baseline or Proposal compiles of one digest run once; the
    others wait and reuse the result. A user compile never waits for a
    scratch compile, and a scratch compile never takes the Compiler's
    per-Project lock, so neither waits for the other.
    """

    def __init__(
        self, compiler: CompileBackend, layout: DataLayout, *, cache_size: int = 64
    ) -> None:
        self.compiler = compiler
        self.layout = layout
        self._cache = _CompileCache(cache_size)
        self._locks = _KeyLocks()

    @classmethod
    def from_settings(cls, settings: Settings) -> ScratchTreeCompiler:
        """A tree compiler with its own Compiler over `settings.data_dir` (agent design §3.1)."""
        layout = DataLayout(settings.data_dir)
        compiler = Compiler(ProjectStore(layout), timeout_s=settings.compile_timeout_s)
        return cls(compiler, layout)

    async def compile(
        self,
        tree: SourceTree,
        *,
        purpose: Purpose,
        cancel: CancelToken,
        timeout_s: float = 120.0,
        turn_id: str | None = None,
    ) -> CompileResult:
        """See TreeCompiler.compile. A user compile uses the Compiler's own timeout."""
        if purpose == "user":
            if tree.overlay:
                raise ValueError("a user compile has no overlay")
        elif purpose in ("baseline", "proposal"):
            if turn_id is None:
                raise ValueError("a Baseline or Proposal compile needs a turn_id")
            _check_turn_id(turn_id)
        else:
            raise ValueError(f"unknown purpose: {purpose!r}")
        cancel.raise_if_set()
        key = _cache_key(await asyncio.to_thread(lambda: self.compiler.toolchain), tree.digest)

        if purpose == "user":
            return await self._compile_user(tree, key, cancel)
        assert turn_id is not None
        async with self._locks.hold(key):
            cached = self._cache.get(key, purpose)
            if cached is not None:
                return cached
            stop = threading.Event()

            def should_stop() -> bool:
                return cancel.is_set() or stop.is_set()

            try:
                result, faithful = await asyncio.to_thread(
                    self._compile_scratch, tree, turn_id, timeout_s, should_stop
                )
            except asyncio.CancelledError:
                stop.set()  # the awaiting task was cancelled: kill TeX too
                raise
            cancel.raise_if_set()
            if faithful:
                self._cache.put(key, purpose, result)
            return result

    def peek(self, digest: str) -> CompileResult | None:
        return self._cache.get(_cache_key(self.compiler.toolchain, digest), "baseline")

    def cleanup(self, project_id: str, turn_id: str) -> None:
        """Delete a Turn's scratch folder; retried on Windows, left for `sweep` if it stays busy."""
        _check_turn_id(turn_id)
        _check_turn_id(project_id)
        _remove_tree(self.layout.build(project_id) / "scratch" / turn_id)

    def sweep(self) -> int:
        """Remove every leftover Turn scratch folder (at startup); returns how many."""
        removed = 0
        if not self.layout.build_root.is_dir():
            return 0
        for project in sorted(self.layout.build_root.iterdir()):
            scratch = project / "scratch"
            if not scratch.is_dir() or scratch.is_symlink():
                continue
            for turn in sorted(scratch.iterdir()):
                if _remove_tree(turn):
                    removed += 1
        return removed

    # -- internals ----------------------------------------------------------

    async def _compile_user(self, tree: SourceTree, key: str, cancel: CancelToken) -> CompileResult:
        cached = self._cache.get(key, "user")
        if cached is not None:
            return cached
        result = await asyncio.to_thread(
            self.compiler.compile, tree.project_id, root_file=tree.root_file, engine=tree.engine
        )
        cancel.raise_if_set()
        self._cache.put(key, "user", result)
        return result

    def _compile_scratch(
        self,
        tree: SourceTree,
        turn_id: str,
        timeout_s: float,
        should_stop: Callable[[], bool],
    ) -> tuple[CompileResult, bool]:
        """Materialize and compile `tree`; also whether what was compiled matches its digest."""
        folder = self.layout.build(tree.project_id) / "scratch" / turn_id / tree.digest
        if folder.exists() and not _remove_tree(folder):
            # An earlier, uncached compile of this tree left files that are
            # still open (a cancelled TeX on Windows); compile beside them.
            folder = folder.with_name(f"{tree.digest}-{uuid.uuid4().hex[:8]}")
        src = folder / "src"
        src.mkdir(parents=True)
        compiled_digest = self._materialize(tree, src)
        result = self.compiler.compile_dir(
            src,
            project_id=tree.project_id,
            root_file=tree.root_file,
            engine=tree.engine,
            out_dir=folder / "out",
            log_path=folder / "compile.log",
            timeout_s=timeout_s,
            should_stop=should_stop,
        )
        return _map_paths(result, src, tree), compiled_digest == tree.digest

    def _materialize(self, tree: SourceTree, src: Path) -> str:
        """Write `tree` into `src`; returns the digest of what was written.

        Overlaid Source files are written with their line endings and the
        other Source files are copied from the Project folder. Assets are
        hard-linked from the per-Project asset cache, never from the Project
        folder, so a write in the scratch folder cannot reach a user's file.
        """
        project_dir = self.layout.project_dir(tree.project_id)
        versions: dict[str, str] = {}
        for rel in sorted(tree.inputs):
            text = tree.overlay.get(rel)
            if text is not None:
                data = encode_text(text, tree.eols.get(rel, "lf"))
            else:
                try:
                    data = resolve_in(project_dir, rel).read_bytes()
                except (OSError, PathPolicyError):
                    continue  # gone since the capture: the digest will not match
            target = _under(src, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            versions[rel] = content_version(data)

        cache_dir = self.layout.build(tree.project_id) / "assets"
        linked: dict[str, tuple[int, int]] = {}
        for rel in sorted(tree.assets):
            try:
                source = resolve_in(project_dir, rel)
                info = source.stat()
            except (OSError, PathPolicyError):
                continue
            cached = _under(cache_dir, rel)
            try:
                _refresh_cache_entry(source, cached, info)
            except OSError:
                continue
            target = _under(src, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(cached, target)
            except OSError:
                shutil.copy2(cached, target)
            linked[rel] = (info.st_size, info.st_mtime_ns)
        return tree_digest(tree.root_file, tree.engine, versions, linked)


def _cache_key(toolchain: str, digest: str) -> str:
    return hashlib.sha256(f"{toolchain}\n{digest}".encode()).hexdigest()


def _check_turn_id(name: str) -> None:
    """Refuse a turn or project id that is not one plain folder name."""
    if not _TURN_ID.fullmatch(name):
        raise ValueError(f"not a plain id: {name!r}")


def _under(root: Path, rel: str) -> Path:
    """`root / rel` for a normalized relative path (one more check before writing)."""
    return root.joinpath(*check_relative_path(rel).split("/"))


def _pdf_stamp(path: Path | None) -> tuple[int, int] | None:
    if path is None:
        return None
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_size, info.st_mtime_ns)


def _refresh_cache_entry(source: Path, cached: Path, info: os.stat_result) -> None:
    """Copy `source` to `cached` unless the cached copy has its size and mtime.

    The copy lands in a new file that replaces the old one, so scratch
    folders that hard-linked the old copy keep their bytes.
    """
    try:
        current = cached.stat()
        if (current.st_size, current.st_mtime_ns) == (info.st_size, info.st_mtime_ns):
            return
    except OSError:
        pass
    cached.parent.mkdir(parents=True, exist_ok=True)
    temp = cached.with_name(f".{cached.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temp)
        os.replace(temp, cached)
    finally:
        temp.unlink(missing_ok=True)


def _map_paths(result: CompileResult, src: Path, tree: SourceTree) -> CompileResult:
    """The result with each Diagnostic's file as a path in `tree`, or None outside it."""
    known = {*tree.inputs, *tree.assets}
    folded = {path.casefold(): path for path in known}
    mapped: list[Diagnostic] = []
    for diagnostic in result.diagnostics:
        rel = _tree_path(diagnostic.file, src, known, folded)
        if rel == diagnostic.file:
            mapped.append(diagnostic)
        else:
            mapped.append(replace(diagnostic, file=rel, line=diagnostic.line if rel else None))
    if all(a is b for a, b in zip(mapped, result.diagnostics, strict=True)):
        return result
    return replace(result, diagnostics=tuple(mapped))


def _tree_path(
    file: str | None, src: Path, known: set[str], folded: Mapping[str, str]
) -> str | None:
    if file is None:
        return None
    path = file.replace("\\", "/")
    if path.startswith("/") or PureWindowsPath(path).drive:
        prefix = src.as_posix().rstrip("/") + "/"
        if not path.casefold().startswith(prefix.casefold()):
            return None
        path = path[len(prefix) :]
    path = str(PurePosixPath(path)).removeprefix("./")
    if path in known:
        return path
    return folded.get(path.casefold())


def _remove_tree(path: Path) -> bool:
    """Delete `path` and everything in it; True once it is gone."""
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return True
    if not path.exists():
        return False

    def writable(func: Callable[[str], object], failed: str, _exc: BaseException) -> None:
        # Windows refuses to delete a read-only file.
        os.chmod(failed, stat.S_IWRITE)
        func(failed)

    for attempt in range(_RMTREE_ATTEMPTS):
        try:
            shutil.rmtree(path, onexc=writable)
            return True
        except OSError:
            if attempt + 1 < _RMTREE_ATTEMPTS:
                time.sleep(_RMTREE_DELAY_S)
    return not path.exists()
