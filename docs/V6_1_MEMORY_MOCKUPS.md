# v6.1 "Memory" — Personal Routing Profile Mockups

**Version:** 6.1-preview
**Theme:** Personalization through learned patterns
**Status:** Design mockups (implementation ready for May 2026)

---

## Feature Overview

v6.1 adds **personal routing profiles** that learn your patterns after 50 routed calls:

1. **Auto-generated profile** — After 50 calls, llm-router learns your task distribution
2. **Override learning** — If you manually override routing 3x for the same task, it remembers
3. **Profile visualization** — Beautiful ASCII display of your patterns
4. **Community sharing** — Export/import profiles so others can benefit from your patterns

**Key insight:** The router should know you better than any config file.

---

## Mockup 1: Profile Auto-Generation (After 50 Calls)

### Command Output
```bash
$ llm-router profile

╔════════════════════════════════════════════════════════════╗
║  💾 Your Routing Profile — Generated 2026-06-10           ║
╚════════════════════════════════════════════════════════════╝

Analyzed 87 routed calls (confidence: 87%)

TASK DISTRIBUTION:
  Code Generation   ████████████░░░░░░░ 60%  (52 calls)
  Architecture      █████░░░░░░░░░░░░░░ 25%  (22 calls)
  Q&A Research      ███░░░░░░░░░░░░░░░░ 15%  (13 calls)

MODEL ACCURACY BY TASK:
────────────────────────────────────────────────────────────

CODE GENERATION:
  Haiku   ████████████████████ 94%  (49/52 calls, avg 1.2s)
  Sonnet  ██████████████████░░ 92%  (fallback only)

ARCHITECTURE ANALYSIS:
  Sonnet  ████████████████████ 96%  (21/22 calls, avg 3.4s)
  Haiku   ███░░░░░░░░░░░░░░░░ 14%  (too risky for architecture)

Q&A RESEARCH:
  Haiku   ███████████████░░░░░ 91%  (12/13 calls, avg 0.8s)
  Gemini  ███████████░░░░░░░░░ 77%  (fallback)

────────────────────────────────────────────────────────────

💾 LEARNED PATTERNS (3 overrides):
  ✓ Security reviews → always Opus (3x manual override)
  ✓ Debugging → Sonnet (Haiku only 82% here, risky)
  ✓ Quick Q&A → Haiku (91% accuracy, faster)

────────────────────────────────────────────────────────────

COST IMPACT:
  Total cost (87 calls):     $14.32
  vs Opus baseline:          $347.20
  Savings:                   $332.88 (96%)

Monthly projection:
  Routed: ~$17.40
  Baseline: ~$421.00
  Savings: ~$403.60

────────────────────────────────────────────────────────────

🧠 TOP RECOMMENDATIONS:

1. You're a Python developer. Haiku handles 94% of your code safely.
   → Keep using Haiku for code generation
   → Only escalate to Sonnet for complex refactoring (14% of cases)

2. Architecture decisions ALWAYS need Sonnet (96% accuracy).
   → You never want Haiku for architecture
   → Router now auto-detects architecture tasks + escalates

3. Quick Q&A works well with Haiku (91% accuracy).
   → Your recent Haiku Q&A score: 91%
   → Only falls back to Gemini Pro when unsure

4. Security reviews = Opus (3x manual override pattern learned).
   → Router now always escalates security reviews to Opus
   → You're right — this is not a place to save cost

5. Debugging is risky with Haiku (82% accuracy).
   → You flagged this 3x by escalating to Sonnet
   → Now defaulting to Sonnet for debug sessions

────────────────────────────────────────────────────────────

This month savings: $403.60 (96% cheaper than baseline)
Annual projection:  $4,843.20 saved

Press 'q' to close | 's' to share | 'e' to export
```

---

## Mockup 2: Override Learning in Action

### Scenario: User Overrides for "Security Review"

**First override (14:30):**
```
You: "Review this OAuth implementation for security issues"
  → router suggests: haiku [23% confidence] ⚠ Low confidence
  You escalate to: opus

  💾 Learned: User prefers Opus for security review
  Logged: 1/3 overrides for "security_review" task
```

**Second override (14:45):**
```
You: "Security audit of password reset flow"
  → router suggests: haiku [18% confidence] ⚠ Very low
  You escalate to: opus

  💾 Learned: User prefers Opus for security review
  Logged: 2/3 overrides for "security_review" task
```

**Third override (15:00):**
```
You: "Check this API auth middleware"
  → router suggests: haiku [25% confidence]
  You escalate to: opus

  💾 Got it. Security reviews now use Opus. ✓

  Profile updated:
  learned_overrides.security_review = "always_opus"

  Learned pattern: User always escalates security tasks to Opus.
  From now on, all "security" tasks route directly to Opus.
```

**Next security review (auto-routed):**
```
You: "Review this JWT implementation"
  → router routes to: opus [100% confidence] ✓ (learned pattern)

  💾 Profile: "Security reviews always use Opus"
```

---

## Mockup 3: Profile Export/Import

### Export Profile

```bash
$ llm-router export-profile python-backend-dev.yaml

✅ Profile exported to python-backend-dev.yaml

Summary:
  Task distribution: Code 60%, Architecture 25%, Q&A 15%
  Preferred models: Haiku (code), Sonnet (architecture), Haiku (Q&A)
  Learned overrides: 3 (security_review, debugging, etc.)
  Quality baseline: 94% accuracy
  Cost savings: 96% vs Opus baseline

Share your profile:
  git add python-backend-dev.yaml
  git commit -m "docs: add routing profile for Python backend devs"
  git push
```

### Import Profile

```bash
$ llm-router import-profile python-backend-dev.yaml

Loading profile: python-backend-dev.yaml
  Task distribution: Code 60%, Architecture 25%, Q&A 15%
  Recommended models: Haiku (code), Sonnet (architecture)
  Learning from 87 real sessions...

Apply this profile? [y/N]

Profile applied! 🚀
  - Code generation now defaults to Haiku
  - Architecture analysis escalates to Sonnet
  - Security reviews always use Opus

Your first 50 calls will be compared against this profile.
(Profiles don't override your actual usage patterns.)
```

### Community Profile Marketplace

```bash
$ llm-router profile-list

╔════════════════════════════════════════════════════════════╗
║  📚 Community Routing Profiles                             ║
╚════════════════════════════════════════════════════════════╝

TOP PROFILES (by usage):

🥇 python-backend-dev (14.2k users)
   Code: Haiku | Architecture: Sonnet | Security: Opus
   94% quality | 96% savings vs baseline
   Star: ⭐⭐⭐⭐⭐

🥈 typescript-frontend (8.7k users)
   Components: Haiku | State Mgmt: Sonnet | Design Decisions: Opus
   92% quality | 94% savings
   Star: ⭐⭐⭐⭐⭐

🥉 data-scientist (6.2k users)
   EDA: Haiku | Modeling: Opus | Viz: Sonnet
   91% quality | 89% savings
   Star: ⭐⭐⭐⭐☆

4️⃣ devops-engineer (4.1k users)
   Config: Haiku | Architecture: Sonnet | Compliance: Opus
   93% quality | 95% savings
   Star: ⭐⭐⭐⭐⭐

5️⃣ mobile-dev (3.8k users)
   UI: Haiku | State Management: Sonnet | Architecture: Opus
   91% quality | 92% savings
   Star: ⭐⭐⭐⭐☆

Import a profile:
  llm-router import-profile python-backend-dev
```

---

## Mockup 4: Notification When Pattern is Learned

### In Statusline After 3rd Override

```
[llm-router] 💾 Got it. Security reviews now use Opus. ✓
```

### In Session Summary at End

```
╔═════════════════════════════════════════════════════════╗
║  Session Summary — 3 routing decisions                 ║
├─────────────────────────────────────────────────────────┤
║                                                         ║
║  💾 Learned Patterns This Session:                      ║
║     ✓ Security reviews → always Opus (3x override)     ║
║     ✓ Debugging → Sonnet (Haiku only 82% for you)      ║
║                                                         ║
║  Profile will be updated next session.                 ║
║  Run: llm-router profile   to see your patterns         ║
║                                                         ║
╚═════════════════════════════════════════════════════════╝
```

---

## Mockup 5: Profile JSON Structure

### Generated profile.json

```json
{
  "generated": "2026-06-10T14:30:00Z",
  "version": "1.0",
  "confidence": 0.87,

  "task_distribution": {
    "code_generation": {
      "frequency": 0.60,
      "call_count": 52,
      "preferred_model": "haiku",
      "accuracy": 0.94,
      "response_time_ms": 1200
    },
    "architecture_analysis": {
      "frequency": 0.25,
      "call_count": 22,
      "preferred_model": "sonnet",
      "accuracy": 0.96,
      "response_time_ms": 3400
    },
    "qa_research": {
      "frequency": 0.15,
      "call_count": 13,
      "preferred_model": "haiku",
      "accuracy": 0.91,
      "response_time_ms": 800
    }
  },

  "model_accuracy_by_task": {
    "code_generation": {
      "haiku": {
        "accuracy": 0.94,
        "sample_size": 49,
        "recommendation": "primary"
      },
      "sonnet": {
        "accuracy": 0.92,
        "sample_size": 3,
        "recommendation": "fallback"
      }
    },
    "architecture_analysis": {
      "sonnet": {
        "accuracy": 0.96,
        "sample_size": 21,
        "recommendation": "primary"
      },
      "haiku": {
        "accuracy": 0.14,
        "sample_size": 1,
        "recommendation": "avoid"
      }
    }
  },

  "learned_overrides": {
    "security_review": "always_opus",
    "debugging": "sonnet_with_fallback_haiku",
    "quick_qa": "haiku_with_escalation"
  },

  "cost_analysis": {
    "total_calls": 87,
    "total_cost_usd": 14.32,
    "baseline_cost_usd": 347.20,
    "savings_usd": 332.88,
    "savings_pct": 0.96,
    "monthly_projection_usd": 403.60,
    "annual_projection_usd": 4843.20
  },

  "recommendations": [
    "You're a Python developer. Haiku handles 94% of your code safely.",
    "Architecture decisions ALWAYS need Sonnet (96% accuracy).",
    "Quick Q&A works well with Haiku (91% accuracy).",
    "Security reviews = Opus (3x manual override pattern learned).",
    "Debugging is risky with Haiku (82% accuracy)."
  ]
}
```

---

## Mockup 6: Real-Time Profile Visualization

### As User Types (Type Ahead)

```
You're typing: "Review this JWT"...

💾 Routing Profile Context:
   This looks like a security review (80% confidence)
   Profile: security_review → always_opus

   → Will route to: opus [learned pattern] ✓
```

### After Decision

```
You: "Review this JWT implementation for security issues"
  → routed to: opus [100% confidence] ✓

  💾 Learned pattern matched: security_review
  Quality expected: 96% (from your profile)
  Cost: $0.062 (baseline: $0.062 if opus, matches profile)
```

---

## Mockup 7: Profile Drift Detection

### When Profile Accuracy Drops

```
⚠ Profile Update Available

Your "code_generation" accuracy has changed:
  Old: 94% (Haiku)
  New: 88% (Haiku) — slight drop

Reason: Recent refactoring tasks scored lower
Action: None needed (88% still good), but monitoring...

If Haiku accuracy drops below 85%, router will:
  1. Automatically escalate to Sonnet for code
  2. Notify you with: "Code quality dropped, escalating to Sonnet"
  3. Re-baseline your profile once accuracy recovers

Current: 88% ✓ Still using Haiku (slight variance is normal)
```

---

## Mockup 8: Annual Impact Card

### From `llm-router profile`

```
╔════════════════════════════════════════════════════════════╗
║  Your Routing Profile Annual Impact                        ║
╠════════════════════════════════════════════════════════════╣
║                                                            ║
║  Calls analyzed:          87 (confidence 87%)             ║
║  Profile accuracy:        94% (vs 99% Opus baseline)      ║
║  Cost per call:           $0.16                           ║
║  Opus baseline per call:  $3.99                           ║
║                                                            ║
║  Monthly savings:         $403.60                          ║
║  Annual savings:          $4,843.20                        ║
║                                                            ║
║  Time saved:              ~2 hours/month                  ║
║                           (faster models = quicker response)
║                                                            ║
║  Efficiency multiplier:   25.0x                            ║
║                           (25 times cheaper than baseline)  ║
║                                                            ║
╚════════════════════════════════════════════════════════════╝
```

---

## Implementation Checklist for v6.1

### Phase 1: Profile Generation (Week 1)
- [ ] Track task counts + model accuracy per task
- [ ] Auto-generate profile.json after 50 calls
- [ ] Compute confidence score (based on sample size)
- [ ] Generate personalized recommendations

### Phase 2: Override Learning (Week 2)
- [ ] Detect manual escalations (tool call showing override)
- [ ] Count overrides per (task_type, original_model, override_model)
- [ ] After 3 occurrences, add to learned_overrides
- [ ] Show notification: "Got it. Security reviews now use Opus. ✓"

### Phase 3: Profile Visualization (Week 3)
- [ ] `llm-router profile` command
- [ ] ASCII charts (bar visualization)
- [ ] Accuracy by task display
- [ ] Recommendations based on patterns

### Phase 4: Export/Import (Week 4)
- [ ] `llm-router export-profile` command
- [ ] `llm-router import-profile` command
- [ ] Validation (check profile format)
- [ ] Merge logic (don't override actual patterns)

### Phase 5: Marketplace (Week 5)
- [ ] Central profiles repository (GitHub-hosted)
- [ ] Profile discovery + ranking
- [ ] One-command import
- [ ] Stats (users using profile, success rate)

### Phase 6: Drift Detection (Week 6)
- [ ] Monitor per-task accuracy over time
- [ ] Alert when accuracy drops >10%
- [ ] Auto-escalation if accuracy < 85%
- [ ] Recovery detection + re-baselining

---

## Success Metrics for v6.1

| Metric | Target | Rationale |
|--------|--------|-----------|
| Profile generation | 80%+ users get profile after 50 calls | Automatic learning |
| Override learning | 70% of users have ≥1 learned pattern | Personalization working |
| Profile sharing | 100+ community profiles | Network effect |
| Accuracy improvement | +3% overall vs v6.0 | Personalization benefit |
| User adoption | 60% run `llm-router profile` monthly | Engagement |

---

**Status:** Mockups complete — Ready for implementation in June 2026
