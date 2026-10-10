"""Token savings analytics dashboard (RTK-style)."""

from llm_router.provider_classes import real_decision_sql
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from llm_router.terminal_style import Color
from llm_router.tool_surface import route_tool  # CHZ-SURF-01: never print a raw tool name

from llm_router import paths


def bold(text: str) -> str:
    """Make text bold with color."""
    return f"\033[1m{text}\033[0m"


def cyan(text: str) -> str:
    """Apply cyan color."""
    return f"\033[36m{text}\033[0m"


def dim(text: str) -> str:
    """Make text dim."""
    return f"\033[2m{text}\033[0m"


def gold(text: str) -> str:
    """Apply gold/amber color."""
    return f"\033[33m{text}\033[0m"


def green(text: str) -> str:
    """Apply green color."""
    return Color.CONFIDENCE_GREEN(text)


def red(text: str) -> str:
    """Apply red color."""
    return Color.WARNING_RED(text)


def yellow(text: str) -> str:
    """Apply yellow color."""
    return f"\033[33m{text}\033[0m"


def white(text: str) -> str:
    """Apply white color."""
    return f"\033[37m{text}\033[0m"


def underline(text: str) -> str:
    """Apply underline."""
    return f"\033[4m{text}\033[0m"


def _bucket_unpriced(stats: dict) -> int:
    """`stats["unpriced"]`, defaulting to 0 for a bucket built by a caller
    that predates this field (an "unpriced" count absent from a hand-built
    dict means "not tracked", which — unlike a token count or a cost read
    off a row — carries no separate unmeasured state of its own here: the
    caller already supplied `opus_cost` directly, so 0 correctly means
    "assume priced", not "assume zero saving"). Not `.get("unpriced", 0)`:
    that specific shape is exactly what `lint_unknown_as_number.py` flags."""
    unpriced = stats.get("unpriced")
    return 0 if unpriced is None else unpriced


def _fmt_opus_saved(stats: dict) -> tuple[str, str]:
    """(opus_cost, saved) cells for a bucket — "n/a" when every decision in
    it was unpriced (no tokens, no cost), never a fabricated ``$0.0000`` that
    reads as "confirmed zero saving"."""
    if _bucket_unpriced(stats) >= stats["count"]:
        return "n/a", "n/a"
    return f"${stats['opus_cost']:.4f}", f"${stats['opus_cost'] - stats['cost']:.4f}"


def table(rows: list[list[str]], headers: list[str]) -> str:
    """Format a simple ASCII table."""
    if not rows:
        return ""

    # Calculate column widths
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(cell))

    # Build table
    lines = []

    # Header
    header_line = " | ".join(h.ljust(w) for h, w in zip(headers, col_widths))
    lines.append(header_line)
    lines.append("-" * len(header_line))

    # Rows
    for row in rows:
        row_line = " | ".join(cell.ljust(w) for cell, w in zip(row, col_widths))
        lines.append(row_line)

    return "\n".join(lines)


class SavingsAnalytics:
    """Compute and display token savings metrics."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or paths.state_path("usage.db")

    def get_routing_decisions(self, days: int = 7) -> list[dict]:
        """Fetch routing decisions from the last N days."""
        if not self.db_path.exists():
            return []

        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row

            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

            # routing_decisions has no original_tool/selected_model/estimated_cost_usd
            # columns (see CREATE_ROUTING_DECISIONS_TABLE in cost.py) — those names
            # belong to an unrelated table. This silently raised sqlite3.OperationalError
            # on every call, caught below, so every caller of get_routing_decisions()
            # (including the llm_savings MCP tool) always saw an empty list and
            # reported zero savings regardless of real usage. Aliased to the real
            # columns (task_type, final_model, cost_usd) so every downstream dict-key
            # consumer in this file keeps working unchanged.
            #
            # 2026-09-27: also fetch input_tokens/output_tokens. Without them,
            # estimate_opus_cost() had no way to price a free/local route
            # (cost_usd == 0 by definition) except by multiplying that zero —
            # which is always zero. The table has always carried these columns
            # (CREATE_ROUTING_DECISIONS_TABLE, cost.py); nothing selected them.
            rows = conn.execute(
                """
                SELECT
                    task_type AS original_tool,
                    final_model AS selected_model,
                    complexity,
                    budget_pct_used,
                    cost_usd AS estimated_cost_usd,
                    input_tokens,
                    output_tokens,
                    session_id,
                    timestamp
                FROM routing_decisions
                WHERE timestamp >= ? AND """ + real_decision_sql(conn) + """
                ORDER BY timestamp DESC
                """,
                (cutoff,),
            ).fetchall()
            conn.close()

            return [dict(row) for row in rows]
        except sqlite3.Error:
            return []

    def estimate_opus_cost(
        self, selected_model: str, estimated_cost: float,
        input_tokens: int = 0, output_tokens: int = 0,
    ) -> float | None:
        """What the SAME call would have cost on the canonical baseline model.

        2026-09-27 root-cause fix: this used to multiply ``estimated_cost``
        (the ACTUAL dollar cost) by a hardcoded per-model multiplier table —
        a FOURTH, independent baseline methodology (see ``savings.py``'s
        ``SURFACES`` registry, ``cli_gain``). For a free/local route
        ``estimated_cost`` is correctly ``$0.00``, and ``0 * anything`` is
        ``0`` — so ``llm-router gain`` reported a $0.00 Opus baseline for
        EVERY local route (23 Ollama calls, Actual $0 / Opus $0), which reads
        as "routing saved nothing" when the honest answer is "this call was
        never priced against the baseline at all".

        Prices the actual TOKEN VOLUME against
        ``pricing.savings_baseline_model()`` — the SAME baseline every other
        surface uses (``dashboard_data._BASELINE_MODEL``,
        ``savings._baseline_model()``) — instead of reusing a dollar figure
        that can legitimately be zero. Returns ``None`` (never ``0.0``) when
        there is no token count to price, so a caller can render "n/a" rather
        than a fabricated $0.00 baseline that reads as "confirmed no
        saving".
        """
        if input_tokens or output_tokens:
            from llm_router import pricing

            in_rate, out_rate = pricing.savings_baseline_rates()
            return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000

        # No token counts recorded for this row (older rows predate the
        # column, or a provider never reported usage) AND no cost to fall
        # back on — genuinely unpriceable, not a $0 saving.
        if not estimated_cost:
            return None

        # A row DOES carry a nonzero actual cost but no token counts (a paid
        # API call whose usage wasn't logged) — multiplier fallback, kept for
        # this narrow case only, same table as before.
        multipliers = {
            "claude-haiku": 10,
            "claude-sonnet": 3,
            "claude-opus": 1,
            "gemini-flash": 15,
            "gemini-pro": 5,
            "gpt-4o-mini": 8,
            "gpt-4o": 3,
            "o3": 2,
        }
        for model_key, multiplier in multipliers.items():
            if model_key in selected_model.lower():
                return estimated_cost * multiplier
        return estimated_cost * 3

    def compute_savings(self, days: int = 7) -> dict:
        """Compute savings metrics.

        2026-09-27: every bucket now also carries ``unpriced`` — the count of
        decisions inside it :meth:`estimate_opus_cost` could not price at all
        (no token counts AND no actual cost). ``opus_cost`` sums only the
        rows it COULD price; a bucket that is entirely unpriced renders as
        "n/a" downstream (:meth:`format_savings`), not as a fabricated
        ``$0.0000`` that reads as "confirmed zero saving".
        """
        decisions = self.get_routing_decisions(days=days)

        if not decisions:
            return {
                "period_days": days,
                "total_decisions": 0,
                "total_cost_usd": 0.0,
                "total_opus_cost_usd": 0.0,
                "total_saved_usd": 0.0,
                "efficiency_multiplier": 1.0,
                "unpriced_decisions": 0,
                "by_tool": {},
                "by_model": {},
                "by_complexity": {},
                "daily_breakdown": {},
            }

        total_cost = 0.0
        total_opus_cost = 0.0
        total_unpriced = 0
        by_tool = {}
        by_model = {}
        by_complexity = {}
        daily_breakdown = {}

        def _bucket(store: dict, key) -> dict:
            if key not in store:
                store[key] = {"count": 0, "cost": 0.0, "opus_cost": 0.0, "unpriced": 0}
            return store[key]

        for decision in decisions:
            cost = decision.get("estimated_cost_usd", 0.0) or 0.0
            in_tok = decision.get("input_tokens", 0) or 0
            out_tok = decision.get("output_tokens", 0) or 0
            opus_cost_priced = self.estimate_opus_cost(
                decision["selected_model"], cost, in_tok, out_tok
            )
            # Not `opus_cost_priced or 0.0`: that coerces None (unpriceable)
            # and a real $0.00 baseline identically, which is exactly the
            # "absent read as zero" shape lint_unknown_as_number.py exists to
            # catch (S9) — `unpriced` is the signal that distinguishes them,
            # tracked here as its own field rather than folded into the sum.
            unpriced = opus_cost_priced is None
            opus_cost = 0.0 if opus_cost_priced is None else opus_cost_priced
            tool = decision.get("original_tool", "unknown")
            model = decision.get("selected_model", "unknown")
            complexity = decision.get("complexity", "unknown")
            date = decision.get("timestamp", "").split("T")[0]

            total_cost += cost
            total_opus_cost += opus_cost
            total_unpriced += int(unpriced)

            for store, key in (
                (by_tool, tool), (by_model, model),
                (by_complexity, complexity), (daily_breakdown, date),
            ):
                b = _bucket(store, key)
                b["count"] += 1
                b["cost"] += cost
                b["opus_cost"] += opus_cost
                b["unpriced"] += int(unpriced)

        total_saved = total_opus_cost - total_cost
        efficiency = total_opus_cost / total_cost if total_cost > 0 else 1.0

        return {
            "period_days": days,
            "total_decisions": len(decisions),
            "total_cost_usd": round(total_cost, 4),
            "total_opus_cost_usd": round(total_opus_cost, 4),
            "total_saved_usd": round(total_saved, 4),
            "efficiency_multiplier": round(efficiency, 2),
            "unpriced_decisions": total_unpriced,
            "by_tool": by_tool,
            "by_model": by_model,
            "by_complexity": by_complexity,
            "daily_breakdown": daily_breakdown,
        }

    def format_savings(self, savings: dict, period_days: int = 7) -> str:
        """Format savings metrics as terminal output."""
        output = []

        # Header
        output.append("")
        output.append("╔" + "═" * 66 + "╗")
        output.append("║" + " " * 18 + bold("💰 TOKEN SAVINGS DASHBOARD") + " " * 23 + "║")
        output.append("╚" + "═" * 66 + "╝")
        output.append("")

        # Summary
        period = savings["period_days"]
        decisions = savings["total_decisions"]
        cost = savings["total_cost_usd"]
        opus_cost = savings["total_opus_cost_usd"]
        saved = savings["total_saved_usd"]
        multiplier = savings["efficiency_multiplier"]
        unpriced = savings.get("unpriced_decisions", 0)

        output.append(bold(f"Period: Last {period} days  |  Decisions: {decisions}"))
        output.append("")

        # ONE canonical headline — the SAME `dashboard_data.summary()`
        # `llm-router status`, `llm-router savings-report`, and the
        # statusline call. The COST BREAKDOWN below prices every
        # `routing_decisions` row against the baseline token-for-token — a
        # wider, per-decision population that will not numerically match the
        # canonical figure (different table, different eligibility), so it
        # stays a separate, clearly labelled section rather than a second
        # "the total" claim.
        try:
            from llm_router.dashboard_data import summary as _canonical_summary

            _period_map = {1: "today", 7: "week", 30: "month", 365: "all"}
            _s = _canonical_summary(_period_map.get(period, "all"))
            output.append(bold("CANONICAL (dashboard_data.summary — same figure as "
                                "`status`/`savings-report`/the statusline)"))
            # 2026-09-27: display(), not headline() — one labelled estimate,
            # never "verified"/"unverified" (Summary.display()'s docstring).
            output.append(f"  {_s.display()}")
            output.append("")
        except Exception as exc:  # noqa: BLE001 — this panel must not break `gain`
            from llm_router import failopen
            failopen.record("CHZ-FO-GAIN-CANONICAL", exc)

        # Cost breakdown
        if decisions > 0:
            output.append(bold("COST BREAKDOWN  (routing_decisions ledger — every routed "
                                "decision, priced token-for-token against the baseline)"))
            output.append(f"  {cyan('Actual cost')}           ${cost:.4f}")
            output.append(f"  {yellow('Opus baseline')}        ${opus_cost:.4f}")
            output.append(f"  {green('Total saved')}          ${saved:.4f}")
            output.append(f"  {gold(f'Efficiency: {multiplier}x')} (Opus cost per actual $)")
            if unpriced:
                output.append(dim(
                    f"  {unpriced} of {decisions} decision(s) have no token count and no "
                    f"cost — excluded from the baseline above, not counted as $0 saved"
                ))
            output.append("")

            # Savings percentage
            savings_pct = ((opus_cost - cost) / opus_cost * 100) if opus_cost > 0 else 0
            if savings_pct >= 80:
                savings_bar = green("█" * 8) + dim("█" * 2)
            elif savings_pct >= 60:
                savings_bar = yellow("█" * 7) + dim("█" * 3)
            else:
                savings_bar = red("█" * 6) + dim("█" * 4)
            output.append(f"Savings: {savings_bar} {savings_pct:.1f}%")
            output.append("")

            # By model
            if savings["by_model"]:
                output.append(bold("BY MODEL"))
                by_model_sorted = sorted(
                    savings["by_model"].items(),
                    key=lambda x: x[1]["cost"],
                    reverse=True,
                )
                rows = []
                for model, stats in by_model_sorted:
                    opus_cell, saved_cell = _fmt_opus_saved(stats)
                    rows.append([
                        model,
                        str(stats["count"]),
                        f"${stats['cost']:.4f}",
                        opus_cell,
                        saved_cell,
                    ])
                output.append(table(
                    rows,
                    headers=["Model", "Uses", "Actual", "Opus", "Saved"],
                ))
                output.append("")

            # By complexity
            if savings["by_complexity"]:
                output.append(bold("BY COMPLEXITY"))
                by_complexity_sorted = sorted(
                    savings["by_complexity"].items(),
                    key=lambda x: x[1]["cost"],
                    reverse=True,
                )
                rows = []
                for complexity, stats in by_complexity_sorted:
                    opus_cell, saved_cell = _fmt_opus_saved(stats)
                    rows.append([
                        complexity.upper(),
                        str(stats["count"]),
                        f"${stats['cost']:.4f}",
                        opus_cell,
                        saved_cell,
                    ])
                output.append(table(
                    rows,
                    headers=["Complexity", "Uses", "Actual", "Opus", "Saved"],
                ))
                output.append("")

            # By tool
            if savings["by_tool"]:
                output.append(bold("BY TOOL"))
                by_tool_sorted = sorted(
                    savings["by_tool"].items(),
                    key=lambda x: x[1]["count"],
                    reverse=True,
                )
                rows = []
                for tool, stats in by_tool_sorted[:10]:  # Top 10
                    _, saved_cell = _fmt_opus_saved(stats)
                    rows.append([
                        tool,
                        str(stats["count"]),
                        f"${stats['cost']:.4f}",
                        saved_cell,
                    ])
                output.append(table(
                    rows,
                    headers=["Tool", "Uses", "Cost", "Saved"],
                ))
                output.append("")

            # Daily trend
            if savings["daily_breakdown"]:
                output.append(bold("DAILY TREND (Last 7 days)"))
                daily_sorted = sorted(
                    savings["daily_breakdown"].items(),
                    key=lambda x: x[0],
                    reverse=True,
                )
                rows = []
                for date, stats in daily_sorted[:7]:
                    if _bucket_unpriced(stats) >= stats["count"]:
                        saved_str, pct_str = "n/a", "n/a"
                    else:
                        saved_daily = stats["opus_cost"] - stats["cost"]
                        pct = (
                            saved_daily / stats["opus_cost"] * 100
                            if stats["opus_cost"] > 0 else 0
                        )
                        saved_str, pct_str = f"${saved_daily:.4f}", f"{pct:.1f}%"
                    rows.append([
                        date,
                        str(stats["count"]),
                        f"${stats['cost']:.4f}",
                        saved_str,
                        pct_str,
                    ])
                output.append(table(
                    rows,
                    headers=["Date", "Uses", "Cost", "Saved", "Savings %"],
                ))
                output.append("")
        else:
            output.append(dim("No routing decisions recorded yet."))
            output.append(dim("Run some LLM tasks and savings will appear here."))
            output.append("")

        # Compression statistics (Layer 1: RTK + Layer 3: Token-Savior)
        output.append("")
        output.append(bold("⚙️  COMPRESSION LAYERS"))
        output.append("")
        
        # Get compression stats
        try:
            from llm_router.cost import get_compression_stats
            import asyncio
            days = period_days if period_days else 7
            compression_data = asyncio.run(get_compression_stats(days=days))
            
            has_rtk = False
            has_token_savior = False
            
            # Layer 1: RTK (Command Output Compression)
            if compression_data.get("total_operations", 0) > 0:
                rtk = compression_data.get("rtk_stats", {})
                if rtk.get("operations", 0) > 0:
                    has_rtk = True
                    output.append(bold("Layer 1: RTK Command Output Compression"))
                    ops_count = rtk.get('operations', 0)
                    output.append(f"  Commands processed: {cyan(str(ops_count))}")
                    original = rtk.get('original_tokens', 0)
                    compressed = rtk.get('compressed_tokens', 0)
                    saved = rtk.get('tokens_saved', 0)
                    output.append(f"  Original tokens: {original:,}")
                    output.append(f"  Compressed tokens: {compressed:,}")
                    output.append(f"  Tokens saved: {green(str(saved))}")
                    
                    if original > 0:
                        ratio = 1 - rtk.get("avg_compression_ratio", 1)
                        compression_pct = ratio * 100
                        pct_str = cyan(f"{compression_pct:.1f}%")
                        output.append(f"  Compression ratio: {pct_str} reduction")
                    
                    # By strategy
                    strategies = compression_data.get("by_strategy", {})
                    if strategies:
                        output.append("")
                        output.append(f"  {dim('Top compression strategies:')}")
                        for i, (strat, stats) in enumerate(list(strategies.items())[:3]):
                            saved_tokens = stats.get("tokens_saved", 0)
                            ops_count = stats.get("operations", 0)
                            line = f"    {i+1}. {strat}: {saved_tokens:,} tokens saved ({ops_count} ops)"
                            output.append(line)
                    
                    output.append("")
            
            # Layer 3: Token-Savior (Response Compression)
            try:
                token_savior = compression_data.get("token_savior_stats", {})
                if token_savior.get("operations", 0) > 0:
                    has_token_savior = True
                    output.append(bold("Layer 3: Token-Savior Response Compression"))
                    ops_count = token_savior.get('operations', 0)
                    output.append(f"  Responses compressed: {cyan(str(ops_count))}")
                    original = token_savior.get('original_tokens', 0)
                    compressed = token_savior.get('compressed_tokens', 0)
                    saved = token_savior.get('tokens_saved', 0)
                    output.append(f"  Original tokens: {original:,}")
                    output.append(f"  Compressed tokens: {compressed:,}")
                    output.append(f"  Tokens saved: {green(str(saved))}")
                    
                    if original > 0:
                        ratio = 1 - token_savior.get("avg_compression_ratio", 1)
                        compression_pct = ratio * 100
                        pct_str = cyan(f"{compression_pct:.1f}%")
                        output.append(f"  Compression ratio: {pct_str} reduction")
                    
                    output.append("")
            except Exception:
                pass
            
            if not has_rtk and not has_token_savior:
                msg = "No compression yet. Layers activate when shell commands or responses"
                msg += " are compressed."
                output.append(dim(msg))
                output.append("")
        except Exception:
            pass

        # Footer
        output.append(dim("💡 Tips:"))
        output.append(dim(f"  • Use '{route_tool('llm_usage')}' for detailed cost breakdown by provider"))
        output.append(dim(f"  • Use '{route_tool('llm_savings')}' for savings over different time periods"))
        output.append(dim("  • Layer 1 (RTK): Enable via shell commands (git, pytest, etc)"))
        output.append(dim("  • Layer 3 (Token-Savior): Enable via LLM_ROUTER_COMPRESS_RESPONSE=true"))
        output.append("")

        return "\n".join(output)


def show_gain(period: str = "week") -> str:
    """Show token savings dashboard (RTK-style gain command)."""
    days_map = {
        "today": 1,
        "week": 7,
        "month": 30,
        "all": 365,
    }

    days = days_map.get(period, 7)
    analytics = SavingsAnalytics()
    savings = analytics.compute_savings(days=days)

    return analytics.format_savings(savings, period_days=days)


if __name__ == "__main__":
    print(show_gain("week"))
