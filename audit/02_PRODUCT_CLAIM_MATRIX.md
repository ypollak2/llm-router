# Phase 2 / 42 / 43 / 44 / 45 — Product Claim Matrix, Release Reality, Install, Upgrade, Failure Experience

Auditor: adversarial-user persona, isolated `HOME=/tmp/audit-home`,
`LLM_ROUTER_HOME=/tmp/audit-home/.llm-router`, fresh `uv venv` at `/tmp/audit-venv`
(Python 3.12.13). Real `~/.claude/settings.json` verified byte-identical
(`8ea0e0a718ac176e12f19dfbc3777589f069d99af6aab80df378ba37e9c1f6ab`) before and
after every experiment below. Package installed exactly as a user would:
`uv pip install llm-routing` → resolved `llm-routing==14.1.0`, i.e. the real
current PyPI artifact, not the repo tree.

Two subjects, kept distinct throughout: **PyPI 14.1.0** (what 1,000 new
installs get today) and **repo HEAD** `357a402` (26 commits ahead of the
`v14.1.0` tag — `git log v14.1.0..HEAD --oneline | wc -l` = 26, not the 15
`audit/FROZEN_STATE.md` claims for that specific range; FROZEN_STATE's "15"
covers only `8c7366b..HEAD`, a sub-range — FROZEN_STATE understates total
drift from the tag by ~11 commits. STRONGLY SUPPORTED, low stakes).

---

## Part 1 — Claim ledger (Phase 2)

| # | Claim (source) | Implementation found | Runtime-reachable? | E2E evidence | Verdict |
|---|---|---|---|---|---|
| 1 | "Install in 30 seconds" / `pip install llm-routing` (README L41-49) | `uv pip install llm-routing` resolves and installs in ~15s | Yes | Ran verbatim; succeeded | VERIFIED (install itself) — but see #2 |
| 2 | First documented next step, `llm-router doctor`/`status`, "Verify" (README Quick Start §3) | `commands/status.py` imports `rich` unconditionally | Yes, and it crashes | `llm-router status` → `ModuleNotFoundError: No module named 'rich'` on a completely clean install, no API keys, no config. `rich` is **not** in `Requires-Dist` for the published 14.1.0 wheel (confirmed via `pip show`/METADATA). Root-caused in the repo's own HEAD `pyproject.toml` comment, which describes this exact incident and says it is "Guarded by tests/test_m11_declared_dependencies.py" — but that fix (`c0fbbc6 fix(packaging): stop shipping a function that raises on every call`) is one of the 26 unreleased commits. | **FALSE / CONFIRMED** — the CLI's own most-visible command crashes for every current user on a clean install |
| 3 | CLI reference table lists `llm-router gain # token-savings analytics by period` (README §CLI) | No such subcommand exists in `cli.py` | N/A | `llm-router gain` → `llm-router: unknown command 'gain' — see 'llm-router --help'` | **FALSE / CONFIRMED** on PyPI 14.1.0 |
| 4 | `llm-router demo` shown as the trust-building first look at "cost-optimized routing examples" | `commands/demo.py` computes `total_opus` and `total_routed` from the same displayed per-case costs, but `total_opus` comes out *smaller* than `total_routed` | Yes | Verbatim output: `Always-Opus: $0.0450 per batch` / `Smart Routing: $0.09001 per batch` / `Savings: $-0.0450 (-100% cheaper)`. The routed total is literally double the "always use the expensive model" baseline, and the demo prints "cheaper" unconditionally regardless of sign. The same output's own "Next steps" tells the user to run `llm_router gain` — the nonexistent command from claim #3. | **FALSE / MISLEADING, CONFIRMED** — the flagship first-run demo shows routing costing 2x more than not routing, and calls it a saving |
| 5 | "Local-first... nothing is sent to an llm-router service" / privacy table (README §Trust) | True for provider calls. But `doctor`'s "Gateway daemon (interpreter drift)" check makes an unconditional HTTP GET to `http://127.0.0.1:17900/healthz` (only overridable via `LLM_ROUTER_URL`), ignoring `HOME`/`LLM_ROUTER_HOME` entirely | Yes | In the isolated-HOME audit, this check reported `✓ daemon Python 3.11.15 matches on-disk venv (/Users/yaliandrona/Projects/Chuzom/.venv/bin/python)` — a **different, unrelated private project's** long-running daemon on this machine, on a machine-wide, unnamespaced port. `doctor` treated an unrelated process as its own gateway and printed a green "OK". | **DESIGN RISK, CONFIRMED** — "local-first, nothing leaves the machine" is true for provider traffic, but the health-check surface is a shared, unauthenticated, unnamespaced localhost port with no per-user/per-install isolation; on any multi-project or multi-tenant machine it will silently cross-report |
| 6 | `OLLAMA_BASE_URL` is *the* documented env var for local models (README Quick Start §2, Configuration §) | `doctor.py`/`seats.py`/`model_evaluator.py` read `OLLAMA_URL` (undocumented in README); only `discover.py`/`dynamic_routing.py`/`config.py`'s provider-detection path read `OLLAMA_BASE_URL`. `config.py` itself has a comment admitting the split is real and intentional | Yes | Set `OLLAMA_BASE_URL=http://10.255.255.1:11434` (unroutable) exactly as README instructs; `doctor` still reported `✓ running — 4 model(s)` because it silently fell back to hard-coded `localhost:11434` (which happens to be real on this dev machine) | **MISLEADING, CONFIRMED** — `doctor`'s "provider connectivity" claim (README §Quick Start) does not check the variable the README tells users to set |
| 7 | "Savings: How It Works" — `savings-report` is described as the source of truth for "detailed token/cost breakdown (all-time)" (README §CLI, §Savings) | `commands/savings_report.py` docstring: *"Reads the authoritative per-call ledger `savings_stats`... as the SINGLE source of truth."* Meanwhile `commands/status.py`'s Rich UI and `commands/doctor.py`'s new "Savings coverage (provenance)" check each compute/report a **different** number from **different** filtering logic | Yes | See Part 4 (upgrade path) for the exact reproduction: three commands against the identical DB report $49.24/500 calls, $51.33/800 calls, and "0 counted, 300 excluded" respectively, at the same moment in time | **FALSE, CONFIRMED** — there is no single source of truth; the claim is directly contradicted by the repo's own three commands |
| 8 | "35–80%" and "87%" savings figures / RouterArena listing (README §Why, §RouterArena) | README itself now states, in its own words: *"single-user observations... with no stated denominator, and should be read as anecdotes."* | N/A | Self-disclosed by the project | **PARTIALLY TRUE / self-corrected** — the README already hedges this claim to the appropriate degree; no further defect found here |
| 9 | "Secrets never leave your machine... fail-closed" (README §Features) | Not exercised in this pass (out of scope for time budget; flagged for the security-focused specialist track, not re-tested here) | — | — | **UNPROVEN by this audit** (not FALSE — simply not independently re-verified in this pass; do not double-count against the security track's own finding) |
| 10 | `LLM_ROUTER_DIRECT_EXECUTION` allowlist description (README §Trust) — README itself lists exactly what the blocklist does NOT stop | Matches what the README says (`rm -rf ./src`, `$HOME` deletes, `git push --force`, exfiltration via `curl`, unstopped) | — | Self-disclosed, worded as a limitation, not a promise | **VERIFIED as an honest limitations statement** — this is a case of the README doing the right thing; noted so it isn't lost among the negative findings |
| 11 | "60 tools... default `consolidated` surface shows 11 front-door tools" (README §MCP Tools) | `doctor` prints `✓ every offered tool resolves to a real implementation` | Yes | Confirmed by `doctor` output above | **VERIFIED** (surface-existence only; tool *quality* not tested here) |
| 12 | `llm-router okf index` / `llm-router sessions status` (README §CLI) | Both exist, both ran cleanly | Yes | `okf index`: `scanned: 1343 source file(s), indexed: 1297 doc(s)`. `sessions status`: `No fragmented sessions`. Note: neither appears in `llm-router --help` output at all — README is accurate, `--help` is incomplete | **VERIFIED** (claim true; `--help` itself under-documents the CLI — separate, minor finding) |

---

## Part 2 — Release reality (Phase 42)

- PyPI `llm-routing` latest version = **14.1.0**, matching `git tag v14.1.0` exactly (`pyproject.toml` at that tag: `version = "14.1.0"`). PyPI is not stale relative to the tag.
- Repo HEAD (`357a402`) is **26 commits ahead** of `v14.1.0` (`git log v14.1.0..HEAD --oneline | wc -l` → 26), including two internal adversarial-audit remediation passes (`52630ae`, and the 15-commit run ending at HEAD) that fix, among others: the `rich`/`requests` missing-dependency crash (#2 above), several cost/measurement/provenance defects, a route-server auth gap, and security-scrubber bypasses.
- **None of these fixes are on PyPI.** A user running `pip install llm-routing` today gets the pre-audit-remediation code — including the `status` crash reproduced above — even though the fixes exist, are merged to `fix/audit-2026-09-22`, and are pushed to `origin`. The branch has not been merged to a release branch, tagged, or published as a new PyPI release as of this audit.
- `CHANGELOG.md` has no "Unreleased" section (`grep -n '^## \[Unreleased\]'` → no match), so a user reading the changelog has no way to know these fixes exist or are coming.
- Dependency drift is real and reproducible: `pip show llm-routing` / wheel METADATA lists `Requires-Dist` without `rich` or `requests`; HEAD's `pyproject.toml` adds both with inline comments documenting exactly why (the crash reproduced in claim #2, and a second transitive-only `requests` usage in `stats.py`/`local_platforms.py`).

## Part 3 — Fresh install (Phase 43)

- `uv pip install llm-routing` into `/tmp/audit-venv` (fresh venv, not the repo's own `.venv`) succeeded, pulling 14.1.0 and ~40 transitive packages.
- `llm-router install` (no flags) under `HOME=/tmp/audit-home` correctly scoped every artifact to the fake home: hooks under `/tmp/audit-home/.claude/hooks/`, MCP registration in `/tmp/audit-home/.claude.json` and `/tmp/audit-home/.claude/settings.json`, state under `/tmp/audit-home/.llm-router/`, Codex config under `/tmp/audit-home/.codex/`. Real `~/.claude/settings.json` confirmed byte-identical before/after (sha256 above). **Isolation for filesystem state works correctly.**
- **Isolation breaks for network state.** The install auto-detected and reported live Ollama models (`qwen3.5:latest`, `qwen3.8:latest`, `nomic-embed-text:latest`, +1) and a live "Gateway daemon" — both are real developer-machine residue (Ollama models this developer pulled; a `chuzom.gateway` daemon from an unrelated private project bound to a fixed, unnamespaced `127.0.0.1:17900`). A genuinely fresh machine would see neither; this developer machine's `doctor`/`install` output cannot be taken as representative of a first-time user's experience for those two checks specifically. `install` also silently detected the machine's real Codex CLI binary on `PATH` and configured it — expected/correct behavior (PATH detection is not a HOME concern) but worth noting as another axis on which "fresh install" testing on a dev box differs from a truly clean machine.
- `llm-router --help` does not list `summary`, `gain`\* (\*because it doesn't exist), `okf index`, or `sessions status`, though several of these are real, working commands documented in the README. The CLI help text and the README are two different, partially-inconsistent sources of truth for "what commands exist."
- The `summary` command (undocumented in `--help`, documented in code only) prints `⚡ C H U Z O M ⚡` as its wordmark — **confirmed, exact source**: `src/llm_router/observability/summary.py:46`: `_LLM_ROUTER_WORDMARK = "⚡ C H U Z O M ⚡"`, and `src/llm_router/banner_art.txt:2`: `.'   ╱─── C H U Z O M ───╲   '.`. This is shipped in the published 14.1.0 wheel (verified in the installed site-packages, not just the repo). `banner.py`'s own wordmark was correctly rebranded (`_BRAND = "llm_router"`); these two other files were missed. A user of the public MIT-licensed `llm-router` package sees the name of a separate, private product baked into their terminal output on a completely ordinary command (`llm-router summary`).

## Part 4 — Upgrade path (Phase 44): the provenance migration, measured

Constructed realistic pre-provenance state: inserted 500 rows directly into
`savings_stats` (the exact old schema PyPI 14.1.0 creates — no `is_simulated`/
`mode` columns) with a plausible cost/savings distribution
(`total saved (raw sum) = $49.24`). Backed up the pre-migration DB to
`/tmp/audit-artifacts/usage_pre_migration_backup.db`.

**Before** (PyPI 14.1.0, pre-fix code) — `llm-router savings-report`:
```
Claude quota saved:  $49.2402   across 500 routed call(s)
```

**After** (repo HEAD, same DB, same `LLM_ROUTER_HOME`) — three different commands, run back to back, same instant:

| Command | Number shown | Table read | Provenance-filtered? |
|---|---|---|---|
| `savings-report` | **$49.2402 / 500 calls** — unchanged | `savings_stats` | **No** — `_query()` has no `is_simulated`/provenance clause at all; confirmed by reading `src/llm_router/commands/savings_report.py` (no reference to `is_simulated`, `is_real`, or `provenance` anywhere in the file) |
| `doctor` → "Savings coverage (provenance)" | **"0 row(s) counted, 300 excluded (unknown origin)... of 300"** | `usage` table (300 synthetic rows inserted separately) | Yes — and correctly explains itself: *"300 usage row(s) predate provenance tracking and are excluded... Totals will rebuild from new, stamped calls."* |
| `status` (Rich UI) | **$51.33 saved · 800 routed calls, all time** | Appears to combine both `usage` (300 rows) and `savings_stats` (500 rows) — 300+500=800 matches exactly | Not visibly |

This is CONFIRMED, not hypothetical: three commands against one identical
database, at one identical moment, report three different totals (**$49.24**,
**$51.33**, and an explicit **"$0 counted"**), and only one of the three
(`doctor`) tells the user why. `savings-report`'s own docstring claims to be
*"the authoritative... SINGLE source of truth"* — directly contradicted by
this reproduction. A real user upgrading past this migration would see
`doctor` warn them their historical savings are unmeasured, while the two
commands actually named for reporting savings (`savings-report`, `status`)
keep showing (different) numbers built from the same unmeasured rows,
unfiltered, unflagged, with no changelog entry (Part 2) explaining any of it
— because none of this has shipped yet.

**Verdict: MISLEADING METRICS, CONFIRMED**, with the additional finding that
the remediation itself (T-05, unreleased) is incomplete: it fixed the ledger
`doctor` reads and did not fix the two commands users actually read for "how
much did I save."

## Part 5 — User-visible failure experience (Phase 45)

| Scenario | What the user sees | Severity |
|---|---|---|
| Clean install, first command (`status`) | Python traceback, `ModuleNotFoundError: No module named 'rich'` | **CONFIRMED, CRITICAL** — see claim #2 |
| First-run trust-building demo (`demo`) | Routed cost shown as *double* the do-nothing baseline, labeled "cheaper" | **CONFIRMED, HIGH** — see claim #4 |
| `OLLAMA_BASE_URL` set per README, Ollama actually unreachable there | `doctor` reports "✓ running" anyway (reading a different, hard-coded variable/host) | **CONFIRMED, HIGH** — silent misconfiguration masked as success, see claim #6 |
| No API keys at all (default state throughout this audit) | Every command degrades gracefully: `doctor` shows `⬜ No external provider API keys found`, `install`/`status`/`demo`/`verify` all run without crashing on this axis | **No defect found** — this specific path is handled well |
| Read-only `$HOME` (`chmod -R a-w`) | `doctor` degrades to warnings ("usage.db missing", "last_classification missing") and still exits reporting "All doctor checks passed" — no crash, no silent data loss observed in this quick pass | **No defect found in this pass** (not exhaustively fuzzed — a deeper pass could still find a write path that throws) |
| Gateway/daemon health check on a shared machine | Cross-reports an unrelated process as "your gateway daemon, ✓ OK" | **CONFIRMED, DESIGN RISK** — see claim #5 |
| Upgrading past the provenance migration | Three different savings totals from three commands, only one explained | **CONFIRMED, HIGH** — see Part 4 |
| `--help` vs. README vs. real commands | Three different, mutually inconsistent listings of "what this CLI can do" | **CONFIRMED, MEDIUM** |

---

## Evidence artifacts

- `/tmp/audit-artifacts/usage_pre_migration_backup.db` — pre-migration `savings_stats`/`usage` snapshot used for Part 4
- `/tmp/audit-artifacts/doctor_unroutable.txt` — full `doctor` output with `OLLAMA_BASE_URL` set to an unroutable address, showing the false "✓ running" result
- Real `~/.claude/settings.json` sha256 before and after all experiments: `8ea0e0a718ac176e12f19dfbc3777589f069d99af6aab80df378ba37e9c1f6ab` (unchanged)
