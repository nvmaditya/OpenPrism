"""Tool-argument parsing, schema checks and text tool-call recovery (agent design §4.6)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from prismtex.agent.providers.toolcalls import (
    ParsedArgs,
    parse_tool_arguments,
    recover_text_tool_calls,
)

ANY_OBJECT: dict[str, Any] = {"type": "object"}
EDIT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path", "start_line", "where"],
    "properties": {
        "path": {"type": "string"},
        "start_line": {"type": ["integer", "null"]},
        "where": {"type": "string", "enum": ["start", "end"]},
    },
}
NO_ARGS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {},
    "required": [],
}
KNOWN = frozenset({"view", "str_replace"})


def test_lenient_retry_doubles_invalid_escapes_only() -> None:
    # Raw JSON with a single-escaped \section: invalid JSON, intact after the retry.
    assert parse_tool_arguments('{"new_str": "\\section{A}"}', ANY_OBJECT) == ParsedArgs(
        {"new_str": "\\section{A}"}, True, None
    )
    # Valid escapes keep their JSON meaning; only \s and the short \u are doubled.
    mixed = parse_tool_arguments('{"a": "\\section \\\\emph \\n \\uZZ"}', ANY_OBJECT)
    assert mixed == ParsedArgs({"a": "\\section \\emph \n \\uZZ"}, True, None)
    assert parse_tool_arguments('{"a": "\\u00e9"}', ANY_OBJECT) == ParsedArgs(
        {"a": "é"}, False, None
    )
    # Strict JSON reads \t as a TAB; the escape guard, not the parser, catches \textbf.
    assert parse_tool_arguments('{"a": "\\textbf"}', ANY_OBJECT).args == {"a": "\textbf"}

    broken = parse_tool_arguments('{"a": ', ANY_OBJECT)
    assert broken.args is None and broken.lenient is False
    assert broken.error == "invalid_args at 6: Expecting value"
    # Still invalid after the retry: the position is in the text the model sent.
    trailing = parse_tool_arguments('{"a": "\\section", }', ANY_OBJECT)
    assert trailing.args is None and trailing.error is not None
    assert trailing.error.startswith("invalid_args at 18: ")  # the message varies by Python


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        (
            '{"path": "a.tex", "start_line": 3, "where": "end", "engine": "x"}',
            "invalid_args: unexpected field 'engine'",
        ),
        ('{"path": "a.tex", "start_line": 3}', "invalid_args: missing required field 'where'"),
        (
            '{"path": "a.tex", "start_line": "3", "where": "end"}',
            "invalid_args: field 'start_line' must be integer or null, not string",
        ),
        (
            '{"path": "a.tex", "start_line": true, "where": "end"}',
            "invalid_args: field 'start_line' must be integer or null, not boolean",
        ),
        (
            '{"path": 7, "start_line": 1, "where": "end"}',
            "invalid_args: field 'path' must be string, not integer",
        ),
        (
            '{"path": "a.tex", "start_line": 1, "where": "middle"}',
            'invalid_args: field \'where\' must be one of "start", "end"',
        ),
        ('["a.tex", 1, "end"]', "invalid_args: the arguments must be a JSON object"),
    ],
)
def test_schema_validation_rejects_extra_missing_and_wrongly_typed_fields(
    raw: str, error: str
) -> None:
    assert parse_tool_arguments(raw, EDIT) == ParsedArgs(None, False, error)


def test_schema_validation_accepts_nulls_for_nullable_fields_and_no_args_for_check() -> None:
    ok = parse_tool_arguments('{"path": "a.tex", "start_line": null, "where": "end"}', EDIT)
    assert ok == ParsedArgs({"path": "a.tex", "start_line": None, "where": "end"}, False, None)
    assert parse_tool_arguments("{}", NO_ARGS) == ParsedArgs({}, False, None)
    assert parse_tool_arguments('{"engine": "xelatex"}', NO_ARGS).error == (
        "invalid_args: unexpected field 'engine'"
    )
    nested = {
        "type": "object",
        "properties": {
            "keys": {"type": "array", "items": {"type": "string"}},
            "range": {"anyOf": [{"type": "null"}, {"type": "object", "required": ["start"]}]},
        },
    }
    assert parse_tool_arguments('{"keys": ["a", 2]}', nested).error == (
        "invalid_args: field 'keys.1' must be string, not integer"
    )
    assert parse_tool_arguments('{"range": {"end": 3}}', nested).error == (
        "invalid_args: field 'range' does not match any allowed form"
    )
    assert parse_tool_arguments('{"range": null}', nested).error is None


def test_text_tool_call_recovery_finds_bare_and_fenced_json_for_known_tools_only() -> None:
    text = (
        "I will look first. "
        '{"name": "view", "arguments": {"path": "main.tex", "start_line": null}}\n'
        "Then edit:\n```json\n"
        '{"name": "str_replace", "parameters": {"path": "main.tex", "old_str": "Hi", '
        '"new_str": "\\section{Hi}"}}\n'
        "```\n"
        '{"name": "rm_rf", "arguments": {"path": "/"}} and \\textbf{bold} {braces}\n'
        '{"name": "view", "arguments": "{\\"path\\": \\"refs.bib\\"}"}'
    )
    calls, reason = recover_text_tool_calls(text, KNOWN)
    assert reason is None
    assert [(c.id, c.name, c.recovered, c.parse_error) for c in calls] == [
        ("rec_1", "view", True, None),
        ("rec_2", "str_replace", True, None),
        ("rec_3", "view", True, None),
    ]
    assert calls[0].args == {"path": "main.tex", "start_line": None}
    # The single-escaped \section survives, and raw_args re-reads to the same arguments.
    assert calls[1].args == {"path": "main.tex", "old_str": "Hi", "new_str": "\\section{Hi}"}
    assert json.loads(calls[1].raw_args) == calls[1].args
    # Arguments written as a JSON string are kept exactly as written.
    assert calls[2].raw_args == '{"path": "refs.bib"}' and calls[2].args == {"path": "refs.bib"}
    assert recover_text_tool_calls("Use the view tool on main.tex {here}.", KNOWN) == ([], None)


def test_text_tool_call_recovery_reports_why_a_known_call_could_not_be_read() -> None:
    calls, reason = recover_text_tool_calls(
        'Calling {"name": "str_replace", "arguments": {"path": "main.tex" "old_str": "a"}}', KNOWN
    )
    assert calls == []
    assert reason == (
        "the str_replace call is not valid JSON (Expecting ',' delimiter at character 65)"
    )
    _, missing = recover_text_tool_calls('{"name": "view", "args": {"path": "a.tex"}}', KNOWN)
    assert missing == 'the view call needs an "arguments" object'
    # An unknown tool is ignored, readable or not.
    assert recover_text_tool_calls('{"name": "rm_rf", "arguments": {', KNOWN) == ([], None)
