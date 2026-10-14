"""P2.12-c (v16.0 floor): the real upgrade (install.run_full), discovery and 3 forced rescans.

On 6 fixture HOMEs the sha256 of every user-owned file is identical after the upgrade and
after the rescans, and a write recorder sees 0 writes to those paths. Files compared are
printed (>= 20 in total). "User-owned" is defined in tests/fixtures/dyn/homes.py.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from types import SimpleNamespace

from llm_router import auto_profile as ap
from llm_router import discover, host_detect, seats
from llm_router import model_discovery as md
from llm_router.commands import install
from tests import test_p015_user_files_untouched as rec
from tests.fixtures.dyn import homes
from tests.fixtures.dyn.fake_ollama import FakeLocalServer


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _tree(home: Path) -> dict[str, str]:
    return {str(p.relative_to(home)): _sha(p) for p in home.rglob("*") if p.is_file() and p.name != "usage.db"}


def undeclared_changes(tree_before: dict[str, str], tree_after: dict[str, str], shape: str) -> list[str]:
    """Changed or created files that no rule declares generated.

    A file under a ``GENERATED_NEW_ONLY_DIRS`` dir (claude-home, claude-desktop) may be
    created by the router, but one that already existed is user-edited and is protected by sha.
    """
    def declared(rel: str) -> bool:
        top = rel.split("/")[0]
        return (rel in homes.GENERATED or top in homes.GENERATED_DIRS
                or (top in homes.GENERATED_NEW_ONLY_DIRS and rel not in tree_before))
    return [n for n, d in tree_after.items() if tree_before.get(n) != d and not declared(n)
            and not (shape == "owned" and n == "profile.yaml")]  # router-written there (P0.15)


def _pin_host_detection(monkeypatch, root: Path) -> None:
    """Which hosts are wired must not depend on the developer's PATH."""
    detect = host_detect.detect_hosts
    monkeypatch.setattr(host_detect, "detect_hosts",
                        lambda **k: detect(**{"home": root, "which": lambda b: None, **k}))


def run_upgrade(root: Path, monkeypatch, shape: str, *, inject: str | None = None,
                seed: str | None = None, fail_undeclared: bool = False) -> tuple[int, list[str]]:
    """``inject``: ``rewrite`` (same bytes, in the recorder window), ``append-window`` (in the
    window, after the rescans) or ``append-after`` (after the recorder window closed).
    ``seed``: a user file written under the home before the run (path relative to the home)."""
    home, owned = rec.build(root, shape, monkeypatch)
    if seed:
        (home / seed).parent.mkdir(parents=True, exist_ok=True)
        (home / seed).write_bytes(b'{"user": true}\n')
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: root))
    monkeypatch.chdir(root)
    monkeypatch.setattr("llm_router.discover.get_cached_ollama_models", lambda: ["ollama/old-a:7b"])
    world = {"n": 0}
    monkeypatch.setattr(ap, "detect_services", lambda: {
        "claude_subscription": True, "claude_quota_remaining": 10.0 * world["n"], "ollama_available": world["n"] % 2 == 0})
    ap.save_detection_cache(ap.detect_services())
    monkeypatch.setattr(seats, "refresh_seats", lambda *a, **k: (_ for _ in ()).throw(OSError("no probe")))
    _pin_host_detection(monkeypatch, root)
    before = {p: _sha(p) for p in owned}
    tree_before = _tree(home)
    rec._WATCH.clear()
    rec._WATCH.update(str(p) for p in owned)
    rec._WRITES = []
    try:
        install.run_full(interactive=False)                                 # the upgrade
        after_upgrade = {p: _sha(p) for p in owned}
        md.ensure_grandfather(["ollama/old-a:7b"])
        with FakeLocalServer(["old-a:7b", "new-z:7b"]) as srv:              # discovery
            monkeypatch.setenv("OLLAMA_HOST", srv.url)
            md._write_cache(md.probe_ollama())
            discover._update_discovery_cache([{"name": n} for n in srv.models])
        for _ in range(3):                                                  # 3 forced rescans
            world["n"] += 1
            ap.rescan_and_update()
        if inject == "rewrite":
            owned[0].write_bytes(owned[0].read_bytes())                     # negative control: sha same, write seen
        elif inject == "append-window":
            with open(owned[0], "ab") as f:                                 # negative control: sha changes in the window
                f.write(b"x")
        writes = list(rec._WRITES)
    finally:
        rec._WRITES = None
    if inject == "append-after":
        with open(owned[0], "ab") as f:                                     # negative control: after the window
            f.write(b"x")
    assert after_upgrade == before, f"{shape}: user-owned file changed by the upgrade"
    assert {p: _sha(p) for p in owned} == before, f"{shape}: changed after rescans"
    assert writes == [], f"{shape}: writes to user-owned paths: {writes}"
    undeclared = undeclared_changes(tree_before, _tree(home), shape)
    assert not (fail_undeclared and undeclared), f"undeclared change: {undeclared}"
    print(f"P2.12-c {shape}: compared {len(owned)} file(s): {[p.name for p in owned]}")
    return len(owned), undeclared


def test_six_homes_upgrade_preserves_user_owned_files(tmp_path, monkeypatch):
    results = {s: run_upgrade(tmp_path / s, monkeypatch, s) for s in rec.SHAPES}
    total = sum(n for n, _ in results.values())
    assert len(results) == 6 and total >= 20, total
    print(f"P2.12-c total files compared: {total}")
    # everything the router created or changed is declared generated (so the list is complete)
    assert {s: u for s, (_, u) in results.items() if u} == {}


@pytest.mark.parametrize("inject,message", [
    ("append-window", "changed after rescans"),
    ("append-after", "changed after rescans"),
    ("rewrite", "writes to user-owned paths"),
])
def test_negative_control_each_injection_fails_with_its_own_message(tmp_path, monkeypatch, inject, message):
    with pytest.raises(AssertionError, match=message):
        run_upgrade(tmp_path / "edited", monkeypatch, "edited", inject=inject)


def test_negative_control_write_to_user_edited_settings_json_is_undeclared(tmp_path, monkeypatch):
    with pytest.raises(AssertionError, match=r"undeclared change:.*claude-home/settings\.json"):
        run_upgrade(tmp_path / "edited", monkeypatch, "edited",
                    seed="claude-home/settings.json", fail_undeclared=True)


def test_undeclared_changes_protects_preexisting_files_in_new_only_dirs():
    before = {"claude-home/settings.json": "a", "hooks/x": "a"}
    after = {"claude-home/settings.json": "b", "hooks/x": "b", "claude-home/rules/r.md": "c",
             "claude-desktop/c.json": "c", "notes.txt": "n"}
    assert undeclared_changes(before, after, "edited") == ["claude-home/settings.json", "notes.txt"]


def test_user_owned_and_generated_are_disjoint():
    assert not set(homes.USER_OWNED) & set(homes.GENERATED)


def test_seat_present_upgrade_only_appends_the_subscription_flag(tmp_path, monkeypatch):
    """The common upgrade: a Claude seat is logged in. ``.env`` is user-owned with one
    declared exception (homes.py): the flag is appended once when it was never set.
    Every other user-owned file is untouched, and a second run changes nothing."""
    root = tmp_path / "seat"
    home, owned = rec.build(root, "upgrade", monkeypatch)
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: root))
    monkeypatch.chdir(root)
    monkeypatch.delenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", raising=False)
    monkeypatch.setattr("llm_router.discover.get_cached_ollama_models", lambda: [])
    monkeypatch.setattr(ap, "detect_services", lambda: {
        "claude_subscription": True, "claude_quota_remaining": 10.0, "ollama_available": False})
    found = SimpleNamespace(claude=SimpleNamespace(present=True), summary_line=lambda: "claude",
                            free_bucket=lambda: ["claude"])
    monkeypatch.setattr(seats, "refresh_seats", lambda *a, **k: found)
    _pin_host_detection(monkeypatch, root)
    env = home / ".env"
    env_before = env.read_bytes() if env.exists() else b""
    before = {p: _sha(p) for p in owned if p != env}

    install.run_full(interactive=False)

    assert {p: _sha(p) for p in owned if p != env} == before
    assert env.read_bytes().startswith(env_before)
    assert env.read_bytes()[len(env_before):].strip() == b"LLM_ROUTER_CLAUDE_SUBSCRIPTION=true"
    snapshot = {p: _sha(p) for p in [*owned, env] if p.exists()}
    install.run_full(interactive=False)                                      # idempotent
    assert {p: _sha(p) for p in [*owned, env] if p.exists()} == snapshot
