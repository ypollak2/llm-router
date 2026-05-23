#!/usr/bin/env python3
"""Enhanced dashboard renderer with model tracking and improved sparklines."""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Match session-end.py color palette exactly (works on both light/dark terminals)
_BOLD = "\033[1m"
_RESET = "\033[0m"
_C_CYAN = "\033[36m"
_C_GREEN = "\033[32m"
_C_YELLOW = "\033[33m"
_C_ORANGE = "\033[33;1m"
_C_RED = "\033[31m"
_C_WHITE = "\033[1m"        # Bold inherits fg — always visible
_C_MUTED = "\033[90m"
_C_LABEL = ""               # Default fg
_C_MAGENTA = "\033[35m"
_C_DARK = "\033[90m"

STATE_DIR = Path.home() / ".llm-router"
DB_PATH = STATE_DIR / "usage.db"


def _fmt_tok(count: int) -> str:
    """Format token count with units."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(count)


_TEST_MODEL_PATTERNS = {"mock-model", "test-model", "fake-model", "mock", "test"}


def _is_test_model(model: str) -> bool:
    """Return True if model name looks like test/mock data."""
    if not model:
        return True
    low = model.lower().strip()
    # Check exact match and also any segment after "/" (e.g. "test/mock-model")
    parts = low.replace("/", " ").replace(":", " ").split()
    return low in _TEST_MODEL_PATTERNS or any(p in _TEST_MODEL_PATTERNS for p in parts)


def query_session_models(session_start: float | None, db_path: str | Path | None = None) -> dict[str, int]:
    """Query models used during the current session from DB.

    Args:
        session_start: Session start timestamp (unix epoch)
        db_path: Override DB path (for testing). Defaults to ~/.llm-router/usage.db.

    Returns: {model_name: call_count, ...}
    """
    resolved = Path(db_path) if db_path else DB_PATH
    if not resolved.exists() or session_start is None:
        return {}
    try:
        conn = sqlite3.connect(str(resolved))
        rows = conn.execute(
            "SELECT model, COUNT(*) as cnt "
            "FROM usage "
            "WHERE success=1 AND timestamp >= datetime(?, 'unixepoch') "
            "GROUP BY model ORDER BY cnt DESC",
            (session_start,),
        ).fetchall()
        conn.close()
        return {
            model: count
            for model, count in rows
            if model and model != "?" and not _is_test_model(model)
        }
    except Exception:
        return {}


def query_last_prompt_model(db_path: str | Path | None = None) -> str | None:
    """Return the model used in the most recent successful routing call.

    Returns: model name string, or None if no recent call found.
    """
    resolved = Path(db_path) if db_path else DB_PATH
    if not resolved.exists():
        return None
    try:
        conn = sqlite3.connect(str(resolved))
        row = conn.execute(
            "SELECT model FROM usage "
            "WHERE success=1 AND model IS NOT NULL AND model != '?' "
            "ORDER BY timestamp DESC LIMIT 1",
        ).fetchone()
        conn.close()
        if row and row[0] and not _is_test_model(row[0]):
            return row[0]
        return None
    except Exception:
        return None


def query_last_prompt_calls(db_path: str | Path | None = None,
                            window_sec: int = 30) -> list[dict]:
    """Return all routing calls from the most recent prompt.

    Groups calls within `window_sec` seconds of the latest call,
    which captures all routing activity from a single user prompt.

    Returns: list of {model, provider, task_type, cost, in_tokens, out_tokens}
    """
    resolved = Path(db_path) if db_path else DB_PATH
    if not resolved.exists():
        return []
    try:
        conn = sqlite3.connect(str(resolved))
        # Find the timestamp of the most recent call
        latest = conn.execute(
            "SELECT timestamp FROM usage "
            "WHERE success=1 AND model IS NOT NULL AND model != '?' "
            "ORDER BY timestamp DESC LIMIT 1",
        ).fetchone()
        if not latest:
            conn.close()
            return []
        # Get all calls within window_sec of the latest
        rows = conn.execute(
            "SELECT model, provider, task_type, cost_usd, input_tokens, output_tokens "
            "FROM usage "
            "WHERE success=1 AND model IS NOT NULL AND model != '?' "
            "AND timestamp >= datetime(?, '-' || ? || ' seconds') "
            "ORDER BY timestamp DESC",
            (latest[0], window_sec),
        ).fetchall()
        conn.close()
        return [
            {"model": r[0], "provider": r[1], "task_type": r[2],
             "cost": r[3], "in_tokens": r[4], "out_tokens": r[5]}
            for r in rows
            if r[0] and not _is_test_model(r[0])
        ]
    except Exception:
        return []


def render_enhanced_sparkline(
    daily_data: list[tuple[str, int, int, float]],  # (date, calls, tokens, saved)
    max_height: int = 8,
) -> str:
    """Render sparkline chart with Y-axis labels, X-axis day markers.

    Args:
        daily_data: From _query_daily_14d — (date_label, calls, tokens, saved)
        max_height: Number of rows in the chart (default 8)

    Returns: Multi-line string with sparkline visualization
    """
    if not daily_data:
        return ""

    calls = [d[1] for d in daily_data]
    if not calls or max(calls) == 0:
        return ""

    max_calls = max(calls)
    chars = " ▁▂▃▄▅▆▇█"

    lines: list[str] = []
    lines.append(f"  {_BOLD}14-Day Routing Activity{_RESET}")
    lines.append(f"  {_C_MUTED}calls/day{_RESET}")
    lines.append("")

    # Build 2D grid
    grid = [[" " for _ in range(len(calls))] for _ in range(max_height)]

    for day_idx, call_count in enumerate(calls):
        if max_calls > 0:
            # Scale so max value fills all rows
            normalized = (call_count / max_calls) * max_height
            full_rows = int(normalized)
            frac = normalized - full_rows
            for row_idx in range(max_height):
                bottom_up = max_height - 1 - row_idx
                if bottom_up < full_rows:
                    grid[row_idx][day_idx] = "█"
                elif bottom_up == full_rows and frac > 0:
                    char_idx = min(len(chars) - 1, int(frac * (len(chars) - 1)))
                    grid[row_idx][day_idx] = chars[char_idx]

    # Y-axis labels (top = max, bottom = 0)
    for row_idx in range(max_height):
        label_val = int((max_height - 1 - row_idx) / (max_height - 1) * max_calls) if max_height > 1 else max_calls
        row_str = "".join(grid[row_idx])
        lines.append(f"  {label_val:>4} ┤ {_C_CYAN}{row_str}{_RESET}")

    # X-axis
    lines.append(f"       └─{'─' * len(calls)}")

    # Day labels
    day_labels = "        "
    for i in range(0, len(calls), 2):
        day_num = i + 1
        if day_num <= 14:
            day_labels += f"D{day_num:<3}"
    lines.append(day_labels)

    # Stats line
    total_calls = sum(calls)
    total_tokens = sum(d[2] for d in daily_data)
    avg_calls = total_calls // len(calls) if calls else 0

    lines.append("")
    lines.append(
        f"    {_C_WHITE}{total_calls:,}{_RESET} calls · "
        f"{_C_WHITE}{_fmt_tok(total_tokens)}{_RESET} tok · "
        f"avg {_C_WHITE}{avg_calls}/day{_RESET}"
    )

    return "\n".join(lines)


def _model_color(model_name: str) -> str:
    """Return ANSI color for a model based on its provider."""
    low = model_name.lower()
    if any(p in low for p in ("gpt-", "o1", "o3", "o4", "chatgpt")):
        return _C_ORANGE   # OpenAI
    if "claude" in low:
        return _C_MAGENTA  # Anthropic
    if any(p in low for p in ("gemini", "gemma")):
        return _C_YELLOW   # Google
    if any(p in low for p in ("ollama", ":", "llama", "qwen", "phi", "mistral")):
        return _C_GREEN    # Local / Ollama
    if "deepseek" in low:
        return _C_CYAN     # DeepSeek
    return _C_WHITE


def render_models_section(models_data: dict[str, int]) -> str:
    """Render section showing which models were used during routing.

    models_data: {model_name: count_of_calls, ...}
    Returns: Multi-line string with model breakdown
    """
    if not models_data:
        return ""

    lines: list[str] = [f"  {_BOLD}Models Routed This Session{_RESET}", ""]

    sorted_models = sorted(models_data.items(), key=lambda x: x[1], reverse=True)
    total = sum(count for _, count in sorted_models)

    for model_name, count in sorted_models:
        pct = (count / total * 100) if total > 0 else 0
        color = _model_color(model_name)

        # Progress bar
        bar_width = 20
        filled = int(pct / 100 * bar_width)
        bar = _C_GREEN + "━" * filled + _C_DARK + "─" * (bar_width - filled) + _RESET

        lines.append(
            f"    {color}{model_name:<22}{_RESET} "
            f"{bar} {pct:>3.0f}% ({count})"
        )

    return "\n".join(lines)


def render_models_section_from_db(session_start: float | None) -> str:
    """Query DB and render models section — convenience wrapper."""
    models = query_session_models(session_start)
    if not models:
        return ""
    return render_models_section(models)
