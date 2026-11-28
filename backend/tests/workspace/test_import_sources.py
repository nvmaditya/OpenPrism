"""ZIP import of Overleaf and OpenAI Prism exports (agent design §15 T24, revamp design Q6)."""

from __future__ import annotations

import io
import stat
import zipfile
from pathlib import Path
from typing import Any

import pytest

from prismtex.errors import ImportRejected, InvalidRequest
from prismtex.layout import DataLayout
from prismtex.workspace import ProjectStore

DOC = "\\documentclass{article}\n\\begin{document}\nx\n\\end{document}\n"


def make_zip(files: dict[str, str | bytes], *, compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


def store_at(tmp_path: Path, **limits: int) -> ProjectStore:
    store = ProjectStore(DataLayout(tmp_path), **limits)
    store.layout.ensure()
    return store


def paths_of(store: ProjectStore, project_id: str) -> list[str]:
    return sorted(e.path for e in store.list_files(project_id))


def reject(store: ProjectStore, data: bytes, **kwargs: Any) -> str:
    with pytest.raises(ImportRejected) as info:
        store.import_zip(io.BytesIO(data), **kwargs)
    assert store.list() == []
    assert list(store.layout.projects.iterdir()) == []
    assert list(store.layout.state_root.iterdir()) == []
    return info.value.reason


PRISM_EXPORT = {
    "My Paper/paper.tex": DOC,
    "My Paper/sections/intro.tex": "\\section{Intro}\n",
    "My Paper/figs/a.png": b"\x89PNG\r\n\x1a\n",
    "My Paper/conversations/c1.json": b"{}",
    "__MACOSX/My Paper/._paper.tex": b"junk",
}


def test_prism_export_skips_conversations_and_keeps_phase_0_root_detection(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    info = store.import_zip(io.BytesIO(make_zip(PRISM_EXPORT)), name="My Paper", source="auto")
    assert paths_of(store, info.id) == ["figs/a.png", "paper.tex", "sections/intro.tex"]
    assert info.root_file == "paper.tex"
    assert store.get(info.id) == info


def test_prism_source_skips_only_the_top_conversations_folder(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    data = make_zip(
        {
            "main.tex": DOC,
            "conversations/c1.json": b"{}",
            "conversations/sub/c2.json": b"{}",
            "chapters/conversations/talk.tex": "\\section{Talk}\n",
            "conversations.tex": "% a file, not the folder\n",
        }
    )
    info = store.import_zip(io.BytesIO(data), source="prism")
    assert paths_of(store, info.id) == [
        "chapters/conversations/talk.tex",
        "conversations.tex",
        "main.tex",
    ]
    assert info.root_file == "main.tex"


def test_overleaf_source_keeps_a_conversations_folder(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    info = store.import_zip(io.BytesIO(make_zip(PRISM_EXPORT)), source="overleaf")
    assert paths_of(store, info.id) == [
        "conversations/c1.json",
        "figs/a.png",
        "paper.tex",
        "sections/intro.tex",
    ]
    assert info.root_file == "paper.tex"


def test_overleaf_zip_root_file_detected_and_top_folder_stripped(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    data = make_zip(
        {
            "thesis/": b"",
            "thesis/thesis.tex": "% \\documentclass{report} in a comment does not count\n" + DOC,
            "thesis/chapters/one.tex": "\\chapter{One}\n",
            "thesis/chapters/conversations/notes.tex": "\\section{Notes}\n",
            "thesis/refs.bib": "@book{x}\n",
        }
    )
    for source in ("auto", "overleaf"):
        info = store.import_zip(io.BytesIO(data), source=source)
        assert paths_of(store, info.id) == [
            "chapters/conversations/notes.tex",
            "chapters/one.tex",
            "refs.bib",
            "thesis.tex",
        ]
        assert info.root_file == "thesis.tex"


def test_every_source_detects_the_same_root_file(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    data = make_zip(
        {
            "p/a/deep.tex": DOC,
            "p/top.tex": DOC,
            "p/notes.tex": "no class here\n",
            "p/conversations/c.json": b"{}",
        }
    )
    roots = {
        source: store.import_zip(io.BytesIO(data), source=source).root_file
        for source in ("auto", "overleaf", "prism")
    }
    assert roots == {"auto": "top.tex", "overleaf": "top.tex", "prism": "top.tex"}


def test_prism_limits_count_only_kept_entries(tmp_path: Path) -> None:
    many = make_zip(
        {
            "main.tex": DOC,
            "refs.bib": "@book{x}\n",
            "a.tex": "a\n",
            "conversations/c1.json": b"{}",
            "conversations/c2.json": b"{}",
        }
    )
    large = make_zip(
        {
            "main.tex": DOC,
            "conversations/c1.json": b"x" * 900,
            "conversations/c2.json": b"x" * 900,
        },
        compression=zipfile.ZIP_STORED,
    )
    kept = store_at(tmp_path / "kept", import_max_files=3, import_max_bytes=1000)
    assert paths_of(kept, kept.import_zip(io.BytesIO(many), source="prism").id) == [
        "a.tex",
        "main.tex",
        "refs.bib",
    ]
    assert paths_of(kept, kept.import_zip(io.BytesIO(large), source="auto").id) == ["main.tex"]
    rejected = store_at(tmp_path / "rejected", import_max_files=3, import_max_bytes=1000)
    assert reject(rejected, many, source="overleaf") == "too_many_files"
    assert reject(rejected, large, source="overleaf") == "too_large"


def test_an_export_of_only_conversations_is_empty(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    assert reject(store, make_zip({"p/conversations/c1.json": b"{}"}), source="prism") == "empty"


def symlink_zip(link_name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("main.tex", DOC)
        link = zipfile.ZipInfo(link_name)
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(link, "/etc/passwd")
    return buf.getvalue()


def test_zip_limits_zip_slip_and_symlinks_are_still_rejected(tmp_path: Path) -> None:
    for source in ("auto", "prism"):
        store = store_at(tmp_path / source, import_max_files=3, import_max_bytes=2000)
        base = {"main.tex": DOC, "conversations/c1.json": b"{}"}
        # Names under conversations/ are checked before they are skipped.
        assert reject(store, make_zip({**base, "../evil.tex": b"x"}), source=source) == "zip_slip"
        assert (
            reject(store, make_zip({**base, "conversations/../../evil": b"x"}), source=source)
            == "zip_slip"
        )
        assert reject(store, symlink_zip("link.tex"), source=source) == "symlink"
        assert reject(store, symlink_zip("conversations/link"), source=source) == "symlink"
        too_many = {**base, **{f"f{i}.tex": b"x" for i in range(3)}}
        assert reject(store, make_zip(too_many), source=source) == "too_many_files"
        too_large = make_zip({**base, "big.bin": b"x" * 2000}, compression=zipfile.ZIP_STORED)
        assert reject(store, too_large, source=source) == "too_large"
        bomb = make_zip({**base, "zeros.bin": bytes(2 * 2**20)})
        big = store_at(tmp_path / f"{source}-big")
        assert reject(big, bomb, source=source) == "ratio"
        assert not (tmp_path / "evil.tex").exists()


def test_crlf_files_keep_their_bytes_on_import(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    crlf = DOC.replace("\n", "\r\n").encode("utf-8")
    mixed = b"line one\r\nline two\nline three\r\n"
    data = make_zip({"p/main.tex": crlf, "p/mixed.tex": mixed, "p/conversations/c.json": b"{}"})
    for source in ("auto", "overleaf", "prism"):
        info = store.import_zip(io.BytesIO(data), source=source)
        assert store.read_bytes(info.id, "main.tex")[0] == crlf
        assert store.read_bytes(info.id, "mixed.tex")[0] == mixed
        assert info.root_file == "main.tex"


def test_an_unknown_source_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    with pytest.raises(InvalidRequest) as info:
        store.import_zip(io.BytesIO(make_zip(PRISM_EXPORT)), source="dropbox")  # pyright: ignore[reportArgumentType]
    assert info.value.extra["field"] == "source"
    assert store.list() == []
