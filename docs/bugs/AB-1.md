---
id: AB-1
status: fixed in #334 (hook version 14)
---
## AB-1. Agent breaker called 4 parallel sibling spawns "nested agents" and blocked them

- **Symptom.** 2026-10-08 about 17:35Z, a top-level Claude Code session sent 6 Agent calls in
  one message. Calls 1-3 started; 4-6 were blocked with `Agent loop circuit breaker: depth 3/3.
  Too many nested agents.` No agent was nested. Minutes later single calls passed (n = 1
  incident, reported by the owner; the installed hook was byte-identical to the repo's
  `agent-route.py` at version 12).
- **Cause.** `agent-route.py` kept ONE per-session counter in `agent_depth_<session>.json`:
  +1 at PreToolUse[Agent], -1 at PostToolUse (`agent-depth-release.py`). That is agents in
  flight, but it was compared with `LLM_ROUTER_MAX_AGENT_DEPTH` (3) and reported as nesting
  depth. The 4th concurrent sibling saw 3 >= 3.
- **Fix.** Nesting depth is now taken from the payload: no `agent_id` is the top-level session
  (depth 0); a caller with `agent_id` is a subagent whose depth comes from a registry in the same
  file. PreToolUse queues the child's depth (`pending`); `subagent-start.py` claims it for the
  new `agent_id` (`agents`). A spawn is blocked when child depth > `LLM_ROUTER_MAX_AGENT_DEPTH`
  (message: "Agent nesting limit"). An unknown `agent_id` counts as depth 1, never deeper. The
  in-flight count stays as a separate runaway cap, `LLM_ROUTER_MAX_CONCURRENT_AGENTS` (default
  16; message: "Too many agents in flight"). The release hook keeps the registry keys.
  Hook versions: agent-route 14, agent-depth-release 3, subagent-start 4. Needs all three deployed.
- **Rule.** A guard's counter and its limit must measure the same quantity, and the message must
  name what was counted. A hook that cannot see a fact (the parent) says so and stays permissive
  rather than inferring it from a proxy.
- **Test.** `TestBreakerMeasuresNestingNotSiblings` in `tests/test_agent_route_hook.py`:
  six top-level siblings (fails on the old hook: call 4 blocked), real depth 3 -> 4 still trips,
  concurrency cap, and the SubagentStart claim.

