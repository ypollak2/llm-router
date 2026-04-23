# 90-Day Plan — LLM Router (Q3 2026 Immediate Action)

**Confidential. Internal only. Concrete deliverables for next 90 days (July–September 2026).**

---

## Executive Summary

The next 90 days are critical: ship v3.5 (workflow routing) and begin v3.6 (semantic task understanding) while landing first enterprise pilots. Success here validates the platform thesis and de-risks v4.0 control plane investment.

**High-level goals**:
1. v3.5 GA (workflow routing) — fully released and documented
2. v3.6 alpha (semantic understanding) — internal validation
3. Enterprise outreach — identify and contract 2 lighthouse customers
4. Governance foundation — spike on v4.0 control plane architecture

**Key success metric**: 2 enterprise pilots signed + v3.5 GA + v3.6 alpha complete = "Platform is viable, control plane investment justified."

---

## Phase 1: Weeks 1–4 (July 2026) — v3.5 GA + Enterprise Outreach Prep

### Product Deliverables

#### 1. v3.5 Workflow Routing — GA Release
**Owner**: Engineering (2 FTE)  
**Status**: Alpha (from prior context)  
**Remaining Work**:
- [ ] Finalize workflow cost calculator (handle edge cases, error scenarios)
- [ ] Add workflow visualization (CLI output showing DAG with costs)
- [ ] Documentation: User guide, API reference, examples (3 real-world patterns)
- [ ] Benchmarking: Show real cost reduction (30%+ on multi-step workflows)

**Deliverables**:
- [ ] Merged PR: workflow routing feature complete
- [ ] Release notes: "v3.5 GA — Workflow-level cost optimization"
- [ ] Blog post: "How multi-step workflows now cost 30% less"
- [ ] Example repos: AutoGen agent + LangGraph workflow using v3.5

**Success Criteria**:
- Zero blocking bugs in GA
- ≥5 open-source users adopt v3.5 in first 4 weeks
- ≥30% cost reduction on real multi-step workflows

#### 2. Marketing Collateral — Enterprise Narrative
**Owner**: Yali (product + marketing)  
**Deliverables**:
- [ ] One-pager: "LLM Router for Enterprise Teams" (problem, solution, proof)
- [ ] Customer case study: Stripe/Databricks/Figma (fictional or with real pilot customer)
- [ ] Pricing calculator: Input team size + usage → estimated savings
- [ ] Competitive matrix: llm-router vs LiteLLM vs Langsmith vs custom

**Success Criteria**:
- One-pager resonates with 3+ CTO personas (share with advisors for feedback)
- Pricing calculator generates leads (email capture)

### Sales & Business Deliverables

#### 3. Enterprise Customer Identification
**Owner**: Yali (with advisor network)  
**Approach**:
- [ ] List 10 target companies (4 AI Platform Teams, 3 startups, 3 regulated industry)
  - Platform Teams: Stripe, Databricks, Figma, Canva
  - Startups: Tavily, Together AI, Anthropic (test customer?)
  - Regulated: Insurance company, financial services, healthcare SaaS
- [ ] Identify champion contacts (CTO, Platform Lead, VP Engineering) via LinkedIn + warm intro
- [ ] Draft outreach email: "We help teams cut LLM costs 60% + govern usage"

**Deliverables**:
- [ ] Target list with decision-maker contacts (private doc)
- [ ] Outreach email template (A/B testable)
- [ ] Warm intro requests sent to 5+ targets

**Success Criteria**:
- ≥3 inbound meetings scheduled for Weeks 3–4
- ≥1 strong pilot prospect (ready to discuss in Phase 2)

#### 4. Advisor/Reference Customer Recruitment
**Owner**: Yali  
**Approach**:
- [ ] Reach out to 5 recent high-profile LLM adopters (founders, CTOs)
- [ ] Offer: "Free enterprise control plane in beta (v4.0) + co-marketing"
- [ ] Goal: Lock in 1–2 reference customers before v4.0 development starts

**Deliverables**:
- [ ] Advisor agreements signed (2–3 advisors)
- [ ] Reference customer LOI (letter of intent for v4.0 pilot)

**Success Criteria**:
- ≥1 advisor committed to 2–3 hr/month advisory
- ≥1 pilot customer LOI signed (for v4.0)

### Engineering Foundation (v4.0 Prep)

#### 5. v4.0 Control Plane — Architecture Spike
**Owner**: Engineering (1 FTE, architecture-focused)  
**Scope**: Design, not implementation  
**Deliverables**:
- [ ] Architecture document: FastAPI + SQLite control plane (50–100 page design spec)
  - API structure (endpoints for orgs, teams, users, policies, audit)
  - Database schema (normalized, audit-friendly)
  - Security model (API key validation, RBAC, isolation)
  - Integration points with existing llm_router
- [ ] Prototype: Working FastAPI server + SQLite (not production-ready, proof-of-concept only)
- [ ] Risk assessment: Identify technical blockers, dependency risks

**Deliverables**:
- [ ] `docs/CONTROL-PLANE-ARCHITECTURE.md` (design spec)
- [ ] Prototype code in `prototypes/control-plane-v1/` (not merged, reference only)
- [ ] Risk log (for Phase 2)

**Success Criteria**:
- Architecture reviewed + approved by advisors
- No blocking technical risks identified
- Clear handoff plan for Phase 2 (v4.0 development starts in Phase 3)

---

## Phase 2: Weeks 5–8 (August 2026) — v3.6 Alpha + Enterprise Sales Cycle

### Product Deliverables

#### 1. v3.6 Semantic Task Understanding — Alpha
**Owner**: Engineering (2 FTE)  
**Scope**: Core classifier + routing integration, no UI yet  
**Deliverables**:
- [ ] Task intent classifier: Heuristic-based (writing, coding, analysis, planning, review)
- [ ] Integration: Update router.py to use intent in model selection
- [ ] Evaluation: Benchmark on 1,000 real prompts (95%+ accuracy target)
- [ ] Documentation: Technical deep-dive on intent classification

**Deliverables**:
- [ ] Merged PR: Intent classifier + router integration
- [ ] Evaluation report: Confusion matrix, edge cases, failure modes
- [ ] Usage guide: "How to benefit from task intent routing"

**Success Criteria**:
- ≥95% accuracy on intent classification (validated on diverse prompts)
- ≥20% additional cost reduction vs v3.5 on mixed workloads (measured internally)
- Zero crashes or regressions in production

#### 2. v3.6 Internal Alpha Testing (Agentic Workloads)
**Owner**: Engineering + Yali  
**Approach**:
- [ ] Run llm-router internally on complex agentic tasks (internal projects, scripts)
- [ ] Measure actual cost reduction vs v3.5
- [ ] Gather feedback: What intents did it misclassify? What patterns weren't recognized?

**Deliverables**:
- [ ] Cost comparison report (v3.5 vs v3.6 on real workloads)
- [ ] Feedback summary: Top 5 misclassifications, improvement ideas

**Success Criteria**:
- ≥20% cost reduction validated on real agentic workloads
- ≥80% user satisfaction with intent routing (subjective, but target)

### Sales & Business Deliverables

#### 3. Enterprise Pilot Contracts — Close Negotiations
**Owner**: Yali  
**Approach**:
- [ ] Follow up on inbound meetings from Phase 1
- [ ] Pitch control plane vision (v4.0) as part of pilot offer
- [ ] Negotiate: Free trial period (60 days) + dedicated support + data sharing for case study

**Deliverables**:
- [ ] Pilot contract signed (1 primary, 1 backup)
- [ ] Pilot kick-off meeting (requirements, success metrics, timeline)

**Success Criteria**:
- ≥1 enterprise pilot contract signed (30% cost reduction target, governance requirements)
- Pilot starts Week 9 (Phase 3)

#### 4. Go-to-Market Messaging — Finalization
**Owner**: Yali  
**Deliverables**:
- [ ] Updated marketing website: v3.5 + v3.6 roadmap + enterprise offering
- [ ] Sales deck: "LLM Router Platform" (11 slides: problem, product, proof, pricing, roadmap)
- [ ] Email sequence: 5-email nurture for pilot prospects (education → demo → trial)

**Success Criteria**:
- Sales deck resonates with 2+ customer archetypes (get feedback from advisors)
- Email sequence generates ≥20% open rate, ≥5% CTA click rate

#### 5. Public Roadmap & Transparency
**Owner**: Yali  
**Deliverables**:
- [ ] Public roadmap announcement (v3.5 GA + v3.6 coming, v4.0 preview)
- [ ] Blog post: "Building the Enterprise Control Plane for LLM Routing"
- [ ] GitHub discussion: Gather community feedback on roadmap priorities

**Success Criteria**:
- ≥1,000 views on roadmap announcement
- ≥5 substantive GitHub comments (feature requests, feedback)

---

## Phase 3: Weeks 9–12 (September 2026) — v3.6 Beta + v4.0 Development Kickoff

### Product Deliverables

#### 1. v3.6 Beta Release
**Owner**: Engineering (2 FTE)  
**Approach**:
- [ ] Promote alpha to beta (enable via `LLM_ROUTER_INTENT_ENABLED=true`)
- [ ] Add optional LLM fallback for low-confidence classifications (Ollama or Gemini Flash)
- [ ] Release notes + migration guide for v3.5 → v3.6 users

**Deliverables**:
- [ ] GitHub release: v3.6-beta1
- [ ] Documentation: "Upgrade to v3.6 (intent-aware routing)"

**Success Criteria**:
- ≥100 beta users opt-in
- ≥99% of calls routed successfully (no crashes)

#### 2. v4.0 Control Plane — Development Begins
**Owner**: Engineering (3 FTE starting this week)  
**Scope**: Core control plane (FastAPI + SQLite)  
**Deliverables** (by end of week 12):
- [ ] FastAPI skeleton: Basic CRUD for orgs, teams, users
- [ ] SQLite schema: org, team, user, policy, audit_log, cost tables
- [ ] Authentication: API key validation, user context extraction
- [ ] Policy checking: Basic enforcement (is model allowed?)
- [ ] Audit logging: Log all LLM calls to SQLite
- [ ] Integration: Hook control plane into existing llm_router (non-breaking)

**Deliverables**:
- [ ] Merged feature branch: `feature/control-plane-v1`
- [ ] Test coverage: ≥80% on new code
- [ ] Documentation: API reference, database schema, integration points

**Success Criteria**:
- Control plane handles 100 concurrent calls without errors
- Audit logs capture 100% of LLM calls
- <100ms overhead per call (latency acceptable)

#### 3. Enterprise Pilot — Onboarding & v3.5 Validation
**Owner**: Yali + Engineering (1 FTE pilot support)  
**Approach**:
- [ ] Deploy v3.5 + v3.6-beta at pilot customer site
- [ ] Measure baseline (current LLM costs) vs v3.5 + v3.6 (target: 30%+ reduction)
- [ ] Weekly check-ins: feedback, issues, early sign of control plane needs

**Deliverables**:
- [ ] Pilot deployment completed (prod or staging)
- [ ] Baseline cost measurement (week 1)
- [ ] Weekly status reports (weeks 2–4)

**Success Criteria**:
- Pilot customer sees ≥25% cost reduction in first month
- Zero production issues (or <5 min resolution time)
- Customer confirms governance/audit needs (justifies v4.0 investment)

---

## Phase 4: End of Quarter (Late September) — Retrospective & Planning

### Deliverables

#### 1. Q3 Retrospective (Internal)
**Owner**: Yali  
- [ ] What worked: Workflow routing, enterprise outreach, pilot success?
- [ ] What didn't: Semantic understanding accuracy, sales cycle speed?
- [ ] Learnings: Adjust strategy for Q4 (v3.6 GA, v4.0 continued)

#### 2. Q4 Planning (90-day plan for Oct–Dec)
**Owner**: Yali  
- [ ] Incorporate Q3 learnings
- [ ] Plan: v3.6 GA, v4.0 alpha (control plane), 2nd enterprise pilot

---

## Resources & Allocation

### Engineering
- **2–3 FTE**: v3.5 GA + v3.6 alpha/beta
- **1 FTE**: v4.0 architecture + early development
- **1 FTE**: Pilot support + customer integration
- **Total**: 4–5 FTE

### Business/Product
- **Yali (1 FTE)**: Enterprise sales, marketing, pilot management
- **Advisors (TBD)**: 2–3 hours/month each (strategic input)

### Infrastructure
- Minimal ($500–1K/month for dev/staging)

---

## Key Risks & Mitigation

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| v3.5 beta finds blocking bugs | Medium | High | Allocate 1 FTE for bug triage, establish SLA (24-hr fix) |
| v3.6 intent classifier accuracy <95% | Medium | Medium | Have fallback to heuristic, add LLM classifier in Phase 2 |
| Enterprise sales cycle stalls | Medium | High | Engage advisors for warm intros, offer free pilot |
| v4.0 technical complexity higher than expected | Low | High | Spike early (week 1–4), refine architecture, adjust timeline |
| Agentic framework APIs change | Low | Medium | Monitor AutoGen/LangGraph releases, plan v3.7 after GA |

---

## Success Metrics (End of 90 Days)

### Product
- [ ] v3.5 GA with ≥5 open-source users reporting cost reduction
- [ ] v3.6 alpha complete with ≥95% accuracy on intent classification
- [ ] v4.0 core development started (FastAPI + SQLite working)

### Business
- [ ] ≥1 enterprise pilot customer deployed + seeing ≥25% cost reduction
- [ ] ≥2 advisor agreements signed
- [ ] ≥20 inbound leads from marketing (newsletter, blog, Twitter)

### Platform Vision
- [ ] Enterprise thesis validated (customer pain confirmed v4.0 direction)
- [ ] Control plane architecture approved (no blocking technical risks)
- [ ] Public roadmap updated (transparency builds trust with community)

---

## Weekly Cadence & Checkpoints

### Weekly Standup (Yali + key stakeholders)
- **Monday**: Week goals, blockers from last week
- **Thursday**: Progress update, adjust for Friday/EOW
- **Friday**: Retrospective, plan for next week

### Biweekly Sync (Engineering + Product)
- **Tuesdays**: Technical deep-dive (v3.5 quality, v3.6 classifier accuracy)
- Focus on identifying blockers early

### Pilot Sync (Every 3 days initially, then weekly)
- **Status**: Deployment progress, cost measurement, customer satisfaction
- **Issues**: Escalation path for blocking issues

### Monthly Retrospective (All hands)
- **End of month**: Celebrate wins, reflect on misses, plan next month

---

## Deliverable Checklist (Copy for tracking)

**Phase 1 (Weeks 1–4)**:
- [ ] v3.5 GA complete
- [ ] Enterprise marketing collateral complete
- [ ] Target customer list (10 companies, contacts)
- [ ] Warm intros sent (≥5)
- [ ] v4.0 architecture spike complete

**Phase 2 (Weeks 5–8)**:
- [ ] v3.6 alpha complete (≥95% accuracy)
- [ ] Enterprise pilots: ≥1 contract signed
- [ ] Go-to-market messaging final
- [ ] Public roadmap announced

**Phase 3 (Weeks 9–12)**:
- [ ] v3.6 beta released
- [ ] v4.0 core development: FastAPI + SQLite working
- [ ] Pilot customer: Deployment + cost measurement
- [ ] ≥1 advisor secured

**Phase 4 (Late September)**:
- [ ] Q3 retrospective
- [ ] Q4 90-day plan ready
