"""Session Summary Dashboard — modern two-panel overview at session end.

Layout: main panel (routing + savings + quota) + 14-day activity bar chart.
Style: concise, information-dense, Tokyo Night palette.
"""

from __future__ import annotations

import datetime
from typing import Optional

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from llm_router.ui.theme import PALETTE


_METHOD_SHORT: dict[str, str] = {
    "heuristic": "heuristic",
    "heuristic-weak": "heuristic",
    "build-fast-path": "build-fast",
    "content-generation-fast-path": "content-gen",
    "ollama": "ollama",
    "llm": "llm-class",
    "context-inherit": "ctx-inherit",
    "code-context-inherit": "ctx-inherit",
    "override": "override",
    "fallback": "fallback",
    "unknown": "unknown",
    "introspection": "introspect",
    "introspect": "introspect",
    "introspection-fast-path": "introspect",
}

_METHOD_SYMBOLS: dict[str, str] = {
    "heuristic": "⚡",
    "heuristic-weak": "⚡",
    "build-fast-path": "🔨",
    "content-generation-fast-path": "📝",
    "ollama": "🧠",
    "llm": "🧠",
    "context-inherit": "🔗",
    "code-context-inherit": "🔗",
    "override": "📌",
    "fallback": "🔄",
    "unknown": "❓",
    "introspection": "🔍",
    "introspect": "🔍",
    "introspection-fast-path": "🔍",
}

_FREE_METHODS = frozenset({
    "heuristic", "heuristic-weak", "build-fast-path",
    "content-generation-fast-path", "context-inherit",
    "code-context-inherit", "introspection", "introspect",
    "introspection-fast-path",
})

# Per-method rich color — makes the routing breakdown scannable at a glance.
# Zero-cost methods → cool/green tones; paid/fallback → warm tones.
_METHOD_COLORS: dict[str, str] = {
    "heuristic":                       "#9ece6a",  # vivid green — free path
    "heuristic-weak":                  "#73daca",  # teal — free path
    "build-fast-path":                 "#41a6b5",  # cyan-blue — build fast
    "content-generation-fast-path":    "#7dcfff",  # sky blue — content gen
    "ollama":                          "#9ece6a",  # green — local/free
    "llm":                             "#7aa2f7",  # blue — classified
    "context-inherit":                 "#bb9af7",  # violet — inherited
    "code-context-inherit":            "#bb9af7",  # violet — inherited
    "override":                        "#ff9e64",  # orange — manual override
    "intent-override-display":         "#ff9e64",  # orange — display override
    "fallback":                        "#e0af68",  # amber — fallback path
    "unknown":                         "#565f89",  # dim — unknown
    "introspection":                   "#73daca",  # teal — introspect
    "introspect":                      "#73daca",
    "introspection-fast-path":         "#73daca",
}

# Policy display metadata — symbol + accent color
_POLICY_STYLES: dict[str, tuple[str, str]] = {
    "balanced":        ("⚖️",  "#7aa2f7"),   # blue
    "local-first":     ("🏠",  "#9ece6a"),   # green
    "cost":            ("💰",  "#e0af68"),   # amber
    "quality":         ("🏆",  "#bb9af7"),   # violet
    "quota-exhaustion": ("📊", "#f7768e"),   # pink/red
    "dynamic":         ("🔀",  "#73daca"),   # teal
}


def _fmt_tok(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _tok_axis_unit(max_v: int) -> tuple[float, str]:
    """Pick ONE unit (divisor, suffix) for a token Y-axis from its max value.

    A Y-axis must use a single scale — formatting each tick independently mixes
    units (e.g. "3.2M" at the top and "901.3k" lower down on the *same* axis).
    Here the whole axis is locked to the unit implied by its maximum tick.
    """
    if max_v >= 1_000_000:
        return 1_000_000.0, "M"
    if max_v >= 1_000:
        return 1_000.0, "k"
    return 1.0, ""


def _fmt_tok_axis(n: int, divisor: float, suffix: str) -> str:
    """Format a single Y-axis tick against the axis-wide unit from _tok_axis_unit."""
    if n == 0:
        return "0"
    if divisor == 1.0:
        return str(n)
    return f"{n / divisor:.1f}{suffix}"


def _fmt_usd(amount: float) -> str:
    return f"${amount:.2f}" if amount >= 0.01 else f"${amount:.4f}"


def _date_axis_label_row(n: int, today: datetime.date) -> str:
    """Collision-safe x-axis date labels for the N-day activity charts.

    oldest (d/m) left · today (d/m) right · a mid-point day between them ONLY when
    it fits with a gap on each side. The previous logic placed the 4-char start
    label at pos 0 and the mid label at (n-1)//2 with no start↔mid collision
    guard, so a narrow chart (n≈9) ran "23/6" and "27" together into "23/627".
    Returns the label row as a plain string (caller adds styling).
    """
    if n < 2:
        return ""
    d_start = (today - datetime.timedelta(days=n - 1)).strftime("%-d/%-m")
    d_end = today.strftime("%-d/%-m")
    width = max(n, len(d_start) + 1 + len(d_end))
    row = [" "] * width
    for i, ch in enumerate(d_start):          # oldest — left
        row[i] = ch
    end_start = max(len(d_start) + 1, width - len(d_end))  # today — right
    for i, ch in enumerate(d_end):
        row[end_start + i] = ch
    d_mid = (today - datetime.timedelta(days=(n - 1) // 2)).strftime("%-d")
    mid_pos = (width - 1) // 2 - len(d_mid) // 2
    if mid_pos > len(d_start) and mid_pos + len(d_mid) < end_start - 1:
        for i, ch in enumerate(d_mid):
            row[mid_pos + i] = ch
    return "".join(row)


class SessionSummaryDashboard:
    """Modern two-panel session summary dashboard."""

    def __init__(self, console: Optional[Console] = None):
        self.console = console or Console()

    def _quota_bar(self, pct: float, width: int = 16) -> str:
        filled = min(width, int(pct / 100 * width))
        return "━" * filled + "─" * (width - filled)

    def _colored_quota_bar(self, pct: float, width: int = 16) -> Text:
        """Progress bar with green→orange→red stripes based on usage %.

        Segment colour is determined by its *position* in the bar, not the
        current fill level, so the colour zones are always visible:
          0–70 % of bar width → green   (safe zone)
         70–90 % of bar width → yellow  (caution zone)
         90–100% of bar width → red     (danger zone)
        Filled segments use the zone colour; unfilled segments are dim ─.
        """
        filled = min(width, int(pct / 100 * width))
        bar = Text()
        for i in range(width):
            seg_end_pct = (i + 1) / width * 100
            if seg_end_pct <= 70:
                zone = "bold green"
            elif seg_end_pct <= 90:
                zone = "bold yellow"
            else:
                zone = "bold red"
            if i < filled:
                bar.append("━", style=zone)
            else:
                bar.append("─", style="dim")
        return bar

    def _format_resets_at(self, iso_ts: str) -> str:
        if not iso_ts:
            return ""
        try:
            dt = datetime.datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
            now = datetime.datetime.now(datetime.timezone.utc)
            delta = dt - now
            if delta.total_seconds() <= 0:
                return "resets soon"
            total_sec = int(delta.total_seconds())
            hours, rem = divmod(total_sec, 3600)
            minutes = rem // 60
            if hours >= 24:
                days = hours // 24
                return f"resets in {days}d {hours % 24}h"
            if hours > 0:
                return f"resets in {hours}h {minutes}m"
            return f"resets in {minutes}m"
        except Exception:
            return iso_ts

    def _bar_chart_rows(self, values: list[int], n_rows: int = 8) -> list[str]:
        """Build ASCII vertical bar chart rows, top to bottom."""
        if not values or max(values) == 0:
            return [" " * len(values)] * n_rows
        BLOCKS = " ▁▂▃▄▅▆▇█"
        max_val = max(values)
        rows = []
        for r in range(n_rows - 1, -1, -1):
            row_bottom = r * max_val / n_rows
            row_top = (r + 1) * max_val / n_rows
            chars = []
            for v in values:
                if v >= row_top:
                    chars.append("█")
                elif v <= row_bottom:
                    chars.append(" ")
                else:
                    fill = (v - row_bottom) / (row_top - row_bottom)
                    chars.append(BLOCKS[max(1, min(8, int(fill * 8 + 0.5)))])
            rows.append("".join(chars))
        return rows

    def render_main_panel(
        self,
        timestamp: str = "",
        decisions: list[dict] | None = None,
        savings: dict | None = None,
        claude_quota_pct: float = 0.0,
        claude_session_pct: float = 0.0,
        claude_session_resets_at: str = "",
        claude_weekly_resets_at: str = "",
        gemini_quota_pct: float = 0.0,
        gemini_resets_at: str = "",
        codex_quota_pct: float = 0.0,
        codex_resets_at: str = "",
        session_delta_pct: float | None = None,
        weekly_delta_pct: float | None = None,
        model_breakdown: dict[str, float] | None = None,
        model_breakdown_note: str | None = None,
        session_models: list[dict] | None = None,
        subscriptions: list[dict] | None = None,
        routing_policy: str = "balanced",
        # New session metrics
        burn_rate_per_hr: float = 0.0,
        session_cost_usd: float = 0.0,
        fallback_pct: float = 0.0,
        escalation_pct: float = 0.0,
        fallback_count: int = 0,
        escalation_count: int = 0,
        routing_effectiveness_pct: float = 0.0,
        session_cost_ratio: float | None = None,
        session_calls_ratio: float | None = None,
    ) -> RenderableType:
        """Single panel: routing decisions (left) + savings (right) + quota + models."""
        decisions = decisions or []
        savings = savings or {}

        total_hits = sum(d.get("count", 0) for d in decisions)
        zero_cost = sum(
            d.get("count", 0) for d in decisions
            if d.get("method", "") in _FREE_METHODS
        )
        zero_pct = (zero_cost / total_hits * 100) if total_hits > 0 else 0.0

        today_saved = savings.get("today", 0.0)
        lifetime_saved = savings.get("lifetime", 0.0)

        # ── Left: routing breakdown ──────────────────────────────────────────
        policy_sym, policy_color = _POLICY_STYLES.get(
            routing_policy, ("⚙️", PALETTE.text_dim)
        )
        left_lines: list[RenderableType] = [
            Text.assemble(
                (f"ROUTING  today  {total_hits} decisions", f"bold {PALETTE.accent}"),
            ),
            Text(""),
        ]

        max_name_len = max(
            (len(_METHOD_SHORT.get(d.get("method", ""), d.get("method", "")[:12]))
             for d in decisions),
            default=10,
        )
        for d in decisions:
            method = d.get("method", "unknown")
            count = d.get("count", 0)
            pct = (count / total_hits * 100) if total_hits > 0 else 0
            symbol = d.get("symbol") or _METHOD_SYMBOLS.get(method, "❓")
            short = d.get("short") or _METHOD_SHORT.get(method, method[:12])
            method_color = _METHOD_COLORS.get(method, PALETTE.text_primary)
            pct_str = f"{pct:>3.0f}%"
            # color the percentage bar segment by how dominant the method is
            pct_color = PALETTE.success if pct >= 50 else (
                PALETTE.warning if pct >= 20 else PALETTE.text_dim
            )
            left_lines.append(Text.assemble(
                (f"  {symbol} ", PALETTE.text_dim),
                (f"{short:<{max_name_len}}", method_color),
                (f"  {count:>3}  ", PALETTE.text_primary),
                (pct_str, pct_color),
            ))

        left_lines.append(Text(""))
        zc_bar = self._colored_quota_bar(zero_pct, width=12)
        zc_line = Text.assemble(
            ("  Zero-cost: ", PALETTE.success),
            zc_bar,
            (f" {zero_pct:.0f}%", PALETTE.success),
        )
        left_lines.append(zc_line)

        # Policy indicator
        left_lines.append(Text(""))
        left_lines.append(Text.assemble(
            ("  Policy ", PALETTE.text_dim),
            (f"{policy_sym} ", PALETTE.text_dim),
            (routing_policy, policy_color),
        ))

        # Routing effectiveness score (cheap routing %)
        if routing_effectiveness_pct > 0:
            eff_bar = self._colored_quota_bar(routing_effectiveness_pct, width=12)
            left_lines.append(Text(""))
            left_lines.append(Text.assemble(
                ("  Effective:", PALETTE.success),
                (" ", ""),
                eff_bar,
                (f" {routing_effectiveness_pct:.0f}%", PALETTE.success),
            ))

        # Fallback / escalation rate
        if fallback_count > 0 or escalation_count > 0:
            fb_color = PALETTE.warning if fallback_pct > 10 else PALETTE.text_dim
            esc_color = PALETTE.error if escalation_pct > 10 else PALETTE.text_dim
            left_lines.append(Text(""))
            fb_str = f"  Fallback {fallback_count} ({fallback_pct:.0f}%)"
            esc_str = f"  Escalated {escalation_count} ({escalation_pct:.0f}%)"
            left_lines.append(Text(fb_str, style=fb_color))
            left_lines.append(Text(esc_str, style=esc_color))

        # Session vs typical comparison
        if session_cost_ratio is not None:
            left_lines.append(Text(""))
            if session_cost_ratio >= 2.0:
                ratio_color = PALETTE.error
                ratio_sym = "↑↑"
            elif session_cost_ratio >= 1.3:
                ratio_color = PALETTE.warning
                ratio_sym = "↑"
            elif session_cost_ratio <= 0.5:
                ratio_color = PALETTE.success
                ratio_sym = "↓↓"
            else:
                ratio_color = PALETTE.text_dim
                ratio_sym = "~"
            left_lines.append(Text.assemble(
                ("  vs typical ", PALETTE.text_dim),
                (f"{ratio_sym} {session_cost_ratio:.1f}×", ratio_color),
                (" cost", PALETTE.text_dim),
            ))

        # ── Right: savings summary ───────────────────────────────────────────
        def _savings_entry(usd: float, tokens: int, label: str,
                           style: str) -> list[RenderableType]:
            # Label FIRST. At this panel width a trailing label wraps to the next
            # line, and a reader then pairs it with the value below — so lifetime,
            # today and 14-day were each being read as the wrong figure. Leading
            # with the label means an overflow pushes the token count down instead,
            # which cannot be mistaken for a different metric.
            tok_part = f"  {_fmt_tok(tokens)} tok" if tokens > 0 else ""
            return [Text.assemble(
                (f"  {label:<9}", PALETTE.text_dim),
                (f"{_fmt_usd(usd):<10}", style),
                (tok_part, PALETTE.text_dim),
            )]

        right_lines: list[RenderableType] = [
            Text("SAVINGS  all sessions", style=f"bold {PALETTE.success}"),
            Text(""),
            *_savings_entry(lifetime_saved, savings.get("lifetime_tokens", 0),
                            "lifetime", PALETTE.success),
            *_savings_entry(today_saved, savings.get("today_tokens", 0),
                            "today", PALETTE.text_primary),
        ]
        # Show 14-day actuals + projections derived from rolling average
        saved_14d = savings.get("14d", 0.0)
        if saved_14d > 0:
            right_lines.extend(
                _savings_entry(saved_14d, savings.get("14d_tokens", 0),
                               "14-day", PALETTE.text_dim)
            )
            daily_avg = saved_14d / 14
            projected_monthly = daily_avg * 30
            projected_yearly = daily_avg * 365
            right_lines.append(Text(""))
            right_lines.append(
                Text("  projected (14-day avg)", style=PALETTE.text_dim)
            )
            right_lines.append(Text.assemble(
                (f"  ~{_fmt_usd(projected_monthly):<8}", PALETTE.success),
                ("/month", PALETTE.text_dim),
            ))
            right_lines.append(Text.assemble(
                (f"  ~{_fmt_usd(projected_yearly):<8}", PALETTE.success),
                ("/year", PALETTE.text_dim),
            ))
        else:
            # Fallback: show week/month if no 14-day data yet
            for key, label in (("week", "week"), ("month", "month")):
                amount = savings.get(key, 0.0)
                if amount > 0:
                    right_lines.extend(
                        _savings_entry(amount, savings.get(f"{key}_tokens", 0),
                                       label, PALETTE.text_dim)
                    )

        # Burn rate + projected spend
        if burn_rate_per_hr > 0:
            right_lines.append(Text(""))
            hr_str = f"{_fmt_usd(burn_rate_per_hr)}/hr"
            projected_day = burn_rate_per_hr * 8
            proj_str = f"~{_fmt_usd(projected_day)}/active-day"
            burn_color = (
                PALETTE.error if burn_rate_per_hr > 1.0
                else PALETTE.warning if burn_rate_per_hr > 0.1
                else PALETTE.text_dim
            )
            right_lines.append(Text.assemble(
                ("  ⚡ ", PALETTE.warning),
                (hr_str, burn_color),
            ))
            right_lines.append(Text(f"  {proj_str}", style=PALETTE.text_dim))
        elif session_cost_usd > 0:
            right_lines.append(Text(""))
            right_lines.append(
                Text(f"  session {_fmt_usd(session_cost_usd)}", style=PALETTE.text_dim)
            )

        # ── Grid: left + right columns ───────────────────────────────────────
        grid = Table.grid(expand=True, padding=(0, 2))
        grid.add_column(ratio=3)
        grid.add_column(ratio=2)
        grid.add_row(Group(*left_lines), Group(*right_lines))

        # ── Quota section (full-width, below grid) ───────────────────────────
        quota_lines: list[RenderableType] = []

        if claude_quota_pct > 0 or claude_session_pct > 0:
            quota_lines.append(Text(""))
            quota_lines.append(
                Text("  QUOTA  Claude Subscription  live", style=f"bold {PALETTE.warning}")
            )

            if claude_session_pct > 0:
                bar = self._colored_quota_bar(claude_session_pct)
                delta_str = ""
                if session_delta_pct is not None:
                    sign = "+" if session_delta_pct >= 0 else ""
                    delta_str = f"  {sign}{session_delta_pct:.1f}pp"
                quota_lines.append(Text.assemble(
                    ("   5h ", PALETTE.text_primary),
                    bar,
                    (f"  {claude_session_pct:.0f}%{delta_str}", PALETTE.text_primary),
                ))
                reset_str = self._format_resets_at(claude_session_resets_at)
                if reset_str:
                    try:
                        dt = datetime.datetime.fromisoformat(
                            claude_session_resets_at.replace("Z", "+00:00")
                        )
                        local_str = dt.astimezone().strftime("%I:%M%p %Z").lstrip("0").lower()
                        quota_lines.append(
                            Text(f"  {reset_str} ({local_str})", style=PALETTE.text_dim)
                        )
                    except Exception:
                        quota_lines.append(Text(f"  {reset_str}", style=PALETTE.text_dim))

            if claude_quota_pct > 0:
                bar = self._colored_quota_bar(claude_quota_pct)
                delta_str = ""
                if weekly_delta_pct is not None:
                    sign = "+" if weekly_delta_pct >= 0 else ""
                    delta_str = f"  {sign}{weekly_delta_pct:.1f}pp"
                quota_lines.append(Text.assemble(
                    ("   weekly ", PALETTE.text_primary),
                    bar,
                    (f"  {claude_quota_pct:.0f}%{delta_str}", PALETTE.text_primary),
                ))
                reset_str = self._format_resets_at(claude_weekly_resets_at)
                if reset_str:
                    quota_lines.append(Text(f"  {reset_str}", style=PALETTE.text_dim))

        if gemini_quota_pct > 0:
            quota_lines.append(Text(""))
            quota_lines.append(
                Text("  Gemini API", style=f"bold {PALETTE.text_primary}")
            )
            bar = self._colored_quota_bar(gemini_quota_pct)
            quota_lines.append(Text.assemble(
                ("   daily rate ", PALETTE.text_primary),
                bar,
                (f"  {gemini_quota_pct:.0f}%", PALETTE.text_primary),
            ))
            gemini_reset = gemini_resets_at
            if not gemini_reset:
                # Gemini daily quota resets at midnight UTC
                tomorrow = (
                    datetime.datetime.now(datetime.timezone.utc).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    ) + datetime.timedelta(days=1)
                )
                gemini_reset = tomorrow.isoformat()
            reset_str = self._format_resets_at(gemini_reset)
            if reset_str:
                quota_lines.append(Text(f"  {reset_str}", style=PALETTE.text_dim))

        if codex_quota_pct > 0:
            quota_lines.append(Text(""))
            quota_lines.append(
                Text("  Codex (OpenAI)", style=f"bold {PALETTE.text_primary}")
            )
            bar = self._colored_quota_bar(codex_quota_pct)
            quota_lines.append(Text.assemble(
                ("   quota ", PALETTE.text_primary),
                bar,
                (f"  {codex_quota_pct:.0f}%", PALETTE.text_primary),
            ))
            if codex_resets_at:
                reset_str = self._format_resets_at(codex_resets_at)
                if reset_str:
                    quota_lines.append(Text(f"  {reset_str}", style=PALETTE.text_dim))

        # ── Flexible extra subscriptions ──────────────────────────────────────
        for sub in (subscriptions or []):
            name = sub.get("name", "Unknown")
            pct = float(sub.get("pct", 0))
            resets_at = sub.get("resets_at", "")
            window = sub.get("window", "quota")
            quota_lines.append(Text(""))
            quota_lines.append(
                Text(f"  {name}", style=f"bold {PALETTE.text_primary}")
            )
            bar = self._colored_quota_bar(pct)
            quota_lines.append(Text.assemble(
                (f"   {window:<10} ", PALETTE.text_primary),
                bar,
                (f"  {pct:.0f}%", PALETTE.text_primary),
            ))
            reset_str = self._format_resets_at(resets_at)
            if reset_str:
                quota_lines.append(Text(f"  {reset_str}", style=PALETTE.text_dim))

        # ── Models section: per-model calls / tokens / cost / savings ─────────
        model_lines: list[RenderableType] = []
        effective_models = [m for m in (session_models or []) if m.get("calls", 0) > 0]

        # Quality tier → color: top-tier (green), mid-tier (accent/blue), budget (dim)
        def _model_tier_color(model_name: str, provider: str) -> str:
            if provider in {"ollama", "codex", "gemini_cli"}:
                return PALETTE.success          # free local → green
            top_tier = {"claude-opus", "o3", "gpt-5", "gemini-2.5-pro", "grok-3"}
            mid_tier = {"claude-sonnet", "gpt-4o", "gemini-2.5-flash", "deepseek"}
            lower = model_name.lower()
            if any(t in lower for t in top_tier):
                return PALETTE.violet           # premium → violet
            if any(t in lower for t in mid_tier):
                return PALETTE.accent           # balanced → blue
            return PALETTE.text_primary         # budget → default

        if effective_models:
            model_lines.append(Text(""))
            model_lines.append(
                Text("  MODELS  this session", style=f"bold {PALETTE.violet}")
            )
            total_m_calls = 0
            total_m_tokens = 0
            total_m_cost = 0.0
            total_m_saved = 0.0
            for m in effective_models[:5]:
                model_name = m.get("model", "unknown")
                short = model_name.split("/")[-1][:20]
                calls = m.get("calls", 0)
                tokens = m.get("tokens", 0)
                cost = m.get("cost_usd", m.get("cost", 0.0))
                saved = m.get("saved_usd", m.get("saved", 0.0))
                provider = m.get("provider", "")
                total_m_calls += calls
                total_m_tokens += tokens
                total_m_cost += cost
                total_m_saved += saved
                tok_str = _fmt_tok(tokens) if tokens else "—"
                tier_color = _model_tier_color(short, provider)
                if provider == "subscription":
                    cost_str = "sub"
                    cost_color = PALETTE.success
                elif cost == 0 and saved > 0:
                    cost_str = "free"
                    cost_color = PALETTE.success
                elif cost > 0:
                    cost_str = _fmt_usd(cost)
                    cost_color = PALETTE.warning
                else:
                    cost_str = "—"
                    cost_color = PALETTE.text_dim
                saved_str = f"+{_fmt_usd(saved)}" if saved > 0.001 else ""
                model_lines.append(Text.assemble(
                    ("  ", PALETTE.text_dim),
                    (f"{short:<20}", tier_color),
                    (f" {calls:>3}×", PALETTE.text_primary),
                    (f"  {tok_str:>6}", PALETTE.text_dim),
                    (f"  {cost_str:<6}", cost_color),
                    (f"  {saved_str}", PALETTE.success),
                ))
            model_lines.append(Text.assemble(
                ("  ", PALETTE.text_dim),
                (f"{'total':<20}", PALETTE.text_primary),
                (f" {total_m_calls:>3}×", PALETTE.text_primary),
                (f"  {_fmt_tok(total_m_tokens):>6}", PALETTE.text_dim),
                (f"  {_fmt_usd(total_m_cost):<6}", PALETTE.warning if total_m_cost > 0 else PALETTE.success),
                (f"  saved {_fmt_usd(total_m_saved)}", PALETTE.success),
            ))
        elif model_breakdown:
            # The heading carries WHAT THIS COVERS, not just the window. This panel is
            # built from `routing_decisions`, which only llm_route and llm_auto write —
            # the llm(task=…) family never does (audit doc 27). Rendering it as a bare
            # "14-day mix" presented one tool's routing as though it were all of it.
            model_lines.append(Text(""))
            _mb_title = "  MODELS  14-day mix"
            if model_breakdown_note:
                _mb_title += f"  ·  {model_breakdown_note}"
            model_lines.append(Text(_mb_title, style=f"bold {PALETTE.violet}"))
            for model, pct in sorted(model_breakdown.items(), key=lambda x: -x[1])[:5]:
                short = model.split("/")[-1][:20]
                bar = self._colored_quota_bar(pct, width=14)
                tier_color = _model_tier_color(short, "")
                model_lines.append(Text.assemble(
                    ("  ", PALETTE.text_dim),
                    (f"{short:<20}", tier_color),
                    ("  ", PALETTE.text_dim),
                    bar,
                    (f"  {pct:.0f}%", PALETTE.text_dim),
                ))
        elif model_breakdown_note:
            # No per-session data AND no classified routes in the window. Rendering
            # nothing here reads as "no routing happened", which is false — the observed
            # case was three days with zero classified routes while `usage` took 643 rows
            # in 24 hours. Absence of data and absence of activity are different facts and
            # a panel that cannot tell them apart is the RED2-02 shape.
            model_lines.append(Text(""))
            model_lines.append(
                Text(f"  MODELS  {model_breakdown_note}", style=f"bold {PALETTE.violet}")
            )

        return Panel(
            Group(grid, *quota_lines, *model_lines),
            border_style=PALETTE.muted_border,
            padding=(1, 2),
            width=70,
        )

    def _sparkline(self, values: list[float], width: int = 14) -> Text:
        """Single-row Unicode block sparkline for a list of float values."""
        BLOCKS = " ▁▂▃▄▅▆▇█"
        if not values or max(values, default=0) == 0:
            return Text("─" * width, style=PALETTE.text_dim)
        max_v = max(values)
        # downsample or upsample to exactly `width` slots
        out = Text()
        for i in range(width):
            src = i * (len(values) - 1) / max(width - 1, 1)
            j = int(src)
            k = min(j + 1, len(values) - 1)
            v = values[j] * (1 - (src - j)) + values[k] * (src - j)
            idx = max(0, min(8, int(v / max_v * 8)))
            out.append(BLOCKS[idx], style=PALETTE.success)
        return out

    def render_activity_panel(
        self,
        daily_calls: list[int],
        daily_tokens: list[int],
        daily_costs: list[float],
        total_saved: float = 0.0,
        overhead_ms: float = 0.0,
        cache_hit_pct: float = 0.0,
        p95_latency: dict[str, float] | None = None,
        daily_cache_trend: list[float] | None = None,
        daily_tokens_saved: list[int] | None = None,
    ) -> RenderableType:
        """14-day activity panel: three bar charts side by side + stats."""
        N_ROWS = 8
        today = datetime.date.today()

        def _date_xaxis(n: int, y_width: int) -> list[Text]:
            """Compact date axis: oldest (d/m) left, today (d/m) right, and a
            mid-point day between them WHEN it fits with a gap on each side.

            The old code placed the 4-char start label at pos 0 and the mid label
            at (n-1)//2 with no collision guard, so for a narrow chart (n≈9) they
            ran together — "23/6" + "27" rendered as "23/627".
            """
            prefix = " " * y_width
            axis_line = f"{prefix} └{'─' * n}"
            if n < 2:
                return [Text(axis_line, style=PALETTE.text_dim)]
            return [
                Text(axis_line, style=PALETTE.text_dim),
                Text(f"{prefix} " + _date_axis_label_row(n, today), style=PALETTE.text_dim),
            ]

        # ── Chart 1: calls/day ────────────────────────────────────────────────
        n = min(14, len(daily_calls)) if daily_calls else 0
        values = daily_calls[-n:] if n > 0 else []
        calls_lines: list[RenderableType] = [Text("calls/day", style=PALETTE.text_dim)]
        if values and max(values) > 0:
            max_val = max(values)
            y_width = max(len(str(max_val)), 3)
            for i, row_chars in enumerate(self._bar_chart_rows(values, n_rows=N_ROWS)):
                y_val = int(max_val * (N_ROWS - 1 - i) / N_ROWS)
                calls_lines.append(Text(f"{y_val:>{y_width}} ┤ {row_chars}", style=PALETTE.accent))
            calls_lines.extend(_date_xaxis(n, y_width + 2))
        else:
            calls_lines.append(Text("no data", style=PALETTE.text_dim))

        # ── Chart 2: savings/day ──────────────────────────────────────────────
        n_s = min(14, len(daily_costs)) if daily_costs else 0
        save_values = daily_costs[-n_s:] if n_s > 0 else []
        save_lines: list[RenderableType] = [Text("savings/day", style=PALETTE.text_dim)]
        if save_values and max(save_values) > 0.0001:
            max_saved_day = max(save_values)
            scaled = [int(v * 100_000) for v in save_values]
            y_width_s = 7
            for i, row_chars in enumerate(self._bar_chart_rows(scaled, n_rows=N_ROWS)):
                y_val = max_saved_day * (N_ROWS - 1 - i) / N_ROWS
                y_str = _fmt_usd(y_val) if y_val >= 0.0001 else "$0"
                save_lines.append(Text.assemble(
                    (f"{y_str:>{y_width_s}} ┤ ", PALETTE.text_dim),
                    (row_chars, PALETTE.success),
                ))
            save_lines.extend(_date_xaxis(n_s, y_width_s + 2))
        else:
            save_lines.append(Text("no data", style=PALETTE.text_dim))

        # ── Chart 3: tokens saved/day ─────────────────────────────────────────
        tok_saved_values = (daily_tokens_saved or [])[-14:]
        n_ts = len(tok_saved_values)
        tok_lines: list[RenderableType] = [Text("tokens saved/day", style=PALETTE.text_dim)]
        if tok_saved_values and max(tok_saved_values) > 0:
            max_ts = max(tok_saved_values)
            _ts_div, _ts_suf = _tok_axis_unit(max_ts)  # lock the whole axis to one unit
            y_width_ts = max(len(_fmt_tok_axis(max_ts, _ts_div, _ts_suf)), 4)
            for i, row_chars in enumerate(self._bar_chart_rows(tok_saved_values, n_rows=N_ROWS)):
                y_val = int(max_ts * (N_ROWS - 1 - i) / N_ROWS)
                tok_lines.append(Text.assemble(
                    (f"{_fmt_tok_axis(y_val, _ts_div, _ts_suf):>{y_width_ts}} ┤ ", PALETTE.text_dim),
                    (row_chars, PALETTE.accent),
                ))
            tok_lines.extend(_date_xaxis(n_ts, y_width_ts + 2))
        else:
            tok_lines.append(Text("no data", style=PALETTE.text_dim))

        # ── Three charts side by side ─────────────────────────────────────────
        charts = Table.grid(expand=True, padding=(0, 1))
        charts.add_column(ratio=1)
        charts.add_column(ratio=1)
        charts.add_column(ratio=1)
        charts.add_row(Group(*calls_lines), Group(*save_lines), Group(*tok_lines))

        # ── Summary stats line ────────────────────────────────────────────────
        lines: list[RenderableType] = [charts, Text("")]

        total_calls = sum(daily_calls[-n:]) if daily_calls and n > 0 else 0
        total_tokens = sum(daily_tokens[-n:]) if daily_tokens and n > 0 else 0
        avg_calls = total_calls // max(n, 1)
        avg_saved = (total_saved / max(n, 1)) if n > 0 and total_saved > 0 else 0.0

        lines.append(
            Text(
                f"  {total_calls:,} calls · {_fmt_tok(total_tokens)} tok · "
                f"{_fmt_usd(total_saved)} over {n} days",
                style=PALETTE.text_primary,
            )
        )

        stats: list[str] = [f"avg {avg_calls}/day"]
        if avg_saved > 0.0001:
            stats.append(f"avg {_fmt_usd(avg_saved)}/day saved")
        if overhead_ms > 0:
            stats.append(f"{overhead_ms:.0f}ms routing overhead")
        if cache_hit_pct > 0:
            stats.append(f"{cache_hit_pct:.0f}% cache hit")
        lines.append(Text(f"  {' · '.join(stats)}", style=PALETTE.text_dim))

        # ── p95 Latency per tier ──────────────────────────────────────────────
        p95 = p95_latency or {}
        _TIER_LABELS = [
            ("simple",        "cheap  "),
            ("moderate",      "mid    "),
            ("complex",       "premium"),
            ("deep_reasoning","reason "),
        ]
        tier_entries = [(lbl, p95[k]) for k, lbl in _TIER_LABELS if k in p95]
        if tier_entries:
            lines.append(Text(""))
            lines.append(Text("p95 latency", style=PALETTE.text_dim))
            parts = []
            for lbl, secs in tier_entries:
                if secs >= 10:
                    lat_str = f"{secs:.0f}s"
                    lat_color = PALETTE.warning
                elif secs >= 3:
                    lat_str = f"{secs:.1f}s"
                    lat_color = PALETTE.text_primary
                else:
                    lat_str = f"{secs:.1f}s"
                    lat_color = PALETTE.success
                parts.append(Text.assemble(
                    (f"  {lbl}", PALETTE.text_dim),
                    (f" {lat_str}", lat_color),
                ))
            lines.append(Text.assemble(*[p for pair in parts for p in [pair, Text("  ")]]))

        # ── Cache hit rate trend sparkline ────────────────────────────────────
        trend = daily_cache_trend or []
        if trend and any(v > 0 for v in trend):
            avg_hit = sum(trend) / len(trend)
            lines.append(Text(""))
            spark = self._sparkline(trend, width=min(14, len(trend)))
            lines.append(Text.assemble(
                ("cache hits  ", PALETTE.text_dim),
                spark,
                (f"  avg {avg_hit:.0f}%", PALETTE.success),
            ))

        return Panel(
            Group(*lines),
            border_style=PALETTE.muted_border,
            title="14-DAY ACTIVITY",
            title_align="left",
            width=100,
        )

    def render_quota_graph(
        self,
        quota_samples: list[tuple[str, float]],
        label: str = "Claude weekly",
    ) -> RenderableType:
        """Line graph: subscription quota % over last 10h in 10-min buckets.

        Args:
            quota_samples: list of (iso_timestamp, pct_0_to_100) pairs
            label: quota label shown in footer
        """
        PLOT_W = 58
        PLOT_H = 7

        now = datetime.datetime.now(datetime.timezone.utc)
        window_start = now - datetime.timedelta(hours=10)
        N_BUCKETS = 60  # 10h / 10-min

        buckets: list[float | None] = [None] * N_BUCKETS
        for ts_str, raw_pct in quota_samples:
            try:
                ts = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts < window_start:
                    continue
                elapsed = (ts - window_start).total_seconds()
                idx = min(N_BUCKETS - 1, int(elapsed / 600))
                buckets[idx] = raw_pct
            except Exception:
                continue

        # Forward-fill: carry last known value forward
        last_known: float | None = None
        for i, v in enumerate(buckets):
            if v is not None:
                last_known = v
            elif last_known is not None:
                buckets[i] = last_known

        values = [v if v is not None else 0.0 for v in buckets]
        has_data = any(v > 0 for v in values)

        no_data_text = Text(
            "  No quota timeline data for this session", style=PALETTE.text_dim
        )
        if not has_data:
            return Panel(
                no_data_text,
                border_style=PALETTE.muted_border,
                title="QUOTA TIMELINE  last 10h · 10-min",
                title_align="left",
                width=70,
            )

        # Downsample to PLOT_W columns via linear interpolation
        xs: list[float] = []
        for col in range(PLOT_W):
            src = col * (len(values) - 1) / max(1, PLOT_W - 1)
            i = int(src)
            j = min(i + 1, len(values) - 1)
            t = src - i
            xs.append(values[i] * (1 - t) + values[j] * t)

        def y_of(v: float) -> int:
            return round((100 - max(0, min(100, v))) * (PLOT_H - 1) / 100)

        ys = [y_of(v) for v in xs]
        grid = [[" "] * PLOT_W for _ in range(PLOT_H)]

        # Draw line segments with box-drawing chars
        for x in range(PLOT_W - 1):
            y1, y2 = ys[x], ys[x + 1]
            if y1 == y2:
                grid[y1][x] = "─"
            elif y2 < y1:  # rising (quota increasing)
                grid[y1][x] = "╰"
                for y in range(y2 + 1, y1):
                    grid[y][x] = "│"
                if x + 1 < PLOT_W:
                    grid[y2][x + 1] = "╮"
            else:  # falling
                grid[y1][x] = "╭"
                for y in range(y1 + 1, y2):
                    grid[y][x] = "│"
                if x + 1 < PLOT_W:
                    grid[y2][x + 1] = "╯"

        grid[ys[-1]][-1] = "●"  # current value marker

        y_label_map = {y_of(p): f"{p}%" for p in (100, 75, 50, 25, 0)}
        lines: list[RenderableType] = []

        for row in range(PLOT_H):
            y_lbl = y_label_map.get(row, "   ")
            row_str = "".join(grid[row])
            pct_at_row = 100 - round(row * 100 / (PLOT_H - 1))
            if pct_at_row >= 80:
                style = PALETTE.error if hasattr(PALETTE, "error") else "red"
            elif pct_at_row >= 50:
                style = PALETTE.warning if hasattr(PALETTE, "warning") else "yellow"
            else:
                style = PALETTE.accent
            t = Text()
            t.append(f"{y_lbl:>4} ", style=PALETTE.text_dim)
            t.append(row_str, style=style)
            lines.append(t)

        # X-axis labels
        axis = [" "] * PLOT_W
        for frac, lbl in ((0.0, "10h"), (0.2, "8h"), (0.4, "6h"), (0.6, "4h"), (0.8, "2h"), (1.0, "now")):
            pos = min(PLOT_W - len(lbl), round(frac * (PLOT_W - 1)))
            for i, ch in enumerate(lbl):
                axis[pos + i] = ch
        lines.append(Text("     " + "".join(axis), style=PALETTE.text_dim))

        # Footer: current value + session delta
        current = values[-1]
        start_val = next((v for v in values if v > 0), values[0])
        delta = current - start_val
        sign = "+" if delta >= 0 else ""
        lines.append(
            Text(
                f"  {label}: {current:.1f}%  session +{sign}{delta:.1f}pp",
                style=PALETTE.text_primary,
            )
        )

        return Panel(
            Group(*lines),
            border_style=PALETTE.muted_border,
            title="QUOTA TIMELINE  last 10h · 10-min",
            title_align="left",
            width=70,
        )

    def render_full_dashboard(
        self,
        timestamp: str = "",
        decisions: list[dict] | None = None,
        savings: dict | None = None,
        daily_calls: list[int] | None = None,
        daily_tokens: list[int] | None = None,
        daily_costs: list[float] | None = None,
        total_saved: float = 0.0,
        model_breakdown: dict[str, float] | None = None,
        model_breakdown_note: str | None = None,
        session_models: list[dict] | None = None,
        models: list[dict] | None = None,
        claude_quota_pct: float = 0.0,
        claude_session_pct: float = 0.0,
        claude_session_resets_at: str = "",
        claude_weekly_resets_at: str = "",
        gemini_quota_pct: float = 0.0,
        gemini_resets_at: str = "",
        codex_quota_pct: float = 0.0,
        codex_resets_at: str = "",
        codex_remaining: str = "",
        claude_remaining: str = "Unknown",
        gemini_remaining: str = "Unknown",
        subscriptions: list[dict] | None = None,
        session_delta_pct: float | None = None,
        weekly_delta_pct: float | None = None,
        overhead_ms: float = 0.0,
        cache_hit_pct: float = 0.0,
        quota_samples: list[tuple[str, float]] | None = None,
        # New session metrics
        burn_rate_per_hr: float = 0.0,
        session_cost_usd: float = 0.0,
        fallback_pct: float = 0.0,
        escalation_pct: float = 0.0,
        fallback_count: int = 0,
        escalation_count: int = 0,
        routing_effectiveness_pct: float = 0.0,
        session_cost_ratio: float | None = None,
        session_calls_ratio: float | None = None,
        p95_latency: dict[str, float] | None = None,
        daily_cache_trend: list[float] | None = None,
        daily_tokens_saved: list[int] | None = None,
    ) -> RenderableType:
        """Two-panel dashboard: main summary + 14-day activity + quota timeline."""
        try:
            from llm_router.config import get_config
            _routing_policy = get_config().llm_router_routing_policy
        except Exception:
            _routing_policy = "balanced"

        main = self.render_main_panel(
            timestamp=timestamp,
            decisions=decisions,
            savings=savings,
            claude_quota_pct=claude_quota_pct,
            claude_session_pct=claude_session_pct,
            claude_session_resets_at=claude_session_resets_at,
            claude_weekly_resets_at=claude_weekly_resets_at,
            gemini_quota_pct=gemini_quota_pct,
            gemini_resets_at=gemini_resets_at,
            codex_quota_pct=codex_quota_pct,
            codex_resets_at=codex_resets_at,
            session_delta_pct=session_delta_pct,
            weekly_delta_pct=weekly_delta_pct,
            model_breakdown=model_breakdown,
            model_breakdown_note=model_breakdown_note,
            session_models=session_models,
            subscriptions=subscriptions,
            routing_policy=_routing_policy,
            burn_rate_per_hr=burn_rate_per_hr,
            session_cost_usd=session_cost_usd,
            fallback_pct=fallback_pct,
            escalation_pct=escalation_pct,
            fallback_count=fallback_count,
            escalation_count=escalation_count,
            routing_effectiveness_pct=routing_effectiveness_pct,
            session_cost_ratio=session_cost_ratio,
            session_calls_ratio=session_calls_ratio,
        )
        activity = self.render_activity_panel(
            daily_calls=daily_calls or [],
            daily_tokens=daily_tokens or [],
            daily_costs=daily_costs or [],
            total_saved=total_saved,
            overhead_ms=overhead_ms,
            cache_hit_pct=cache_hit_pct,
            p95_latency=p95_latency,
            daily_cache_trend=daily_cache_trend,
            daily_tokens_saved=daily_tokens_saved,
        )
        panels: list[RenderableType] = [Text(""), main, Text(""), activity]
        if quota_samples:
            quota_graph = self.render_quota_graph(quota_samples)
            panels.extend([Text(""), quota_graph])
        return Group(*panels)

    def print_dashboard(self, **kwargs) -> None:
        """Render and print complete dashboard to console."""
        self.console.print(self.render_full_dashboard(**kwargs))
