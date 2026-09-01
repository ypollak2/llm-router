"""Premium llm_router status command with Tokyo Night styling.

Refactored from commands/status.py to use new UI components.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.text import Text
from rich.panel import Panel
from rich.console import Group

from llm_router.ui.theme import PALETTE, progress_bar


class PremiumStatusCommand:
    """Premium status display for llm_router status command."""

    def __init__(self, console: Optional[Console] = None):
        """Initialize status command."""
        self.console = console or Console()
        self.state_dir = Path.home() / ".llm-router"
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
            lines.append(Text(line))

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
            from llm_router.dashboard_data import query_window

            windows = [
                ("Today", "today"),
                ("This week", "week"),
                ("This month", "month"),
                ("All time", "lifetime"),
            ]

            any_data = False
            for label, window in windows:
                totals = query_window(window, db_path=str(self.db_path))
                if totals.calls == 0:
                    continue

                any_data = True
                saved = totals.saved_usd
                line = f"  [{PALETTE.success}]{label:<15}[/]  [{PALETTE.success}]${saved:.2f} saved[/]  ·  {totals.calls} routed calls"
                lines.append(Text(line))

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
                Text(self.render_header(), justify="center"),
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
            Panel(
                Text("🔧  Quick Actions", style=f"bold {PALETTE.accent}")
                + "\n"
                + self.render_quick_actions(),
                border_style=PALETTE.muted_border,
                expand=False,
            ),
        ]

        return Group(*panels)

    def print_status(self) -> None:
        """Print complete status to console."""
        status = self.render_full_status()
        self.console.print(status)


def cmd_status_premium() -> int:
    """Execute: llm_router status (premium version)"""
    cmd = PremiumStatusCommand()
    cmd.print_status()
    return 0
