"""Every variable `Settings` reads is documented in `.env.example` and `docs/configuration.md`."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import AliasChoices
from pydantic.fields import FieldInfo

from prismtex.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = ROOT / ".env.example"
CONFIGURATION_DOC = ROOT / "docs" / "configuration.md"
PREFIX = "PRISMTEX_"
# Read by the UI or start.sh, not by the server.
NOT_BACKEND_SETTINGS = frozenset({"PRISMTEX_API_URL", "UI_PORT"})
# Read by the test suite and the eval runner only.
TEST_ONLY = frozenset({"PRISMTEX_REQUIRE_TEX", "PRISMTEX_EVAL_SUBSET", "PRISMTEX_UPDATE_SNAPSHOTS"})


def variable_names(name: str, field: FieldInfo) -> set[str]:
    """The environment variables a field reads, as pydantic-settings resolves them.

    A field with a `validation_alias` reads its alias choices; it also reads
    `PRISMTEX_<FIELD>` when there is no alias or `populate_by_name` is on.
    """
    alias = field.validation_alias
    names: set[str] = set()
    if isinstance(alias, AliasChoices):
        names.update(choice.upper() for choice in alias.choices if isinstance(choice, str))
    elif isinstance(alias, str):
        names.add(alias.upper())
    if alias is None or Settings.model_config.get("populate_by_name"):
        names.add(f"{PREFIX}{name.upper()}")
    return names


def expected_names() -> set[str]:
    names: set[str] = set()
    for name, field in Settings.model_fields.items():
        names |= variable_names(name, field)
    return names


def test_settings_variable_names_include_the_anthropic_key_aliases() -> None:
    expected = expected_names()
    assert {"ANTHROPIC_API_KEY", "CLAUDE_API_KEY", "PRISMTEX_ANTHROPIC_API_KEY"} <= expected
    assert {"PRISMTEX_OPENAI_API_KEY", "PRISMTEX_DEFAULT_MODEL", "PRISMTEX_DATA_DIR"} <= expected
    assert not expected & {"OPENAI_API_KEY", "OPENAI_BASE_URL", "GROQ_API_KEY"}


def test_env_example_lists_every_setting() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", text, flags=re.MULTILINE))
    expected = expected_names()
    assert expected - documented == set(), "missing from .env.example"
    assert documented - expected - NOT_BACKEND_SETTINGS == set(), "unknown in .env.example"
    # Every entry is commented out and carries no secret.
    assert not re.findall(r"^[A-Z][A-Z0-9_]*=", text, flags=re.MULTILINE)
    assert not re.findall(r"^#\s*[A-Z0-9_]*(?:API_KEY|TOKEN)=\S", text, flags=re.MULTILINE)


def test_configuration_doc_lists_every_setting() -> None:
    text = CONFIGURATION_DOC.read_text(encoding="utf-8")
    expected = expected_names()
    named = set(re.findall(r"`([A-Z][A-Z0-9_]*[A-Z0-9])`", text))  # not the bare prefix
    assert expected - named == set(), "missing from docs/configuration.md"
    prefixed = {name for name in named if name.startswith(PREFIX)}
    unknown = prefixed - expected - NOT_BACKEND_SETTINGS - TEST_ONLY
    assert unknown == set(), "unknown in docs/configuration.md"
