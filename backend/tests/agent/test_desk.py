"""The ProposalDesk: the open Proposal, supersession and resolve (agent design §6.3 to §6.5)."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from prismtex.agent.errors import AgentError, ProjectBusy, ProposalClosed, ProposalNotFound
from prismtex.agent.proposals import Proposal, ProposalDesk, ProposalMeta, compute
from prismtex.agent.staging import Staging, capture_base
from prismtex.agent.threads import Resolution
from prismtex.agent.types import EditorContext, ResolveRequest, StaleHunk
from prismtex.agent.verify import Verification
from prismtex.history import Checkpoint, CheckpointReason, GitShadowHistory, History, NullHistory
from prismtex.workspace import FileVersion, ProjectStore

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


class Recording(NullHistory):
    """A History that records every checkpoint call and passes it on."""

    def __init__(self, inner: History | None = None) -> None:
        self.inner = inner or NullHistory()
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def checkpoint(
        self,
        project_id: str,
        *,
        reason: CheckpointReason,
        message: str,
        trailers: Mapping[str, str] | None = None,
    ) -> Checkpoint | None:
        self.calls.append((reason, message, dict(trailers or {})))
        return await self.inner.checkpoint(
            project_id, reason=reason, message=message, trailers=trailers
        )


@dataclass
class Env:
    store: ProjectStore
    pid: str
    history: Recording
    desk: ProposalDesk
    active: dict[str, str] = field(default_factory=dict[str, str])
    resolutions: list[tuple[str, str, Resolution]] = field(
        default_factory=list[tuple[str, str, Resolution]]
    )

    def new_desk(self) -> ProposalDesk:
        """A desk over the same data directory, as after a restart."""
        return ProposalDesk(
            self.store.layout.root,
            self.store,
            self.history,
            active_turn=self.active.get,
            on_resolved=lambda pid, thread, r: self.resolutions.append((pid, thread, r)),
        )

    def propose(
        self,
        edit: Callable[[Staging], Any],
        turn_id: str = "t_1",
        *,
        carried: Proposal | None = None,
        editor: EditorContext | None = None,
    ) -> Proposal:
        staging, _ = Staging.open(capture_base(self.store, self.pid, editor), carried)
        edit(staging)
        meta = ProposalMeta(
            turn_id=turn_id,
            project_id=self.pid,
            thread_id="th_1",
            model="fake:m",
            profile="agentic",
            created_at=f"2026-11-28T00:00:0{turn_id[-1]}.000Z",
            status="completed",
        )
        proposal = compute(staging.files(), (), CLEAN, meta, carried)
        assert proposal is not None
        return proposal

    def stored(self, proposal_id: str) -> Path:
        return self.store.layout.state(self.pid) / "proposals" / f"{proposal_id}.json"

    def text(self, path: str) -> str:
        return self.store.read(self.pid, path).text


def make_env(project: Project, history: History | None = None) -> Env:
    store, pid = project
    recording = Recording(history)
    env = Env(store, pid, recording, desk=None)  # pyright: ignore[reportArgumentType]
    env.desk = env.new_desk()
    return env


def git_history(store: ProjectStore) -> GitShadowHistory:
    found = GitShadowHistory.detect(store.layout.history_root, store)
    assert found is not None
    return found


def two_edits(text: str) -> str:
    return text.replace("two\n", "TWO\n").replace("four\n", "FOUR\n")


def request(env: Env, proposal: Proposal, **decisions: Any) -> ResolveRequest:
    return ResolveRequest(env.pid, proposal.id, **decisions)


@pytest.mark.anyio
async def test_desk_accept_takes_a_save_then_one_accept_checkpoint_and_closes_when_nothing_is_undecided(  # noqa: E501
    blank_project: Project, put: Put
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", PAPER)
    git = git_history(store)
    env = make_env(blank_project, git)

    def edit(staging: Staging) -> None:
        staging.write("main.tex", two_edits(PAPER), by="model")
        staging.create("sections/new.tex", "New.\n", by="model")

    proposal = env.propose(edit)
    env.desk.publish(proposal)
    assert env.desk.open_proposal(pid) == proposal
    assert json.loads(env.stored(proposal.id).read_bytes())["v"] == 1
    ids = tuple(h.id for h in proposal.hunks())
    assert len(ids) == 3

    result = await env.desk.resolve(request(env, proposal, accept="all"), by="user")
    assert (result.proposal_id, result.applied, result.rejected) == (proposal.id, ids, ())
    assert (result.stale, result.undecided, result.closed) == ((), (), True)
    assert [(f.path, f.text) for f in result.files] == [
        ("main.tex", two_edits(PAPER)),
        ("sections/new.tex", "New.\n"),
    ]
    assert result.files[0].version == store.read(pid, "main.tex").version
    assert env.text("main.tex") == two_edits(PAPER) and env.text("sections/new.tex") == "New.\n"

    assert [call[0] for call in env.history.calls] == ["save", "accept"]
    assert env.history.calls[0][1] == f"Changes before accepting proposal {proposal.id}"
    _, message, trailers = env.history.calls[1]
    assert message == f"Accept proposal {proposal.id}: 3 changes in main.tex and sections/new.tex"
    assert trailers == {
        "Proposal": proposal.id,
        "Turns": "t_1",
        "Model": "fake:m",
        "Profile": "agentic",
        "Hunks": ",".join(ids),
    }
    [accepted, *_] = await git.list(pid)
    assert result.checkpoint_id == accepted.id
    assert (accepted.reason, accepted.proposal_id, accepted.turn_ids, accepted.model) == (
        "accept",
        proposal.id,
        ("t_1",),
        "fake:m",
    )

    assert not env.stored(proposal.id).exists() and env.desk.open_proposal(pid) is None
    with pytest.raises(ProposalClosed) as closed:
        env.desk.get(pid, proposal.id)
    assert closed.value.detail["open_proposal_id"] is None
    [(project_id, thread_id, resolution)] = env.resolutions
    assert (project_id, thread_id) == (pid, "th_1")
    assert (resolution.proposal_id, resolution.applied, resolution.rejected) == (
        proposal.id,
        ids,
        (),
    )
    assert resolution.stale == () and resolution.at.endswith("Z")


@pytest.mark.anyio
async def test_accepting_one_hunk_keeps_its_neighbours_applicable_after_more_user_edits(
    blank_project: Project, put: Put
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", PAPER)
    env = make_env(blank_project)
    target = two_edits(PAPER).replace("six\n", "six\nsix and a half\n")
    proposal = env.propose(lambda s: s.write("main.tex", target, by="model"))
    env.desk.publish(proposal)
    first, second, third = proposal.hunks()

    result = await env.desk.resolve(request(env, proposal, accept={first.id}), by="user")
    assert result.applied == (first.id,) and result.undecided == (second.id, third.id)
    assert not result.closed and env.stored(proposal.id).is_file()
    reduced = env.desk.get(pid, proposal.id)
    [change] = reduced.files
    assert change.base_version == store.read(pid, "main.tex").version
    assert change.new_text == target and [h.id for h in change.hunks] == [second.id, third.id]
    assert change.hunks[0].before == ("one", "TWO", "three")  # context from the written text

    # The user saves an unrelated line at the top; the next accept still applies.
    put(store, pid, "main.tex", "% a note\n" + env.text("main.tex"))
    result = await env.desk.resolve(request(env, reduced, accept={second.id}), by="user")
    assert result.applied == (second.id,) and result.stale == ()
    result = await env.desk.resolve(request(env, reduced, accept="all"), by="user")
    assert result.applied == (third.id,) and result.closed
    assert env.text("main.tex") == "% a note\n" + target
    assert [r.applied for _, _, r in env.resolutions] == [(first.id,), (second.id,), (third.id,)]


@pytest.mark.anyio
async def test_desk_marks_file_missing_and_file_exists_stale_without_writing(
    blank_project: Project, put: Put
) -> None:
    store, pid = blank_project
    put(store, pid, "a.tex", "a\n")
    env = make_env(blank_project)

    def edit(staging: Staging) -> None:
        staging.write("a.tex", "A\n", by="model")
        staging.create("new.tex", "agent text\n", by="model")

    proposal = env.propose(edit)
    env.desk.publish(proposal)
    modify, create = proposal.hunks()
    store.delete_file(pid, "a.tex")
    put(store, pid, "new.tex", "the user's own file\n")
    users = store.read_bytes(pid, "new.tex")

    result = await env.desk.resolve(request(env, proposal, accept="all"), by="user")
    assert result.applied == () and result.files == () and result.checkpoint_id is None
    assert result.stale == (
        StaleHunk(modify.id, "a.tex", "file_missing"),
        StaleHunk(create.id, "new.tex", "file_exists"),
    )
    assert result.undecided == (modify.id, create.id) and not result.closed
    assert store.read_bytes(pid, "new.tex") == users
    assert [call[0] for call in env.history.calls] == ["save"]  # nothing written: no accept
    assert env.desk.get(pid, proposal.id) == proposal
    assert env.resolutions[-1][2].stale == (modify.id, create.id)


@pytest.mark.anyio
async def test_resolve_rebases_after_an_unrelated_user_save_and_marks_overlap_stale(
    blank_project: Project, put: Put
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", PAPER)
    env = make_env(blank_project)
    target = PAPER.replace("two\n", "TWO\n").replace("six\n", "SIX\n")
    proposal = env.propose(lambda s: s.write("main.tex", target, by="model"))
    env.desk.publish(proposal)
    shifted, overlapped = proposal.hunks()
    users = "% a note\n" + PAPER.replace("six\n", "six, rewritten\n")
    put(store, pid, "main.tex", users)

    result = await env.desk.resolve(request(env, proposal, accept="all"), by="user")
    assert result.applied == (shifted.id,)
    assert result.stale == (StaleHunk(overlapped.id, "main.tex", "context_changed"),)
    assert env.text("main.tex") == users.replace("two\n", "TWO\n")
    assert result.undecided == (overlapped.id,) and not result.closed
    [change] = env.desk.get(pid, proposal.id).files
    assert change.hunks == (overlapped,)  # it no longer applies, so it is kept as it was

    rejected = await env.desk.resolve(request(env, proposal, reject="all"), by="user")
    assert rejected.rejected == (overlapped.id,) and rejected.closed
    assert env.text("main.tex") == users.replace("two\n", "TWO\n")


@pytest.mark.anyio
async def test_resolve_with_a_mismatched_base_version_writes_nothing(
    blank_project: Project, put: Put
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", PAPER)
    env = make_env(blank_project)
    proposal = env.propose(lambda s: s.write("main.tex", two_edits(PAPER), by="model"))
    env.desk.publish(proposal)
    before = store.read_bytes(pid, "main.tex")
    result = await env.desk.resolve(
        request(env, proposal, accept="all", base_versions={"main.tex": "0" * 64}), by="user"
    )
    assert result.applied == () and {s.reason for s in result.stale} == {"version_mismatch"}
    assert store.read_bytes(pid, "main.tex") == before and result.files == ()


@pytest.mark.anyio
async def test_resolve_is_refused_while_a_turn_is_active_unless_auto(
    blank_project: Project, put: Put
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", PAPER)
    env = make_env(blank_project)
    proposal = env.propose(lambda s: s.write("main.tex", two_edits(PAPER), by="model"))
    env.desk.publish(proposal)
    env.active[pid] = "t_9"
    with pytest.raises(ProjectBusy) as busy:
        await env.desk.resolve(request(env, proposal, accept="all"), by="user")
    assert busy.value.detail["active_turn_id"] == "t_9"
    assert env.text("main.tex") == PAPER and env.history.calls == []

    result = await env.desk.resolve(request(env, proposal, accept="all"), by="auto")
    assert result.closed and env.text("main.tex") == two_edits(PAPER)
    assert [call[0] for call in env.history.calls] == ["save", "auto_accept"]


@pytest.mark.anyio
async def test_a_superseded_proposal_is_closed_and_the_open_one_survives_a_restart(
    blank_project: Project, put: Put
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", PAPER)
    env = make_env(blank_project)
    first = env.propose(lambda s: s.write("main.tex", two_edits(PAPER), by="model"))
    env.desk.publish(first)
    second = env.propose(
        lambda s: s.write("main.tex", two_edits(PAPER).replace("six\n", "SIX\n"), by="model"),
        "t_2",
        carried=first,
    )
    assert second.supersedes == first.id and {h.id for h in first.hunks()} < second.undecided
    env.desk.publish(second)
    assert env.desk.open_proposal(pid) == second and not env.stored(first.id).exists()

    for desk in (env.desk, env.new_desk()):
        with pytest.raises(ProposalClosed) as closed:
            desk.get(pid, first.id)
        assert closed.value.detail["open_proposal_id"] == second.id
        with pytest.raises(ProposalClosed):
            await desk.resolve(request(env, first, accept="all"), by="user")
        for unknown in ("t_unknown", "../escape", ""):
            with pytest.raises(ProposalNotFound):
                desk.get(pid, unknown)

    restarted = env.new_desk()
    assert restarted.open_proposal(pid) == second
    result = await restarted.resolve(request(env, second, accept="all"), by="user")
    assert result.closed and env.text("main.tex") == two_edits(PAPER).replace("six\n", "SIX\n")


def test_publishing_a_proposal_with_nothing_to_review_closes_the_open_one(
    blank_project: Project, put: Put
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", PAPER)
    env = make_env(blank_project)
    first = env.propose(lambda s: s.write("main.tex", two_edits(PAPER), by="model"))
    env.desk.publish(first)
    # A follow-up Turn that only took the carried changes back.
    taken_back = env.propose(lambda s: s.write("main.tex", PAPER, by="model"), "t_2", carried=first)
    assert taken_back.undecided == frozenset() and taken_back.supersedes == first.id
    env.desk.publish(taken_back)
    assert env.desk.open_proposal(pid) is None and not env.stored(first.id).exists()
    with pytest.raises(ProposalClosed):
        env.desk.get(pid, taken_back.id)

    # Publishing an id again makes it the open Proposal once more.
    env.desk.publish(first)
    assert env.desk.open_proposal(pid) == first and env.desk.get(pid, first.id) == first


@pytest.mark.anyio
async def test_reject_takes_hunks_out_without_writing_and_tells_the_thread(
    blank_project: Project, put: Put
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", PAPER)
    env = make_env(blank_project)
    proposal = env.propose(lambda s: s.write("main.tex", two_edits(PAPER), by="model"))
    env.desk.publish(proposal)
    two, four = proposal.hunks()
    before = store.read_bytes(pid, "main.tex")

    nothing = await env.desk.resolve(request(env, proposal), by="user")
    assert nothing.undecided == (two.id, four.id) and env.resolutions == []

    result = await env.desk.resolve(request(env, proposal, reject={two.id}), by="user")
    assert (result.applied, result.rejected, result.undecided) == ((), (two.id,), (four.id,))
    [change] = env.desk.get(pid, proposal.id).files
    assert change.hunks == (four,) and change.new_text == PAPER.replace("four\n", "FOUR\n")
    result = await env.desk.resolve(request(env, proposal, reject="all"), by="user")
    assert result.rejected == (four.id,) and result.closed and result.checkpoint_id is None
    assert store.read_bytes(pid, "main.tex") == before and env.history.calls == []
    assert [(r.rejected, r.applied) for _, _, r in env.resolutions] == [
        ((two.id,), ()),
        ((four.id,), ()),
    ]


@pytest.mark.anyio
async def test_resolve_rejects_unknown_ids_and_overlapping_decisions(
    blank_project: Project, put: Put
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", PAPER)
    env = make_env(blank_project)
    proposal = env.propose(lambda s: s.write("main.tex", two_edits(PAPER), by="model"))
    env.desk.publish(proposal)
    two, _ = proposal.hunks()
    for decisions in (
        {"accept": frozenset({"h000000000000"})},
        {"reject": frozenset({"h000000000000"})},
        {"accept": frozenset({two.id}), "reject": frozenset({two.id})},
        {"accept": "all", "reject": "all"},
    ):
        with pytest.raises(AgentError) as error:
            await env.desk.resolve(request(env, proposal, **decisions), by="user")
        assert error.value.code == "invalid_request"
    assert env.text("main.tex") == PAPER and env.history.calls == []


@pytest.mark.anyio
async def test_accept_keeps_crlf_and_the_line_endings_of_untouched_lines(
    blank_project: Project,
) -> None:
    store, pid = blank_project
    store.write_bytes(pid, "crlf.tex", b"one\r\ntwo\r\nthree\r\n")
    store.write_bytes(pid, "mixed.tex", b"a\r\nb\nc\r\nd\n")
    env = make_env(blank_project)

    def edit(staging: Staging) -> None:
        staging.write("crlf.tex", "one\nTWO\nthree\nfour\n", by="model")
        staging.write("mixed.tex", "a\nb\nC\nd\ne\n", by="model")

    proposal = env.propose(edit)
    env.desk.publish(proposal)
    result = await env.desk.resolve(request(env, proposal, accept="all"), by="user")
    assert result.closed
    assert store.read_bytes(pid, "crlf.tex")[0] == b"one\r\nTWO\r\nthree\r\nfour\r\n"
    assert store.read_bytes(pid, "mixed.tex")[0] == b"a\r\nb\nC\nd\ne\n"
    assert dict((f.path, f.text) for f in result.files)["mixed.tex"] == "a\nb\nC\nd\ne\n"
    assert result.files[1].version == store.read_bytes(pid, "mixed.tex")[1]


@pytest.mark.anyio
async def test_accepting_a_delete_removes_the_file_only_while_it_is_unchanged(
    blank_project: Project, put: Put
) -> None:
    store, pid = blank_project
    put(store, pid, "a.tex", "a\n")
    put(store, pid, "b.tex", "b\n")
    env = make_env(blank_project)

    def edit(staging: Staging) -> None:
        staging.delete("a.tex", by="model")
        staging.delete("b.tex", by="model")

    proposal = env.propose(edit)
    env.desk.publish(proposal)
    delete_a, delete_b = proposal.hunks()
    put(store, pid, "b.tex", "b, edited\n")
    result = await env.desk.resolve(request(env, proposal, accept="all"), by="user")
    assert result.applied == (delete_a.id,)
    assert result.stale == (StaleHunk(delete_b.id, "b.tex", "context_changed"),)
    assert [(f.path, f.version, f.text) for f in result.files] == [("a.tex", None, None)]
    assert [e.path for e in store.list_files(pid)] == ["b.tex", "main.tex"]


@pytest.mark.anyio
async def test_a_buffer_based_proposal_applies_once_the_buffer_is_saved(
    blank_project: Project, put: Put
) -> None:
    store, pid = blank_project
    disk = put(store, pid, "main.tex", PAPER)
    buffer = PAPER.replace("seven\n", "seven, typed but not saved\n")
    editor = EditorContext("main.tex", disk.version, buffer=buffer)
    proposal = (env := make_env(blank_project)).propose(
        lambda s: s.write("main.tex", two_edits(s.read("main.tex").text), by="model"),
        editor=editor,
    )
    [change] = proposal.files
    assert change.base_source == "buffer" and not proposal.auto_accept_eligible
    env.desk.publish(proposal)
    assert env.text("main.tex") == PAPER  # the buffer is never written by the agent

    saved = store.write(pid, "main.tex", buffer, base_version=disk.version)
    assert saved.version == change.base_version
    result = await env.desk.resolve(
        request(env, proposal, accept="all", base_versions={"main.tex": saved.version}),
        by="user",
    )
    assert result.closed and env.text("main.tex") == two_edits(buffer)
