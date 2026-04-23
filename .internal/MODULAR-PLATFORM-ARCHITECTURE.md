# LLM Router — Modular Platform Architecture & Execution Plan

**STATUS**: Architecture & Execution Planning Phase  
**CLASSIFICATION**: Internal / Confidential  
**AUDIENCE**: Leadership, Principal Architects, Enterprise Strategy  
**LAST UPDATED**: 2026-04-23

---

## Table of Contents

1. [Architectural Principle](#architectural-principle)
2. [Modular Capability Domains](#modular-capability-domains)
3. [Shared Domain Model](#shared-domain-model)
4. [Package & Service Boundaries](#package--service-boundaries)
5. [Role of Current OSS Package](#role-of-current-oss-package)
6. [Detailed Execution Plan](#detailed-execution-plan)
7. [Cross-Cutting Workstreams](#cross-cutting-workstreams)
8. [Dependency & Sequencing Map](#dependency--sequencing-map)
9. [OSS vs Enterprise vs Managed Classification](#oss-vs-enterprise-vs-managed-classification)
10. [First 90-Day Plan](#first-90-day-plan)
11. [What NOT to Do](#what-not-to-do)

---

## Architectural Principle

### The Core Problem

The current llm-router is a monolithic **open-source package** where all capabilities (routing, policy, workflow, telemetry, governance) are tightly coupled into a single MCP server that runs entirely at the client/runtime level.

This architecture works for a free-tier router but **breaks down at enterprise scale** because:

1. **No central intelligence**: Each local router instance makes decisions in isolation. No global telemetry, no cross-tenant insights, no centralized policies.
2. **No governance layer**: All policy decisions are local YAML files with no audit, approval, or enforcement guarantees.
3. **No workflow orchestration**: Complex multi-step tasks are stitched together at the prompt level, not natively routed.
4. **No escalation path**: Quality issues have no structured remediation. No approval queue, no manual override, no escalation.
5. **No token optimization**: Each request is optimized locally; no global deduplication, pruning, or cross-request optimization.
6. **No meaningful telemetry**: Telemetry is logged locally to SQLite; no way to aggregate across instances, teams, or enterprises.
7. **Single edition**: The free OSS package IS the product. Commercial editions are "same package + features flag", which commoditizes everything.

### The Correct Architectural Principle

**Build the platform as a set of decoupled, pluggable, domain-specific modules that communicate via well-defined contracts. Treat the current OSS runtime as ONE possible integration point, not as the center of the business.**

Key principles:

1. **Separation of Concerns**: Each capability domain is independent, with clear inputs/outputs and no coupling between domains.

2. **Distributed Intelligence**: Routing decisions are made at the edge (local runtime), but informed by central intelligence (policies, benchmarks, signals) from the control plane.

3. **Contract-First Design**: All communication between domains is via explicit, versioned contracts (protobuf, OpenAPI, or domain objects). No direct coupling to internal implementation details.

4. **Multiple Runtimes**: The OSS router is one runtime adapter. Future runtimes could be: Python library, Go binary, Docker container, AWS Lambda, Kubernetes operator, SaaS control plane UI, etc.

5. **Optionality Preserved**: Every capability can be deployed as:
   - Local-only (no control plane)
   - Hybrid (local + control plane for policies/telemetry)
   - Fully managed (control plane owns all decisions)
   - Enterprise self-hosted (control plane in customer VPC)
   - Commercial add-on (local routing + premium control plane)

6. **Progressive Disclosure**: Free users get local routing. Paid tiers unlock central policies, team collaboration, advanced analytics, and escalation workflows.

### Why This Matters for Strategy

- **Avoid OSS commodity trap**: If all value is in the free package, there's no enterprise differentiation.
- **Multiple revenue streams**: Local package ($0), team SaaS ($500-2K/month), enterprise control plane ($5K+/month), managed hosting ($10K+/month).
- **Competitive moat**: Competitors copying the OSS package get 30% of the value. Enterprise workflow intelligence stays proprietary.
- **M&A optionality**: The platform can be sold as (1) standalone SaaS, (2) an acquisition to an AI platform (Anthropic, OpenAI, etc.), or (3) a component of a larger enterprise infrastructure play.

---

## Modular Capability Domains

### 1. **Workload Understanding Domain**

**Purpose**: Classify and characterize incoming work to enable intelligent routing decisions.

**Core Responsibilities**:
- Detect task type (QUERY, GENERATE, CODE, ANALYZE, RESEARCH, IMAGE, VIDEO, AUDIO, WORKFLOW)
- Detect complexity (SIMPLE, MODERATE, COMPLEX, DEEP_REASONING)
- Extract workload signals (token count, latency sensitivity, cost sensitivity, quality target, user tier)
- Cache classification results to avoid re-classification
- Detect continuation prompts and multi-step task patterns

**Why It's Modular**:
- Classification logic should be reusable across all runtimes (OSS, library, CLI, control plane UI).
- Classifiers can be swapped (heuristic, local LLM, API-based, ensemble).
- Classification cache is independent of routing decisions.
- Different use cases need different classification strategies (real-time vs. batch, accuracy vs. latency).

**Inputs**:
- User prompt / task description
- Session context (previous exchanges, conversation history)
- User metadata (tier, preferences, history)
- Configuration (complexity thresholds, task detection rules)

**Outputs**:
- ClassificationResult (complexity, task_type, confidence, reasoning)
- WorkloadProfile (summarized characteristics for downstream routing)

**Interfaces**:
```python
# Core classifier interface
async def classify_workload(
    prompt: str,
    context: Optional[str] = None,
    user_id: Optional[str] = None,
    config: ClassifierConfig = default_config
) -> ClassificationResult:
    """Classify a prompt into complexity and task type."""

# Caching interface
async def cached_classify(
    prompt: str,
    cache_key: Optional[str] = None
) -> ClassificationResult:
    """Classify with caching, skipping expensive re-classification."""

# Batch classification for workflows
async def classify_workflow_steps(
    steps: List[WorkflowStep]
) -> List[ClassificationResult]:
    """Classify all steps in a workflow upfront."""
```

**What Must NOT Be Coupled**:
- Routing profile selection (that's Routing Policy Domain)
- Model selection (that's Routing Decision Engine)
- Budget/quota decisions (that's Budget and Governance domains)
- Result execution (that's Runtime)

**Parts That Are Open**:
- Heuristic classifiers and regex patterns (open-source)
- Classification schema and dataclasses (open-source)
- Local classification cache (open-source)

**Parts That May Be Commercial**:
- Advanced ensemble classifiers (uses multiple APIs in parallel)
- Pattern recognition for multi-step workflows (proprietary ML)
- User history-based classification refinement
- Industry-specific classifiers (finance, healthcare, legal)

---

### 2. **Routing Policy Domain**

**Purpose**: Define WHAT routing decisions should be made under various conditions (user type, team, cost constraints, quality targets, compliance rules).

**Core Responsibilities**:
- Define routing policies (rules for which models to use, fallback chains, complexity->profile mappings)
- Support policy inheritance (global > org > team > user > session)
- Enforce policy constraints (cost caps, model blacklists, approval requirements)
- Validate policies against capability discovery
- Audit policy changes and access

**Why It's Modular**:
- Policies must be independent of the runtime executing them.
- Different deployments need different policies (OSS user: simple default, enterprise: complex RBAC rules).
- Policies need to be managed centrally (control plane) while enforced locally (runtime).
- Policy lifecycle (creation, approval, deployment, audit) is separate from policy execution.

**Inputs**:
- Policy definitions (YAML, JSON, or control plane API)
- User/team/org context
- Workload classification (from Workload Understanding Domain)
- Capability discovery results (available models, their costs/speeds)

**Outputs**:
- RoutingPolicy (the applicable policy for this user/team/context)
- RoutingConstraints (do's and don'ts: approved models, cost cap, approval required?, etc.)
- PolicyAuditEvent (who accessed/changed what policy, when, why)

**Interfaces**:
```python
# Policy resolution interface
async def get_routing_policy(
    user_id: str,
    team_id: Optional[str] = None,
    org_id: Optional[str] = None,
    classification: ClassificationResult = None
) -> RoutingPolicy:
    """Resolve the effective policy for this context."""

async def get_routing_constraints(
    policy: RoutingPolicy,
    workload: WorkloadProfile
) -> RoutingConstraints:
    """Extract do/don't constraints from policy for this workload."""

# Policy lifecycle (control plane)
async def create_policy(
    policy_def: PolicyDefinition,
    creator_id: str,
    requires_approval: bool = False
) -> Policy:
    """Create a new policy with optional approval gate."""

async def approve_policy(
    policy_id: str,
    approver_id: str
) -> Policy:
    """Approve a pending policy for deployment."""

async def deploy_policy(
    policy_id: str,
    target_scope: str  # "global", "org:xyz", "team:abc", etc.
) -> None:
    """Deploy approved policy to its target scope."""

# Audit interface
async def get_policy_audit_log(
    scope: str,
    since: datetime
) -> List[PolicyAuditEvent]:
    """Get all policy changes in a scope."""
```

**What Must NOT Be Coupled**:
- Specific model names or capabilities (that's Discovery / Capability Domain)
- Budget calculation or quota enforcement (that's Budget Domain)
- Workflow orchestration logic (that's Workflow Domain)
- Actual routing decision (that's Routing Decision Engine)

**Parts That Are Open**:
- Policy schema and DSL (open-source)
- Local policy loading from YAML (open-source)
- Policy validation logic

**Parts That Are Proprietary/Enterprise**:
- Policy control plane UI and API
- Policy approval workflows
- Hierarchical policy inheritance with role-based access
- Policy templates and industry standards (banking, healthcare, etc.)
- Dynamic policy generation (ML-driven policy recommendations)

---

### 3. **Routing Decision Engine**

**Purpose**: Given a workload, a policy, and available models, compute the optimal model/provider/chain to use.

**Core Responsibilities**:
- Score all available models based on quality, cost, latency, user budget/preferences
- Apply policy constraints (filter out blacklisted models, enforce approval gates, etc.)
- Build fallback chains with optimal ordering
- Enforce budget limits (monthly spend cap, session limits, token budgets)
- Make decisions: primary model, fallback models, escalation paths

**Why It's Modular**:
- Scoring algorithm is independent of the runtime executing the decision.
- Scoring can use different data sources (local benchmarks, central analytics, real-time feedback).
- Different runtimes may make different decisions based on available data.
- Scoring weights and preferences should be configurable per user/team/policy.

**Inputs**:
- WorkloadProfile (from Workload Understanding Domain)
- RoutingPolicy and RoutingConstraints (from Routing Policy Domain)
- ModelCapabilities (discovered models, costs, speeds, context windows)
- BudgetState (current spend, remaining quota per provider)
- UserPreferences (quality mode, model preferences)
- RealTimeSignals (quality feedback, latency measurements, error rates)

**Outputs**:
- RoutingDecision (recommended model, fallback chain, reasoning, confidence)
- CostEstimate (expected cost and latency)
- RequiredApprovals (list of approval gates that must be satisfied before execution)

**Interfaces**:
```python
# Decision-making interface
async def route_and_decide(
    workload: WorkloadProfile,
    policy: RoutingPolicy,
    budget_state: BudgetState,
    available_models: List[ModelCapability]
) -> RoutingDecision:
    """Compute the optimal routing decision."""

async def score_models(
    workload: WorkloadProfile,
    available_models: List[ModelCapability],
    weights: ScoringWeights = default_weights
) -> List[ScoredModel]:
    """Score all models for this workload."""

async def build_fallback_chain(
    primary_model: str,
    workload: WorkloadProfile,
    policy: RoutingPolicy,
    scored_models: List[ScoredModel]
) -> FallbackChain:
    """Build ordered list of fallback models."""

# Cost/budget interface
async def estimate_cost(
    workload: WorkloadProfile,
    model: str
) -> CostEstimate:
    """Estimate cost and latency for a specific model."""

async def check_budget(
    user_id: str,
    estimated_cost: float,
    policy: RoutingPolicy
) -> BudgetCheckResult:
    """Check if this request fits within budget/policy limits."""

# Preference interface
async def apply_user_preferences(
    decision: RoutingDecision,
    preferences: UserPreferences
) -> RoutingDecision:
    """Apply user's quality mode, model preferences, etc. to decision."""
```

**What Must NOT Be Coupled**:
- Specific runtime implementation (the router and the library both use this)
- Policy enforcement (that's Policy Domain's job to filter; this domain scores what's allowed)
- Actual execution (that's Runtime Domain)
- Telemetry recording (that's Analytics Domain)

**Parts That Are Open**:
- Scoring algorithm and weights (open-source)
- Budget checking logic (open-source)
- Cost estimation (open-source)
- Capability discovery integration

**Parts That Are Proprietary/Enterprise**:
- Advanced scoring incorporating ML models for quality/latency prediction
- Dynamic weight optimization (ML learns which weights maximize user satisfaction)
- Real-time signal integration with control plane telemetry
- Cost optimization recommendations
- Predictive budget warnings

---

### 4. **Workflow Routing & Orchestration Domain**

**Purpose**: Enable routing and optimization for multi-step tasks, not just single-shot requests.

**Core Responsibilities**:
- Represent workflows as DAGs (directed acyclic graphs) of steps
- Classify each step in a workflow upfront
- Route each step to optimal model based on step characteristics and data flow
- Parallelize independent steps
- Handle data dependencies and context passing between steps
- Detect and dedup repeated sub-workflows (e.g., "analyze X then summarize" repeated for multiple Xs)
- Provide hooks for manual intervention (pausing, re-routing, approvals)

**Why It's Modular**:
- Workflows are a different execution model than single-shot requests.
- Workflow logic must be independent of the runtime (local OSS router vs. control plane orchestrator).
- Workflow data (steps, dependencies, results) must be stored and replayable.
- Workflow-level optimization (dedup, parallelization) is separate from single-request routing.

**Inputs**:
- Workflow definition (sequence of steps with dependencies)
- Session context and conversation history
- User preferences for workflow style (parallel vs. sequential, aggressive dedup vs. safety)
- RoutingPolicy (applies to each step in the workflow)

**Outputs**:
- WorkflowExecutionPlan (steps to execute, ordering, parallelization, estimated cost/time)
- WorkflowExecution (in-progress or completed, with step results and inter-step data)
- WorkflowOptimizationPlan (dedup opportunities, pruning suggestions, cost reductions)

**Interfaces**:
```python
# Workflow planning interface
async def plan_workflow(
    workflow_def: WorkflowDefinition,
    context: SessionContext,
    policy: RoutingPolicy
) -> WorkflowExecutionPlan:
    """Analyze workflow structure and plan execution."""

async def detect_workflow_pattern(
    prompt: str,
    history: List[Message]
) -> Optional[WorkflowPattern]:
    """Detect if this looks like a multi-step workflow."""

# Workflow execution interface
async def execute_workflow(
    plan: WorkflowExecutionPlan,
    policy: RoutingPolicy,
    on_step_complete: Optional[Callable] = None
) -> WorkflowExecution:
    """Execute workflow according to plan, with callbacks for step completion."""

# Optimization interface
async def optimize_workflow(
    execution: WorkflowExecution,
    history: List[WorkflowExecution]
) -> WorkflowOptimizationPlan:
    """Suggest optimizations based on execution history."""

async def dedup_workflow_steps(
    plan: WorkflowExecutionPlan,
    history: List[WorkflowExecution],
    threshold: float = 0.8
) -> DedupPlan:
    """Identify and dedup repeated sub-workflows."""

# Multi-step routing interface
async def route_workflow_steps(
    steps: List[WorkflowStep],
    policy: RoutingPolicy
) -> List[RoutingDecision]:
    """Route all steps in a workflow, considering step characteristics and data flow."""
```

**What Must NOT Be Coupled**:
- Classification logic (that's Workload Understanding Domain)
- Routing decisions (that's Routing Decision Engine)
- Actual step execution (that's Runtime)

**Parts That Are Open**:
- Workflow representation (DAG format, step definitions)
- Basic parallelization and sequencing
- Dependency tracking

**Parts That Are Proprietary/Enterprise**:
- Advanced dedup and pruning algorithms
- ML-based workflow pattern detection (learns what workflows look like in your domain)
- Workflow optimization recommendations
- Approval gates and human-in-the-loop intervention
- Workflow versioning and A/B testing
- Advanced cost optimization for complex workflows

---

### 5. **Token Optimization Engine**

**Purpose**: Reduce token consumption across the entire workflow/session, not just individual requests.

**Core Responsibilities**:
- Detect repeated prompts and cache responses
- Detect semantic duplicates across steps in a workflow
- Prune redundant context (what information is truly needed for this step?)
- Suggest request compression or prompt refactoring
- Model token usage across workflows to predict savings opportunities
- Recommend caching strategies (what to cache, where to cache)
- Integrate with provider-level features (prompt caching, KV cache, batch operations)

**Why It's Modular**:
- Token optimization is independent of routing decisions.
- Optimizations can be applied at multiple layers (request level, step level, session level, workflow level).
- Some optimizations require telemetry from the entire workflow; others are local.
- Different runtimes may have different optimization capabilities.

**Inputs**:
- Request/prompt (for dedup and compression)
- WorkflowExecution (for cross-step optimization)
- SessionContext (conversation history, previous results)
- UserProfile (past requests, typical patterns)
- ModelCapabilities (provider supports prompt caching? KV cache? batch? etc.)

**Outputs**:
- OptimizationPlan (cache this, prune that, compress here, deduplicate there)
- OptimizationSuggestions (did we detect repeated prompts? Recommend semantic dedup? Could this be batched?)
- ExpectedSavings (estimated token reduction, cost reduction)

**Interfaces**:
```python
# Dedup interface
async def check_request_cache(
    prompt: str,
    context_hash: str = None
) -> Optional[CachedResponse]:
    """Check if we've seen this request before."""

async def cache_response(
    prompt: str,
    response: LLMResponse,
    ttl_seconds: int = 3600
) -> None:
    """Cache a response for future reuse."""

# Semantic dedup interface
async def find_semantic_duplicates(
    steps: List[WorkflowStep],
    threshold: float = 0.8
) -> List[DupGroup]:
    """Find similar/duplicate steps in a workflow."""

# Compression interface
async def compress_prompt(
    prompt: str,
    context: str,
    target_reduction: float = 0.2
) -> CompressedPrompt:
    """Compress prompt while preserving meaning."""

# Batch optimization interface
async def optimize_for_batch(
    requests: List[Request],
    policy: RoutingPolicy
) -> BatchOptimizationPlan:
    """Plan batch execution to minimize total tokens."""

# Workflow-level optimization
async def optimize_workflow_tokens(
    execution: WorkflowExecution,
    policy: RoutingPolicy
) -> TokenOptimizationPlan:
    """Suggest token optimizations across entire workflow."""
```

**What Must NOT Be Coupled**:
- Routing decisions (that's Routing Decision Engine)
- Workflow orchestration (that's Workflow Domain)
- Actual execution (that's Runtime)

**Parts That Are Open**:
- Request dedup detection
- Simple prompt compression heuristics
- Cache management (local LRU, TTL)

**Parts That Are Proprietary/Enterprise**:
- Semantic dedup using embeddings
- Advanced prompt compression using LLM
- Batch optimization algorithm
- Cross-tenant dedup (using anonymized, aggregated patterns)
- Predictive caching (learn what's likely to be needed)
- Integration with provider-level caching APIs

---

### 6. **Quality & Escalation Domain**

**Purpose**: Ensure outputs meet quality standards; escalate, retry, or refine when they don't.

**Core Responsibilities**:
- Define quality standards (per task type, per model, per user)
- Evaluate response quality (correctness, relevance, safety, format)
- Detect quality failures (hallucinations, wrong format, too short/long, off-topic)
- Route escalation (retry with better model, manual review, user feedback, etc.)
- Track quality metrics per model per task type
- Provide feedback loops to improve routing decisions

**Why It's Modular**:
- Quality evaluation is independent of routing.
- Evaluation can use different strategies (heuristic checks, LLM evaluation, user feedback).
- Escalation logic is a separate workflow from primary execution.
- Quality signals inform future routing decisions (feedback loop).

**Inputs**:
- LLMResponse (the actual response to evaluate)
- Workload profile (what was asked)
- UserExpectations (what constitutes acceptable quality for this user)
- QualityPolicy (quality requirements and escalation rules)
- ModelMetrics (historical quality for this model on this task type)

**Outputs**:
- QualityVerdict (pass/fail, confidence, reasoning)
- QualityIssue (specific problem: hallucination, wrong format, off-topic, etc.)
- EscalationPlan (retry with better model, human review, refine, etc.)
- QualityFeedback (for training the quality evaluation model)

**Interfaces**:
```python
# Evaluation interface
async def evaluate_response_quality(
    response: LLMResponse,
    workload: WorkloadProfile,
    user_expectations: QualityExpectations,
    policy: QualityPolicy
) -> QualityVerdict:
    """Evaluate if response meets quality standards."""

async def detect_quality_issues(
    response: LLMResponse,
    workload: WorkloadProfile
) -> List[QualityIssue]:
    """Identify specific quality problems."""

# Escalation interface
async def plan_escalation(
    verdict: QualityVerdict,
    workload: WorkloadProfile,
    policy: QualityPolicy,
    previous_attempts: List[LLMResponse]
) -> EscalationPlan:
    """Plan how to fix quality issues."""

async def execute_escalation(
    plan: EscalationPlan,
    policy: RoutingPolicy
) -> LLMResponse:
    """Execute escalation (retry, refine, manual review, etc.)."""

# Feedback interface
async def record_quality_feedback(
    response: LLMResponse,
    verdict: QualityVerdict,
    user_feedback: Optional[str] = None
) -> None:
    """Record feedback for future quality improvements."""

async def get_quality_metrics(
    model: str,
    task_type: TaskType,
    window: str = "7d"  # "1d", "7d", "30d", "all"
) -> ModelQualityMetrics:
    """Get quality metrics for a model on a task type."""
```

**What Must NOT Be Coupled**:
- Routing decisions (that's Routing Decision Engine; quality evaluation comes AFTER routing)
- Workflow orchestration (though workflows can use quality evaluation as a step)
- Actual model execution (that's Runtime)

**Parts That Are Open**:
- Quality verdict schema and enums
- Simple heuristic checks (response length, format validation)
- User feedback recording

**Parts That Are Proprietary/Enterprise**:
- Advanced quality evaluation using LLM judges
- Hallucination detection
- Safety and compliance checking
- Escalation workflow and approval gates
- Human-in-the-loop review interface
- Industry-specific quality standards (finance, healthcare, legal)

---

### 7. **Telemetry & Analytics Domain**

**Purpose**: Collect, aggregate, and analyze decision/execution data to drive insights and improve the platform.

**Core Responsibilities**:
- Record all routing decisions, executions, costs, quality verdicts
- Aggregate data across users, teams, orgs
- Compute savings metrics, quality trends, cost forecasts
- Detect anomalies (unusual spend, quality drops, latency spikes)
- Generate recommendations (model improvements, cost optimizations, quality fixes)
- Support attribution (who initiated this request, which team, which project)
- Provide data for business analytics (CAC, LTV, churn signals)

**Why It's Modular**:
- Telemetry collection should not block request execution (async, batched).
- Different deployments need different telemetry backends (local SQLite, central analytics, cloud data warehouse).
- Analytics are independent of routing logic.
- Telemetry informs policy/routing improvements but doesn't change how routing works today.

**Inputs**:
- RoutingDecision (what was decided, why)
- ExecutionEvent (request started, completed, failed)
- CostEvent (tokens used, cost incurred)
- QualityVerdict (quality pass/fail)
- UserAction (user feedback, explicit quality ratings)

**Outputs**:
- TelemetryRecord (stored for later analysis)
- AggregatedMetrics (for dashboards, reports, recommendations)
- Anomaly (detected unusual behavior)
- Recommendation (suggested improvement)

**Interfaces**:
```python
# Recording interface
async def record_routing_decision(
    decision: RoutingDecision,
    user_id: str,
    session_id: str,
    metadata: dict = {}
) -> None:
    """Record a routing decision (non-blocking, async)."""

async def record_execution(
    decision: RoutingDecision,
    response: LLMResponse,
    verdict: QualityVerdict = None,
    metadata: dict = {}
) -> None:
    """Record execution and outcome."""

async def record_cost_event(
    user_id: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float
) -> None:
    """Record token/cost usage."""

# Analytics interface
async def get_metrics(
    scope: str,  # "user:xyz", "team:abc", "org:def", "global"
    metric: str,  # "cost", "quality", "latency", "model_distribution", etc.
    window: str = "7d"
) -> MetricResult:
    """Get aggregated metrics for a scope."""

async def compute_savings(
    user_id: str,
    window: str = "7d"
) -> SavingsReport:
    """Compute cost savings from routing decisions."""

async def forecast_budget(
    user_id: str,
    days_ahead: int = 30
) -> BudgetForecast:
    """Forecast token/cost consumption for the period."""

# Recommendations interface
async def get_recommendations(
    scope: str,
    category: str  # "cost_optimization", "quality_improvement", "model_discovery", etc.
) -> List[Recommendation]:
    """Get actionable recommendations."""

async def detect_anomalies(
    scope: str,
    severity: str = "medium"  # "low", "medium", "high"
) -> List[Anomaly]:
    """Detect unusual patterns."""
```

**What Must NOT Be Coupled**:
- Routing decisions (telemetry is passive observation, not decision-making)
- Actual model execution (though telemetry observes it)

**Parts That Are Open**:
- Telemetry schema (dataclasses for events)
- Local recording (to SQLite, files)
- Basic aggregation queries

**Parts That Are Proprietary/Enterprise**:
- Central analytics platform
- Advanced recommendation algorithms (ML-driven)
- Anomaly detection
- Business intelligence dashboards
- Attribution and chargeback (for enterprise cost centers)
- Compliance reporting (audit trails, data retention policies)
- Real-time alerting

---

### 8. **Governance & Approval Domain**

**Purpose**: Ensure decisions align with organizational policies, security requirements, and compliance rules.

**Core Responsibilities**:
- Define approval workflows (who approves what, under what conditions)
- Implement approval gates (block execution until approvals are obtained)
- Audit all decisions and approvals (create, review, approve, execute)
- Enforce compliance rules (data residency, model whitelist/blacklist, cost limits, etc.)
- Manage escalations (what to do if approval is blocked, denied, or timed out)
- Track accountability (who requested, who approved, who executed)

**Why It's Modular**:
- Governance is independent of routing logic.
- Different organizations have different governance requirements.
- Governance can be optional (OSS users have none, enterprise users have strict RBAC + approval gates).
- Approval workflows can be simple (one admin) or complex (multi-level, role-based).

**Inputs**:
- RoutingDecision (what's being proposed)
- RoutingPolicy (what approvals are required for this decision)
- UserContext (who's requesting, what's their role, what's their authority)
- CompliancePolicy (what rules must be enforced)

**Outputs**:
- ApprovalRequest (created, pending review)
- ApprovalVerdict (approved, denied, timed out)
- AuditEvent (logged for compliance)
- ComplianceCheck (pass/fail, remediation if fail)

**Interfaces**:
```python
# Approval interface
async def request_approval(
    decision: RoutingDecision,
    policy: RoutingPolicy,
    requester_id: str,
    reason: str = ""
) -> ApprovalRequest:
    """Create an approval request."""

async def get_approval_status(
    request_id: str
) -> ApprovalStatus:
    """Check if approval has been granted, denied, or timed out."""

async def approve(
    request_id: str,
    approver_id: str,
    notes: str = ""
) -> None:
    """Grant approval."""

async def deny(
    request_id: str,
    approver_id: str,
    reason: str
) -> None:
    """Deny approval."""

async def wait_for_approval(
    request_id: str,
    timeout_seconds: int = 3600
) -> ApprovalVerdict:
    """Block until approval is granted, denied, or times out."""

# Compliance interface
async def check_compliance(
    decision: RoutingDecision,
    policy: CompliancePolicy,
    user_context: UserContext
) -> ComplianceCheck:
    """Check if decision complies with all rules."""

async def enforce_compliance(
    decision: RoutingDecision,
    policy: CompliancePolicy
) -> RoutingDecision:
    """Apply any compliance-driven modifications (e.g., add approval, change model, etc.)."""

# Audit interface
async def record_audit_event(
    event_type: str,  # "request_created", "approved", "denied", "executed", etc.
    decision: RoutingDecision = None,
    user_id: str = None,
    approval_id: str = None,
    metadata: dict = {}
) -> AuditEvent:
    """Record an audit event."""

async def get_audit_log(
    scope: str,  # "user:xyz", "team:abc", "org:def"
    since: datetime = None,
    event_types: List[str] = None
) -> List[AuditEvent]:
    """Retrieve audit trail."""
```

**What Must NOT Be Coupled**:
- Routing decisions (governance is a gate, not part of decision-making)
- Actual execution (approval happens before execution)

**Parts That Are Open**:
- Approval request schema
- Basic audit logging

**Parts That Are Proprietary/Enterprise**:
- Approval workflow engine (complex, multi-level, role-based)
- RBAC and delegation
- Compliance policy engine
- Integration with enterprise auth (SSO, LDAP)
- Advanced audit and compliance reporting
- SOC 2, HIPAA, PCI-DSS compliance support

---

### 9. **Runtime Integration Layer**

**Purpose**: Execute routing decisions in a runtime-specific way (local process, HTTP API, Lambda, etc.).

**Core Responsibilities**:
- Resolve routing decisions into actual LLM API calls
- Handle provider-specific integrations (OpenAI SDK, Anthropic SDK, LiteLLM, etc.)
- Manage fallback chains (try primary, if it fails, try fallback, etc.)
- Handle retries, timeouts, and error recovery
- Stream responses when applicable
- Convert provider-specific responses to unified LLMResponse format

**Why It's Modular**:
- Runtime is the most likely place to have multiple implementations (Python SDK, Go CLI, JavaScript library, cloud function, etc.).
- Runtime should be agnostic to all upstream logic (workload understanding, routing, policy, quality evaluation).
- Runtime just executes what the Routing Decision Engine says.
- Different runtimes may have different capabilities (streaming, async/sync, batch, etc.).

**Inputs**:
- RoutingDecision (primary model, fallback chain, budget limits)
- Prompt / request content
- Stream flag (streaming response or buffered)
- Timeout (how long to wait)

**Outputs**:
- LLMResponse (unified format across all providers)
- StreamedResponse (if streaming is enabled)
- ExecutionError (provider error, timeout, rate-limit, etc.)

**Interfaces**:
```python
# Execution interface
async def execute_routing_decision(
    decision: RoutingDecision,
    prompt: str,
    budget_limit: float = None,
    timeout_seconds: int = 600
) -> LLMResponse:
    """Execute decision, handling fallback chain and error recovery."""

async def execute_with_fallback(
    primary_model: str,
    fallback_chain: List[str],
    prompt: str,
    on_failure: Callable = None
) -> LLMResponse:
    """Try primary, and fallback to chain on errors."""

async def stream_response(
    decision: RoutingDecision,
    prompt: str
) -> AsyncIterator[str]:
    """Stream response tokens as they arrive."""

# Provider integration interface
async def call_provider(
    provider: str,
    model: str,
    prompt: str,
    system_prompt: str = None,
    temperature: float = None,
    max_tokens: int = None,
    timeout_seconds: int = 600
) -> LLMResponse:
    """Make a direct API call to a provider."""

# Error handling interface
async def handle_provider_error(
    error: Exception,
    model: str,
    decision: RoutingDecision
) -> RetryDecision:
    """Decide whether to retry, fallback, or fail."""
```

**What Must NOT Be Coupled**:
- Routing decision logic (that's Routing Decision Engine)
- Anything upstream (policy, quality, governance)
- Anything downstream (telemetry should be recorded by the caller)

**Parts That Are Open**:
- Basic execution loop
- Provider SDK integration
- Error handling and retry logic

**Parts That Are Proprietary/Enterprise**:
- Advanced streaming and response handling
- Custom provider integrations
- Performance optimizations (connection pooling, caching, etc.)

---

### 10. **Enterprise Control Plane**

**Purpose**: Central service providing policies, governance, analytics, and management across all deployed runtimes.

**Core Responsibilities**:
- Host policy definitions and enforcement rules
- Manage team/org hierarchies and RBAC
- Collect and aggregate telemetry from all runtimes
- Provide dashboards, reports, recommendations
- Manage approval workflows
- Audit all decisions and changes
- Provide management APIs (policy creation, team management, etc.)
- Enforce enterprise features (approvals, cost controls, model restrictions, etc.)

**Why It's Modular**:
- Control plane is entirely optional (OSS users don't need it; local defaults apply).
- Control plane is independent of routing logic (runtimes function locally; they just fetch policies from control plane).
- Control plane is a separate deployable service (can be SaaS, self-hosted, embedded in enterprise infrastructure).

**Inputs**:
- Telemetry events from all runtimes
- Policy creation/modification requests from admins
- Approval requests from runtimes
- User/team/org management from admins

**Outputs**:
- Policies (fetched by runtimes)
- Approval decisions
- Analytics / dashboards
- Recommendations
- Audit logs

**Deployment Options**:
- **Not deployed**: OSS users have no control plane (use defaults).
- **SaaS**: Managed control plane hosted by us.
- **Self-hosted**: Enterprise customer deploys control plane in their VPC.
- **Embedded**: Control plane logic embedded in a larger enterprise platform.

**Interfaces**:
```python
# Policy API
GET /api/v1/policies/user/{user_id}
GET /api/v1/policies/team/{team_id}
GET /api/v1/policies/org/{org_id}
POST /api/v1/policies (create)
PUT /api/v1/policies/{policy_id} (update)
DELETE /api/v1/policies/{policy_id}

# Approval API
POST /api/v1/approvals (create approval request)
GET /api/v1/approvals/{approval_id} (check status)
POST /api/v1/approvals/{approval_id}/approve
POST /api/v1/approvals/{approval_id}/deny

# Telemetry API
POST /api/v1/telemetry/routing_decision
POST /api/v1/telemetry/execution
POST /api/v1/telemetry/cost

# Analytics API
GET /api/v1/analytics/metrics
GET /api/v1/analytics/savings
GET /api/v1/analytics/recommendations
GET /api/v1/analytics/anomalies

# Management API
POST /api/v1/teams (create team)
PUT /api/v1/teams/{team_id} (update)
GET /api/v1/teams/{team_id}/members
POST /api/v1/teams/{team_id}/members
DELETE /api/v1/teams/{team_id}/members/{user_id}
```

---

## Shared Domain Model

The following dataclasses/objects define the contracts between capability domains. All modules must use these objects to communicate.

### Core Objects

```python
@dataclass(frozen=True)
class WorkloadProfile:
    """Summarizes key characteristics of a piece of work."""
    task_type: TaskType  # QUERY, GENERATE, CODE, ANALYZE, RESEARCH, IMAGE, VIDEO, AUDIO, WORKFLOW
    complexity: Complexity  # SIMPLE, MODERATE, COMPLEX, DEEP_REASONING
    confidence: float  # Classification confidence 0.0-1.0
    estimated_tokens: int  # Rough estimate of tokens to consume
    latency_requirement_ms: Optional[int]  # Max acceptable latency (None = no constraint)
    cost_sensitivity: CostSensitivity  # LOW, MEDIUM, HIGH
    quality_target: QualityTarget  # What constitutes acceptable output
    user_tier: str  # "free", "pro", "team", "enterprise"
    user_id: str
    session_id: str
    workflow_id: Optional[str]  # Set if this is part of a multi-step workflow

@dataclass(frozen=True)
class RoutingPolicy:
    """Defines WHAT routing decisions should be made for a given scope."""
    policy_id: str
    scope: str  # "global", "org:xyz", "team:abc", "user:def"
    approved_models: FrozenSet[str]  # Empty = all allowed
    blacklisted_models: FrozenSet[str]
    max_cost_per_request: Optional[float]
    max_cost_per_day: Optional[float]
    max_cost_per_month: Optional[float]
    requires_approval_above: Optional[float]  # Requests above this cost need approval
    complexity_to_profile: Dict[Complexity, RoutingProfile]  # Override default mapping
    fallback_chain: List[str]  # If primary fails, try these in order
    created_at: datetime
    created_by: str
    last_modified_at: datetime
    last_modified_by: str

@dataclass(frozen=True)
class RoutingConstraints:
    """What a specific routing decision can and cannot do."""
    approved_models: FrozenSet[str]
    must_have_approval: bool
    cost_limit: Optional[float]
    quality_requirement: QualityLevel
    compliance_checks: List[str]  # List of compliance rules to enforce

@dataclass(frozen=True)
class RoutingDecision:
    """Output of the routing decision engine."""
    decision_id: str
    workload: WorkloadProfile
    primary_model: str
    fallback_chain: List[str]
    estimated_cost: float
    estimated_latency_ms: float
    reasoning: str  # Why this model was chosen
    confidence: float
    required_approvals: List[str]  # Approval IDs needed before execution
    policy_applied: RoutingPolicy
    created_at: datetime

@dataclass(frozen=True)
class LLMResponse:
    """Unified response from executing a routing decision."""
    response_id: str
    content: str
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: float
    citations: List[str]  # For research results
    media_url: Optional[str]  # For image/video/audio
    created_at: datetime

@dataclass(frozen=True)
class QualityVerdict:
    """Evaluation of whether a response meets quality standards."""
    verdict_id: str
    response_id: str
    passed: bool
    confidence: float  # 0.0-1.0
    issues: List[QualityIssue]  # List of detected problems (empty if passed)
    reasoning: str
    evaluator: str  # "heuristic", "llm", "user_feedback", etc.
    created_at: datetime

@dataclass(frozen=True)
class QualityIssue:
    """A specific quality problem with a response."""
    issue_type: str  # "hallucination", "wrong_format", "off_topic", "too_short", etc.
    description: str
    severity: str  # "low", "medium", "high"

@dataclass(frozen=True)
class WorkflowStep:
    """A single step in a multi-step workflow."""
    step_id: str
    task_type: TaskType
    prompt: str
    system_prompt: Optional[str]
    inputs: Dict[str, Any]  # Input data for this step
    depends_on: List[str]  # IDs of steps this depends on (for DAG ordering)
    routing_override: Optional[RoutingDecision]  # If specified, use this routing
    created_at: datetime

@dataclass(frozen=True)
class WorkflowExecutionPlan:
    """Plan for executing a workflow."""
    plan_id: str
    steps: List[WorkflowStep]
    execution_order: List[str]  # Topological sort of step IDs
    parallelizable_groups: List[List[str]]  # Groups of steps that can run in parallel
    estimated_total_cost: float
    estimated_total_latency_ms: float
    created_at: datetime

@dataclass(frozen=True)
class WorkflowExecution:
    """Record of a completed (or in-progress) workflow execution."""
    execution_id: str
    plan: WorkflowExecutionPlan
    step_results: Dict[str, LLMResponse]  # step_id -> response
    step_verdicts: Dict[str, QualityVerdict]  # step_id -> quality verdict
    total_cost: float
    total_latency_ms: float
    status: str  # "in_progress", "completed", "failed", "paused"
    created_at: datetime
    completed_at: Optional[datetime]

@dataclass(frozen=True)
class OptimizationPlan:
    """Recommendations for optimizing a workflow or session."""
    plan_id: str
    opportunities: List[OptimizationOpportunity]  # What can be optimized
    estimated_savings: float  # Expected cost reduction
    estimated_token_savings: int
    priority: str  # "low", "medium", "high"
    created_at: datetime

@dataclass(frozen=True)
class OptimizationOpportunity:
    """A specific way to optimize."""
    opportunity_type: str  # "dedup", "cache", "compress", "model_downshift", etc.
    description: str
    affected_steps: List[str]  # Which steps this affects
    estimated_savings: float
    implementation_effort: str  # "trivial", "easy", "moderate", "hard"

@dataclass(frozen=True)
class ApprovalRequest:
    """Request for approval of a decision."""
    request_id: str
    decision: RoutingDecision
    requester_id: str
    reason: str
    required_approvers: List[str]  # User IDs of people who must approve
    approvals_received: Dict[str, ApprovalResult]  # user_id -> result
    status: str  # "pending", "approved", "denied", "timed_out"
    created_at: datetime
    expires_at: datetime
    completed_at: Optional[datetime]

@dataclass(frozen=True)
class ApprovalResult:
    """Result of one person's approval decision."""
    approver_id: str
    decision: str  # "approved", "denied"
    notes: str
    decided_at: datetime

@dataclass(frozen=True)
class AuditEvent:
    """Record of an action for compliance and accountability."""
    event_id: str
    event_type: str  # "decision_created", "approved", "executed", "quality_check", etc.
    user_id: str
    scope: str  # "user:xyz", "team:abc", "org:def"
    resource_id: Optional[str]  # ID of what was acted on
    resource_type: Optional[str]  # "routing_decision", "approval", "policy", etc.
    action: str  # "create", "read", "update", "delete", "approve", "deny", etc.
    result: str  # "success", "denied", "error", etc.
    metadata: Dict[str, Any]  # Additional context
    created_at: datetime

@dataclass(frozen=True)
class TelemetryEvent:
    """A single recorded event for analytics."""
    event_id: str
    event_type: str  # "routing_decision", "execution", "cost", "quality", etc.
    user_id: str
    session_id: str
    decision_id: Optional[str]
    response_id: Optional[str]
    model: Optional[str]
    task_type: Optional[TaskType]
    complexity: Optional[Complexity]
    cost_usd: Optional[float]
    tokens_used: Optional[int]
    latency_ms: Optional[float]
    quality_pass: Optional[bool]
    metadata: Dict[str, Any]
    created_at: datetime
```

### Why This Model Matters

1. **Decoupling**: Each domain produces and consumes well-defined objects. No direct coupling to implementation details.

2. **Reusability**: Objects are the same across all runtimes (local OSS, Python SDK, Go CLI, control plane, etc.).

3. **Versionability**: Objects can be versioned (v1, v2, etc.) for backward compatibility as the platform evolves.

4. **Serialization**: Objects can be serialized to JSON/protobuf for cross-service communication or persistence.

5. **Immutability**: All objects are frozen (immutable). This makes them safe to share across async tasks and threads.

---

## Package & Service Boundaries

### Proposed Module Structure

```
llm-router-platform/
├── llm-router-core/              # Shared libraries (open-source)
│   ├── workload-understanding/   # Classification, dedup detection
│   ├── routing-policy/           # Policy loading, resolution, validation
│   ├── routing-engine/           # Decision logic, scoring
│   ├── workflow-orchestration/   # DAG execution, step planning
│   ├── token-optimization/       # Caching, dedup, compression
│   ├── quality-evaluation/       # Quality checks, escalation planning
│   ├── telemetry/                # Event recording, local aggregation
│   ├── governance/               # Approval request schema, audit logging
│   ├── runtime-adapter/          # Abstract interface for execution
│   └── types/                    # Shared dataclasses (domain model)
│
├── llm-router-runtimes/          # Runtime implementations
│   ├── python-sdk/               # Python library
│   │   └── src/llm_router/  # (Current OSS package, refactored)
│   ├── cli-binary/               # Go CLI (future)
│   ├── js-sdk/                   # JavaScript/Node.js SDK (future)
│   └── adapters/
│       ├── mcp-adapter/          # MCP server (current OSS adapter)
│       ├── langchain-adapter/    # LangChain integration (future)
│       ├── llamaindex-adapter/   # LlamaIndex integration (future)
│       └── litellm-adapter/      # Direct LiteLLM integration (future)
│
├── llm-router-control-plane/     # Central management service
│   ├── api-server/               # FastAPI server with REST/GraphQL APIs
│   ├── policy-engine/            # Policy resolution and enforcement
│   ├── approval-workflow/        # Approval queue and decision logic
│   ├── analytics-engine/         # Aggregation, reporting, recommendations
│   ├── auth-and-rbac/            # Authentication, RBAC, SSO
│   ├── database/                 # PostgreSQL schema for persistence
│   ├── dashboard/                # Web UI (React/SvelteKit/Next.js)
│   └── deployments/              # Docker, Kubernetes manifests
│
├── llm-router-enterprise/        # Enterprise-only features
│   ├── advanced-policy-engine/   # Complex RBAC, delegation, approval workflows
│   ├── compliance-framework/     # SOC 2, HIPAA, PCI-DSS, etc.
│   ├── sso-and-provisioning/     # SAML, OIDC, SCIM
│   ├── advanced-analytics/       # ML-driven recommendations
│   ├── performance-pack/         # Caching, batch optimization, latency
│   └── marketplace/              # Template library, policy templates
│
├── llm-router-managed/           # Managed SaaS offering
│   ├── helm-charts/              # Kubernetes deployment
│   ├── terraform/                # Infrastructure-as-code
│   ├── multi-tenant-isolation/   # Tenant data isolation
│   ├── billing-and-metering/     # Usage tracking, billing
│   └── support-and-ops/          # Operational tooling
│
└── docs/
    ├── architecture/             # Design docs (this file)
    ├── api-reference/            # API documentation
    ├── deployment-guide/         # How to deploy
    └── contributing/             # Open-source contribution guide
```

### Module Descriptions

#### **llm-router-core/** (Shared, Open-Source)

These are libraries that all runtimes depend on. They contain the "business logic" of routing, policy, optimization, etc.

**Type**: Library packages (Python: pypi, Go: GitHub, JS: npm)

**Purpose**: Provide reusable, runtime-agnostic routing and optimization logic.

**Key APIs**:
- Classification (detect task type/complexity)
- Policy resolution (get applicable policies)
- Routing decision (compute optimal model)
- Workflow planning (plan multi-step execution)
- Token optimization (detect dedup, compression, caching)
- Quality evaluation (assess response quality)
- Telemetry recording (async event logging)

**Open-Source**:
- All of it (no commercial gating)
- MIT or Apache 2.0 license

**Dependencies**:
- Python 3.10+, async support
- Pydantic for schemas
- No heavy dependencies (keep lightweight)

---

#### **llm-router-runtimes/** (Multiple Implementations)

Different ways to USE llm-router-core.

**Type**: Applications/libraries in various languages

**Runtimes**:

1. **python-sdk/**
   - Current OSS package (refactored)
   - Async/await based
   - MCP server (one of many adapters)
   - Importable as a library
   - Dependency: llm-router-core

2. **cli-binary/** (future)
   - Go CLI tool
   - Standalone binary
   - Dependency: Go bindings to llm-router-core (FFI or gRPC)

3. **js-sdk/** (future)
   - Node.js / JavaScript library
   - Works in browsers with API proxy
   - Dependency: TypeScript version of llm-router-core

**Adapters** (ways to integrate with existing tools):

- **mcp-adapter**: MCP server (current)
- **langchain-adapter**: LangChain integration (future)
- **llamaindex-adapter**: LlamaIndex integration (future)
- **litellm-adapter**: Direct LiteLLM wrapper (future)

All runtimes and adapters import from llm-router-core.

---

#### **llm-router-control-plane/** (Central Service, Proprietary/Enterprise)

The centralized management service that orchestrates policy, governance, analytics, and team management.

**Type**: Service (API + Web UI)

**Deployment Options**:
- SaaS (hosted by us)
- Self-hosted (customer deploys in their VPC)
- Not deployed (OSS users don't use it; use local defaults)

**Key Components**:

1. **api-server/**
   - FastAPI REST API (or GraphQL)
   - Receives telemetry from all runtimes
   - Serves policies to runtimes
   - Handles approval requests
   - Manages users, teams, orgs
   - Generates recommendations

2. **policy-engine/**
   - Resolves policies for a given context (user/team/org)
   - Validates policies against capability discovery
   - Generates policy audit logs

3. **approval-workflow/**
   - Creates approval requests
   - Routes to appropriate approvers based on RBAC
   - Tracks approval status
   - Handles timeouts and escalations

4. **analytics-engine/**
   - Aggregates telemetry from all runtimes
   - Computes savings metrics
   - Detects anomalies
   - Generates recommendations
   - Forecasts budget

5. **auth-and-rbac/**
   - User authentication (local + SSO)
   - Role-based access control
   - Team and org hierarchy management
   - Permissions enforcement

6. **database/**
   - PostgreSQL schema for all persistent data
   - Policies, users, teams, orgs, telemetry, audit logs, approvals

7. **dashboard/**
   - Web UI for admins and users
   - Policy management
   - Team management
   - Analytics and dashboards
   - Approval queue
   - Settings

**Ownership**: Proprietary (not open-source)

**License**: Commercial (included with paid plans)

---

#### **llm-router-enterprise/** (Premium Features, Commercial)

Advanced features for enterprise customers.

**Type**: Library modules that extend control plane capabilities

**Components**:

1. **advanced-policy-engine/**
   - Complex RBAC with delegation
   - Multi-level approval workflows
   - Conditional policies (if X then Y)
   - Policy templates and inheritance

2. **compliance-framework/**
   - SOC 2, HIPAA, PCI-DSS, GDPR compliance
   - Data residency enforcement
   - Audit trail guarantees
   - Encryption at rest and in transit

3. **sso-and-provisioning/**
   - SAML 2.0 SSO
   - OIDC (OpenID Connect)
   - SCIM 2.0 provisioning
   - Just-in-time (JIT) user creation

4. **advanced-analytics/**
   - ML-driven recommendations
   - Predictive budget forecasting
   - Anomaly detection with ML
   - Custom dashboards
   - Business intelligence (CAC, LTV, churn)

5. **performance-pack/**
   - Advanced caching (distributed Redis)
   - Batch job optimization
   - Latency prediction and optimization
   - Cost forecasting with accuracy guarantees

6. **marketplace/**
   - Library of policy templates
   - Industry-specific policies (finance, healthcare, etc.)
   - Workflow templates
   - Model recommendations

**Ownership**: Proprietary

**License**: Enterprise (sold separately or as add-on)

---

#### **llm-router-managed/** (SaaS Packaging, Managed Service)

Everything needed to run llm-router as a managed multi-tenant SaaS.

**Type**: DevOps / Infrastructure

**Components**:

1. **helm-charts/**
   - Kubernetes Helm charts for easy deployment
   - StatefulSet for API server
   - StatefulSet for analytics engine
   - PostgreSQL database
   - Redis cache

2. **terraform/**
   - Infrastructure-as-code (AWS, GCP, Azure)
   - VPC, databases, load balancers, monitoring
   - Auto-scaling policies

3. **multi-tenant-isolation/**
   - Tenant data isolation (row-level security in PostgreSQL)
   - Network isolation (if needed)
   - Resource quotas per tenant

4. **billing-and-metering/**
   - Usage tracking (token consumption, API calls, etc.)
   - Billing engine (integrates with Stripe, etc.)
   - Invoice generation
   - Cost allocation per team/project

5. **support-and-ops/**
   - Monitoring and alerting (Prometheus, Grafana)
   - Logging (ELK, DataDog, etc.)
   - On-call playbooks
   - Support ticketing integration

**Ownership**: Proprietary

**License**: Managed SaaS (usage-based billing)

---

## Role of Current OSS Package

### What It Should Be

The current `/src/llm_router/` package should evolve into:

1. **Reference Implementation of Python Runtime**: Shows how to build a runtime using llm-router-core modules.

2. **MCP Server Adapter**: One way to expose routing decisions to Claude Code, but not the only way.

3. **Educational / Demo**: Clear example of how to integrate llm-router-core into a working application.

### What Should Be Extracted

Move out of `/src/llm_router/` and into llm-router-core:

1. **Classification logic** (classifier.py, heuristics) → workload-understanding module
2. **Policy resolution** (policy.py, repo_config.py) → routing-policy module
3. **Routing decision** (router.py, model_selector.py, chain_builder.py, scorer.py) → routing-engine module
4. **Workflow orchestration** (orchestrator.py) → workflow-orchestration module
5. **Token optimization** (semantic_cache.py, prompt_cache.py, compaction.py) → token-optimization module
6. **Quality evaluation** (judge.py, response_validation.py) → quality-evaluation module
7. **Telemetry** (cost.py, model_tracking.py, quality.py) → telemetry module
8. **Shared types** (types.py) → types module

### What Should Stay Local

Keep in the MCP runtime implementation:

1. **MCP server scaffolding** (server.py, how to register tools)
2. **MCP tool handlers** (tools/routing.py, tools/text.py, etc.)
3. **CLI commands** (cli.py, install_hooks.py, quickstart.py)
4. **Local hook execution** (hooks/auto-route.py, hooks/session-end.py)
5. **Provider-specific integrations** (codex_agent.py, gemini_cli_agent.py)
6. **Local caching and configuration** (cache.py, config.py, safe_config.py)

### Architecture Refactoring Steps

**Phase 1: Extract Core Modules**
- Move domain logic to llm-router-core
- Keep llm-router/src as a consumer of llm-router-core
- Add llm-router-core as a dependency in pyproject.toml

**Phase 2: Create SDK Boundaries**
- Python SDK depends on llm-router-core
- MCP adapter is one way to use the Python SDK
- Expose Python SDK for direct library import

**Phase 3: Multiple Runtimes**
- Build additional runtimes/adapters using the same llm-router-core
- Demonstrate that core logic is truly reusable

### Benefits of Refactoring

1. **Decoupling**: MCP server isn't the "heart" of routing; it's just one way to call routing.
2. **Reusability**: Go CLI or JavaScript library can use the same policy/optimization logic.
3. **Enterprise Path**: Control plane doesn't depend on MCP; it uses the same core modules.
4. **Testing**: Core logic is easier to test in isolation.
5. **Contributions**: OSS community contributes to core; enterprise features stay separate.

---

## Detailed Execution Plan

### Stage 0: Architecture Finalization & Planning (Week 1-2)

**Goal**: Define the exact boundaries, contracts, and project structure.

**Why Now**: Before writing code, we need agreement on what each module owns.

**Deliverables**:

1. **Finalized Domain Model** (you're reading the draft)
   - Refine dataclasses based on feedback
   - Define versioning strategy (v1, v2, etc.)
   - Write serialization specs (JSON, protobuf)

2. **API Contracts**
   - Define HTTP APIs for control plane (OpenAPI spec)
   - Define gRPC services (if using gRPC)
   - Define async Python interfaces (type hints)

3. **Project Structure Document**
   - Exact file paths and module naming
   - Dependency graph (which modules depend on which)
   - CI/CD structure

4. **Ownership Matrix**
   - Which team/person owns which module
   - Approval/review processes
   - Release cadence per module

5. **Testing Strategy**
   - Unit tests per module (test/llm-router-core/{module}/)
   - Integration tests (how modules work together)
   - Contract tests (ensure interfaces are honored)

**Exit Criteria**:
- [ ] Domain model approved by team
- [ ] Contracts documented and agreed
- [ ] Project structure finalized
- [ ] CI/CD plan documented
- [ ] Testing strategy signed off

**Risks**:
- Changing the domain model mid-project is expensive
- Contracts must be stable before implementation
- Missing dependencies (e.g., control plane needs X from core, but core doesn't have it)

---

### Stage 1: Shared Intelligence Foundations (Week 3-6)

**Goal**: Build llm-router-core modules that all runtimes will depend on.

**Why This Stage**: Core modules are the foundation; everything else builds on them.

**Deliverables**:

1. **llm-router-core Repository**
   - New public GitHub repository
   - MIT license
   - Minimal dependencies (pydantic, aiohttp, SQLite)

2. **types/ Module**
   - All dataclasses from shared domain model
   - Serialization (to/from JSON)
   - Version numbers (v1, v1.1, etc.)

3. **workload-understanding/ Module**
   - Classification logic (heuristic + Ollama + API)
   - TaskType and Complexity enums
   - WorkloadProfile generation
   - Classification caching

4. **routing-policy/ Module**
   - Policy schema and validation
   - Policy loading (from YAML, JSON, or dict)
   - Policy resolution (find applicable policy for user/team/org)
   - RoutingConstraints extraction
   - Audit event generation

5. **routing-engine/ Module**
   - Scoring algorithm and weights
   - Model selection (primary + fallback chain)
   - Budget checking and enforcement
   - RoutingDecision generation
   - No direct coupling to MCP or providers

6. **Quality Evaluation Foundation**
   - QualityVerdict and QualityIssue schemas
   - Heuristic quality checks (length, format, etc.)
   - Hooks for LLM-based evaluation (not implemented yet, just interface)

7. **Telemetry Foundation**
   - TelemetryEvent schema
   - Local async recording (to SQLite, files, or stdout)
   - Aggregation queries (cost sum, quality rate, etc.)
   - No remote transmission yet (that's control plane)

8. **Tests**
   - Unit tests for each module (80%+ coverage)
   - Integration tests (policy + routing together)
   - Contract tests (interfaces are honored)
   - Performance tests (classification should be <100ms, routing <50ms)

**Architecture Decisions**:
- Python async/await everywhere (using asyncio)
- Pydantic for all dataclasses (not frozen yet, just working versions)
- SQLite for local persistence (no external dependencies)
- No external network calls (Ollama is local, or use free/cheap APIs)

**Exit Criteria**:
- [ ] Core repository created and public
- [ ] All 7 modules implemented and tested
- [ ] 80%+ test coverage
- [ ] Documentation (README, module docstrings)
- [ ] Versioning strategy (semver, changelog)
- [ ] PyPI package published (llm-router-core)

**Risks**:
- API surface is still evolving; changes are expensive
- Tight coupling between modules (e.g., routing-policy needs types)
- Dependencies on external services that might be unavailable

---

### Stage 2: Runtime Decoupling & Refactoring (Week 7-12)

**Goal**: Refactor the current OSS package to use llm-router-core; prove modularity by extracting what depends on MCP.

**Why This Stage**: Validates that core is truly decoupled from runtime.

**Deliverables**:

1. **Refactor Current OSS Package**
   - Add llm-router-core as a dependency
   - Replace internal classification logic with core version
   - Replace policy loading with core version
   - Replace routing decision with core version
   - Remove duplicate code

2. **Extracted Common Logic**
   - workload_understanding.py → uses core.classification
   - policy.py → uses core.policy_resolution
   - router.py → uses core.routing_decision
   - types.py → imports from core.types
   - cost.py → uses core.telemetry
   - judge.py → uses core.quality_evaluation

3. **MCP-Specific Code (stays local)**
   - server.py (MCP scaffolding)
   - tools/ (MCP tool handlers)
   - cli.py (CLI commands)
   - hooks/ (local hook execution)
   - provider integrations (codex_agent.py, gemini_cli_agent.py)

4. **New Runtime Abstraction**
   - runtime_adapter.py (interface)
   - mcp_adapter.py (MCP implementation of runtime_adapter)
   - Shows how other runtimes would implement the same interface

5. **Python SDK**
   - llm-router (pure library, no MCP)
   - Directly imports llm-router-core
   - Exposes routing.route() as primary API
   - MCP server uses the SDK under the hood

6. **Integration Tests**
   - Test that refactored code produces same results as before
   - Regression tests (old functionality still works)

**Architecture Changes**:
- Current llm-router/src/ now imports from llm-router-core
- Reduces llm-router/src/ by ~2000 LOC (logic moved to core)
- Tests now validate both core modules AND local adaptation

**Exit Criteria**:
- [ ] All existing functionality still works (zero regression)
- [ ] Internal duplicate code removed
- [ ] llm-router-core used by default (not fallback)
- [ ] New SDK published as separate package
- [ ] MCP adapter is clearly just an adapter, not the core
- [ ] Documentation updated (architecture, module boundaries)

**Risks**:
- Breaking existing users if refactoring introduces bugs
- Performance regression if core modules are slower
- Circular dependencies between core and runtime (must avoid)

---

### Stage 3: Workflow Routing & Multi-Step Tasks (Week 13-20)

**Goal**: Enable native support for multi-step workflows, not just single requests.

**Why This Stage**: Many users want to optimize entire workflows, not individual requests.

**Deliverables**:

1. **Workflow Definition & Parsing**
   - WorkflowStep, WorkflowDefinition, WorkflowExecutionPlan schemas
   - Parser (turn prompt description → workflow DAG)
   - Validation (check dependencies, detect cycles)

2. **Multi-Step Classification**
   - Classify all steps in a workflow upfront
   - Detect workflow patterns (e.g., "analyze X then summarize")
   - Estimate total tokens and cost

3. **Workflow Routing**
   - Route each step to optimal model
   - Consider data dependencies between steps
   - Parallelize independent steps
   - BuildFallbackChain per step

4. **Workflow Execution**
   - Execute workflow according to plan
   - Pass results between steps
   - Handle failures (retry, escalate, skip)
   - Track execution progress

5. **Workflow Optimization**
   - Detect dedup opportunities (same prompt asked twice)
   - Suggest semantic dedup (similar prompts, reuse cached result)
   - Suggest parallelization
   - Suggest result caching

6. **Tests**
   - Unit tests for workflow planning
   - Integration tests for execution
   - Optimization tests (dedup actually saves tokens)

**API Changes**:
- New WorkflowRouting interface in core.routing_engine
- New Workflow module in llm-router-core
- New /tools/orchestration.py in MCP server (if using workflows from MCP)

**Exit Criteria**:
- [ ] Workflow DAG representation complete
- [ ] Each step routed independently
- [ ] Parallelization working
- [ ] Dedup saving real tokens (measured in tests)
- [ ] Backward compatible (single-step requests still work)

**Risks**:
- Workflow parsing is hard (extracting steps from natural language)
- Dedup detection requires embeddings (external service)
- Complex DAGs might be slower to optimize than necessary

---

### Stage 4: Token Optimization Engine (Week 21-28)

**Goal**: Reduce token consumption across requests, workflows, and sessions.

**Why This Stage**: Token savings are high-leverage (every % saved = real cost reduction).

**Deliverables**:

1. **Request-Level Caching**
   - Semantic cache (detect similar requests)
   - Exact match cache (seen this exact request before)
   - Cache invalidation strategies
   - Measure cache hit rate

2. **Prompt Compression**
   - Heuristic compression (remove redundant words, extra whitespace)
   - LLM-based compression (use cheap model to compress)
   - Context pruning (what information is truly needed?)
   - Measure token savings

3. **Dedup Detection**
   - Detect repeated tasks in a session
   - Semantic dedup (similar tasks, reuse result)
   - Multi-tenant dedup (if permitted by policy, anonymized patterns)
   - Measure token savings

4. **Batch Optimization**
   - Detect opportunities for batching
   - Use provider batch APIs where available
   - Measure latency/cost trade-off

5. **Integration with Routing**
   - Optimization suggestions before routing (cheaper with this optimization)
   - Post-execution optimization analysis (did optimization save tokens?)
   - Update routing decisions based on saved tokens

6. **Tests & Measurement**
   - Measure token savings (before vs. after optimization)
   - Measure quality (does optimization reduce quality?)
   - Performance tests (optimization should be fast)

**New Modules in Core**:
- core.optimization (new module)

**Exit Criteria**:
- [ ] Caching implemented and tested
- [ ] Compression saving measurable tokens
- [ ] Dedup detection working
- [ ] Batch optimization available
- [ ] No quality degradation from optimizations

**Risks**:
- Aggressive compression might hurt quality
- Dedup might be too aggressive (false positives)
- Batch APIs might have latency trade-offs

---

### Stage 5: Quality & Escalation Framework (Week 29-36)

**Goal**: Ensure responses meet quality standards; escalate when they don't.

**Why This Stage**: Quality issues undermine trust; escalation paths are required for enterprise.

**Deliverables**:

1. **Quality Evaluation**
   - Heuristic checks (response length, format, syntax)
   - LLM-based evaluation (use judge model to assess quality)
   - User feedback collection (thumbs up/down, explicit ratings)
   - Quality metrics per model per task type

2. **Quality Policies**
   - Define quality standards (per user, per team, per model)
   - Thresholds for pass/fail
   - Escalation rules (what to do if quality is poor)

3. **Escalation Workflows**
   - Retry with better model
   - Refine (ask user for clarification, try again)
   - Manual review (route to human, hold result pending review)
   - Rollback (use cached previous result)

4. **Quality Feedback Loop**
   - Record quality verdicts
   - Update routing decisions based on quality signals
   - ML learns which models are best for each user

5. **Tests**
   - Unit tests for quality evaluation
   - Integration tests for escalation workflows
   - Measurement: quality improvement from escalation

**New Modules**:
- core.quality (new module)
- core.escalation (new module)

**Exit Criteria**:
- [ ] Quality evaluation implemented
- [ ] Escalation workflows working
- [ ] Quality metrics per model available
- [ ] Feedback loop updating routing
- [ ] No false positive quality failures

**Risks**:
- Quality evaluation models might be unreliable
- Escalation might be too conservative (always escalate)
- False positives might frustrate users

---

### Stage 6: Governance & Approval (Week 37-44)

**Goal**: Implement approval gates, audit logging, and compliance workflows.

**Why This Stage**: Enterprise customers require governance; can't scale without it.

**Deliverables**:

1. **Approval Workflow Engine**
   - Approval request creation
   - Routing to appropriate approvers (RBAC-aware)
   - Approval decision recording
   - Timeout and escalation handling

2. **Audit Logging**
   - Record all decisions, approvals, executions
   - Immutable audit trail
   - Query audit logs (who did what, when, why)
   - Export for compliance reporting

3. **Compliance Rules Engine**
   - Define compliance policies (data residency, model restrictions, cost limits)
   - Check decisions against compliance policies
   - Automatically apply remediation (add approval, block, escalate)

4. **RBAC Framework**
   - User roles and permissions
   - Role hierarchy (admin > moderator > user)
   - Delegation (user can delegate approval authority)
   - Audit role changes

5. **Tests**
   - Unit tests for approval workflow
   - Integration tests for RBAC
   - Compliance rule enforcement tests
   - Audit trail integrity tests

**New Modules**:
- core.governance (new module)
- control-plane.approval-workflow
- control-plane.audit-logging
- control-plane.rbac

**Exit Criteria**:
- [ ] Approval workflow implemented
- [ ] Audit trails immutable and queryable
- [ ] RBAC enforced
- [ ] Compliance rules enforced
- [ ] Enterprise-ready governance

**Risks**:
- Governance adds latency (must be fast)
- Approval bottlenecks (who approves what?)
- Audit logging must be scalable (high volume)

---

### Stage 7: Control Plane MVP (Week 45-52)

**Goal**: Build the central management service (policies, governance, analytics, recommendations).

**Why This Stage**: OSS works locally, but enterprises need central control.

**Deliverables**:

1. **API Server** (FastAPI)
   - REST API endpoints (see domain model section)
   - OpenAPI documentation
   - Rate limiting and auth

2. **Policy Management API**
   - Create/read/update/delete policies
   - Policy versioning and history
   - Policy deployment
   - Policy validation

3. **Approval Workflow API**
   - Create approval requests
   - Query approval status
   - Approve/deny endpoints
   - Approval history

4. **Analytics API**
   - Metrics endpoints (cost, quality, latency, model distribution)
   - Savings calculations
   - Recommendations
   - Anomaly detection

5. **User & Team Management API**
   - Create/manage teams
   - Manage team members
   - Role and permission assignment
   - Org hierarchy management

6. **Telemetry Ingestion**
   - Receive events from runtimes
   - Async processing (non-blocking)
   - Aggregation and storage

7. **Database Schema**
   - PostgreSQL schema for all entities
   - Indexes for performance
   - Audit tables

8. **Web Dashboard** (simple MVP)
   - Policy management UI
   - Approval queue view
   - Analytics dashboard (cost, quality, trends)
   - Team management UI

9. **Docker & Deployment**
   - Docker image for control plane
   - docker-compose for local development
   - Kubernetes manifests for production

10. **Tests**
    - Unit tests for API handlers
    - Integration tests (API + database)
    - Load tests (throughput, latency)

**Exit Criteria**:
- [ ] API fully functional
- [ ] Database schema complete
- [ ] Dashboard working
- [ ] Can deploy locally (docker-compose)
- [ ] Can query policies from runtime
- [ ] Telemetry ingestion working

**Risks**:
- Control plane complexity (many moving parts)
- Database performance at scale
- Dashboard usability (admin experience)
- SaaS deployment complexity

---

### Stage 8: Enterprise Packaging & Self-Hosting (Week 53-60)

**Goal**: Package and distribute control plane for enterprise self-hosting.

**Why This Stage**: Enterprise customers want to run control plane in their VPC.

**Deliverables**:

1. **Helm Charts**
   - llm-router control plane
   - PostgreSQL database
   - Redis cache
   - RBAC and network policies

2. **Terraform Modules**
   - AWS (VPC, RDS, ALB, ECS, etc.)
   - GCP (Cloud Run, Cloud SQL, Load Balancer)
   - Azure (App Service, SQL Database, etc.)

3. **Installation & Setup Guide**
   - Prerequisites (Kubernetes version, cloud account)
   - Step-by-step installation
   - Configuration (SMTP, SSO, etc.)
   - Troubleshooting

4. **Multi-Tenant Support**
   - Row-level security (PostgreSQL)
   - Tenant isolation (network, data, compute)
   - Per-tenant rate limits and quotas

5. **Security Hardening**
   - TLS encryption (all traffic)
   - Database encryption at rest
   - Secrets management (Vault, cloud provider)
   - Network isolation
   - RBAC and audit logging

6. **High Availability**
   - Database replication
   - Multiple API server replicas
   - Load balancing
   - Failover and recovery

7. **Backup & Recovery**
   - Automated daily backups
   - Point-in-time recovery
   - Backup testing

**Exit Criteria**:
- [ ] Helm charts production-ready
- [ ] Terraform modules working
- [ ] Installation guide complete
- [ ] Multi-tenant isolation verified
- [ ] HA and failover tested
- [ ] Enterprise customers can self-host

**Risks**:
- Self-hosting is complex (many failure modes)
- Kubernetes expertise required
- Support burden (customer infrastructure issues)

---

## Cross-Cutting Workstreams

These workstreams run in parallel with stages and persist across the entire roadmap.

### 1. **Domain Model & Contracts Workstream**

**Objective**: Define, maintain, and evolve the shared domain model as understanding deepens.

**Owners**: Architecture team, all domain leads

**Key Activities**:
- Review and refine dataclasses as modules are built
- Define serialization strategies (JSON, protobuf)
- Version contracts (v1, v2, etc.)
- Write contract tests (ensure interfaces are honored)
- Document rationale for each domain object

**Deliverables**:
- Finalized domain model (already outlined)
- Contract tests (verify interfaces)
- Serialization specs (JSON schema, protobuf IDL)
- Version/upgrade guide (how to upgrade from v1 to v2)

**Success Metrics**:
- Zero breaking changes to contracts during stages 1-7
- All modules honor the same contract versions
- Contract tests at 100% pass rate

**Timeline**: Weeks 1-60 (ongoing refinement, freeze before production)

---

### 2. **Workload Intelligence Workstream**

**Objective**: Continuously improve classification accuracy, pattern detection, and context understanding.

**Owners**: Classification team, data science

**Key Activities**:
- Build training datasets (prompts, labels, outcomes)
- Improve heuristic classifiers
- Train ensemble models (combining heuristics + LLM)
- A/B test classifiers on real user data
- Detect workflow patterns (multi-step tasks)
- Build continuation detection (follow-up requests)

**Deliverables**:
- Improved classification accuracy (e.g., 90%+ on unseen data)
- Workflow pattern library (common workflows detected)
- Continuation detection working
- A/B testing framework for classifier improvements

**Success Metrics**:
- Classification accuracy (F1 score, macro-average > 0.90)
- False positive rate (misclassified simple as complex)
- Workflow detection recall (what % of workflows detected)
- Latency (classification < 100ms p99)

**Timeline**: Weeks 1-60 (continuous improvement)

---

### 3. **Policy & Governance Workstream**

**Objective**: Design and implement policy systems that enterprise customers need.

**Owners**: Product, policy team, compliance

**Key Activities**:
- Define policy DSL (what can be expressed)
- Validate policies against capabilities
- Build policy templates (common policies)
- Design approval workflows (simple to complex)
- Define RBAC model (roles, permissions, delegation)
- Design audit system (what to log, retention)

**Deliverables**:
- Policy DSL specification
- Policy validator (catches misconfigurations)
- Policy templates library
- RBAC model and implementation
- Audit system design
- Compliance reporting templates

**Success Metrics**:
- Policy validation catches 100% of invalid configurations
- Policy templates cover 80% of use cases
- Approval workflow latency < 5s
- Audit query latency < 1s for 1M records

**Timeline**: Weeks 1-60 (parallel to control plane build)

---

### 4. **Decision Intelligence Workstream**

**Objective**: Make routing decisions smarter using data and feedback.

**Owners**: ML/analytics team, routing team

**Key Activities**:
- Collect quality feedback (model performance per task type)
- Build quality prediction models (which model best for this task?)
- Optimize scoring weights based on user satisfaction
- A/B test routing decisions
- Track counterfactuals (what if we chose a different model?)
- Build recommendation engine (suggest better models)

**Deliverables**:
- Quality metrics database (model performance history)
- Quality prediction model (which model is best?)
- Weight optimization (learn weights that maximize user satisfaction)
- A/B testing framework
- Recommendations engine (suggest cost/quality improvements)

**Success Metrics**:
- Quality prediction accuracy > 80%
- User satisfaction improves with routing improvements
- Recommendations are acted on by 30%+ of users
- A/B tests show statistically significant improvements

**Timeline**: Weeks 1-60 (continuous, data-driven improvement)

---

### 5. **Workflow Orchestration Workstream**

**Objective**: Make workflows a first-class feature, with optimization and native routing.

**Owners**: Workflow team, orchestration lead

**Key Activities**:
- Design workflow representation (DAG, JSON schema)
- Build workflow parser (prompt → DAG)
- Implement parallel execution
- Optimize for common patterns
- Build workflow libraries (templates)
- Design workflow UX (how users define workflows)

**Deliverables**:
- Workflow schema and parser
- Parallel execution engine
- Workflow optimization algorithms
- Workflow template library
- Workflow UX documentation

**Success Metrics**:
- Workflow definition latency < 500ms
- Execution latency == linear with number of steps (perfect parallelization)
- Dedup detects 80%+ of repeated steps
- Workflow users see 30%+ token savings

**Timeline**: Weeks 13-60 (introduced in stage 3, continuous improvement)

---

### 6. **Token Optimization Workstream**

**Objective**: Every aspect of the system optimizes for token reduction.

**Owners**: Optimization team, cost engineering

**Key Activities**:
- Measure baseline token consumption (before optimization)
- Implement caching strategies (exact match, semantic, TTL)
- Implement compression (heuristic, LLM-based)
- Implement dedup (exact, semantic, cross-tenant)
- Implement batch optimization
- Measure total token savings achieved

**Deliverables**:
- Caching system (pluggable backends: memory, Redis, S3)
- Compression algorithms and benchmarks
- Dedup detection system
- Batch optimization engine
- Token savings dashboard (before/after metrics)

**Success Metrics**:
- Token savings: 20% for simple tasks, 40%+ for workflows
- Cache hit rate: 10-30% for typical users
- Compression effectiveness: 15-25% token reduction
- Quality degradation: < 2% from optimization

**Timeline**: Weeks 21-60 (introduced in stage 4, continuous tuning)

---

### 7. **Quality & Trust Workstream**

**Objective**: Ensure outputs are reliable, correct, and safe.

**Owners**: Quality team, safety/compliance

**Key Activities**:
- Define quality standards per task type
- Build quality evaluation models (hallucination detection, etc.)
- Implement escalation workflows (retry, refine, manual review)
- Build user feedback mechanisms
- Track quality trends per model
- Build quality assurance dashboards

**Deliverables**:
- Quality standards and metrics definitions
- Quality evaluation models (accuracy > 85%)
- Escalation workflow engine
- Feedback collection system
- Quality dashboard (trends, anomalies)

**Success Metrics**:
- Quality evaluation accuracy > 85%
- Escalation success rate > 90% (second attempt succeeds)
- False positive rate < 5% (real quality issues, not false alarms)
- User satisfaction with quality > 4/5

**Timeline**: Weeks 29-60 (introduced in stage 5, continuous improvement)

---

### 8. **Telemetry & Analytics Workstream**

**Objective**: Collect, analyze, and act on data to improve the platform and user outcomes.

**Owners**: Analytics team, data engineering

**Key Activities**:
- Design telemetry schema (what to collect)
- Build telemetry ingestion pipeline (non-blocking, scalable)
- Build aggregation queries (cost, quality, latency)
- Build dashboards (user-facing, admin-facing)
- Build recommendation engine (automated insights)
- Build anomaly detection (unusual behavior)

**Deliverables**:
- Telemetry schema and data pipeline
- Aggregation queries (optimized for performance)
- Dashboards (web UI, exportable reports)
- Recommendation engine (cost, quality, model improvements)
- Anomaly detection system

**Success Metrics**:
- Telemetry ingestion latency < 5s (non-blocking)
- Query latency < 1s for daily aggregations
- Recommendations are relevant (followed up by 40%+)
- Anomalies detected within 1 hour of occurrence
- Data retention 1 year (configurable)

**Timeline**: Weeks 1-60 (starts simple, grows with system)

---

### 9. **Runtime Ecosystem Workstream**

**Objective**: Enable multiple language/platform runtimes, all using the same core logic.

**Owners**: Runtime lead, SDK team

**Key Activities**:
- Design runtime adapter interface
- Build Python SDK (library form)
- Build MCP adapter (current form)
- Design and start Go CLI
- Design and start JavaScript SDK
- Build framework integrations (LangChain, LlamaIndex, etc.)

**Deliverables**:
- Runtime adapter interface
- Python SDK (importable library)
- MCP adapter (Claude integration)
- Go CLI (future, designed in stage 1)
- JavaScript SDK (future, designed in stage 1)
- Framework adapters (pluggable into common tools)

**Success Metrics**:
- Python SDK: 5K+ downloads/month by month 6
- MCP adapter: works in Claude Code
- Go CLI: compiles and runs
- Framework adapters: reduce integration effort by 80%

**Timeline**: Weeks 1-60 (Python first, others follow)

---

### 10. **Enterprise Packaging Workstream**

**Objective**: Make llm-router deployable in enterprise environments (VPC-local, secure, compliant).

**Owners**: DevOps/infrastructure team, enterprise architect

**Key Activities**:
- Design Kubernetes deployment (Helm)
- Design cloud-native deployment (Terraform)
- Implement multi-tenant isolation
- Implement security hardening (TLS, secrets, RBAC)
- Implement HA and failover
- Implement backup/recovery
- Document setup and operations

**Deliverables**:
- Helm charts (production-ready)
- Terraform modules (AWS, GCP, Azure)
- Multi-tenant setup guide
- Security checklist and hardening guide
- HA setup guide
- Operations playbooks (monitoring, alerting, troubleshooting)

**Success Metrics**:
- Helm chart installation time < 15 min
- Multi-tenant isolation verified by security audit
- HA failover time < 30s
- Backup/recovery tested monthly
- Enterprise deployment success rate 100%

**Timeline**: Weeks 45-60 (introduced in stage 8)

---

## Dependency & Sequencing Map

### Hard Dependencies (Must Complete First)

1. **Stage 0 → All Others**: Architecture must be finalized before code is written.

2. **Stage 1 → Stage 2**: Core modules must exist before refactoring runtime to use them.

3. **Stage 1 → Stage 3**: Workflow support requires core classification and routing.

4. **Stage 1 → Stage 4**: Token optimization needs core to be complete.

5. **Stage 1 → Stage 5**: Quality evaluation needs routing decisions to exist.

6. **Stage 1 → Stage 6**: Governance needs core structures (policies, audit).

7. **Stage 2 → Stage 7**: Control plane needs refactored runtime to prove decoupling.

8. **Stage 7 → Stage 8**: Self-hosting requires control plane to be mature.

### Parallel Workstreams (Can Start Early)

- **Workload Intelligence** (Week 1): Collect data while stages 1-2 are building.
- **Policy & Governance** (Week 1): Design policies in parallel with core.
- **Decision Intelligence** (Week 1): Start collecting feedback on current router.
- **Telemetry** (Week 1): Start with basic telemetry to inform later decisions.
- **Runtime Ecosystem** (Week 3): Start planning runtimes once core architecture is clear.

### Critical Path

```
Stage 0 (Weeks 1-2)
    ↓
Stage 1 (Weeks 3-6) [Core modules]
    ↓
Stage 2 (Weeks 7-12) [Refactor runtime]
    ├→ Stage 3 (Weeks 13-20) [Workflows] (parallel)
    ├→ Stage 4 (Weeks 21-28) [Token optimization] (parallel)
    ├→ Stage 5 (Weeks 29-36) [Quality] (parallel)
    ├→ Stage 6 (Weeks 37-44) [Governance] (parallel)
    │
    ↓
Stage 7 (Weeks 45-52) [Control plane] (requires stages 1-6)
    ↓
Stage 8 (Weeks 53-60) [Enterprise packaging] (requires stage 7)
```

**Critical Path Duration**: 60 weeks (~14 months)

**Parallelization Potential**: Stages 3-6 can overlap after stage 2 completes. Workstreams run in parallel throughout.

---

## OSS vs Enterprise vs Managed vs Private Classification

Each capability and component is classified as belonging to one of these tiers:

### **OSS Core** (Free, Open-Source)

**Purpose**: Basic routing and optimization, fully local (no control plane).

**Components**:
- llm-router-core (all modules): workload understanding, routing decisions, basic quality evaluation, telemetry recording
- Python SDK (pure library)
- MCP adapter (for Claude Code)
- CLI (basic commands)
- Local policy loading (YAML files)
- Local caching and storage (SQLite)

**License**: MIT or Apache 2.0

**Examples of Features**:
- Task classification (heuristic + Ollama)
- Model selection (scoring algorithm)
- Basic fallback chains
- Local token caching
- Simple quality heuristics
- Local cost tracking

**Who Uses It**: Individual developers, hobbyists, researchers, small teams

**Revenue**: $0 (free forever)

---

### **Shared Platform** (Available in Both OSS and Enterprise)

**Purpose**: Logic that's useful everywhere and worth keeping in sync.

**Components**:
- Core domain models (types, interfaces)
- Basic telemetry recording
- Basic audit logging
- Provider integrations (OpenAI, Gemini, etc. SDKs)

**License**: MIT or Apache 2.0 (same as OSS core)

**Examples of Features**:
- WorkflowExecution tracking
- TelemetryEvent schema
- AuditEvent schema
- Provider APIs (unified interface)

**Who Uses It**: Developers building on llm-router

**Revenue**: $0 (no direct revenue from shared logic)

---

### **Enterprise Control Plane** (Proprietary, Paid)

**Purpose**: Centralized management, governance, team collaboration.

**Components**:
- Control plane API server
- Policy management service
- Approval workflow engine
- User/team/org management
- RBAC and role management
- Basic analytics (cost, quality, latency)
- Audit logging and compliance reporting

**License**: Proprietary / Commercial

**Packaging Options**:
- SaaS (managed by us, $500-2K/month per team)
- Self-hosted (customer deploys in VPC, $5K-10K/month)

**Examples of Features**:
- Create and enforce policies across team
- Approval workflows (who can approve what)
- Team collaboration (shared budget, policies)
- Cost tracking per team/project
- Usage reporting and forecasting
- Audit trail (compliance)

**Who Uses It**: Teams of 3+, mid-market companies, enterprises

**Revenue**: $500-2K/month per team (SaaS) or $5K-10K/month (self-hosted)

---

### **Enterprise Premium Features** (Proprietary, Paid Add-On)

**Purpose**: Advanced features for large enterprises (100+ employees, complex governance).

**Components**:
- Advanced policy engine (conditions, templates, inheritance)
- Advanced approval workflows (multi-level, delegation)
- Advanced RBAC (fine-grained permissions)
- SSO/SAML/OIDC integration
- Advanced analytics (ML-driven recommendations, predictive budgeting)
- Cost optimization engine (automated recommendations)
- Industry-specific compliance (HIPAA, PCI-DSS, SOC 2)
- Performance pack (distributed caching, batch optimization, latency prediction)

**License**: Proprietary / Commercial

**Packaging**: Add-on to Enterprise Control Plane ($5K-50K/month depending on feature set)

**Examples of Features**:
- Multi-level approval workflows with delegation
- Industry-specific policies (finance, healthcare)
- Predictive cost forecasting (ML-powered)
- Advanced anomaly detection
- Custom dashboard builder
- Enterprise SSO (SAML, OIDC)
- Dedicated support SLA

**Who Uses It**: Large enterprises, highly regulated industries

**Revenue**: $5K-50K/month per enterprise (tiered)

---

### **Managed SaaS** (Proprietary, Infrastructure Service)

**Purpose**: Fully managed llm-router running in our infrastructure.

**Components**:
- Everything in OSS + Control Plane + Enterprise features
- Hosted, managed, monitored by us
- Multi-tenant (isolated from other customers)
- Automatic backups, updates, scaling
- 99.9% SLA guarantee
- 24/7 support

**License**: Proprietary / Subscription

**Pricing**: Usage-based (tokens consumed, API calls, teams, etc.)

**Packaging Options**:
- Starter: $0 (free tier, 100K tokens/month)
- Professional: $500/month (1M tokens/month, 3 teams)
- Enterprise: Custom pricing ($5K-100K+/month, unlimited tokens, dedicated support)

**Examples of Features**:
- All OSS features + Control Plane + Enterprise features
- Automatic scaling (handles 10M tokens/second)
- Global CDN for low latency
- Advanced security (DDoS protection, WAF)
- Compliance certifications (SOC 2, HIPAA, PCI-DSS)
- Dedicated account manager
- Custom integrations

**Who Uses It**: Companies that want zero ops burden, don't want to self-host

**Revenue**: Usage-based, typically $1K-50K/month per customer

---

### **Internal/Private Features** (Not Public, For Now)

**Purpose**: Experimental or competitive features we're not ready to expose.

**Components**:
- Advanced workload classification (proprietary ML models)
- Advanced dedup algorithms (cross-tenant, anonymized patterns)
- Cost optimization (proprietary algorithms)
- Next-generation control plane (under development)
- Enterprise competitive intelligence (what features are most valuable)

**License**: Proprietary / Internal

**Current Status**: Research and development phase

**Future Plans**: May become Enterprise Premium or Managed SaaS features once mature

**Examples of Features**:
- ML-based workflow pattern detection (learns from all customer data)
- Cross-tenant dedup insights (anonymized patterns available to all)
- Predictive model recommendations (learns what works best for each user)
- Real-time cost optimization (automatically downshifts models to save cost)

**Who Uses It**: Internal team only (for now)

**Revenue**: Future — will be part of Enterprise or Managed SaaS

---

## First 90-Day Plan

### **Month 1 (Weeks 1-4): Architecture Finalization & Core Foundations**

**Objectives**:
1. Finalize and approve domain model and architecture.
2. Set up core infrastructure (GitHub repos, CI/CD, package publishing).
3. Start building core modules (types, workload understanding, routing policy).

**Week 1-2: Architecture Finalization**

**Deliverables**:
- [ ] Domain model document finalized and approved
- [ ] Contract specs written (OpenAPI for control plane APIs)
- [ ] Project structure finalized (repos, folders, naming)
- [ ] Ownership matrix (who owns what)
- [ ] CI/CD plan documented

**Engineering**:
- Create llm-router-core repository (public, MIT license)
- Set up GitHub Actions CI/CD
- Create project structure (folders for each module)
- Create base pyproject.toml (dependencies, test config)
- Create contributing guide

**Week 3-4: Core Module Foundation**

**Deliverables**:
- [ ] types/ module complete (all dataclasses from domain model)
- [ ] workload-understanding/ module started (heuristic classifier)
- [ ] routing-policy/ module started (policy loading, resolution)
- [ ] Tests for types module (100% coverage)

**Engineering**:
- Implement all dataclasses from domain model in types/
- Add serialization (to_dict, from_dict for JSON)
- Implement heuristic classifier (TaskType, Complexity detection)
- Implement policy loading (YAML file support)
- Write 50+ unit tests
- Publish llm-router-core v0.1.0 to PyPI

**Validation**:
- [ ] Types serialize/deserialize correctly
- [ ] Classifier accuracy > 80% on test data
- [ ] Policy loading works with sample YAML files
- [ ] All tests pass

---

### **Month 2 (Weeks 5-8): Core Module Build-Out**

**Objectives**:
1. Complete core modules (routing, quality, telemetry).
2. Publish llm-router-core v0.2.0.
3. Start refactoring current OSS package.

**Week 5-6: Routing & Quality Modules**

**Deliverables**:
- [ ] routing-engine/ module complete (scoring, decision logic)
- [ ] quality-evaluation/ module complete (heuristic checks)
- [ ] Tests for both modules (80%+ coverage)
- [ ] Integration tests (policy + routing together)

**Engineering**:
- Implement RoutingDecision logic (score models, build chains)
- Implement budget checking and enforcement
- Implement quality checks (length, format, etc.)
- Write integration tests (end-to-end scenario)
- Update docs

**Validation**:
- [ ] Routing decisions are correct (primary model, fallbacks)
- [ ] Budget limits enforced
- [ ] Quality checks identify failures
- [ ] Benchmarks: routing < 50ms, quality check < 10ms

**Week 7-8: Telemetry & Testing**

**Deliverables**:
- [ ] telemetry/ module complete (event recording, aggregation)
- [ ] Tests for telemetry module (80%+ coverage)
- [ ] Core modules published as v0.2.0
- [ ] Refactor current OSS package to use core (started)

**Engineering**:
- Implement TelemetryEvent recording (async, non-blocking)
- Implement local aggregation queries (cost sum, quality rate)
- Write performance tests (telemetry latency)
- Update CLI to use core modules
- Start removing duplicate code from OSS package

**Validation**:
- [ ] Telemetry recording is non-blocking (< 1ms overhead)
- [ ] Aggregation queries are fast (< 100ms for 10K events)
- [ ] OSS package still works with core modules
- [ ] Zero regressions in existing functionality

---

### **Month 3 (Weeks 9-12): Runtime Refactoring & First Validation**

**Objectives**:
1. Complete OSS package refactoring (use core modules).
2. Publish Python SDK (library form, separate from MCP).
3. Validate that core modules work in real usage.

**Week 9-10: OSS Refactoring**

**Deliverables**:
- [ ] Current OSS package refactored (imports from core)
- [ ] Duplicate code removed
- [ ] All tests still passing (zero regressions)
- [ ] Documentation updated

**Engineering**:
- Replace internal classification with core.classification
- Replace policy loading with core.policy
- Replace routing logic with core.routing_engine
- Remove ~2000 LOC of duplicate code
- Run full test suite (ensure no regressions)
- Update README and docs

**Validation**:
- [ ] All tests pass
- [ ] Behavioral equivalence (same outputs, different inputs)
- [ ] Performance no worse than before
- [ ] User experience unchanged

**Week 11-12: Python SDK & Validation**

**Deliverables**:
- [ ] Python SDK published (importable library)
- [ ] MCP adapter refactored (now uses SDK)
- [ ] Real-world validation (test with actual users)
- [ ] Month 1-3 retrospective

**Engineering**:
- Create llm-router package (pure library, no MCP)
- Expose main routing API: `await route(prompt, user_id, policy)`
- Publish to PyPI
- Refactor MCP server to use SDK
- Beta test with early adopters (5-10 teams)
- Collect feedback and bugs

**Validation**:
- [ ] SDK works as library (can be imported)
- [ ] MCP adapter uses SDK
- [ ] Beta users report no issues (or known issues are tracked)
- [ ] Performance benchmarks show < 2% overhead from refactoring

**Retrospective**:
- Document Month 1-3 learnings
- Review architecture (still correct? Changes needed?)
- Plan Month 4-6 more accurately based on reality
- Publish retrospective (internal)

---

## What NOT to Do

### ❌ 1. **Don't Overload the Current OSS Router with New Features**

**Mistake**: Adding control plane, governance, advanced analytics directly into the current llm-router package.

**Why It's Wrong**:
- Makes the package bloated (hundreds of MB)
- Couples free tier to commercial features
- Makes it hard to deploy in different ways (SaaS vs. self-hosted vs. local)
- Confuses the community about what the package is for

**What to Do Instead**:
- Keep OSS package minimal (routing core only)
- Control plane is a separate service
- If users want control plane, they deploy it separately (or use our SaaS)

---

### ❌ 2. **Don't Build the Control Plane UI Before Stable API Contracts**

**Mistake**: Building the web UI for policy management before the underlying API contracts are stable.

**Why It's Wrong**:
- UI assumes API structure; changing API breaks UI
- UI refactoring is expensive and time-consuming
- Dashboard becomes a bottleneck (can't start until API is ready)

**What to Do Instead**:
- Finalize API contracts in Stage 0
- Write integration tests for API (contract tests)
- Only then build UI
- Validate API through CLI or Postman before UI

---

### ❌ 3. **Don't Deploy Multi-Tenant Control Plane Before Single-Tenant Works**

**Mistake**: Building multi-tenant support as a core feature from day 1.

**Why It's Wrong**:
- Multi-tenancy adds enormous complexity (data isolation, quotas, billing)
- Single-tenant control plane is 10x simpler to build and debug
- Multi-tenant issues are catastrophic if not handled correctly

**What to Do Instead**:
- Stage 7 (Month 11-12): Build single-tenant control plane
- Stage 8+: Add multi-tenant support as a feature
- Start with assumption that each customer gets their own deployment
- Multi-tenancy is a SaaS packaging optimization, not a core requirement

---

### ❌ 4. **Don't Ship Optimization Tricks Without Measurement Harnesses**

**Mistake**: Shipping token dedup, prompt compression, or caching without before/after metrics.

**Why It's Wrong**:
- You don't know if optimizations actually work
- Aggressive optimization might reduce quality (no way to know)
- Users can't prove ROI to their managers

**What to Do Instead**:
- Stage 4: Build measurement infrastructure first (telemetry, dashboards)
- Measure baseline token consumption (no optimization)
- Implement optimization, measure again
- Only ship if savings are real and quality is maintained
- Publish metrics with every release ("20% token savings")

---

### ❌ 5. **Don't Mix Approval/Audit Logic into Local Config Systems**

**Mistake**: Adding approval gates and audit trails to local YAML files.

**Why It's Wrong**:
- YAML files don't scale to complex workflows (if A approves, then do X; if B denies, do Y)
- No audit trail (YAML changes aren't tracked automatically)
- No versioning or rollback
- Can't handle complex org structures (who approves what is complex)

**What to Do Instead**:
- OSS (local): No approvals (trust user's local config)
- Enterprise: Approval workflows in control plane (Stage 6)
- If local users want approvals, they use control plane (SaaS or self-hosted)

---

### ❌ 6. **Don't Assume Everything Should Be Open-Source**

**Mistake**: Publishing all code (control plane, advanced analytics, enterprise policies) as open-source.

**Why It's Wrong**:
- No sustainable business model
- Competitors get everything for free
- Enterprise features are copied overnight
- Community doesn't want to maintain production infrastructure

**What to Do Instead**:
- OSS: Core routing logic, SDK, MCP adapter (MIT license)
- Proprietary: Control plane, advanced features, SaaS
- Hybrid: Some control plane components could be open-source (with commercial SaaS offering premium features)

---

### ❌ 7. **Don't Build for "Enterprise Ready" Without the Basics**

**Mistake**: Building advanced features (ML-driven routing, predictive budgeting, automated cost optimization) before basic deployment/governance/security works.

**Why It's Wrong**:
- Enterprise customers care about operations and security first
- Advanced features are nice-to-have
- Enterprises won't buy a product that can't be deployed securely

**What to Do Instead**:
- Stage 7: MVP control plane (policies, basic governance, deployment)
- Stage 8: Self-hosted deployment (Kubernetes, Terraform)
- Enterprise Premium: Advanced features (once deployment is solid)

---

### ❌ 8. **Don't Couple Multiple Runtimes if You Haven't Built the First**

**Mistake**: Designing for "Python SDK, Go CLI, JS library, Rust module" before completing the Python SDK.

**Why It's Wrong**:
- Overgeneral design (trying to support 10 use cases, delivers 3)
- More abstractions = more complexity
- Can't validate that design works until you build at least one runtime

**What to Do Instead**:
- Stage 2: Build and validate Python SDK
- Stage 3-6: Improve Python SDK based on real usage
- Later: Build second runtime (Go CLI or JS SDK)
- Let design emerge from first two implementations

---

### ❌ 9. **Don't Assume Policy Compliance Without Audit**

**Mistake**: Building policy enforcement without audit trail.

**Why It's Wrong**:
- No way to prove compliance ("we enforce model X", but did we really?)
- Audits fail because you can't produce evidence
- Regulatory bodies want immutable audit trails

**What to Do Instead**:
- Build audit logging as a core requirement (not optional)
- Audit trail must be immutable (append-only, signed)
- Policy enforcement and audit together, not separate
- Test audit capabilities from day 1

---

### ❌ 10. **Don't Gold-Plate the First Release**

**Mistake**: Perfecting every detail before shipping anything.

**Why It's Wrong**:
- You won't ship anything for 12+ months
- Real users will reveal requirements you didn't anticipate
- Perfect becomes enemy of good

**What to Do Instead**:
- Month 1-3: Ship core (routing, classification, basic policies)
- Month 4-6: Get real users and feedback
- Month 7-12: Expand features based on actual usage patterns
- Accept that v1 is incomplete; v2 will be better

---

### ❌ 11. **Don't Build Control Plane Without First Runtime Usage**

**Mistake**: Designing the control plane before understanding how runtimes actually use routing.

**Why It's Wrong**:
- API design assumptions will be wrong
- You'll build features nobody wants
- Runtimes can't give meaningful feedback to control plane

**What to Do Instead**:
- Stage 2 (Month 7-12): Get Python SDK + MCP adapter in real use
- Collect telemetry from real users (what decisions do they make?)
- Document actual usage patterns
- Stage 7 (Month 11-12): Design control plane APIs based on real patterns
- Control plane serves observed needs, not imagined ones

---

### ❌ 12. **Don't Forget About Performance Until "Later"**

**Mistake**: Building features without measuring latency/throughput, then optimizing at the end.

**Why It's Wrong**:
- Optimization at the end is a painful rewrite
- Users already adopted slow version
- Features might not be optimizable (bad architecture)

**What to Do Instead**:
- Measure performance from Stage 1 (classification < 100ms, routing < 50ms)
- Latency budgets per component
- Regular benchmarks (weekly)
- Don't ship feature if it violates latency budget

---

## Final Deliverables

### **A. Architectural Principle Summary**

The llm-router platform is being rebuilt as a set of decoupled, domain-specific modules with clear contracts between them. The current OSS package becomes ONE runtime adapter, not the center of the architecture. This preserves optionality for future editions (SaaS, enterprise self-hosted, managed service, library integrations) without being locked into the current design.

**Key Principles**:
1. Separation of Concerns: Each domain is independent (workload understanding, routing policy, routing decision, quality, governance, telemetry, etc.)
2. Contract-First: All communication is via well-defined, versioned contracts (dataclasses, APIs, interfaces)
3. Multiple Runtimes: Python SDK, MCP adapter, Go CLI, JS SDK, framework integrations all use the same core logic
4. Progressive Disclosure: Free users get local routing; paid tiers unlock central policies, governance, advanced analytics
5. Business Optionality: Every capability can be deployed as local-only, hybrid, or fully managed without redesign

---

### **B. Modular Capability Map**

| Domain | Purpose | Modularity | Open/Proprietary |
|--------|---------|-----------|-----------------|
| Workload Understanding | Classify task type/complexity | Independent classifier logic | Open |
| Routing Policy | Define routing rules per org/team | Independent from routing decision | Proprietary (enterprise) |
| Routing Decision Engine | Compute optimal model | Core routing logic | Open |
| Workflow Orchestration | Multi-step task routing | Independent workflow engine | Proprietary (enterprise) |
| Token Optimization | Reduce token consumption | Independent optimization algorithms | Proprietary (advanced) |
| Quality & Escalation | Ensure quality standards | Independent evaluation + escalation | Proprietary (enterprise) |
| Telemetry & Analytics | Collect and analyze data | Independent recording + aggregation | Proprietary (SaaS) |
| Governance & Approval | Enforce policies, audit | Independent approval + audit workflows | Proprietary (enterprise) |
| Runtime Integration | Execute routing decisions | Pluggable runtime adapters | Open (SDK), Proprietary (SaaS) |
| Enterprise Control Plane | Central management service | Independent service | Proprietary (enterprise) |

---

### **C. Shared Domain Model**

Defined above in detail (WorkloadProfile, RoutingPolicy, RoutingDecision, LLMResponse, QualityVerdict, WorkflowExecutionPlan, OptimizationPlan, ApprovalRequest, AuditEvent, TelemetryEvent, and supporting objects).

All modules communicate via these frozen dataclasses. No implementation coupling.

---

### **D. Package/Module/Service Boundary Proposal**

Detailed above under "Package & Service Boundaries" section.

```
llm-router-core/          (shared libraries, open-source)
  ├── workload-understanding/
  ├── routing-policy/
  ├── routing-engine/
  ├── workflow-orchestration/
  ├── token-optimization/
  ├── quality-evaluation/
  ├── telemetry/
  ├── governance/
  ├── runtime-adapter/ (interface)
  └── types/

llm-router-runtimes/      (multiple implementations)
  ├── python-sdk/        (library)
  ├── cli-binary/        (Go CLI, future)
  ├── js-sdk/           (JavaScript, future)
  └── adapters/         (LangChain, LlamaIndex, LiteLLM, etc.)

llm-router-control-plane/ (proprietary service)
  ├── api-server/
  ├── policy-engine/
  ├── approval-workflow/
  ├── analytics-engine/
  ├── auth-and-rbac/
  ├── database/
  └── dashboard/

llm-router-enterprise/    (proprietary features)
  ├── advanced-policy-engine/
  ├── compliance-framework/
  ├── sso-and-provisioning/
  ├── advanced-analytics/
  ├── performance-pack/
  └── marketplace/

llm-router-managed/       (SaaS packaging)
  ├── helm-charts/
  ├── terraform/
  ├── multi-tenant-isolation/
  ├── billing-and-metering/
  └── support-and-ops/
```

---

### **E. Role of Current OSS Package in Future Architecture**

The current `/src/llm_router/` package evolves into:

1. **Reference Implementation**: Shows how to build a runtime using llm-router-core
2. **Python SDK + MCP Adapter**: The SDK can be imported as a library; the MCP adapter is one way to use it
3. **Local Runtime**: Executes routing decisions locally (no control plane required)

**What Gets Extracted to llm-router-core**:
- Classification logic (classifier.py)
- Policy loading and resolution (policy.py)
- Routing decision logic (router.py, model_selector.py, scorer.py, chain_builder.py)
- Workflow orchestration (orchestrator.py)
- Token optimization (semantic_cache.py, prompt_cache.py, compaction.py)
- Quality evaluation (judge.py)
- Telemetry (cost.py, model_tracking.py, quality.py)

**What Stays in OSS Package**:
- MCP server scaffolding (server.py)
- MCP tool handlers (tools/)
- CLI commands (cli.py)
- Local hook execution (hooks/)
- Provider integrations (codex_agent.py, gemini_cli_agent.py)
- Local configuration management

**Benefit**: OSS package is ~30% smaller, easier to understand, and focused on one thing (being a good MCP server and CLI tool).

---

### **F. Detailed Staged Execution Plan**

Covered extensively above in "Detailed Execution Plan" section.

**Summary of 8 Stages**:
1. **Stage 0 (Weeks 1-2)**: Architecture finalization and planning
2. **Stage 1 (Weeks 3-6)**: Core modules (llm-router-core library)
3. **Stage 2 (Weeks 7-12)**: Runtime decoupling (refactor OSS to use core)
4. **Stage 3 (Weeks 13-20)**: Workflow routing and multi-step tasks
5. **Stage 4 (Weeks 21-28)**: Token optimization engine
6. **Stage 5 (Weeks 29-36)**: Quality and escalation framework
7. **Stage 6 (Weeks 37-44)**: Governance and approval gates
8. **Stage 7 (Weeks 45-52)**: Enterprise control plane MVP
9. **Stage 8 (Weeks 53-60)**: Enterprise packaging and self-hosting

---

### **G. Cross-Cutting Workstreams**

10 persistent workstreams running in parallel:
1. Domain Model & Contracts (ongoing refinement)
2. Workload Intelligence (continuous classification improvement)
3. Policy & Governance (enterprise design)
4. Decision Intelligence (data-driven routing)
5. Workflow Orchestration (multi-step optimization)
6. Token Optimization (every % counts)
7. Quality & Trust (hallucination detection, escalation)
8. Telemetry & Analytics (data pipeline, dashboards)
9. Runtime Ecosystem (multiple languages/platforms)
10. Enterprise Packaging (Kubernetes, Terraform, multi-tenant)

---

### **H. Dependency & Sequencing Map**

**Critical Path**: Stage 0 → Stage 1 → Stage 2 → [Stages 3-6 in parallel] → Stage 7 → Stage 8

**Duration**: 60 weeks (~14 months)

**Parallelization**: Workstreams run in parallel throughout; stages overlap where possible.

**Key Dependencies**:
- Architecture (Stage 0) → Everything else
- Core modules (Stage 1) → Runtime refactoring (Stage 2)
- Runtime refactoring (Stage 2) → Control plane (Stage 7)
- Control plane (Stage 7) → Enterprise packaging (Stage 8)

---

### **I. OSS vs Enterprise vs Managed vs Private Classification**

| Feature | OSS | Enterprise | Managed SaaS | Private (R&D) |
|---------|-----|-----------|-------------|---|
| Classification | ✓ Free | ✓ Core (enterprise) | ✓ Included | ✓ Advanced ML |
| Routing decisions | ✓ Free | ✓ Core (enterprise) | ✓ Included | ✓ Predictive |
| Local caching | ✓ Free | ✓ Core | ✓ Included + distributed Redis | ✓ Global dedup |
| Quality evaluation | ✓ Heuristic | ✓ + LLM judge | ✓ + LLM judge | ✓ Advanced LLM |
| Telemetry | ✓ Local | ✓ Local + cloud | ✓ Cloud + multi-tenant | ✓ Advanced analytics |
| Policies | ✓ Local YAML | ✓ Control plane | ✓ Control plane | ✓ Conditional, templated |
| Approvals | ✗ No | ✓ Enterprise | ✓ Enterprise | ✓ Advanced workflows |
| RBAC | ✗ No | ✓ Enterprise | ✓ Enterprise | ✓ Fine-grained |
| Analytics | ✓ Basic | ✓ Enterprise | ✓ Enterprise | ✓ ML-driven |
| SLA | ✓ None | ✓ Available (self-host) | ✓ 99.9% | ✓ 99.99% |
| Cost | $0 | $500-10K/mo | $1K-50K/mo | TBD |

---

### **J. First 90-Day Plan**

**Month 1 (Weeks 1-4): Architecture Finalization & Core Foundations**
- Finalize domain model and contracts
- Set up llm-router-core repository
- Start implementing core modules (types, classification, policy)

**Month 2 (Weeks 5-8): Core Module Build-Out**
- Complete routing, quality, telemetry modules
- Publish llm-router-core v0.2.0
- Start refactoring OSS package to use core

**Month 3 (Weeks 9-12): Runtime Refactoring & First Validation**
- Complete OSS refactoring (zero regressions)
- Publish Python SDK
- Beta test with 5-10 early adopters
- Collect feedback and bugs

**Deliverables by End of Month 3**:
- [ ] llm-router-core v0.2.0 published (PyPI)
- [ ] Python SDK published (importable library)
- [ ] OSS package refactored (uses core, no duplicate code)
- [ ] Zero regressions (all tests pass)
- [ ] Beta validation (real users testing)
- [ ] Month 1-3 retrospective and plan updates

**Success Criteria**:
- Core modules handle 80%+ of routing use cases
- OSS package 30% smaller (code moved to core)
- Python SDK usable as standalone library
- Beta users report no critical issues
- Performance within acceptable bounds (< 2% overhead)

---

### **K. What NOT to Do**

1. **Don't overload the OSS router** with control plane features
2. **Don't build UI before stable APIs**
3. **Don't deploy multi-tenant before single-tenant works**
4. **Don't ship optimizations without measurement**
5. **Don't mix approval/audit into local config**
6. **Don't assume everything is open-source**
7. **Don't build "enterprise ready" without basics**
8. **Don't couple multiple runtimes before first one works**
9. **Don't assume compliance without audit trails**
10. **Don't gold-plate the first release**
11. **Don't build control plane before runtime usage data**
12. **Don't forget performance until "later"**

---

## Conclusion

This modular platform architecture enables llm-router to grow from a single-router tool into a comprehensive LLM orchestration platform serving:

- **Free developers**: OSS router with local policies and caching
- **Teams**: Shared policies, approval workflows, analytics (SaaS control plane)
- **Enterprises**: Advanced governance, compliance, SSO, self-hosting (proprietary control plane)
- **AI platforms**: Embedded routing engine (library integration)

The current OSS package becomes the Python runtime adapter — important, but not the center of the business. Future runtimes, control planes, and commercial editions are built on the same core modules, preserving optionality and enabling multiple revenue streams.

**This is a 14-month journey (60 weeks). It's ambitious but achievable with disciplined execution, clear ownership, and willingness to defer advanced features until foundations are solid.**

---

**Document Ends**
