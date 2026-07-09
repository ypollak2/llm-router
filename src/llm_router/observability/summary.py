"""Session-end summary — content model + renderers over llm-router's usage log.

Ported from Chuzom's ``summary.py``. The content model (:class:`SessionSummaryData`)
and the rendering are kept; the DATA SOURCE is rewired from Chuzom's lineage/agents
stores to llm-router's own ``usage.db`` (the ``usage`` table), so there is no
``chuzom`` dependency.

    collect()          -> SessionSummaryData   (reads ~/.llm-router/usage.db)
    render_markdown()  -> str                  (portable; CI / Claude Desktop / logs)
    render()           -> None                 (rich terminal panels; rich optional)

``rich`` is an OPTIONAL dependency: :func:`render` falls back to printing the
markdown when rich is not installed. Everything is fail-soft — a missing or
malformed db yields an empty summary, never an exception.

Not yet wired from llm-router data (left empty, a follow-up): routing inversions,
PII-forced-local catches, and agent-session rollups — these have no direct column
in the current ``usage`` schema.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _state_dir() -> Path:
    return Path(os.environ.get("LLM_ROUTER_STATE_DIR", str(Path.home() / ".llm-router")))


# Provider → coarse cost tier, for the tier distribution bar. Best-effort; an
# unknown provider lands in "unknown" rather than skewing the picture.
_PROVIDER_TIER = {
    "ollama": "local",
    "llamacpp": "local",
    "lmstudio": "local",
    "gemini": "cheap",
    "google": "cheap",
    "deepseek": "cheap",
    "groq": "cheap",
    "together": "cheap",
    "mistral": "cheap",
    "openrouter": "cheap",
    "openai": "mid",
    "cohere": "mid",
    "xai": "mid",
    "anthropic": "premium",
}
_TIER_ORDER = ["local", "cheap", "mid", "premium", "unknown"]


def _tier_for(provider: Optional[str], model: Optional[str]) -> str:
    p = (provider or "").lower()
    if p in _PROVIDER_TIER:
        tier = _PROVIDER_TIER[p]
        # Premium flagships bump a mid provider up a notch.
        m = (model or "").lower()
        if tier == "mid" and any(k in m for k in ("o3", "gpt-5", "opus")):
            return "premium"
        return tier
    return "unknown"


@dataclass
class SessionSummaryData:
    """All aggregated stats the dashboard needs. Pure data — no rendering."""

    total_decisions: int = 0
    total_cost_usd: float = 0.0
    total_latency_ms: int = 0
    baseline_cost_usd: float = 0.0
    savings_usd: float = 0.0
    savings_pct: float = 0.0
    tier_counts: dict = field(default_factory=dict)
    tier_costs: dict = field(default_factory=dict)
    provider_counts: dict = field(default_factory=dict)
    provider_costs: dict = field(default_factory=dict)
    top_routes: list = field(default_factory=list)      # (task_type, model, count)
    cost_sparkline: list = field(default_factory=list)
    earliest_ts: float = 0.0
    latest_ts: float = 0.0
    latencies_ms: list = field(default_factory=list)
    latency_p50_ms: int = 0
    latency_p95_ms: int = 0
    latency_p99_ms: int = 0
    success_count: int = 0
    fail_count: int = 0

    @property
    def health(self) -> str:
        """Coarse one-glyph health from the failure rate."""
        fail_rate = (self.fail_count / self.total_decisions) if self.total_decisions else 0.0
        if fail_rate < 0.02:
            return "🟢"
        if fail_rate < 0.10:
            return "🟡"
        return "🔴"

    @property
    def duration_seconds(self) -> float:
        if self.earliest_ts and self.latest_ts:
            return max(0.0, self.latest_ts - self.earliest_ts)
        return 0.0


def _percentile(values: list, pct: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((pct / 100.0) * (len(s) - 1)))))
    return int(s[k])


def _epoch(ts) -> float:
    """usage.timestamp may be an epoch float or an ISO string; coerce to epoch."""
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            from datetime import datetime
            return datetime.fromisoformat(ts).timestamp()
        except (ValueError, TypeError):
            return 0.0
    return 0.0


def collect(db_path: Optional[str] = None, since_seconds: Optional[float] = None,
            limit: int = 5000) -> SessionSummaryData:
    """Aggregate a :class:`SessionSummaryData` from llm-router's ``usage`` table.

    Fail-soft: a missing db / table returns an empty summary. ``since_seconds``
    keeps only rows newer than ``now - since_seconds`` (None = all-time).
    """
    data = SessionSummaryData()
    db = Path(db_path) if db_path else (_state_dir() / "usage.db")
    if not db.is_file():
        return data
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return data
    try:
        rows = conn.execute(
            "SELECT timestamp, model, provider, task_type, cost_usd, latency_ms, "
            "       success, potential_cost_usd, saved_usd "
            "FROM usage ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.Error:
        return data
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    if not rows:
        return data

    import time as _time
    cutoff = (_time.time() - since_seconds) if since_seconds else None
    route_counter: dict = {}
    for ts, model, provider, task_type, cost, latency, success, potential, saved in rows:
        e = _epoch(ts)
        if cutoff is not None and e < cutoff:
            continue
        cost = float(cost or 0.0)
        latency = int(latency or 0)
        potential = float(potential or 0.0)
        saved = float(saved or 0.0)

        data.total_decisions += 1
        data.total_cost_usd += cost
        data.total_latency_ms += latency
        data.baseline_cost_usd += potential if potential > 0 else cost
        data.savings_usd += saved
        if e:
            data.earliest_ts = min(data.earliest_ts or e, e)
            data.latest_ts = max(data.latest_ts, e)
        if latency:
            data.latencies_ms.append(latency)
        if success in (1, True, "1", "true", "True"):
            data.success_count += 1
        elif success in (0, False, "0", "false", "False"):
            data.fail_count += 1

        tier = _tier_for(provider, model)
        data.tier_counts[tier] = data.tier_counts.get(tier, 0) + 1
        data.tier_costs[tier] = data.tier_costs.get(tier, 0.0) + cost
        prov = (provider or "unknown")
        data.provider_counts[prov] = data.provider_counts.get(prov, 0) + 1
        data.provider_costs[prov] = data.provider_costs.get(prov, 0.0) + cost
        key = (task_type or "?", model or "?")
        route_counter[key] = route_counter.get(key, 0) + 1

    if data.baseline_cost_usd > 0:
        data.savings_pct = round(
            100.0 * max(0.0, data.baseline_cost_usd - data.total_cost_usd)
            / data.baseline_cost_usd, 1
        )
    data.latency_p50_ms = _percentile(data.latencies_ms, 50)
    data.latency_p95_ms = _percentile(data.latencies_ms, 95)
    data.latency_p99_ms = _percentile(data.latencies_ms, 99)
    data.top_routes = [
        (tt, m, n)
        for (tt, m), n in sorted(route_counter.items(), key=lambda kv: kv[1], reverse=True)[:8]
    ]
    return data


# ── Rendering ────────────────────────────────────────────────────────────────
def _fmt_cost(usd: float) -> str:
    if usd <= 0:
        return "$0.00"
    if usd < 0.01:
        return f"${usd:.4f}"
    return f"${usd:.2f}"


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def _bar(count: int, total: int, width: int = 24) -> str:
    if total <= 0:
        return ""
    filled = int(round(width * count / total))
    return "█" * filled + "░" * (width - filled)


def render_markdown(data: SessionSummaryData) -> str:
    """A portable Markdown session summary (no rich, no color, no network)."""
    if data.total_decisions == 0:
        return "### ⚡ llm-router — session summary\n\n_No routing activity recorded yet._\n"

    lines = ["### ⚡ llm-router — session summary", ""]
    lines.append(
        f"{data.health} **{data.total_decisions}** routes · "
        f"spent **{_fmt_cost(data.total_cost_usd)}** · "
        f"saved **{_fmt_cost(data.savings_usd)}** "
        f"(**{data.savings_pct:.0f}%** vs baseline) · "
        f"{_fmt_duration(data.duration_seconds)}"
    )
    lines.append("")

    # Tier distribution
    lines.append("**Tier mix**")
    lines.append("")
    lines.append("| Tier | Routes | Cost | |")
    lines.append("|---|--:|--:|:--|")
    for tier in _TIER_ORDER:
        n = data.tier_counts.get(tier, 0)
        if not n:
            continue
        lines.append(
            f"| {tier} | {n} | {_fmt_cost(data.tier_costs.get(tier, 0.0))} | "
            f"`{_bar(n, data.total_decisions)}` |"
        )
    lines.append("")

    # Providers
    if data.provider_counts:
        provs = sorted(data.provider_counts.items(), key=lambda kv: kv[1], reverse=True)
        lines.append("**Providers**: " + " · ".join(
            f"{p} ({n}, {_fmt_cost(data.provider_costs.get(p, 0.0))})" for p, n in provs
        ))
        lines.append("")

    # Latency
    if data.latencies_ms:
        lines.append(
            f"**Latency**: p50 {data.latency_p50_ms}ms · "
            f"p95 {data.latency_p95_ms}ms · p99 {data.latency_p99_ms}ms"
        )
        lines.append("")

    # Outcomes
    if data.success_count or data.fail_count:
        lines.append(f"**Outcomes**: {data.success_count} ok · {data.fail_count} failed")
        lines.append("")

    # Top routes
    if data.top_routes:
        lines.append("**Top routes**")
        lines.append("")
        for tt, model, n in data.top_routes:
            lines.append(f"- `{tt}` → `{model}` × {n}")
        lines.append("")

    # Punchline
    lines.append(
        f"> Saved {_fmt_cost(data.savings_usd)} ({data.savings_pct:.0f}%) "
        f"across {data.total_decisions} routes this session."
    )
    return "\n".join(lines) + "\n"


def render(data: SessionSummaryData, *, console=None) -> None:
    """Render to the terminal. Uses ``rich`` when available; otherwise prints the
    portable Markdown. ``rich`` is an optional dependency."""
    try:
        from rich.console import Console
        from rich.panel import Panel
    except ImportError:
        print(render_markdown(data))
        return

    con = console or Console()
    if data.total_decisions == 0:
        con.print(Panel("No routing activity recorded yet.", title="⚡ llm-router"))
        return
    body = (
        f"{data.health}  [bold]{data.total_decisions}[/] routes   "
        f"spent [bold]{_fmt_cost(data.total_cost_usd)}[/]   "
        f"saved [bold green]{_fmt_cost(data.savings_usd)}[/] "
        f"([green]{data.savings_pct:.0f}%[/])   "
        f"{_fmt_duration(data.duration_seconds)}\n"
    )
    for tier in _TIER_ORDER:
        n = data.tier_counts.get(tier, 0)
        if n:
            body += f"\n  {tier:<8} {_bar(n, data.total_decisions)} {n}"
    if data.top_routes:
        body += "\n\n[dim]top:[/] " + ", ".join(
            f"{tt}→{m} ×{n}" for tt, m, n in data.top_routes[:4]
        )
    con.print(Panel(body, title="⚡ llm-router — session summary", expand=False))


def cli_summary(argv: Optional[list] = None) -> int:
    """`python -m llm_router.observability.summary [--markdown]` — print the summary."""
    import sys
    argv = argv if argv is not None else sys.argv[1:]
    data = collect()
    if "--markdown" in argv or not sys.stdout.isatty():
        sys.stdout.write(render_markdown(data))
    else:
        render(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_summary())
