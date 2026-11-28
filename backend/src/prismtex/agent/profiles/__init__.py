"""Profiles: how one run turns a request into staged changes and a reply (agent design §4).

`choose_profile` picks the Profile of a Turn and `classify_intent` routes the
request (§4.1); `make_profile` builds a fresh Profile for each Turn, which keeps
its conversation across the Turn's Repair rounds. The prompts live in
`prompts/*.md` beside this package.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, ClassVar, Final, Protocol

from prismtex.agent.errors import TurnRejected

if TYPE_CHECKING:
    from prismtex.agent.providers import ModelSpec
    from prismtex.agent.turn import ProfileResult, RepairFeedback, TurnScope
    from prismtex.agent.types import EditorContext, Intent, ProfileName

__all__ = [
    "EDIT_VERBS",
    "QUESTION_WORDS",
    "Profile",
    "choose_profile",
    "classify_intent",
    "make_profile",
]


class Profile(Protocol):
    """One way of running a Turn; a new instance serves exactly one Turn."""

    name: ClassVar[ProfileName]

    async def run(self, scope: TurnScope, feedback: RepairFeedback | None) -> ProfileResult:
        """One run: the first with `feedback` None, then once per Repair round."""
        ...


def choose_profile(
    spec: ModelSpec, request: ProfileName | None, project: ProfileName | None
) -> ProfileName:
    """The request's Profile, else the Project setting, else the model's catalog default.

    The agentic Profile needs native tool calling: choosing it for a model
    without it raises `TurnRejected("invalid_request")` with `field` "profile"
    and `reason` "profile_unfit". Guided runs on any model.
    """
    chosen: ProfileName = request or project or spec.default_profile
    if chosen == "agentic" and not spec.native_tools:
        raise TurnRejected(
            "invalid_request",
            f"{spec.label} cannot call tools, so it cannot run the agentic profile. "
            "Choose the guided profile or another model.",
            field="profile",
            reason="profile_unfit",
        )
    return chosen


QUESTION_WORDS: Final = frozenset(
    {
        "what",
        "why",
        "how",
        "which",
        "where",
        "when",
        "who",
        "is",
        "are",
        "does",
        "do",
        "can",
        "could",
        "should",
        "would",
        "explain",
        "summarize",
        "summarise",
        "describe",
        "list",
    }
)
EDIT_VERBS: Final = frozenset(
    {
        "add",
        "append",
        "insert",
        "remove",
        "delete",
        "drop",
        "cut",
        "rewrite",
        "rephrase",
        "reword",
        "fix",
        "correct",
        "change",
        "replace",
        "rename",
        "make",
        "shorten",
        "condense",
        "tighten",
        "expand",
        "extend",
        "proofread",
        "polish",
        "improve",
        "simplify",
        "translate",
        "format",
        "convert",
        "move",
        "merge",
        "split",
        "cite",
        "update",
        "write",
    }
)


def _forms(verb: str) -> set[str]:
    """The verb with its common inflections: -s, -es, -ed, -d, -ing, and a doubled consonant."""
    forms = {verb, verb + "s", verb + "es", verb + "ed", verb + "ing"}
    if verb.endswith("e"):
        forms |= {verb + "d", verb[:-1] + "ing"}
    if verb.endswith("y"):
        forms |= {verb[:-1] + "ies", verb[:-1] + "ied"}
    if re.fullmatch(r".*[^aeiou][aeiou][bdgmnprt]", verb):
        forms |= {verb + verb[-1] + "ed", verb + verb[-1] + "ing"}
    return forms


_EDIT_FORMS: Final = frozenset(form for verb in EDIT_VERBS for form in _forms(verb))
# Irregular past forms that still ask for a change ("rewrote", "made", "wrote").
_EDIT_FORMS_IRREGULAR: Final = frozenset({"made", "rewrote", "rewritten", "wrote", "written"})
_WORD = re.compile(r"[a-z]+(?:'[a-z]+)?")


def classify_intent(message: str, editor: EditorContext | None) -> Intent:
    """The intent of a request, by a cheap word heuristic (agent design §4.1).

    "edit" when an edit verb appears (in any common inflection); otherwise
    "question" when the message ends with "?" or starts with a question word;
    otherwise "unknown". The editor context does not change the result: a
    selection says where, not what. A misclassification costs at most a
    follow-up Turn, and review-first makes an unwanted edit harmless.
    """
    words = _WORD.findall(message.casefold())
    if any(w in _EDIT_FORMS or w in _EDIT_FORMS_IRREGULAR for w in words):
        return "edit"
    ends_with_question = message.rstrip().rstrip("\"')]").endswith("?")
    if ends_with_question or (words and words[0].split("'")[0] in QUESTION_WORDS):
        return "question"
    return "unknown"


def make_profile(name: ProfileName) -> Profile:
    """A new Profile for one Turn. The guided Profile is not available yet."""
    if name == "agentic":
        from prismtex.agent.profiles.agentic import AgenticProfile

        return AgenticProfile()
    if name == "guided":
        raise NotImplementedError("the guided profile is not available yet")
    raise ValueError(f"unknown profile: {name!r}")
