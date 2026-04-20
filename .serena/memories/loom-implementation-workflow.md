# Loom-Based Implementation Workflow

Unified workflow for implementing strategic improvements to Chronicle + LLM-Router using Project Loom orchestration.

## Workspace Structure

```
~/Projects/
├── loom.yaml                           (workspace config)
├── meta-workspace/
│   ├── services/
│   │   ├── llm-router -> ../llm-router
│   │   └── chronicle -> ../chronicle
│   ├── scripts/
│   │   └── loom                        (orchestration script)
│   ├── configs/
│   │   ├── dependency-graph.json
│   │   └── dependency-graph.html
│   └── .claude/
│       ├── file-ownership.json
│       ├── git-context.json
│       └── session-templates.json
├── llm-router/                         (core repo)
│   ├── src/llm_router/
│   │   ├── router.py                   (routing logic)
│   │   ├── classifier.py               (complexity classification)
│   │   └── tools/routing.py            (MCP tools)
│   └── tests/
└── chronicle/                          (core repo)
    ├── packages/
    │   ├── core/src/                   (decision storage)
    │   ├── cli/src/commands/           (user commands)
    │   └── mcp/src/                    (MCP server)
    └── tests/
```

---

## Key Loom Commands

```bash
# Status check across repos
loom status                    # Git status of both repos

# Analyze changes before committing
loom analyze-impact llm-router/src/llm_router/router.py
loom trace-dependency llm-router
loom check-boundary llm-router chronicle

# Work with both repos
loom install                   # Install both repos
loom test-all                  # Run tests in both
loom doctor                    # Health checks

# Atomic multi-repo commits
loom sync-commit "feat: implement decision logging in router"
```

---

## PHASE 1: Foundations (Week 1)

### Goal
Enable the core hooks that make Chronicle + LLM-Router work together.

### Tasks

#### Task 1.1: Enable Extended Thinking
**File**: `~/.claude/settings.json`

```bash
cd ~/Projects/meta-workspace
loom pre-change ~/.claude/settings.json
# Edit file: set "alwaysThinkingEnabled": true
loom post-change ~/.claude/settings.json
```

#### Task 1.2: Enable LLM-Router Auto-Routing Hook
**File**: `~/.claude/settings.json`

```json
{
  "hooks": {
    "UserPromptSubmit": "llm-router route --auto-classify"
  }
}
```

**Verify**:
```bash
source ~/Projects/.venv/bin/activate
llm-router config view
```

#### Task 1.3: Enable Chronicle SessionStart Hook
**File**: `~/.claude/mcp.json`

Ensure Chronicle MCP server registered:
```json
{
  "mcpServers": {
    "chronicle": {
      "command": "chronicle",
      "args": ["mcp"]
    }
  }
}
```

**Verify**:
```bash
# Test MCP server starts
chronicle mcp &
sleep 2
ps aux | grep "chronicle mcp"
```

#### Task 1.4: Set Project Profiles
**Files**: `~/.llm-router/config.json` per project

```bash
cd ~/Projects/llm-router
llm-router config set profile premium --project-scope

cd ~/Projects/chronicle
llm-router config set profile premium --project-scope
```

**Why**:
- Both are internal tools → need highest quality (Opus for complex decisions)
- Profile tells router: "Always escalate to Sonnet/Opus if needed, cost is secondary"

**Verify**:
```bash
llm-router config view
# Should show: profile = premium
```

**Testing Phase 1**:
```bash
cd ~/Projects/meta-workspace
loom test-all
# Both repos should pass tests
```

---

## PHASE 2: Decision Logging (Week 2)

### Goal
Implement decision capture in LLM-Router so Chronicle can store architectural choices.

### Changes Across Both Repos

#### Task 2.1: Add Decision Logging Tool to LLM-Router
**Repo**: `llm-router`
**Files to modify**:
- `src/llm_router/tools/routing.py` (add new tool)
- `src/llm_router/mcp.py` (register MCP tool)

**New tool: `llm_log_routing_decision`**
```python
@tool
def llm_log_routing_decision(
    decision: str,
    rationale: str,
    complexity_level: str,  # simple | moderate | complex
    model_chosen: str,
    affected_files: List[str],
) -> str:
    """
    Log a routing decision to Chronicle for institutional memory.
    Called after complex routing choices to document why a model was chosen.
    """
    chronicle.log_decision(
        title=f"Routing Decision: {decision}",
        rationale=f"{rationale}\nModel chosen: {model_chosen}\nComplexity: {complexity_level}",
        affected_files=affected_files,
        risk="HIGH"
    )
    return f"Decision logged to Chronicle"
```

**Testing**:
```bash
cd ~/Projects/llm-router
pytest tests/tools/test_routing.py -v
```

#### Task 2.2: Connect Chronicle to LLM-Router Routing Logic
**Repo**: `chronicle`
**File to modify**: `packages/core/src/store.ts`

Add integration point:
```typescript
// After routing decision is made, if it's high-impact:
if (complexity === 'complex' || previousAttemptsFailed) {
  await chronicle.logDecision({
    title: `Routing: ${task} → ${chosenModel}`,
    rationale: `Complexity: ${complexity}. Cheaper models insufficient.`,
    affected: ['router.py', 'classifier.py'],
    risk: 'HIGH'
  })
}
```

**Testing**:
```bash
cd ~/Projects/chronicle
npm test
```

#### Task 2.3: Atomic Commit
```bash
cd ~/Projects/meta-workspace
loom analyze-impact llm-router/src/llm_router/tools/routing.py
loom analyze-impact chronicle/packages/core/src/store.ts
loom sync-commit "feat: Add decision logging to routing decisions

- LLM-Router now logs complex routing choices to Chronicle
- Chronicles new integration point for routing memory
- Enables institutional knowledge of why routing decisions were made

Impact: HIGH (affects core routing + decision memory)
Tested: Both repos passing"
```

---

## PHASE 3: SessionStart Context Injection (Week 3)

### Goal
Chronicle injects routing strategy context at session start.

### Changes

#### Task 3.1: Configure SessionStart Hook in Chronicle
**Repo**: `chronicle`
**File**: `packages/cli/src/commands/inject.ts`

Add routing strategy injection:
```typescript
export async function injectRoutingContext() {
  const decisions = await store.getDecisions({
    type: 'routing',
    limit: 10,
    recent: true
  });
  
  const context = decisions.map(d => 
    `• ${d.title}: ${d.rationale}`
  ).join('\n');
  
  return {
    system: `Previous routing decisions:\n${context}`,
    timestamp: Date.now()
  };
}
```

**Hook registration** in `~/.claude/settings.json`:
```json
{
  "hooks": {
    "SessionStart": "chronicle inject --format=context --type=routing"
  }
}
```

**Testing**:
```bash
# Start a new Claude session
# Check that routing strategy appears at top
# Example: "Previous routing decisions: • Routing Decision: Complex refactoring → Sonnet..."
```

#### Task 3.2: Update LLM-Router to Read Injected Context
**Repo**: `llm-router`
**File**: `src/llm_router/classifier.py`

The classifier now reads session context:
```python
def classify_with_context(prompt: str, session_context: str = "") -> Complexity:
    # If session context mentions similar past decisions, use that hint
    if "similar" in session_context.lower():
        return extract_complexity_hint(session_context)
    
    # Otherwise, normal classification
    return classify(prompt)
```

**Testing**:
```bash
pytest tests/test_classifier.py -v
```

#### Task 3.3: Atomic Commit
```bash
cd ~/Projects/meta-workspace
loom sync-commit "feat: SessionStart context injection from routing decisions

- Chronicle now injects past routing decisions at session start
- LLM-Router reads session context to improve classification
- Enables learning from previous similar decisions

Impact: HIGH (affects both repos' session flow)
Tested: Session context verified, both repos passing"
```

---

## PHASE 4: Profile-Based Routing & Feedback Loop (Week 4)

### Goal
Complete the feedback loop: routes → decisions → memory → better routes.

### Changes

#### Task 4.1: Implement Feedback Loop in LLM-Router
**Repo**: `llm-router`
**Files**:
- `src/llm_router/tools/routing.py` (add `llm_rate_routing`)
- `src/llm_router/cost.py` (track rating accuracy)

New tool:
```python
@tool
def llm_rate_routing_decision(
    good: bool,
    reason: str = "",
    decision_id: str = ""
) -> str:
    """Rate whether the chosen model was appropriate for this task."""
    store.add_rating(decision_id, good, reason)
    # Update classifier accuracy metrics
    return f"Routing rated: {'good' if good else 'bad'}"
```

**Testing**:
```bash
cd ~/Projects/llm-router
pytest tests/tools/test_feedback.py -v
```

#### Task 4.2: Chronicle Captures Feedback
**Repo**: `chronicle`
Update `packages/core/src/store.ts`:

```typescript
// When routing is rated negative, capture failure pattern
await chronicle.logRejection({
  what: `Routing to ${model} for ${task}`,
  why: `${reason} - Model insufficient for this task complexity`,
  replacedBy: `Upgrade to higher-tier model next time`
});
```

**Testing**:
```bash
cd ~/Projects/chronicle
npm test
```

#### Task 4.3: Atomic Commit
```bash
cd ~/Projects/meta-workspace
loom sync-commit "feat: Feedback loop for routing accuracy

- LLM-Router tracks routing decisions and their outcomes
- Chronicle captures failed routing patterns
- System learns: bad routes → captured rejection → better classification next time
- Enables continuous improvement of routing accuracy

Impact: MEDIUM (enables learning but not blocking)
Tested: Feedback mechanism verified, both repos passing"
```

---

## PHASE 5: Project Memory Integration (Week 5+)

### Goal
Each repo has MEMORY.md documenting decisions, learnings, and patterns.

### Implementation

#### Task 5.1: Create LLM-Router MEMORY.md
**File**: `~/Projects/llm-router/.claude/memory/MEMORY.md`

```markdown
---
name: llm-router-context
description: Routing strategy decisions, learned patterns, failed approaches
type: project
---

# LLM-Router Institutional Memory

## Architecture Decisions
- Complexity classification: Multi-layer (heuristics → Ollama → Gemini Flash)
- Profile system: budget/balanced/premium
- Feedback loop: Ratings stored in SQLite for continuous improvement

## Learned Patterns
- Token counting alone is 15% inaccurate
- Heuristics + local LLM (Ollama) sufficient for 80% of cases
- Extended thinking needed for ambiguous complexity scores

## Failed Approaches
1. Pure token counting → Too inaccurate, replaced by heuristics
2. Semantic embeddings → Too slow, replaced by rule-based scoring
3. Single global profile → Doesn't work, replaced by per-project profiles

## Active Issues
- Classifier uncertainty on edge cases (< 70% confidence)
- Provider outage handling needs improvement
- Budget pressure oracle accuracy at high usage

## Team/Personal Patterns
- Tend to overestimate complexity (prefer Sonnet over Haiku)
- Like to review routing decisions manually before accepting
```

#### Task 5.2: Create Chronicle MEMORY.md
**File**: `~/Projects/chronicle/.claude/memory/MEMORY.md`

```markdown
---
name: chronicle-context
description: Decision capture strategy, integration patterns, success metrics
type: project
---

# Chronicle Institutional Memory

## Purpose
Prevent AI from repeating rejected solutions; maintain architectural coherence across sessions.

## Key Decisions
- Store decisions in git-tracked markdown (no external DB)
- SessionStart hook auto-injects context
- Deep ADRs for high-risk decisions

## Integration with LLM-Router
- Captures routing decisions made during sessions
- Stores failure patterns (rejections)
- Provides context for better classification next time

## Success Metrics
- Decision documentation rate (decisions per week)
- Average decision reuse rate (should trend up)
- Session re-onboarding time (should decrease)
```

---

## Daily Workflow with Loom

### Before Starting Work
```bash
cd ~/Projects/meta-workspace

# Check status
loom status

# Pull latest from both repos
loom pull

# Run tests
loom test-all
```

### Before Making Changes
```bash
# Analyze impact
loom pre-change <file-path>

# This shows:
# - Which files depend on this file
# - Other impacted tests
# - Affected repos
```

### During Development
1. Make changes in relevant repo
2. Write tests first (TDD)
3. Use extended thinking for complex decisions
4. Log important decisions: `chronicle_log_decision(...)`
5. Rate routing: `llm-router rate --good` or `--bad "reason"`

### Before Committing
```bash
# Verify changes don't break anything
loom post-change <affected-file>

# Commit atomically across repos if needed
loom sync-commit "feat|fix|refactor: clear message"
```

### End of Session
```bash
# Save session context
/save-session

# Update MEMORY.md if new patterns learned
# Commit all changes
loom sync-commit "docs: session summary + learnings"
```

---

## Monitoring & Metrics

### Weekly Review
```bash
# Check cost efficiency
llm-router usage --period week
curl http://localhost:3001  # cc-lens dashboard

# Review decisions made
chronicle search --type=decision --limit=10

# Check routing feedback
llm-router feedback --show-accuracy
```

### Monthly Review
1. **Decision Quality**: Are we capturing important decisions?
2. **Routing Accuracy**: Is feedback loop improving classification?
3. **Cost Trends**: Are profiles optimized per project?
4. **Memory Coverage**: Are MEMORY.md files up-to-date?

---

## Loom + Strategic Improvements Mapping

| Strategic Goal | Implementation | Loom Support |
|---|---|---|
| Decision Logging | `chronicle_log_decision` in workflow | `loom analyze-impact` guides which decisions matter |
| Auto-Routing | LLM-Router hook | `loom sync-commit` ensures hook changes atomic |
| Extended Thinking | Settings enabled | `loom pre-change` warns before changing settings |
| Project Memory | MEMORY.md per repo | `loom serve` exposes memory via MCP |
| Session Continuity | `/save-session` | `loom test-all` ensures session context persists |
| Atomic Changes | `loom sync-commit` | Cross-repo changes guaranteed consistent |

---

## Next Actions

1. ✅ Loom workspace created
2. ⬜ PHASE 1: Enable foundations (this week)
   - [ ] Extended thinking in settings
   - [ ] Auto-routing hook
   - [ ] Project profiles
   - [ ] `loom test-all` passes
3. ⬜ PHASE 2: Decision logging (next week)
4. ⬜ PHASE 3: SessionStart injection (following week)
5. ⬜ PHASE 4: Feedback loop (week after)
6. ⬜ PHASE 5: Memory integration (ongoing)

Start with PHASE 1 this week.
