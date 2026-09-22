# Security and privacy audit

v14.1.0 · 2026-09-21. Injection-tested against isolated temp stores; the real
`~/.llm-router` was inspected read-only as evidence.

---

## The canonical-scrubber claim

`secret_scrubber.scrub_text()` declares itself *"the single source of truth every
content store should call"* (CHZ-SEC-01).

**True for the stores that call it** — `result_cache`, `semantic_cache`,
`session_store`, `context`, `envelope`, `prompt_capture` all route through
`persist_redact()`/`scrub_text()` before disk. **Not true of the repo.** Three
paths bypass it.

---

## CRITICAL

### S-01 · `trace.py` writes raw prompts, model output and tool I/O
**0 scrubber references.** `emit()` truncates to 600 chars and writes verbatim:

| Call site | What lands on disk |
|---|---|
| `agent_loop.py:669` | the full user prompt |
| `agent_loop.py:728` | raw model output |
| `agent_loop.py:797` | raw tool arguments |
| `agent_loop.py:801` | raw tool results (file contents, command output) |
| `tools/local_task.py:224` | the task objective |

Injection-tested: an Anthropic key, an AWS key id, a `password=`, a home path, an
email and a public IP all survived **in full plaintext**. Created with the process
umask (no 0600 opener), no TTL, not covered by `commands/gc.py`. Gated behind
`LLM_ROUTER_TRACE=1` — a documented debugging flag.

### S-02 · `tool_intercept.py` writes raw shell commands — **live on this machine**
**0 scrubber references.** `_log_intercept()` writes the command (200-char
truncation) to `intercepts.jsonl`.

Verified on the audited machine: **mode `644`**, 177 rows, written during this
session. Today's contents are benign (`sed`, `cat`, `find`) but nothing would stop
a `curl -H "Authorization: Bearer …"` landing in plaintext, world-readable, with
no expiry.

**The hardening exists elsewhere and was not carried here:** `auto-route.py`'s
transcript shards are correctly `0600` via a private opener.

---

## HIGH

### S-03 · `error_sanitization.py` — a fourth, weaker scrubber
Misses Anthropic, OpenAI, GitHub, JWT and PEM patterns entirely — injection
showed all five passing through unredacted. It covers only AWS/Google keys, DB
strings and paths.

**0 callers.** Dead today, which is the only reason severity is not higher. But
`secret_scrubber`'s docstring claims the three drifted scrubbers were *unified*;
this one was **orphaned, not fixed**, and its name and docstring invite exactly
the wiring that would reintroduce the leak.

It also logs the pre-redaction original via stdlib `logging.debug(..., extra=…)`
— not surfaced under this project's own structlog config, but live under any host
application that renders LogRecord extras (Sentry breadcrumbs, `python-json-logger`).

### S-04 · Gateway and route_server have no request authentication
Bind is correctly gated — `net_bind.refuse_public_bind_or_exit` is wired and
defaults to `127.0.0.1`. **Per-request auth is absent**: `gateway.py` has one grep
hit for auth, a comment acknowledging the gap, while `commands/sse.py` requires
Bearer on every request (5 hits).

Any other local process — a malicious dependency, another user on a shared box, a
compromised browser extension hitting `localhost` — can trigger real billed model
calls.

---

## MEDIUM / LOW

- **S-05** `envelope.py::capture_repo_state` builds paths as `rp / rel` with no
  containment check, unlike the properly hardened `tools/fs.py::_assert_under_root`.
  Not exploitable today: the only caller extracts filenames with a `\b`-anchored
  regex that strips leading `../` and `/`. Defence-in-depth gap, not a live hole.
- **S-06** `LLM_ROUTER_PERSIST_RAW=1` disables redaction across
  result_cache/semantic_cache/session_store/context simultaneously. Documented,
  default off — but no startup warning banner.
- **S-07** `result_cache` retention: two TTL notions. `_TTL` governs read-time
  freshness; physical deletion is `LLM_ROUTER_PERSIST_TTL_DAYS` (default 30). So a
  "code" answer stops matching after 24h but its bytes persist up to 30 days.
  Setting the var to `0` disables purging entirely — an easy operator footgun.
- **S-08** Verifier snippets are executed as arbitrary Python via
  `subprocess.run([sys.executable, "-c", script])`. The model's answer is passed
  safely via env (no interpolation). Offline tooling only, gated by the
  `PROPOSED→VALIDATED→APPROVED→ACTIVE` lifecycle — but that gate is a **string
  comparison** (H-10), so an autonomous agent scripting the CLI with any `--by`
  value other than `"assistant"` passes it.

---

## Positive findings worth recording

- **No `shell=True` anywhere in production code.** `tools/local_task.py` documents
  and fixes a real prior command-injection primitive (`_run_check` used
  `shell=True` until 2026-09-14); now `shlex.split` + no shell.
- All real subprocess execution uses `asyncio.create_subprocess_exec`.
- `result_cache` hygiene is reference quality: `PRAGMA secure_delete=ON`, VACUUM
  after purge, 0600 enforcement on the db and its `-wal`/`-shm` sidecars.
- `config.load_disk_keys()` reads 0600 key files; its raw-key return value has
  zero callers outside its own module.
- The structlog scrubber is wired as the **first** processor, so field-name-based
  redaction applies before any renderer.
- No path found where model output becomes a fetched URL or an out-of-sandbox
  file read on the live request path.

---

## The exfiltration chain that is actually realised

```
user prompt or agent Bash command containing a credential
   → local model tool loop / bash interception
   → trace.jsonl or intercepts.jsonl, plaintext, mode 644, no TTL
   → readable by any local account, indefinitely
```

Both ends of this chain are opt-in flags, and one of them (`LLM_ROUTER_BASH_INTERCEPT`)
**was active during this audit**.

---

## Not covered

- `okf.py` knowledge-store retrieval for URL-fetch-from-model-output.
- `agentic/react.py` ReAct tool-call chain in depth.
- `semantic/store.py`, `semantic/traces.py` — persist code entities and retrieval
  traces; skimmed, not exhaustively checked for secret leakage via snippets.
- A full grep of every file writing to `~/.llm-router/*` — targeted by likelihood,
  not exhaustive.
