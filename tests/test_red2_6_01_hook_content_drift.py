"""Regression: RED2-6-01 / RED2-6-03 — hook & rules auto-update must be
CONTENT-aware, not purely version-stamp-gated.

`check_and_update_hooks()`/`check_and_update_rules()` re-copied only when the
bundled version stamp was strictly newer. A hook/rules file whose behaviour
changed without a stamp bump (a repeated real slip that stranded even security
fixes on installed machines) never propagated. They now also re-copy when the
stamps match but the installed bytes differ — while never downgrading.
"""
from __future__ import annotations

import llm_router.install_hooks as ih


def _setup(tmp_path, monkeypatch):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    monkeypatch.setattr(ih, "_HOOKS_SRC", src)
    monkeypatch.setattr(ih, "_HOOKS_DST", dst)
    monkeypatch.setattr(ih, "_SETTINGS_PATH", tmp_path / "settings.json")
    # Neutralize the legacy-alias sync (needs settings/other files we don't stub).
    monkeypatch.setattr(ih, "_sync_legacy_hook_alias", lambda *a, **k: None)
    # Single managed hook for the test.
    monkeypatch.setattr(ih, "_HOOK_DEFS", [("h.py", "llm_router-h.py", "SessionStart", "")])
    return src / "h.py", dst / "llm_router-h.py"


def test_content_drift_at_same_version_refreshes(tmp_path, monkeypatch):
    src, dst = _setup(tmp_path, monkeypatch)
    src.write_text("# llm_router-hook-version: 5\nNEW behaviour (fixed)\n")
    dst.write_text("# llm_router-hook-version: 5\nOLD behaviour (buggy)\n")

    msgs = ih.check_and_update_hooks()

    assert dst.read_text() == src.read_text(), "RED2-6-01: content drift not propagated"
    assert any("Refreshed" in m and "drift" in m for m in msgs), msgs


def test_drift_backs_up_the_previous_file(tmp_path, monkeypatch):
    """RED1-7-02: a hand-edited managed hook must be backed up (not silently
    destroyed) before a content-drift overwrite, and the backup named in output."""
    src, dst = _setup(tmp_path, monkeypatch)
    src.write_text("# llm_router-hook-version: 5\nbundled\n")
    user_edit = "# llm_router-hook-version: 5\nUSER CUSTOMIZATION\n"
    dst.write_text(user_edit)

    msgs = ih.check_and_update_hooks()

    bak = dst.with_suffix(dst.suffix + ".bak")
    assert bak.exists(), "RED1-7-02: no backup made before overwrite"
    assert bak.read_text() == user_edit, "backup does not contain the user's edit"
    assert dst.read_text() == src.read_text(), "file not refreshed to bundled"
    assert any(".bak" in m for m in msgs), f"backup not surfaced in output: {msgs}"


def test_rules_drift_backs_up_previous(tmp_path, monkeypatch):
    rsrc = tmp_path / "rsrc"
    rdst = tmp_path / "rdst"
    rsrc.mkdir()
    rdst.mkdir()
    monkeypatch.setattr(ih, "_RULES_SRC", rsrc)
    monkeypatch.setattr(ih, "_RULES_DST", rdst)
    (rsrc / "llm_router.md").write_text("<!-- llm_router-rules-version: 7 -->\nbundled\n")
    user_edit = "<!-- llm_router-rules-version: 7 -->\nMY ORG RULES\n"
    (rdst / "llm_router.md").write_text(user_edit)

    msg = ih.check_and_update_rules()

    bak = (rdst / "llm_router.md").with_suffix(".md.bak")
    assert bak.exists() and bak.read_text() == user_edit, "rules edit not backed up"
    assert ".bak" in (msg or "")


def test_backup_failure_skips_overwrite(tmp_path, monkeypatch):
    """RED1-8-02: if the backup cannot be written, the overwrite must be SKIPPED
    (never destroy a hand-edited file with no recovery path)."""
    src, dst = _setup(tmp_path, monkeypatch)
    src.write_text("# llm_router-hook-version: 5\nbundled\n")
    user_edit = "# llm_router-hook-version: 5\nUSER EDIT\n"
    dst.write_text(user_edit)
    monkeypatch.setattr(ih, "_backup_before_overwrite", lambda d: None)  # simulate backup failure

    msgs = ih.check_and_update_hooks()

    assert dst.read_text() == user_edit, "RED1-8-02: overwrite proceeded despite backup failure"
    assert any("SKIPPED" in m for m in msgs), f"no skip signal: {msgs}"


def test_second_drift_does_not_clobber_first_bak(tmp_path, monkeypatch):
    """RED1-8-03/RED2-8-02: a second drift must not overwrite the first .bak."""
    src, dst = _setup(tmp_path, monkeypatch)
    src.write_text("# llm_router-hook-version: 5\nbundled\n")
    edit1 = "# llm_router-hook-version: 5\nEDIT ONE\n"
    dst.write_text(edit1)
    ih.check_and_update_hooks()  # refresh #1 → .bak holds edit1

    bak = dst.with_suffix(dst.suffix + ".bak")
    assert bak.read_text() == edit1
    # user re-edits, then a second drift refresh happens
    edit2 = "# llm_router-hook-version: 5\nEDIT TWO\n"
    dst.write_text(edit2)
    ih.check_and_update_hooks()  # refresh #2

    assert bak.read_text() == edit1, "RED1-8-03: first .bak clobbered — original edit lost"
    # edit2 preserved somewhere (a timestamped .bak)
    others = [p for p in dst.parent.glob(dst.name + ".*bak") if p != bak]
    assert any(p.read_text() == edit2 for p in others), "second edit not preserved"


def test_identical_content_is_a_noop(tmp_path, monkeypatch):
    src, dst = _setup(tmp_path, monkeypatch)
    body = "# llm_router-hook-version: 5\nsame\n"
    src.write_text(body)
    dst.write_text(body)
    # CHZ-SURF-01: the installer also syncs the stdlib-only tool_surface support
    # module next to the hooks. Sync it first so the state really IS identical —
    # otherwise this test asserts a noop against a genuinely-missing file.
    ih._sync_hook_support_files()
    msgs = ih.check_and_update_hooks()
    assert msgs == [], "no update expected when content is identical"


def test_newer_version_still_updates(tmp_path, monkeypatch):
    src, dst = _setup(tmp_path, monkeypatch)
    src.write_text("# llm_router-hook-version: 6\nv6\n")
    dst.write_text("# llm_router-hook-version: 5\nv5\n")
    msgs = ih.check_and_update_hooks()
    assert dst.read_text() == src.read_text()
    assert any("→ v6" in m or "v5 → v6" in m for m in msgs), msgs


def test_never_downgrades_a_newer_installed_hook(tmp_path, monkeypatch):
    """src_v < dst_v (dev/newer installed) must be left untouched even if content differs."""
    src, dst = _setup(tmp_path, monkeypatch)
    src.write_text("# llm_router-hook-version: 5\nolder bundled\n")
    dst.write_text("# llm_router-hook-version: 9\nnewer installed\n")
    ih.check_and_update_hooks()
    assert dst.read_text() == "# llm_router-hook-version: 9\nnewer installed\n"


def test_rules_content_drift_refreshes(tmp_path, monkeypatch):
    rsrc = tmp_path / "rsrc"
    rdst = tmp_path / "rdst"
    rsrc.mkdir()
    rdst.mkdir()
    monkeypatch.setattr(ih, "_RULES_SRC", rsrc)
    monkeypatch.setattr(ih, "_RULES_DST", rdst)
    (rsrc / "llm_router.md").write_text("<!-- llm_router-rules-version: 7 -->\nNEW rules\n")
    (rdst / "llm_router.md").write_text("<!-- llm_router-rules-version: 7 -->\nOLD rules\n")

    msg = ih.check_and_update_rules()

    assert (rdst / "llm_router.md").read_text() == (rsrc / "llm_router.md").read_text()
    assert msg and "drift" in msg, msg


def test_rules_identical_is_noop(tmp_path, monkeypatch):
    rsrc = tmp_path / "rsrc"
    rdst = tmp_path / "rdst"
    rsrc.mkdir()
    rdst.mkdir()
    monkeypatch.setattr(ih, "_RULES_SRC", rsrc)
    monkeypatch.setattr(ih, "_RULES_DST", rdst)
    body = "<!-- llm_router-rules-version: 7 -->\nsame\n"
    (rsrc / "llm_router.md").write_text(body)
    (rdst / "llm_router.md").write_text(body)
    assert ih.check_and_update_rules() is None
