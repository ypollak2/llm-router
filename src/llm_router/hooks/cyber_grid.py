"""Cyber-Grid: static, high-density session summary using Rich.

Renders a single cohesive frame to stdout — no alternate screen, no interactivity.
Designed for Claude Code session-end hooks where output lands in terminal scrollback.
"""

from __future__ import annotations

import math
from io import StringIO
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.style import Style
from rich.table import Table
from rich.text import Text

# ── Adaptive palette ─────────────────────────────────────────────────────────
# Uses Rich named colors (from terminal's 256-color palette) so text is
# readable on BOTH light and dark terminal backgrounds.
# Key rule: never use fixed RGB for labels/data — it won't adapt to bg color.

_NEON_GREEN = Style(color="green")
_NEON_GREEN_BOLD = Style(color="green", bold=True)
_INFO_BLUE = Style(color="cyan")
_INFO_BLUE_BOLD = Style(color="cyan", bold=True)
_ACCENT = Style(color="blue")
_ACCENT_DIM = Style(color="blue", dim=True)
_WARN_YELLOW = Style(color="yellow", bold=True)
_WARN_ORANGE = Style(color="yellow", bold=True)
_DANGER_RED = Style(color="red", bold=True)
_LABEL = Style()                          # default fg — always readable
_MUTED = Style(dim=True)                  # dimmed default fg
_DIM_GRAY = Style(dim=True)               # structural elements
_WHITE = Style(bold=True)                 # bold fg — always visible
_WHITE_BOLD = Style(bold=True)
_BRIGHT = Style(bold=True)
_HEADER_BG = Style(bold=True)
_MAGENTA = Style(color="magenta", bold=True)
_CYAN_BRIGHT = Style(color="cyan", bold=True)

GRID_WIDTH = 70


# ── Braille chart ────────────────────────────────────────────────────────────

_BRAILLE_BASE = 0x2800
_BRAILLE_DOTS = [
    [0x01, 0x08],  # row 0 (top)
    [0x02, 0x10],  # row 1
    [0x04, 0x20],  # row 2
    [0x40, 0x80],  # row 3 (bottom)
]


def _braille_chart(values: list[float], width: int = 30, height: int = 4) -> list[str]:
    """Render values as a Braille dot chart. Returns list of text lines."""
    if not values:
        return ["  (no data)"]

    max_val = max(values) if max(values) > 0 else 1.0
    n = len(values)

    # Each braille char is 2 columns wide, 4 rows tall
    chars_wide = min(width, math.ceil(n / 2))
    rows = height  # each braille char = 4 dot rows, but we use 1 char row = 4 dot rows

    # Normalize values to 0..(rows * 4 - 1)
    total_dot_rows = rows * 4
    scaled = [int(v / max_val * (total_dot_rows - 1)) if max_val > 0 else 0 for v in values]

    # Build character grid
    grid: list[list[int]] = [[_BRAILLE_BASE] * chars_wide for _ in range(rows)]

    for i, sv in enumerate(scaled):
        col = i // 2
        sub_col = i % 2
        if col >= chars_wide:
            break

        # sv=0 is bottom, sv=total_dot_rows-1 is top
        for dot_row in range(total_dot_rows):
            if dot_row <= sv:
                # Map dot_row to char_row and sub_row (inverted: bottom = high dot_row index)
                char_row = rows - 1 - (dot_row // 4)
                sub_row = 3 - (dot_row % 4)
                if 0 <= char_row < rows:
                    grid[char_row][col] |= _BRAILLE_DOTS[sub_row][sub_col]

    lines = []
    for row in grid:
        lines.append("".join(chr(c) for c in row))
    return lines


def _sparkline_blocks(values: list[float]) -> str:
    """Compact block sparkline for inline use."""
    if not values:
        return ""
    chars = " ▁▂▃▄▅▆▇█"
    mx = max(values) if max(values) > 0 else 1
    return "".join(chars[min(len(chars) - 1, round(v / mx * (len(chars) - 1)))] for v in values)


# ── Progress bars ────────────────────────────────────────────────────────────

def _format_reset_time(iso_ts: str) -> str:
    """Convert ISO reset timestamp to 'resets in Xh Ym (HH:MM)' with local time."""
    if not iso_ts:
        return ""
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        local_time = dt.astimezone().strftime("%H:%M")
        diff = dt - now
        total_min = int(diff.total_seconds() / 60)
        if total_min <= 0:
            return f"reset pending ({local_time})"
        hours, mins = divmod(total_min, 60)
        if hours > 0:
            return f"resets in {hours}h {mins}m ({local_time})"
        return f"resets in {mins}m ({local_time})"
    except Exception:
        return ""


def _quota_bar(pct: float, width: int = 14) -> Text:
    """Color-coded quota progress bar."""
    filled = max(0, min(width, round(pct / 100 * width)))
    empty = width - filled

    if pct < 30:
        fill_style = _NEON_GREEN
    elif pct < 60:
        fill_style = _WARN_YELLOW
    elif pct < 80:
        fill_style = _WARN_ORANGE
    else:
        fill_style = _DANGER_RED

    bar = Text()
    bar.append("━" * filled, style=fill_style)
    bar.append("─" * empty, style=_DIM_GRAY)
    return bar


def _pct_delta(start: float | None, end: float) -> Text:
    """Format percentage with delta."""
    t = Text()
    t.append(f"{end:>3.0f}%", style=_WHITE)
    if start is not None:
        delta = end - start
        if abs(delta) < 0.01:
            t.append("  no change", style=_MUTED)
        else:
            sign = "+" if delta >= 0 else ""
            fmt = f"{sign}{delta:.1f}pp" if abs(delta) >= 0.1 else f"{sign}{delta:.2f}pp"
            style = _WARN_ORANGE if abs(delta) > 5 else _MUTED
            t.append(f"  {fmt}", style=style)
    return t


# ── Section builders ─────────────────────────────────────────────────────────

def _build_header(data: dict) -> Table:
    """Top bar: session meta + context window gauge."""
    tbl = Table(show_header=False, show_edge=False, box=None, padding=(0, 1),
                expand=True)
    tbl.add_column(ratio=1)
    tbl.add_column(justify="right", ratio=1)

    left = Text()
    left.append("⚡ ", style=_NEON_GREEN)
    left.append("LLM ROUTER", style=_BRIGHT)

    sid = data.get("session_id", "")
    if sid:
        left.append(f"  {sid}", style=_LABEL)

    dur = data.get("duration_secs", 0)
    if dur > 0:
        mins = int(dur // 60)
        hrs = mins // 60
        mins = mins % 60
        if hrs > 0:
            left.append(f"  {hrs}h {mins}m", style=_ACCENT)
        else:
            left.append(f"  {mins}m", style=_ACCENT)

    right = Text()
    cc = data.get("cc_current")
    if cc:
        weekly_pct = cc.get("weekly_pct", 0)
        right.append("quota ", style=_LABEL)
        right.append_text(_quota_bar(weekly_pct, width=10))
        right.append(f" {weekly_pct:.0f}%", style=_WHITE)

    tbl.add_row(left, right)
    return tbl


def _build_intelligence(data: dict) -> Table:
    """Left column: routing breakdown + Claude subscription."""
    tbl = Table(show_header=False, show_edge=False, box=None, padding=(0, 0),
                expand=True)
    tbl.add_column()

    # Routing logic breakdown
    routing = data.get("routing_logic", [])
    if routing:
        total_hits = sum(d["hits"] for d in routing)
        zero_cost = sum(
            d["hits"] for d in routing
            if d["method"] not in ("ollama", "llm")
        )
        zero_pct = round(zero_cost / total_hits * 100) if total_hits > 0 else 0

        header = Text()
        header.append("ROUTING", style=_INFO_BLUE_BOLD)
        header.append(f"  {total_hits} decisions", style=_LABEL)
        tbl.add_row(header)
        tbl.add_row(Text())

        _METHOD_SYMBOLS = {
            "heuristic": "⚡", "heuristic-weak": "⚡",
            "build-fast-path": "🔨", "content-generation-fast-path": "📝",
            "ollama": "🧠", "llm": "🧠",
            "context-inherit": "🔗", "code-context-inherit": "🔗",
            "override": "📌", "fallback": "🔄",
        }

        for d in routing:
            pct = (d["hits"] / total_hits) * 100 if total_hits > 0 else 0
            sym = _METHOD_SYMBOLS.get(d["method"], "❓")
            row = Text()
            row.append(f" {sym} ", style=_WHITE)
            row.append(f"{d['method']:<16}", style=_LABEL)
            row.append(f"{d['hits']:>3}", style=_WHITE)
            row.append(f"  {pct:>3.0f}%", style=_DIM_GRAY)
            tbl.add_row(row)

        # Zero-cost indicator
        tbl.add_row(Text())
        zc_line = Text()
        zc_line.append(" Zero-cost: ", style=_WHITE_BOLD)
        bar_w = 10
        filled = max(0, min(bar_w, round(zero_pct / 100 * bar_w)))
        zc_style = _NEON_GREEN if zero_pct >= 80 else (_WARN_YELLOW if zero_pct >= 50 else _WARN_ORANGE)
        zc_line.append("█" * filled, style=zc_style)
        zc_line.append("░" * (bar_w - filled), style=_DIM_GRAY)
        zc_line.append(f" {zero_pct}%", style=zc_style)
        tbl.add_row(zc_line)

    # Claude subscription
    cc = data.get("cc_current")
    if cc:
        tbl.add_row(Text())
        sub_header = Text()
        sub_header.append(" Claude Subscription", style=_CYAN_BRIGHT)
        is_live = data.get("cc_is_live", False)
        sub_header.append("  live" if is_live else "  cached",
                          style=_NEON_GREEN if is_live else _MUTED)
        tbl.add_row(sub_header)

        start = data.get("cc_start")
        for label, key in [("5h", "session_pct"), ("wk", "weekly_pct"), ("sn", "sonnet_pct")]:
            end_val = cc.get(key, 0.0)
            if end_val == 0 and key == "sonnet_pct":
                s_start = start.get(key) if start else None
                if s_start is None or s_start == 0:
                    continue
            start_val = start.get(key) if start else None
            row = Text()
            row.append(f" {label:>3} ", style=_LABEL)
            row.append_text(_quota_bar(end_val, width=12))
            row.append(" ", style=_WHITE)
            row.append_text(_pct_delta(start_val, end_val))
            # Show reset time for 5h window
            if key == "session_pct":
                resets_at = cc.get("session_resets_at", "")
                if resets_at:
                    reset_str = _format_reset_time(resets_at)
                    if reset_str:
                        row.append(f"  {reset_str}", style=_DIM_GRAY)
            tbl.add_row(row)

    return tbl


def _build_financial(data: dict) -> Table:
    """Right column: savings wallet + baseline comparison."""
    tbl = Table(show_header=False, show_edge=False, box=None, padding=(0, 0),
                expand=True)
    tbl.add_column()

    header = Text()
    header.append("SAVINGS", style=_NEON_GREEN_BOLD)
    tbl.add_row(header)
    tbl.add_row(Text())

    cumulative = data.get("cumulative", [])
    period_map = {label: (calls, ti, to, saved) for label, calls, ti, to, saved in cumulative}
    all_time = period_map.get("all time", (0, 0, 0, 0.0))
    today_d = period_map.get("today", (0, 0, 0, 0.0))

    # Hero number
    lifetime = all_time[3]
    hero = Text()
    hero.append(f" ${lifetime:.2f}" if lifetime >= 1.0 else f" ${lifetime:.4f}",
                style=_NEON_GREEN_BOLD)
    hero.append("  lifetime", style=_LABEL)
    tbl.add_row(hero)

    today_line = Text()
    today_saved = today_d[3]
    today_str = f"${today_saved:.4f}" if today_saved < 1.0 else f"${today_saved:.2f}"
    today_line.append(f" {today_str}", style=_WHITE)
    today_line.append("  today", style=_LABEL)
    tbl.add_row(today_line)
    tbl.add_row(Text())

    # Period grid with token counts
    for label, calls, ti, to, saved in cumulative:
        short = {"today": "today", "this week": "week", "this month": "month",
                 "all time": "all"}.get(label, label)
        s = f"${saved:.2f}" if saved >= 1.0 else f"${saved:.4f}"
        tok_total = ti + to
        tok_str = _fmt_tok(tok_total) if tok_total > 0 else ""
        row = Text()
        row.append(f" {short:<6}", style=_LABEL)
        row.append(f" {s:>8}", style=_WHITE)
        row.append(f" {calls:>5}", style=_DIM_GRAY)
        if tok_str:
            row.append(f" {tok_str:>6}", style=_MUTED)
        tbl.add_row(row)

    return tbl


def _model_style(model_name: str) -> Style:
    """Return Rich style for a model based on its provider."""
    low = model_name.lower()
    if any(p in low for p in ("gpt-", "o1", "o3", "o4", "chatgpt", "codex")):
        return _WARN_ORANGE   # OpenAI
    if "claude" in low:
        return _MAGENTA       # Anthropic
    if any(p in low for p in ("gemini", "gemma")):
        return _WARN_YELLOW   # Google
    if any(p in low for p in ("ollama", ":", "llama", "qwen", "phi", "mistral")):
        return _NEON_GREEN    # Local
    if "deepseek" in low:
        return _INFO_BLUE     # DeepSeek
    return _WHITE


def _build_l14_panel(data: dict) -> Panel | None:
    """Bottom panel: 14-day activity block chart with axes + models used."""
    daily = data.get("daily_14d", [])
    if not daily:
        return None

    call_values = [float(d[1]) for d in daily]
    total_calls = sum(d[1] for d in daily)
    total_tokens = sum(d[2] for d in daily)
    avg_calls = total_calls // max(len(daily), 1)
    max_calls = max(call_values) if call_values else 0

    content = Text()

    # Title
    content.append("calls/day\n", style=_DIM_GRAY)

    # Block chart with Y-axis (8 rows)
    chart_height = 8
    chars = " ▁▂▃▄▅▆▇█"

    grid = [[" " for _ in range(len(call_values))] for _ in range(chart_height)]
    for day_idx, cv in enumerate(call_values):
        if max_calls > 0:
            normalized = (cv / max_calls) * chart_height
            full_rows = int(normalized)
            frac = normalized - full_rows
            for row_idx in range(chart_height):
                bottom_up = chart_height - 1 - row_idx
                if bottom_up < full_rows:
                    grid[row_idx][day_idx] = "█"
                elif bottom_up == full_rows and frac > 0:
                    ci = min(len(chars) - 1, int(frac * (len(chars) - 1)))
                    grid[row_idx][day_idx] = chars[ci]

    for row_idx in range(chart_height):
        label_val = int((chart_height - 1 - row_idx) / (chart_height - 1) * max_calls) if chart_height > 1 else int(max_calls)
        content.append(f"{label_val:>4} ┤ ", style=_DIM_GRAY)
        content.append("".join(grid[row_idx]), style=_INFO_BLUE)
        content.append("\n")

    # X-axis
    content.append(f"     └─{'─' * len(call_values)}\n", style=_DIM_GRAY)
    x_labels = "      "
    for i in range(0, len(call_values), 2):
        day_num = i + 1
        if day_num <= 14:
            x_labels += f"D{day_num:<3}"
    content.append(x_labels + "\n", style=_DIM_GRAY)

    # Stats line (no savings)
    content.append(f"\n {total_calls} calls", style=_WHITE)
    content.append(f" · {_fmt_tok(total_tokens)} tok", style=_LABEL)
    content.append(f" · avg {avg_calls}/day", style=_LABEL)

    # Quality metrics
    quality_parts: list[tuple[str, Style]] = []

    eff = data.get("efficiency", {})
    if eff:
        fb = eff["total"] - eff["on_target"]
        if fb == 0:
            quality_parts.append((f"0 fallbacks ({eff['total']})", _NEON_GREEN))
        else:
            quality_parts.append((f"{fb}/{eff['total']} fallbacks", _WARN_ORANGE))

    overhead = data.get("overhead", {})
    if overhead and overhead.get("count", 0) > 0:
        ms = overhead["avg_ms"]
        ms_style = _NEON_GREEN if ms < 50 else (_WARN_YELLOW if ms < 200 else _WARN_ORANGE)
        quality_parts.append((f"{ms:.0f}ms avg routing", ms_style))

    cache = data.get("cache_stats", {})
    if cache:
        hr = cache["hit_rate_pct"]
        hr_style = _NEON_GREEN if hr >= 50 else _MUTED
        quality_parts.append((f"{hr:.0f}% cache hit", hr_style))

    if quality_parts:
        content.append("\n ")
        for i, (text, style) in enumerate(quality_parts):
            if i > 0:
                content.append(" · ", style=_DIM_GRAY)
            content.append(text, style=style)

    return Panel(content, title="14-DAY ACTIVITY", title_align="left",
                 border_style=_ACCENT, padding=(0, 1))


def _build_models_panel(data: dict) -> Panel | None:
    """Panel showing which model was used for the last routed prompt."""
    try:
        from llm_router.hooks.dashboard_enhanced import query_last_prompt_model
        import os
        db_path = data.get("db_path") or os.path.expanduser("~/.llm-router/usage.db")
        model = query_last_prompt_model(db_path=db_path)
    except Exception:
        return None

    if not model:
        return None

    content = Text()
    style = _model_style(model)
    content.append(f"  {model}", style=style)

    return Panel(content, title="LAST ROUTED MODEL", title_align="left",
                 border_style=_ACCENT, padding=(0, 1))


def _build_insight(data: dict) -> Panel | None:
    """Wildcard insight: suggest a cheaper model for the most expensive task type."""
    tools = data.get("tools", {})
    if not tools:
        return None

    # Find highest-cost task type
    most_expensive = max(tools.items(), key=lambda x: x[1]["cost"], default=None)
    if not most_expensive or most_expensive[1]["cost"] == 0:
        return None

    task, info = most_expensive
    top_model = max(info["models"], key=info["models"].get) if info["models"] else "?"
    avg_cost = info["cost"] / info["count"] if info["count"] > 0 else 0

    # Only show if there's meaningful savings to suggest
    if avg_cost < 0.0005:
        return None

    tip = Text()
    tip.append("💡 ", style=_WARN_YELLOW)
    tip.append(f"{info['count']}× ", style=_WHITE)
    tip.append(f"'{task}'", style=_INFO_BLUE)
    tip.append(f" used {top_model}", style=_LABEL)
    tip.append(f" (${avg_cost:.4f}/call)", style=_MUTED)
    tip.append(" — consider local model for savings", style=_LABEL)

    return Panel(tip, border_style=_DIM_GRAY, padding=(0, 1))


def _fmt_tok(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


# ── Main render ──────────────────────────────────────────────────────────────

def render_cyber_grid(data: dict[str, Any]) -> str:
    """Render the full Cyber-Grid summary. Returns ANSI string."""
    buf = StringIO()
    console = Console(
        file=buf,
        force_terminal=True,
        color_system="256",
        width=GRID_WIDTH,
    )

    # Clean-break rule
    from datetime import datetime
    ts = datetime.now().strftime("%H:%M:%S")
    console.print()
    console.rule(f"Session Concluded · {ts}", style=_DIM_GRAY)
    console.print()

    # Header
    console.print(_build_header(data))

    # Two-column: Intelligence (left) + Savings (right)
    cumulative = data.get("cumulative", [])
    has_savings = any(saved > 0 for _, _, _, _, saved in cumulative) if cumulative else False

    if has_savings:
        grid = Table(show_header=False, box=None, padding=(0, 1), expand=True)
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)
        grid.add_row(_build_intelligence(data), _build_financial(data))
        console.print(Panel(grid, border_style=_ACCENT, padding=(1, 1)))
    else:
        console.print(Panel(
            _build_intelligence(data),
            border_style=_ACCENT,
            padding=(1, 1),
        ))

    # 14-Day Activity panel (enhanced block chart)
    l14 = _build_l14_panel(data)
    if l14:
        console.print(l14)

    # Models routed this session
    models_panel = _build_models_panel(data)
    if models_panel:
        console.print(models_panel)

    # Wildcard insight
    insight = _build_insight(data)
    if insight:
        console.print(insight)

    return buf.getvalue()
