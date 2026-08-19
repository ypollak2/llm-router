"""Version-agnostic model aliases — single source of truth for "latest" ids.

This module has NO llm_router dependencies so it can be imported from both
``policy.py`` (chain loading) and ``profiles.py`` (routing table) without a
circular import.

Chains in ``policies/*.yaml`` may use a family alias ending in ``:latest``,
e.g. ``anthropic/claude-opus:latest``. It is resolved to the concrete id from
``LATEST_CLAUDE`` at load time (in ``policy._parse_chains``), so when a newer
model ships you update ONE line here and every chain follows.
"""

from __future__ import annotations

from typing import Dict

# Map model FAMILY -> current concrete id. Update only this when a newer
# version ships (e.g. bump claude-opus to "anthropic/claude-opus-5").
LATEST_CLAUDE: Dict[str, str] = {
    "anthropic/claude-opus": "anthropic/claude-opus-4-8",
    "anthropic/claude-sonnet": "anthropic/claude-sonnet-4-6",
    "anthropic/claude-haiku": "anthropic/claude-haiku-4-5-20251001",
}

_LATEST_SUFFIX = ":latest"


def model_family(model: str) -> str:
    """Strip a trailing version/date suffix to get the family.

    ``anthropic/claude-opus-4-8`` -> ``anthropic/claude-opus``
    ``anthropic/claude-haiku-4-5-20251001`` -> ``anthropic/claude-haiku``
    Non-versioned ids are returned unchanged.
    """
    import re
    return re.sub(r"-\d.*$", "", model)


def model_matches(model: str, pattern: str) -> bool:
    """True if ``model`` equals ``pattern`` or belongs to its family.

    ``pattern`` may be a concrete id (exact match) or a family prefix
    (matches every version in that family, including future ones).
    """
    if model == pattern:
        return True
    return model.startswith(pattern + "-")


def family_lookup(table: dict, model: str, default=None):
    """Look up ``model`` in ``table`` (keyed by concrete id), falling back to any
    entry in the same family. Lets a new version (e.g. claude-opus-4-9) inherit
    the cost/benchmark of its family instead of returning a KeyError/None.
    """
    if model in table:
        return table[model]
    fam = model_family(model)
    for key, value in table.items():
        if model_family(key) == fam:
            return value
    return default


def resolve_model_alias(entry: str) -> str:
    """Resolve a ``family:latest`` alias to its concrete id.

    Unknown families or non-alias entries are returned unchanged, so this is
    safe to apply to every chain entry indiscriminately.
    """
    if not entry.endswith(_LATEST_SUFFIX):
        return entry
    family = entry[: -len(_LATEST_SUFFIX)]
    return LATEST_CLAUDE.get(family, entry)
