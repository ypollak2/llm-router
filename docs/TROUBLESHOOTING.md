# Troubleshooting Guide

## Quick Diagnostics

Start with these commands to understand your system state:

```bash
# Check which providers are configured
python -c "from llm_router.config import get_config; print(get_config().available_providers)"

# Verify provider connectivity
# In Claude Code: Use llm_health

# Check budget status
# In Claude Code: Use llm_usage

# See recent routing decisions and costs
# In Claude Code: Use llm_quality_report
```

---

## Common Issues

### "ModuleNotFoundError: No module named 'anthropic'"

**Cause**: Claude subscription mode is enabled but the Anthropic SDK isn't installed.

**Fix**:
```bash
pip install anthropic
# or
uv pip install anthropic
```

If using Codex or multi-model setup, ensure Anthropic SDK is available:
```bash
uv add anthropic
```

---

### "API key is invalid" or "401 Unauthorized"

**Cause**: Incorrect or expired API key.

**Diagnosis**:
```bash
# In Claude Code:
Use llm_setup with action="test" provider="openai"  # test a specific provider
Use llm_setup with action="discover"                 # scan for keys on your machine
```

**Fix**:
1. Regenerate the API key at the provider's dashboard
2. Update `.env` with the new key
3. Restart your Claude session
4. Test with `llm_health` or `llm_setup action=test`

**Keys expire or rotate**:
- OpenAI: https://platform.openai.com/api-keys
- Google Gemini: https://aistudio.google.com/apikey
- Anthropic: https://console.anthropic.com/settings/keys
- All other providers: check their dashboards

---

### "All models failed" RuntimeError

**Cause**: Every model in the fallback chain failed.

**Diagnosis**:
```bash
# In Claude Code:
Use llm_health                           # Check provider status
Use llm_usage                            # See if budget is exhausted
Use llm_providers                        # List available providers
```

**Possible causes**:
1. **No providers configured**: Add at least one API key to `.env`
2. **Budget exceeded**: Check `llm_usage` — monthly or daily limit hit
3. **All providers down**: Check `llm_health` — circuit breakers are tripped
4. **Ollama not running**: If using local models, start Ollama
5. **Network issue**: No internet or firewall blocking API calls

**Fix**:
- Configure at least one provider (Gemini free tier recommended)
- Check budget limits in `~/.llm-router/routing.yaml` or pass `budget=`
- Restart Ollama if using local models
- Check firewall/proxy settings if behind corporate network

---

### "Budget exceeded" BudgetExceededError

**Cause**: Monthly or daily spending limit exceeded.

**Types**:
- **Global monthly limit**: Total spend across all providers hit
- **Provider limit**: Single provider quota exhausted (e.g., Sonnet weekly quota)
- **Per-task daily cap**: Daily spend for a specific task type exceeded

**Diagnosis**:
```bash
# In Claude Code:
Use llm_usage period="today"             # See today's spending
Use llm_usage period="month"             # See monthly spending
Use llm_usage                            # See budget limits
```

**Fix — increase budget**:
```bash
# In Claude Code:
Use llm_set_profile "premium"            # Higher limits, may cost more
# or
Use llm_setup action="add" provider="gemini"  # Add free Gemini tier
```

**Fix — check policy caps**:
```bash
# In Claude Code — check per-task daily caps
# View ~/.llm-router/routing.yaml and look for task_caps:
cat ~/.llm-router/routing.yaml | grep -A 5 task_caps
```

**Fix — change routing profile**:
- `budget` — cheapest models (Haiku, Gemini Flash)
- `balanced` — mid-tier (Sonnet, GPT-4o)
- `premium` — frontier models (Opus, o3)

---

### "Circuit breaker open" — provider is unavailable

**Cause**: Provider is down, rate-limited, or repeatedly failing.

**How circuit breaker works**:
- After 3 consecutive failures → provider is circuit-breaker tripped
- Router automatically skips to next provider
- Circuit breaker resets after 5 minutes of no attempts

**Diagnosis**:
```bash
# In Claude Code:
Use llm_health  # Shows "circuit_breaker: open" for affected providers
```

**Fix**:
1. Wait 5 minutes for circuit breaker to reset
2. Check provider status page (e.g., https://status.openai.com)
3. Verify your API key is valid: `Use llm_setup action=test`
4. Configure a backup provider

---

### Slow responses or timeouts

**Cause**: Slow provider or network latency.

**Diagnosis**:
```bash
# In Claude Code:
Use llm_quality_report  # Shows latency per model over time
```

**Common causes**:
- **Complex task on simple model**: Use `complexity="complex"` explicitly
- **Ollama not responsive**: Check Ollama status: `ollama serve`
- **Rate limiting**: Provider is slowly serving due to quota limits
- **Network latency**: Check your internet connection

**Fix**:
```bash
# Use premium profile for faster inference
Use llm_set_profile "premium"

# Or specify a fast model:
# In Claude Code:
Use llm_route prompt="..." model="openai/gpt-4o-mini"
```

---

### Hook deadlock — Claude Code is stuck

**CRITICAL ISSUE**: The routing enforcement hook is blocking core Claude tools.

**Symptoms**:
- Read/Edit/Write/Bash tools fail with "Tool blocked"
- Can't fix the hook because tools are blocked
- Session is completely stuck

**Cause**: The enforce-route hook has a blocklist that includes core Claude tools.

**Emergency Fix**:
```bash
# BEFORE opening Claude Code, run this in terminal:
export LLM_ROUTER_ENFORCE=off

# Now open Claude Code and fix the hook
# Check ~/.claude/hooks/llm-router-enforce-route.py
# Ensure it NEVER blocks: Read, Edit, Write, Bash, Grep, Glob, Agent
```

**Prevention**:
✅ Use **blocklist approach**: Block specific violations only
❌ Never use allowlist that omits core tools

**Safe block list example**:
```python
# SAFE — blocks only routing violations, allows core tools
if tool_name in ["Bash", "WebSearch", "Read"]:
    allow_tool()  # Core tools ALWAYS allowed

if violation_detected and tool_name not in CORE_TOOLS:
    block_tool()
```

---

### Wrong model selected (expected GPT-4o, got Haiku)

**Cause**: Complexity was classified as "simple" when you expected "complex".

**Diagnosis**:
```bash
# In Claude Code:
Use llm_classify complexity=null  # See auto-classification result
Use llm_quality_report            # Check recent routing decisions
```

**Why this happens**:
1. **Heuristic matched too early**: Short prompts classified as "simple"
2. **Ollama offline**: Can't do local classification, falls back to heuristic
3. **Classifier miscalibrated**: Qwen/Gemini misclassified the task

**Fix — explicit complexity override**:
```bash
# In Claude Code:
Use llm_code prompt="..." complexity="complex"
Use llm_analyze prompt="..." complexity="complex"
```

**Fix — check classification cache**:
```bash
# In Claude Code:
Use llm_cache_stats         # See classification cache hit rate
Use llm_cache_clear         # Clear stale classifications if needed
```

---

### "Ollama is required" but local models are disabled

**Cause**: Semantic cache or classification tried to use Ollama but it's not running.

**Diagnosis**:
```bash
# Check Ollama status
curl -s http://localhost:11434/api/tags || echo "Ollama not running"
```

**Fix**:
```bash
# Start Ollama
ollama serve

# Or disable semantic caching if you don't need it
# In Claude Code: Use llm_setup to toggle cache
```

---

### Usage tracking shows zero costs

**Cause**: SQLite database is empty or corrupted.

**Diagnosis**:
```bash
# Check database exists
ls -la ~/.llm-router/routing.db

# Check if tables exist
sqlite3 ~/.llm-router/routing.db ".tables"
```

**Fix**:
```bash
# Reinitialize database
rm -f ~/.llm-router/routing.db

# Next routing call will recreate it
# In Claude Code:
Use llm_usage  # This will initialize fresh database
```

---

### Permission denied errors

**Cause**: Hook files don't have execute permissions.

**Diagnosis**:
```bash
ls -la ~/.claude/hooks/llm-router-*.py
# Should show: -rwxr-xr-x (755 permissions)
```

**Fix**:
```bash
chmod 755 ~/.claude/hooks/llm-router-*.py

# Or reinstall hooks
# In Claude Code:
Use llm_setup action="install_hooks"
```

---

### Configuration conflicts (env vs config.yaml)

**Cause**: Both `.env` and `~/.llm-router/config.yaml` exist with different keys.

**How priority works**:
1. `.env` (project-level) — highest priority
2. `~/.llm-router/config.yaml` (user-level)
3. Environment variables (system-wide)

**Diagnosis**:
```bash
# Check which file is being used
grep -l "OPENAI_API_KEY" .env ~/.llm-router/config.yaml 2>/dev/null

# Check merged config
python -c "from llm_router.config import get_config; print(get_config())"
```

**Fix**:
- Use `.env` for project-specific keys (recommended)
- Use `~/.llm-router/config.yaml` for shared credentials across projects
- Don't duplicate keys in both files — keep in one place

---

### Routing violates policy constraints

**Cause**: Route hook blocked a tool or operation per policy.

**Symptoms**:
- Tool shows "blocked" warning in context
- MCP tool call fails with policy error
- Session message mentions routing violation

**Diagnosis**:
```bash
# Check active policy
# In Claude Code:
Use llm_policy  # Show current policy rules and violations
```

**Fix**:
1. Update routing or task type to comply with policy
2. Request policy exception from admin
3. Check `~/.llm-router/routing.yaml` for task-specific constraints

---

### Model chain doesn't include expected provider

**Cause**: Provider is circuit-breaker tripped, budget exhausted, or no API key.

**Diagnosis**:
```bash
# In Claude Code:
Use llm_health providers=["openai", "gemini"]  # See provider status
Use llm_providers                              # See available providers
```

**Why a provider disappears**:
- ❌ No API key configured
- ❌ Circuit breaker tripped (3 consecutive failures)
- ❌ Budget exhausted for that provider
- ❌ Provider model doesn't exist for the task type
- ❌ Provider blocked by policy rule

**Fix**:
1. Add missing API key: `Use llm_setup action="add" provider="openai"`
2. Wait 5 minutes for circuit breaker reset
3. Check budget: `Use llm_usage`
4. Check policy: `Use llm_policy`

---

### Cost estimates are wildly off

**Cause**: Estimation is based on token counts, actual costs may differ.

**Why estimates are approximate**:
- Token counts are heuristic (~4 chars = 1 token)
- Actual token count varies by model
- Special tokens (images, code) cost more
- Cache hits reduce actual costs

**Diagnosis**:
```bash
# In Claude Code:
Use llm_quality_report  # See estimated vs actual costs
```

**Accuracy**:
- ✅ Within 10-20% for typical text
- ❌ Off by 30%+ for images/videos
- ❌ Off by 50%+ if prompts use special formats

---

## Advanced Debugging

### Enable verbose logging

```bash
# Show detailed routing decisions
export LLM_ROUTER_LOG_LEVEL=DEBUG

# Show SQL queries
export LLM_ROUTER_SQL_DEBUG=1

# Show hook lifecycle
export LLM_ROUTER_HOOK_DEBUG=1
```

### Inspect pending state

```bash
# In Claude Code:
# Pending routing state (when a hook is deciding what to do)
ls -la ~/.llm-router/pending_*.json

# Check what's pending
cat ~/.llm-router/pending_route_*.json | python -m json.tool
```

### Check hook execution order

```bash
# See hook logs in Claude settings
# Hooks print to stderr if debugging is enabled

# Manual hook test
python -c "from llm_router.hooks.auto_route import main; main()"
```

### Validate configuration

```bash
# In Claude Code:
# Check syntax and conflicts
Use llm_setup action="discover"   # Find all keys on machine
Use llm_setup action="status"     # Show all configured providers

# Test each provider
Use llm_health                     # Test connectivity
```

---

## Getting Help

### When to ask for help

- **Can't configure a provider**: Check PROVIDERS.md first, then use `llm_setup`
- **Budget issues**: Use `llm_usage` to understand limits
- **Hook deadlock**: Export `LLM_ROUTER_ENFORCE=off` and fix the hook
- **Unexpected model selection**: Use `llm_quality_report` and `llm_classify`

### Useful diagnostic commands

```bash
# In Claude Code:
Use llm_quality_report              # See routing quality and latency
Use llm_health                       # Check provider status
Use llm_usage                        # See spending and limits
Use llm_providers                    # List available models
Use llm_policy                       # Show policy violations
```

### Report a bug

Include these details:
1. Output of `llm_health`
2. Output of `llm_usage`
3. Your profile: `llm_set_profile` result
4. Recent decision: `llm_quality_report period="today"`
5. Error message and stack trace

---

## Performance Tuning

### For faster inference

```bash
# Use faster models
Use llm_set_profile "budget"        # Haiku (3x faster than Sonnet)

# Or explicitly:
Use llm_code prompt="..." model="openai/gpt-4o-mini"
```

### For cheaper cost

```bash
# Use budget models
Use llm_set_profile "budget"        # Haiku/Flash (10x cheaper)

# Enable semantic cache (requires Ollama)
# If embedding hits, skips LLM call entirely
```

### For maximum quality

```bash
# Use frontier models
Use llm_set_profile "premium"       # Opus/o3 (most capable)

# Enable judge evaluation
export LLM_ROUTER_JUDGE_SAMPLE_RATE=1.0
```

---

## Known Limitations

1. **Semantic cache requires Ollama**: Embeddings computed locally
2. **Judge evaluation is async**: Scores appear in next session's history
3. **Circuit breaker resets after 5 min**: No way to reset manually (except delete DB)
4. **Budget tracking is approximate**: Token estimates may be off by 10-30%
5. **Policy enforcement is per-hook**: Some routes may bypass policy temporarily

---

## FAQ

**Q: Should I use `.env` or `~/.llm-router/config.yaml`?**
A: Use `.env` for project-specific keys (in `.gitignore`). Use `config.yaml` for shared credentials across projects.

**Q: What happens if all providers fail?**
A: Router tries emergency BUDGET fallback as last resort. If that fails, raises RuntimeError.

**Q: Can I manually reset the circuit breaker?**
A: Yes — delete `~/.llm-router/routing.db` and restart (fresh database).

**Q: How do I know if a model is low quality?**
A: Use `llm_quality_report` to see average judge scores. Models with <0.7 score are automatically deprioritized.

**Q: Can I disable routing and call a specific model?**
A: Yes — use explicit `model=` parameter in any `llm_*` tool: `Use llm_code model="openai/gpt-4o"`

---

## More Resources

- [PROVIDERS.md](./PROVIDERS.md) — Setup guide for 20+ providers
- [ARCHITECTURE.md](./ARCHITECTURE.md) — System design and data flow
- [BENCHMARKS.md](./BENCHMARKS.md) — Cost and latency metrics
- GitHub Issues: https://github.com/ypollak2/llm-router/issues
