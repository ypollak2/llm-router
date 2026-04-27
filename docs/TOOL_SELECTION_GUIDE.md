# Which Tool For Which Task?

48 MCP tools, organized by what you're trying to do. Find your use case, find your tool.

---

## Quick Navigation

- [I want to...](#use-case-index)
- [Tool Categories](#tool-categories)
- [Decision Tree](#decision-tree)
- [Advanced Scenarios](#advanced-scenarios)

---

## Use-Case Index

### Content Generation & Writing
| Task | Tool | Notes |
|------|------|-------|
| **Generate code** | `llm_code` | Complex algorithms, full features |
| **Write documentation** | `llm_generate` | Markdown docs, guides, specs |
| **Draft emails/messages** | `llm_generate` | Social media, email, Slack |
| **Create content outline** | `llm_generate` | Blog posts, articles, newsletters |
| **Refactor existing code** | `llm_code` | Rewrite/improve existing code |
| **Suggest better variable names** | `llm_query` | Quick naming suggestions |
| **Write unit tests** | `llm_code` | Test code generation |

### Analysis & Problem-Solving
| Task | Tool | Notes |
|------|------|-------|
| **Debug a bug** | `llm_analyze` | Complex debugging, deep reasoning |
| **Explain error message** | `llm_query` | Quick explanation of errors |
| **Compare two approaches** | `llm_analyze` | Weighing trade-offs, pros/cons |
| **Review code** | `llm_analyze` | Full code review with suggestions |
| **Optimize algorithm** | `llm_analyze` | Performance improvements |
| **Understand architecture** | `llm_query` | Learning how system works |
| **Trace code execution** | `llm_analyze` | Complex execution flows |

### Research & Learning
| Task | Tool | Notes |
|------|------|-------|
| **Research current topic** | `llm_research` | Current events, web search |
| **Find library docs** | `llm_query` | Quick lookup of documentation |
| **Compare libraries** | `llm_research` | Which library is best for X? |
| **Learn new framework** | `llm_query` | Framework concepts and patterns |
| **Check best practices** | `llm_analyze` | Industry standards, patterns |

### Routing & Optimization
| Task | Tool | Notes |
|------|------|-------|
| **Smart model selection** | `llm_route` | Full re-classification, override defaults |
| **Classify task complexity** | `llm_classify` | Get confidence scores for routing |
| **Track token usage** | `llm_track_usage` | Log usage for analytics |
| **Stream long responses** | `llm_stream` | Real-time response generation |

### Media Generation
| Task | Tool | Notes |
|------|------|-------|
| **Generate image** | `llm_image` | Diagrams, illustrations, mockups |
| **Generate video** | `llm_video` | Animations, demo videos |
| **Generate audio/voiceover** | `llm_audio` | Text-to-speech, narration |

### Multi-Step Tasks
| Task | Tool | Notes |
|------|------|-------|
| **Complex pipeline** | `llm_orchestrate` | Research → analyze → generate |
| **Browse templates** | `llm_pipeline_templates` | See pre-built pipeline patterns |

### Monitoring & Admin
| Task | Tool | Notes |
|------|------|-------|
| **Check spending** | `llm_budget` | Current budget status |
| **View savings** | `llm_savings` | Cumulative cost reduction |
| **Monitor model quality** | `llm_quality_report` | Model performance metrics |
| **Check provider health** | `llm_health` | Are providers up? |
| **See available providers** | `llm_providers` | What providers are configured? |
| **Set routing profile** | `llm_set_profile` | Switch between aggressive/balanced/conservative |
| **View usage metrics** | `llm_usage` | Detailed usage breakdown |

### API Keys & Configuration
| Task | Tool | Notes |
|------|------|-------|
| **Check Claude subscription status** | `llm_check_usage` | Real-time quota data |
| **Refresh OAuth tokens** | `llm_refresh_claude_usage` | Update Claude subscription data |
| **Update usage manually** | `llm_update_usage` | Manual usage entry |
| **Setup providers** | `llm_setup` | Configure new API keys |
| **Save session context** | `llm_save_session` | Persist learning across sessions |

### Model-Specific Routing
| Task | Tool | Notes |
|------|------|-------|
| **Route to Codex (OpenAI)** | `llm_codex` | Direct Codex call (uses OpenAI subscription) |
| **Route to Gemini CLI** | `llm_gemini` | Direct Gemini call |

---

## Tool Categories

### 🎯 Core Routing Tools
These are your primary interfaces for directing work to models:

```
llm_route          — Smart routing with full re-classification
llm_classify       — Get complexity score without routing
llm_query          — Quick questions (auto-routes to cheap model)
llm_generate       — Content creation (auto-routes to generation specialist)
llm_code           — Code generation/refactoring (auto-routes to coding model)
llm_analyze        — Deep analysis (auto-routes to reasoning model)
llm_research       — Web-based research (routes to Perplexity/web models)
```

**When to use each:**
- `llm_route` — When you want full control over classification
- `llm_query` — "What is X?" (simple question)
- `llm_generate` — "Write Y" (content creation)
- `llm_code` — "Implement Z" (code task)
- `llm_analyze` — "Why is A broken?" (deep problem-solving)
- `llm_research` — "What's the latest on B?" (current information needed)

### 🎨 Media Tools
```
llm_image          — Generate images (DALL-E, Gemini, Flux, Stable Diffusion)
llm_video          — Generate videos (Gemini Veo, Runway, Kling)
llm_audio          — Text-to-speech (ElevenLabs, OpenAI TTS)
```

### 🔄 Streaming & Orchestration
```
llm_stream         — Real-time response streaming (long outputs)
llm_orchestrate    — Multi-step pipelines (research → analyze → generate)
llm_pipeline_templates — See available pipeline patterns
```

### 💰 Monitoring & Analytics
```
llm_budget         — Real-time spending status
llm_savings        — Cumulative savings report
llm_usage          — Detailed token usage breakdown
llm_gain           — Savings multiplier and efficiency metrics
llm_quality_report — Model performance by accuracy
llm_quality_guard  — Alert on quality degradation
llm_session_spend  — Real-time session cost
```

### 🏥 Health & Configuration
```
llm_health         — Provider status checks
llm_providers      — List configured providers
llm_setup          — Configure API keys and onboarding
```

### 🔐 Authentication & Subscription
```
llm_check_usage    — Claude subscription real-time status
llm_refresh_claude_usage — Update OAuth token
llm_update_usage   — Manual usage entry
llm_save_session   — Persist session for next session
```

### 🎯 Direct Model Access
```
llm_codex          — Direct access to Codex (OpenAI)
llm_gemini         — Direct access to Gemini CLI
```

### 📊 Usage Tracking
```
llm_track_usage    — Log usage for cumulative savings
```

---

## Decision Tree

```
What do you want to do?

├─ Generate or write something
│  ├─ Code? → llm_code
│  ├─ Documentation/content? → llm_generate
│  └─ Creative writing? → llm_generate
│
├─ Answer a question / Quick lookup
│  ├─ Simple question? → llm_query
│  ├─ Needs web search? → llm_research
│  └─ Current events? → llm_research
│
├─ Debug / Problem-solve
│  ├─ Simple error? → llm_query
│  └─ Complex issue? → llm_analyze
│
├─ Generate media
│  ├─ Image? → llm_image
│  ├─ Video? → llm_video
│  └─ Audio/voiceover? → llm_audio
│
├─ Complex pipeline
│  ├─ Research then write? → llm_orchestrate
│  ├─ Analyze then code? → llm_orchestrate
│  └─ See templates? → llm_pipeline_templates
│
├─ Check spending / analytics
│  ├─ Current spend? → llm_budget
│  ├─ Total savings? → llm_savings
│  └─ Model quality? → llm_quality_report
│
└─ Admin / Configuration
   ├─ Setup new key? → llm_setup
   ├─ Check subscription? → llm_check_usage
   └─ View providers? → llm_providers
```

---

## Advanced Scenarios

### Scenario 1: "I want to research AND write a blog post"
**Tools:** `llm_orchestrate` (with research + generate pipeline)

Alternative: Use `llm_research` then `llm_generate` separately

### Scenario 2: "Generate code, but I want to use a specific model"
**Tools:** `llm_route` (full re-classification) then invoke directly

Alternative: Use `llm_codex` or `llm_gemini` for model-specific routing

### Scenario 3: "I want to compare costs of different models"
**Tools:** `llm_usage`, `llm_gain`, `llm_quality_report`

Then use `llm_route` with `complexity_override` to test different models

### Scenario 4: "I need real-time response for long output"
**Tools:** `llm_stream` (streaming mode)

Use when normal `llm_generate` / `llm_code` takes too long

### Scenario 5: "I want to debug a specific provider issue"
**Tools:** `llm_health` (check provider status), then `llm_setup` (reconfigure)

### Scenario 6: "Complex multi-step research + analysis + code"
**Tools:** `llm_orchestrate` with custom pipeline

Or chain: `llm_research` → `llm_analyze` → `llm_code`

---

## Tool Complexity Matrix

| Tool | Learning Curve | Typical Use |
|------|:---------------:|-----------|
| **llm_query** | ⭐ Easy | Quick questions |
| **llm_generate** | ⭐ Easy | Writing anything |
| **llm_code** | ⭐ Easy | Generate/refactor code |
| **llm_analyze** | ⭐⭐ Medium | Deep problem-solving |
| **llm_research** | ⭐⭐ Medium | Web-based research |
| **llm_route** | ⭐⭐ Medium | Custom routing decisions |
| **llm_image** | ⭐⭐ Medium | Image generation |
| **llm_orchestrate** | ⭐⭐⭐ Complex | Multi-step pipelines |
| **llm_stream** | ⭐⭐ Medium | Real-time responses |
| **llm_budget** | ⭐ Easy | Check spending |
| **llm_health** | ⭐ Easy | Check status |
| **llm_setup** | ⭐⭐ Medium | Configure keys |

---

## Pro Tips

✅ **Do:**
- Start with `llm_query` / `llm_generate` / `llm_code` (auto-routing handles it)
- Use `llm_analyze` for bugs and architecture decisions
- Check `llm_budget` regularly to understand your spending
- Use `llm_orchestrate` for complex multi-step tasks
- Let auto-routing decide most of the time (it's good at it)

❌ **Don't:**
- Always use `llm_route` (auto-routing already works)
- Over-specify tools (let the system decide)
- Ignore `llm_health` if a provider seems slow
- Skip `llm_budget` — understand your costs

---

## Cheat Sheet

**One-liners for common tasks:**

```bash
# Quick questions
llm_query "What is X?"

# Write code
llm_code "Implement Y function"

# Debug something
llm_analyze "Why is this broken? <code>"

# Content creation
llm_generate "Write blog post about X"

# Research current topic
llm_research "Latest trends in AI, 2026"

# Complex pipeline
llm_orchestrate "Research X, analyze, then code solution"

# Check budget
llm_budget

# View savings
llm_gain

# See routing decision
llm_classify "<prompt>"
```

---

## Questions?

- Which tool is best for X? → Check this guide's use-case index
- How do I use tool Y? → See `llm_help <tool-name>`
- What are realistic timings? → See specific tool docs
- How much will this cost? → `llm_budget` before, `llm_savings` after

Happy routing! 🚀
