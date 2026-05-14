"""LLM Router — Mission Control TUI Dashboard.

A sophisticated terminal UI dashboard built on Textual that provides
real-time visibility into routing metrics, subscription usage, cost
savings, and historical activity patterns.

Launch:
    python -m llm_router.dashboard.tui
    # or: llm-router tui
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.timer import Timer
from textual.widgets import Footer, Static

# ── Paths ─────────────────────────────────────────────────────────────────────

STATE_DIR = Path.home() / ".llm-router"
DB_PATH = STATE_DIR / "usage.db"
USAGE_JSON = STATE_DIR / "usage.json"
SAVINGS_LOG = STATE_DIR / "savings_log.jsonl"
SESSION_SPEND = STATE_DIR / "session_spend.json"

# ── Tokyo Night Palette (true-color hex) ──────────────────────────────────────

class TN:
    """Tokyo Night Storm palette — muted bg, vibrant accents."""
    BG = "#1a1b26"
    BG_DARK = "#16161e"
    BG_HIGHLIGHT = "#292e42"
    FG = "#c0caf5"
    FG_DIM = "#565f89"
    FG_DARK = "#3b4261"
    COMMENT = "#565f89"
    CYAN = "#7dcfff"
    BLUE = "#7aa2f7"
    MAGENTA = "#bb9af7"
    GREEN = "#9ece6a"
    YELLOW = "#e0af68"
    ORANGE = "#ff9e64"
    RED = "#f7768e"
    TEAL = "#73daca"
    SPRING = "#9ece6a"
    CORAL = "#f7768e"

# ── Braille Sparkline Renderer ────────────────────────────────────────────────

BRAILLE_BASE = 0x2800
# Braille dot positions (row, col) -> bit offset
# Col 0: dots 1,2,3,7  Col 1: dots 4,5,6,8
_DOT_MAP = [
    [0x01, 0x08],  # row 0
    [0x02, 0x10],  # row 1
    [0x04, 0x20],  # row 2
    [0x40, 0x80],  # row 3
]


def braille_sparkline(values: list[float], width: int = 30, height: int = 4) -> list[str]:
    """Render a list of values as a dense Braille-pattern graph.

    Each character encodes a 2-wide x 4-tall dot matrix, giving
    ``width * 2`` data points horizontally and ``height * 4`` vertical
    resolution levels.
    """
    if not values:
        return [" " * width] * height

    cols = width * 2
    rows = height * 4

    # Resample values to fit cols
    resampled: list[float] = []
    for i in range(cols):
        idx = i * len(values) / cols
        lo = int(idx)
        hi = min(lo + 1, len(values) - 1)
        frac = idx - lo
        resampled.append(values[lo] * (1 - frac) + values[hi] * frac)

    v_min = min(resampled)
    v_max = max(resampled)
    v_range = v_max - v_min if v_max > v_min else 1.0

    # Normalize to 0..rows-1
    scaled = [int((v - v_min) / v_range * (rows - 1)) for v in resampled]

    # Build grid (row 0 = top)
    grid: list[list[bool]] = [[False] * cols for _ in range(rows)]
    for col_idx, level in enumerate(scaled):
        # Fill from bottom up to level
        for r in range(rows - 1, rows - 1 - level - 1, -1):
            if 0 <= r < rows:
                grid[r][col_idx] = True

    # Encode to braille characters
    lines: list[str] = []
    for block_row in range(height):
        line = ""
        for block_col in range(width):
            code = BRAILLE_BASE
            for dr in range(4):
                for dc in range(2):
                    gr = block_row * 4 + dr
                    gc = block_col * 2 + dc
                    if gr < rows and gc < cols and grid[gr][gc]:
                        code |= _DOT_MAP[dr][dc]
            line += chr(code)
        lines.append(line)
    return lines


def mini_sparkline(values: list[float], width: int = 14) -> str:
    """Single-row sparkline using block characters."""
    blocks = " " + "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
    if not values:
        return " " * width
    mx = max(values) if max(values) > 0 else 1
    out = ""
    step = max(1, len(values) // width)
    for i in range(0, min(len(values), width * step), step):
        idx = int(values[i] / mx * 7)
        out += blocks[min(idx, 7)]
    return out[:width].ljust(width)

# ── Data Layer (sync SQLite reads) ────────────────────────────────────────────


@dataclass
class DashboardData:
    """Snapshot of all metrics consumed by the TUI."""
    # Session / subscription
    session_pct: float = 0.0
    weekly_pct: float = 0.0
    model_breakdown: dict[str, float] = field(default_factory=dict)
    plan_name: str = "Pro"

    # Calls & costs
    total_calls: int = 0
    today_calls: int = 0
    today_cost: float = 0.0
    today_tokens: int = 0
    month_calls: int = 0
    month_cost: float = 0.0

    # Model usage
    models: list[dict] = field(default_factory=list)
    task_types: list[dict] = field(default_factory=list)
    profiles: list[dict] = field(default_factory=list)
    complexity_breakdown: dict[str, int] = field(default_factory=dict)

    # Routing engine
    heuristic_count: int = 0
    ollama_count: int = 0
    api_count: int = 0
    total_decisions: int = 0
    zero_cost_pct: float = 0.0

    # Savings
    today_saved: float = 0.0
    week_saved: float = 0.0
    month_saved: float = 0.0
    lifetime_saved: float = 0.0
    today_saved_calls: int = 0
    week_saved_calls: int = 0
    month_saved_calls: int = 0
    lifetime_saved_calls: int = 0
    lifetime_tokens: int = 0
    lifetime_cost: float = 0.0

    # L14 daily series
    daily_calls: list[int] = field(default_factory=list)
    daily_tokens: list[int] = field(default_factory=list)
    daily_cost: list[float] = field(default_factory=list)
    daily_labels: list[str] = field(default_factory=list)
    l14_total_calls: int = 0
    l14_total_tokens: int = 0
    l14_savings: float = 0.0


def _read_usage_json() -> dict:
    try:
        return json.loads(USAGE_JSON.read_text())
    except Exception:
        return {}


def _fetch_data() -> DashboardData:
    """Synchronous data fetch from SQLite + JSON files."""
    d = DashboardData()

    # ── Usage JSON (subscription) ─────────────────────────────────────
    uj = _read_usage_json()
    d.session_pct = uj.get("session_pct", 0.0)
    d.weekly_pct = uj.get("weekly_pct", 0.0)
    d.plan_name = uj.get("plan", "Pro")
    d.model_breakdown = uj.get("model_breakdown", {})

    if not DB_PATH.exists():
        return d

    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=3)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=2000")
    except Exception:
        return d

    try:
        # ── Today ─────────────────────────────────────────────────────
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost_usd),0), "
            "COALESCE(SUM(input_tokens+output_tokens),0) "
            "FROM usage WHERE timestamp >= datetime('now','start of day')"
        ).fetchone()
        if row:
            d.today_calls, d.today_cost, d.today_tokens = row[0], row[1], row[2]

        # ── Month ─────────────────────────────────────────────────────
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost_usd),0) FROM usage "
            "WHERE timestamp >= datetime('now','start of month')"
        ).fetchone()
        if row:
            d.month_calls, d.month_cost = row[0], row[1]

        # ── Total / lifetime ──────────────────────────────────────────
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(input_tokens+output_tokens),0), "
            "COALESCE(SUM(cost_usd),0) FROM usage"
        ).fetchone()
        if row:
            d.total_calls, d.lifetime_tokens, d.lifetime_cost = row[0], row[1], row[2]

        # ── Top models (7d) ───────────────────────────────────────────
        rows = conn.execute(
            "SELECT model, COUNT(*) as n, COALESCE(SUM(cost_usd),0), "
            "COALESCE(AVG(latency_ms),0) "
            "FROM usage WHERE timestamp >= datetime('now','-7 days') "
            "GROUP BY model ORDER BY n DESC LIMIT 8"
        ).fetchall()
        d.models = [
            {"model": r[0], "calls": r[1], "cost": round(r[2], 6), "latency": round(r[3], 0)}
            for r in rows
        ]

        # ── Task types (7d) ───────────────────────────────────────────
        rows = conn.execute(
            "SELECT task_type, COUNT(*) FROM usage "
            "WHERE timestamp >= datetime('now','-7 days') GROUP BY task_type"
        ).fetchall()
        d.task_types = [{"type": r[0], "calls": r[1]} for r in rows]

        # ── Profiles (7d) ─────────────────────────────────────────────
        rows = conn.execute(
            "SELECT profile, COUNT(*) FROM usage "
            "WHERE timestamp >= datetime('now','-7 days') GROUP BY profile"
        ).fetchall()
        d.profiles = [{"profile": r[0], "calls": r[1]} for r in rows]

        # ── Complexity breakdown (7d) ─────────────────────────────────
        try:
            rows = conn.execute(
                "SELECT complexity, COUNT(*) FROM usage "
                "WHERE timestamp >= datetime('now','-7 days') GROUP BY complexity"
            ).fetchall()
            d.complexity_breakdown = {r[0]: r[1] for r in rows if r[0]}
        except Exception:
            pass

        # ── Routing decisions ─────────────────────────────────────────
        try:
            row = conn.execute("SELECT COUNT(*) FROM routing_decisions").fetchone()
            d.total_decisions = row[0] if row else 0

            rows = conn.execute(
                "SELECT classifier_type, COUNT(*) FROM routing_decisions "
                "GROUP BY classifier_type"
            ).fetchall()
            for r in rows:
                ct = (r[0] or "").lower()
                if "heuristic" in ct:
                    d.heuristic_count += r[1]
                elif "ollama" in ct:
                    d.ollama_count += r[1]
                else:
                    d.api_count += r[1]

            row = conn.execute(
                "SELECT COUNT(*) FROM routing_decisions WHERE cost_usd = 0 OR cost_usd IS NULL"
            ).fetchone()
            if row and d.total_decisions > 0:
                d.zero_cost_pct = row[0] / d.total_decisions * 100
        except Exception:
            pass

        # ── Savings computation ───────────────────────────────────────
        SONNET_IN, SONNET_OUT = 3.0, 15.0  # $/M

        def _savings_for(where: str) -> tuple[float, int]:
            row = conn.execute(
                f"SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
                f"COALESCE(SUM(cost_usd),0), COUNT(*) FROM usage WHERE success=1 AND {where}"
            ).fetchone()
            if not row:
                return 0.0, 0
            baseline = (row[0] * SONNET_IN + row[1] * SONNET_OUT) / 1_000_000
            return max(0.0, baseline - row[2]), row[3]

        d.today_saved, d.today_saved_calls = _savings_for(
            "timestamp >= datetime('now','start of day')"
        )
        d.week_saved, d.week_saved_calls = _savings_for(
            "timestamp >= datetime('now','-7 days')"
        )
        d.month_saved, d.month_saved_calls = _savings_for(
            "timestamp >= datetime('now','start of month')"
        )
        d.lifetime_saved, d.lifetime_saved_calls = _savings_for("1=1")

        # ── L14 daily series ──────────────────────────────────────────
        rows = conn.execute(
            "SELECT date(timestamp) as day, COUNT(*), "
            "COALESCE(SUM(input_tokens+output_tokens),0), COALESCE(SUM(cost_usd),0) "
            "FROM usage WHERE timestamp >= datetime('now','-14 days') "
            "GROUP BY day ORDER BY day"
        ).fetchall()
        for r in rows:
            d.daily_labels.append(r[0][-5:])  # MM-DD
            d.daily_calls.append(r[1])
            d.daily_tokens.append(r[2])
            d.daily_cost.append(r[3])

        d.l14_total_calls = sum(d.daily_calls)
        d.l14_total_tokens = sum(d.daily_tokens)
        d.l14_savings = sum(d.daily_cost)  # will subtract from baseline below

    except Exception:
        pass
    finally:
        conn.close()

    return d


# ── Helper: Gradient Progress Bar ─────────────────────────────────────────────

def gradient_bar(pct: float, width: int = 20, label: str = "") -> str:
    """Render a gradient-shaded progress bar with true-color escape codes."""
    pct = max(0.0, min(100.0, pct))
    filled = int(pct / 100 * width)
    empty = width - filled

    # Color gradient: green -> yellow -> orange -> red
    bar = ""
    for i in range(filled):
        ratio = i / max(width - 1, 1)
        if ratio < 0.5:
            r, g, b = _lerp_color((0x9e, 0xce, 0x6a), (0xe0, 0xaf, 0x68), ratio * 2)
        elif ratio < 0.8:
            r, g, b = _lerp_color((0xe0, 0xaf, 0x68), (0xff, 0x9e, 0x64), (ratio - 0.5) / 0.3)
        else:
            r, g, b = _lerp_color((0xff, 0x9e, 0x64), (0xf7, 0x76, 0x8e), (ratio - 0.8) / 0.2)
        bar += f"\033[38;2;{r};{g};{b}m\u2588"

    bar += "\033[38;2;59;66;97m" + "\u2591" * empty + "\033[0m"
    pct_str = f"{pct:.0f}%" if pct >= 1 else f"{pct:.1f}%"
    if label:
        return f"  {label:<12} {bar} {pct_str}"
    return f"  {bar} {pct_str}"


def _lerp_color(c1: tuple[int, ...], c2: tuple[int, ...], t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return (
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t),
    )


def _tc(hex_color: str, text: str) -> str:
    """True-color ANSI text."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"\033[38;2;{r};{g};{b}m{text}\033[0m"


def _tc_bold(hex_color: str, text: str) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"\033[1;38;2;{r};{g};{b}m{text}\033[0m"


def _dim(text: str) -> str:
    return _tc(TN.FG_DIM, text)


# ── Textual Widgets ───────────────────────────────────────────────────────────

class PanelBox(Static):
    """A styled panel container with a title bar."""

    DEFAULT_CSS = """
    PanelBox {
        border: round $accent;
        padding: 0 1;
        margin: 0;
        background: #1a1b26;
    }
    """


class SubscriptionPanel(Static):
    """Panel A — Session Overview & Usage Meters."""

    DEFAULT_CSS = """
    SubscriptionPanel {
        height: auto;
        min-height: 12;
        border: round #7aa2f7;
        padding: 0 1;
        background: #16161e;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(id="sub-content")

    def refresh_data(self, d: DashboardData) -> None:
        lines: list[str] = []
        lines.append(_tc_bold(TN.BLUE, " SUBSCRIPTION STATUS"))
        lines.append(_dim("─" * 36))

        plan_color = TN.GREEN if d.plan_name.lower() in ("pro", "max") else TN.YELLOW
        lines.append(f"  Plan: {_tc_bold(plan_color, d.plan_name.upper())}  "
                      f"{_dim('|')}  Status: {_tc_bold(TN.GREEN, 'ACTIVE')}")
        lines.append("")

        lines.append(gradient_bar(d.session_pct, width=22, label="Session"))
        lines.append(gradient_bar(d.weekly_pct, width=22, label="Weekly"))
        lines.append("")

        # Model-specific bars from breakdown
        if d.model_breakdown:
            lines.append(_dim("  Model Quotas"))
            for model, pct in sorted(d.model_breakdown.items()):
                short = model.split("-")[0].capitalize() if "-" in model else model[:10]
                lines.append(gradient_bar(pct, width=18, label=short[:10]))
        else:
            # Synthetic bars from usage patterns
            opus_est = min(d.session_pct * 1.2, 100)
            sonnet_est = min(d.session_pct * 0.8, 100)
            lines.append(_dim("  Estimated Model Load"))
            lines.append(gradient_bar(opus_est, width=18, label="Opus"))
            lines.append(gradient_bar(sonnet_est, width=18, label="Sonnet"))

        self.query_one("#sub-content").update("\n".join(lines))


class MetricsPanel(Static):
    """Panel B — Dynamic Metrics & Complexity Breakdown."""

    DEFAULT_CSS = """
    MetricsPanel {
        height: auto;
        min-height: 12;
        border: round #bb9af7;
        padding: 0 1;
        background: #16161e;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(id="metrics-content")

    def refresh_data(self, d: DashboardData) -> None:
        lines: list[str] = []
        lines.append(_tc_bold(TN.MAGENTA, " METRICS & COST ANALYSIS"))
        lines.append(_dim("─" * 40))

        # KPI row
        lines.append(
            f"  {_tc(TN.CYAN, 'Today')} {d.today_calls:>5} calls  "
            f"{_tc(TN.YELLOW, f'${d.today_cost:.4f}')}"
        )
        lines.append(
            f"  {_tc(TN.CYAN, 'Month')} {d.month_calls:>5} calls  "
            f"{_tc(TN.YELLOW, f'${d.month_cost:.4f}')}"
        )
        lines.append(
            f"  {_tc(TN.CYAN, 'Total')} {d.total_calls:>5} calls  "
            f"{_tc(TN.YELLOW, f'${d.lifetime_cost:.4f}')}"
        )
        lines.append("")

        # Top models table
        lines.append(_tc(TN.FG_DIM, "  Model              Calls   Cost     Lat"))
        lines.append(_dim("  " + "─" * 38))
        model_colors = [TN.CYAN, TN.BLUE, TN.TEAL, TN.GREEN, TN.MAGENTA,
                        TN.YELLOW, TN.ORANGE, TN.CORAL]
        for i, m in enumerate(d.models[:6]):
            color = model_colors[i % len(model_colors)]
            name = m["model"][:18].ljust(18)
            calls = str(m["calls"]).rjust(5)
            cost = f"${m['cost']:.4f}".rjust(8)
            lat = f"{m['latency']:.0f}ms".rjust(7)
            lines.append(f"  {_tc(color, name)} {calls} {cost} {lat}")

        lines.append("")

        # Complexity breakdown
        if d.complexity_breakdown:
            lines.append(_tc(TN.FG_DIM, "  Complexity Routing (7d)"))
            total_cx = sum(d.complexity_breakdown.values()) or 1
            for cx, count in sorted(d.complexity_breakdown.items()):
                pct = count / total_cx * 100
                bar_w = int(pct / 100 * 16)
                bar = _tc(TN.TEAL, "\u2588" * bar_w) + _tc(TN.FG_DARK, "\u2591" * (16 - bar_w))
                lines.append(f"  {(cx or '?')[:10]:<10} {bar} {pct:>4.0f}% ({count})")

        self.query_one("#metrics-content").update("\n".join(lines))


class RoutingPanel(Static):
    """Panel C — Routing Engine Logic."""

    DEFAULT_CSS = """
    RoutingPanel {
        height: auto;
        min-height: 12;
        border: round #73daca;
        padding: 0 1;
        background: #16161e;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(id="routing-content")

    def refresh_data(self, d: DashboardData) -> None:
        lines: list[str] = []
        lines.append(_tc_bold(TN.TEAL, " ROUTING ENGINE"))
        lines.append(_dim("─" * 32))

        total = d.total_decisions or 1

        heur_pct = d.heuristic_count / total * 100
        olla_pct = d.ollama_count / total * 100
        api_pct = d.api_count / total * 100

        lines.append("")
        ico_heur = "\u26a1"
        ico_fast = "\U0001f528"
        ico_adv = "\U0001f9e0"
        heur_s = f"{heur_pct:>5.1f}%"
        olla_s = f"{olla_pct:>5.1f}%"
        api_s = f"{api_pct:>5.1f}%"
        lines.append(
            f"  {_tc(TN.YELLOW, ico_heur)} {_tc_bold(TN.YELLOW, 'Heuristic')}    "
            f"{_tc(TN.FG, heur_s)}  ({d.heuristic_count})"
        )
        lines.append(
            f"  {_tc(TN.ORANGE, ico_fast)} {_tc_bold(TN.ORANGE, 'Fast-Path')}    "
            f"{_tc(TN.FG, olla_s)}  ({d.ollama_count})"
        )
        lines.append(
            f"  {_tc(TN.MAGENTA, ico_adv)} {_tc_bold(TN.MAGENTA, 'Advanced')}     "
            f"{_tc(TN.FG, api_s)}  ({d.api_count})"
        )

        lines.append("")
        lines.append(_dim("─" * 32))
        lines.append(
            f"  Total decisions: {_tc_bold(TN.CYAN, str(d.total_decisions))}"
        )
        lines.append(
            f"  Zero-cost:       {_tc_bold(TN.GREEN, f'{d.zero_cost_pct:.0f}%')}"
        )

        lines.append("")

        # Decision split ring (text-based)
        lines.append(_tc(TN.FG_DIM, "  Decision Split"))
        total_bar = 24
        h_w = max(1, int(heur_pct / 100 * total_bar)) if d.heuristic_count else 0
        o_w = max(1, int(olla_pct / 100 * total_bar)) if d.ollama_count else 0
        a_w = total_bar - h_w - o_w
        ring = (_tc(TN.YELLOW, "\u2588" * h_w)
                + _tc(TN.ORANGE, "\u2588" * o_w)
                + _tc(TN.MAGENTA, "\u2588" * max(0, a_w)))
        lines.append(f"  {ring}")

        # Task type summary
        if d.task_types:
            lines.append("")
            lines.append(_tc(TN.FG_DIM, "  Task Distribution (7d)"))
            tt_total = sum(t["calls"] for t in d.task_types) or 1
            for t in sorted(d.task_types, key=lambda x: x["calls"], reverse=True)[:5]:
                pct = t["calls"] / tt_total * 100
                _tt_icons = {
                    "query": "\u2753", "code": "\U0001f4bb", "research": "\U0001f50d",
                    "analyze": "\U0001f4ca", "generate": "\u270f\ufe0f",
                }
                icon = _tt_icons.get((t["type"] or "").lower(), "\u2022")
                lines.append(
                    f"  {icon} {(t['type'] or '?')[:12]:<12} "
                    f"{_tc(TN.FG, f'{pct:>4.0f}%')} ({t['calls']})"
                )

        self.query_one("#routing-content").update("\n".join(lines))


class SavingsPanel(Static):
    """Panel D — Financial Summary & Wallet."""

    DEFAULT_CSS = """
    SavingsPanel {
        height: auto;
        min-height: 10;
        border: round #9ece6a;
        padding: 0 1;
        background: #16161e;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(id="savings-content")

    def refresh_data(self, d: DashboardData) -> None:
        lines: list[str] = []
        lines.append(_tc_bold(TN.GREEN, " SAVINGS WALLET"))
        lines.append(_dim("─" * 40))

        def _trend(val: float) -> str:
            if val > 0:
                return _tc(TN.GREEN, "\u25b2")
            elif val < 0:
                return _tc(TN.RED, "\u25bc")
            return _tc(TN.FG_DIM, "\u25cf")

        rows = [
            ("Today", d.today_saved, d.today_saved_calls),
            ("Week", d.week_saved, d.week_saved_calls),
            ("Month", d.month_saved, d.month_saved_calls),
            ("Lifetime", d.lifetime_saved, d.lifetime_saved_calls),
        ]
        lines.append(
            f"  {'Period':<10} {'Saved':>10} {'Calls':>7} {'Trend':>6}"
        )
        lines.append(_dim("  " + "─" * 36))
        for label, saved, calls in rows:
            saved_str = f"${saved:.4f}" if saved < 10 else f"${saved:.2f}"
            color = TN.GREEN if saved > 0 else TN.FG_DIM
            lines.append(
                f"  {_tc(TN.CYAN, label):<22} "
                f"{_tc(color, saved_str):>22} {str(calls):>7}  {_trend(saved)}"
            )

        lines.append("")
        lines.append(_dim("  " + "─" * 36))

        # Annualized projections
        if d.lifetime_saved_calls > 0 and d.lifetime_tokens > 0:
            # Estimate annual from lifetime data (rough approximation)
            days_active = max(1, len(d.daily_labels))
            annual_tokens = int(d.lifetime_tokens / days_active * 365)
            annual_cost = d.lifetime_cost / days_active * 365
            annual_saved = d.lifetime_saved / days_active * 365

            def _fmt_tokens(t: int) -> str:
                if t >= 1_000_000:
                    return f"{t / 1_000_000:.1f}M"
                if t >= 1_000:
                    return f"{t / 1_000:.0f}K"
                return str(t)

            lines.append(_tc(TN.FG_DIM, "  Annual Projections (est.)"))
            lines.append(
                f"  Tokens/yr:    {_tc(TN.CYAN, _fmt_tokens(annual_tokens))}"
            )
            lines.append(
                f"  Cost/yr:      {_tc(TN.YELLOW, f'${annual_cost:.2f}')}"
            )
            lines.append(
                f"  Savings/yr:   {_tc_bold(TN.GREEN, f'${annual_saved:.2f}')}"
            )

        self.query_one("#savings-content").update("\n".join(lines))


class ActivityPanel(Static):
    """Panel E — L14 Activity Graph with Braille rendering."""

    DEFAULT_CSS = """
    ActivityPanel {
        height: auto;
        min-height: 14;
        border: round #e0af68;
        padding: 0 1;
        background: #16161e;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(id="activity-content")

    def refresh_data(self, d: DashboardData) -> None:
        lines: list[str] = []
        lines.append(_tc_bold(TN.YELLOW, " L14 ACTIVITY  ") + _dim("Last 14 Days"))
        lines.append(_dim("─" * 60))

        # Summary stats
        avg_calls = d.l14_total_calls / max(len(d.daily_calls), 1)

        def _fmt_tokens(t: int | float) -> str:
            t = int(t)
            if t >= 1_000_000:
                return f"{t / 1_000_000:.1f}M"
            if t >= 1_000:
                return f"{t / 1_000:.0f}K"
            return str(t)

        lines.append(
            f"  {_tc(TN.CYAN, 'Calls')}: {d.l14_total_calls:<6}  "
            f"{_tc(TN.TEAL, 'Tokens')}: {_fmt_tokens(d.l14_total_tokens):<8}  "
            f"{_tc(TN.GREEN, 'Savings')}: ${sum(d.daily_cost):.4f}  "
            f"{_tc(TN.FG_DIM, 'Avg')}: {avg_calls:.0f}/day"
        )
        lines.append("")

        # Braille graph of calls
        if d.daily_calls:
            graph_width = 28
            graph_height = 3
            braille_lines = braille_sparkline(
                [float(c) for c in d.daily_calls],
                width=graph_width,
                height=graph_height,
            )

            # Y-axis labels
            mx = max(d.daily_calls) if d.daily_calls else 1
            y_labels = [str(mx), str(mx // 2), "0"]
            while len(y_labels) < graph_height:
                y_labels.insert(-1, "")

            vbar = _dim("\u2502")
            lines.append(_tc(TN.FG_DIM, "  Calls/Day"))
            for i, bl in enumerate(braille_lines):
                yl = y_labels[i] if i < len(y_labels) else ""
                yl_fmt = _tc(TN.FG_DIM, yl.rjust(5))
                bl_fmt = _tc(TN.CYAN, bl)
                lines.append(f"  {yl_fmt} {vbar}{bl_fmt}{vbar}")

            # X-axis
            if d.daily_labels:
                first = d.daily_labels[0] if d.daily_labels else ""
                last = d.daily_labels[-1] if d.daily_labels else ""
                axis_width = graph_width
                padding = axis_width - len(first) - len(last)
                x_axis = first + " " * max(1, padding) + last
                corner_l = _dim("\u2514")
                hbar = _dim("\u2500" * graph_width)
                corner_r = _dim("\u2518")
                lines.append(f"        {corner_l}{hbar}{corner_r}")
                lines.append(f"        {_tc(TN.FG_DIM, x_axis)}")

            lines.append("")

            # Token sparkline
            lines.append(
                f"  {_tc(TN.FG_DIM, 'Tokens')} "
                f"{_tc(TN.TEAL, mini_sparkline([float(t) for t in d.daily_tokens], width=28))}"
                f"  {_tc(TN.FG_DIM, 'peak')}: {_fmt_tokens(max(d.daily_tokens) if d.daily_tokens else 0)}"
            )

            # Cost sparkline
            lines.append(
                f"  {_tc(TN.FG_DIM, 'Cost  ')} "
                f"{_tc(TN.YELLOW, mini_sparkline(d.daily_cost, width=28))}"
                f"  {_tc(TN.FG_DIM, 'peak')}: ${max(d.daily_cost) if d.daily_cost else 0:.4f}"
            )

            # Per-day table
            lines.append("")
            lines.append(_tc(TN.FG_DIM, "  Date      Calls  Tokens     Cost"))
            lines.append(_dim("  " + "─" * 36))
            # Show last 7 days
            start = max(0, len(d.daily_labels) - 7)
            for i in range(start, len(d.daily_labels)):
                lbl = d.daily_labels[i]
                c = d.daily_calls[i]
                t = d.daily_tokens[i]
                cost = d.daily_cost[i]
                lines.append(
                    f"  {_tc(TN.FG_DIM, lbl)}  {str(c):>5}  "
                    f"{_fmt_tokens(t):>8}  ${cost:.4f}"
                )
        else:
            lines.append(_dim("  No activity data in the last 14 days."))
            lines.append(_dim("  Route tasks via llm_query / llm_code to start tracking."))

        self.query_one("#activity-content").update("\n".join(lines))


class HeaderBanner(Static):
    """Custom header with branding."""

    DEFAULT_CSS = """
    HeaderBanner {
        height: 3;
        background: #16161e;
        color: #c0caf5;
        text-align: center;
        padding: 0;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(id="header-text")

    def refresh_data(self, d: DashboardData) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        bolt = "\u26a1"
        dot = "\u25cf"
        title = _tc_bold(TN.CYAN, f"{bolt} LLM Router") + _dim(" | ") + _tc(TN.FG, "Mission Control")
        sep = _dim("|")
        ver = _tc(TN.FG_DIM, "v8.5.3")
        live = _tc(TN.GREEN, dot)
        ts = _tc(TN.FG_DIM, now)
        calls_s = _tc(TN.CYAN, str(d.total_calls))
        status = f"{ver}  {sep}  {live} {ts}  {sep}  {calls_s} calls tracked"
        self.query_one("#header-text").update(f"{title}    {status}")


# ── Main App ──────────────────────────────────────────────────────────────────

class MissionControlApp(App):
    """LLM Router Mission Control — Terminal Dashboard."""

    TITLE = "LLM Router Mission Control"
    SUB_TITLE = "Real-time routing intelligence"

    CSS = """
    Screen {
        background: #1a1b26;
    }

    #top-row {
        height: auto;
        min-height: 14;
    }

    #bottom-row {
        height: auto;
        min-height: 12;
    }

    #activity-row {
        height: auto;
        min-height: 16;
    }

    .col-left {
        width: 1fr;
        min-width: 38;
    }

    .col-center {
        width: 1fr;
        min-width: 44;
    }

    .col-right {
        width: 1fr;
        min-width: 36;
    }

    .col-wide {
        width: 1fr;
    }

    .col-half {
        width: 1fr;
    }

    Footer {
        background: #16161e;
        color: #565f89;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("d", "toggle_dark", "Theme"),
    ]

    _refresh_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        yield HeaderBanner()

        with Horizontal(id="top-row"):
            yield SubscriptionPanel(classes="col-left")
            yield MetricsPanel(classes="col-center")
            yield RoutingPanel(classes="col-right")

        with Horizontal(id="bottom-row"):
            yield SavingsPanel(classes="col-half")

        with Horizontal(id="activity-row"):
            yield ActivityPanel(classes="col-wide")

        yield Footer()

    def on_mount(self) -> None:
        self._do_refresh()
        self._refresh_timer = self.set_interval(10, self._do_refresh)

    def action_refresh(self) -> None:
        self._do_refresh()

    def _do_refresh(self) -> None:
        data = _fetch_data()
        self.query_one(HeaderBanner).refresh_data(data)
        self.query_one(SubscriptionPanel).refresh_data(data)
        self.query_one(MetricsPanel).refresh_data(data)
        self.query_one(RoutingPanel).refresh_data(data)
        self.query_one(SavingsPanel).refresh_data(data)
        self.query_one(ActivityPanel).refresh_data(data)


def run() -> None:
    """Entry point for the TUI dashboard."""
    app = MissionControlApp()
    app.run()


if __name__ == "__main__":
    run()
