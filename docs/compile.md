# Compiling

PrismTeX compiles a project with latexmk. The server builds the command line and the environment itself, so a project cannot choose flags, and TeX cannot *write* outside the build folder.

Reading is different, and this page is honest about it. The argv and environment hardening (`openin_any=p`, a scrubbed environment, `-no-shell-escape`, `-norc`) is **not** a security boundary against untrusted LaTeX: pdfTeX's `\pdfobj ... file` reads any file the server account can read and copies it into the PDF, bypassing `openin_any`; LuaLaTeX's Lua layer reads arbitrary files even under `--safer`; and Biber executes code from a crafted control file. Phase 0 raises the bar with cheap, honest mitigations — a post-run `.fls` audit for pdfTeX/XeTeX, LuaLaTeX disabled, and a server-owned Biber guard — but running genuinely untrusted projects still needs an OS-level sandbox or container (a tracked follow-up; see Security). This page says how a compile works, what each setting is for, what each mitigation does and does not guarantee, what was verified and on which TeX distribution, and what is not covered.

The code is in `backend/src/prismtex/compile/`.

## What happens on a compile

1. `POST /api/projects/{id}/compile` calls `Compiler.compile`. The body may name a `root_file` and an `engine` for this run only; otherwise the project's settings are used.
2. The root file is checked: it must be an existing `.tex` file inside the project that passes the file API's path policy, must not start with `-`, and must not contain whitespace other than spaces or any of `` % # $ & ~ ^ { } \ ` ! ``. The engine must be `pdflatex`, `xelatex` or `lualatex`; `lualatex` is stored but refused at compile time (see "LuaLaTeX is disabled").
3. The project root is checked for a file that Windows could run as a tool (an executable extension, or an extensionless file named after a TeX program). If one is present, the compile is refused with `409 unsafe_project_file` before latexmk starts.
4. One compile per project runs at a time. A second request for the same project waits for the first. `Compiler.is_compiling(project_id)` reports (without blocking) whether a compile holds the lock; the project-delete route uses it to refuse deleting a project mid-compile.
5. latexmk runs with the project folder as its working directory and writes every output (PDF, SyncTeX file, `.aux`, `.log`, `.bbl`, `.blg`, `.fls`, latexmk's database) to `<data_dir>/build/<id>/out/`. The project folder gets no build artifacts. The source folder's subdirectories are mirrored (empty) into the build folder first, because latexmk does not create them itself when `-file-line-error` is on, so `\include{chapters/x}` would otherwise fail.
6. For `pdflatex` and `xelatex`, the `.fls` recorder files are audited: if the engine read a file outside the project, the build folder and the TeX trees, the PDF is deleted and the compile fails with an `access_denied` error (see "The .fls audit").
7. The TeX log, the BibTeX log and latexmk's own output are parsed into diagnostics (below).
8. The combined log is stored under `<data_dir>/state/<id>/logs/`, and the result is returned.

`GET /api/projects/{id}/pdf` serves `build/<id>/out/<stem>.pdf`, where `<stem>` is the root file's name without `.tex`. The compile response's `pdf_url` points there, with a `v` query parameter of `<pdf mtime_ns>-<log_id>` (and a `root_file` parameter when the run used a different root file than the project's). The `log_id` is unique per compile, so two compiles that happen to leave the PDF with the same mtime — possible on a coarse-mtime file system — still get different URLs and the viewer reloads. The PDF is sent with `Cache-Control: no-store`, `Content-Disposition: inline` and `X-Content-Type-Options: nosniff`.

`GET /api/projects/{id}/logs` lists the stored logs, newest first, and `GET /api/projects/{id}/logs/{log_id}` returns one with its text. Only the newest 20 are kept.

## The command line

```
latexmk -norc <engine flag> <-engine=ABS %O %S> -e "$bibtex=q{ABS %O %S}" -e "$biber=q{<python> -P -m prismtex.compile.biberguard %O %S}" -e "$makeindex=q{ABS %O -o %D %S}" -e "$kpsewhich=q{ABS %S}" -interaction=nonstopmode -file-line-error -synctex=1 -no-shell-escape -outdir=<absolute build folder> <root file>
```

| Part | Why |
|---|---|
| `-norc` | latexmk reads no `latexmkrc` file: not the system's, not the user's, not one in the project. A `latexmkrc` is Perl, so a project's own would run arbitrary code. |
| `-pdf` (pdflatex), `-pdfxe` (xelatex), `-pdflua` (lualatex) | Selects the engine. |
| `-pdflatex=`/`-xelatex=`/`-lualatex=<tool> ... %O %S` | Names the engine command by its **absolute** path (resolved with `shutil.which` on the compile's PATH). latexmk fills in `%O` (options) and `%S` (the source file). Without this, latexmk starts the engine by bare name from the project folder, and on Windows `CreateProcess`/`cmd.exe` search the current directory before PATH, so a project file called `pdflatex.exe` could run. The LuaTeX value also carries `--safer --nosocket`, but lualatex is refused before it runs (see "LuaLaTeX is disabled"). |
| `-e "$bibtex=..."`, `-e "$makeindex=..."`, `-e "$kpsewhich=..."` | Pin the helper tools to their absolute paths too (same Windows reason). `-e` runs with `-norc`. |
| `-e "$biber=q{<python> -P -m prismtex.compile.biberguard %O %S}"` | Routes Biber through a server-owned guard (see "The Biber guard"). `-P` keeps the project folder — Biber's working directory — off `sys.path`, so it cannot shadow the `prismtex` package. |
| `-interaction=nonstopmode` | TeX never waits for input. stdin is closed as well. |
| `-file-line-error` | Errors are printed as `file:line: message`, which the parser reads. (It also stops latexmk from auto-creating aux subfolders, which is why the source tree is mirrored into the build folder first.) |
| `-synctex=1` | Writes `<stem>.synctex.gz` for the PDF-to-source jumps that arrive in Phase 2. |
| `-no-shell-escape` | `\write18` cannot run commands. The environment says the same (`shell_escape=f`). |
| `-outdir=...` | Outputs go to the build folder, outside the project. |
| no `-halt-on-error` | TeX carries on after an error, so one compile reports more than one problem. |

The command runs without a shell (`subprocess.Popen` with an argument list). latexmk is looked up on the `PATH` of the compile's environment and started by its absolute path.

latexmk itself starts the engine and BibTeX through a shell (`sh` on Unix, `cmd.exe` on Windows), with the file name in double quotes. That is why the root file may not contain `` ` ``, `$`, `\`, `"`, `%` or `!`: inside double quotes those still mean something to a shell. Run directly on a root file named ``a`touch x`b.tex``, latexmk runs `touch x` (verified on 2026-11-25).

## The environment

latexmk gets only these variables from the server's environment, when they are set: `PATH`, `PATHEXT`, `SYSTEMROOT`, `SystemRoot`, `WINDIR`, `COMSPEC`, `TEMP`, `TMP`, `TMPDIR`, `HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TEXMFHOME`, `TEXMFVAR`, `TEXMFCONFIG` and any `MIKTEX_*`. Everything else is dropped, including provider API keys, `PRISMTEX_ACCESS_TOKEN`, `TEXINPUTS`, `BIBINPUTS`, `LD_PRELOAD` and `PERL5OPT`. This filter keeps keys out of the variables TeX is handed, but it is not the only way a Lua or pdfTeX document could reach them: LuaLaTeX's Lua layer can read `/proc/<server-pid>/environ` (which is why `prismtex serve` calls `prctl(PR_SET_DUMPABLE, 0)` on Linux so a same-uid reader cannot, and why LuaLaTeX is disabled anyway), and pdfTeX's `\pdfobj ... file{/proc/<pid>/environ}` would try the same (also closed by `PR_SET_DUMPABLE`, and caught by the `.fls` audit). A server running as root is not covered by `PR_SET_DUMPABLE`.

Then these are set:

| Variable | Value | Why |
|---|---|---|
| `openin_any` | `p` | Paranoid reading: TeX and BibTeX refuse to open files given as absolute paths, paths containing `..`, or names starting with a dot. Files found in the TeX installation's own trees are still read. Note the holes below (pdfTeX `\pdfobj`, LuaTeX Lua I/O, Biber) that do not go through this check. |
| `openout_any` | `p` | Paranoid writing, with the same rules. |
| `TEXMFOUTPUT` | the build folder | The one absolute folder TeX may write to, and read its own outputs back from. |
| `shell_escape` | `f` | Shell escape off, matching `-no-shell-escape`. |
| `MISSFONT_LOG` | `0` | kpathsea otherwise appends `missfont.log` to the current directory (the project folder) when it cannot build a font; this does not go through `openout_any`. `0` turns it off, so the project folder stays free of build artifacts (ADR 0004). |
| `NoDefaultCurrentDirectoryInExePath` | `1` | Windows: keeps the current directory out of the executable search path, so a project file cannot be run as a tool (belt-and-suspenders with the absolute tool paths and the `unsafe_project_file` refusal). |
| `PYTHONPATH` | the dir holding `prismtex` | So `python -P -m prismtex.compile.biberguard` resolves where prismtex is not installed. |
| `max_print_line` | `10000` | TeX does not wrap log lines at 79 characters, so messages and file names stay on one line for the parser. |
| `error_line`, `half_error_line` | `254`, `238` | Longer error context lines (the defaults are 79 and 50), so more of the source line around an error is shown. |

## The process

- stdin is closed, and stdout and stderr are read together.
- On a timeout (`PRISMTEX_COMPILE_TIMEOUT_S`, 120 seconds by default) the whole process tree is killed, not only latexmk. On Linux and macOS latexmk starts in a new session and its process group gets `SIGKILL`; on Windows it starts in a new process group and `taskkill /T /F` ends the tree. A PDF that TeX was writing when it was killed is deleted, because it would be truncated.
- Output beyond 4 MiB is read and discarded, so a chatty run cannot fill memory or block on a full pipe. The `.log` and `.blg` files on disk are read head-and-tail only (the first 512 KiB and the last 1.5 MiB), so a runaway `\message` loop that grows the log to gigabytes is read in bounded memory. On a timeout the log is not parsed at all (the result would be thrown away), so the request returns near the timeout rather than after minutes of parsing. The stored log (latexmk's output followed by the bounded TeX log) is cut to at most 2 MiB, keeping its start and its end.
- Output is decoded as UTF-8; bytes that are not valid UTF-8 become U+FFFD.

## Status

| Status | When |
|---|---|
| `ok` | latexmk exited with 0, the PDF belongs to this run, no diagnostic is an error, and the `.fls` audit found no outside read. |
| `failed` | Anything else that ran to the end, including a PDF discarded by the `.fls` audit. |
| `timeout` | The compile was stopped. The only diagnostic says after how many seconds. |
| `unavailable` | latexmk is not on `PATH` (diagnostic kind `tex_missing`), or the engine is `lualatex`, which is disabled this release (diagnostic kind `engine_disabled`). |

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
| `access_denied` | warning *or* error | **warning**, from latexmk's output: kpathsea's `Not reading from <path> (openin_any = p)` / `Not writing to <path> (openout_any = p)` — a read or write that was *refused*. TeX Live 2024+ appends `; extended check` or `; no extended check` (and TL2024.0 a double space); all wordings are recognised. **error**, from the compiler: the `.fls` audit found the engine *did* read a file outside the project, so the PDF was discarded. The program name is shown by base name only, never the server path. |
| `compile_failed` | error | added by the compiler, see Status |
| `timeout` | error | added by the compiler |
| `tex_missing` | error | added by the compiler, see Status |
| `engine_disabled` | error | added by the compiler: `lualatex` is disabled this release |

How the parser finds file and line:

- An error's file and line come from its `file:line:` prefix. A `! ...` error takes its line from the `l.<n>` context line TeX prints after it, and its file from the file stack. A "not found" error with no context of its own takes the line of the `Emergency stop` that follows it in the same file.
- An error's message is the text after `file:line: `, followed on a new line by the `l.<n> ...` context line when there is one. Package errors continued on `(<package>)` lines are joined into one message, and so are warnings.
- A warning's line comes from `on input line L`; its file from the file stack. The parser follows the `(` and `)` TeX prints as it opens and closes files. A `(` that is not followed by a file name pushes a placeholder, so the stack stays balanced. Text TeX echoes from the source (error context, help text, runaway arguments, box contents) is skipped, because it can hold unbalanced parentheses.
- TeX Live prints `./sections/a.tex`; this becomes `sections/a.tex`. Absolute paths inside the project folder are made relative, and bare relative names (as BibTeX and, reportedly, MiKTeX print them) are kept. Paths outside the project, and build files such as `.aux` and `.bbl`, map to `null`. A diagnostic without a file has no line either.
- Duplicates are dropped, and at most 200 diagnostics are kept, errors first, in log order.

The parser's tests run over real logs in `backend/tests/fixtures/texlogs/`, made by the compile command above from small documents written for each case.

## The `.fls` audit (pdflatex and xelatex)

pdfTeX's `\pdfobj ... file{<path>}` opens a file and copies its bytes into the PDF as an object stream. This does **not** go through kpathsea, so `openin_any=p` does not stop it: a document can read any file the server account can read (the project's `.env`, `/etc/passwd`, `~/.ssh/…`) and ship it inside the PDF to whoever opens it. The same is true of `\font` and `\pdfmapfile` with an absolute path.

latexmk runs the engine with `-recorder`, which lists every file the engine opened as an `INPUT` line in `<stem>.fls`. After the run, `prismtex.compile.flsaudit` resolves every `INPUT` line from every `*.fls` in the build folder and checks it against the allowed roots: the project folder, the build folder, and the TeX installation's own trees (every tree in `kpsewhich -var-value=TEXMF`, plus the named `TEXMF*` variables and `/etc/texmf`, `/var/lib/texmf`, `/usr/share/texmf`, `/usr/local/share/texmf` as a floor). If any read resolves elsewhere, the PDF is deleted and the compile returns `failed` with an `access_denied` **error** naming the offending path as the document wrote it (never the resolved server path).

This is a mitigation, not a sandbox. It audits **after** the run, so a read on an early latexmk pass that the final `.fls` no longer shows could be missed, and it cannot see a read a program makes without the recorder — which is exactly why LuaLaTeX (whose Lua I/O is unrecorded) is refused rather than audited. The real fix is an OS-level sandbox (see Security).

## LuaLaTeX is disabled

A compile with `engine=lualatex` returns `unavailable` with an `engine_disabled` diagnostic. `lualatex` is still a valid value for the project's `engine` setting (`PATCH` stores it), but it is refused at compile time, because under `--safer`:

- `io.lines`, `io.input`, `lfs.dir` and `kpse.find_file` are **not** stubbed (only `io.open`, `os.execute` and `io.popen` are), so the Lua layer still reads arbitrary files and directory listings, and `/proc/<pid>/environ` to reach the server's environment. None of this appears in the `.fls`, so the audit above cannot catch it.
- `luaotfload` aborts (`luaotfload can't run with option --safer`), so `fontspec` and OpenType fonts do not work anyway.

Dropping `--safer` is not an option: without it, LuaTeX's Lua can also *write* and *delete* files outside the project. So LuaLaTeX stays off until it can run under an OS sandbox. Use `pdflatex` or `xelatex`.

## The Biber guard

Biber is a separate Perl program latexmk invokes for `biblatex` with `backend=biber`. None of the TeX hardening reaches it: it opens the data sources in `\addbibresource` itself (no `openin_any`), and it turns a data source's `type`/`datatype` attribute from the `.bcf` control file into a Perl package name that it runs through `eval` — so a crafted `\addbibresource[datatype=...]` can execute code (both verified on 2026-11-25).

latexmk is therefore told to run Biber as `python -P -m prismtex.compile.biberguard`. The guard parses the `.bcf` first and refuses (non-zero exit, so latexmk reports the compile failed) if any `<bcf:datasource>` name is absolute, contains `..`, or is remote (`http`/`https`/`ftp`/`file`), or if any data-source attribute holds a character outside a strict allowlist (letters, digits, `. _ -`) — which is what stops the `eval`-based execution. Only then does it run the real `biber --noconf` (on POSIX it replaces itself with `execv`; on Windows, whose `exec` would end the guard at once with status 0 while biber still runs, it runs biber as a child and returns its exit status) (`--noconf` stops a project `biber.conf` from redirecting Biber's I/O; an empty config file crashes Biber, so `--noconf` is used instead). `-P` keeps the project folder off `sys.path` so it cannot shadow `prismtex`. If the Python interpreter's path cannot sit safely in latexmk's command (it contains a space or a shell character, as a Python under `C:\Program Files` does on Windows), the guard cannot be named, so `$biber` is set to latexmk's `NONE` keyword instead: Biber never runs and a document that needs it fails to compile, rather than Biber running unguarded. Install Python (or the virtualenv) at a path without spaces to use Biber there. BibTeX (`\bibliography` with natbib/plain) needs no guard: it honours `openin_any=p`.

## What was verified

On 2026-11-25, on Ubuntu 24.04 with TeX Live 2023 as packaged by Debian (pdfTeX 1.40.25, LuaHBTeX 1.17.0, latexmk 4.83, BibTeX 0.99d, Biber 2.19; xelatex not installed). `pytest -m tex` runs these checks against real TeX, and they fail rather than skip when latexmk is missing:

- The `blank`, `paper` and `resume` templates compile `ok` with pdflatex, with no undefined citations or references; the paper's three citations resolve through BibTeX with `-outdir`; a SyncTeX file is written.
- After compiles, including a failing one, the project folder is byte-identical to before. A missing font leaves no `missfont.log` in it.
- `\include{chapters/one}` from a root `main.tex`, and `\include{paper/ch1}` with the root file at `paper/main.tex`, both compile `ok` (the source tree is mirrored into the build folder) and leave the project folder byte-identical, over two runs.
- `\input` of an absolute path (`/etc/hostname` and a file in a temporary folder), `\input{../outside}`, `\immediate\write18{...}` (logged as `runsystem(...)...disabled`), `\openout` to an absolute path and to `../escaped.txt` are all refused; nothing outside is read or written. The refusal wording is recognised across TeX Live 2023 and 2024+ (`; (no )extended check`).
- `\pdfobj stream file{<absolute outside>}` and `{../outside}` are caught by the `.fls` audit: the status is not `ok`, an `access_denied` error is reported, the PDF is deleted, and the secret reaches neither the log nor any surviving output file. A `\pdfobj` of a file inside the project still compiles `ok`.
- `lualatex` is refused at compile time (`unavailable`, `engine_disabled`). A direct probe confirms the gap that justifies it: under `--safer`, `io.open` is a stub but `io.lines` is not, and it reads an outside file.
- biblatex with the Biber backend produces its bibliography through the guard; an absolute `\addbibresource` is refused and its entry never reaches the `.bbl` or PDF; a crafted `datatype` does not execute a command, including when the guard cannot be named and Biber is disabled instead.
- A `latexmkrc` and a `.latexmkrc` in the project are ignored. Without `-norc` the same file runs its commands (checked by hand).
- An infinite loop (`\def\spin{\spin}\spin`) times out, and afterwards no process that was part of the compile is left. A runaway `\message` loop times out near the timeout (not ~3× it), in bounded memory, with a bounded stored log.
- An undefined control sequence in `sections/x.tex` is reported at that file and line; an undefined `\cite` yields `undefined_citation` at its file and line, and BibTeX's warning for the key.
- A second compile with no changes is still `ok`; an older PDF is not reported for a document that now fails, on the first or the unchanged second run; BibTeX errors go away when the bibliography is removed.
- `GET /pdf` serves the compiled PDF.

Not verified yet:

- **MiKTeX.** None of the above has been run on MiKTeX. Its latexmk is the same script, but `openin_any`/`openout_any`, `TEXMFOUTPUT`, the `.fls` recorder, and the wording of its log and refusal messages may differ. The revamp design asks for a MiKTeX spike; this is still open.
- **xelatex.** It is not installed on the machine used here, so `-pdfxe` has only been checked as a command line. The `.fls` audit applies to it the same way, but XeTeX also reads system fonts (through fontconfig), which the audit's allowed roots do not list; a real xelatex run may need those added.
- **TeX Live 2024+/2026.** The refusal-wording change is handled (and has a fixture), but the suite has only been run end-to-end on TeX Live 2023. Whether a newer LuaTeX closes the Lua read holes is unverified; LuaLaTeX stays disabled regardless.
- **Windows.** The `taskkill` path of the timeout kill, and the `unsafe_project_file` refusal and absolute tool paths that matter there, are exercised in CI's Windows job (no TeX needed for the refusal) but have not been run here.

## Known limits

- **No OS sandbox.** The mitigations above (`.fls` audit, LuaLaTeX disabled, Biber guard, `PR_SET_DUMPABLE`) raise the bar but are not a sandbox. The `.fls` audit is after the fact; a per-pass guard would be stronger but is fragile across platforms. Running genuinely untrusted projects still warrants an OS-level sandbox or a throwaway container — see Security.
- kpathsea may run TeX Live's `mktextfm`, `mktexpk` or `mktexmf` scripts to build a missing font. That is not shell escape, but it is a program TeX starts.
- Because of `openin_any=p`, TeX cannot `\input` a project file whose name starts with a dot.
- TeX follows symbolic links inside the project folder. ZIP import refuses symlinks and the file API cannot create them, so only someone with access to the data folder can add one. The source-tree mirror into the build folder does not follow symlinked directories.
- A root file in a subfolder is still compiled from the project folder, so its `\input` paths are relative to the project, not to the root file. (Its `\include` subfolders are created in the build folder so the aux files land correctly.)
- The build folder's path is passed through latexmk's shell command too. The data folder (`PRISMTEX_DATA_DIR`) should not contain `"`, `` ` `` or `$`.
- `GET /pdf` while a compile is writing the PDF may return a partial file. The UI fetches the PDF after the compile returns.
- A process that leaves its process group (which TeX cannot do with shell escape off) would escape the timeout kill on Linux and macOS.

## Security

The compile hardening (argv flags, the scrubbed environment, `openin_any`/`openout_any`, `-no-shell-escape`, `-norc`) stops a project from choosing flags, running shell commands, writing outside the build folder, and reading the server's environment through the variables TeX is handed. It is **not** a boundary that makes reading the filesystem safe:

- **pdfTeX `\pdfobj ... file`** reads any readable file into the PDF, bypassing `openin_any`. Phase 0 mitigation: the post-run `.fls` audit deletes a PDF built from an outside read (pdflatex, xelatex). It is after the fact and recorder-dependent, not a sandbox.
- **LuaLaTeX's Lua layer** reads arbitrary files, directory listings and `/proc/<pid>/environ` even under `--safer`, none of it recorded. Phase 0 mitigation: LuaLaTeX is disabled at compile time.
- **Biber** executes code from a crafted `.bcf` and reads data sources outside the project. Phase 0 mitigation: the server-owned Biber guard vets the `.bcf` and runs `biber --noconf`.
- **`/proc/<server-pid>/environ`** (reachable from pdfTeX and LuaTeX) is closed on Linux by `prctl(PR_SET_DUMPABLE, 0)` at server start-up — for a same-uid reader only; a server running as **root** is not protected, so do not run it as root.
- **Windows tool hijack**: the engine and helper tools are named by absolute path, `NoDefaultCurrentDirectoryInExePath=1` is set, and a project with an executable-extension file (or an extensionless file named after a TeX tool) at its root is refused with `409 unsafe_project_file`.

The real fix is to run TeX inside an OS-level read sandbox (on Linux, bubblewrap or Landlock with read-only binds for the TeX trees and the project, read-write only for the build folder, and a PID namespace to hide other processes' `/proc`). That is a new system dependency, so it is a **tracked follow-up for a later phase**, not Phase 0. Until it lands, run PrismTeX so that compiling an untrusted project cannot harm you: under a dedicated low-privilege account with no secrets it can read, or in a throwaway container. SECURITY.md carries the same posture.
