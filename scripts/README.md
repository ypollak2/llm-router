# scripts/

Development, CI, benchmarking, and release tooling. None of this ships in the
published package — `scripts/` is excluded from the sdist (`pyproject.toml`).

All scripts are meant to be run **from the repo root**:

```bash
uv run python scripts/<bucket>/<script>.py
bash scripts/<bucket>/<script>.sh
```

---

## `ci/` — gates run by GitHub Actions

| Script | Purpose |
|---|---|
| `check_identity.py` | Brand gate: fails if the forbidden vendor string appears outside the documented allowlist (see the rationale comments in the script). Wired into `.github/workflows/ci.yml`. |
| `verify-version-sync.py` | Asserts `pyproject.toml` and all three plugin manifests carry the same version. |
| `verify-plugin-sync.py` | Asserts the Claude / Codex / Factory plugin manifests agree on name and version. |

## `release/` — cutting and publishing a release

| Script | Purpose |
|---|---|
| `pre-release-verify.sh` | Pre-flight checklist: clean tree, version sync, plugin sync, tests. Run this first. |
| `release.sh` | Full release flow — tag, build, sdist leak-check, publish. |
| `release.py` | Python half of the release flow (changelog/tag/artifact handling). |
| `verify-release.py` | Post-release verification of the published artifact. |
| `sync-versions.py` | Rewrites the version across `pyproject.toml` and every plugin manifest. |
| `publish-pypi.sh` | Build + upload to PyPI (`--dry-run` supported). |
| `publish-deprecation.sh` | Publishes the deprecated `claude-code-llm-router` shim package. |
| `agoragentic_register.py` | One-time Agoragentic marketplace registration. |
| `agoragentic_publish_listing.py` | Publishes/updates the Agoragentic listing. |
| `agoragentic_deploy_serverless.py` | Deploys the serverless Agoragentic endpoint. |

## `bench/` — benchmarking and evaluation

| Script | Purpose |
|---|---|
| `benchmark.py` | End-to-end routing benchmark across profiles. |
| `benchmark_routing_decision.py` | Micro-benchmark of the routing-decision path, for before/after comparisons. |
| `update_benchmarks.py` | Regenerates `docs/BENCHMARKS.md` from `src/llm_router/data/benchmarks.json`. Run by `.github/workflows/benchmarks.yml`. |
| `eval_classifier.py` | Scores the prompt classifier against a labelled set. |
| `routerarena/` | RouterArena submission tooling — `build_submission.py`, `build_robustness.py`, `convert_to_jsonl.py`, `run_sub10_openrouter.py`. See [`submissions/routerarena/README.md`](../submissions/routerarena/README.md). |

## `dev/` — local development and demos

| Script | Purpose |
|---|---|
| `install.sh` | Local dev install + host wiring. |
| `router_isolation_test.sh` | Isolation test suite (routing sanity, cache contamination, dashboard accuracy). See [`guide/TESTING.md`](../guide/TESTING.md). |
| `run_port_tests.sh` | Runner for the ported-module test subset. |
| `test_savings_realtime.sh` | Live savings-tracking smoke test. |
| `demo_routing.py` | Prints routing decisions for a set of sample prompts. |
| `audit_demo.sh` | Demo of the misroute-audit CLI. |
| `analyze-violations.py` | Analyses routing-enforcement violations from the local log. |
| `cleanup-hook-health.py` | Prunes stale hook-health records. |
| `gen_cast.py` | Generates terminal-cast recordings. |
| `generate-readme-svgs.py` | Regenerates the README SVG art. |
