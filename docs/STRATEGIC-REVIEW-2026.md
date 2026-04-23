# LLM Router — Deep Strategic Review
## Platform Evolution & Roadmap (2026–2027)

---

## EXECUTIVE SUMMARY

### What llm-router Is Today

**A production-grade MCP server** that intelligently routes individual LLM calls to the cheapest capable model, with strong foundations in:
- **Complexity-based routing** — simple→Haiku/Flash, moderate→Sonnet/GPT-4o, complex→Opus/o3
- **Free-first chains** — Ollama (local) → Codex (subscription) → Gemini Flash → OpenAI → Claude/Perplexity
- **Budget management** — Per-provider spend caps, daily alerts, quota pressure cascades
- **Cost tracking & analytics** — Lifetime savings, per-session breakdowns, team dashboards with webhook push
- **Policy engine** — Org-level allow/deny lists, per-task spend caps, model allow-lists
- **Multi-agent support** — Integrations with Agno, Codex, Gemini CLI, Factory Droid, Trae
- **Observability** — Routing decision logging, quality score tracking, benchmarking framework

**Real impact**: Achieved **87% cost reduction** ($6.95 vs $50–60 baseline) and **94% token reduction** over a 14-day sprint with realistic usage patterns.

### Biggest Gaps

| Gap | Severity | Why It Matters |
|-----|----------|---|
| **No multi-agent workflow orchestration** | 🔴 High | Can't route different steps of a complex agent task differently — wastes tokens on cheap steps using expensive models |
| **No skill/tool-level routing policies** | 🔴 High | Team can't define "code review tasks always use best model" or "summarization uses Haiku" — policies are global |
| **No advanced token optimization** | 🔴 High | Missing proactive strategies: semantic dedup is basic, no context pruning, no speculative routing, no prompt compression |
| **No centralized SaaS control plane** | 🟡 High | Enterprise teams need one place to manage routing for 50+ developers + services — can't do that from laptop configs |
| **No workload class semantics** | 🟡 Medium | All code generation is "complex" — but fixing a typo ≠ architecting a system. No semantic task understanding |
| **Limited approval/audit workflows** | 🟡 Medium | Large orgs need "human approves expensive model use" — hooks just route silently |
| **No versioned policy rollouts** | 🟡 Medium | Can't A/B test routing policies or roll back bad changes across team |
| **Weak quality guarantees** | 🟡 Medium | Quality mode is simple binary; no SLO-based routing, no automated escalation when quality drops |
| **No extensibility for custom routing** | 🟡 Medium | Can't plug in custom classifiers, cost models, or task detectors — all hardcoded |
| **Limited self-hosted enterprise deployment** | 🟡 Medium | No Docker Compose + SSO + RBAC — enterprise pilots end in "nice tool, but we can't deploy it" |

### Highest-Value Opportunities

1. **Workflow-level routing** — Route different steps of an agent task to different tiers (planning→Opus, boilerplate→Haiku, review→Sonnet) → **30–40% additional token savings on complex tasks**
2. **Centralized control plane** — One service for team/org routing policy, audit logs, approvals → **unlocks enterprise + team adoption**
3. **Token optimization engine** — Proactive semantic dedup, context pruning, prompt compression → **15–25% additional token reduction**
4. **Skill/tool-level policies** — Route "code review" differently than "summarization" → **teams can enforce quality/cost trade-offs per task type**
5. **Workload class detection** — Detect task intent (write, analyze, plan, review) and route accordingly → **better quality + cost trade-offs**

---

## SECTION 1: CURRENT STATE & STRENGTHS

### Architecture Overview

llm-router's core stack (v7.4.1):

```
User Prompt → [Classification] → [Routing] → [Budget Checks] → [Execution] → [Logging]
                    ↓                ↓            ↓                ↓
            Complexity (4 tiers)  Free-first   Caps enforce   LiteLLM/native  SQLite
            Intent detection      chains (5)   pressure       20+ providers   + webhook
            Workload (basic)      Fallback     Quota %        Quality monitor
```

**38 core modules**, 22K+ lines of Python, 2K+ lines of hooks, **41 MCP tools**.

### Current Strengths

✅ **Production-grade** — 87% cost reduction achieved in real usage  
✅ **Maturity** — 2+ years of hardening, real-world scale  
✅ **Free-first philosophy** — Ollama + Codex + cheap APIs always prioritized  
✅ **Cost transparency** — Every decision logged, savings quantified  
✅ **Team-ready** — Policy engine, org governance, webhook push  
✅ **Multi-provider** — 20+ providers, LiteLLM integration, fallback chains  
✅ **Observability** — Routing decision logging, quality scores, benchmarks  
✅ **Easy deployment** — One-command install, works everywhere MCP is supported  

---

## SECTION 2: CRITICAL GAPS (DETAILED ANALYSIS)

### Gap 1: No Multi-Agent Workflow Routing

**Current state**: Routes individual calls. When Agent A calls → calls B → calls C, each gets routed independently by complexity.

**Problem**: Can't optimize across steps. Example:
- Step 1 (gather web info) = SIMPLE → routes to Haiku (wasteful for research task)
- Step 2 (synthesize analysis) = MODERATE → routes to Sonnet
- Step 3 (code review) = COMPLEX → routes to Opus

Optimal would be:
- Step 1 → Perplexity (specialized for research)
- Step 2 → Sonnet (analysis)
- Step 3 → Opus (review)

**Impact**: 30–40% wasted tokens on multi-step workflows (most common for serious AI work).

**Who cares**: Agents, product teams, AI engineers building complex systems.

**Missing**: Step-level complexity override, conditional routing (if step 1 fails, escalate step 2), workflow SLOs.

### Gap 2: No Semantic Workload Classification

**Current**: All code is "complex", all writing is categorized the same.

**Reality**:
- Typo fixing in existing code → should be SIMPLE
- Adding 5-line feature → should be MODERATE
- Architecting new system → should be COMPLEX

Same applies to writing (email ≠ research paper), analysis (summary ≠ deep dive).

**Impact**: ~10–15% token waste on suboptimal classification.

**Who cares**: Teams optimizing per-task routing.

### Gap 3: No Skill/Tool-Level Policies

**Current**: Policies are global (task type or model level) or none.

**Can't express**:
- "code_review skill always uses Opus"
- "summarization tool always uses Haiku"
- "payment_processor service blocks unproven models"
- "researcher_agent gets premium tier"

**Impact**: Teams can't enforce quality guarantees per skill.

### Gap 4: Limited Token Optimization Strategies

**Current**: Passive cost tracking. Missing proactive reduction:

| Strategy | Saving | Status |
|---|---|---|
| Semantic dedup (embeddings + cache) | 10–15% | Partial (SHA-256 hash only) |
| Context pruning (relevance scoring) | 10–15% | Missing |
| Prompt compression (LLM-based) | 8–12% | Missing |
| Speculative routing (cheap + verify) | 15–20% | Missing |
| Conditional escalation (quality gates) | 10–15% | Missing |
| Provider specialization | 5–8% | Hardcoded, not semantic |
| Caching strategies | 5–10% | Anthropic prompt caching only |

**Potential**: 60–80% additional savings by combining these.

### Gap 5: No Centralized Governance (Enterprise Blocker)

**For individuals**: Works great.  
**For teams of 20**: Starts to break (config differences, no approval gates).  
**For enterprises (50+)**: Completely infeasible.

**Missing**:
- Central policy management (one source of truth)
- Approval workflows (expensive model use requires sign-off)
- Immutable audit log (compliance requirement)
- Cost attribution (which team spent how much)
- Versioned rollouts (test policy A/B before full deployment)
- Policy simulation ("if we block GPT-4o, savings = $X")

---

## SECTION 3: GAP ANALYSIS TABLE

| Category | Gap | Severity | User Impact | Resolution |
|---|---|---|---|---|
| **Routing Quality** | Multi-agent workflow routing | 🔴 High | 30–40% token waste on multi-step tasks | v3.5: Step-level overrides |
| | Semantic workload classification | 🔴 High | 10–15% suboptimal routing | v3.6: Intent detector |
| | Skill/tool-level policies | 🔴 High | Can't enforce per-skill quality | v3.7: Advanced policy scopes |
| | Task detection from context | 🟡 Medium | 5–10% classification uncertainty | v3.5+: Synthetic prompts |
| **Token Optimization** | Limited proactive reduction | 🔴 High | 60–80% additional savings left on table | v4.0–4.2: Multi-strategy toolkit |
| | No context pruning | 🟡 Medium | 10–15% bloated prompts | v4.1: Relevance scoring |
| | Basic semantic dedup | 🟡 Medium | 5–10% duplicate LLM calls | v3.8: Vector search |
| | No speculative routing | 🟡 Medium | 15–20% lost escalation savings | v4.3: In-flight quality monitoring |
| **Governance** | No centralized control plane | 🔴 High | Enterprise deployments infeasible | v4.0: Control plane MVP |
| | No approval workflows | 🔴 High | No gates on expensive models | v4.2: Approval engine |
| | No versioned policies | 🟡 Medium | Can't safely test/rollback | v4.2: Versioning + scheduler |
| | No cost attribution | 🟡 Medium | Can't chargeback to teams | v5.2: Chargeback system |
| | Limited audit trails | 🟡 Medium | Compliance violations | v4.0: Immutable audit log |
| **Enterprise** | No self-hosted deployment | 🔴 High | Can't deploy to data center | v4.0: Docker Compose + K8s ready |
| | No SSO/RBAC | 🔴 High | Manual user management | v4.0: OIDC + role-based access |
| | No multi-tenant support | 🟡 Medium | SaaS model not viable | v5.1: Multi-tenant isolation |
| **Extensibility** | No plugin system | 🟡 Medium | Can't extend classifier/cost models | v4.5: Plugin architecture |
| | No skill metadata registry | 🟡 Medium | Can't optimize by skill type | v5.0: Skill metadata framework |
| **Observability** | Limited routing explainability | 🟡 Medium | Users don't trust decisions | v3.8+: Decision context logging |
| | No real-time dashboards | 🟡 Medium | Can't monitor live routing | v5.0: Real-time HUD |
| | No decision context | 🟡 Medium | Can't debug bad routing | v3.8: Full decision context in logs |

---

## SECTION 4: PROPOSED TARGET ARCHITECTURE

### 4.1 High-Level Design

```
┌────────────────────────────────────────────────────────────┐
│         Enterprise Control Plane (SaaS / Self-Hosted)       │
│  • Policy management & versioning                           │
│  • Approval workflows & gates                               │
│  • Audit log & compliance                                   │
│  • Team/cost center management                              │
│  • Analytics & recommendations                              │
└────────────────────────────────────────────────────────────┘
            ↑ API (policy pull, decision push)
            │
┌───────────┴──────────────────────────────────────────────────┐
│              Local LLM Router (MCP Server)                    │
│                                                               │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ Classification & Workload Understanding             │    │
│  │ • Complexity (simple/moderate/complex/deep)         │    │
│  │ • Workload intent (write/analyze/plan/review)       │    │
│  │ • Skill/tool detection                              │    │
│  │ • Context-aware continuation                        │    │
│  └─────────────────────────────────────────────────────┘    │
│                           ↓                                  │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ Workflow Orchestration                              │    │
│  │ • Multi-step task routing                           │    │
│  │ • Step-level complexity overrides                   │    │
│  │ • Conditional routing (quality-based escalation)    │    │
│  │ • Workflow SLO enforcement                          │    │
│  └─────────────────────────────────────────────────────┘    │
│                           ↓                                  │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ Policy & Governance Layer                           │    │
│  │ • Pull org policies from control plane              │    │
│  │ • Apply skill/service/agent-level rules             │    │
│  │ • Approval gate (expensive models)                  │    │
│  │ • Cost attribution tracking                         │    │
│  └─────────────────────────────────────────────────────┘    │
│                           ↓                                  │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ Token Optimization                                  │    │
│  │ • Semantic dedup cache (embeddings)                 │    │
│  │ • Context pruning (relevance scoring)               │    │
│  │ • Prompt compression (LLM-based)                    │    │
│  │ • Speculative routing (cheap → escalate if needed)  │    │
│  └─────────────────────────────────────────────────────┘    │
│                           ↓                                  │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ Routing Decision Engine                             │    │
│  │ • Model scoring (quality + latency + budget)        │    │
│  │ • Free-first chain building                         │    │
│  │ • Fallback & resilience                             │    │
│  └─────────────────────────────────────────────────────┘    │
│                           ↓                                  │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ Execution & Observability                           │    │
│  │ • LiteLLM dispatch (text)                           │    │
│  │ • Native APIs (media)                               │    │
│  │ • Quality monitoring & escalation                   │    │
│  │ • Immutable decision logging                        │    │
│  │ • Metric collection                                 │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                               │
│  Storage: SQLite (local) + PostgreSQL (central)              │
│  Vector DB: For semantic dedup caching                       │
└────────────────────────────────────────────────────────────┘
```

### 4.2 Control Plane Capabilities (MVP)

**Admin API**:
- Policy CRUD (org/team/service scopes)
- Policy versioning + rollout scheduling
- Approval queue management
- Audit log viewer + export
- Team & cost center management

**Admin Web UI**:
- Visual policy editor
- Approval dashboard
- Audit log browser
- Analytics view (spend by team, top models)
- Compliance reports

**Backing Services**:
- PostgreSQL (policies, audit, approvals)
- FastAPI (admin API)
- React (web UI)
- OIDC provider (SSO)

---

## SECTION 5: PRIORITIZED 12-MONTH ROADMAP

### Phase 1: Foundation (Months 1–3)

**Goal**: Build capabilities that unlock new routing opportunities.

**v3.5 — Workflow-Level Routing**
- Step-level complexity overrides in `llm_orchestrate`
- Policy inheritance for workflows
- **Effort**: 4–6 weeks | **Saving**: 30–40% on multi-step tasks

**v3.6 — Workload Class Detection**
- Semantic classifier for task intent (writing, analysis, planning, review, boilerplate)
- Integration into routing decision
- **Effort**: 3–4 weeks | **Saving**: 8–12% via better granularity

**v3.7 — Advanced Policy Engine**
- Skill-level and service-level scopes
- Policy inheritance and overrides
- **Effort**: 3–4 weeks | **Impact**: Teams enforce per-skill guarantees

**v3.8 — Semantic Dedup Cache**
- Ollama embeddings + vector search
- **Effort**: 2–3 weeks | **Saving**: 10–15% on repetitive tasks

**Milestone**: Org routing maturity + workflow optimization enabled

---

### Phase 2: Enterprise Scale (Months 4–6)

**Goal**: Enable enterprise adoption via central governance.

**v4.0 — Centralized Control Plane (MVP)**
- FastAPI backend + PostgreSQL + React UI
- Policy CRUD + versioning + rollout scheduling
- Approval workflows + audit log
- SSO/RBAC (OIDC)
- **Effort**: 8–10 weeks | **Impact**: Enterprise deployments viable

**v4.1 — Local-to-CP Integration**
- Policy pull API
- Decision push API
- **Effort**: 2–3 weeks | **Impact**: Org-wide policy enforcement

**v4.2 — Approval Workflows & Gates**
- Pre-route approval checks
- Cost caps with escalation
- **Effort**: 2–3 weeks | **Impact**: Governance + budget control

**Milestone**: Enterprise-ready with central governance

---

### Phase 3: Advanced Optimization (Months 7–9)

**Goal**: Maximize token savings + extensibility.

**v4.3 — Conditional Escalation & Speculative Routing**
- Quality signal monitoring (token count, confidence, errors)
- In-flight escalation + fallback
- **Effort**: 6–8 weeks | **Saving**: 15–20% additional + quality guarantees

**v4.4 — Context Pruning & Compression**
- Relevance scoring + semantic compression
- **Effort**: 4–5 weeks | **Saving**: 10–15%

**v4.5 — Plugin System**
- Plugin architecture for classifiers, judges, cost models
- Example plugins + documentation
- **Effort**: 4–5 weeks | **Impact**: Community extensibility

**Milestone**: Advanced platform with extensibility

---

### Phase 4: Market Leadership (Months 10–12)

**v5.0 — Analytics & Insights**
- Cohort analysis, trend detection
- ML-driven recommendations
- **Effort**: 6–8 weeks

**v5.1 — Multi-Tenant Enterprise**
- Multi-tenant isolation + per-org policies
- **Effort**: 6–7 weeks | **Impact**: SaaS model viable

**v5.2 — Cost Attribution & Chargeback**
- Team/cost center allocation
- Invoice generation
- **Effort**: 4–5 weeks

**Milestone**: Market-ready SaaS platform

---

## SECTION 6: TOP 10 CONCRETE NEXT ACTIONS

1. **Design Workflow Orchestration API** (Week 1)
   - Step-level routing hint syntax
   - Policy inheritance for workflows
   - RFC + stakeholder feedback

2. **Implement Workload Class Classifier** (Weeks 2–4)
   - Train on 500+ user prompt labels
   - Intent → workload class pipeline
   - 85%+ accuracy target

3. **Prototype Semantic Dedup Cache** (Weeks 3–5)
   - Ollama embeddings integration
   - Vector search in SQLite
   - Measure dedup hit rate

4. **Design Control Plane Architecture** (Week 2)
   - API specs, data model, security design
   - Multi-tenancy strategy
   - Stakeholder sign-off

5. **Build Control Plane MVP** (Weeks 4–12)
   - FastAPI + PostgreSQL + React
   - Policy CRUD, versioning, rollout scheduler
   - SSO/RBAC, audit log

6. **Implement Local-to-CP Sync** (Weeks 6–8)
   - Policy pull + decision push APIs
   - Conflict resolution

7. **Build Quality Escalation Engine** (Weeks 9–11)
   - Quality signal definitions
   - Automatic escalation triggers
   - Logging + tracking

8. **Create Context Pruning Module** (Weeks 9–11)
   - Relevance scoring (BM25 + cosine)
   - Pruning strategy
   - Integration into pipeline

9. **Write Plugin System** (Weeks 9–12)
   - Classifier, judge, cost model abstractions
   - Plugin discovery + loading
   - 3 example plugins

10. **Start Market Research** (Week 2, ongoing)
    - Survey 10–15 companies on control plane needs
    - Pricing strategy exploration
    - Feature prioritization

---

## SECTION 7: SUCCESS METRICS (90 Days)

✅ Workflow-level routing deployed (30–40% token reduction target)  
✅ Control plane MVP live (locally self-hosted)  
✅ Semantic dedup cache reducing 10–15% of repetitive calls  
✅ Conditional escalation preventing cheap-model failures  
✅ Approval workflows functional  
✅ 1 enterprise pilot enrolled (self-hosted mode)  

---

## FINAL VERDICT

**What llm-router needs to become a serious platform**:

1. **Workflow-level routing** — Unlock 30–40% additional token savings
2. **Central control plane** — Enable enterprise adoption
3. **Advanced token optimization** — Semantic dedup, context pruning, escalation
4. **Skill/tool-level policies** — Teams enforce quality/cost per task type
5. **Extensibility** — Let community build custom classifiers, cost models

**Timeline**: 12 months to platform-ready SaaS/self-hosted.  
**Investment**: ~20–25 eng-weeks for MVP control plane, another 15–20 for advanced features.  
**Window**: 12 months before competitors build equivalent (market moves fast in LLM space).  

**Do it, or stay niche.**
