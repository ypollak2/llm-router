"""Cross-host lifecycle-event map (First Forty, task 16).

Every host port reads from this table. Without it each port re-derives the same
mapping by hand and the three implementations drift, which is the failure mode
task 17's extraction exists to prevent.

Scope of what is asserted here
------------------------------
Event NAMES and the block/rewrite semantics below were read from vendor
documentation on 2026-08-31. Exact payload FIELD names were verified only for
Claude Code, by reading this repo's own working hooks. Where a field name has
not been verified against a real payload it is ``None`` and
:func:`unverified_fields` reports it, so a port fails loudly at build time
rather than silently reading a key that does not exist.

Do not fill a ``None`` in from memory. Run the host, capture one real payload,
then record it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Event(str, Enum):
    """Canonical lifecycle events, named for what they mean rather than for any
    one host's spelling."""

    SESSION_START = "session_start"
    SESSION_END = "session_end"
    PROMPT_SUBMIT = "prompt_submit"
    PRE_TOOL = "pre_tool"
    POST_TOOL = "post_tool"
    SUBAGENT_START = "subagent_start"
    SUBAGENT_STOP = "subagent_stop"
    STOP = "stop"


@dataclass(frozen=True)
class HostEvents:
    """How one host spells the canonical events, and what a handler may do.

    ``events`` maps canonical -> host name. A canonical event absent from the
    mapping is not supported by that host; a port must degrade rather than
    assume.
    """

    name: str
    config_path: str
    plugin_root_var: str | None
    events: dict[Event, str]

    # Payload keys. None means "not verified against a real payload".
    prompt_key: str | None = None
    session_key: str | None = None
    tool_name_key: str | None = None
    tool_input_key: str | None = None

    # Can a PROMPT_SUBMIT handler stop the turn before the model sees it?
    can_block_prompt: bool = False
    # Can a PRE_TOOL handler rewrite the tool's arguments (not just allow/deny)?
    can_rewrite_tool_input: bool = False
    # The JSON a handler prints to block. None where unverified.
    block_shape: dict | None = None
    # Where injected context goes in the handler's response.
    context_key: str | None = None

    notes: tuple[str, ...] = field(default_factory=tuple)


CLAUDE_CODE = HostEvents(
    name="claude-code",
    config_path="~/.claude/settings.json",
    plugin_root_var="${CLAUDE_PLUGIN_ROOT}",
    events={
        Event.SESSION_START: "SessionStart",
        Event.PROMPT_SUBMIT: "UserPromptSubmit",
        Event.PRE_TOOL: "PreToolUse",
        Event.POST_TOOL: "PostToolUse",
        Event.SUBAGENT_START: "SubagentStart",
        Event.STOP: "Stop",
    },
    prompt_key="prompt",
    session_key="session_id",
    tool_name_key="tool_name",
    tool_input_key="tool_input",
    can_block_prompt=True,
    can_rewrite_tool_input=False,
    block_shape={"decision": "block", "reason": "<text>"},
    context_key="hookSpecificOutput.additionalContext",
    notes=(
        "Fully verified: these are the keys this repo's shipped hooks read and "
        "write today.",
        "Also the engine behind Claude Desktop's Code tab, which inherits the "
        "same settings file — see task 27.",
    ),
)

CODEX = HostEvents(
    name="codex",
    config_path="~/.codex/hooks.json",
    plugin_root_var="${CODEX_PLUGIN_ROOT}",
    events={
        Event.SESSION_START: "SessionStart",
        Event.SESSION_END: "SessionEnd",
        Event.PROMPT_SUBMIT: "UserPromptSubmit",
        Event.PRE_TOOL: "PreToolUse",
        Event.POST_TOOL: "PostToolUse",
        Event.SUBAGENT_START: "SubagentStart",
        Event.SUBAGENT_STOP: "SubagentStop",
        Event.STOP: "Stop",
    },
    # Payload keys NOT yet captured from a running Codex. Task 18 must record a
    # real payload before reading any of these.
    prompt_key=None,
    session_key=None,
    tool_name_key=None,
    tool_input_key=None,
    can_block_prompt=True,
    can_rewrite_tool_input=True,
    block_shape={"decision": "block", "reason": "<text>"},
    context_key=None,
    notes=(
        "Hooks are enabled by default; disabled via [features].hooks = false.",
        "PreToolUse can rewrite arguments via updatedInput — a capability the "
        "Claude Code path does not have, so the shared core must treat rewrite "
        "as optional rather than assume allow/deny only.",
        "Handlers are typed `command` or `mcp_tool`; a plugin ships them at "
        "hooks/hooks.json under its root.",
        "Codex also exposes PermissionRequest, PreCompact and PostCompact, "
        "which have no canonical equivalent here yet.",
    ),
)

CURSOR = HostEvents(
    name="cursor",
    config_path=".cursor/hooks.json",
    plugin_root_var=None,
    events={
        Event.SESSION_START: "sessionStart",
        Event.SESSION_END: "sessionEnd",
        Event.PROMPT_SUBMIT: "beforeSubmitPrompt",
        Event.PRE_TOOL: "preToolUse",
        Event.POST_TOOL: "postToolUse",
        Event.SUBAGENT_START: "subagentStart",
        Event.SUBAGENT_STOP: "subagentStop",
        Event.STOP: "stop",
    },
    prompt_key=None,
    session_key=None,
    tool_name_key=None,
    tool_input_key=None,
    can_block_prompt=True,
    can_rewrite_tool_input=False,
    block_shape=None,
    context_key=None,
    notes=(
        "Config is PROJECT-scoped: hook command paths resolve relative to "
        ".cursor/hooks.json, and there is no plugin-root variable. A global "
        "install must therefore write per-project or say plainly that it does "
        "not — task 25.",
        "Cloud agents run project hooks but NOT prompt hooks, so auto-routing "
        "does not apply there. Document it rather than discover it in an issue.",
        "Also exposes beforeShellExecution / beforeMCPExecution, which give the "
        "same hold semantics PreToolUse provides on the other hosts.",
    ),
)

GEMINI_CLI = HostEvents(
    name="gemini-cli",
    config_path="~/.gemini/settings.json",
    plugin_root_var=None,
    events={
        Event.PROMPT_SUBMIT: "UserPromptSubmit",
        Event.POST_TOOL: "PostToolUse",
        Event.SESSION_END: "SessionEnd",
    },
    prompt_key="prompt",
    session_key="session_id",
    can_block_prompt=True,
    block_shape={"decision": "block", "reason": "<text>"},
    context_key="hookSpecificOutput.additionalContext",
    notes=(
        "Already shipping: gemini-cli-auto-route.py and friends live in "
        "src/llm_router/hooks/. Recorded here so the table is the whole truth "
        "rather than only the new work.",
    ),
)

HOSTS: dict[str, HostEvents] = {
    h.name: h for h in (CLAUDE_CODE, CODEX, CURSOR, GEMINI_CLI)
}


def supports(host: str, event: Event) -> bool:
    """True when ``host`` has a lifecycle event matching ``event``."""
    return event in HOSTS[host].events


def host_event_name(host: str, event: Event) -> str:
    """The host's own spelling of a canonical event.

    Raises rather than guessing: a port that asks for an unsupported event has
    a bug, and silently returning the canonical name would write a hooks.json
    the host ignores without complaint.
    """
    spec = HOSTS[host]
    try:
        return spec.events[event]
    except KeyError:
        raise KeyError(
            f"{host!r} has no lifecycle event for {event.value!r}; "
            f"supported: {sorted(e.value for e in spec.events)}"
        ) from None


def unverified_fields(host: str) -> list[str]:
    """Payload fields not yet captured from a real run of this host.

    A port must not read a field listed here. Capture one real payload first —
    inventing a plausible key is how a hook silently receives None forever.
    """
    spec = HOSTS[host]
    return [
        attr
        for attr in ("prompt_key", "session_key", "tool_name_key", "tool_input_key")
        if getattr(spec, attr) is None
    ]


def routing_ready(host: str) -> bool:
    """Can this host carry full auto-routing today?

    Requires prompt interception, a hold point, and verified payload keys for
    both. This is the honest answer the host support matrix (task 29) should
    print — not an aspiration.
    """
    spec = HOSTS[host]
    if not (spec.can_block_prompt and supports(host, Event.PROMPT_SUBMIT)):
        return False
    if not supports(host, Event.PRE_TOOL):
        return False
    return not unverified_fields(host)
