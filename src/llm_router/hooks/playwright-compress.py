#!/usr/bin/env python3
# llm-router-hook-version: 1
"""PostToolUse hook — compress Playwright page snapshots via cheap LLM.

After every browser_snapshot call:
  1. Skip if snapshot < SNAPSHOT_LINE_THRESHOLD lines (not worth the cost)
  2. Try Ollama (free, local) → Gemini Flash (cheap) → rule-based fallback
  3. Inject compressed summary as contextForAgent so Claude can use refs
     directly without needing depth-escalation (depth:3→6→8) re-snapshots

Token savings: 60-80% reduction in Playwright-heavy sessions by eliminating
the re-snapshot and retry-storm patterns.

Env vars:
  OLLAMA_HOST                     Ollama base URL (default: http://localhost:11434)
  LLM_ROUTER_OLLAMA_MODEL         Model for compression (default: qwen2.5:7b)
  GEMINI_API_KEY                  Enables Gemini Flash fallback
  LLM_ROUTER_PLAYWRIGHT_COMPRESS  Set to "off" to disable this hook
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

# ── Configuration ─────────────────────────────────────────────────────────────
SNAPSHOT_LINE_THRESHOLD = 40       # Skip compression for small snapshots
OLLAMA_BASE = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("LLM_ROUTER_OLLAMA_MODEL", "qwen2.5:7b")
OLLAMA_TIMEOUT = 8                 # seconds — fast enough for local models
MAX_SNAPSHOT_CHARS = 10_000        # Truncate before sending to LLM

_COMPRESS_PROMPT = """\
You are a DOM summarizer for a Playwright automation agent.

Given the Playwright page snapshot below, extract ONLY:

REFS:
  ref:ID "short description" (element type)  — one per line
STATE: one-sentence summary of current page state
ERRORS: any visible error messages, or "none"

Rules:
- Include ALL interactive elements (buttons, inputs, links, selects, checkboxes, textareas)
- Keep descriptions to 3–5 words
- Output ONLY the three sections — no preamble, no markdown

Snapshot:
{snapshot}"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _bare_tool_name(full_name: str) -> str:
    """Strip MCP server prefix: mcp__plugin_playwright__browser_snapshot → browser_snapshot."""
    return full_name.split("__")[-1] if "__" in full_name else full_name


def _extract_snapshot(payload: dict) -> str | None:
    """Pull text content from a browser_snapshot tool result."""
    result = payload.get("toolResult", {})
    if isinstance(result, str):
        return result or None
    if isinstance(result, dict):
        if "text" in result:
            return result["text"] or None
        content = result.get("content", [])
        if isinstance(content, list):
            parts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            return "\n".join(parts) or None
    return None


# ── Compression strategies (free-first) ──────────────────────────────────────

def _try_ollama(snapshot: str) -> str | None:
    """Compress via Ollama. Returns summary or None on any failure."""
    prompt = _COMPRESS_PROMPT.format(snapshot=snapshot[:MAX_SNAPSHOT_CHARS])
    body = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0, "num_predict": 400},
    }).encode()
    try:
        req = urllib.request.Request(
            f"{OLLAMA_BASE}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
            data = json.loads(resp.read())
            text = data.get("response", "").strip()
            return text or None
    except Exception:
        return None


def _try_gemini(snapshot: str) -> str | None:
    """Compress via Gemini Flash. Returns summary or None if key absent or error."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return None
    prompt = _COMPRESS_PROMPT.format(snapshot=snapshot[:MAX_SNAPSHOT_CHARS])
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 400},
    }).encode()
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-1.5-flash:generateContent?key={api_key}"
    )
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            candidates = data.get("candidates", [])
            if not candidates:
                return None
            parts = candidates[0].get("content", {}).get("parts", [])
            text = " ".join(p.get("text", "") for p in parts).strip()
            return text or None
    except Exception:
        return None


def _rule_based(snapshot: str) -> str:
    """Instant rule-based fallback: regex-extract refs and state from the raw snapshot."""
    lines = snapshot.split("\n")
    refs: list[str] = []
    errors: list[str] = []
    state_hints: list[str] = []

    _interactive_kw = {"button", "input", "link", "select", "checkbox", "textarea", "tab", "text box"}
    _error_kw = {"error", "failed", "timeout", "exception", "not found", "unavailable"}
    _state_kw = {"loading", "running", "complete", "ready", "success", "processing", "explained", "idle"}

    for line in lines:
        low = line.lower()
        stripped = line.strip()
        if "ref:" in low and any(kw in low for kw in _interactive_kw):
            refs.append(stripped[:120])
        elif any(kw in low for kw in _error_kw) and stripped:
            errors.append(stripped[:80])
        elif any(kw in low for kw in _state_kw) and stripped:
            state_hints.append(stripped[:80])

    refs_section = "\n".join(f"  {r}" for r in refs[:20]) if refs else "  (none extracted — snapshot may be non-interactive)"
    error_line = errors[0][:80] if errors else "none"
    state_line = state_hints[0][:80] if state_hints else "page loaded"

    return f"REFS:\n{refs_section}\nSTATE: {state_line}\nERRORS: {error_line}"


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # Allow opt-out via env var
    if os.environ.get("LLM_ROUTER_PLAYWRIGHT_COMPRESS", "").lower() == "off":
        sys.exit(0)

    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, OSError):
        sys.exit(0)

    # Only fire for browser_snapshot
    tool_name = payload.get("toolName", "")
    if _bare_tool_name(tool_name) != "browser_snapshot":
        sys.exit(0)

    snapshot = _extract_snapshot(payload)
    if not snapshot:
        sys.exit(0)

    original_lines = snapshot.count("\n") + 1
    if original_lines < SNAPSHOT_LINE_THRESHOLD:
        sys.exit(0)

    # Free-first compression chain
    compressed = _try_ollama(snapshot) or _try_gemini(snapshot) or _rule_based(snapshot)

    compressed_lines = compressed.count("\n") + 1
    context = (
        f"[LLM-Router] DOM snapshot compressed {original_lines}→{compressed_lines} lines:\n"
        f"{compressed}\n"
        "⚡ Use the refs above directly — avoid re-snapshot unless page state changes."
    )

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "contextForAgent": context,
            }
        },
        sys.stdout,
    )


if __name__ == "__main__":
    main()
