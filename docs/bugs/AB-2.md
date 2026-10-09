---
id: AB-2
status: fixed in this change (agent-route 15)
---
## AB-2. Agent breaker state: lost updates, a leaked pending entry, no cleanup

Review findings on #334 (AB-1), each reproduced before it was fixed.

- **Symptom.** (a) 12 parallel PreToolUse[Agent] hooks left the in-flight count at 12, 8 and 10
  and `pending` at 12, 4 and 8 on three reviewer runs; the new test lost an update in 20 of 20 runs
  on 070f94f9 (in-flight or pending short of 12, as low as 1). (b) A Codex-delegated spawn (block branch) rolled the count
  back but kept its `pending` entry; the next unrelated SubagentStart claimed it, so a fresh
  depth-1 agent could inherit depth 3 and have a legitimate child blocked as depth 4. (c)
  `_drop_pending` dropped the newest entry, not the caller's. (d) `agent_depth_<session>.json`
  was never removed.
- **Cause.** Every write was `write_text` on a read-modify-write with no lock; the pending
  entry had no identity; the Codex branch did not call `_drop_pending`; the budget blocks and
  the final "route to a cheap model" block (nothing spawns, no PostToolUse follows) rolled back
  neither the count nor the entry.
- **Fix.** Hook versions: agent-route 15, agent-depth-release 4, subagent-start 5,
  session-end 23 (all four must be deployed; an older subagent-start ignores the new 3-field
  entries and never claims them).
  - Pending entries are `[ts, depth, token]`; token is the payload's `tool_use_id`, else a uuid.
    Drop is by token. Readers accept entries of 2 or more fields.
  - All three writers do a locked read-modify-write: `fcntl.flock` on a sidecar
    `agent_depth_<session>.json.lock`, polled non-blocking for at most 0.25 s (the hooks' 300 ms
    budget; `LLM_ROUTER_BREAKER_LOCK_WAIT_S` overrides it, the contention tests set 10 for 2-vCPU CI), then atomic replace (tmp + `os.replace`, mode 0600). On lock failure the hook logs
    to stderr and proceeds unlocked, as before: it fails open and never stalls a spawn.
    The in-flight count changes by delta inside the lock, not by absolute value.
  - Every exit that spawns nothing (Codex, direct, CLI delegation, both budget blocks, the final
    routed block) gives back its slot and its own pending entry in one locked update.
  - session-end.py removes the state and lock file on the SessionEnd event (a SessionEnd hook
    exists: session-end.py is registered on Stop and SessionEnd). The 200/200 caps on `pending`
    and `agents` still bound a session that never ends cleanly.
- **Limit that stays.** Without the SubagentStart hook (`subagent-start.py`) nothing claims the
  queued depths, so every subagent counts as depth 1 and only `LLM_ROUTER_MAX_CONCURRENT_AGENTS`
  bounds recursion; `LLM_ROUTER_MAX_AGENT_DEPTH` above 1 cannot trip. `llm-router doctor` now
  warns when agent-route is registered in settings.json and SubagentStart is not (it already
  reported a missing hook file).
- **Rule.** Shared hook state that several processes write needs a lock and an atomic replace;
  a queue entry needs an identity so the owner can retract exactly its own.
- **Test.** `tests/test_agent_breaker_state.py`: `test_12_parallel_pretooluse_hooks_lose_no_update`
  (20 runs), `test_parallel_release_and_claim_lose_no_update` (20 runs),
  `test_codex_delegation_drops_its_pending_entry`,
  `test_drop_pending_removes_this_spawns_entry_not_a_siblings`,
  `test_lock_unavailable_fails_open_and_logs`, FIFO / TTL / registry / depth-0 / drop-on-block
  coverage tests, `test_session_end_removes_breaker_state_and_lock`, doctor tests.

