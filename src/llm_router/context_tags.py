"""The one place that names the trimmable layers of a context pack as tagged text.

PLAN v16 P1.3 (R-CTX-4). `pack_overflow` can only trim what it can find in a
payload, and it finds layers by these tags. Whatever renders a `ContextPack`
into a system message MUST call `render_layers`, so the writer (the renderer)
and the reader (`pack_overflow`) share these constants and cannot drift.
Untagged text is never trimmed.

Pure stdlib; imports nothing from the router.
"""
from __future__ import annotations

from typing import Any

#: Trim order. Index 0 is trimmed first. These are `ContextPack` field names.
LAYERS = ("project", "summary", "instructions")

TAG_PROJECT = "project-knowledge"
TAG_SUMMARY = "conversation-summary"
TAG_INSTRUCTIONS = "project-instructions"

#: layer name -> tag name
TAGS = {"project": TAG_PROJECT, "summary": TAG_SUMMARY, "instructions": TAG_INSTRUCTIONS}


def wrap(layer: str, body: str) -> str:
    tag = TAGS[layer]
    return f"<{tag}>{body}</{tag}>"


def render_layers(pack: Any) -> str:
    """The pack's project, summary and instructions as tagged blocks.

    `pack` is `ContextPack`-shaped (attributes ``project``, ``summary``,
    ``instructions``). Empty layers are omitted. Request and recent messages are
    not layers and are not rendered here (they are never trimmed).
    """
    parts = []
    for layer in LAYERS:
        body = getattr(pack, layer, None)
        if body:
            parts.append(wrap(layer, body))
    return "\n".join(parts)
