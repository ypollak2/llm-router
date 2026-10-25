# PREREG-CLS-DEV AMEND2: Codex runs with the shell tool disabled via a dedicated CODEX_HOME (D-283)

This file is frozen only when `PREREG-CLS-DEV-AMEND2.sha256` exists beside it and
`scripts/check_prereg.py docs/prereg/PREREG-CLS-DEV-AMEND2.md` reports a match; until then it is a draft. It
amends `PREREG-CLS-DEV.md` (r5, sha256 `6d173a1c081579f4decc82e8024f2814b2a24477052f2580c8f21dcb38ffc795`, frozen_utc
2026-10-22T16:02:26Z), which stays byte-identical, and sits beside `PREREG-CLS-DEV-AMEND1.md`, which also stays
byte-identical. It changes only the items under "What changes". Every frozen script (`cls_eval.py`,
`cls_dev_run.py`, `cls_dev_lib.py`, `cls_label_run.py`, `cls_gen_run.py`, `cls_resemble.py`, `cls_send_lib.py`,
`cls_pins.py`), every threshold and floor, the `codex exec` argv, and the PINS-STATIC file stand.

## Authority

D-283 (owner decision, 2026-10-25). The rule itself is the owner's: every CLS runner invocation that spawns
`codex exec` sets `CODEX_HOME` to a dedicated directory, `~/.codex-cls`. The author chose the details that
implement it, each disclosed below: the exact `config.toml` bytes, the logging of the path and the config hash, and
the reading of the canary.

## Deviation and evidence (counts only)

* **What happened.** The DEV-SYN v1 generation run (`cls_gen_run.py run`, frozen) parked with exit 11, "codex
  read-scope canary: the token appeared in the output; parked", on 2026-10-25, **before any generation call**.
  Nothing was sent to a generator and $0 was spent. PREREG §3.2 ("Call hygiene") says a token in the output parks
  the run and "the exposure is then a decision, not a default". This amendment is that decision.
* **Why the run cannot simply be resumed.** The frozen code re-runs the canary on every start and resume
  (`scripts/cls_gen_run.py` line 703-704, `S.run_codex_canary(...)`; `cls_label_run.py` line 657 does the same). GPT
  generation and GPT labels therefore cannot proceed while the canary reads the token.
* **Root cause (researched against codex-cli 0.161.0, the installed version).**
  * In `-s read-only`, `SandboxPolicy::ReadOnly` maps to full-disk read
    (`codex-rs/protocol/src/permissions.rs:2046-2056` at tag `rust-v0.161.0`).
  * The macOS Seatbelt policy for it is `(allow file-read*)` (`sandboxing/src/seatbelt.rs` ~986-988).
  * In `codex exec` the model can read files only through the shell tool.
  * The config key `[features] shell_tool = false` disables command-execution tools
    (https://learn.chatgpt.com/docs/config-file/config-reference).
  So §3.2's parenthesis "(a read-only sandbox may still read the filesystem)" is what happened: the sandbox did not
  restrict reads, and the model had a tool to read with.
* **The frozen argv cannot change.** `codex exec -s read-only --skip-git-repo-check --ephemeral -C <empty cwd>
  [--json] -m <pin>` is pinned in PINS-STATIC, as are the binary path and the CLI help-text hash. This amendment
  changes none of them.

## What changes

1. **Rule.** Every CLS runner invocation that spawns `codex exec` (generation, labels, the canary, and verify) runs
   with the environment variable `CODEX_HOME=<absolute path>`, the path being the dedicated directory
   `~/.codex-cls` expanded to an absolute path. The
   variable is exported in the shell that starts the runner; no script changes. The frozen child env
   (`cls_send_lib.codex_child_env`, lines 483-488) copies `os.environ` and pops only `ANTHROPIC_BASE_URL`,
   `ANTHROPIC_API_KEY` and `OPENAI_API_KEY`, and `child_call` passes that env to `send` (line 577), which passes it to
   `subprocess.run(..., env=env)` (lines 559-560). The canary reaches the same path (`run_codex_canary`,
   lines 713-715, calls `child_call`). So `CODEX_HOME` reaches every Codex child.
2. **The directory holds only two files.**
   * `config.toml`, containing exactly these bytes (LF line endings, one trailing newline, 2 lines, 30 bytes):

     ```
     [features]
     shell_tool = false
     ```

     sha256 of those bytes: `3bb7d6ae222d964fc3036e035fbda67b2888f6ddda2c27c71d11251907d4201a`.
   * `auth.json`, copied by the owner from `~/.codex/auth.json`, mode 0600. This amendment records neither its
     contents nor its hash.
3. **Effect.** With no shell tool the generator and the judge have no file-read path. They need no tools. The argv,
   the binary path and the CLI help-text hash do not change.
4. **Logging.** At each run start the coordinator logs, in the coordinator's log, the `CODEX_HOME` path and the
   sha256 of `$CODEX_HOME/config.toml`, and checks the latter equals the value in item 2. A mismatch or a missing
   file means the run does not start.
5. **Canary interpretation.** With no shell tool, a negative canary shows that **the model cannot read**, not that
   **the sandbox restricts reads**. The positive control already exists: the parked run of 2026-10-25, with the
   real `CODEX_HOME`, read the token. That run is the evidence that the canary can detect reads. The §3.2 rule
   (token present: park, exit 11; non-zero exit: exit 24; `model:` header must equal the pin: exit 22) is unchanged.
   The §13 residual stands: a negative canary is evidence, not proof.
6. **Pre-run check (procedure, not code).** Before the rerun the coordinator runs one manual probe:
   `CODEX_HOME=~/.codex-cls codex exec -s read-only --skip-git-repo-check --ephemeral -C <empty dir> -m gpt-6-astra`,
   asking the model to read a token file outside the cwd. Pass condition: the token is absent from the output and
   the `model:` header matches the pin. The result is recorded in the coordinator's log. The author of this
   amendment did not run it.
7. **What `~/.codex/config.toml` no longer applies.** The user's real `~/.codex/config.toml` is not loaded. Defaults
   set there (for example the model or reasoning settings) do not apply to CLS runs. The model is pinned by `-m`
   anyway (D-147), and the `model:` header check of §3.2 still applies. Any reasoning-effort default the owner
   relied on is now the Codex built-in default; this is disclosed, not measured.

## Residuals

* **Silent loss of the key.** If a Codex update renames or ignores `shell_tool`, the protection disappears without
  an error. The canary would then read the token again and park (exit 11). That is a fail-closed detection; it
  depends on the canary still running at every start and resume, which the frozen code does.
* **Credential duplicate.** The copied `auth.json` duplicates a credential in a second place. Mode 0600, same user,
  same machine. It can go stale when the login in `~/.codex` is refreshed; a failed Codex call then parks as
  non-zero exit (24).
* **Evidence, not proof.** See "What changes" item 5.

## What does NOT change

* The frozen scripts, the `codex exec` and `claude -p` argv, the binary paths, the help-text hashes and
  `PINS-STATIC.json`; `PREREG-CLS-DEV.md`, `PREREG-CLS-DEV-AMEND1.md` and their sidecars.
* Every threshold and floor, the Claude calls, T0 and the cutoff, the scrubber and the secret rules, and every rule
  of the base prereg not named under "What changes".

## Freeze record

To freeze: after an independent review, write `PREREG-CLS-DEV-AMEND2.sha256` as `<sha256>  PREREG-CLS-DEV-AMEND2.md`
and `frozen_utc <date -u>` (after 2026-10-24T21:40:07Z), then run `scripts/check_prereg.py
docs/prereg/PREREG-CLS-DEV-AMEND2.md`. Any change to this file changes the hash and needs a new sidecar. The probe
(item 6) and the rerun of the generation run start after the freeze.
