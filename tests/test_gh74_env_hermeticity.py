"""GH-74 pin: importing ``llm_router.providers`` must not let litellm's
implicit ``dotenv.load_dotenv()`` splice ancestor-directory secrets into the
real process environment.

Root cause: ``litellm/__init__.py`` calls ``dotenv.load_dotenv()`` (no path
argument) on first import, unless ``LITELLM_MODE`` is already set to
something other than its "DEV" default. ``load_dotenv()`` with no path calls
python-dotenv's ``find_dotenv()``, which does an *upward filesystem search*
starting from the caller's own source file -- i.e. from litellm's install
location deep inside site-packages, not from this repo and not from
``$HOME`` -- so on any machine where an ancestor directory of the venv
happens to hold a ``.env`` (a developer's personal ``~/.env``, or -- as this
test proves -- even the repo root itself), importing litellm quietly mutates
the real ``os.environ`` for the rest of the interpreter's life.

That is what made ``tests/test_t3_s2_max_wall_clock_seconds.py`` and
``tests/test_t4_m2_classification_allowlist.py`` fail on a clean local
checkout while passing in CI: on the reporting developer's machine, a
personal ``~/.env`` with a real ``XAI_API_KEY`` sits above this repo's
``.venv``, so ``RouterConfig.available_providers`` ended up as ``{"xai"}``
purely from that accidental import-order side effect -- not from anything
the test, the shell, ``$HOME``, or ``RouterConfig`` itself did. CI has no
such ancestor ``.env``, so it never saw the leak.

Fix: ``llm_router/providers.py`` sets ``LITELLM_MODE=PROD`` (via
``os.environ.setdefault``, so an operator's own explicit choice always wins)
before importing litellm, opting out of litellm's ambient dotenv load
entirely -- both in production and in tests.

Both tests below run the probe as a real ``.py`` *file* in a subprocess,
never via ``python -c``. That distinction is load-bearing: python-dotenv's
``find_dotenv()`` only does the upward walk from the caller's real source
file when ``__main__`` has a ``__file__`` attribute; under ``python -c``,
``__main__`` has none, so ``find_dotenv()`` silently falls back to
``os.getcwd()`` instead -- a completely different, CWD-driven code path that
would validate the wrong mechanism (and, for
``test_available_providers_unaffected_by_ancestor_dotenv``, would rig the
test to pass regardless of the fix, since ``tmp_path`` is nowhere near the
ancestor ``.env``). A real invocation of ``pytest`` -- and therefore of every
test this issue is about -- always has a ``__main__.__file__``, so the
frame-walk path is the one that matters.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_MARKER_VALUE = "gh74-ancestor-dotenv-marker-should-never-leak"


def _clean_subprocess_env() -> dict[str, str]:
    """A copy of this process's env with every provider-key var removed.

    Stripping only ``XAI_API_KEY`` is not enough: this test file runs inside
    a pytest session where tests/conftest.py's own GH-74 autouse fixture
    (``_isolate_provider_api_keys``) sets ``OPENAI_API_KEY=test-key`` on the
    real ``os.environ`` for the duration of every test (that is how
    ``monkeypatch.setenv`` works). Without stripping the full provider set, a
    subprocess spawned from inside a test would inherit that fixture's
    ``OPENAI_API_KEY`` and no longer be a clean baseline -- exactly the kind
    of ambient-state confound this whole issue is about.
    """
    import llm_router.config as config_module

    provider_map = config_module.RouterConfig.__private_attributes__["_PROVIDER_MAP"].default
    strip = set()
    for field_name, (_, litellm_var) in provider_map.items():
        strip.add(field_name.upper())
        strip.add(litellm_var)
    return {k: v for k, v in os.environ.items() if k not in strip}


def _run_script(code: str, *, cwd: Path, script_dir: Path) -> subprocess.CompletedProcess:
    """Run ``code`` as a real ``.py`` file (never ``python -c``) in a
    subprocess with a fully scrubbed provider-key environment.

    ``script_dir`` is where the throwaway script file itself lives (its
    ``__file__`` is what python-dotenv's ``find_dotenv`` would walk up from
    if IT were the caller -- it never is here, litellm is -- so this choice
    doesn't affect what's under test); ``cwd`` is the subprocess's working
    directory, which only matters for ``RouterConfig``'s own separate
    CWD-relative ``env_file`` entry, not for litellm's mechanism.
    """
    script_path = script_dir / "probe.py"
    script_path.write_text(textwrap.dedent(code))
    return subprocess.run(
        [sys.executable, str(script_path)],
        cwd=str(cwd),
        env=_clean_subprocess_env(),
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.fixture
def ancestor_dotenv():
    """Create ``<repo root>/.env`` with a marker key; remove it afterward.

    Skips (never overwrites) if a real ``.env`` is already present at the
    repo root -- only ``.env.example`` is meant to be tracked there, but this
    must never clobber a developer's real file if one exists.
    """
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        pytest.skip(f"{env_path} already exists; refusing to overwrite a real file")
    env_path.write_text(f"XAI_API_KEY={_MARKER_VALUE}\n")
    try:
        yield env_path
    finally:
        env_path.unlink(missing_ok=True)


def test_importing_providers_does_not_leak_ancestor_dotenv_into_os_environ(
    ancestor_dotenv: Path, tmp_path: Path
) -> None:
    """The GH-74 hermeticity property, pinned directly against the real
    mechanism: importing ``llm_router.providers`` (and therefore litellm)
    must never mutate ``os.environ`` from a ``.env`` file that sits above the
    installed package in the filesystem.

    Without the fix, litellm's unconditional ``load_dotenv()`` walks up from
    its own install location, crosses the repo root, finds the fixture's
    ``.env`` there, and injects ``XAI_API_KEY`` into the subprocess's real
    ``os.environ`` -- exactly the mechanism that produced
    ``Configured providers: {'xai'}`` in GH-74. The probe's own CWD
    (``tmp_path``, outside the repo) is deliberately *not* where the marker
    lives, so a pass here can only be explained by litellm's own mechanism
    being neutralized -- not by accidentally missing the file some other way.

    Mutation check: reverting the ``LITELLM_MODE`` line in
    ``llm_router/providers.py`` makes this test fail (the marker key leaks
    through); restoring it makes it pass again -- verified by hand.
    """
    result = _run_script(
        """
        import os, sys
        assert "XAI_API_KEY" not in os.environ, "harness leaked XAI_API_KEY into the subprocess before import"
        import llm_router.providers  # noqa: F401 -- triggers litellm's own import, unguarded
        leaked = os.environ.get("XAI_API_KEY")
        print("LEAKED=" + repr(leaked))
        sys.exit(1 if leaked else 0)
        """,
        cwd=tmp_path,
        script_dir=tmp_path,
    )
    assert result.returncode == 0, (
        "importing llm_router.providers leaked the ancestor .env's XAI_API_KEY "
        "into os.environ -- litellm's implicit dotenv auto-load is not "
        f"neutralized. stdout={result.stdout!r} stderr={result.stderr[-2000:]!r}"
    )


def test_available_providers_unaffected_by_ancestor_dotenv(
    ancestor_dotenv: Path, tmp_path: Path
) -> None:
    """End-to-end version of the same property, through the exact code path
    GH-74 actually broke: ``RouterConfig.available_providers`` inside a
    fresh process that imports the router (and therefore litellm) with the
    ancestor ``.env``'s ``XAI_API_KEY`` present and no other provider
    configured. Before the fix this includes ``"xai"``; after, it does not.

    The probe's CWD is ``tmp_path`` -- outside the repo -- specifically so
    ``RouterConfig.model_config['env_file']``'s own second, bare-relative
    ``'.env'`` entry (which pydantic-settings resolves against the process
    CWD, a real, separate, and long-standing hermeticity wrinkle that
    predates GH-74 and is arguably an intentional "project-local .env"
    convenience for CLI users) cannot also pick up the fixture's repo-root
    ``.env`` and confound the result. This isolates exactly the litellm
    auto-load path -- the same isolation the first test achieves by scrubbing
    ``os.environ`` directly instead of relying on CWD.

    Mutation check: reverting the ``LITELLM_MODE`` line in
    ``llm_router/providers.py`` makes this test fail with
    ``PROVIDERS={'xai'}``; restoring it makes it pass again -- verified by
    hand (a strict subset-check, not blank-set equality, avoids coupling
    this to unrelated ambient providers like a locally-running Ollama).
    """
    result = _run_script(
        """
        import os, sys
        # Must go through the router (which imports llm_router.providers, which
        # imports litellm) -- llm_router.config alone never imports litellm, so
        # constructing a bare RouterConfig() would never exercise this
        # mechanism at all (see llm_router/router.py's own import chain).
        import llm_router.router  # noqa: F401
        from llm_router.config import RouterConfig
        providers = RouterConfig().available_providers
        print("PROVIDERS=" + repr(providers))
        sys.exit(1 if "xai" in providers else 0)
        """,
        cwd=tmp_path,
        script_dir=tmp_path,
    )
    assert result.returncode == 0, (
        "RouterConfig().available_providers picked up the ancestor .env's "
        f"XAI_API_KEY via litellm's implicit dotenv load: stdout={result.stdout!r} "
        f"stderr={result.stderr[-2000:]!r}"
    )
