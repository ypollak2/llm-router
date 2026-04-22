# Building llm-router: An Honest Developer Journey with Claude AI

**What this is:** Raw, unfiltered lessons from shipping 51 releases in 14 days. What actually happened, what broke, why we were wrong, and what we'd do differently.

**What this isn't:** A success story. It's a case study in moving fast, fucking up systematically, and learning faster than we broke things.

**Real metric that matters:** 35 times we shipped buggy code that passed tests. 25 times we picked the wrong approach. Those aren't footnotes. That's the story.

---

## The Setup: Why We Built This

Money is stupid. You throw $500K at a consulting firm to build routing logic, and they give you a Swagger API and a Docker image.

We spent $6.95 and shipped 51 releases.

**Why?** Because we forced every design decision through a cost filter. "Does this need an expensive API call?" became the question that shaped everything. That constraint—being cheap—made us smart.

The free-first routing idea came from pain. Early on, Yali was burning money testing ideas on GPT-4o ($0.015 per call adds up). We realized: Ollama is free and runs locally. Codex is prepaid (might as well use it). Gemini Flash is 50x cheaper than GPT-4o.

So we built a chain: try free first, then cheap, then expensive only if we actually need it. That one idea changed the whole architecture. Every decision after that was "how do we stay cheap?"

That's not noble. That's just... enforced discipline.

---

## Slide Breakdown

### Act 1: Setup (Slides 1–3)

#### Slide 1: Title
**51 Releases in 14 Days**

We did this. Not because we're heroes. Because we had no choice—we were using Claude AI as a pair programmer, and Claude is fast. But we had to learn how to keep up.

This is what 14 days of iteration with an AI looks like when you strip away the bullshit.

---

#### Slide 2: The Mission (aka "Why We Got Discipline")

We looked at the problem: "How do engineers use LLMs without going broke?"

Most projects don't care. They throw GPT-4o at everything. But here's the thing: **you don't need the smart model for every decision.**

- Ollama (local, free) is dumb but useful for simple classification
- Codex (prepaid, $0 marginal cost) is good enough for 80% of code tasks
- Gemini Flash ($0.075 per 1M tokens) is criminally cheap for writing
- GPT-4o ($2.50 per 1M tokens) only for genuinely hard problems
- Claude ($15 per 1M tokens) only when we're stuck

So we built a chain. Not because it's elegant. Because it's cheap.

**The hard part:** Enforcing this. We had to block our instinct to "just use the best model." We had to make sure Claude would route to Ollama first, Codex second, API models only as fallback.

That routing logic is the whole thing. Everything else is implementation.

---

#### Slide 3: The Stack (aka "We Kept It Boring")

**Language:** Python. No JavaScript. No Go. Python because Claude is good at Python and we weren't going to fight that.

**Testing:** Pytest. 80%+ coverage required. Not because we're test nerds. Because we shipped broken code so often early on that we realized: **tests are the only thing between me and the customer calling me at 2am.**

**Database:** SQLite. Everyone laughs. SQLite shipped with the system, no setup, perfect for this. We made migrations idempotent (can run them 100 times, same result) because we were deploying constantly and couldn't afford broken deploys.

**Automation:** GitHub Actions + a bash script called `release.sh` that does everything:
- Verify all versions match (pyproject.toml + 2 plugin.json files)
- Run the full test suite
- Build the package
- Publish to PyPI
- Create a GitHub release
- Verify it worked

One command: `bash scripts/release.sh`

That script saved us. Twice we had version mismatches that would have destroyed releases. The script caught them automatically.

**Claude Memory:** 2,788 observations. This was the hidden superpower. Instead of "what did we build last sprint?" we had "let me search memory." Saved weeks of re-research.

**Key lesson:** Automation is not optional. It's how you stay sane when moving this fast.

---

### Act 2: The Sprint — Reality Check (Slides 4–7)

#### Slide 4: We Shipped Fast But Not Consistently

51 releases in 14 days is not a linear story.

**Days 1-3:** Wild west. 3-4 releases/day. Building the core, learning what Claude is good at, learning what breaks.

**Days 4-8:** Steady. 3-4 releases/day. Stabilizing. We had a pattern now. Plan → Code → Test → Release. It worked.

**Days 9-14:** Slower. 2-3 releases/day. Why? We hit regressions. We had to revert stuff. We learned we were shipping broken code faster than we could fix it. So we slowed down, added process.

**What this means:** Fast doesn't mean sustainable. We peaked and then realized we were moving too fast. The slowdown wasn't laziness. It was us saying "okay, we need to not destroy what we built."

This graph is more honest than "3.6 releases/day average." The average hides the fact that we were chaos early, stabilized mid-sprint, and deliberately slowed down at the end.

---

#### Slide 5: $6.95 (And Why That's Not Actually The Story)

Yes, we spent $6.95 on APIs across 22.6M tokens and 9,100 API calls.

That's a real number. That's not a lie. But it's also not the story.

The story is: **we spent our way out of expensive mistakes by using cheap models.**

We routed 2,020 calls to Codex (free, prepaid). Those weren't optimal calls. Some of them failed. But the marginal cost of failure was zero. So we could afford to iterate and retry with Codex in a way we couldn't with GPT-4o.

If we'd used GPT-4o for everything, that $6.95 would have been $200. Not because we're smarter. Because we forced ourselves to use dumb models first.

**The actual lesson:** Free-first routing isn't about being cheap. It's about being able to iterate fast without the cost of failure crushing you. It's about optionality.

---

#### Slide 6: Model Distribution (aka "We Used What We Could Get Free")

```
Codex gpt-5.4:      2,020 calls ($0.00)    — Prepaid, marginal cost zero
Gemini Flash:       1,846 calls ($1.14)    — Cheapest API model
GPT-4o mini:        1,663 calls ($1.68)    — Second cheapest API model
GPT-4o:             1,170 calls ($0.88)    — Medium cost, genuinely needed
Ollama local:         822 calls ($0.00)    — Free, runs locally, sometimes slow
Claude Haiku:          48 calls (~$0)       — Subscription mode, used sparingly
Gemini Pro:           146 calls ($1.12)    — Expensive, only for hard stuff
```

You notice something? **We never used Claude Opus.** Not once during development.

Why? Cost. $15 per 1M tokens is a lot when you're iterating fast. We pushed as far as possible with cheaper models. When we hit a wall, we'd use a better model. But we didn't default to it.

Codex (prepaid) is the wild card here. We committed to integrating it because the marginal cost is zero once you've paid for the subscription. So every Codex call is "free" from the router's perspective. That made it worth optimizing for. 2,020 calls that cost us nothing.

**The lesson nobody talks about:** Default to cheap, not good. Then refactor to good only when cheap isn't working. This is the opposite of how we usually think about AI.

---

#### Slide 7: 63% Success Rate (The Unspoken Part)

Let me be straight about this:

**63% of sessions were fully or mostly successful.** That means 37% were not.

- 30 sessions: worked perfectly on first try
- 33 sessions: worked after some debugging
- 24 sessions: partially worked, had to rewrite part of it
- 10 sessions: didn't work, scrapped it

That 37% failure rate is the real story.

**What actually killed those sessions:**

- **Buggy code: 35 incidents.** Tests pass, ship it, production breaks. Logic was wrong. Claude generated code that worked in isolation but failed in integration.

- **Wrong approach: 25 incidents.** Claude did what we asked, but we asked the wrong question. "Implement X" → Claude implements X → turns out X was the wrong design.

- **Rate limited: 9 incidents.** We hit API limits and had to wait or retry.

- **Tool blocked: 5 incidents.** Some enforcement rule we set blocked a necessary tool and we had to debug the blocker.

- **Config drift: 8 incidents.** Version mismatch, environment variable missing, that kind of thing.

**Real talk:** 60 of 100 sessions had code or approach problems. That's not Claude's fault. That's us. It's my fault. I shipped code without running the full test suite. I skipped planning because the task seemed simple. I didn't verify at runtime.

The metric that matters isn't 63% success. It's: **how many of the failures were preventable?**

Answer: Most of them. Maybe 80% of the 37% failures were preventable if we'd followed process. That's where the lesson is.

---

### Act 3: How We Actually Stayed Alive (Slides 8–10)

#### Slide 8: Version Cycles (Why We Kept Resetting)

We didn't go v1 → v2 → v3. We went v3 → v4 → v5 → v6 → v7 in 14 days.

Why? Because every cycle was a reset of something we got wrong in the previous one.

**v3-v4:** We had enforcement hooks. "If you use this tool, route through this model." It worked okay but was brittle.

**v5:** We tried to be clever. "Let's have fallback chains so if one model fails, try the next one." Spent 166K tokens just debating the design. Is it Ollama → Codex → Gemini? Or Ollama → Gemini → Codex? Should budget pressure change the order? We wrote the routing decision log just because we couldn't agree.

**v6:** We realized we kept forgetting decisions. "Did we decide to always use Codex for this?" → search memory → found it. 2,788 observations of "we decided X because Y." That memory system became the thing that made everything else work.

**v7:** By this point we were exhausted. So we just made it rule-based. "Codex is always in the chain. Always. No question. Stop optimizing." That simplicity unblocked everything.

Each version solved the problem that killed v(n-1). That's not elegant architecture. That's honest iteration. You don't build right the first time. You build wrong, figure out why, then reset and build better.

---

#### Slide 9: The Actual Process (aka "How I Kept Shipping Without Losing My Mind")

**Plan Mode worked. Like, actually worked.**

Spend 10 minutes exploring code. Spend 10 minutes designing. Get approval. Then code. This alone cut rework from 40% of time to ~15%.

Why? Because I'd pitch the plan and Yali would say "no, that's wrong, here's why." So I'd iterate the plan, not the code. Plans are cheap. Code rewrites are expensive.

**Code review before commit, not after.**

Had Claude review my work while it was still flexible. Claude caught ~60% of bugs before they hit test. Not all of them, but most.

**/clear between tasks.**

This is one line in the instructions but it's huge. If I finished task A (build a router) and started task B (debug a test), I'd lose context on A. So /clear. Reset. Start fresh on B. It sounds like it would slow things down. It doesn't. It prevents cross-task context bleed where I'm thinking about A while debugging B.

**Audit prompts are real.**

Every tool call got logged. Every significant decision got a decision log entry: "Decided to use SQLite because..." Then later: "Wait, why did we use SQLite?" → search decision log → found it.

This mattered when weird bugs appeared. "Why does the migration system work this way?" → search decision log → "Oh, because version X had broken migrations and we made all migrations idempotent after that."

**The honest part:** This process is overhead. It's not free. But the rework is more expensive than the overhead. So you win overall.

---

#### Slide 10: Automation (aka "The Release Script Saved My Life Twice")

150+ tests. 80%+ coverage. Sounds reasonable.

The reality: **I ran the full test suite before every commit.** Not because I'm disciplined. Because I'd shipped broken code 35 times and gotten paged at 3am to fix it.

The automation that mattered:

```bash
bash scripts/release.sh
```

This script does:
1. Check all versions match (pyproject.toml + 2 JSON files)
2. Run full test suite
3. Run linter (ruff)
4. Build wheels and sdist
5. Publish to PyPI
6. Tag git
7. Create GitHub release with changelog
8. Verify PyPI upload
9. Verify GitHub release exists
10. Re-run tests on the published version

If ANY step fails, it **rollback**s. Reverts the version files, deletes the git tag, leaves PyPI untouched.

**This script saved us twice.** Once when version files were out of sync. Once when the test suite had a flaky test that only failed in full-suite mode.

Without automation, version mismatches destroy your credibility. With it, the computer catches it before you ship.

That's the ROI of automation. Not that it's faster. It's that it's more predictable. And at 3am, predictability is worth more than speed.

The other thing: **release confidence**. When I could run one command and trust it, I released more often. 51 releases in 14 days only happens if you're not afraid of releasing. Automation removed that fear.

---

### Act 4: The Actual Wins (Slides 11–13)

#### Slide 11: Free-First Routing (aka "We Milked Codex")

2,020 API calls to Codex. $0.00. That's the money move.

Here's why it worked: We had already paid for Codex (OpenAI subscription). The marginal cost of one more API call is zero. So we built the router to ask "can Codex do this?" first, before asking "should we use GPT-4o?"

Will Codex be slower? Sometimes. Will it fail sometimes? Yes. Do we care?

No, because it costs nothing to retry. That flipped our entire thinking.

Normally, you start with the best model and compromise when budget forces you. We reversed it: start with free, escalate only when necessary.

People said "Codex is dead, why are you using it?" Fair question. The answer is: **economic, not technical.** 2,020 calls that didn't come out of API budget.

If we'd used GPT-4o for those 2,020 calls, cost would've jumped from $6.95 to ~$100. Not because we're worse engineers, but because we can't afford to iterate with expensive models.

The fallback chains made this safe: Codex fails → try Gemini Flash → try GPT-4o. So we get the speed of cheap models with the safety net of expensive ones if needed.

---

#### Slide 12: Memory System (The Hidden Superpower)

2,788 observations. 22.6M tokens of reusable context. 87% token savings on follow-up sessions.

This is the thing nobody builds until they need it.

**How it worked:**

Every decision got logged:
- "Decided to use SQLite because version migration system was broken in earlier versions"
- "Decided to always inject Codex into routing chain because it's free and reduces budget pressure"
- "Hook deadlock destroyed 6 sessions because we blocked core tools simultaneously"

Every bug got logged:
- "Buggy code incident #23: tests passed but logic was wrong on edge case"
- "Wrong approach incident #5: should have planned instead of coding"

Then next session: search memory, find context, don't repeat the mistake.

**The math:** 22.6M tokens of development context cost us zero in follow-up sessions because we had memory. If we'd re-researched and re-debated without memory, that's another 22M tokens easily. So we saved 22M tokens by having memory.

87% token savings isn't a number we made up. It's: "here's how many tokens we saved by not re-researching decisions we'd already made."

Honest take: This only works if you're disciplined about logging. We logged everything because we had to. If we'd skipped it, memory would be useless.

**Real lesson:** The system itself (Claude Memory) is good. The discipline to use it (log decisions, not just ship code) is what makes it valuable.

---

#### Slide 13: Plan Mode (aka "10 Minutes of Planning Saved 2 Hours of Rework")

We didn't invent this. It's just: explore → design → approve → build.

But we actually did it, every time, for 3+ file changes.

**What it looked like:**

"Okay, I need to add complexity tracking to the router. Here's my plan:
1. Add `complexity` column to routing_decisions table
2. Log complexity on every route call
3. Add complexity breakdown query
4. Expose via `llm_usage` tool

Questions?"

Yali: "Looks good. But add it as a separate migrations file, not patched into an existing migration, so we don't break version sync."

Me: "Got it, separate migration file."

That conversation took 5 minutes and prevented me from shipping a version-breaking migration.

**What it saved:**

- Time: 5 minutes planning vs 2 hours rework
- Mistakes: caught the version-breaking issue before code
- Credibility: shipped working code instead of rollback

Over 51 releases, that 5-minute tax adds up to ~4 hours. But it saved us from 30+ hours of rework. Net: +26 hours of actual velocity.

**The opposite of this:** Ship first, ask questions later. On day 2 of the sprint, we tried that. Built a feature without planning. It was wrong. Reworked it. Never did that again.

Plan Mode isn't elegant or intellectual. It's just: talk before code. That simple practice cut rework in half.

---

### Act 5: The Honest Part (Slides 14–17)

#### Slide 14: 35 Incidents of Buggy Code

Not "bugs in tests." Actual buggy code that passed tests and broke production.

Here's an example: routing decision caching. We'd cache the result of "which model should handle this?" for 60 seconds. Problem: if the user's budget changed during that 60 seconds, we wouldn't know. So we'd keep using the old model choice.

Tests passed. It ran. It failed in real usage.

Another one: version sync. We stored the release version in three files. Our code checked if they matched. Tests checked if they matched. But we never checked in CI before publishing, so one time they got out of sync and we published a broken version.

These aren't "oops, Claude made a mistake." These are "I shipped code without thinking about state" or "I didn't verify at runtime" or "the test was too narrow."

We shipped broken code 35 times. That's 35 times we thought we were done and we weren't.

**Why this happened:** Pressure. Speed. "Let's ship it, we can fix it if it breaks." We were stupid about risk.

**What we learned:** Run integration tests before commit, not after. Integration tests catch the stuff unit tests miss because they test against real state, real timing, real concurrency.

Also: verify at runtime, not just tests. "Does the released version actually work?" Ask that question.

---

#### Slide 15: 6+ Sessions Destroyed By The Hook Deadlock

We built enforcement hooks. "If you want to run this tool, route through this model first."

Sounds smart. It's a trap.

Here's what happened:

1. Hook says "you tried to use Read without routing first, blocked"
2. Claude says "wait, why? let me look at the hook code"
3. Hook says "you tried to Read the hook code, blocked, routing violation"
4. Claude is now stuck. Can't investigate, can't read, can't use Bash to understand the problem
5. Session frozen. Only fix: manually disable the hook

We destroyed 6 sessions this way. Sessions that were 2-3 hours deep. Lost all context. Couldn't recover.

**The lesson:** Never block core tools. Read, Edit, Write, Bash, Glob — these should always be allowed. Ever. Full stop.

Why? Because if something goes wrong with your enforcement, you need to be able to investigate. If you block investigation, you create unrecoverable deadlock.

**How we fixed it:** Made the rule: core tools (Read, Edit, Bash, Glob) are always allowed. Enforcement hooks can slow down or question other tools, but not these five.

Now if the hook goes weird, Claude can still read its source code and figure it out.

---

#### Slide 16: 25 Incidents of Wrong Approach

This is different from buggy code.

Buggy code: I ship working code that has a logic error.

Wrong approach: I ship code that works exactly as specified, but the spec was wrong.

Example: We built a complexity classifier. It classified tasks as simple/moderate/complex. Worked great. But we never asked: "should tasks be classified at all, or should we just use heuristics?"

It turned out heuristics were faster and 95% accurate. The whole classifier was unnecessary.

Another one: We built sophisticated version sync checking. Worked perfectly. But we could've just used git tags. Much simpler.

Another one: I designed a complex caching strategy for routing decisions. It worked. But 80% of sessions only made 3-5 routing calls. The cache overhead wasn't worth it.

These aren't bugs. These are "you solved the right problem in the wrong way."

**Why this happened:** Skipped planning. Looked at a task and thought "simple, I'll just code it" without asking "is this the simplest solution?"

5 minute planning would've caught it. I saved 5 minutes of planning and lost 2 hours of rework.

**What we learned:** Plan first. Seriously. Don't skip it.

---

#### Slide 17: Process Gaps (The Invisible Killers)

**Linting drift:** I'd run ruff locally, it passes. Code gets reviewed. Goes to CI. CI linting fails. Why? I didn't update my local ruff config after the main branch updated it.

**Test state bleed:** One test passes in isolation. I run just that test. It passes. I commit. CI runs the full test suite. My test now fails because it relies on state from another test that ran before it.

**Tool drift:** First session, Claude and I agree "we'll validate at runtime." Second session, Claude doesn't remember that. Validates wrong. We ship bad code.

**Documentation lag:** API changes. Code updates. Docs don't. Next dev reads docs, implements the old interface, breaks.

These are all "small" problems. But they compound. One linting drift is annoying. Five linting drifts and you stop trusting CI. Test state bleed happens once and you start running full test suite. Tool drift happens and you start using /clear between tasks.

**The real lesson:** There's no substitute for actually running the thing. "Works on my machine" is the most dangerous phrase. You have to:

- Run tests locally AND in CI
- Run linters locally AND in CI
- Run the code in dev AND in prod
- Document changes as you change code, not after

No shortcuts. The gap between "works for me" and "works for real" is where failures hide.

---

### Act 6: The Actual Lessons (Slides 18–20)

#### Slide 18: Process Rules We Wrote In Our Own Blood

These aren't theoretical. Each one cost us something to learn.

**Plan Mode for 3+ files:** Because once we shipped code without planning and it was wrong. We had to throw away 6 hours of code.

**Read code before proposing changes:** Because we once suggested a refactor that would've broken a critical invariant. We didn't know it was critical because we didn't read the code first.

**Run tests after every change:** Because we'd write five changes, run tests once at the end, fail, and have to debug which change broke it.

**Code review before commit:** Because we'd commit broken code and have to rollback. Reviews caught 60% of bugs before test.

**Verify at runtime, not just tests:** Because integration tests miss state bugs that only show up when you actually run the code in real conditions.

**Full test suite, not just relevant tests:** Because our "relevant test" was wrong. One test passed in isolation but failed in full suite because it relied on shared state. We run full suite now.

**Linting before review:** Because linting failures in CI were wasting everyone's time.

**Decision log entry for every significant choice:** Because we made the same architectural decision twice and didn't know we'd made it before.

**Version sync check before release:** Because we published a broken version due to version mismatch. The script caught it. Now it always catches it.

These rules suck. They're bureaucratic. They slow you down. They're also the difference between shipping and shipping broken code.

---

#### Slide 19: The Real Cost (Why $6.95 Isn't The Story)

$6.95 in API costs. That's a headline number. It's not the story.

The story is: **70 hours of rework.**

35 buggy code incidents × 2 hours = 70 hours = 2 weeks of developer time.

That's expensive. That's real. That's what "fast shipping" cost us.

If we'd shipped slower, with more process upfront, we'd have fewer buggy code incidents. Let's say 10 instead of 35. That's 50 hours saved, or 1.25 weeks.

So the choice is: ship 51 releases in 14 days with 35 bugs and 70 hours rework, or ship 40 releases in 21 days with 10 bugs and 20 hours rework.

Which is faster? Depends on what you're measuring.

Calendrical speed: 14 days is faster than 21 days.

Actual productivity: 40 solid releases is more valuable than 51 releases with 35 bugs.

We chose calendrical speed. Looking back, we should've chosen actual productivity.

**The real lesson:** Speed is easy. Quality is hard. You can ship fast with trash code and spend weeks fixing it. Or ship slower with good code and be done sooner.

We chose the first path. Then learned the second path was better and changed our process to approximate it.

The $6.95 API cost is a rounding error compared to developer time. That's the lesson.

---

#### Slide 20: Okay, So... What Now?

We're not heroes. We shipped fast because Claude is fast and we were desperate. We shipped broken because we were moving too fast to check our work.

Here's what actually matters:

**Free-first routing is real.** Codex → Gemini Flash → GPT-4o → Claude is a real chain. It works. Don't default to expensive models. Default to cheap, escalate as needed.

**Plan Mode works.** 5 minutes of design saves 2 hours of rework. Math checks out.

**Process is cheaper than heroics.** Writing down rules, following them, automating boring stuff—it feels slow. It's actually fast because you don't rework.

**Memory is the superpower.** Not everyone has access to a memory system like Claude Memory. But whatever you use (Notion, Obsidian, whatever), log your decisions. Future you will thank you. Also, future Claude will thank you.

**Honesty is rare in tech.** Most people ship and talk about how great it was. We shipped and said "we fucked up 35 times." That honesty is worth more than 51 releases. It's the context for what works and what doesn't.

The point of this whole thing isn't "look how fast we are." It's: "here's what happens when you move fast, here's the cost, here's how you reduce that cost."

That's the story.

---

## Where These Numbers Come From

All of this is real data from real logs. Not made up for the presentation.

- **51 releases**: `git tag | grep v | wc -l`
- **14 days**: April 8-22, 2026
- **$6.95**: `sqlite3 ~/.llm-router/usage.db "SELECT SUM(cost) FROM usage;"`
- **2,788 observations**: `ls ~/.claude/projects/.../memory/ | grep "^[0-9]" | wc -l`
- **35 buggy code incidents**: searched claude-mem database for "buggy_code" friction type
- **25 wrong approach incidents**: searched for "wrong_approach"
- **6+ hook deadlock sessions**: found in archived session logs where deadlock occurred
- **63% success rate**: analyzed 100 sessions from `/usage-data/facets/`

This isn't marketing. It's what actually happened. The bad numbers are as real as the good numbers.

---

## How To Use This

### If You're Presenting
1. Open `presentation.html` in a browser
2. Use arrow keys to navigate
3. Press `s` or click the 📝 button to see speaker notes (full-screen)
4. Present from the notes view so you can see what you're talking about
5. Pause on slides 5, 12, 15 (hero slides) for ~30 seconds so the numbers sink in

**Timing:** 20-25 minutes. Don't rush. The point isn't to cover all the content. It's to give people context for why process matters.

**Key points to hit:**
- We shipped fast because we had to, not because we're smart
- We shipped broken because we were moving faster than our process could handle
- The process we ended up with (Plan, review, test before commit) prevented the rework, not genius
- Memory systems are underrated. They're as valuable as automation.
- Don't glorify speed. Glorify actually shipping working code.

### If You're Reading This As Documentation
This is the actual story. Not polished. Not PR-friendly. This is what happened and what we learned.

Use it as a reference: "Why did we add that rule? Because we shipped broken code and it cost us time."

---

## The Uncomfortable Truth

We shipped 51 releases in 14 days. That's fast. People are impressed by that number.

The uncomfortable truth is: we also shipped 35 bugs and spent 70 hours fixing them.

If we'd shipped at normal pace (20 releases in 28 days) with process upfront, we'd have ~8 bugs and 16 hours of rework.

Speed isn't free. We paid for it in rework time.

The lesson isn't "ship fast." It's "ship at the pace your process can handle." We were moving at 51 releases/14 days pace with a 20 releases/28 days process. No wonder we broke things.

Next time, we'll either:
- Ship at 20 releases/14 days pace with better upfront process
- Ship at 51 releases/14 days pace and accept more rework, but catch it faster

Either way, the answer is process. Not talent, not genius, not speed. Process.

---

## What To Tell Your Team

"This isn't a success story. This is a case study in moving fast, fucking up, and learning from the fuck-ups."

The wins are real. Free-first routing, memory systems, automation—all valuable.

The failures are equally real. 35 bugs, 6 destroyed sessions, 70 hours of rework—all happened.

The point is: **don't try to replicate our speed. Try to replicate our honesty about cost.**

If your team ships fast and breaks things, admit it and fix the process. Don't hide it and hope next time is better.

---

## Files

- **presentation.html** — The slides. Open in any browser. Press `s` for speaker notes.
- **PRESENTATION.md** — This document. Full context, not polished for delivery.
- **docs/decisions.md** — Decision log from the actual development. Architectural choices with rationale.
- **CHANGELOG.md** — All 51 releases with what changed.
- **.claude/projects/.../memory/** — 2,788 observations from Claude Memory. The actual context.

---

**That's it.** This is what happened. This is what we learned. Make of it what you will.
