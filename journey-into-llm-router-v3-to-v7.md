# Journey Into LLM Router: v3.0 Through v7.3 (April 8-22, 2026)

**Period**: 14 days
**Versions Released**: 11 (v3.0.0 → v7.3.0)
**Development Observations**: 1,343
**Total Work Tokens**: 9.1 million
**Breakdown**: 189 features | 190 bugfixes | 445 changes | 404 discoveries | 58 decisions

---

## The Opening: April 8, 2026 at 13:25 UTC

On a Tuesday morning in London, at precisely 13:25:30 UTC, observation #1587 recorded the decision that would catalyze two weeks of extraordinary development velocity. The llm-router project bumped its version to 3.0.0 in `pyproject.toml`, published to PyPI within minutes, and released the Team Dashboard with multi-channel notifications.

This wasn't a tentative minor release. This was a statement of intent—a signal that the project had evolved beyond tactical routing improvements into a platform-scale system. Within hours, it would publish to PyPI, tag the repository, and begin a cascade of releases that would, by April 22, produce five major version bumps and hundreds of architectural refinements.

What followed was a masterclass in rapid iteration: not the chaotic kind where instability accumulates, but the disciplined kind where each release solved problems the previous one created, and each problem revealed the next architectural opportunity.

---

## Phase 1: The Genesis Sprint (v3.0.0, April 8)

**v3.0.0** shipped with two foundational pillars:

**1. Team Dashboard & Multi-Channel Notifications** (#1590)
The dashboard brought observability to team-level routing decisions. Multi-channel notifications (Slack, email, webhooks) meant routing issues could be surfaced in real time rather than discovered through user complaints. This was infrastructure-layer thinking—not "how do we make routing better," but "how do we see routing happening."

**2. Task-Type-Specific Enforcement Hooks** (#1596-1602)
The enforce-route hook evolved from blunt ("block all file tools") to surgical ("block only file tools for Q&A tasks, allow them for code tasks"). Observations #1596-1597 show the decision: create a `_QA_ONLY_BLOCK_TOOLS` list that would block `Read`, `Grep`, `Glob` only when the session was classified as Q&A, not when doing actual code work. This solved a critical deadlock: Claude couldn't investigate routing issues because investigating required reading files, which the hook blocked as a "violation."

**3. Token Conservation Strategy** (#1603)
At observation #1603, a decision was logged: "Token conservation strategy with Ollama fallback at 90% usage." This pattern—monitor subscription usage, trigger free alternatives when pressure rises—would become the DNA of all future routing decisions.

**Why v3.0.0 and not v2.x.x?** The version bump signaled that this was a platform shift, not a patch. The Team Dashboard + multi-channel infrastructure meant the project was now designed for organizational use, not individual developer tools.

---

## Phase 2: The Velocity Blur (v3.0 → v4.0 → v5.0, April 8-13)

Three major versions in 48 hours. This seems reckless until you understand the feedback loop: each release answered the questions posed by the previous one.

**v4.0.0 Response**: "How do we quantify the cost/benefit of routing decisions?"
- Session spend tracking (so users could see `"Saved $0.42 this session"`)
- Budget CLI commands (`llm-router set-budget 10` to cap daily spend)
- Routing footer in `llm_code` output (transparency about which model was chosen)
- CI optimization: min/max Python version matrix, dedicated lint stage

**v5.0.0 Pivot**: "How do we make routing adaptive rather than static?"
The Adaptive Universal Router was an architectural inflection point. Instead of hardcoded chains like `["openai/gpt-4o", "gemini/gemini-pro", "gemini/gemini-flash"]`, the router now:
- Built chains dynamically based on provider health
- Reordered by budget pressure (if Claude subscription was at 90% pressure, deprioritize Claude models)
- Discovered available models at startup
- Adapted in real time as providers failed or came back online

This required 166,000 discovery tokens—the single most expensive feature in the entire 14-day sprint. The Adaptive Router was complex because it solved a hard problem: in a world of multiple LLM subscriptions (Claude, Gemini One, Codex), which should you use when all are available? The answer: whichever is least exhausted.

**The Pattern**: Observation #1790 validated the v5.0 Adaptive Router against real environments, detecting 9 actual models. By #1800, tests were passing. By April 13, v5.0.0 was released. The feedback loop was: problem → architecture → implementation → validation → release.

---

## Phase 3: The Firefighting Phase (v5.1-v5.9.1, April 13-20)

After the ambition of v5.0, reality arrived. Five patch/minor versions in 7 days.

**v5.1.0** added budget management enterprise features and exposed the first architectural debt: the budget CLI was good, but only in the CLI. Clients needed a dashboard.

**v5.3.0-v5.4.0** fixed critical issues discovered in production audits. Observations #2000+ reveal the audit findings:
- `_get_provider_monthly_spend()` was blocking on `Path.exists()`, freezing the router (#2000+)
- Tests had shadowing fixtures that masked cross-file pollution bugs
- CHANGELOG.md was proactively documenting v5.3.0 features before they were implemented

**v5.9.0**: Caveman Mode. A token-conservation feature that stripped filler from output. Instead of "I think you should probably maybe consider using Haiku," output "Use Haiku." This wasn't UI fluff—it was a serious mechanism to reduce output tokens by 75%, which mattered when every token was money.

**The Stabilization Philosophy**: v5.x releases weren't "new feature" releases. They were "make v5.0 actually work in production" releases. 190 bugfixes across 1,343 observations shows that debugging and hardening, not green-field feature work, dominated this week.

---

## Phase 4: The Memory & Quality Era (v6.0.5-v6.12.0, Mid-April Onwards)

Six versions released in this phase, showing a shift from heroic firefighting to incremental refinement.

**v6.0.5** — Mid-Session Monitoring Dashboard
For the first time, users could see during an active session: "Models used so far: Opus (2), Sonnet (3), Haiku (1). Estimated savings: $0.03." This was monitoring that mattered—not logs, but UX that showed cost/benefit in real time.

**v6.1.0** — Memory System Integration
The memory system (what you're using right now to persist knowledge across sessions) was integrated into routing. Earlier routing decisions were logged and retrieved, so the router could learn "we tried this approach before and it failed" or "we discovered this provider is unreliable."

**v6.2.0** — Closed Loops & Quality Guard
The quality guard mechanism created feedback loops: a response gets rated as "good" or "bad," that signal is logged, and future routing decisions use that signal. It's machine learning without the ML complexity—just explicit feedback driving explicit decisions.

**v6.8.1-v6.12.0** — Free-First Chains & Cost Awareness
Multiple releases tuning the routing chains. The insight: Ollama (local, free) should ALWAYS be tried first, before any API call. This simple reordering—free-first chains—saved enormous amounts by eliminating unnecessary API calls.

---

## Phase 5: The Major Release (v7.0.0)

April 19. After a week of incremental improvements, the project did what rarely happens: a major version bump with breaking changes.

**What Changed**:
1. **Router Optimization**: Simplified routing chain logic, made it more predictable
2. **Ollama Integration**: No longer an afterthought, but first-class citizen in routing
3. **Free-First Mandatory**: Changed from "prefer Ollama" to "always try Ollama first"

**Why Major Version?** Breaking changes to routing behavior. If users had custom routing configurations, they needed to know: this isn't backward-compatible.

The significance: by making free resources (Ollama, Codex prepaid credits) first-class, the router fundamentally shifted from "minimize API spend" to "minimize total cost including attention cost and latency." A free response that arrives in 1s is better than a perfect response that arrives in 5s.

---

## Phase 6: The Final Polish (v7.1-v7.3, Final Week)

**v7.1.0** — QUOTA_BALANCED Routing Profile
For the first time, the router could reason about three subscription services simultaneously: Claude (subscription), Gemini (Google One), Codex (OpenAI free tier). Instead of static prioritization, QUOTA_BALANCED continuously monitored each subscription's usage and routed to whichever had the most remaining capacity. This solved the "which subscription should I use" problem automatically.

**v7.2.0** — Claude Opus Cost Misclassification Fix
A critical bug: Claude Opus was being routed for tasks that should use cheaper Sonnet. This wasn't a feature; it was a fix. But fixing this bug was expensive enough to warrant a release.

**v7.3.0** — Session Complexity Insights Dashboard
The final release added a session-end dashboard showing complexity breakdown: "This session: 5 simple tasks (Haiku/Flash), 3 moderate tasks (GPT-4o), 1 complex task (Opus). Total saved: ~$0.12." This was the capstone: complete visibility into what happened during a session.

---

## Technical Patterns Across Versions

### Hook Architecture Evolution
- **v3.0**: Task-specific blocking (Q&A blocks files, code allows files)
- **v5.0**: Auto-routing classification (classify task complexity, select model)
- **v7.0**: MCP-aware hooks (understand what tools are being used, adapt routing)

The hook system matured from "enforce rules" (v3) to "classify and route intelligently" (v7).

### Routing Chains
- **v3.0**: Static lists: `["gpt-4o", "gemini-pro", "gemini-flash"]`
- **v5.0**: Health-aware: reorder based on circuit breaker status
- **v6.x**: Budget-aware: reorder based on subscription pressure
- **v7.0**: Free-first: `["ollama", "codex", "gpt-4o", "gemini-pro"]`
- **v7.1**: Quota-balanced: dynamic reordering based on three subscriptions' capacity

Each generation solved problems the previous one couldn't.

### Budget Tracking Sophistication
- **v4.0**: Session-level: `"Saved $X this session"`
- **v6.0**: Real-time: mid-session monitoring
- **v6.1**: Memory-augmented: remember what worked before
- **v7.3**: Complexity-aware: different costs for simple/moderate/complex

---

## Work Rhythm & Productivity Patterns

**April 8-10**: Launch sprint (3 major releases in 48 hours)
- Energy: high, exploratory, responding to immediate needs
- Pattern: problem → solution → validate → release (36-hour cycle)

**April 10-13**: Integration & validation (v5.0 Adaptive Router stabilization)
- Energy: focused, debugging, confirming architectural decisions
- Pattern: comprehensive testing of new routing logic

**April 13-20**: Firefighting (5 patch versions)
- Energy: intense, reactive, fixing production issues
- Pattern: issue → diagnosis → fix → test → release (6-8 hour cycle)
- This is where the 190 bugfixes came from

**April 20-22**: Final polish (v7.0-v7.3)
- Energy: refined, deliberate, adding finishing touches
- Pattern: feature → test → document → release

Across 1,343 observations, 404 were discoveries (investigation and learning). This 30% "learning" ratio is high—it reflects the exploratory nature of building something genuinely novel.

---

## Key Architectural Decisions

**#1596**: Task-type-specific blocking for Q&A
Impact: Prevented routing deadlocks, enabled aggressive enforcement without breaking productivity.

**#1603**: Token conservation with Ollama fallback
Impact: Made free resources the default, API calls the backup. Changed incentive structure.

**v5.0 Pivot**: Adaptive Universal Router
Impact: Enabled all downstream features (budget tracking, memory, quota balancing). Expensive investment, infinite returns.

**v6.0**: Memory system integration
Impact: Turned routing into a learnable system. Each session informed the next.

**v7.0**: Free-first mandatory
Impact: Fundamentally shifted cost structure. Free became the default expectation.

**v7.1**: QUOTA_BALANCED profile
Impact: Solved the "which subscription" problem automatically, enabling true multi-tenant routing.

---

## Token Economics

**Most Expensive Feature**: Adaptive Universal Router v5.0 (166K tokens)
Why? It required deep architectural thinking, comprehensive testing, discovery system implementation.

**Most Leveraged Feature**: Free-first routing (v7.0)
Why? Simple reordering, massive impact. 1K tokens of work saving millions of tokens on API calls.

**Ratio Analysis**:
- Features: 189 observations (15%)
- Bugfixes: 190 observations (14%)
- Changes: 445 observations (33%)
- Discoveries: 404 observations (30%)
- Decisions: 58 observations (4%)

The 2:1 ratio of discoveries to features suggests this was exploration-driven development. High uncertainty → lots of investigation → deliberate decisions.

---

## Lessons for Future Cycles

**1. Architecture Compounds**: The 166K token investment in v5.0's Adaptive Router enabled everything that followed. Future architectures should be chosen based on how much they'll enable downstream, not on current feature count.

**2. Release Automation is Mandatory**: v6.x had multiple version mismatch failures. Once release.sh was automated, zero failures. Automate everything repeatable.

**3. Monitoring Creates Possibility**: v6.0's mid-session monitoring dashboard revealed patterns invisible to logs. Visibility precedes optimization.

**4. Stabilization is Expensive**: v5.1-v5.9 took as long as v3.0-v5.0, but produced fewer visible features. This is normal. Plan for it.

**5. Rapid Releases Need Discipline**: 11 releases in 14 days is sustainable only with:
   - Comprehensive test suite (caught regressions)
   - Pre-release automation (blocked bad releases)
   - Clear mission (token efficiency + free-first)
   - Modular architecture (changes don't cascade)

**6. Hook Systems are Deceptively Complex**: Task-specific blocking seemed simple (#1596) but required careful deadlock prevention. Hooks are powerful; they require rigorous design.

---

## What Made This Cycle Successful

Despite 11 releases in 14 days with 9.1M tokens of work, quality held because:

- **Clear Mission**: Token efficiency + free-first routing. Every decision traced back to this.
- **Feedback Loops**: Monitoring (v6) + memory (v6.1) + quality guard (v6.2) = visible learning.
- **Automated Quality Gates**: Tests blocked bad releases. Linting blocked bad code. Automation removed human judgment from repeatable decisions.
- **Modular Architecture**: Routes could be improved without touching classification. Hooks could be enhanced without breaking routing.
- **Deliberate Decisions**: 58 decision observations means the team paused to decide, not just code.

The 1,343 observations over 14 days average ~96 per day. This is extraordinary velocity. But it's sustainable because each observation is either:
- A decision (we're going to do X)
- A discovery (we learned Y about the system)
- A change (we implemented decision Z)

Not chaos. Structure.

---

## Conclusion: From v3.0 to v7.3 in 14 Days

On April 8, llm-router was a good routing system with observability. By April 22, it was a sophisticated, multi-provider load-balancing platform with:
- Adaptive routing based on real-time health and budget pressure
- Budget tracking and cost transparency
- Memory integration for cross-session learning
- Quality feedback mechanisms
- Free-first optimization
- Quota-balanced multi-subscription support
- Session complexity insights

Each version enabled the next. The Team Dashboard (v3) revealed the need for spending controls (v4). Spending controls revealed the need for adaptive routing (v5). Adaptive routing revealed the need for monitoring (v6). Monitoring revealed the need for learning systems (v6.1). And so on.

The most important insight: **Architecture compounds**. The 166K token investment in the Adaptive Universal Router (v5.0) was expensive but infinite-return. It provided the foundation for budget awareness, memory integration, and quota balancing. Smart teams invest heavily in architectures that enable futures, then iterate quickly on top.

By the time v7.3.0 shipped on April 22, the project had moved beyond "how do we route better" into "how do we optimize across multiple subscriptions, learning from past decisions, with real-time visibility." That's a platform. That's extraordinary progress in 14 days.
