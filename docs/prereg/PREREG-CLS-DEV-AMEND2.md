# PREREG-CLS-DEV AMEND2: Codex runs with every local-file-reading tool disabled via a dedicated CODEX_HOME (D-283, D-284)

This file is frozen only when `PREREG-CLS-DEV-AMEND2.sha256` exists beside it and
`scripts/check_prereg.py docs/prereg/PREREG-CLS-DEV-AMEND2.md` reports a match; until then it is a draft. It
amends `PREREG-CLS-DEV.md` (r5, sha256 `6d173a1c081579f4decc82e8024f2814b2a24477052f2580c8f21dcb38ffc795`, frozen_utc
2026-10-22T16:02:26Z), which stays byte-identical, and sits beside `PREREG-CLS-DEV-AMEND1.md`, which also stays
byte-identical. It changes only the items under "What changes". Every frozen script (`cls_eval.py`,
`cls_dev_run.py`, `cls_dev_lib.py`, `cls_label_run.py`, `cls_gen_run.py`, `cls_resemble.py`, `cls_send_lib.py`,
`cls_pins.py`), every threshold and floor, the `codex exec` argv, and the PINS-STATIC file stand.

## Authority

D-283 (owner decision, 2026-10-25). The rule itself is the owner's: every CLS runner invocation that spawns
`codex exec` sets `CODEX_HOME` to a dedicated directory, `~/.codex-cls`, whose config turns off Codex tools.
D-284 (coordinator decision, within D-283): the config disables **every** default-on tool that can read local
files, not only the shell. The author chose the details that implement it, each disclosed below: the exact
`config.toml` bytes, the auth handling, the session-log directory, the logging of the path and the config hash, and
the reading of the canary.

## Deviation and evidence (counts only)

* **What happened.** The DEV-SYN v1 generation run (`cls_gen_run.py run`, frozen) parked with exit 11, "codex
  read-scope canary: the token appeared in the output; parked", on 2026-10-25, **before any generation call**.
  Nothing was sent to a generator and $0 was spent. PREREG §3.2 ("Call hygiene") says a token in the output parks
  the run and "the exposure is then a decision, not a default". This amendment is that decision.
* **Why the run cannot simply be resumed.** The frozen code re-runs the canary on every start and resume
  (`scripts/cls_gen_run.py` line 703-704, `S.run_codex_canary(...)`; `cls_label_run.py` line 657 does the same). GPT
  generation and GPT labels therefore cannot proceed while the canary reads the token.
* **Root cause (researched against codex-cli 0.161.0, the installed version; all paths below are
  `codex-rs/...` at tag `rust-v0.161.0` of github.com/openai/codex).**
  * In `-s read-only`, `SandboxPolicy::ReadOnly` maps to full-disk read (`protocol/src/permissions.rs:2046-2056`).
    The macOS Seatbelt policy for it is `(allow file-read*)` (`sandboxing/src/seatbelt.rs` ~986-988).
  * The shell tool is **not the only read path**. Four default-on tools can read local files and the sandbox does
    not restrict them: `exec_command`/`write_stdin` (shell), `view_image`, the `image_gen.imagegen` tool
    (reference images) and the `spawn_agent` family (`local_image` input items). Their keys and sources are in
    the tool inventory below. A fifth, `apply_patch`, reads files to verify a patch but never returns their
    contents; it has no disable key (see the inventory and Residuals).
  * The first four can be turned off in `config.toml`. The documented key `[features] shell_tool = false` covers only the
    shell (https://learn.chatgpt.com/docs/config-file/config-reference); the other keys come from the source.
  So §3.2's parenthesis "(a read-only sandbox may still read the filesystem)" is what happened: the sandbox did not
  restrict reads, and the model had tools to read with.
* **The frozen argv cannot change.** `codex exec -s read-only --skip-git-repo-check --ephemeral -C <empty cwd>
  [--json] -m <pin>` is pinned in PINS-STATIC, as are the binary path and the CLI help-text hash. This amendment
  changes none of them.

## What changes

1. **Rule.** Every CLS runner invocation that spawns `codex exec` (generation, labels, the canary, and verify) runs
   with the environment variable `CODEX_HOME=<absolute path>`, the path being the dedicated directory
   `~/.codex-cls` expanded to an absolute path. The variable is exported in the shell that starts the runner; no
   script changes. The frozen child env (`cls_send_lib.codex_child_env`, lines 483-488) copies `os.environ` and
   pops only `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY` and `OPENAI_API_KEY`, and `child_call` chooses that env at
   line 577 and passes it to `send` at line 580 (lines 577-580), which passes it to `subprocess.run(..., env=env)`
   (lines 559-560). The canary reaches the same path (`run_codex_canary`, lines 713-715, calls `child_call`). So
   `CODEX_HOME` reaches every Codex child.
2. **The directory is created with only two entries; Codex writes runtime state (tmp/, logs, sessions) into it.**
   * `config.toml`, containing exactly these bytes (LF line endings, one trailing newline, 19 lines, 321 bytes):

     ```
     cli_auth_credentials_store = "file"
     web_search = "disabled"

     [features]
     shell_tool = false
     view_image = false
     image_generation = false
     multi_agent = false
     apps = false
     plugins = false
     tool_suggest = false
     goals = false
     sleep_tool = false

     [agents]
     enabled = false

     [tools.experimental_request_user_input]
     enabled = false
     ```

     sha256 of those bytes: `203b7e933cb8496a1448450a733e30c4a4091b8e1c03f45d50a083fe4f9feb87`.
     Key by key (all from `codex-rs` at `rust-v0.161.0`):
     * `cli_auth_credentials_store = "file"`: pins file-based auth (`config/src/config_toml.rs:290`; enum
       `config/src/types.rs:113-126`, default `File`), so the auth handling in item 4 applies.
     * `web_search = "disabled"`: top-level key (`config/src/config_toml.rs:471-472`); the default mode is `Cached`
       (`protocol/src/config_types.rs:376-381`).
     * `[features]`: `shell_tool`, `view_image`, `image_generation`, `multi_agent`, `apps`, `plugins`,
       `tool_suggest`, `goals`, `sleep_tool` (registry `features/src/lib.rs`, lines in the inventory below).
     * `[agents] enabled = false` (`config/src/config_toml.rs:753-756`).
     * `[tools.experimental_request_user_input] enabled = false` (`config/src/config_toml.rs:695-709`).
   * `auth.json`, a symbolic link, see item 4. This amendment records neither its contents nor its hash.
3. **Tool inventory.** Every model-callable tool registered by `codex exec` at 0.161.0 with a fresh `CODEX_HOME`,
   from `core/src/tools/spec_plan.rs` (core tools 1016-1035, extension tools 1485-1513, hosted 622-655) and the
   feature registry. "Reads local files" means: can return or leak the content or existence of a local path.

   | Tool | Default in exec | Reads local files? | Disable | Source |
   |---|---|---|---|---|
   | `exec_command`, `write_stdin` (shell) | on | **yes**, any path the sandbox lets it read: all of the disk in `read-only` | `[features] shell_tool = false` | `spec_plan.rs:1079-1090`; `features/src/lib.rs:986-989` |
   | `view_image` | on | **yes**, returns the bytes of any image file; its errors reveal whether a path exists | `[features] view_image = false` | `spec_plan.rs:1283`; `features/src/lib.rs:992-995`; `core/src/tools/handlers/view_image.rs:159-183` |
   | `image_gen.imagegen` | on when ChatGPT auth, plan not Free, and the model accepts images | **yes**, `referenced_image_paths` (max 5) are read from disk and sent out | `[features] image_generation = false` | `spec_plan.rs:727-735, 1501-1505`; `features/src/lib.rs:1612-1615`; `ext/image-generation/src/tool.rs:95, 452, 559-566` |
   | `spawn_agent`, `wait_agent` and the other collab tools | on: the bundled catalog entry for the pinned model sets `multi_agent_version` v2 | **yes**, an input item of type `local_image` carries a path that the child reads | `[agents] enabled = false` **and** `[features] multi_agent = false` | `core/src/tools/handlers/multi_agents_spec.rs:575, 592-594`; `spec_plan.rs:672-684`; `core/src/config/mod.rs:1595-1621`; `config/src/config_toml.rs:753-756`; `features/src/lib.rs:1372-1375`; `models-manager/models.json` |
   | `apply_patch` | on: the catalog entry has `apply_patch_tool_type = freeform` | **partly**: it reads the target to verify a patch but never returns file content; errors show existence and echo the model's own lines | **none at 0.161.0** | `spec_plan.rs:1269`; `apply-patch/src/file_update.rs:110, 211`; the feature `apply_patch_freeform` is `Removed` (`features/src/lib.rs:1224-1227`) |
   | web search (hosted `web_search`) | on, mode `Cached`; absent for the pinned model, whose catalog entry has `use_responses_lite` (`spec_plan.rs:628`) | no (sends data out, reads nothing local) | `web_search = "disabled"` | `spec_plan.rs:622-655`; `config/src/config_toml.rs:472` |
   | `sleep` | on | no | `[features] sleep_tool = false` | `spec_plan.rs:1234-1255`; `features/src/lib.rs:998-1001` |
   | goal tools (`ext/goal`) | on | no (state database) | `[features] goals = false` | `ext/goal/src/extension.rs:533-545`; `features/src/lib.rs:1762-1765` |
   | apps/connectors tools (`codex_apps` MCP server) | on | no (remote connectors) | `[features] apps = false` | `features/src/lib.rs:1414-1417` |
   | `request_plugin_install`, `list_available_plugins_to_install` | on | no | `[features] tool_suggest = false`, `plugins = false` | `spec_plan.rs:657-662, 1256-1267`; `features/src/lib.rs:1492-1495, 1504-1507` |
   | `request_user_input` | on | no | `[tools.experimental_request_user_input] enabled = false` | `spec_plan.rs:1169-1176`; `core/src/config/mod.rs:2713-2719` |
   | `update_plan` | off | no | n/a | `core/src/config/mod.rs:2721-2726` |
   | `list_mcp_resources`, `list_mcp_resource_templates`, `read_mcp_resource` | off: only with a configured MCP server, and the fresh home has none | n/a | n/a | `spec_plan.rs:1132-1134` |
   | code-mode `exec`/`wait` | on: the catalog entry has `tool_mode = code_mode_only` | no: the V8 runtime exposes only `tools`, `ALL_TOOLS`, `text`, `image`, `store`, `load`, `notify`, `yield_control`, `exit`, and no file access; it reaches only the registered tools above | n/a | `code-mode-runtime/src/runtime/globals.rs:36-48` |
   | skill `list`/`read` | off: needs a cloud skill provider or resolved executor roots | no local arbitrary path (catalog entries only) | n/a | `ext/skills/src/tools/mod.rs:66-72` |

   The model-catalog values (`tool_mode`, `multi_agent_version`, `apply_patch_tool_type`, `use_responses_lite`) are
   those of the bundled `models-manager/models.json` entry for `gpt-6-astra`; a catalog refreshed from the service
   may differ, which is why the keys above do not rely on the catalog and the canary runs at every start.
4. **Auth: a symlink, not a copy.** `$CODEX_HOME/auth.json` is a symbolic link to the main login file. Evidence at
   0.161.0: Codex reads it with `File::open(path)` (follows the link) and writes it with
   `OpenOptions::truncate(true).write(true).create(true).open(path)` (`login/src/auth/storage.rs:193-200, 214-231`):
   a write goes **through** the link, there is no temp file and no atomic rename, so the link is not replaced by a
   regular file. A refresh calls `persist_tokens`, which loads and saves through the same storage
   (`login/src/auth/manager.rs:1600-1620`), so a token refreshed by either home lands in one file. Before it
   refreshes, a process reloads the file and skips the refresh if the tokens changed
   (`login/src/auth/manager.rs:2841-2870`). Refresh tokens are single use
   (`REFRESH_TOKEN_REUSED_MESSAGE`, `login/src/auth/manager.rs:207`); a **copy** would fork the refresh token, and
   the second refresh from either copy would fail and log the other out. The keyring option does not help: the key
   is derived from the canonical `CODEX_HOME` path (`login/src/auth/storage.rs:246-257`), so each home gets its own
   entry. The procedure is the owner's, never an agent's, because it touches the real `~/.codex`:

   ```
   mkdir -m 700 ~/.codex-cls
   cat > ~/.codex-cls/config.toml <<'EOF'
   cli_auth_credentials_store = "file"
   web_search = "disabled"

   [features]
   shell_tool = false
   view_image = false
   image_generation = false
   multi_agent = false
   apps = false
   plugins = false
   tool_suggest = false
   goals = false
   sleep_tool = false

   [agents]
   enabled = false

   [tools.experimental_request_user_input]
   enabled = false
   EOF
   shasum -a 256 ~/.codex-cls/config.toml        # must print the sha256 in item 2
   test -f ~/.codex/auth.json && ln -s ~/.codex/auth.json ~/.codex-cls/auth.json
   ```

   The `test -f` guard is the file-mode check: with a keyring login there is no `~/.codex/auth.json` and this
   procedure does not apply (the link is not made). The `shasum` line catches stray indentation from a raw copy.
   Recovery if a login is ever lost: run `codex login` in the main home (no `CODEX_HOME`); the link then points at
   the new file.
5. **Effect.** With no shell, image, agent or image-generation tool the generator and the judge have no
   file-read path that returns content. They need no tools. The argv, the binary path and the CLI help-text hash do
   not change. The one tool left that touches files is `apply_patch`, which cannot return file content
   (Residuals).
6. **Logging.** At each run start the coordinator logs, in the coordinator's log, the `CODEX_HOME` path and the
   sha256 of `$CODEX_HOME/config.toml`, and checks the latter equals the value in item 2. A mismatch or a missing
   file means the run does not start.
7. **Session logs live under `$CODEX_HOME/sessions`.** Codex writes sessions to `$CODEX_HOME/sessions`
   (`core/src/session_rollout_init_error.rs:38`; `SESSIONS_SUBDIR = "sessions"`, `rollout/src/lib.rs:86`). The base
   prereg §3.2 "Session logs" (lines 400-410) and the pinned `SESSION_LOG_DIRS`
   (`scripts/cls_pins.py:58`, `"codex": "~/.codex/sessions"`) name the old directory. For CLS runs the
   coordinator's before/after listing and purge use `$CODEX_HOME/sessions` instead. The frozen CLI takes the
   directory as an argument, so no script changes: `cls_send_lib.py session-snapshot --dir <dir>`
   (`scripts/cls_send_lib.py:981-983`), `session-log-report --codex-dirname <name>` (lines 984-989; the value is
   only a label printed in the report, `session_log_report`, lines 915-926) and `purge --root <dir>` (lines 990-994;
   `purge_paths`, line 931, deletes only listed regular files under the root). The pinned `SESSION_LOG_DIRS` string
   is now nominal for CLS runs. The coordinator may still list `~/.codex/sessions` as a control to show that CLS runs
   left nothing there; the base prereg's rule that its author never reads `~/.codex` stands.
8. **Canary interpretation.** With these tools off, a negative canary shows that **the model cannot read**, not that
   **the sandbox restricts reads**. The positive control already exists: the parked run of 2026-10-25, with the
   real `CODEX_HOME`, read the token. That run is the evidence that the canary can detect reads. The canary is a
   **text-file** canary: it detects a read of text, not of an image or other binary content. The §3.2 rule (token
   present: park, exit 11; non-zero exit: exit 24; `model:` header must equal the pin: exit 22) is unchanged. The
   §13 residual stands: a negative canary is evidence, not proof.
9. **Pre-run check (procedure, not code).** Before the rerun the coordinator runs two manual checks. (a)
   `CODEX_HOME=~/.codex-cls codex features list`: it calls no model and must show `false` for `shell_tool`,
   `view_image`, `image_generation`, `multi_agent`, `apps`, `plugins`, `tool_suggest`, `goals` and `sleep_tool`. A
   bad `config.toml` makes it fail with "failed to load bootstrap configuration". (b)
   `CODEX_HOME=~/.codex-cls codex exec -s read-only --skip-git-repo-check --ephemeral -C <empty dir> -m gpt-6-astra`,
   asking the model to read a token file outside the cwd. Pass condition: the token is absent from the output and
   the `model:` header matches the pin. Both results are recorded in the coordinator's log. The author of this
   amendment did not run (b); it ran (a) only against a throwaway `CODEX_HOME` holding the exact bytes of item 2
   and saw the nine keys `false`.
10. **What `~/.codex/config.toml` no longer applies.** The user's real `~/.codex/config.toml` is not loaded.
   Defaults set there (for example the model or reasoning settings) do not apply to CLS runs. The model is pinned
   by `-m` anyway (D-147), and the `model:` header check of §3.2 still applies. Any reasoning-effort default the
   owner relied on is now the Codex built-in default; this is disclosed, not measured.

## Residuals

* **A future Codex version can reopen a read path.** The inventory is a snapshot of 0.161.0. A tool added in a
  later version, or one that a later version turns on by default, or a renamed or ignored key, would reopen a read
  path with no error. The text canary detects only reads of **text** files; a path that returns image bytes or only
  leaks existence would pass it. The mitigation is procedural: the installed binary and its help hash are pinned,
  and a Codex update needs a new inventory before it is used for CLS runs.
* **`apply_patch` stays on.** It has no disable key at 0.161.0. It does not return file content. It can leak whether
  a path exists and whether a guessed line matches (the "Failed to find expected lines" error). The read-only
  sandbox rejects the write. Reading a random canary token that way is not feasible, but the channel exists.
* **Silent loss of a key.** If a Codex update renames or ignores one of the keys, the protection disappears without
  an error. For a text-reading tool the canary would read the token again and park (exit 11), which is a fail-closed
  detection; it depends on the canary still running at every start and resume, which the frozen code does.
* **Shared credential.** The symlink means `~/.codex-cls` and `~/.codex` share one login. The dedicated home gives
  config isolation, not credential isolation; with no file tool the model has no way to read the file. Two Codex
  processes that refresh at the same instant can still race: the write truncates before it writes
  (`login/src/auth/storage.rs:222-229`), and the in-process refresh lock does not cover a second process. If the
  login is lost, recover with `codex login` in the main home (item 4). If the owner logs out of the main home, CLS
  Codex calls fail as non-zero exit (24).
* **Evidence, not proof.** See "What changes" item 8.

## What does NOT change

* The frozen scripts, the `codex exec` and `claude -p` argv, the binary paths, the help-text hashes and
  `PINS-STATIC.json`; `PREREG-CLS-DEV.md`, `PREREG-CLS-DEV-AMEND1.md` and their sidecars.
* Every threshold and floor, the Claude calls, T0 and the cutoff, the scrubber and the secret rules, and every rule
  of the base prereg not named under "What changes".

## Freeze record

To freeze: after an independent review, write `PREREG-CLS-DEV-AMEND2.sha256` as `<sha256>  PREREG-CLS-DEV-AMEND2.md`
and `frozen_utc <date -u>` (after 2026-10-24T21:40:07Z), then run `scripts/check_prereg.py
docs/prereg/PREREG-CLS-DEV-AMEND2.md`. Any change to this file changes the hash and needs a new sidecar. The probe
(item 9) and the rerun of the generation run start after the freeze.
