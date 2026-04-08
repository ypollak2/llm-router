# Releasing

`llm-router` now follows a release-train model:

- `main` may contain unreleased work.
- `CHANGELOG.md` must start with `## Unreleased`.
- `pyproject.toml` is the source of truth for the last published package version.
- Tagging `vX.Y.Z` publishes to PyPI and creates or updates the matching GitHub Release from `CHANGELOG.md`.

## Every User-Facing Push

If a push changes tools, hooks, install flow, config surface, or Claude Code integration:

1. Update `CHANGELOG.md` under `## Unreleased`.
2. Update `README.md` if install, setup, configuration, supported tools, or roadmap status changed.
3. Review Claude/registry metadata if the public MCP surface changed:
   - `.claude-plugin/plugin.json`
   - `.claude-plugin/marketplace.json`
   - `glama.json`
   - `mcp-registry.json`
   - `smithery.yaml`

The `Release Hygiene` GitHub Action enforces the required `CHANGELOG.md` and `README.md` updates for the most common user-facing paths.

## Every Release

Before tagging a new version:

1. Move the shipped items from `## Unreleased` into a new `## vX.Y.Z — ...` section in `CHANGELOG.md`.
2. Bump the version in:
   - `pyproject.toml`
   - `uv.lock`
   - `.claude-plugin/plugin.json`
   - `.claude-plugin/marketplace.json`
   - `glama.json`
   - `mcp-registry.json`
3. Re-read `README.md` and make sure the install flow, feature list, roadmap status, and tool counts still match the code.
4. Re-check `smithery.yaml` if config/env vars changed.
5. Run:

```bash
uv lock --check
uv sync --extra dev --extra agno
uv run pytest tests/ -q --ignore=tests/test_integration.py --timeout=30
uv run ruff check src/ tests/
python3 scripts/release_guard.py sync
```

6. Tag the release:

```bash
git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin vX.Y.Z
```

## What Happens On Tag Push

`.github/workflows/publish.yml` now does all of the release plumbing:

1. Verifies the tag matches `pyproject.toml`.
2. Runs the publish test gate.
3. Builds and publishes to PyPI.
4. Creates or updates the GitHub Release using the matching `CHANGELOG.md` section.

## Guardrails

`scripts/release_guard.py` is the automation source for release hygiene:

- `sync` checks version sync across package/plugin/registry metadata and changelog structure.
- `changes` checks whether a diff touched user-facing paths that require `CHANGELOG.md` and `README.md`.
- `title` and `notes` extract GitHub Release content from `CHANGELOG.md`.
