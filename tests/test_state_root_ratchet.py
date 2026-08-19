"""The state-root ratchet must be able to fail, and `is_isolated()` must not over-claim.

Audit #37. `llm_router.paths` is the one module that honours `LLM_ROUTER_HOME`; 182 places in
`src/llm_router/` resolve `~/.llm-router` themselves and never ask it. `usage.db` alone is reached
~23 different ways.

That is how `session_store.py` read the operator's real session content while a test had
asserted `is_isolated()` and believed itself sandboxed, and how a sandboxed test destroyed
live data (`evidence/AUDITOR_INCIDENT.md`).

Migrating 182 sites relocates every existing user's data and is the owner's call. What is
in scope here is (1) stopping the number growing, and (2) making the guard stop certifying
what it does not govern.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "lint_state_root.py"
_BASELINE = _ROOT / ".llm-router" / "zero-tolerance-audit" / "state_root_baseline.json"


def _gate():
    spec = importlib.util.spec_from_file_location("lint_state_root", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestTheRatchetCanActuallyFail:
    """A gate that cannot fail is the antipattern this audit has found eleven times.

    Proving it, rather than trusting it, is the entire lesson of #13 (G-C had no
    implementation) and #22 (the G4 ratchet grandfathered a can't-fail test).
    """

    def test_it_passes_on_the_current_tree(self):
        r = subprocess.run([sys.executable, str(_SCRIPT)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr

    def test_it_FAILS_when_a_new_direct_resolution_appears(self, tmp_path):
        """The load-bearing assertion. Verified by hand first: dropping one module that
        does `Path.home() / ".llm-router"` into src/llm_router moved it 182 -> 183 and exit 1."""
        g = _gate()
        canary = tmp_path / "llm_router"
        canary.mkdir()
        (canary / "_canary.py").write_text(
            'from pathlib import Path\nX = Path.home() / ".llm-router" / "canary.db"\n'
        )
        g.SRC = canary
        g.REPO = tmp_path
        found = g.scan()
        assert sum(found.values()) == 1, found

    def test_the_expanduser_spelling_is_caught_too(self, tmp_path):
        """Both shapes the survey found. Catching only one would let the other spread."""
        g = _gate()
        canary = tmp_path / "llm_router"
        canary.mkdir()
        (canary / "_canary.py").write_text(
            'import os\nX = os.path.expanduser("~/.llm-router/usage.db")\n'
        )
        g.SRC = canary
        g.REPO = tmp_path
        assert sum(g.scan().values()) == 1

    def test_another_tools_directory_is_NOT_counted(self, tmp_path):
        """`~/.claude`, `~/.cursor` and friends belong to other products. Redirecting
        those because llm_router was sandboxed would be a NEW defect, not a fix."""
        g = _gate()
        canary = tmp_path / "llm_router"
        canary.mkdir()
        (canary / "_canary.py").write_text(
            'from pathlib import Path\n'
            'A = Path.home() / ".claude" / "settings.json"\n'
            'B = Path.home() / ".cursor" / "rules"\n'
        )
        g.SRC = canary
        g.REPO = tmp_path
        assert g.scan() == {}

    def test_paths_py_itself_is_exempt(self, tmp_path):
        """The canonical resolver is where this is SUPPOSED to happen."""
        g = _gate()
        canary = tmp_path / "llm_router"
        canary.mkdir()
        (canary / "paths.py").write_text(
            'from pathlib import Path\nX = Path.home() / ".llm-router"\n'
        )
        g.SRC = canary
        g.REPO = tmp_path
        assert g.scan() == {}


class TestTheBaselineIsHonest:
    def test_the_baseline_records_a_real_backlog_not_an_empty_one(self):
        """A baseline of zero would mean the check found nothing and is decorative.

        This one records 182 across 114 modules. The number IS the finding — audit #22's
        complaint about the G4 ratchet was that it hid a defect while reporting clean, and
        the difference here is that nothing is hidden.
        """
        base = json.loads(_BASELINE.read_text())
        assert base["total"] > 100, (
            "the baseline collapsed — either the check stopped matching, or a migration "
            "happened and the baseline should be re-locked with --write-baseline"
        )
        assert base["total"] == sum(base["by_file"].values())


class TestIsIsolatedDoesNotOverClaim:
    def test_it_only_promises_what_paths_resolves(self, monkeypatch, tmp_path):
        from llm_router import paths

        monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
        assert paths.is_isolated()
        assert paths.llm_router_home() == tmp_path
        assert paths.state_path("usage.db") == tmp_path / "usage.db"

    def test_its_docstring_states_the_limit_rather_than_implying_safety(self):
        """A guard whose scope lives only in an audit document is a guard that will be
        over-trusted by the next reader. The limit belongs where they will look."""
        from llm_router import paths

        doc = paths.is_isolated.__doc__ or ""
        assert "does **not** certify" in doc or "does not certify" in doc
        assert "120" in doc, "the measured count belongs in the docstring, not just a doc"
