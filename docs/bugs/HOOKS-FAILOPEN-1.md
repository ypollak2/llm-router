---
id: HOOKS-FAILOPEN-1
status: fixed in `fix/hooks-fail-open-non-dict`
---
## HOOKS-FAILOPEN-1. Hooks crashed (rc=1, traceback) on a non-object JSON payload

- **Symptom.** With `[]`, `"x"` or `123` on stdin, `agent-depth-release`, `agent-route`, `cc-usage-track`, `enforce-route`,
  `subagent-start` and `usage-refresh` exited 1 with a traceback (`AttributeError: 'list' object has no attribute 'get'`);
  `bash-compress` and `playwright-compress` crashed on `123` (`argument of type 'int' is not iterable` in the payload helper);
  `usage-refresh` also crashed on invalid JSON (`JSONDecodeError`). Found in the independent review of #379; pre-existing on main.
- **Cause.** `json.loads` succeeds on any JSON value, and the code then called `.get` (or `in`) on it. The PG9 `set_session` block
  already tested `isinstance(payload, dict)`, but nothing after it did. `[]` and `"x"` only survived in the two compress hooks by luck
  (`in` works on list and str).
- **Fix.** After the payload is parsed, a non-dict payload exits 0 silently (the same outcome as an empty payload); `usage-refresh`
  also treats invalid JSON as a no-op. Eight hooks changed, versions bumped: agent-depth-release 6 -> 7, agent-route 18 -> 19,
  bash-compress 3 -> 4, cc-usage-track 4 -> 5, enforce-route 16 -> 17, playwright-compress 3 -> 4, subagent-start 7 -> 8,
  usage-refresh 4 -> 5. `hooks/` and `src/llm_router/hooks/` stay byte-identical.
- **Test.** `tests/test_hooks_fail_open_malformed_stdin.py` runs every `hooks/*.py` as a subprocess (scratch HOME and
  LLM_ROUTER_HOME) with stdin in `[]`, `"x"`, `123`, `{}`, empty, `not json`; asserts rc 0 and no traceback. A floor test fails if
  the hook set is empty. No timing assertions.
