---
name: skill-installer
description: Install skills from the curated catalog or a package path into PRISM_SKILL_HOME. Use when adding a skill for the agent to load.
---

# skill-installer

The `skill-installer` skill installs skills into the configured skill home (`PRISM_SKILL_HOME`). It can list skills from the curated catalog and install a selected catalog skill.

It can also install from a local package directory or (when credentials allow) a GitHub repository path.

## Workflow

1. List catalog skills.
2. Install by catalog name into skill-home.
3. Confirm the skill appears in `list_skills` and `load_skill` returns its body.
