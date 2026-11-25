from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from prismtex.errors import (
    FileExists,
    FileNotFound,
    InvalidRequest,
    NotText,
    PathPolicyError,
    PayloadTooLarge,
    ProjectNotFound,
    RootFileInvalid,
    VersionConflict,
)
from prismtex.layout import DataLayout
from prismtex.workspace import ProjectStore, content_version

PAPER_FILES = {
    "main.tex",
    "references.bib",
    "figures/example-figure.png",
    "sections/introduction.tex",
    "sections/method.tex",
    "sections/results.tex",
    "sections/conclusion.tex",
}
RESUME_FILES = {
    "main.tex",
    "sections/header.tex",
    "sections/summary.tex",
    "sections/experience.tex",
    "sections/education.tex",
    "sections/skills.tex",
}


def paths_of(store: ProjectStore, project_id: str) -> set[str]:
    return {entry.path for entry in store.list_files(project_id)}


# -- projects ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "name", "files"),
    [
        ("blank", "Untitled project", {"main.tex"}),
        ("paper", "Example paper", PAPER_FILES),
        ("resume", "Example résumé", RESUME_FILES),
    ],
)
def test_create_from_template(store: ProjectStore, kind: Any, name: str, files: set[str]) -> None:
    info = store.create(kind=kind)
    assert len(info.id) == 32 and int(info.id, 16) >= 0
    assert (info.name, info.kind, info.root_file, info.engine) == (
        name,
        kind,
        "main.tex",
        "pdflatex",
    )
    assert info.created_at == info.updated_at
    assert info.created_at.endswith("+00:00")
    assert paths_of(store, info.id) == files
    assert not (store.layout.project_dir(info.id) / "prism-uploads").exists()
    saved = json.loads(store.layout.project_json(info.id).read_text(encoding="utf-8"))
    assert saved == {"schema": 1, **info.to_dict()}


def test_resume_template_says_prismtex(store: ProjectStore) -> None:
    info = store.create(kind="resume")
    text = store.read(info.id, "sections/summary.tex").text
    assert "PrismTeX" in text and "OpenPrism" not in text


def test_paper_figure_is_a_small_png(store: ProjectStore) -> None:
    info = store.create(kind="paper")
    data, _ = store.read_bytes(info.id, "figures/example-figure.png")
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(data) <= 4096


def test_create_with_name_and_invalid_inputs(store: ProjectStore) -> None:
    assert store.create(name="  My thesis  ").name == "My thesis"
    assert store.create(name="   ").name == "Untitled project"
    with pytest.raises(InvalidRequest) as info:
        store.create(name="x" * 121)
    assert info.value.extra["field"] == "name"
    with pytest.raises(InvalidRequest):
        store.create(kind="import")  # type: ignore[arg-type]


def test_list_newest_first_and_get(store: ProjectStore) -> None:
    first = store.create(name="first")
    second = store.create(name="second")
    third = store.create(name="third")
    assert [p.id for p in store.list()] == [third.id, second.id, first.id]
    assert store.get(second.id) == second


def test_list_skips_broken_project_json(store: ProjectStore) -> None:
    good = store.create()
    broken = store.create()
    store.layout.project_json(broken.id).write_bytes(b"{not json")
    other = store.create()
    store.layout.project_json(other.id).write_bytes(b'{"id": "someone-else"}')
    assert [p.id for p in store.list()] == [good.id]
    with pytest.raises(ProjectNotFound):
        store.get(broken.id)


@pytest.mark.parametrize("bad_id", ["", "abc", "../" + "a" * 30, "A" * 32, "g" * 32, "0" * 33])
def test_get_rejects_bad_ids(store: ProjectStore, bad_id: str) -> None:
    with pytest.raises(ProjectNotFound):
        store.get(bad_id)


def test_get_unknown_project(store: ProjectStore) -> None:
    with pytest.raises(ProjectNotFound) as info:
        store.get("0" * 32)
    assert info.value.extra == {"project_id": "0" * 32}


def test_update(store: ProjectStore) -> None:
    info = store.create(kind="paper")
    store.write(info.id, "other.tex", "\\documentclass{article}\n", None)
    updated = store.update(info.id, name=" Renamed ", root_file="./other.tex", engine="lualatex")
    assert (updated.name, updated.root_file, updated.engine) == ("Renamed", "other.tex", "lualatex")
    assert updated.updated_at > info.updated_at
    assert updated.created_at == info.created_at
    assert store.get(info.id) == updated
    settings = store.settings(info.id)
    assert (settings.root_file, settings.engine) == ("other.tex", "lualatex")

    # Omitted fields stay as they are; None clears the root file.
    assert store.update(info.id).root_file == "other.tex"
    assert store.update(info.id, root_file=None).root_file is None


def test_update_validation(store: ProjectStore) -> None:
    info = store.create()
    with pytest.raises(RootFileInvalid):
        store.update(info.id, root_file="missing.tex")
    with pytest.raises(InvalidRequest) as err:
        store.update(info.id, engine="context")  # type: ignore[arg-type]
    assert err.value.extra["field"] == "engine"
    with pytest.raises(InvalidRequest) as err:
        store.update(info.id, name="  ")
    assert err.value.extra["field"] == "name"
    assert store.get(info.id) == info  # nothing changed


def test_concurrent_updates_do_not_lose_writes(store: ProjectStore) -> None:
    info = store.create()

    def rename(i: int) -> None:
        store.update(info.id, name=f"name {i}")

    threads = [threading.Thread(target=rename, args=(i,)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert store.get(info.id).name.startswith("name ")


def test_delete_removes_everything(store: ProjectStore) -> None:
    info = store.create(kind="paper")
    layout = store.layout
    layout.out(info.id).mkdir(parents=True)
    (layout.out(info.id) / "main.pdf").write_bytes(b"%PDF")
    layout.logs(info.id).mkdir(parents=True)
    layout.history_git(info.id).mkdir(parents=True)
    readonly = layout.history_git(info.id) / "object"
    readonly.write_bytes(b"x")
    readonly.chmod(0o444)
    keep = store.create()

    store.delete(info.id)

    for path in (
        layout.project_dir(info.id),
        layout.state(info.id),
        layout.build(info.id),
        layout.history_git(info.id),
    ):
        assert not path.exists()
    assert [p.id for p in store.list()] == [keep.id]
    with pytest.raises(ProjectNotFound):
        store.delete(info.id)


def test_project_dir_is_validated(store: ProjectStore) -> None:
    info = store.create()
    assert store.project_dir(info.id) == store.layout.project_dir(info.id)
    with pytest.raises(ProjectNotFound):
        store.project_dir("f" * 32)


# -- files ---------------------------------------------------------------------


@pytest.fixture
def pid(store: ProjectStore) -> str:
    return store.create(kind="blank").id


def test_read_returns_lf_text_version_and_eol(store: ProjectStore, pid: str) -> None:
    root = store.layout.project_dir(pid)
    (root / "crlf.tex").write_bytes(b"a\r\nb\r\n")
    got = store.read(pid, "crlf.tex")
    assert (got.text, got.eol, got.version) == ("a\nb\n", "crlf", content_version(b"a\r\nb\r\n"))


def test_read_errors(store: ProjectStore, pid: str) -> None:
    root = store.layout.project_dir(pid)
    (root / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")
    with pytest.raises(NotText) as info:
        store.read(pid, "image.png")
    assert info.value.extra == {"path": "image.png"}
    with pytest.raises(FileNotFound):
        store.read(pid, "missing.tex")
    (root / "folder").mkdir()
    with pytest.raises(FileNotFound):
        store.read(pid, "folder")
    data, version = store.read_bytes(pid, "image.png")
    assert version == content_version(data)


def test_crlf_file_stays_crlf(store: ProjectStore, pid: str) -> None:
    root = store.layout.project_dir(pid)
    (root / "win.tex").write_bytes(b"one\r\ntwo\r\n")
    base = store.read(pid, "win.tex").version
    written = store.write(pid, "win.tex", "one\ntwo\nthree\n", base)
    assert (root / "win.tex").read_bytes() == b"one\r\ntwo\r\nthree\r\n"
    assert written.eol == "crlf"
    assert written.text == "one\ntwo\nthree\n"
    assert written.version == content_version(b"one\r\ntwo\r\nthree\r\n")


def test_new_file_is_lf(store: ProjectStore, pid: str) -> None:
    written = store.write(pid, "new/dir/file.tex", "a\r\nb\n", None)
    assert written.eol == "lf"
    assert (store.layout.project_dir(pid) / "new" / "dir" / "file.tex").read_bytes() == b"a\nb\n"


def test_bom_round_trips(store: ProjectStore, pid: str) -> None:
    root = store.layout.project_dir(pid)
    (root / "bom.tex").write_bytes(b"\xef\xbb\xbfx\n")
    got = store.read(pid, "bom.tex")
    store.write(pid, "bom.tex", got.text, got.version)
    assert (root / "bom.tex").read_bytes() == b"\xef\xbb\xbfx\n"


def test_stale_base_version_conflicts_and_keeps_bytes(store: ProjectStore, pid: str) -> None:
    root = store.layout.project_dir(pid)
    original = (root / "main.tex").read_bytes()
    stale = content_version(b"something else")
    with pytest.raises(VersionConflict) as info:
        store.write(pid, "main.tex", "overwritten\n", stale)
    assert info.value.extra == {"path": "main.tex", "current_version": content_version(original)}
    assert (root / "main.tex").read_bytes() == original


def test_base_version_for_missing_file_conflicts(store: ProjectStore, pid: str) -> None:
    with pytest.raises(VersionConflict) as info:
        store.write(pid, "gone.tex", "x", content_version(b"x"))
    assert info.value.current_version is None
    assert not (store.layout.project_dir(pid) / "gone.tex").exists()


def test_unconditional_write_and_matching_version(store: ProjectStore, pid: str) -> None:
    first = store.write(pid, "main.tex", "v1\n", None)
    second = store.write(pid, "main.tex", "v2\n", first.version)
    assert store.read(pid, "main.tex") == second


def test_write_limits_and_shapes(store: ProjectStore, pid: str) -> None:
    with pytest.raises(PayloadTooLarge) as info:
        store.write(pid, "big.tex", "x" * (10 * 2**20 + 1), None)
    assert info.value.extra == {"limit_mb": 10}
    store.write(pid, "dir/a.tex", "x", None)
    with pytest.raises(InvalidRequest):
        store.write(pid, "dir", "x", None)
    with pytest.raises(InvalidRequest):
        store.write(pid, "dir/a.tex/b.tex", "x", None)
    with pytest.raises(InvalidRequest):
        store.write(pid, "bad.tex", "\ud800", None)


def test_writes_leave_no_temp_files(store: ProjectStore, pid: str) -> None:
    for i in range(3):
        store.write(pid, "main.tex", f"v{i}\n", None)
    names = os.listdir(store.layout.project_dir(pid))
    assert names == ["main.tex"]


def test_atomic_write_temp_files_are_hidden_and_reserved(store: ProjectStore, pid: str) -> None:
    root = store.layout.project_dir(pid)
    # A temp file left by a crash mid-write (also tried in a subfolder).
    (root / ".prismtex-abc.tmp").write_bytes(b"partial")
    (root / "sub").mkdir()
    (root / "sub" / ".prismtex-xyz.tmp").write_bytes(b"partial")
    assert paths_of(store, pid) == {"main.tex"}
    tree = store.tree(pid)
    assert {child["name"] for child in tree["children"]} == {"main.tex", "sub"}
    assert store.tree(pid, "sub")["children"] == []
    # The reserved name cannot be read or written through the file API.
    for call in (
        lambda: store.read(pid, ".prismtex-abc.tmp"),
        lambda: store.write(pid, ".prismtex-new.tmp", "x", None),
        lambda: store.write_bytes(pid, "sub/.prismtex-new.tmp", b"x"),
        lambda: store.upload(pid, filename=".prismtex-up.tmp", data=b"x"),
    ):
        with pytest.raises(PathPolicyError) as info:
            call()
        assert info.value.reason == "internal"


def test_remove_stale_temp_files_at_startup(store: ProjectStore, pid: str) -> None:
    from prismtex.workspace import remove_stale_temp_files

    root = store.layout.project_dir(pid)
    (root / ".prismtex-abc.tmp").write_bytes(b"partial")
    (root / "sub").mkdir()
    (root / "sub" / ".prismtex-xyz.tmp").write_bytes(b"partial")
    remove_stale_temp_files(store.layout)
    assert not (root / ".prismtex-abc.tmp").exists()
    assert not (root / "sub" / ".prismtex-xyz.tmp").exists()
    assert (root / "main.tex").exists()


def test_write_retries_permission_error_then_succeeds(
    store: ProjectStore, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prismtex.workspace import store as store_module

    calls = {"n": 0}
    real_replace = os.replace

    def flaky(src: Any, dst: Any) -> None:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError("sharing violation")
        real_replace(src, dst)

    monkeypatch.setattr(store_module.os, "replace", flaky)
    monkeypatch.setattr(store_module.time, "sleep", lambda _s: None)
    written = store.write(pid, "main.tex", "survived\n", None)
    assert written.text == "survived\n"
    assert calls["n"] == 3


def test_write_raises_file_busy_when_replace_keeps_failing(
    store: ProjectStore, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prismtex.errors import FileBusy
    from prismtex.workspace import store as store_module

    def always_busy(src: Any, dst: Any) -> None:
        raise PermissionError("sharing violation")

    monkeypatch.setattr(store_module.os, "replace", always_busy)
    monkeypatch.setattr(store_module.time, "sleep", lambda _s: None)
    with pytest.raises(FileBusy) as info:
        store.write(pid, "main.tex", "x", None)
    assert info.value.extra == {"path": "main.tex"}
    # The temp file is cleaned up even when the replace never lands.
    assert os.listdir(store.layout.project_dir(pid)) == ["main.tex"]


@pytest.mark.parametrize("path", [".git/config", ".prism/meta.json", ".GIT/HEAD", ".Prism/x"])
def test_internal_folders_are_refused(store: ProjectStore, pid: str, path: str) -> None:
    with pytest.raises(PathPolicyError) as info:
        store.write(pid, path, "x", None)
    assert info.value.reason == "internal"
    for call in (
        lambda: store.read(pid, path),
        lambda: store.read_bytes(pid, path),
        lambda: store.write_bytes(pid, path, b"x"),
        lambda: store.delete_file(pid, path),
        lambda: store.rename(pid, "main.tex", path),
        lambda: store.tree(pid, path),
        lambda: store.upload(pid, filename="x", data=b"x", folder=path.split("/")[0]),
    ):
        with pytest.raises(PathPolicyError):
            call()


def test_internal_folders_are_hidden(store: ProjectStore, pid: str) -> None:
    root = store.layout.project_dir(pid)
    for folder in (".git", ".prism", "sub/.git"):
        (root / folder).mkdir(parents=True)
        (root / folder / "data").write_bytes(b"x")
    (root / ".github").mkdir()
    (root / ".github" / "ci.yml").write_bytes(b"x")
    assert paths_of(store, pid) == {"main.tex", ".github/ci.yml"}
    tree = store.tree(pid)
    names = {child["name"] for child in tree["children"]}
    assert names == {"main.tex", ".github", "sub"}


def test_paths_outside_are_refused(store: ProjectStore, pid: str) -> None:
    for path in ("../escape.tex", "/etc/passwd", "C:\\x.tex", "con.tex"):
        with pytest.raises(PathPolicyError):
            store.write(pid, path, "x", None)


def test_list_files_kinds_and_order(store: ProjectStore) -> None:
    pid = store.create(kind="paper").id
    entries = store.list_files(pid)
    assert [e.path for e in entries] == sorted(e.path for e in entries)
    kinds = {e.path: e.kind for e in entries}
    assert kinds["main.tex"] == "source"
    assert kinds["references.bib"] == "source"
    assert kinds["figures/example-figure.png"] == "asset"
    sizes = {e.path: e.size for e in entries}
    root = store.layout.project_dir(pid)
    assert sizes["main.tex"] == (root / "main.tex").stat().st_size
    store.write_bytes(pid, "Style.STY", b"x")
    assert {e.path: e.kind for e in store.list_files(pid)}["Style.STY"] == "source"


def test_tree_shape(store: ProjectStore) -> None:
    pid = store.create(kind="paper", name="My paper").id
    store.write(pid, "Zeta.tex", "", None)
    store.write(pid, "alpha.tex", "", None)
    tree = store.tree(pid)
    assert (tree["name"], tree["path"], tree["type"]) == ("My paper", "", "dir")
    names = [child["name"] for child in tree["children"]]
    # Folders first, then case-insensitive names.
    assert names == ["figures", "sections", "alpha.tex", "main.tex", "references.bib", "Zeta.tex"]
    sections = tree["children"][1]
    assert sections["path"] == "sections"
    assert sections["children"][0] == {
        "name": "conclusion.tex",
        "path": "sections/conclusion.tex",
        "type": "file",
        "size": (store.layout.project_dir(pid) / "sections" / "conclusion.tex").stat().st_size,
        "kind": "source",
    }
    sub = store.tree(pid, "sections/")
    assert (sub["name"], sub["path"]) == ("sections", "sections")
    assert len(sub["children"]) == 4
    with pytest.raises(FileNotFound):
        store.tree(pid, "missing")
    with pytest.raises(InvalidRequest):
        store.tree(pid, "main.tex")


def test_upload(store: ProjectStore, pid: str) -> None:
    entry = store.upload(pid, filename="C:\\Users\\me\\plot.png", data=b"\x89PNG", folder="figs")
    assert (entry.path, entry.size, entry.kind) == ("figs/plot.png", 4, "asset")
    entry = store.upload(pid, filename="refs.bib", data=b"@book{}", folder="")
    assert entry.path == "refs.bib"
    # Uploading again overwrites.
    store.upload(pid, filename="refs.bib", data=b"@article{}")
    assert store.read_bytes(pid, "refs.bib")[0] == b"@article{}"
    for bad in ("..", "", "con.png", "a:b.png"):
        with pytest.raises(PathPolicyError):
            store.upload(pid, filename=bad, data=b"x")
    with pytest.raises(PathPolicyError):
        store.upload(pid, filename="x.png", data=b"x", folder="../up")


def test_upload_limit(tmp_path: Path) -> None:
    layout = DataLayout(tmp_path)
    layout.ensure()
    store = ProjectStore(layout, max_upload_bytes=10)
    pid = store.create().id
    store.upload(pid, filename="ok.bin", data=b"x" * 10)
    with pytest.raises(PayloadTooLarge):
        store.upload(pid, filename="big.bin", data=b"x" * 11)


def test_rename(store: ProjectStore, pid: str) -> None:
    store.write(pid, "a.tex", "a", None)
    store.write(pid, "b.tex", "b", None)
    store.rename(pid, "a.tex", "moved/c.tex")
    assert paths_of(store, pid) == {"main.tex", "b.tex", "moved/c.tex"}
    with pytest.raises(FileExists) as info:
        store.rename(pid, "b.tex", "moved/c.tex")
    assert info.value.extra == {"path": "moved/c.tex"}
    with pytest.raises(FileNotFound):
        store.rename(pid, "missing.tex", "x.tex")
    store.rename(pid, "moved", "renamed")
    assert "renamed/c.tex" in paths_of(store, pid)
    with pytest.raises(InvalidRequest):
        store.rename(pid, "renamed", "renamed/inner")
    store.rename(pid, "b.tex", "B.tex")
    assert "B.tex" in paths_of(store, pid)


def test_delete_file_and_folder(store: ProjectStore, pid: str) -> None:
    store.write(pid, "dir/a.tex", "a", None)
    store.write(pid, "dir/sub/b.tex", "b", None)
    store.write(pid, "x.tex", "x", None)
    store.delete_file(pid, "x.tex")
    store.delete_file(pid, "dir")
    assert paths_of(store, pid) == {"main.tex"}
    with pytest.raises(FileNotFound):
        store.delete_file(pid, "x.tex")


def test_delete_files_reports_each_path(store: ProjectStore, pid: str) -> None:
    store.write(pid, "a.tex", "a", None)
    result = store.delete_files(pid, ["a.tex", "missing.tex", "../x"])
    assert result["deleted"] == ["a.tex"]
    assert [(e["path"], e["code"]) for e in result["errors"]] == [
        ("missing.tex", "file_not_found"),
        ("../x", "path_denied"),
    ]
    with pytest.raises(ProjectNotFound):
        store.delete_files("0" * 32, ["a.tex"])


def _symlink(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")


def test_symlinks_out_of_the_project(store: ProjectStore, pid: str, tmp_path: Path) -> None:
    root = store.layout.project_dir(pid)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_bytes(b"secret")
    _symlink(root / "dir-link", outside)
    _symlink(root / "file-link.txt", secret)

    assert paths_of(store, pid) == {"main.tex"}
    for path in ("dir-link/secret.txt", "file-link.txt"):
        with pytest.raises(PathPolicyError):
            store.read(pid, path)
        with pytest.raises(PathPolicyError):
            store.write(pid, path, "pwned", None)
    store.delete_file(pid, "file-link.txt")
    store.delete_file(pid, "dir-link")
    assert secret.read_bytes() == b"secret"
    assert not (root / "dir-link").exists()
