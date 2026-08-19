"""RED4-01 (P0) — install must not destroy a statusLine it does not own.

`install()` assigned straight over `settings.json["statusLine"]`. A user with
their own status line — Powerline, a custom script, anything — lost it silently:
no backup, no warning, and `uninstall` deleted the key rather than restoring it,
so the loss was permanent in both directions.

The severity is not about one JSON key. It is that a *router* reached into an
unrelated part of the user's editor configuration and overwrote it, and that
nothing in install, uninstall, or the test suite noticed. These tests are the
part that notices.

Every test resolves its paths inside tmp_path and asserts it, per the plan's
safety rule: the audit's own DB tests destroyed real user data by writing to a
HOME that was not as isolated as it looked.
"""

from __future__ import annotations

import json

import pytest


CUSTOM_STATUSLINE = {
    "type": "command",
    "command": "~/.config/my-powerline/render.sh --fancy",
    "padding": 1,
}


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Point every install surface inside tmp_path and prove it landed there."""
    import llm_router.install_hooks as ih
    import llm_router.install_manifest as im

    claude = tmp_path / ".claude"
    hooks_dst = claude / "hooks"
    settings_path = claude / "settings.json"
    manifest_path = tmp_path / ".llm-router" / "install-manifest.json"

    monkeypatch.setattr(ih, "_HOOKS_DST", hooks_dst)
    monkeypatch.setattr(ih, "_SETTINGS_PATH", settings_path)
    monkeypatch.setattr(ih, "_RULES_DST", claude / "rules")
    monkeypatch.setattr(ih, "_CLAUDE_DIR", claude)
    monkeypatch.setattr(im, "_manifest_path", lambda: manifest_path)

    # The safety assertion the plan requires: if any of these escaped tmp_path we
    # would be editing the operator's real config.
    for p in (hooks_dst, settings_path, manifest_path):
        assert str(p).startswith(str(tmp_path)), f"{p} escaped the tmpdir"

    settings_path.parent.mkdir(parents=True, exist_ok=True)

    class _Sandbox:
        def __init__(self) -> None:
            self.ih = ih
            self.im = im
            self.settings_path = settings_path
            self.hooks_dst = hooks_dst
            self.manifest_path = manifest_path

        def write_settings(self, data: dict) -> None:
            settings_path.write_text(json.dumps(data, indent=2))

        def read_settings(self) -> dict:
            return json.loads(settings_path.read_text())

    return _Sandbox()


def test_foreign_statusline_is_recorded_before_install_overwrites_it(sandbox):
    sandbox.write_settings({"statusLine": CUSTOM_STATUSLINE, "model": "opus"})

    sandbox.ih.install()

    rec = sandbox.im.find("json_key", sandbox.settings_path, key="statusLine")
    assert rec is not None, "install overwrote statusLine without recording the original"
    assert rec["had_key"] is True
    assert rec["previous"] == CUSTOM_STATUSLINE


def test_install_warns_when_it_overwrites_a_foreign_statusline(sandbox):
    """Silence is the defect. The user must be told at install time."""
    sandbox.write_settings({"statusLine": CUSTOM_STATUSLINE})

    actions = sandbox.ih.install()

    warnings = [a for a in actions if "WARNING" in a and "statusLine" in a]
    assert warnings, f"no warning about replacing statusLine; got: {actions}"


def test_uninstall_restores_the_original_byte_for_byte(sandbox):
    original = {"statusLine": CUSTOM_STATUSLINE, "model": "opus"}
    sandbox.write_settings(original)
    before = json.dumps(original["statusLine"], sort_keys=True)

    sandbox.ih.install()
    assert sandbox.read_settings()["statusLine"] != CUSTOM_STATUSLINE  # llm_router's now

    sandbox.ih.uninstall()

    restored = sandbox.read_settings()["statusLine"]
    assert json.dumps(restored, sort_keys=True) == before
    # And nothing else in the file was collateral damage.
    assert sandbox.read_settings()["model"] == "opus"


def test_uninstall_removes_the_key_when_there_was_none_before(sandbox):
    """The mirror case: a user who had NO status line must not be left with one."""
    sandbox.write_settings({"model": "opus"})

    sandbox.ih.install()
    assert "statusLine" in sandbox.read_settings()

    sandbox.ih.uninstall()
    assert "statusLine" not in sandbox.read_settings()


def test_idempotent_across_install_install_uninstall_uninstall(sandbox):
    """A second install must not re-capture llm_router's own value as "the original".

    This is the subtle way the fix could fail while looking correct: install once
    and the original is safe; install twice and the capture is overwritten with
    llm_router's replacement, so uninstall restores llm_router's own status line and the
    user's is gone. The restore path would still report success.
    """
    sandbox.write_settings({"statusLine": CUSTOM_STATUSLINE})

    sandbox.ih.install()
    sandbox.ih.install()

    rec = sandbox.im.find("json_key", sandbox.settings_path, key="statusLine")
    assert rec["previous"] == CUSTOM_STATUSLINE, "second install clobbered the capture"

    sandbox.ih.uninstall()
    assert sandbox.read_settings()["statusLine"] == CUSTOM_STATUSLINE

    # A second uninstall is a no-op, not a crash and not a re-deletion.
    sandbox.ih.uninstall()
    assert sandbox.read_settings()["statusLine"] == CUSTOM_STATUSLINE


def test_llm_routers_own_statusline_is_not_recorded_as_a_users(sandbox):
    """Installing over a previous llm_router install must capture nothing."""
    sandbox.write_settings(
        {"statusLine": {"type": "command", "command": "bash /somewhere/llm_router-statusline.sh"}}
    )

    sandbox.ih.install()

    rec = sandbox.im.find("json_key", sandbox.settings_path, key="statusLine")
    assert rec is None, "llm_router's own statusLine was captured as if it were the user's"


def test_uninstall_leaves_no_file_in_the_hooks_directory(sandbox):
    """RED4-08: hook *support* modules carry no event/matcher, so the removal
    loop keyed on _HOOK_DEFS never saw them and they were orphaned on disk."""
    sandbox.write_settings({})

    sandbox.ih.install()
    assert sandbox.hooks_dst.exists()

    sandbox.ih.uninstall()

    leftovers = [p.name for p in sandbox.hooks_dst.rglob("*") if p.is_file()]
    assert leftovers == [], f"uninstall left files behind in ~/.claude/hooks: {leftovers}"


def test_a_backup_of_settings_exists_after_a_destructive_install(sandbox):
    """The manifest is not the only line of defence — the file itself is copied.

    If the manifest is lost (deleted ~/.llm-router, a failed write, a user tidying
    up), the recorded original goes with it. A backup on disk survives that.
    """
    sandbox.write_settings({"statusLine": CUSTOM_STATUSLINE})

    sandbox.ih.install()

    backups = list(sandbox.settings_path.parent.glob("settings.json*.bak"))
    assert backups, "no backup written before overwriting a foreign statusLine"
    assert any(
        json.loads(b.read_text()).get("statusLine") == CUSTOM_STATUSLINE for b in backups
    ), "a backup exists but none of them contain the original statusLine"
