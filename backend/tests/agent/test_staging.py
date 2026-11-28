"""The Turn's base and its staging overlay (agent design §2.2 `staging`, §6.2)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from prismtex.agent.proposals import Proposal, ProposalMeta, compute
from prismtex.agent.staging import CarryReport, Staging, ToolError, capture_base
from prismtex.agent.types import EditorContext
from prismtex.agent.verify import Verification
from prismtex.errors import RootFileMissing
from prismtex.workspace import FileVersion, ProjectStore, content_version, encode_text

Project = tuple[ProjectStore, str]
Put = Callable[[ProjectStore, str, str, str], FileVersion]

CLEAN = Verification("clean", None, 0, 0, 0, (), (), 0, (), True)
PAPER = (
    "\\documentclass{article}\n"
    "\\begin{document}\n"
    "\\section{A}\n"
    "one\n"
    "two\n"
    "three\n"
    "four\n"
    "five\n"
    "six\n"
    "seven\n"
    "\\end{document}\n"
)


def meta(pid: str, turn_id: str = "t_1") -> ProposalMeta:
    return ProposalMeta(
        turn_id=turn_id,
        project_id=pid,
        thread_id="th_1",
        model="fake:m",
        profile="agentic",
        created_at=f"2026-11-28T00:00:0{turn_id[-1]}.000Z",
        status="completed",
    )


def propose(staging: Staging, pid: str, turn_id: str, carried: Proposal | None) -> Proposal:
    proposal = compute(staging.files(), (), CLEAN, meta(pid, turn_id), carried)
    assert proposal is not None
    return proposal


def code(action: Callable[[], Any]) -> str:
    with pytest.raises(ToolError) as caught:
        action()
    return caught.value.code


def test_staging_denies_paths_extensions_control_chars_and_size_with_tool_error_codes(
    blank_project: Project,
) -> None:
    store, pid = blank_project
    store.write_bytes(pid, "fig.png", b"\x89PNG\r\n\x1a\n")
    store.write_bytes(pid, "latexmkrc", b"$pdf_mode = 1;\n")
    staging, _ = Staging.open(capture_base(store, pid, None), None)
    text = staging.read("main.tex").text

    assert code(lambda: staging.read(".prism/meta.json")) == "path_denied"
    assert code(lambda: staging.write(".prism/meta.json", "{}", by="model")) == "path_denied"
    assert code(lambda: staging.read("../outside.tex")) == "path_denied"
    assert code(lambda: staging.write("latexmkrc", "$x = 0;\n", by="model")) == "write_denied"
    assert code(lambda: staging.write("main.tex", text + "\\x\x08egin\n", by="model")) == (
        "control_char"
    )
    assert code(lambda: staging.create("big.tex", "x" * (2**20 + 1), by="model")) == "too_large"
    assert code(lambda: staging.write("main.tex", "x" * (2**20 + 1), by="host")) == "too_large"
    assert code(lambda: staging.create("main.tex", "x\n", by="model")) == "exists"
    assert code(lambda: staging.read("fig.png")) == "binary"
    assert code(lambda: staging.read("missing.tex")) == "not_found"
    assert code(lambda: staging.write("missing.tex", "x\n", by="model")) == "not_found"
    assert code(lambda: staging.delete("missing.tex", by="model")) == "not_found"
    assert staging.mark() == 0 and staging.files() == []

    staging.create("exactly.tex", "x" * 2**20, by="model")  # 1 MiB is allowed
    assert staging.paths() == ["exactly.tex", "fig.png", "latexmkrc", "main.tex"]


def test_control_characters_already_in_a_file_do_not_block_editing_it(
    blank_project: Project, put: Put
) -> None:
    store, pid = blank_project
    put(store, pid, "old.tex", "page one\n\x0c\npage two\n")
    staging, _ = Staging.open(capture_base(store, pid, None), None)
    change = staging.write("old.tex", "page one\n\x0c\npage 2\n", by="model")
    assert change.span == (3, 3)
    error = pytest.raises(ToolError, staging.write, "old.tex", "page\x01 one\n\x0c\n", by="model")
    assert error.value.code == "control_char" and "Line 1" in error.value.message


def test_staging_write_of_the_start_text_removes_the_overlay_and_changed_since_tracks_marks(
    blank_project: Project, put: Put
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", PAPER)
    staging, report = Staging.open(capture_base(store, pid, None), None)
    assert report == CarryReport(carried=(), dropped=())
    start = staging.read("main.tex")

    first = staging.mark()
    change = staging.write("main.tex", PAPER.replace("two\n", "TWO\n"), by="model")
    assert (change.path, change.op, change.span) == ("main.tex", "modify", (5, 5))
    assert staging.changed_since(first)
    assert staging.read("main.tex").view_id != start.view_id

    second = staging.mark()
    staging.write("main.tex", PAPER.replace("two\n", "TWO\n"), by="model")  # the same text
    assert not staging.changed_since(second)
    assert [f.path for f in staging.files()] == ["main.tex"]

    staging.write("main.tex", PAPER, by="model")  # back to the start text
    assert staging.changed_since(second)
    assert staging.files() == []
    assert staging.read("main.tex") == start
    assert compute(staging.files(), (), CLEAN, meta(pid), None) is None

    removed = staging.write("main.tex", PAPER.replace("two\nthree\n", ""), by="model")
    assert removed.span == (5, 4)
    created = staging.create("sections/new.tex", "a\r\nb\r\n", by="model")
    assert created.span == (1, 2) and staging.read("sections/new.tex").text == "a\nb\n"
    deleted = staging.delete("sections/new.tex", by="model")
    assert (deleted.op, deleted.span) == ("delete", None)
    assert [f.path for f in staging.files()] == ["main.tex"]  # created and deleted again


def test_trees_overlay_the_base_and_the_staged_text_and_never_touch_the_disk(
    blank_project: Project, put: Put
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", PAPER)
    put(store, pid, "notes.tex", "notes\n")
    before = {e.path: store.read_bytes(pid, e.path)[0] for e in store.list_files(pid)}
    base = capture_base(store, pid, None)
    staging, _ = Staging.open(base, None)
    staging.write("main.tex", PAPER.replace("one\n", "ONE\n"), by="model")
    staging.create("sections/intro.tex", "Intro.\n", by="model")
    staging.delete("notes.tex", by="model")

    baseline = base.tree()
    assert baseline.root_file == "main.tex" and baseline.overlay["main.tex"] == PAPER
    assert baseline.inputs["main.tex"] == base.files["main.tex"].version
    proposal = staging.tree()
    assert proposal.overlay["main.tex"] == PAPER.replace("one\n", "ONE\n")
    assert proposal.overlay["sections/intro.tex"] == "Intro.\n"
    assert proposal.overlay["notes.tex"] is None and "notes.tex" not in proposal.inputs
    assert proposal.digest != baseline.digest
    assert {e.path: store.read_bytes(pid, e.path)[0] for e in store.list_files(pid)} == before

    store.update(pid, root_file=None)
    unrooted = capture_base(store, pid, None)
    assert unrooted.root_file is None
    with pytest.raises(RootFileMissing):
        unrooted.tree()
    with pytest.raises(RootFileMissing):
        Staging.open(unrooted, None)[0].tree()


def test_capture_base_lists_a_non_utf8_source_file_with_the_assets(blank_project: Project) -> None:
    store, pid = blank_project
    store.write_bytes(pid, "old.bib", "@misc{k, author = {Müller}}\n".encode("latin-1"))
    base = capture_base(store, pid, None)
    assert "old.bib" in base.assets and "old.bib" not in base.files
    staging, _ = Staging.open(base, None)
    assert code(lambda: staging.read("old.bib")) == "binary"
    assert code(lambda: staging.write("old.bib", "@misc{k}\n", by="model")) == "binary"
    assert code(lambda: staging.create("old.bib", "@misc{k}\n", by="model")) == "exists"


def test_host_writes_give_host_hunks_and_model_writes_model_hunks(
    blank_project: Project, put: Put
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", PAPER)
    staging, _ = Staging.open(capture_base(store, pid, None), None)
    text = staging.read("main.tex").text
    text = text.replace("\\begin{document}\n", "\\usepackage{booktabs}\n\\begin{document}\n")
    staging.write("main.tex", text, by="host")
    staging.write("main.tex", text.replace("six\n", "SIX\n"), by="model")
    [change] = propose(staging, pid, "t_1", None).files
    assert [(h.new_lines, h.origin) for h in change.hunks] == [
        (("\\usepackage{booktabs}",), "host"),
        (("SIX",), "model"),
    ]


def test_carry_forward_applies_undecided_hunks_as_start_text(
    blank_project: Project, put: Put
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", PAPER)
    first, _ = Staging.open(capture_base(store, pid, None), None)
    first.write("main.tex", PAPER.replace("two\n", "TWO\n").replace("six\n", "SIX\n"), by="model")
    first.create("sections/new.tex", "New.\n", by="host")
    p1 = propose(first, pid, "t_1", None)
    assert [len(c.hunks) for c in p1.files] == [2, 1]
    ids = [h.id for h in p1.hunks()]

    # The user saved an unrelated line at the top: the carried Hunks shift.
    put(store, pid, "main.tex", "% a note\n" + PAPER)
    second, report = Staging.open(capture_base(store, pid, None), p1)
    assert report == CarryReport(carried=tuple(ids), dropped=())
    expected = "% a note\n" + PAPER.replace("two\n", "TWO\n").replace("six\n", "SIX\n")
    assert second.read("main.tex").text == expected
    assert second.read("sections/new.tex").text == "New.\n"
    assert compute(second.files(), (), CLEAN, meta(pid, "t_2"), p1) is None  # nothing new

    second.write("main.tex", expected.replace("four\n", "FOUR\n"), by="model")
    p2 = propose(second, pid, "t_2", p1)
    assert (p2.id, p2.supersedes, p2.turn_ids) == ("t_2", "t_1", ("t_1", "t_2"))
    p2_ids = {h.id for h in p2.hunks()}
    assert set(ids) <= p2_ids and len(p2_ids) == 4  # carried ids are stable; one new Hunk
    assert next(h for h in p2.hunks() if h.path == "sections/new.tex").origin == "host"
    assert p2.files[0].new_text == expected.replace("four\n", "FOUR\n")


def test_carry_drops_a_hunk_whose_lines_the_user_rewrote_and_reports_it(
    blank_project: Project, put: Put
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", PAPER)
    first, _ = Staging.open(capture_base(store, pid, None), None)
    first.write("main.tex", PAPER.replace("two\n", "TWO\n").replace("six\n", "SIX\n"), by="model")
    p1 = propose(first, pid, "t_1", None)
    rewritten, kept = p1.files[0].hunks

    # The user saved their own text over the first Hunk's old line.
    users = PAPER.replace("two\n", "two, as the user wrote it\n")
    put(store, pid, "main.tex", users)
    second, report = Staging.open(capture_base(store, pid, None), p1)
    assert report.dropped == (rewritten.id,) and report.carried == (kept.id,)
    start = second.read("main.tex").text
    assert start == users.replace("six\n", "SIX\n")
    assert "two, as the user wrote it" in start and "TWO" not in start

    # With only that Hunk open, the start text is exactly the user's text.
    alone = replace(p1, undecided=frozenset({rewritten.id}))
    third, report = Staging.open(capture_base(store, pid, None), alone)
    assert report == CarryReport(carried=(), dropped=(rewritten.id,))
    assert third.read("main.tex").text == users and third.files() == []


def test_carry_drops_a_create_whose_path_the_user_took_and_a_delete_of_a_changed_file(
    blank_project: Project, put: Put
) -> None:
    store, pid = blank_project
    put(store, pid, "old.tex", "old\n")
    first, _ = Staging.open(capture_base(store, pid, None), None)
    first.create("new.tex", "agent text\n", by="model")
    first.delete("old.tex", by="model")
    p1 = propose(first, pid, "t_1", None)
    assert [(c.path, c.op) for c in p1.files] == [("new.tex", "create"), ("old.tex", "delete")]
    put(store, pid, "new.tex", "the user's file\n")
    put(store, pid, "old.tex", "old, edited\n")
    second, report = Staging.open(capture_base(store, pid, None), p1)
    assert report.carried == () and set(report.dropped) == {h.id for h in p1.hunks()}
    assert second.read("new.tex").text == "the user's file\n"
    assert second.read("old.tex").text == "old, edited\n"


def test_dirty_buffer_base_version_is_the_version_of_the_saved_buffer(
    client: TestClient, store: ProjectStore
) -> None:
    pid = store.create(kind="blank").id
    crlf = b"\\documentclass{article}\r\n\\begin{document}\r\nHello\r\n\\end{document}\r\n"
    store.write_bytes(pid, "main.tex", crlf)
    disk = store.read(pid, "main.tex")
    assert disk.eol == "crlf"
    buffer = "\\documentclass{article}\n\\begin{document}\nHello, edited\n\\end{document}\n"

    base = capture_base(store, pid, EditorContext("main.tex", disk.version, buffer=buffer))
    file = base.files["main.tex"]
    assert (file.source, file.eol, file.text) == ("buffer", "crlf", buffer)
    assert file.version == content_version(encode_text(buffer, "crlf"))
    assert base.tree().inputs["main.tex"] == file.version
    assert store.read(pid, "main.tex").version == disk.version  # the buffer is never written

    saved = client.put(
        f"/api/projects/{pid}/files/main.tex",
        json={"content": buffer},
        headers={"If-Match": f'"{disk.version}"'},
    )
    assert saved.status_code == 200 and saved.json()["version"] == file.version

    # A clean buffer (no buffer text), or one equal to the disk file, is the disk.
    clean = capture_base(store, pid, EditorContext("main.tex", file.version))
    same = capture_base(store, pid, EditorContext("main.tex", file.version, buffer=buffer))
    assert clean.files["main.tex"].source == same.files["main.tex"].source == "disk"
