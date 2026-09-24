# Domain 13 — Adversarial verification of 02_runtime_routing.md and 05_config_cli.md

Verifier: independent pass, did not write the original findings. Baseline: worktree
`llm-router-forensic`, detached at `3c96d23` (confirmed via `git rev-parse HEAD`). All
Python run with `HOME=$(mktemp -d) PYTHONPATH=.../src /Users/yaliandrona/Projects/llm-router/.venv/bin/python`
against the baseline. No full test suite run; no Ollama/paid APIs invoked. No repo file
modified.

Method: for each finding, re-derived the evidence from source (not from the report's own
quotes), ran the CLI commands myself and checked exit codes, traced call graphs, and
where the finding made a claim about a "live install," cross-checked it against
`~/.claude/settings.json`, `~/.claude/hooks/`, `hooks/hooks.json`, and
`src/llm_router/install_hooks.py`'s registration table — not just the worktree.

---

## RTE-002 — SIGNALS table drift (hook vs. classify.py)

**Verdict: CONFIRMED. Severity: HIGH (unchanged).**

- Diffed the two dict literals programmatically (not by eye): hook's `SIGNALS`
  (`hooks/auto-route.py:1018`) has 7 categories (`image, query, research, code,
  analyze, generate, coordination`); `classify.py:_SIGNALS` (`:57`) has 6 — no
  `coordination`. Byte-for-byte comparison of the `research`/`code`/`generate`
  sub-dicts: all three differ (research 2407 vs 2563 chars, code 5274 vs 4513, generate
  2720 vs 2304) — the "backfilled VERBATIM" claim at `classify.py:33` is false as
  written.
- The docstring's own stated reason for omitting `coordination` — "no matching
  `TaskType`" (`classify.py:34`) — is independently falsifiable: `TaskType.COORDINATE`
  exists at `types.py:89`, confirmed by grep.
- Checked for a parity test that would catch this drift: `tests/test_hook_classifier_equivalence.py`
  exists and is real (not vacuous — it fixed a month of silent-skip via a bad
  `parents[2]` path), but it only tests `complexity_for(...)` (the complexity level
  given a task_type), never `task_type` classification itself. No test anywhere
  asserts SIGNALS-table/task_type parity between the two engines. RTE-002's
  recommendation ("add a test asserting byte-identical SIGNALS tables") is a real,
  unmet gap.
- Correction to the original: the hook's *complexity* engine is **not** independent
  of `classify.py` as the four-engine framing implies — `hooks/auto-route.py:1553`
  delegates to `classify.py`'s `complexity_for(..., policy=HOOK_LIVE_POLICY)`, pinned
  at 100% agreement over 1000 comparisons (`test_hook_classifier_equivalence.py`).
  Only *task_type* classification (the SIGNALS tables) is still duplicated. This
  doesn't refute RTE-002 — the task_type drift is real and evidenced — but the
  parent doc's Overview list ("four independent ... combinations") somewhat overstates
  how independent the complexity axis is; worth a one-line correction there, not here.

## RTE-003 — Low-signal default decides 49.8% of routing (labelled CRITICAL)

**Verdict: PARTIALLY CONFIRMED as a finding; CRITICAL severity is not supported.
Corrected severity: LOW (the underlying gap is real; it is a measured, guarded,
documented property of the classifier, not an active correctness defect).**

- The 49.8%/41.4%/8.4% figures and the two-different-defaults claim are corroborated
  verbatim in three independent places: `classify.py:521-547`, the main checkout's
  `CLAUDE.md` ("A default is not a classification," S8, audit 2026-09-22), and
  `tests/test_s8_an_unclassified_prompt_is_not_a_confident_route.py`, which exists,
  asserts its own premise before its conclusion, and pins both defaults.
- Checked RTE-003's own open item — whether `low_signal_classifications()` is
  surfaced in `llm-router doctor`: **YES**, confirmed. `commands/doctor.py:1451-1454`
  and `:1572-1574` iterate `counter_registry.readings()` generically, and
  `low_signal_classifications` is registered there (`counter_registry.py:406-411`).
  This resolves RTE-003's stated uncertainty in the KEEP direction.
- Why CRITICAL is wrong: the project's own CLAUDE.md explicitly frames this as
  "measure the fall-through rate before believing a classifier" and "a one-word edit
  to a default is a routing change" — i.e., this is presented, by the project itself,
  as an already-diagnosed, already-instrumented, already-test-pinned property, not an
  open safety hole. CRITICAL implies an active, unmitigated risk of harm; what exists
  is a correctly-labeled uncertainty with a counter, a regression test, and an explicit
  decision (documented, not silent) to not guess at a better default without a
  labelled set. Nothing found in this pass shows the ambiguous-default routing
  produces unsafe or data-losing behavior — worst case is a Q&A prompt routed to
  `query` vs. `analyze`, both non-destructive read paths. Downgrading a well-measured,
  well-guarded, self-acknowledged statistical property to CRITICAL substitutes a
  correctness-sounding label for what is actually a routing-quality/measurement gap.

## RTE-004 — Direct-execution path bypasses budget/quota/gates/redaction

**Verdict: CONFIRMED. Severity: HIGH (agree with original, arguably HIGH-not-MEDIUM
on the budget/quota axis given real spend is possible — see below).**

- `router.py` is confirmed the sole call site for `maybe_redact`
  (`router.py:3948`), `run_gates` (`:3013`), `reserve_tokens`/`check_quota`
  (`:2861/:3869`). Grepped `src/llm_router/hooks/direct_executor.py` and
  `src/llm_router/hooks/chain_builder.py` directly: zero references to any of the
  four. The "budget"/"quota" hits that do appear there are wall-clock hook-deadline
  budgeting and quota-*pressure-zone* (model selection), an unrelated concept from
  `router.py`'s token/dollar budget and quota-denial machinery.
- Traced whether redaction happens upstream of direct execution, since this was the
  most likely way to refute the finding: `auto-route.py`'s `_scrub_secrets_text()`
  is real and does call the canonical `secret_scrubber.scrub_text`, but every call
  site (`:2119-2120`, `:4690`) scrubs text **only before writing it to local disk**
  (transcript shard, pending-route file) — never before it is sent over HTTP.
  `direct_executor.py`'s `call_gemini`/`call_openai`/`call_ollama` (`:265,336,377`)
  contain no scrub call at all. So `LLM_ROUTER_REDACTION=on` genuinely does not
  protect this path, as claimed — confirmed, not just grepped-for-absence.
- Checked whether the direct-execution chain is restricted to genuinely free/local
  models (which would make the budget-bypass claim mostly theoretical):
  **no** — `chain_builder.py:183-186` defines real paid `ModelSpec`s for
  `gemini-2.5-flash`, `gemini-2.0-pro`, `gpt-4o-mini`, `gpt-4o`, all reachable from
  this same ungoverned path. This means an operator's Gemini/OpenAI spend genuinely
  bypasses `budget.reserve_tokens`/`quota_routing.check_quota` on the default-ON
  direct-execution path — a real dollar-cost gap, not just a privacy one.

## CFG-001 — LLM_ROUTER_ENFORCE conflicting defaults (stop-enforce.py / status-bar.py)

**Verdict: PARTIALLY CONFIRMED. Corrected severity: MEDIUM (down from HIGH) — the
status-bar.py half is live and correctly diagnosed; the stop-enforce.py half is dead
code and the finding's "Is behavior currently used? YES — both hooks are installed
and run" claim is false for stop-enforce.py.**

- Confirmed the literal code: `stop-enforce.py:180` and `status-bar.py:117` both
  read `os.environ.get("LLM_ROUTER_ENFORCE", "hard")` directly, bypassing
  `enforce_config.resolve_enforce_mode()` (default `"smart"`, `enforce_config.py:39`).
  The divergence is real.
- Checked whether `stop-enforce.py` is actually wired up as a Claude Code hook, since
  the parent document's own RTE-001 (same audit, same pass) had already found that a
  file existing in `hooks/` doesn't mean it runs. It is not:
  - `hooks/hooks.json`'s `"Stop"` array registers only `session-end.py`
    (confirmed: `hooks/hooks.json:96-104`).
  - `src/llm_router/install_hooks.py`'s hook-registration table (`:692-707`, the
    list that actually gets copied+registered into `~/.claude/hooks/` +
    `settings.json`) has an entry for `session-end.py` → `"Stop"` and none for
    `stop-enforce.py` at all.
  - Live check on this machine: `grep -n "Stop" ~/.claude/settings.json` resolves
    only to `llm_router-session-end.py`; `ls ~/.claude/hooks/` has no
    `stop-enforce.py` (with or without the `llm_router-` prefix).
  - `stop-enforce.py` IS real, substantial code (224 lines, hook-version-tagged,
    covered by its own unit tests in `tests/test_stop_enforce_override.py` etc.),
    but every test loads it directly via `importlib.util.spec_from_file_location`
    — none of them exercise it as an installed Stop hook, because it isn't one.
  - By contrast, `status-bar.py` IS confirmed live: registered in
    `hooks/hooks.json` (`UserPromptSubmit`), in `install_hooks.py`'s table
    (`:694`), and present on this machine as
    `~/.claude/hooks/llm_router-status-bar.py`.
- Net: the "wrong default" bug for the enforcement-mode status line is real and
  user-visible (status-bar.py). The "wrong default silently changes violation
  tracking" half of the finding, attributed to stop-enforce.py, describes a code
  path that cannot currently execute in a standard install — it is orphaned,
  same class of defect as RTE-001 (which the parent document itself downgraded
  from HIGH to LOW for exactly this reason on a sibling file). CFG-001 should be
  split: status-bar.py stays a real, live MEDIUM bug; the stop-enforce.py half
  should be reclassified as dead-code hygiene, not an active safety gap.

## CFG-002 — Stale `.claude/hooks/auto-route.py` with conflicting env defaults

**Verdict: PARTIALLY CONFIRMED. Corrected severity: LOW (down from MEDIUM-HIGH) —
the drifted defaults are real and precisely as described, but the finding's central
"Is behavior currently used? YES, for any Claude Code session opened at this repo's
root" claim is contradicted by this same audit's own RTE-001 and by direct
verification.**

- Confirmed all cited facts exactly: `.claude/hooks/auto-route.py` last touched
  2026-04-13 (commit `3f452ae`), canonical `src/llm_router/hooks/auto-route.py`
  last touched 2026-09-24; defaults differ exactly as quoted —
  `LLM_ROUTER_CONFIDENCE_THRESHOLD` "4" vs "2", `LLM_ROUTER_OLLAMA_MODEL`
  `"gemma4:latest"` (not a real tag) vs `""`, `LLM_ROUTER_OLLAMA_TIMEOUT` "5" vs
  "45" (and a third value "4" in `commands/doctor.py`, also confirmed).
- The finding's premise, though, is that this stale file actually executes for
  "any Claude Code session opened at this repo's root." This is the exact premise
  the task brief asked to check, and it does not hold:
  - No `.claude/settings.json` exists anywhere relevant — not in this worktree,
    not in the main checkout (`/Users/yaliandrona/Projects/llm-router/.claude/`
    has only `settings.local.json`, a 3-line permissions file with no `hooks`
    key).
  - Claude Code hooks are only invoked when explicitly registered in a
    `settings.json` `hooks` block (global, project, or local) — a file merely
    existing under `.claude/hooks/` is not auto-discovered.
  - This is precisely what `02_runtime_routing.md`'s own RTE-001 already
    concluded for the sibling files in the same directory, after the coordinator
    caught the original mischaracterization: "this file **never runs**... The
    repo's own `.claude/hooks/` ... is referenced by no `.claude/settings.json`
    (none exists in the worktree) and no plugin manifest."
  - CFG-002 (05_config_cli.md) was written asserting the opposite conclusion
    about the same file, in the same audit pass, without cross-referencing
    RTE-001's correction. This is an internal contradiction in the forensic
    report set, not just an overstatement.
- The env-var drift itself remains a legitimate, evidenced repo-hygiene finding
  (three real conflicting defaults, one of them not even a valid Ollama tag) — but
  its practical impact is "a dead file could confuse a future reader," identical
  in kind and severity to RTE-001, not "every contributor session is silently
  misrouted."

## CFG-004 — safe_config.py precedence docstring contradicts actual code

**Verdict: CONFIRMED, including by direct empirical test (stronger evidence than
the original static analysis). Severity: MEDIUM-HIGH (unchanged).**

- Confirmed `RouterConfig(BaseSettings)` (`config.py:193`) sets
  `model_config["env_file"]` (`:587`) and has no `settings_customise_sources`
  override anywhere in the file — so pydantic-settings' documented default
  source order applies (init > env > dotenv > defaults).
- Ran an empirical test rather than trusting the static claim: wrote a `.env` with
  `OPENAI_API_KEY=from_dotenv_value` and set the real env var
  `OPENAI_API_KEY=from_real_env_value` in the same process, then instantiated
  `RouterConfig()` fresh. Result: `from_real_env_value` won — confirming the real
  environment variable outranks `.env`, the exact opposite of
  `safe_config.py`'s documented "1. .env file ... 3. Environment variables."
- Confirmed `model_post_init` (`config.py:808-829`) only applies
  `~/.llm-router/config.yaml` values into fields that are still falsy
  (`if not current: setattr(...)`) — i.e., YAML really is applied last, not
  tier-2 as documented.
- This is a documentation-vs-code bug with no behavioral fix proposed or needed;
  confirmed as described.

## CFG-009 — ~45% of CLI commands silently ignore `--help`

**Verdict: CONFIRMED by direct execution. Severity: MEDIUM-HIGH (unchanged).**

- Executed against the baseline with a fresh `$HOME`:
  - `llm-router status --help` → ran the real status dashboard, exit 0.
  - `llm-router config --help` → ran the real config report, exit 0.
  - `llm-router budget --help` → ran the real budget-caps table, exit 0.
  - `llm-router doctor --help` → ran the real health check, printed "17 issue(s)
    found," **exited 1** — exactly the finding's claim that a help request on
    this command returns the tool's own "broken install" exit code.
- `cli.py`'s dispatch confirms the mechanism: 16 argparse-based commands get
  `--help` for free; hand-rolled commands only get it if the author manually
  checked for it. No shared dispatch-layer guard exists.

## CFG-010 — `last`/`retrospect` exit code always 0 regardless of failure

**Verdict: CONFIRMED by direct execution, exact reproduction of the cited case.
Severity: HIGH (unchanged).**

- Ran `llm-router last` against a fresh, empty `$HOME`: printed
  `Error: .../.llm-router/usage.db not found. Run some routed calls first.` and
  exited **0** (confirmed via `$?`).
- Confirmed the source cause directly: `commands/last.py:main()` really does
  `return 1` on that path (line ~150); `cli.py`'s dispatch
  (`elif args[0] == "last": ... _last_main(args[1:])`, no `sys.exit`) discards the
  return value. `cli.py` shows the same-class bug already fixed once for `verify`
  (`sys.exit(_verify_main(args[1:]))`, with an explicit `CHZ-PKG-005` comment
  citing exactly this failure mode) — confirming the finding's claim that the
  fix pattern already exists in-repo and simply wasn't applied to `last`/
  `retrospect`/`snapshot`. `retrospect` shares the identical unwrapped-dispatch
  shape (confirmed at the same `cli.py` location); did not independently force
  its `return 1` branch, matching the original's own "not separately executed"
  confidence caveat for that command.

---

## Summary table

| Finding | Verdict | Severity (orig → corrected) |
|---|---|---|
| RTE-002 | CONFIRMED | HIGH → HIGH |
| RTE-003 | PARTIALLY CONFIRMED | CRITICAL → LOW |
| RTE-004 | CONFIRMED | HIGH → HIGH |
| CFG-001 | PARTIALLY CONFIRMED | HIGH → MEDIUM |
| CFG-002 | PARTIALLY CONFIRMED | MEDIUM-HIGH → LOW |
| CFG-004 | CONFIRMED | MEDIUM-HIGH → MEDIUM-HIGH |
| CFG-009 | CONFIRMED | MEDIUM-HIGH → MEDIUM-HIGH |
| CFG-010 | CONFIRMED | HIGH → HIGH |
