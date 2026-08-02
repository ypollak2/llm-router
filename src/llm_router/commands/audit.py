"""Audit command — post-hoc misroute audit report (WS9).

Wires ``llm_router.audit_routing.run_audit()`` (WS6, implemented and tested
but previously invoked from no entry point) to the CLI. This command is
read-only / reporting with respect to the live routing decision path: it
never touches request/response routing, and ``run_audit()`` itself only
writes to the additive ``audit_verdict`` / ``audit_checked_at`` metadata
columns on already-recorded ``routing_decisions`` rows (see
``audit_routing.py``'s module docstring). Running this command can never
change what model a future request routes to.

Respects ``LLM_ROUTER_AUDIT_DISABLED`` — when set, ``run_audit()`` itself
short-circuits and returns ``{"disabled": True, ...}``; this command simply
renders that outcome.
"""

from __future__ import annotations

import os
import sys


# ── Formatting utilities (mirrors commands/team.py) ─────────────────────────

def _color_enabled() -> bool:
    """Check if color output is enabled."""
    return sys.stdout.isatty() and not os.getenv("NO_COLOR")


def _bold(s: str) -> str:
    """Bold text."""
    return f"\033[1m{s}\033[0m" if _color_enabled() else s


def _green(s: str) -> str:
    """Green text."""
    return f"\033[32m{s}\033[0m" if _color_enabled() else s


def _red(s: str) -> str:
    """Red text."""
    return f"\033[31m{s}\033[0m" if _color_enabled() else s


def _yellow(s: str) -> str:
    """Yellow text."""
    return f"\033[33m{s}\033[0m" if _color_enabled() else s


def _dim(s: str) -> str:
    """Dim text."""
    return f"\033[2m{s}\033[0m" if _color_enabled() else s


# ── Audit report ─────────────────────────────────────────────────────────────

def _run_audit(flags: list[str]) -> None:
    """llm-router audit [--limit N] [--json].

    Runs the post-hoc misroute audit (a bounded, offline re-score of
    unaudited ``routing_decisions`` rows) and prints a summary report.
    """
    import asyncio
    import json

    from llm_router.audit_routing import audit_disabled, run_audit

    limit = 100
    as_json = False
    i = 0
    while i < len(flags):
        flag = flags[i]
        if flag == "--limit" and i + 1 < len(flags):
            try:
                limit = int(flags[i + 1])
            except ValueError:
                pass
            i += 2
            continue
        if flag == "--json":
            as_json = True
            i += 1
            continue
        i += 1

    report = asyncio.run(run_audit(limit=limit))

    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return

    print(f"\n{_bold('[llm-router] Misroute Audit Report')}\n")

    if report.get("disabled") or audit_disabled():
        print(_yellow("  Audit disabled (LLM_ROUTER_AUDIT_DISABLED is set)."))
        print(f"  Unset it to re-enable: {_dim('unset LLM_ROUTER_AUDIT_DISABLED')}\n")
        return

    sampled = report.get("sampled", 0)
    audited = report.get("audited", 0)

    if sampled == 0:
        print(_yellow("  No unaudited routing decisions found."))
        print(f"  {_dim('Nothing to sample — either the table is empty or everything already has a verdict.')}\n")
        return

    print(f"  Sampled:  {_bold(str(sampled))}")
    print(f"  Audited:  {_bold(str(audited))}\n")

    counts = report.get("verdict_counts", {}) or {}
    misroute = counts.get("likely_misroute", 0)
    correct = counts.get("likely_correct", 0)
    insufficient = counts.get("insufficient_data", 0)

    print(f"  {'Verdict':<20} {'Count':>6}")
    print(f"  {'-' * 20} {'-' * 6}")
    print(f"  {_red('likely_misroute'):<29} {misroute:>6}")
    print(f"  {_green('likely_correct'):<29} {correct:>6}")
    print(f"  {_dim('insufficient_data'):<29} {insufficient:>6}")

    baseline = report.get("mis_route_rate_inferred_baseline")
    if baseline is not None:
        print(f"\n  {_dim(f'Fleet-wide inferred misroute rate (baseline): {baseline:.1%}')}")
    print()


# ── Entry point ─────────────────────────────────────────────────────────────

def cmd_audit(args: list[str]) -> int:
    """Execute: llm-router audit [--limit N] [--json]

    Post-hoc misroute audit report. Read-only / reporting only — never
    mutates live routing behavior. Respects LLM_ROUTER_AUDIT_DISABLED.
    """
    _run_audit(args)
    return 0
