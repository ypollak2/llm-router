"""Format direct model responses for hook output.

Supports two render modes:
- "block": Returns {"decision": "block", "reason": text} — zero cost, warning-styled in TUI
- "echo":  Returns {"decision": "approve"} + additionalContext — costs 1 turn, injected into agent context

Current Claude Code uses `additionalContext` exclusively for UserPromptSubmit
hookSpecificOutput. The older `contextForAgent` key is no longer honored — it
silently dropped on the floor, which broke directive injection until 2026-06-06.
"""

from __future__ import annotations

import os

from llm_router.hooks.direct_executor import DirectResult

# Render mode: "block" (free, warning-styled), "echo" (1 turn, normal text),
# or "auto" (P1 truthful-routing default: block when the draft answers a
# self-contained prompt — the only case that actually saves a Claude turn —
# and fall back to advisory echo otherwise). Resolution of "auto" happens at
# the call site in auto-route.py, which has the prompt + classifier in scope.
RENDER_MODE = os.environ.get("LLM_ROUTER_RENDER_MODE", "auto").lower()


def _format_latency(latency_ms: int) -> str:
    """Honest latency display.

    A measured sub-millisecond call (e.g. a localhost stub) must be
    distinguishable from a fabricated zero, so 0 renders as "<1ms" rather
    than "0ms" (audit P4: never display a value indistinguishable from
    the old hardcoded-zero bug).
    """
    if latency_ms < 1:
        return "<1ms"
    if latency_ms < 1000:
        return f"{latency_ms}ms"
    return f"{latency_ms / 1000:.1f}s"


def format_direct_response(result: DirectResult, task_type: str, complexity: str) -> str:
    """Format a DirectResult for user display (used in block mode).

    Shows the response directly, with a compact metadata footer.
    """
    model_label = f"{result.model.provider}/{result.model.model}"
    tier = _tier_label(result.model.provider)
    latency = _format_latency(result.latency_ms)

    tokens = f"{result.input_tokens + result.output_tokens} tokens" if result.input_tokens + result.output_tokens > 0 else "0 tokens used"
    metadata = f"[{model_label}] {tier} | {task_type}/{complexity} | {latency} | {tokens}"

    # §2.5 honesty: only claim "context-free" when the call really was.
    if getattr(result, "history_turns", 0) > 0:
        ctx_note = (
            f"⚠ Unverified draft from a routed model (saw the last "
            f"{result.history_turns} conversation turn(s), but NO access to your "
            "files/tools) — verify before trusting:\n\n"
        )
    else:
        ctx_note = (
            "⚠ Unverified draft from a context-free model (no access to your "
            "files/history) — verify before trusting:\n\n"
        )
    return (
        f"{ctx_note}"
        f"{result.text}\n\n"
        f"{metadata}"
    )


def format_echo_context(result: DirectResult, task_type: str, complexity: str) -> str:
    """Format a DirectResult as an additionalContext directive for Claude (echo mode).

    Uses additionalContext (the only key Claude Code currently honors for
    UserPromptSubmit injection). The framing is cooperative (explains the
    goal, permits corrections) rather than adversarial — earlier versions
    used "OVERRIDE ALL OTHER INSTRUCTIONS / Do NOT acknowledge" wording
    that matched prompt-injection patterns and Claude's safety training
    resisted it. See docs/decisions.md 2026-05-27.
    """
    model_label = f"{result.model.provider}/{result.model.model}"
    tier = _tier_label(result.model.provider)
    latency = _format_latency(result.latency_ms)
    tokens = f"{result.input_tokens + result.output_tokens} tokens" if result.input_tokens + result.output_tokens > 0 else "0 tokens used"
    metadata = f"[{model_label}] {tier} | {task_type}/{complexity} | {latency} | {tokens}"

    route_prefix = f"🎯 LLM Router routed → {model_label} · {task_type}/{complexity} · {latency} · {tokens}"
    # Quota-saved metric: append the cumulative weekly + 5h counterfactual
    # in subscription-percentage-point terms when the savings are
    # non-trivial (≥0.5 pp). Computed best-effort; any failure is silent
    # so the routing notice never breaks because the metric DB is
    # unreachable.
    try:
        from llm_router.quota_savings import compute_quota_savings
        _snap = compute_quota_savings()
        if _snap is not None and _snap.is_meaningful():
            route_prefix = f"{route_prefix} · {_snap.short_form()}"
    except Exception:
        pass
    # Per-provider tier hint: for subscription routes (Claude), show
    # weekly + 5h quota remaining; for API routes (Gemini, OpenAI,
    # Codex), show the 30-day rolling spend on the routed provider.
    # Free / local providers (Ollama) emit nothing.
    try:
        from llm_router.quota_savings import provider_route_hint
        _hint = provider_route_hint(result.model.provider)
        if _hint:
            route_prefix = f"{route_prefix} · {_hint}"
    except Exception:
        pass
    return (
        f"ROUTING NOTICE — this prompt was classified as {task_type}/{complexity}. A cheap "
        f"model ({model_label}, {tier}, {latency}, {tokens}) drafted a candidate answer to "
        f"conserve your Claude subscription quota.\n\n"
        + (
            f"IMPORTANT — this draft was produced with the last {result.history_turns} "
            "conversation turn(s) as context, but WITHOUT access to your files, codebase, "
            "tools, or shell. Treat it as an UNVERIFIED hint, never as fact. Decide:\n"
            if getattr(result, "history_turns", 0) > 0 else
            "IMPORTANT — this draft was produced WITHOUT access to your files, codebase, tools, "
            "shell, or this conversation's history. Treat it as an UNVERIFIED hint, never as "
            "fact. Decide:\n"
        ) +
        "  - If the question is fully self-contained (general knowledge, no reference to the "
        "user's code/files/project/data or earlier turns) AND you can confirm the draft is "
        "correct: deliver it (lightly corrected), and begin your reply with this exact line "
        "then a blank line:\n"
        f"      {route_prefix}\n"
        "  - If the answer depends on ANYTHING the draft model could not see — the user's "
        "files, repo, prior conversation, current state, tool output — IGNORE the draft "
        "entirely and answer normally from real context. Do NOT relay it, and do NOT prefix "
        "the routed line (you did not route).\n"
        "  - If unsure whether the draft is trustworthy, discard it and answer from context. "
        "Correctness outranks the token saving.\n\n"
        "───── UNVERIFIED DRAFT (no context — verify or discard) ─────\n"
        f"{result.text}\n"
        "───── END UNVERIFIED DRAFT ─────\n"
        f"Source: {metadata}"
    )


def build_echo_output(result: DirectResult, task_type: str, complexity: str) -> dict:
    """Build the full hook output dict for echo mode (uses additionalContext)."""
    context = format_echo_context(result, task_type, complexity)
    return {
        "decision": "approve",
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }


def build_block_output(result: DirectResult, task_type: str, complexity: str) -> dict:
    """Build the full hook output dict for block mode."""
    formatted = format_direct_response(result, task_type, complexity)
    return {"decision": "block", "reason": formatted}


def _tier_label(provider: str) -> str:
    """Return a human-readable tier label."""
    tiers = {
        "ollama": "[FREE/LOCAL]",
        "codex": "[FREE/SUB]",
        "gemini": "[API]",
        "openai": "[API]",
    }
    return tiers.get(provider, "[UNKNOWN]")
