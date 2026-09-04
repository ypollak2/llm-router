"""Pure helpers for wiring llm-router into Codex CLI.

Everything Codex-specific that can be computed without touching the
filesystem lives here, so the installer and doctor share one definition and
the tests can pin it.

Two facts about Codex (0.153, verified 2026-09-04 against a real run) drive
this module:

1. Codex reads MCP servers from ``~/.codex/config.toml`` under
   ``[mcp_servers.<name>]``. Not ``config.yaml``, not ``config.json``.
2. Codex **silently skips** a hook from ``hooks.json`` unless
   ``config.toml`` carries a matching trust record::

       [hooks.state."<abs hooks.json>:<event_label>:<group>:<handler>"]
       trusted_hash = "sha256:<hex>"

   The hash is over a normalized identity of the hook, not the file text
   (codex-rs/hooks/src/engine/discovery.rs ``hook_hash`` +
   codex-rs/config/src/fingerprint.rs ``version_for_toml``): the handler
   config with defaults filled in, wrapped with the event label, serialized
   as canonical JSON (keys sorted recursively, no whitespace), SHA-256.
   An installer that writes the hook without the record installs nothing.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

DEFAULT_HOOK_TIMEOUT_SEC = 600
SESSION_END_DEFAULT_TIMEOUT_SEC = 30  # clamped separately by Codex; we never install one

MCP_SERVER_NAME = "llm_router"
MCP_TABLE = f"mcp_servers.{MCP_SERVER_NAME}"

# Routing doors that `codex exec` (approval policy "never") must be able to
# call without a prompt. Admin and agent-session tools are deliberately absent.
APPROVED_TOOLS = ("llm", "llm_act", "llm_edit", "llm_image", "llm_audio", "llm_route",
                  "llm_router_status", "llm_router_session")

AGENTS_BLOCK_START = "<!-- llm-router:start -->"
AGENTS_BLOCK_END = "<!-- llm-router:end -->"

# Codex event name -> label used in state keys and the hashed identity.
EVENT_LABELS = {
    "PreToolUse": "pre_tool_use",
    "PermissionRequest": "permission_request",
    "PostToolUse": "post_tool_use",
    "PreCompact": "pre_compact",
    "PostCompact": "post_compact",
    "SessionStart": "session_start",
    "SessionEnd": "session_end",
    "UserPromptSubmit": "user_prompt_submit",
    "SubagentStart": "subagent_start",
    "SubagentStop": "subagent_stop",
    "Stop": "stop",
}

# Events whose matcher survives normalization. For the others Codex drops it
# before hashing (codex-rs/hooks/src/events/common.rs matcher_pattern_for_event).
EVENTS_WITH_MATCHER = frozenset({
    "PreToolUse", "PermissionRequest", "PostToolUse", "SessionStart", "SessionEnd",
    "SubagentStart", "SubagentStop", "PreCompact", "PostCompact",
})


def _canonical(value):
    if isinstance(value, dict):
        return {k: _canonical(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    return value


def hook_trust_hash(event: str, handler: dict, matcher: str | None = None) -> str:
    """The ``trusted_hash`` Codex expects for one command hook.

    ``handler`` is the JSON object from hooks.json (``type``, ``command``,
    optional ``timeout``, ``async``, ``statusMessage``,
    ``additionalContextLimit``). Only ``type: command`` is supported here.
    """
    if event not in EVENT_LABELS:
        raise ValueError(f"unknown Codex hook event {event!r}")
    if handler.get("type", "command") != "command":
        raise ValueError("only command hooks are supported")
    normalized: dict = {
        "type": "command",
        "command": handler["command"],
        "timeout": int(handler.get("timeout") or DEFAULT_HOOK_TIMEOUT_SEC),
        "async": bool(handler.get("async", False)),
    }
    if handler.get("statusMessage"):
        normalized["statusMessage"] = handler["statusMessage"]
    limit = handler.get("additionalContextLimit")
    if limit is not None and limit != 2500:
        normalized["additionalContextLimit"] = limit
    identity: dict = {"event_name": EVENT_LABELS[event], "hooks": [normalized]}
    if event in EVENTS_WITH_MATCHER and matcher is not None:
        identity["matcher"] = matcher
    payload = json.dumps(_canonical(identity), separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def hook_state_key(hooks_json: Path | str, event: str, group_index: int, handler_index: int) -> str:
    return f"{hooks_json}:{EVENT_LABELS[event]}:{group_index}:{handler_index}"


def hook_state_table(key: str) -> str:
    """The TOML table header for one trust record (the key is always quoted)."""
    return f'hooks.state."{key}"'


def trust_records(hooks_json: Path | str, hooks_doc: dict, *, only_commands: set[str] | None = None) -> dict[str, str]:
    """``{state_key: trusted_hash}`` for every command hook in a hooks.json document.

    ``only_commands`` restricts the result to handlers whose command is in the
    set -- the installer trusts what it wrote and nothing else.
    """
    out: dict[str, str] = {}
    for event, groups in (hooks_doc.get("hooks") or {}).items():
        if event not in EVENT_LABELS or not isinstance(groups, list):
            continue
        for gi, group in enumerate(groups):
            if not isinstance(group, dict):
                continue
            matcher = group.get("matcher")
            for hi, handler in enumerate(group.get("hooks") or []):
                if not isinstance(handler, dict) or handler.get("type", "command") != "command":
                    continue
                if only_commands is not None and handler.get("command") not in only_commands:
                    continue
                out[hook_state_key(hooks_json, event, gi, hi)] = hook_trust_hash(event, handler, matcher)
    return out


# ── config.toml text surgery ────────────────────────────────────────────────
# Codex users hand-edit config.toml; never rewrite the file, only insert or
# replace the one table we own. tomllib reads, regex writes.

_TABLE_RE_TEMPLATE = r'^\[{header}\]\n(?:(?!\[).*\n?)*'


def _table_pattern(header_literal: str) -> re.Pattern:
    return re.compile(_TABLE_RE_TEMPLATE.format(header=re.escape(header_literal)), re.MULTILINE)


def upsert_toml_table(text: str, header_literal: str, body: str) -> str:
    """Replace the table ``[header_literal]`` (header text exactly as it
    appears between the brackets) or append it. Body lines are written as
    given; the caller quotes values."""
    block = f"[{header_literal}]\n{body.rstrip()}\n"
    pat = _table_pattern(header_literal)
    if pat.search(text):
        return pat.sub(lambda _m: block + "\n", text, count=1).rstrip("\n") + "\n"
    if not text.strip():
        return block
    sep = "" if text.endswith("\n") else "\n"
    return f"{text}{sep}\n{block}"


def remove_toml_table(text: str, header_literal: str) -> str:
    pat = _table_pattern(header_literal)
    return pat.sub("", text, count=1)


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)  # JSON string escaping is valid TOML basic-string escaping


def mcp_table_body(command: str, args: list[str]) -> str:
    return f"command = {toml_string(command)}\nargs = [{', '.join(toml_string(a) for a in args)}]"


def tool_table(tool: str) -> str:
    return f"{MCP_TABLE}.tools.{tool}"


def approved_tools() -> tuple[str, ...]:
    """APPROVED_TOOLS filtered to what the active tool surface registers."""
    try:
        from llm_router.tool_surface import registered_tools
        reg = registered_tools()
    except Exception:  # noqa: BLE001
        reg = None
    if reg is None:
        return APPROVED_TOOLS
    return tuple(t for t in APPROVED_TOOLS if t in reg)


def read_mcp_server(config_toml_text: str) -> dict | None:
    """The ``[mcp_servers.llm_router]`` table as a dict, or None."""
    import tomllib
    try:
        data = tomllib.loads(config_toml_text)
    except tomllib.TOMLDecodeError:
        return None
    entry = (data.get("mcp_servers") or {}).get(MCP_SERVER_NAME)
    return dict(entry) if isinstance(entry, dict) else None


def read_trust_records(config_toml_text: str) -> dict[str, str]:
    import tomllib
    try:
        data = tomllib.loads(config_toml_text)
    except tomllib.TOMLDecodeError:
        return {}
    state = ((data.get("hooks") or {}).get("state") or {})
    return {k: v.get("trusted_hash") for k, v in state.items() if isinstance(v, dict) and v.get("trusted_hash")}


# ── AGENTS.md marked block ──────────────────────────────────────────────────

def upsert_marked_block(text: str, body: str) -> str:
    """Insert or replace the llm-router block in an AGENTS.md. Everything
    outside the markers is preserved byte for byte."""
    block = f"{AGENTS_BLOCK_START}\n{body.strip()}\n{AGENTS_BLOCK_END}\n"
    pat = re.compile(re.escape(AGENTS_BLOCK_START) + r".*?" + re.escape(AGENTS_BLOCK_END) + r"\n?", re.DOTALL)
    if pat.search(text):
        return pat.sub(lambda _m: block, text, count=1)
    if not text.strip():
        return block
    sep = "" if text.endswith("\n") else "\n"
    return f"{text}{sep}\n{block}"


def remove_marked_block(text: str) -> str:
    pat = re.compile(r"\n*" + re.escape(AGENTS_BLOCK_START) + r".*?" + re.escape(AGENTS_BLOCK_END) + r"\n?", re.DOTALL)
    out = pat.sub("\n", text, count=1)
    out = re.sub(r"\n{3,}", "\n\n", out).strip("\n")
    return out + "\n" if out else ""
