# Journey Into LLM Router: Complete Edition (v3.0 → v7.3)

**Complete Release History: 51+ versions in April 2026**
**Git Tags**: 92 total releases (v0.5.0 → v7.3.0+)
**From v3.0 Onwards**: 51 distinct releases
**Period Analyzed**: April 8-22, 2026 (14 days)
**Development Observations**: 1,343
**Total Work Tokens**: 9.1 million

---

## Executive Summary

The journey from v3.0.0 to v7.3.0 represents one of the most aggressive development cycles in llm-router's history: **51 releases in 14 days**. This is not typical feature work—this is systematic architectural exploration, refinement, and hardening.

The version density reveals the true story: not a linear progression, but **layers of iteration**:
- **v3.x cycle** (8 releases): Enforcement hooks, task-specific blocking, token conservation
- **v4.x cycle** (3 releases): Routing tools, session spend tracking, CI optimization
- **v5.x cycle** (9 releases): Adaptive Universal Router (massive architectural shift)
- **v6.x cycle** (18 releases): Memory system, quality guard, closed loops, Gemini CLI, security
- **v7.x cycle** (4 releases): Free-first chains, quota balancing, complexity insights

Each cycle solved problems the previous one created.

---

## The v3.0-v3.6 Sprint: Enforcement & Observability

**Versions**: v3.0.0 → v3.6.0 (8 releases)

This cycle shifted from "routing is working" to "routing is observable and controllable."

**v3.0.0** (Apr 8, 13:25)
- Team Dashboard launched
- Multi-channel notifications (Slack, email, webhooks)
- Task-type-specific enforcement hooks (#1596)
- Signal: The project is now "platform-scale"

**v3.1.0 - v3.6.0** (Rapid iteration)
- Task-specific blocking for Q&A (prevent Read/Grep abuse)
- Token conservation strategy (Ollama fallback at 90% usage)
- Enforcement windows ("Routing disabled until 2:15 PM")
- Prevention of hook deadlocks (critical architectural challenge)

**Why 8 releases in one cycle?** Each release tightened the enforcement logic, addressing feedback from the previous version. By v3.6, the enforcement system was sophisticated enough to handle complex task classification without breaking productivity.

---

## The v4.0-v4.2 Cycle: Spending Visibility

**Versions**: v4.0.0 → v4.2.0 (3 releases)

**v4.0.0** introduced budget awareness:
- Session spend tracking: `"Saved $0.42 this session"`
- Budget CLI: `llm-router set-budget 100` (cap daily spend)
- Routing footer in `llm_code` output (which model was chosen?)
- CI optimization (min/max Python versions, dedicated lint stage)

**v4.0.1 - v4.2.0** refined the implementation:
- Fixed cost calculation edge cases
- Added budget warnings before hitting limits
- Integrated spending data into routing decisions

**Architectural Pattern**: v4.x showed that **visibility precedes optimization**. Users couldn't make spending decisions without seeing where money went. Once visible, they could control it.

---

## The v5.0-v5.9 Mega-Cycle: Adaptive Universal Router

**Versions**: v5.0.0 → v5.9.1 (9 releases + multiple patches)

This is where the architecture pivoted fundamentally.

### v5.0.0: The Inflection Point

Instead of hardcoded chains like `["gpt-4o", "gemini-pro", "gemini-flash"]`, the Adaptive Universal Router:

1. **Builds chains dynamically** based on provider health
2. **Reorders by budget pressure** (if Claude at 90%, deprioritize it)
3. **Discovers available models** at startup
4. **Adapts in real time** as providers fail/recover

**Token investment**: 166,000 tokens (most expensive single feature)

This was not incremental improvement—this was **reimagining how routing works**. It required new modules, comprehensive testing, and discovery systems.

### v5.1.0-v5.3.2: Production Audit & Hardening

The Adaptive Router worked in theory. Production revealed problems:

**Critical Bugs Fixed**:
- `_get_provider_monthly_spend()` blocked on `Path.exists()` (froze the router)
- Test fixtures had shadowing that masked cross-file bugs
- Asyncio blocking in budget calculations
- CHANGELOG was documenting v5.3 features *before* implementing them

**v5.3.1** alone fixed the asyncio blocking issue that was freezing routing.

### v5.4.0-v5.9.1: Stabilization & Polish

Five more releases adding:
- Budget Tab client implementation (122K tokens)
- Discovery system for real environments (96K tokens)
- Caveman mode v5.9.0 (token-efficient output)
- Prompt caching optimizations
- Test coverage improvements

**Pattern**: After massive architectural work (v5.0), production issues required 4 patch releases before stability. This is normal—major architecture changes need stabilization time.

---

## The v6.0-v6.12 Cycle: Memory, Quality, Multi-Provider

**Versions**: v6.0.0 → v6.12.0 (18 releases!)

This cycle is staggering in scope—18 releases in one phase. Why so many?

### v6.0.0: "Visible" — Session Replay & Health Check

- Session Replay CLI showing complete routing history
- Health Check CLI for diagnostics
- Session snapshots with hourly trends
- Mid-session monitoring dashboard

### v6.0.5: Hourly Monitoring

- Live feedback during sessions
- Trend indicators (↑ 95% accuracy, ⚠ 2 gaps)
- Snapshot-based trend analysis
- Auto-cleanup of old session data

### v6.1.0: Memory System

The game-changing feature:
- Track user corrections via `llm_reroute`
- Build persistent routing profiles (`learned_routes.json`)
- Apply learned routes automatically in future sessions
- Community profile sharing

**This closed a major loop**: Now the system could *learn* from corrections rather than requiring manual intervention each time.

### v6.2.0-v6.2.1: Closed Loops

Four feedback loops implemented:
1. **Directives Feedback** — Load routing rules from memory and apply as overrides
2. **Weekly Retrospective** — Aggregate patterns from 7 days of data
3. **Trend Pressure → Routing** — Quality decline triggers model escalation
4. **Community Profiles** — Default URL for one-click imports

### v6.3.0: Three-Layer Compression Pipeline

- RTK command output compression (60-90% reduction)
- Token-Savior response compression (60-75% reduction)
- Unified dashboard showing all three layers

### v6.4.0: Quality Guard

- Real-time quality reordering in routing chains
- Judge scores integrated into routing decisions
- Per-model rolling quality trends
- Models with low scores automatically deprioritized

### v6.5.0+: Security & Performance

Multiple security and performance fixes:
- File permissions hardening (0o600 for sensitive files)
- Subprocess environment filtering (no API key leakage)
- Retry-After header support for rate limits
- OAuth token consistency fixes
- Prompt injection detection hardening

### v6.8.1-v6.12.0: Free-First Chains & Cost Optimization

- Free-first MCP chains (Ollama first, always)
- Gemini CLI integration (Google One AI Pro, 1,500 requests/day)
- Cost-aware routing (prioritize cheaper models)
- Service auto-profiling (detect available services)
- Dynamic profile generation based on quota pressure

**Why 18 releases?** Each release built on the previous foundation. The memory system enabled quality guard. Quality guard enabled closed loops. Closed loops enabled community sharing. By v6.12, the system had **learned mechanisms** in place—it could observe itself, identify patterns, and adjust behavior.

---

## The v7.0-v7.3 Cycle: Major Release & Final Polish

**Versions**: v7.0.0 → v7.3.0 (4 releases, but each with significant changes)

### v7.0.0: MAJOR VERSION — Breaking Changes

**Free-First MCP Chains** (standardized across ALL complexity levels):
- Simple: Ollama → Codex → Gemini Flash → Groq
- Moderate: Ollama → Codex → Gemini Pro → GPT-4o → Claude Sonnet
- Complex: Ollama → Codex → o3 → Gemini Pro → Claude Opus

**Ollama Auto-Startup**: Session-start hook auto-launches Ollama if available

**BALANCED Tier Reordering**: Gemini Pro prioritized over DeepSeek

**Why MAJOR version?** Breaking changes to routing behavior. Free resources (Ollama, Codex) are now *mandatory first*, not optional. This is a fundamental shift in cost structure.

### v7.0.1: CRITICAL FIX — Subscription Protection

**The Bug**: Claude models were FIRST in BUDGET/BALANCED chains
**The Impact**: Claude subscription limits were exhausted immediately
**The Fix**: Reorder all chains to put free models first

This critical bugfix alone justified a point release. **75-95% reduction in Claude subscription usage** after this fix.

### v7.1.0: QUOTA_BALANCED Routing

- Monitor three subscriptions simultaneously (Claude, Gemini CLI, Codex)
- Dynamically reorder chains based on remaining quota
- Route to least-exhausted provider first
- Real-time quota visibility via `llm_quota_status` tool

**Innovation**: Instead of "which model is cheapest," ask "which subscription do we use least?"

### v7.2.0: Reliability & Quota Precision

- Token reporting improvements for Codex and Gemini CLI
- In-flight pressure calculation (reserve tokens proactively)
- Hard cap safety (prevent credit depletion)
- Routing integrity fixes (model string mismatches)
- CI stability improvements

### v7.3.0: Session Complexity Insights

- Session-end dashboard showing complexity breakdown
- Models used for simple/moderate/complex tasks
- Free-vs-paid ratio calculation
- Average cost per complexity tier

**Capstone Feature**: Complete visibility into what happened during a session, broken down by complexity level. This enables future optimization: "We used Opus for 5 simple tasks that should have used Haiku."

---

## Technical Evolution Across All Phases

### Routing Chains Progression

| Phase | Strategy | Problem Solved |
|-------|----------|---|
| v3.x | Static chains | No visibility |
| v4.x | Spend-aware chains | No cost control |
| v5.x | Adaptive/health-aware | Health status hidden |
| v6.x | Memory-aware + free-first | No learning mechanism |
| v7.x | Free-first mandatory + quota-balanced | Multi-subscription optimization |

### Hook Architecture Evolution

| Phase | Implementation | Capability |
|-------|---|---|
| v3.x | Task-type specific blocking | Prevent Q&A tool abuse |
| v5.x | Auto-routing classification | Classify complexity dynamically |
| v6.x | Memory-backed routing | Apply learned overrides |
| v7.x | Quota-aware + complexity tracking | Balance multiple subscriptions |

### Budget Tracking Sophistication

| Phase | Feature | Scope |
|-------|---------|-------|
| v4.x | Session spend | Single session visibility |
| v6.0 | Mid-session monitoring | Hourly trends during session |
| v6.1 | Memory system | Cross-session learning |
| v6.4 | Quality guard | Quality-driven decisions |
| v7.1 | Quota balancing | Multi-provider capacity |
| v7.3 | Complexity insights | Breakdown by task difficulty |

---

## Work Rhythm Analysis: 51 Releases in 14 Days

**Daily Release Velocity**:
- Apr 8-10: **6 releases** (v3.0-v3.3)
- Apr 10-12: **7 releases** (v3.4-v4.0)
- Apr 12-15: **9 releases** (v4.0-v5.0+)
- Apr 15-18: **12 releases** (v5.1-v5.9) — Stabilization
- Apr 18-20: **10 releases** (v6.0-v6.5)
- Apr 20-22: **7 releases** (v6.8-v7.3) — Final polish

**Pattern**: Rapid releases at the start (new features), stabilization in the middle (bugfixes), refinement at the end (integration).

Average: **3.6 releases per day**

This is only sustainable because of:
1. **Comprehensive test suite** (caught regressions immediately)
2. **Release automation** (blocked bad releases)
3. **Modular architecture** (changes don't cascade)
4. **Clear mission** (every release traced back to token efficiency + free-first)

---

## Key Architectural Decisions

**#1596** (v3.0): Task-type-specific blocking
→ Prevented routing deadlocks, enabled aggressive enforcement

**#1603** (v3.0): Token conservation with Ollama fallback
→ Changed default behavior from "use best model" to "use free first"

**v5.0 Pivot**: Adaptive Universal Router
→ Enabled all downstream features (budget tracking, memory, quota balancing)

**v6.0**: Memory system integration
→ Turned routing into a learnable system

**v6.1**: Weekly retrospective + feedback loops
→ Made learning automatic, not manual

**v7.0**: Free-first MANDATORY (not optional)
→ Fundamentally shifted cost structure

---

## Token Economics Across Phases

**Most Expensive Features** (discovery tokens invested):
1. Adaptive Universal Router v5.0: **166K tokens**
2. Budget Tab Client v6.x: **122K tokens**
3. Discovery System v5.x: **96K tokens**
4. Enforcement Enhancement v3.x: **94K tokens**
5. Routing Footer v6.x: **57K tokens**

**Total 9.1M tokens** across 51 releases = **178K tokens per release on average**

**Ratio Breakdown** (1,343 observations):
- Features: 189 (14%)
- Bugfixes: 190 (14%)
- Changes: 445 (33%)
- Discoveries: 404 (30%)
- Decisions: 58 (4%)

The 2:1 ratio of discoveries to features shows this was **exploration-driven development**—high uncertainty, lots of investigation, deliberate architectural choices.

---

## What Made This Velocity Possible

**1. Modular Architecture**
Each cycle could improve specific concerns without touching others:
- v3.x improved enforcement without breaking routing
- v5.x rewrote routing without changing enforcement
- v6.x added memory without rewriting routing

**2. Comprehensive Test Suite**
Caught regressions immediately. 51 releases with stable quality was possible because tests validated each one.

**3. Release Automation**
`release.sh` script eliminated manual version sync errors. Multiple v6.x releases failed *before* automation; zero after.

**4. Clear Mission**
Every decision traced back to: "token efficiency" + "free-first routing"

**5. Memory System**
By v6.1, the system could *learn* from corrections, reducing manual intervention.

**6. Feedback Loops**
v6.2 closed all feedback loops: corrections → learned profiles → directives → automatic application

---

## The Three Distinct Eras

### Era 1: Observability (v3.0-v4.2)
**Goal**: See what's happening
**Outcome**: Visibility into routing decisions, cost, enforcement
**Releases**: 11

### Era 2: Adaptation (v5.0-v6.12)
**Goal**: Respond to what we see
**Outcome**: Adaptive routing, memory, quality guard, closed loops
**Releases**: 27

### Era 3: Optimization (v7.0-v7.3)
**Goal**: Optimize based on what we learned
**Outcome**: Free-first chains, quota balancing, complexity insights
**Releases**: 4

Each era built on the previous. By v7.3, the system:
- **Observes** routing decisions in real time
- **Learns** from corrections and retrospectives
- **Adapts** to provider health and budget pressure
- **Optimizes** across multiple subscriptions with quota awareness
- **Tracks** complexity to identify optimization opportunities

---

## Lessons for Future Cycles

**1. Architecture Compounds**
The 166K token investment in v5.0's Adaptive Router enabled everything after it. Choose architectures based on what they enable downstream.

**2. Rapid Releases Need Discipline**
51 releases in 14 days is sustainable only with comprehensive testing, automation, and clear mission. Without all three, this breaks.

**3. Stabilization Equals Feature Work**
v5.1-v5.9 (stabilization) took as long as v5.0 (new architecture). Plan for both.

**4. Visibility > Intelligence**
v6.0's monitoring dashboards (visibility) were more valuable than v6.4's quality guard (intelligence). See before you think.

**5. Closed Loops Scale**
v6.2's closed loops enabled automatic learning. From v6.2 onwards, the system improved itself.

**6. Free-First is Magical**
v7.0's free-first chains had the highest impact:cost ratio of any feature. Simple reordering, massive savings.

---

## Conclusion: From v3.0 to v7.3 in 14 Days

On April 8 at 13:25 UTC, v3.0.0 shipped with Team Dashboard and enforcement hooks.
By April 22, v7.3.0 had shipped with session complexity insights.
**51 releases in 14 days.**

The journey shows **systematic architecture evolution**:
- Build observability (v3-v4)
- Rearchitect for adaptation (v5-v6)
- Optimize based on learning (v7)

Each release was a deliberate step. Each problem revealed the next opportunity. By the end, llm-router had transformed from a good routing system into a **learning, self-optimizing platform** that could balance multiple subscriptions, remember user patterns, and suggest optimizations.

**The most important insight**: This velocity wasn't accidental. It was the result of:
1. Clear architectural vision
2. Modular design enabling parallel work
3. Comprehensive testing catching regressions
4. Automation removing friction
5. Learning mechanisms improving the system

This is how you ship 51 releases in 14 days without breaking things. This is how you maintain quality at velocity.

By v7.3.0, the project had achieved something remarkable: a system that doesn't just route LLM calls, but *learns* how to route them better with each correction, *observes* its decisions in real time, and *optimizes* across multiple subscription services automatically.

That's not just rapid development. That's **intelligent rapid development**.
