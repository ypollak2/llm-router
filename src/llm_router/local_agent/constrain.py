"""Ollama structured output for the edit protocol (plan item 3.2).

The edit protocol (``llm_router.edit``) asks the local model for a JSON array
of ``{file, old_string, new_string[, description]}`` objects and validates the
reply (exact-once match, syntax gate, retry with the rejection fed back). On
this machine's fixtures that protocol passed 19/20; the one failure (c007 in
``~/.rsi/runs/routing-golden-v1/20260928T103518Z-postdeploy-e893c8d/
results_edit_scoped.jsonl``) never produced a parseable edit array in 3 tries.

Ollama's ``format`` field takes a JSON Schema and constrains decoding to it,
so the reply is syntactically valid JSON of the parser's shape by
construction. This is IN ADDITION to the validator, not instead of it: the
schema cannot know whether ``old_string`` occurs exactly once in the file.

Ollama only. Servers older than 0.5 accept ``format`` only as the string
``"json"`` and answer a schema object with HTTP 400; callers then retry the
same request without ``format`` (:func:`is_format_rejection`), so an old
server behaves exactly as before this change.
"""

from __future__ import annotations

#: The shape ``llm_router.edit.parse_edit_response`` accepts: a top-level
#: array; each item an object with ``file``, ``old_string`` and
#: ``new_string`` (required) and an optional ``description``.
EDIT_PAIRS_SCHEMA: dict = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "file": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "description": {"type": "string"},
        },
        "required": ["file", "old_string", "new_string"],
        "additionalProperties": False,
    },
}


def with_edit_format(payload: dict) -> dict:
    """A copy of an Ollama ``/api/chat`` payload with the edit schema as ``format``."""
    return {**payload, "format": EDIT_PAIRS_SCHEMA}


def is_format_rejection(status: int | None) -> bool:
    """True when an HTTP status means the server refused the request itself
    (an older Ollama that cannot bind a schema ``format`` answers 400), so an
    unconstrained retry is worth one more call. Timeouts and connection
    errors carry no status and are not retried: they would fail again."""
    return status == 400
