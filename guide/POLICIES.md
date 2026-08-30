# Routing Policies

Policies are YAML files in `src/llm_router/policies/` that override the
routing strategy: which models handle which subjects, the per-task fallback
chains, behavior knobs (`confidence_threshold`, `cost_cap_per_query`, etc.).

A policy is *strategy*. The complexity classifier and the bandit telemetry
are runtime *mechanics* that apply regardless of which policy is active —
so a policy that says "code prompts go to `qwen3-coder-next`" can still be
overridden by the bandit when telemetry says a different model is winning
on code.

## Activating a policy

```bash
export LLM_ROUTER_POLICY=cost_aggressive   # or balanced, conservative, aggressive, …
```

The env var is read once per process; restart the MCP server (or your
host CLI) after changing it.

## Policies that ship with llm-router

| Name | Confidence | Typical savings | Typical use |
|---|:---:|:---:|---|
| `standard` | 4 | 35–45% | The historical default — direct API tier, no policy needed for typical Pro/Max usage. |
| `balanced` | 4 | 35–45% | Cost/quality tradeoff (alias for `standard` behaviour with the v10 model-strategy fields populated). |
| `aggressive` | 2 | 60–75% | Route more, escalate less. Max cost reduction. |
| `conservative` | 6 | 10–15% | Route only when the classifier is very confident. Quality over savings. |
| `cost_aggressive` | 3 | 70–85% | **OpenRouter open-weight workhorse pool + subject specialists.** Recommended when `OPENROUTER_API_KEY` is set. New in v10. |
| `routerarena_tuned` | 3 | 70–85% | **Deprecated alias** for `cost_aggressive`. Slated for removal in v11. |

## Writing a custom policy

The minimal YAML looks like this — drop it at
`~/.llm-router/policies/<your-name>.yaml`:

```yaml
name: my_strategy             # must be a valid Python identifier; matches filename minus .yaml
description: |
  Short explanation of when and why this policy beats the defaults.

# ── Behavior knobs ────────────────────────────────────────────────
confidence_threshold: 4       # 0-10. Lower = route more aggressively.
skip_acknowledgements: true   # skip routing for "yes"/"ok"/"thanks"
route_coordination: false     # route git/deploy/execution commands
prefer_ollama: true           # try local Ollama before anything else
cost_cap_per_query: 0.05      # production safety net, in USD

# ── Model strategy ────────────────────────────────────────────────
workhorses:
  - ollama/qwen3.5:latest                                # local, free
  - openrouter/qwen/qwen3-235b-a22b-2507                 # cheap workhorse
  - codex/gpt-5.4                                        # subscription-free

specialists:                                              # subject → specialist model
  code:      openrouter/qwen/qwen3-coder-next
  medical:   openrouter/google/gemini-3.1-flash-lite
  reasoning: openrouter/x-ai/grok-4.3

fallback_chain_complex:                                   # deep-reasoning escalation
  - openrouter/anthropic/claude-sonnet-4
  - openai/o3

# ── Per-(profile, task) chain overrides ───────────────────────────
# Optional. If omitted, profile/task chains fall through to standard.yaml.
chains:
  budget:
    query:
      - ollama/qwen3.5:latest
      - openrouter/qwen/qwen3-235b-a22b-2507
    code:
      - openrouter/qwen/qwen3-coder-next
  balanced:
    query:
      - openrouter/qwen/qwen3-235b-a22b-2507
      - codex/gpt-5.4
```

## How policy fields drive routing

| Field | Where it kicks in |
|---|---|
| `workhorses` | The free-first chain head when no subject specialist applies. The first OpenRouter (or paid-but-cheap) model is the default escalation after local Ollama. |
| `specialists` | Looked up by the classifier-emitted `Subject`. If `specialists[subject]` exists, it's prepended to the chain so it's tried first. |
| `chains[profile][task]` | Overrides the static `ROUTING_TABLE` lookup for `(profile, task_type)`. Useful when one policy wants completely different chains per profile. |
| `confidence_threshold` | The classifier's confidence floor for "route this prompt". Below the threshold, the prompt skips routing. |
| `cost_cap_per_query` | Production safety net. Calls projected above the cap escalate to a no-route or alternate strategy. |

## Subjects (Plan 07 Cat B)

The classifier emits one of these subjects per prompt; policies map them to specialists:

`general` (no specialist needed) · `code` · `medical` · `math` · `physics` · `chemistry` · `biology` · `history` · `law` · `business` · `finance` · `economics` · `narrative` · `reasoning` · `cloze` · `trivia`

A policy doesn't need to declare a specialist for every subject — unhandled subjects fall through to `workhorses`.

## The bandit consults your specialists, doesn't replace them

The Plan 07 epsilon-greedy bandit (`llm_router.bandit`) reorders the candidate chain based on telemetry: if `routing_decisions` shows a different model winning on (policy, subject) than the one your YAML names, the bandit surfaces the empirical winner first.

If you want byte-identical pre-v10 routing (deterministic A/B vs v9), opt out:

```bash
export LLM_ROUTER_BANDIT=off
```

## Validating a policy

```bash
# Make sure your YAML parses and the chains/specialists/workhorses load cleanly:
python -c "from llm_router.policy import PolicyManager; \
           p = PolicyManager().load_policy('my_strategy'); \
           print('workhorses:', p.workhorses); \
           print('specialists:', p.specialists)"

# Compare against the default to see what changes:
llm-router policy diff balanced my_strategy
```

### SUBSCRIPTION_LOCAL profile

`SUBSCRIPTION_LOCAL` is for setups with one paid seat plus a free local bucket. Set
`LLM_ROUTER_SUBSCRIPTION_PROVIDER` to the paid provider (e.g. `anthropic`) — the profile is a
complete no-op unless this is set. Simple/moderate prompts route to local/free providers first;
complex prompts prefer the paid seat. Add extra self-hosted free providers with
`LLM_ROUTER_INTERNAL_PROVIDERS`. When the seat's quota pressure reaches
`LLM_ROUTER_SUBSCRIPTION_PRESSURE_THRESHOLD` (`0.80` by default), the seat is demoted to last and
more traffic shifts to local providers. Enable with `LLM_ROUTER_COST_PROFILE=subscription_local`, or
leave it on under any profile once a subscription is set (turn that off with
`LLM_ROUTER_SUBSCRIPTION_REORDER_ALL_PROFILES=off`).

## See also

* `src/llm_router/policies/cost_aggressive.yaml` — a complete production-ready example
* `src/llm_router/policies/standard.yaml` — the historical default, mirrors the in-code `ROUTING_TABLE`
* [PROVIDERS.md](PROVIDERS.md) — provider env vars + setup
* [CHANGELOG.md v10.0.0](../CHANGELOG.md) — the policy YAML system, bandit, and OpenRouter all landed in v10
