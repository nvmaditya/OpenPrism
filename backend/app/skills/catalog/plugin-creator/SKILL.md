---
name: plugin-creator
description: Scaffold Codex-style plugins with .codex-plugin/plugin.json and optional folders. Use when creating or updating a local plugin package.
---

# plugin-creator

The `plugin-creator` skill creates and scaffolds Codex plugins. It creates the required `.codex-plugin/plugin.json` manifest and can add optional plugin folders and files.

It also supports personal-marketplace-style entries and updating an existing local plugin layout.

## Workflow

1. Choose a plugin name (lowercase, hyphens).
2. Scaffold `.codex-plugin/plugin.json` under the target directory (project or plugins root).
3. Optionally add README and stub entry files.
4. Never write outside the allowed project or plugins root.
