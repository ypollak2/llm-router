# Security and privacy audit — 2026-09-22

Injection-tested against isolated temp stores. The real `~/.llm-router` was read
as evidence only.

---

## CRITICAL

### S-01 · `library/store.py::scrub_secrets` — a sixth table, live, 0644
Misses **Slack, JWT and Google API keys**; does not import `scrub_text` or
`persist_redact`. Reachable on every Bash/Edit tool call through the
`library-harvest` PostToolUse hook, which scrubs `tool_input["command"]` with it
before appending to `raw/events.jsonl`.

End-to-end reproduction (isolated tmp repo) wrote a Slack token and a JWT to
`raw/events.jsonl` in cleartext, byte-verified. `write_doc()` persists via
`tmp.write_text()` + `os.replace()` and is never chmod'd — **mode 644**, while the
codebase's own `private_opener` idiom exists for exactly this.

**The chain is realised, not theoretical:** `pack.py` later re-injects that
content verbatim as `additionalContext` into future prompts, which go to whichever
model answers next.

    untrusted tool input → weak scrub → world-readable file
    → re-injected context → remote model

### S-02 · `hooks/agent-route.py::_scrub_agent_prompt` — same gap, installed now
Byte-identical weak pattern list. Its comment claims "secrets in the prompt are
scrubbed before storage" — false for those three classes.

**Confirmed live on the audited machine:** registered as a `PreToolUse[Agent]`
hook in `~/.claude/settings.json`, and `agent_calls.json` was written at 08:37
today. The real file was read read-only: 50 stored calls, **0 currently matching
those shapes** — no live secret leaked, mechanism confirmed vulnerable.

The destination file *is* created 0600, so here the exposure is content, not
permissions.

---

## HIGH

### S-03 · `route_server.py` has no auth and no opt-in
`/route` and `/feedback` both trigger real routing and can trigger paid model
calls. `do_POST` checks `is_forbidden_cross_origin()` and nothing else; a grep for
`Authorization`/auth in the file returns nothing. The gateway's M-08 opt-in token
was never applied here, **despite this file's own comment acknowledging the
identical gap**. Same class, less mitigated.

### S-04 · `LLM_ROUTER_HOME` does not sandbox host-integration commands
`llm-router update`, run with `LLM_ROUTER_HOME` pointed at a tmp dir, copied 15
hook files into the real `~/.claude/hooks/` and reinstalled
`~/.claude/rules/llm_router.md` and the statusline script — all hardcoded to
`Path.home()`. Content verified byte-identical; nothing lost. But any isolated
test, CI job or audit that runs `install`/`update`/`dev-refresh` touches the
operator's real config, and this is what flipped three tests during this audit.

---

## MEDIUM

| ID | Finding |
|---|---|
| **S-05** | **`commands/sse.py`'s advertised auth does not exist.** It imports `llm_router.enterprise.identity/.oidc/.rbac`; that package is nowhere in the repository. `main_sse_secured()` raises `ModuleNotFoundError` before any socket binds. Fails **closed** — not exploitable — but `gateway.py:76` cites this very module as the precedent justifying its own auth being opt-in. **The precedent does not exist.** |
| **S-06** | **Gateway auth is off by default.** Mitigated by loopback bind and a global cross-origin guard, and the code is honest about the residual risk. Any local process can still spend money through it. Documented tradeoff, not a hidden one |
| **S-07** | **`verifiers.run_verifier` hands the full operator environment to a subprocess** (`env=dict(os.environ)`, including live API keys) that only checks an answer string, and the preamble's `read()` has no path confinement. **Not currently exploitable**: the only caller interpolates through a regex excluding quotes and separators, and no LLM-authored snippet is ever wired in. `safe_subprocess.py` already implements an env allowlist for this reason and this does not use it. A least-privilege violation waiting on the next caller |
| **S-08** | **The "in sync" fallback scrubber is not in sync** — `hooks/auto-route.py:2035` claims to mirror the canonical table and misses Slack, JWT, `pk-`/`rk-` and PEM. Three drifted because the 2026-09-21 fix widened canonical without updating the copy. Sink is `transcript_*.jsonl` |

---

## LOW

| ID | Finding |
|---|---|
| **S-09** | `session_store.py` uses `open()`-then-`chmod` where `private_opener` exists and five siblings use it — brief world-readable window at creation |
| **S-10** | `SECURITY.md` says 6 of 12 commands are refused by the allowlist; the reproduced count is 7. Its self-disclosed gaps (`cat ../../.ssh/id_rsa` still allowed; `agent_loop`'s docstring claims sandboxing that `run_command` does not honour) remain accurate and honest |

---

## What is genuinely well defended — verified, not assumed

* **The canonical scrubber's own coverage is real**, and is a genuine superset of
  every rival pattern table. The property test passes.
* **`persist_redact` fails closed**: any internal exception returns
  `[REDACTION-FAILED: content withheld]`, never the input.
* **`prompt_capture` fails closed end to end** — the whole body including the
  scrub sits inside one outer handler, so an import failure means no record
  rather than an unscrubbed one.
* **`scripts/groundtruth/scrub.py` refuses a weaker fallback** and raises instead
  — and verifiably does, rather than merely saying so.
* **`result_cache`, `semantic_cache`, `context` genuinely delegate** to
  `persist_redact` at their real write sites.
* **Safe defaults**: `persist_raw=False`, `persist_redaction=True`.
* **No `shell=True` or `os.system` anywhere**; all subprocess use is argv-list.
* **The cross-origin guard is global and ordered first**, before auth, in both
  HTTP surfaces — verified in the dispatch code, not the comment.
* **`org_policy` and `signals/pii` are legitimately-scoped detectors**, not
  drifted scrubbers: they flag and refuse, and never persist the matched value.
* **`main_sse` refuses a non-loopback bind** without an explicit opt-in, and is
  not registered as a console script.

---

## The privacy pattern

The canonical scrubber is correct and is now a true superset. **Two live call
sites do not use it**, and one of them writes world-readable files whose contents
are re-injected into later prompts.

The 2026-09-21 fix and its test both addressed the canonical function. Neither
addressed, or could have detected, a caller that never calls it.

---

## Not covered this pass

`control_plane/` auth and `signing.py`; `tools/fs.py` traversal boundaries;
`hooks/tool_intercept.py` injection surface beyond permissions; cache TTL and
retention windows. Flagged rather than left silent.
