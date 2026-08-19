"""Regression: CHZ-AUD-C-04 — `llm_router verify` must check EVERY installed hook,
derived from the install manifest (_HOOK_DEFS), not a hardcoded subset."""
import os
import pathlib

from llm_router.install_hooks import _HOOK_DEFS
from llm_router.commands.verify import check_hooks


def test_verify_checks_all_installed_hooks(tmp_path, monkeypatch):
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    hooks = tmp_path / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    all_dst = [dst for (_s, dst, _e, _m) in _HOOK_DEFS]
    # Install every hook EXCEPT one (session-end), all executable.
    missing = "llm_router-session-end.py"
    for dst in all_dst:
        if dst == missing:
            continue
        f = hooks / dst
        f.write_text("#!/usr/bin/env python3\n")
        os.chmod(f, 0o755)

    ok, messages = check_hooks()
    joined = "\n".join(messages)
    # The check must cover ALL manifest hooks (one message per hook).
    for dst in all_dst:
        assert dst in joined, f"C-04: verify did not check installed hook {dst}"
    # And it must catch the missing one (not report all-good).
    assert ok is False
    assert any(missing in m and "not found" in m for m in messages)


def test_verify_covers_more_than_the_old_three():
    all_dst = {dst for (_s, dst, _e, _m) in _HOOK_DEFS}
    assert len(all_dst) > 3, "manifest should have >3 hooks (C-04 was a 3-of-N gap)"
