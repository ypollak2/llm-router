# Language-Agnostic Core Architecture

**STATUS**: Design Ready  
**TARGET**: Phase 1 Foundation (Weeks 1–2)  
**APPROACH**: Thin wrapper (gRPC gateway) around existing Python router for Phase 1; full rewrite in Phase 2  

---

## Core Principle

> **Core logic is not a library. Core is a service.**

The fundamental fix: instead of `llm_router_core` as a Python library that other runtimes must import/bind to, the core is a **gRPC microservice** that **all runtimes** (Python, Go, JavaScript, Rust) communicate with via **standardized service contracts** (protobuf).

This means:
- Core logic can be implemented in ANY language (Python, Go, Rust, Java)
- SDKs are thin clients, not wrappers
- Runtimes are truly decoupled (no shared dependencies)
- Easy to scale, monitor, and replace core independently
- Path to multi-language implementations (e.g., Python core for v1, Go rewrite for v2)

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        gRPC Service Layer                       │
│                    (Language-Agnostic Contracts)                │
├─────────────────────────────────────────────────────────────────┤
│  WorkloadUnderstanding | RoutingPolicy | RoutingDecisionEngine  │
└─────────────────────────────────────────────────────────────────┘
                              ▲
         ┌────────────────────┼────────────────────┐
         │                    │                    │
    ┌─────────┐          ┌─────────┐          ┌─────────┐
    │ Python  │          │   Go    │          │JavaScr. │
    │  SDK    │          │  CLI    │          │  SDK    │
    │(FastAPI)│          │  (gRPC) │          │ (Node)  │
    └─────────┘          └─────────┘          └─────────┘

Core Service (Phase 1: Python + FastAPI gRPC gateway)
Core Service (Phase 2: Go rewrite with gRPC + REST)
```

---

## Service Definitions (Protobuf v3)

### 1. WorkloadUnderstanding Service

**Purpose**: Classify incoming tasks by complexity.

```protobuf
syntax = "proto3";

package llmrouter.v1;

import "google/protobuf/timestamp.proto";

// WorkloadUnderstanding service: classify tasks by complexity
service WorkloadUnderstanding {
  rpc Classify(ClassifyRequest) returns (ClassifyResponse);
  rpc GetClassifier(GetClassifierRequest) returns (ClassifierMetadata);
}

// Request: analyze a task string and return complexity classification
message ClassifyRequest {
  string task_description = 1;          // "Write a blog post about Rust"
  optional string context = 2;          // Additional context
  optional string language = 3;         // Hint: "python", "typescript", etc.
  string request_id = 4;                // Idempotency key
  google.protobuf.Timestamp timestamp = 5;
}

// Response: task complexity classification
message ClassifyResponse {
  string request_id = 1;                // Echo request_id
  Complexity complexity = 2;            // SIMPLE | MODERATE | COMPLEX
  float confidence = 3;                 // 0.0–1.0, higher = more confident
  ClassificationMethod method = 4;      // HEURISTIC | OLLAMA | API | FALLBACK
  string reasoning = 5;                 // Debug info: why this classification
  uint32 latency_ms = 6;                // How long did classification take
  string model_used = 7;                // Which model classified (for metrics)
}

enum Complexity {
  COMPLEXITY_UNSPECIFIED = 0;
  SIMPLE = 1;
  MODERATE = 2;
  COMPLEX = 3;
}

enum ClassificationMethod {
  METHOD_UNSPECIFIED = 0;
  HEURISTIC = 1;        // Fast pattern matching
  OLLAMA = 2;           // Local LLM (qwen2.5)
  API = 3;              // Gemini Flash / GPT-4o-mini
  FALLBACK = 4;         // Default when others fail
}

message GetClassifierRequest {
  // Retrieve metadata about the current classifier
}

message ClassifierMetadata {
  string version = 1;
  repeated string available_methods = 2;
  string ollama_status = 3;  // "healthy" | "unavailable"
  map<string, string> method_config = 4;
}
```

---

### 2. RoutingPolicy Service

**Purpose**: Define rules that map complexity → models.

```protobuf
syntax = "proto3";

package llmrouter.v1;

import "google/protobuf/timestamp.proto";

// RoutingPolicy service: define and retrieve routing rules
service RoutingPolicy {
  rpc DefinePolicy(DefinePolicyRequest) returns (PolicyResponse);
  rpc GetPolicy(GetPolicyRequest) returns (Policy);
  rpc UpdatePolicy(UpdatePolicyRequest) returns (PolicyResponse);
  rpc ListPolicies(ListPoliciesRequest) returns (ListPoliciesResponse);
}

// Define a new routing policy
message DefinePolicyRequest {
  string policy_name = 1;     // "routing-policy-prod-v1"
  string description = 2;
  repeated Rule rules = 3;    // Complexity → Model mapping
  bool is_default = 4;        // Is this the default policy?
  google.protobuf.Timestamp created_at = 5;
}

// A single routing rule: complexity → model list
message Rule {
  Complexity applicable_complexity = 1;  // SIMPLE | MODERATE | COMPLEX
  repeated ModelOption models = 2;       // Ordered list of fallback models
  string description = 3;                // "For simple tasks, use budget models"
}

// A model option with priority and constraints
message ModelOption {
  string model_name = 1;         // "gpt-4o-mini", "gemini-flash", "ollama/qwen"
  uint32 priority = 2;           // 1 = first try, 2 = fallback, etc.
  ModelConstraints constraints = 3;
  uint32 estimated_cost_cents = 4;  // Cost per 1K input tokens
  float estimated_latency_ms = 5;   // Typical response time
}

// Constraints on when a model can be selected
message ModelConstraints {
  bool requires_api_key = 1;
  bool requires_internet = 2;
  repeated string compatible_runtimes = 3;  // "python", "go", "javascript"
  string budget_tier = 4;                   // "budget" | "balanced" | "premium"
}

// Retrieve a policy by name
message GetPolicyRequest {
  string policy_name = 1;
}

message Policy {
  string policy_name = 1;
  string description = 2;
  repeated Rule rules = 3;
  google.protobuf.Timestamp created_at = 4;
  google.protobuf.Timestamp updated_at = 5;
  string created_by = 6;
  bool is_active = 7;
}

// Update an existing policy
message UpdatePolicyRequest {
  string policy_name = 1;
  optional string description = 2;
  optional repeated Rule rules = 3;
  optional bool is_default = 4;
}

message PolicyResponse {
  bool success = 1;
  string message = 2;
  string policy_name = 3;
  google.protobuf.Timestamp timestamp = 4;
}

// List all available policies
message ListPoliciesRequest {
  bool active_only = 1;
}

message ListPoliciesResponse {
  repeated Policy policies = 1;
  uint32 total_count = 2;
}
```

---

### 3. RoutingDecisionEngine Service

**Purpose**: Evaluate context and make model selection decision.

```protobuf
syntax = "proto3";

package llmrouter.v1;

import "google/protobuf/timestamp.proto";

// RoutingDecisionEngine service: make optimal model selection
service RoutingDecisionEngine {
  rpc SelectModel(SelectModelRequest) returns (SelectModelResponse);
  rpc EvaluateDecision(EvaluateDecisionRequest) returns (EvaluationResponse);
  rpc GetDecisionHistory(GetHistoryRequest) returns (DecisionHistory);
}

// Request: select the best model for a task
message SelectModelRequest {
  string request_id = 1;                    // Idempotency key
  Complexity complexity = 2;                // Pre-classified
  string policy_name = 3;                   // Which policy to apply
  BudgetContext budget = 4;                 // Current budget pressure
  PerformanceContext performance = 5;       // Latency/quality preferences
  google.protobuf.Timestamp timestamp = 6;
}

// Budget context: how much quota/money is available?
message BudgetContext {
  string budget_tier = 1;                   // "budget" | "balanced" | "premium"
  float remaining_monthly_budget_usd = 2;   // For API-based models
  float session_usage_percent = 3;          // Claude subscription: 0–100
  repeated ProviderQuota provider_quotas = 4;
}

message ProviderQuota {
  string provider = 1;                      // "openai" | "gemini" | "claude" | "ollama"
  float used_percent = 2;                   // 0–100
  string status = 3;                        // "healthy" | "warning" | "exhausted"
}

// Performance context: what are the requirements?
message PerformanceContext {
  uint32 max_latency_ms = 1;               // Timeout constraint
  string priority = 2;                      // "speed" | "cost" | "quality" | "balanced"
  optional string preferred_provider = 3;   // Hint, not requirement
  bool allow_fallback_chain = 4;            // Try multiple models if first fails?
}

// Response: selected model and fallback chain
message SelectModelResponse {
  string request_id = 1;
  string selected_model = 2;               // "gpt-4o-mini"
  repeated string fallback_chain = 3;      // ["gemini-flash", "ollama/qwen"]
  string selection_rationale = 4;          // Why this model?
  float confidence = 5;                    // 0–1, how confident in selection?
  uint32 latency_ms = 6;
  SelectionMethod method = 7;              // How was it selected?
  ModelMetadata selected_metadata = 8;     // Details about selected model
}

message ModelMetadata {
  string model_name = 1;
  string provider = 2;
  uint32 estimated_cost_cents = 3;        // Cost for typical task
  uint32 typical_latency_ms = 4;
  string availability = 5;                 // "available" | "degraded" | "unavailable"
  string reasoning = 6;                    // Why this model matches the task
}

enum SelectionMethod {
  METHOD_UNSPECIFIED = 0;
  POLICY_MATCH = 1;         // Matched policy rules
  BUDGET_AWARE = 2;         // Adjusted for budget pressure
  PERFORMANCE_AWARE = 3;    // Adjusted for latency requirement
  FALLBACK_CHAIN = 4;       // Using fallback after primary failed
}

// Evaluate a routing decision after seeing results
message EvaluateDecisionRequest {
  string request_id = 1;
  string selected_model = 2;
  bool succeeded = 3;
  uint32 actual_latency_ms = 4;
  uint32 actual_cost_cents = 5;
  float quality_score = 6;                 // 0–1, how good was the response?
  string feedback = 7;                     // User feedback (optional)
}

message EvaluationResponse {
  string request_id = 1;
  bool recorded = 2;
  string message = 3;
}

// Retrieve decision history for analysis
message GetHistoryRequest {
  uint32 limit = 1;                        // How many decisions to return
  optional string policy_name = 2;         // Filter by policy
  google.protobuf.Timestamp from_time = 3; // Earliest decision
  google.protobuf.Timestamp to_time = 4;   // Latest decision
}

message DecisionHistory {
  repeated DecisionRecord records = 1;
  uint32 total_count = 2;
}

message DecisionRecord {
  string request_id = 1;
  Complexity complexity = 2;
  string selected_model = 3;
  string selection_method = 4;
  bool succeeded = 5;
  uint32 latency_ms = 6;
  uint32 cost_cents = 7;
  float quality_score = 8;
  google.protobuf.Timestamp timestamp = 9;
}
```

---

## Shared Domain Models (Protobuf)

```protobuf
syntax = "proto3";

package llmrouter.v1;

// Re-export Complexity enum for use across services
enum Complexity {
  COMPLEXITY_UNSPECIFIED = 0;
  SIMPLE = 1;
  MODERATE = 2;
  COMPLEX = 3;
}

// Model information (used by multiple services)
message LLMModel {
  string identifier = 1;           // "gpt-4o-mini"
  string provider = 2;             // "openai" | "gemini" | "anthropic" | "ollama"
  string display_name = 3;
  uint32 input_token_cost_cents = 4;     // Per 1K tokens
  uint32 output_token_cost_cents = 5;
  string status = 6;               // "available" | "degraded" | "unavailable"
  repeated string capabilities = 7;      // "reasoning" | "vision" | "function-calling"
}
```

---

## Service Deployment Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                     gRPC Service (Port 50051)                │
│                   (Python FastAPI + grpc-python)             │
│                                                              │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ API Layer (gRPC-Python server)                      │    │
│  │  - WorkloadUnderstanding.Classify()                 │    │
│  │  - RoutingPolicy.DefinePolicy()                     │    │
│  │  - RoutingDecisionEngine.SelectModel()              │    │
│  └─────────────────────────────────────────────────────┘    │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ Business Logic (existing llm_router Python code)    │    │
│  │  - Complexity classifier                            │    │
│  │  - Policy engine                                    │    │
│  │  - Model selector with fallback chains              │    │
│  │  - Budget pressure calculator                       │    │
│  └─────────────────────────────────────────────────────┘    │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ Data Layer                                          │    │
│  │  - SQLite (local: policies, decision history)       │    │
│  │  - In-memory cache (model metadata, classification) │    │
│  │  - Provider APIs (OpenAI, Gemini, Anthropic, Ollama)│    │
│  └─────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────┘
```

---

## SDK Architecture (How Runtimes Connect)

### Python SDK

```python
import llmrouter

# Initialize client pointing to gRPC service
client = llmrouter.Client(
    service_url="localhost:50051",  # Or production URL
    credentials=None  # mTLS cert optional
)

# Classify a task
classification = await client.classify(
    task="Write a blog post about AI safety"
)
print(f"Complexity: {classification.complexity}")

# Get active routing policy
policy = await client.get_policy("routing-policy-prod-v1")

# Select a model
decision = await client.select_model(
    complexity=classification.complexity,
    policy_name="routing-policy-prod-v1",
    budget=Budget(tier="balanced"),
)
print(f"Selected model: {decision.selected_model}")

# Evaluate the decision after use
await client.evaluate_decision(
    request_id=decision.request_id,
    model=decision.selected_model,
    succeeded=True,
    latency_ms=250,
    quality_score=0.95,
)
```

### Go CLI

```go
import "github.com/llm-router/go-sdk/v1"

client := llmrouter.NewClient("localhost:50051")

classification, err := client.Classify(ctx, &llmrouter.ClassifyRequest{
    TaskDescription: "Write a blog post about AI safety",
})

decision, err := client.SelectModel(ctx, &llmrouter.SelectModelRequest{
    Complexity: classification.Complexity,
    PolicyName: "routing-policy-prod-v1",
    Budget: &llmrouter.BudgetContext{BudgetTier: "balanced"},
})

fmt.Printf("Selected: %s\n", decision.SelectedModel)
```

### JavaScript SDK

```typescript
import { LLMRouter } from '@llm-router/js-sdk';

const client = new LLMRouter({
  serviceUrl: 'localhost:50051'
});

const classification = await client.classify({
  taskDescription: 'Write a blog post about AI safety'
});

const decision = await client.selectModel({
  complexity: classification.complexity,
  policyName: 'routing-policy-prod-v1',
  budget: { budgetTier: 'balanced' }
});

console.log(`Selected: ${decision.selectedModel}`);
```

---

## Deployment Scenarios

### Scenario 1: Local Development
- gRPC service runs on localhost:50051
- SDKs point to localhost:50051
- SQLite database stores policies locally
- Ideal for: Single developer, testing

### Scenario 2: Self-Hosted (Single Instance)
- gRPC service runs in Docker container
- Kubernetes deployment (optional)
- SQLite for small deployments, PostgreSQL for larger
- Policies stored in database
- Ideal for: Small teams, on-premise deployments

### Scenario 3: Self-Hosted (Replicated)
- gRPC service load-balanced across 2–3 instances
- Shared PostgreSQL database
- Redis cache for classification results
- Prometheus metrics
- Ideal for: Medium organizations, HA required

### Scenario 4: Managed (SaaS) — Phase 2
- Multi-tenant gRPC service
- Dedicated tenant databases
- Central PostgreSQL + Redis
- RBAC, audit logging, compliance
- Ideal for: Enterprise, policy-driven orgs

---

## Contract Stability & Versioning

**Protobuf Versioning**:
- Current: `v1` (llmrouter.v1 package)
- Reserved future: `v2` (new capabilities without breaking v1)
- Compatibility: Services maintain backward compatibility within major version

**API Stability Guarantee**:
- Added fields are optional with defaults (never breaking)
- New services can be added (existing clients unaffected)
- Removal only in major version bumps (v1 → v2)

---

## Testing Strategy

### Unit Tests (Per Service)
- Classify: 20+ test cases (heuristic, Ollama, API, fallback)
- RoutingPolicy: 15+ test cases (define, update, list, delete)
- SelectModel: 25+ test cases (budget pressure, latency, fallback)

### Integration Tests
- Multi-service workflows: classify → get policy → select model
- gRPC gateway to Python backend
- All 3 SDKs (Python, Go, JavaScript) against gRPC service

### Load Tests
- 1000 req/sec sustained
- Latency < 500ms p99
- Memory stable (no leaks)
- Connection pool management

---

## Migration Path (Python-Only → Multi-Language)

### Phase 1 (Weeks 1–12)
- gRPC gateway wraps existing Python router
- All business logic still in Python
- Python SDK available
- Go/JavaScript SDK stubs (types only)

### Phase 2 (Months 4–8)
- Evaluate: rewrite core in Go or keep Python?
- If Go: Go core gRPC service (functionally identical to Python version)
- SDKs tested against both Python and Go backends

### Phase 3 (Post-GA)
- Customers choose: Python core (flexible, slower) or Go core (fast, strict)
- Both supported simultaneously
- Planned deprecation of Python core after 6 months

---

## Conclusion

This architecture achieves:
✅ **True modularity**: Core is a service, not a library  
✅ **Language-agnostic**: Runtimes talk via gRPC, not imports  
✅ **Decoupled**: No shared dependencies between SDKs  
✅ **Scalable**: Easy to replicate, monitor, replace core  
✅ **Future-proof**: Supports Python → Go migration without SDK changes  
✅ **Production-ready**: gRPC is industry-standard, protobuf is battle-tested  

Next steps: Build protobuf definitions, create gRPC service skeleton, start Phase 1 Week 1.
