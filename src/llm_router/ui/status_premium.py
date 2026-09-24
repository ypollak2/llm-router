"""Premium llm-router status command with Tokyo Night styling.

Refactored from commands/status.py to use new UI components.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from rich.console import Console
from rich.text import Text
from rich.panel import Panel
from rich.console import Group

from llm_router.ui.theme import PALETTE, progress_bar

from llm_router import paths


class PremiumStatusCommand:
    """Premium status display for llm-router status command."""

    def __init__(self, console: Optional[Console] = None):
        """Initialize status command."""
        self.console = console or Console()
        self.state_dir = paths.llm_router_home()
        self.usage_json = self.state_dir / "usage.json"
        self.db_path = self.state_dir / "usage.db"

    def render_header(self) -> str:
        """Render premium header with health status."""
        health = "Optimal"  # Could be computed from actual state
        header = f"⚡ LLM_ROUTER Status  ·  Health: {health}"
        return f"[bold {PALETTE.accent}]{header}[/]"

    def load_pressure(self) -> Optional[dict]:
        """Return the subscription pressure data, or None when it is unknown.

        None and "0%" are different facts and must not be conflated. A fresh
        install has no usage.json until the first refresh runs, and reporting
        that as 0% told the user they had their whole quota left when the truth
        was that nothing had been measured yet.
        """
        try:
            with open(self.usage_json) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        if not isinstance(data, dict):
            return None
        # `pending` is the install-time placeholder; `is_fallback` is the one
        # session-start.py writes when the OAuth fetch fails, setting session,
        # weekly and sonnet all to 50. This method originally covered only the
        # first — the missing-file case — and the second is the more common one:
        # it was observed rendering "50%/5h 50%/wk" while the API returned 2%
        # and 24%.
        #
        # Absence of is_fallback means measured. The refresh hook's success path
        # omits the key, so defaulting the other way blanks the quota on every
        # healthy install.
        if data.get("pending") or data.get("is_fallback"):
            return None
        return data

    def render_subscription_quotas(self) -> Group:
        """Render Claude Code subscription quotas."""
        lines = [Text("📊  Claude Code Subscription", style=f"bold {PALETTE.accent}")]

        pressure_data = self.load_pressure()
        if pressure_data is None:
            # Says "usage" deliberately. The placeholder must still identify what
            # it is a placeholder FOR — a panel that goes silent reads as a
            # missing feature rather than pending data, and a caller checking
            # that the status mentions usage should not have to special-case the
            # unmeasured state.
            lines.append(
                Text(
                    "  Usage not measured yet — run `llm-router status` inside your\n"
                    "  host, or wait for the first session refresh to populate it.",
                    style=PALETTE.muted if hasattr(PALETTE, "muted") else PALETTE.warning,
                )
            )
            return Group(*lines)

        quotas = [
            ("Session Quota (5h)", pressure_data.get("session_pct"), "5h window"),
            ("Weekly Usage", pressure_data.get("weekly_pct"), "7d window"),
            ("Sonnet Monthly", pressure_data.get("sonnet_pct"), "30d window"),
        ]

        for label, pct, window in quotas:
            if pct is None:
                lines.append(Text(f"  {label:<20} {'—':<16}  not reported  ·  {window}"))
                continue
            bar = progress_bar(pct, max_val=100.0, width=16)
            pct_color = (
                PALETTE.success if pct < 70 else PALETTE.warning if pct < 90 else PALETTE.error
            )
            line = f"  {label:<20} {bar}  [{pct_color}]{pct:.0f}%[/]  ·  {window}"
            # S7: markup in the string, so it must be PARSED, not rendered
            # literally. `Text(...)` does the latter.
            lines.append(Text.from_markup(line))

        return Group(*lines)

    def render_routing_savings(self) -> Group:
        """Render routing savings by period."""
        lines = [
            Text("💎  Routing Savings", style=f"bold {PALETTE.accent}"),
        ]

        if not os.path.exists(self.db_path):
            lines.append(Text("  No data yet — route some tasks first"))
            return Group(*lines)

        try:
            from llm_router import dashboard_data as _dd
            from llm_router.dashboard_data import query_primary_metric, query_window
            from llm_router.savings import (
                CanonicalSavings,
                label_money,
                under_subscription,
                unverified_note,
            )

            windows = [
                ("Today", "today"),
                ("This week", "week"),
                ("This month", "month"),
                ("All time", "lifetime"),
            ]

            sub = under_subscription()
            any_data = False
            for label, window in windows:
                totals = query_window(window, db_path=str(self.db_path))
                if totals.calls == 0:
                    continue

                any_data = True
                saved = totals.saved_usd
                # R7: no bare `$` — every money figure carries the subscription
                # caveat (label_money), so "$0.20 saved" cannot be shown when
                # under a subscription no cash actually changed hands.
                money_ctx = CanonicalSavings(
                    window=window,
                    baseline_equivalent_avoided_usd=saved,
                    routing_overhead_usd=0.0,
                    real_dollars_avoided_usd=saved,
                    baseline_model=_dd._BASELINE_MODEL,
                    n_rows=totals.calls,
                    provenance_filtered=True,
                    under_subscription=sub,
                    source="dashboard_data.query_window",
                )
                money = label_money(saved, money_ctx)
                line = f"  [{PALETTE.success}]{label:<15}[/]  [{PALETTE.success}]{money}[/]"
                lines.append(Text.from_markup(line))
                note = unverified_note(totals.unverified_saved_usd, totals.unverified_calls)
                if note:
                    # Text(), not from_markup: the note is data, not markup.
                    lines.append(Text(f"  {'':<15}  {note}", style=PALETTE.text_dim))

                if window == "today":
                    # North Star (points 8+12): verified share of eligible
                    # Claude turns, numerator and denominator from the SAME
                    # table and window — see PrimaryMetric's docstring.
                    metric = query_primary_metric(window, db_path=str(self.db_path))
                    rendered = metric.render()
                    if rendered:
                        lines.append(Text(f"  {'':<15}  {rendered}", style=PALETTE.text_dim))

            if not any_data:
                lines.append(Text("  No external routing yet — route some tasks first"))

            # Top models inline
            top_models_text = "  Top models:  "
            try:
                import sqlite3

                conn = sqlite3.connect(str(self.db_path))
                rows = conn.execute(
                    "SELECT final_model as model, COUNT(*) as n "
                    "FROM routing_decisions WHERE success=1 "
                    "GROUP BY final_model ORDER BY n DESC LIMIT 3"
                ).fetchall()
                conn.close()

                if rows:
                    models_str = "  ·  ".join(
                        f"{row[0].split('/')[-1]} ({row[1]}×)" for row in rows
                    )
                    top_models_text += models_str
                    lines.append(Text(top_models_text, style=PALETTE.text_dim))
            except Exception:
                pass

        except ImportError:
            lines.append(Text("  dashboard_data module not available"))
        except Exception as e:
            lines.append(Text(f"  Error querying savings: {e}"))

        return Group(*lines)

    def render_quick_actions(self) -> str:
        """Render quick action shortcuts footer."""
        actions = [
            "① llm-router dashboard  — Live web dashboard",
            "② llm-router doctor     — System health check",
            "③ llm-router update     — Pull latest hooks",
        ]
        return "\n".join(f"  {action}" for action in actions)

    def render_full_status(self) -> Group:
        """Render complete premium status display."""
        panels = [
            Panel(
                # S7: `Text(...)` does NOT interpret markup — it renders the
                # literal characters. `render_header()` returns a markup
                # string, so the first command the README sends a new user to
                # printed `[bold #7aa2f7]⚡ LLM_ROUTER Status …[/]` verbatim on
                # a clean install of 15.0.0. `from_markup` is what parses it.
                Text.from_markup(self.render_header(), justify="center"),
                border_style=PALETTE.muted_border,
                expand=False,
            ),
            Text(""),
            Panel(
                self.render_subscription_quotas(),
                border_style=PALETTE.muted_border,
                expand=False,
            ),
            Text(""),
            Panel(
                self.render_routing_savings(),
                border_style=PALETTE.muted_border,
                expand=False,
            ),
            Text(""),
        ]
        # T-07: only when something actually degraded — a panel that is always
        # there and always empty is furniture, not a signal.
        _degraded = self.render_degraded_operations()
        if str(_degraded):
            panels += [
                Panel(_degraded, border_style=PALETTE.warning, expand=False),
                Text(""),
            ]
        panels += [
            Panel(
                Text("🔧  Quick Actions", style=f"bold {PALETTE.accent}")
                + "\n"
                + self.render_quick_actions(),
                border_style=PALETTE.muted_border,
                expand=False,
            ),
        ]

        return Group(*panels)

    def render_degraded_operations(self) -> Text:
        """Fail-open counters (T-07). Empty Text when there is nothing to say.

        58 `failopen.record()` sites, 0 readers outside tests. `doctor` is the
        full report; `status` shows it only when something HAS degraded, so a
        healthy install stays quiet and a degraded one cannot be missed.
        """
        out = Text()
        try:
            from llm_router import failopen

            counts = failopen.snapshot()
            if counts.readable and not counts.total and not counts.unpersisted_total:
                return out
            out.append("⚠️  Degraded operations\n", style=f"bold {PALETTE.warning}")
            for line in counts.render_report(limit=4):
                if "could NOT be recorded" in line or "UNREADABLE" in line:
                    out.append(f"{line}\n", style=f"bold {PALETTE.error}")
                else:
                    out.append(f"{line}\n", style=PALETTE.text_dim)
            out.append("run `llm-router doctor` for the full list", style=PALETTE.text_dim)
        except Exception:  # noqa: BLE001 — status must still render
            return Text()
        return out

    def print_status(self) -> None:
        """Print complete status to console."""
        status = self.render_full_status()
        self.console.print(status)


def cmd_status_premium() -> int:
    """Execute: llm-router status (premium version)"""
    cmd = PremiumStatusCommand()
    cmd.print_status()
    return 0
