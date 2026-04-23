# Phase 1 Redesign: 12-Week MVP (50% Scope Cut)

**STATUS**: Ready to Execute  
**DURATION**: 12 weeks (3 months)  
**RELEASE TARGET**: End of Week 12 as v1.0-alpha  
**SCOPE**: Core routing MVP only — no Governance, Workflows, or Telemetry  

---

## Executive Summary

Phase 1 delivers the **minimum viable core** to prove that language-agnostic routing can work:
- Workload Understanding: Classify user tasks by complexity
- Routing Policy: Define which model handles which complexity
- Routing Decision Engine: Select the best model for the job (gRPC API)

**What's NOT in Phase 1**: Governance, Workflows, Telemetry, Control Plane, Multi-Tenant SaaS, Managed Hosting.

**Validation**: Real customer usage with 3-5 pilot partners in Month 1 (not Month 11).

---

## Month 1: Architecture & Customer Validation (Weeks 1–4)

### Week 1–2: Finalize Architecture & gRPC Contracts
- **Owner**: Lead Architect
- **Deliverables**:
  - gRPC service definitions (protobuf) for core 3 capabilities
  - Domain model in protobuf (language-neutral)
  - API reference documentation
  - Decision: Build new gRPC core from scratch, or wrap existing Python router?
    - **Recommendation**: Wrap existing Python router in gRPC gateway for Phase 1 MVP. Full rewrite to Go/Rust for Phase 2.
- **Validation Gate**: Architecture review by external architect (not the team that built it)
- **Effort**: 80 hours (2 engineers × 2 weeks)

### Week 2–3: Customer Validation Research (CRITICAL PATH)
- **Owner**: Product Manager + Sales
- **Activities**:
  - Schedule 5 customer interviews (target: teams using multiple LLMs, $10k–$1M annual LLM spend)
  - Key questions:
    - How do you currently choose between LLM providers?
    - What prevents automated routing in your current stack?
    - Would you adopt a language-agnostic routing core? Why/why not?
    - What governance would you require? (approval workflows, cost caps, model restrictions)
    - What's your adoption path: local library → gRPC service → managed SaaS?
  - Document findings in "Phase 1 Validation Report"
- **Success Criteria**:
  - 3+ customers confirm core routing problem exists
  - At least 1 customer willing to pilot Phase 1 MVP
  - No deal-breaker governance requirements identified
- **Effort**: 40 hours (2 people × 2 weeks, part-time)

### Week 3–4: Core Architecture Lock & SDK Planning
- **Owner**: Lead Architect + Platform Team
- **Deliverables**:
  - Finalized gRPC service definitions
  - Protobuf domain model (frozen)
  - SDK specifications for Python, Go, JavaScript (what each SDK will expose)
  - Plan for which SDKs to build in Phase 1 (minimum 1, recommend Python)
  - Deployment guide for gRPC service (Docker, Kubernetes)
- **Validation Gate**: Customer advisory board review (3 pilot partners)
- **Effort**: 120 hours (3 engineers × 2-3 weeks)

**End of Month 1 Gates**:
- ✅ gRPC core is contract-locked (protobuf)
- ✅ At least 1 customer pilot agreement signed
- ✅ No critical governance blockers identified
- ✅ If gates fail: Pivot to Phase 2 scope or extend validation

---

## Month 2: Core & SDK Implementation (Weeks 5–8)

### Week 5–6: gRPC Gateway & Core Service
- **Owner**: Backend Team (2–3 engineers)
- **Approach**: Wrap existing Python router in gRPC gateway (FastAPI + grpc-python)
- **Deliverables**:
  - gRPC service running locally (localhost:50051)
  - Workload Understanding service: classify task complexity
  - Routing Policy service: define model selection rules
  - Routing Decision Engine: select optimal model (includes budget pressure, latency, quality)
  - Service tests: 80%+ coverage
  - Prometheus metrics: latency, decision distribution, error rate
- **Effort**: 200 hours (2–3 engineers × 4 weeks)

### Week 6–7: Python SDK
- **Owner**: SDK Team (1–2 engineers)
- **Deliverables**:
  - Python client library (`llm-router-sdk-python`)
  - Async/sync interfaces
  - Structured types mirroring protobuf
  - Examples: notebook, CLI, FastAPI integration
  - Tests: 80%+ coverage
  - Documentation: quick start, API reference
- **Effort**: 120 hours (2 engineers × 3 weeks)

### Week 7–8: Pilot Integration & Validation
- **Owner**: Platform + Pilot Partner (dedicated liaison)
- **Activities**:
  - Deploy gRPC service to pilot customer environment
  - Integrate with their application (Python SDK)
  - Collect metrics: routing decisions, latency, cost savings
  - Weekly sync calls to debug & iterate
  - Document integration patterns
- **Success Criteria**:
  - Routing decisions are correct (manual spot-checks)
  - Latency < 500ms p99
  - Cost savings achieved (baseline: direct Opus usage → routing saves 30–50%)
  - Zero crashes after 1 week of production traffic
- **Effort**: 160 hours (2 engineers × 4 weeks, full-time support)

**End of Month 2 Gates**:
- ✅ gRPC service in production with 1 pilot customer
- ✅ Python SDK released (v1.0-alpha)
- ✅ Initial cost savings proven
- ✅ Metrics dashboard shows system health
- ✅ If gates fail: 2-week hardening sprint before release

---

## Month 3: Hardening, Release, & Phase 2 Planning (Weeks 9–12)

### Week 9–10: Stability & Performance
- **Owner**: Backend + DevOps (2–3 engineers)
- **Activities**:
  - Load testing: sustain 1000 req/sec with <500ms p99
  - Chaos testing: service failures, network partitions, provider outages
  - Security scan: OWASP Top 10
  - Documentation: deployment, troubleshooting, monitoring
  - Go SDK stub (interface only, no implementation yet)
  - JavaScript SDK stub (interface only)
- **Effort**: 160 hours (2–3 engineers × 3 weeks)

### Week 10–11: Release Preparation & Marketing
- **Owner**: Release Manager + Product Marketing
- **Deliverables**:
  - v1.0-alpha release notes
  - Blog post: "Introducing Language-Agnostic Routing Core"
  - GitHub repo setup & documentation site
  - Community roadmap (what comes in Phase 2)
  - Pricing/licensing decision (open source vs. dual license)
- **Effort**: 80 hours (2 people × 3 weeks)

### Week 11–12: v1.0-alpha Release & Phase 2 Planning
- **Owner**: Entire team
- **Activities**:
  - Tag v1.0-alpha, publish to GitHub/Docker Hub
  - Release communication (blog, Twitter, dev communities)
  - Community Q&A / office hours
  - Retrospective on Phase 1
  - Draft Phase 2 plan (Governance, Workflows, Telemetry in Months 4–8)
  - Evaluate: rewrite core to Go/Rust, or expand Python gateway?
- **Effort**: 120 hours (distributed across team)

**End of Month 3 Gates**:
- ✅ v1.0-alpha released (GitHub, Docker Hub, PyPI)
- ✅ 3–5 pilot customers in production
- ✅ Documentation complete
- ✅ Phase 2 plan signed off by executive sponsor
- ✅ Community feedback collected

---

## Effort & Team Summary

**Total Phase 1 Effort**: ~1,080 engineer-hours = 270 engineer-weeks = 1.3 FTE for 12 weeks (or 3 FTE for 4 weeks)

### Recommended Team (Minimum)
| Role | Count | Weeks | Notes |
|------|-------|-------|-------|
| Lead Architect | 1 | 12 | gRPC design, decisions, validation |
| Backend Engineer (gRPC) | 2 | 12 | Core service, integrations |
| SDK Engineer (Python) | 1 | 8 | Python SDK, examples |
| DevOps / Platform | 1 | 12 | Deployment, monitoring, testing |
| Product Manager | 1 | 4 (part-time) | Customer validation, roadmap |
| QA / Reliability | 1 | 8 | Load testing, stability |
| **Total** | **6–7** | **12 weeks** | ~270 eng-weeks @ 3.5 FTE |

---

## Critical Dependencies & Sequencing

```
Week 1–2: Architecture Lock
    ↓
Week 2–3: Customer Validation (PARALLEL)
    ↓
Week 3–4: gRPC Contract Finalize
    ↓
Week 5–6: Core Service Implementation
    ↓
Week 6–7: Python SDK (PARALLEL to Core)
    ↓
Week 7–8: Pilot Integration & Validation
    ↓
Week 9–10: Hardening & Load Testing
    ↓
Week 11–12: Release & Phase 2 Planning
```

**Critical Path**: Architecture → Validation → Core → Pilot Integration → Release

**Slack (non-critical)**: SDK development, documentation (can slip 1 week without delaying release)

---

## What's Explicitly NOT in Phase 1

| Capability | Reason | When |
|------------|--------|------|
| **Governance** | Not needed for MVP — single team pilots | Phase 2 (Month 4–5) |
| **Approval Workflows** | Over-engineered for MVP — rules engine sufficient | Phase 2 |
| **Multi-Tenant SaaS** | Premature — validate single-tenant model first | Phase 3 (Post-GA) |
| **Telemetry Dashboard** | Nice-to-have — Prometheus + logs sufficient | Phase 2 |
| **Workflow Orchestration** | Scope creep — MVP is stateless routing | Phase 2 (Month 6–8) |
| **Managed Hosting** | No revenue model validated — risky | Phase 3 (Month 10+) |
| **Control Plane / UI** | No governance need in Phase 1 | Phase 2 |
| **Go Core Rewrite** | High risk, high cost — Phase 1 uses Python + gRPC | Phase 2 (Month 7–8) |
| **Advanced Caching** | Complexity — basic in-memory cache sufficient | Phase 2 |
| **Quality Escalation** | Binary routing sufficient for MVP | Phase 2 (Month 5) |

---

## Success Metrics (End of Phase 1)

| Metric | Target | Notes |
|--------|--------|-------|
| **Latency (p99)** | < 500ms | Routing decision including gRPC call |
| **Availability** | > 99.5% | Uptime in pilot production environments |
| **Cost Savings** | 30–50% | vs. direct Opus baseline |
| **Pilot Customers** | 3–5 | Real production workloads |
| **Python SDK Coverage** | 80%+ | Unit test coverage |
| **Documentation** | Complete | Quick start, API reference, deployment guide |
| **Community Adoption** | 50+ GitHub stars | By end of week 12 |
| **Bug Reports** | < 2 critical | During first 2 weeks post-release |

---

## Phase 2 (Months 4–8) — Sketch Only

Phase 2 adds:
- **Governance** (month 4–5): Approval workflows, cost caps, model restrictions
- **Quality & Escalation** (month 5): Quality evaluation, fallback routing
- **Workflow Orchestration** (month 6–8): Multi-step LLM chains, conditional logic
- **Go Core Rewrite** (month 7–8): Language-agnostic core in Go with gRPC service
- **Telemetry & Analytics** (month 8): Dashboard, cost reporting, usage analytics
- **Additional SDKs** (month 8): JavaScript, Go (after core rewrite)

Estimated effort: 2,000+ eng-hours (larger team, higher complexity)

---

## Decision Checklist

Before starting Week 1, confirm:

- [ ] Executive sponsor signed off on 12-week timeline and $XXX budget
- [ ] Customer advisory board (3–5 companies) identified and committed to validation interviews
- [ ] Architecture review by external principal architect scheduled (Week 2)
- [ ] Python + gRPC (not Go) confirmed as Phase 1 tech stack
- [ ] Release target: Open source (GitHub) vs. proprietary vs. dual-license decided
- [ ] DevOps: Docker + Kubernetes vs. serverless decided
- [ ] Pilot customer integration lead assigned
- [ ] Phase 2 scope document (governance, workflows, telemetry) drafted

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|-----------|
| Customer validation reveals governance must be in Phase 1 | Timeline slip | Require customers to sign off on Phase 2 governance in Week 3 |
| gRPC performance too slow (>500ms latency) | Pilot fails | Weekly load testing from Week 5 onwards; pivot to REST if needed |
| Python SDK adoption low (community wants Go/JS first) | Phase 2 rework | Build all 3 SDKs in parallel in Phase 1B (extend to 14 weeks) |
| Pilot customer churn (integration too hard) | Loss of validation | Embed full-time SDK engineer at customer site (week 7–8) |
| Core service crash in production | Reputation damage | Chaos testing mandatory before pilot release; on-call support 24/7 |
| Team burnout (1.3 FTE is lean) | Quality drops | Regular retros; strict scope discipline; say no to creep |

---

## Conclusion

Phase 1 is **executable, achievable, and de-risked**:
- Customer validation happens FIRST (Month 1), not after 9 months of building
- Scope is cut by 50% (only 3 core capabilities)
- Timeline is realistic (12 weeks, not 60)
- Success metrics are measurable and binary
- Phase 2 can pivot based on what Phase 1 learns

**Go/no-go decision point**: End of Week 3 (after customer validation). If no customers believe in routing, stop and pivot.
