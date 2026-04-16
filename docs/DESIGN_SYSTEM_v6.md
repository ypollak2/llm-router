# Design System — llm-router v6.0–v6.1

**Version:** 1.0
**Updated:** 2026-04-16
**Theme:** Visibility + Memory
**Inspired by:** Claude (elegant, minimal) + Agent metaphor (orchestration, flow)

---

## Core Design Philosophy

llm-router makes **invisible routing decisions visible** and **learns your patterns over time**. The design system reflects this duality:

- **Visibility:** Clear, legible signals show what's happening in real-time
- **Memory:** Personal touches show the system understands you
- **Elegance:** Minimal, purposeful design (inspired by Claude)
- **Intelligence:** Visual metaphors of orchestration and flow

---

## 1. Color Palette

### Primary Colors

| Name | Hex | ANSI | Use Case | Meaning |
|------|-----|------|----------|---------|
| **Orchestrate Blue** | `#0066CC` | `34` (bright blue) | Primary actions, routing flow | Intelligence, trust, routing |
| **Memory Amber** | `#CC8800` | `33` (bright yellow) | Memory features, personalization | Learning, growth, personalization |
| **Confidence Green** | `#00AA44` | `32` (bright green) | Success, high confidence, savings | Trust, quality, positive outcome |

### Semantic Colors

| Name | Hex | ANSI | Use Case |
|------|-----|------|----------|
| **Warning Red** | `#DD3333` | `31` (bright red) | Low confidence, escalation, error |
| **Info Gray** | `#666666` | `37` (bright white) | Secondary info, metadata |
| **Subtle** | `#AAAAAA` | `90` (bright black) | Tertiary info, less important |

### Terminal Compatibility

- **Dark terminals:** Use bright ANSI colors (34, 33, 32, 31, 37, 90)
- **Light terminals:** Dim ANSI colors or reverse background
- **No-color mode:** `NO_COLOR=1` → fallback to ASCII symbols only

---

## 2. Typography

### Heading Hierarchy

```
┌─ H1: Main title (features, chapters)
│   UPPERCASE, 80 chars max
│   Example: "╔═ PERSONAL ROUTING PROFILE ═╗"
│
├─ H2: Section title
│   Title Case, 60 chars max
│   Example: "Your Routing Patterns"
│
└─ H3: Subsection
    Sentence case, 50 chars max
    Example: "Task distribution by frequency"
```

### Font Styles

| Style | Terminal | Use Case |
|-------|----------|----------|
| **Bold** | `\033[1m` (or `**bold**` in Markdown) | Headers, emphasis, decision points |
| **Dim** | `\033[2m` (or `~~dim~~`) | Secondary info, timestamps, metadata |
| **Monospace** | `` ` code ` `` | Model names, costs, file paths, commands |

### Examples

```
╔═══════════════════════════════════════════════════════╗
║  💾 PERSONAL ROUTING PROFILE                           ║
╚═══════════════════════════════════════════════════════╝

Generated: 2026-06-10 (based on 87 routed calls)

Task Distribution:
  Code Generation   ████████████░░░░░░░ 60%
  Architecture      █████░░░░░░░░░░░░░░ 25%
```

---

## 3. Icons & Symbols

### Routing Flow

| Symbol | Name | Meaning | Usage |
|--------|------|---------|-------|
| `→` | Arrow | Direct routing to model | Statusline: `→ haiku` |
| `⚡` | Lightning | Fast, efficient, power-saving | `$0.001 saved ⚡` |
| `⬆` | Escalate Up | Upgrade to more powerful model | Confidence too low, escalating |
| `⬇` | Escalate Down | Downgrade to cheaper model | Budget pressure, safe to downgrade |

### Memory & Learning

| Symbol | Name | Meaning | Usage |
|--------|------|---------|-------|
| `💾` | Memory | Personalization, learning, profile | Profile header, learned patterns |
| `🧠` | Brain | Intelligence, learned behavior | Smart routing, personalized decisions |
| `📚` | Library | Knowledge, history, patterns | Profile library, community profiles |
| `✓` | Check | Confirmed pattern, learned override | "Got it. Security reviews now use Opus." |

### Quality & Confidence

| Symbol | Name | Meaning | Usage |
|--------|------|---------|-------|
| `★` (full) | Star Full | High confidence (80–100%) | Model selection, trust |
| `☆` (empty) | Star Empty | Low confidence (0–40%) | Warning, escalation needed |
| `⚠` | Warning | Potential quality issue | Degradation alert, low confidence |
| `✔` | Check | Quality passed | Good response, no escalation needed |
| `🚨` | Alert | Critical issue | Quality guard prevented misroute |

### Status & Outcome

| Symbol | Name | Meaning | Usage |
|--------|------|---------|-------|
| `✅` | Success | Task completed well | Session end, cost saved |
| `⏱` | Clock | Time, latency tracking | Response time, performance |
| `💰` | Money | Cost, savings | `Saved $2.29`, cost display |
| `📊` | Chart | Data, analytics | Profile visualization header |

---

## 4. ANSI Styling Guide

### Color-Coded Model Names

```python
# In code, use this pattern:
def format_model(model: str) -> str:
    if model in ("haiku", "gemini-flash"):
        return f"\033[32m{model}\033[0m"  # Green (cheap, fast)
    elif model in ("sonnet", "gpt-4o"):
        return f"\033[34m{model}\033[0m"  # Blue (balanced)
    elif model in ("opus", "o3"):
        return f"\033[33m{model}\033[0m"  # Yellow (powerful, expensive)
    else:
        return model

# Output:
→ haiku (code/simple) $0.001 saved ⚡
```

### Box Drawings

**Profile Card (elegant, simple):**
```
╔════════════════════════════════════════╗
║  💾 Your Routing Profile               ║
╠════════════════════════════════════════╣
║  Code Generation: 94% (Haiku)          ║
║  Architecture:    96% (Sonnet)         ║
║  Q&A Research:    91% (Haiku)          ║
╠════════════════════════════════════════╣
║  This month: $14.32 | Saved: $403.20   ║
╚════════════════════════════════════════╝
```

**Routing Decision (minimal, fast):**
```
→ haiku [87%] (code/simple) $0.001 saved ⚡
```

**Alert (attention-grabbing):**
```
⚠ Quality Alert
  Haiku quality for "code" dropped to 82%
  → Escalating to Sonnet until recovery
```

---

## 5. Component Examples

### 5.1 Live Routing HUD (Statusline)

**Format:** `→ MODEL [CONFIDENCE%] (TASK/COMPLEXITY) $COST saved ⚡`

**Width constraint:** <50 characters (fit in typical statusline)

**Examples:**

```
Light environment:
→ haiku [87%] (code/simple) $0.001 ⚡

Dark environment (with ANSI):
\033[34m→ haiku\033[0m [87%] (code/simple) \033[32m$0.001 ⚡\033[0m

No-color mode:
→ haiku [87%] (code/simple) $0.001 saved
```

### 5.2 Session Replay (Structured Transcript)

```
═══════════════════════════════════════════════════════════
                   SESSION REPLAY — May 10
═══════════════════════════════════════════════════════════

14:30 You: "Write a function to parse JSON"
      ├─ Routed to: \033[32mhaiku\033[0m (code/simple)
      ├─ Confidence: ★★★★★★★★☆☆ 87%
      ├─ Reasoning: <50 lines, standard library, low risk
      ├─ Cost: $0.0001
      └─ ✅ Passed quality check (97% accuracy)

14:31 You: "What's the architecture of this project?"
      ├─ Routed to: \033[34msonnet\033[0m (analysis/moderate)
      ├─ Confidence: ★★★★★★★★★☆ 92%
      ├─ Reasoning: Multi-file analysis, domain knowledge
      ├─ Cost: $0.008
      └─ ✅ Passed quality check (96% accuracy)

───────────────────────────────────────────────────────────
SUMMARY:  12 routed calls | $0.186 spent | $1.847 saved (90%)
───────────────────────────────────────────────────────────
```

### 5.3 Personal Profile Visualization

```
╔════════════════════════════════════════════════════════════╗
║  💾 Your Routing Profile — Generated 2026-06-10           ║
╚════════════════════════════════════════════════════════════╝

TASK DISTRIBUTION:
Code Generation   ████████████░░░░░░░ 60%  (52 calls)
Architecture      █████░░░░░░░░░░░░░░ 25%  (22 calls)
Q&A Research      ███░░░░░░░░░░░░░░░░ 15%  (13 calls)

MODEL ACCURACY:
\033[32mHaiku  \033[0m  ████████████████████ 94% (code)
\033[34mSonnet \033[0m  ████████████████████ 96% (architecture)
\033[32mHaiku  \033[0m  ███████████████░░░░░ 91% (research)

SAVINGS THIS MONTH:
Routed Cost:  $14.32
Baseline:     $347.20  (all Opus)
\033[32mSaved:        $332.88 (96%)\033[0m

🧠 LEARNED OVERRIDES (3):
  ✓ Security reviews → always Opus (3x manual override pattern)
  ✓ Debugging → Sonnet fallback (Haiku only 82% here)
  ✓ Quick Q&A → Haiku (91% accuracy, faster)

\033[32m→ Keep using Haiku for code, Sonnet for architecture\033[0m
```

### 5.4 Savings Proof Card (Shareable)

```
╔═════════════════════════════════════════════════╗
║  💰 SESSION SUMMARY — May 15 (14:30–15:45)    ║
╠═════════════════════════════════════════════════╣
║                                                 ║
║  Cost this session:       $0.18                ║
║  Opus baseline:           $2.47                ║
║  \033[32mSaved:                 $2.29 (93%)\033[0m          ║
║                                                 ║
║  ─────────────────────────────────────────────  ║
║  This month (Sep 1–15):                        ║
║                                                 ║
║  Cost:     $14.32      (12 sessions)           ║
║  \033[32mSaved:    $367.20     (96%)\033[0m             ║
║                                                 ║
║  ─────────────────────────────────────────────  ║
║  💾 Annual projection:  \033[32m$1,248 saved\033[0m         ║
║                                                 ║
║  Powered by llm-router ⚡                      ║
║  Get started: uvx llm-router install           ║
╚═════════════════════════════════════════════════╝

Sharable text:
I saved $367.20 this month using llm-router 🚀
14x efficiency vs Claude Opus!
```

### 5.5 Quality Guard Alert

```
┌─────────────────────────────────────────────────┐
│ 🚨 Quality Alert                                │
├─────────────────────────────────────────────────┤
│                                                 │
│ Haiku quality for "code" dropped to 82%        │
│ (from 94% avg). Recent errors detected.        │
│                                                 │
│ Action: Escalating code tasks to Sonnet        │
│         until quality recovers.                │
│                                                 │
│ 💾 Learned pattern: You always want            │
│    Sonnet for security reviews anyway.         │
│                                                 │
│ Debug: `llm-router quality --task=code`       │
└─────────────────────────────────────────────────┘
```

---

## 6. Implementation Guidelines

### 6.1 Python Terminal Styling

```python
# File: src/llm_router/terminal_style.py

from enum import Enum

class Color(Enum):
    """ANSI color codes for terminal output."""
    BLUE = "\033[34m"      # Orchestrate blue (routing)
    AMBER = "\033[33m"     # Memory amber (learning)
    GREEN = "\033[32m"     # Confidence green (savings)
    RED = "\033[31m"       # Warning red (errors)
    RESET = "\033[0m"      # Reset formatting

class Symbol(Enum):
    """Unicode symbols for routing language."""
    ARROW = "→"
    LIGHTNING = "⚡"
    ESCALATE_UP = "⬆"
    ESCALATE_DOWN = "⬇"
    MEMORY = "💾"
    BRAIN = "🧠"
    STAR_FULL = "★"
    STAR_EMPTY = "☆"
    CHECK = "✓"
    SUCCESS = "✅"
    WARNING = "⚠"
    ALERT = "🚨"

def format_routing_hud(model: str, confidence: float,
                        task: str, cost: float) -> str:
    """Format live routing HUD for statusline."""
    return (
        f"{Symbol.ARROW.value} {Color.BLUE.value}{model}{Color.RESET.value} "
        f"[{int(confidence*100)}%] ({task}) "
        f"{Color.GREEN.value}${cost:.4f}{Color.RESET.value} {Symbol.LIGHTNING.value}"
    )

# Usage in statusline:
hud = format_routing_hud("haiku", 0.87, "code/simple", 0.001)
# → haiku [87%] (code/simple) $0.001 ⚡
```

### 6.2 Web Dashboard Styling

For `localhost:7337` dashboard (v6.0+):

```
CSS color variables:
--color-orchestrate: #0066CC (routing flow, primary actions)
--color-memory: #CC8800 (personalization, learning)
--color-confidence: #00AA44 (success, savings)
--color-warning: #DD3333 (low confidence, escalation)

Layout principle:
- Dashboard is mirror of statusline HUD
- Card-based layout (one card per decision)
- Animated transitions for "escalation" events
- Color coding for model tier (green=cheap, blue=balanced, amber=expensive)
```

### 6.3 README Styling

```markdown
# llm-router

> Route every AI call to the cheapest model that can do the job well.
> **Visibility** × **Memory** = Intelligent routing that learns you.

## Design System

- 🎨 **Color-coded models:** Green (fast), Blue (balanced), Amber (powerful)
- 🧠 **Learned patterns:** Personal routing profile after 50 calls
- ⚡ **Savings proof:** ASCII card, badge, social sharing
- 💾 **Memory first:** Remember your overrides, improve over time
```

---

## 7. Accessibility Guidelines

### No Color-Only Information

❌ **Bad:**
```
Confidence: 87% (shown only in green)
→ haiku (green = cheap, blue = balanced)
```

✅ **Good:**
```
Confidence: ★★★★★★★★☆☆ 87%  (symbols + percentage)
→ haiku (code/simple) [87%]    (symbols + text describe confidence)
```

### High Contrast

- ANSI colors chosen for dark + light terminal compatibility
- Symbols never rely solely on color differentiation
- Fallback text descriptions for icons

### Text Sizing

- Statusline: 50 char max (fits smallest terminals)
- Profile visualization: 80 char width (standard terminal width)
- Cards: 60 char width for readability

---

## 8. Brand Voice & Messaging

### Tone

- **Confident:** "The router knows what it's doing"
- **Clear:** "Every decision is explained"
- **Personal:** "It learns from your preferences"
- **Efficient:** "Minimal tokens, maximum clarity"

### Key Messages

| Feature | Message | Tone |
|---------|---------|------|
| Visibility (HUD) | "You know what's happening in real-time" | Confidence |
| Memory (Profile) | "It learns your patterns — no tuning needed" | Personal |
| Quality (Guard) | "Safe escalation — you don't pay for mistakes" | Reassurance |
| Savings (Card) | "Your efficiency multiplier — X times cheaper" | Pride |

### Example Messaging

```
Good routing decision:
  "✅ Routed to haiku (87% confidence). Saved $0.001 vs Opus."

Escalation (safety):
  "⚠ Confidence too low (42%). Escalating to Sonnet for safety."

Learned behavior:
  "💾 Got it. Security reviews now always use Opus. ✓"

Monthly summary:
  "You've saved $367.20 this month. 96% cheaper than baseline. 🚀"
```

---

## 9. Application Checklist

### v6.0 — Visible (May 2026)

- [ ] **Statusline HUD:** Format + ANSI colors + caveman-mode fallback
- [ ] **Session replay:** Structured transcript with symbols + confidence
- [ ] **CLI verify command:** Health check with routing chain display
- [ ] **Documentation:** Update README with design system reference

### v6.1 — Memory (June 2026)

- [ ] **Profile visualization:** ASCII charts + recommendations
- [ ] **Memory card:** Personal profile display with learned patterns
- [ ] **Override notifications:** Acknowledgement of learned behavior
- [ ] **Community profiles:** Marketplace listing with consistent styling

---

## 10. Design Evolution

This design system is **intentionally minimal** to respect token constraints and terminal compatibility. Future versions (v6.2+) may add:

- SVG icons for web dashboard
- Enhanced animations for decision highlights
- Interactive profile exploration
- Themed variants (light/dark/monochrome)

**Core principle:** If it can't be rendered in a 80-character wide terminal with ANSI colors, simplify it.

---

## Files Created

- `src/llm_router/terminal_style.py` — Color + Symbol enums, formatting functions
- `docs/DESIGN_SYSTEM_v6.md` — This document
- `.claude-plugin/design-tokens.json` — Design token definitions for consistency
- `tests/test_terminal_styling.py` — Unit tests for terminal output formatting

---

**Design System Version:** 1.0
**Last Updated:** 2026-04-16
**Next Review:** After v6.0 release (May 2026)
