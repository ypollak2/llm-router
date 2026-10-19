# F104: benchmark coding agents run with a scratch HOME

Status: code change, not a prereg amendment. Frozen preregs and their `.sha256` sidecars are not edited.

## Why

During P2.0 code-arm runs the coding agents ran with the owner's real HOME and twice performed an
`llm-router uninstall`-shaped deletion of the live install (F86 2026-10-18: proxy LaunchAgent
plists and `proxy_default.json`; F86-2 2026-10-19: `~/.claude/settings.json` hook wiring,
statusLine, `rules/llm_router.md`). Cause: `claude_env()` in `scripts/p20_requal.py` copied
`os.environ` (real HOME), and Pi's `lib_pi.run_pi` does `dict(os.environ)`.

## What changed

* `agent_env()` / `claude_env(home)` build an allow-listed env: `HOME=<scratch>`, `PATH`, `LANG`,
  `LC_ALL`, `USER`, `LOGNAME` (USER is the macOS Keychain account name), and for Claude only
  `LLM_ROUTER_SESSION_KIND=research` plus auth. Nothing else, so no `LLM_ROUTER_*`,
  `ANTHROPIC_*`, or other keys leak.
* Scratch HOME: `<run_dir>/agent_home/<arm>/<item>/a<attempt>` (fresh, 0700, outside the work
  tree); the Claude Q&A CLI uses `<run_dir>/claude_home`.
* Pi: the helper lives outside the repo (hashed research code), so the runner swaps `os.environ`
  for the call (`scrubbed_environ`) and restores it.
* Guard: `assert_agent_env` refuses (exit 2) when HOME is the real HOME (user database, via
  `pwd`) or the caller's `$HOME`, or when an unlisted key is present. There is no
  `--i-accept-real-home` flag: the markers always exist on the owner's machine, so a
  marker-based refusal would be a standing prompt; refusing the env itself has no override to misuse.

## Auth (never printed or logged)

Claude authenticates by one of: `CLAUDE_CODE_OAUTH_TOKEN` passed through, or (F113)
`P20_CLAUDE_CREDENTIALS_FILE`. Under the scratch HOME the macOS Keychain is not visible:
`claude -p` answers "Not logged in" (confirmed 2026-10-19), so the Keychain is not a usable path.

### Owner step (F113)

1. Run `claude setup-token` in a terminal and put the printed token in
   `~/.llm-router-bench/claude-oauth-token` (suggested path), mode 0600
   (`chmod 600`; the directory 0700).
2. Export `P20_CLAUDE_CREDENTIALS_FILE=~/.llm-router-bench/claude-oauth-token` for the harness.

The harness reads the file and sets `CLAUDE_CODE_OAUTH_TOKEN` only in the Claude child's env
(`claude_env`, not the Pi env). It never logs, prints or writes the value (nothing is copied into
the scratch HOME; the earlier copy to `.claude/.credentials.json` is gone). It refuses, naming no
path and no value, when the file is group/world accessible, empty or unreadable.

## Limits

A scratch HOME is not a sandbox. An agent can still use absolute paths or `pwd` lookups. Stronger
isolation (`sandbox-exec` or a separate OS user) is not part of F104 or F110.

### F110 hardening (rv-642 residual)

* PATH: the agent PATH is an allow-list built by `agent_path()`: `/usr/bin /bin /usr/sbin /sbin`
  plus caller directories that hold node, npm, npx, git, python, python3 or uv. Any directory
  holding an `llm-router` or `llm_router` executable is dropped (so the run's venv bin
  is gone), as are `~/.local/bin` and the uv tool dirs. A directory with both node and
  llm-router is dropped whole and fails closed (the Pi dry run shows it). `assert_agent_env`
  refuses (exit 2, no secret named) if `shutil.which("llm-router")` still resolves on that PATH.
* `launchctl` remains reachable (it lives in `/bin`). The threat is `llm-router uninstall`
  calling it against the user's launchd domain, which HOME does not scope; removing llm-router
  from PATH is the guard. An agent that calls `launchctl` directly by name is not prevented.
* Not covered: the legacy-brand CLI is not on the deny list (the identity gate forbids its name
  in runtime code); it is only dropped when its directory is dropped for another reason.
* HOME: `assert_agent_env` also refuses a HOME inside `real_home()` unless it is under the run dir.
* The hidden-test runner (`p20_code_runner`, a grader, not an agent) kept the caller PATH in F110; F113 replaces that (below).
* Because `~/.local/bin` is dropped, a `claude` installed only there will not resolve in the
  agent env and the call fails closed (`claude_error`).

### F113 hardening (rv-643 residual)

* Import guard: `assert_agent_env` runs every `python` / `python3` that resolves on the agent PATH
  as `python -I -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('llm_router')
  is None else 1)"` with the agent env, and refuses (exit 2) if any can import `llm_router`
  (exit 1 or a probe timeout both refuse). `PYTHONNOUSERSITE=1` is in the agent env and the
  allow-list.
* Hidden-test grader: `p20_code_runner` now runs agent-written code with `agent_path(...)` as PATH
  and an interpreter picked by `grader_python`: the first python on that PATH that passes the
  import guard and can `import pytest`. It no longer uses the run venv python. Consequence:
  the owner needs a python on the filtered PATH that has pytest and not llm_router (for example a
  Homebrew python with pytest); otherwise the grader refuses (fails closed) instead of grading.
* uvx note (not guarded): `uvx` / `uv tool run` can fetch `llm-router` from the index into a cache
  under the scratch HOME. That leaves the owner install untouched, but the agent could then run a
  fresh copy. `uv` stays on the PATH because the agents need it for their own tasks.

### Pre-freeze smoke checklist (F113; none of it was run by F104, F110 or F113's build)

Helper: `python scripts/p20_prefreeze_smoke.py [--claude-call]` prints `PASS`/`FAIL` per check and
nothing else. Exit 0 when all pass.

1. `claude -p` under the scratch HOME with the token (owner-run, spends one call):
   `P20_CLAUDE_CREDENTIALS_FILE=... python scripts/p20_prefreeze_smoke.py --claude-call`
   checks `claude_token` (file mode and content) and `claude_call` (one real call).
2. Pi node resolution: `node_on_agent_path` confirms `node` resolves on the filtered PATH.
3. Import guard: `import_guard` confirms at least one python is on the agent PATH and none
   imports `llm_router`.
4. Print `agent_path(os.environ["PATH"])` and confirm no llm-router or `~/.local/bin`.

## Prereg

PREREG-P20-requal section 6 says children run "with the real HOME (auth only)". F104 supersedes
that line. Any new P2.0 amendment (and the P20T techop sidecar, which imports `p20_requal`) must
reference F104 and re-pin `CODE_RUNNER_SHA256` / the repo SHA.
