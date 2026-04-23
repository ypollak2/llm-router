# LLM Router — Confidential Product Strategy

**CONFIDENTIAL - INTERNAL LEADERSHIP ONLY**

---

## PART I: MARKET OPPORTUNITY & THESIS

### The Fundamental Problem (Not Being Solved)

**Current state (2026)**: LLM routing is solved for individuals.
- llm-router: 87% cost reduction in real usage
- Free-first chains work
- Complexity-based routing works
- Developers love it

**What's NOT solved**: LLM routing for teams and enterprises.
- No central policy control
- No approval workflows
- No audit trails (compliance blocker)
- No skill/service-level routing policies
- No workflow-level optimization
- No provisioning / deployment model for enterprises
- No governance for regulated industries

**Why this matters NOW**:
1. **Team adoption is blocked** — Companies can't deploy a tool where routing decisions are unmeasured, unapproved, unaudited
2. **Token optimization is incomplete** — Enterprise customers spending $10K–100K+/month on LLM APIs; even 20% saving = $2–20K/month
3. **Workflow optimization is missing** — Multi-step agent tasks (research → synthesis → review) don't route per-step → massive waste at scale
4. **Enterprise AI is accelerating** — Enterprises building AI teams NOW; whoever owns routing governance WINS that category

### The Opportunity Size

**TAM (Total Addressable Market)**:

1. **Enterprise AI teams** (direct)
   - ~2,000 mid-market + enterprise companies with 5+ AI engineers
   - Average spend on LLM APIs: $5K–50K/month
   - Potential 20–30% savings = $1K–15K/month per customer
   - Market size: 2,000 × $10K = **$20M+/year sustainable**

2. **AI Platform teams** (embedded)
   - Companies building internal AI platforms (10–100 employees)
   - Need routing as infrastructure layer
   - Deploy as part of AI platform stack
   - Market size: 500+ companies × $5K = **$2.5M+**

3. **Managed / SaaS** (future)
   - Cloud-hosted control plane for enterprises
   - 10–20% of enterprises prefer managed
   - Higher margins, stickier contracts
   - Market size: **$5–10M/year SaaS ARR potential**

**TAM total**: **$25–50M/year** enterprise market alone (10x larger than individual developer market).

### Why Now

1. **LLM API costs have stabilized** — Enough to justify routing optimization
2. **Multi-step agent workflows are mainstream** — Companies building complex AI systems
3. **Enterprises have LLM budgets** — No longer experimental; dedicated cloud spend
4. **Competitors are weak** — LiteLLM = proxy, OpenRouter = easy to use but not enterprise-ready, Portkey = complex and expensive
5. **llm-router has proof** — Public success case (87% savings) creates credibility
6. **Governance is becoming non-negotiable** — SOC 2, ISO 27001, audit requirements

---

## PART II: WHAT MAKES THIS A PLATFORM, NOT JUST A TOOL

### Why Open-Source Alone Isn't Enough

**Open-source reaches**:
- ✅ Individual developers
- ✅ Small teams (<10 people)
- ❌ Teams requiring central governance
- ❌ Regulated enterprises (healthcare, finance, government)
- ❌ Companies with procurement processes
- ❌ Organizations needing support SLAs

**Enterprise buyers need**:
1. **Central control plane** — One person manages routing for 50+ developers
2. **Audit trail** — Every decision logged, immutable, exportable
3. **Approval workflows** — CFO can gate expensive model use
4. **Support / SLA** — Call a number when broken, not "file a GitHub issue"
5. **Compliance** — SOC 2, HIPAA, FedRAMP ready
6. **Vendor credibility** — Can't bet company on solo maintainer

**Open-source can provide** points 1–2 (control plane, audit).  
**Can't compete on** points 3–6 (org processes, support, compliance, vendor trust).

### The Wedge: Open-Source → Enterprise

**Strategy**:
1. Keep routing engine open (value, community, trust)
2. Open-source basic control plane (demonstrates capability)
3. Proprietary: enterprise packaging, compliance, support
4. Managed SaaS: hosted control plane for those who prefer it
5. Future: vertically integrated product for specific industries

**This creates**:
- Adoption loop (developers use open, company adopts, enterprise IT evaluates)
- Community contribution to core
- Enterprise revenue with open-source trust
- Multiple revenue streams (self-hosted, managed, services)

---

## PART III: ENTERPRISE THESIS (DETAILED)

### Buyer Profile

**Title**: VP Engineering, AI Platform Lead, or Infrastructure Lead

**Pain points**:
1. **Cost control** — LLM bill growing monthly, no visibility into what's being optimized
2. **Quality control** — Teams using different models inconsistently; performance variance
3. **Compliance** — Can't audit who used what model, when, why; violates SOC 2
4. **Governance** — No way to enforce "this sensitive task only uses internal models"
5. **Efficiency** — Multi-step workflows don't route intelligently; waste on expensive models
6. **Liability** — If model makes an error, can't prove they used best available option

**Willingness to pay**:
- **Annual savings > cost of solution** — If saving $50K/year, will pay $5–15K/year
- **Compliance value** — If unlocks a deal or solves audit gap, budgets are flexible
- **Time value** — Central control saves 5–10 hours/week per platform engineer

### User Profile (Day-1 Users After Enterprise Adoption)

**Primary**: Platform / Infrastructure engineers (deploy + operate control plane)  
**Secondary**: AI product leads (define policies, manage budgets)  
**Tertiary**: Finance / CFO (review spend, enforce budgets)  

### Likely First Customers

**Type 1: AI Platform Teams**
- Building internal AI platform for 50–500 engineers
- Need routing as infrastructure layer
- Willing to deploy on-prem or self-hosted
- **Willingness to pay**: $10–50K/year
- **Timeline**: 2–4 weeks to decision
- **Example**: Large tech company's central AI platform

**Type 2: AI-Native Startups**
- Building product on LLM APIs
- Need cost control for profitability
- Prefer managed solution
- **Willingness to pay**: $5–20K/year + per-call fee
- **Timeline**: 1–2 weeks to decision
- **Example**: AI SaaS company with 20+ engineers

**Type 3: Regulated Industry Users**
- Healthcare, finance, legal tech
- Need audit + compliance
- Willing to pay premium for on-prem
- **Willingness to pay**: $50K+/year
- **Timeline**: 3–6 months (procurement)
- **Example**: Healthcare company's AI analytics team

### Pricing Hypothesis

**Self-Hosted** (on-prem, open-source control plane + support):
- $5K–15K/year base
- +$500–2K per additional service/team

**Managed SaaS** (cloud-hosted control plane):
- $10K–30K/year base
- +$0.001–0.005 per routed LLM call
- Premium for compliance (SOC 2, etc.)

**Services** (implementation, custom policies, training):
- $5K–50K per engagement

**Expected enterprise ACV**: $15K–50K/year (self-hosted), $20K–100K/year (managed + services).

---

## PART IV: STRATEGIC DIFFERENTIATION

### What Makes llm-router Defensible

**Moat 1: Community trust + public success**
- 87% savings proven in public
- Open-source DNA
- No vendor lock-in perception
- Worth: **3–6 months head start vs competitors**

**Moat 2: Workflow-level routing** (if built first)
- No other router does per-step optimization
- 30–40% additional savings on agent tasks
- Hard to replicate without understanding multi-agent patterns
- Worth: **6–12 months advantage**

**Moat 3: Enterprise control plane** (if built well)
- Network effect (policies shared, benchmarked across customers)
- Switching cost (audit history, policy definitions)
- Data moat (aggregate routing quality benchmarks)
- Worth: **12–18 months if control plane has strong UX**

**Moat 4: Extensibility / Plugin ecosystem**
- Community builds custom classifiers, cost models, integrations
- Creates ecosystem lock-in
- Harder to migrate
- Worth: **6–12 months if plugins become standard**

**NOT a moat** (don't rely on):
- Token cost optimization alone (easy to copy)
- Free-first chains (easy to copy)
- Budget management (easy to copy)
- Any feature that's purely open-source (anyone can fork)

### Competitive Positioning

| Competitor | Strength | Weakness | vs llm-router |
|---|---|---|---|
| LiteLLM Proxy | Proxy, simple | No governance, no routing smarts | We have routing + governance |
| OpenRouter | Cheap, simple UI | Limited governance, middleware | We have control + flexibility |
| Portkey | Enterprise features | Complex, expensive, not open | We're open + simpler |
| RouteLLM | Research backing | Academic, not production | We're production + profitable |
| Anthropic (future) | Claude native | Single provider | We multi-provider + open |

**Differentiation summary**: Open-source core + enterprise governance + workflow optimization = unique position. This is hard to replicate.

---

## PART V: WHAT BECOMES OPEN VS ENTERPRISE VS MANAGED

### Layer Model

```
Layer 4: Stealth Differentiation (Keep Private)
├─ Advanced analytics + recommendations
├─ ML-driven policy suggestions
└─ Predictive cost modeling

Layer 3: Enterprise/Managed-Only
├─ Control plane (on-prem or SaaS)
├─ RBAC + SSO
├─ Compliance/audit features
├─ Multi-tenant isolation
└─ Cost attribution + chargeback

Layer 2: Open-Source (with Enterprise UX)
├─ Routing engine (core)
├─ Workflow orchestration
├─ Policy model
├─ Token optimization
├─ Self-hosted basic control plane (optional)
└─ Plugin system

Layer 1: Community Extensions (Pure Open-Source)
├─ Custom classifiers
├─ Task-type definitions
├─ Routing policies (shared library)
└─ Integrations
```

### Specific Capability Classification

| Capability | Classification | Reasoning |
|---|---|---|
| **Complexity classifier** | Open | Core value, easy to replicate |
| **Workflow routing** | Open | Strategic advantage if first, but hard to keep proprietary |
| **Workload intent detection** | Open + Enterprise | Open core, enterprise proprietary models |
| **Policy model (org/team/skill scopes)** | Open | Infrastructure, not differentiator |
| **Policy approval workflows** | Enterprise | Enterprise-only feature |
| **Central control plane** | Hybrid | Basic version open, advanced managed-only |
| **RBAC + SSO** | Enterprise | Enterprise requirement |
| **Audit log + compliance** | Enterprise | Regulatory requirement |
| **Cost attribution + chargeback** | Enterprise | Business requirement |
| **Semantic dedup cache** | Open | Token optimization, hard to charge for |
| **Context pruning** | Open | Token optimization, community will want it |
| **Speculative routing** | Open | Advanced but not defensible |
| **Quality monitoring + escalation** | Hybrid | Open basic, enterprise advanced |
| **Analytics dashboard** | Enterprise | "Why" visibility is enterprise feature |
| **Policy recommendations** | Enterprise | Requires aggregate data across orgs |
| **Custom pricing models** | Enterprise | Per-customer business logic |
| **Self-hosted deployment on K8s** | Hybrid | Open basic, enterprise hardened |
| **Plugin system** | Open | Community extension point |
| **SaaS control plane** | Managed-Only | Cloud offering, managed service |
| **Compliance certifications** | Enterprise | SOC 2, HIPAA, FedRAMP |

**Principle**: Open what's easy to copy, hard to charge for, or strategically valuable for adoption. Keep proprietary what requires coordination (control plane), enterprise processes (approval workflows), or aggregate data (analytics).

---

## PART VI: WHAT WE SHOULD BUILD FIRST (SEQUENCING RATIONALE)

### Sequencing Thesis

**Thesis**: Build workflow routing + control plane MVP in parallel (not sequentially).

**Why**:
1. **Workflow routing** unlocks 30–40% additional token savings → proof point for enterprise value
2. **Control plane** unlocks enterprise conversations → but needs workflow routing to justify complexity
3. **Building sequentially** delays both; builds in parallel accelerates both

**Alternative (worse)**: Build control plane first, then workflows.
- **Risk**: Enterprise buys control plane but gets little value without workflow routing
- **Reality**: First pilot will demand both; building one first wastes time

### What NOT to Build Yet

❌ **Multi-tenant SaaS** — Too early. Prove self-hosted first. Multitenant adds complexity without proving unit economics.

❌ **Advanced analytics + ML recommendations** — Too early. First get enterprise customers using control plane, then learn what they want.

❌ **Custom pricing models per customer** — Too early. Use simple $10K/year pricing, iterate later.

❌ **Vertically integrated industry solutions** (healthcare, finance) — Too early. Prove horizontal platform first, verticalize later.

❌ **Plugin ecosystem** — Too early. First get 5–10 customers on control plane, then open extensibility.

❌ **Bedrock / Azure OpenAI / Vertex native support** — Too early. Focus on LiteLLM integration; let community add providers.

### What SHOULD Build First

✅ **Workflow-level routing** (4–6 weeks)
- Unlocks 30–40% additional savings
- Proof point for platform value
- Required for first enterprise pilots

✅ **Workload intent detection** (3–4 weeks)
- Semantic understanding of task
- Better routing granularity
- Early validation of ML/modeling capability

✅ **Control plane MVP** (8–10 weeks, parallel to above)
- Policy CRUD + versioning
- Approval workflows
- Audit log + immutable events
- SSO/RBAC (OIDC)
- Self-hosted + local
- NOT SaaS yet; not multi-tenant yet

✅ **Enterprise integration** (2–3 weeks)
- Local router ↔ control plane sync
- Policy pull + decision push
- Deployment guide for self-hosted

✅ **Token optimization engine** (4–5 weeks, after CP MVP)
- Semantic dedup (most impactful)
- Context pruning
- Escalation gates

✅ **Enterprise deployment guide** (2–3 weeks)
- Docker Compose, K8s manifests
- Hardening recommendations
- Monitoring setup

---

## PART VII: OPERATING MODEL (HOW WE WORK)

### Public vs Private Work

**Public commits** (to main branch):
- Open-source features only
- No business logic, no control plane, no enterprise-specific code
- Routing engine improvements
- Hook improvements
- MCP tool improvements

**Private development** (in `.internal/` branch or local):
- Control plane code initially
- Enterprise authentication / RBAC code
- Compliance/audit features
- Analytics and dashboards
- Advanced token optimization

**Hybrid** (open-source + enterprise wrapper):
- Workflow routing engine (open)
- Enterprise policy model extensions (private)
- Control plane (initially private, eventually open-source basic + managed advanced)

### How We Validate Privately Before Committing

**Pattern**:
1. Build control plane + enterprise features in private branch
2. Deploy to 1–2 friendly enterprise customers
3. Validate product-market fit signals
4. Decide: open-source some parts, keep some proprietary
5. Only then merge to main (if open-sourcing)

**This enables**:
- Real enterprise feedback before public commitment
- No reveal of commercial direction to competitors
- Clean separation: "we're keeping this proprietary" is choice, not necessity
- Ability to open-source later if strategy changes

### Team Structure

**Open-source team** (public visibility):
- Routing engine improvements
- Hook improvements
- Public feature development

**Enterprise team** (private visibility):
- Control plane development
- Enterprise features
- Security/compliance work
- Early customer pilots

**Could overlap or be same people**, but work in separate branches. Merge to main only when ready to go public.

---

## PART VIII: OPTIONALITY & COMPANY-BUILDING READINESS

### This Strategy Keeps Open

✅ **Option to remain independent open-source project**
- If enterprise doesn't take off, keep improving open-source
- Sustainable via GitHub sponsorships, consulting, community

✅ **Option to build company**
- Control plane + enterprise offering creates revenue model
- With 5–10 customers at $20K/year = $100K–200K ARR → can fund team
- With 20–30 customers = $400K–600K ARR → fundable

✅ **Option to be acquired**
- If Anthropic, OpenAI, or cloud company wants routing layer, can sell
- Open-source + growing enterprise business = attractive acquisition target

✅ **Option to build vertical solutions**
- Healthcare, finance, government applications built on platform
- Each vertical could be separate business

### Fundraising Readiness

**Not ready to fundraise yet**, but this strategy enables it:

**Proof points needed before Series A**:
1. ✅ Open-source adoption (already have: 1K+ PyPI downloads/month)
2. ⚠️ Enterprise product-market fit (0/10 customers; need 3–5)
3. ⚠️ Enterprise revenue ($0; need $100K–200K ARR to credibly fundraise)
4. ⚠️ Workflow routing delivering promised 30–40% savings (need proof)
5. ⚠️ Control plane solving enterprise governance problem (need validation)

**Timeline to fundraising readiness**: 12–18 months if execution goes well.

**Investor thesis** (when ready):
- Large TAM ($25–50M enterprise market)
- Huge unit economics (20–30% LLM cost savings = powerful ROI)
- Competitive moat (workflow routing + control plane)
- Open-source distribution (user acquisition cost $0)
- Expansion opportunity (managed SaaS, verticals, integrations)

---

## PART IX: CORE ASSUMPTIONS & RISKS

### Key Assumptions (Validate or Fail Fast)

1. **Assumption**: Enterprises care about cost control + governance enough to adopt new tool
   - **How to validate**: Talk to 20 enterprise customers; get 3–5 LOIs
   - **Fallback**: If false, pivot to developer-focused (keep open-source, get VC)

2. **Assumption**: Workflow routing delivers 30–40% additional savings in real usage
   - **How to validate**: Build MVP, deploy with 2–3 customers, measure token reduction
   - **Fallback**: If overstated, reset to 15–20% and adjust messaging

3. **Assumption**: Enterprises will pay $15K–50K/year for control plane
   - **How to validate**: Get 1–2 customers to sign term sheet
   - **Fallback**: If false, lower price to $5K–15K, add per-call fee

4. **Assumption**: Control plane can be built self-hosted (no managed SaaS needed for initial traction)
   - **How to validate**: Deploy to 3 customers on their infrastructure
   - **Fallback**: If fails, build managed SaaS first (harder, more capital-intensive)

5. **Assumption**: Competitors won't copy workflow routing + control plane in 6–12 months
   - **How to validate**: Monitor competitive landscape; keep roadmap private
   - **Fallback**: If copied, compete on community trust + execution speed

### Top Risks

| Risk | Probability | Impact | Mitigation |
|---|---|---|---|
| **Enterprises don't care about routing optimization** | 20% | Critical | Talk to customers early; pivot if false |
| **Workflow routing doesn't deliver promised savings** | 25% | High | Build MVP, validate with customers |
| **Can't recruit enterprise team** | 30% | Medium | Hire contractors / fractional CTO initially |
| **Competitors copy before we ship** | 40% | Medium | Focus on speed, keep roadmap private |
| **Control plane too complex to self-host** | 25% | High | Start with simple design, add features later |
| **Enterprise sales cycles too long** | 50% | High | Start with AI-native startups (shorter sales) |
| **Pricing too high / wrong model** | 45% | Medium | Validate pricing with 3+ customers before launch |
| **Open-source community forks enterprise features** | 35% | Medium | Keep enterprise code proprietary + license differently |
| **Burnout building enterprise + maintaining open-source** | 60% | High | Hire team; separate workflows; don't do alone |

---

## FINAL DECISION POINTS (This Quarter)

### Question 1: Do We Commit to Enterprise?

**Decision needed**: Is the goal to build a company/product, or stay as open-source tool?

- **If yes** → Proceed with this strategy (control plane + enterprise packaging)
- **If no** → Focus on routing improvements only (simpler roadmap)

**Recommendation**: **Yes, commit to enterprise.** Open-source can sustain 1–2 person, but platform potential is much larger. Build optionality (can always revert to pure open-source if enterprise fails).

### Question 2: Private or Public Strategy Development?

**Decision needed**: Develop control plane publicly or privately?

- **Public** → More community feedback, but reveals direction to competitors
- **Private** → Faster iteration, better control, ready for enterprise pilots

**Recommendation**: **Develop privately initially.** Get to working MVP + 1–2 customer pilots, then decide what to open-source. Keeps strategic flexibility.

### Question 3: Solo or Team?

**Decision needed**: Is this a solo project or recruiting team?

- **Solo** → Can move fast initially, but hits ceiling ~v4.0
- **Team** → Slower start, but scales to enterprise product

**Recommendation**: **Start solo for v3.5–v4.0 (12 weeks), then hire.** Too early to hire full team; validate market first. Then bring in small team for enterprise build.

### Question 4: Monetization Timeline

**Decision needed**: When to monetize?

- **Immediately** → Charge for control plane even in beta (validates willingness to pay)
- **After MVP** → Get 2–3 customers, then introduce pricing
- **After 5+ customers** → Proven product-market fit, then scale

**Recommendation**: **Monetize after MVP (v4.0 release).** Don't charge alpha customers, but do charge at v4.0 launch. If customers balk, revert to open-source. Risk is minimal.

---

## NEXT STEPS (THIS WEEK)

1. **Validate enterprise thesis** — Call 5–10 enterprise customers: "Would you pay $15K/year for routing governance + cost optimization?"
2. **Design control plane** — Sketch API, data model, deployment architecture
3. **Prototype workflow routing** — POC of step-level routing in existing orchestrator
4. **Recruit advisor** — Bring on enterprise GTM advisor (part-time)
5. **Set up private development** — Create `.internal/` branch, establish development process

---

**Status**: Strategy approved, ready to execute.
**Next review**: 90 days (after v3.5 + v4.0 MVP completed).
