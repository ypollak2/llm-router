# Open-Source vs Enterprise vs Managed SaaS Capability Matrix

**Confidential. Internal only. Defines commercial boundary and product tiers.**

---

## Executive Summary

This matrix clarifies what features are free (open-source), what requires self-hosted enterprise license, and what is reserved for managed SaaS. Goal: Maximize open-source adoption while creating clear upgrade paths to paid.

**Philosophy**:
- **Free tier (open-source)**: Cost optimization for individuals + small teams (v3.5–v3.8)
- **Enterprise tier (self-hosted)**: Governance, compliance, and control plane for large teams (v4.0+)
- **Managed SaaS tier**: Same as enterprise + operations, compliance certifications, no ops burden

---

## Capability Matrix

### Core Routing (v3.5–v3.8)

| Feature | Open-Source | Self-Hosted Enterprise | Managed SaaS |
|---------|---|---|---|
| Single-call routing | ✅ | ✅ | ✅ |
| Multi-provider failover | ✅ | ✅ | ✅ |
| Complexity classification | ✅ | ✅ | ✅ |
| Workflow routing (v3.5) | ✅ | ✅ | ✅ |
| Semantic task understanding (v3.6) | ✅ | ✅ | ✅ |
| Agentic framework integration (v3.7) | ✅ | ✅ | ✅ |
| Tool-level routing (v3.8) | ✅ | ✅ | ✅ |
| Cost tracking (per-call) | ✅ | ✅ | ✅ |
| Basic savings report | ✅ | ✅ | ✅ |
| CLI + API | ✅ | ✅ | ✅ |

**Commercial Logic**: Routing is the core value prop. Make it free to drive adoption. Everyone gets it.

---

### Control Plane & Governance (v4.0+)

| Feature | Open-Source | Self-Hosted Enterprise | Managed SaaS |
|---------|---|---|---|
| **Control Plane Basics** |
| Multi-user support (API keys) | ❌ | ✅ | ✅ |
| Organization management | ❌ | ✅ | ✅ |
| Team/project grouping | ❌ | ✅ | ✅ |
| Role-based access control (RBAC) | ❌ | ✅ | ✅ |
| **Policies & Enforcement** |
| Basic routing policies | ❌ | ✅ | ✅ |
| Model allow/deny lists | ❌ | ✅ | ✅ |
| Cost quotas (per team/day) | ❌ | ✅ | ✅ |
| Policy DSL (v4.3) | ❌ | ✅ | ✅ |
| Approval workflows | ❌ | ✅ | ✅ |
| **Audit & Compliance** |
| Audit logs (basic) | ❌ | ✅ | ✅ |
| Cost attribution (org/team) | ❌ | ✅ | ✅ |
| Compliance dashboard | ❌ | ✅ | ✅ |
| HIPAA-compliant logs (v4.2) | ❌ | ✅ | ✅ |
| FCA/SEC audit trails (v4.2) | ❌ | ✅ | ✅ |
| **Analytics** |
| Cost reports | ❌ | ✅ | ✅ |
| Team benchmarking | ❌ | Limited | ✅ |
| Anomaly detection (v4.4) | ❌ | Limited | ✅ |
| BI integrations (v5.0) | ❌ | Limited | ✅ |

**Commercial Logic**: 
- Control plane is where governance-driven customers pay (enterprises need this)
- Self-hosted is cheaper than SaaS but requires ops knowledge
- SaaS removes ops burden, includes compliance certifications (premium)

---

### Enterprise & Compliance (v4.1+)

| Feature | Open-Source | Self-Hosted Enterprise | Managed SaaS |
|---------|---|---|---|
| **Deployment** |
| Docker + docker-compose | ❌ | ✅ | N/A |
| Kubernetes-ready | ❌ | ✅ | N/A |
| On-premises deployment | ❌ | ✅ | N/A |
| VPC/private network support | ❌ | ✅ | N/A |
| Multi-tenant SaaS (managed) | ❌ | ❌ | ✅ |
| **Security & Compliance** |
| SSO/SAML (v4.1) | ❌ | ✅ | ✅ |
| 2FA enforcement | ❌ | ✅ | ✅ |
| Encryption at rest | ❌ | ✅ | ✅ |
| Encryption in transit (TLS) | ✅ | ✅ | ✅ |
| SOC2 Type II cert | ❌ | ❌ | ✅ |
| HIPAA compliance | ❌ | ✅ (setup required) | ✅ (built-in) |
| FCA/SEC audit cert | ❌ | Limited | ✅ |
| GDPR compliance | ❌ | ✅ (setup required) | ✅ (built-in) |
| **Operations** |
| Self-hosted support (best effort) | ❌ | Limited (community) | N/A |
| Dedicated support (SLA) | ❌ | ✅ (paid add-on) | ✅ |
| Uptime SLA (99.9%) | ❌ | ❌ | ✅ |
| Managed backups | ❌ | ❌ | ✅ |
| Disaster recovery | ❌ | Manual | ✅ (automated) |
| Monitoring & alerting | ❌ | Manual | ✅ (included) |

**Commercial Logic**:
- Self-hosted requires enterprise ops skills (appeals to large tech orgs)
- SaaS provides compliance + ops relief (appeals to security-conscious orgs)
- Support + SLAs are where we capture margin

---

### Ecosystem & Extensibility (v4.2+)

| Feature | Open-Source | Self-Hosted Enterprise | Managed SaaS |
|---------|---|---|---|
| **Integrations** |
| Webhook support (v4.5) | ❌ | ✅ | ✅ |
| Slack notifications | ❌ | ✅ | ✅ |
| Jira integration | ❌ | ✅ | ✅ |
| GitHub integration | ❌ | ✅ | ✅ |
| PagerDuty integration | ❌ | ✅ | ✅ |
| Datadog/Grafana integration | ❌ | ✅ | ✅ |
| **Marketplace & Community** |
| Policy marketplace (v5.0) | ❌ | Limited | ✅ |
| Integration marketplace (v5.1) | ❌ | Limited | ✅ |
| Benchmark comparison (v5.1) | ❌ | ❌ | ✅ |
| Community policy library | ❌ | Limited | ✅ |
| Custom integration SDK | ❌ | ✅ | ✅ |
| **Services** |
| Implementation consulting | ❌ | 💰 | ✅ |
| Custom policy development | ❌ | 💰 | ✅ |
| Compliance audit support | ❌ | 💰 | ✅ |
| Managed services | ❌ | ❌ | ✅ |

**Commercial Logic**:
- Marketplace is SaaS-only (community + data advantage)
- Services are high-margin revenue (implementation, compliance)
- Custom integrations available to both, but easier in managed SaaS

---

## Version Release Map (What's in Each Tier at Each Release)

### v3.5 (Current)
```
Open-Source:      Workflow routing ✅
Self-Hosted Ent:  None yet
Managed SaaS:     Not yet launched
```

### v3.6 (Q4 2026)
```
Open-Source:      Workflow routing ✅ + Semantic understanding ✅
Self-Hosted Ent:  None yet
Managed SaaS:     Not yet launched
```

### v3.7–v3.8 (Q1–Q2 2027)
```
Open-Source:      Full routing stack ✅ (workflow + semantic + agentic + tool-level)
Self-Hosted Ent:  None yet
Managed SaaS:     Not yet launched
```

### v4.0 (Q3 2027)
```
Open-Source:      Full routing stack (no change)
Self-Hosted Ent:  Control plane (basic governance, audit, RBAC) ✅
Managed SaaS:     Not yet launched
```

### v4.1 (Q4 2027)
```
Open-Source:      Full routing stack (no change)
Self-Hosted Ent:  Control plane + PostgreSQL + advanced governance ✅
Managed SaaS:     Managed SaaS launched ✅ (control plane + compliance + operations)
```

### v4.2–v4.5 (Q1–Q3 2028)
```
Open-Source:      Full routing stack (no change)
Self-Hosted Ent:  Enterprise features (HIPAA, policy DSL, advanced analytics) ✅
Managed SaaS:      Same + compliance certs + ops + marketplace + services ✅
```

### v5.0+ (Q4 2028+)
```
Open-Source:      Full routing stack (no change)
Self-Hosted Ent:  Advanced features + extensibility (limited marketplace)
Managed SaaS:      Full platform + marketplace + network effects ✅
```

---

## Licensing & Pricing Strategy

### Open-Source (Free)
**License**: MPL 2.0 (permissive, allows commercial use)  
**Target**: Individual developers, small teams, startups
**Monetization**: Organic growth → eventual self-hosted or SaaS conversion

### Self-Hosted Enterprise
**License**: Commercial (separate from open-source)  
**Cost Model**: 
- Base license: $500–2,000/month (per org)
- Per-seat: $50–200/month per user (optional)
- Total: $500–10,000/month depending on org size

**Target**: Tech companies with ops capabilities, compliance teams
**Monetization**: License fee + support (paid SLA)

### Managed SaaS
**Cost Model**: 
- Base: $3,000–5,000/month (includes 5 seats, unlimited routing calls)
- Additional seats: $500/month each
- Per-call overage: $0.00001 per 1M tokens (optional, if heavy usage)
- Services (consulting, policy dev): $5,000–50,000 per engagement

**Target**: Enterprises without ops resources, heavily regulated industries
**Monetization**: SaaS recurring + professional services

### Free Tier (SaaS)
**Offered**: Yes, to drive adoption
**Limits**:
- Up to 3 team members
- Unlimited routing calls
- No control plane (team management, audit logs)
- Basic cost reporting only
- Community support only

**Conversion**: Free users convert to paid when:
- Team grows >3 people (need team management)
- Compliance audit requires audit logs (need enterprise tier)
- Enterprise pilot opportunity (direct sales)

---

## Commercial Boundary Decisions

### Key Decision: Why Control Plane is Paid

**Control plane** (v4.0+) is the commercial boundary because:
1. **Marginal cost of routing is zero** — once built, serving 1M users costs same as 1K
2. **Governance value is clear** — enterprises pay for audit trails + policies + compliance
3. **Open-source focus remains cost optimization** — "everyone can save on LLMs"
4. **Upgrade path is natural** — individual saved money → team needs governance → enterprise platform

### Key Decision: Why SaaS Costs More Than Self-Hosted

**Managed SaaS** costs 2–3x more than self-hosted because:
1. **Compliance certifications** (SOC2, HIPAA) are expensive to maintain
2. **Operational responsibility** (uptime, backups, disaster recovery)
3. **Zero ops burden** for customer (major selling point)
4. **Marketplace + services** ecosystem available only in SaaS
5. **Network effects** (benchmarking) valuable only in managed platform

**Segments willing to pay**:
- Regulated industries (banking, insurance, healthcare) — $10K+/month acceptable
- Mid-market SaaS (100–1K employees) — $5K/month acceptable
- Startups with compliance needs — negotiate down to $2–3K/month

---

## Feature Backlog Classification

Use this classification for every feature decision:

```python
class FeatureTier(Enum):
  OPEN_SOURCE = "all"              # Everyone gets it (routing features)
  ENTERPRISE_ONLY = "self-hosted"  # Only paid enterprise customers
  SAAS_ONLY = "managed"            # Only managed SaaS customers
  ENTERPRISE_PLUS = "both"         # Both self-hosted and SaaS (no open-source)

# Example classifications
feature_tier_map = {
  "workflow_routing": FeatureTier.OPEN_SOURCE,       # v3.5
  "semantic_understanding": FeatureTier.OPEN_SOURCE, # v3.6
  "control_plane": FeatureTier.ENTERPRISE_PLUS,      # v4.0+
  "policy_dsl": FeatureTier.ENTERPRISE_PLUS,         # v4.3
  "marketplace": FeatureTier.SAAS_ONLY,              # v5.0+
  "benchmarking": FeatureTier.SAAS_ONLY,             # v5.0+
  "managed_services": FeatureTier.SAAS_ONLY,         # v5.0+
}
```

---

## Customer Acquisition Flow

```
Free Tier (Open-Source)
      ↓ (team grows OR compliance need)
Free Tier SaaS (up to 3 users)
      ↓ (need governance OR audit)
Paid Self-Hosted (tech-heavy orgs)
      OR
Paid Managed SaaS (operations-light orgs)
      ↓ (expand usage OR buy services)
Enterprise Premier (heavy users, services buyers)
```

---

## Revenue Projection (By Tier)

### Year 1 (After v4.1 SaaS Launch, Q4 2027)
- Open-source: $0 (organic growth)
- Self-hosted: $100K ARR (10 customers × $10K average)
- Managed SaaS: $200K ARR (40 customers × $5K average)
- Services: $50K (3–4 consulting engagements)
- **Total**: ~$350K ARR

### Year 2 (Q4 2028)
- Open-source: $0
- Self-hosted: $300K ARR (30 customers × $10K)
- Managed SaaS: $1.5M ARR (250 customers × $6K average)
- Services: $300K (30–40 engagements)
- **Total**: ~$2.1M ARR

### Year 3+ (Q4 2029)
- Open-source: $0 (but enables SaaS adoption)
- Self-hosted: $500K ARR (50 customers × $10K)
- Managed SaaS: $4–5M ARR (700–800 customers × $6K)
- Services: $800K–1M (80–100 engagements)
- **Total**: ~$5–6.3M ARR

**Margin structure**:
- Open-source: No direct revenue, but drives SaaS adoption (CAC = $0)
- Self-hosted: 50% gross margin (licensing + support)
- SaaS: 70–75% gross margin (platform at scale)
- Services: 60–70% gross margin (consulting labor)
- **Blended**: 60–65% gross margin by Year 3

---

## Competitive Positioning

### vs LiteLLM (Open-Source Proxy)
- **LiteLLM**: "Free routing proxy" (our open-source tier is similar)
- **We win on enterprise**: "We also solve governance (LiteLLM doesn't)"
- **Strategy**: Don't compete on free tier, win on SaaS + enterprise

### vs Langsmith (LangChain Observability)
- **Langsmith**: "Great for tracing, but not optimized for cost"
- **We win on**: "Cost + governance (core value prop Langsmith doesn't have)"
- **Strategy**: Partner with LangChain, not compete

### vs Custom In-House Solutions (Stripe, Google, Meta)
- **In-house**: "Perfect for that org, useless for everyone else"
- **We win on**: "Portable, pre-built governance, compliance, no ops burden"
- **Strategy**: Target organizations without in-house resources

---

## FAQ: Commercial Strategy

**Q: Why not charge for open-source (dual licensing)?**  
A: Open-source free tier is our primary distribution. Charging splits the community, reduces adoption. SaaS + enterprise self-hosted capture premium value without fracturing free tier.

**Q: Why not SaaS-only (no self-hosted)?**  
A: Large tech orgs (Stripe, Databricks, Google) won't use SaaS. Self-hosted tier captures $10M+ TAM. Plus, self-hosted leads to SaaS conversions (team grows, wants compliance).

**Q: Will open-source users pirate enterprise features?**  
A: Enforcement is hard (code is OSS). Strategy: Make SaaS so good (compliance, marketplace, ops relief) that paying is obvious. Enterprise features are hardest to self-host correctly (compliance audits, disaster recovery).

**Q: How do we prevent massive self-hosted deployments from replacing SaaS?**  
A: Price self-hosted competitively, but SaaS premium reflects: (1) no ops, (2) compliance certs, (3) network effects. Regulated industries will always choose SaaS (risk profile). Tech companies can go either way (we're happy).

**Q: What if competitors undercut SaaS pricing?**  
A: Our moat is compliance + control plane DSL + marketplace + network effects. Price is secondary. Plus, customer acquisition cost is lower in SaaS (freemium → SaaS conversion).

---

## Implementation Checklist

- [ ] v3.5–v3.8: Keep open-source only (no paid features)
- [ ] v4.0: Build control plane, create separate commercial license
- [ ] v4.0: Finalize self-hosted pricing ($500–2K/month)
- [ ] v4.1: Launch managed SaaS, finalize SaaS pricing ($3–5K/month + services)
- [ ] v4.1: Launch free SaaS tier (up to 3 users, no control plane)
- [ ] v4.2+: Add SaaS-only features (marketplace, benchmarking, services)
- [ ] v5.0+: Review pricing model, iterate based on customer feedback
