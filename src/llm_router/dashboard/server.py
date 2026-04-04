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
import logging
from pathlib import Path

log = logging.getLogger("llm_router.dashboard")

DEFAULT_PORT = 7337


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
                "SELECT timestamp, task_type, complexity, final_model, "
                "cost_usd, latency_ms, success "
                "FROM routing_decisions ORDER BY timestamp DESC LIMIT 10"
            )
            stats["recent"] = [
                {
                    "timestamp": r[0], "task_type": r[1], "complexity": r[2],
                    "model": r[3], "cost_usd": r[4], "latency_ms": r[5], "success": r[6],
                }
                for r in await c.fetchall()
            ]

            c = await db.execute(
                "SELECT COALESCE(SUM(estimated_claude_cost_saved),0), "
                "COALESCE(SUM(external_cost),0) FROM savings_stats"
            )
            row = await c.fetchone()
            if row:
                stats["savings"] = {
                    "total_saved_usd": round(row[0], 4),
                    "total_external_usd": round(row[1], 4),
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

    stats["config"] = {
        "profile": config.llm_router_profile.value,
        "monthly_budget": config.llm_router_monthly_budget,
        "daily_limit": config.llm_router_daily_spend_limit,
        "subscription_mode": config.llm_router_claude_subscription,
    }

    return stats


def _html() -> str:
    """Return the self-contained dashboard HTML.

    All database values are passed as JSON and rendered via textContent /
    Chart.js data arrays — never injected via innerHTML — so there is no
    XSS surface even if the SQLite database is tampered with.

    Design: "Liquid Glass" dark theme inspired by Google Stitch designs.
    Primary: #8B5CF6 (purple), Secondary: #22D3EE (cyan), BG: #0F172A.
    Fonts: Inter (body), Space Grotesk (labels) — loaded from Google Fonts CDN.
    """
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LLM Router</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Space+Grotesk:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
:root {
  --bg:        #0F172A;
  --surface:   #1E293B;
  --surface2:  #162032;
  --border:    rgba(139,92,246,0.18);
  --border2:   rgba(255,255,255,0.06);
  --purple:    #8B5CF6;
  --purple-lo: rgba(139,92,246,0.12);
  --purple-md: rgba(139,92,246,0.25);
  --cyan:      #22D3EE;
  --cyan-lo:   rgba(34,211,238,0.10);
  --amber:     #F59E0B;
  --green:     #10B981;
  --red:       #EF4444;
  --text:      #E2E8F0;
  --text-muted:#94A3B8;
  --text-dim:  #475569;
  --sidebar-w: 220px;
  --radius:    14px;
  --radius-sm: 8px;
}
*,*::before,*::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: 'Inter', system-ui, sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  display: flex;
  overflow-x: hidden;
}

/* ── Sidebar ────────────────────────────────── */
.sidebar {
  width: var(--sidebar-w);
  min-height: 100vh;
  background: linear-gradient(180deg, #131f35 0%, #0d1625 100%);
  border-right: 1px solid var(--border2);
  display: flex;
  flex-direction: column;
  padding: 1.5rem 0;
  position: fixed;
  top: 0; left: 0; bottom: 0;
  z-index: 100;
  transform: translateX(0);
  transition: transform 0.3s cubic-bezier(0.4,0,0.2,1);
}
.sidebar-logo {
  display: flex; align-items: center; gap: 0.6rem;
  padding: 0 1.25rem 1.75rem;
  border-bottom: 1px solid var(--border2);
  margin-bottom: 1rem;
}
.sidebar-logo .logo-icon {
  width: 34px; height: 34px;
  background: linear-gradient(135deg, var(--purple) 0%, #6D28D9 100%);
  border-radius: 9px;
  display: flex; align-items: center; justify-content: center;
  font-size: 1.1rem;
  box-shadow: 0 0 16px rgba(139,92,246,0.4);
  animation: logoGlow 3s ease-in-out infinite;
}
@keyframes logoGlow {
  0%,100% { box-shadow: 0 0 16px rgba(139,92,246,0.4); }
  50%      { box-shadow: 0 0 28px rgba(139,92,246,0.7); }
}
.sidebar-logo .logo-text {
  font-family: 'Space Grotesk', sans-serif;
  font-weight: 700; font-size: 1rem;
  background: linear-gradient(90deg, var(--text) 0%, var(--purple) 100%);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
}
.sidebar-logo .logo-ver {
  font-size: 0.6rem; color: var(--text-dim);
  font-family: 'Space Grotesk', sans-serif;
  margin-top: 1px;
}

.nav-section-label {
  font-family: 'Space Grotesk', sans-serif;
  font-size: 0.65rem; font-weight: 600;
  letter-spacing: 0.1em;
  color: var(--text-dim);
  text-transform: uppercase;
  padding: 0.6rem 1.25rem 0.3rem;
}

.nav-item {
  display: flex; align-items: center; gap: 0.65rem;
  padding: 0.6rem 1.25rem;
  font-size: 0.85rem; font-weight: 500;
  color: var(--text-muted);
  cursor: pointer;
  border-radius: 0;
  border-left: 2px solid transparent;
  transition: all 0.2s ease;
  user-select: none;
  position: relative;
  overflow: hidden;
}
.nav-item::before {
  content: '';
  position: absolute; inset: 0;
  background: var(--purple-lo);
  transform: translateX(-100%);
  transition: transform 0.25s ease;
}
.nav-item:hover::before { transform: translateX(0); }
.nav-item:hover { color: var(--text); border-left-color: var(--purple); }
.nav-item.active {
  color: var(--purple);
  border-left-color: var(--purple);
  background: var(--purple-lo);
}
.nav-item .nav-icon { font-size: 0.9rem; width: 18px; text-align: center; flex-shrink: 0; }

.sidebar-footer {
  margin-top: auto;
  padding: 1rem 1.25rem 0;
  border-top: 1px solid var(--border2);
}
.status-dot {
  display: inline-block; width: 7px; height: 7px;
  border-radius: 50%; background: var(--green);
  box-shadow: 0 0 8px var(--green);
  animation: pulse-dot 2s ease-in-out infinite;
  margin-right: 0.4rem;
}
@keyframes pulse-dot {
  0%,100% { opacity: 1; transform: scale(1); }
  50%      { opacity: 0.6; transform: scale(0.85); }
}
.sidebar-status { font-size: 0.72rem; color: var(--text-muted); }

/* ── Main area ──────────────────────────────── */
.main {
  margin-left: var(--sidebar-w);
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 100vh;
}

/* ── Top bar ────────────────────────────────── */
.topbar {
  display: flex; align-items: center; gap: 1rem;
  padding: 1rem 2rem;
  background: rgba(30,41,59,0.6);
  backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--border2);
  position: sticky; top: 0; z-index: 50;
}
.topbar-title {
  font-family: 'Space Grotesk', sans-serif;
  font-size: 1rem; font-weight: 600;
  color: var(--text);
}
.topbar-badge {
  font-family: 'Space Grotesk', sans-serif;
  font-size: 0.65rem; font-weight: 600;
  letter-spacing: 0.06em;
  padding: 2px 10px;
  border-radius: 999px;
  background: var(--purple-md);
  color: var(--purple);
  border: 1px solid var(--border);
  animation: badgeFade 0.5s ease;
}
@keyframes badgeFade { from { opacity: 0; transform: scale(0.85); } to { opacity: 1; transform: scale(1); } }
.topbar-right { margin-left: auto; display: flex; align-items: center; gap: 0.75rem; }
.topbar-time { font-size: 0.72rem; color: var(--text-dim); }
.refresh-btn {
  background: var(--purple-lo);
  border: 1px solid var(--border);
  color: var(--purple);
  font-size: 0.75rem; font-weight: 500;
  padding: 4px 12px; border-radius: 6px;
  cursor: pointer;
  transition: all 0.2s;
}
.refresh-btn:hover { background: var(--purple-md); }
.refresh-btn.spinning .btn-icon { display: inline-block; animation: spin 0.6s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

/* ── Panels ─────────────────────────────────── */
.panel { display: none; animation: panelIn 0.35s cubic-bezier(0.4,0,0.2,1); }
.panel.active { display: block; }
@keyframes panelIn {
  from { opacity: 0; transform: translateY(10px); }
  to   { opacity: 1; transform: translateY(0); }
}

/* ── Stat cards ─────────────────────────────── */
.stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 1rem;
  padding: 1.5rem 2rem 0;
}
.stat-card {
  background: linear-gradient(135deg, var(--surface) 0%, var(--surface2) 100%);
  border: 1px solid var(--border2);
  border-radius: var(--radius);
  padding: 1.25rem 1.5rem;
  position: relative;
  overflow: hidden;
  transition: transform 0.25s ease, box-shadow 0.25s ease, border-color 0.25s ease;
  animation: cardIn 0.4s cubic-bezier(0.4,0,0.2,1) both;
}
.stat-card:nth-child(1) { animation-delay: 0.05s; }
.stat-card:nth-child(2) { animation-delay: 0.10s; }
.stat-card:nth-child(3) { animation-delay: 0.15s; }
.stat-card:nth-child(4) { animation-delay: 0.20s; }
.stat-card:nth-child(5) { animation-delay: 0.25s; }
.stat-card:nth-child(6) { animation-delay: 0.30s; }
@keyframes cardIn {
  from { opacity: 0; transform: translateY(16px); }
  to   { opacity: 1; transform: translateY(0); }
}
.stat-card::before {
  content: '';
  position: absolute; top: -1px; left: 0; right: 0; height: 2px;
  background: linear-gradient(90deg, transparent, var(--purple), transparent);
  opacity: 0;
  transition: opacity 0.3s;
}
.stat-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 8px 30px rgba(0,0,0,0.3), 0 0 0 1px var(--border);
  border-color: var(--border);
}
.stat-card:hover::before { opacity: 1; }
.stat-card .card-label {
  font-family: 'Space Grotesk', sans-serif;
  font-size: 0.65rem; font-weight: 600;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  margin-bottom: 0.6rem;
}
.stat-card .card-value {
  font-family: 'Space Grotesk', sans-serif;
  font-size: 1.85rem; font-weight: 700;
  color: var(--text);
  line-height: 1;
  transition: color 0.3s;
}
.stat-card .card-sub {
  font-size: 0.72rem; color: var(--text-dim);
  margin-top: 0.35rem;
}
.stat-card .card-icon {
  position: absolute; top: 1.1rem; right: 1.25rem;
  font-size: 1.4rem; opacity: 0.25;
  transition: opacity 0.3s, transform 0.3s;
}
.stat-card:hover .card-icon { opacity: 0.45; transform: scale(1.1); }

/* Color accents */
.accent-purple .card-value { color: var(--purple); }
.accent-cyan   .card-value { color: var(--cyan); }
.accent-amber  .card-value { color: var(--amber); }
.accent-green  .card-value { color: var(--green); }

/* ── Charts grid ────────────────────────────── */
.charts-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1rem;
  padding: 1.25rem 2rem 2rem;
}
.chart-card {
  background: linear-gradient(135deg, var(--surface) 0%, var(--surface2) 100%);
  border: 1px solid var(--border2);
  border-radius: var(--radius);
  padding: 1.4rem;
  transition: border-color 0.25s;
  animation: cardIn 0.45s cubic-bezier(0.4,0,0.2,1) both;
}
.chart-card:nth-child(1) { animation-delay: 0.15s; }
.chart-card:nth-child(2) { animation-delay: 0.20s; }
.chart-card:nth-child(3) { animation-delay: 0.25s; }
.chart-card:nth-child(4) { animation-delay: 0.30s; }
.chart-card:hover { border-color: var(--border); }
.chart-card-header {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 1.1rem;
}
.chart-card-title {
  font-family: 'Space Grotesk', sans-serif;
  font-size: 0.78rem; font-weight: 600;
  color: var(--text-muted);
  letter-spacing: 0.03em;
}
.chart-card-badge {
  font-size: 0.6rem; font-weight: 600;
  padding: 1px 7px; border-radius: 999px;
  background: var(--cyan-lo); color: var(--cyan);
  border: 1px solid rgba(34,211,238,0.25);
  font-family: 'Space Grotesk', sans-serif;
}

/* ── Traffic table ──────────────────────────── */
.traffic-table { width: 100%; border-collapse: collapse; font-size: 0.78rem; }
.traffic-table th {
  font-family: 'Space Grotesk', sans-serif;
  font-size: 0.63rem; font-weight: 600;
  color: var(--text-dim);
  text-transform: uppercase; letter-spacing: 0.07em;
  padding: 0.4rem 0.6rem;
  border-bottom: 1px solid var(--border2);
  text-align: left;
}
.traffic-table td {
  padding: 0.55rem 0.6rem;
  border-bottom: 1px solid rgba(255,255,255,0.04);
  color: var(--text-muted);
  transition: background 0.15s;
}
.traffic-table tr:last-child td { border-bottom: none; }
.traffic-table tbody tr {
  transition: background 0.18s, transform 0.18s;
}
.traffic-table tbody tr:hover {
  background: var(--purple-lo);
}
.traffic-table tbody tr.row-new {
  animation: rowSlide 0.4s ease both;
}
@keyframes rowSlide {
  from { opacity: 0; transform: translateX(-8px); }
  to   { opacity: 1; transform: translateX(0); }
}
.traffic-table .model-cell {
  font-family: 'Space Grotesk', sans-serif;
  font-size: 0.72rem; font-weight: 500;
  color: var(--cyan);
}
.pill {
  display: inline-flex; align-items: center; gap: 3px;
  padding: 1px 8px; border-radius: 999px;
  font-family: 'Space Grotesk', sans-serif;
  font-size: 0.65rem; font-weight: 600;
}
.pill-ok   { background: rgba(16,185,129,0.12); color: #34D399; border: 1px solid rgba(16,185,129,0.25); }
.pill-fail { background: rgba(239,68,68,0.12);  color: #FC8181; border: 1px solid rgba(239,68,68,0.25); }

/* ── Savings velocity gauge ─────────────────── */
.gauge-card {
  grid-column: span 2;
  background: linear-gradient(135deg, rgba(139,92,246,0.08) 0%, var(--surface2) 100%);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 1.5rem 2rem;
  display: flex; align-items: center; gap: 2rem;
  animation: cardIn 0.5s cubic-bezier(0.4,0,0.2,1) 0.1s both;
}
.gauge-arc-wrap {
  position: relative; width: 120px; height: 70px; flex-shrink: 0;
}
.gauge-arc-wrap svg { overflow: visible; }
.gauge-needle {
  transform-origin: 60px 60px;
  transition: transform 1.2s cubic-bezier(0.34,1.56,0.64,1);
}
.gauge-label {
  position: absolute; bottom: -4px; left: 0; right: 0;
  text-align: center;
  font-family: 'Space Grotesk', sans-serif;
  font-size: 0.6rem; color: var(--text-dim); letter-spacing: 0.05em;
}
.gauge-stats { flex: 1; min-width: 0; }
.gauge-main-val {
  font-family: 'Space Grotesk', sans-serif;
  font-size: 2.4rem; font-weight: 700;
  background: linear-gradient(90deg, var(--purple) 0%, var(--cyan) 100%);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  line-height: 1;
}
.gauge-main-label {
  font-size: 0.72rem; color: var(--text-muted); margin-top: 0.25rem;
}
.gauge-eff {
  display: inline-block;
  margin-top: 0.5rem;
  font-family: 'Space Grotesk', sans-serif;
  font-size: 0.8rem; font-weight: 600;
  padding: 2px 12px; border-radius: 999px;
  background: var(--purple-md); color: var(--purple);
  border: 1px solid var(--border);
}

/* ── Routing flow chart ─────────────────────── */
.flow-wrap {
  display: flex; flex-direction: column; gap: 0.5rem;
  padding-top: 0.25rem;
}
.flow-row {
  display: flex; align-items: center; gap: 0.6rem;
  animation: rowSlide 0.3s ease both;
}
.flow-row:nth-child(1) { animation-delay: 0.05s; }
.flow-row:nth-child(2) { animation-delay: 0.10s; }
.flow-row:nth-child(3) { animation-delay: 0.15s; }
.flow-label {
  font-family: 'Space Grotesk', sans-serif;
  font-size: 0.68rem; font-weight: 500;
  color: var(--text-muted); width: 52px; flex-shrink: 0;
}
.flow-bar-wrap {
  flex: 1; height: 16px;
  background: var(--surface2);
  border-radius: 99px; overflow: hidden;
  position: relative;
}
.flow-bar {
  height: 100%; border-radius: 99px;
  transition: width 1.1s cubic-bezier(0.34,1.2,0.64,1);
  position: relative;
}
.flow-bar::after {
  content: '';
  position: absolute; inset: 0;
  background: linear-gradient(90deg, transparent 60%, rgba(255,255,255,0.15) 100%);
}
.flow-bar-0 { background: linear-gradient(90deg, var(--purple), #A78BFA); }
.flow-bar-1 { background: linear-gradient(90deg, var(--cyan),   #67E8F9); }
.flow-bar-2 { background: linear-gradient(90deg, var(--amber),  #FCD34D); }
.flow-bar-3 { background: linear-gradient(90deg, var(--green),  #6EE7B7); }
.flow-bar-4 { background: linear-gradient(90deg, #F472B6,       #FDA4AF); }
.flow-pct {
  font-family: 'Space Grotesk', sans-serif;
  font-size: 0.65rem; font-weight: 600;
  color: var(--text-dim); width: 32px; text-align: right; flex-shrink: 0;
}

/* ── Quota arc ──────────────────────────────── */
.quota-ring { position: relative; width: 80px; height: 80px; }
.quota-ring svg { transform: rotate(-90deg); }
.quota-ring-text {
  position: absolute; inset: 0;
  display: flex; flex-direction: column;
  align-items: center; justify-content: center;
}
.quota-ring-text .ring-val {
  font-family: 'Space Grotesk', sans-serif;
  font-size: 1rem; font-weight: 700;
  color: var(--text);
}
.quota-ring-text .ring-label {
  font-size: 0.5rem; color: var(--text-dim);
  font-family: 'Space Grotesk', sans-serif;
}
.quota-ring-track { stroke: var(--border2); fill: none; stroke-width: 7; }
.quota-ring-fill  { fill: none; stroke-width: 7; stroke-linecap: round;
                    stroke: var(--purple);
                    transition: stroke-dashoffset 1.4s cubic-bezier(0.34,1.56,0.64,1),
                                stroke 0.5s; }

/* ── Loading skeleton ───────────────────────── */
.skeleton {
  background: linear-gradient(90deg, var(--surface2) 25%, var(--surface) 50%, var(--surface2) 75%);
  background-size: 200% 100%;
  animation: shimmer 1.5s infinite;
  border-radius: 4px;
}
@keyframes shimmer {
  0%   { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}

/* ── Animated counter ───────────────────────── */
.count-anim { transition: opacity 0.3s; }

/* ── Live feed indicator ────────────────────── */
.live-indicator {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: 0.6rem; color: var(--green);
  font-family: 'Space Grotesk', sans-serif; font-weight: 600;
}
.live-dot {
  width: 5px; height: 5px; border-radius: 50%; background: var(--green);
  animation: pulse-dot 1.5s ease-in-out infinite;
}

/* ── Logs panel ─────────────────────────────── */
.logs-list { display: flex; flex-direction: column; gap: 0.4rem; }
.log-row {
  display: flex; align-items: center; gap: 0.75rem;
  padding: 0.65rem 0.9rem;
  background: var(--surface2);
  border: 1px solid var(--border2);
  border-radius: var(--radius-sm);
  font-size: 0.78rem;
  transition: background 0.2s, border-color 0.2s;
  animation: rowSlide 0.35s ease both;
}
.log-row:hover { background: var(--purple-lo); border-color: var(--border); }
.log-time { font-family: 'Space Grotesk', sans-serif; font-size: 0.65rem; color: var(--text-dim); flex-shrink: 0; width: 42px; }
.log-type { font-family: 'Space Grotesk', sans-serif; font-size: 0.65rem; font-weight: 600;
            padding: 1px 6px; border-radius: 4px; flex-shrink: 0; }
.log-type-code     { background: rgba(139,92,246,0.15); color: var(--purple); }
.log-type-query    { background: rgba(34,211,238,0.12); color: var(--cyan); }
.log-type-analyze  { background: rgba(245,158,11,0.12); color: var(--amber); }
.log-type-generate { background: rgba(16,185,129,0.12); color: var(--green); }
.log-type-research { background: rgba(244,114,182,0.12); color: #F472B6; }
.log-model { font-family: 'Space Grotesk', sans-serif; font-size: 0.68rem; color: var(--cyan); }
.log-latency { margin-left: auto; font-size: 0.68rem; color: var(--text-dim); flex-shrink: 0; }
.log-cost    { font-size: 0.65rem; color: var(--text-dim); flex-shrink: 0; }

/* ── Config panel ───────────────────────────── */
.config-grid {
  display: grid; grid-template-columns: 1fr 1fr; gap: 1rem;
  padding: 1.25rem 2rem 2rem;
}
.config-card {
  background: var(--surface); border: 1px solid var(--border2);
  border-radius: var(--radius); padding: 1.25rem;
  animation: cardIn 0.4s cubic-bezier(0.4,0,0.2,1) both;
}
.config-card-title {
  font-family: 'Space Grotesk', sans-serif;
  font-size: 0.75rem; font-weight: 600;
  color: var(--text-muted); margin-bottom: 1rem;
  text-transform: uppercase; letter-spacing: 0.07em;
}
.config-row {
  display: flex; align-items: center; justify-content: space-between;
  padding: 0.5rem 0;
  border-bottom: 1px solid var(--border2);
  font-size: 0.78rem;
}
.config-row:last-child { border-bottom: none; }
.config-key { color: var(--text-muted); }
.config-val { font-family: 'Space Grotesk', sans-serif; font-weight: 500; color: var(--text); }
.config-val.on  { color: var(--green); }
.config-val.off { color: var(--text-dim); }

/* ── Footer ─────────────────────────────────── */
.footer {
  margin-top: auto;
  padding: 0.9rem 2rem;
  border-top: 1px solid var(--border2);
  display: flex; align-items: center; justify-content: space-between;
  font-size: 0.68rem; color: var(--text-dim);
}

/* ── Scrollbar ──────────────────────────────── */
::-webkit-scrollbar { width: 5px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 999px; }

/* ── Responsive ─────────────────────────────── */
@media (max-width: 900px) {
  .sidebar { transform: translateX(-100%); }
  .sidebar.open { transform: translateX(0); }
  .main { margin-left: 0; }
  .charts-grid, .config-grid { grid-template-columns: 1fr; }
  .gauge-card { grid-column: span 1; }
  .stat-grid { grid-template-columns: repeat(2, 1fr); }
}
</style>
</head>
<body>

<!-- ── Sidebar ───────────────────────────── -->
<nav class="sidebar" id="sidebar">
  <div class="sidebar-logo">
    <div class="logo-icon">&#129504;</div>
    <div>
      <div class="logo-text">LLM Router</div>
      <div class="logo-ver">v1.3.0</div>
    </div>
  </div>

  <span class="nav-section-label">Dashboards</span>
  <div class="nav-item active" data-panel="overview" onclick="switchPanel(this)">
    <span class="nav-icon">&#128202;</span> Overview
  </div>
  <div class="nav-item" data-panel="performance" onclick="switchPanel(this)">
    <span class="nav-icon">&#9889;</span> Performance
  </div>
  <div class="nav-item" data-panel="logs" onclick="switchPanel(this)">
    <span class="nav-icon">&#128203;</span> Logs &amp; Analysis
  </div>

  <span class="nav-section-label" style="margin-top:.75rem">System</span>
  <div class="nav-item" data-panel="config" onclick="switchPanel(this)">
    <span class="nav-icon">&#9881;</span> Configuration
  </div>

  <div class="sidebar-footer">
    <div class="sidebar-status">
      <span class="status-dot"></span>
      <span id="sb-status">Connected</span>
    </div>
  </div>
</nav>

<!-- ── Main ──────────────────────────────── -->
<div class="main">

  <!-- Top bar -->
  <div class="topbar">
    <div class="topbar-title" id="panel-title">Overview</div>
    <span class="topbar-badge" id="mode-badge">loading&#8230;</span>
    <div class="topbar-right">
      <span class="topbar-time" id="refresh-ts"></span>
      <button class="refresh-btn" onclick="doRefresh()" id="refresh-btn">
        <span class="btn-icon">&#8635;</span> Refresh
      </button>
    </div>
  </div>

  <!-- ── OVERVIEW panel ─────────────────── -->
  <div class="panel active" id="panel-overview">

    <!-- Savings velocity gauge -->
    <div style="padding: 1.25rem 2rem 0;">
      <div class="gauge-card">
        <div class="gauge-arc-wrap">
          <svg viewBox="0 0 120 70" width="120" height="70">
            <path d="M10,60 A50,50 0 0,1 110,60" fill="none" stroke="rgba(255,255,255,0.06)" stroke-width="8" stroke-linecap="round"/>
            <path d="M10,60 A50,50 0 0,1 110,60" fill="none" stroke="url(#gaugeGrad)" stroke-width="8"
                  stroke-linecap="round" id="gauge-arc"
                  stroke-dasharray="157" stroke-dashoffset="157"
                  style="transition: stroke-dashoffset 1.5s cubic-bezier(0.34,1.2,0.64,1)"/>
            <defs>
              <linearGradient id="gaugeGrad" x1="0" y1="0" x2="1" y2="0">
                <stop offset="0%"   stop-color="#8B5CF6"/>
                <stop offset="100%" stop-color="#22D3EE"/>
              </linearGradient>
            </defs>
          </svg>
          <div class="gauge-label">Efficiency</div>
        </div>
        <div class="gauge-stats">
          <div class="gauge-main-val" id="g-saved">$0.00</div>
          <div class="gauge-main-label">Total saved vs Opus baseline</div>
          <div class="gauge-eff" id="g-eff">0% efficiency</div>
        </div>
        <div style="display:flex;flex-direction:column;gap:0.5rem;margin-left:auto;">
          <div style="text-align:right">
            <div style="font-family:'Space Grotesk',sans-serif;font-size:.65rem;color:var(--text-dim);text-transform:uppercase;letter-spacing:.07em">Today</div>
            <div style="font-family:'Space Grotesk',sans-serif;font-size:1.1rem;font-weight:700;color:var(--text)" id="g-today-cost">$0.0000</div>
          </div>
          <div style="text-align:right">
            <div style="font-family:'Space Grotesk',sans-serif;font-size:.65rem;color:var(--text-dim);text-transform:uppercase;letter-spacing:.07em">Month</div>
            <div style="font-family:'Space Grotesk',sans-serif;font-size:1.1rem;font-weight:700;color:var(--text)" id="g-month-cost">$0.0000</div>
          </div>
        </div>
      </div>
    </div>

    <!-- Stat cards -->
    <div class="stat-grid">
      <div class="stat-card accent-cyan">
        <div class="card-label">Today&#39;s Calls</div>
        <div class="card-value count-anim" id="s-calls">&#8212;</div>
        <div class="card-sub">API + subscription</div>
        <div class="card-icon">&#128222;</div>
      </div>
      <div class="stat-card accent-purple">
        <div class="card-label">Tokens (today)</div>
        <div class="card-value count-anim" id="s-tokens">&#8212;</div>
        <div class="card-sub">input + output</div>
        <div class="card-icon">&#128171;</div>
      </div>
      <div class="stat-card accent-green">
        <div class="card-label">Session Quota</div>
        <div class="card-value" id="s-session-wrap" style="display:flex;align-items:center;gap:.75rem">
          <div class="quota-ring" id="quota-ring-wrap">
            <svg viewBox="0 0 80 80" width="80" height="80">
              <circle cx="40" cy="40" r="31" class="quota-ring-track"/>
              <circle cx="40" cy="40" r="31" class="quota-ring-fill" id="quota-circle"
                      stroke-dasharray="195" stroke-dashoffset="195"/>
            </svg>
            <div class="quota-ring-text">
              <span class="ring-val" id="s-session">0%</span>
              <span class="ring-label">session</span>
            </div>
          </div>
        </div>
        <div class="card-icon">&#9711;</div>
      </div>
      <div class="stat-card accent-amber">
        <div class="card-label">Cache Hits (7d)</div>
        <div class="card-value count-anim" id="s-semhits">&#8212;</div>
        <div class="card-sub">semantic dedup</div>
        <div class="card-icon">&#9889;</div>
      </div>
    </div>

    <!-- Charts -->
    <div class="charts-grid">
      <!-- Cost chart -->
      <div class="chart-card">
        <div class="chart-card-header">
          <span class="chart-card-title">Daily Cost — 14 days</span>
          <span class="chart-card-badge">USD</span>
        </div>
        <canvas id="costChart" height="155"></canvas>
      </div>

      <!-- Model distribution -->
      <div class="chart-card">
        <div class="chart-card-header">
          <span class="chart-card-title">Model Distribution — 7 days</span>
          <span class="chart-card-badge">calls</span>
        </div>
        <canvas id="modelChart" height="155"></canvas>
      </div>

      <!-- Routing flow (task types as horizontal bars) -->
      <div class="chart-card">
        <div class="chart-card-header">
          <span class="chart-card-title">Prompt Intent Routing</span>
          <span class="chart-card-badge">7 days</span>
        </div>
        <div class="flow-wrap" id="flow-wrap">
          <div style="color:var(--text-dim);font-size:.75rem">Loading&#8230;</div>
        </div>
      </div>

      <!-- Recent traffic -->
      <div class="chart-card">
        <div class="chart-card-header">
          <span class="chart-card-title">Recent Routed Traffic</span>
          <span class="live-indicator"><span class="live-dot"></span>LIVE</span>
        </div>
        <div id="recent-table-wrap"></div>
      </div>
    </div>
  </div>

  <!-- ── PERFORMANCE panel ───────────────── -->
  <div class="panel" id="panel-performance">
    <div class="stat-grid" style="padding-top:1.5rem;">
      <div class="stat-card accent-cyan">
        <div class="card-label">Total Calls (month)</div>
        <div class="card-value count-anim" id="p-month-calls">&#8212;</div>
        <div class="card-icon">&#128201;</div>
      </div>
      <div class="stat-card accent-purple">
        <div class="card-label">Month Cost</div>
        <div class="card-value count-anim" id="p-month-cost">&#8212;</div>
        <div class="card-icon">&#128181;</div>
      </div>
      <div class="stat-card accent-green">
        <div class="card-label">Top Model</div>
        <div class="card-value" id="p-top-model" style="font-size:1rem">&#8212;</div>
        <div class="card-icon">&#127942;</div>
      </div>
      <div class="stat-card accent-amber">
        <div class="card-label">Weekly Quota</div>
        <div class="card-value" id="p-weekly">&#8212;</div>
        <div class="card-icon">&#128197;</div>
      </div>
    </div>
    <div class="charts-grid">
      <div class="chart-card" style="grid-column:span 2">
        <div class="chart-card-header">
          <span class="chart-card-title">Cost per Model — 7 days</span>
          <span class="chart-card-badge">USD</span>
        </div>
        <canvas id="modelCostChart" height="120"></canvas>
      </div>
      <div class="chart-card">
        <div class="chart-card-header">
          <span class="chart-card-title">Profile Distribution</span>
          <span class="chart-card-badge">7 days</span>
        </div>
        <canvas id="profileChart" height="155"></canvas>
      </div>
      <div class="chart-card">
        <div class="chart-card-header">
          <span class="chart-card-title">Task Type Breakdown</span>
          <span class="chart-card-badge">7 days</span>
        </div>
        <canvas id="taskChart" height="155"></canvas>
      </div>
    </div>
  </div>

  <!-- ── LOGS panel ──────────────────────── -->
  <div class="panel" id="panel-logs">
    <div style="padding:1.5rem 2rem 2rem">
      <div class="chart-card">
        <div class="chart-card-header">
          <span class="chart-card-title">Recent Routing Decisions</span>
          <span class="live-indicator"><span class="live-dot"></span>LIVE</span>
        </div>
        <div class="logs-list" id="logs-list">
          <div style="color:var(--text-dim);font-size:.8rem">Loading&#8230;</div>
        </div>
      </div>
    </div>
  </div>

  <!-- ── CONFIG panel ────────────────────── -->
  <div class="panel" id="panel-config">
    <div class="config-grid">
      <div class="config-card">
        <div class="config-card-title">Routing Settings</div>
        <div class="config-row">
          <span class="config-key">Profile</span>
          <span class="config-val" id="cfg-profile">&#8212;</span>
        </div>
        <div class="config-row">
          <span class="config-key">Subscription Mode</span>
          <span class="config-val" id="cfg-sub">&#8212;</span>
        </div>
        <div class="config-row">
          <span class="config-key">Monthly Budget</span>
          <span class="config-val" id="cfg-budget">&#8212;</span>
        </div>
        <div class="config-row">
          <span class="config-key">Daily Spend Limit</span>
          <span class="config-val" id="cfg-daily">&#8212;</span>
        </div>
      </div>
      <div class="config-card">
        <div class="config-card-title">Claude Subscription</div>
        <div class="config-row">
          <span class="config-key">Session %</span>
          <span class="config-val" id="cfg-session-pct">&#8212;</span>
        </div>
        <div class="config-row">
          <span class="config-key">Weekly %</span>
          <span class="config-val" id="cfg-weekly-pct">&#8212;</span>
        </div>
        <div class="config-row">
          <span class="config-key">Sonnet %</span>
          <span class="config-val" id="cfg-sonnet-pct">&#8212;</span>
        </div>
        <div class="config-row">
          <span class="config-key">Last Refresh</span>
          <span class="config-val" id="cfg-refresh-time" style="font-size:.7rem">&#8212;</span>
        </div>
      </div>
    </div>
  </div>

  <!-- Footer -->
  <div class="footer">
    <span>LLM Router Dashboard &#183; Auto-refreshes every 30s</span>
    <a href="/api/stats" style="color:var(--text-dim)">raw JSON</a>
  </div>

</div><!-- /main -->

<script>
"use strict";

/* ── Palette ───────────────────────────────── */
const C = {
  purple: "#8B5CF6", cyan: "#22D3EE", amber: "#F59E0B",
  green: "#10B981",  pink: "#F472B6", red: "#EF4444",
  grid: "rgba(255,255,255,0.05)",
  tick: "#475569",
};
const PALETTE = [C.purple,C.cyan,C.amber,C.green,C.pink,"#A78BFA","#67E8F9","#FCD34D"];

/* ── Chart defaults ────────────────────────── */
Chart.defaults.color = C.tick;
Chart.defaults.font.family = "Inter, system-ui, sans-serif";

/* ── Safe text helper ──────────────────────── */
function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

/* ── Panel switching ───────────────────────── */
const PANEL_TITLES = {
  overview: "Overview", performance: "Model Performance",
  logs: "Logs & Analysis", config: "Configuration",
};
let activePanel = "overview";

function switchPanel(navEl) {
  const id = navEl.dataset.panel;
  if (id === activePanel) return;
  activePanel = id;

  document.querySelectorAll(".nav-item").forEach(n => n.classList.remove("active"));
  navEl.classList.add("active");

  document.querySelectorAll(".panel").forEach(p => p.classList.remove("active"));
  const panel = document.getElementById("panel-" + id);
  if (panel) panel.classList.add("active");

  setText("panel-title", PANEL_TITLES[id] || id);
}

/* ── Animated counter ──────────────────────── */
function animateCount(el, target, prefix, suffix, decimals) {
  if (!el) return;
  const start = 0, duration = 800;
  const startTime = performance.now();
  function step(now) {
    const pct = Math.min((now - startTime) / duration, 1);
    const ease = 1 - Math.pow(1 - pct, 3);
    const val = start + (target - start) * ease;
    el.textContent = prefix + val.toFixed(decimals) + suffix;
    if (pct < 1) requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}

/* ── Gauge arc ─────────────────────────────── */
function setGaugeArc(pct) {
  const arc = document.getElementById("gauge-arc");
  if (!arc) return;
  const len = 157;
  arc.style.strokeDashoffset = (len - len * Math.min(pct, 1)).toFixed(1);
}

/* ── Quota ring ────────────────────────────── */
function setQuotaRing(pct) {
  const circ = document.getElementById("quota-circle");
  if (!circ) return;
  const r = 31, circumference = 2 * Math.PI * r;
  circ.style.strokeDasharray  = circumference;
  circ.style.strokeDashoffset = (circumference * (1 - Math.min(pct, 1))).toFixed(2);
  circ.style.stroke = pct >= 0.95 ? C.red : pct >= 0.85 ? C.amber : C.purple;
}

/* ── Routing flow bars ─────────────────────── */
function buildFlowBars(taskTypes) {
  const wrap = document.getElementById("flow-wrap");
  if (!wrap) return;
  if (!taskTypes || taskTypes.length === 0) {
    wrap.textContent = "No data yet.";
    return;
  }
  const total = taskTypes.reduce((s, t) => s + t.calls, 0) || 1;
  wrap.replaceChildren();
  taskTypes.slice(0, 6).forEach((t, i) => {
    const pct = t.calls / total;
    const row = document.createElement("div");
    row.className = "flow-row";
    const lbl = document.createElement("div");
    lbl.className = "flow-label";
    lbl.textContent = (t.task_type || "?").toLowerCase();
    const barWrap = document.createElement("div");
    barWrap.className = "flow-bar-wrap";
    const bar = document.createElement("div");
    bar.className = "flow-bar flow-bar-" + (i % 5);
    bar.style.width = "0%";
    barWrap.appendChild(bar);
    const pctLbl = document.createElement("div");
    pctLbl.className = "flow-pct";
    pctLbl.textContent = Math.round(pct * 100) + "%";
    row.appendChild(lbl); row.appendChild(barWrap); row.appendChild(pctLbl);
    wrap.appendChild(row);
    // animate in
    requestAnimationFrame(() => {
      setTimeout(() => { bar.style.width = (pct * 100).toFixed(1) + "%"; }, 80 + i * 40);
    });
  });
}

/* ── Recent traffic table ──────────────────── */
function buildRecentTable(rows) {
  const wrap = document.getElementById("recent-table-wrap");
  if (!wrap) return;
  if (!rows || rows.length === 0) {
    wrap.textContent = "No recent decisions yet.";
    return;
  }
  const tbl  = document.createElement("table");
  tbl.className = "traffic-table";
  const head = tbl.createTHead();
  const hr   = head.insertRow();
  ["Time","Type","Model","Latency",""].forEach(h => {
    const th = document.createElement("th");
    th.textContent = h;
    hr.appendChild(th);
  });
  const body = tbl.createTBody();
  rows.forEach((r, idx) => {
    const tr = body.insertRow();
    tr.className = "row-new";
    tr.style.animationDelay = (idx * 0.04) + "s";
    const time = r.timestamp ? r.timestamp.slice(11, 16) : "";
    [time, r.task_type || "?"].forEach(val => {
      const td = tr.insertCell();
      td.textContent = val;
    });
    const modelTd = tr.insertCell();
    modelTd.className = "model-cell";
    modelTd.textContent = (r.model || "?").split("/").pop();
    const latTd = tr.insertCell();
    latTd.textContent = r.latency_ms ? Math.round(r.latency_ms) + "ms" : "—";
    const statusTd = tr.insertCell();
    const pill = document.createElement("span");
    pill.className = "pill " + (r.success ? "pill-ok" : "pill-fail");
    pill.textContent = r.success ? "ok" : "fail";
    statusTd.appendChild(pill);
  });
  wrap.replaceChildren(tbl);
}

/* ── Logs list ──────────────────────────────── */
function buildLogs(rows) {
  const list = document.getElementById("logs-list");
  if (!list) return;
  if (!rows || rows.length === 0) { list.textContent = "No log entries yet."; return; }
  list.replaceChildren();
  rows.forEach((r, idx) => {
    const row = document.createElement("div");
    row.className = "log-row";
    row.style.animationDelay = (idx * 0.04) + "s";
    const time = document.createElement("span");
    time.className = "log-time";
    time.textContent = r.timestamp ? r.timestamp.slice(11, 16) : "";
    const type = document.createElement("span");
    type.className = "log-type log-type-" + (r.task_type || "query").toLowerCase();
    type.textContent = (r.task_type || "?").toLowerCase();
    const model = document.createElement("span");
    model.className = "log-model";
    model.textContent = (r.model || "?").split("/").pop();
    const lat = document.createElement("span");
    lat.className = "log-latency";
    lat.textContent = r.latency_ms ? Math.round(r.latency_ms) + "ms" : "—";
    const cost = document.createElement("span");
    cost.className = "log-cost";
    cost.textContent = r.cost_usd ? "$" + Number(r.cost_usd).toFixed(5) : "";
    row.appendChild(time); row.appendChild(type); row.appendChild(model);
    row.appendChild(lat); row.appendChild(cost);
    list.appendChild(row);
  });
}

/* ── Chart instances ───────────────────────── */
let costChart, modelChart, modelCostChart, profileChart, taskChart;
const CHART_OPTS_SHARED = {
  plugins: { legend: { display: false } },
  scales: {
    x: { ticks: { color: C.tick, font: { size: 10 } }, grid: { color: C.grid } },
    y: { ticks: { color: C.tick, font: { size: 10 } }, grid: { color: C.grid }, beginAtZero: true },
  },
};

function initOrUpdateChart(ref, id, cfg) {
  if (!ref) {
    return new Chart(document.getElementById(id), cfg);
  }
  ref.data = cfg.data;
  ref.update("active");
  return ref;
}

/* ── Main refresh ──────────────────────────── */
async function refresh() {
  const btn = document.getElementById("refresh-btn");
  if (btn) btn.classList.add("spinning");
  try {
    const res = await fetch("/api/stats");
    const d   = await res.json();
    applyData(d);
  } catch(e) {
    setText("sb-status", "Error");
  } finally {
    if (btn) btn.classList.remove("spinning");
  }
}

function applyData(d) {
  /* ── top bar ── */
  setText("mode-badge", d.config?.subscription_mode ? "CC Subscription" : "API Mode");
  setText("refresh-ts", "Updated " + new Date().toLocaleTimeString());
  setText("sb-status", "Connected");

  /* ── savings gauge ── */
  const saved = d.savings?.total_saved_usd ?? 0;
  const ext   = d.savings?.total_external_usd ?? 0;
  const eff   = (saved + ext) > 0 ? saved / (saved + ext) : 0;
  setText("g-saved", "$" + saved.toFixed(2));
  setText("g-eff",   Math.round(eff * 100) + "% efficiency");
  setText("g-today-cost", "$" + (d.today.cost_usd || 0).toFixed(4));
  setText("g-month-cost", "$" + (d.month.cost_usd || 0).toFixed(4));
  setGaugeArc(eff);

  /* ── stat cards ── */
  animateCount(document.getElementById("s-calls"),   d.today.calls || 0,  "", "", 0);
  animateCount(document.getElementById("s-tokens"),  d.today.tokens || 0, "", "", 0);
  animateCount(document.getElementById("s-semhits"), d.semantic_cache?.hits ?? 0, "", "", 0);

  /* session quota ring */
  const rawSession = (d.usage || {}).session_pct ?? 0;
  const sp = typeof rawSession === "number" && rawSession <= 1 ? rawSession : rawSession / 100;
  setText("s-session", Math.round(sp * 100) + "%");
  setQuotaRing(sp);

  /* ── performance panel ── */
  animateCount(document.getElementById("p-month-calls"), d.month.calls || 0, "", "", 0);
  animateCount(document.getElementById("p-month-cost"),  d.month.cost_usd || 0, "$", "", 4);
  const topModel = (d.models ?? [])[0]?.model ?? "—";
  setText("p-top-model", topModel.split("/").pop());
  const rawWeekly = (d.usage || {}).weekly_pct ?? 0;
  const wp = typeof rawWeekly === "number" && rawWeekly <= 1 ? rawWeekly : rawWeekly / 100;
  setText("p-weekly", Math.round(wp * 100) + "%");

  /* ── config panel ── */
  const cfg = d.config || {};
  setText("cfg-profile", cfg.profile || "—");
  const subEl = document.getElementById("cfg-sub");
  if (subEl) {
    subEl.textContent = cfg.subscription_mode ? "Enabled" : "Disabled";
    subEl.className = "config-val " + (cfg.subscription_mode ? "on" : "off");
  }
  setText("cfg-budget", cfg.monthly_budget > 0 ? "$" + cfg.monthly_budget.toFixed(2) : "Unlimited");
  setText("cfg-daily",  cfg.daily_limit   > 0 ? "$" + cfg.daily_limit.toFixed(2)   : "Unlimited");
  const u = d.usage || {};
  setText("cfg-session-pct", Math.round(sp * 100) + "%");
  setText("cfg-weekly-pct",  Math.round(wp * 100) + "%");
  const sonnetRaw = u.sonnet_pct ?? 0;
  const sonnetPct = typeof sonnetRaw === "number" && sonnetRaw <= 1 ? sonnetRaw : sonnetRaw / 100;
  setText("cfg-sonnet-pct", Math.round(sonnetPct * 100) + "%");
  setText("cfg-refresh-time", u.last_updated || "—");

  /* ── Cost bar chart ── */
  const dc = d.daily_cost ?? [];
  costChart = initOrUpdateChart(costChart, "costChart", {
    type: "bar",
    data: {
      labels: dc.map(r => r.day.slice(5)),
      datasets: [{
        label: "USD", data: dc.map(r => r.cost_usd),
        backgroundColor: "rgba(139,92,246,0.35)",
        borderColor: C.purple, borderWidth: 1,
        borderRadius: 5, borderSkipped: false,
      }]
    },
    options: {
      ...CHART_OPTS_SHARED,
      animation: { duration: 800 },
    }
  });

  /* ── Model doughnut ── */
  const models = d.models ?? [];
  modelChart = initOrUpdateChart(modelChart, "modelChart", {
    type: "doughnut",
    data: {
      labels: models.map(m => m.model.split("/").pop()),
      datasets: [{ data: models.map(m => m.calls), backgroundColor: PALETTE,
                   borderWidth: 0, hoverOffset: 6 }]
    },
    options: {
      plugins: { legend: { position: "right",
        labels: { color: C.tick, font: { size: 10 }, boxWidth: 10, padding: 8 }
      }},
      animation: { animateRotate: true, duration: 900 },
      cutout: "68%",
    }
  });

  /* ── Model cost bar ── */
  modelCostChart = initOrUpdateChart(modelCostChart, "modelCostChart", {
    type: "bar",
    data: {
      labels: models.map(m => m.model.split("/").pop()),
      datasets: [{
        label: "Cost USD", data: models.map(m => m.cost_usd),
        backgroundColor: PALETTE.map(c => c + "55"),
        borderColor: PALETTE, borderWidth: 1.5, borderRadius: 4,
      }]
    },
    options: { ...CHART_OPTS_SHARED, animation: { duration: 700 } },
  });

  /* ── Profile doughnut ── */
  const profiles = d.profiles ?? [];
  profileChart = initOrUpdateChart(profileChart, "profileChart", {
    type: "doughnut",
    data: {
      labels: profiles.map(p => p.profile),
      datasets: [{ data: profiles.map(p => p.calls), backgroundColor: PALETTE.slice(3),
                   borderWidth: 0, hoverOffset: 6 }]
    },
    options: {
      plugins: { legend: { position: "right",
        labels: { color: C.tick, font: { size: 10 }, boxWidth: 10, padding: 8 }
      }},
      animation: { animateRotate: true, duration: 900 },
      cutout: "60%",
    }
  });

  /* ── Task type pie ── */
  const tasks = d.task_types ?? [];
  taskChart = initOrUpdateChart(taskChart, "taskChart", {
    type: "pie",
    data: {
      labels: tasks.map(t => t.task_type),
      datasets: [{ data: tasks.map(t => t.calls), backgroundColor: PALETTE.slice(1),
                   borderWidth: 0, hoverOffset: 6 }]
    },
    options: {
      plugins: { legend: { position: "right",
        labels: { color: C.tick, font: { size: 10 }, boxWidth: 10, padding: 8 }
      }},
      animation: { animateRotate: true, duration: 900 },
    }
  });

  /* ── Routing flow bars ── */
  buildFlowBars(tasks);

  /* ── Tables & logs ── */
  buildRecentTable(d.recent ?? []);
  buildLogs(d.recent ?? []);
}

function doRefresh() { refresh(); }

refresh();
setInterval(refresh, 30_000);
</script>
</body>
</html>"""


async def run(port: int = DEFAULT_PORT) -> None:
    """Start the dashboard HTTP server.

    Args:
        port: TCP port to listen on (default 7337).
    """
    try:
        from aiohttp import web
    except ImportError:
        print(
            "aiohttp is required for the dashboard.\n"
            "Install it: pip install aiohttp\n"
            "Or: uv add aiohttp"
        )
        return

    async def handle_index(request: "web.Request") -> "web.Response":
        return web.Response(text=_html(), content_type="text/html")

    async def handle_stats(request: "web.Request") -> "web.Response":
        stats = await _get_stats()
        return web.Response(
            text=json.dumps(stats, default=str),
            content_type="application/json",
        )

    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/stats", handle_stats)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", port)
    await site.start()

    print(f"\n✓ LLM Router Dashboard running at http://localhost:{port}\n")
    print("  Press Ctrl+C to stop.\n")

    try:
        import asyncio
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await runner.cleanup()
