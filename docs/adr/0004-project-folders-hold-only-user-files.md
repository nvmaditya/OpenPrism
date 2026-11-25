---
status: proposed
---

# Project folders hold only the user's files; history is a git shadow repository

A project folder under `<data_dir>/projects/<id>/` contains only what the user put there. Everything else lives beside it:

- settings, threads, turn traces and pending proposals in `<data_dir>/state/<id>/`;
- compile output and scratch compiles in `<data_dir>/build/<id>/`;
- history in `<data_dir>/history/<id>.git`, a git repository driven by the `git` CLI with the project folder as its work tree.

We made this choice for three reasons:

- **Clean exports.** Exports and imports stay plain folders with no app files.
- **No write path to app data.** The agent and the file API cannot reach app data at all. Before this, `.prism/meta.json` was writable through both.
- **Better history at no cost.** Git gives diff and restore for free, and survives crashes, which the old in-memory snapshot did not.

Existing projects with a `.prism/` folder are migrated once at startup. A user who wants their own git repository inside the project folder can have one: the shadow repository uses a separate `--git-dir`, so it never touches theirs.
