"""Three shipped surfaces that could not work — T-18, T-20, S-07.

Each is the same shape: code that ships, is reachable, and fails or lies on
every invocation, with no test exercising it.

* **T-18** `llm-router profile` imported `PROFILE_PATH`, which does not exist —
  `auto_profile` exposes `_profile_path()`, a function, because the repo-wide
  import-time path sweep replaced the constant. 100% broken. `dev-refresh`
  shelled out to `llm_router-install-hooks`; the registered script is
  `llm-router-install-hooks`. `tui` dumped a raw `ModuleNotFoundError` for a
  DECLARED optional extra.
* **T-20** `quota_tracker` queried `provider = 'gemini'`. Every Gemini model is
  tagged `provider = 'google'`, so real Gemini spend was $0 by construction.
* **S-07** `run_verifier` executes a GENERATED snippet and handed it
  `dict(os.environ)` — every provider key in the parent process.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))


# ── T-18 ─────────────────────────────────────────────────────────────────────

def test_profile_imports_a_symbol_that_exists():
    """The import that raised on every `llm-router profile` invocation."""
    from llm_router.auto_profile import _profile_path  # noqa: F401
    from llm_router.commands import profile as cmd

    src = pathlib.Path(cmd.__file__).read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.split("\n") if not ln.lstrip().startswith("#"))
    assert "PROFILE_PATH" not in code.replace("_profile_path", ""), (
        "commands/profile.py still imports the non-existent PROFILE_PATH"
    )


def test_profile_runs(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    from llm_router.commands.profile import cmd_profile

    assert cmd_profile([]) == 0


def test_dev_refresh_calls_the_registered_script_name():
    """`llm_router-install-hooks` (underscores) is not what pyproject registers."""
    import tomllib

    from llm_router.commands import dev_refresh

    root = pathlib.Path(__file__).resolve().parents[1]
    scripts = tomllib.loads((root / "pyproject.toml").read_text(
        encoding="utf-8"))["project"]["scripts"]

    src = pathlib.Path(dev_refresh.__file__).read_text(encoding="utf-8")
    assert '["llm-router-install-hooks"]' in src
    assert "llm-router-install-hooks" in scripts, (
        "the hyphenated name is not registered either — check pyproject"
    )
    assert '["llm_router-install-hooks"]' not in src


def test_tui_names_its_optional_extra_instead_of_raising(monkeypatch):
    """A declared optional extra must not present as a crash.

    `textual` being absent is by design on a default install. A raw
    ModuleNotFoundError traceback reads like a bug in llm-router.
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.argv=['llm-router','tui'];"
         "from llm_router.cli import main; main()"],
        capture_output=True, text=True, cwd=str(root),
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(root / "src"),
             "NO_COLOR": "1", "HOME": str(pathlib.Path.home())},
    )
    combined = proc.stdout + proc.stderr
    if "No module named 'textual'" not in combined and proc.returncode == 0:
        pytest.skip("textual is installed here; nothing to assert")
    assert "Traceback" not in combined, f"tui still raises a raw traceback:\n{combined}"
    assert "textual" in combined
    assert "pip install" in combined


# ── T-20 ─────────────────────────────────────────────────────────────────────

def test_the_google_provider_family_is_defined_once():
    from llm_router.model_registry import GOOGLE_PROVIDERS

    assert "google" in GOOGLE_PROVIDERS, "the name the registry actually assigns"
    assert "gemini" in GOOGLE_PROVIDERS, "the name the old query used"


def test_every_registered_gemini_model_is_covered():
    """Anti-vacuity: the set must match what the registry really tags.

    A hand-written family that drifts from the registry is the defect itself,
    one rename later.
    """
    from llm_router.model_registry import GOOGLE_PROVIDERS, _BUNDLED_DEFAULTS as MODELS

    providers = {m.provider for m in MODELS if "gemini" in m.id.lower()}
    assert providers, "no Gemini models in the registry — this test proves nothing"
    assert providers <= GOOGLE_PROVIDERS, (
        f"registry tags Gemini models {providers - GOOGLE_PROVIDERS}, "
        "which the spend query would miss"
    )


def test_the_router_does_not_keep_its_own_copy():
    import inspect

    from llm_router import router

    src = inspect.getsource(router)
    assert '{"gemini", "google", "google_subscription"' not in src


# ── S-07 ─────────────────────────────────────────────────────────────────────

def test_a_generated_verifier_cannot_read_provider_keys(monkeypatch):
    """The snippet is model-authored. It must not inherit the parent's secrets."""
    from groundtruth.verifiers import run_verifier

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-CANARY")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-CANARY2")
    # Deliberately a name no denylist knows: an allowlist must exclude it anyway.
    monkeypatch.setenv("TOTALLY_UNKNOWN_SECRET", "canary3")

    probe = (
        "import os\n"
        "leaked = [k for k in ('ANTHROPIC_API_KEY','OPENAI_API_KEY',"
        "'TOTALLY_UNKNOWN_SECRET') if k in os.environ]\n"
        "assert not leaked, leaked\n"
    )
    accepted, why = run_verifier(probe, "x")
    assert accepted, f"the verifier subprocess inherited provider keys: {why}"


def test_the_verifier_still_receives_what_it_needs():
    """Anti-vacuity: an empty environment would pass the test above and break
    every real verifier."""
    from groundtruth.verifiers import run_verifier

    accepted, why = run_verifier(
        "import os; assert os.environ['BENCH_ANSWER'] == 'the answer'", "the answer")
    assert accepted, f"BENCH_ANSWER did not reach the subprocess: {why}"
