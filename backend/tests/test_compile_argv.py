"""The latexmk argv and its environment: the flags of spec §11.1 and no secrets."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from prismtex.compile import build_latexmk_argv, build_tex_env, is_shell_safe_name
from prismtex.compile.argv import resolve_tools

OUT = Path("/data/build/p/out").resolve()

# The guard latexmk is told to run instead of biber (always wired, even on
# POSIX, because it closes the Biber outside-read and the eval-based RCE).
BIBER_E = f"$biber=q{{{sys.executable} -P -m prismtex.compile.biberguard %O %S}}"


def _tail(out: Path) -> list[str]:
    return [
        "-interaction=nonstopmode",
        "-file-line-error",
        "-synctex=1",
        "-no-shell-escape",
        f"-outdir={out}",
    ]


@pytest.mark.parametrize(
    ("engine", "pdf_flag", "engine_opt"),
    [
        ("pdflatex", "-pdf", "-pdflatex=pdflatex %O %S"),
        ("xelatex", "-pdfxe", "-xelatex=xelatex %O %S"),
        ("lualatex", "-pdflua", "-lualatex=lualatex --safer --nosocket %O %S"),
    ],
)
def test_argv_per_engine(engine: str, pdf_flag: str, engine_opt: str) -> None:
    # tools=None: every program keeps its bare name (the POSIX/default shape).
    argv = build_latexmk_argv("main.tex", engine, OUT)  # pyright: ignore[reportArgumentType]
    assert argv == [
        "latexmk",
        "-norc",
        pdf_flag,
        engine_opt,
        "-e",
        BIBER_E,
        *_tail(OUT),
        "main.tex",
    ]


def test_lualatex_command_is_one_argv_element() -> None:
    argv = build_latexmk_argv("main.tex", "lualatex", OUT)
    assert "-lualatex=lualatex --safer --nosocket %O %S" in argv


def test_tools_are_passed_by_absolute_path() -> None:
    # CORRECTNESS-1: the engine and every helper tool is named by an absolute
    # path, so Windows cannot run a same-named file from the project folder.
    tools = {
        "pdflatex": "/opt/texbin/pdflatex",
        "bibtex": "/opt/texbin/bibtex",
        "makeindex": "/opt/texbin/makeindex",
        "kpsewhich": "/opt/texbin/kpsewhich",
    }
    argv = build_latexmk_argv("main.tex", "pdflatex", OUT, tools=tools)
    assert "-pdflatex=/opt/texbin/pdflatex %O %S" in argv
    assert "$bibtex=q{/opt/texbin/bibtex %O %S}" in argv
    assert "$makeindex=q{/opt/texbin/makeindex %O -o %D %S}" in argv
    assert "$kpsewhich=q{/opt/texbin/kpsewhich %S}" in argv
    # biber always routes through the guard, run by this interpreter.
    assert BIBER_E in argv


def test_resolve_tools_finds_real_tex_binaries() -> None:
    tools = resolve_tools(os.environ.get("PATH"))
    # pdflatex, bibtex, biber and kpsewhich are present in the TeX lane's env.
    pdflatex = tools.get("pdflatex")
    assert pdflatex is not None and os.path.isabs(pdflatex)


def test_argv_never_enables_shell_escape_or_rc_files() -> None:
    for engine in ("pdflatex", "xelatex", "lualatex"):
        argv = build_latexmk_argv("sections/paper.tex", engine, OUT)  # pyright: ignore[reportArgumentType]
        assert argv[:2] == ["latexmk", "-norc"]
        assert argv[-1] == "sections/paper.tex"
        joined = " ".join(argv)
        assert "-shell-escape" not in joined.replace("-no-shell-escape", "")
        assert "-halt-on-error" not in joined
        assert "-r" not in argv
        # The only -e assignments pin known tools (biber guard, bibtex, ...).
        values = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
        assert all(
            v.startswith(("$biber=", "$bibtex=", "$makeindex=", "$kpsewhich=")) for v in values
        )


def test_relative_out_dir_becomes_absolute() -> None:
    argv = build_latexmk_argv("main.tex", "pdflatex", Path("build/out"))
    assert f"-outdir={os.path.abspath('build/out')}" in argv


@pytest.mark.parametrize(
    "root_file",
    ["-main.tex", "", "a`touch x`b.tex", "a$HOME.tex", 'a"b.tex', "a%b.tex", "a!b.tex", "a\\b.tex"],
)
def test_unsafe_root_files_are_refused(root_file: str) -> None:
    with pytest.raises(ValueError):
        build_latexmk_argv(root_file, "pdflatex", OUT)


def test_unknown_engine_is_refused() -> None:
    with pytest.raises(ValueError):
        build_latexmk_argv("main.tex", "tex", OUT)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    ("name", "safe"),
    [
        ("main.tex", True),
        ("my paper (v2).tex", True),
        ("sections/a;b.tex", True),
        ("résumé.tex", True),
        ("a`b`.tex", False),
        ("a$b.tex", False),
        ("a\nb.tex", False),
    ],
)
def test_shell_safe_names(name: str, safe: bool) -> None:
    assert is_shell_safe_name(name) is safe


BASE = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/ann",
    "LANG": "en_US.UTF-8",
    "TMPDIR": "/tmp",
    "TEXMFHOME": "/home/ann/texmf",
    "MIKTEX_USERCONFIG": "C:/miktex",
    "SystemRoot": "C:/Windows",
    # Everything below must not reach TeX.
    "OPENAI_API_KEY": "sk-secret",
    "ANTHROPIC_API_KEY": "sk-ant-secret",
    "PRISMTEX_ACCESS_TOKEN": "token-secret",
    "AWS_SECRET_ACCESS_KEY": "aws-secret",
    "TEXINPUTS": "/etc//:",
    "BIBINPUTS": "/etc//:",
    "LD_PRELOAD": "/tmp/evil.so",
    "PERL5OPT": "-Mevil",
    "PERL5LIB": "/tmp/evil",
    "LATEXMKRCSYS": "/tmp/evil.rc",
    "openin_any": "a",
    "openout_any": "a",
    "shell_escape": "t",
    "max_print_line": "79",
}


def test_env_keeps_only_the_allowlist() -> None:
    env = build_tex_env(BASE, OUT)
    prismtex_parent = env["PYTHONPATH"]  # the dir holding the prismtex package
    assert (Path(prismtex_parent) / "prismtex" / "__init__.py").is_file()
    assert env == {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/ann",
        "LANG": "en_US.UTF-8",
        "TMPDIR": "/tmp",
        "TEXMFHOME": "/home/ann/texmf",
        "MIKTEX_USERCONFIG": "C:/miktex",
        "SystemRoot": "C:/Windows",
        "openin_any": "p",
        "openout_any": "p",
        "shell_escape": "f",
        "TEXMFOUTPUT": str(OUT),
        "max_print_line": "10000",
        "error_line": "254",
        "half_error_line": "238",
        "MISSFONT_LOG": "0",
        "NoDefaultCurrentDirectoryInExePath": "1",
        "PYTHONPATH": prismtex_parent,
    }


def test_env_has_no_secret_values() -> None:
    values = set(build_tex_env(BASE, OUT).values())
    for secret in ("sk-secret", "sk-ant-secret", "token-secret", "aws-secret", "/tmp/evil.so"):
        assert secret not in values


def test_env_from_the_real_environment_drops_prismtex_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRISMTEX_ACCESS_TOKEN", "do-not-leak")
    env = build_tex_env(os.environ, OUT)
    assert "PRISMTEX_ACCESS_TOKEN" not in env
    assert "do-not-leak" not in env.values()
