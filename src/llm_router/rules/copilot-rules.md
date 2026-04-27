# LLM Router — VS Code / GitHub Copilot Integration

This document explains how to use llm-router MCP tools with VS Code and GitHub Copilot.

## Configuration for VS Code / Copilot

Add this to your VS Code `mcp.json`:

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

File location:
- **macOS**: `~/Library/Application Support/Code/User/mcp.json`
- **Windows**: `%APPDATA%\Code\User\mcp.json`
- **Linux**: `~/.config/Code/User/mcp.json`

## Available Tools

### Smart Routing
- `llm_route` - Classify and route to optimal model
- `llm_classify` - Quick task complexity classification
- `llm_auto` - Automatic routing with cost tracking

### Text Operations
- `llm_query` - Simple questions and Q&A
- `llm_research` - Web-grounded research with sources
- `llm_generate` - Content creation and writing
- `llm_analyze` - Complex analysis and debugging
- `llm_code` - Code generation and improvements

### Utilities
- `llm_usage` - View routing stats and savings
- `llm_health` - Check provider availability

## Quick Start

Use `llm_route(prompt="your task")` to automatically classify and route any task to the optimal model for cost savings.
