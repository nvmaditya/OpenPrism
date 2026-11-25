from __future__ import annotations

import io
import stat
import zipfile
from pathlib import Path

import pytest

from prismtex.errors import ImportRejected
from prismtex.layout import DataLayout
from prismtex.workspace import ProjectStore, detect_root_file

DOC = b"\\documentclass{article}\n\\begin{document}x\\end{document}\n"


def make_zip(files: dict[str, bytes], *, compression: int = zipfile.ZIP_DEFLATED) -> io.BytesIO:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    buffer.seek(0)
    return buffer


def store_with(tmp_path: Path, **limits: int) -> ProjectStore:
    layout = DataLayout(tmp_path / "data")
    layout.ensure()
    return ProjectStore(layout, **limits)


def files_of(store: ProjectStore, project_id: str) -> set[str]:
    return {entry.path for entry in store.list_files(project_id)}


def assert_nothing_created(store: ProjectStore) -> None:
    assert store.list() == []
    assert list(store.layout.projects.iterdir()) == []
    assert list(store.layout.state_root.iterdir()) == []


def reject(store: ProjectStore, archive: io.BytesIO) -> str:
    with pytest.raises(ImportRejected) as info:
        store.import_zip(archive)
    assert info.value.code == "import_rejected"
    assert_nothing_created(store)
    return info.value.reason


def test_import_creates_a_project(tmp_path: Path) -> None:
    store = store_with(tmp_path)
    info = store.import_zip(
        make_zip({"main.tex": DOC, "refs.bib": b"@book{x}", "img/a.png": b"\x89PNG"}),
        name="Thesis",
    )
    assert (info.kind, info.name, info.root_file, info.engine) == (
        "import",
        "Thesis",
        "main.tex",
        "pdflatex",
    )
    assert files_of(store, info.id) == {"main.tex", "refs.bib", "img/a.png"}
    assert store.read_bytes(info.id, "main.tex")[0] == DOC
    assert store.get(info.id) == info


def test_default_name(tmp_path: Path) -> None:
    store = store_with(tmp_path)
    assert store.import_zip(make_zip({"a.tex": DOC})).name == "Imported project"


def test_top_folder_is_stripped(tmp_path: Path) -> None:
    store = store_with(tmp_path)
    info = store.import_zip(
        make_zip({"paper/": b"", "paper/main.tex": DOC, "paper/sections/a.tex": b"a"})
    )
    assert files_of(store, info.id) == {"main.tex", "sections/a.tex"}
    assert info.root_file == "main.tex"


def test_top_folder_is_kept_when_files_differ(tmp_path: Path) -> None:
    store = store_with(tmp_path)
    info = store.import_zip(make_zip({"a/main.tex": DOC, "b/x.tex": b"x"}))
    assert files_of(store, info.id) == {"a/main.tex", "b/x.tex"}
    single = store.import_zip(make_zip({"main.tex": DOC}))
    assert files_of(store, single.id) == {"main.tex"}


def test_junk_is_skipped(tmp_path: Path) -> None:
    store = store_with(tmp_path)
    info = store.import_zip(
        make_zip(
            {
                "proj/main.tex": DOC,
                "proj/.DS_Store": b"x",
                "proj/sub/.DS_Store": b"x",
                "__MACOSX/proj/._main.tex": b"x",
                "proj/.git/config": b"x",
                "proj/vendor/.git/HEAD": b"x",
                "proj/.prism/meta.json": b"{}",
            }
        )
    )
    assert files_of(store, info.id) == {"main.tex"}


@pytest.mark.parametrize(
    "name",
    ["../evil.tex", "a/../../evil.tex", "/etc/evil", "C:\\evil.tex", "C:/evil", "con.tex", "a:b"],
)
def test_zip_slip_is_rejected(tmp_path: Path, name: str) -> None:
    store = store_with(tmp_path)
    assert reject(store, make_zip({"main.tex": DOC, name: b"x"})) == "zip_slip"
    assert not (tmp_path / "evil.tex").exists()


def test_symlink_is_rejected(tmp_path: Path) -> None:
    store = store_with(tmp_path)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("main.tex", DOC)
        link = zipfile.ZipInfo("link.tex")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "/etc/passwd")
    buffer.seek(0)
    assert reject(store, buffer) == "symlink"


def test_too_large(tmp_path: Path) -> None:
    store = store_with(tmp_path, import_max_bytes=1000)
    archive = make_zip({"main.tex": DOC, "big.bin": b"x" * 1000}, compression=zipfile.ZIP_STORED)
    assert reject(store, archive) == "too_large"


def test_too_many_files(tmp_path: Path) -> None:
    store = store_with(tmp_path, import_max_files=3)
    assert reject(store, make_zip({f"f{i}.tex": b"x" for i in range(4)})) == "too_many_files"
    ok = store.import_zip(make_zip({f"f{i}.tex": b"x" for i in range(3)}))
    assert len(files_of(store, ok.id)) == 3


def test_compression_ratio(tmp_path: Path) -> None:
    store = store_with(tmp_path)
    assert reject(store, make_zip({"main.tex": DOC, "zeros.bin": bytes(2 * 2**20)})) == "ratio"
    # Highly compressible but small files are fine.
    ok = store.import_zip(make_zip({"main.tex": DOC, "zeros.bin": bytes(2**20)}))
    assert "zeros.bin" in files_of(store, ok.id)


def test_empty(tmp_path: Path) -> None:
    store = store_with(tmp_path)
    assert reject(store, make_zip({})) == "empty"
    assert reject(store, make_zip({"folder/": b"", "__MACOSX/x": b"x"})) == "empty"


def test_not_a_zip(tmp_path: Path) -> None:
    store = store_with(tmp_path)
    assert reject(store, io.BytesIO(b"this is not a zip archive")) == "bad_zip"


def test_file_and_folder_clash(tmp_path: Path) -> None:
    store = store_with(tmp_path)
    assert reject(store, make_zip({"a": b"file", "a/b.tex": b"x", "main.tex": DOC})) == "bad_zip"


def test_damaged_entry_removes_the_project(tmp_path: Path) -> None:
    store = store_with(tmp_path)
    archive = make_zip({"main.tex": DOC, "b.tex": b"hello world " * 100})
    raw = bytearray(archive.getvalue())
    # Corrupt the compressed bytes of the second entry; the headers still parse.
    offset = raw.find(b"b.tex") + len(b"b.tex") + 5
    raw[offset : offset + 8] = b"\x00" * 8
    assert reject(store, io.BytesIO(bytes(raw))) == "bad_zip"


def test_root_detection_on_import(tmp_path: Path) -> None:
    store = store_with(tmp_path)
    info = store.import_zip(
        make_zip({"chapters/one.tex": b"\\chapter{One}", "thesis.tex": DOC, "z/deep.tex": DOC})
    )
    assert info.root_file == "thesis.tex"
    none = store.import_zip(make_zip({"notes.tex": b"% \\documentclass{article}\n"}))
    assert none.root_file is None


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"main.tex": DOC, "a.tex": DOC}, "main.tex"),
        ({"sub/main.tex": DOC, "a.tex": DOC}, "sub/main.tex"),
        ({"b.tex": DOC, "a.tex": DOC}, "a.tex"),
        ({"x/a.tex": DOC, "b.tex": DOC}, "b.tex"),
        ({"x/main.tex": DOC, "main.tex": DOC}, "main.tex"),
        ({"A.tex": DOC, "a.tex": DOC}, "A.tex"),
        ({"paper.TEX": DOC}, "paper.TEX"),
        ({"main.tex": b"% \\documentclass{article}\n", "other.tex": DOC}, "other.tex"),
        ({"main.tex": b"\\documentclassx", "other.txt": DOC}, None),
        ({"main.tex": b"50\\% \\documentclass{article}"}, "main.tex"),
        ({}, None),
    ],
)
def test_detect_root_file(files: dict[str, bytes], expected: str | None) -> None:
    def read(path: str) -> str:
        return files[path].decode()

    assert detect_root_file(files.keys(), read) == expected
