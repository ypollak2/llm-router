---
id: ROUTE-CONT-1
status: fixed in `fix/route-continuation-shadow` (owner decision D-57)
---
## ROUTE-CONT-1. A second `_CONTINUATION_RE` shadowed the strict-ack pattern, so directives bypassed routing

- **Symptom.** The UserPromptSubmit bypass is documented as "strict ack only" and logs
  `CONTINUATION: bypass to host agent (strict ack)`, but prompts such as the 33-character
  "keep going, open the PR when done" took it. They skipped the `user_prompt` session record and
  routing.
- **Cause.** `hooks/auto-route.py` (and its identical copy `src/llm_router/hooks/auto-route.py`)
  bound `_CONTINUATION_RE` twice at module level: a strict single-word acknowledgement pattern
  (~L2715) and, from commit 9a370e69 (audit/28 D-1), a broad directive pattern (~L3717: "keep going",
  "fix it", "push it", ...). Python keeps the later binding, and names resolve at call time, so every
  call site, including the bypass, used the broad one.
- **Fix.** Two names: `_STRICT_ACK_RE` (bypass in `main`, `_is_continuation`) and
  `_CONTINUATION_DIRECTIVE_RE` (`_is_context_dependent`, `_is_short_code_followup`). Pattern contents
  unchanged. Side effect: "no", "great", "stop" and the other strict-only words now bypass, as the
  strict pattern always intended; they did not under the broad pattern.
- **Test.** `tests/test_route_cont_1_regex_shadow.py`: the directive is not bypassed, single-word acks
  are, the directive call sites keep their behaviour, and an AST guard fails if any module-level name
  is assigned twice in either hook copy. On `origin/main` (2f001d53) 5 of its tests fail.
