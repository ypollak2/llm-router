"""First-run defects observed while switching this machine to llm-router (2026-08-31).

Each test here reproduces a defect that a fresh install actually exhibits. Per the
bug-triage-and-fix contract: the repro must fail before the patch and pass after.

  task 09 — a fresh install has no usage.json, so the statusline reports quota it
            does not have instead of saying the data is missing.
  task 10 — backups are written next to the destination and never pruned; 697
            files / 17 MB were observed in ~/.claude on this host.
  task 12 — __version__ prefers installed dist metadata, so a source checkout
            reports whatever was last pip-installed (13.0.4 vs a 13.0.8 tree).
  task 13 — provider keys saved under ~/.llm-router/*.key are never read into the
            environment, so the router reports the provider as unconfigured.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ── task 09 ────────────────────────────────────────────────────────────────────


def test_quota_absent_is_not_reported_as_zero(tmp_path, monkeypatch):
    """A missing usage.json must read as 'unknown', never as a real 0%.

    status_premium swallowed FileNotFoundError into {} and then rendered
    session/weekly/sonnet as 0.0% alongside hardcoded 'remaining' strings, so a
    fresh install showed invented numbers with nothing marking them unknown.
    """
    from llm_router.ui import status_premium as sp

    cmd = sp.PremiumStatusCommand()
    monkeypatch.setattr(cmd, "usage_json", tmp_path / "definitely-absent.json")

    pressure = cmd.load_pressure()

    assert pressure is None, (
        "a missing usage.json must be distinguishable from real zero pressure; "
        f"got {pressure!r}"
    )


def test_quota_present_is_parsed(tmp_path, monkeypatch):
    from llm_router.ui import status_premium as sp

    usage = tmp_path / "usage.json"
    usage.write_text(json.dumps({"session_pct": 29.0, "weekly_pct": 17.0, "sonnet_pct": 0.0}))

    cmd = sp.PremiumStatusCommand()
    monkeypatch.setattr(cmd, "usage_json", usage)

    pressure = cmd.load_pressure()
    assert pressure is not None
    assert pressure["session_pct"] == 29.0


def test_install_seeds_usage_json(tmp_path, monkeypatch):
    """`llm-router install` must leave a usage.json behind.

    Otherwise quota — the headline feature — is blank until some PostToolUse hook
    happens to fire, with no indication to the user that anything is pending.
    """
    from llm_router import install_hooks as ih

    state = tmp_path / ".llm-router"
    monkeypatch.setattr(ih, "STATE_DIR", state, raising=False)

    written = ih.seed_usage_json()

    assert written.exists(), "install did not seed usage.json"
    data = json.loads(written.read_text())
    assert data.get("pending") is True, (
        "the seed must mark itself as not-yet-refreshed so the statusline can say "
        "'waiting for first refresh' rather than showing a fabricated 0%"
    )


# ── task 10 ────────────────────────────────────────────────────────────────────


def test_backups_are_bounded(tmp_path, monkeypatch):
    """Repeated drift must not grow ~/.claude without limit."""
    from llm_router import install_hooks as ih

    monkeypatch.setattr(ih, "STATE_DIR", tmp_path / ".llm-router", raising=False)

    dst = tmp_path / "hooks" / "a-hook.py"
    dst.parent.mkdir(parents=True)
    dst.write_text("v0")

    for i in range(12):
        dst.write_text(f"v{i}")
        assert ih._backup_before_overwrite(dst) is not None

    siblings = list(dst.parent.glob("*.bak*"))
    assert len(siblings) <= 1, (
        "timestamped backups must not pile up next to the destination; "
        f"found {len(siblings)} in the hooks dir"
    )

    total = len(list((tmp_path / ".llm-router" / "backups").glob("*"))) + len(siblings)
    assert total <= ih.MAX_BACKUPS_PER_FILE + 1, (
        f"backups are unbounded: {total} retained after 12 drift events"
    )


def test_first_backup_is_still_never_clobbered(tmp_path, monkeypatch):
    """RED1-8-03 must survive the bounding change: the first capture is sacred."""
    from llm_router import install_hooks as ih

    monkeypatch.setattr(ih, "STATE_DIR", tmp_path / ".llm-router", raising=False)

    dst = tmp_path / "hooks" / "a-hook.py"
    dst.parent.mkdir(parents=True)
    dst.write_text("FIRST EDIT")
    ih._backup_before_overwrite(dst)

    for i in range(10):
        dst.write_text(f"later {i}")
        ih._backup_before_overwrite(dst)

    primary = dst.with_suffix(dst.suffix + ".bak")
    assert primary.exists(), "the first .bak disappeared"
    assert primary.read_text() == "FIRST EDIT", "the first captured edit was lost"


# ── task 12 ────────────────────────────────────────────────────────────────────


def test_version_matches_the_running_source_tree():
    """A source checkout must report its own version, not the last pip install."""
    import tomllib

    import llm_router

    src_root = Path(llm_router.__file__).resolve().parents[2]
    pyproject = src_root / "pyproject.toml"
    if not pyproject.exists():
        pytest.skip("not running from a source checkout")

    declared = tomllib.load(pyproject.open("rb"))["project"]["version"]
    assert llm_router.__version__ == declared, (
        f"running source is {declared} but __version__ reports "
        f"{llm_router.__version__} — bug reports will carry the wrong version"
    )


# ── task 13 ────────────────────────────────────────────────────────────────────


def test_on_disk_provider_keys_are_loaded(tmp_path, monkeypatch):
    """A key saved to ~/.llm-router/<provider>.key must reach the environment.

    openrouter.key was present and populated on this host while the doctor kept
    reporting OPENROUTER_API_KEY as unset, leaving the largest provider pool dark.
    """
    from llm_router import config as cfg

    state = tmp_path / ".llm-router"
    state.mkdir()
    (state / "openrouter.key").write_text("FAKE-KEY-FOR-TEST-ONLY\n")

    monkeypatch.setattr(cfg, "STATE_DIR", state, raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    loaded = cfg.load_disk_keys()

    assert "OPENROUTER_API_KEY" in loaded
    assert loaded["OPENROUTER_API_KEY"] == "FAKE-KEY-FOR-TEST-ONLY"


def test_disk_keys_never_override_the_environment(tmp_path, monkeypatch):
    """An explicitly exported key wins over a stale file on disk."""
    from llm_router import config as cfg

    state = tmp_path / ".llm-router"
    state.mkdir()
    (state / "openrouter.key").write_text("FAKE-KEY-STALE-ON-DISK\n")

    monkeypatch.setattr(cfg, "STATE_DIR", state, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "FAKE-KEY-FROM-ENVIRONMENT")

    cfg.load_disk_keys()

    import os

    assert os.environ["OPENROUTER_API_KEY"] == "FAKE-KEY-FROM-ENVIRONMENT"


# ── task 11 ────────────────────────────────────────────────────────────────────


def test_orphaned_own_hooks_are_reaped(tmp_path, monkeypatch):
    """Hooks dropped from _HOOK_DEFS between versions must not linger forever."""
    from llm_router import install_hooks as ih

    hooks = tmp_path / "hooks"
    hooks.mkdir()
    monkeypatch.setattr(ih, "_HOOKS_DST", hooks)
    monkeypatch.setattr(ih, "_HOOK_DEFS", [("live.py", "llm_router-live.py", "Stop", "")])

    live = hooks / "llm_router-live.py"
    orphan = hooks / "llm_router-removed-last-version.py"
    live.write_text("# current")
    orphan.write_text("# shipped in an older release, defined by nothing now")

    orphans = ih._orphaned_managed_hooks()

    assert orphan in orphans, "a hook no longer in _HOOK_DEFS was not detected as orphaned"
    assert live not in orphans, "the live hook must never be reaped"


def test_reaper_never_touches_another_products_hooks(tmp_path, monkeypatch):
    """Deleting a competing tool's files is the overreach that caused #94. """
    from llm_router import install_hooks as ih

    hooks = tmp_path / "hooks"
    hooks.mkdir()
    monkeypatch.setattr(ih, "_HOOKS_DST", hooks)
    monkeypatch.setattr(ih, "_HOOK_DEFS", [("live.py", "llm_router-live.py", "Stop", "")])

    foreign = hooks / "otherrouter-auto-route.py"
    foreign.write_text("# belongs to another product")

    assert foreign not in ih._orphaned_managed_hooks()
    assert foreign.exists()


def test_a_second_router_is_reported_not_deleted():
    """Two routers on UserPromptSubmit double-classify every prompt."""
    from llm_router import install_hooks as ih

    settings = {
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [{"command": "/x/.venv/bin/python /y/otherrouter-auto-route.py"}]},
                {"hooks": [{"command": "/x/.venv/bin/python /y/llm_router-auto-route.py"}]},
            ]
        }
    }

    rival = ih._competing_router_hooks(settings)

    assert len(rival) == 1
    assert "otherrouter-auto-route.py" in rival[0]
    assert not any("llm_router" in c for c in rival), "our own hooks must never be flagged"


# ── task 41 ────────────────────────────────────────────────────────────────────


def _statusline_source() -> str:
    from llm_router import install_hooks as ih

    return (ih._PACKAGE_DIR / "hooks" / "statusline-command.sh").read_text()


def test_statusline_probes_the_real_cli_name():
    """The money figure silently vanished because the CLI name was wrong.

    The interpreter-resolution loop probed `command -v llm_router` (underscore).
    The installed executable is `llm-router` (hyphen), so the highest-signal
    candidate — the interpreter behind the user's own install — never matched.
    On a host where no other candidate could import llm_router, the savings
    figure disappeared with no error, because the loop is wrapped in a
    never-break-the-statusline fallback.
    """
    src = _statusline_source()
    assert "command -v llm-router" in src, (
        "statusline does not probe the hyphenated CLI name, so it cannot find "
        "the interpreter behind a normal install"
    )


def test_statusline_labels_the_money_as_saved():
    """A bare dollar figure beside a quota percentage reads as spend.

    It is the opposite: savings versus an all-premium baseline, summed over
    every session since local midnight.
    """
    src = _statusline_source()
    assert "saved" in src.split("💰")[1][:80], (
        "the money segment must say 'saved' — an unlabelled figure next to a "
        "quota percentage is read as money spent"
    )


def test_statusline_reports_today_not_the_session():
    """Today means every session since midnight, via the canonical union."""
    src = _statusline_source()
    assert 'query_window("today"' in src, (
        "the statusline must use the canonical today-window aggregation, which "
        "unions all five usage tables; a per-session figure under-reports"
    )


# ── task 33 ────────────────────────────────────────────────────────────────────


def test_session_summary_labels_money_as_saved():
    """The most-screenshotted line this product produces must not read as spend.

    The condensed summary renders `saved today $X · lifetime $Y · quota used
    5h N%`. Before this, the money bits were bare (`today $32.85`) and sat
    inches from a quota percentage — the identical inversion a user hit on the
    statusline, where an unlabelled figure was read as money spent.
    """
    import importlib.util

    from llm_router import install_hooks as ih

    spec = importlib.util.spec_from_file_location(
        "session_end_under_test", ih._PACKAGE_DIR / "hooks" / "session-end.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # A captured excerpt of the real box, not an invented fixture — the module's
    # own docstring records that a hand-written fixture once kept these tests
    # green while the line printed no money at all.
    summary = (
        "  1153 decisions routed\n"
        "  today $32.88\n"
        "  lifetime $52.19\n"
        "  5h 16%  weekly 18%\n"
    )
    line = mod._condense(summary)

    assert "saved today" in line, (
        f"the money figure is unlabelled and will be read as spend: {line!r}"
    )
    assert "quota used" in line, "quota label lost"


def test_session_summary_is_on_by_default():
    """A summary nobody sees cannot be the thing people share."""
    import importlib.util
    import os

    from llm_router import install_hooks as ih

    spec = importlib.util.spec_from_file_location(
        "session_end_default_mode", ih._PACKAGE_DIR / "hooks" / "session-end.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    os.environ.pop("LLM_ROUTER_STOP_HOOK", None)
    assert mod._stop_hook_mode() == "condensed", (
        "the session summary must render without opt-in; it is the product's "
        "most shareable artifact"
    )


def test_unmeasured_quota_panel_still_names_itself():
    """The placeholder must say what it is a placeholder FOR.

    Removing the fabricated 0% left a panel mentioning neither usage nor a
    percentage, so a caller checking that the status surfaces usage saw nothing
    and a reader saw what looked like a missing feature. CI caught it; a machine
    with a populated usage.json never would have.
    """
    from llm_router.ui import status_premium as sp

    cmd = sp.PremiumStatusCommand()
    cmd.usage_json = cmd.state_dir / "definitely-absent-usage.json"

    from rich.console import Console

    console = Console(record=True, width=100)
    console.print(cmd.render_subscription_quotas())
    out = console.export_text().lower()

    assert "usage" in out, f"unmeasured quota panel does not mention usage: {out!r}"
    assert "0%" not in out, "the fabricated zero is back"
