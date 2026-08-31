"""LLM Router TUI — Modern Terminal User Interface for v0.3.3+.

Modern dashboard for real-time streaming visualization with:
  - Live output streaming panel with syntax highlighting
  - Route progress timeline with stage indicators
  - Real-time metrics (tokens, cost, latency, throughput)
  - Interactive session replay and cost analysis
  - Full keyboard navigation

Framework: Textual (TUI framework) + Rich (formatting) + Plotext (charts)
"""

from __future__ import annotations

__version__ = "13.0.8"
__all__ = [
    "LLMRouterDashboard",
    "run_dashboard",
]

from llm_router.tui.app import LLMRouterDashboard, run_dashboard
