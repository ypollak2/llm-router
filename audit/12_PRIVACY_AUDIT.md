# Phase 26 — Privacy Audit: can secrets/PII reach durable storage?

HEAD `357a402e`, branch `fix/audit-2026-09-22`. All probes ran under
`export LLM_ROUTER_HOME=$(mktemp -d)`, `LLM_ROUTER_BASH_INTERCEPT=off`,
`.venv/bin/python`. Canaries are synthetic, clearly labelled
(`CANARY<n>`), correctly shaped for the pattern under test, never real
credentials. Probe script:
`/private/tmp/claude-501/.../scratchpad/privacy_probe.py` (session-local
scratchpad, not in the repo).

## Method

1. Read every scrubber in the tree (found **6 live pattern tables**, not
   the 4 the 2026-09-22 audit counted — see below).
2. Called each durable-storage write API directly with a canary set
   covering: OpenAI/Anthropic/GitHub/Google keys, AWS key-id + secret
   (labelled and bare), JWT, Slack token, PEM block, bearer token,
   password, email, home path, username, private-repo URL, customer id,
   phone number, Postgres DSN-with-password.
3. Recursively grepped the isolated `LLM_ROUTER_HOME` (`grep -rnaiob`,
   binary-safe) for every canary, and queried `usage.db` /
   `result_cache.db` directly with `sqlite3`.
4. Captured one live HTTP POST from `llm_router.alerts.emit_alert` with a
   local `http.server` to prove machine-leaving exfiltration.

## Scrubber inventory (measured, not documented)

| Module | Role | Delegates to canonical? |
|---|---|---|
| `secret_scrubber.py` (`scrub_text`) | canonical credential scrubber | — (is the canonical) |
| `persist_redaction.py` (`persist_redact`) | PII + credentials, used by result_cache/session_store/semantic_cache | yes, layered on top of its own patterns |
| `scripts/groundtruth/scrub.py` (`scrub`) | credentials + identity/location, used by prompt_capture/GT pipeline | yes, first layer |
| `library/store.py` (`scrub_secrets`) | body text for Library docs | yes (confirmed, `test_m07_no_second_scrubber.py` passes) |
| `hooks/agent-route.py` (`_scrub_agent_prompt`) | agent journal prompt field | yes (confirmed, same test) |
| `plugins/redaction.py` | shared `RedactionResult` type only, no patterns | n/a |
| `org_policy.py` / `signals/pii.py` | **detectors**, not scrubbers (reject-at-load / force-local-routing) — legitimately broader/narrower | n/a |

Six independent pattern tables exist (not four), three of which
(`secret_scrubber`, `persist_redaction`, `groundtruth/scrub`) all claim
credential coverage. `persist_redaction.py` does **not** delegate its
credential patterns to `secret_scrubber` — it runs its own copy first,
then layers `secret_scrubber.scrub_text` on top as a second pass. This
is currently harmless (its own patterns are a superset for the classes
tested) but is exactly the shape that drifted before (CHZ-SEC-01).

## Canary survival — confirmed raw in durable storage

| Store (file) | Perms | Canary that survived | Field | Evidence |
|---|---|---|---|---|
| `usage.db` → `execution_events` | 0600 | Full OpenAI key, labelled AWS secret, full JWT | `rejection_reason`, `fallback_reason`, `metadata` | `sqlite3` dump: `rejection_reason='ConnectionError: sk-proj-CANARY1AAA...leaked...for canary12.synthetic@...'`, `fallback_reason='auth_error: token aws_secret_access_key=CANARY5bbb...'`, `metadata='{"detail": "eyJhbGci...CANARY6sigpart"}'` |
| `attempts.jsonl` | **0644** | Full Anthropic key | `reason` | byte offset 145: `sk-ant-api03-CANARY2AAA...` |
| `auto-route-debug.log` | **0644** | Full GitHub PAT | raw message | byte offset 106: `ghp_CANARY3AAA...` |
| external webhook (`LLM_ROUTER_ALERT_WEBHOOK`) | n/a (leaves the machine) | Postgres DSN with password | `detail.error` | HTTP POST body captured: `{"event":"budget_postgres_fallback",...,"detail":{"error":"connection failed: postgresql://dbuser:CANARY-DB-SECRET-pw123@db.internal:5432/billing"}}` — same string also echoed unredacted to the structlog `critical` line |
| `result_cache.db` (`results` + `results_fts*`) | 0600 | Home path `/Users/canary13user/...`, username, customer id | `user_prompt` (main table **and** FTS shadow table) | row dump shows path/username/customer-id fields untouched while all 10 credential-shaped fields in the same prompt were correctly redacted |
| `projects/.../session_context_*.jsonl` (session_store) | 0644 | Home path, username, customer id | event `content` | same three fields, same gap, same root cause (`persist_redaction` has no HOME/username/customer rule) |
| `prompt_capture.jsonl` / `ground_truth_candidates.jsonl` | 0644 | username, customer id, private-repo URL(by design) | `prompt` | home path *was* redacted here (`<HOME:e9009de8>`) — this is the one store with HOME coverage |
| `fail_open.jsonl` | 0600 | username | `d` (`detail`) | `{"c":"CHZ-PROBE-TEST","e":"ValueError","d":"{'model': 'probe', 'note': 'username: canary14operator'}"}` |
| `semantic_cache` table (`usage.db`) | 0600 | Digits from phone + customer-id | `discriminator` | `_discriminator()` called directly: `{"nums": ["0099887", "14155550117", "16", "17"], "pol": []}` — its own docstring says "never prompt text" |

Bare (unlabelled) AWS secret-access-key shape confirmed unredacted by
**all three** scrubbers (`scrub_text`, `persist_redact`,
`groundtruth.scrub`) — deliberate per code comments (classed as a
detector's job, not a scrubber's), but it means this exact credential
shape reaches every store below untouched whenever it's pasted without
its `aws_secret_access_key=` label. Well-formed `AKIA...` key-ids,
Stripe keys, Slack tokens, DB-URL-with-creds *are* caught by
`persist_redaction`/`groundtruth.scrub`, but **DB-URL-with-inline-creds
is absent from `secret_scrubber.SECRET_PATTERNS` entirely** — which is
why the Postgres-DSN webhook leak above happened even in the structlog
line, independent of the webhook bug.

## Key questions answered

- **Before or after persistence?** Every leak found is *absence* of
  scrubbing, not a before/after ordering bug. `execution_ledger.py` has
  **zero** scrub/redact calls anywhere in the file (confirmed by grep).
  `attempt_log.py`, `hooks/auto-route.py:_debug_log`, and
  `alerts.emit_alert` are the same: no scrubber is ever invoked on these
  code paths. Where scrubbing exists (`result_cache`, `session_store`,
  `semantic_cache` response, `prompt_capture`), it correctly runs
  *before* the write, and fails closed to a placeholder on error.
- **Scrub failure swallowed → raw write anyway?** No — every scrubbed
  path (`persist_redact`, `groundtruth.scrub`) fails closed to
  `[REDACTION-FAILED: content withheld]` / disabled capture, never to
  the raw string. The bug class here is a field that is **never routed
  through any scrubber at all**, which a "does scrubbing fail safely"
  test cannot catch — it has to check the call site, exactly as
  `test_m07_no_second_scrubber.py`'s newer T-04 section already does for
  two other sites (both now fixed).
- **Real leak vector for `execution_ledger` / `attempt_log` / debug log**:
  `f"{type(exc).__name__}: {exc}"[:120/200]` patterns in `router.py`
  (lines ~3294, ~3477) and `hooks/direct_executor.py:665`, and
  `f"...{exc!r}"` in `hooks/auto-route.py` (lines 371, 1479, 3729). Any
  provider/Ollama exception whose message echoes request/response
  content (auth failures often quote the bad key back; connection
  errors often quote the URL) lands here verbatim.
- **Redaction-on-read?** None found anywhere. Content written before a
  rule existed, via the `LLM_ROUTER_PERSIST_RAW=1` escape hatch, or
  through any gap above stays raw forever; no store re-scrubs on read.
- **File permissions at creation:** `usage.db`, `result_cache.db`,
  `fail_open.jsonl` are 0600 from creation (confirmed). `attempts.jsonl`
  and `auto-route-debug.log` are 0644 (world-readable) **and** are two
  of the four confirmed raw-credential stores.
- **Escape hatches:** `LLM_ROUTER_PERSIST_RAW=1` /
  `LLM_ROUTER_PERSIST_REDACTION=off` disable all scrubbing for
  `result_cache`/`session_store`/`semantic_cache` in one env var, by
  design ("trusted local debugging only" — nothing enforces that).

## Findings

| # | Severity | Confidence | Finding |
|---|---|---|---|
| F1 | CRITICAL | CONFIRMED | `execution_ledger` (`usage.db`) persists full credentials (API keys, JWTs, labelled AWS secrets) raw via `rejection_reason`/`fallback_reason`/`metadata` — zero scrubbing anywhere in `execution_ledger.py`. |
| F2 | CRITICAL | CONFIRMED | `llm_router.alerts.emit_alert` POSTs its `detail` dict to an external `LLM_ROUTER_ALERT_WEBHOOK` with zero scrubbing; `budget_backend.py`'s Postgres-fallback call site feeds it `str(err)`, which can and did (probe) carry a full DSN with password. This is the one finding that leaves the machine. |
| F3 | HIGH | CONFIRMED | `attempts.jsonl` (world-readable, 0644) persists raw exception text via `attempt_log.record(reason=...)`; real call site `direct_executor.py:665` feeds it provider exception messages. |
| F4 | HIGH | CONFIRMED | `auto-route-debug.log` (world-readable, 0644) persists raw `{exc!r}` via `_debug_log`, including from live Ollama-classification failures — no scrub call exists in `_debug_log` at all. |
| F5 | MEDIUM | CONFIRMED | Home-directory paths (identity) and free-text usernames/customer ids are unscrubbed in every store that isn't the Ground-Truth capture path — `persist_redaction.py` (used by result_cache/session_store/semantic_cache) has no HOME rule and no username/customer-id rule anywhere in the tree except the opt-in, off-by-default denylist. |
| F6 | MEDIUM | CONFIRMED | `semantic_cache._discriminator()` extracts raw digit runs (phone numbers, partial card/account numbers, numeric customer ids) from the prompt into the persisted `discriminator` column, contradicting its own docstring ("never prompt text"). |
| F7 | MEDIUM | DESIGN RISK | Bare/unlabelled AWS secret-access-keys pass all three scrubbers by deliberate design; every store above inherits this gap. |
| F8 | LOW | DESIGN RISK | `failopen.record(detail=...)` has no scrubbing contract — safe only because today's two call sites pass `str(model)`; a future caller passing exception/prompt text would leak silently (confirmed the raw write itself with a canary). |
| F9 | LOW | DESIGN RISK | `LLM_ROUTER_PERSIST_RAW=1` disables all redaction for three durable stores from one env var with no additional gate. |
| F10 | INFO | INVALIDATED (fixed) | The previously-reported T-04/F-11 drift (`library/store.scrub_secrets`, `hooks/agent-route._scrub_agent_prompt` not delegating) is fixed at HEAD — `tests/security/test_m07_no_second_scrubber.py` passes (16/16). Ground-Truth pipeline (`prompt_capture` → `accumulate` → `envelope`) correctly re-scrubs at every stage and fails closed. |
