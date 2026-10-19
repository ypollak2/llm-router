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

Claude authenticates by one of: macOS Keychain (account from `USER`), `CLAUDE_CODE_OAUTH_TOKEN`
passed through, or `P20_CLAUDE_CREDENTIALS_FILE` copied to `<scratch>/.claude/.credentials.json`
(0600). Not verified here (no benchmark or `claude` call was run): whether Keychain lookup works
under a scratch HOME. Run one smoke `claude -p` call with the scratch env before the freeze.

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
* The hidden-test runner (`p20_code_runner`, a grader, not an agent) keeps the caller PATH.
* Because `~/.local/bin` is dropped, a `claude` installed only there will not resolve in the
  agent env and the call fails closed (`claude_error`).

### Pre-freeze smoke checklist (not run by F104 or F110)

1. One `claude -p` call with the scratch env (`claude_env(scratch_home(...), run_dir)`): confirm
   Keychain/OAuth auth works under the scratch HOME and that `claude` resolves on the filtered
   PATH. Unverified; it fails closed.
2. One Pi dry run with `agent_env`: confirm `node` resolves on the filtered PATH.
3. Print `agent_path(os.environ["PATH"])` and confirm no llm-router or `~/.local/bin`.

## Prereg

PREREG-P20-requal section 6 says children run "with the real HOME (auth only)". F104 supersedes
that line. Any new P2.0 amendment (and the P20T techop sidecar, which imports `p20_requal`) must
reference F104 and re-pin `CODE_RUNNER_SHA256` / the repo SHA.
