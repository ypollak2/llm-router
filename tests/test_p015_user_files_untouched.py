"""T9f (floor): on 6 fixture HOMEs no user-owned file changes through upgrade, discovery and 3 rescans.

Checked two ways: the sha256 of every user-owned file before and after, and a write
recorder (a process-wide audit hook) that must see 0 writes to those paths.
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

from llm_router import auto_profile as ap
from llm_router import discover
from llm_router import model_discovery as md
from tests.fixtures.dyn import homes
from tests.fixtures.dyn.fake_ollama import FakeLocalServer

_WRITES: list[str] | None = None
_WATCH: set[str] = set()
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_TRUNC | os.O_CREAT


def _hook(event: str, args: tuple) -> None:
    if _WRITES is None:
        return
    try:
        if event == "open":
            path, mode, flags = args
            writing = bool(flags & _WRITE_FLAGS) if isinstance(flags, int) else False
            if isinstance(mode, str) and any(c in mode for c in "wax+"):
                writing = True
            if writing and isinstance(path, (str, bytes, os.PathLike)) and os.fsdecode(path) in _WATCH:
                _WRITES.append(f"open {os.fsdecode(path)}")
        elif event in ("os.rename", "os.replace"):
            dst = args[1]
            if isinstance(dst, (str, bytes, os.PathLike)) and os.fsdecode(dst) in _WATCH:
                _WRITES.append(f"{event} -> {os.fsdecode(dst)}")
        elif event == "os.remove" and os.fsdecode(args[0]) in _WATCH:
            _WRITES.append(f"remove {os.fsdecode(args[0])}")
    except Exception:                                        # noqa: BLE001 - a recorder must never break the code under test
        return


sys.addaudithook(_hook)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


SHAPES = ["upgrade", "edited", "crlf", "empty", "nonyaml", "owned"]


def build(root: Path, shape: str, monkeypatch) -> tuple[Path, list[Path]]:
    """A fixture HOME and the files in it the router does not own."""
    if shape in ("edited", "owned"):
        home = homes.make_home(root, "upgrade")
        monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
        (home / "profile.yaml").unlink()
        ap.save_profile(ap.generate_profile_yaml({"claude_subscription": True, "claude_quota_remaining": 80.0}))
        if shape == "edited":
            p = home / "profile.yaml"
            p.write_bytes(p.read_bytes() + b"overrides:\n  code:\n    force_quality: true  # mine\n")
    else:
        home = homes.make_home(root, shape)
        monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    owned = [home / n for n in homes.USER_OWNED if (home / n).exists()]
    if shape == "owned":
        owned = [p for p in owned if p.name != "profile.yaml"]    # the router wrote it, so it may refresh it
    return home, owned


def run_scenario(root: Path, monkeypatch, shape: str) -> int:
    global _WRITES
    home, owned = build(root, shape, monkeypatch)
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    monkeypatch.setattr("llm_router.discover.get_cached_ollama_models", lambda: ["ollama/old-a:7b"])
    world = {"n": 0}
    monkeypatch.setattr(ap, "detect_services", lambda: {
        "claude_subscription": True, "claude_quota_remaining": 10.0 * world["n"], "ollama_available": world["n"] % 2 == 0})
    ap.save_detection_cache(ap.detect_services())
    before = {p: _sha(p) for p in owned}
    _WATCH.clear()
    _WATCH.update(str(p) for p in owned)
    _WRITES = []
    try:
        md.ensure_grandfather(["ollama/old-a:7b"])                          # the upgrade
        with FakeLocalServer(["old-a:7b", "new-z:7b"]) as srv:              # discovery
            monkeypatch.setenv("OLLAMA_HOST", srv.url)
            md._write_cache(md.probe_ollama())
            discover._update_discovery_cache([{"name": n} for n in srv.models])
        for _ in range(3):                                                  # 3 forced rescans
            world["n"] += 1
            ap.rescan_and_update()
        writes = list(_WRITES)
    finally:
        _WRITES = None
    for p in owned:
        assert _sha(p) == before[p], f"{shape}: {p.name} changed"
    assert writes == [], f"{shape}: writes to user-owned paths: {writes}"
    print(f"T9f {shape}: compared {len(owned)} file(s): {[p.name for p in owned]}")
    return len(owned)


def test_six_homes_no_user_owned_file_changes(tmp_path, monkeypatch, capsys):
    total = sum(run_scenario(tmp_path / s, monkeypatch, s) for s in SHAPES)
    assert len(SHAPES) == 6 and total >= 20, total
    # the edited and legacy profiles carry the regeneration beside them
    def home_of(shape: str) -> Path:
        return next(p for p in (tmp_path / shape).iterdir() if p.is_dir())

    for shape in ("edited", "crlf", "empty", "nonyaml", "upgrade"):
        assert (home_of(shape) / "profile.generated.yaml").exists(), shape
    assert not (home_of("owned") / "profile.generated.yaml").exists()
    print(f"T9f total files compared: {total}")


def test_negative_control_the_old_rescan_turns_it_red(tmp_path, monkeypatch):
    """With the ownership check removed (the pre-P0.15 behaviour) the same scenario fails."""
    monkeypatch.setattr(ap, "profile_state", lambda: "absent")
    with pytest.raises(AssertionError):
        run_scenario(tmp_path / "edited", monkeypatch, "edited")


def test_a_fresh_home_has_no_user_file_and_the_router_creates_only_its_own(tmp_path, monkeypatch):
    home = homes.make_home(tmp_path, "fresh")
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    monkeypatch.setattr("llm_router.discover.get_cached_ollama_models", lambda: [])
    md.ensure_grandfather([])
    assert sorted(p.name for p in home.iterdir()) == ["grandfathered_models.init", "grandfathered_models.json"]
