"""SubagentStart hook — inject routing context into every new agent's initial messages.
# llm_router-hook-version: 5

Fires once when Claude spawns an agent (Agent tool call completes the PreToolUse
gate and runAgent() starts). The hook's additionalContext is prepended to the
agent's initialMessages so the agent is routing-aware from its very first turn.

Key differences from auto-route (UserPromptSubmit):
  - No prompt text available — cannot classify the task.
  - Cannot block — runAgent.ts only reads additionalContexts, no blocking path.
  - Output field MUST be "additionalContext" (not "contextForAgent") because
    runAgent.ts reads hookResult.additionalContexts directly, it never shows
    raw stdout to the agent.

Hook input:
  { "hook_event_name": "SubagentStart", "agent_id": "...", "agent_type": "..." }

Hook output:
  { "hookSpecificOutput": { "hookEventName": "SubagentStart", "additionalContext": "..." } }

Skips:
  - Explore agents (agent_type == "Explore") — pure retrieval, routing context is noise.
  - When usage.json is missing — exits cleanly, agent starts without context.
"""

from __future__ import annotations

import json
import os
import sys
import time

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

        _hook_latency.begin("subagent-start", "SubagentStart", _HOOK_T0)
    except ImportError:
        pass  # llm_router is not importable on this host: no recorder, no row
    except Exception as _hl_exc:  # noqa: BLE001 -- timing must never break the hook
        import sys as _hl_sys

        print(f"llm-router: hook latency not recorded ({type(_hl_exc).__name__})", file=_hl_sys.stderr)

# .env -> os.environ for this process (llm_router.env_loader). The real
# environment wins; without the package this is a no-op, as it always was.
try:
    from llm_router.env_loader import load_dotenv_files as _apply_dotenv
    _apply_dotenv()
except Exception:
    pass


def _qa_routing_on() -> bool:
    """True when the owner has re-enabled Q&A routing (LLM_ROUTER_QA_ROUTING=on).

    Same switch, same parsing as session-start.py and auto-route.py. Off by
    default since 2026-10-01: questions and analysis are answered directly.
    """
    return os.environ.get("LLM_ROUTER_QA_ROUTING", "off").strip().lower() in (
        "1", "on", "true", "yes",
    )


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
    """(route_tool, route_call, route_call_with_complexity) from llm_router.tool_surface.

    Falls back to the stdlib-only copy the installer drops next to the hooks, then
    to the in-repo source, then to identity (correct only for tier `off`).
    """
    try:
        from llm_router.tool_surface import (
            route_call,
            route_call_with_complexity,
            route_tool,
        )
        return route_tool, route_call, route_call_with_complexity
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
            return _mod.route_tool, _mod.route_call, _mod.route_call_with_complexity
    except Exception:  # noqa: BLE001 — a broken support module must not kill the hook
        pass
    return (
        lambda n, **k: n,
        lambda n, *a, **k: (f"{n}({', '.join(a)})" if a else n),
        lambda n, c, *a, **k: f"{n}(complexity='{c}'" + ("".join(', ' + x for x in a)) + ")",
    )


route_tool, route_call, route_call_with_complexity = _load_tool_surface_fns()


# ── Pressure reading ──────────────────────────────────────────────────────────

def _read_pressure() -> dict[str, float]:
    """Read per-bucket Claude subscription pressure from usage.json.

    Returns fractions 0.0–1.0 for each bucket. Defaults to 0.0 on any error
    (conservative: assume no pressure when data is missing).
    """
    usage_path = _router_home() / "usage.json"
    try:
        data = json.loads(usage_path.read_text())

        def _norm(k: str) -> float:
            v = float(data.get(k, 0.0))
            return v / 100.0 if v > 1.0 else v

        return {
            "session": _norm("session_pct"),
            "sonnet":  _norm("sonnet_pct"),
            "weekly":  _norm("weekly_pct"),
        }
    except Exception:
        return {"session": 0.0, "sonnet": 0.0, "weekly": 0.0}


def _is_pressure_stale(max_age_seconds: int = 1800) -> bool:
    """Return True if usage.json is missing or older than 30 minutes."""
    usage_path = _router_home() / "usage.json"
    if not usage_path.exists():
        return True
    return (time.time() - usage_path.stat().st_mtime) > max_age_seconds


def _pressure_status(p: dict[str, float]) -> str:
    """Classify overall pressure into a human-readable status label."""
    if p["weekly"] >= 0.95 or p["session"] >= 0.95:
        return "CRITICAL"
    if p["sonnet"] >= 0.95 or p["session"] >= 0.85:
        return "HIGH"
    if p["session"] >= 0.60 or p["sonnet"] >= 0.70:
        return "MEDIUM"
    return "LOW"


def _claim_nesting_depth(payload: dict) -> None:
    """Register this new agent's nesting depth for agent-route.py's breaker.

    agent-route.py (PreToolUse[Agent]) queues the depth the child will have; the
    oldest queued entry is claimed here, where the child's agent_id is first known.
    Same session id and file as agent-route.py. Never raises.
    """
    try:
        import re
        agent_id = str(payload.get("agent_id") or "").strip()
        if not agent_id:
            return
        sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
        if not sid:
            try:
                sid = (_router_home() / "session_id.txt").read_text().strip()
            except OSError:
                sid = "unknown"
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", sid) or "unknown"
        path = _router_home() / f"agent_depth_{safe}.json"
        if not path.exists():
            return  # no breaker state for this session: nothing was queued
        lock_fh = None
        try:
            import fcntl
            lock_fh = open(os.open(f"{path}.lock", os.O_RDWR | os.O_CREAT, 0o600), "a+")
            deadline = time.monotonic() + 0.25  # hook latency budget, then fail open
            while True:
                try:
                    fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.002)
        except Exception as exc:  # noqa: BLE001
            print(f"llm-router: agent breaker state lock unavailable ({type(exc).__name__}); "
                  f"continuing unlocked", file=sys.stderr)
            if lock_fh is not None:
                lock_fh.close()
                lock_fh = None
        try:
            data = json.loads(path.read_text())
            now = time.time()
            pending = [p for p in data.get("pending", []) if isinstance(p, list) and len(p) >= 2
                       and now - float(p[0]) < 120.0]
            if not pending:
                return
            depth = int(pending.pop(0)[1])  # FIFO: oldest entry
            agents = data.get("agents") if isinstance(data.get("agents"), dict) else {}
            agents[agent_id] = depth
            data["agents"] = dict(list(agents.items())[-200:])
            data["pending"] = pending
            import uuid
            tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(data))
            os.replace(tmp, path)
        finally:
            if lock_fh is not None:
                lock_fh.close()
    except FileNotFoundError:
        return  # no breaker state for this session: nothing was queued
    except Exception as exc:  # noqa: BLE001 -- never break agent start; say so on stderr
        print(f"llm-router: nesting depth not recorded ({type(exc).__name__})", file=sys.stderr)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    agent_type = payload.get("agent_type", "")

    _claim_nesting_depth(payload)

    # Explore agents are pure retrieval — routing context adds noise, not value.
    if agent_type == "Explore":
        sys.exit(0)

    p = _read_pressure()
    status = _pressure_status(p)

    if _qa_routing_on():
        # Routing table summary — mirrors CLAUDE.md and auto-route logic.
        if status in ("LOW", "MEDIUM"):
            # The proxy's opus tier, never a literal (plan v16 P0.2).
            try:
                from llm_router.proxy.tiers import tier_model
                opus_id = tier_model("opus") or "opus"
            except ImportError:
                opus_id = "opus"
            routing_rules = (
                "simple→Haiku (/model claude-haiku-4-5-20251001) | "
                "moderate→Sonnet (current) | "
                f"complex→Opus (/model {opus_id}) | "
                f"research→{route_tool('llm_research')} MCP tool"
            )
        else:
            # HIGH / CRITICAL — subscription pressure exceeded, use external providers
            routing_rules = (
                f"simple→{route_tool('llm_query')} (external) | "
                f"moderate→{route_tool('llm_analyze')} (external) | "
                f"complex→{route_tool('llm_code')} (external) | "
                f"research→{route_tool('llm_research')} (external)"
            )
        directive_note = (
            "\nYour own Agent tool calls are intercepted by the routing hook — "
            "respect routing directives."
        )
    else:
        # Owner decision 2026-10-01: questions and analysis are not routed, at any
        # pressure level, so the agent is not taught an llm() table it must not use.
        routing_rules = "complex→Opus | questions and analysis: answer directly"
        directive_note = ""

    stale_note = (
        f"\n⚠️  Usage data >30min old — routing thresholds may be inaccurate. "
        f"Run {route_tool('llm_check_usage')}."
    ) if _is_pressure_stale() else ""
    context = (
        f"[llm_router] Routing context for this agent:\n"
        f"Pressure: session={p['session']:.0%} sonnet={p['sonnet']:.0%} "
        f"weekly={p['weekly']:.0%} | {status}\n"
        f"Rules: {routing_rules}"
        f"{directive_note}"
        f"{stale_note}"
    )

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "SubagentStart",
                "additionalContext": context,
            }
        },
        sys.stdout,
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
