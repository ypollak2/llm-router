# LLM Router — Claude Desktop Integration

This document explains how to use llm-router MCP tools within Claude Desktop.

## Configuration for Claude Desktop

Add this to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "llm-router": {
      "command": "uvx",
      "args": ["claude-code-llm-router"]
    }
  }
}
```

File location:
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

## Available MCP Tools

Once configured, you'll have access to:

### Smart Routing
- `llm_auto` - Auto-route with savings tracking (recommended for all tasks)
- `llm_route` - Full complexity classification and routing
- `llm_classify` - Quick complexity detection

### Text Tools
- `llm_query` - Questions, lookups, Q&A
- `llm_research` - Web-grounded research
- `llm_generate` - Writing, content creation
- `llm_analyze` - Deep analysis and reasoning
- `llm_code` - Code generation and refactoring

### Usage & Admin
- `llm_usage` - Check savings and usage
- `llm_health` - Provider status
- `llm_budget` - Spending caps and pressure

## Recommended Usage

Start with `llm_auto` for all tasks — it handles cost optimization automatically and tracks your savings across sessions.
