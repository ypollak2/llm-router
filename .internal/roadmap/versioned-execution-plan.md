# Versioned Execution Plan — LLM Router v3.5 through v5.x

**Confidential. Internal only. Detailed engineering + product roadmap for 18–24 months.**

---

## Executive Summary

This roadmap spans v3.5 (released) through v5.x (18–24 months out) and details the sequencing required to move from "individual developer tool" to "enterprise platform." Key strategic shifts:

1. **v3.5–v3.8**: Build workflow-level routing and semantic task understanding (enables 30–40% additional cost savings)
2. **v4.0–v4.2**: Introduce control plane and policy engine (enables governance, compliance, enterprise sales)
3. **v4.3–v4.5**: Ship enterprise features (audit logs, SSO, on-prem, advanced analytics)
4. **v5.0+**: Managed SaaS, marketplace, partner integrations (sustains growth, network effects)

**Why this sequence?** Workflow routing de-risks control plane development (proof that semantic understanding works), and control plane de-risks enterprise features (proof that governance resonates).

---

## v3.5 (RELEASED) — Individual Developer Experience

### Problem Statement
Current product (v3.4 and earlier) is powerful but isolated: each tool call is routed independently. Single-call optimization leaves 30–40% cost reduction on the table for multi-step workflows (e.g., "plan, then implement, then review").

### Why It Matters Now
- Users running complex agentic workflows (AutoGen, LangGraph) are adopting competing products that understand multi-step patterns
- Workflow-level optimization is visible in Claude desktop (agentic tasks are much cheaper than single calls)
- Early adopter feedback: "llm-router is great for single calls, but doesn't help my agents"

### Product Scope
- Multi-call sequence detection and cost-aware routing (e.g., detect "plan → code → review" pattern)
- Workflow session management (group related calls, assign to same model chain)
- Cost projection for multi-step workflows (show user: "This 5-step workflow will cost ~$0.50 on GPT-4o, $0.15 on Claude")
- Basic workflow DAG visualization (UI or CLI output showing cost by step)

### Engineering Scope
- **Core**: Sequence detector (stateful routing that tracks call patterns within a session)
- **Feature**: Workflow cost calculator (estimate total workflow cost before execution)
- **Integration**: Hook into existing routing chain (non-breaking change to router.py)
- **Observability**: Per-workflow cost tracking (savings report breaks down by workflow type)

### Technical Design Expectations
```python
# New tool: llm_route_workflow
# Input: list of tasks with dependencies
# Output: per-task routing recommendations + total cost estimate

llm_route_workflow([
  {"step": "plan", "task": "outline implementation", "complexity": "moderate"},
  {"step": "code", "task": "write implementation", "dependency": "plan"},
  {"step": "review", "task": "review and refactor", "dependency": "code"}
])
# Returns:
# [
#   {"step": "plan", "model": "claude-3.5-sonnet", "cost": $0.12, "duration": "12s"},
#   {"step": "code", "model": "claude-3.5-sonnet", "cost": $0.35, "duration": "45s"},
#   {"step": "review", "model": "claude-3.5-haiku", "cost": $0.05, "duration": "8s"}
# ]
# Total: $0.52, 65s (vs $1.45 if each step was routed independently)
```

### Dependencies
- Existing router infrastructure (v3.4)
- No new external dependencies

### Risks
- **Sequence detection false positives**: May route similar-looking calls as "workflow" when they're independent → **Mitigation**: Make detection opt-in (explicit workflow declaration)
- **Latency**: Stateful routing adds per-call lookup cost → **Mitigation**: Cache at session level, keep in-memory

### Tradeoffs
- **Breadth vs Depth**: Shipping basic workflow detection now vs waiting for perfect graph-based DAG execution → **Decision**: Ship basic, iterate
- **UI Complexity**: Show detailed workflow DAG vs simple cost summary → **Decision**: CLI/API first, UI in v3.6

### Success Metrics
- ≥30% additional cost reduction on multi-step workflows (vs v3.4)
- ≥50% of advanced users (those running agentic frameworks) adopting workflow routing
- <50ms latency overhead per workflow call
- User feedback: "Workflow routing cuts my multi-agent inference costs in half"

### Public vs Internal Classification
**PUBLIC**: This is a user-facing feature and will be announced in release notes.

### Testing Requirements
- Unit tests: sequence detector (true positives, false negatives)
- Integration tests: real workflows (plan → code → review patterns)
- Performance tests: per-workflow latency, memory overhead
- User acceptance tests: real agents (AutoGen, LangGraph) running through llm-router

### Rollout Logic
1. **Alpha**: Internal testing only (Yali's projects)
2. **Beta**: Opt-in via environment variable `LLM_ROUTER_WORKFLOW_ENABLED=true`
3. **GA**: Default enabled, with opt-out mechanism

---

## v3.6 — Semantic Task Understanding

### Problem Statement
Current routing doesn't understand the **intent** of tasks, only their complexity. A "rewrite marketing copy" task is routed the same as "debug a crash," but they have very different optimal models (cheap writing model vs expensive deep reasoning).

### Why It Matters Now
- Early v3.5 feedback shows workflow routing is powerful, but suboptimal if all steps hit the same model
- Content generation tasks need cheaper, faster models (Gemini Flash, Haiku)
- Analysis and reasoning tasks need more capable models (Claude Sonnet, o3)
- LLM market is bifurcating: cheap commodity models for writing/generation, expensive reasoning models for complex tasks
- Opportunity: 15–25% additional cost reduction by matching task intent to model capability

### Product Scope
- Semantic task classifier: "Is this writing, analysis, planning, coding, or review?"
- Intent-based routing: Route writing tasks to Gemini Flash/Haiku, analysis to Sonnet/o3
- Per-task complexity override: Allow user to say "This looks like writing, but treat it as complex reasoning"
- Explanation of routing decision: "Routed to Haiku because this is content generation (low complexity, cheap model)"

### Engineering Scope
- **Core**: Task intent classifier (lightweight, <100ms per call)
- **Feature**: Update router.py to use intent in addition to complexity
- **Integration**: Extend existing RoutingProfile and TaskType enums
- **Observability**: Track intent distribution across workloads (e.g., 30% writing, 50% coding, 20% analysis)

### Technical Design Expectations
```python
class TaskIntent(Enum):
  WRITING = "writing"      # Content creation, marketing, documentation
  CODING = "coding"        # Code generation, implementation
  ANALYSIS = "analysis"    # Deep reasoning, debugging, design
  PLANNING = "planning"    # Outlining, structuring, brainstorming
  REVIEW = "review"        # Code review, QA, editing

# New classifier function
def classify_task_intent(prompt: str) -> TaskIntent:
  # Uses heuristics + optional LLM call (Ollama or cheap API)
  # Keywords: "write", "generate", "create" → WRITING
  # Keywords: "debug", "analyze", "explain" → ANALYSIS
  # Keywords: "implement", "code", "function" → CODING
  # etc.
  pass

# Updated routing decision
decision = router.route(
  prompt=user_prompt,
  complexity=classify_complexity(user_prompt),
  intent=classify_task_intent(user_prompt)
)
# Intent shapes model selection:
# intent=WRITING → prefer Gemini Flash, then GPT-4o-mini, then Claude 3.5 Haiku
# intent=ANALYSIS → prefer Claude 3.5 Sonnet, then GPT-4o, then o3
# intent=CODING → prefer Claude 3.5 Sonnet, then GPT-4o, then Gemini Pro
```

### Dependencies
- v3.5 (workflow routing)
- Optional: Local Ollama for intent classification (already in config)

### Risks
- **Classifier accuracy**: Misclassifying intent (e.g., "write code" labeled as WRITING instead of CODING) → **Mitigation**: Heuristic-first classifier, LLM fallback only if uncertain
- **Over-optimization**: Aggressively routing cheap models might reduce quality for borderline tasks → **Mitigation**: Track rejection/retry rates, auto-escalate if needed

### Tradeoffs
- **Speed vs Accuracy**: Fast heuristic classifier vs slower but more accurate LLM-based classifier → **Decision**: Ship heuristic first (5ms), add LLM fallback in v3.7
- **Task Intent Granularity**: 5 intents (above) vs 10+ (more precise) → **Decision**: Ship with 5, expand based on user feedback

### Success Metrics
- ≥95% accuracy on intent classification (validated on 1,000 real prompts)
- ≥20% additional cost reduction on mixed workloads (vs v3.5)
- Intent distribution visible in savings reports
- User feedback: "The tool finally understands what I'm trying to do"

### Public vs Internal Classification
**PUBLIC**: This is a user-facing feature (though internals are complex).

### Testing Requirements
- Unit tests: intent classifier (true positives/negatives for each intent)
- Integration tests: real prompts from users (validate 95%+ accuracy)
- A/B testing: v3.5 vs v3.6 routing on same workload (measure cost difference)
- User feedback: Deploy to alpha users, collect intent mislabeling reports

### Rollout Logic
1. **Alpha**: Internal only, heuristic classifier only
2. **Beta**: Opt-in via `LLM_ROUTER_INTENT_ENABLED=true`, add LLM fallback for uncertain cases
3. **GA**: Default enabled, with intent override mechanism for edge cases

---

## v3.7 (Q1–Q2 2027) — Agentic Framework Integration

### Problem Statement
Agentic frameworks (AutoGen, LangGraph, CrewAI) are becoming the standard way to build AI apps, but they lack built-in cost awareness. Users are building multi-agent systems that blow budgets because no framework has workflow routing.

### Why It Matters Now
- AutoGen and LangGraph reaching production maturity (Microsoft, LangChain backed)
- Early users of llm-router want it deeply integrated into their agents
- Enterprise customers will demand agent-level cost tracking
- Opportunity: Make llm-router a "must-have" for agentic app builders

### Product Scope
- Official llm-router integrations: AutoGen, LangGraph, CrewAI (first 3)
- Per-agent routing: "This agent should route through our cheap models, that agent should use expensive reasoning"
- Agent cost attribution: Dashboard showing cost by agent, by step
- Observability: Trace tool showing which agent called which model at what cost

### Engineering Scope
- **Core**: Adapter layer for each framework (AutoGen, LangGraph, CrewAI)
- **Feature**: Per-agent routing policy (optional, overrides global settings)
- **Integration**: MCP tools for agent introspection (list agents, get costs, etc.)
- **Observability**: Agent-level cost tracking in cost.py

### Technical Design Expectations
```python
# AutoGen integration example
from autogen import AssistantAgent, UserProxyAgent
from llm_router.integrations.autogen import LLMRouterConfig

# Configure routing per agent
router_config = LLMRouterConfig(
  default_profile="balanced",
  agent_overrides={
    "coder": {"profile": "balanced", "intent_preference": "coding"},
    "reviewer": {"profile": "budget", "intent_preference": "review"},
    "planner": {"profile": "moderate", "intent_preference": "planning"}
  }
)

# Agents now use llm-router internally
assistant = AssistantAgent(
  "coder",
  llm_config={
    "client": router_config.get_client("coder"),
    "model": "auto"  # llm_router decides model per call
  }
)
```

### Dependencies
- v3.6 (semantic task understanding)
- AutoGen 0.2+, LangGraph 0.1+, CrewAI 0.1+ (optional, for integration)

### Risks
- **Framework API changes**: Frameworks evolve, integrations break → **Mitigation**: Version-specific adapters, deprecation warnings
- **Adoption friction**: Users need to refactor existing agents → **Mitigation**: Provide migration guides, offer consulting services

### Tradeoffs
- **Breadth vs Depth**: Support 3 frameworks well vs 10 frameworks partially → **Decision**: Ship 3 well, add more based on user demand
- **Agent-level granularity**: Override every setting per agent vs just profile → **Decision**: Start with profile override, add more in v3.8

### Success Metrics
- ≥80% of agentic framework users aware of llm-router integration
- ≥3 open-source integrations with ≥100 stars each
- ≥50% of enterprise customers using agentic frameworks adopting integration
- Integration documentation: ≥1,000 views per month

### Public vs Internal Classification
**PUBLIC**: Major feature, highlighted in release notes and marketing.

### Testing Requirements
- Integration tests: Real agents (AutoGen, LangGraph, CrewAI) using llm-router
- Framework version matrix: Test against 2–3 versions per framework
- End-to-end tests: Multi-agent workflow with cost tracking
- User acceptance: Feedback from early adopters

### Rollout Logic
1. **Alpha**: AutoGen only, internal testing
2. **Beta**: AutoGen + LangGraph, early adopter release
3. **GA**: All 3 frameworks, public announcement

---

## v3.8 (Q2 2027) — Skill/Tool-Level Routing

### Problem Statement
Current routing is prompt-level: "I have a user prompt, route it." But in agentic systems, users don't specify prompts directly — they define **tools/skills** that agents invoke. A single agent might have 10 tools (search, summarize, write, code, etc.), each needing different routing.

### Why It Matters Now
- Skill routing is the missing piece for agentic apps to be cost-efficient
- Enterprise customers want "this team's code review tool always uses Claude, but search tool uses GPT-4o-mini"
- v3.7 integration feedback will likely ask: "Can I route different tools differently within the same agent?"

### Product Scope
- Tool-level routing: "search" tool uses cheap model, "code_gen" tool uses expensive model
- Tool registry: Define routing policy per tool across all agents
- Tool cost attribution: Dashboard showing cost by tool, by agent
- Tool performance tracking: Measure latency/quality per tool

### Engineering Scope
- **Core**: Tool registry and routing policy application
- **Feature**: Update adapter layer (AutoGen, LangGraph, CrewAI) to apply tool-level routing
- **Integration**: MCP tools for tool management
- **Observability**: Tool-level cost tracking

### Technical Design Expectations
```python
# Tool-level routing policy
tool_routing_policy = {
  "search": {
    "profile": "budget",
    "intent_preference": "analysis",
    "max_cost_per_call": 0.01
  },
  "code_gen": {
    "profile": "balanced",
    "intent_preference": "coding",
    "max_cost_per_call": 0.50
  },
  "write_marketing_copy": {
    "profile": "budget",
    "intent_preference": "writing",
    "max_cost_per_call": 0.02
  },
  "*": {  # Default for all other tools
    "profile": "balanced"
  }
}

# Apply when agent invokes a tool
def invoke_tool(tool_name: str, prompt: str):
  policy = tool_routing_policy.get(tool_name, tool_routing_policy["*"])
  client = router.get_client(profile=policy["profile"], intent=policy.get("intent_preference"))
  result = client.complete(prompt=prompt)
  # Track cost under this tool's budget
  cost_tracker.record(tool=tool_name, cost=result.cost)
  return result
```

### Dependencies
- v3.7 (agentic framework integration)

### Risks
- **Complexity explosion**: Users have 100+ tools, managing routing policy becomes burden → **Mitigation**: Smart defaults, auto-discovery, UI for policy management
- **Coupling**: Tool routing becomes brittle if tools change → **Mitigation**: Version tool policies, provide migration tooling

### Tradeoffs
- **Granularity**: Tool-level vs function-level routing → **Decision**: Tool-level now, function-level in v4.x if demanded
- **Policy complexity**: Simple routing vs complex conditional logic → **Decision**: Start simple, add DSL in v4.x

### Success Metrics
- ≥80% of enterprise agentic users applying tool-level routing
- ≥40% additional cost reduction for agentic workflows (vs v3.6)
- Tool policy visualization: >5,000 active policies in production
- User feedback: "Now I can fine-tune costs at the tool level"

### Public vs Internal Classification
**PUBLIC**: User-facing feature, but technical audience (agentic developers).

### Testing Requirements
- Unit tests: Tool policy resolution and application
- Integration tests: Multi-tool agents with different routing policies
- Performance tests: Large policy sets (100+ tools)
- User acceptance: Early adopters apply complex policies

### Rollout Logic
1. **Alpha**: Internal, AutoGen only
2. **Beta**: Opt-in via `LLM_ROUTER_TOOL_LEVEL_ENABLED=true`
3. **GA**: Default enabled, with tool-level policy management UI in v4.1

---

## v4.0 (Q3 2027) — Control Plane Foundation

### Problem Statement
At v3.8, llm-router has solved cost optimization (individuals save 60–70%), but teams still lack **governance, compliance, and audit trails**. Enterprise customers repeatedly say: "Great tool, but we need to control what models teams can use, audit what they spent, and enforce policies."

This is the inflection point where llm-router shifts from "developer tool" to "platform."

### Why It Matters Now
- v3.5–v3.8 proof that workflow routing and semantic understanding work
- Enterprise pilot customers (identified in v2.1) are ready to adopt if governance exists
- Regulatory pressure (SOC2, FCA, SEC) making audit trails mandatory
- Market window: Competitors haven't shipped control planes yet; window closing in 6–12 months

### Product Scope
- Self-hosted control plane (FastAPI + SQLite first, PostgreSQL in v4.1)
- RBAC: Org-level, team-level, user-level roles and permissions
- Policy engine: Define and enforce routing policies (which models, when, for whom)
- Audit log: Immutable record of all LLM calls (who, when, what, cost)
- Cost dashboard: Real-time spend visibility by org/team/user/model
- Multi-workspace: Support multiple isolated orgs in single deployment

### Engineering Scope
- **Backend**: FastAPI server with SQLite (100–1,000 users per instance)
- **Database**: Schema for orgs, teams, users, policies, audit logs, cost tracking
- **API**: REST endpoints for policy CRUD, cost queries, user management
- **Integration**: Hook into existing llm_router to send audit events and check policies
- **Observability**: Per-org and per-team analytics (cost trends, model adoption, etc.)

### Technical Design Expectations
```yaml
# Control plane architecture (self-hosted)
control-plane/
  ├── app.py              # FastAPI server
  ├── models/
  │   ├── org.py          # Org (billing, settings)
  │   ├── team.py         # Team (cost attribution, quotas)
  │   ├── user.py         # User (RBAC, API keys)
  │   ├── policy.py       # Routing policies
  │   ├── audit_log.py    # Call audit trail
  │   └── cost.py         # Cost tracking
  ├── routes/
  │   ├── orgs.py         # Org management
  │   ├── teams.py        # Team management
  │   ├── policies.py     # Policy CRUD
  │   ├── audit.py        # Audit log queries
  │   └── analytics.py    # Cost dashboards
  ├── security/
  │   ├── auth.py         # API key validation, JWT
  │   └── rbac.py         # Role checking
  └── database/
      ├── connection.py   # SQLite pool
      └── migrations/     # Schema migrations

# Integration with existing llm_router
# Before routing a call, check control plane:
async def route_with_governance(prompt, user_api_key):
  # 1. Validate API key + get user/org/team
  user = control_plane.get_user(api_key=user_api_key)
  
  # 2. Apply org-level policy (e.g., "Can't use GPT-4o")
  policies = control_plane.get_policies(org_id=user.org_id)
  
  # 3. Route using existing llm_router logic
  decision = router.route(prompt, complexity=..., intent=...)
  
  # 4. Check if decision complies with policies
  if decision.model not in policies.allowed_models:
    # Escalate to next best model or reject
    decision = router.route_with_constraints(prompt, allowed_models=policies.allowed_models)
  
  # 5. Check team quota
  team_cost_today = control_plane.get_team_cost(org_id=user.org_id, team_id=user.team_id, date=today)
  if team_cost_today + decision.estimated_cost > policies.team_quota_daily:
    # Reject or warn
    raise BudgetExceededError(f"Team quota exceeded: ${team_cost_today} + ${decision.estimated_cost} > ${policies.team_quota_daily}")
  
  # 6. Execute and record audit log
  result = execute(decision)
  control_plane.record_audit_log(
    org_id=user.org_id,
    team_id=user.team_id,
    user_id=user.id,
    model=decision.model,
    tokens_in=result.tokens_in,
    tokens_out=result.tokens_out,
    cost=result.cost,
    timestamp=now()
  )
  
  return result
```

### Dependencies
- FastAPI 0.100+
- SQLAlchemy 2.0+
- SQLite (built-in) or PostgreSQL driver (optional)
- JWT for API key validation
- Existing llm_router (v3.8)

### Risks
- **Operational complexity**: Teams now need to run and maintain control plane → **Mitigation**: Provide Docker image, managed SaaS option in v4.2
- **Policy complexity**: Enterprise customers want complex conditional policies → **Mitigation**: Start with simple rules, add DSL/UI in v4.3
- **Audit log explosion**: High-volume teams generate millions of audit logs → **Mitigation**: Implement archival strategy (hot/cold storage), pagination

### Tradeoffs
- **SQLite vs PostgreSQL**: Ship with SQLite (simple) vs PostgreSQL (scalable) → **Decision**: Ship SQLite, migrate in v4.1 based on feedback
- **Real-time analytics vs eventual consistency**: Show cost in real-time vs batch update → **Decision**: Batch updates (5-min refresh) in v4.0, real-time in v4.1+
- **RBAC complexity**: Simple (admin/team-lead/member) vs rich (custom roles) → **Decision**: Start with 3 roles, add custom in v4.2

### Success Metrics
- Control plane successfully governs routing for ≥100 org + team combinations
- Audit logs capture ≥99% of LLM calls with <100ms overhead
- Policy compliance: ≥99% of calls comply with active policies
- Customer feedback: "We can now audit all AI usage and enforce governance"

### Public vs Internal Classification
**INTERNAL**: This is the foundation for enterprise offering; not marketed separately.

### Testing Requirements
- Unit tests: Policy resolution, RBAC checks, quota enforcement
- Integration tests: Full flow (auth → policy check → route → audit → analytics)
- Performance tests: Audit log writes under load (1,000 calls/sec)
- Security tests: API key validation, RBAC enforcement, audit log integrity
- Compliance tests: SOC2 audit trail requirements

### Rollout Logic
1. **Alpha**: Internal only, self-hosted, minimal UI
2. **Beta**: Early enterprise customers (identified in v2.1), with dedicated support
3. **GA**: Public release, with SaaS option coming in v4.2

---

## v4.1 (Q4 2027) — Control Plane at Scale + SaaS

### Problem Statement
v4.0 control plane works for single instances (100–1,000 users) but doesn't scale for large orgs or managed SaaS. Enterprise customers want: (1) managed option (don't want to run infrastructure), (2) multi-tenant SaaS with strong isolation, (3) advanced analytics and reporting.

### Why It Matters Now
- v4.0 enterprise pilots will request managed option or high-scale deployment
- SaaS revenue is more predictable than seat-based licensing
- Managed option reduces friction for security-conscious customers

### Product Scope
- Managed SaaS control plane (hosted by llm-router team)
- Multi-tenant isolation (org data completely isolated)
- Advanced analytics: Cost trends, model adoption, team benchmarking
- Billing integration: Stripe for seat-based + per-call pricing
- SSO integration: OAuth/SAML for enterprise directory integration
- High-availability: PostgreSQL + replication, load balancing

### Engineering Scope
- **Backend**: Scale v4.0 from SQLite to PostgreSQL
- **SaaS**: Multi-tenant separation, billing automation
- **Security**: SOC2 compliance, encryption at rest, audit trail for SaaS
- **Analytics**: Data warehouse + BI dashboard
- **Monitoring**: Observability for multi-tenant deployment

### Technical Design Expectations
```yaml
# v4.0 → v4.1 migration (SQLite → PostgreSQL)
# Self-hosted: Deploy PostgreSQL + llm-router control plane
# Managed SaaS: llm-router hosts + maintains PostgreSQL

# Multi-tenant isolation
# Each org gets:
# - Isolated schema or row-level security (RLS)
# - Encrypted API keys (never visible to other orgs)
# - Separate audit logs (org can't see other org's logs)
# - Dedicated cost accounting

# Billing integration
stripe_config = {
  "product_id": "prod_control_plane",
  "pricing": {
    "seat_based": "$500/month per seat (user)",
    "usage_based": "$0.0001 per 1M tokens routed (metered billing)"
  }
}

# SSO integration
sso_providers = ["okta", "azure_ad", "google_workspace"]
# Enterprise customer: "Connect to our Okta tenant"
# → Auto-provision users in control plane
# → Enforce org policies (IP whitelisting, 2FA, etc.)
```

### Dependencies
- PostgreSQL 14+
- Stripe API
- OAuth/SAML libraries (authlib, python-saml)
- Data warehouse framework (e.g., DuckDB for analytics)

### Risks
- **Data breach**: Multi-tenant data isolation isn't perfect → **Mitigation**: Security audit, penetration testing, SOC2 Type II
- **Operational complexity**: Running SaaS is harder than shipping software → **Mitigation**: Hire DevOps engineer, implement runbooks
- **Pricing tension**: Too cheap = can't sustain, too expensive = loses to self-hosted → **Mitigation**: Tiered pricing (small/medium/enterprise)

### Tradeoffs
- **Self-hosted vs SaaS**: Ship both, or SaaS-only → **Decision**: Ship both, but prioritize SaaS for go-to-market
- **Feature parity**: v4.0 self-hosted and v4.1 SaaS have same features vs SaaS-only features → **Decision**: Feature parity + early access for SaaS

### Success Metrics
- ≥50% of enterprise customers adopt managed SaaS
- MRR from control plane: ≥$100K by end of Q4 2027
- SOC2 Type II certification achieved
- Customer NPS: ≥50 for control plane product

### Public vs Internal Classification
**PUBLIC**: Major milestone, announced in press release and roadmap.

### Testing Requirements
- Multi-tenant isolation tests: Verify orgs can't access each other's data
- SSO integration tests: OAuth/SAML flows with Okta, Azure AD, Google
- Billing tests: Stripe integration, usage metering, invoice generation
- Performance tests: PostgreSQL at 10K concurrent users
- Security: Penetration testing, code review, SOC2 preparation

### Rollout Logic
1. **Alpha**: SaaS only, closed beta, 5–10 enterprise customers
2. **Beta**: Self-hosted + SaaS, broader beta
3. **GA**: Public SaaS launch, PostgreSQL self-hosted GA

---

## v4.2–v4.5 (Q1–Q3 2028) — Enterprise Features & Security

### Problem Statement
Control plane foundation (v4.0–v4.1) is in place, but enterprise customers need:
- Advanced audit and compliance features (HIPAA, SOC2, FCA audit trails)
- Fine-grained policy DSL (not just simple rules)
- Advanced analytics (cost attribution, team benchmarking, anomaly detection)
- Extensibility (webhooks, custom integrations)

This is the "enterprise moat" phase where we lock in long-term customers.

### Why It Matters Now
- v4.0–v4.1 enterprise pilots will ask for advanced features
- Compliance/regulatory landscape is shifting (regulators want AI audit trails)
- Competitive threat: Larger vendors (AWS, Azure) are building observability products
- Enterprise GTM requires these features to win large deals

### Scope (v4.2–v4.5)

**v4.2 (Q1 2028): Advanced Audit & Compliance**
- HIPAA-compliant audit logs (PHI protection, encryption, retention policies)
- FCA/SEC compliance features (derivative trading, algorithmic trading governance)
- Compliance dashboard: SOC2, HIPAA, FCA audit checklist
- Data retention policies: Auto-archive old logs, export for compliance reviews

**v4.3 (Q2 2028): Policy DSL & Advanced Governance**
- Policy DSL: Declarative language for complex rules (not just YAML)
  - Examples: "If team is data-science AND usage > $1K today, escalate to approval workflow"
  - Examples: "If model is GPT-4 AND input contains PII, use Claude instead"
- Approval workflows: Policy violations trigger manual review (manager approves or rejects)
- Policy versioning: Track policy changes, rollback if needed

**v4.4 (Q3 2028): Analytics & Business Intelligence**
- Cost attribution: By org, team, project, user, model, intent, tool, workflow
- Benchmarking: "Your team spends 20% more on LLMs than similar teams" (anonymized)
- Anomaly detection: Alert on unusual spending patterns (e.g., spike in Claude usage)
- BI dashboard: Grafana/Looker integration, custom reporting
- Forecasting: Project next month's LLM spend based on trends

**v4.5 (Q3 2028): Extensibility & Integrations**
- Webhook support: Send policy violations, cost alerts to external systems (Slack, PagerDuty)
- Custom integrations: Jira (cost breakdown by project), GitHub (cost by repository)
- API stability guarantee: Publish stable API contracts, backward compatibility for 18+ months
- Marketplace: Community-built policies, integrations, dashboards

### Engineering Scope
- v4.2: SQLAlchemy schema updates for compliance metadata, encryption enhancements
- v4.3: Policy DSL parser (built on Lark or similar), approval workflow engine
- v4.4: Data warehouse (ClickHouse or Snowflake), BI integrations, ML models (anomaly detection)
- v4.5: Webhook system, integration SDKs, marketplace infrastructure

### Technical Design Expectations
```python
# v4.3: Policy DSL Example
policy_dsl = """
policy "data_science_budget_limit" {
  when team == "data-science" and cost_today > $1000 {
    action = "require_approval"
    message = "Team has exceeded daily budget. Manager approval required."
  }
}

policy "pii_protection" {
  when prompt contains ["ssn", "credit_card", "password"] {
    action = "escalate_to_claude"
    reason = "Input contains PII; using Claude for safety"
  }
}

policy "gpt4_limit_for_team" {
  when team == "interns" and model == "gpt-4" {
    action = "deny"
    message = "Interns can only use Haiku and Claude 3.5"
  }
}
"""

# v4.4: Analytics Example
cost_analysis = control_plane.get_cost_analysis(
  org_id="org_123",
  date_range=("2028-01-01", "2028-01-31"),
  group_by=["team", "model"],
  anomaly_detection=True
)
# Returns:
# {
#   "by_team": {
#     "data-science": {"total": $5000, "by_model": {...}},
#     "platform": {"total": $2000, "by_model": {...}}
#   },
#   "anomalies": [
#     {"team": "data-science", "change": "+150%", "reason": "New LLM-intensive project"}
#   ]
# }
```

### Dependencies
- v4.0–v4.1 (control plane foundation)
- Policy DSL library (Lark)
- Data warehouse (ClickHouse or Snowflake)
- BI tool SDK (Grafana, Looker, or custom)
- ML library (scikit-learn or similar for anomaly detection)

### Risks
- **DSL complexity**: Users struggle to write policies → **Mitigation**: Provide UI for policy builder, templates for common cases
- **Data warehouse costs**: ClickHouse/Snowflake can be expensive → **Mitigation**: Use local SQLite + periodic export to cloud warehouse
- **Integration maintenance**: 20+ integrations to maintain → **Mitigation**: Community-maintained integration marketplace

### Tradeoffs
- **Feature count**: Ship 20 integrations in v4.5 vs launch marketplace and let community build → **Decision**: Marketplace (reduces maintenance burden)
- **DSL power**: Turing-complete DSL vs restricted subset → **Decision**: Restricted subset (easier to reason about security)

### Success Metrics
- ≥80% of enterprise customers using advanced policies
- ≥100K cost attribution records per day (tracked across all customers)
- ≥20 community-built integrations in marketplace
- Compliance dashboard: ≥1,000 SOC2 audit references completed
- Customer feedback: "Policy DSL gives us the control we need"

### Public vs Internal Classification
- v4.2 (compliance) — PUBLIC (required for enterprise certifications)
- v4.3 (policy DSL) — PUBLIC (major feature)
- v4.4 (analytics) — PUBLIC (core value proposition)
- v4.5 (integrations) — PUBLIC (ecosystem play)

### Testing Requirements
- Policy DSL: Parser tests, semantic validation, conflict detection
- Approval workflows: State machine tests, notification delivery
- Analytics: Data aggregation correctness, anomaly detection accuracy
- Integrations: End-to-end flow for each integration type
- Compliance: Audit trail immutability, encryption at rest/transit

### Rollout Logic
Each quarter, GA one feature (4.2, 4.3, 4.4, 4.5). Beta releases 6 weeks before GA to gather feedback.

---

## v5.0+ (Q4 2028 onwards) — Network Effects & Marketplace

### Problem Statement
By v4.5 (Q3 2028), llm-router is a mature platform with strong enterprise adoption and governance features. The next phase is building **network effects and ecosystem**: marketplace of policies, integrations, and managed services that increase lock-in and drive expansion revenue.

### Product Scope
- Marketplace: Browse and install community policies, integrations, benchmarks
- Managed services: Consulting, custom policy development, compliance audits
- Benchmarking network: Anonymized peer comparison (industry, company size, use case)
- Extended ecosystem: Partner integrations (Databricks, LangChain, Hugging Face)
- SaaS upsell: Advanced features (anomaly detection, predictive budgeting, AI-assisted policy generation)

### Engineering Scope
- Marketplace backend: Policy/integration discovery, versioning, ratings
- Benchmarking engine: Anonymized aggregation, differential privacy for data protection
- Managed services platform: Booking, delivery tracking, customer portal
- Partner SDKs: Official libraries for major frameworks (LangChain, Databricks, etc.)

### Technical Design Expectations
```yaml
# Marketplace schema
marketplace/
  ├── policies/
  │   ├── "pii-detection-advanced"  # Community policy
  │   ├── "cost-limit-by-project"
  │   └── "compliance-sox-audit"
  ├── integrations/
  │   ├── "jira-cost-breakdown"
  │   ├── "slack-budget-alerts"
  │   └── "datadog-cost-correlation"
  └── benchmarks/
      ├── "saas-companies-series-a"
      ├── "healthcare-compliance-requirements"
      └── "financial-services-cost-per-transaction"

# Benchmarking example (with differential privacy)
benchmark = control_plane.get_benchmark(
  industry="saas",
  company_size="100-1000",
  use_case="content-generation"
)
# Returns anonymized aggregate:
# {
#   "avg_cost_per_user_month": $150,
#   "median_model_preference": "gemini-flash",
#   "top_workflow_patterns": ["writing", "editing", "summarization"],
#   "percentiles": {
#     "p10": $50,
#     "p50": $150,
#     "p90": $400
#   }
# }
```

### Success Metrics
- ≥500 policies in marketplace
- ≥100 integrations available
- ≥50% of enterprise customers adopt ≥1 marketplace policy
- MRR from managed services: ≥$50K
- Benchmarking adoption: ≥70% of customers opt-in to anonymized comparison

---

## Timeline Summary

| Version | Quarter | Focus | Enterprise Readiness |
|---------|---------|-------|----------------------|
| v3.5 | Q3 2026 (current) | Workflow routing | ⭐ (optimization) |
| v3.6 | Q4 2026 | Semantic task understanding | ⭐ (optimization) |
| v3.7 | Q1 2027 | Agentic framework integration | ⭐⭐ (workflow) |
| v3.8 | Q2 2027 | Tool-level routing | ⭐⭐ (workflow) |
| v4.0 | Q3 2027 | Control plane foundation | ⭐⭐⭐ (governance) |
| v4.1 | Q4 2027 | Control plane at scale + SaaS | ⭐⭐⭐⭐ (platform) |
| v4.2–v4.5 | Q1–Q3 2028 | Enterprise features | ⭐⭐⭐⭐⭐ (ready) |
| v5.0+ | Q4 2028+ | Marketplace & ecosystem | ⭐⭐⭐⭐⭐ (lock-in) |

---

## Sequencing Rationale

### Why v3.5–v3.8 Before v4.0?

1. **Proof before platform**: Workflow and semantic routing prove that llm-router can understand complex patterns, not just optimize single calls. This gives enterprise customers confidence in deeper features (control plane).
2. **User feedback**: v3.5–v3.8 will generate tons of feedback about what features users actually need. This feeds into control plane design.
3. **Operational simplicity**: v3.5–v3.8 are relatively low-risk additions to existing router. v4.0 is a completely new product (control plane). Ship low-risk features first.
4. **Go-to-market**: Individual developers (v3.5–v3.8 users) become early advocates for control plane when they join teams.

### Why v4.0 Before v4.2–v4.5?

1. **Foundation first**: Control plane (v4.0–v4.1) is the prerequisite for all enterprise features. Can't audit without core control plane.
2. **Time to value**: Enterprise customers need working control plane + basic audit in place to even consider advanced features. Ship foundation, then layer on.
3. **Go-to-market**: Use v4.0–v4.1 to land enterprise customers, then upsell advanced features (v4.2+).

### Why Managed SaaS in v4.1 (Not v5.0)?

1. **Remove adoption friction**: Enterprise customers hesitant to self-host control plane (ops burden). SaaS removes this objection.
2. **Revenue acceleration**: SaaS is more scalable revenue model than on-prem licensing.
3. **Timing**: By Q4 2027, we'll have enough enterprise customers to justify SaaS infrastructure investment.

---

## Dependencies & Blockers

### Hard Dependencies (Can't Skip)
- v3.5 is prerequisite for v3.6–v3.8
- v3.8 is prerequisite for v4.0 (users expect tool-level routing in control plane)
- v4.0 is prerequisite for v4.1–v4.5
- v4.1 is prerequisite for advanced enterprise features (SaaS infrastructure needed)

### Soft Dependencies (Can be Parallel)
- v3.7 (agentic integration) can start before v3.8 complete
- v4.1 (SaaS) can start before v4.0 GA if parallel teams

### External Blockers
- AutoGen, LangGraph API stability (v3.7)
- Enterprise customer willingness to pilot (v4.0–v4.1)
- Compliance/security audit for SOC2 (v4.1)
- Regulatory changes (v4.2+)

---

## Investment Required

### Engineering Headcount (FTE by Phase)
- v3.5–v3.8: 2–3 engineers (routing/semantic understanding)
- v4.0–v4.1: 3–4 engineers (backend), 1–2 frontend (control plane UI)
- v4.2–v4.5: 5–6 engineers (compliance, DSL, analytics, integrations)
- v5.0+: Sustaining +5 engineers (marketplace, managed services)

### Infrastructure & Services Costs
- v3.5–v4.0: Minimal (~$500/month for dev/staging)
- v4.1: PostgreSQL + SaaS infrastructure (~$5K–10K/month)
- v4.2–v5.0: Data warehouse + BI tools (~$10K–20K/month)

### Runway Needed
- v3.5–v4.1 (18 months): 4–6 engineers, ~$2–3M burn
- v4.2+ (self-sustaining from enterprise revenue)

---

## Success Criteria (End of v5.0)

By end of v5.0 (Q4 2028), llm-router should have:
- ≥100 enterprise customers (ARR $5M+)
- ≥50K open-source downloads per month
- ≥500 active marketplace policies
- ≥10K teams running on control plane
- ≥$50K MRR from managed services
- SOC2 + HIPAA compliance certifications
- Strong product-market fit in agentic platforms and enterprise AI governance
