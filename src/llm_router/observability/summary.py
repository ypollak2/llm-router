"""Session Summary Dashboard — terminal-rendered overview of routing activity.

Pulls from ~/.llm-router/lineage.db and ~/.llm-router/sessions.db and renders
a panel-based dashboard with:

    1. HEADLINE   — total cost + savings vs always-premium baseline
    2. SPARKLINE  — spend pattern across the session
    3. TIERS      — bar chart of local/cheap/mid/premium distribution
    4. PROVIDERS  — per-provider call counts + cost
    5. INVERSIONS — up/down inversion alerts with examples
    6. AGENTS     — per-session rollup if any agents ran
    7. SAFETY     — PII catches forced to local routing
    8. ROUTES     — top (task_type, model) pairs
    9. PUNCHLINE  — one-line summary the user can copy-paste

Designed to render in under 100ms with no network. Lazy-imports rich so
basic `llm_router --help` stays snappy.
"""
from __future__ import annotations

import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from llm_router.lineage import Inversion, LineageStore, Tier
from llm_router.agents import SessionStore


# Premium baseline pricing — for the "vs always-premium" savings number.
# Using GPT-4o pricing (mid-tier) so the savings number is realistic, not
# inflated by comparing to Opus. Update when prices change.
_BASELINE_PER_1K_INPUT = 0.0025
_BASELINE_PER_1K_OUTPUT = 0.010


_TIER_COLOR = {
    Tier.LOCAL.value: "green",
    Tier.CHEAP.value: "cyan",
    Tier.MID.value: "yellow",
    Tier.PREMIUM.value: "magenta",
    Tier.UNKNOWN.value: "white",
}

# LLM Router brand identity — used across both the terminal dashboard and the
# markdown export so the surface feels like one product, not two skins.
_LLM_ROUTER_WORDMARK = "⚡ C H U Z O M ⚡"
_LLM_ROUTER_TAGLINE = "routing intelligence · cost savings · safety telemetry"
_LLM_ROUTER_PANEL_PREFIX = "◆ LLM Router · "
_LLM_ROUTER_LOGO_ASCII = r"""
   ___ _  _ _   _ _____ ___  __  __
  / __| || | | | |_  / / _ \|  \/  |
 | (__| __ | |_| |/ /_| (_) | |\/| |
  \___|_||_|\___//___|\___/|_|  |_|
"""


@dataclass
class SessionSummaryData:
    """All aggregated stats the dashboard needs. Pure data — no rendering."""

    total_decisions: int = 0
    total_cost_usd: float = 0.0
    total_latency_ms: int = 0
    baseline_cost_usd: float = 0.0
    savings_usd: float = 0.0
    savings_pct: float = 0.0
    # #28 (Gate 7): how many rows fell back to the latency ESTIMATE for their
    # baseline tokens (no actual counts recorded). 0 ⇒ the baseline is fully
    # measured; >0 ⇒ partially estimated — callers should label it honestly.
    baseline_estimated_rows: int = 0
    tier_counts: dict[str, int] = field(default_factory=dict)
    tier_costs: dict[str, float] = field(default_factory=dict)
    provider_counts: dict[str, int] = field(default_factory=dict)
    provider_costs: dict[str, float] = field(default_factory=dict)
    up_inversions: list[dict] = field(default_factory=list)
    down_inversions: list[dict] = field(default_factory=list)
    inversion_rate: float = 0.0
    pii_catches: int = 0
    framework_counts: dict[str, int] = field(default_factory=dict)
    top_routes: list[tuple[str, str, int]] = field(default_factory=list)
    cost_sparkline: list[float] = field(default_factory=list)
    agent_sessions: list[dict] = field(default_factory=list)
    earliest_ts: float = 0.0
    latest_ts: float = 0.0
    host_counts: dict[str, int] = field(default_factory=dict)
    # v2 — latency distribution + per-tier latency
    latencies_ms: list[int] = field(default_factory=list)
    latency_p50_ms: int = 0
    latency_p95_ms: int = 0
    latency_p99_ms: int = 0
    # v2 — outcome breakdown for the status badge
    success_count: int = 0
    fail_count: int = 0

    @property
    def health(self) -> str:
        """Coarse one-glyph health: 🟢 / 🟡 / 🔴 based on inversion + failure rates."""
        fail_rate = (
            self.fail_count / self.total_decisions
            if self.total_decisions else 0.0
        )
        if self.inversion_rate < 0.05 and fail_rate < 0.02:
            return "🟢"
        if self.inversion_rate < 0.15 and fail_rate < 0.10:
            return "🟡"
        return "🔴"

    @property
    def duration_seconds(self) -> float:
        if self.earliest_ts and self.latest_ts:
            return max(0.0, self.latest_ts - self.earliest_ts)
        return 0.0


def collect(
    lineage_store: LineageStore | None = None,
    session_store: SessionStore | None = None,
    *,
    since_seconds: float | None = 86400.0,
    limit: int = 5000,
) -> SessionSummaryData:
    """Aggregate stats from lineage + sessions DBs.

    Args:
        lineage_store / session_store: defaults to ~/.llm-router/lineage.db and
            ~/.llm-router/sessions.db when None.
        since_seconds: only include rows newer than this. None = all-time.
        limit: cap on lineage rows fetched (defaults to 5000 — covers a
            multi-day session).
    """
    lineage_store = lineage_store or LineageStore()
    session_store = session_store or SessionStore()
    rows = lineage_store.recent(limit=limit)
    if since_seconds is not None:
        cutoff = time.time() - since_seconds
        rows = [r for r in rows if r["timestamp"] >= cutoff]

    data = SessionSummaryData()
    if not rows:
        return data

    data.total_decisions = len(rows)
    data.earliest_ts = min(r["timestamp"] for r in rows)
    data.latest_ts = max(r["timestamp"] for r in rows)

    # Cost + latency + tier + provider aggregation
    for row in rows:
        cost = row.get("cost_usd", 0.0) or 0.0
        latency = row.get("latency_ms", 0) or 0
        tier = row.get("model_tier", Tier.UNKNOWN.value)
        model = row.get("model_chosen", "<unknown>")
        provider = model.split("/", 1)[0] if "/" in model else model
        host = row.get("host", "<unknown>")
        outcome = row.get("outcome", "success")

        data.total_cost_usd += cost
        data.total_latency_ms += latency
        data.latencies_ms.append(latency)
        if outcome == "success":
            data.success_count += 1
        else:
            data.fail_count += 1
        data.tier_counts[tier] = data.tier_counts.get(tier, 0) + 1
        data.tier_costs[tier] = data.tier_costs.get(tier, 0.0) + cost
        data.provider_counts[provider] = data.provider_counts.get(provider, 0) + 1
        data.provider_costs[provider] = data.provider_costs.get(provider, 0.0) + cost
        data.host_counts[host] = data.host_counts.get(host, 0) + 1

        framework = row.get("framework")
        if framework:
            data.framework_counts[framework] = (
                data.framework_counts.get(framework, 0) + 1
            )

        inv = row.get("inversion", Inversion.NONE.value)
        if inv == Inversion.UP.value:
            data.up_inversions.append({
                "model_chosen": model,
                "complexity": row.get("complexity"),
                "task_type": row.get("task_type"),
                "timestamp": row.get("timestamp"),
            })
        elif inv == Inversion.DOWN.value:
            data.down_inversions.append({
                "model_chosen": model,
                "complexity": row.get("complexity"),
                "task_type": row.get("task_type"),
                "timestamp": row.get("timestamp"),
            })

        # PII catches: heuristic via notes column
        notes = (row.get("notes") or "").lower()
        if "pii" in notes or "secret" in notes:
            data.pii_catches += 1

    # Baseline cost: the counterfactual "what if a premium host did every row".
    # #28 (Gate 7): use the ACTUAL measured token counts recorded in lineage
    # (input_tokens/output_tokens) when present. Rows written without token counts
    # carry 0/0 — for those we fall back to the old latency proxy (~500 output
    # tokens ≈ 2000ms) so historical rows still contribute a baseline, and each
    # such row is counted in `baseline_estimated_rows` so callers can label the
    # figure honestly instead of presenting an estimate as a measured total.
    for row in rows:
        in_tok = int(row.get("input_tokens", 0) or 0)
        out_tok = int(row.get("output_tokens", 0) or 0)
        if in_tok > 0 or out_tok > 0:
            input_tokens, output_tokens = in_tok, out_tok
        else:
            latency = row.get("latency_ms", 0) or 0
            output_tokens = max(20, latency // 4)  # rough proxy (token-less rows)
            input_tokens = max(50, output_tokens * 2)
            data.baseline_estimated_rows += 1
        data.baseline_cost_usd += (
            (input_tokens / 1000) * _BASELINE_PER_1K_INPUT
            + (output_tokens / 1000) * _BASELINE_PER_1K_OUTPUT
        )
    # AUD-06: signed, not clamped. This clamp was present at 7c6fdaa — the commit
    # certified RELEASE QUALIFIED under Gate 7 ("surfaces reconcile, no
    # estimate-as-measured"). Gate 7 therefore passed on a surface structurally
    # incapable of reporting a loss, which is how the defect survived an audit
    # whose whole purpose was reconciling this figure.
    data.savings_usd = data.baseline_cost_usd - data.total_cost_usd
    if data.baseline_cost_usd > 0:
        data.savings_pct = data.savings_usd / data.baseline_cost_usd
    inv_total = len(data.up_inversions) + len(data.down_inversions)
    data.inversion_rate = inv_total / data.total_decisions if data.total_decisions else 0.0

    # Top routes — (task_type, tier) pairs ordered by frequency
    route_counts = Counter(
        (r.get("task_type", "?"), r.get("model_tier", "?"))
        for r in rows
    )
    data.top_routes = [
        (tt, tier, count)
        for (tt, tier), count in route_counts.most_common(8)
    ]

    # Latency percentiles
    if data.latencies_ms:
        sorted_lat = sorted(data.latencies_ms)
        n = len(sorted_lat)
        data.latency_p50_ms = sorted_lat[n // 2]
        data.latency_p95_ms = sorted_lat[int(n * 0.95)] if n > 1 else sorted_lat[-1]
        data.latency_p99_ms = sorted_lat[int(n * 0.99)] if n > 1 else sorted_lat[-1]

    # Cost sparkline — bucket spend into 24 time buckets across session
    if data.duration_seconds > 0:
        buckets = 24
        bucket_size = max(1.0, data.duration_seconds / buckets)
        spark: dict[int, float] = defaultdict(float)
        for row in rows:
            idx = int(
                (row["timestamp"] - data.earliest_ts) / bucket_size
            )
            idx = min(idx, buckets - 1)
            spark[idx] += row.get("cost_usd", 0.0) or 0.0
        data.cost_sparkline = [spark.get(i, 0.0) for i in range(buckets)]

    # Agent sessions
    try:
        # Get unique session_ids from lineage that have rollups
        session_ids = {
            r.get("session_id") for r in rows if r.get("session_id")
        }
        for sid in session_ids:
            try:
                rollup = session_store.rollup(sid)
                data.agent_sessions.append(rollup)
            except Exception as exc:
                # Skip sessions whose store entry is gone. Expected and benign
                # individually; a SPIKE means the store is being pruned out from
                # under the dashboard, which shows up as silently shrinking
                # history rather than as an error.
                from llm_router import failopen
                failopen.record("CHZ-FO-SUMMARY-SESSION-ROLLUP", exc, detail=str(sid))
                continue
    except Exception as exc:
        # The whole agent-session block failed, not one session. The dashboard
        # then renders with NO agent sessions, which is indistinguishable from
        # "you ran none" -- the RED2-02 shape on a different surface.
        from llm_router import failopen
        failopen.record("CHZ-FO-SUMMARY-AGENT-SESSIONS", exc)

    return data


# ────────────────────────────────────────────────────────────────────────
# Rendering
# ────────────────────────────────────────────────────────────────────────

_SPARK_CHARS = " ▁▂▃▄▅▆▇█"


def _render_sparkline(values: list[float], width: int = 24) -> str:
    """Render a list of floats as a unicode bar sparkline."""
    if not values:
        return "—"
    max_v = max(values) or 1.0
    out = []
    for v in values[:width]:
        idx = int(round((v / max_v) * (len(_SPARK_CHARS) - 1)))
        out.append(_SPARK_CHARS[idx])
    return "".join(out)


def _fmt_cost(usd: float) -> str:
    if usd >= 1.0:
        return f"${usd:.2f}"
    if usd >= 0.01:
        return f"${usd:.4f}"
    if usd > 0:
        return f"{usd * 100:.3f}¢"
    return "$0.00"


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f} s"
    if seconds < 3600:
        return f"{seconds / 60:.1f} min"
    return f"{seconds / 3600:.1f} h"


def _histogram(values: list[int], buckets: int = 20, width: int = 40) -> list[str]:
    """Build a horizontal histogram of values. Returns list of (label, bar) strings."""
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi == lo:
        return [f"{lo:>5} ms  {'█' * width}  {len(values)}"]
    span = hi - lo
    bucket_size = max(1, span // buckets)
    counts: dict[int, int] = {}
    for v in values:
        idx = (v - lo) // bucket_size
        counts[idx] = counts.get(idx, 0) + 1
    max_count = max(counts.values()) if counts else 1
    out = []
    for i in range(min(buckets, max(counts.keys()) + 1)):
        n = counts.get(i, 0)
        bar_len = int((n / max_count) * width) if max_count else 0
        bar = "█" * bar_len
        low = lo + i * bucket_size
        out.append(f"{low:>5} ms │ {bar}{' ' * (width - bar_len)} │ {n}")
    return out


def _gradient_bar(values: list[float], width: int = 60) -> list[str]:
    """Multi-block gradient bar for cost-over-time. Returns colored block string."""
    if not values:
        return ["—"]
    max_v = max(values) or 1.0
    # Bucket into width buckets (or use existing if shorter)
    blocks = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
    out_parts = []
    for v in values[:width]:
        ratio = v / max_v
        idx = int(round(ratio * (len(blocks) - 1)))
        out_parts.append(blocks[idx])
    return ["".join(out_parts)]


def render(data: SessionSummaryData, *, console=None) -> None:
    """Render the dashboard to console (defaults to stdout via rich)."""
    from rich import box
    from rich.align import Align
    from rich.columns import Columns
    from rich.console import Console, Group
    from rich.panel import Panel
    from rich.progress_bar import ProgressBar
    from rich.table import Table
    from rich.text import Text

    if console is None:
        import sys
        console = Console(force_terminal=sys.stdout.isatty(), color_system="truecolor")

    # ── STATUS BANNER — wordmark + tagline + health glyph ─────────────
    # Three-line banner so the dashboard's identity is unmistakable: a
    # rendered wordmark on top, the product tagline in the middle, and the
    # live health glyph + duration anchoring the bottom.
    wordmark_line = Text(_LLM_ROUTER_WORDMARK, style="bold bright_blue")
    tagline_line = Text(_LLM_ROUTER_TAGLINE, style="dim italic")
    status_line = Text.assemble(
        (f"{data.health}  ", ""),
        ("Session Summary", "bold white"),
        ("  ·  ", "dim"),
        (_fmt_duration(data.duration_seconds), "dim"),
    )
    status_banner = Panel(
        Group(
            Align.center(wordmark_line),
            Align.center(tagline_line),
            Align.center(status_line),
        ),
        border_style="bright_blue",
        box=box.HEAVY,
        padding=(0, 2),
    )

    # ── HEADLINE — savings vs baseline ─────────────────────────────────
    headline_text = Text.assemble(
        ("Session savings  ", "bold"),
        (_fmt_cost(data.savings_usd), "bold green" if data.savings_usd > 0 else "white"),
        (f"  ({data.savings_pct * 100:.0f}% vs always-premium)", "dim"),
    )
    spend_line = Text.assemble(
        ("Spent ", "dim"),
        (_fmt_cost(data.total_cost_usd), "bold"),
        ("  ·  baseline ", "dim"),
        (_fmt_cost(data.baseline_cost_usd), "dim"),
    )
    decisions_line = Text.assemble(
        (f"{data.total_decisions} routing decisions", "bold"),
        ("  ·  ", "dim"),
        (_fmt_duration(data.duration_seconds), "dim"),
        ("  ·  ", "dim"),
        (f"{data.total_latency_ms / max(1, data.total_decisions):.0f} ms avg", "dim"),
    )
    headline = Panel(
        Group(headline_text, spend_line, decisions_line),
        title=f"{_LLM_ROUTER_PANEL_PREFIX}Headline",
        border_style="bright_blue",
        padding=(1, 2),
    )

    # ── SPARKLINE — spend over time ────────────────────────────────────
    if data.cost_sparkline:
        spark_text = Text(_render_sparkline(data.cost_sparkline),
                          style="cyan")
        spark_panel = Panel(
            Align.center(spark_text, vertical="middle"),
            title=f"{_LLM_ROUTER_PANEL_PREFIX}Spend over time  "
                  f"({len(data.cost_sparkline)} buckets)",
            border_style="cyan",
            padding=(0, 2),
        )
    else:
        spark_panel = None

    # ── TIER DISTRIBUTION ─────────────────────────────────────────────
    tier_table = Table(box=box.MINIMAL, show_header=True, show_edge=False,
                       header_style="bold", expand=True)
    tier_table.add_column("Tier", style="bold")
    tier_table.add_column("Calls", justify="right")
    tier_table.add_column("Cost", justify="right")
    tier_table.add_column("Share", justify="left", ratio=2)
    max_calls = max(data.tier_counts.values()) if data.tier_counts else 1
    tier_order = [Tier.LOCAL.value, Tier.CHEAP.value, Tier.MID.value,
                  Tier.PREMIUM.value, Tier.UNKNOWN.value]
    for tier in tier_order:
        n = data.tier_counts.get(tier, 0)
        if n == 0:
            continue
        cost = data.tier_costs.get(tier, 0.0)
        color = _TIER_COLOR.get(tier, "white")
        bar = ProgressBar(total=max_calls, completed=n,
                          complete_style=color, finished_style=color)
        tier_table.add_row(
            Text(tier, style=color),
            str(n), _fmt_cost(cost), bar,
        )
    tier_panel = Panel(tier_table,
                       title=f"{_LLM_ROUTER_PANEL_PREFIX}Tier distribution",
                       border_style="green", padding=(0, 1))

    # ── PROVIDERS ─────────────────────────────────────────────────────
    provider_table = Table(box=box.MINIMAL, show_header=True,
                           show_edge=False, header_style="bold")
    provider_table.add_column("Provider")
    provider_table.add_column("Calls", justify="right")
    provider_table.add_column("Cost", justify="right")
    for provider, count in sorted(
        data.provider_counts.items(), key=lambda kv: -kv[1]
    )[:8]:
        provider_table.add_row(
            provider,
            str(count),
            _fmt_cost(data.provider_costs.get(provider, 0.0)),
        )
    provider_panel = Panel(provider_table,
                           title=f"{_LLM_ROUTER_PANEL_PREFIX}Providers",
                           border_style="cyan", padding=(0, 1))

    # ── INVERSIONS ────────────────────────────────────────────────────
    inv_lines = []
    if data.up_inversions:
        inv_lines.append(Text.assemble(
            (f"↑ {len(data.up_inversions)} UP-inversion(s)", "bold red"),
            ("  — complex prompts routed to cheap/local", "dim"),
        ))
        for inv in data.up_inversions[:3]:
            inv_lines.append(Text.assemble(
                ("  · ", "dim red"),
                (str(inv["task_type"]), "yellow"),
                (" / ", "dim"),
                (str(inv["complexity"]), "bold"),
                (" → ", "dim"),
                (str(inv["model_chosen"]), "red"),
            ))
    if data.down_inversions:
        inv_lines.append(Text.assemble(
            (f"↓ {len(data.down_inversions)} DOWN-inversion(s)", "bold yellow"),
            ("  — simple prompts forced to premium", "dim"),
        ))
        for inv in data.down_inversions[:3]:
            inv_lines.append(Text.assemble(
                ("  · ", "dim yellow"),
                (str(inv["task_type"]), "yellow"),
                (" / ", "dim"),
                (str(inv["complexity"]), "bold"),
                (" → ", "dim"),
                (str(inv["model_chosen"]), "yellow"),
            ))
    if not inv_lines:
        inv_lines = [Text("✓ No routing inversions detected — every prompt "
                          "went to the right tier", style="green")]
    rate_color = (
        "green" if data.inversion_rate < 0.05
        else "yellow" if data.inversion_rate < 0.15
        else "red"
    )
    inv_lines.append(Text.assemble(
        ("Inversion rate: ", "dim"),
        (f"{data.inversion_rate * 100:.1f}%", rate_color),
        (" (target < 5%)", "dim"),
    ))
    inversions_panel = Panel(
        Group(*inv_lines),
        title=f"{_LLM_ROUTER_PANEL_PREFIX}Routing health (inversions)",
        border_style=rate_color, padding=(0, 1),
    )

    # ── SAFETY (PII) ──────────────────────────────────────────────────
    safety_color = "green" if data.pii_catches == 0 else "bright_green"
    safety_msg = (
        f"✓ {data.pii_catches} PII / secret leak(s) caught — forced local routing"
        if data.pii_catches > 0
        else "✓ No PII / secret signals fired (no leaks observed)"
    )
    safety_panel = Panel(
        Text(safety_msg, style=safety_color),
        title=f"{_LLM_ROUTER_PANEL_PREFIX}Safety",
        border_style=safety_color, padding=(0, 1),
    )

    # ── AGENTS ────────────────────────────────────────────────────────
    agent_panel = None
    if data.agent_sessions:
        agent_table = Table(box=box.MINIMAL, show_header=True,
                            show_edge=False, header_style="bold")
        agent_table.add_column("Agent")
        agent_table.add_column("Session", style="dim")
        agent_table.add_column("Steps", justify="right")
        agent_table.add_column("Cost", justify="right")
        agent_table.add_column("State")
        for sess in data.agent_sessions[:8]:
            agent_table.add_row(
                sess.get("agent_id", "?"),
                sess.get("session_id", "?")[:8] + "…",
                str(sess.get("total_steps", 0)),
                _fmt_cost(sess.get("total_cost_usd", 0.0)),
                sess.get("state", "?"),
            )
        agent_panel = Panel(agent_table,
                            title=f"{_LLM_ROUTER_PANEL_PREFIX}Agent sessions",
                            border_style="magenta", padding=(0, 1))

    # ── TOP ROUTES ────────────────────────────────────────────────────
    routes_table = Table(box=box.MINIMAL, show_header=True,
                         show_edge=False, header_style="bold")
    routes_table.add_column("Task")
    routes_table.add_column("Tier")
    routes_table.add_column("Calls", justify="right")
    for task, tier, count in data.top_routes[:6]:
        color = _TIER_COLOR.get(tier, "white")
        routes_table.add_row(task, Text(tier, style=color), str(count))
    routes_panel = Panel(routes_table,
                         title=f"{_LLM_ROUTER_PANEL_PREFIX}Top routes",
                         border_style="bright_black", padding=(0, 1))

    # ── PUNCHLINE ─────────────────────────────────────────────────────
    punch_parts = [
        f"LLM Router classified {data.total_decisions} prompts",
    ]
    if data.tier_counts.get(Tier.LOCAL.value, 0) > 0:
        n = data.tier_counts[Tier.LOCAL.value]
        punch_parts.append(f"routed {n} to local (free)")
    if data.tier_counts.get(Tier.CHEAP.value, 0) > 0:
        n = data.tier_counts[Tier.CHEAP.value]
        c = data.tier_costs.get(Tier.CHEAP.value, 0.0)
        punch_parts.append(f"{n} to cheap ({_fmt_cost(c)})")
    if data.pii_catches > 0:
        punch_parts.append(
            f"caught {data.pii_catches} PII leak(s) → forced local"
        )
    if data.savings_usd > 0:
        punch_parts.append(
            f"saved {_fmt_cost(data.savings_usd)} "
            f"({data.savings_pct * 100:.0f}%) vs always-premium"
        )
    punchline = "  ·  ".join(punch_parts) + "."
    punchline_panel = Panel(
        Text(punchline, style="bold bright_white"),
        border_style="bright_blue",
        title=f"{_LLM_ROUTER_PANEL_PREFIX}One-line",
        padding=(0, 2),
    )

    # ── LATENCY DISTRIBUTION ──────────────────────────────────────────
    latency_panel = None
    if data.latencies_ms:
        hist_lines = _histogram(data.latencies_ms, buckets=10, width=30)
        hist_text = Text("\n".join(hist_lines), style="cyan")
        lat_summary = Text.assemble(
            ("p50: ", "dim"),
            (f"{data.latency_p50_ms} ms", "bold green"),
            ("    p95: ", "dim"),
            (f"{data.latency_p95_ms} ms",
             "bold yellow" if data.latency_p95_ms > 5000 else "bold"),
            ("    p99: ", "dim"),
            (f"{data.latency_p99_ms} ms",
             "bold red" if data.latency_p99_ms > 10000 else "bold"),
        )
        latency_panel = Panel(
            Group(lat_summary, Text(""), hist_text),
            title=f"{_LLM_ROUTER_PANEL_PREFIX}Latency distribution",
            border_style="cyan",
            padding=(0, 1),
        )

    # ── SIGNATURE — sign-off line for brand consistency ───────────────
    # Keeps the dashboard feeling like one product, not a stack of tables.
    # Dim style so it never competes with the data above it.
    signature = Align.center(
        Text.assemble(
            (_LLM_ROUTER_WORDMARK, "dim bright_blue"),
            ("   ·   ", "dim"),
            ("`llm_router summary --markdown` to share  ·  "
             "`llm_router summary --watch` for live mode",
             "dim italic"),
        )
    )

    # ── ASSEMBLE ──────────────────────────────────────────────────────
    console.print()
    console.print(status_banner)
    console.print(headline)
    if spark_panel:
        console.print(spark_panel)
    console.print(Columns([tier_panel, provider_panel], equal=False,
                          expand=True))
    if latency_panel:
        console.print(latency_panel)
    console.print(Columns([inversions_panel, safety_panel], expand=True))
    if agent_panel:
        console.print(agent_panel)
    console.print(routes_panel)
    console.print(punchline_panel)
    console.print(signature)
    console.print()


def render_markdown(data: SessionSummaryData) -> str:
    """Alternative renderer for `llm_router summary --markdown` / sharing.

    Leads with the LLM Router ASCII wordmark in a code block so the export feels
    like the terminal dashboard rather than a generic table dump.
    """
    out = [
        "```",
        _LLM_ROUTER_LOGO_ASCII.strip("\n"),
        "```",
        "",
        "# LLM Router · Session Summary",
        "",
        f"> _{_LLM_ROUTER_TAGLINE}_  ·  health {data.health}  ·  "
        f"{_fmt_duration(data.duration_seconds)}",
        "",
    ]

    out.append("## Headline\n")
    out.append(
        f"- **Session cost:** {_fmt_cost(data.total_cost_usd)}  "
        f"_(baseline {_fmt_cost(data.baseline_cost_usd)})_"
    )
    out.append(
        f"- **Savings vs always-premium:** "
        f"**{_fmt_cost(data.savings_usd)} ({data.savings_pct * 100:.0f}%)**"
    )
    out.append(f"- **Routing decisions:** {data.total_decisions}")
    out.append(f"- **Session duration:** {_fmt_duration(data.duration_seconds)}")
    out.append(
        f"- **Avg latency:** "
        f"{data.total_latency_ms / max(1, data.total_decisions):.0f} ms"
    )
    out.append("")

    out.append("## Spend pattern\n")
    if data.cost_sparkline:
        out.append(f"```\n{_render_sparkline(data.cost_sparkline)}\n```")
    out.append("")

    out.append("## Tier distribution\n")
    out.append("| Tier | Calls | Cost |")
    out.append("|---|---:|---:|")
    for tier in [Tier.LOCAL.value, Tier.CHEAP.value, Tier.MID.value,
                 Tier.PREMIUM.value, Tier.UNKNOWN.value]:
        n = data.tier_counts.get(tier, 0)
        if n == 0:
            continue
        c = data.tier_costs.get(tier, 0.0)
        out.append(f"| `{tier}` | {n} | {_fmt_cost(c)} |")
    out.append("")

    out.append("## Providers\n")
    out.append("| Provider | Calls | Cost |")
    out.append("|---|---:|---:|")
    for p, n in sorted(data.provider_counts.items(), key=lambda kv: -kv[1])[:8]:
        out.append(f"| `{p}` | {n} | {_fmt_cost(data.provider_costs[p])} |")
    out.append("")

    out.append("## Routing health\n")
    out.append(
        f"- Inversion rate: **{data.inversion_rate * 100:.1f}%**  "
        f"(target < 5%)"
    )
    out.append(f"- UP-inversions: {len(data.up_inversions)} (complex → cheap)")
    out.append(f"- DOWN-inversions: {len(data.down_inversions)} (simple → premium)")
    out.append("")

    out.append("## Safety\n")
    if data.pii_catches > 0:
        out.append(
            f"- ✓ **{data.pii_catches}** secret/PII leak(s) caught — "
            f"forced local routing"
        )
    else:
        out.append("- ✓ No PII signals fired this session")
    out.append("")

    if data.agent_sessions:
        out.append("## Agent sessions\n")
        out.append("| Agent | Session | Steps | Cost | State |")
        out.append("|---|---|---:|---:|---|")
        for s in data.agent_sessions[:8]:
            out.append(
                f"| `{s.get('agent_id', '?')}` "
                f"| `{s.get('session_id', '?')[:8]}…` "
                f"| {s.get('total_steps', 0)} "
                f"| {_fmt_cost(s.get('total_cost_usd', 0.0))} "
                f"| `{s.get('state', '?')}` |"
            )
        out.append("")

    if data.top_routes:
        out.append("## Top routes\n")
        out.append("| Task | Tier | Calls |")
        out.append("|---|---|---:|")
        for task, tier, count in data.top_routes[:6]:
            out.append(f"| `{task}` | `{tier}` | {count} |")
        out.append("")

    out.append("---")
    out.append(
        f"_{_LLM_ROUTER_WORDMARK}  ·  generated by `llm_router summary` — "
        f"run with `--watch` for live mode, `--since-hours N` for a wider window._"
    )

    return "\n".join(out)


# ────────────────────────────────────────────────────────────────────────
# CLI entrypoint
# ────────────────────────────────────────────────────────────────────────

def cli_summary(
    *,
    since_hours: float = 24.0,
    limit: int = 5000,
    markdown: bool = False,
    watch: bool = False,
    watch_interval: float = 5.0,
) -> int:
    """Implementation behind `llm_router summary`. Returns exit code.

    --watch enables live mode: re-collects + re-renders every interval
    seconds using rich.live.Live so the dashboard updates in place.
    Ideal for keeping it open in a side terminal during a session.
    """
    if markdown:
        data = collect(since_seconds=since_hours * 3600, limit=limit)
        print(render_markdown(data))
        return 0

    if watch:
        from rich.console import Console
        from rich.live import Live

        console = Console()
        try:
            with Live(console=console, screen=True, auto_refresh=False) as live:
                while True:
                    import time
                    data = collect(
                        since_seconds=since_hours * 3600, limit=limit
                    )
                    # Render into a buffer console, capture, then update Live
                    from io import StringIO

                    from rich.console import Console as BufConsole
                    buf = StringIO()
                    sub = BufConsole(file=buf, width=console.width,
                                     force_terminal=True, color_system="auto")
                    if data.total_decisions == 0:
                        sub.print(
                            f"\n[dim]No routing decisions recorded in the last "
                            f"{since_hours:.0f}h. Waiting…  "
                            f"(refresh every {watch_interval:.0f}s, "
                            f"Ctrl+C to exit)[/]\n"
                        )
                    else:
                        render(data, console=sub)
                    live.update(buf.getvalue(), refresh=True)
                    time.sleep(watch_interval)
        except KeyboardInterrupt:
            return 0
        return 0

    data = collect(since_seconds=since_hours * 3600, limit=limit)
    if data.total_decisions == 0:
        print(
            f"{_LLM_ROUTER_WORDMARK}  ·  no routing decisions in the last "
            f"{since_hours:.0f}h.\nRoute a few prompts, then re-run "
            "`llm_router summary`."
        )
        return 0
    render(data)
    return 0
