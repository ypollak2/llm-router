"""One canonical source per concept, enforced — R11.

The audit's CLASS-B finding: fixes get applied call-site-by-call-site instead
of consolidated, so the same defect reappears in a new file. Dated evidence:

* path resolution — 9 instances, including 5 on the same day a "repo-wide
  sweep" declared the class closed, plus 2 more found by this round
  (`direct_diagnostics._samples_path`, `seats.seats_path`, both of which
  escaped `LLM_ROUTER_HOME` into the operator's real home).
* provider identity — 3 instances, the third (`cost.py::VALID_PROVIDERS`,
  which rejected `'google'` and so swallowed every real Gemini decision)
  created by the very commit that fixed the second.

Fixing instance N without preventing instance N+1 is what produced that
history. These tests are the prevention: they fail when a SECOND
implementation of a canonical concept appears, naming the file and line.

The one place the repo already did this — the secret scrubbers, consolidated
onto `secret_scrubber.scrub_text` — held under a full adversarial audit. This
generalises that template.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

#: The module that OWNS each concept. Code there is allowed to be the one
#: implementation; everyone else must import it.
CANONICAL = {
    "state_path": SRC / "llm_router" / "paths.py",
    "provider_family": SRC / "llm_router" / "model_registry.py",
}

#: Files permitted to compose a state path directly, each with a reason.
#: LOWER THIS LIST. Never extend it without recording why.
STATE_PATH_ALLOWED = {
    # The canonical resolver itself.
    "llm_router/paths.py": "owns the concept",
    # Explicit `home=` override parameters: these branches are only reached
    # when a caller passes a directory ON PURPOSE, and each falls through to
    # paths.state_path() otherwise. Verified 2026-09-22.
    "llm_router/direct_diagnostics.py": "explicit home= override branch only",
    "llm_router/seats.py": "explicit home= override branch only",
    # Hook processes that must resolve state before the package is importable.
    "llm_router/hooks/agent_writes.py": "standalone hook, pre-import resolution",
    "llm_router/hooks/direct_executor.py": "standalone hook, pre-import resolution",
    "llm_router/hooks/draft_usage.py": "standalone hook, pre-import resolution",
    "llm_router/hooks/tool_intercept.py": "standalone hook, pre-import resolution",
    # Display/uninstall strings, not resolution.
    "llm_router/commands/uninstall.py": "prints the path, does not resolve it",
    # `Path(base) if base else Path.home()/...` — the else-branch is dead in
    # practice because every caller threads a base derived from
    # LLM_ROUTER_HOME. VERIFIED 2026-09-22: each resolves under an isolated
    # home. Still hand-copied resolvers and still candidates for
    # consolidation; allowlisted because they are correct, not because they
    # are good.
    "llm_router/attempt_log.py": "base= threaded by caller; verified isolated",
    "llm_router/model_discovery.py": "base= threaded by caller; verified isolated",
    "llm_router/trace.py": "base= threaded by caller; verified isolated",
    "llm_router/vision_registry.py": "base= threaded by caller; verified isolated",
    # Reads LLM_ROUTER_HOME first; Path.home() is only the documented default.
    "llm_router/prompt_capture.py": "env-first, home is the default arm",
    # THE ONE SITE THAT MUST NOT USE THE RESOLVER. _refuse_unisolated_test_write
    # asks "is a test about to write to the OPERATOR'S REAL database?" — a
    # question state_path() cannot answer, because state_path() follows
    # LLM_ROUTER_HOME and would therefore compare an isolated path against
    # itself. It did exactly that briefly on 2026-09-22 and silently refused
    # every test write.
    "llm_router/cost.py": "isolation guard; must NOT follow LLM_ROUTER_HOME",
}


def _src_files():
    return sorted(SRC.rglob("*.py"))


def _rel(p: pathlib.Path) -> str:
    return str(p.relative_to(SRC))


def _code_lines(path: pathlib.Path):
    """Yield (lineno, text) for lines that are not comments or docstrings."""
    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return
    doc_spans = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            doc_spans.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    for i, line in enumerate(text.split("\n"), 1):
        if i in doc_spans or line.strip().startswith("#"):
            continue
        yield i, line


# ── concept 1: state path resolution ─────────────────────────────────────────

_STATE_PATTERN = re.compile(
    r'(Path\.home\(\)|expanduser\(\s*["\']~)[^\n]*\.llm-router'
)


def test_no_second_state_path_resolver():
    """`~/.llm-router` is composed in exactly one place.

    A hand-copied `Path.home() / ".llm-router"` does not consult
    LLM_ROUTER_HOME, so it escapes isolation silently — which is how
    `direct_samples.jsonl` and `seats.json` were written into the operator's
    real home during a probe that believed it was sandboxed.
    """
    offenders = []
    for f in _src_files():
        rel = _rel(f)
        if rel in STATE_PATH_ALLOWED:
            continue
        for lineno, line in _code_lines(f):
            if _STATE_PATTERN.search(line):
                offenders.append(f"{rel}:{lineno}  {line.strip()[:70]}")
    assert not offenders, (
        "state path composed outside llm_router/paths.py:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse `paths.state_path(...)`. If this site genuinely needs to "
          "bypass it, add it to STATE_PATH_ALLOWED with a reason."
    )


def test_the_state_path_scan_can_actually_fire():
    """Anti-vacuity. A scan that matches nothing protects nothing."""
    assert _STATE_PATTERN.search('root = Path.home() / ".llm-router"')
    assert _STATE_PATTERN.search("p = os.path.expanduser('~') + '/.llm-router'")
    assert not _STATE_PATTERN.search("from llm_router import paths")


def test_the_allowlist_entries_still_exist():
    """A stale allowlist silently widens the scan's blind spot."""
    missing = [rel for rel in STATE_PATH_ALLOWED if not (SRC / rel).exists()]
    assert not missing, f"STATE_PATH_ALLOWED references deleted files: {missing}"


# ── concept 2: provider identity ─────────────────────────────────────────────

#: Sites permitted to name the family, each with a reason.
PROVIDER_FAMILY_ALLOWED = {
    # `router._google_providers()` keeps a literal fallback so a missing
    # registry cannot break routing. A fallback that can DRIFT is the bug this
    # test exists to prevent, so it is pinned equal to the canonical set by
    # `test_the_router_fallback_cannot_drift` below. Allowlisted because it is
    # pinned, not because it is exempt.
    "llm_router/router.py": "fail-safe fallback, pinned equal to canonical",
}


def test_provider_families_are_imported_not_relisted():
    """No module hand-lists a provider family that model_registry owns.

    `cost.py` listed `'gemini'` and omitted `'google'`. Every real Gemini
    routing decision therefore raised ValueError, was swallowed by a broad
    `except`, and never reached `routing_decisions` — a whole provider missing
    from the ledger, silently.
    """
    from llm_router.model_registry import GOOGLE_PROVIDERS

    # A line is an attempt to BE the family only if it names most of it.
    # Naming one or two is ordinary: `_CHEAP_PROVIDERS` and `_PAID_PROVIDERS`
    # are different concepts that legitimately mention gemini, and flagging
    # them would push real offenders into an allowlist that then becomes the
    # blind spot. Threshold set at >= 4 of 5, which the three known instances
    # all cleared and the unrelated concepts do not.
    THRESHOLD = max(2, len(GOOGLE_PROVIDERS) - 1)

    offenders = []
    for f in _src_files():
        rel = _rel(f)
        if rel in ("llm_router/model_registry.py",) or rel in PROVIDER_FAMILY_ALLOWED:
            continue
        for lineno, line in _code_lines(f):
            named = sum(1 for n in GOOGLE_PROVIDERS
                        if f"'{n}'" in line or f'"{n}"' in line)
            if named >= THRESHOLD:
                offenders.append(f"{rel}:{lineno}  {line.strip()[:70]}")
    assert not offenders, (
        "provider family re-listed instead of imported:\n  "
        + "\n  ".join(offenders)
        + "\n\nImport GOOGLE_PROVIDERS / OPENAI_PROVIDERS from model_registry."
    )


@pytest.mark.asyncio
async def test_a_real_gemini_decision_reaches_the_ledger(tmp_path, monkeypatch):
    """The behaviour, not just the source. `google` is the name the registry
    assigns; it must be accepted."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    import sqlite3

    from llm_router import cost

    db = await cost._get_db()
    await db.close()

    kw = dict(
        prompt="what is 2+2", task_type="query", profile="fast",
        classifier_type="signals", classifier_model=None,
        classifier_confidence=0.9, classifier_latency_ms=1.0,
        complexity="simple", recommended_model="gemini-2.5-pro",
        base_model="gemini-2.5-pro", was_downshifted=False,
        budget_pct_used=0.0, quality_mode="balanced",
        final_model="gemini-2.5-pro", success=True,
        input_tokens=10, output_tokens=5, cost_usd=0.01, latency_ms=100.0,
    )
    await cost.log_routing_decision(final_provider="google", **kw)

    conn = sqlite3.connect(str(tmp_path / "usage.db"))
    got = [r[0] for r in conn.execute(
        "SELECT final_provider FROM routing_decisions")]
    conn.close()
    assert "google" in got, (
        "a Gemini decision tagged with the registry's own provider name did "
        "not reach routing_decisions"
    )


@pytest.mark.asyncio
async def test_an_unknown_provider_is_still_rejected(tmp_path, monkeypatch):
    """Anti-vacuity: widening the set must not disable the guard."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    from llm_router import cost

    db = await cost._get_db()
    await db.close()
    with pytest.raises(ValueError):
        await cost.log_routing_decision(
            final_provider="nonsense-provider",
            prompt="x", task_type="query", profile="fast",
            classifier_type="signals", classifier_model=None,
            classifier_confidence=0.9, classifier_latency_ms=1.0,
            complexity="simple", recommended_model="m", base_model="m",
            was_downshifted=False, budget_pct_used=0.0,
            quality_mode="balanced", final_model="m", success=True,
            input_tokens=1, output_tokens=1, cost_usd=0.0, latency_ms=1.0,
        )


# ── isolation, end to end ────────────────────────────────────────────────────

@pytest.mark.parametrize("module, fn", [
    ("llm_router.direct_diagnostics", "_samples_path"),
    ("llm_router.seats", "seats_path"),
])
def test_state_resolvers_honour_llm_router_home(tmp_path, monkeypatch, module, fn):
    """Both of these escaped into the operator's real home before R11."""
    import importlib

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    m = importlib.import_module(module)
    resolved = getattr(m, fn)()
    assert str(resolved).startswith(str(tmp_path)), (
        f"{module}.{fn}() resolved to {resolved}, outside LLM_ROUTER_HOME"
    )


def test_the_router_fallback_cannot_drift():
    """`router._google_providers()` carries a literal fallback. Pin it.

    A copy that is allowed to diverge is exactly how `cost.py` came to reject
    `'google'`. This makes divergence a test failure rather than a silent
    whole-provider gap in the ledger.
    """
    from llm_router.model_registry import GOOGLE_PROVIDERS
    from llm_router.router import _google_providers

    assert _google_providers() == GOOGLE_PROVIDERS, (
        "router's fallback provider set has drifted from model_registry's"
    )


def test_the_provider_scan_can_actually_fire():
    """Anti-vacuity: a threshold set too high protects nothing."""
    from llm_router.model_registry import GOOGLE_PROVIDERS

    fake = "X = frozenset({" + ", ".join(f'"{n}"' for n in sorted(GOOGLE_PROVIDERS)) + "})"
    named = sum(1 for n in GOOGLE_PROVIDERS if f'"{n}"' in fake)
    assert named >= max(2, len(GOOGLE_PROVIDERS) - 1), (
        "a full re-listing of the family would not trip the scan"
    )
    # and an ordinary mention must NOT trip it
    ordinary = '_PAID_PROVIDERS = {"openai", "gemini", "perplexity"}'
    named2 = sum(1 for n in GOOGLE_PROVIDERS if f'"{n}"' in ordinary)
    assert named2 < max(2, len(GOOGLE_PROVIDERS) - 1), (
        "the scan would flag an unrelated concept that merely mentions gemini"
    )


def test_llm_router_home_beats_HOME(tmp_path, monkeypatch):
    """The isolation contract, pinned.

    R11 routed `seats.json` and friends through `paths.state_path()`, which
    made LLM_ROUTER_HOME authoritative over a patched HOME. Two test files had
    encoded the opposite, so this states the rule once rather than leaving it
    implicit in whichever fixture happens to set which variable.
    """
    import importlib

    other = tmp_path / "real-home"
    other.mkdir()
    monkeypatch.setenv("HOME", str(other))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "isolated"))

    from llm_router import paths, seats
    importlib.reload(paths)
    importlib.reload(seats)

    resolved = seats.seats_path()
    assert str(tmp_path / "isolated") in str(resolved), (
        f"LLM_ROUTER_HOME did not win over HOME: {resolved}"
    )
    assert str(other) not in str(resolved)
