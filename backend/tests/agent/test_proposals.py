"""Hunks, rebase, line maps and Proposal computation (agent design §6.1 to §6.4)."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

import pytest

from prismtex.agent.proposals import (
    AlreadyApplied,
    ChangeNote,
    Exact,
    Proposal,
    ProposalMeta,
    Shifted,
    Stale,
    compute,
    line_map,
    make_hunk,
    rebase,
    split_lines,
)
from prismtex.agent.staging import BaseFile, StagedFile
from prismtex.agent.types import TurnStatus
from prismtex.agent.verify import DiagnosticView, Verification
from prismtex.workspace import content_version

CLEAN = Verification("clean", None, 0, 0, 0, (), (), 0, (), True)

BASE = [
    "\\section{A}",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "\\section{B}",
    "nine",
]
TEXT = "\n".join(BASE) + "\n"


def test_rebase_applies_a_shifted_hunk_and_marks_an_overlapping_one_stale() -> None:
    h1 = make_hunk("main.tex", BASE, 3, ("two",), ("TWO",), origin="model")
    h2 = make_hunk("main.tex", BASE, 8, ("seven",), ("SEVEN",), origin="model")
    current = ["% a note the user added"] + BASE[:7] + ["seven!"] + BASE[8:]
    assert rebase(h1, BASE) == Exact(start=3)
    assert rebase(h1, current) == Shifted(start=4)
    assert rebase(h2, current) == Stale(reason="context_changed")
    applied = BASE[:2] + ["TWO"] + BASE[3:]
    assert rebase(h1, applied) == AlreadyApplied(start=3)


def base_file(path: str, text: str, source: Literal["disk", "buffer"] = "disk") -> BaseFile:
    return BaseFile(path, text, content_version(text.encode()), "lf", source)


def staged(
    path: str, base: str, text: str | None, *, source: Literal["disk", "buffer"] = "disk"
) -> StagedFile:
    return StagedFile(path, base_file(path, base, source), base, text, ())


def meta(status: TurnStatus = "completed", turn_id: str = "t_1") -> ProposalMeta:
    created = "2026-11-28T00:00:00.000Z"
    return ProposalMeta(turn_id, "p1", "th_1", "fake:m", "guided", created, status)


def test_hunk_ids_are_stable_content_hashes_with_collision_suffixes() -> None:
    h = make_hunk("main.tex", BASE, 3, ("two",), ("TWO",), origin="model")
    context = (["\\section{A}", "one"], ["three", "four", "five"])
    payload = json.dumps(["main.tex", context[0], ["two"], ["TWO"], context[1]])
    assert h.id == "h" + hashlib.sha256(payload.encode()).hexdigest()[:12]
    assert (list(h.before), list(h.after)) == context
    assert make_hunk("main.tex", BASE, 3, ("two",), ("TWO",), origin="host").id == h.id
    assert make_hunk("other.tex", BASE, 3, ("two",), ("TWO",), origin="model").id != h.id
    assert make_hunk("main.tex", BASE, 3, ("two",), ("Two",), origin="model").id != h.id
    with pytest.raises(ValueError):
        make_hunk("main.tex", BASE, 3, ("one",), ("ONE",), origin="model")

    # Two identical changes with identical context: the second id gets "-2".
    lines = ["x"] * 3 + ["a"] + ["x"] * 6 + ["a"] + ["x"] * 3
    text = "\n".join(lines) + "\n"
    files = [staged("rep.tex", text, text.replace("a\n", "b\n"))]
    proposal = compute(files, (), CLEAN, meta(), None)
    assert proposal is not None
    first, second = proposal.files[0].hunks
    assert first.id == make_hunk("rep.tex", lines, 4, ("a",), ("b",), origin="model").id
    assert (second.old_start, second.id) == (11, first.id + "-2")
    assert proposal.undecided == {first.id, second.id}
    again = compute(files, (), CLEAN, meta(turn_id="t_2"), None)
    assert again is not None and [h.id for h in again.hunks()] == [first.id, second.id]


def test_compute_returns_none_when_nothing_differs_from_the_start_text() -> None:
    assert compute([], (), CLEAN, meta(), None) is None
    assert compute([staged("main.tex", TEXT, TEXT)], (), CLEAN, meta(), None) is None
    # Whether the file ends with a line break is not a change on its own.
    assert compute([staged("main.tex", TEXT, TEXT[:-1])], (), CLEAN, meta(), None) is None
    carried = TEXT.replace("two\n", "TWO\n")
    kept = StagedFile("main.tex", base_file("main.tex", TEXT), carried, carried, ())
    assert compute([kept], (), CLEAN, meta(), None) is None

    # Taking a carried change back is a change: it supersedes with nothing to review.
    back = StagedFile("main.tex", base_file("main.tex", TEXT), carried, TEXT, ())
    empty = compute([back], (), CLEAN, meta(), None)
    assert empty is not None and empty.files == () and empty.undecided == frozenset()


def test_line_map_round_trips_unchanged_lines_and_returns_none_for_replaced_ones() -> None:
    new_lines = ["% top", *BASE[:2], "TWO", "two and a half", *BASE[3:7], *BASE[8:]]
    proposal = compute(
        [staged("main.tex", TEXT, "\n".join(new_lines) + "\n")], (), CLEAN, meta(), None
    )
    assert proposal is not None
    [change] = proposal.files
    assert [(h.old_start, h.old_lines, h.new_lines) for h in change.hunks] == [
        (0, (), ("% top",)),
        (3, ("two",), ("TWO", "two and a half")),
        (8, ("seven",), ()),
    ]
    lines = line_map(change)
    new = split_lines(change.new_text or "")
    assert new == new_lines
    for number, line in enumerate(BASE, 1):
        mapped = lines.to_new(number)
        if line in ("two", "seven"):
            assert mapped is None
        else:
            assert mapped is not None and new[mapped - 1] == line
            assert lines.to_base(mapped) == number
    assert [lines.to_base(n) for n in (1, 4, 5)] == [None, None, None]
    assert lines.to_new(0) is None and lines.to_new(len(BASE) + 1) is None

    created = compute([StagedFile("new.tex", None, None, "a\n", ())], (), CLEAN, meta(), None)
    assert created is not None and line_map(created.files[0]).runs == ()


def test_rebase_tries_old_lines_alone_and_anchors_pure_insertions() -> None:
    pair = make_hunk("main.tex", BASE, 3, ("two", "three"), ("2", "3"), origin="model")
    single = make_hunk("main.tex", BASE, 3, ("two",), ("2",), origin="model")
    moved = ["X", "Y", "Z", "two", "three", "W", "five"]
    assert rebase(pair, moved) == Shifted(start=4)  # two lines, found once
    assert rebase(single, moved) == Stale()  # one line alone is never enough
    assert rebase(pair, [*moved, "two", "three"]) == Stale()  # found twice

    insert = make_hunk("main.tex", BASE, 2, (), ("inserted",), origin="model")
    assert (insert.before, insert.after) == (("\\section{A}", "one"), ("two", "three", "four"))
    assert rebase(insert, BASE) == Exact(start=2)
    assert rebase(insert, ["top", *BASE]) == Shifted(start=3)
    assert rebase(insert, [*BASE[:2], "inserted", *BASE[2:]]) == AlreadyApplied(start=3)
    assert rebase(insert, [*BASE[:2], "between", *BASE[2:]]) == Stale()

    into_empty = make_hunk("new.tex", [], 0, (), ("hello",), origin="model")
    assert rebase(into_empty, []) == Exact(start=0)
    assert rebase(into_empty, ["hello"]) == AlreadyApplied(start=1)
    assert rebase(into_empty, ["other"]) == Stale()
    emptied = make_hunk("old.tex", ["a", "b"], 1, ("a", "b"), (), origin="model")
    assert rebase(emptied, ["a", "b"]) == Exact(start=1)
    assert rebase(emptied, []) == AlreadyApplied(start=0)


def test_compute_makes_create_and_delete_hunks_and_decides_auto_accept() -> None:
    files = [
        StagedFile("new.tex", None, None, "Line one.\nLine two.\n", ()),
        StagedFile("old.tex", base_file("old.tex", "gone\n"), "gone\n", None, ()),
        staged("main.tex", TEXT, TEXT.replace("two\n", "TWO\n")),
    ]
    proposal = compute(files, (), CLEAN, meta(), None)
    assert proposal is not None
    assert [(c.path, c.op, c.added, c.removed) for c in proposal.files] == [
        ("main.tex", "modify", 1, 1),
        ("new.tex", "create", 2, 0),
        ("old.tex", "delete", 0, 1),
    ]
    modify, create, delete = proposal.files
    assert modify.base_version == content_version(TEXT.encode()) and modify.base_source == "disk"
    assert (create.base_version, create.new_text) == (None, "Line one.\nLine two.\n")
    assert [(h.old_start, h.old_lines, h.new_lines) for h in create.hunks] == [
        (0, (), ("Line one.", "Line two."))
    ]
    assert (delete.new_text, delete.base_version) == (None, content_version(b"gone\n"))
    assert [(h.old_start, h.old_lines, h.new_lines) for h in delete.hunks] == [(1, ("gone",), ())]
    assert (proposal.id, proposal.supersedes, proposal.turn_ids) == ("t_1", None, ("t_1",))
    assert proposal.undecided == {h.id for h in proposal.hunks()}
    assert proposal.auto_accept_eligible

    removed = ChangeNote("heading_removed", "main.tex", 3, "\\section{A}", None)
    added = ChangeNote("heading_added", "main.tex", 3, None, "\\section{C}")
    regressed = Verification("regressed", None, 2, 0, 1, (), (), 0, (), True)

    def eligible(
        *,
        notes: tuple[ChangeNote, ...] = (),
        verification: Verification = CLEAN,
        status: TurnStatus = "completed",
        source: Literal["disk", "buffer"] = "disk",
    ) -> bool:
        changed = [*files[:2], staged("main.tex", TEXT, TEXT.replace("two", "2"), source=source)]
        result = compute(changed, notes, verification, meta(status), None)
        assert result is not None
        return result.auto_accept_eligible

    assert eligible(notes=(added,))
    assert not eligible(notes=(removed,))
    assert not eligible(verification=regressed)
    assert not eligible(status="budget_exhausted")
    assert not eligible(source="buffer")


def test_proposal_json_round_trips_and_rejects_other_versions() -> None:
    error = DiagnosticView("main.tex", 4, "error", "undefined_control_sequence", "Undefined", "\\x")
    verification = Verification("regressed", None, 1, 0, 1, (error,), (), 2, ("added x",), False)
    notes = (ChangeNote("label_removed", "main.tex", 2, "sec:a", None, dangling_refs=1),)
    files = [
        staged("main.tex", TEXT, TEXT.replace("two\n", "TWO\n")),
        StagedFile("new.tex", None, None, "Ünïcode ✓\n", ("Ünïcode ✓",)),
    ]
    proposal = compute(files, notes, verification, meta(), None)
    assert proposal is not None and proposal.hunks()[1].origin == "host"
    data = json.loads(json.dumps(proposal.to_dict()))
    assert data["v"] == 1 and data["undecided"] == [h.id for h in proposal.hunks()]
    assert Proposal.from_dict(data) == proposal
    with pytest.raises(ValueError):
        Proposal.from_dict({**data, "v": 2})
    with pytest.raises(ValueError):
        Proposal.from_dict({k: v for k, v in data.items() if k != "files"})
    with pytest.raises(ValueError):
        Proposal.from_dict({**data, "profile": "unknown"})
