"""Workspace pieces the agent needs: write_new, versioned delete, the source
path policy and agent settings (agent design §2.3, §10.3, §12)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from prismtex.layout import DataLayout
from prismtex.workspace import (
    AgentSettings,
    PathDenied,
    PathPolicyError,
    ProjectStore,
    VersionConflict,
    check_source_path,
    content_version,
)

Project = tuple[ProjectStore, str]


def test_crlf_file_has_a_bytes_version_and_write_new_refuses_an_existing_file(
    blank_project: Project,
) -> None:
    store, pid = blank_project
    raw = b"\\documentclass{article}\r\nHi\r\n"
    (store.project_dir(pid) / "main.tex").write_bytes(raw)
    fv = store.read(pid, "main.tex")
    assert fv.text == "\\documentclass{article}\nHi\n" and fv.eol == "crlf"
    assert fv.version == content_version(raw) == hashlib.sha256(raw).hexdigest()
    with pytest.raises(VersionConflict) as exc:
        store.write_new(pid, "main.tex", "replaced\n")
    assert exc.value.current_version == fv.version
    assert exc.value.extra["current_version"] == fv.version
    assert (store.project_dir(pid) / "main.tex").read_bytes() == raw


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "C:/x.tex",
        "C:x.tex",
        "\\\\srv\\share\\a.tex",
        "../x.tex",
        "a/../../x.tex",
        "a\x00.tex",
        "a<b.tex",
        "main.tex:ads",
        "CON.tex",
        "aux.bib",
        "com1.txt",
        "a./b.tex",
        "b.tex ",
        "-x.tex",
        "sections/-x.tex",
        "a" * 237 + ".tex",
        ".git/config",
        ".prism/meta.json",
        ".prismtex/x",
        ".GIT/config",
    ],
)
def test_check_source_path_denies_escapes_devices_streams_and_state_prefixes(path: str) -> None:
    with pytest.raises(PathDenied) as exc:
        check_source_path(path, for_write=False)
    assert exc.value.code == "path_denied"
    assert exc.value.path == path


@pytest.mark.parametrize(
    "path", ["latexmkrc", ".latexmkrc", "build.lua", "x.py", "run.pl", "fig.png"]
)
def test_check_source_path_for_write_allows_only_source_extensions(path: str) -> None:
    assert check_source_path(path, for_write=False) == path
    with pytest.raises(PathDenied) as exc:
        check_source_path(path, for_write=True)
    assert exc.value.code == "write_denied"


def test_check_source_path_normalizes_separators_and_dot_segments() -> None:
    assert check_source_path("sections\\intro.tex", for_write=True) == "sections/intro.tex"
    assert check_source_path("./sections/./refs.bib", for_write=True) == "sections/refs.bib"


def test_path_denied_is_a_phase_0_path_policy_error() -> None:
    with pytest.raises(PathPolicyError):
        check_source_path("../x.tex", for_write=False)
    denied = PathDenied("x.py", "write_denied", "nope", code="write_denied")
    assert isinstance(denied, PathPolicyError)
    assert denied.to_body()["detail"]["code"] == "write_denied"


def test_list_files_reports_kinds_and_sizes(blank_project: Project, put) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", "hello\n")
    store.write_bytes(pid, "fig.png", b"\x89PNG....")
    entries = {e.path: e for e in store.list_files(pid)}
    assert entries["main.tex"].kind == "source"
    assert entries["main.tex"].size == len(b"hello\n")
    assert entries["fig.png"].kind == "asset"
    assert entries["fig.png"].size == 8


def test_delete_with_a_stale_base_version_raises_version_conflict_and_keeps_the_file(
    blank_project: Project, put
) -> None:
    store, pid = blank_project
    first = put(store, pid, "notes.tex", "one\n")
    put(store, pid, "notes.tex", "two\n")
    with pytest.raises(VersionConflict) as exc:
        store.delete_file(pid, "notes.tex", base_version=first.version)
    assert exc.value.current_version == store.read(pid, "notes.tex").version
    assert store.read(pid, "notes.tex").text == "two\n"

    store.delete_file(pid, "notes.tex", base_version=store.read(pid, "notes.tex").version)
    assert "notes.tex" not in {e.path for e in store.list_files(pid)}


def test_delete_with_a_base_version_of_a_missing_file_is_a_conflict(
    blank_project: Project,
) -> None:
    store, pid = blank_project
    with pytest.raises(VersionConflict) as exc:
        store.delete_file(pid, "gone.tex", base_version="0" * 64)
    assert exc.value.current_version is None


def test_settings_agent_defaults_and_update_persist_in_state(
    blank_project: Project, tmp_path: Path
) -> None:
    store, pid = blank_project
    assert store.settings(pid).agent == AgentSettings()
    files_before = store.list_files(pid)

    store.update_settings(pid, agent=AgentSettings(auto_accept=True, profile="guided"))

    fresh = ProjectStore(DataLayout(tmp_path))
    agent = fresh.settings(pid).agent
    assert agent.auto_accept is True and agent.profile == "guided" and agent.model is None
    assert store.list_files(pid) == files_before


def test_agent_settings_reject_an_unknown_profile() -> None:
    with pytest.raises(ValueError):
        AgentSettings(profile="chatty")  # pyright: ignore[reportArgumentType]


def test_update_settings_and_phase_0_update_keep_each_others_fields(
    blank_project: Project, put
) -> None:
    store, pid = blank_project
    put(store, pid, "thesis.tex", "\\documentclass{article}\n")

    store.update_settings(pid, agent=AgentSettings(model="ollama:qwen3"))
    store.update(pid, root_file="thesis.tex")
    settings = store.settings(pid)
    assert settings.root_file == "thesis.tex" and settings.agent.model == "ollama:qwen3"

    store.update(pid, engine="xelatex")
    store.update_settings(pid, agent=AgentSettings(auto_accept=True))
    settings = store.settings(pid)
    assert settings.engine == "xelatex" and settings.root_file == "thesis.tex"
    assert settings.agent == AgentSettings(auto_accept=True)


def test_a_broken_agent_block_reads_as_defaults(blank_project: Project) -> None:
    store, pid = blank_project
    path = store.layout.project_json(pid)
    path.write_text(path.read_text(encoding="utf-8").replace('"schema"', '"agent": 7, "schema"'))
    assert store.settings(pid).agent == AgentSettings()


def test_settings_of_a_project_without_a_root_file_reports_none(blank_project: Project) -> None:
    store, pid = blank_project
    store.update(pid, root_file=None)
    assert store.settings(pid).root_file is None


def test_write_new_creates_an_lf_file_and_write_without_base_version_stays_unconditional(
    blank_project: Project,
) -> None:
    store, pid = blank_project
    created = store.write_new(pid, "sections/new.tex", "a\r\nb\n")
    on_disk = (store.project_dir(pid) / "sections" / "new.tex").read_bytes()
    assert on_disk == b"a\nb\n"
    assert created.eol == "lf" and created.version == content_version(on_disk)

    again = store.write(pid, "sections/new.tex", "c\n", base_version=None)
    assert (store.project_dir(pid) / "sections" / "new.tex").read_bytes() == b"c\n"
    assert again.version != created.version
