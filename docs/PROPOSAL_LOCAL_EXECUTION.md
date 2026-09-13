**Proposal: enforce local task execution through a dedicated MCP service, with Claude’s native execution tools continuously unavailable.**

Use `PreToolUse` as a guard and diagnostic surface. Move multi-step execution into a persistent local service. Claude may submit a task and summarize its result; the local model chooses investigation steps and generates changes, while deterministic code performs filesystem and process operations.

This enforces **local ownership of execution**, not zero Claude tokens. Eliminating Claude’s turn entirely requires a local CLI/frontend using the same service.

Read-only review: no files modified.

**1. What the hook surface actually permits**

| Hook | Can do | Cannot do |
|---|---|---|
| `UserPromptSubmit` | Add context or reject submission. | Replace individual tool executions. |
| `PreToolUse` | Deny execution with a reason; rewrite arguments; add context. | Return a documented successful synthetic tool result. |
| `PostToolUse` | Replace visible output using currently documented `updatedToolOutput`; legacy `updatedMCPToolOutput` covers MCP. | Prevent or undo execution. |
| `Stop` | Prevent stopping and request continuation. | Replace completed operations. |
| `SessionStart` | Initialize session context/environment. | Substitute a pending tool result. |
| `SessionEnd` | Cleanup and accounting. | Substitute execution or results. |

These are the current documented contracts. `updatedInput` changes arguments to the existing tool; it does not select another executor. [Claude Code hook reference](https://code.claude.com/docs/en/hooks)

**Can deny-with-output work? Yes as substitute text; no as a normal successful tool-result API.**

This checkout already implements that distinction. [`tool_intercept.py:9`](src/llm_router/hooks/tool_intercept.py:9) reports an earlier live verification: the native call was prevented and substitute text reached Claude, **wrapped as an error**. [`deny_payload():250`](src/llm_router/hooks/tool_intercept.py:250) carries the answer in `permissionDecisionReason`.

Therefore, the hook can execute an operation itself and deliver its answer through denial feedback. Whether Claude accepts that answer or retries remains model behavior.

The current documentation’s `PostToolUse.updatedToolOutput` support differs from the older claim in this module’s opening comment. Installed-version support remains unverified; output replacement still occurs after execution. [PostToolUse contract](https://code.claude.com/docs/en/hooks#posttooluse-decision-control)

**Required experiment before implementation:** on the installed Claude version, use a disposable fixture and capture the transcript/stream output. Intercept Read, Grep, Write, Edit and Bash; execute each through a test executor; return a unique denial marker. Assert native execution never occurs, inspect the resulting `tool_use_id`, content and error status, and measure retries. Separately probe schema-correct `updatedToolOutput`. Repeat with parallel calls, duplicate delivery, hook failure and timeout. This proposal does not assume those experiments have passed.

**2. Candidate architectures and actual coverage**

“Covered” below means supported operations execute through the local executor. It does not imply complete compatibility with every Claude tool option.

| Architecture | Read | Write/edit | Search/list | `run_command` | What it accomplishes |
|---|---|---|---|---|---|
| **A. PreToolUse substitution** | Deterministic reads; optional local interpretation | Possible with explicit authorization, transactional execution and deduplication | Deterministic adapters | Restricted commands; arbitrary shell requires a separate sandbox | Changes executor/result delivery for individual calls. Claude still chooses every call. |
| **B. Whole-task handoff** | Local loop owns reads | Local model generates changes; executor applies permitted changes | Local loop investigates | Sandboxed command adapter | Removes intermediate operations from Claude’s reasoning loop. Requires durable execution outside the prompt hook. |
| **C. Dedicated local MCP process** | Via primitive tools or internal task operations | Same | Same | Same | Provides a real tool response and process lifetime independent of hooks. Primitive-by-primitive delegation still retains Claude’s loop; a task interface implements B. |

**A is useful as a compatibility optimization, not the primary enforcement architecture.** Current interception covers raster-image Read and selected Bash calls, not general Read/Edit/Write/Grep. Its Bash implementation executes first, then falls through on nonzero exit, short output or ineffective compression. That can cause the command to execute again natively. See [`tool_intercept.py:318`](src/llm_router/hooks/tool_intercept.py:318). Never extend that behavior to mutations.

Executing inside a hook also assumes responsibility for authorization. Matching hooks run in parallel, so another hook’s denial does not stop an executor hook’s side effects. Hook errors can allow the original action to proceed. [Hook behavior and limitations](https://code.claude.com/docs/en/hooks-guide)

**B needs a new lifetime, not merely a higher timeout.** The current prompt branch invokes `execute_agent` at [`auto-route.py:3867`](src/llm_router/hooks/auto-route.py:3867), with the default 90-second budget at [`auto-route.py:2381`](src/llm_router/hooks/auto-route.py:2381). Its default rendering remains advisory at [`auto-route.py:2731`](src/llm_router/hooks/auto-route.py:2731). Extending that synchronous draft computation does not establish task ownership.

**Recommend B delivered through C.** Expose task submission, bounded waiting/result retrieval and cancellation. Keep filesystem primitives internal to the service in this mode.

All three need explicit adapters for notebooks, binary/media operations, browser automation and other integrations. Unsupported operations must return `blocked`; they must not fall through to native tools. The existing local `run_command` deliberately supports one argv invocation, not shell pipelines or redirects. [`agent_loop.py:258`](src/llm_router/hooks/agent_loop.py:258)

**3. What needs a model**

| Operation | Deterministic execution | Model contribution |
|---|---|---|
| Read | Open file, select range, return bytes/text | Decide what to inspect; interpret evidence |
| Search/list | Run matching/enumeration with explicit options | Choose search strategy; investigate findings |
| Write/edit | Apply exact content or validated patch | Generate the intended change |
| Run command | Launch process, enforce limits, capture exit/output | Choose command; diagnose outcome |
| Verification | Run fixed checks and inspect actual artifacts | Assess semantics where checks are insufficient |

Calling Qwen to “perform a Read” adds latency without improving I/O. Likewise, applying a Claude-authored patch locally moves the write operation but leaves change-generation cost with Claude.

The service should accept **objectives and constraints**, then keep intermediate tool results and next-step reasoning local. Measure cloud turns and tokens separately from operation counts.

**4. The enforcement boundary**

Add a new `local_only` execution mode, separate from today’s route-first enforcement.

The current gate clears pending state on any bare `llm_*` name before that tool executes. Exact-name and same-server matches also clear it. [`enforce-route.py:1333`](src/llm_router/hooks/enforce-route.py:1333) Read-only native tools have exemptions, and repeated violations can trigger an automatic unblock. [`enforce-route.py:1403`](src/llm_router/hooks/enforce-route.py:1403), [`enforce-route.py:1502`](src/llm_router/hooks/enforce-route.py:1502)

Replace those semantics **within the new mode** with these invariants:

1. **Native execution remains unavailable for the entire session.** No acknowledgment, routing call, successful task or completion receipt unlocks it.
2. **Only exact, configured local-task MCP tools are exposed.** No `llm_*` prefix allowance, general `llm_act`, foreign filesystem MCP, or native Agent/Task delegation.
3. **The service authorizes every operation.** Claude and Qwen cannot supply a policy override, arbitrary workspace root or self-issued capability.
4. **Execution failure preserves the restriction.** Ollama failure, malformed state, budget exhaustion and repeated denials never enable native or cloud execution.

Provide a supported launcher that removes built-in execution tools with `--tools ""` and loads only the dedicated server through `--strict-mcp-config` and `--mcp-config`. An explicitly tested interaction-tool subset can be added later. These flags are documented separately from permission auto-approval. [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference)

Install a catch-all `PreToolUse` guard as defense and feedback, independent of the pending-route latch. **Tool removal must remain effective when that hook crashes.** Validate the complete plugin/tool/hook inventory at startup; loading one MCP configuration alone does not establish that every other executable integration is harmless.

The guarantee applies to model-requested operations under this supported launch profile. Claude Code still performs host housekeeping, such as reading configuration and writing transcripts. Preventing arbitrary same-user processes or administrators from bypassing the profile requires OS isolation and protected configuration.

The worker must run with scoped filesystem access, stripped credentials and restricted network access. Its command children must not reach cloud APIs, the policy store or executor control socket. A worktree is an isolation aid, **not a sandbox**.

Pin approved local model identities/digests and a loopback Ollama endpoint. Reject remote endpoints, redirects and cloud-backed model configurations. Restrict planning, repair and verification model calls too.

Do not simply reuse default `llm_delegate`: its planner uses the general router, and its adapter set includes Codex. [`tools/agentic.py:60`](src/llm_router/tools/agentic.py:60), [`tools/agentic.py:88`](src/llm_router/tools/agentic.py:88)

**5. Task lifecycle and completion contract**

1. `UserPromptSubmit` registers the original objective, constraints and a session/turn nonce with the broker. It performs no agent loop in `local_only`.
2. Claude submits the registered task through the dedicated MCP tool. The service binds it to an authorized root, workspace snapshot and immutable policy.
3. The local loop investigates and stages changes. Every operation produces executor-generated evidence.
4. A supervisor runs task-specific acceptance checks against actual artifacts.
5. The service returns a typed outcome and bounded evidence report. Permitted promotion applies verified changes with conflict checks; otherwise the deliverable remains a proposal.

Suggested initial budgets: **600 seconds total, 64 model steps, 60 seconds per inference and 120 seconds per command**, with 20% reserved for verification. These are starting settings to benchmark. Every operation and retry must use the remaining task deadline; reconnects must not reset it.

Use durable job IDs and a blocking wait with progress notifications. A host timeout retrieves the existing job instead of resubmitting work. Avoid repeated cloud polling turns.

Return statuses such as:

```json
{
  "task_id": "broker-generated",
  "status": "incomplete",
  "reason": "investigation_budget_exhausted",
  "workspace_snapshot": "sha256:...",
  "operations_receipt": "receipt-id",
  "verification": {
    "passed": [],
    "failed": [],
    "not_run": ["required_regression"]
  },
  "changes": {
    "staged": ["src/example.py"],
    "applied": []
  }
}
```

Terminal statuses should distinguish `verified_complete`, `proposed`, `incomplete`, `blocked`, `failed` and `cancelled`.

This fixes a concrete current defect: the loop returns partial-work strings on deadline or iteration exhaustion, while `quality_ok` primarily checks length and refusal phrases. Those strings can pass as successful results. [`agent_loop.py:650`](src/llm_router/hooks/agent_loop.py:650), [`agent_loop.py:760`](src/llm_router/hooks/agent_loop.py:760), [`direct_executor.py:317`](src/llm_router/hooks/direct_executor.py:317)

Receipts must come from the executor’s protected store and bind task/turn identity, policy, model identity, operation arguments, read ranges, truncation, file hashes, subprocess outcomes and verification. They prove recorded execution and checks—not semantic correctness.

For changes, preserve the initial dirty and untracked workspace state. Require preimage hashes before promotion, serialize conflicting writers and journal recovery. Do not automatically retry a non-idempotent command whose outcome became unknown after a crash.

**6. Quality and safety boundary**

The supplied **8/11 versus 10/11** result supports selective delegation, not unrestricted autonomous execution. Eleven cases do not establish a general production error rate, and a larger budget does not prove investigation failures are solved.

| Work | Proposed treatment |
|---|---|
| Bounded lookups and inventories | Local execution with exact evidence and explicit search coverage |
| Mechanical transformations | Local generation/application with deterministic postconditions |
| Isolated fixes with meaningful regression checks | Local staging; independently verified completion |
| Open-ended cross-system investigation | Local evidence gathering; unresolved conclusions remain explicit |
| Security-sensitive, concurrency-critical or irreversible changes | Require stronger review; no autonomous promotion based only on local self-report |

Detect the observed failure patterns directly:

- **Assumed facts:** repository claims must reference captured evidence from the relevant snapshot. Validate citation existence; recognize that this cannot prove interpretation.
- **Skipped investigation:** require task-specific evidence obligations. One irrelevant read does not establish completion.
- **Search blind spots:** preserve options, scope and truncation indicators; support pagination. Current search defaults to `*.py`, ignores case and caps matches at 50. [`agent_loop.py:234`](src/llm_router/hooks/agent_loop.py:234)
- **False modification claims:** distinguish staged, proposed and applied artifacts. Current `propose` correctly changes nothing. [`agent_writes.py:213`](src/llm_router/hooks/agent_writes.py:213)
- **False verification:** supervisor-owned checks must inspect real state. The worker cannot replace a regression check with `echo ok`, weaken its acceptance criteria or grade its own output.
- **Stalls/exhaustion:** terminate as incomplete with evidence and remaining obligations.

Reuse the actual-repository verification approach in [`acceptance.py:129`](src/llm_router/agentic/acceptance.py:129), but require meaningful criteria. A matching diff symbol or zero exit code alone is insufficient.

The command allowlist is also insufficient confinement: it admits Python, Node, test runners and other programs capable of writes or networking. [`agent_writes.py:47`](src/llm_router/hooks/agent_writes.py:47) Enforce restrictions through the sandbox, including when running repository tests.

Cloud review can remain an explicitly permitted **text review** using locally gathered evidence. Cloud tool execution stays disabled. Fully local deployments surface uncertainty to the user instead.

**7. Concrete implementation plan**

Paths below are relative to `src/llm_router/`.

| Files | Change |
|---|---|
| **New:** `local_execution/protocol.py`, `policy.py`, `store.py` | Typed tasks/results, immutable capabilities, durable jobs and executor receipts |
| **New:** `local_execution/executor.py`, `sandbox.py`, `verify.py` | Shared deterministic adapters, confinement, artifact promotion and independent verification |
| **New:** `local_execution/service.py`, `server.py`; `tools/local_tasks.py` | Persistent worker and dedicated MCP surface |
| **New:** `commands/local.py` | Supported Claude launcher and direct local-task frontend |
| **New:** `hooks/local-execution-gate.py` | Small catch-all guard with exact tool identity checks |
| `hooks/agent_loop.py`, `hooks/direct_executor.py`, `agentic/react.py` | Consolidate execution adapters; typed termination, real deadlines, cancellation and progress. Avoid adding a third divergent loop |
| `hooks/auto-route.py`, `hooks/enforce-route.py`, `hooks/tool_intercept.py` | Bypass advisory draft/legacy interception paths in `local_only`; preserve old behavior outside it |
| `hooks/agent_writes.py`, `agentic/acceptance.py`, `agentic/worktree.py` | Capability-based staging/promotion and stronger verification |
| `install_hooks.py`, `install_manifest.py`, `enforce_config.py`, `env_registry.py`, `tool_surface.py` | Install, validate and report the new profile and tool surface |
| `execution_ledger.py` | Separate attempted, executed, rejected, proposed and verified outcomes |

Worktree handling needs explicit hardening: current creation ignores command failure, and merge applies a tracked diff then discards the worktree even when application fails. Preserve failed artifacts and include new files. [`worktree.py:51`](src/llm_router/agentic/worktree.py:51)

Proposed configuration:

```yaml
execution_mode: local_only       # advisory remains the legacy default
local_execution:
  model: qwen3-coder:30b
  model_digest: "<approved digest>"
  ollama_url: http://127.0.0.1:11434
  task_budget_s: 600
  max_steps: 64
  writes: propose               # apply requires an authorized task capability
  commands: sandboxed
```

Add `LLM_ROUTER_EXECUTION_MODE`, `LLM_ROUTER_LOCAL_MODEL`, `LLM_ROUTER_LOCAL_TASK_BUDGET_S` and `LLM_ROUTER_LOCAL_MAX_STEPS`. Resolve configuration once at launch; project content and worker subprocess environments cannot weaken it.

`LLM_ROUTER_ENFORCE=strict` and `LLM_ROUTER_ZERO_CLAUDE=1` must **not** silently acquire this new meaning.

Migration: ship protocol/executor tests first; run explicitly non-enforcing observation trials; qualify supported Claude versions and sandbox platforms; then offer opt-in `local_only`. Refuse strict startup when required confinement or tool-surface checks fail. Preserve legacy mode for compatibility.

**8. Tests that prove enforcement and useful execution**

Both properties must pass. A system that blocks everything satisfies only half the requirement.

1. **Negative execution tests:** attempt every native operation, aliases, foreign MCP tools and subagent paths. An acknowledgment call, unrelated completed task or replayed receipt must never enable a subsequent Read/Edit/Bash. Assert native executor entry counters and mutation sentinels remain untouched.
2. **Failure injection:** malformed hook input, hook crash/timeout, missing state, Ollama down, corrupt receipts and repeated denials must leave native tools unavailable.
3. **Positive end-to-end fixture:** create randomized fixture data known only to the executor. Require a task to read, search, edit, create a file and run a meaningful check. Independently inspect exact outputs and resulting disk state.
4. **False-completion tests:** fake workers returning acknowledgment, irrelevant reads, fabricated diffs, unapplied proposals or exhausted-loop text must fail completion checks.
5. **Confinement tests:** symlink escapes, stale preimages, concurrent writers, malicious test subprocesses, remote model endpoints and forbidden provider calls. Verify actual process/network behavior.
6. **Recovery tests:** duplicate submission, reconnect, cancellation and crash during mutation. Unknown command outcomes must not cause automatic re-execution.
7. **Mutation tests:** intentionally remove the gate, unlock after a receipt, accept incomplete results or enable cloud fallback. Each mutation must fail the suite.
8. **Live quality evaluation:** rerun the supplied benchmark plus held-out cases and repeated trials. Report false completion, verified task completion, native execution count, cloud turns/tokens and end-to-end latency separately.

Extend the existing interception, agent-loop, write-guard, installation and verification suites; add a pinned-Claude conformance suite. Existing denial-payload unit tests establish JSON shape, not live enforcement. [`test_tool_intercept.py:212`](tests/test_tool_intercept.py:212)

**9. Honest failure modes**

- **Latency:** cold model loading and dozens of local inference steps can take minutes. Warm workers and task batching help; the proposed budget requires measurement.
- **Context loss:** a task summary may omit an earlier constraint. Carry original instructions, relevant conversation facts, snapshot identity and unresolved questions explicitly. Avoid the current blanket 2,000-character delegation-context truncation. [`tools/agentic.py:161`](src/llm_router/tools/agentic.py:161)
- **Wrong but plausible results:** evidence and passing tests cannot establish complete correctness. Unsupported conclusions remain uncertain; high-impact changes need review.
- **Ollama unavailable:** return a typed failure and preserve staged work. No native/cloud execution fallback.
- **Claude refuses to delegate:** execution can be prevented; useful cooperation cannot be guaranteed. Bound denial loops and terminate with an actionable failure, without unlocking tools.
- **Partial side effects:** filesystem promotion can be journaled; arbitrary commands may not be reversible or exactly-once. Restrict their capabilities and retain uncertain outcomes.
- **Host changes or bypasses:** version changes, plugins and sibling hooks can invalidate assumptions. Enforcement requires a qualified launch profile, protected policy and repeated conformance testing; an arbitrary Claude session cannot inherit the guarantee merely by installing a hook.