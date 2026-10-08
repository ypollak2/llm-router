#!/usr/bin/env python3
# llm_router-hook-version: 18
"""PreToolUse[Agent] hook — intercept subagent spawning, route reasoning to cheap models.

When Claude spawns a subagent (Agent tool), this hook intercepts and decides:

  APPROVE → pure retrieval tasks: file reads, searches, directory listings.
            These need local filesystem access — subagents are the right tool.

  BLOCK   → reasoning tasks: analysis, coding, generation, explanation.
            These are routed to the appropriate llm_* MCP tool instead,
            which routes to a cheaper model than Opus.

Pressure-aware profile selection (passed to the MCP tool):
  < 85% quota:
    simple   → profile=budget   (Haiku — much cheaper than Opus)
    moderate → profile=balanced  (Sonnet — cheaper than Opus)
    complex  → profile=premium   (Opus — best quality, full quota available)
  ≥ 85% quota:
    simple   → profile=budget   (cheapest external: Gemini Flash, Groq)
    moderate → profile=balanced  (DeepSeek, GPT-4o)
    complex  → profile=balanced  (same — at high pressure premium = balanced cost)

Note: Explore subagent type is always approved (pure retrieval by design).
Note: Mixed tasks (read files then analyze) are blocked; Claude is instructed
      to read files with local tools then pass content to the MCP tool.

NS3 (2026-09-27): suitable spawns (research / code-reading / analysis that
does not need the parent's live context and is not a write-heavy multi-file
edit) are offered to Codex CLI FIRST, ahead of the `_allow_routed_spawn()`
model-pin path below. Before this change that path ran unconditionally for
every non-Explore, non-allowlisted spawn (default `LLM_ROUTER_ALLOW_SUBAGENTS
=on`) and returned immediately, so `_try_cli_delegation` — the only existing
code that calls Codex — was unreachable in the default configuration. That is
the root cause `model_tracking.jsonl` showed 0 Codex calls across 30 days
despite this hook being "on". See `_try_codex_subagent_delegation`.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# -- KPI G1: record how long this invocation ran (llm_router.hook_latency) -----
# The clock starts BEFORE the first llm_router import, so the package import is
# inside the measurement. Armed only when run as a script: a test that imports
# this file must not register an exit-time write. Fail-open: no llm_router on
# the path means no row for this run; any other error is reported on stderr
# (never stdout, which the host parses) and the hook carries on.
import time as _hl_time

_HOOK_T0 = _hl_time.monotonic()
if __name__ == "__main__":
    try:
        from llm_router import hook_latency as _hook_latency

        _hook_latency.begin("agent-route", "PreToolUse", _HOOK_T0)
    except ImportError:
        pass  # llm_router is not importable on this host: no recorder, no row
    except Exception as _hl_exc:  # noqa: BLE001 -- timing must never break the hook
        import sys as _hl_sys

        print(f"llm-router: hook latency not recorded ({type(_hl_exc).__name__})", file=_hl_sys.stderr)

# P0.9: name where the time went (phases_ms on the hook_latency row), so a reader can
# tell router time from routed-model time (the delegation phases). Both are no-ops
# unless the recorder was armed above, so a test that imports this file records nothing.
try:
    from llm_router.hook_latency import mark_main_start as _hl_mark_main, phase as _hl_phase
except ImportError:  # llm_router is not importable on this host: no recorder, no phases
    import contextlib as _hl_contextlib

    def _hl_phase(name):  # noqa: ARG001
        return _hl_contextlib.nullcontext()

    def _hl_mark_main():
        return None


# ── Registered-tool surface (CHZ-SURF-01) ────────────────────────────────────
# Tool names are tier-dependent (LLM_ROUTER_SLIM). NEVER put a raw tool name in
# output: under the DEFAULT `consolidated` tier the legacy llm_query /
# llm_analyze / llm_code / llm_research / llm_generate names are not registered,
# so naming one hands the caller "Error: No such tool available" — after which
# it silently does the work on the expensive model and the savings dashboard
# cannot distinguish that from "chose not to route".
def _router_home():
    """Router state dir, resolved per call so LLM_ROUTER_HOME is honoured.

    M-04: this was a module constant bound at import, so a hook launched with
    LLM_ROUTER_HOME set still wrote to the operator's real home directory.

    Imports locally: hooks are standalone scripts with varied import headers and
    several do not import Path or os at module scope.
    """
    import os as _os
    from pathlib import Path as _P

    base = _os.environ.get("LLM_ROUTER_HOME", "").strip()
    return _P(base).expanduser() if base else _P.home() / ".llm-router"


def _load_tool_surface_fns():
    """(route_tool, route_call, route_call_with_complexity, call_parts, tool_for_task).

    Falls back to the stdlib-only copy the installer drops next to the hooks, then
    to the in-repo source, then to identity (correct only for tier `off`).
    """
    try:
        from llm_router.tool_surface import (
            call_parts,
            route_call,
            route_call_with_complexity,
            route_tool,
            tool_for_task,
        )
        return route_tool, route_call, route_call_with_complexity, call_parts, tool_for_task
    except ImportError:
        pass
    try:
        import importlib.util as _ilu
        from pathlib import Path as _P
        _here = _P(__file__).resolve().parent
        for _cand in (_here / "llm_router_tool_surface.py", _here.parent / "tool_surface.py"):
            if not _cand.exists():
                continue
            _spec = _ilu.spec_from_file_location("llm_router_tool_surface", _cand)
            _mod = _ilu.module_from_spec(_spec)
            sys.modules["llm_router_tool_surface"] = _mod  # dataclasses needs this
            _spec.loader.exec_module(_mod)
            return (_mod.route_tool, _mod.route_call,
                    _mod.route_call_with_complexity, _mod.call_parts,
                    _mod.tool_for_task)
    except Exception:  # noqa: BLE001 — a broken support module must not kill the hook
        pass
    return (
        lambda n, **k: n,
        lambda n, *a, **k: (f"{n}({', '.join(a)})" if a else n),
        lambda n, c, *a, **k: f"{n}(complexity='{c}'" + ("".join(', ' + x for x in a)) + ")",
        lambda n, **k: (n, []),
        # Last-resort fallback mirrors tool_surface.DEFAULT_TASK_TOOL: llm_route,
        # never a completion door — see RED8-06.
        lambda t: {"research": "llm_research", "generate": "llm_generate",
                   "analyze": "llm_analyze", "code": "llm_code",
                   "query": "llm_query", "image": "llm_image",
                   "coordination": "llm_query", "auto": "llm_route"}.get(t, "llm_route"),
    )


(route_tool, route_call,
 route_call_with_complexity, call_parts, tool_for_task) = _load_tool_surface_fns()

# ── .env loader (mirrors auto-route.py) ──────────────────────────────────────
# PreToolUse[Agent] runs without an interactive shell, so OLLAMA_BUDGET_MODELS,
# GEMINI_API_KEY, etc. from ~/.llm-router/.env are not in os.environ unless we load
# them. Without this, build_chain() falls back to its hardcoded default model
# (often not pulled) and DIRECT routing silently degrades to paid/Claude tiers.

def _load_dotenv(load_into: "dict[str, str] | None" = None) -> None:
    """Load .env files into `load_into` (default: os.environ); never override.

    One implementation, shared by every entry point: llm_router.env_loader.
    The real environment always wins; the working directory's .env is filtered
    (SEC-002/003). `load_into` lets tests load into a dict instead of os.environ.
    """
    try:
        from llm_router.env_loader import load_dotenv_files
    except Exception:
        load_dotenv_files = None
    if load_dotenv_files is not None:
        load_dotenv_files(extra_paths=[Path(__file__).resolve().parent.parent.parent.parent / ".env"], target=load_into)
        return
    # llm_router is not importable (plugin bundle run by a bare interpreter):
    # user-level files only. The project's own .env is never trusted without
    # the SEC-002/003 filter that lives in llm_router.env_loader.
    target = os.environ if load_into is None else load_into
    for env_path in (_router_home() / ".env", Path.home() / ".env"):
        try:
            text = env_path.read_text()
        except (OSError, ValueError):
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key and key not in target:
                target[key] = value.strip().strip("\"'")


_load_dotenv()

# ── Agent resource limits ────────────────────────────────────────────────────

AGENT_MAX_COST_USD = 5.0            # Hard per-agent cost limit
SESSION_MAX_COST_USD = 50.0         # Hard per-session cost limit (fallback)
SOFT_BUDGET_FACTOR = 0.8            # Warn if cost > 80% of remaining budget

# ── Retrieval detection (approve subagent) ───────────────────────────────────
# These signal that the subagent's job is FINDING/READING, not REASONING.
# If these dominate and no reasoning verbs are present → approve.

_RETRIEVAL_INTENT = re.compile(
    r"\b(?:find (?:all |every |any )?(?:files?|classes?|functions?|methods?|patterns?|"
    r"references?|usages?|imports?|calls?|definitions?|symbols?)|"
    r"search (?:for|through|across)|"
    r"list (?:all |every )?(?:files?|directories?|modules?|classes?|functions?)|"
    r"glob|grep|scan|inventory|discover|locate|"
    r"what files?|which files?|where (?:is|are)|show me (?:the )?(?:files?|structure|list)|"
    r"explore (?:the )?(?:codebase|directory|repo|project|structure)|"
    r"map (?:the )?(?:codebase|dependencies|imports?|structure)|"
    r"read (?:the )?(?:file|files?|content) (?:at|from|of|named?)|"
    r"get (?:the )?(?:content|text|source) (?:of|from))\b",
    re.IGNORECASE,
)

_REASONING_INTENT = re.compile(
    r"\b(?:analyze|analyse|evaluate|assess|explain|describe|summarize|"
    r"implement|write|create|build|generate|draft|"
    r"fix|debug|diagnose|resolve|repair|"
    r"compare|contrast|review|audit|critique|"
    r"optimize|refactor|improve|redesign|"
    r"plan|design|architect|strategy|"
    r"why|how does|what causes|what is (?:wrong|the (?:issue|problem|bug|root cause))|"
    r"identify (?:bugs?|issues?|problems?|patterns?|improvements?)|"
    r"should (?:I|we)|is (?:it |this )?(?:correct|right|good|bad|safe|secure))\b",
    re.IGNORECASE,
)

# ── Complexity signals ───────────────────────────────────────────────────────

_COMPLEX_SIGNALS = re.compile(
    r"\b(?:comprehensive|complete|full|entire|end-to-end|thorough|in-depth|"
    r"detailed|deep dive|all (?:aspects?|parts?|components?|modules?|files?)|"
    r"across (?:the )?(?:codebase|repo|project|all)|"
    r"architecture|system design|multiple|several|various|every|"
    r"production|scalable|critical|security|performance)\b",
    re.IGNORECASE,
)

_SIMPLE_SIGNALS = re.compile(
    r"\b(?:quick|simple|brief|short|just|only|single|one|"
    r"small|minor|tiny|trivial|basic|specific|particular)\b",
    re.IGNORECASE,
)

# ── Task type → MCP tool mapping ─────────────────────────────────────────────

_TASK_SIGNALS: dict[str, re.Pattern] = {
    "code": re.compile(
        r"\b(?:implement|write (?:a |the )?(?:function|class|module|test|script)|"
        r"build|scaffold|refactor|fix (?:the |a )?(?:bug|error|issue|crash)|"
        r"add (?:a )?(?:feature|method|test|endpoint)|"
        r"update (?:the )?(?:code|logic|function)|"
        r"create (?:a )?(?:function|class|module|component|test))\b",
        re.IGNORECASE,
    ),
    "analyze": re.compile(
        r"\b(?:analyze|evaluate|assess|review|audit|critique|debug|diagnose|"
        r"explain|describe|compare|identify (?:issues?|bugs?|problems?|patterns?)|"
        r"root cause|deep dive|how does|why (?:does|is|did)|"
        r"what (?:is|are) (?:the )?(?:issue|problem|bug|pattern|bottleneck)|"
        r"should (?:we|I)|pros? and cons?|trade-?off)\b",
        re.IGNORECASE,
    ),
    "research": re.compile(
        r"\b(?:research|look up|find out|what(?:'s| is) (?:the )?(?:latest|current)|"
        r"what happened|market|trend|news|latest|recent|current state)\b",
        re.IGNORECASE,
    ),
    "generate": re.compile(
        r"\b(?:write (?:a |an |the )?(?:document|readme|changelog|report|email|"
        r"summary|description|comment|docstring)|"
        r"draft|compose|create (?:content|documentation|text))\b",
        re.IGNORECASE,
    ),
    "query": re.compile(
        r"\b(?:what is|what are|how (?:do|does|can|to)|"
        r"where (?:is|are)|when (?:does|did|is)|which|"
        r"tell me|can you explain|define|clarify)\b",
        re.IGNORECASE,
    ),
}

# RED8-06: the private _TOOL_MAP is gone. It held 5 of the 8 task types and fell
# back to llm_analyze — a COMPLETION DOOR that cannot run tools — while
# auto-route.py fell back to llm_route for the same unrecognised input. The five
# shared keys agreed, so the maps looked consistent; the divergence only showed
# on everything else, and no test drove one prompt through both. The canonical
# map now lives in llm_router.tool_surface (see tool_for_task above).


# ── Agent loop circuit breaker ──────────────────────────────────────────────

def _get_max_depth() -> int:
    """Read LLM_ROUTER_MAX_AGENT_DEPTH from environment, default 3."""
    try:
        return int(os.environ.get("LLM_ROUTER_MAX_AGENT_DEPTH", "3"))
    except (ValueError, TypeError):
        return 3


def _get_max_concurrent() -> int:
    """LLM_ROUTER_MAX_CONCURRENT_AGENTS: runaway cap on agents in flight, default 16."""
    try:
        return int(os.environ.get("LLM_ROUTER_MAX_CONCURRENT_AGENTS", "16"))
    except (ValueError, TypeError):
        return 16


#: LLM_ROUTER_AGENT_SLOT_TTL_S: how long an in-flight slot is trusted without a
#: PostToolUse release. Default 3600 s (1 h) = 2x a 30 min agent run, the longest a
#: council / long review is expected to take. It is a judgement, not a measurement:
#: nothing records real agent durations. Too small expires a live agent's slot (the
#: cap then undercounts, which fails safe); too large keeps leaked slots (crash,
#: another hook denying the call) blocking spawns for that long.
_SLOT_TTL_DEFAULT_S = 3600.0


def _slot_ttl_s() -> float:
    try:
        ttl = float(os.environ.get("LLM_ROUTER_AGENT_SLOT_TTL_S", str(_SLOT_TTL_DEFAULT_S)))
        return ttl if ttl > 0 else _SLOT_TTL_DEFAULT_S
    except (ValueError, TypeError):
        return _SLOT_TTL_DEFAULT_S


def _get_session_id() -> str:
    """Return a session identifier unique to THIS Claude Code process.

    Prefers ``CLAUDE_CODE_SESSION_ID`` (set by Claude Code itself, unique per
    running session) over the legacy ~/.llm-router/session_id.txt scheme. That
    file is a single machine-wide singleton written fresh by every session's
    SessionStart hook — so two Claude Code windows running concurrently (e.g.
    one per project) silently share ONE session id, and therefore one agent
    nesting-depth counter. A depth-3 circuit trip in project A then blocks
    project B's very first, unrelated Agent call. CLAUDE_CODE_SESSION_ID is
    genuinely unique per process, so keying on it (see _depth_file below)
    gives each concurrent session its own counter instead.
    """
    env_session = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if env_session:
        return env_session
    session_file = _router_home() / "session_id.txt"
    try:
        return session_file.read_text().strip()
    except FileNotFoundError:
        return "unknown"


def _depth_file(session_id: str) -> Path:
    """Per-session depth-state file — NOT a single shared file.

    Keying the file itself (not just a session_id field inside one shared
    file) means two concurrent sessions never read-modify-write the same
    file, closing the race window entirely rather than relying on a string
    comparison that both processes could pass at once.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id) or "unknown"
    return _router_home() / f"agent_depth_{safe}.json"


def _read_state(session_id: str) -> dict:
    """Whole per-session breaker state (never raises)."""
    try:
        data = json.loads(_depth_file(session_id).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _write_state(session_id: str, state: dict) -> None:
    """Persist breaker state atomically (tmp file + os.replace, mode 0600).

    A reader never sees a half-written file. Callers that read-modify-write must
    hold _state_lock; this function alone does not make a RMW safe.
    """
    state = dict(state)
    if isinstance(state.get("slots"), list):
        state["depth"] = len(state["slots"])  # derived: the slots are the truth
    else:
        state["depth"] = max(0, int(state.get("depth", 0)))
    state["session_id"] = session_id
    state["ts"] = time.time()
    path = _depth_file(session_id)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(state))
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _lock_wait_s() -> float:
    """Seconds to wait for the state lock, per acquisition (default 0.25).

    One PreToolUse run takes the lock at most once, so this is also the most the
    breaker can add to a run. The host kills PreToolUse after 320 s (hooks.json); the
    wait is a latency choice, not a timeout guard.
    """
    try:
        return max(0.0, float(os.environ.get("LLM_ROUTER_BREAKER_LOCK_WAIT_S", "0.25")))
    except ValueError:
        return 0.25


def _log_hook_error(message: str, session_id: str = "") -> None:
    """One line in hook_errors.log (the llm_router.hook_health schema). Never raises.

    The message names an exception class, never prompt text.
    """
    try:
        home = _router_home()
        home.mkdir(parents=True, exist_ok=True)
        entry = {"timestamp": datetime.now().isoformat(), "hook": "agent-route",
                 "error": message[:200]}
        if session_id:
            entry["context"] = {"session_id": session_id}
        with (home / "hook_errors.log").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:  # noqa: BLE001 -- logging must never break a spawn
        pass


@contextlib.contextmanager
def _state_lock(session_id: str):
    """Exclusive flock on a sidecar lock file (the state file's inode changes on replace).

    Polls non-blocking for at most _lock_wait_s() (default 0.25 s). On any failure
    to lock it logs (stderr and hook_errors.log) and yields anyway: the breaker fails
    open (the pre-lock behaviour) rather than stalling a spawn.
    """
    fh = None
    try:
        import fcntl
        path = _depth_file(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(f"{path}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fh = os.fdopen(fd, "a+")
        deadline = time.monotonic() + _lock_wait_s()
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.002)
    except Exception as exc:  # noqa: BLE001 -- never stall or break a spawn on the lock
        _msg = (f"agent breaker state lock unavailable ({type(exc).__name__}); "
                f"continuing unlocked")
        print(f"llm-router: {_msg}", file=sys.stderr)
        _log_hook_error(_msg, session_id)
        if fh is not None:
            with contextlib.suppress(Exception):
                fh.close()
            fh = None
    try:
        yield
    finally:
        if fh is not None:
            with contextlib.suppress(Exception):
                fh.close()  # closing releases the flock


def _update_state(session_id: str, mutate) -> None:
    """Locked read-modify-write of the per-session state. Never raises."""
    try:
        with _state_lock(session_id):
            state = _read_state(session_id)
            mutate(state)
            _write_state(session_id, state)
    except Exception as exc:  # noqa: BLE001 -- fail open, say so
        _msg = f"agent breaker state not saved ({type(exc).__name__})"
        print(f"llm-router: {_msg}", file=sys.stderr)
        _log_hook_error(_msg, session_id)


def _live_slots(state: dict, now: float | None = None) -> list:
    """In-flight entries [token, start_ts] younger than the slot TTL.

    Each real spawn owns one entry, released by token (PostToolUse) or expired here.
    A state file written before slots existed carries a bare ``depth`` count: it is
    read as that many entries aged from the file's ``ts``, so a count leaked by the
    old code expires on its own instead of blocking spawns for the session.
    """
    now = time.time() if now is None else now
    raw = state.get("slots")
    if not isinstance(raw, list):
        try:
            legacy, ts = max(0, int(state.get("depth", 0))), float(state.get("ts", 0))
        except (ValueError, TypeError):
            legacy, ts = 0, 0.0
        raw = [[f"legacy{i}", ts] for i in range(legacy)]
    ttl, live = _slot_ttl_s(), []
    for e in raw:
        try:
            if isinstance(e, list) and len(e) >= 2 and now - float(e[1]) < ttl:
                live.append([str(e[0]), float(e[1])])
        except (ValueError, TypeError):
            continue
    return live


def _read_agent_depth(session_id: str) -> int:
    """Agents currently in flight (live slots only).

    This is a CONCURRENCY count, not nesting depth. It used to be compared with
    the nesting limit, so 4 parallel siblings from one top-level session
    tripped a "nested agents" breaker (docs/BUGS.md, bug AB-1).
    """
    return len(_live_slots(_read_state(session_id)))


# Nesting registry. A hook payload fired from inside a subagent carries
# ``agent_id``; one fired by the top-level session does not. Claude Code gives
# no parent link, so each spawn records the depth its child WILL have in a
# short FIFO ("pending"), and subagent-start.py, which does see the child's
# agent_id, claims the oldest entry into "agents". Siblings share a depth, so a
# mis-ordered claim between siblings is harmless.
_PENDING_TTL_S = 120.0
_MAX_REGISTRY = 200


def _caller_depth(hook_input: dict, state: dict) -> int:
    """Nesting depth of the agent making this Agent call: 0 = top-level session.

    A caller with an agent_id the registry never saw is at least depth 1 (it IS a
    subagent); we do not guess deeper than we can prove.
    """
    agent_id = str(hook_input.get("agent_id") or "").strip()
    if not agent_id:
        return 0
    agents = state.get("agents")
    try:
        return max(1, int(agents.get(agent_id, 1))) if isinstance(agents, dict) else 1
    except (ValueError, TypeError):
        return 1


def _commit_spawn(session_id: str, token: str, child_depth: int, take_slot: bool) -> None:
    """Record an APPROVED spawn in ONE locked read-modify-write.

    Pushes the pending entry [ts, child_depth, token] that subagent-start.py claims
    and, when ``take_slot``, an in-flight slot [token, start_ts] that PostToolUse
    releases by token. Called only where the hook lets the spawn proceed: a blocked
    or routed-away call never writes, so there is nothing to give back and a run
    takes the lock at most once (it used to take it up to three times).
    """
    def _m(state: dict) -> None:
        now = time.time()
        pending = [p for p in state.get("pending", []) if isinstance(p, list) and len(p) >= 2
                   and now - float(p[0]) < _PENDING_TTL_S]
        pending.append([now, child_depth, token])
        state["pending"] = pending[-_MAX_REGISTRY:]
        slots = _live_slots(state, now)
        if take_slot:
            slots.append([token, now])
        state["slots"] = slots[-_MAX_REGISTRY:]
    _update_state(session_id, _m)


# ── Agent call tracking (for error recovery) ────────────────────────────────

# Inline secret patterns (this file is loaded standalone by Claude Code as a
# hook, so we can't rely on the llm_router package being importable). Mirrors
# llm_router.library.store.scrub_secrets — inline substitution preserves the
# surrounding prompt for error-recovery context while stripping credentials.
# 🥷 Backslash-Security: using vibe-coding rules for Logging & Error Handling
_AGENT_SECRET_PATTERNS = [
    re.compile(r"\b[A-Z][A-Z0-9_]*_(?:API_)?KEY\s*[=:]\s*\S+"),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    ),
]


def _scrub_agent_prompt(text: str) -> str:
    """Redact credentials before persisting. Canonical first, local only as fallback.

    T-04 / S-02. This previously used `_AGENT_SECRET_PATTERNS` alone, and the
    2026-09-22 audit measured the gap: it missed **Slack tokens, JWTs and Google
    API keys** that `secret_scrubber` covers, while the docstring above it
    claimed prompts were scrubbed before storage.

    That is not shipped-but-unused code. This hook is registered as a
    `PreToolUse[Agent]` hook, so it runs on every delegation in a live session,
    and it writes to a GLOBAL, cross-project `agent_calls.json`.

    The local list is kept — but only as a fallback for the early-boot case where
    `llm_router` is not importable, which is the reason it exists. It is no longer
    the primary path, and it no longer silently defines what "scrubbed" means.
    """
    if not text:
        return text
    try:
        from llm_router.secret_scrubber import scrub_text

        return scrub_text(text)
    except Exception:  # noqa: BLE001 — a hook must never fail over redaction
        for pat in _AGENT_SECRET_PATTERNS:
            text = pat.sub("[REDACTED]", text)
        return text


_AGENT_CALLS_LEDGER_MAX_AGE_S = 30 * 24 * 3600  # 30 days


def _agent_calls_ledger_file() -> Path:
    return _router_home() / "agent_calls_ledger.jsonl"


def _append_agent_calls_ledger(entry: dict) -> None:
    """Append-only, 30-day-rolling companion to the 50-cap agent_calls.json.

    ``agent_calls.json`` is capped at 50 entries for its one real consumer
    (``hooks/agent-error.py``, which only ever wants the MOST RECENT call for
    fallback suggestions) and turns over within hours in an active session —
    it cannot answer "how much sub-agent traffic went where over the last 30
    days", which is exactly the question the NS3 Codex-delegation lever needs
    answered. This ledger keeps every row for 30 days, pruned by AGE instead
    of COUNT, without changing agent_calls.json's existing cap or behaviour.

    Fire-and-forget: a broken ledger must never break routing.
    """
    try:
        f = _agent_calls_ledger_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - _AGENT_CALLS_LEDGER_MAX_AGE_S
        kept: list[str] = []
        if f.exists():
            for line in f.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # A row with no readable timestamp is not "0 seconds old" — it is
                # unmeasured, and the honest behaviour for an unmeasured age is to
                # let it drop off the 30-day window rather than treating absence
                # as freshly-written (see lint_unknown_as_number.py: an absent
                # value compared against a threshold must not silently read as 0).
                ts = row.get("timestamp")
                if isinstance(ts, (int, float)) and ts >= cutoff:
                    kept.append(line)
        kept.append(json.dumps(entry))
        f.write_text("\n".join(kept) + "\n")
        try:
            os.chmod(f, 0o600)
        except OSError:
            pass
    except Exception as exc:
        try:
            from llm_router import failopen
            failopen.record("CHZ-FO-NS3-AGENT-CALLS-LEDGER", exc)
        except Exception:
            pass


def _north_star_ledger_file() -> Path:
    return _router_home() / "north_star_units.jsonl"


def _record_north_star_unit(lever: str, *, model: str, outcome: str, **meta) -> float | None:
    """Append one North-Star routing unit as a JSON line — the NS1-readable
    signal for this lever (``lever="agent_route_codex"``).

    Written for every decision this lever makes (delegated / unsuitable /
    budget_exhausted / codex_unavailable / codex_failed), not only successes,
    so "0 Codex calls in 30 days" (the NS3 baseline) becomes distinguishable
    from "the lever ran N times and every one was correctly declined".
    Fire-and-forget: a broken ledger must never break routing. Returns the row's ``ts``
    (the unit id the verifier joins on derives from it), or None when nothing was written.
    """
    try:
        entry = {
            "ts": time.time(),
            "lever": lever,
            "model": model,
            "outcome": outcome,
            "session_kind": _session_kind_of(meta.get("session_id") or _get_session_id()),
            **meta,
        }
        f = _north_star_ledger_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
        try:
            os.chmod(f, 0o600)
        except OSError:
            pass
        return entry["ts"]
    except Exception as exc:
        # This is the NS1-readable signal for the whole lever — a silent loss
        # here is the exact failure mode this feature exists to avoid (see
        # llm_router.failopen's own docstring: a caught exception is
        # information, and discarding it converts a known failure into an
        # unknown one).
        try:
            from llm_router import failopen
            failopen.record("CHZ-FO-NS3-NORTH-STAR-LEDGER", exc)
        except Exception:
            pass
    return None


def _session_kind_of(session_id: str | None) -> str | None:
    """KPI session tag (organic/research/harness/headless) or None if untagged."""
    try:
        from llm_router import session_kind
        return session_kind.kind_of(session_id)
    except Exception:
        return None


def _log_agent_call(subagent_type: str, prompt: str, decision: str) -> None:
    """Log agent call for error recovery tracking.

    Persists to ~/.llm-router/agent_calls.json with a rolling history of last 50 calls.
    Used by PostToolUse[Agent] hook to suggest fallbacks when agents fail.

    Secrets in the prompt are scrubbed before storage and the file is written
    owner-only (0o600) so pasted credentials can't leak to other local users.

    Also appends the same (scrubbed) entry to the 30-day append-only ledger
    (see ``_append_agent_calls_ledger``) so sub-agent routing decisions —
    including NS3 Codex delegation — stay measurable past the 50-call cap.
    """
    calls_file = _router_home() / "agent_calls.json"

    # Read existing history
    history = []
    try:
        data = json.loads(calls_file.read_text())
        history = data.get("calls", [])
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # Append new call (scrub secrets before truncating/storing)
    entry = {
        "timestamp": time.time(),
        "subagent_type": subagent_type,
        "prompt": _scrub_agent_prompt(prompt[:500]),  # scrub + truncate
        "decision": decision,
        "session_id": _get_session_id(),
        "session_kind": _session_kind_of(_get_session_id()),
    }
    history.append(entry)
    _append_agent_calls_ledger(entry)

    # Keep last 50 calls only
    history = history[-50:]

    # Write back owner-only.
    # 🥷 Backslash-Security: using vibe-coding rules for File Upload Security
    calls_file.parent.mkdir(parents=True, exist_ok=True)
    calls_file.write_text(json.dumps({
        "calls": history,
        "version": 1,
    }))
    try:
        os.chmod(calls_file, 0o600)
    except OSError:
        pass


# ── Agent cost estimation ───────────────────────────────────────────────────

# WP-03: hoisted out of _estimate_agent_cost (it was rebuilt on every call) and
# split by complexity. These are NOT model rates — they are whole-call USD
# budget guesses keyed by task shape, and none corresponds to any per-token
# price. The pricing lint flagged them anyway, because ("moderate","analyze")
# = 0.80 and ("complex","analyze") = 4.00 happen to spell the retired Haiku
# pair. Splitting by complexity keeps that coincidence out of one node so the
# lint stays credible; the numbers are unchanged.
#
# Worth stating now that they are visible: all ten are hand-estimated and have
# no derivation from real token pricing, so `budget_usd` is being enforced
# against invented figures. Making them real belongs with the escalation-budget
# work (WP-10), not here.
_SIMPLE_TASK_USD = {
    ("simple", "retrieval"): 0.15,
    ("simple", "query"): 0.30,
    ("simple", "code"): 0.20,
}
_MODERATE_TASK_USD = {
    ("moderate", "retrieval"): 0.30,
    ("moderate", "query"): 0.50,
    ("moderate", "code"): 1.00,
    ("moderate", "analyze"): 0.80,
}
_COMPLEX_TASK_USD = {
    ("complex", "code"): 3.00,
    ("complex", "analyze"): 4.00,
    ("complex", "research"): 2.50,
}
_AGENT_COST_ESTIMATES_USD = {
    **_SIMPLE_TASK_USD,
    **_MODERATE_TASK_USD,
    **_COMPLEX_TASK_USD,
}


def _estimate_agent_cost(complexity: str, task_type: str) -> float:
    """Estimate agent call cost in USD based on complexity and task type.
    
    Base rates (conservative upper estimates):
    - simple/retrieval: $0.15
    - simple/query: $0.30
    - simple/code: $0.20
    - moderate/retrieval: $0.30
    - moderate/query: $0.50
    - moderate/code: $1.00
    - moderate/analyze: $0.80
    - complex/code: $3.00
    - complex/analyze: $4.00
    - complex/research: $2.50
    
    Returns conservative estimate to avoid budget surprises.
    """
    # Default conservative estimate for unmapped types
    return _AGENT_COST_ESTIMATES_USD.get((complexity, task_type), 1.50)


def _initialize_session_budget() -> float:
    """Initialize session budget if not already done.

    Creates ~/.llm-router/session_budget.json with initial budget based on
    quota pressure. Called once per session to set up provisional tracking.

    Returns the initial budget in USD.
    """
    budget_file = _router_home() / "session_budget.json"

    # If already initialized this session, return existing
    if budget_file.exists():
        try:
            data = json.loads(budget_file.read_text())
            if data.get("session_id") == _get_session_id():
                return float(data.get("initial", 30.0))
        except (json.JSONDecodeError, ValueError):
            pass

    # Calculate initial budget based on quota pressure
    pressure = _get_claude_pressure()
    # Allocate 30% of available budget to agents this session
    # This prevents a single session from consuming entire weekly quota
    base_budget = 30.0
    allocated = base_budget * (1.0 - pressure)
    initial_budget = max(5.0, allocated)  # Minimum $5 always allocated

    # An empty HOME has no ~/.llm-router yet; 0700 matches the repo state-dir convention.
    budget_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    budget_file.write_text(json.dumps({
        "session_id": _get_session_id(),
        "initial": initial_budget,
        "remaining": initial_budget,
        "provisional_spend": 0.0,
        "timestamp": time.time(),
    }))

    return initial_budget


def _decrement_budget_provisional(estimated_cost: float) -> None:
    """Decrement remaining budget provisionally when agent is approved.

    This prevents multiple agents from each thinking they have budget available.
    Provisional spend will be reconciled against actual cost when agent completes.
    """
    budget_file = _router_home() / "session_budget.json"

    try:
        data = json.loads(budget_file.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        _initialize_session_budget()
        data = json.loads(budget_file.read_text())

    remaining = float(data.get("remaining", 30.0))
    provisional = float(data.get("provisional_spend", 0.0))

    # Decrement remaining by estimated cost
    new_remaining = max(0.0, remaining - estimated_cost)
    new_provisional = provisional + estimated_cost

    data["remaining"] = new_remaining
    data["provisional_spend"] = new_provisional
    data["timestamp"] = time.time()

    budget_file.write_text(json.dumps(data))


def _get_remaining_budget() -> float:
    """Get remaining session budget in USD.

    Priority:
      1. ~/.llm-router/session_budget.json (provisional tracking)
      2. Infer from usage.json (session % remaining)
      3. Conservative default $10 (assume 1/3 remaining)

    Returns a float >= 0.0 representing remaining budget in USD.
    """
    # Layer 1: Session budget file (tracking provisional spend)
    budget_file = _router_home() / "session_budget.json"
    try:
        data = json.loads(budget_file.read_text())
        if "remaining" in data:
            remaining = float(data.get("remaining", 0.0))
            return max(0.0, remaining)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # Layer 2: Infer from usage pressure
    session_pct = _get_claude_pressure()  # 0.0–1.0
    # Assume $30 typical session budget
    session_budget = 30.0
    spent = session_budget * session_pct
    remaining = max(0.0, session_budget - spent)
    return remaining


# ── Session pressure ─────────────────────────────────────────────────────────

def _get_claude_pressure() -> float:
    """Read Claude quota pressure from cache file or SQLite DB.

    Priority:
      1. ~/.llm-router/usage.json  — written by llm_update_usage, fastest
      2. ~/.llm-router/usage.db    — SQLite claude_usage table, authoritative
      3. Conservative default 0.3  — never assume unlimited quota when blind

    Returns a fraction 0.0–1.0.
    """
    # Layer 1: fast JSON cache
    usage_path = _router_home() / "usage.json"
    try:
        data = json.loads(usage_path.read_text())
        if "highest_pressure" in data:
            return float(data["highest_pressure"])
        session_pct = data.get("session_pct", 0.0) / 100.0
        weekly_pct = data.get("weekly_pct", 0.0) / 100.0
        return max(session_pct, weekly_pct)
    except Exception:
        pass

    # Layer 2: SQLite fallback — reads most recent claude_usage row
    db_path = _router_home() / "usage.db"
    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path), timeout=1)
        row = conn.execute(
            "SELECT messages_used, messages_limit FROM claude_usage "
            "ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row and row[1] and row[1] > 0:
            return min(1.0, row[0] / row[1])
    except Exception:
        pass

    # Layer 3: conservative default — don't assume full quota when blind
    return 0.3


def _is_pressure_stale(max_age_seconds: int = 1800) -> bool:
    """Return True if usage.json is missing or older than 30 minutes."""
    usage_path = _router_home() / "usage.json"
    if not usage_path.exists():
        return True
    return (time.time() - usage_path.stat().st_mtime) > max_age_seconds


# ── Classifiers ───────────────────────────────────────────────────────────────

def _is_retrieval_only(prompt: str) -> bool:
    """True if the task is pure file/symbol retrieval with no reasoning required."""
    has_retrieval = bool(_RETRIEVAL_INTENT.search(prompt))
    has_reasoning = bool(_REASONING_INTENT.search(prompt))
    # Approve only when clearly retrieval AND no analysis intent detected
    return has_retrieval and not has_reasoning


def _classify_complexity(prompt: str) -> str:
    # Explicit complex signals or very long prompt → complex
    if _COMPLEX_SIGNALS.search(prompt) or len(prompt) > 500:
        return "complex"
    # Only downgrade to simple if there are explicit simple signals
    # AND the prompt is genuinely short (don't let small prompts sneak by)
    if _SIMPLE_SIGNALS.search(prompt) and len(prompt) < 80:
        return "simple"
    return "moderate"


def _classify_task_type(prompt: str) -> str:
    """Return the best-matching task type for the subagent prompt."""
    scores: dict[str, int] = {}
    for task, pattern in _TASK_SIGNALS.items():
        matches = pattern.findall(prompt)
        scores[task] = len(matches)
    best = max(scores, key=scores.get)  # type: ignore[arg-type]
    return best if scores[best] > 0 else "analyze"


def _complexity_to_profile(complexity: str, session: float, sonnet: float, weekly: float) -> str:
    """Map complexity + per-bucket pressure to the appropriate routing profile.

    Cascade rule: higher pressure forces ALL lower complexity tiers external too.
      weekly/session ≥ 95% → everything external (global emergency)
      sonnet         ≥ 95% → simple + moderate external
      session        ≥ 85% → simple only external
    """
    all_external = weekly >= 0.95 or session >= 0.95
    if all_external:
        return "budget" if complexity == "simple" else "balanced"
    if sonnet >= 0.95:
        return "budget" if complexity == "simple" else "balanced"
    if complexity == "simple" and session >= 0.85:
        return "budget"
    return {"simple": "budget", "moderate": "balanced", "complex": "premium"}[complexity]


# ── Headless session guard ───────────────────────────────────────────────────
# 2026-09-28 incident: `claude -p "..." --model sonnet --output-format json`
# (a headless benchmark run) hit this hook's default routed-spawn path, which
# shelled out to `codex exec` for a Task-tool call. A ~40s task became 832s
# and the benchmark numbers were contaminated. Headless runs (scripts, CI,
# SDK callers) must never get a surprise external-process fan-out.
#
# SIGNAL, verified empirically (not assumed) on 2026-09-28 with a real
# `claude -p ... --output-format json` run that spawned a Task-tool subagent,
# observed via a throwaway diagnostic PreToolUse[Agent] hook:
#   - The hook's own stdin JSON payload carries NO entrypoint field at all.
#     Its top-level keys were: cwd, hook_event_name, permission_mode,
#     prompt_id, session_id, tool_input, tool_name, tool_use_id,
#     transcript_path. There is nothing to read here.
#   - The environment variable CLAUDE_CODE_ENTRYPOINT IS the signal: it was
#     "sdk-cli" for that headless `-p` run, versus "cli" for an ordinary
#     interactive session (confirmed against this process's own inherited
#     env). This also matches what Claude Code records in session
#     transcripts under the same field name for the same distinction.
# "claude-desktop" (the desktop app) is deliberately NOT treated as headless —
# a person is actively driving it, so a surprise Codex spawn there is a UX
# question, not a benchmark-contamination one.

def _headless_entrypoint() -> str:
    """CLAUDE_CODE_ENTRYPOINT, lower-cased and stripped. See guard note above."""
    return os.environ.get("CLAUDE_CODE_ENTRYPOINT", "").strip().lower()


def _is_headless_entrypoint(entrypoint: str) -> bool:
    """True for any programmatic entrypoint (sdk-cli, sdk-py, and any future
    sdk-* variant). False for "" (unset — never observed for a real Claude
    Code process; treated as interactive so a stripped environment doesn't
    silently disable routing) and for "claude-desktop" (see guard note)."""
    return entrypoint.startswith("sdk")


def _headless_route_override_enabled() -> bool:
    """LLM_ROUTER_AGENT_ROUTE_HEADLESS=on keeps agent-route active in headless
    sessions — an explicit opt-in for anyone who WANTS Codex/DIRECT routing
    from a script or SDK caller. Default off."""
    return os.environ.get("LLM_ROUTER_AGENT_ROUTE_HEADLESS", "off").strip().lower() in (
        "1", "on", "true", "yes")


# ── Main ─────────────────────────────────────────────────────────────────────

def _route_allowlist() -> set[str]:
    """Subagent types that BYPASS agent-routing (always approved).

    For agents that must do real tool-work — running tests, editing files,
    QA/validation, code review — where redirecting to an ``llm_*`` text call is
    not a substitute. ``Explore`` is always allowed separately.

    Configured via ``LLM_ROUTER_AGENT_ROUTE_ALLOW`` (comma-separated subagent_type
    values). Read from the environment first, then from ``~/.llm-router/.env`` so it
    takes effect without restarting the host. Example:
        LLM_ROUTER_AGENT_ROUTE_ALLOW=code-reviewer,qa,test-runner
    """
    vals = os.environ.get("LLM_ROUTER_AGENT_ROUTE_ALLOW", "").strip()
    if not vals:
        try:
            for line in (_router_home() / ".env").read_text().splitlines():
                line = line.strip()
                if line.startswith("LLM_ROUTER_AGENT_ROUTE_ALLOW="):
                    vals = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        except OSError:
            pass
    return {t.strip() for t in vals.split(",") if t.strip()}


# ── Subagent DIRECT execution (route the work onto a cheap model) ────────────

_COMPLEXITY_RANK = {"simple": 1, "moderate": 2, "complex": 3}


def _govern_run(subagent_type: str, provider: str, model: str,
                in_tok: int, out_tok: int, complexity: str) -> None:
    """Phase 3 — record a routed subagent run as a governed agents/ session.

    Each routed subagent becomes a first-class session in ~/.llm-router/sessions.db
    (visible via llm_router_agent_list / llm_router_agent_check_budget): budget cap = the
    Claude-equivalent baseline it would have spent, consumed = the actual external
    cost. The gap (cap − consumed) is the saving, now auditable at the governance
    layer in addition to the savings log. Fire-and-forget; never breaks routing.
    """
    if os.environ.get("LLM_ROUTER_SUBAGENT_GOVERNANCE", "on").strip().lower() in ("0", "off", "false", "no"):
        return
    try:
        from llm_router.agents.session import SessionStore
        from llm_router.hooks.savings_logger import _baseline_cost, _cost_for
    except Exception:
        return
    try:
        external = _cost_for(provider, model, in_tok, out_tok)
        baseline = _baseline_cost(complexity, in_tok, out_tok)
        cap = baseline if baseline > 0 else max(external, 1e-6)
        store = SessionStore()
        try:
            sess = store.create(
                agent_id=f"subagent:{subagent_type}", budget_usd=cap,
                framework="llm_router-subagent-route",
            )
            store.record_step(sess.session_id, cost_usd=min(external, cap))
            store.complete(sess.session_id)
        finally:
            store.close()
    except Exception:
        pass


def _model_pin_enabled() -> bool:
    return os.environ.get("LLM_ROUTER_SUBAGENT_MODEL_PIN", "on").strip().lower() not in ("0", "off", "false", "no")


def _emit_model_pin(tool_input: dict, model: str) -> None:
    """Phase 4 (Option-A) — approve the spawn but rewrite its model to a cheaper tier.

    Uses Claude Code PreToolUse input rewriting (`updatedInput` under
    `hookSpecificOutput`): the subagent still spawns with the full harness, just on a
    cheaper Claude tier. If the host build ignores `updatedInput`, the `allow` still
    holds and the spawn proceeds on the inherited model — graceful degradation.
    """
    json.dump({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {**tool_input, "model": model},
            "permissionDecisionReason": f"[llm_router] model pinned → {model} (lightweight subagent)",
        }
    }, sys.stdout)


# ── llm_router multi-agent (v0.7.0): ALLOW real subagent spawns (cheap tier + routing) ──
# Lets councils / parallel reviews actually run as subagents instead of being
# replaced by a single cheap call. Cost stays bounded via the model pin below and
# the depth circuit-breaker in main(). Default on; LLM_ROUTER_ALLOW_SUBAGENTS=off
# restores the legacy block-and-route behavior.
_SPAWN_MODEL = {"simple": "haiku", "moderate": "sonnet", "complex": "sonnet"}
_MODEL_RANK = {"haiku": 0, "sonnet": 1, "opus": 2, "fable": 2}


def _spawn_model(complexity: str, caller_model: str | None) -> str:
    """Pick the spawn tier by complexity, but only ever DOWNGRADE relative to a
    model the caller explicitly requested (never spawn a costlier tier)."""
    routed = _SPAWN_MODEL.get(complexity, "sonnet")
    if caller_model and _MODEL_RANK.get(caller_model, 9) < _MODEL_RANK.get(routed, 9):
        return caller_model
    return routed


def _allow_routed_spawn() -> bool:
    return os.environ.get("LLM_ROUTER_ALLOW_SUBAGENTS", "on").strip().lower() not in (
        "0", "off", "false", "no")


_SPAWN_ROUTING_NOTE = (
    "\n\n--- llm_router: routing (inherited) ---\n"
    "You are a llm_router-routed subagent. For substantive generation, analysis, "
    f"research, or code synthesis, prefer the llm_router MCP tools ("
    f"{route_tool('llm_query')} / {route_tool('llm_analyze')} / "
    f"{route_tool('llm_code')} / {route_tool('llm_research')}"
    ") over doing the heavy work directly; "
    "use your own file/search tools to gather context and apply concrete edits. "
    "Do NOT spawn further subagents."
)


def _qa_routing_on() -> bool:
    """True when the owner has re-enabled Q&A routing (LLM_ROUTER_QA_ROUTING=on).

    Same switch, same parsing as session-start.py / subagent-start.py /
    auto-route.py. Off by default: owner decision 2026-10-02, on measured
    quality — on hook-routed dev prompts, local-model answers were acceptable
    3-24% of the time vs 55-100% for Sonnet (two blind graders, calibration
    passed). _SPAWN_ROUTING_NOTE tells a spawned subagent to prefer routing
    its own substantive work to llm_router tools, which is the same policy
    applied one layer down — so it follows the same switch rather than a new
    one, and an owner who flips LLM_ROUTER_QA_ROUTING=on to restore the old
    Q&A-routing behaviour gets it restored consistently everywhere, not just
    in the main session.
    """
    return os.environ.get("LLM_ROUTER_QA_ROUTING", "off").strip().lower() in (
        "1", "on", "true", "yes",
    )


def _with_routing_note(tool_input: dict) -> dict:
    if not _qa_routing_on():
        return tool_input
    ti = dict(tool_input)
    p = ti.get("prompt") or ""
    if "llm_router: routing (inherited)" not in p:
        ti["prompt"] = p + _SPAWN_ROUTING_NOTE
    return ti


def _try_direct_subagent(
    prompt: str, task_type: str, complexity: str, session_id: str,
    subagent_type: str = "general-purpose", ledger_session_id: str | None = None,
) -> str | None:
    """Run the subagent's task on a routed cheap model instead of spawning Opus.

    Mirrors the main-session DIRECT path in auto-route.py: build a provider chain
    by complexity+pressure, execute (tool-loop for file work, single-shot for
    Q&A), log savings tagged ``claude_code_subagent``, and return the result text.

    Off by default: owner decision 2026-10-02, on measured quality — on
    hook-routed dev prompts, local-model answers were acceptable only 3-24% of
    the time vs 55-100% for Sonnet (two blind graders, calibration passed).
    ``LLM_ROUTER_SUBAGENT_DIRECT=on`` restores this path.

    Returns the routed output, or None to fall back to a real spawn. Fire-and-
    forget: any failure returns None so the subagent path stays robust.
    """
    if os.environ.get("LLM_ROUTER_SUBAGENT_DIRECT", "off").strip().lower() in ("0", "off", "false", "no"):
        return None
    # Only DIRECT-execute up to the configured complexity ceiling — bigger work
    # would block the hook too long and is better off as a real (cheaper-tier) spawn.
    max_c = os.environ.get("LLM_ROUTER_SUBAGENT_DIRECT_MAX_COMPLEXITY", "moderate").strip().lower()
    if _COMPLEXITY_RANK.get(complexity, 2) > _COMPLEXITY_RANK.get(max_c, 2):
        return None

    try:
        from llm_router.hooks.chain_builder import (
            build_chain,
            get_current_pressure,
            needs_claude_tools,
        )
        from llm_router.hooks.direct_executor import execute_agent, execute_chain
    except Exception:
        return None

    try:
        zone, _pct = get_current_pressure()
        chain = build_chain(complexity, zone, task_type)
        if not chain:
            return None
        if needs_claude_tools(prompt, task_type):
            result = execute_agent(prompt, chain, timeout=60)  # Ollama tool-loop
        else:
            result = execute_chain(prompt, chain, task_type, timeout=15)
    except Exception:
        return None

    if not result or not (getattr(result, "text", "") or "").strip():
        return None

    # Visible UI signal (Claude Code surfaces PreToolUse stderr to the user).
    if os.environ.get("LLM_ROUTER_ROUTE_BANNER", "on").strip().lower() not in ("0", "off", "false", "no"):
        try:
            sys.stderr.write(
                f"🎯 subagent routed → {result.model.provider}/{result.model.model} "
                f"· {task_type}/{complexity} · {result.latency_ms / 1000.0:.1f}s\n"
            )
        except Exception:
            pass

    # ── SAVINGS: same pipeline as main-session DIRECT, tagged for subagents ──
    try:
        from llm_router.hooks.savings_logger import log_direct_savings, log_direct_to_db
        log_direct_savings(
            result=result, task_type=task_type, complexity=complexity,
            session_id=session_id, host="claude_code_subagent",
        )
        # The ledger takes the hook payload's session id, never `session_id` above:
        # that one falls back to the machine-wide session_id.txt (last SessionStart
        # wins) and then to "unknown". log_direct_to_db stores only a valid id.
        log_direct_to_db(
            result=result, prompt=prompt, task_type=task_type,
            complexity=complexity, classifier_type="agent-route", session_id=ledger_session_id,
        )
    except Exception:
        pass

    _govern_run(subagent_type, result.model.provider, result.model.model,
                int(result.input_tokens or 0), int(result.output_tokens or 0), complexity)
    return result.text


def _log_cli_savings(content: str, provider: str, model: str, duration_sec: float,
                     prompt: str, task_type: str, complexity: str, session_id: str,
                     ledger_session_id: str | None = None) -> None:
    """Log savings for a CLI-delegated subagent run. CLI agents don't report token
    counts, so estimate from text length (chars/4), the same heuristic cc-usage-track
    uses. host=claude_code_subagent_cli keeps delegation savings separately attributable."""
    try:
        from llm_router.hooks.direct_executor import DirectResult, ModelSpec
        from llm_router.hooks.savings_logger import log_direct_savings, log_direct_to_db
        synthetic = DirectResult(
            text=content, model=ModelSpec(provider, model),
            latency_ms=int(duration_sec * 1000),
            input_tokens=max(1, len(prompt) // 4),
            output_tokens=max(1, len(content) // 4),
        )
        log_direct_savings(
            result=synthetic, task_type=task_type, complexity=complexity,
            session_id=session_id, host="claude_code_subagent_cli",
        )
        log_direct_to_db(  # the payload's session id: see _try_direct_subagent
            result=synthetic, prompt=prompt, task_type=task_type,
            complexity=complexity, classifier_type="agent-route-cli", session_id=ledger_session_id,
        )
    except Exception:
        pass


def _delegation_scope_root(cwd: str | None = None) -> str | None:
    """Project root to scope OKF context injection to for a CLI-delegated run.

    ``cwd`` is the hook payload's ``cwd`` field: Claude Code's statement of
    where the calling session is running, and the authoritative answer. The
    hook's own process cwd is only a fallback, because nothing guarantees the
    two agree (a hook can be spawned from elsewhere).

    A payload cwd inside a repo answers with that repo's root. One outside any
    repo answers None unless an environment override names a project -- never
    the directory itself, which would be a confidently wrong bucket (the same
    OKF-SCOPE-05 rule ``resolve_scope_or_none`` applies to the process cwd).

    ``working_dir`` doubles as the subprocess cwd, which is already correct
    here, so this only feeds ``context_root`` -- it never moves the subprocess.
    """
    try:
        from llm_router.semantic.scope import find_repo_root, resolve_scope_or_none
        if cwd:
            repo = find_repo_root(cwd)
            if repo is not None:
                return str(repo)
        # No payload cwd, or it is outside any repo: an environment override may
        # still name a project; otherwise the answer is None (OKF-SCOPE-05).
        root = resolve_scope_or_none(cwd=cwd or None)
        return str(root) if root is not None else None
    except Exception:
        return None


# ── Codex delegation: model, time, quota ────────────────────────────────────
# Measured 2026-10-01..03 (REPORT.txt, 17 real agent tasks through `codex exec`):
# gpt-6-astra passed 13/17, the same as Claude on the same tasks. Wall time per
# task: median 91s, 7 of 17 over 120s, max 223s -- so the old 120s default cut
# off 41% of the work Codex could do. The ChatGPT Plus window then ran dry after
# those 17 tasks ("...or try again at 11:33 PM."), and every later call failed.
_CODEX_DEFAULT_AGENT_MODEL = "gpt-6-astra"
_CODEX_FALLBACK_AGENT_MODEL = "gpt-5.5"

#: The registered PreToolUse hook timeout and the margin it leaves below that
#: for the delegation subprocess are install_hooks' numbers, imported rather
#: than copied so they cannot drift (a hook past its timeout is killed
#: SILENTLY, so raising one without raising the other drops the work with no
#: trace). The bare-literal fallback covers the rare case this module runs
#: somewhere install_hooks cannot be imported from; it must be kept equal to
#: install_hooks._AGENT_ROUTE_HOOK_TIMEOUT_SEC / _DELEGATION_MARGIN_SEC.
try:
    from llm_router.install_hooks import (
        _AGENT_ROUTE_HOOK_TIMEOUT_SEC as _HOOK_TIMEOUT_SEC,
        _DELEGATION_MARGIN_SEC,
    )
except Exception:  # noqa: BLE001 -- fall back to the same numbers, literally
    _HOOK_TIMEOUT_SEC = 320
    _DELEGATION_MARGIN_SEC = 20

#: Ceiling on the delegation timeout: the codex subprocess must finish, and
#: this hook must still have time to read its output and exit, before Claude
#: Code's wall-clock kill fires. Also the default -- measured: median 91s,
#: 7/17 real tasks over 120s, max 223s. Re-measured on the live ledger
#: (``delegated`` rows with duration_sec, 7 days to 2026-10-06, N=46): p50 53s,
#: p90 156s, p95 219s, max 260s, 6/46 over 120s -- so 300s clears the observed
#: max and 120s would have cut 13%. Override: LLM_ROUTER_SUBAGENT_CLI_TIMEOUT.
_CODEX_MAX_TIMEOUT_SEC = _HOOK_TIMEOUT_SEC - _DELEGATION_MARGIN_SEC
_CODEX_DEFAULT_TIMEOUT_SEC = _CODEX_MAX_TIMEOUT_SEC
_MIN_RUN_SEC = 15

#: A usage-limit message with no parseable reset time still benches Codex for
#: this long, rather than re-hitting a dead quota on every spawn.
_UNPARSEABLE_QUOTA_BENCH_SEC = 3600

_QUOTA_RE = re.compile(
    r"usage limit|hit your .{0,40}limit|rate.?limit|quota|too many requests|"
    r"credits? (?:have been )?(?:exhausted|depleted)",
    re.IGNORECASE,
)
_MODEL_UNAVAILABLE_RE = re.compile(
    r"model[^.]{0,80}(?:not found|not supported|does not exist|unavailable|"
    r"not available|no access|do not have access|invalid|unknown)|"
    r"(?:unknown|unsupported|invalid) model|"
    r"not (?:supported|available) when using codex",
    re.IGNORECASE,
)

#: Set on first delegation in this hook process; later attempts get only what is
#: left of it, so NS3 timing out and the Phase-2 path retrying cannot stack two
#: full timeouts past the hook's own kill time.
_delegation_started: float | None = None


def _codex_agent_model() -> str:
    """Model passed to ``codex exec -m``. LLM_ROUTER_CODEX_AGENT_MODEL overrides."""
    return (os.environ.get("LLM_ROUTER_CODEX_AGENT_MODEL", "").strip()
            or _CODEX_DEFAULT_AGENT_MODEL)


def _delegation_timeout() -> int:
    timeout = _CODEX_DEFAULT_TIMEOUT_SEC
    try:
        timeout = max(_MIN_RUN_SEC, int(os.environ.get(
            "LLM_ROUTER_SUBAGENT_CLI_TIMEOUT", str(_CODEX_DEFAULT_TIMEOUT_SEC))))
    except (TypeError, ValueError):
        pass
    if timeout > _CODEX_MAX_TIMEOUT_SEC:
        try:
            from llm_router import failopen
            failopen.record(
                "CHZ-FO-CODEX-TIMEOUT-CLAMPED",
                detail=(
                    f"LLM_ROUTER_SUBAGENT_CLI_TIMEOUT={timeout} exceeds the "
                    f"installed hook timeout minus margin "
                    f"({_CODEX_MAX_TIMEOUT_SEC}s); clamped to "
                    f"{_CODEX_MAX_TIMEOUT_SEC}s"
                ),
            )
        except Exception:  # noqa: BLE001 -- the clamp itself must not fail
            pass
        timeout = _CODEX_MAX_TIMEOUT_SEC
    return timeout


def _delegation_time_left(configured: int) -> int:
    """Seconds this hook process may still spend on external CLI delegation.

    Computed as ``configured - elapsed`` from a stored START instant, not as
    ``(start + configured) - now``: the latter rounds ``start + configured``
    to the nearest representable float BEFORE subtracting ``now``, and on the
    first call (where ``now == start``, same float, zero real elapsed time)
    that rounding can land a hair below the true sum -- e.g. 299.99999999997
    instead of exactly 300.0 -- which ``int()`` then truncates to 299. Sampled
    first-call failure rates at realistic `time.monotonic()` magnitudes (see
    PR description): up to ~1.9% in the worst binade, ~0.0002% at a typical
    multi-day uptime. Subtracting the two monotonic readings directly avoids
    forming that intermediate sum, so elapsed is exactly 0.0 on the first call
    (``now - now`` is always exact in IEEE 754) and `configured - elapsed`
    floors the same way the old (bug-free) arithmetic did for every later,
    genuinely-fractional call.
    """
    global _delegation_started
    now = time.monotonic()
    if _delegation_started is None:
        _delegation_started = now
    elapsed = now - _delegation_started
    # `time.monotonic()` is documented never to go backward, so `elapsed` is
    # never negative in practice and this upper clamp is not expected to ever
    # bind -- it is here because the budget handed to Codex must STRUCTURALLY
    # never exceed `configured`, not just as a consequence of relying on that
    # clock guarantee holding on every platform.
    return min(configured, max(0, int(configured - elapsed)))


def _codex_bench_until() -> float | None:
    """Epoch when Codex is usable again, or None when it is not benched."""
    try:
        from llm_router import provider_reset
        return provider_reset.get_provider_reset_until("codex")
    except Exception:
        return None  # fail open: an unreadable bench file must not stop delegation


def _fmt_clock(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%H:%M")


def _bench_after_quota_failure(provider_key: str, text: str) -> float | None:
    """Persist "``provider_key`` is out of quota until ...". Returns the reset
    epoch, or None when nothing was benched. ``provider_key`` is ``"codex"`` for
    the whole account or ``"codex:<model>"`` for one model."""
    try:
        from llm_router import provider_reset
        parsed = provider_reset.parse_reset_epoch(text)
        if parsed is not None:
            return provider_reset.note_provider_error(
                provider_key, RuntimeError(text), text=text)
        if _QUOTA_RE.search(text):
            until = time.time() + _UNPARSEABLE_QUOTA_BENCH_SEC
            # source="cli": the text is a Codex CLI run's output (KPI G4 bench log).
            if provider_reset.record_provider_reset(provider_key, until, reason=text, source="cli"):
                return until
    except Exception as exc:
        try:
            from llm_router import failopen
            failopen.record("CHZ-FO-AGENT-ROUTE-CODEX-BENCH", exc)
        except Exception:
            pass
    return None


def _note_codex_success(model: str) -> None:
    """KPI G4: a Codex call succeeded. If that model's bench (or the whole
    account's) was in force, the bench was wrong -- the model is logged. Never
    raises; a failure is counted, not swallowed."""
    try:
        from llm_router import provider_reset
        provider_reset.note_provider_success(f"codex:{model}")
        provider_reset.note_provider_success("codex")
    except Exception as exc:
        try:
            from llm_router import failopen
            failopen.record("CHZ-FO-AGENT-ROUTE-CODEX-SUCCESS-NOTE", exc)
        except Exception:
            pass


def _run_codex_agent(prompt: str, timeout: int, context_root: str | None):
    """Run one delegated task on Codex (see ``_run_codex_agent_inner``); any window
    slot still held when it returns -- no Codex call was made -- is handed back."""
    try:
        return _run_codex_agent_inner(prompt, timeout, context_root)
    finally:
        _release_reservation()


def _run_codex_agent_inner(prompt: str, timeout: int, context_root: str | None):
    """Run one delegated task on Codex with the configured model.

    Returns ``(res, status)``. ``status`` is ``"ok"``, ``"failed"``,
    ``"benched"`` (this call hit the usage limit and Codex is now benched until
    ``_codex_bench_until()``) or ``"no_time"`` (the shared wall-clock allowance
    is spent; ``res`` is None).

    The configured model is tried first. If it is out of quota or not offered to
    this account, gpt-5.5 is tried within the SAME time allowance. Quota on the
    fallback too means the whole account is dry, so Codex is benched; quota on
    the primary alone benches only that model.
    """
    import asyncio

    from llm_router.codex_agent import run_codex

    primary = _codex_agent_model()
    models = [primary]
    if primary != _CODEX_FALLBACK_AGENT_MODEL:
        models.append(_CODEX_FALLBACK_AGENT_MODEL)

    last = None
    for i, model in enumerate(models):
        is_last = i == len(models) - 1
        try:
            from llm_router import provider_reset
            if provider_reset.is_provider_reset_blocked(f"codex:{model}") and not is_last:
                continue  # this model is benched; go straight to the fallback
        except Exception:
            pass
        left = _delegation_time_left(timeout)
        if left < _MIN_RUN_SEC:
            _release_reservation()  # declined before any Codex call
            return last, "no_time"
        if not _spend_window_slot():  # every dispatch spends window quota
            return last, "failed"  # window exhausted for the fallback dispatch
        res = asyncio.run(run_codex(prompt, model=model, timeout=left, context_root=context_root))
        last = res
        if res and getattr(res, "success", False) and (res.content or "").strip():
            _note_codex_success(model)
            return res, "ok"
        text = str(getattr(res, "content", "") or "")
        if _QUOTA_RE.search(text):
            if not is_last:
                _bench_after_quota_failure(f"codex:{model}", text)
                continue
            if _bench_after_quota_failure("codex", text) is not None:
                return res, "benched"
            return res, "failed"
        if _MODEL_UNAVAILABLE_RE.search(text) and not is_last:
            continue
        return res, "failed"
    return last, "failed"


def _is_deep_reasoning(prompt: str) -> bool:
    """The repo's own deep-reasoning detector (reason_gate), False if unavailable."""
    try:
        from llm_router.reason_gate import needs_reasoning
        return bool(needs_reasoning(prompt))
    except Exception:
        return False


#: The window slot reserved by ``_codex_window_declines`` and not yet spent on a
#: Codex call: ``True`` when a slot is held, with its token in ``_held_token``.
#: The hook is a one-shot process, so a module global is the right scope (same
#: as ``_delegation_started``).
_held_reservation = False
_held_token = None


def _release_reservation() -> None:
    """Hand back a held slot that never reached a Codex call. No-op otherwise."""
    global _held_reservation, _held_token
    held, token = _held_reservation, _held_token
    _held_reservation, _held_token = False, None
    if held and token is not None:
        try:
            from llm_router import codex_window
            codex_window.release(token)
        except Exception:
            pass


def _codex_window_declines(path: str, prompt: str, subagent_type: str, task_type: str,
                           complexity: str, session_id: str) -> bool:
    """True when the rolling 5h Codex window says not to dispatch this one.

    An extra requirement on top of the caller's own gate, so it only narrows:
    when under half the window budget remains, only complex or deep-reasoning
    work is admitted; when it is spent, nothing is until a slot frees. A decline
    is recorded with its reason (outcome ``window_tight`` / ``window_exhausted``).

    When the delegation is admitted this ALSO reserves its slot, deciding and
    counting in one locked step (``codex_window.reserve``), so concurrent hook
    processes cannot all pass a check that only one of them should. The slot is
    spent by the first Codex call in ``_run_codex_agent``, or handed back by
    ``_release_reservation`` if no call is made.
    Fails open: a broken counter must not stop delegation.
    """
    global _held_reservation, _held_token
    try:
        from llm_router import codex_window
        adm = codex_window.reserve(
            complexity=complexity, deep_reasoning=_is_deep_reasoning(prompt))
    except Exception:
        return False
    if adm.allowed:
        _held_reservation, _held_token = True, adm.token
        return False
    _record_north_star_unit(
        "agent_route_codex", model="", outcome=f"window_{adm.tier}",
        subagent_type=subagent_type, task_type=task_type,
        complexity=complexity, session_id=session_id, path=path,
        reason=adm.reason[:200], window_used=adm.state.used,
        window_budget=adm.state.budget,
    )
    return True


def _spend_window_slot() -> bool:
    """Account one Codex dispatch. The first uses the slot reserved at admission;
    a later one (the gpt-5.5 fallback) reserves its own, refused only when the
    window is exhausted. Returns False when that dispatch must not happen."""
    global _held_reservation, _held_token
    if _held_reservation:
        _held_reservation, _held_token = False, None
        return True
    try:
        from llm_router import codex_window
        return codex_window.reserve(
            complexity="complex", deep_reasoning=True, enforce_tier=False).allowed
    except Exception:
        return True


def _note_codex_failure(path: str, status: str, res, subagent_type: str,
                        task_type: str, complexity: str, session_id: str) -> None:
    """Record why a Codex delegation produced nothing. Never silent: a quota
    hit becomes outcome ``codex_quota`` (and a stderr line naming the reset), any
    other failure ``codex_failed`` with the CLI's own message."""
    model = getattr(res, "model", "") if res else ""
    if status == "benched":
        until = _codex_bench_until()
        when = _fmt_clock(until) if until else "later"
        _record_north_star_unit(
            "agent_route_codex", model=model, outcome="codex_quota",
            subagent_type=subagent_type, task_type=task_type,
            complexity=complexity, session_id=session_id, path=path,
            reason=f"Codex usage limit hit; benched until {when}: "
                   f"{getattr(res, 'content', '')}"[:200],
            reason_code="quota",
        )
        if os.environ.get("LLM_ROUTER_ROUTE_BANNER", "on").strip().lower() not in ("0", "off", "false", "no"):
            try:
                sys.stderr.write(
                    f"⚠ Codex usage limit hit — sub-agents stay on Claude until {when}\n")
            except Exception:
                pass
        return
    # reason_code is the stable, queryable cause (never empty); reason is the
    # CLI's free text. 143 codex_failed rows in the 7 days to 2026-10-06 had
    # only the latter, so grouping them meant regexing prose.
    if status == "no_time":
        reason = "delegation wall-clock allowance already spent in this hook run"
        code = "no_time"
    elif res is None:
        reason, code = "no CodexResult returned", "no_result"
    else:
        reason = str(getattr(res, "content", "") or "")
        code = getattr(res, "reason_code", "") or "unclassified"
    _record_north_star_unit(
        "agent_route_codex", model=model, outcome="codex_failed",
        subagent_type=subagent_type, task_type=task_type,
        complexity=complexity, session_id=session_id, path=path,
        reason=(reason or code)[:200], reason_code=code,
    )


def _try_cli_delegation(
    prompt: str, task_type: str, complexity: str, session_id: str,
    subagent_type: str = "general-purpose",
    cwd: str | None = None, ledger_session_id: str | None = None,
) -> str | None:
    """Phase 2 — delegate bigger/tool-heavy subagent work to a real external agent
    CLI (Codex / Gemini CLI) that brings its own toolchain and runs on an external
    subscription (free from Claude quota). Returns the CLI output, or None to fall
    back. Bounded by LLM_ROUTER_SUBAGENT_CLI_TIMEOUT so the hook can't hang.

    Triggers only for tool-needing or complex tasks — a single cheap LLM call
    (the DIRECT tier) already covers simple/moderate Q&A.
    """
    if os.environ.get("LLM_ROUTER_SUBAGENT_CLI_DELEGATION", "on").strip().lower() in ("0", "off", "false", "no"):
        return None

    try:
        from llm_router.hooks.chain_builder import needs_claude_tools
    except Exception:
        needs_claude_tools = lambda *_a, **_k: False  # noqa: E731
    if not (needs_claude_tools(prompt, task_type) or complexity == "complex"):
        return None

    # Budget guard: don't delegate if the session's agent budget is spent.
    if _get_remaining_budget() <= 0:
        return None

    try:
        import asyncio

        from llm_router.codex_agent import is_codex_available
        from llm_router.gemini_cli_agent import is_gemini_cli_available, run_gemini_cli
    except Exception:
        return None

    timeout = _delegation_timeout()
    _scope_root = _delegation_scope_root(cwd)
    _benched_until = _codex_bench_until()
    if _benched_until is not None:
        _record_north_star_unit(
            "agent_route_codex", model="", outcome="benched",
            subagent_type=subagent_type, task_type=task_type,
            complexity=complexity, session_id=session_id,
            reason=f"Codex usage limit; benched until {_fmt_clock(_benched_until)}",
            path="cli_delegation",
        )
    _codex_ok = _benched_until is None and not _codex_window_declines(
        "cli_delegation", prompt, subagent_type, task_type, complexity, session_id)
    try:
        if _codex_ok and is_codex_available():
            provider = "codex"
            res, status = _run_codex_agent(prompt, timeout, _scope_root)
            if status != "ok":
                _note_codex_failure("cli_delegation", status, res, subagent_type,
                                    task_type, complexity, session_id)
        elif is_gemini_cli_available():
            provider = "gemini-cli"
            left = _delegation_time_left(timeout)
            if left < _MIN_RUN_SEC:
                return None
            res = asyncio.run(run_gemini_cli(prompt, timeout=left, context_root=_scope_root))
        else:
            return None
    except Exception:
        return None
    finally:
        _release_reservation()  # admitted for Codex but no Codex call made

    if not res or not getattr(res, "success", False) or not (res.content or "").strip():
        return None

    if os.environ.get("LLM_ROUTER_ROUTE_BANNER", "on").strip().lower() not in ("0", "off", "false", "no"):
        try:
            sys.stderr.write(
                f"🎯 subagent delegated → {provider}/{res.model} "
                f"· {task_type}/{complexity} · {res.duration_sec:.1f}s\n"
            )
        except Exception:
            pass

    _log_cli_savings(res.content, provider, res.model, res.duration_sec,
                     prompt, task_type, complexity, session_id, ledger_session_id)
    _govern_run(subagent_type, provider, res.model,
                max(1, len(prompt) // 4), max(1, len(res.content) // 4), complexity)
    return res.content


# ── NS3: suitable sub-agent spawns → Codex CLI, ahead of everything else ────
# ROOT CAUSE this section fixes: `_allow_routed_spawn()` below defaults to ON
# (LLM_ROUTER_ALLOW_SUBAGENTS default "on") and, for every non-Explore,
# non-allowlisted spawn, model-pins and returns UNCONDITIONALLY — before
# `_try_direct_subagent` or `_try_cli_delegation` (the only existing caller of
# Codex) are ever reached. That made `_try_cli_delegation` dead code in the
# default configuration and is why model_tracking.jsonl showed 0 Codex calls
# across 30 days despite this hook being "on". This section runs BEFORE that
# branch so a suitable spawn reaches Codex regardless of LLM_ROUTER_ALLOW_SUBAGENTS.

_CODEX_UNSUITABLE_SUBAGENT_TYPES = {
    # "fork" inherits the CALLER's full conversation context by definition
    # (see the Agent tool's own description). Codex CLI is a standalone
    # process with none of that context, so a fork spawn is never suitable
    # for external delegation regardless of task type.
    "fork",
}

# Real production spawns can classify read-mostly general-purpose delegation as
# task_type=query, which is Codex-suitable for the same reason as research,
# analyze, and code-reading prompts. The fork subagent-type exclusion above and
# the _MULTI_FILE_WRITE_SIGNALS write-heavy-prompt exclusion below still apply
# regardless of task_type.
_CODEX_SUITABLE_TASK_TYPES = {"research", "analyze", "code", "query"}

_MULTI_FILE_WRITE_SIGNALS = re.compile(
    r"\b(?:across (?:multiple|all|every|several) files?|multi-file|"
    r"refactor (?:the )?(?:entire|whole|full)(?:\s+(?:codebase|repo|repository|project))?|"
    r"rewrite (?:the )?(?:entire|whole|full)|"
    r"implement (?:this |it )?across|"
    r"edit (?:multiple|several|many) files|"
    r"large[- ]scale (?:refactor|migration)|"
    r"migrate (?:the )?(?:entire|whole|full)?\s*codebase)\b",
    re.IGNORECASE,
)


def _is_codex_suitable(subagent_type: str, task_type: str, prompt: str) -> bool:
    """Suitable = research / code-reading / analysis / read-mostly queries
    that (a) does not need the parent session's live conversational context
    and (b) is not a write-heavy, multi-file edit. Codex CLI runs as its own
    subprocess with its own toolchain — it can read/search/reason over files
    on disk, but it is not a substitute for a real multi-file edit loop and
    it cannot see anything the parent session holds only in memory."""
    if subagent_type in _CODEX_UNSUITABLE_SUBAGENT_TYPES:
        return False
    if task_type not in _CODEX_SUITABLE_TASK_TYPES:
        return False
    if _MULTI_FILE_WRITE_SIGNALS.search(prompt):
        return False
    return True


# Conservative default: 10% of the ~1000/day ChatGPT-Plus estimate that
# quota_balance.get_codex_pressure()/config.codex_daily_limit already use
# elsewhere in this codebase (that figure is itself a local, self-reported
# counter — Codex CLI exposes no queryable rate-limit API; see `codex doctor`
# and `~/.llm-router/codex_quota.json`). This lever gets its OWN dedicated
# counter file (below), not a share of that existing one, so sub-agent
# delegation cannot silently starve the Codex quota other llm_* routes draw
# from, and vice versa.
_CODEX_SUBAGENT_DEFAULT_DAILY_BUDGET = 100


def _codex_subagent_daily_budget() -> int:
    try:
        return max(0, int(os.environ.get(
            "LLM_ROUTER_AGENT_ROUTE_CODEX_DAILY_BUDGET",
            str(_CODEX_SUBAGENT_DEFAULT_DAILY_BUDGET),
        )))
    except (TypeError, ValueError):
        return _CODEX_SUBAGENT_DEFAULT_DAILY_BUDGET


def _codex_subagent_budget_file() -> Path:
    return _router_home() / "agent_route_codex_budget.json"


def _codex_subagent_budget_remaining() -> int:
    """Remaining Codex sub-agent delegations allowed today (UTC calendar day)."""
    budget = _codex_subagent_daily_budget()
    try:
        data = json.loads(_codex_subagent_budget_file().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return budget
    today = datetime.now(timezone.utc).date().isoformat()
    if data.get("date") != today:
        return budget
    try:
        count = int(data.get("count", 0))
    except (TypeError, ValueError):
        count = 0
    return max(0, budget - count)


def _codex_subagent_budget_increment() -> None:
    """Record one Codex sub-agent delegation attempt against today's budget.

    Incremented before the call is made (not only on success): a dispatched
    `codex exec` is a real request against the real ChatGPT-Plus rate limit
    whether or not it succeeds, and the budget is meant to bound requests
    made, not answers received.
    """
    f = _codex_subagent_budget_file()
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        data = json.loads(f.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    if data.get("date") != today:
        data = {"date": today, "count": 0}
    data["count"] = int(data.get("count", 0)) + 1
    data["date"] = today
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data))
    except OSError as exc:
        try:
            from llm_router import failopen
            failopen.record("CHZ-FO-NS3-CODEX-BUDGET-WRITE", exc)
        except Exception:
            pass


def _verify_enqueue_marker(res, session_id: str, ts) -> None:
    """Verifier PR C (SHADOW): after a delegation returned successfully, capture
    ``git diff HEAD`` + untracked files (in the dir Codex ran in: this process's cwd, since
    ``run_codex(working_dir=None)``) into a 0600 patch file and append a pending_verify marker.
    A truncated/capped run gets no marker. The detached ``verify_worker`` does the rest; nothing
    here waits on it. Fail-open: any error writes no marker and records a failopen code, and the
    turn is untouched."""
    try:
        from llm_router import verify_queue
        verify_queue.enqueue_from_run(
            os.getcwd(), session_id=session_id, ts=ts,
            truncated=bool(getattr(res, "truncated", False)),
            content=str(getattr(res, "content", "") or ""))
    except Exception as exc:  # noqa: BLE001
        try:
            from llm_router import failopen
            failopen.record("CHZ-FO-VERIFY-MARKER", exc)
        except Exception:  # noqa: BLE001
            pass


def _try_codex_subagent_delegation(
    prompt: str, task_type: str, complexity: str, subagent_type: str, session_id: str,
    cwd: str | None = None, ledger_session_id: str | None = None,
) -> str | None:
    """NS3 — delegate a SUITABLE sub-agent spawn to Codex CLI before Claude
    ever spawns anything for it.

    Bounded by a dedicated daily budget (see ``_codex_subagent_daily_budget``)
    so this lever cannot starve the Codex quota other llm_* routes already
    draw from. Every decision this function makes — delegated, unsuitable,
    budget-exhausted, unavailable, or a Codex failure — is recorded as a
    North Star unit (``lever="agent_route_codex"``, see
    ``_record_north_star_unit``) so this lever becomes measurable regardless
    of which branch it takes.

    Returns the Codex output (to be used verbatim as the subagent's result),
    or ``None`` to fall through to the existing model-pinned-spawn / DIRECT /
    CLI-delegation chain, unchanged.
    """
    # Documented carve-out (owner, 2026-10-02): unlike the local-model direct path and
    # the routing note, which are now opt-in, Codex delegation stays ON by default
    # (opt out with LLM_ROUTER_AGENT_ROUTE_CODEX=off); it is measured separately.
    if os.environ.get("LLM_ROUTER_AGENT_ROUTE_CODEX", "on").strip().lower() in (
        "0", "off", "false", "no"):
        return None  # feature fully disabled — no ledger row, nothing was attempted

    if not _is_codex_suitable(subagent_type, task_type, prompt):
        _record_north_star_unit(
            "agent_route_codex", model="", outcome="unsuitable",
            subagent_type=subagent_type, task_type=task_type,
            complexity=complexity, session_id=session_id,
        )
        return None

    remaining = _codex_subagent_budget_remaining()
    if remaining <= 0:
        _record_north_star_unit(
            "agent_route_codex", model="", outcome="budget_exhausted",
            subagent_type=subagent_type, task_type=task_type,
            complexity=complexity, session_id=session_id,
            reason=f"daily Codex sub-agent budget ({_codex_subagent_daily_budget()}/day) spent; falling back to Claude",
        )
        return None

    try:
        from llm_router.codex_agent import is_codex_available
    except Exception as e:
        _record_north_star_unit(
            "agent_route_codex", model="", outcome="codex_unavailable",
            subagent_type=subagent_type, task_type=task_type,
            complexity=complexity, session_id=session_id,
            reason=f"codex_agent import failed: {e}"[:200],
        )
        return None

    if not is_codex_available():
        _record_north_star_unit(
            "agent_route_codex", model="", outcome="codex_unavailable",
            subagent_type=subagent_type, task_type=task_type,
            complexity=complexity, session_id=session_id,
            reason="no Codex CLI binary found on this machine",
        )
        return None

    timeout = _delegation_timeout()

    # A benched Codex (usage limit, see provider_reset) is skipped BEFORE any
    # budget is reserved or process spawned, and the skip is recorded.
    benched_until = _codex_bench_until()
    if benched_until is not None:
        _record_north_star_unit(
            "agent_route_codex", model="", outcome="benched",
            subagent_type=subagent_type, task_type=task_type,
            complexity=complexity, session_id=session_id, path="ns3",
            reason=f"Codex usage limit; benched until {_fmt_clock(benched_until)}",
        )
        return None

    if _codex_window_declines("ns3", prompt, subagent_type, task_type, complexity, session_id):
        return None

    # Reserve budget before dispatch — see _codex_subagent_budget_increment docstring.
    _codex_subagent_budget_increment()

    try:
        res, status = _run_codex_agent(prompt, timeout, _delegation_scope_root(cwd))
    except Exception as e:
        _record_north_star_unit(
            "agent_route_codex", model="", outcome="codex_failed",
            subagent_type=subagent_type, task_type=task_type,
            complexity=complexity, session_id=session_id, path="ns3",
            reason=f"run_codex raised: {e}"[:200],
            reason_code="run_codex_raised",
        )
        return None

    if status != "ok":
        _note_codex_failure("ns3", status, res, subagent_type,
                            task_type, complexity, session_id)
        return None

    if os.environ.get("LLM_ROUTER_ROUTE_BANNER", "on").strip().lower() not in ("0", "off", "false", "no"):
        try:
            sys.stderr.write(
                f"🎯 subagent → Codex (NS3) · {res.model} · {task_type}/{complexity} "
                f"· {res.duration_sec:.1f}s · budget {remaining - 1}/{_codex_subagent_daily_budget()} left today\n"
            )
        except Exception:
            pass

    _log_cli_savings(res.content, "codex", res.model, res.duration_sec,
                      prompt, task_type, complexity, session_id, ledger_session_id)
    _govern_run(subagent_type, "codex", res.model,
                max(1, len(prompt) // 4), max(1, len(res.content) // 4), complexity)
    _delegated_ts = _record_north_star_unit(
        "agent_route_codex", model=res.model, outcome="delegated",
        subagent_type=subagent_type, task_type=task_type,
        complexity=complexity, session_id=session_id,
        duration_sec=res.duration_sec,
        **({"truncated": True} if getattr(res, "truncated", False) else {}),
        **({"reason_code": res.reason_code} if getattr(res, "reason_code", "") else {}),
    )
    _verify_enqueue_marker(res, session_id, _delegated_ts)
    return res.content


def main() -> None:
    _hl_mark_main()
    with _hl_phase("session_io"):
        try:
            hook_input = json.load(sys.stdin)
        except (json.JSONDecodeError, EOFError):
            sys.exit(0)  # approve: can't parse input
    try:
        from llm_router.hook_latency import set_session as _hl_set_session

        _hl_set_session(hook_input.get("session_id") if isinstance(hook_input, dict) else None)
    except Exception:  # noqa: BLE001 -- llm_router without set_session: no session on the row
        pass

    tool_name = hook_input.get("tool_name", "")
    if tool_name != "Agent":
        sys.exit(0)  # approve: not an Agent call

    tool_input = hook_input.get("tool_input", {})
    prompt = tool_input.get("prompt", "").strip()
    subagent_type = tool_input.get("subagent_type", "general-purpose")

    # ── Headless session guard — checked before anything else, including the
    # empty-prompt short-circuit, so a headless run never takes a routing
    # decision or writes session state (budget/depth files). See the guard
    # note above _headless_entrypoint().
    _entrypoint = _headless_entrypoint()
    if _is_headless_entrypoint(_entrypoint) and not _headless_route_override_enabled():
        _log_agent_call(
            subagent_type, prompt,
            f"skipped_headless:entrypoint={_entrypoint or 'unknown'}",
        )
        sys.exit(0)  # approve, no decision — headless session, routing skipped

    if not prompt:
        sys.exit(0)  # approve: nothing to classify

    # ── Initialize session budget if not already done ──────────────────────────
    with _hl_phase("budget_init"):
        _initialize_session_budget()

    # State is read once, lock-free. Nothing is written until the spawn is approved
    # (_commit_spawn: one locked read-modify-write, at most once per run). The pending
    # entry is committed for Explore/allowlist spawns too: SubagentStart fires for
    # those and must not claim another spawn's entry.
    _early_sid = _get_session_id()
    _pending_token = str(hook_input.get("tool_use_id") or "").strip() or uuid.uuid4().hex
    _state0 = _read_state(_early_sid)
    _child_depth0 = _caller_depth(hook_input, _state0) + 1

    # ── Always approve Explore subagents — they're pure retrieval ────────────
    if subagent_type == "Explore":
        _commit_spawn(_early_sid, _pending_token, _child_depth0, take_slot=False)
        _log_agent_call(subagent_type, prompt, "approved_explore")
        if _model_pin_enabled():  # Phase 4: lightweight read/search → Haiku, not Opus
            _emit_model_pin(tool_input, "haiku")
            return
        sys.exit(0)

    # ── Special rule: allowlisted subagent types bypass routing ──────────────
    # Agents that must do real tool-work (run tests, QA, edit files) where an
    # llm_* call is not a substitute. See LLM_ROUTER_AGENT_ROUTE_ALLOW.
    if subagent_type in _route_allowlist():
        _commit_spawn(_early_sid, _pending_token, _child_depth0, take_slot=False)
        _log_agent_call(subagent_type, prompt, "approved_allowlist")
        sys.exit(0)

    # ── Circuit breaker: real nesting depth, plus a concurrency runaway cap ────
    with _hl_phase("depth"):
        session_id = _early_sid
        state = _state0
        caller_depth = _child_depth0 - 1
        child_depth = _child_depth0
        in_flight = len(_live_slots(state))
        max_depth = _get_max_depth()
        max_concurrent = _get_max_concurrent()

    block_reason = None
    if child_depth > max_depth:
        block_reason = (
            f"[llm_router] Agent nesting limit: this agent is at depth {caller_depth}; "
            f"spawning would reach depth {child_depth}, over the limit of {max_depth} "
            f"(LLM_ROUTER_MAX_AGENT_DEPTH). Use llm_* MCP tools directly instead."
        )
    elif in_flight >= max_concurrent:
        block_reason = (
            f"[llm_router] Too many agents in flight: {in_flight}/{max_concurrent} "
            f"(LLM_ROUTER_MAX_CONCURRENT_AGENTS). Wait for some to finish."
        )

    if block_reason:
        # Active alert: a runaway breaker trip should page ops,
        # not just silently block. Guarded so the hook never breaks.
        # stdout is the hook's JSON decision channel — structlog's default
        # sink is stdout, so redirect any alert logging to stderr to keep
        # the decision payload parseable.
        try:
            import contextlib

            from llm_router.alerts import RUNAWAY_BREAKER_TRIP, emit_alert
            with contextlib.redirect_stdout(sys.stderr):
                emit_alert(
                    RUNAWAY_BREAKER_TRIP,
                    detail={"session_id": session_id, "nesting_depth": child_depth,
                            "max_depth": max_depth, "in_flight": in_flight,
                            "max_concurrent": max_concurrent},
                )
        except Exception:
            pass
        json.dump({"decision": "block", "reason": block_reason}, sys.stdout)
        return

    # ── Detect retrieval-only tasks ──────────────────────────────────────────
    if _is_retrieval_only(prompt):
        _commit_spawn(session_id, _pending_token, child_depth, take_slot=True)
        _log_agent_call(subagent_type, prompt, "approved_retrieval")
        if _model_pin_enabled():  # Phase 4: pure retrieval → Haiku, not Opus
            _emit_model_pin(tool_input, "haiku")
            return
        sys.exit(0)

    # ── Classify reasoning task ──────────────────────────────────────────────
    with _hl_phase("classify"):
        task_type = _classify_task_type(prompt)
        complexity = _classify_complexity(prompt)

    # ── NS3: suitable spawns → Codex CLI FIRST (external, free from Claude quota) ──
    # Must run before _allow_routed_spawn() below: that branch defaults to ON and
    # returns unconditionally, which is why Codex delegation was unreachable before
    # this change. See the NS3 docstring above _try_codex_subagent_delegation.
    with _hl_phase("codex_delegation"):  # routed model time when it runs
        _codex_delegated = _try_codex_subagent_delegation(
            prompt, task_type, complexity, subagent_type, session_id,
            cwd=hook_input.get("cwd"), ledger_session_id=hook_input.get("session_id"))
    if _codex_delegated is not None:
        _log_agent_call(subagent_type, prompt, "routed_codex_subagent")
        json.dump({
            "decision": "block",
            "reason": (
                "[llm_router] Subagent task was delegated to Codex CLI (external, "
                "ChatGPT subscription — free from Claude quota); budget and outcome "
                "logged (lever=agent_route_codex). Use this result directly as the "
                "subagent's output — do not re-do the work:\n\n" + _codex_delegated +
                "\n\n[NS1] agent_route_codex: result used verbatim as subagent output."
            ),
        }, sys.stdout)
        return

    # ── llm_router multi-agent: ALLOW a real spawn on a cheap tier + inherit routing ─
    # (depth breaker above already bounds nesting; cheap model bounds cost). This
    # is what makes councils / parallel reviews possible instead of collapsing
    # every subagent into a single cheap call.
    if _allow_routed_spawn():
        _commit_spawn(session_id, _pending_token, child_depth, take_slot=True)
        model = _spawn_model(complexity, tool_input.get("model"))
        _log_agent_call(subagent_type, prompt, "allowed_routed_spawn")
        _emit_model_pin(_with_routing_note(tool_input), model)
        return

    # ── NS4: don't route a class whose routed answers keep failing ───────────
    # Checked before attempting the routed chain at all, so a class the
    # breaker has opened never spends the latency on a doomed attempt.
    _qb_allowed = True
    _qb_decision = None
    try:
        from llm_router import quality_breaker as _quality_breaker
        _qb_decision = _quality_breaker.should_route("agent_route", task_type)
        _qb_allowed = _qb_decision.allowed
    except Exception:
        _qb_allowed, _qb_decision = True, None  # fail open — never block on a bug here

    # ── DIRECT subagent execution: route the work onto a cheap model ─────────
    # Instead of merely blocking with advice, actually run the task on the
    # routed chain and hand the result back as the subagent's output. Savings
    # are logged (host=claude_code_subagent). Falls through on any failure.
    with _hl_phase("direct_subagent"):  # routed model time when it runs
        _routed = (_try_direct_subagent(prompt, task_type, complexity, session_id, subagent_type,
                                        ledger_session_id=hook_input.get("session_id"))
                   if _qb_allowed else None)
    if not _qb_allowed:
        _log_agent_call(subagent_type, prompt,
                         f"breaker_open:{_qb_decision.reason if _qb_decision else 'agent_route'}")
    if _routed is not None:
        _log_agent_call(subagent_type, prompt, "routed_direct")
        json.dump({
            "decision": "block",
            "reason": (
                "[llm_router] Subagent task was executed by a routed model (not spawned); "
                "savings logged. Use this result directly as the subagent's output — "
                "do not re-do the work:\n\n" + _routed
            ),
        }, sys.stdout)
        return

    # ── Phase 2: CLI delegation for bigger/tool-heavy work ───────────────────
    # What DIRECT didn't take (tool tasks, complex work) goes to a real external
    # agent CLI (Codex / Gemini) running on an external subscription. Savings
    # logged (host=claude_code_subagent_cli). Falls through on any failure.
    with _hl_phase("cli_delegation"):  # routed model time when it runs
        _delegated = _try_cli_delegation(
            prompt, task_type, complexity, session_id, subagent_type, cwd=hook_input.get("cwd"),
            ledger_session_id=hook_input.get("session_id"))
    if _delegated is not None:
        _log_agent_call(subagent_type, prompt, "routed_cli_delegation")
        json.dump({
            "decision": "block",
            "reason": (
                "[llm_router] Subagent task was delegated to an external agent CLI "
                "(Codex/Gemini); savings logged. Use this result directly as the "
                "subagent's output — do not re-do the work:\n\n" + _delegated
            ),
        }, sys.stdout)
        return

    # ── Estimate cost for this agent call ───────────────────────────────────
    with _hl_phase("limits"):
        estimated_cost = _estimate_agent_cost(complexity, task_type)
        remaining_budget = _get_remaining_budget()
    
    # ── Check resource limits ───────────────────────────────────────────────
    # Soft limit: warn if cost > 80% of remaining budget (informational only)
    soft_limit = remaining_budget * SOFT_BUDGET_FACTOR
    if estimated_cost > soft_limit and remaining_budget > 0:
        # Could log warning here if we had stderr access
        # sys.stderr.write(f"[warning] Agent cost ${estimated_cost:.2f} exceeds soft limit (80% of remaining ${remaining_budget:.2f})\n")
        pass
    
    # Hard limit: block if cost exceeds remaining budget
    if estimated_cost > remaining_budget:
        result = {
            "decision": "block",
            "reason": (
                f"[llm_router] Agent would exceed session budget.\n\n"
                f"  Estimated cost: ${estimated_cost:.2f}\n"
                f"  Remaining budget: ${remaining_budget:.2f}\n\n"
                f"Use llm_* MCP tools instead (typically cheaper and more efficient)."
            ),
        }
        json.dump(result, sys.stdout)
        return
    
    # Hard limit: block if cost exceeds per-agent maximum
    if estimated_cost > AGENT_MAX_COST_USD:
        result = {
            "decision": "block",
            "reason": (
                f"[llm_router] Agent estimated cost exceeds per-agent limit.\n\n"
                f"  Estimated: ${estimated_cost:.2f}\n"
                f"  Per-agent limit: ${AGENT_MAX_COST_USD:.2f}\n\n"
                f"Task is too complex for a single agent. Break it into smaller steps\n"
                f"or use a series of llm_* MCP tool calls."
            ),
        }
        json.dump(result, sys.stdout)
        return

    # ── All limit checks passed: decrement budget provisionally ──────────────────
    # This tracks the estimated cost as "provisional spend" so multiple agents
    # don't all think they have budget available. Will be reconciled on completion.
    _decrement_budget_provisional(estimated_cost)

    # Log the blocked reasoning task call for error recovery tracking
    _log_agent_call(subagent_type, prompt, "blocked_reasoning")
    
    raw_pressure = _get_claude_pressure()  # legacy single value for display

    # Read per-bucket pressure from usage.json for accurate threshold decisions
    _p = {"session": raw_pressure, "sonnet": raw_pressure, "weekly": raw_pressure}
    _usage_path = _router_home() / "usage.json"
    try:
        _data = json.loads(_usage_path.read_text())
        def _f(k: str) -> float:
            v = float(_data.get(k, 0.0))
            return v / 100.0 if v > 1.0 else v
        _p = {"session": _f("session_pct"), "sonnet": _f("sonnet_pct"), "weekly": _f("weekly_pct")}
    except Exception:
        pass

    profile = _complexity_to_profile(complexity, _p["session"], _p["sonnet"], _p["weekly"])
    tool = tool_for_task(task_type)

    _model_hint = {
        "budget": "Gemini Flash / Groq (session pressure — cheap external)",
        "balanced": "GPT-4o / Gemini Pro (quota pressure — external)",
        "premium": "Opus via subscription (no API cost — quota available)",
    }
    model_hint = _model_hint.get(profile, profile)

    # Agentic model pin (v0.5.5): when LLM_ROUTER_AGENTIC_MODEL is set, the router
    # leads agentic/reasoning tasks with it — surface that in the hint so the
    # route indicator reflects the real preferred model.
    _agentic = os.environ.get("LLM_ROUTER_AGENTIC_MODEL", "").strip()
    if _agentic:
        model_hint = f"{_agentic} (agentic pin) → {model_hint}"

    pressure_note = ""
    if _p["weekly"] >= 0.95:
        pressure_note = f"  ⚠️  Weekly={_p['weekly']:.0%} — all tiers on external models.\n"
    elif _p["sonnet"] >= 0.95:
        pressure_note = f"  ⚠️  Sonnet={_p['sonnet']:.0%} — moderate/complex on external models.\n"
    elif _p["session"] >= 0.85:
        pressure_note = f"  ⚠️  Session={_p['session']:.0%} — simple tasks on external models.\n"

    # Build the block instruction
    # Use repr() for the prompt so newlines are visible and the instruction is copy-safe
    prompt_repr = prompt[:800] + ("..." if len(prompt) > 800 else "")

    stale_note = (
        f"\n  ⚠️  Usage data >30min old — quota thresholds may be inaccurate. "
        f"Run {route_tool('llm_check_usage')}.\n"
    ) if _is_pressure_stale() else ""
    # CHZ-SURF-01: head + pinned args, kept separate so the multi-line call below
    # renders as `llm(\n  task="analyze",\n  prompt=…\n)` and not `llm(task=…)(…)`.
    _call_head, _call_pinned = call_parts(tool)
    block_reason = (
        f"[AGENT-ROUTE] Subagent blocked — routing reasoning to cheap model.\n\n"
        f"  Task:       {task_type}/{complexity}\n"
        f"  Est. Cost:  ${estimated_cost:.2f} (remaining: ${remaining_budget:.2f})\n"
        f"  Profile:    {profile} → {model_hint}\n"
        f"  Quota:      session={_p['session']:.0%} sonnet={_p['sonnet']:.0%} weekly={_p['weekly']:.0%}\n"
        f"{pressure_note}"
        f"{stale_note}\n"
        f"ACTION REQUIRED — do this instead of spawning the subagent:\n\n"
        f"  1. If the task needs LOCAL FILE CONTENT:\n"
        f"     Use Read / Grep / Glob tools to extract the text.\n"
        f"     Embed the content directly in the prompt below.\n\n"
        f"  2. Call this MCP tool:\n\n"
        # CHZ-SURF-01: head and pinned args come from call_parts, NOT route_tool —
        # route_tool already embeds the args (llm(task="analyze")), so using it as
        # the head here would emit the uncallable `llm(task="analyze")(prompt=…)`.
        # `profile=` was ALSO dropped: it is not a parameter of llm_query/analyze/
        # code/research/generate OR of the `llm` door, so every call this block
        # printed was rejected for an unexpected keyword argument on every tier.
        # The profile is already reported above in the "Profile:" line.
        f"     {_call_head}(\n"
        + "".join(f"       {_a},\n" for _a in _call_pinned) +
        f'       prompt="""{prompt_repr}""",\n'
        f"     )\n\n"
        f"  3. Return the tool output as your response — no further work needed.\n\n"
        f"Cost saved: subagent would use Opus for reasoning; {route_tool(tool)} uses {model_hint}."
    )

    with _hl_phase("emit"):
        result = {
            "decision": "block",
            "reason": block_reason,
        }
        json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
