"""P2.12-c (v16.0 floor): the real upgrade (install.run_full), discovery and 3 forced rescans.

On 6 fixture HOMEs the sha256 of every user-owned file is identical after the upgrade and
after the rescans, and a write recorder sees 0 writes to those paths. Files compared are
printed (>= 20 in total). "User-owned" is defined in tests/fixtures/dyn/homes.py.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from llm_router import auto_profile as ap
from llm_router import discover, seats
from llm_router import model_discovery as md
from llm_router.commands import install
from tests import test_p015_user_files_untouched as rec
from tests.fixtures.dyn import homes
from tests.fixtures.dyn.fake_ollama import FakeLocalServer


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _tree(home: Path) -> dict[str, str]:
    return {str(p.relative_to(home)): _sha(p) for p in home.rglob("*") if p.is_file() and p.name != "usage.db"}


def _declared(rel: str) -> bool:
    return rel in homes.GENERATED or rel.split("/")[0] in homes.GENERATED_DIRS


def run_upgrade(root: Path, monkeypatch, shape: str, *, tamper: bool = False) -> tuple[int, list[str]]:
    home, owned = rec.build(root, shape, monkeypatch)
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: root))
    monkeypatch.chdir(root)
    monkeypatch.setattr("llm_router.discover.get_cached_ollama_models", lambda: ["ollama/old-a:7b"])
    world = {"n": 0}
    monkeypatch.setattr(ap, "detect_services", lambda: {
        "claude_subscription": True, "claude_quota_remaining": 10.0 * world["n"], "ollama_available": world["n"] % 2 == 0})
    ap.save_detection_cache(ap.detect_services())
    monkeypatch.setattr(seats, "refresh_seats", lambda *a, **k: (_ for _ in ()).throw(OSError("no probe")))
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
        if tamper and owned:
            with open(owned[0], "ab") as f:                                 # negative control
                f.write(b"x")
        writes = list(rec._WRITES)
    finally:
        rec._WRITES = None
    assert after_upgrade == before, f"{shape}: user-owned file changed by the upgrade"
    assert {p: _sha(p) for p in owned} == before, f"{shape}: changed after rescans"
    assert writes == [], f"{shape}: writes to user-owned paths: {writes}"
    undeclared = [n for n, d in _tree(home).items() if tree_before.get(n) != d and not _declared(n)
                  and not (shape == "owned" and n == "profile.yaml")]  # router-written there (P0.15)
    print(f"P2.12-c {shape}: compared {len(owned)} file(s): {[p.name for p in owned]}")
    return len(owned), undeclared


def test_six_homes_upgrade_preserves_user_owned_files(tmp_path, monkeypatch):
    results = {s: run_upgrade(tmp_path / s, monkeypatch, s) for s in rec.SHAPES}
    total = sum(n for n, _ in results.values())
    assert len(results) == 6 and total >= 20, total
    print(f"P2.12-c total files compared: {total}")
    # everything the router created or changed is declared generated (so the list is complete)
    assert {s: u for s, (_, u) in results.items() if u} == {}


def test_negative_control_a_changed_user_file_is_caught(tmp_path, monkeypatch):
    with pytest.raises(AssertionError):
        run_upgrade(tmp_path / "edited", monkeypatch, "edited", tamper=True)


def test_user_owned_and_generated_are_disjoint():
    assert not set(homes.USER_OWNED) & set(homes.GENERATED)
