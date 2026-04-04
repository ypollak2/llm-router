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
    """
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LLM Router Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #0f1117; color: #e2e8f0; min-height: 100vh; }
  header { padding: 1.25rem 2rem; background: #1a1f2e; border-bottom: 1px solid #2d3748;
           display: flex; align-items: center; gap: 1rem; }
  header h1 { font-size: 1.25rem; font-weight: 600; }
  .badge { font-size: 0.7rem; background: #2b6cb0; padding: 2px 8px;
           border-radius: 999px; color: #bee3f8; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
          gap: 1rem; padding: 1.5rem 2rem; }
  .card { background: #1a1f2e; border-radius: 10px; padding: 1.25rem;
          border: 1px solid #2d3748; }
  .card h3 { font-size: 0.75rem; color: #718096; text-transform: uppercase;
             letter-spacing: .05em; margin-bottom: .5rem; }
  .card .val { font-size: 1.75rem; font-weight: 700; color: #63b3ed; }
  .card .sub { font-size: 0.8rem; color: #718096; margin-top: .25rem; }
  .charts { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem;
            padding: 0 2rem 1.5rem; }
  .chart-card { background: #1a1f2e; border-radius: 10px; padding: 1.25rem;
                border: 1px solid #2d3748; }
  .chart-card h3 { font-size: 0.8rem; color: #a0aec0; margin-bottom: 1rem; }
  table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
  th { color: #718096; font-weight: 500; padding: .5rem .75rem; text-align: left;
       border-bottom: 1px solid #2d3748; }
  td { padding: .5rem .75rem; border-bottom: 1px solid #1e2533; color: #cbd5e0; }
  tr:last-child td { border-bottom: none; }
  .pill { display: inline-block; padding: 1px 8px; border-radius: 999px;
          font-size: 0.7rem; font-weight: 600; }
  .pill-ok   { background: #27674922; color: #9ae6b4; border: 1px solid #27674944; }
  .pill-fail { background: #c0562122; color: #feb2b2; border: 1px solid #c0562144; }
  footer { text-align: center; padding: 1.5rem; color: #4a5568; font-size: 0.75rem; }
  @media (max-width: 768px) { .charts { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<header>
  <span style="font-size:1.5rem">&#129504;</span>
  <h1>LLM Router</h1>
  <span class="badge" id="mode-badge">loading&#8230;</span>
  <span style="margin-left:auto;font-size:.75rem;color:#718096" id="refresh-ts"></span>
</header>

<div class="grid">
  <div class="card"><h3>Today&#39;s calls</h3><div class="val" id="s-calls">&#8212;</div></div>
  <div class="card"><h3>Today&#39;s cost</h3><div class="val" id="s-cost">&#8212;</div></div>
  <div class="card"><h3>Month cost</h3><div class="val" id="s-month">&#8212;</div></div>
  <div class="card">
    <h3>Total saved</h3>
    <div class="val" id="s-saved">&#8212;</div>
    <div class="sub" id="s-saved-sub"></div>
  </div>
  <div class="card"><h3>Session quota</h3><div class="val" id="s-session">&#8212;</div></div>
  <div class="card"><h3>Cache hits (7d)</h3><div class="val" id="s-semhits">&#8212;</div></div>
</div>

<div class="charts">
  <div class="chart-card">
    <h3>Daily cost (14 days)</h3>
    <canvas id="costChart" height="180"></canvas>
  </div>
  <div class="chart-card">
    <h3>Model distribution (7 days)</h3>
    <canvas id="modelChart" height="180"></canvas>
  </div>
  <div class="chart-card">
    <h3>Task types (7 days)</h3>
    <canvas id="taskChart" height="180"></canvas>
  </div>
  <div class="chart-card">
    <h3>Recent routing decisions</h3>
    <div id="recent-table-wrap"></div>
  </div>
</div>

<footer>Auto-refreshes every 30&#160;s &#183;
  <a href="/api/stats" style="color:#4a5568">raw JSON</a>
</footer>

<script>
"use strict";
const COLORS = ["#63b3ed","#68d391","#f6ad55","#fc8181","#d6bcfa","#81e6d9","#fbd38d","#b794f4"];

/* Safe text helper — never write untrusted values via innerHTML */
function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

let costChart, modelChart, taskChart;

function buildRecentTable(rows) {
  const wrap = document.getElementById("recent-table-wrap");
  if (!rows || rows.length === 0) {
    setText("recent-table-wrap", "No recent decisions yet.");
    return;
  }

  const tbl  = document.createElement("table");
  const head = tbl.createTHead();
  const hr   = head.insertRow();
  ["Time","Type","Model","Latency",""].forEach(h => {
    const th = document.createElement("th");
    th.textContent = h;
    hr.appendChild(th);
  });

  const body = tbl.createTBody();
  rows.forEach(r => {
    const tr = body.insertRow();

    const time = r.timestamp ? r.timestamp.slice(11,16) : "";
    [
      time,
      r.task_type || "?",
      (r.model || "?").split("/").pop(),
      r.latency_ms ? Math.round(r.latency_ms) + "ms" : "—",
    ].forEach(val => {
      const td = tr.insertCell();
      td.textContent = val;
    });

    const statusTd = tr.insertCell();
    const pill = document.createElement("span");
    pill.className = "pill " + (r.success ? "pill-ok" : "pill-fail");
    pill.textContent = r.success ? "ok" : "fail";
    statusTd.appendChild(pill);
  });

  wrap.replaceChildren(tbl);
}

async function refresh() {
  const res  = await fetch("/api/stats");
  const d    = await res.json();

  setText("mode-badge", d.config?.subscription_mode ? "CC Subscription" : "API Mode");
  setText("refresh-ts", "Updated " + new Date().toLocaleTimeString());

  setText("s-calls",   (d.today.calls || 0).toLocaleString());
  setText("s-cost",    "$" + (d.today.cost_usd || 0).toFixed(4));
  setText("s-month",   "$" + (d.month.cost_usd || 0).toFixed(4));

  const saved = d.savings?.total_saved_usd ?? 0;
  const ext   = d.savings?.total_external_usd ?? 0;
  setText("s-saved",     "$" + saved.toFixed(2));
  setText("s-saved-sub", ext > 0 ? (saved/(saved+ext)*100).toFixed(0)+"% vs Opus baseline" : "");

  const sp = Math.round(((d.usage || {}).session_pct ?? 0) * 100);
  setText("s-session",  sp + "%");
  setText("s-semhits",  (d.semantic_cache?.hits ?? 0).toLocaleString());

  /* Daily cost bar chart */
  const dc = d.daily_cost ?? [];
  const costLabels = dc.map(r => r.day.slice(5));
  const costData   = dc.map(r => r.cost_usd);
  if (!costChart) {
    costChart = new Chart(document.getElementById("costChart"), {
      type: "bar",
      data: {
        labels: costLabels,
        datasets: [{ label: "USD", data: costData,
          backgroundColor: "#63b3ed88", borderColor: "#63b3ed",
          borderWidth: 1, borderRadius: 4 }]
      },
      options: {
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: "#718096", font: { size: 10 } }, grid: { color: "#1e2533" } },
          y: { ticks: { color: "#718096", font: { size: 10 } }, grid: { color: "#1e2533" } }
        }
      }
    });
  } else {
    costChart.data.labels = costLabels;
    costChart.data.datasets[0].data = costData;
    costChart.update();
  }

  /* Model doughnut */
  const models = d.models ?? [];
  const modelLabels = models.map(m => m.model.split("/").pop());
  const modelData   = models.map(m => m.calls);
  if (!modelChart) {
    modelChart = new Chart(document.getElementById("modelChart"), {
      type: "doughnut",
      data: { labels: modelLabels, datasets: [{ data: modelData, backgroundColor: COLORS }] },
      options: { plugins: { legend: { position: "right",
        labels: { color: "#a0aec0", font: { size: 10 }, boxWidth: 12 } } } }
    });
  } else {
    modelChart.data.labels = modelLabels;
    modelChart.data.datasets[0].data = modelData;
    modelChart.update();
  }

  /* Task type pie */
  const tasks = d.task_types ?? [];
  const taskLabels = tasks.map(t => t.task_type);
  const taskData   = tasks.map(t => t.calls);
  if (!taskChart) {
    taskChart = new Chart(document.getElementById("taskChart"), {
      type: "pie",
      data: { labels: taskLabels, datasets: [{ data: taskData, backgroundColor: COLORS.slice(2) }] },
      options: { plugins: { legend: { position: "right",
        labels: { color: "#a0aec0", font: { size: 10 }, boxWidth: 12 } } } }
    });
  } else {
    taskChart.data.labels = taskLabels;
    taskChart.data.datasets[0].data = taskData;
    taskChart.update();
  }

  buildRecentTable(d.recent ?? []);
}

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
