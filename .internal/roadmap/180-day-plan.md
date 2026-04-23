# 180-Day Plan — LLM Router (Q3 2026 – Q1 2027 Roadmap)

**Confidential. Internal only. Roadmap and detailed planning for next 180 days.**

---

## Executive Summary

This 180-day plan extends the 90-day plan into the full Q3–Q1 cycle (July 2026 – December 2026). Goal: **Ship v3.5 GA + v3.6 beta while landing 2–3 enterprise pilots and establishing v4.0 control plane architecture.**

Key deliverables:
1. **v3.5 GA** (July): Workflow routing fully released
2. **v3.6 beta** (October): Semantic task understanding in beta
3. **2+ enterprise pilots** (Sep–Nov): Deployed and validating cost reduction
4. **v4.0 architecture approved** (August): Technical foundation locked in
5. **Series A conversations started** (Oct–Nov): With investors, based on pilot success

**Success = v3.5 + v3.6 stable + 2+ paying pilots + clear path to $5M ARR by Q4 2027**

---

## Month-by-Month Breakdown

### Month 1: July 2026 (Weeks 1–4) — v3.5 GA

**Product Focus**: Complete and release v3.5 workflow routing

#### Week 1–2: Final QA and Documentation
- [ ] Fix remaining v3.5 bugs (target: zero critical bugs)
- [ ] Write user guide for workflow routing (with 5+ examples)
- [ ] Create migration guide for v3.4 → v3.5 users
- [ ] Benchmark cost reduction on real workflows (target: 30%+)
- [ ] Draft release notes (technical + marketing)

#### Week 3: v3.5 Release
- [ ] Release v3.5 GA to PyPI
- [ ] Publish GitHub release + announce on social media
- [ ] Blog post: "Workflow Routing: Cut Multi-Step AI Costs 30%"
- [ ] Update main README with v3.5 highlights
- [ ] Kick off community survey (what to build next?)

#### Week 4: Post-Release Triage
- [ ] Monitor v3.5 adoption (target: 100+ users in first month)
- [ ] Triage user feedback, prioritize high-impact bugs
- [ ] Plan v3.5.1 (patch release with community feedback)
- [ ] Start collecting workflow patterns for case studies

#### Sales/Business Concurrently
- [ ] Finalize target customer list (10 companies)
- [ ] Draft outreach emails for enterprise prospects
- [ ] Schedule intro calls with 5+ potential customers
- [ ] Create one-pager: "LLM Router for Enterprise Teams"

**Success Criteria**:
- ✅ v3.5 GA released with zero critical bugs
- ✅ ≥100 users adopt v3.5 in first month
- ✅ ≥3 enterprise intro calls scheduled

---

### Month 2: August 2026 (Weeks 5–8) — v4.0 Architecture + Enterprise Sales

**Product Focus**: v3.6 development + v4.0 architecture planning

#### Week 5–6: v4.0 Architecture Spike (Engineering Lead)
- [ ] Design FastAPI control plane structure (endpoints, schema, security)
- [ ] Prototype: "Hello World" control plane (org/team/user CRUD, basic auth)
- [ ] Document architecture: API specification, database schema, integration points
- [ ] Risk assessment: Identify technical blockers
- [ ] Architecture review with team (sign-off on design)

**Deliverable**: `docs/CONTROL-PLANE-ARCHITECTURE.md` (50+ pages, production-ready design)

#### Week 5–8: v3.6 Development
- [ ] Finalize intent classifier (heuristic-based, >95% accuracy)
- [ ] Integrate into router.py (update routing decision logic)
- [ ] Build evaluation benchmark (1,000 diverse prompts, validation set)
- [ ] Document intent types + classification rules
- [ ] Create CLI demo showing intent classification

**Deliverable**: v3.6-alpha merged, passing all tests

#### Week 5–8: Enterprise Sales (Yali)
- [ ] Conduct 5–10 customer interviews (AI Platform Teams, startups, regulated)
- [ ] Document pain points: governance, audit, cost visibility
- [ ] Validate control plane value prop ("Doesn't LiteLLM already solve this?")
- [ ] Identify 2 strongest pilot candidates
- [ ] Draft pilot contracts (60-day trial, cost reduction target 30%)

**Deliverable**: 2 pilot contracts with decision-makers

#### Week 8: Monthly Retrospective
- [ ] Review July + August progress (on track?)
- [ ] Adjust roadmap if needed (scope changes, resourcing)
- [ ] Plan September priorities

**Success Criteria**:
- ✅ v4.0 architecture reviewed + approved
- ✅ v3.6-alpha complete, ≥95% accuracy on intent classification
- ✅ ≥2 enterprise pilots signed (contracts in hand)

---

### Month 3: September 2026 (Weeks 9–12) — v3.6 Beta + Pilot Deployment

**Product Focus**: v3.6 beta release, pilot customer onboarding

#### Week 9–10: v3.6 Beta Release
- [ ] Promote v3.6 from alpha to beta
- [ ] Add LLM fallback for low-confidence intent classifications
- [ ] Release CLI option: `--intent-aware-routing=true` (opt-in)
- [ ] Documentation: Intent types, how routing changes with intent
- [ ] Blog post: "Semantic Task Understanding: 20% Extra Cost Savings"

**Deliverable**: v3.6-beta1 release

#### Week 9–10: v4.0 Development Begins
- [ ] Stand up FastAPI server scaffold
- [ ] Implement SQLite schema (org, team, user, policy, audit, cost tables)
- [ ] Build auth layer (API key validation)
- [ ] Implement CRUD for orgs, teams, users
- [ ] Add basic audit logging
- [ ] 500 lines of code, passing tests, non-breaking integration with router

**Deliverable**: Feature branch `feature/control-plane-v1` with core CRUD working

#### Week 11–12: Pilot Customer Onboarding
- [ ] Deploy v3.5 + v3.6-beta at pilot customer #1
- [ ] Baseline measurement: Current LLM spend before optimization
- [ ] Integration: Connect their app to llm-router
- [ ] Training: How to use routing, cost tracking
- [ ] Kick-off meeting: Success metrics, timeline, support plan

**Deliverable**: Pilot #1 deployed, baseline measurement recorded

#### Week 11–12: Parallel Sales for Pilot #2
- [ ] Close pilot #2 contract
- [ ] Schedule deployment for Week 1 of October
- [ ] Negotiate terms: Same as pilot #1 (30% cost reduction, 60 days free)

**Success Criteria**:
- ✅ v3.6 beta released
- ✅ v4.0 development started (auth + CRUD working)
- ✅ Pilot #1 deployed with baseline measurement
- ✅ Pilot #2 contract signed

---

### Month 4: October 2026 (Weeks 13–16) — Pilot Validation + v4.0 Alpha

**Product Focus**: Pilot customer support, control plane core development

#### Week 13–16: Pilot Customer Support (1 FTE dedicated)
- [ ] Weekly check-ins with both pilots
- [ ] Measure cost reduction (target: ≥25% in first 4 weeks)
- [ ] Fix any critical issues (<1 hour SLA)
- [ ] Gather feedback: What features would make this more valuable?
- [ ] Document learnings: Which routing patterns work best?

**Deliverable**: Weekly status reports, cost reduction proof

#### Week 13–16: v4.0 Control Plane — Core Features
- [ ] Policy API: Create/update/delete routing policies
- [ ] Cost quota enforcement: Block calls if team over budget
- [ ] Audit logging: 99%+ of calls logged to SQLite
- [ ] Analytics endpoints: Cost by team, cost by model, cost over time
- [ ] Integration hook: Non-breaking integration with router.py

**Deliverable**: v4.0-alpha with policy + audit working, <100ms latency

#### Week 13–15: Marketing/GTM Preparation
- [ ] Update website roadmap: v3.5 GA, v3.6 beta, v4.0 coming
- [ ] Create case study template (for pilot success stories)
- [ ] Draft pilot success metrics document (shareable with press)
- [ ] Plan launch GTM for v4.0 (timeline: Q4 2026 alpha, Q1 2027 beta)

#### Week 16: Month Retrospective + Planning for November
- [ ] Assess pilot progress (on track for 30% cost reduction?)
- [ ] Revisit 2027 roadmap (adjust v4.0 timeline if pilots change priorities)
- [ ] Plan next month: v3.6 GA vs v4.0 alpha, enterprise hiring?

**Success Criteria**:
- ✅ Both pilots deployed, seeing initial cost reduction (≥15%)
- ✅ v4.0-alpha control plane core features working
- ✅ v3.6 beta adoption growing (100+ users)

---

### Month 5: November 2026 (Weeks 17–20) — v3.6 GA + v4.0 Internal Testing

**Product Focus**: v3.6 GA release, v4.0 hardening for alpha

#### Week 17–18: v3.6 GA Release
- [ ] Promote v3.6 from beta to GA
- [ ] Measure production impact: Cost savings, accuracy, latency
- [ ] Release notes + announcement
- [ ] Blog post: "Intent-Aware Routing Now GA: 20% More Savings"
- [ ] Start planning v3.7 (agentic framework integration)

**Deliverable**: v3.6 GA released

#### Week 17–20: v4.0 Control Plane Hardening
- [ ] Scale testing: Audit logs under 1K calls/sec, <100ms latency
- [ ] Security audit: API key handling, RBAC enforcement, isolation
- [ ] Error handling: Graceful degradation if control plane unavailable
- [ ] Documentation: API reference, quick-start guide, architecture deep-dive
- [ ] Prepare for alpha release to advisors + pilot customers

**Deliverable**: v4.0-alpha ready for closed beta testing

#### Week 17–20: Pilot Validation + Case Study
- [ ] Confirm both pilots have achieved 30%+ cost reduction
- [ ] Conduct post-pilot interviews: What worked? What didn't?
- [ ] Gather testimonials + permission for case study
- [ ] Draft case study: "How [Company] Cut LLM Costs 40%"
- [ ] Plan next phase with pilots: "Would you use control plane (v4.0)?"

**Deliverable**: Pilot success validated, case study 80% complete

#### Week 20: Q4 Planning + Advisor Sync
- [ ] Review Q3 progress vs goals
- [ ] Sync with advisors: market feedback, fundraising readiness
- [ ] Finalize Q4 roadmap: v3.7, v4.0 alpha, enterprise hiring
- [ ] Discuss Series A timing (Jan 2027 vs Mar 2027?)

**Success Criteria**:
- ✅ v3.6 GA released
- ✅ Both pilots confirm 30%+ cost reduction
- ✅ v4.0-alpha ready for beta testing
- ✅ Case study completed (marketable proof)

---

### Month 6: December 2026 (Weeks 21–24) — v4.0 Alpha + Planning 2027

**Product Focus**: v4.0 alpha release, roadmap planning for 2027

#### Week 21–22: v4.0 Control Plane — Alpha Release
- [ ] Release v4.0-alpha to advisors + early customers
- [ ] Closed feedback group (5–10 people) tests control plane
- [ ] Gather feedback: Pain points, missing features, roadmap priorities
- [ ] Document alpha issues, prioritize for beta (v4.0b1, Q1 2027)

**Deliverable**: v4.0-alpha released, feedback collected

#### Week 21–24: Roadmap Planning 2027
- [ ] Planning session: What did we learn in Q3–Q4 2026?
- [ ] Update versioned execution plan: v3.7–v4.0 detailed scopes
- [ ] Assess v4.0 complexity: On track for Q1 2027 beta?
- [ ] Plan hiring: Engineer leads needed for WS1–9
- [ ] Finalize Series A narrative (product + traction + market)

**Deliverable**: Detailed 2027 roadmap, Series A narrative

#### Week 22–24: Marketing & Case Study Launch
- [ ] Publish case study: "How [Pilot Company] Achieved 40% LLM Cost Savings"
- [ ] Press release: "LLM Router Surpasses 1,000 Active Users, Announces Enterprise Platform"
- [ ] Blog post: "2026 in Review: From Single-Call Routing to Multi-Workflow Optimization"
- [ ] Social media campaign: Share metrics, community wins, roadmap transparency

#### Week 23–24: Series A Preparation
- [ ] Create investor deck (11 slides: problem, market, product, traction, team, ask)
- [ ] Identify 20 investors to pitch (Seed/Series A firms focused on infra + AI)
- [ ] Reach out to warm intros (advisors, customers, network)
- [ ] Plan launch meeting schedule (Jan 2027)
- [ ] Refine valuation / raise amount ($5–10M Series A target)

#### Week 24: Year-End Retrospective
- [ ] Q4 review: Hit all goals? Surprises?
- [ ] 180-day plan retrospective: What worked? What didn't?
- [ ] Team retro: Celebrate wins, discuss what to improve in 2027
- [ ] Plan holiday break + Q1 2027 kick-off

**Success Criteria**:
- ✅ v4.0-alpha released, strong feedback
- ✅ 2 pilot case studies published
- ✅ 2027 roadmap finalized + detailed
- ✅ Series A investor conversations started

---

## 180-Day Summary: Milestones & Metrics

### Product Milestones
| Milestone | Target | Status |
|-----------|--------|--------|
| v3.5 GA | July | On track |
| v3.6 beta | October | On track |
| v3.6 GA | November | On track |
| v4.0 architecture approved | August | On track |
| v4.0 core dev started | September | On track |
| v4.0-alpha released | December | On track |

### Business Metrics (Target)
| Metric | Target | Measurement |
|--------|--------|-------------|
| Open-source users | 500+ | GitHub stars, PyPI downloads |
| v3.6 adoption | ≥100 users in beta | Opt-in via env var |
| Enterprise pilots | 2 deployed | Signed contracts + deployments |
| Pilot cost reduction | 30%+ | Monthly measurement |
| Case studies published | 2 | Blog + press release |
| Advisors secured | 2–3 | Advisory agreements signed |
| Series A conversations | ≥10 | Investor meetings scheduled |

### Team & Resourcing
| Role | Phase 1 (Q3) | Phase 2 (Q4) | Comments |
|------|---|---|---|
| Engineering | 4–5 FTE | 5–6 FTE | Hiring lead engineer for WS3 |
| Product/GTM | 1 FTE | 1.5 FTE | Yali + 0.5 FTE marketer |
| Support (pilots) | 0.5 FTE | 1 FTE | Dedicated pilot support |
| Advisors | 2 | 3 | Add 1 GTM advisor in Q4 |

---

## Risk Mitigation (180-Day Specific)

| Risk | Likelihood | Mitigation |
|------|-----------|-----------|
| Pilot customer not seeing 30% cost reduction | Medium | Set realistic expectations, measure carefully, adjust routing if needed |
| v3.6 intent classifier accuracy <95% | Low | Heuristic fallback, LLM classifier as v3.6.1, user feedback loop |
| v4.0 design too complex for execution | Medium | Spike in August (design) gives early warning; can simplify scope |
| Series A investors skeptical of LLM routing opportunity | Low | Pilot case studies + metrics prove market fit; advisors validate market |

---

## Financial Projection (180-Day Cycle)

**Costs**:
- Engineering (5–6 FTE × $250K salary): ~$1.25M
- Infrastructure: ~$50K
- Marketing/Sales: ~$100K
- Other: ~$50K
- **Total burn**: ~$1.5M for 180 days

**Revenue**:
- Open-source: $0
- Pilot contracts: $0 (free trial period)
- **Total revenue**: $0
- **Net burn**: $1.5M

**Funding needed for this cycle**: ~$1.5M (covered by seed round or runway)

**Runway to profitability**: v4.0 control plane (Jan 2027+) is when revenue starts flowing ($50K+ MRR by Q4 2027).

---

## Success Criteria (End of 180 Days)

By end of December 2026, the plan is successful if:

- [ ] v3.5 + v3.6 + v3.6 fully GA and adopted by ≥500 open-source users
- [ ] 2 enterprise pilots deployed, validated ≥30% cost reduction
- [ ] 2 case studies published (marketable proof for Series A)
- [ ] v4.0 architecture approved + development in progress
- [ ] 2–3 advisors secured + Series A conversations started
- [ ] Clear roadmap for 2027 (v3.7, v4.0 beta, enterprise hiring)

**Go/No-Go Decision Point**: If by November any of the first 3 criteria aren't met, pause Series A planning and reassess.

---

## Transition to 12-Month Plan

At the end of 180 days, we transition to the next 6-month cycle (Jan–June 2027):

**Next 180 days (Jan–June 2027) will focus on**:
- v3.7 (agentic framework integration)
- v4.0 beta (control plane hardening)
- Series A fundraising (close $5–10M by March 2027)
- Enterprise hiring (VP Eng, VP Sales)
- 2+ new enterprise pilots

See: `180-plus-plan.md` (TBD) for detailed planning.
