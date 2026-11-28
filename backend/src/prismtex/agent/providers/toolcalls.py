"""Tool-call arguments: parsing, schema checks and text recovery (agent design §4.6).

`parse_tool_arguments` reads strict JSON first. On failure it retries once with every
backslash that is not a valid JSON escape doubled (`\\s`, `\\c`, `\\e`, `\\l`, `\\p`,
`\\v`, `\\u` without four hex digits). No valid JSON contains those sequences, so the
retry is lossless and a single-escaped `\\section` arrives intact. The arguments are
then checked against the subset of JSON Schema the tools use.

`recover_text_tool_calls` finds tool calls a model wrote as JSON text instead of in the
tool-calling format.
"""

from __future__ import annotations

import json
import re
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from prismtex.agent.providers import ToolCallCompleted

__all__ = ["ParsedArgs", "parse_tool_arguments", "recover_text_tool_calls"]


@dataclass(frozen=True, slots=True)
class ParsedArgs:
    """Parsed and validated arguments, or the reason they were refused.

    `lenient` is True when the arguments only parsed after the escape-doubling retry.
    `error` starts with "invalid_args"; `args` is None whenever `error` is set.
    """

    args: Mapping[str, Any] | None
    lenient: bool
    error: str | None


def parse_tool_arguments(raw: str, schema: Mapping[str, Any]) -> ParsedArgs:
    """Parse `raw` as a JSON object and check it against `schema`."""
    lenient = False
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as strict:
        doubled = _Doubled.of(raw)
        if not doubled.at:
            return ParsedArgs(None, False, _invalid_at(strict.pos, strict.msg))
        try:
            value = json.loads(doubled.text)
        except json.JSONDecodeError as error:
            return ParsedArgs(None, False, _invalid_at(doubled.to_original(error.pos), error.msg))
        lenient = True
    if not isinstance(value, dict):
        return ParsedArgs(None, lenient, "invalid_args: the arguments must be a JSON object")
    problem = _check(value, schema, ())
    if problem is not None:
        return ParsedArgs(None, lenient, f"invalid_args: {problem}")
    return ParsedArgs(value, lenient, None)


def recover_text_tool_calls(
    text: str, known: frozenset[str]
) -> tuple[list[ToolCallCompleted], str | None]:
    """Tool calls written as JSON text, bare or in a fenced block, for `known` tools only.

    A call is an object `{"name": <known tool>, "arguments" | "parameters": {...}}`;
    arguments given as a JSON string are kept as written. Recovered calls get the ids
    `rec_1`, `rec_2`, ... and `recovered=True`. The second item is the reason a call that
    names a known tool could not be read, or None.
    """
    calls: list[ToolCallCompleted] = []
    spans: list[tuple[int, int]] = []
    doubled: _Doubled | None = None
    position = 0
    while (match := _OBJECT_START.search(text, position)) is not None:
        start = match.start()
        if doubled is None:
            doubled = _Doubled.of(text)
        decoded = _decode_at(text, start, doubled)
        if decoded is not None:
            call = _as_call(decoded[0], known, len(calls) + 1)
            if call is not None:
                calls.append(call)
                spans.append((start, decoded[1]))
                position = decoded[1]
                continue
        position = start + 1
    reason = None
    for mention in _NAME.finditer(text):
        name = mention.group(1)
        if name in known and not any(a <= mention.start() < b for a, b in spans):
            reason = _unreadable(text, mention.start(), name, doubled or _Doubled.of(text))
            break
    return calls, reason


# -- lenient escapes -------------------------------------------------------------

# A backslash, then a valid JSON escape if there is one.
_ESCAPE = re.compile(r'\\(?:u[0-9a-fA-F]{4}|["\\/bfnrt])?')


@dataclass(frozen=True, slots=True)
class _Doubled:
    """`text` with every invalid escape's backslash doubled, and how positions moved."""

    text: str
    at: tuple[int, ...]  # original offsets of the backslashes that were doubled
    inserted: tuple[int, ...]  # offsets of the inserted backslashes in `text`

    @classmethod
    def of(cls, raw: str) -> _Doubled:
        parts: list[str] = []
        at: list[int] = []
        last = 0
        for match in _ESCAPE.finditer(raw):
            if match.end() - match.start() == 1:
                parts.append(raw[last : match.end()])
                parts.append("\\")
                at.append(match.start())
                last = match.end()
        parts.append(raw[last:])
        inserted = tuple(offset + index + 1 for index, offset in enumerate(at))
        return cls("".join(parts), tuple(at), inserted)

    def to_doubled(self, offset: int) -> int:
        return offset + bisect_left(self.at, offset)

    def to_original(self, offset: int) -> int:
        return offset - bisect_left(self.inserted, offset)


def _invalid_at(position: int, message: str) -> str:
    return f"invalid_args at {position}: {message}"


# -- schema subset ---------------------------------------------------------------


def _check(value: Any, schema: Mapping[str, Any], path: tuple[str, ...]) -> str | None:
    """The first way `value` breaks `schema`, or None.

    Supported: `type` (a name or a list of names, for nullable unions), `anyOf`, `enum`,
    `properties`, `required`, `additionalProperties: false` and `items`.
    """
    options = schema.get("anyOf")
    if options is not None and all(_check(value, option, path) is not None for option in options):
        return f"{_field(path)} does not match any allowed form"
    types = schema.get("type")
    if types is not None:
        names: Sequence[str] = [types] if isinstance(types, str) else types
        if not any(_is_type(value, name) for name in names):
            return f"{_field(path)} must be {' or '.join(names)}, not {_json_type(value)}"
    enum = schema.get("enum")
    if enum is not None and not any(type(v) is type(value) and v == value for v in enum):
        allowed = ", ".join(json.dumps(v, ensure_ascii=False) for v in enum)
        return f"{_field(path)} must be one of {allowed}"
    if isinstance(value, dict):
        properties: Mapping[str, Any] = schema.get("properties", {})
        for name in schema.get("required", ()):
            if name not in value:
                return f"missing required field {_quoted((*path, name))}"
        if schema.get("additionalProperties") is False:
            extra = [key for key in value if key not in properties]
            if extra:
                fields = ", ".join(_quoted((*path, key)) for key in extra)
                return f"unexpected field{'s' if len(extra) > 1 else ''} {fields}"
        for name, sub in properties.items():
            if name in value:
                problem = _check(value[name], sub, (*path, name))
                if problem is not None:
                    return problem
    items = schema.get("items")
    if isinstance(value, list) and items is not None:
        for index, item in enumerate(value):
            problem = _check(item, items, (*path, str(index)))
            if problem is not None:
                return problem
    return None


def _is_type(value: Any, name: str) -> bool:
    if name == "null":
        return value is None
    if name == "boolean":
        return isinstance(value, bool)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if name == "string":
        return isinstance(value, str)
    if name == "array":
        return isinstance(value, list)
    if name == "object":
        return isinstance(value, dict)
    raise ValueError(f"unsupported JSON Schema type in a tool schema: {name!r}")


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _quoted(path: tuple[str, ...]) -> str:
    return "'" + ".".join(path) + "'"


def _field(path: tuple[str, ...]) -> str:
    return f"field {_quoted(path)}" if path else "the arguments"


# -- text recovery ---------------------------------------------------------------

# Where a JSON object with a key may start; LaTeX braces such as `{text}` never match.
_OBJECT_START = re.compile(r'\{\s*"')
_NAME = re.compile(r'"name"\s*:\s*"([^"\\]+)"')
_DECODER = json.JSONDecoder()


def _decode_at(text: str, start: int, doubled: _Doubled) -> tuple[Any, int] | None:
    """The JSON value at `start` and where it ends, strictly or after doubling escapes."""
    try:
        return _DECODER.raw_decode(text, start)
    except json.JSONDecodeError:
        pass
    if not doubled.at:
        return None
    try:
        value, end = _DECODER.raw_decode(doubled.text, doubled.to_doubled(start))
    except json.JSONDecodeError:
        return None
    return value, doubled.to_original(end)


def _as_call(value: Any, known: frozenset[str], number: int) -> ToolCallCompleted | None:
    if not isinstance(value, dict):
        return None
    name = value.get("name")
    if not isinstance(name, str) or name not in known:
        return None
    key = "arguments" if "arguments" in value else "parameters"
    arguments = value.get(key)
    if isinstance(arguments, str):
        raw, args = arguments, _object_or_none(arguments)
    elif isinstance(arguments, dict):
        raw = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        args = arguments
    else:
        return None
    return ToolCallCompleted(
        id=f"rec_{number}", name=name, raw_args=raw, args=args, parse_error=None, recovered=True
    )


def _object_or_none(raw: str) -> Mapping[str, Any] | None:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _unreadable(text: str, mention: int, name: str, doubled: _Doubled) -> str:
    """Why the object around a mention of a known tool is not a readable call."""
    start = text.rfind("{", 0, mention)
    if start == -1:
        return f"the {name} call is not a JSON object"
    try:
        _DECODER.raw_decode(text, start)
    except json.JSONDecodeError as strict:
        if not doubled.at:
            return f"the {name} call is not valid JSON ({strict.msg} at character {strict.pos})"
        try:
            _DECODER.raw_decode(doubled.text, doubled.to_doubled(start))
        except json.JSONDecodeError as error:
            position = doubled.to_original(error.pos)
            return f"the {name} call is not valid JSON ({error.msg} at character {position})"
    return f'the {name} call needs an "arguments" object'
