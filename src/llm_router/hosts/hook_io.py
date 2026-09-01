"""Host-agnostic hook I/O boundary (First Forty, task 17).

``auto-route.py`` is ~4,000 lines and ``enforce-route.py`` ~1,600, both written
directly against Claude Code's payload shape and its ``{"decision": "block"}``
response. Porting that volume to Codex and Cursor by hand would produce three
implementations that drift apart within a release.

This module is the seam. Everything host-specific about a hook is reading the
payload and writing the response; the decision logic in between is already
host-neutral, it is just entangled with those two ends. Normalising both ends
here lets the existing logic be lifted once rather than rewritten per host.

What this module deliberately does NOT do: guess. A host whose payload keys have
not been captured from a real run raises :class:`UnverifiedHost` rather than
reading a plausible-looking key, because a wrong key yields ``None`` forever and
fails silently — the most expensive way for a port to be wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from llm_router.hosts.events import HOSTS, Event, unverified_fields


class UnverifiedHost(RuntimeError):
    """Raised when a host's payload contract has not been captured yet.

    Carries the field names still missing so the message tells the porter
    exactly what to record.
    """

    def __init__(self, host: str, missing: list[str]) -> None:
        super().__init__(
            f"host {host!r} has unverified payload fields: {', '.join(missing)}. "
            f"Capture one real payload from a running {host} and record the keys "
            f"in llm_router.hosts.events before normalising. Do not fill them in "
            f"from documentation."
        )
        self.host = host
        self.missing = missing


@dataclass(frozen=True)
class HookRequest:
    """One lifecycle invocation, in host-neutral form."""

    host: str
    event: Event
    prompt: str = ""
    session_id: str = ""
    tool_name: str = ""
    tool_input: dict[str, Any] | None = None
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class HookDecision:
    """What a hook wants to happen, before any host has spelled it.

    ``block`` stops the turn or the tool. ``context`` is text injected for the
    agent to read. ``updated_input`` rewrites a tool's arguments, which only
    Codex can honour — see :func:`render`, which refuses to silently drop it.
    """

    block: bool = False
    reason: str = ""
    context: str = ""
    updated_input: dict[str, Any] | None = None


def normalize(host: str, event: Event, raw: dict[str, Any]) -> HookRequest:
    """Turn a host's raw hook payload into a :class:`HookRequest`."""
    if host not in HOSTS:
        raise KeyError(f"unknown host {host!r}; known: {sorted(HOSTS)}")

    spec = HOSTS[host]
    missing = unverified_fields(host)
    # Only the fields this event actually needs have to be verified. A
    # SESSION_START hook does not read tool_input, so an unported host can still
    # carry its session hooks.
    needed = {
        Event.PROMPT_SUBMIT: {"prompt_key", "session_key"},
        Event.PRE_TOOL: {"session_key", "tool_name_key", "tool_input_key"},
        Event.POST_TOOL: {"session_key", "tool_name_key"},
    }.get(event, {"session_key"})
    blocking = sorted(set(missing) & needed)
    if blocking:
        raise UnverifiedHost(host, blocking)

    return HookRequest(
        host=host,
        event=event,
        prompt=raw.get(spec.prompt_key or "", "") or "",
        session_id=raw.get(spec.session_key or "", "") or "",
        tool_name=raw.get(spec.tool_name_key or "", "") or "",
        tool_input=raw.get(spec.tool_input_key or "") or {},
        raw=raw,
    )


def render(host: str, decision: HookDecision) -> dict[str, Any]:
    """Spell a :class:`HookDecision` the way ``host`` expects to read it.

    Refuses rather than degrades when a host cannot express the decision. A
    rewrite silently dropped on a host without ``updatedInput`` support would
    let the original, unmodified tool call run — the opposite of what the hook
    asked for, and invisible.
    """
    if host not in HOSTS:
        raise KeyError(f"unknown host {host!r}; known: {sorted(HOSTS)}")
    spec = HOSTS[host]

    if decision.updated_input is not None and not spec.can_rewrite_tool_input:
        raise ValueError(
            f"{host!r} cannot rewrite tool input, so this decision cannot be "
            f"honoured. Dropping the rewrite would run the ORIGINAL tool call "
            f"silently; block the call instead, or gate the rewrite on "
            f"HOSTS[{host!r}].can_rewrite_tool_input."
        )

    out: dict[str, Any] = {}

    if decision.block:
        if not spec.can_block_prompt:
            raise ValueError(f"{host!r} cannot block; decision is unrepresentable")
        if spec.block_shape is None:
            raise UnverifiedHost(host, ["block_shape"])
        out.update({"decision": "block", "reason": decision.reason})

    if decision.context:
        if spec.context_key is None:
            raise UnverifiedHost(host, ["context_key"])
        _nest(out, spec.context_key, decision.context)

    if decision.updated_input is not None:
        out["updatedInput"] = decision.updated_input

    return out


def _nest(target: dict[str, Any], dotted: str, value: Any) -> None:
    """Assign into ``target`` along a dotted path, creating dicts as needed.

    Claude Code wants ``hookSpecificOutput.additionalContext`` rather than a
    flat key, and the event map records that nesting literally so a host with a
    different shape needs no code change here.
    """
    parts = dotted.split(".")
    cursor = target
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = value
