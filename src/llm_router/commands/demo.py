"""Demo command — show routing decisions."""

from __future__ import annotations

import os
import sqlite3
import sys

from llm_router import paths


# ── ANSI helpers (respect NO_COLOR / non-tty) ─────────────────────────────────

def _color_enabled() -> bool:
    return sys.stdout.isatty() and not os.getenv("NO_COLOR")


def _bold(s: str) -> str:
    return f"\033[1m{s}\033[0m" if _color_enabled() else s


def _green(s: str) -> str:
    return f"\033[32m{s}\033[0m" if _color_enabled() else s


def _yellow(s: str) -> str:
    return f"\033[33m{s}\033[0m" if _color_enabled() else s


def _red(s: str) -> str:
    return f"\033[31m{s}\033[0m" if _color_enabled() else s


def _dim(s: str) -> str:
    return f"\033[2m{s}\033[0m" if _color_enabled() else s


def _visual_len(s: str) -> int:
    """Return visible character count, stripping ANSI escape codes."""
    import re
    return len(re.sub(r'\033\[[0-9;]*m', '', s))


def _pad(s: str, width: int) -> str:
    """Left-justify s to visual width (handles ANSI-colored strings correctly)."""
    return s + " " * max(0, width - _visual_len(s))


# ── Command entry point ────────────────────────────────────────────────────────

def cmd_demo(args: list[str]) -> int:
    """Entry point for demo command."""
    _run_demo()
    return 0


# ── Helpers ────────────────────────────────────────────────────────────────────

def _baseline_usd(model: str, input_tokens, output_tokens, actual_usd: float) -> float | None:
    """What one call would have cost on the savings baseline model, or ``None``.

    ``None`` means "cannot be priced", and the caller MUST drop the row from the
    comparison rather than score it as zero. A row counted at zero on the
    baseline side understates the baseline and inflates the printed saving —
    exactly the failure :func:`pricing.cost_usd` refuses to commit when it
    returns ``None`` instead of ``0.0``.

    A call that already ran ON the baseline model gets its own recorded cost as
    its baseline. Routing saved nothing there, and saying so is the honest
    answer; charging it a flat constant instead is what produced a *negative*
    saving printed as "cheaper" (audit 2026-09-22, T-02).
    """
    from llm_router import pricing

    baseline_model = pricing.savings_baseline_model()
    if pricing.resolve(model or "") == baseline_model:
        return float(actual_usd)
    in_tok = int(input_tokens or 0)
    out_tok = int(output_tokens or 0)
    if in_tok <= 0 and out_tok <= 0:
        return None  # no tokens recorded — nothing to price a baseline from
    return pricing.cost_usd(baseline_model, in_tok, out_tok)


def _load_real_routing_history(db_path: str, limit: int = 8) -> list[tuple]:
    """Return the last *limit* real routing decisions from usage.db.

    Returns list of ``(prompt_snippet, task_type, complexity, model, cost_str,
    baseline_usd)``, where ``baseline_usd`` is per-row and derived from that
    row's own token counts — see :func:`_baseline_usd`. Empty list if DB
    missing or table has no external calls.
    """
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT prompt, task_type, complexity, model, cost_usd, "
            "input_tokens, output_tokens "
            "FROM usage WHERE success=1 AND provider!='subscription' "
            "AND prompt IS NOT NULL "
            "ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
    except Exception:
        return []

    result = []
    for r in rows:
        prompt = (r["prompt"] or "").strip()[:44]
        if len(r["prompt"] or "") > 44:
            prompt = prompt[:43] + "…"
        task   = (r["task_type"] or "query")[:8]
        compl  = (r["complexity"] or "moderate")[:12]
        raw_model = r["model"] or "?"
        model  = raw_model.split("/")[-1][:18]
        cost_val = float(r["cost_usd"] or 0.0)
        cost   = f"${cost_val:.5f}" if cost_val < 0.001 else f"${cost_val:.4f}"
        try:
            baseline = _baseline_usd(raw_model, r["input_tokens"], r["output_tokens"], cost_val)
        except Exception:
            baseline = None
        result.append((f'"{prompt}"', task, compl, model, cost, baseline))
    return result


def _baseline_model_label() -> str:
    """Short display name for the savings baseline model.

    The old demo hard-coded "Opus" in the comparison label while computing the
    baseline from a constant unrelated to Opus pricing. The label now follows
    :func:`pricing.savings_baseline_model`, so an operator who overrides the
    baseline sees the model they actually chose.
    """
    from llm_router import pricing

    model = pricing.savings_baseline_model()
    for family in ("opus", "sonnet", "haiku"):
        if family in model.lower():
            return family.capitalize()
    return model


def _example_cases() -> list[tuple]:
    """The fallback examples shown when there is no routing history yet.

    Token counts are illustrative — they describe a plausible short question, a
    mid-sized analysis and a real implementation task. The DOLLARS are not
    illustrative: both the routed cost and the baseline come from
    :mod:`llm_router.pricing`, so the demo cannot quote a price the router does
    not charge. The complex row runs on the baseline model itself, which makes
    its saving exactly $0 — that row is the one the flat-constant arithmetic
    used to turn into a negative saving labelled "cheaper".
    """
    from llm_router import pricing

    baseline_model = pricing.savings_baseline_model()
    shapes = [
        ('"what does os.path.join do?"',            "query",   "simple",   "haiku",        320,  180),
        ('"why is my async code slow?"',            "analyze", "moderate", "sonnet",       1200, 900),
        ('"implement a Redis-backed rate limiter"', "code",    "complex",  baseline_model, 2400, 1800),
    ]

    cases: list[tuple] = []
    for prompt, task, compl, model, in_tok, out_tok in shapes:
        resolved = pricing.resolve(model) or model
        routed = pricing.cost_usd(resolved, in_tok, out_tok)
        if routed is None:
            # Unpriced model in the example table — show the row, but never
            # invent a number for it or let it into the comparison.
            cases.append((prompt, task, compl, _baseline_model_label() if resolved == baseline_model
                          else resolved.split("/")[-1][:18], "n/a", None))
            continue
        cost = f"${routed:.5f}" if routed < 0.001 else f"${routed:.4f}"
        baseline = _baseline_usd(resolved, in_tok, out_tok, routed)
        cases.append((prompt, task, compl, resolved.split("/")[-1][:18], cost, baseline))
    return cases


# ── Implementation ─────────────────────────────────────────────────────────────

def _run_demo() -> None:
    """Show routing decisions — real history if available, examples otherwise."""

    db_path = str(paths.state_path("usage.db"))
    real_rows = _load_real_routing_history(db_path)
    using_real = bool(real_rows)

    # Fallback static examples — 3 focused cases showing cost savings
    cc_mode = os.getenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "").lower() in ("true", "1", "yes")
    gemini_mode = os.getenv("LLM_ROUTER_GEMINI_SUBSCRIPTION", "").lower() in ("true", "1", "yes")
    has_perplexity = bool(os.getenv("PERPLEXITY_API_KEY"))
    has_openai     = bool(os.getenv("OPENAI_API_KEY"))
    has_gemini     = bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))

    # 3 core examples: simple → moderate → complex cost story.
    # Priced from the real table, not from literals: a hand-written dollar
    # figure here is a claim about what llm-router charges, and it drifts the
    # moment the table moves. Token counts are illustrative and labelled as
    # such; the dollars they produce are not invented.
    EXAMPLE_CASES = _example_cases()

    cases = real_rows if using_real else EXAMPLE_CASES
    col_w = [44, 8, 12, 18, 9]
    sep = "─" * (sum(col_w) + len(col_w) * 2 + 2)

    title = "your last routing decisions" if using_real else "cost-optimized routing examples"
    print(f"\n{_bold('llm-router demo')}  — {title}\n")

    if not using_real:
        config_parts = []
        if cc_mode:
            config_parts.append("Claude Code subscription")
        if gemini_mode:
            config_parts.append("Gemini subscription")
        if has_perplexity:
            config_parts.append("Perplexity")
        if has_openai:
            config_parts.append("OpenAI")
        if has_gemini:
            config_parts.append("Gemini")
        if not config_parts:
            config_parts.append("no external APIs configured")
        print(f"  Active config: {', '.join(config_parts)}")
        print(f"  {_dim('(no routing history yet — showing examples)')}\n")
    else:
        print(f"  {_dim('Source: ~/.llm-router/usage.db  (your actual routing decisions)')}\n")

    print(f"  {'Prompt':<{col_w[0]}}  {'Task':<{col_w[1]}}  {'Complexity':<{col_w[2]}}  {'Model':<{col_w[3]}}  {'Cost'}")
    print("  " + sep)

    # PER-ROW baseline. The old code added a flat 0.015 for every row, which
    # charged a premium call a fraction of what it really cost and printed the
    # resulting NEGATIVE saving as "cheaper" (audit 2026-09-22, T-02). A row we
    # cannot price on the baseline is dropped from BOTH sides and disclosed,
    # never scored as zero.
    total_routed = 0.0
    total_baseline = 0.0
    compared_rows = 0
    unpriced_rows = 0
    for prompt, task, complexity, model, cost_str, baseline in cases:
        if complexity == "simple":
            compl_label = _green(complexity)
        elif complexity in ("moderate", "—"):
            compl_label = _yellow(complexity)
        elif complexity in ("complex", "deep_reason", "deep_reasoning"):
            compl_label = _red(complexity)
        else:
            compl_label = complexity

        try:
            cost_val = float(cost_str.lstrip("$"))
            cost_label = _green(cost_str) if cost_val < 0.002 else (
                _yellow(cost_str) if cost_val < 0.01 else _red(cost_str))
        except ValueError:
            cost_label = cost_str

        prompt_disp = prompt if len(prompt) <= col_w[0] else prompt[:col_w[0] - 1] + "…"
        print(
            f"  {_pad(prompt_disp, col_w[0])}"
            f"  {_pad(task, col_w[1])}"
            f"  {_pad(compl_label, col_w[2])}"
            f"  {_pad(model, col_w[3])}"
            f"  {cost_label}"
        )
        try:
            routed_val = float(cost_str.lstrip("$"))
        except ValueError:
            continue
        if baseline is None:
            unpriced_rows += 1
            continue
        total_routed += routed_val
        total_baseline += float(baseline)
        compared_rows += 1

    print("  " + sep)

    if compared_rows and total_baseline > 0:
        savings_amount = total_baseline - total_routed
        savings_pct = 100 * (savings_amount / total_baseline)
        baseline_label = _baseline_model_label()

        print(f"\n  {_bold('Cost Comparison:')}  {_dim(f'(n={compared_rows} of {len(cases)} calls)')}")
        print(f"    {_red(f'Always-{baseline_label}:')} ${total_baseline:.4f} per batch")
        print(f"    {_green('Smart Routing:')} ${total_routed:.5f} per batch")

        if savings_amount > 0:
            print(f"\n  {_bold('Savings:')}  {_green(f'${savings_amount:.4f}')} ({_green(f'{savings_pct:.0f}%')} cheaper)")
            if savings_pct > 70:
                print(f"  {_yellow('→')} {_green('Excellent savings')} — routing paid for itself immediately")
            elif savings_pct > 50:
                print(f"  {_yellow('→')} {_green('Good savings')} — paying for advanced features at budget prices")
        elif savings_amount == 0:
            print(f"\n  {_bold('Savings:')}  $0.0000 (0%) — these calls already ran on {baseline_label}")
        else:
            # Routing cost MORE. Say so. The word "cheaper" is not available for
            # a negative number, and hiding the sign is how a demo lies.
            print(f"\n  {_bold('Savings:')}  {_red(f'-${abs(savings_amount):.4f}')} "
                  f"({_red(f'{abs(savings_pct):.0f}%')} {_red('more expensive')})")
            print(f"  {_yellow('→')} This batch cost more than {baseline_label} would have")

        if unpriced_rows:
            print(f"  {_dim(f'{unpriced_rows} call(s) excluded — no token counts to price a baseline from')}")
    elif unpriced_rows:
        print(f"\n  {_dim(f'No cost comparison: none of {len(cases)} call(s) could be priced against a baseline.')}")

    if not using_real:
        print(f"\n  {_yellow('Next steps:')}")
        print(f"    {_yellow('→')} Run {_bold('llm-router install')} to enable automatic routing")
        if not cc_mode:
            print(f"    {_yellow('→')} Set {_bold('LLM_ROUTER_CLAUDE_SUBSCRIPTION=true')} to use subscription models")
    else:
        print(f"\n  {_yellow('Your routing history:')}")
    print(f"  {_yellow('→')} Check savings: {_bold('llm-router gain')}")
    print(f"  {_yellow('→')} View dashboard: {_bold('llm-router dashboard')}\n")
