#!/usr/bin/env bash
# Atomic release: bump version EVERYWHERE, test, build, verify, commit, tag, push,
# publish to PyPI, draft a GitHub release — in one shot, so PyPI / GitHub / the 6
# plugin manifests never drift again.
#
#   Usage: scripts/release.sh <X.Y.Z>
#   Pre-req: add a "## v<X.Y.Z>" section to CHANGELOG.md first.
#
# Credentialed steps (push / publish / gh release) DEGRADE GRACEFULLY: if auth is
# missing they print the exact command to run by hand instead of failing the release.
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="${1:-}"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "usage: scripts/release.sh X.Y.Z"; exit 1; }

# Every file that carries the version: pyproject + the 6 plugin manifests.
JSON_MANIFESTS=(
  .claude-plugin/plugin.json .claude-plugin/marketplace.json
  .codex-plugin/plugin.json  .codex-plugin/marketplace.json
  .factory-plugin/plugin.json .factory-plugin/marketplace.json
)

echo "──> 1/9  Pre-flight checks"
git diff --quiet && git diff --cached --quiet || { echo "FATAL: uncommitted changes — commit or stash first."; exit 1; }
grep -q "## v$VERSION" CHANGELOG.md || { echo "FATAL: no '## v$VERSION' section in CHANGELOG.md — add it first."; exit 1; }
if curl -s "https://pypi.org/pypi/llm-routing/json" | grep -q "\"$VERSION\""; then
  echo "FATAL: $VERSION is already published on PyPI (versions are immutable)."; exit 1
fi

echo "──> 2/9  Bump version → $VERSION (pyproject + ${#JSON_MANIFESTS[@]} manifests)"
perl -0pi -e "s/(^\s*version\s*=\s*)\"[0-9]+\.[0-9]+\.[0-9]+\"/\${1}\"$VERSION\"/m" pyproject.toml
for f in "${JSON_MANIFESTS[@]}"; do
  VERSION="$VERSION" python3 - "$f" <<'PY'
import json, os, sys
p = sys.argv[1]; v = os.environ["VERSION"]
def bump(o):
    if isinstance(o, dict):
        return {k: (v if k == "version" and isinstance(val, str) else bump(val)) for k, val in o.items()}
    if isinstance(o, list):
        return [bump(x) for x in o]
    return o
json.dump(bump(json.load(open(p))), open(p, "w"), indent=2); open(p, "a").write("\n")
PY
done

echo "──> 3/9  Verify all versions agree on $VERSION"
grep -q "\"$VERSION\"" pyproject.toml || { echo "FATAL: pyproject not bumped"; exit 1; }
for f in "${JSON_MANIFESTS[@]}"; do grep -q "\"$VERSION\"" "$f" || { echo "FATAL: $f not bumped"; exit 1; }; done

echo "──> 4/9  Enforcement lint + tests (version-sync + unit suites)"
uv run python scripts/lint_no_direct_llm.py src/llm_router
uv run --extra dev pytest tests/qa/test_plugin_packaging.py -k version -q
uv run --extra dev pytest tests/test_direct_session_spend.py tests/test_local_task_no_route.py tests/test_public_import.py -q

echo "──> 5/9  Build wheel + sdist, twine check"
rm -rf dist; uv build
uvx twine check dist/*

echo "──> 6/9  Clean-room import verify (fresh venv, no editable repo)"
TMP=$(mktemp -d); uv venv --python 3.12 "$TMP/v" >/dev/null 2>&1
uv pip install --python "$TMP/v/bin/python" dist/llm_router_router-"$VERSION"-py3-none-any.whl >/dev/null 2>&1
"$TMP/v/bin/python" -c "import importlib.metadata as m, llm_router.server; assert m.version('llm-routing')=='$VERSION'; print('   clean-room OK:', m.version('llm-routing'))"
rm -rf "$TMP"

echo "──> 7/9  Commit + tag"
git add pyproject.toml "${JSON_MANIFESTS[@]}" CHANGELOG.md
git commit -q -m "release: v$VERSION"
git tag -a "v$VERSION" -m "v$VERSION"
echo "   committed $(git rev-parse --short HEAD), tagged v$VERSION"

echo "──> 8/9  Push (main + tag)"
if git push origin main && git push origin "v$VERSION"; then
  echo "   pushed."
else
  echo "   ⚠️  PUSH FAILED (SSH auth?). Finish by hand: git push origin main && git push origin v$VERSION"
fi

echo "──> 9/9  Publish to PyPI + draft GitHub release"
if uv publish 2>/dev/null; then
  echo "   published to PyPI."
else
  echo "   ⚠️  PUBLISH skipped (no token in env). Finish by hand: uv publish --token \"\$PYPI_TOKEN\""
fi
if command -v gh >/dev/null && gh release create "v$VERSION" --title "v$VERSION" \
     --notes "$(awk "/^## v$VERSION/{f=1;next} /^## v/{f=0} f" CHANGELOG.md)" 2>/dev/null; then
  echo "   GitHub release created."
else
  echo "   ⚠️  GitHub release not created (no gh auth). Create from tag v$VERSION at github.com/LLM Router/LLM Router/releases/new"
fi

echo ""
echo "✅ Release v$VERSION done locally + tagged. Any ⚠️ above are credentialed steps to finish by hand."
