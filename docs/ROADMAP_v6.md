# Product Roadmap v6.0–v7.0 — "Visible → Memory → Quality → Local → Community → Platform"

**Version:** 6.0–7.0
**Timeline:** May–October 2026 (6 months, monthly releases)
**Author:** yali.pollak
**Last Updated:** 2026-04-16

---

## Executive Summary

The market has spoken. Two observations from our competitor analysis:

1. **Visibility** is the #1 driver of Claude Code plugin adoption (claude-hud, 19k stars)
2. **Memory** is the #1 driver of retention (claude-mem, 59k stars)

Our routing is currently **invisible** and **forgets** you. This roadmap fixes both, moving from internal black box → visible dashboard → learned personal profiles → quality-proven → local-model-native → community-scaled → platform infrastructure.

Each version adds one major pillar. Each pillar maps to a market signal with real GitHub stars backing it.

---

## Product Cycle Framework

### Monthly Rhythm

Every sprint (week 1):

1. **Market Pulse** — `llm_research` LLM trends + GitHub trending (Python/LLM tag) + HN "Show HN" (last 30 days) → save to `docs/market/YYYY-MM.md`
2. **Competitor Diff** — `gh search repos "llm routing" --sort=stars` — flag star velocity changes, watch NadirClaw/UncommonRoute
3. **User Feedback Scan** — Triage GitHub Issues: tag bug/feature/UX/perf. PyPI downloads. Recurring complaints (>3) = pattern
4. **RICE Prioritization** — (Reach × Impact × Confidence) / Effort. Top 5 features per version
5. **Version Theme** — One word. 3–5 features. Always: 1 UX + 1 reliability + 1 new feature

### Quality Gates (Before Every Version)

```bash
# Tests pass
uv run pytest tests/ -m "" -q

# All versions in sync
python3 -c "import tomllib, json; v=$(tomllib.load(open('pyproject.toml','rb'))['project']['version']); assert json.load(open('.claude-plugin/plugin.json'))['version']==v"

# README updated
# CHANGELOG.md updated
# Commit + PyPI publish + GitHub release

# One-command install verification
pip install --upgrade claude-code-llm-router && llm-router install --check
```

---

## v6.0 — "Visible" (May 2026)

**Market Signal:** claude-hud (19k stars) — developers crave seeing what Claude does

**Problem:** Routing is invisible. Users don't know which model handled their call, why, or how much it saved.

**Thesis:** Make every routing decision legible. This is our HUD.

### Features

#### 1. Live Routing HUD (Statusline)
Shows real-time routing on every message:
```
→ haiku (code/simple) $0.001 saved ⚡
→ sonnet (analysis/moderate) $0.008 saved ⚡
→ opus (planning/complex) escalated $0.062 saved ⚡
```

**Implementation:**
- Hook into `UserPromptSubmit` → extract routed model + cost delta
- Render in Claude Code statusline (existing `caveman_mode` infrastructure)
- Show confidence score: `→ haiku [87% confidence]`
- One-line per message, cleared on next message

**User Value:** "I can see which model is handling this, right now."

---

#### 2. Session Routing Replay
New command: `llm-router replay` — prints session transcript with routing decisions inline.

**Example output:**
```
# Session replay — 2026-05-10 14:30–15:45 (12 routed calls)

[14:30] You: "Write a function to parse JSON"
    → routed to haiku (code/simple)
    ✅ haiku responded in 1.2s, $0.001
    Reasoning: <50 lines, standard library, low risk

[14:31] You: "What's the architecture of this project?"
    → routed to sonnet (analysis/moderate)
    ✅ sonnet responded in 3.4s, $0.008
    Reasoning: Multi-file analysis, domain knowledge required

[14:35] You: "Help me architect the auth system"
    → tried haiku [23% confidence] → escalated to sonnet
    ✅ sonnet responded in 4.1s, $0.009
    Reasoning: Confidence too low (architecture requires deep reasoning)
    Saved vs baseline (opus): $0.054

---SUMMARY---
Total routed: 12 calls
Cost: $0.186
Savings vs Opus baseline: $1.847 (90% savings)
Avg confidence: 86%
Escalations: 2 (17%)
```

**Implementation:**
- Query `routing_decisions` table
- Render with ANSI color + timestamps
- Include reasoning + savings per call

**User Value:** "I can review exactly what happened this session and learn my patterns."

---

#### 3. Routing Confidence Score
Each decision shows confidence: `→ haiku [87%]`

**Implementation:**
- Extend `router.classify()` to return `(model, complexity, confidence_pct)`
- Confidence = (1 - P(misclassification)) from classifier metrics
- Low confidence (<60%) triggers auto-escalation with silent fallback

**User Value:** "I can trust the low-confidence escalation. I won't get surprised with a cheap model on risky code."

---

#### 4. One-Command Health Check
New command: `llm-router verify`

**Output (30-second test):**
```
llm-router verify

────────────────────────────────────────────
         llm-router health check
────────────────────────────────────────────

✅ Configuration loaded from ~/.llm-router/config.yaml
✅ SQLite database: ~/.llm-router/usage.db (45 MB, last write 5 mins ago)

────────── Active Models ──────────
✅ Ollama (http://localhost:11434)
   └─ gemma4:latest (7B, avg 120 tok/s)
   └─ qwen2.5-coder:7b (7B, avg 95 tok/s)

✅ OpenAI API (org-id=org-xxx)
   └─ gpt-4o (available)
   └─ o3 (available)

✅ Gemini API
   └─ gemini-2.5-flash (available)

❌ Perplexity API (OFFLINE — check PERPLEXITY_API_KEY)

────────── Hooks Status ──────────
✅ auto-route hook (fires on every UserPromptSubmit)
✅ session-end hook (tracks savings)
✅ enforce-route hook (policy enforcement)

────────── Live Routing Chain ──────────
simple:   ollama → openai/gpt-4o-mini → gemini/flash
moderate: ollama → openai/gpt-4o → gemini/pro → sonnet
complex:  ollama → openai/o3 → claude/opus

────────── Last 5 Decisions ──────────
2 min ago  → haiku (code, simple)      $0.0001
5 min ago  → sonnet (analysis, mod)    $0.008
12 min ago → opus (planning, complex)  $0.062
...

────────────────────────────────────────────
No issues detected. You're good! 🚀
```

**Implementation:**
- Health check each provider (quick curl)
- Verify hooks are installed + executable
- Show routing chain for current profile
- Query last 5 decisions from DB

**User Value:** "I can verify everything is working without verbose output."

---

### Metrics

| Metric | Target | Rationale |
|--------|--------|-----------|
| Installation friction | <2 minutes | Must beat manual setup |
| HUD visibility | 100% adoption | Always on, no config needed |
| Replay usefulness | 80% of users run it once/week | Teaches routing patterns |
| Help desk reduction | -30% | Visibility reduces "which model was used?" questions |

---

### Acceptance Criteria

- [ ] Live HUD shows on all routed messages (statusline)
- [ ] `llm-router replay` works for any past session
- [ ] `llm-router verify` detects 95%+ of common issues
- [ ] Confidence score never misses (accurate to ±5%)
- [ ] Zero performance impact (<10ms overhead per call)

---

## v6.1 — "Memory" (June 2026)

**Market Signal:** claude-mem (59k stars) — memory + personalization = #1 driver of retention

**Problem:** The router forgets you. After 50 sessions, it's still using generic heuristics instead of learning your patterns.

**Thesis:** The router should know you better than any config file.

### Features

#### 1. Personal Routing Profile Auto-Generation
After 50 routed calls, auto-generate `~/.llm-router/profile.json`:

**Example:**
```json
{
  "generated": "2026-06-05T14:30:00Z",
  "confidence": 0.87,
  "task_distribution": {
    "code_generation": {
      "frequency": "60%",
      "preferred_model": "haiku",
      "accuracy": "94%"
    },
    "architecture_analysis": {
      "frequency": "25%",
      "preferred_model": "sonnet",
      "accuracy": "96%"
    },
    "qa_research": {
      "frequency": "15%",
      "preferred_model": "haiku",
      "accuracy": "91%"
    }
  },
  "recommendations": [
    "You write Python code (60%) — Haiku is 94% accurate for your code",
    "Architecture analysis (25%) — You always need Sonnet (Haiku only 71% on architecture)",
    "Q&A (15%) — Haiku handles 91% of your questions well, escalates to Sonnet when uncertain"
  ],
  "learned_overrides": {
    "security_review": "always_opus",
    "quick_qa": "always_haiku",
    "refactoring": "sonnet_with_fallback_haiku"
  }
}
```

**Implementation:**
- Run nightly (or on-demand with `llm-router learn`)
- Query last 50 routed calls from DB
- Cluster by task type
- Compute accuracy per model/task combo
- Suggest optimal routing per task

**User Value:** "After using the router, it learns my patterns. My routing gets better automatically."

---

#### 2. Override Learning
When user manually overrides routing 3x for same task type → remember permanently.

**Example:**
```
# User runs security review
1st time: router suggests haiku → user escalates to opus
2nd time: router suggests haiku → user escalates to opus
3rd time: router suggests opus automatically

# Profile updated:
learned_overrides.security_review = "always_opus"
```

**Implementation:**
- Hook into enforcement tool (detect manual override)
- Track (task_type, original_model, override_model) tuples
- After 3 occurrences, add to learned_overrides
- Notification: "Got it. Security reviews now always use Opus. ✅"

**User Value:** "I only have to correct the router 3 times, then it gets it forever."

---

#### 3. Usage Pattern Visualization
Command: `llm-router profile`

**Output:**
```
Your Routing Profile
═══════════════════════════════════════

Generated: 2026-06-10 (based on 87 routed calls since 2026-05-15)
Confidence: 87% (recommend ~20 more calls for stability)

───── Task Distribution ─────
Code Generation   ████████████░░░░░░░ 60%  (52 calls)
Architecture      █████░░░░░░░░░░░░░░ 25%  (22 calls)
Q&A Research      ███░░░░░░░░░░░░░░░░ 15%  (13 calls)

───── Model Accuracy by Task ─────
CODE GENERATION:
  Haiku   ████████████████████ 94% (49/52 calls, avg 1.2s)
  Sonnet  ██████████████████░░ 92% (fallback only)

ARCHITECTURE:
  Sonnet  ████████████████████ 96% (21/22 calls, avg 3.4s)
  Haiku   ███░░░░░░░░░░░░░░░░ 14% (too risky)

Q&A RESEARCH:
  Haiku   ███████████████░░░░░ 91% (12/13 calls, avg 0.8s)
  Gemini  ███████████░░░░░░░░░ 77% (fallback)

───── Cost Impact ─────
Total routed (May 15–Jun 10): $14.32
vs Opus baseline (all opus):    $347.20
Savings:                        $332.88 (96%)

Monthly projection: $17.40 routed, $421/mo baseline, $403/mo saved

───── Top Recommendations ─────
1. You're a Python developer. Haiku handles 94% of your code.
   → Keep using Haiku for code, Sonnet only for architecture.

2. Your security reviews always escalate to Opus (3x override pattern).
   → Now always using Opus for security. Smart call.

3. You occasionally use Gemini for research (lower quality, 77%).
   → Consider using Haiku instead (91% accuracy, faster).

Press 'q' to close.
```

**Implementation:**
- Query `routing_decisions` + `quality_scores` tables
- Group by inferred task type
- Render ASCII charts with `rich` library
- Show cost impact + projections

**User Value:** "I can see my routing patterns and learn from them. The data is beautiful and actionable."

---

#### 4. Profile Export/Import (Community Sharing)
Export your profile as a shareable YAML template:

```bash
llm-router export-profile python-backend-dev.yaml
# → Creates a profile you can commit to GitHub

# Others can import:
llm-router import-profile https://raw.githubusercontent.com/user/repo/python-backend-dev.yaml
```

**Use Case Examples:**
- Python backend dev: "Haiku for tests, Sonnet for design, Opus for security"
- TypeScript frontend: "Haiku for components, Sonnet for state management"
- Data scientist: "Haiku for EDA, Opus for modeling decisions"
- DevOps engineer: "Haiku for configs, Opus for architecture"

**Implementation:**
- Export profile.json as YAML + description
- Create `docs/profiles/` with community profiles
- `llm-router profile-list` shows available profiles + stars

**User Value:** "I can benefit from patterns discovered by thousands of other users with my exact workflow."

---

### Metrics

| Metric | Target | Rationale |
|--------|--------|-----------|
| Profile generation time | <5 seconds | Must feel instant |
| Profile accuracy after 50 calls | 80%+ | Confident recommendations |
| User adoption of learned_overrides | 70% | Shows personalization working |
| Community profile shares | 100+ by end of quarter | Network effect |

---

### Acceptance Criteria

- [ ] Personal profile auto-generates after 50 calls with 80%+ accuracy
- [ ] Override learning works for 3+ task types
- [ ] Profile visualization shows clear patterns
- [ ] Export/import works across machines
- [ ] Community profiles load without errors

---

## v6.2 — "Quality" (July 2026)

**Market Signal:** UncommonRoute messaging — "82% cost savings, 79.4% accuracy, 93.4% pass rate" converts skeptics

**Problem:** #1 objection to adoption: "Will cheaper model break my code?"

**Thesis:** Prove quality with numbers. Publish benchmarks. Make it safe.

### Features

#### 1. Quality Guard
Before routing to cheaper model on high-stakes tasks, run a lightweight pre-check. Auto-escalate if quality signals are present.

**Task Categories:**
- 🟢 Safe (can use cheap model): "Write unit test", "Format code", "Explain function"
- 🟡 Medium (check confidence): "Add feature", "Refactor module", "Debug issue"
- 🔴 Unsafe (must escalate): "Security review", "Architecture decision", "Compliance check"

**Pre-Check Logic:**
```python
def should_escalate(task: Task, model: Model) -> bool:
    """Check if task is safe for this model."""
    if task.category == "unsafe":
        return True  # Always escalate

    if task.category == "medium":
        # Medium: check confidence + task complexity
        if model == "haiku" and task.complexity > "moderate":
            return True  # Haiku not confident enough

    return False
```

**Implementation:**
- Lightweight classifier (regex patterns on task description)
- If escalation needed, silent fallback to next model in chain
- Log decision to `routing_decisions` table

**User Value:** "The router won't silently assign a weak model to a critical task. I can trust the low cost."

---

#### 2. Quality Score Tracking
Every routed response gets a lightweight quality score (background judge).

**Scoring Dimensions:**
- Correctness (0–100%): Does it work? Pass basic tests?
- Completeness (0–100%): Did it answer fully?
- Style (0–100%): Does it follow conventions?
- Confidence (0–100%): How sure is the judge?

**Implementation:**
- Lightweight local judge (Ollama qwen3.5 or Claude Haiku) scores every response
- Store in `response_quality` table
- Compute running average per (model, task_type)
- Sample rate configurable: `LLM_ROUTER_JUDGE_SAMPLE_RATE=0.1` (10%) or `1.0` (100%)

**User Value:** "I can measure if a cheaper model is actually working for me."

---

#### 3. Degradation Alerts
If quality score drops below threshold → automatic escalation + alert.

**Example:**
```
🚨 Quality Alert
─────────────────────────
Haiku quality for "code" dropped to 82% (from 94% avg).
Reason: 3 recent responses had syntax errors.

Action: Escalating code tasks to Sonnet until quality recovers.
Last response: Auto-escalated to Sonnet for safety.

Tip: Run `llm-router quality --task=code` to debug.
```

**Implementation:**
- Track 30-day rolling quality avg per (model, task_type)
- If drops >10% → alert + escalate
- Daily check, email if configured

**User Value:** "If a cheaper model stops working for me, the router automatically switches back."

---

#### 4. Published Benchmarks
Command: `llm-router benchmark`

**Output (shareable, posted to GitHub):**
```markdown
# llm-router Quality Benchmarks — May 2026

Tested on 200 real user sessions (1,247 routed calls).

## Summary

| Task Type | Haiku | Sonnet | Opus | Escalation Rate |
|-----------|-------|--------|------|-----------------|
| Code generation | 94% | 98% | 99% | 6% (haiku→sonnet) |
| Architecture | 71% | 96% | 99% | 29% (haiku→sonnet) |
| Q&A research | 91% | 94% | 96% | 9% |
| Bug debugging | 82% | 94% | 98% | 18% |

**Overall savings:** 82% cost reduction vs Opus baseline
**Quality maintained:** 94% avg accuracy (vs 99% Opus baseline)
**Escalation rate:** 12% (good — means confidence is working)
**Net recommendation:** Haiku for 60% of tasks safely.

## Methodology

- Real user sessions (not synthetic benchmarks)
- Quality scored by lightweight judge (Ollama qwen3.5)
- Escalation tracked automatically
- Pass/fail = "would this pass code review?"

## Community Contribution

These benchmarks are from **YOUR sessions**. Thank you for helping us make routing better! 🙏

Run `llm-router benchmark --opt-out` to disable contribution.
```

**Implementation:**
- Aggregate quality scores across users (anonymous by default)
- Post to GitHub releases with version tag
- Generate markdown with tables + methodology

**User Value:** "I can show my boss: 'Look, we're saving 82% and maintaining 94% quality.'"

---

### Metrics

| Metric | Target | Rationale |
|--------|--------|-----------|
| Quality guard accuracy | 95%+ | Must not over-escalate |
| Judge agreement with humans | 85%+ | Lightweight judge must align with reality |
| Benchmark publication | Monthly | Build credibility over time |
| Adoption of benchmarks in sales/marketing | 100% | This becomes our proof point |

---

### Acceptance Criteria

- [ ] Quality Guard prevents misrouting to unsafe models (95% success rate)
- [ ] Quality scores accurate to ±5% vs human judgment
- [ ] Degradation alerts fire with <1 hour latency
- [ ] Published benchmarks credible (independently auditable)

---

## v6.3 — "Local First" (August 2026)

**Market Signal:** Ollama explosive growth (40% user base growth in Q1 2026), NadirClaw threat (416 stars)

**Problem:** Local models are growing faster than cloud APIs. We're betting on cloud; we should double down on local.

**Thesis:** Become the best Ollama controller. Make local the default.

### Features

#### 1. Model Discovery Dashboard
Command: `llm-router models`

**Output:**
```
Local Models (Ollama @ http://localhost:11434)
═════════════════════════════════════════════

gemma4:latest              (7B)
  Speed: 120 tok/s ⚡ Fast
  Used: 143 times (last: 5 mins ago)
  Quality: 91% accuracy (code generation)
  Recommendation: Haiku replacement ✅
  Size: 4.9 GB
  Last loaded: 2 hours ago

qwen2.5-coder:7b           (7B)
  Speed: 95 tok/s ⚡ Fast
  Used: 47 times (last: 1 hour ago)
  Quality: 88% accuracy (code generation)
  Recommendation: Alternative for CPU-heavy tasks
  Size: 4.2 GB
  Last loaded: 30 mins ago

mistral:7b                 (7B)
  Speed: 110 tok/s ⚡ Fast
  Used: 12 times
  Quality: 85% accuracy (general Q&A)
  Size: 4.1 GB
  Last loaded: 1 week ago

llama2:7b                  (7B)
  Speed: 100 tok/s ⚡ Medium
  Used: 0 times (consider removing)
  Size: 3.8 GB
  Recommendation: ❌ Not used in 2 weeks, consider pruning

───────────────────────────────────────────
Total local capacity: 18.4 GB / 50 GB available
Models in use: 3
Models idle: 1
```

**Implementation:**
- Query Ollama `/api/tags` endpoint
- Track usage per model from `routing_decisions` DB
- Compute quality scores (avg from quality judge)
- Show size + speed metrics

**User Value:** "I can see all my local models, which ones work best, and which ones are wasting space."

---

#### 2. Auto Model Recommendation
After install, analyze usage patterns and recommend new models:

**Example:**
```
🎯 Model Recommendation
──────────────────────

Based on your 87 routed calls:

Recommendation: Add qwen2.5-coder:7b
└─ Matches 60% of your code tasks
└─ Saves: ~$8–12/month vs Opus
└─ Speed: 2.3s vs 0.8s (acceptable for 20% of tasks)

One-liner:
  ollama pull qwen2.5-coder:7b
```

**Implementation:**
- Analyze task distribution from profile
- Match to open-source models on Ollama library
- Recommend top 3 based on (fit %, cost savings, speed)
- Provide one-liner to install

**User Value:** "I discover new local models that work for MY specific use case."

---

#### 3. Offline Graceful Mode
When no internet, route everything through Ollama silently.

**Implementation:**
- Health check internet (quick dns query)
- If offline, set `_force_local=True` in router
- All external models → fallback to Ollama
- Silent (no user-facing error), automatic recovery

**User Value:** "I can code on a plane. Routing just works locally without internet."

---

#### 4. Local Model Benchmarking
Command: `llm-router benchmark local`

**Output:**
```
Local Model Benchmark — Your Machine
═════════════════════════════════════

Testing 200 real code generation tasks on your models...
(This takes ~30 minutes. Run overnight: `llm-router benchmark local --bg`)

Results:
────────────────────────────────────────────
gemma4:latest         91% accuracy, 120 tok/s
qwen2.5-coder:7b      88% accuracy, 95 tok/s
mistral:7b            85% accuracy, 110 tok/s

Recommendation:
Use gemma4 for code (91% is good). Only escalate to cloud for architecture (29% of tasks).

Estimated monthly cost:
- All local:     $0.00 (already paid for gemma4)
- Hybrid smart:  $4.20 (local 71%, cloud 29%)
- All cloud:     $73.40

💰 Savings: $69.20/month by staying local
```

**Implementation:**
- Generate test suite (200 code generation tasks)
- Run against each local model, time, score
- Show quality + speed tradeoffs
- Calibrate routing thresholds to user's hardware

**User Value:** "I know exactly which local models work on MY machine for MY patterns."

---

### Metrics

| Metric | Target | Rationale |
|--------|--------|-----------|
| Ollama adoption among users | 80% | Market trend |
| Model discovery usefulness | 70% of users have >1 model | Ecosystem building |
| Offline uptime | 99% | Must work without internet |
| Benchmark accuracy | 90%+ | Must match real usage |

---

### Acceptance Criteria

- [ ] Model dashboard shows all local models + quality metrics
- [ ] Auto-recommend suggests 3+ viable models
- [ ] Offline mode works transparently (no user error messages)
- [ ] Benchmarks run in <1 hour on typical hardware

---

## v6.4 — "Community" (September 2026)

**Market Signal:** Plugin marketplace success (1.9k stars), word-of-mouth distribution (claude-mem 59k, claude-hud 19k)

**Problem:** Routing savings are valuable but invisible to others. Word-of-mouth is our distribution.

**Thesis:** Make savings social. Users will market for us.

### Features

#### 1. Savings Proof Card
On session end, beautiful ASCII card showing impact:

**Example:**
```
╔═══════════════════════════════════════════════════════════╗
║  💰 Session Summary — 2026-09-15 (14:30–15:45)           ║
╠═══════════════════════════════════════════════════════════╣
║                                                           ║
║  Cost this session:       $0.18                           ║
║  Opus baseline:           $2.47 (all Opus)                ║
║                                                           ║
║  ✨ Saved:                $2.29 (93%)                      ║
║                                                           ║
║  ─────────────────────────────────────────────           ║
║  This month (Sep 1–15):                                   ║
║                                                           ║
║    Cost:     $14.32      (12 sessions)                    ║
║    Saved:    $367.20     (96% vs Opus)                    ║
║                                                           ║
║  ─────────────────────────────────────────────           ║
║  Yearly projection:       $1,248 saved                    ║
║                                                           ║
║  Powered by llm-router ⚡                                  ║
║  Get started: uvx llm-router install                      ║
╚═══════════════════════════════════════════════════════════╝
```

**Sharable Text Format:**
```
I saved $367.20 this month using llm-router 🚀
14x efficiency vs Claude Opus. Intelligent routing FTW.

Get started: uvx llm-router install
#llmrouter #claude #ai #productiviy
```

**Implementation:**
- Compute session cost + savings in session-end hook
- Render card with `rich` library
- Offer one-click "Copy for Twitter/Slack"

**User Value:** "I can flex my savings. Friends ask questions. They install."

---

#### 2. README Badge
One-liner adds efficiency badge to any project:

**Badge Format:**
```markdown
[![llm-router efficiency](https://badge.llm-router.dev/efficiency?profile=YOUR_HASH)](https://github.com/ypollak2/llm-router)
```

**Rendered:**
```
[llm-router: 87% efficiency ⚡]
```

**Implementation:**
- Generate unique profile hash per user (anonymous)
- Badge endpoint (serverless function) returns live efficiency %
- Click through to llm-router README

**User Value:** "Everyone who visits my GitHub repo sees that I use smart routing."

---

#### 3. Routing Config Marketplace
Users share and import routing profiles:

```bash
llm-router share-config python-backend-dev

# Creates a shareable link:
# https://llm-router.dev/market/profiles/python-backend-dev

# Others import:
llm-router import-config python-backend-dev
```

**Example Profiles:**
- `python-backend-dev` — "Haiku for tests, Sonnet for design, Opus for security"
- `typescript-frontend` — "Haiku for components, Sonnet for state management"
- `data-scientist` — "Haiku for EDA, Opus for modeling decisions"
- `devops-engineer` — "Haiku for configs, Opus for architecture"
- `writer` — "Sonnet for drafts, Opus for final"

**Implementation:**
- Marketplace endpoint (simple YAML storage + GitHub sync option)
- Vote/star profiles
- Show "14k users" below top profiles

**User Value:** "I can benefit from patterns discovered by thousands of developers."

---

#### 4. Anonymous Leaderboard (Opt-In)
Weekly top savers. Gamified.

```
Top Savers This Week (Opt-In Community)
═══════════════════════════════════════════

🥇 #1: Saved $847.20 (user-abc123)
       "TypeScript full-stack dev. Haiku for components, Opus for architecture decisions."
       Profile: typescript-fullstack | 23k followers

🥈 #2: Saved $723.14 (user-xyz789)
       "Python data scientist. 94% local, 6% cloud. Saving $14k/year."
       Profile: python-scientist | 18k followers

🥉 #3: Saved $612.95 (user-qwe456)
       "DevOps at Series B startup. Team of 5. $3k/month savings company-wide."
       Profile: devops-team | 12k followers

─────────────────────────────────────────
Your ranking: #127 (you're in top 2%)
Your savings: $84.20 (this week)
Projected annual: $4,378 saved

Want to compete? Share your profile:
  llm-router share-config my-workflow

Opt out of leaderboard: llm-router privacy --leaderboard=off
```

**Implementation:**
- Opt-in (off by default, must explicitly enable)
- Anonymized (user ID hash, no email)
- Weekly ranking by total savings
- Gamification: badges for milestones ($100, $1k, $10k saved)

**User Value:** "I can see how my savings compare. Friendly competition drives adoption."

---

### Metrics

| Metric | Target | Rationale |
|--------|--------|-----------|
| Proof card shareability | 40% of users share | Word-of-mouth distribution |
| Badge adoption | 1k+ repos | Social proof |
| Profile marketplace profiles | 500+ by year-end | Community contribution |
| Leaderboard monthly active | 10k+ users | Engagement loop |

---

### Acceptance Criteria

- [ ] Proof card renders perfectly (copy-paste ready)
- [ ] Badge shows accurate live efficiency
- [ ] Marketplace profiles load + work correctly
- [ ] Leaderboard is anonymous + fair

---

## v7.0 — "Platform" (October 2026)

**Market Signal:** plano (6.3k stars, +250/mo) shows agent-routing is next wave

**Problem:** We're a tool for users. We should be infrastructure for other tools.

**Thesis:** Become the routing service that powers other AI tools.

### Features

#### 1. Public Routing API
```bash
curl -X POST https://api.llm-router.dev/route \
  -H "Authorization: Bearer $ROUTING_API_KEY" \
  -d '{
    "task": "code generation",
    "complexity": "moderate",
    "context": "Python, async patterns",
    "preferred_models": ["haiku", "sonnet"],
    "budget": 0.01
  }'

# Response:
{
  "model": "haiku",
  "provider": "openai",
  "confidence": 0.87,
  "reason": "Moderate complexity, fits your patterns",
  "estimated_cost": 0.0008,
  "fallback_chain": ["sonnet", "claude/opus"]
}
```

**Implementation:**
- FastAPI + OAuth authentication
- Route incoming task → run classifier → return model
- Track usage per API key
- Pricing: free tier (1k calls/day) + paid (overages)

**User Value (other tools):** "I can use llm-router's intelligence from my own application."

---

#### 2. Agent Chain Routing
Extend routing to multi-step agent chains:

```python
chain = [
  {"step": "research", "task": "gather info on React 19"},
  {"step": "analyze", "task": "compare with Vue 3"},
  {"step": "generate", "task": "write comparison blog post"},
  {"step": "edit", "task": "polish and review"}
]

optimal_chain = await route_chain(chain, budget=0.05, quality_min=0.90)
# → {
#   "research": "haiku",      # simple web search
#   "analyze": "sonnet",      # moderate comparison
#   "generate": "sonnet",     # complex writing
#   "edit": "haiku"           # simple formatting
# }
```

**Implementation:**
- Extend `route()` to handle chains
- Optimize each step independently
- Respect inter-step constraints (quality must stay >threshold)
- Return chain execution plan

**User Value:** "Multi-step agents route efficiently, respecting quality for the whole chain."

---

#### 3. Plugin SDK
Allow community to extend routing with custom rules:

```python
from llm_router import register_task_type, Router

@register_task_type("security_audit")
def route_security_audit(context: RouteContext) -> Route:
    """Custom routing for security audits — always needs Opus."""
    return Route(
        model="opus",
        fallback=["sonnet"],
        reason="Security audits require maximum reasoning"
    )

router = Router()
result = await router.route(
    task="Audit this OAuth implementation",
    task_type="security_audit"
)
# → Always routes to Opus, regardless of cost/complexity
```

**Implementation:**
- Plugin registry in `~/.llm-router/plugins/`
- Load custom task types on startup
- Run local validation before using plugin

**User Value (community):** "I can define routing rules for my domain."

---

#### 4. Marketplace Listing
One-click install on Claude Code, Cursor, VS Code, and Windsurf marketplaces.

**Installation:**
```
Claude Code Marketplace → llm-router v7.0
Click Install → Automatic setup → Done
```

**Implementation:**
- Maintain marketplace manifests for each IDE
- CI/CD publishes to each marketplace on tag
- Docs for each IDE's specific install flow

**User Value:** "Installation is one-click, no terminal needed."

---

### Metrics

| Metric | Target | Rationale |
|--------|--------|-----------|
| API calls/day | 100k+ | Network effect |
| Plugin authors | 50+ | Community ecosystem |
| IDE marketplace listings | 4+ | Multi-platform distribution |
| Adoption velocity | 10x YoY | Platform network effects |

---

### Acceptance Criteria

- [ ] Public API works with OAuth authentication
- [ ] Agent chains route optimally (maintain quality, minimize cost)
- [ ] Plugin SDK extensible without modifying core code
- [ ] Marketplace installs work across all 4 IDEs

---

## Version Summary & Timeline

| Version | Release | Theme | Key Feature | Market Signal |
|---------|---------|-------|-------------|---------------|
| v6.0 | May 2026 | Visible | Live routing HUD | claude-hud (19k) |
| v6.1 | Jun 2026 | Memory | Personal routing profile | claude-mem (59k) |
| v6.2 | Jul 2026 | Quality | Quality Guard + benchmarks | UncommonRoute messaging |
| v6.3 | Aug 2026 | Local First | Ollama dashboard | Ollama +40% growth |
| v6.4 | Sep 2026 | Community | Savings card + badge | Plugin marketplace (1.9k) |
| v7.0 | Oct 2026 | Platform | Public routing API | plano (6.3k) |

---

## Release Checklist (Every Version)

### Before Release
- [ ] Full test suite passes: `uv run pytest tests/ -m "" -q`
- [ ] All versions in sync (pyproject.toml + plugin.json + marketplace.json)
- [ ] README updated with new features + screenshots
- [ ] CHANGELOG.md updated with summary
- [ ] Market intel in `docs/market/YYYY-MM.md`

### Release
- [ ] Commit + push to main
- [ ] Version bump commit
- [ ] PyPI publish: `uv publish`
- [ ] GitHub release with notes
- [ ] Plugin reinstall: `llm-router install`

### Post-Release
- [ ] Verify install works: `pip install --upgrade && llm-router install --check`
- [ ] Update marketplace listings
- [ ] Monitor early bug reports (first 48h)

---

## Competitive Differentiation

### vs NadirClaw (416 stars)
- They: Generic routing to local models
- Us: **IDE-native + personalization + quality proof**
- v6.0 (Visible) + v6.1 (Memory) make us 10x better

### vs RouteLLM (4.8k stars, academic)
- They: Research framework
- Us: **Production tool with hooks + real usage data**
- v6.2 (Quality) proves production superiority

### vs plano (6.3k stars)
- They: Agent orchestration (higher level)
- Us: **Solo dev tooling (lower friction)**
- v7.0 (Platform) lets plano call us, not replace us

---

## Success Metrics (6-month goal)

| Metric | Target | How to Measure |
|--------|--------|----------------|
| Monthly active users | 5k–10k | PyPI downloads + hook telemetry |
| GitHub stars | 500–1k | GitHub API |
| Community profiles shared | 100+ | Marketplace submissions |
| API calls/day (v7.0) | 10k+ | API telemetry |
| Cost savings (aggregate) | $1M+ / year | Anonymized usage data |

---

## Open Questions

1. **Rebranding?** Current working names: Volta, Lyra, Opus (conflict with Claude Opus). User preference?
2. **Team features?** v6.4 assumes solo dev. Should we plan team/org features earlier?
3. **Ollama vs other local?** v6.3 focuses on Ollama. Other local inference (LM Studio, vLLM, Hugging Face)?
4. **Platform pricing?** v7.0 public API. Freemium model? Pricing tiers?

---

**Status:** Ready for Yali approval + roadshow to community

**Next:** Implement v6.0 (May 2026 target), measure market response, adjust v6.1+ accordingly.
