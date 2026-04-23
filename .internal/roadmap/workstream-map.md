# Workstream Map — LLM Router Platform Evolution

**Confidential. Internal only. Organizational structure and work allocation for v3.5–v5.x.**

---

## Overview

This document maps all platform development into **9 workstreams**, each owned by a team lead and responsible for a core capability area. Workstreams are mostly independent but have critical dependencies (noted below).

**Goal**: Clear ownership, parallel execution, explicit handoffs.

---

## Workstream 1: Workflow Routing & Orchestration

**Owner**: Engineering (2 FTE, lead TBD)  
**Duration**: v3.5–v3.8 (9 months, then sustaining)  
**Mission**: Enable cost-aware multi-step workflow routing and orchestration

### Deliverables by Version
- **v3.5**: Multi-call sequence detection, workflow cost calculator, basic DAG visualization
- **v3.6**: Integrate semantic task understanding into workflow routing
- **v3.7**: AutoGen, LangGraph, CrewAI integration (agentic framework support)
- **v3.8**: Tool-level routing (per-tool policies within workflows)

### Key Dependencies
- **Upstream**: Semantic task understanding (WS2) — needed by v3.6
- **Upstream**: Agentic framework APIs (external) — needed by v3.7
- **Downstream**: All workstreams depend on workflow routing working correctly

### Success Metrics
- v3.5: ≥30% cost reduction on multi-step workflows
- v3.6: ≥20% additional reduction via intent awareness
- v3.7: ≥80% adoption by agentic framework users
- v3.8: ≥100 active tool-level routing policies

### Critical Risks
- AutoGen/LangGraph API changes break integrations → **Mitigation**: Version pinning, release notes tracking
- Workflow complexity explodes → **Mitigation**: Start simple (DAG), add sophistication in iterations

---

## Workstream 2: Semantic Task Understanding & Intent Classification

**Owner**: Engineering (1.5 FTE, lead TBD)  
**Duration**: v3.6–v3.8 (6+ months, then sustaining)  
**Mission**: Classify task intent and optimize routing based on what users are trying to do

### Deliverables by Version
- **v3.6**: Heuristic + LLM-based intent classifier (writing, coding, analysis, planning, review)
- **v3.7**: Integrate intent into agentic framework routing
- **v3.8**: Intent-based tool selection (tool A prefers writing model, tool B prefers reasoning model)

### Key Dependencies
- **Upstream**: None (can start immediately in v3.6)
- **Downstream**: WS1 (workflow routing) uses intent signals

### Success Metrics
- v3.6: ≥95% accuracy on intent classification
- v3.6: ≥20% additional cost reduction via intent routing
- v3.7: Intent-aware routing in 3 agentic frameworks
- v3.8: Intent-aware tool routing adopted by ≥50% of power users

### Critical Risks
- Intent classifier misclassifies (e.g., "write code" labeled as writing instead of coding) → **Mitigation**: Heuristic first, LLM fallback, user feedback loop
- Model cost/capability misaligned with intent → **Mitigation**: A/B test routing tables, track rejection rates

---

## Workstream 3: Control Plane Foundation & Multi-Tenant Infrastructure

**Owner**: Engineering (3–4 FTE, lead TBD)  
**Duration**: v4.0–v4.1 (12 months)  
**Mission**: Build governance platform (FastAPI backend + SaaS infrastructure)

### Deliverables by Version
- **v4.0**: Self-hosted control plane (FastAPI + SQLite) with basic RBAC, policies, audit
- **v4.1**: PostgreSQL migration, managed SaaS, SSO/SAML integration, advanced analytics

### Key Dependencies
- **Upstream**: All of v3.5–v3.8 must be stable (control plane depends on stable routing)
- **Upstream**: Enterprise thesis validated (WS8 validates market fit)
- **Downstream**: All enterprise features (WS4, WS5) depend on control plane

### Success Metrics
- v4.0: ≥100 enterprise orgs managing routing policies
- v4.0: ≥99% audit log integrity (zero lost calls)
- v4.1: ≥1K multi-tenant users on managed SaaS
- v4.1: SOC2 Type II certification achieved

### Critical Risks
- Multi-tenant isolation broken (data leak between orgs) → **Mitigation**: Pen testing, security audit, RLS validation
- SaaS operational complexity (uptime, scaling, billing) → **Mitigation**: Hire DevOps engineer, establish runbooks
- Customer onboarding friction → **Mitigation**: Dedicated customer success team

---

## Workstream 4: Enterprise Compliance & Advanced Governance

**Owner**: Engineering (2 FTE, lead TBD)  
**Duration**: v4.2–v4.5 (12 months, sustaining)  
**Mission**: Enterprise-grade compliance (HIPAA, SOC2, FCA, policy DSL, approval workflows)

### Deliverables by Version
- **v4.2**: HIPAA-compliant audit logs, FCA/SEC governance templates, compliance dashboard
- **v4.3**: Policy DSL (declarative rules), approval workflows for policy violations
- **v4.4**: Anomaly detection, predictive budgeting, advanced analytics
- **v4.5**: Extensibility (webhooks, custom integrations)

### Key Dependencies
- **Upstream**: Control plane (WS3) must be stable
- **Upstream**: Enterprise thesis (WS8) guides compliance priorities
- **Downstream**: Commercial packaging (WS9) packages compliance features into enterprise tier

### Success Metrics
- v4.2: SOC2 Type II certification
- v4.3: ≥80% of enterprise customers using policy DSL
- v4.4: Anomaly detection <5% false positive rate
- v4.5: ≥100 active webhooks in production

### Critical Risks
- DSL complexity too high (users can't write policies) → **Mitigation**: UI policy builder, templates, community library
- Compliance requirements change rapidly → **Mitigation**: Advisory board, quarterly review cycle
- Data warehouse costs explode → **Mitigation**: Use DuckDB (free) + optional cloud warehouse

---

## Workstream 5: Analytics, Attribution & Business Intelligence

**Owner**: Data/Analytics Engineer (2 FTE, lead TBD)  
**Duration**: v4.4–v5.x (ongoing)  
**Mission**: Cost attribution, benchmarking, anomaly detection, BI dashboards

### Deliverables by Version
- **v4.4**: Cost attribution by org/team/project/user, benchmarking, anomaly detection
- **v5.0**: BI dashboard ecosystem (Grafana, Looker, custom), forecasting engine
- **v5.x**: Advanced ML (predictive budgeting, cost optimization recommendations)

### Key Dependencies
- **Upstream**: Control plane (WS3) provides cost data
- **Upstream**: Enterprise compliance (WS4) provides audit logs for attribution
- **Downstream**: Marketplace (WS7) uses benchmarking data

### Success Metrics
- v4.4: ≥100K cost attribution records per day per org
- v4.4: Benchmarking adopted by ≥70% of customers (anonymized peer comparison)
- v5.0: ≥20 BI integrations available
- v5.x: Forecasting accuracy >90% for 30-day predictions

### Critical Risks
- Privacy: Benchmarking accidentally reveals customer identity → **Mitigation**: Differential privacy, strict anonymization rules
- Performance: Analytics queries too slow under load → **Mitigation**: Incremental materialization, query optimization

---

## Workstream 6: Ecosystem Integrations & SDKs

**Owner**: Engineering (1.5 FTE, lead TBD)  
**Duration**: v3.7–v5.x (ongoing)  
**Mission**: Native integrations with popular frameworks and platforms

### Deliverables by Version
- **v3.7**: AutoGen, LangGraph, CrewAI integration SDKs
- **v4.2–4.5**: Jira, GitHub, Slack, PagerDuty, Datadog integrations
- **v5.0+**: Partner ecosystems (LangChain, Hugging Face, Databricks, Modal)

### Key Dependencies
- **Upstream**: Workflow routing (WS1) must be stable
- **Upstream**: Control plane (WS3) provides policy/audit API for integrations
- **Downstream**: Marketplace (WS7) distributes integrations

### Success Metrics
- v3.7: ≥1,000 GitHub stars per integration
- v4.5: ≥20 active integrations in marketplace
- v5.x: Partner integrations account for ≥30% of platform traffic

### Critical Risks
- Integration maintenance burden (20+ integrations) → **Mitigation**: Community ownership, marketplace model
- Partner APIs change → **Mitigation**: Version pinning, deprecation warnings

---

## Workstream 7: Marketplace & Community Ecosystem

**Owner**: Product/Community Lead (1 FTE, lead TBD)  
**Duration**: v5.0+ (ongoing)  
**Mission**: Marketplace for policies, integrations, benchmarks; community-driven extensibility

### Deliverables by Version
- **v5.0**: Marketplace backend (discovery, versioning, ratings), policy repository
- **v5.1**: Integration marketplace, benchmark catalog
- **v5.x**: Managed services (consulting, implementation support)

### Key Dependencies
- **Upstream**: Control plane (WS3), compliance (WS4), integrations (WS6) provide ecosystem
- **Upstream**: Analytics (WS5) provides benchmarking data
- **Downstream**: None (terminal workstream)

### Success Metrics
- v5.0: ≥500 policies in marketplace
- v5.1: ≥100 integrations available
- v5.x: ≥$50K/month MRR from managed services

### Critical Risks
- Low-quality community contributions → **Mitigation**: Review process, certification tiers
- Marketplace becomes noise (too many irrelevant policies) → **Mitigation**: Curation, recommendation engine

---

## Workstream 8: Market Research, Enterprise Sales & GTM

**Owner**: Yali (1 FTE) + Sales Consultant (0.5 FTE)  
**Duration**: Ongoing (parallel with engineering)  
**Mission**: Validate market thesis, land enterprise customers, execute go-to-market

### Deliverables by Phase
- **Phase 1 (Q3 2026)**: Enterprise thesis validated (2 lighthouse customers identified)
- **Phase 2 (Q4 2026–Q1 2027)**: ≥2 enterprise pilots deployed (v3.5 + v3.6)
- **Phase 3 (Q2–Q3 2027)**: v4.0 control plane pilots (governance value proven)
- **Phase 4 (Q4 2027+)**: GTM execution (marketing, sales, customer success)

### Key Dependencies
- **Upstream**: Product stability (WS1–3) — pilots depend on working product
- **Downstream**: Feedback feeds into roadmap (WS1–6)

### Success Metrics
- Q3 2026: ≥1 enterprise pilot signed
- Q4 2027: ≥10 enterprise customers, $500K ARR
- Q2 2028: ≥50 enterprise customers, $3M ARR

### Critical Risks
- Customer acquisition cost too high → **Mitigation**: PLG (free tier) + sales combo
- Enterprise sales cycle too long → **Mitigation**: Start with startups (faster cycle)

---

## Workstream 9: Commercial Packaging, Licensing & Operations

**Owner**: Yali (0.5 FTE) + Finance/Ops Consultant (0.5 FTE)  
**Duration**: v4.1+ (ongoing)  
**Mission**: Business model, licensing, SaaS operations, pricing strategy

### Deliverables by Phase
- **v4.0**: Open-source + commercial positioning (free tier vs paid)
- **v4.1**: SaaS launch (pricing: seats + usage-based), billing automation
- **v4.2+**: Commercial tiers (open-source free, enterprise managed service)

### Key Dependencies
- **Upstream**: Market research (WS8) validates willingness to pay
- **Upstream**: Control plane (WS3) enables multi-tenant SaaS
- **Downstream**: Feeds into sales (WS8) with pricing strategy

### Success Metrics
- v4.0: Clear open-source/commercial boundary
- v4.1: ≥$100K MRR SaaS revenue
- v4.2+: ≥60% gross margin on SaaS

### Critical Risks
- Pricing too aggressive → customers flee to self-hosted → **Mitigation**: Freemium model, fair pricing
- Open-source users feel excluded → **Mitigation**: Generous free tier, clear upgrade path

---

## Workstream Dependencies Map

```
     WS1 (Workflow Routing)
           ↓
     WS2 (Semantic Understanding) ← inputs into WS1
           ↓
     WS3 (Control Plane) ← must be stable
           ↓
     WS4 (Compliance) ← builds on WS3
     WS5 (Analytics)  ← uses WS3 + WS4 data
     WS6 (Integrations) ← uses WS1 + WS3
           ↓
     WS7 (Marketplace) ← aggregates WS4, WS5, WS6
           ↓
     WS8 (Sales/GTM) ← validates all above
           ↓
     WS9 (Commercial) ← packages all above into products
```

### Critical Path (Cannot Parallelize)
1. **WS1 (v3.5–v3.8)** must complete before WS3 starts (control plane depends on stable routing)
2. **WS3 (v4.0)** must complete before WS4, WS5, WS6 mature (enterprise features depend on governance)
3. **WS8 (Market validation)** must validate control plane value before WS9 commits to GTM

### Can Parallelize
- WS2 and WS1 (can develop in parallel, integrate in v3.6)
- WS4, WS5, WS6 (can develop in parallel once WS3 foundation is ready)
- WS8 throughout (market research concurrent with engineering)

---

## Team Structure (Recommended)

### Phase 1 (Q3 2026): Bootstrap Phase
```
Yali (founder/PM)
├── Engineering: 4–5 FTE
│   ├── WS1/WS2 (2 FTE)
│   ├── WS3 Spike (1 FTE)
│   ├── Pilot support (1 FTE)
│   └── Tech lead (0.5 FTE, oversight)
├── Sales/Marketing (0.5 FTE)
│   └── Advisor network, pilot recruitment
└── Operations/Finance (0.5 FTE)
    └── Pricing, billing model setup
```

### Phase 2 (Q4 2027–Q3 2028): Scaling Phase
```
CEO/Founder (Yali)
├── VP Engineering (hire)
│   ├── WS1/WS2 team (2 FTE)
│   ├── WS3 team (3–4 FTE)
│   ├── WS4 team (2 FTE)
│   ├── WS5 team (2 FTE)
│   └── WS6 team (1.5 FTE)
├── VP Sales/Marketing (hire or consultant)
│   ├── WS8 Sales team (2 FTE)
│   └── Marketing/content (1 FTE)
├── Head of Product (hire)
│   └── WS7 product ownership
└── Head of Operations (hire)
    ├── WS9 commercial model
    ├── Finance/Billing
    └── Customer Success
```

---

## Success Criteria (Per Workstream)

| Workstream | v3.5–3.8 Goal | v4.0–4.1 Goal | v4.2–5.x Goal |
|-----------|---|---|---|
| WS1 (Workflow) | 30%+ cost reduction | Stable routing engine | Handles 1M+ calls/day |
| WS2 (Semantic) | 95%+ accuracy | 20% additional savings | Intent-driven all routing |
| WS3 (Control Plane) | Architecture validated | 1K multi-tenant users | Governance standard in market |
| WS4 (Compliance) | Risk map created | SOC2 Type II cert | HIPAA + FCA compliance |
| WS5 (Analytics) | Design spike | 100K records/day tracked | Benchmarking in 70% of users |
| WS6 (Integrations) | 3 frameworks | 20+ integrations | Partner ecosystem active |
| WS7 (Marketplace) | Infrastructure designed | 500 policies + 100 integrations | $50K/month services revenue |
| WS8 (Sales) | 1 pilot signed | 10 customers, $500K ARR | 50 customers, $3M ARR |
| WS9 (Commercial) | Model designed | $100K MRR SaaS | 60% gross margin |

---

## Handoff Protocol Between Workstreams

### Critical Handoffs

**WS2 → WS1 (Intent to Workflow Routing)**
- Deliverable: Intent classifier API (input: prompt, output: TaskIntent enum)
- SLA: Classifier must respond in <100ms with ≥95% accuracy
- Contract: WS1 can override intent classification with user hint
- Review: Weekly sync on integration, classifier accuracy

**WS1 → WS3 (Workflow Routing to Control Plane)**
- Deliverable: Stable router.py with workflow support (API contract)
- SLA: Zero breaking changes to router API for 6 months (v3.5–v4.0)
- Contract: WS3 integrates router as library, doesn't modify core logic
- Review: Monthly architecture sync

**WS3 → WS4 (Control Plane to Compliance)**
- Deliverable: Audit API (log all calls, query by org/team/user)
- SLA: ≥99% of calls logged, <100ms audit overhead
- Contract: WS4 extends audit schema (HIPAA-specific fields), doesn't break existing logs
- Review: Monthly compliance sync

**WS8 → WS9 (Market Data to Pricing)**
- Deliverable: Willingness-to-pay research, competitor pricing, TAM estimates
- SLA: Monthly research updates based on pilot feedback
- Contract: WS9 sets pricing based on WS8 data, WS8 monitors market for changes
- Review: Quarterly GTM sync

---

## Q&A: Workstream Ownership & Decisions

**Q: What if a feature spans multiple workstreams?**  
A: Identify primary owner, other workstreams are stakeholders. Example: "Intent in workflow routing" (WS2 owns classifier, WS1 owns integration). Weekly sync ensures coordination.

**Q: How do workstreams handle blocked dependencies?**  
A: **Rule**: If WS2 is blocked, WS1 proceeds with heuristic intent classification (non-blocking fallback). If WS3 is blocked, WS4 starts design (non-blocking). Always have a fallback path.

**Q: How are roadmap changes (user feedback) incorporated?**  
A: Monthly workstream leads sync (1 hour) to review feedback, propose roadmap adjustments. Changes go through product lead (Yali) for prioritization.

**Q: What if a workstream falls behind schedule?**  
A: Red flag at weekly standup. WS lead proposes: (1) scope reduction, (2) resource increase, (3) timeline slip. Yali decides. If critical path is at risk, WS9 (commercial) may be deprioritized to free resources.

---

## Workstream Health Dashboard (Template)

Use this weekly to track health:

```
WS1 (Workflow Routing)
├── Status: GREEN
├── Blockers: None
├── On track for: v3.5 GA (Week 2)
└── Risk: Minor (intent classifier integration, WS2 on track)

WS2 (Semantic Understanding)
├── Status: YELLOW
├── Blocker: Accuracy <95% on domain-specific tasks (legal, medical)
├── On track for: v3.6 alpha (Week 4, with heuristic fallback)
└── Risk: Medium (intent classifier may need LLM component sooner)

WS3 (Control Plane)
├── Status: YELLOW
├── Blocker: Arch review pending (Week 1)
├── On track for: v4.0 alpha (Week 9, after architecture approved)
└── Risk: High (timeline aggressive, scope creep risk)

... (repeat for WS4–9)
```

Update weekly, surface RED items to Yali immediately.
