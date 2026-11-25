# Compiling

PrismTeX compiles a project with latexmk. The server builds the command line and the environment itself, so a project cannot choose flags or run code, and TeX cannot read or write files outside the project (Biber is an exception; see Known limits). This page says how a compile works, what each hardening setting is for, what was verified and on which TeX distribution, and what is known not to be covered.

The code is in `backend/src/prismtex/compile/`.

## What happens on a compile

1. `POST /api/projects/{id}/compile` calls `Compiler.compile`. The body may name a `root_file` and an `engine` for this run only; otherwise the project's settings are used.
2. The root file is checked: it must be an existing `.tex` file inside the project that passes the file API's path policy, must not start with `-`, and must not contain whitespace other than spaces or any of `` % # $ & ~ ^ { } \ ` ! ``. The engine must be `pdflatex`, `xelatex` or `lualatex`.
3. One compile per project runs at a time. A second request for the same project waits for the first.
4. latexmk runs with the project folder as its working directory and writes every output (PDF, SyncTeX file, `.aux`, `.log`, `.bbl`, `.blg`, latexmk's database) to `<data_dir>/build/<id>/out/`. The project folder gets no build artifacts.
5. The TeX log, the BibTeX log and latexmk's own output are parsed into diagnostics (below).
6. The combined log is stored under `<data_dir>/state/<id>/logs/`, and the result is returned.

`GET /api/projects/{id}/pdf` serves `build/<id>/out/<stem>.pdf`, where `<stem>` is the root file's name without `.tex`. The compile response's `pdf_url` points there, with a `v` query parameter that changes whenever the PDF does (and a `root_file` parameter when the run used a different root file than the project's). The PDF is sent with `Cache-Control: no-store`, `Content-Disposition: inline` and `X-Content-Type-Options: nosniff`.

`GET /api/projects/{id}/logs` lists the stored logs, newest first, and `GET /api/projects/{id}/logs/{log_id}` returns one with its text. Only the newest 20 are kept.

## The command line

```
latexmk -norc <engine flag> -interaction=nonstopmode -file-line-error -synctex=1 -no-shell-escape -outdir=<absolute build folder> <root file>
```

| Part | Why |
|---|---|
| `-norc` | latexmk reads no `latexmkrc` file: not the system's, not the user's, not one in the project. A `latexmkrc` is Perl, so a project's own would run arbitrary code. |
| `-pdf` (pdflatex), `-pdfxe` (xelatex), `-pdflua -lualatex=lualatex --safer --nosocket %O %S` (lualatex) | Selects the engine. The LuaTeX command line is one argument; latexmk fills in `%O` (options) and `%S` (the source file). `--safer` replaces Lua's `os.execute`, `io.popen` and other risky functions with stubs that do nothing, and disables `io.open` (LuaTeX 1.17; other versions may limit it to reading, which still goes through kpathsea's checks). `--nosocket` disables Lua's network library. |
| `-interaction=nonstopmode` | TeX never waits for input. stdin is closed as well. |
| `-file-line-error` | Errors are printed as `file:line: message`, which the parser reads. |
| `-synctex=1` | Writes `<stem>.synctex.gz` for the PDF-to-source jumps that arrive in Phase 2. |
| `-no-shell-escape` | `\write18` cannot run commands. The environment says the same (`shell_escape=f`). |
| `-outdir=...` | Outputs go to the build folder, outside the project. |
| no `-halt-on-error` | TeX carries on after an error, so one compile reports more than one problem. |

The command runs without a shell (`subprocess.Popen` with an argument list). latexmk is looked up on the `PATH` of the compile's environment and started by its absolute path.

latexmk itself starts the engine and BibTeX through a shell (`sh` on Unix, `cmd.exe` on Windows), with the file name in double quotes. That is why the root file may not contain `` ` ``, `$`, `\`, `"`, `%` or `!`: inside double quotes those still mean something to a shell. Run directly on a root file named ``a`touch x`b.tex``, latexmk runs `touch x` (verified on 2026-11-25).

## The environment

latexmk gets only these variables from the server's environment, when they are set: `PATH`, `PATHEXT`, `SYSTEMROOT`, `SystemRoot`, `WINDIR`, `COMSPEC`, `TEMP`, `TMP`, `TMPDIR`, `HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TEXMFHOME`, `TEXMFVAR`, `TEXMFCONFIG` and any `MIKTEX_*`. Everything else is dropped, including provider API keys, `PRISMTEX_ACCESS_TOKEN`, `TEXINPUTS`, `BIBINPUTS`, `LD_PRELOAD` and `PERL5OPT`. LuaTeX's `os.getenv` can read any variable it is given; without this filter a document could print the server's keys into its PDF.

Then these are set:

| Variable | Value | Why |
|---|---|---|
| `openin_any` | `p` | Paranoid reading: TeX and BibTeX refuse to open files given as absolute paths, paths containing `..`, or names starting with a dot. Files found in the TeX installation's own trees are still read. |
| `openout_any` | `p` | Paranoid writing, with the same rules. |
| `TEXMFOUTPUT` | the build folder | The one absolute folder TeX may write to, and read its own outputs back from. |
| `shell_escape` | `f` | Shell escape off, matching `-no-shell-escape`. |
| `max_print_line` | `10000` | TeX does not wrap log lines at 79 characters, so messages and file names stay on one line for the parser. |
| `error_line`, `half_error_line` | `254`, `238` | Longer error context lines (the defaults are 79 and 50), so more of the source line around an error is shown. |

## The process

- stdin is closed, and stdout and stderr are read together.
- On a timeout (`PRISMTEX_COMPILE_TIMEOUT_S`, 120 seconds by default) the whole process tree is killed, not only latexmk. On Linux and macOS latexmk starts in a new session and its process group gets `SIGKILL`; on Windows it starts in a new process group and `taskkill /T /F` ends the tree. A PDF that TeX was writing when it was killed is deleted, because it would be truncated.
- Output beyond 4 MiB is read and discarded, so a chatty run cannot fill memory or block on a full pipe. The stored log (latexmk's output followed by the TeX log) is cut to at most 2 MiB, keeping its start and its end.
- Output is decoded as UTF-8; bytes that are not valid UTF-8 become U+FFFD.

## Status

| Status | When |
|---|---|
| `ok` | latexmk exited with 0, the PDF belongs to this run, and no diagnostic is an error. |
| `failed` | Anything else that ran to the end. |
| `timeout` | The compile was stopped. The only diagnostic says after how many seconds. |
| `unavailable` | latexmk is not on `PATH`. The only diagnostic (kind `tex_missing`) says to install TeX Live or MiKTeX. |

latexmk skips work when nothing has changed since the last run. A file in the build folder therefore counts as belonging to this run if the run wrote it, or if latexmk reports "All targets ... are up-to-date" (it then vouches that the outputs match the sources). The PDF counts only if latexmk also exited with 0: after a failed run, an unchanged re-run exits non-zero and still says "up-to-date", while the PDF on disk is from before the failure. The BibTeX log is read if this run wrote it, or if the TeX log shows the document still reads the `.bbl` file; a `.blg` left from a bibliography that was since removed is ignored.

latexmk 4.83 has one quirk this works around: after the bibliography is removed from a document whose BibTeX run had failed, the next run exits with 12 ("gave an error in previous invocation of latexmk") although TeX succeeded. When TeX wrote a fresh PDF and a log without errors this run and that is latexmk's only complaint, latexmk is run once more within the remaining time, and that run exits with 0.

A `failed` compile always carries at least one error. If the logs name none, a diagnostic of kind `compile_failed` says what is known: that latexmk exited before TeX wrote a log, that no PDF was produced (and whether TeX reported "No pages of output."), or latexmk's exit code.

## Diagnostics

Each diagnostic has a `file` (a path relative to the project, or `null`), a `line` (or `null`), a `severity` (`error`, `warning` or `info`), a `kind` and a `message`.

| Kind | Severity | Source |
|---|---|---|
| `undefined_control_sequence` | error | `Undefined control sequence.` |
| `missing_file` | error | ``LaTeX Error: File `x' not found.``, a package's ``File `x' not found`` error (graphics), or TeX's ``I can't find file `x'`` |
| `latex_error` | error | other `LaTeX Error:` messages |
| `package_error` | error | `Package <name> Error:` or `Class <name> Error:` |
| `tex_error` | error | any other TeX error (`file:line: ...` or `! ...`), such as `Missing $ inserted.` |
| `fatal` | error | `Emergency stop.`, `==> Fatal error occurred, no output PDF file produced!`, `*** (job aborted, ...)` |
| `undefined_citation` | warning | ``Citation `k' on page N undefined on input line L``, in the wording of LaTeX, natbib and biblatex |
| `undefined_reference` | warning | ``Reference `k' on page N undefined on input line L`` |
| `multiply_defined_label` | warning | ``Label `x' multiply defined`` |
| `font_warning` | warning | `LaTeX Font Warning:` |
| `package_warning` | warning | other `Package <name> Warning:` or `Class <name> Warning:` |
| `latex_warning` | warning | other `LaTeX Warning:` |
| `bad_box` | info | `Overfull` or `Underfull` `\hbox` or `\vbox`, with the line from `at line(s) N` |
| `bibtex_error` | error | the `.blg`: `I couldn't open ... file`, `I found no ...`, and other lines with a `---` location; Biber's `ERROR` lines |
| `bibtex_warning` | warning | the `.blg`: `Warning--...`; Biber's `WARN` lines |
| `access_denied` | warning | latexmk's output: kpathsea's `Not reading from <path> (openin_any = p)` or `Not writing to <path> (openout_any = p)`, explaining a refused read or write |
| `compile_failed` | error | added by the compiler, see Status |
| `timeout` | error | added by the compiler |
| `tex_missing` | error | added by the compiler |

How the parser finds file and line:

- An error's file and line come from its `file:line:` prefix. A `! ...` error takes its line from the `l.<n>` context line TeX prints after it, and its file from the file stack. A "not found" error with no context of its own takes the line of the `Emergency stop` that follows it in the same file.
- An error's message is the text after `file:line: `, followed on a new line by the `l.<n> ...` context line when there is one. Package errors continued on `(<package>)` lines are joined into one message, and so are warnings.
- A warning's line comes from `on input line L`; its file from the file stack. The parser follows the `(` and `)` TeX prints as it opens and closes files. A `(` that is not followed by a file name pushes a placeholder, so the stack stays balanced. Text TeX echoes from the source (error context, help text, runaway arguments, box contents) is skipped, because it can hold unbalanced parentheses.
- TeX Live prints `./sections/a.tex`; this becomes `sections/a.tex`. Absolute paths inside the project folder are made relative, and bare relative names (as BibTeX and, reportedly, MiKTeX print them) are kept. Paths outside the project, and build files such as `.aux` and `.bbl`, map to `null`. A diagnostic without a file has no line either.
- Duplicates are dropped, and at most 200 diagnostics are kept, errors first, in log order.

The parser's tests run over real logs in `backend/tests/fixtures/texlogs/`, made by the compile command above from small documents written for each case.

## What was verified

On 2026-11-25, on Ubuntu 24.04 with TeX Live 2023 as packaged by Debian (pdfTeX 1.40.25, LuaHBTeX 1.17.0, latexmk 4.83, BibTeX 0.99d, Biber 2.19). `pytest -m tex` runs these checks against real TeX, and they fail rather than skip when latexmk is missing:

- The `blank`, `paper` and `resume` templates compile `ok` with pdflatex, with no undefined citations or references; the paper's three citations resolve through BibTeX with `-outdir`; a SyncTeX file is written. The blank template also compiles with lualatex.
- After compiles, including a failing one, the project folder is byte-identical to before.
- `\input` of an absolute path (`/etc/hostname` and a file in a temporary folder), `\input{../outside}`, `\immediate\write18{...}` (logged as `runsystem(...)...disabled`), `\openout` to an absolute path and to `../escaped.txt` are all refused; nothing outside is read or written.
- A `latexmkrc` and a `.latexmkrc` in the project are ignored. Without `-norc` the same file runs its commands (checked by hand).
- In lualatex, `io.open` (for reading or writing, inside the project or outside it), `os.execute` and `io.popen` are stubs that do nothing in LuaTeX 1.17, and `os.getenv` returns `nil` for a variable the server has but did not pass on, while `HOME` is still visible.
- An infinite loop (`\def\spin{\spin}\spin`) times out, and afterwards no process that was part of the compile is left.
- An undefined control sequence in `sections/x.tex` is reported at that file and line; an undefined `\cite` yields `undefined_citation` at its file and line, and BibTeX's warning for the key.
- A second compile with no changes is still `ok`; an older PDF is not reported for a document that now fails, on the first or the unchanged second run; BibTeX errors go away when the bibliography is removed.
- `GET /pdf` serves the compiled PDF.

Not verified yet:

- **MiKTeX.** None of the above has been run on MiKTeX. Its latexmk is the same script, but `openin_any`/`openout_any`, `TEXMFOUTPUT` and the wording of its log and refusal messages may differ. The revamp design asks for a MiKTeX spike; this is still open.
- **xelatex.** It is not installed on the machine used here, so `-pdfxe` has only been checked as a command line.
- **lualatex with fonts.** This machine lacks luaotfload (Debian's `texlive-luatex` package is not installed), so lualatex fell back to Computer Modern with font warnings. With `--safer`, `io.open` is a stub; whether luaotfload and fontspec still work under `--safer` was not tested.
- **Windows.** The `taskkill` path of the timeout kill is exercised by the runner tests in CI's Windows job, but has not been run here.

## Known limits

- **Biber reads files outside the project.** When a document uses biblatex with the Biber backend, latexmk runs `biber`, which opens the `.bib` files named in `\addbibresource` itself, without kpathsea's `openin_any` check. A document can therefore load a BibTeX-format file from anywhere the server can read, and its entries are typeset into the PDF (verified on 2026-11-25: an entry from an absolute path outside the project appeared in the `.bbl`). Biber also supports remote resources, which was not tested. BibTeX refuses such paths. Closing this needs either not running Biber or checking the data sources in the `.bcf` file before it runs; neither is done yet.
- kpathsea may run TeX Live's `mktextfm`, `mktexpk` or `mktexmf` scripts to build a missing font. That is not shell escape, but it is a program TeX starts.
- Because of `openin_any=p`, TeX cannot `\input` a project file whose name starts with a dot.
- TeX follows symbolic links inside the project folder. ZIP import refuses symlinks and the file API cannot create them, so only someone with access to the data folder can add one.
- A root file in a subfolder is still compiled from the project folder, so its `\input` paths are relative to the project, not to the root file.
- The build folder's path is passed through latexmk's shell command too. The data folder (`PRISMTEX_DATA_DIR`) should not contain `"`, `` ` `` or `$`.
- `GET /pdf` while a compile is writing the PDF may return a partial file. The UI fetches the PDF after the compile returns.
- A process that leaves its process group (which TeX cannot do with shell escape off) would escape the timeout kill on Linux and macOS.
