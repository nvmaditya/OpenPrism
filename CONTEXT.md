# PrismTeX

A self-hosted LaTeX workspace with an AI writing agent whose changes are reviewable, compile-checked and reversible. (Called OpenPrism until 2026-11-25.)

## Language

### Projects and files

**Project**:
A folder of LaTeX sources and assets that compiles into one document.
_Avoid_: workspace, repo, paper

**Root file**:
The `.tex` file a compile starts from, usually `main.tex`.
_Avoid_: main file, entry file

**Source file**:
A text file the compile reads, such as `.tex`, `.bib`, `.sty` or `.cls`.
_Avoid_: document, code file

**Asset**:
A binary file a project uses, such as an image or a PDF figure.
_Avoid_: upload, attachment

**Project state**:
What the app keeps about a project outside the project folder: settings, threads, history and compile output.
_Avoid_: metadata folder, `.prism`

### Compiling

**Compile**:
One run of the TeX toolchain over a project's root file, producing a PDF and a log.
_Avoid_: build, render

**Diagnostic**:
One error or warning from a compile, located at a source file and line.
_Avoid_: log line, issue

**Baseline**:
The diagnostics a project had before an agent turn started, used to judge whether the turn made things worse.
_Avoid_: before-state, reference compile

**Outline**:
The structure of a project derived from its sources: headings with file, line range and label, the `\input` graph, labels and citations, and the files the Root file never includes.
_Avoid_: table of contents, structure

### The agent

**Provider**:
A service or runtime that serves models, such as Ollama, Anthropic or an OpenAI-compatible endpoint.
_Avoid_: backend, LLM

**Profile**:
How the agent works with a given model. In the *agentic* profile the model decides which tools to call; in the *guided* profile the host picks the region and the model rewrites it as plain text.
_Avoid_: mode, strategy

**Thread**:
A saved conversation between the user and the agent about one project.
_Avoid_: chat, session

**Turn**:
One user request in a thread and everything the agent does to answer it.
_Avoid_: run, job

**Step**:
One model call inside a turn, together with the tool calls it asks for.
_Avoid_: iteration

**Region**:
The span of a source file that a guided turn is allowed to change, such as the user's selection or one section.
_Avoid_: range, block

**Proposal**:
The file changes a turn suggests, held apart from the project until the user accepts them.
_Avoid_: patch, pending changes, diff

**Hunk**:
One contiguous change inside a proposal that the user can accept or reject on its own.
_Avoid_: chunk

**Repair round**:
An extra step in which the agent sees the new diagnostics its proposal caused and tries to fix them.
_Avoid_: retry, fix loop

**Trace**:
The record of a turn kept for debugging: prompt sizes, tool calls, timings and usage.
_Avoid_: log

**Usage**:
The tokens a turn consumed and, for paid providers, what they cost.
_Avoid_: spend, billing

### History and trust

**Checkpoint**:
A saved version of a project's source files, taken when the user saves or accepts a proposal, that can be restored later.
_Avoid_: snapshot, backup, version

**History**:
The ordered checkpoints of one project.
_Avoid_: versions, timeline

**Citation check**:
Looking up a bibliography entry in scholarly indexes (Crossref, arXiv, OpenAlex, Semantic Scholar) to confirm the work exists and matches the entry.
_Avoid_: validation, verification

**Provenance record**:
The account of which AI changes were accepted into a project, from which model and when. It is kept in the trailers of each accept checkpoint in the project's History; any index of it can be rebuilt from there.
_Avoid_: audit log, AI log

### Evaluating the agent

**Eval task**:
A fixture project, a request and the checks that decide whether the agent handled it correctly.
_Avoid_: test case, benchmark item

**Trial**:
One run of an eval task against one model.
_Avoid_: attempt, sample
