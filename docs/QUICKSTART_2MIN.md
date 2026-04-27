# LLM Router — 2-Minute Quickstart

Get cost optimization running **right now**. ~120 seconds, 4 steps.

---

## ⏱️ Timeline: 2 Minutes

**0:00–0:30** — Install
**0:30–1:00** — Configure (optional)
**1:00–2:00** — Verify & start routing

---

## 📦 Step 1: Install (30 seconds)

Pick **one** command based on your setup:

### Claude Code (Recommended)
```bash
pip install llm-router
llm-router install
```
✅ Done. Hooks installed automatically.

### Codex CLI
```bash
pip install llm-router
llm-router install --host codex
```
✅ Done. Hooks installed for Codex.

### Gemini CLI
```bash
pip install llm-router
llm-router install --host gemini-cli
```
✅ Done. Hooks installed for Gemini.

### VS Code / Cursor
```bash
pip install llm-router
llm-router install --host vscode  # or cursor
```
✅ Done. MCP server configured.

---

## 🔑 Step 2: (Optional) Add API Keys (30 seconds)

### Skip This If:
- ✅ You only want free models (Ollama, Codex, Gemini Free)
- ✅ You already have API keys in environment variables

### Or Run:
```bash
# Create ~/.llm-router/config.yaml with your keys
cat > ~/.llm-router/config.yaml <<'EOF'
openai_api_key: "sk-..."      # Optional: for GPT-4o, o3
gemini_api_key: "AIza..."     # Optional: for Gemini Pro
ollama_base_url: "http://localhost:11434"  # Optional: for local Ollama
EOF

chmod 600 ~/.llm-router/config.yaml
```

**That's it.** Keys are never committed to git.

---

## ✅ Step 3: Verify Installation (30 seconds)

```bash
llm-router doctor
```

You should see:
```
✅ llm-router installed
✅ Hooks configured
✅ Storage ready
✅ Providers available
```

If you see ❌ errors, run:
```bash
llm-router install --force  # Reinstall hooks
```

---

## 🚀 Step 4: Start Routing (30 seconds)

### Claude Code / Codex / Gemini
Routing starts **automatically**. No action needed.
- Your prompts are analyzed automatically
- Cheap models handle simple tasks
- Expensive models only for complex work
- Savings tracked in real-time

### VS Code / Cursor
Use MCP tools manually:
```bash
# In your editor, invoke:
/llm_route <prompt>
```

### Check Your Savings (Anytime)
```bash
llm-router gain
```

Shows:
```
Today's Savings: $1.23
This Week: $8.47
All Time: $12.94
Efficiency: 3.2x (vs Opus baseline)
```

---

## 📊 That's It! You're Saving Money

### What's Happening Automatically

```
Your Prompt
    ↓
Auto-Router (analyzes task type)
    ↓
Free Model? → Ollama / Codex (FREE)
    ↓
Simple? → Haiku / GPT-4o-mini ($0.0001)
    ↓
Moderate? → Sonnet / GPT-4o ($0.003)
    ↓
Complex? → Opus / o3 ($0.01)
    ↓
💰 Cost Tracked & Reported
```

**Result: 60–80% cost reduction vs Opus-everywhere.**

---

## 🎯 Next Steps (After 2 Minutes)

### Understand Your Routing
```bash
llm-router snapshot     # See what happened this session
llm-router replay       # Re-run past decisions
llm-router dashboard    # Open analytics in browser
```

### Configure More
```bash
llm-router init-policy      # Customize routing behavior
llm-router setup           # Configure advanced options
```

### Read More
- [Host Support Matrix](HOST_SUPPORT_MATRIX.md) — Which host is best for you
- [SECURITY.md](../SECURITY.md) — What we do with your prompts
- [MCP Tools Reference](TOOLS.md) — 48 tools available

---

## ❓ Common Issues (30-Second Fixes)

### "Command not found: llm-router"
```bash
pip install llm-router  # Make sure it's installed
which llm-router        # Verify it's in PATH
```

### "Hooks not running"
```bash
llm-router install --force   # Force reinstall
llm-router doctor           # Check status
```

### "I don't see savings"
```bash
llm-router budget           # Check if it's tracking
llm-router gain             # View cumulative savings
```

### "Which host should I use?"
→ See [HOST_SUPPORT_MATRIX.md](HOST_SUPPORT_MATRIX.md)
→ TL;DR: Claude Code (best) > Codex (excellent) > Gemini (good) > VS Code (manual)

---

## 📞 Getting Help

- **Setup issues** → `llm-router doctor` and check output
- **Performance questions** → `llm-router budget` and `llm-router gain`
- **Feature questions** → [GitHub Discussions](https://github.com/ypollak2/llm-router/discussions)
- **Bugs/Errors** → [GitHub Issues](https://github.com/ypollak2/llm-router/issues)
- **Security concerns** → [SECURITY.md](../SECURITY.md#reporting-security-vulnerabilities)

---

## ✨ That's Genuinely It

You now have:
- ✅ Automatic model routing
- ✅ Cost optimization (60–80% savings)
- ✅ Usage analytics
- ✅ Hook-based automation (Claude Code / Codex / Gemini)

**Total time: 2 minutes**
**Start saving: immediately**

Enjoy your cheaper LLM calls! 🚀
