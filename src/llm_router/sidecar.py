"""Sidecar pre-execution — run deterministic ops BEFORE Claude sees the prompt.

Background
----------
The dominant quota consumer in a coding session isn't routing — it's
Claude executing local tools (Read, Bash, Grep) to gather context that
the user could have pre-fetched. When a user asks "show me my routing
today", Claude has to:

1. Decide to run a SQL query
2. Generate the SQL
3. Call Bash to run it
4. Parse the output
5. Format the response

Steps 1–4 are deterministic given the prompt shape. Pre-executing them
in a hook + injecting the results into Claude's context lets Claude
skip straight to step 5, saving the tool-call overhead AND the
reasoning-between-tool-calls cycles.

This module is **opt-in** behind ``LLM_ROUTER_SIDECAR_PREFETCH=1`` (default
off) because pre-executing the wrong thing is a footgun. The v1
handlers below are read-only, allowlisted, and time-capped — adding a
write-capable handler should require a separate review.

Architecture
------------
``classify(prompt)`` runs LLM Router's existing introspection detector
plus the patterns below. If a handler matches, ``execute(handler,
prompt)`` runs it and returns a ``PreExecutionResult`` carrying the
data + a short cost summary. Callers (the UserPromptSubmit hook) wire
the result into the ``additionalContext`` field of the SessionStart
response so Claude sees it as a system message before reasoning.

Handlers
--------
* :func:`_handle_routing_distribution` — "show me my routing today" →
  aggregates ``routing_decisions`` from ``~/.llm-router/usage.db`` and
  returns a tier histogram + cost summary.
* :func:`_handle_git_status` — "what's the git status" → runs
  ``git status --short`` and returns the output. Read-only.
* :func:`_handle_recent_commits` — "show me my recent commits" →
  runs ``git log --oneline -10``. Read-only.

Failure mode is always silent: returns ``None`` if the handler can't
satisfy the prompt, and the hook falls through to normal routing.
A buggy handler must never block the user's prompt from reaching
Claude.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

__all__ = [
    "PreExecutionResult",
    "is_enabled",
    "classify",
    "execute",
    "HANDLERS",
]


def is_enabled() -> bool:
    """Opt-in switch. Default OFF — pre-executing the wrong thing is a
    footgun, so users must explicitly turn this on after reviewing the
    handler allowlist."""
    return os.environ.get("LLM_ROUTER_SIDECAR_PREFETCH", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


@dataclass(frozen=True)
class PreExecutionResult:
    """One sidecar execution's outcome.

    * ``handler`` — short name for telemetry (which pattern matched).
    * ``context`` — the text that gets injected into Claude's context.
      Pre-formatted as markdown so Claude reads it cleanly.
    * ``duration_ms`` — how long the sidecar took; visible in the
      injection header so users can see if a handler is misbehaving.
    """

    handler: str
    context: str
    duration_ms: int


# ── Pattern matchers ────────────────────────────────────────────────────


_ROUTING_DISTRIBUTION_RE = re.compile(
    r"\b(show me|list|how many|distribution of|tally|"
    r"what did i|what's the breakdown)\b.*\b(routings?|routes?|"
    r"decisions?)\b",
    re.IGNORECASE,
)
_GIT_STATUS_RE = re.compile(
    r"\b(show|what'?s|what is|tell me|how is)\b.*\bgit\s+status\b",
    re.IGNORECASE,
)
_RECENT_COMMITS_RE = re.compile(
    r"\b(show me|list|what are)\b.*\b(recent|last|my)\s+commits?\b",
    re.IGNORECASE,
)


# ── Handlers ────────────────────────────────────────────────────────────


def _handle_routing_distribution(_prompt: str) -> str | None:
    """Aggregate today's routing decisions, return a markdown summary.

    Skips backfilled sidecars (they don't reflect real traffic) and
    summarises by tier, model, and cost so the user gets the same
    answer they'd otherwise spend several tool calls building.
    """
    db = Path.home() / ".llm-router" / "usage.db"
    if not db.is_file():
        return None
    try:
        conn = sqlite3.connect(str(db))
        rows = conn.execute(
            "SELECT complexity, final_model, COALESCE(cost_usd,0) "
            "FROM routing_decisions "
            "WHERE date(timestamp,'localtime') = date('now','localtime') "
            "  AND COALESCE(reason_code,'') != 'sidecar_backfill'"
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        try:
            conn.close()
        except UnboundLocalError:
            pass

    if not rows:
        return ("**Routing distribution (today)**: no decisions recorded yet — "
                "the router hasn't been invoked for this day.")

    tiers: Counter = Counter()
    models: Counter = Counter()
    total_cost = 0.0
    for complexity, model, cost in rows:
        tiers[complexity or "unknown"] += 1
        models[model or "unknown"] += 1
        total_cost += float(cost or 0.0)

    lines = [
        f"**Routing distribution (today, {len(rows)} decisions)**",
        "",
        "By tier:",
    ]
    for tier, n in tiers.most_common():
        pct = 100.0 * n / len(rows)
        lines.append(f"* `{tier}` — {n} ({pct:.1f}%)")
    lines.append("")
    lines.append("By model:")
    for model, n in models.most_common():
        pct = 100.0 * n / len(rows)
        lines.append(f"* `{model}` — {n} ({pct:.1f}%)")
    lines.append("")
    lines.append(f"Total cost: ${total_cost:.4f}")
    return "\n".join(lines)


def _handle_git_status(_prompt: str) -> str | None:
    """Return ``git status --short`` output. Read-only; safe."""
    if shutil.which("git") is None:
        return None
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    body = result.stdout.strip() or "(working tree clean)"
    return f"**git status --short**\n\n```\n{body}\n```"


def _handle_recent_commits(_prompt: str) -> str | None:
    """Return last 10 commits via ``git log --oneline -10``. Read-only."""
    if shutil.which("git") is None:
        return None
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-10"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    body = result.stdout.strip() or "(no commits)"
    return f"**Recent commits**\n\n```\n{body}\n```"


# ── Registry ────────────────────────────────────────────────────────────


HANDLERS: list[tuple[str, re.Pattern, Callable[[str], str | None]]] = [
    ("routing_distribution", _ROUTING_DISTRIBUTION_RE, _handle_routing_distribution),
    ("git_status", _GIT_STATUS_RE, _handle_git_status),
    ("recent_commits", _RECENT_COMMITS_RE, _handle_recent_commits),
]


def classify(prompt: str) -> str | None:
    """Return the handler name matching ``prompt``, or ``None``.

    Patterns are ordered most-specific first. The first matching handler
    wins; later handlers don't get a turn. This is intentional — pre-
    executing two things on one prompt risks Claude getting conflicting
    or duplicate context blocks.
    """
    if not prompt or not prompt.strip():
        return None
    for name, pattern, _ in HANDLERS:
        if pattern.search(prompt):
            return name
    return None


def execute(handler_name: str, prompt: str) -> PreExecutionResult | None:
    """Run the named handler. Returns ``None`` if it produced no result.

    All exceptions are swallowed — a buggy handler must never block
    the user's prompt from reaching Claude. The hook caller decides
    whether to inject the result; we just produce it.
    """
    start = time.time()
    try:
        for name, _pattern, fn in HANDLERS:
            if name == handler_name:
                body = fn(prompt)
                if not body:
                    return None
                return PreExecutionResult(
                    handler=name,
                    context=body,
                    duration_ms=int((time.time() - start) * 1000),
                )
    except Exception:
        return None
    return None
