"""MAP-FIX: writers with different environments must not flap the shared map.

Measured on a real host (map_changes.jsonl, 2026-10-13..25): 623 codex and 622
claude status events, 58/57 OPENROUTER added/removed. The launchd proxy (PATH
/usr/bin:/bin:/usr/sbin:/sbin) wrote "claude CLI not found"; MCP servers and
hook children (full PATH) wrote "connected"; a fresh process missed the key
that lives only in ``$LLM_ROUTER_HOME/openrouter.key``. No real CLI runs here:
every binary is a fake in tmp_path.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

from llm_router import config, map_rediscovery, resource_map, seats
from llm_router.discovery import keys
from llm_router.discovery.keys import KeyInfo

MINIMAL_PATH = "/usr/bin:/bin"


def _exe(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def _no_http(*_a, **_k):
    raise OSError("no network in tests")


@pytest.fixture
def state(tmp_path, monkeypatch):
    home = tmp_path / "state"
    home.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    monkeypatch.setattr(seats, "FALLBACK_DIRS", ())   # pass only via the learned file
    monkeypatch.setattr(seats, "LEARN_NAMES", (*seats.LEARN_NAMES, "fakenode"))
    monkeypatch.setattr(seats, "INTERPRETERS", ("fakenode",))
    return home


@pytest.fixture
def fake_clis(tmp_path):
    """claude (native-like sh script) and codex as `#!/usr/bin/env fakenode`."""
    bin_dir, node_dir = tmp_path / "bin", tmp_path / "nodebin"
    record = tmp_path / "probe_path.txt"
    _exe(bin_dir / "claude",
         '#!/bin/sh\necho \'{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "max"}\'\n')
    _exe(bin_dir / "codex", "#!/usr/bin/env fakenode\n")
    _exe(node_dir / "fakenode", f'#!/bin/sh\necho "$PATH" > "{record}"\necho "Logged in using ChatGPT"\n')
    return bin_dir, node_dir, record


def _learn_from_full_path(bin_dir: Path, node_dir: Path) -> None:
    full = {"PATH": f"{bin_dir}{os.pathsep}{node_dir}{os.pathsep}{MINIMAL_PATH}"}
    for name in ("claude", "codex", "fakenode"):
        assert seats.find_cli(name, env=full)


def _detect(tmp_path) -> seats.Seats:
    return seats.detect_seats(home=tmp_path, env={}, opener=_no_http)


def test_minimal_path_finds_learned_clis_and_gives_node_to_the_probe(state, fake_clis, tmp_path, monkeypatch):
    bin_dir, node_dir, record = fake_clis
    _learn_from_full_path(bin_dir, node_dir)
    monkeypatch.setenv("PATH", MINIMAL_PATH)
    assert shutil.which("claude") is None and shutil.which("codex") is None   # non-vacuous

    found = _detect(tmp_path)

    assert found.claude.status == "connected" and found.claude.kind == "claude.ai"
    assert found.codex.status == "connected" and found.codex.kind == "chatgpt"
    assert str(node_dir) in record.read_text().strip().split(os.pathsep)


def test_without_the_learned_file_minimal_path_says_not_found(state, fake_clis, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", MINIMAL_PATH)
    found = _detect(tmp_path)
    assert found.claude.status == "absent" and found.claude.reason == "claude CLI not found"


def test_learned_file_is_private(state, fake_clis):
    _learn_from_full_path(*fake_clis[:2])
    path = state / seats.CLI_PATH_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = json.loads(path.read_text())
    assert str(fake_clis[0]) in data["dirs"] and "claude" in data["dirs"][str(fake_clis[0])]["names"]


def _inputs(found: seats.Seats, tmp_path, key_rows: list) -> resource_map.MapInputs:
    return resource_map.MapInputs(
        seats=found, env={}, http_get=lambda _u: None, keys=lambda: key_rows,
        policy="balanced", codex_sessions=tmp_path / "no-sessions",
        claude_reading=lambda: (None, "absent", None), codex_counter=lambda: None,
        gemini_quota=lambda: None, competence={},
    )


def _poll(found, tmp_path, now, key_rows):
    return map_rediscovery.poll(inputs=_inputs(found, tmp_path, key_rows), now=now,
                                paths=[], map_path=tmp_path / "state" / "resource_map.json",
                                state_dir=tmp_path / "state", force=True)


OR_KEY = [KeyInfo("OPENROUTER_API_KEY", "openrouter", "env", ("llm-router",), "unknown")]


def _changes(state: Path) -> list[dict]:
    return [json.loads(x) for x in (state / map_rediscovery.CHANGES_FILE).read_text().splitlines()]


def test_two_writers_with_different_paths_do_not_flap(state, fake_clis, tmp_path, monkeypatch):
    bin_dir, node_dir, _ = fake_clis
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{node_dir}{os.pathsep}{MINIMAL_PATH}")
    t0 = time.time()
    _poll(_detect(tmp_path), tmp_path, t0, [])                 # full-PATH writer
    monkeypatch.setenv("PATH", MINIMAL_PATH)                   # launchd-shaped writer
    _poll(_detect(tmp_path), tmp_path, t0 + 60, OR_KEY)        # the key add forces a line

    lines = _changes(state)
    assert lines, "no change line written: the writer check below would be vacuous"
    assert not [e for e in lines if e["event"] == "status" and e["resource"] in ("claude", "codex")]
    assert all(set(e["writer"]) == {"pid", "entry", "path_sig"} for e in lines)
    data = resource_map.load_map(state / "resource_map.json")
    assert data["writer"]["pid"] == os.getpid()


def test_not_found_does_not_overwrite_a_recent_connected_but_age_caps_it(state, tmp_path):
    seen = seats.Seats(claude=seats.Seat(kind="claude.ai", plan="max"), codex=seats.Seat(kind="chatgpt"))
    blind = seats.Seats(claude=seats.Seat(reason="claude CLI not found"),
                        codex=seats.Seat(reason="codex CLI not found"))
    t0 = time.time() - 3 * 3600
    _poll(seen, tmp_path, t0, [])
    _poll(blind, tmp_path, t0 + 60, OR_KEY)
    rows = {r["resource"]: r for r in resource_map.load_map(state / "resource_map.json")["resources"]}
    assert rows["claude"]["status"] == "connected"
    assert rows["claude"]["carried_from"]["new_reason"] == "claude CLI not found"
    assert not [e for e in _changes(state) if e["event"] == "status"]

    _poll(blind, tmp_path, t0 + 2 * 3600, [])                  # past CARRY_MAX_AGE_S
    status = {e["resource"]: e["after"] for e in _changes(state) if e["event"] == "status"}
    assert status == {"claude": "absent", "codex": "absent"}


def test_real_logout_is_not_carried(state, tmp_path):
    seen = seats.Seats(claude=seats.Seat(kind="claude.ai"))
    out = seats.Seats(claude=seats.Seat(status="installed_not_connected", reason="Not logged in"))
    t0 = time.time()
    _poll(seen, tmp_path, t0, [])
    _poll(out, tmp_path, t0 + 60, [])
    assert [e["after"] for e in _changes(state) if e["resource"] == "claude"] == ["installed_not_connected"]


def test_disk_key_file_is_discovered_and_build_is_the_same_before_and_after_load(state, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_state_dir", lambda: state)   # conftest points it elsewhere
    (state / "openrouter.key").write_text("sk-test-not-real\n")
    monkeypatch.setenv("OPENROUTER_API_KEY", "placeholder")
    monkeypatch.delenv("OPENROUTER_API_KEY")                   # undo restores "absent"
    monkeypatch.setattr(keys, "keychain_has", lambda _v: False)
    monkeypatch.chdir(tmp_path)

    found = {k.var: k for k in keys.discover(env={}, dotenv_paths=[], keychain=lambda _v: False)}
    assert found["OPENROUTER_API_KEY"].source == f"keyfile:{state / 'openrouter.key'}"

    def key_rows():
        inp = _inputs(seats.Seats(), tmp_path, [])
        inp.keys, inp.env = None, dict(os.environ)
        return [r for r in resource_map.build(inp)["resources"] if r["resource"].startswith("api/")]

    before = key_rows()
    assert "OPENROUTER_API_KEY" in config.load_disk_keys()
    assert os.environ["OPENROUTER_API_KEY"] == "sk-test-not-real"
    assert key_rows() == before
    assert any(r["resource"] == "api/OPENROUTER_API_KEY" for r in before)
    assert all("sk-test" not in json.dumps(r) for r in before)


@pytest.mark.parametrize("exc, words", [
    (subprocess.TimeoutExpired(["claude"], 3.0), "timed out"),
    (OSError("exec format error"), "failed to launch"),
])
def test_probe_failure_has_its_own_reason(exc, words, tmp_path):
    def runner(_argv, _timeout):
        raise exc
    found = seats.detect_seats(runner=runner, which=lambda _n: None, env={}, home=tmp_path,
                               opener=_no_http)
    for seat in (found.claude, found.codex):
        assert "CLI not found" not in seat.reason and words in seat.reason
        assert seat.status == "installed_not_connected"
        assert seats.no_evidence(seat.status, seat.reason)


def test_learned_dir_expires_after_its_binary_is_removed(state, tmp_path):
    bin_dir = tmp_path / "gone"
    _exe(bin_dir / "claude", "#!/bin/sh\n")
    t0 = time.time()
    assert seats.find_cli("claude", env={"PATH": str(bin_dir)}, now=t0)
    (bin_dir / "claude").unlink()

    assert str(bin_dir) in seats.learned_dirs(t0 + 100)        # non-vacuous
    late = t0 + seats.LEARNED_MAX_AGE_S + 1
    assert str(bin_dir) not in seats.learned_dirs(late)
    assert str(bin_dir) not in seats.search_path({"PATH": MINIMAL_PATH}, late).split(os.pathsep)
    assert seats.find_cli("claude", env={"PATH": MINIMAL_PATH}, now=late) is None


def test_carry_recent_for_the_rescan_path():
    now = time.time()
    iso = lambda t: seats.datetime.fromtimestamp(t, tz=seats.timezone.utc).isoformat()  # noqa: E731
    prior = seats.Seats(claude=seats.Seat(kind="claude.ai"), detected_at=iso(now - 600))
    blind = seats.Seats(claude=seats.Seat(reason="claude CLI not found"), detected_at=iso(now))
    assert seats.carry_recent(blind, prior, now).claude.status == "connected"
    old = seats.Seats(claude=seats.Seat(kind="claude.ai"), detected_at=iso(now - 7200))
    assert seats.carry_recent(blind, old, now) is blind
