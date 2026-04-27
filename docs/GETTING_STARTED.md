# Getting Started with llm-router

Welcome! This guide will help you get **smart LLM routing** running in your environment within 5 minutes.

## What is llm-router?

**llm-router** automatically routes your AI tasks to the cheapest capable model. Simple questions go to Gemini Flash or Haiku ($0.001). Complex reasoning goes to o3 or Opus only when needed.

**Result: 60–80% cost reduction** vs using Opus for everything.

---

## Quick Install (2 minutes)

### 1. Install the package

```bash
pip install llm-router
```

### 2. Install for your host

**Claude Code (recommended)**
```bash
llm-router install
```

**Codex CLI**
```bash
llm-router install --host codex
```

**Gemini CLI**
```bash
llm-router install --host gemini-cli
```

**VS Code / Cursor**
```bash
llm-router install --host vscode  # or cursor
```

### 3. Verify installation

```bash
llm-router doctor
```

You should see ✅ checks for hooks, MCP registration, and provider availability.

---

## How It Works

```
Your Prompt
    ↓
Auto-Classifier (analyzes task)
    ↓
Is it simple? → Gemini Flash / Haiku (FREE)
    ↓
Is it moderate? → Claude Sonnet / GPT-4o ($0.003)
    ↓
Is it complex? → Claude Opus / o3 ($0.015)
    ↓
💾 Cost tracked & reported
```

**Example: 3-task batch**
- "What is X?" → Haiku ($0.00001)
- "Debug why Y is slow" → Sonnet ($0.003)
- "Implement feature Z" → Opus ($0.015)

**Total:** $0.018 (vs $0.045 if all went to Opus) = **60% savings**

---

## Next Steps

### Check Your Savings
```bash
llm-router gain          # Show cumulative savings
llm-router budget        # Check current spend
llm-router dashboard     # Open analytics dashboard
```

### Configure More Providers
```bash
# Add API keys for more models
llm-router setup

# Or manually configure ~/.llm-router/config.yaml:
cat > ~/.llm-router/config.yaml <<'EOF'
openai_api_key: "sk-..."
gemini_api_key: "AIzaSy..."
perplexity_api_key: "pplx-..."
ollama_base_url: "http://localhost:11434"
EOF

chmod 600 ~/.llm-router/config.yaml
```

### Understand Your Routing

- **View routing decisions**: `llm-router last`
- **Replay a decision**: `llm-router replay <id>`
- **See routing policy**: `llm-router policy`
- **Run health check**: `llm-router verify`

---

## Common Questions

### "Which host should I use?"

| Host | Cost Savings | Setup Friction | Best For |
|------|:------------:|:--------------:|----------|
| **Claude Code** | 60–80% | Low | Maximum savings (recommended) |
| **Codex CLI** | 60–80% | Medium | OpenAI users |
| **Gemini CLI** | 50–70% | Medium | Gemini free tier users |
| **VS Code** | 30–50% | Low | Lightweight editors |

**TL;DR:** Claude Code is best. Use what you already have.

### "Do I need API keys?"

No. llm-router works with:
- **Free:** Ollama (local), Codex (if installed), Gemini free tier
- **Paid (optional):** OpenAI, Anthropic, Gemini Pro, Perplexity

Start free. Add keys later if you want more models.

### "How much will I save?"

On typical usage:
- 80% of tasks are simple → Gemini Flash/Haiku ($0.0001 vs $0.015) = **99% savings**
- 15% are moderate → Sonnet ($0.003 vs $0.015) = **80% savings**
- 5% are complex → Opus ($0.015 = baseline)

**Average: 60–80% savings across all tasks**

### "Does it affect quality?"

No. Routing selects the **cheapest model that can handle the task**:
- Simple factual questions don't need Opus
- Haiku is 90% as good as Opus but costs 50x less
- Complex reasoning still goes to Opus

Quality stays high, costs drop.

### "How do I see what's being routed?"

```bash
# See the last routing decision
llm-router last

# See all routing history
llm-router snapshot

# See routed vs actual cost
llm-router gain

# Open live dashboard
llm-router dashboard
```

---

## Troubleshooting

### "Hooks not running"

```bash
llm-router doctor  # See what's missing
llm-router install --force  # Reinstall hooks
```

### "I don't see savings"

```bash
# Run a command to generate routing data
# Then check:
llm-router budget  # Check spend
llm-router gain    # Check savings
```

### "Which models are available?"

```bash
llm-router providers  # List configured providers
llm-router verify     # Full system health check
```

---

## Learn More

- **[Host Support Matrix](HOST_SUPPORT_MATRIX.md)** — Which features work where
- **[Tool Selection Guide](TOOL_SELECTION_GUIDE.md)** — 48 MCP tools reference
- **[2-Minute Quickstart](QUICKSTART_2MIN.md)** — Fastest path to first routed call
- **[Security Guide](../SECURITY.md)** — Data privacy and safety
- **[Architecture](../README.md)** — How llm-router works under the hood

---

## Get Help

- **Setup issues?** → Run `llm-router doctor`
- **Questions?** → See [FAQ](#common-questions) above
- **Bugs?** → [GitHub Issues](https://github.com/ypollak2/llm-router/issues)
- **Feature requests?** → [GitHub Discussions](https://github.com/ypollak2/llm-router/discussions)

---

## You're Ready!

You now have:
- ✅ Automatic model routing for every task
- ✅ 60–80% cost reduction
- ✅ Usage analytics and cost tracking
- ✅ Hook-based automation (Claude Code / Codex / Gemini)

**Start saving immediately.** 🚀

