"""Opt-in per-call routing proxy for Claude Code's own ``/v1/messages`` calls.

Off unless a user starts it (``llm-router proxy``) and points ONE session at it
with ``ANTHROPIC_BASE_URL=http://127.0.0.1:<port>``. Nothing installs it, and
nothing changes settings or hooks to enable it.

Every call is passed through to Anthropic unchanged, except the step classes
the router's policy allows (``steps.py``). Those are served by a non-Claude,
tool-capable model and answered in Anthropic format. A served reply that fails
validation, errors, or misses the per-step latency budget falls back to
Anthropic, and the fallback is recorded (``ledger.py``).

Modules:
  steps      which calls are eligible (step classes) and what text to classify
  translate  Anthropic <-> Ollama request/response translation, validation, SSE
  backends   pluggable serving backends and request trims
  ledger     per-call rows (never headers, never content) and ``stats()``
  server     the Starlette app, pass-through, fallback, and ``main()``

Design record: ``docs/spikes/per-call-proxy-2026-09-28.md`` and
``docs/proxy.md``.
"""
