"""LLM Router web dashboard — read-only stats at localhost:7337.

A lightweight aiohttp server that reads from the same SQLite DB and
usage.json the MCP server writes. No shared mutable state with the MCP
process — pure read-only access.

Routes:
    GET /           → HTML dashboard page
    GET /api/stats  → JSON data used by dashboard charts

Start via CLI:
    llm-router dashboard
    llm-router dashboard --port 7338
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from llm_router.logging import configure_logging, get_logger

log = get_logger("llm_router.dashboard")

DEFAULT_PORT = 7337
_TOKEN_FILE = Path.home() / ".llm-router" / "dashboard.token"


def _get_or_create_token() -> str:
    """Return the persistent dashboard auth token, creating it on first call."""
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _TOKEN_FILE.exists():
        return _TOKEN_FILE.read_text().strip()
    token = secrets.token_urlsafe(32)
    _TOKEN_FILE.write_text(token)
    os.chmod(_TOKEN_FILE, 0o600)
    return token


async def _get_stats() -> dict:
    """Read routing stats from SQLite and usage.json for the dashboard."""
    from llm_router.config import get_config
    from llm_router.cost import _get_db

    config = get_config()
    stats: dict = {
        "today": {"calls": 0, "cost_usd": 0.0, "tokens": 0},
        "month": {"calls": 0, "cost_usd": 0.0},
        "models": [],
        "profiles": [],
        "task_types": [],
        "recent": [],
        "savings": {"total_saved_usd": 0.0, "total_external_usd": 0.0},
        "usage": {},
        "semantic_cache": {"hits": 0},
    }

    try:
        db = await _get_db()
        try:
            c = await db.execute(
                "SELECT COUNT(*), COALESCE(SUM(cost_usd),0), "
                "COALESCE(SUM(input_tokens+output_tokens),0) "
                "FROM usage WHERE timestamp >= datetime('now','start of day')"
            )
            row = await c.fetchone()
            if row:
                stats["today"] = {"calls": row[0], "cost_usd": round(row[1], 6), "tokens": row[2]}

            c = await db.execute(
                "SELECT COUNT(*), COALESCE(SUM(cost_usd),0) FROM usage "
                "WHERE timestamp >= datetime('now','start of month')"
            )
            row = await c.fetchone()
            if row:
                stats["month"] = {"calls": row[0], "cost_usd": round(row[1], 4)}

            c = await db.execute(
                "SELECT model, COUNT(*) as n, COALESCE(SUM(cost_usd),0) as cost "
                "FROM usage WHERE timestamp >= datetime('now','-7 days') "
                "GROUP BY model ORDER BY n DESC LIMIT 10"
            )
            stats["models"] = [
                {"model": r[0], "calls": r[1], "cost_usd": round(r[2], 6)}
                for r in await c.fetchall()
            ]

            c = await db.execute(
                "SELECT profile, COUNT(*) FROM usage "
                "WHERE timestamp >= datetime('now','-7 days') GROUP BY profile"
            )
            stats["profiles"] = [{"profile": r[0], "calls": r[1]} for r in await c.fetchall()]

            c = await db.execute(
                "SELECT task_type, COUNT(*) FROM usage "
                "WHERE timestamp >= datetime('now','-7 days') GROUP BY task_type"
            )
            stats["task_types"] = [{"task_type": r[0], "calls": r[1]} for r in await c.fetchall()]

            c = await db.execute(
                "SELECT date(timestamp) as day, COALESCE(SUM(cost_usd),0) "
                "FROM usage WHERE timestamp >= datetime('now','-14 days') "
                "GROUP BY day ORDER BY day"
            )
            stats["daily_cost"] = [
                {"day": r[0], "cost_usd": round(r[1], 6)} for r in await c.fetchall()
            ]

            c = await db.execute(
                "SELECT timestamp, task_type, NULL as complexity, model, "
                "cost_usd, latency_ms, success "
                "FROM usage ORDER BY timestamp DESC LIMIT 20"
            )
            stats["recent"] = [
                {
                    "timestamp": r[0], "task_type": r[1], "complexity": r[2],
                    "model": r[3], "cost_usd": r[4], "latency_ms": r[5], "success": r[6],
                }
                for r in await c.fetchall()
            ]

            # Savings = baseline Sonnet cost minus actual external spend
            SONNET_IN, SONNET_OUT = 3.0, 15.0  # $/M tokens
            c = await db.execute(
                "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
                "COALESCE(SUM(cost_usd),0) FROM usage "
                "WHERE success=1 AND provider!='subscription'"
            )
            row = await c.fetchone()
            if row:
                baseline = (row[0] * SONNET_IN + row[1] * SONNET_OUT) / 1_000_000
                external = row[2]
                stats["savings"] = {
                    "total_saved_usd": round(max(0.0, baseline - external), 4),
                    "total_external_usd": round(external, 4),
                }

            c = await db.execute(
                "SELECT COUNT(*) FROM usage WHERE model LIKE 'cache/%' "
                "AND timestamp >= datetime('now','-7 days')"
            )
            row = await c.fetchone()
            stats["semantic_cache"]["hits"] = row[0] if row else 0

        finally:
            await db.close()
    except Exception as exc:
        log.warning("Dashboard DB read failed: %s", exc)

    usage_path = Path.home() / ".llm-router" / "usage.json"
    try:
        stats["usage"] = json.loads(usage_path.read_text())
    except Exception:
        pass

    try:
        from importlib.metadata import version as _pkg_version
        _pkg_ver = _pkg_version("claude-code-llm-router")
    except Exception:
        _pkg_ver = "?"
    stats["config"] = {
        "profile": config.llm_router_profile.value,
        "monthly_budget": config.llm_router_monthly_budget,
        "daily_limit": config.llm_router_daily_spend_limit,
        "subscription_mode": config.llm_router_claude_subscription,
        "version": _pkg_ver,
    }

    return stats


def _html(token: str = "") -> str:
    """Return the Stitch-designed dashboard HTML with the auth token injected.

    Uses Tailwind CDN with the exact Stitch color token system (Liquid Glass).
    DB values rendered via esc() before being placed into table row markup —
    all string values are HTML-escaped before insertion, so there is no XSS
    surface even if the SQLite database is tampered with.

    Design: "The Ethereal Engine" Liquid Glass theme from Google Stitch.
    Primary: #d0bcff (violet), Secondary: #5de6ff (cyan), BG: #0b1326.
    Fonts: Inter (body/headlines), Space Grotesk (labels), JetBrains Mono (code).
    Icons: Material Symbols Outlined.
    """
    html = r"""<!DOCTYPE html>
<html class="dark" lang="en">
<head>
<meta charset="utf-8"/>
<meta content="width=device-width, initial-scale=1.0" name="viewport"/>
<title>LLM Router</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&family=Space+Grotesk:wght@300;500;700&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet"/>
<link href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:wght,FILL@100..700,0..1&display=swap" rel="stylesheet"/>
<script src="https://cdn.tailwindcss.com?plugins=forms,container-queries"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<script id="tailwind-config">
tailwind.config = {
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        "on-surface": "#dae2fd",
        "primary": "#d0bcff",
        "primary-container": "#a078ff",
        "secondary": "#5de6ff",
        "tertiary": "#ffb869",
        "error": "#ffb4ab",
        "outline": "#958ea0",
        "outline-variant": "#494454",
        "surface": "#0b1326",
        "surface-dim": "#0b1326",
        "surface-container-lowest": "#060e20",
        "surface-container-low": "#131b2e",
        "surface-container": "#171f33",
        "surface-container-high": "#222a3d",
        "surface-container-highest": "#2d3449",
        "surface-variant": "#2d3449",
        "on-surface-variant": "#cbc3d7",
        "on-primary": "#3c0091",
        "on-secondary": "#00363e",
        "on-error": "#690005",
        "inverse-surface": "#dae2fd",
        "background": "#0b1326",
        "on-background": "#dae2fd",
        "tertiary-container": "#ca801e",
        "error-container": "#93000a",
      },
      borderRadius: {
        "DEFAULT": "0.125rem", "lg": "0.25rem", "xl": "0.5rem", "full": "0.75rem",
      },
      fontFamily: {
        "headline": ["Inter"], "body": ["Inter"],
        "label": ["Space Grotesk"], "mono": ["JetBrains Mono"],
      },
    },
  },
}
</script>
<style>
.material-symbols-outlined { font-variation-settings:'FILL' 0,'wght' 400,'GRAD' 0,'opsz' 24; vertical-align:middle; }
.glass-panel { background:rgba(45,52,73,0.4); backdrop-filter:blur(20px); -webkit-backdrop-filter:blur(20px); border:1px solid rgba(149,142,160,0.15); }
.nav-active  { color:#5de6ff!important; font-weight:700; background:rgba(30,40,64,0.4); }
.nav-inactive { color:#494454; }
.nav-inactive:hover { background:rgba(30,40,64,0.3); color:#cbc3d7; }
.spinning { animation:spin 0.8s linear infinite; display:inline-block; }
@keyframes spin { to { transform:rotate(360deg); } }
.hidden { display:none; }
</style>
</head>
<body class="bg-surface font-body text-on-surface">

<!-- Top header -->
<header class="flex justify-between items-center px-6 h-16 w-full fixed z-50 bg-slate-950/40 backdrop-blur-xl shadow-[0_0_32px_rgba(208,188,255,0.06)]">
  <div class="flex items-center gap-6">
    <span class="text-xl font-bold bg-clip-text text-transparent bg-gradient-to-br from-violet-300 to-violet-600 font-headline tracking-tight">LLM Router</span>
    <span id="mode-badge" class="text-[10px] font-mono bg-primary/10 text-primary border border-primary/20 px-3 py-1 rounded-full uppercase tracking-wider">CC Subscription</span>
  </div>
  <div class="flex items-center gap-4">
    <span id="refresh-ts" class="text-xs font-mono text-outline">—</span>
    <button onclick="doRefresh()" id="refresh-btn" class="material-symbols-outlined text-outline hover:text-primary transition-colors cursor-pointer select-none">refresh</button>
  </div>
</header>

<!-- Sidebar -->
<aside class="h-screen w-64 fixed left-0 top-0 pt-20 flex flex-col px-4 bg-slate-900/60 backdrop-blur-lg z-40 border-r border-outline-variant/5">
  <div class="px-4 mb-6">
    <div class="flex items-center gap-2 mb-1">
      <div class="w-2 h-2 rounded-full bg-secondary" style="animation:pulse 2s ease-in-out infinite;box-shadow:0 0 6px #5de6ff;"></div>
      <span class="font-mono text-[10px] uppercase tracking-widest text-secondary">Router Node</span>
    </div>
    <p id="sb-profile" class="text-[10px] text-outline font-mono">balanced | v1.4.1</p>
  </div>
  <nav class="space-y-1 flex-1">
    <button onclick="showTab('overview')" data-tab="overview" class="nav-active flex items-center gap-3 w-full px-4 py-2.5 rounded-r-full transition-all text-left">
      <span class="material-symbols-outlined text-xl">dashboard</span>
      <span class="font-mono text-sm uppercase tracking-widest">Overview</span>
    </button>
    <button onclick="showTab('performance')" data-tab="performance" class="nav-inactive flex items-center gap-3 w-full px-4 py-2.5 rounded-r-full transition-all text-left">
      <span class="material-symbols-outlined text-xl">monitoring</span>
      <span class="font-mono text-sm uppercase tracking-widest">Performance</span>
    </button>
    <button onclick="showTab('config')" data-tab="config" class="nav-inactive flex items-center gap-3 w-full px-4 py-2.5 rounded-r-full transition-all text-left">
      <span class="material-symbols-outlined text-xl">tune</span>
      <span class="font-mono text-sm uppercase tracking-widest">Configuration</span>
    </button>
    <button onclick="showTab('logs')" data-tab="logs" class="nav-inactive flex items-center gap-3 w-full px-4 py-2.5 rounded-r-full transition-all text-left">
      <span class="material-symbols-outlined text-xl">terminal</span>
      <span class="font-mono text-sm uppercase tracking-widest">Logs</span>
    </button>
    <button onclick="showTab('budget')" data-tab="budget" class="nav-inactive flex items-center gap-3 w-full px-4 py-2.5 rounded-r-full transition-all text-left">
      <span class="material-symbols-outlined text-xl">account_balance_wallet</span>
      <span class="font-mono text-sm uppercase tracking-widest">Budget</span>
    </button>
  </nav>
  <div class="pb-8 px-4">
    <div id="sb-status" class="text-[10px] font-mono text-outline">Connecting...</div>
    <a href="/api/stats" target="_blank" class="text-[10px] font-mono text-outline/50 hover:text-primary transition-colors mt-1 block">raw JSON ↗</a>
  </div>
</aside>

<!-- Main content -->
<main class="pl-64 pt-16 min-h-screen">

<!-- TAB: Overview -->
<div id="tab-overview" class="p-8 max-w-[1600px] mx-auto space-y-8">
  <div class="grid grid-cols-1 lg:grid-cols-12 gap-8">

    <!-- Savings gauge -->
    <div class="lg:col-span-4 glass-panel rounded-3xl p-8 relative overflow-hidden">
      <div class="relative z-10">
        <div class="flex justify-between items-start mb-6">
          <div>
            <h2 class="text-xs font-label uppercase tracking-widest text-outline">Efficiency Gauge</h2>
            <p class="text-2xl font-bold text-white">Savings Velocity</p>
          </div>
          <span id="eff-pct" class="text-secondary font-mono text-xl font-bold">0%</span>
        </div>
        <div class="flex justify-center py-4">
          <div class="w-52 h-52 relative flex items-center justify-center">
            <svg class="absolute inset-0 w-full h-full" viewBox="0 0 208 208">
              <circle cx="104" cy="104" r="86" fill="none" stroke="rgba(45,52,73,0.6)" stroke-width="14"/>
              <circle id="gauge-arc" cx="104" cy="104" r="86" fill="none"
                      stroke="url(#gaugeGrad)" stroke-width="14" stroke-linecap="round"
                      stroke-dasharray="540" stroke-dashoffset="540"
                      transform="rotate(-90 104 104)" style="transition:stroke-dashoffset 1s ease;"/>
              <defs>
                <linearGradient id="gaugeGrad" x1="0%" y1="0%" x2="100%" y2="0%">
                  <stop offset="0%" style="stop-color:#5de6ff;"/>
                  <stop offset="100%" style="stop-color:#d0bcff;"/>
                </linearGradient>
              </defs>
            </svg>
            <div class="z-10 text-center">
              <span id="g-saved" class="text-4xl font-extrabold text-white">$0</span>
              <p class="text-[10px] font-mono text-outline uppercase mt-1">Lifetime Savings</p>
            </div>
          </div>
        </div>
        <div class="mt-6 space-y-3">
          <div class="flex justify-between text-xs">
            <span class="text-outline">External API Spend</span>
            <span id="g-ext-cost" class="font-mono text-error">$0.00</span>
          </div>
          <div class="w-full h-1 bg-surface-container-highest rounded-full overflow-hidden">
            <div id="g-ext-bar" class="h-full bg-error transition-all duration-700" style="width:0%"></div>
          </div>
          <div class="flex justify-between text-xs">
            <span class="text-outline">Today's Cost</span>
            <span id="g-today-cost" class="font-mono text-secondary">$0.00</span>
          </div>
          <div class="w-full h-1 bg-surface-container-highest rounded-full overflow-hidden">
            <div id="g-today-bar" class="h-full bg-secondary transition-all duration-700" style="width:0%;box-shadow:0 0 8px #5de6ff;"></div>
          </div>
        </div>
      </div>
      <div class="absolute -top-20 -right-20 w-48 h-48 bg-primary/10 rounded-full blur-3xl pointer-events-none"></div>
    </div>

    <!-- 3 stat cards + routing flow -->
    <div class="lg:col-span-8 grid grid-cols-1 md:grid-cols-3 gap-6">
      <div class="bg-surface-container-high rounded-3xl p-6 border border-outline-variant/10 flex flex-col justify-between">
        <div class="flex justify-between items-start">
          <div class="bg-primary/10 p-2 rounded-xl"><span class="material-symbols-outlined text-primary">route</span></div>
          <span class="text-[10px] font-mono text-primary">today</span>
        </div>
        <div class="mt-8">
          <p class="text-xs font-label text-outline uppercase tracking-wider">Calls Today</p>
          <h3 id="s-calls" class="text-4xl font-extrabold text-white mt-1">0</h3>
        </div>
      </div>
      <div class="bg-surface-container-high rounded-3xl p-6 border border-outline-variant/10 flex flex-col justify-between">
        <div class="flex justify-between items-start">
          <div class="bg-secondary/10 p-2 rounded-xl"><span class="material-symbols-outlined text-secondary">data_usage</span></div>
          <span class="text-[10px] font-mono text-secondary">today</span>
        </div>
        <div class="mt-8">
          <p class="text-xs font-label text-outline uppercase tracking-wider">Tokens</p>
          <h3 id="s-tokens" class="text-4xl font-extrabold text-white mt-1">0</h3>
        </div>
      </div>
      <div class="bg-surface-container-high rounded-3xl p-6 border border-outline-variant/10 flex flex-col justify-between">
        <div class="flex justify-between items-start">
          <div class="bg-tertiary/10 p-2 rounded-xl"><span class="material-symbols-outlined text-tertiary">bolt</span></div>
          <span class="flex items-center gap-1">
            <span class="w-1.5 h-1.5 rounded-full bg-secondary" style="animation:pulse 2s ease-in-out infinite;"></span>
            <span class="text-[10px] font-mono text-outline">7d</span>
          </span>
        </div>
        <div class="mt-8">
          <p class="text-xs font-label text-outline uppercase tracking-wider">Cache Hits</p>
          <h3 id="s-semhits" class="text-4xl font-extrabold text-white mt-1">0</h3>
        </div>
      </div>
      <!-- Routing flow spans 3 cols -->
      <div class="md:col-span-3 glass-panel rounded-3xl p-8 relative overflow-hidden">
        <div class="flex justify-between items-center mb-4">
          <div>
            <h3 class="text-base font-bold text-white">Task → Model Routing</h3>
            <p class="text-xs text-outline font-label">Live routing decisions by task type (7 days)</p>
          </div>
          <span class="px-3 py-1 bg-primary/10 rounded-lg text-[10px] font-mono text-primary uppercase border border-primary/20">Real-time</span>
        </div>
        <div id="routing-flow" class="h-44 w-full relative"></div>
      </div>
    </div>
  </div>

  <!-- Recent traffic -->
  <section class="space-y-4">
    <div class="flex items-center justify-between">
      <h3 class="text-xl font-bold text-white flex items-center gap-3">
        <span class="material-symbols-outlined text-primary">history</span>
        Recent Routed Traffic
      </h3>
      <button onclick="showTab('logs')" class="text-sm font-label text-primary hover:underline transition-all">View full stream →</button>
    </div>
    <div class="bg-surface-container-low rounded-3xl border border-outline-variant/10 overflow-hidden">
      <table class="w-full border-collapse">
        <thead class="bg-surface-container-highest/50">
          <tr>
            <th class="text-left px-6 py-4 text-[10px] font-mono text-outline uppercase tracking-wider">Timestamp</th>
            <th class="text-left px-6 py-4 text-[10px] font-mono text-outline uppercase tracking-wider">Task / Complexity</th>
            <th class="text-left px-6 py-4 text-[10px] font-mono text-outline uppercase tracking-wider">Model</th>
            <th class="text-left px-6 py-4 text-[10px] font-mono text-outline uppercase tracking-wider">Status</th>
            <th class="text-right px-6 py-4 text-[10px] font-mono text-outline uppercase tracking-wider">Cost / Latency</th>
          </tr>
        </thead>
        <tbody id="recent-tbody">
          <tr><td colspan="5" class="px-6 py-10 text-center text-outline font-mono text-xs">Loading...</td></tr>
        </tbody>
      </table>
    </div>
  </section>
</div>

<!-- TAB: Performance -->
<div id="tab-performance" class="p-8 max-w-[1600px] mx-auto space-y-8 hidden">
  <div>
    <h1 class="text-3xl font-extrabold font-headline text-white">Performance Intelligence</h1>
    <p class="text-sm text-outline font-label mt-1">Routing efficiency, model distribution, and cost trends</p>
  </div>
  <div class="grid grid-cols-1 lg:grid-cols-12 gap-8">
    <div class="lg:col-span-8 glass-panel rounded-3xl p-8">
      <div class="flex justify-between items-center mb-6">
        <div>
          <h3 class="text-lg font-bold text-white">Daily Cost Trend</h3>
          <p class="text-xs text-outline font-label">External API spend — trailing 14 days</p>
        </div>
        <span class="px-3 py-1 bg-secondary/10 text-secondary text-[10px] font-mono rounded-lg border border-secondary/20 uppercase">14 days</span>
      </div>
      <div class="h-64"><canvas id="costChart"></canvas></div>
    </div>
    <div class="lg:col-span-4 glass-panel rounded-3xl p-8 flex flex-col items-center justify-center gap-6">
      <h3 class="text-xs font-label text-outline uppercase tracking-wider self-start">Efficiency Score</h3>
      <div class="relative w-40 h-40 flex items-center justify-center">
        <svg class="absolute inset-0 w-full h-full" viewBox="0 0 160 160">
          <circle cx="80" cy="80" r="64" fill="none" stroke="rgba(45,52,73,0.6)" stroke-width="10"/>
          <circle id="eff-arc" cx="80" cy="80" r="64" fill="none"
                  stroke="#5de6ff" stroke-width="10" stroke-linecap="round"
                  stroke-dasharray="402" stroke-dashoffset="402"
                  transform="rotate(-90 80 80)"
                  style="transition:stroke-dashoffset 1s ease;filter:drop-shadow(0 0 6px #5de6ff);"/>
        </svg>
        <div class="z-10 text-center">
          <span id="eff-score" class="text-4xl font-extrabold text-white">0%</span>
          <p class="text-[10px] font-mono text-outline uppercase">optimum</p>
        </div>
      </div>
      <div class="w-full space-y-2 text-xs">
        <div class="flex justify-between"><span class="text-outline">Lifetime Saved</span><span id="p-saved" class="font-mono text-secondary">$0.00</span></div>
        <div class="w-full h-0.5 bg-surface-container-highest rounded-full">
          <div id="p-saved-bar" class="h-full bg-secondary rounded-full transition-all duration-700" style="width:0%"></div>
        </div>
        <div class="flex justify-between mt-2"><span class="text-outline">Month Cost</span><span id="p-month-cost" class="font-mono text-primary">$0.00</span></div>
        <div class="flex justify-between"><span class="text-outline">Month Calls</span><span id="p-month-calls" class="font-mono text-on-surface">0</span></div>
      </div>
    </div>
  </div>
  <div class="grid grid-cols-1 lg:grid-cols-12 gap-8">
    <div class="lg:col-span-4 glass-panel rounded-3xl p-8">
      <h3 class="text-lg font-bold text-white mb-6">Model Distribution <span class="text-xs text-outline font-label font-normal ml-1">7 days</span></h3>
      <div class="h-52 flex items-center justify-center"><canvas id="modelChart"></canvas></div>
      <div id="model-legend" class="mt-4 space-y-1.5"></div>
    </div>
    <div class="lg:col-span-8 glass-panel rounded-3xl p-8">
      <h3 class="text-lg font-bold text-white mb-6">Model Benchmark</h3>
      <table class="w-full">
        <thead>
          <tr>
            <th class="text-left pb-4 text-[10px] font-mono text-outline uppercase tracking-wider">Backend Name</th>
            <th class="text-right pb-4 text-[10px] font-mono text-outline uppercase tracking-wider">Calls</th>
            <th class="text-right pb-4 text-[10px] font-mono text-outline uppercase tracking-wider">Cost / 1K tok</th>
            <th class="text-right pb-4 text-[10px] font-mono text-outline uppercase tracking-wider">Health</th>
          </tr>
        </thead>
        <tbody id="model-tbody">
          <tr><td colspan="4" class="py-10 text-center text-outline font-mono text-xs">Loading...</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</div>

<!-- TAB: Configuration -->
<div id="tab-config" class="p-8 max-w-[1600px] mx-auto space-y-8 hidden">
  <div>
    <h1 class="text-3xl font-extrabold font-headline text-white">Configuration &amp; Status</h1>
    <p class="text-sm text-outline font-label mt-1">Active routing profile and Claude subscription health</p>
  </div>
  <div class="grid grid-cols-1 lg:grid-cols-2 gap-8">
    <div class="glass-panel rounded-3xl p-8">
      <h3 class="text-lg font-bold text-white mb-6">Router Settings</h3>
      <div class="space-y-1">
        <div class="flex justify-between items-center py-3 border-b border-outline-variant/10">
          <span class="text-sm text-outline font-label">Active Profile</span>
          <span id="cfg-profile" class="font-mono text-sm text-primary bg-primary/10 px-3 py-1 rounded-lg">—</span>
        </div>
        <div class="flex justify-between items-center py-3 border-b border-outline-variant/10">
          <span class="text-sm text-outline font-label">CC Subscription Mode</span>
          <span id="cfg-sub" class="font-mono text-sm px-3 py-1 rounded-lg">—</span>
        </div>
        <div class="flex justify-between items-center py-3 border-b border-outline-variant/10">
          <span class="text-sm text-outline font-label">Monthly Budget</span>
          <span id="cfg-budget" class="font-mono text-sm text-on-surface">—</span>
        </div>
        <div class="flex justify-between items-center py-3">
          <span class="text-sm text-outline font-label">Daily Spend Limit</span>
          <span id="cfg-daily" class="font-mono text-sm text-on-surface">—</span>
        </div>
      </div>
    </div>
    <div class="glass-panel rounded-3xl p-8 space-y-6">
      <div class="flex justify-between items-start">
        <h3 class="text-lg font-bold text-white">Claude Subscription</h3>
        <span id="cfg-refresh-time" class="text-[10px] font-mono text-outline">—</span>
      </div>
      <div class="space-y-5">
        <div>
          <div class="flex justify-between text-xs mb-2">
            <span class="text-outline font-label">Session</span>
            <span id="cfg-session-pct" class="font-mono text-on-surface">0%</span>
          </div>
          <div class="w-full h-2 bg-surface-container-highest rounded-full overflow-hidden">
            <div id="cfg-session-bar" class="h-full bg-gradient-to-r from-secondary to-primary rounded-full transition-all duration-700" style="width:0%"></div>
          </div>
        </div>
        <div>
          <div class="flex justify-between text-xs mb-2">
            <span class="text-outline font-label">Weekly (all)</span>
            <span id="cfg-weekly-pct" class="font-mono text-on-surface">0%</span>
          </div>
          <div class="w-full h-2 bg-surface-container-highest rounded-full overflow-hidden">
            <div id="cfg-weekly-bar" class="h-full bg-gradient-to-r from-secondary to-primary rounded-full transition-all duration-700" style="width:0%"></div>
          </div>
        </div>
        <div>
          <div class="flex justify-between text-xs mb-2">
            <span class="text-outline font-label">Sonnet Only</span>
            <span id="cfg-sonnet-pct" class="font-mono text-on-surface">0%</span>
          </div>
          <div class="w-full h-2 bg-surface-container-highest rounded-full overflow-hidden">
            <div id="cfg-sonnet-bar" class="h-full bg-gradient-to-r from-secondary to-primary rounded-full transition-all duration-700" style="width:0%"></div>
          </div>
        </div>
      </div>
    </div>
  </div>
  <div class="glass-panel rounded-3xl p-8">
    <h3 class="text-lg font-bold text-white mb-6">Task Type Distribution <span class="text-xs text-outline font-label font-normal ml-1">7 days</span></h3>
    <div id="task-breakdown" class="grid grid-cols-2 md:grid-cols-5 gap-4">
      <div class="col-span-5 text-center text-outline font-mono text-xs py-4">Loading...</div>
    </div>
  </div>
</div>

<!-- TAB: Budget -->
<div id="tab-budget" class="p-8 max-w-[1600px] mx-auto space-y-8 hidden">
  <div>
    <h1 class="text-3xl font-extrabold font-headline text-white">Budget Caps</h1>
    <p class="text-sm text-outline font-label mt-1">Set monthly spend limits per provider — the router avoids providers as they approach their cap</p>
  </div>
  <div id="budget-summary" class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-2">
    <div class="bg-surface-container-low rounded-2xl border border-outline-variant/10 p-5">
      <p class="text-xs text-outline uppercase tracking-widest font-label mb-1">Total Monthly Spend</p>
      <p class="text-2xl font-bold text-on-surface" id="budget-total-spend">—</p>
    </div>
    <div class="bg-surface-container-low rounded-2xl border border-outline-variant/10 p-5">
      <p class="text-xs text-outline uppercase tracking-widest font-label mb-1">Total Budget Cap</p>
      <p class="text-2xl font-bold text-on-surface" id="budget-total-cap">—</p>
    </div>
    <div class="bg-surface-container-low rounded-2xl border border-outline-variant/10 p-5">
      <p class="text-xs text-outline uppercase tracking-widest font-label mb-1">Overall Pressure</p>
      <p class="text-2xl font-bold text-on-surface" id="budget-overall-pct">—</p>
    </div>
  </div>
  <div id="budget-providers" class="space-y-3"></div>
  <p class="text-xs text-outline mt-2">Caps are saved to <code class="font-mono">~/.llm-router/budgets.json</code> and take effect immediately on the next routing call.</p>
</div>

<!-- TAB: Logs -->
<div id="tab-logs" class="p-8 max-w-[1600px] mx-auto space-y-8 hidden">
  <div>
    <h1 class="text-3xl font-extrabold font-headline text-white">Routing Logs</h1>
    <p class="text-sm text-outline font-label mt-1">Recent routing decisions — last 20 entries</p>
  </div>
  <div class="bg-surface-container-low rounded-3xl border border-outline-variant/10 overflow-hidden">
    <table class="w-full border-collapse">
      <thead class="bg-surface-container-highest/50">
        <tr>
          <th class="text-left px-6 py-4 text-[10px] font-mono text-outline uppercase tracking-wider">Timestamp</th>
          <th class="text-left px-6 py-4 text-[10px] font-mono text-outline uppercase tracking-wider">Task</th>
          <th class="text-left px-6 py-4 text-[10px] font-mono text-outline uppercase tracking-wider">Complexity</th>
          <th class="text-left px-6 py-4 text-[10px] font-mono text-outline uppercase tracking-wider">Model</th>
          <th class="text-right px-6 py-4 text-[10px] font-mono text-outline uppercase tracking-wider">Cost</th>
          <th class="text-right px-6 py-4 text-[10px] font-mono text-outline uppercase tracking-wider">Latency</th>
          <th class="text-center px-6 py-4 text-[10px] font-mono text-outline uppercase tracking-wider">Status</th>
        </tr>
      </thead>
      <tbody id="logs-tbody">
        <tr><td colspan="7" class="px-6 py-10 text-center text-outline font-mono text-xs">Loading...</td></tr>
      </tbody>
    </table>
  </div>
</div>

</main>

<script>
// ── HTML escaping — all DB values go through esc() before any DOM insertion ──
function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s == null ? '' : s);
  return d.innerHTML;
}

// ── Tab navigation ────────────────────────────────────────────────────────
const TABS = ['overview', 'performance', 'config', 'logs', 'budget'];
function showTab(name) {
  TABS.forEach(t => {
    const panel = document.getElementById('tab-' + t);
    const btn   = document.querySelector('[data-tab="' + t + '"]');
    if (panel) panel.classList.toggle('hidden', t !== name);
    if (btn) {
      btn.className = btn.className
        .replace(/\bnav-active\b|\bnav-inactive\b/g, '').trim() + ' ' +
        (t === name ? 'nav-active' : 'nav-inactive') +
        ' flex items-center gap-3 w-full px-4 py-2.5 rounded-r-full transition-all text-left';
    }
  });
}

// ── Budget tab ────────────────────────────────────────────────────────────
async function loadBudget() {
  let data;
  try {
    const r = await fetch('/api/budget', {headers: {'X-Dashboard-Token': window.DASHBOARD_TOKEN}});
    data = await r.json();
  } catch(e) { return; }

  const providers = data.providers || [];
  const LOCAL = new Set(['ollama','vllm','llamacpp','lm_studio']);

  const paid = providers.filter(p => !LOCAL.has(p.provider));
  const totalSpend = paid.reduce((s, p) => s + (p.spend_usd || 0), 0);
  const totalCap   = paid.reduce((s, p) => s + (p.cap_usd  || 0), 0);
  const overallPct = totalCap > 0 ? Math.round(totalSpend / totalCap * 100) : 0;

  setText('budget-total-spend', '$' + totalSpend.toFixed(2));
  setText('budget-total-cap', totalCap > 0 ? '$' + totalCap.toFixed(2) : 'No cap');
  setText('budget-overall-pct', totalCap > 0 ? overallPct + '%' : '—');

  const container = document.getElementById('budget-providers');
  if (!container) return;
  container.innerHTML = providers.map(p => {
    const isLocal = LOCAL.has(p.provider);
    const pct = Math.round((p.pressure || 0) * 100);
    const barW = Math.min(100, pct);
    const barColor = pct >= 80 ? 'bg-error' : pct >= 50 ? 'bg-tertiary' : 'bg-primary';
    const capVal = p.cap_usd > 0 ? p.cap_usd.toFixed(2) : '';
    return `
    <div class="bg-surface-container-low rounded-2xl border border-outline-variant/10 p-5">
      <div class="flex items-center justify-between mb-3">
        <div>
          <span class="font-bold text-on-surface text-base">${esc(p.provider)}</span>
          ${isLocal ? '<span class="ml-2 text-xs text-primary bg-primary/10 px-2 py-0.5 rounded-full">local · free</span>' : ''}
        </div>
        <span class="text-sm text-outline">${isLocal ? 'free' : '$' + (p.spend_usd||0).toFixed(4) + ' spent'}</span>
      </div>
      <div class="w-full bg-surface-container rounded-full h-2 mb-3">
        <div class="${barColor} h-2 rounded-full transition-all" style="width:${barW}%"></div>
      </div>
      ${isLocal ? '' : `
      <div class="flex items-center gap-2 mt-2">
        <label class="text-xs text-outline">Monthly cap $</label>
        <input id="cap-input-${esc(p.provider)}" type="number" min="0" step="1"
          value="${esc(capVal)}" placeholder="no cap"
          class="w-24 bg-surface-container border border-outline-variant/30 rounded-lg px-2 py-1 text-sm text-on-surface focus:outline-none focus:ring-1 focus:ring-primary" />
        <button onclick="saveBudgetCap('${esc(p.provider)}')"
          class="px-3 py-1 text-xs font-mono bg-primary/10 hover:bg-primary/20 text-primary rounded-lg transition-all">
          Save
        </button>
        <span id="cap-status-${esc(p.provider)}" class="text-xs text-outline"></span>
      </div>`}
    </div>`;
  }).join('');
}

async function saveBudgetCap(provider) {
  const input = document.getElementById('cap-input-' + provider);
  const status = document.getElementById('cap-status-' + provider);
  if (!input || !status) return;
  const val = parseFloat(input.value);
  if (isNaN(val) || val < 0) { status.textContent = '✗ invalid'; return; }
  status.textContent = 'Saving…';
  try {
    const r = await fetch('/api/budget/set', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Dashboard-Token': window.DASHBOARD_TOKEN},
      body: JSON.stringify({provider, cap: val})
    });
    const d = await r.json();
    status.textContent = d.ok ? '✓ saved' : ('✗ ' + (d.error || 'error'));
    if (d.ok) setTimeout(() => loadBudget(), 500);
  } catch(e) { status.textContent = '✗ network error'; }
}

// ── Helpers ───────────────────────────────────────────────────────────────
function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}
function setBar(id, pct) {
  const el = document.getElementById(id);
  if (el) el.style.width = Math.min(100, Math.max(0, Math.round(pct * 100))) + '%';
}
function fmtNum(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return String(Math.round(n));
}
function normPct(v) { return typeof v === 'number' ? (v <= 1 ? v : v / 100) : 0; }
function shortModel(m) { return String(m || '—').split('/').pop(); }
function shortTs(ts) {
  const s = String(ts || '');
  return s.includes('T') ? s.split('T')[1].split('.')[0] : s.slice(-8) || '—';
}
function fullTs(ts) { return String(ts || '').replace('T', ' ').split('.')[0] || '—'; }

// ── Charts ────────────────────────────────────────────────────────────────
let costChart = null, modelChart = null;
const PALETTE = ['#d0bcff','#5de6ff','#ffb869','#a078ff','#2fd9f4','#ca801e','#958ea0'];
const CHART_SHARED = {
  responsive: true, maintainAspectRatio: false,
  plugins: {
    legend: { display: false },
    tooltip: {
      backgroundColor: 'rgba(6,14,32,0.95)', borderColor: 'rgba(208,188,255,0.2)', borderWidth: 1,
      titleColor: '#d0bcff', bodyColor: '#cbc3d7', padding: 12, cornerRadius: 8,
    },
  },
  scales: {
    x: { grid: { color: 'rgba(73,68,84,0.15)' }, ticks: { color: '#958ea0', font: { family: 'JetBrains Mono', size: 10 } } },
    y: { grid: { color: 'rgba(73,68,84,0.15)' }, ticks: { color: '#958ea0', font: { family: 'JetBrains Mono', size: 10 } } },
  },
};
function initOrUpdateChart(chart, id, config) {
  const canvas = document.getElementById(id);
  if (!canvas) return chart;
  if (chart) { chart.data = config.data; chart.update(); return chart; }
  return new Chart(canvas.getContext('2d'), config);
}

// ── Routing flow (SVG) ────────────────────────────────────────────────────
const TASK_COLORS = {
  code:'#d0bcff', research:'#5de6ff', generate:'#ffb869',
  analyze:'#a078ff', query:'#2fd9f4', image:'#ca801e', audio:'#ffb4ab',
};
const MODEL_COLORS = ['#d0bcff','#5de6ff','#ffb869','#a078ff'];

function buildRoutingFlow(taskTypes, models) {
  const el = document.getElementById('routing-flow');
  if (!el) return;
  const tasks = (taskTypes || []).slice(0, 5);
  const mdls  = (models    || []).slice(0, 4);
  if (!tasks.length) {
    // Safe: only static text, no user data
    el.textContent = 'No routing data yet — make a few calls first';
    el.className += ' flex items-center justify-center text-outline font-mono text-xs';
    return;
  }
  const H = 176, ls = H / (tasks.length + 1), rs = H / (mdls.length + 1);
  const maxCalls = Math.max(...tasks.map(t => t.calls), 1);

  // Build SVG paths (only numeric and color values — no user strings in SVG)
  let paths = '';
  tasks.forEach((t, li) => {
    const ly = (ls * (li + 1)) / H * 100;
    const color = TASK_COLORS[t.task_type] || '#958ea0';
    const sw = (1.5 + (t.calls / maxCalls) * 6).toFixed(1);
    mdls.forEach((m, ri) => {
      const ry = (rs * (ri + 1)) / H * 100;
      const op = li === ri ? 0.35 : 0.12;
      paths += `<path d="M 140 ${ly} C 220 ${ly}, 220 ${ry}, 300 ${ry}" fill="transparent" stroke="${color}" stroke-width="${sw}" stroke-opacity="${op}"/>`;
    });
  });

  const svgEl = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svgEl.setAttribute('class', 'absolute inset-0 w-full h-full pointer-events-none');
  svgEl.setAttribute('viewBox', '0 0 440 100');
  svgEl.setAttribute('preserveAspectRatio', 'none');
  svgEl.innerHTML = paths; // paths contain only numeric coords & hex colors — safe

  const wrapper = document.createElement('div');
  wrapper.className = 'relative w-full h-full';
  wrapper.appendChild(svgEl);

  // Left labels (task types) — built with DOM, esc() for text
  tasks.forEach((t, i) => {
    const y = ls * (i + 1);
    const color = TASK_COLORS[t.task_type] || '#958ea0';
    const row = document.createElement('div');
    row.className = 'absolute left-0 flex items-center gap-2';
    row.style.top = (y - 14) + 'px';
    const badge = document.createElement('div');
    badge.className = 'px-3 py-1 rounded font-mono text-xs text-on-surface bg-surface-container-lowest';
    badge.style.border = `1px solid ${color}44`;
    badge.textContent = t.task_type || '?'; // textContent — XSS-safe
    const cnt = document.createElement('span');
    cnt.className = 'text-[9px] font-mono';
    cnt.style.color = color;
    cnt.textContent = t.calls;
    row.appendChild(badge); row.appendChild(cnt);
    wrapper.appendChild(row);
  });

  // Right labels (models) — built with DOM, esc() via textContent
  mdls.forEach((m, i) => {
    const y = rs * (i + 1);
    const color = MODEL_COLORS[i % MODEL_COLORS.length];
    const label = shortModel(m.model).split('-').slice(0, 3).join('-');
    const row = document.createElement('div');
    row.className = 'absolute right-0 text-right';
    row.style.top = (y - 14) + 'px';
    const badge = document.createElement('div');
    badge.className = 'px-3 py-1 rounded font-mono text-xs inline-block';
    badge.style.border = `1px solid ${color}55`;
    badge.style.color = color;
    badge.style.background = color + '10';
    badge.textContent = label; // textContent — XSS-safe
    row.appendChild(badge);
    wrapper.appendChild(row);
  });

  el.innerHTML = '';
  el.appendChild(wrapper);
}

// ── Table renderers (all user strings go through esc()) ───────────────────
function statusBadge(ok) {
  // No user data here — only static strings
  return ok
    ? '<span class="text-[10px] px-2 py-0.5 rounded font-mono border bg-secondary/10 text-secondary border-secondary/20">200 OK</span>'
    : '<span class="text-[10px] px-2 py-0.5 rounded font-mono border bg-error/10 text-error border-error/20">FAILED</span>';
}

function buildRecentTraffic(rows) {
  const tbody = document.getElementById('recent-tbody');
  if (!tbody) return;
  tbody.replaceChildren();
  if (!rows?.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 5; td.className = 'px-6 py-10 text-center text-outline font-mono text-xs';
    td.textContent = 'No routing decisions yet';
    tr.appendChild(td); tbody.appendChild(tr); return;
  }
  rows.slice(0, 8).forEach(r => {
    const ok   = r.success !== 0;
    const cost = r.cost_usd ? '$' + Number(r.cost_usd).toFixed(4) : '$0.00';
    const lat  = r.latency_ms ? Math.round(r.latency_ms) + 'ms' : '—';
    const dotColor = ok ? '#5de6ff' : '#ffb4ab';
    const tr = document.createElement('tr');
    tr.className = 'hover:bg-surface-container-high/50 transition-colors';
    tr.innerHTML =
      `<td class="px-6 py-4 text-xs font-mono text-on-surface-variant">${esc(shortTs(r.timestamp))}</td>` +
      `<td class="px-6 py-4 font-mono text-xs text-white">${esc(r.task_type || '—')} <span class="text-[10px] text-outline ml-1">${esc(r.complexity || '')}</span></td>` +
      `<td class="px-6 py-4"><div class="flex items-center gap-2"><div class="w-1.5 h-1.5 rounded-full flex-shrink-0" style="background:${dotColor}"></div><span class="font-mono text-xs text-on-surface">${esc(shortModel(r.model))}</span></div></td>` +
      `<td class="px-6 py-4">${statusBadge(ok)}</td>` +
      `<td class="px-6 py-4 text-right font-mono text-xs text-on-surface-variant">${esc(cost)} / ${esc(lat)}</td>`;
    tbody.appendChild(tr);
  });
}

function buildLogs(rows) {
  const tbody = document.getElementById('logs-tbody');
  if (!tbody) return;
  tbody.replaceChildren();
  if (!rows?.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 7; td.className = 'px-6 py-10 text-center text-outline font-mono text-xs';
    td.textContent = 'No routing decisions yet';
    tr.appendChild(td); tbody.appendChild(tr); return;
  }
  const cxColor = { simple: 'text-secondary', moderate: 'text-tertiary', complex: 'text-primary' };
  rows.forEach(r => {
    const ok   = r.success !== 0;
    const cost = r.cost_usd ? '$' + Number(r.cost_usd).toFixed(4) : '$0.00';
    const lat  = r.latency_ms ? Math.round(r.latency_ms) + 'ms' : '—';
    const cx   = cxColor[r.complexity] || 'text-outline';
    const tr = document.createElement('tr');
    tr.className = 'hover:bg-surface-container-high/50 transition-colors';
    tr.innerHTML =
      `<td class="px-6 py-4 text-xs font-mono text-on-surface-variant">${esc(fullTs(r.timestamp))}</td>` +
      `<td class="px-6 py-4 font-mono text-xs text-white">${esc(r.task_type || '—')}</td>` +
      `<td class="px-6 py-4 font-mono text-xs ${cx}">${esc(r.complexity || '—')}</td>` +
      `<td class="px-6 py-4 font-mono text-xs text-on-surface">${esc(shortModel(r.model))}</td>` +
      `<td class="px-6 py-4 text-right font-mono text-xs text-on-surface-variant">${esc(cost)}</td>` +
      `<td class="px-6 py-4 text-right font-mono text-xs text-on-surface-variant">${esc(lat)}</td>` +
      `<td class="px-6 py-4 text-center">${statusBadge(ok)}</td>`;
    tbody.appendChild(tr);
  });
}

function buildModelTable(models) {
  const tbody = document.getElementById('model-tbody');
  if (!tbody) return;
  tbody.replaceChildren();
  if (!models?.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 4; td.className = 'py-10 text-center text-outline font-mono text-xs';
    td.textContent = 'No model data yet';
    tr.appendChild(td); tbody.appendChild(tr); return;
  }
  models.forEach(m => {
    const label = shortModel(m.model);
    const c1k   = m.calls > 0 ? '$' + (m.cost_usd / m.calls * 1000).toFixed(4) : '$0.00';
    const tr = document.createElement('tr');
    tr.className = 'hover:bg-surface-container-high/50 transition-colors';
    tr.innerHTML =
      `<td class="py-3.5 font-mono text-sm text-on-surface">${esc(label)}</td>` +
      `<td class="py-3.5 text-right font-mono text-sm text-on-surface-variant">${esc(m.calls)}</td>` +
      `<td class="py-3.5 text-right font-mono text-sm text-on-surface-variant">${esc(c1k)}</td>` +
      `<td class="py-3.5 text-right"><span class="text-[10px] font-mono text-secondary bg-secondary/10 px-2 py-0.5 rounded border border-secondary/20">OPERATIONAL</span></td>`;
    tbody.appendChild(tr);
  });
}

function buildTaskBreakdown(taskTypes) {
  const el = document.getElementById('task-breakdown');
  if (!el) return;
  el.replaceChildren();
  if (!taskTypes?.length) {
    const div = document.createElement('div');
    div.className = 'col-span-5 text-center text-outline font-mono text-xs py-4';
    div.textContent = 'No data yet';
    el.appendChild(div); return;
  }
  const total = taskTypes.reduce((s, t) => s + t.calls, 0);
  taskTypes.forEach(t => {
    const pct   = total > 0 ? Math.round(t.calls / total * 100) : 0;
    const color = TASK_COLORS[t.task_type] || '#958ea0';
    const card  = document.createElement('div');
    card.className = 'bg-surface-container-high rounded-2xl p-4 border border-outline-variant/10 text-center';
    const pctEl = document.createElement('div');
    pctEl.className = 'text-2xl font-extrabold font-mono mb-1';
    pctEl.style.color = color;
    pctEl.textContent = pct + '%';
    const nameEl = document.createElement('div');
    nameEl.className = 'text-[10px] font-mono text-outline uppercase tracking-wider';
    nameEl.textContent = t.task_type || '?';
    const cntEl = document.createElement('div');
    cntEl.className = 'text-xs font-mono text-on-surface-variant mt-1';
    cntEl.textContent = t.calls + ' calls';
    card.appendChild(pctEl); card.appendChild(nameEl); card.appendChild(cntEl);
    el.appendChild(card);
  });
}

function buildModelLegend(models) {
  const el = document.getElementById('model-legend');
  if (!el || !models?.length) return;
  el.replaceChildren();
  models.slice(0, 6).forEach((m, i) => {
    const label = shortModel(m.model);
    const row   = document.createElement('div');
    row.className = 'flex items-center justify-between text-xs py-1';
    const left  = document.createElement('div');
    left.className = 'flex items-center gap-2';
    const dot   = document.createElement('div');
    dot.className = 'w-2 h-2 rounded-full flex-shrink-0';
    dot.style.background = PALETTE[i % PALETTE.length];
    const name  = document.createElement('span');
    name.className = 'font-mono text-on-surface-variant';
    name.textContent = label;
    const cnt   = document.createElement('span');
    cnt.className = 'font-mono text-outline';
    cnt.textContent = m.calls;
    left.appendChild(dot); left.appendChild(name);
    row.appendChild(left); row.appendChild(cnt);
    el.appendChild(row);
  });
}

// ── Main data application ─────────────────────────────────────────────────
function applyData(d) {
  setText('mode-badge', d.config?.subscription_mode ? 'CC Subscription' : 'API Mode');
  setText('refresh-ts', 'Updated ' + new Date().toLocaleTimeString());
  setText('sb-status', '● Connected');
  setText('sb-profile', (d.config?.profile || '?') + ' | v' + (d.config?.version || '?'));

  const saved = d.savings?.total_saved_usd ?? 0;
  const ext   = d.savings?.total_external_usd ?? 0;
  const eff   = (saved + ext) > 0 ? saved / (saved + ext) : 0;

  setText('g-saved', '$' + saved.toFixed(2));
  setText('eff-pct', Math.round(eff * 100) + '%');
  setText('g-ext-cost', '$' + ext.toFixed(2));
  setText('g-today-cost', '$' + (d.today?.cost_usd || 0).toFixed(4));

  const gaugeArc = document.getElementById('gauge-arc');
  if (gaugeArc) gaugeArc.style.strokeDashoffset = 540 * (1 - eff);
  const effArc = document.getElementById('eff-arc');
  if (effArc) effArc.style.strokeDashoffset = 402 * (1 - eff);
  setText('eff-score', Math.round(eff * 100) + '%');

  setBar('g-ext-bar', (saved + ext) > 0 ? ext / (saved + ext) : 0);
  const bgt = d.config?.monthly_budget || 0;
  setBar('g-today-bar', bgt > 0 ? (d.today?.cost_usd || 0) / bgt : Math.min(1, d.today?.cost_usd || 0));

  setText('s-calls',   fmtNum(d.today?.calls  || 0));
  setText('s-tokens',  fmtNum(d.today?.tokens || 0));
  setText('s-semhits', fmtNum(d.semantic_cache?.hits ?? 0));

  setText('p-saved',       '$' + saved.toFixed(2));
  setText('p-month-cost',  '$' + (d.month?.cost_usd || 0).toFixed(4));
  setText('p-month-calls', String(d.month?.calls || 0));
  setBar('p-saved-bar', eff);

  const cfg = d.config || {};
  setText('cfg-profile', cfg.profile || '—');
  const subEl = document.getElementById('cfg-sub');
  if (subEl) {
    subEl.textContent = cfg.subscription_mode ? 'Enabled' : 'Disabled';
    subEl.className = cfg.subscription_mode
      ? 'font-mono text-sm text-secondary bg-secondary/10 px-3 py-1 rounded-lg'
      : 'font-mono text-sm text-outline bg-surface-container-high px-3 py-1 rounded-lg';
  }
  setText('cfg-budget', cfg.monthly_budget > 0 ? '$' + cfg.monthly_budget.toFixed(2) : 'Unlimited');
  setText('cfg-daily',  cfg.daily_limit   > 0 ? '$' + cfg.daily_limit.toFixed(2)   : 'Unlimited');

  const u   = d.usage || {};
  const sp  = normPct(u.session_pct);
  const wp  = normPct(u.weekly_pct);
  const snp = normPct(u.sonnet_pct);
  setText('cfg-session-pct', Math.round(sp  * 100) + '%');
  setText('cfg-weekly-pct',  Math.round(wp  * 100) + '%');
  setText('cfg-sonnet-pct',  Math.round(snp * 100) + '%');
  setText('cfg-refresh-time', u.last_updated || '—');
  setBar('cfg-session-bar', sp);
  setBar('cfg-weekly-bar',  wp);
  setBar('cfg-sonnet-bar',  snp);

  buildTaskBreakdown(d.task_types);
  buildRoutingFlow(d.task_types, d.models);
  buildRecentTraffic(d.recent);
  buildLogs(d.recent);
  buildModelTable(d.models);
  buildModelLegend(d.models);

  const dc = d.daily_cost ?? [];
  costChart = initOrUpdateChart(costChart, 'costChart', {
    type: 'bar',
    data: {
      labels: dc.map(r => r.day?.slice(5) || ''),
      datasets: [{
        label: 'USD', data: dc.map(r => r.cost_usd),
        backgroundColor: 'rgba(208,188,255,0.2)',
        borderColor: '#d0bcff', borderWidth: 1,
        borderRadius: 6, borderSkipped: false,
      }],
    },
    options: { ...CHART_SHARED, animation: { duration: 800 } },
  });

  const models = d.models ?? [];
  modelChart = initOrUpdateChart(modelChart, 'modelChart', {
    type: 'doughnut',
    data: {
      labels: models.map(m => shortModel(m.model)),
      datasets: [{ data: models.map(m => m.calls), backgroundColor: PALETTE, borderWidth: 0, hoverOffset: 6 }],
    },
    options: {
      responsive: true, maintainAspectRatio: false, cutout: '70%',
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: 'rgba(6,14,32,0.95)', borderColor: 'rgba(208,188,255,0.2)', borderWidth: 1,
          titleColor: '#d0bcff', bodyColor: '#cbc3d7', padding: 12, cornerRadius: 8,
        },
      },
    },
  });
}

// ── Refresh loop ──────────────────────────────────────────────────────────
async function doRefresh() {
  const btn = document.getElementById('refresh-btn');
  if (btn) btn.classList.add('spinning');
  try {
    const res = await fetch('/api/stats', {headers: {'X-Dashboard-Token': window.DASHBOARD_TOKEN}});
    const d   = await res.json();
    applyData(d);
  } catch (e) {
    setText('sb-status', '○ Error — retrying in 30s');
  } finally {
    if (btn) btn.classList.remove('spinning');
  }
}

doRefresh();
setInterval(doRefresh, 30000);

// Load budget tab on first visit
document.querySelector('[data-tab="budget"]').addEventListener('click', () => loadBudget());
</script>
</body>
</html>"""
    # Inject the dashboard auth token as a JS global so all fetch() calls can use it
    return html.replace(
        "<script>\n// ── HTML escaping",
        f"<script>\nwindow.DASHBOARD_TOKEN = {json.dumps(token)};\n// ── HTML escaping",
        1,
    )


async def run(port: int = DEFAULT_PORT) -> None:
    """Start the dashboard HTTP server.

    Args:
        port: TCP port to listen on (default 7337).
    """
    configure_logging()
    try:
        from aiohttp import web
    except ImportError:
        log.error(
            "dashboard_missing_dependency",
            dependency="aiohttp",
            install_hint="pip install aiohttp or uv add aiohttp",
        )
        return

    token = _get_or_create_token()

    @web.middleware
    async def auth_middleware(request: "web.Request", handler) -> "web.Response":
        if request.path == "/":
            return await handler(request)
        provided = request.headers.get("X-Dashboard-Token") or request.rel_url.query.get("token")
        if provided != token:
            raise web.HTTPUnauthorized(text="Unauthorized")
        return await handler(request)

    async def handle_index(request: "web.Request") -> "web.Response":
        return web.Response(text=_html(token), content_type="text/html")

    async def handle_stats(request: "web.Request") -> "web.Response":
        stats = await _get_stats()
        return web.Response(
            text=json.dumps(stats, default=str),
            content_type="application/json",
        )

    async def handle_budget_get(request: "web.Request") -> "web.Response":
        from llm_router.budget import get_all_budget_states
        from llm_router.budget_store import list_caps
        states = await get_all_budget_states()
        providers = [
            {
                "provider": p,
                "pressure": round(s.pressure, 4),
                "spend_usd": round(s.spend_usd, 4),
                "cap_usd": round(s.cap_usd, 2),
            }
            for p, s in sorted(states.items())
        ]
        return web.Response(
            text=json.dumps({"providers": providers, "caps": list_caps()}),
            content_type="application/json",
        )

    async def handle_budget_set(request: "web.Request") -> "web.Response":
        try:
            body = await request.json()
            provider = str(body["provider"]).strip().lower()
            cap = float(body["cap"])
        except Exception as e:
            return web.Response(
                text=json.dumps({"ok": False, "error": str(e)}),
                content_type="application/json", status=400,
            )
        from llm_router.budget_store import set_cap, remove_cap
        from llm_router.budget import invalidate_cache
        if cap <= 0:
            remove_cap(provider)
        else:
            set_cap(provider, cap)
        invalidate_cache(provider)
        return web.Response(
            text=json.dumps({"ok": True, "provider": provider, "cap": cap}),
            content_type="application/json",
        )

    async def handle_metrics(request: "web.Request") -> "web.Response":
        from llm_router.budget import get_all_budget_states
        from llm_router.cost import get_savings_by_period
        states = await get_all_budget_states()
        try:
            savings = await get_savings_by_period()
            total_saved = savings.get("all_time", {}).get("saved_usd", 0.0)
            total_external = savings.get("all_time", {}).get("external_usd", 0.0)
        except Exception:
            total_saved = total_external = 0.0

        lines = [
            "# HELP llm_router_spend_usd Monthly spend per provider",
            "# TYPE llm_router_spend_usd gauge",
        ]
        for p, s in sorted(states.items()):
            lines.append(f'llm_router_spend_usd{{provider="{p}"}} {s.spend_usd:.4f}')

        lines += [
            "# HELP llm_router_budget_pressure Budget pressure per provider (0=free 1=exhausted)",
            "# TYPE llm_router_budget_pressure gauge",
        ]
        for p, s in sorted(states.items()):
            lines.append(f'llm_router_budget_pressure{{provider="{p}"}} {s.pressure:.4f}')

        lines += [
            "# HELP llm_router_cap_usd Configured monthly cap per provider (0=unlimited)",
            "# TYPE llm_router_cap_usd gauge",
        ]
        for p, s in sorted(states.items()):
            lines.append(f'llm_router_cap_usd{{provider="{p}"}} {s.cap_usd:.2f}')

        lines += [
            "# HELP llm_router_savings_usd_total Lifetime savings vs Sonnet baseline",
            "# TYPE llm_router_savings_usd_total counter",
            f"llm_router_savings_usd_total {total_saved:.4f}",
            "# HELP llm_router_external_usd_total Lifetime external API spend",
            "# TYPE llm_router_external_usd_total counter",
            f"llm_router_external_usd_total {total_external:.4f}",
            "",
        ]
        return web.Response(text="\n".join(lines), content_type="text/plain")

    app = web.Application(middlewares=[auth_middleware])
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/stats", handle_stats)
    app.router.add_get("/api/budget", handle_budget_get)
    app.router.add_post("/api/budget/set", handle_budget_set)
    app.router.add_get("/metrics", handle_metrics)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", port)
    await site.start()

    dashboard_url = f"http://localhost:{port}/?token={token}"
    log.info(
        "dashboard_started",
        port=port,
        token=token,
        url=dashboard_url,
    )
    log.info("dashboard_ready", stop_hint="Press Ctrl+C to stop")

    try:
        import asyncio
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await runner.cleanup()
