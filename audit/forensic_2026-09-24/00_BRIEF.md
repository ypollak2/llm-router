# Forensic audit brief (condensed faithfully from the user's 78-section brief, 2026-09-24)

Goal: determine what llm-router should look like if an exceptional team maintained it today from
first principles — "the smallest, clearest, safest architecture that fully preserves the capabilities
that actually matter." Aggressive about simplification, conservative about breaking proven behavior.
Preference: DELETE > MERGE > REUSE > MOVE > RENAME > REFACTOR > ABSTRACT > ADD. Every module, config
option, public API, compatibility path, store, persisted representation and concept has a carrying cost.

## §1 Non-negotiable rules
- Evidence before conclusions. Never claim unused/redundant/dead/obsolete/duplicated/unreachable/
  deprecated/unnecessary/incorrect/dangerous without proof: file, symbol, lines, callers, imports,
  tests, config refs, CLI refs, doc refs, runtime registration.
- No direct import ≠ dead. Account for decorators, entry points, registries, MCP registration, plugin
  discovery, importlib, reflection, command dispatch, string lookup, hooks, package metadata,
  subprocess entry points (hook scripts run as files!), config-driven loading.
- Deletion classes: PROVEN DEAD / EFFECTIVELY DEAD / LEGACY / DUPLICATE / LOW-VALUE / UNCERTAIN.
  Never silently turn UNCERTAIN into DEAD.
## §2 AUDIT ONLY. Do not modify implementation, delete, rename, reformat. Write only your audit file.

## §3 Recon: map root, src/llm_router, tests, _quarantined_tests, hooks, commands, skills, config, docs,
guide, architecture, audit, packaging, npm, scripts, submissions, plugin dirs, .github workflows, MCP
config, pyproject, lock files, ignore files, changelogs, SECURITY, CONTRIBUTING. Determine language,
Python versions, build/packaging, package + executable names, entry points, runtime/optional/dev deps,
lint/format/type/test/coverage config, release mechanism, hosts, providers, persistence, config
sources, env-var surface, plugin/MCP/hook mechanisms. Repository map explaining RESPONSIBILITY of each
significant dir/module.
## §4 Baseline: tests (pass/fail/skip/xfail/quarantined/warnings/collection errors), lint, format, type
check, time, coverage. Explain WHY tests are quarantined (obsolete/flaky/debt/env/abandoned/genuine
integration/hidden failures).
## §5 Metrics: total/production/test/doc LOC, modules, packages, public symbols, CLI commands, MCP tools,
providers, hosts, env vars, config keys, SQLite tables, JSON/JSONL formats, registries, caches, stores,
routing strategies, policy concepts. 25 largest files, 25 highest-complexity functions, 25 most inbound /
outbound deps, cycles, large public surfaces, single-caller modules, zero-caller modules, classes with
one instantiation, protocols with one impl, near-empty wrappers. LOC is a smell detector, not quality.
## §6 Runtime flows (trace code, not docs): A normal routed query (host→hook/MCP→classification→context→
policy→capability→selection→execution→fallback→validation→accounting→persistence→result); B free/local
(Ollama); C premium/native; D zero-Claude direct replacement; E direct-execution agent (tool calling,
write/edit/run_command, validation, rejection, fallback); F MCP registration→execution; G CLI; H host
installation per host; I context/knowledge (OKF/project/session/memory) and its effect on routing;
J accounting (attempt→tokens/cost→persistence→attribution→savings→CLI/dashboard). For each: modules
crossed, request/response representations, decision objects, storage writes, config lookups,
transformations, divergence points, swallowed errors, fallbacks, telemetry, where one fact can become
inconsistent.
## §7 Product core: CORE / SUPPORTING / OPTIONAL / EXPERIMENTAL / LEGACY / DEV-ONLY / DOC-ONLY. Smallest
credible product preserving 95% of user value.
## §8 Dead code: imports (unused/wildcard/compat/side-effect/aliases), functions (never called, called only
by dead code, test-only, trivial wrappers, shims), classes (never instantiated, one consumer, pure
wrappers, fake abstraction), constants (unused/duplicate/conflicting defaults/magic values), EVERY
LLM_ROUTER_* env var (defined where, read where, default, documented, tested, effect, conflicts,
aliases, deprecation; undocumented / documented-but-unread / read-but-ineffective / aliases / remnants),
config keys (same), CLI flags (undocumented/ignored/redundant/superseded/inconsistent), MCP tools
(impl, consumer, overlap, unique value, obsoleted by consolidated mode?, public?), providers (valid,
reachable), hosts (implemented/partial/aspirational/experimental/documented accurately), scripts (CI/
packaging/manual/obsolete/duplicated by CLI or another script), docs referencing removed commands/old
env vars/package names/architecture/model names/superseded results, dependencies (verify dynamic/optional).
## §9 Semantic duplication (not just copy-paste). Investigate: cache.py vs cache/, benchmark/ vs
benchmarks.py, contract.py vs contracts.py, dashboard/ vs dashboard_data.py, classification components,
context prep/optimization/signals, execution signals/ledgers, feedback, budget, usage/accounting,
monitoring vs observability, memory vs knowledge vs context, agents vs agentic, policy vs rules vs gates,
capabilities vs classification, host modules vs integrations, command modules vs CLI helpers, storage vs
direct SQLite. For each family: canonical concept, #implementations, #representations, differences,
intentional?, could one replace others, what breaks, what disappears. → Semantic Duplication Map.
## §10 Abstractions (base classes, protocols, registries, managers, coordinators, services, factories,
repositories, backends, adapters, strategies, handlers, engines, controllers, providers, builders,
envelopes, contracts, wrappers): what concrete complexity does it remove? Flag single-impl, pure
delegation, duplicates Python interfaces, no real substitution point, mocked-only, spreads one op across
files, anticipatory, local-simpler-but-system-more-complex. Show Current A→B→C→D vs Potential A→C and
what B/D contribute.
## §11 Module/package structure: too flat/fragmented, weak boundaries, misplaced files, tiny packages,
unclear ownership, name collisions, tangles, cycles, generic names (utils/common/helpers/tools/context/
data), split by implementation detail. Cohesion. Target package tree (no one-class-per-file).
## §12 Domain model: route, decision, model, provider, capability, classification, prompt, request,
response, attempt, execution, policy, budget, quota, cost, savings, context, knowledge, session, host,
tool, agent, fallback, signal, feedback — canonical type, alternative types, dicts, JSON/DB/config/API
forms, "dictionary soup", object→dict→object→JSON→dict conversions.
## §13 Routing engine (safety-critical): how complexity classified, capabilities derived, candidates
identified, providers filtered, cost/quota/latency/context/privacy considered, policies, historical
feedback, fallback, retries, circuit breaking, quality verification, success/failure recording. Competing
decision engines? Paths producing different behavior, policy/capability bypasses, hardcoded models,
host-specific routing outside core, fallback duplicated in providers, inconsistent defaults, stale model
ids, hidden priority rules, order dependence. → single routing decision graph with actual precedence.
## §14 Edge cases: no provider; only local; local timeout; malformed response; auth failure; rate limit;
500; timeout; stream interrupted; cancellation; huge/empty/binary/Unicode prompt; tool call; structured
output; media; code edit; multi-file; current-info; high-risk command; prompt with credentials; ambiguous
alias; unsupported model/provider capability; exhausted subscription/budget; corrupted state; SQLite
locked; missing/malformed/contradictory config; stale cache/pricing/knowledge index; missing index;
context too large; circular fallback; chain exhausted; retry storm; repeated route after partial
execution; hook invoked twice. "Cheap first" becoming "slow and expensive eventually" — failed attempts
are not free.
## §15 Context/memory/knowledge: map prompt/session/project/code context, OKF, historical memory,
decisions, lineage, feedback, execution history, routing signals. Needed for routing / generation /
analytics / learning? persisted/ephemeral? duplicate? stale? invalidation owner? Duplication, excess
tokens, stale/low-value content, per-request injection, unbounded growth, repeated parsing,
cross-project contamination, privacy leakage, path identity, symlinks, repo identity collisions. Measure
context tokens added before the routed model sees the task.
## §16 State/storage: every store (SQLite, JSON, JSONL, YAML, config, project dirs, knowledge stores,
attempt history, session state, caches, migrations): owner, schema, writer, reader, lifecycle, migration,
concurrency, corruption, retention, cleanup, versioning. Same fact persisted twice? Which wins? Dual
writes, transactions, concurrent CLI/MCP/hook processes, file/SQLite locking, atomic writes, crash recovery.
## §17 Cost/savings: actual cost, theoretical cost, subscription quota, tokens (real/estimated), baseline
model/price, savings %, prepaid/free/local usage, failed attempts, retries, classification cost, routing
overhead. Distinguish money avoided / counterfactual API spend / quota preserved / premium turns avoided /
premium tokens avoided / routed turns / draft turns / accepted drafts / direct replacements / free-provider
usage. Conflations across dashboards, CLI, telemetry, README. Every % claim needs numerator, denominator,
n, window, baseline, workload, mode, host, methodology; else OBSERVATIONAL.
## §18 Providers: per provider — adapter, request/response conversion, streaming, tools, structured
output, media, timeout, retry, error normalization, model discovery, pricing, rate limits, credentials.
Boilerplate duplication; files touched to add a provider; provider metadata duplicated across source/
config/docs/pricing/capabilities/tests → single source of truth.
## §19 Hosts: per coding host — integration level, install, hooks, MCP, auto/manual routing, status UI,
limits, uninstall, update. Capability matrix from executable evidence: fully supported / with
limitations / MCP-only / manual / experimental / planned / unsupported. Compare to docs.
## §20 Direct execution / agentic safety: model output→tool selection→args→validation→path normalization→
project-root enforcement→symlinks→subprocess→env→network→filesystem→secrets→allow/blocklists→
confirmation→proposal-only→writes. Adversarial: ../../, symlink escape, absolute paths, nested shells,
interpreters (python -c, node -e, bash -c), destructive git, package-manager scripts, network clients,
credential files, env dumping, command-substitution variants, encoding tricks, argument injection,
recursive deletes, exfiltration. Does the boundary survive alternative paths? Distinguish prevention of
system damage / project damage / credential reads / credential exfiltration / network access.
## §21 Errors: broad except, swallowed, warning-only, fail-open vs fail-closed, unclassified retries,
double wrapping, inconsistent types, misleading messages, stack traces to users, lost provider details,
secrets in errors. Correct policy per boundary. Fallbacks hiding bugs ("always falls back" = silently
disabled core feature).
## §22 Async/concurrency: sync/async/threads/subprocesses/background tasks/concurrent attempts/DB/locks;
blocking in async, needless async, sync/async duplicate APIs, forgotten awaits, task leaks, races, shared
mutable globals, concurrent DB writes, unbounded concurrency, lock ordering.
## §23 Performance (routing critical path): startup, hook latency, classification, context loading, DB,
selection, local-model timeout impact, failed-attempt cost, serialization, FS traversal, indexing;
repeated disk/config/metadata/pricing reads, regex recompilation, N+1, missed/unneeded parallelism.
Routing overhead independent of inference.
## §24 Caches: key, value, TTL, invalidation, bound, persistence, concurrency, correctness; measured
benefit? stale-cache bugs; cache.py vs cache/.
## §25 Config: precedence across defaults/env/.env/YAML/CLI flags/host/project/user config → table. Two
knobs one behavior, contradictory defaults, import-time reads, repeated reads, boolean explosion,
invalid combinations; N booleans → one mode; count reachable states.
## §26 Dependencies: per runtime dep — where used, capability, stdlib replaceable?, heavy?, optional but
installed for all?, burden. Unused, duplicate, overlapping, obsolete, undeclared transitive, trivial-use,
dev deps needed at runtime. Don't replace mature libs with custom code.
## §27 API surface: Python imports, CLI commands/flags, MCP tools, env vars, YAML, hooks, plugin/provider/
host contracts — stable/public/semi-public/internal-but-exposed/legacy/experimental. Accidental APIs via
broad __init__ exports.
## §28 CLI: hierarchy, naming, discoverability, help, aliases, redundant/legacy commands, option/output
consistency, machine-readable output, exit codes; docs vs actual --help.
## §29 MCP: per tool — does the model need it as a separate concept, usage, overlap, mergeable into one
typed op, selection confusion, token budget, internal machinery exposed. Approx schema token footprint:
full vs consolidated surface.
## §30 Tests: count, test vs prod LOC, time, skips, xfails, quarantine, fixtures, mocks, snapshots,
integration, e2e. Tests that duplicate, test implementation, trivial getters, assert mocks, lock in
obsolete architecture, block simplification without protecting behavior, duplicate production
calculations, never meaningfully fail, huge setup. Gaps in: routing correctness, fallback, cost
accounting, hosts, direct execution, security, migrations, concurrency, config precedence, back-compat.
Classify: CRITICAL CONTRACT / HIGH-VALUE REGRESSION / USEFUL UNIT / IMPLEMENTATION-COUPLED / DUPLICATE /
LOW-VALUE / OBSOLETE. Don't delete tests to cut LOC.
## §31 Security: committed secrets, unsafe subprocess, shell injection, path/symlink traversal, SSRF,
unsafe fetch, unsafe deserialization/pickle, eval/exec, unsafe temp files, permissions, credential/prompt
logging, telemetry leakage, SQL injection, RCE, plugin trust, remote content execution, dependency vulns,
untrusted config execution, malicious model output, prompt injection → tool execution. No model string
becomes a privileged op because the model produced it.
## §32 Privacy: all data that can leave the machine per provider (prompt, context, source, env-derived,
metadata, ids, tool results). Verify secret scrubbing (API keys, private keys, tokens, bearer, .env,
unusual/multiline) applies before EVERY external path.
## §33 Observability: can it answer why a route happened, which policy/classifier result, candidates
considered/rejected, cost, fallback, stage timings, did it replace premium work, later rejected, saved
anything under the stated definition? Duplicate telemetry systems; one coherent event model.
## §34 Logging: levels, format, duplication, PII, secrets, verbosity, rotation, structure, console
pollution, libraries logging directly, logs used as program state, warnings that should be errors.
## §35 Migrations: current schema, upgrade path, still-needed old migrations, downgrade, idempotency,
corrupted state; collapsible baggage (don't delete history blindly).
## §36 Versioning/release: version source, tags, changelog, workflow, PyPI, deprecated aliases, metadata,
npm; one authoritative version; duplicated version strings.
## §37 Hygiene: committed generated artifacts, stale audit reports, obsolete benchmarks, old architecture
docs, one-off migrations, abandoned experiments, redundant assets, local dev artifacts, duplicate config,
obsolete ignore patterns, stale plugin metadata. Does each root file belong at root?
## §38 Git archaeology (git log/blame) for suspicious code, to separate intent from residue.
## §39 Comments: obsolete, contradicting code, obvious, old/dead TODOs, promising unsupported behavior;
narrative comments compensating for over-complicated code.
## §40 Naming: attempt/run/execution, decision/route/selection, policy/rule/gate, memory/context/knowledge,
host/integration/client, cost/saving/quota → terminology table + canonical terms.
## §41 README Claim Ledger: | Claim | README location | Code evidence | Test evidence | Measured evidence |
Status | — VERIFIED / PARTIALLY VERIFIED / OBSERVATIONAL / STALE / AMBIGUOUS / MISLEADING / UNSUPPORTED.
Claims: hosts, automatic routing, zero-key, routing behavior, savings, quota, local-first, privacy,
secret protection, provider/model/tool counts, reliability, circuit breakers, fallback, latency,
context, OKF, direct execution, file/command protection, benchmarks, RouterArena, config, capabilities.
A test named after a feature does not prove the user-facing claim.
## §42-43 README: README keeps what/who/why/differentiator/60s install/one example/hosts at a glance/core
capabilities/safety+privacy caveats/links/contributing; rest to GETTING_STARTED, HOST SUPPORT,
PROVIDERS, POLICIES, TOOLS, ARCHITECTURE, MEASUREMENT, SECURITY, TROUBLESHOOTING. Rewrite: scannable,
accurate, evidence-backed, clear on subscription vs API routing, automatic vs manual hosts, draft vs
actual turn replacement, what "savings" means, privacy limits, direct-execution risk. Not marketing,
not architecture, no hidden limitations.
## §44 Doc duplication: topic→every doc; contradictions among README, guide, docs, architecture,
SECURITY, CONTRIBUTING, changelog, CLI help, .env.example, config examples; canonical owner per topic.
## §45 Feature matrix: | Capability | Claimed | Implemented | Tested | Documented | Recommended status |.
## §46 Plugins/extras: dashboards, media, agentic, benchmarks, storage backends, control-plane,
provider ecosystems, visualization, learning systems → core / extra / plugin / dev tooling / separate.
## §47 Overengineering questions (one file? function vs hierarchy? mapping vs branching? one typed model
vs dicts? one mode vs booleans? one event schema? one provider registry? one routing pipeline vs
host-specific? one persistence layer? delete instead of redesign?)
## §48 Underengineering: regex-only security, concurrent storage without transactions, fragile string
protocols, global mutable state, unversioned persisted data, undocumented external contracts, exceptions
as routing logic, untested security-critical behavior.
## §49 Deletion ledger | Candidate | Type | Evidence | Depends on it | Removal risk | Est. LOC | Confidence |
grouped: safe now / after deprecation / after migration / needs evidence / retain. Include source,
tests, docs, deps, config, scripts, assets. Best deletions remove a whole concept.
## §50 Consolidation ledger | Concepts | Current impls | Canonical | Deleted concepts | Risk |.
## §51-52 Target architecture: current vs target tree, why per move; per package responsibility,
permitted/forbidden deps, public interface. Fitness: files changed to add provider / host / policy /
capability / change accounting / change storage / remove a feature.
## §53 Risk classes R0 pure deletion … R5 breaking user-facing.
## §54-57 Phased plan (0 baseline+guardrails, 1 proven deletion, 2 local simplification, 3 semantic
consolidation, 4 package restructuring, 5 public surface reduction, 6 docs, 7 README), atomic commits
(title, files, why, behavior preserved, validation, expected net change), simplification + quality
metrics (baseline → justified target; no arbitrary %).
## §58 Do-not-change register. §59-65 Top 10s: complexity sources, deletions (complexity removed / risk),
consolidations, correctness risks, security risks (evidence, exploitability, no inflation), doc
problems (incorrect mental models), testing problems (false confidence).
## §66 REQUIRED FINDING FORMAT
```
ID: / Category: / Severity: CRITICAL|HIGH|MEDIUM|LOW / Confidence:
Location: Files: Symbols: Lines:
Observation: / Evidence: / Why this exists, if discoverable: / Why this matters:
User-visible impact: / Engineering impact: / Is behavior currently used? YES|NO|UNCERTAIN
Recommended action: KEEP|DELETE|MERGE|MOVE|SIMPLIFY|REWRITE|DEPRECATE / Proposed target:
Behavioral compatibility risk: / Security risk: / Performance impact: / Estimated complexity removed:
Validation required: / Dependencies on other findings:
```
Do not assign HIGH because code is ugly.
## §70-73 Maintainer test (concepts/files/state systems/registries/config sources to modify routing);
new-feature test (files changed today to: add a model with tools+structured output; add a provider;
host; policy; usage metric; persisted field); removal test; one-source-of-truth audit for version,
providers, models, capabilities, pricing, host support, env vars, CLI commands, MCP tools, policies,
cost calculations, model aliases, doc capability tables.
## §74 DO NOT: code golf, clever expressions, generic util layers, frameworks, hypothetical abstractions,
tiny files, DI everywhere, one-impl interfaces, classes for everything, style rewrites, new deps for
trivia, silent back-compat removal, deleting dynamic integrations static analysis can't see, coverage
%≈quality, more docs≈better docs, hiding limitations, preserving complexity because tests encode it.
## §75 For every non-trivial component: why exists, who uses, what invariant, what if deleted, same
responsibility elsewhere, canonical?, exposes internals?, duplicate persistence?, config still needed?,
abstraction earning cost?, test protects behavior or implementation?, would we design it this way today
(if no, what replaces it)?
## §79 Completion bar: continue until more inspection yields materially diminishing new findings.
