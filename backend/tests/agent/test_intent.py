"""Choosing a Profile and routing intent (agent design §4.1)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from prismtex.agent.errors import TurnRejected
from prismtex.agent.profiles import Profile, choose_profile, classify_intent, make_profile
from prismtex.agent.profiles.agentic import AgenticProfile
from prismtex.agent.providers import catalog
from prismtex.agent.providers.fake import anthropic_like, fake_spec
from prismtex.agent.types import EditorContext, ProfileName, Selection

QWEN = catalog.spec("ollama:qwen3:4b-instruct-2507-q4_K_M")

INTENTS = [
    ("What does Section 2 claim?", "question"),
    ("why is the proof in the appendix", "question"),
    ("Explain the method", "question"),
    ("Summarize the introduction.", "question"),
    ("List the sections", "question"),
    ("Is the theorem stated correctly", "question"),
    ("What's the main contribution", "question"),
    ("The bound in Lemma 3 holds?", "question"),
    ("Fix the typo in the introduction.", "edit"),
    ("Can you shorten the abstract?", "edit"),
    ("Could you add a table of results?", "edit"),
    ("make this more concise", "edit"),
    ("Proofread the document", "edit"),
    ("Please cite Kipf and Welling here", "edit"),
    ("Why does it fail? Fix it.", "edit"),
    ("The paragraph needs tightening", "edit"),
    ("It should be rewritten for clarity", "edit"),
    ("Simplified wording, please", "edit"),
    ("Thanks!", "unknown"),
    ("The second paragraph", "unknown"),
    ("more formal", "unknown"),
]


@pytest.mark.parametrize(("message", "intent"), INTENTS)
def test_classify_intent_table(message: str, intent: str) -> None:
    assert classify_intent(message, None) == intent


def test_classify_intent_table_and_choose_profile_order_with_profile_unfit() -> None:
    for message, intent in INTENTS:
        assert classify_intent(message, None) == intent, message
    editor = EditorContext("main.tex", "v", selection=Selection(3, 1, 4, 1))
    assert classify_intent("more formal", editor) == "unknown"

    assert choose_profile(QWEN, None, None) == "guided"  # the catalog default
    assert choose_profile(anthropic_like, None, None) == "agentic"
    assert choose_profile(QWEN, None, "agentic") == "agentic"  # the Project setting
    assert choose_profile(QWEN, "guided", "agentic") == "guided"  # the request wins
    assert choose_profile(anthropic_like, "guided", None) == "guided"

    no_tools = replace(fake_spec, native_tools=False, default_profile="guided")
    assert choose_profile(no_tools, None, None) == "guided"
    forced: tuple[tuple[ProfileName | None, ProfileName | None], ...] = (
        ("agentic", None),
        (None, "agentic"),
    )
    for request, project in forced:
        with pytest.raises(TurnRejected) as caught:
            choose_profile(no_tools, request, project)
        assert caught.value.code == "invalid_request"
        assert caught.value.detail == {"field": "profile", "reason": "profile_unfit"}


def test_make_profile_gives_a_new_agentic_profile_per_turn_and_no_guided_one_yet() -> None:
    first, second = make_profile("agentic"), make_profile("agentic")
    assert isinstance(first, AgenticProfile) and first is not second
    assert first.name == "agentic"
    profile: Profile = first
    assert profile.name == "agentic"
    with pytest.raises(NotImplementedError):
        make_profile("guided")
