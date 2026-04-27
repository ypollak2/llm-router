# LLM Router — Codex CLI Integration

This document explains how to use llm-router MCP tools within the Codex CLI agent.

## Available MCP Tools

When Codex CLI has the llm-router plugin registered, the following MCP tools are available:

### Text & Analysis
- `llm_query` - Simple questions, lookups, quick facts
- `llm_research` - Web-grounded research, current events
- `llm_generate` - Writing, content creation, brainstorming
- `llm_analyze` - Deep analysis, code review, debugging
- `llm_code` - Code generation, refactoring, algorithm design
- `llm_edit` - Bulk code edits across multiple files

### Media
- `llm_image` - Image generation
- `llm_video` - Video generation
- `llm_audio` - Text-to-speech

### Administration
- `llm_route` - Classify task complexity and route to best model
- `llm_classify` - Quick complexity classification
- `llm_usage` - Check API usage and savings
- `llm_providers` - List configured providers and status
- `llm_health` - Check provider health and circuit breaker status

## Usage Examples

```
# Simple question routing to Ollama or Haiku
llm_query(prompt="What's the difference between async/await and promises?")

# Web research routing to Perplexity
llm_research(prompt="Latest developments in quantum computing in 2026")

# Code generation with complexity detection
llm_code(prompt="Generate a React component for a todo list with hooks")

# Content generation
llm_generate(prompt="Write a 500-word article about AI safety")

# Deep code analysis
llm_analyze(prompt="Analyze this function for performance bottlenecks", complexity="complex")
```

## Token-Efficient Response Style

When using Codex CLI with llm-router, prefer output that:
- **Leads with the answer** — no preamble like "I'll help", "Let me think", "Sure"
- **Uses fragments** — "Returns mutated object" not "This returns a mutated object"
- **Skips filler** — drop articles (a/the) when meaning is clear
- **Preserves code** — all technical detail stays, only prose is condensed

Example: Instead of "I think you should use the useCallback hook because it prevents unnecessary re-renders of child components", output: "useCallback. Prevents child re-renders."

## Cost Optimization Tips

- `llm_query` is cheapest for simple lookups
- `llm_research` includes web access (use when you need current data)
- `llm_auto` is the recommended tool — it automatically routes through the full cost-saving chain
- Batch related tasks together to reduce overhead
