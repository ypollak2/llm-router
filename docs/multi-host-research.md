# Phase 0 Research — Multi-Host + Persistent Savings (v3.1.0)

## Cross-Session Savings — Status

### What already works (persistent today)
- `routing_decisions` table: filled by `log_routing_decision()` after every `llm_route` call.  
  This is what the stop/status-bar hooks already query for "this week 1614 calls" — it IS cross-session.
- `usage` table: filled by `log_usage()` after every external API call, with `saved_usd` / `baseline_model` / `potential_cost_usd` columns (added in v2.1 migrations).  
  `get_savings_by_period()` queries this table → `llm_savings` already shows cross-session savings from external API calls.

### What is NOT working (savings_stats gap)
- `savings_stats` table exists in schema but is **never populated**.
- `import_savings_log()` in `cost.py` (line 977) reads `~/.llm-router/savings_log.jsonl` and imports into `savings_stats`. It is correct and complete — but **never called from anywhere**.
- `usage-refresh.py` hook writes JSONL correctly with timestamp, task_type, session_id, estimated_saved.

### Fix: one line
In `tools/admin.py::llm_savings()`, call `await import_savings_log()` before `get_savings_by_period()`.
Also call it in `llm_usage()` for completeness.

### Impact on plan
Phase 1 "core refactor" is **not needed** for cross-session savings.  
Revised Phase 1 scope: wire the import + add `host` column to `savings_stats` for multi-host tracking.

---

## Hook/Classifier Overlap (auto-route.py vs classifier.py)

- `auto-route.py` (hook, 49 KB): standalone script, no package imports, uses inline regex heuristics + Ollama + Gemini Flash API. Outputs `hookSpecificOutput` JSON to stdout.
- `classifier.py` (MCP, 10.5 KB): async, uses `providers.call_llm()`, returns `ClassificationResult`. Used by `llm_route`, `llm_classify`, `llm_auto`, etc.

**Overlap**: Both implement the same 3-layer chain (heuristic → Ollama → cheap API). The hook can't import from the package (it's a standalone script executed by Claude Code), so duplication is intentional and necessary. No refactor needed.

**Hook-only logic**: `.env` loading, stdout routing-hint formatting, `SKIP_TOOLS` list, `LLM_ROUTER_ENFORCE` flag. These must stay in the hook.

---

## Codex CLI Hook API

- **No Codex plugin or hooks found** locally (`~/.claude/plugins/codex*` — not found, `codex` not in PATH).
- Research conclusion: Codex CLI does not expose a `UserPromptSubmit`-equivalent hook API (it's a stateless CLI, not a persistent IDE agent). Confirmed: Codex fallback = Desktop-tier (capability extension via llm_auto + rules file). Acceptable per user decision.

---

## Claude Desktop Config

- `~/Library/Application Support/Claude/claude_desktop_config.json` — **not present** on this machine (Desktop not installed).
- Standard format (from docs): 
```json
{
  "mcpServers": {
    "llm-router": {
      "command": "uvx",
      "args": ["claude-code-llm-router"],
      "env": { "LLM_ROUTER_PROFILE": "balanced" }
    }
  }
}
```
- Platform locations: macOS `~/Library/Application Support/Claude/`, Linux `~/.config/Claude/`, Windows `%APPDATA%\Claude\`

## VS Code / Copilot Config

Standard `.vscode/mcp.json` format:
```json
{
  "servers": {
    "llm-router": {
      "command": "uvx",
      "args": ["claude-code-llm-router"]
    }
  }
}
```

---

## FastMCP String Return

`llm_route` returns a multi-line formatted string (not JSON). FastMCP tools return strings.  
For `llm_auto`, we will return the result as a formatted string with routing metadata appended — no JSON envelope needed. The metadata lines can use a consistent prefix (`> **Routed via ...**`) that's easy to parse if needed.

---

## Summary: Revised Plan Scope

| Original plan | Revised |
|---|---|
| Phase 1: Core refactor (extract classification into `src/llm_router/classification/`) | ❌ Not needed — existing structure is already clean |
| Phase 1: Persistent savings — wire JSONL import | ✅ One-liner: call `import_savings_log()` in `llm_savings()` and `llm_usage()` |
| Phase 1: Add `host` column to savings_stats | ✅ Simple migration |
| Phase 2: `llm_auto` sibling tool | ✅ Add to `tools/routing.py` |
| Phase 2: `llm_savings` tool | ✅ Already exists — just needs the import wire-up |
| Phase 3: Host adapters + install CLI | ✅ New install subcommands + rules files |
| Phase 4: Docs | ✅ |
| Phase 5: Tests | ✅ Narrower scope |
| Phase 6: Release v3.1.0 | ✅ (was v2.0.0, corrected to v3.1.0) |

