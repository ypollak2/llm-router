# Confidential Strategy Index — LLM Router Platform

**Internal only. Guides access to confidential strategic documents.**

---

## Document Map

This index organizes the confidential strategy across key areas. All documents are designed to work together — read in the order below for full context.

### Tier 1: Foundational Strategy (Read First)

**[STRATEGIC-REVIEW.md](../docs/STRATEGIC-REVIEW-2026.md)**
- What: Deep analysis of market opportunity, gaps, and platform vision
- When to read: First — establishes why platform matters
- Length: ~15,000 words
- Key sections: Gap analysis, target architecture, multi-agent routing, token optimization opportunities

**[enterprise-thesis.md](./strategy/enterprise-thesis.md)**
- What: Detailed buyer personas, TAM, market timing, competitive landscape
- When to read: Second — validates market assumptions
- Length: ~6,000 words
- Key sections: Persona profiles (Alex/Stripe, Jordan/startup, Sarah/insurance), $25–50M TAM, why now, moat

**[capability-matrix.md](./strategy/capability-matrix.md)**
- What: Commercial boundary (free vs enterprise vs SaaS), pricing, revenue model
- When to read: Third — defines how to monetize
- Length: ~5,000 words
- Key sections: Feature matrix by tier, licensing, pricing, revenue projections, customer acquisition flow

---

### Tier 2: Execution & Roadmap (Planning)

**[versioned-execution-plan.md](./roadmap/versioned-execution-plan.md)**
- What: Detailed v3.5–v5.x roadmap with product/engineering scope, dependencies, risks
- When to read: Fourth — understand the full 18–24 month journey
- Length: ~12,000 words
- Key sections: v3.5 workflow routing, v4.0 control plane, v4.2+ enterprise features, sequencing rationale

**[90-day-plan.md](./roadmap/90-day-plan.md)**
- What: Concrete deliverables for next 90 days (Q3 2026)
- When to read: When starting active work
- Length: ~4,000 words
- Key sections: Phase 1 (GA v3.5), Phase 2 (enterprise sales), Phase 3 (v3.6 + v4.0 kickoff), success metrics

**[workstream-map.md](./roadmap/workstream-map.md)**
- What: Team organization into 9 workstreams, dependencies, staffing model
- When to read: When planning hiring or team structure
- Length: ~6,000 words
- Key sections: WS1–9 (routing, semantic, control plane, compliance, analytics, integrations, marketplace, sales, commercial), dependencies, team structure by phase

---

### Tier 3: Detailed Strategy (Deep Dives)

**[product-strategy.md](./strategy/product-strategy.md)**
- What: Detailed product strategy including market opportunity, customer personas, competitive positioning, open-core boundary, sequencing, operating model
- When to read: For product decision-making
- Length: ~8,000 words
- Key sections: Market opportunity, personas, positioning, open-core decision, sequencing rationale, decision points

---

### Tier 4: Supporting Documents (Future)

**[operating-model.md](./strategy/operating-model.md)** *(to be created)*
- What: How the business operates (org structure, decision-making, metrics, culture)
- When to read: For operational planning

**[enterprise-implementation-guide.md](./strategy/enterprise-implementation-guide.md)** *(to be created)*
- What: How to implement enterprise features (HIPAA, SSO, policies) step-by-step
- When to read: When shipping v4.0+

---

## Reading Paths (Based on Role)

### For Founders/Decision-Makers
1. Strategic review (STRATEGIC-REVIEW-2026.md) — understand the opportunity
2. Enterprise thesis — validate market assumptions
3. Capability matrix — decide on business model
4. Versioned execution plan — commit to roadmap
5. 90-day plan — begin execution

**Time**: 4–5 hours. Result: Clear on strategy and next steps.

### For VP Engineering / Engineering Lead
1. Versioned execution plan (v3.5–v3.8 focus) — understand technical requirements
2. Workstream map — understand team structure
3. 90-day plan — understand immediate deliverables
4. Strategic review (technical sections) — understand platform vision

**Time**: 3–4 hours. Result: Clear on what to build and why.

### For Product/GTM Lead
1. Enterprise thesis — understand customers
2. Capability matrix — understand pricing
3. Product strategy — understand positioning
4. 90-day plan (sales section) — understand customer acquisition
5. Versioned execution plan — understand feature roadmap

**Time**: 2–3 hours. Result: Clear on GTM strategy and customer messaging.

### For VP Finance/CFO
1. Capability matrix (revenue section) — understand pricing model
2. Versioned execution plan (dependencies/risks) — understand investment required
3. 90-day plan (resourcing) — understand immediate hiring/costs
4. Workstream map (team structure) — understand long-term org plan

**Time**: 1–2 hours. Result: Clear on revenue potential and investment.

---

## Key Concepts (Glossary)

### Control Plane
The governance + audit infrastructure that manages routing policies, user permissions, cost quotas, and audit logs. Available in v4.0+ (enterprise tier).

### Workflow Routing
Multi-call optimization where llm-router understands that calls are part of a larger workflow (plan → implement → review) and routes them as a unit, saving 30–40% vs single-call routing.

### Semantic Task Understanding
Intent classification (writing, coding, analysis, planning, review) to match models to task type. Saves 20% additional cost vs complexity-only routing.

### Open-Core Boundary
The line between free (open-source) and paid (enterprise/SaaS) features. Routing is free, governance (control plane) is paid.

### TAM
Total Addressable Market. We estimate $25–50M ARR across AI Platform Teams ($15M), startups ($7.7M), and regulated industries ($28.8M).

### SaaS vs Self-Hosted
- **Managed SaaS**: llm-router hosts and operates the platform (~$3–5K/month)
- **Self-Hosted Enterprise**: Customer deploys and operates their own control plane (~$500–2K/month)

### Moat
Competitive advantage. Ours: first to market on integrated cost + governance, workflow routing at scale, compliance + audit.

---

## Decision Log

This section tracks major strategic decisions made during planning.

### Decision 1: Sequencing (v3.5–v3.8 Before v4.0)
- **Decision**: Develop workflow routing and semantic understanding (v3.5–v3.8) before building control plane (v4.0)
- **Rationale**: Proves platform can understand complex multi-step patterns; de-risks control plane investment; generates user feedback
- **Alternative**: Build control plane first (rejected — too risky without workflow proof)
- **Impact**: Adds 3–4 months to v4.0, but increases success probability

### Decision 2: Open-Core Strategy
- **Decision**: Keep routing free (open-source), monetize via governance (control plane + SaaS)
- **Rationale**: Routing has near-zero marginal cost once built; governance is natural upsell for teams; maximizes adoption
- **Alternative**: Dual-licensing routing (rejected — splits community, reduces adoption)
- **Impact**: Trade: lower immediate revenue, higher long-term TAM

### Decision 3: SaaS Launch Timing (v4.1)
- **Decision**: Launch managed SaaS in v4.1 (Q4 2027), not earlier
- **Rationale**: SaaS requires PostgreSQL (v4.1), multi-tenant infrastructure, compliance certs; can't launch in v4.0 with SQLite
- **Alternative**: Launch SaaS in v5.0 (rejected — delays revenue, misses market window)
- **Impact**: SaaS available 12 months after control plane first availability

### Decision 4: Target Customer (Startups First, Enterprise Later)
- **Decision**: Land 2–3 AI-native startups first (faster sales cycle), use as references for enterprise
- **Rationale**: Enterprise sales cycle 12+ months; startups 6–8 weeks; need references early
- **Alternative**: Enterprise-first (rejected — too slow, high failure risk)
- **Impact**: First revenue from startups (smaller deals) by Q4 2027, enterprise revenue follows in 2028

### Decision 5: Control Plane Tier (Self-Hosted + SaaS, No Hybrid)
- **Decision**: Offer control plane as either self-hosted or managed SaaS, not both
- **Rationale**: Customer must choose one; hybrid creates support complexity
- **Alternative**: Hybrid (rejected — unclear which to recommend to customers)
- **Impact**: Clearer customer journey, simplified sales

---

## Success Criteria (Platform-Level)

By end of v5.0 (Q4 2028), the platform is successful if:

- [ ] ≥100 enterprise customers (control plane usage)
- [ ] ≥$5M ARR (blended: SaaS + self-hosted + services)
- [ ] ≥50K open-source downloads/month (organic growth)
- [ ] SOC2 + HIPAA compliance certifications
- [ ] ≥500 marketplace policies + 100+ integrations
- [ ] ≥60% gross margin (SaaS tier)
- [ ] Product-market fit validated with 2–3 lighthouse customers
- [ ] Clear moat (control plane + governance hard to replicate)

---

## Risk Register (High-Level)

| Risk | Likelihood | Impact | Mitigation | Owner |
|------|-----------|--------|-----------|-------|
| v3.5–3.8 adoption slower than expected | Medium | Medium | PLG + community outreach | WS8 |
| Enterprise pilots reject control plane | Medium | High | Validate early (v4.0 alpha), get feedback | WS3 |
| Competitors (AWS, LangChain) build competing platform | Medium | High | Move fast on control plane + marketplace moat | WS3, WS7 |
| SaaS ops costs higher than projected | Low | Medium | Monitor infrastructure closely, optimize | WS9 |
| Policy DSL complexity too high for users | Medium | Medium | UI policy builder, templates, community lib | WS4 |

---

## Document Maintenance

This index is updated quarterly. Keep in sync:

1. **After major decisions**: Update Decision Log
2. **After quarterly reviews**: Update Success Criteria progress
3. **When roadmap changes**: Update versioned-execution-plan.md and 90-day-plan.md
4. **When GTM strategy shifts**: Update enterprise-thesis.md and capability-matrix.md

---

## Access Control & Confidentiality

**Distribution**:
- Yali (founder): All documents
- Advisors: Strategic review + enterprise thesis + 90-day plan only
- Early employees: Versioned execution plan + workstream map only
- Board/Investors: All documents (after funding)

**Confidentiality tier**:
- 🔒 **HIGHEST**: Product strategy, operating model, pricing (not shared)
- 🔒 **HIGH**: Capability matrix, versioned execution plan (shared with advisors only)
- 🔒 **MEDIUM**: Enterprise thesis, 90-day plan (shared with early employees)
- 📖 **PUBLIC**: Strategic review (share as thought leadership after editing)

---

## Next Steps

1. **Week 1**: Yali reviews all documents, refines strategy, locks roadmap
2. **Week 2**: Share enterprise thesis + capability matrix with advisors, validate assumptions
3. **Week 3**: Share 90-day plan with leadership team, begin hiring/resourcing
4. **Week 4**: Execute 90-day plan (v3.5 GA, enterprise outreach, v4.0 spike)

---

## Document Versions

| Document | Version | Date | Status |
|----------|---------|------|--------|
| STRATEGIC-REVIEW-2026.md | 1.0 | 2026-04-23 | Complete |
| enterprise-thesis.md | 1.0 | 2026-04-23 | Complete |
| capability-matrix.md | 1.0 | 2026-04-23 | Complete |
| product-strategy.md | 1.0 | 2026-04-23 | Complete |
| versioned-execution-plan.md | 1.0 | 2026-04-23 | Complete |
| 90-day-plan.md | 1.0 | 2026-04-23 | Complete |
| workstream-map.md | 1.0 | 2026-04-23 | Complete |
| operating-model.md | — | — | Planned |
| enterprise-implementation-guide.md | — | — | Planned |

---

## Contact & Questions

For questions about strategy, contact Yali directly. For implementation questions, escalate to relevant workstream lead (see workstream-map.md).

Last updated: 2026-04-23
