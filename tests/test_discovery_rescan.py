"""Rescans use isolated state, fake probes and fake seat detection."""
import json
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_router import discovery_rescan as scan
from llm_router import model_discovery, seats
from llm_router.commands.discover import cmd_discover


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_NO_DETACHED_SPAWN", "1")
    monkeypatch.setenv("LLM_ROUTER_RESCAN_INTERVAL_S", "3600")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    for name in tuple(__import__("os").environ):
        if name.endswith("_API_KEY") or name in seats._API_KEY_VARS:
            monkeypatch.delenv(name)
    for name in ("LLM_ROUTER_OLLAMA_MODEL", "OLLAMA_MODELS", "OLLAMA_BUDGET_MODELS"):
        monkeypatch.delenv(name, raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("unexpected network, subprocess, or real seat detection")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(model_discovery, "probe_ollama", forbidden)
    monkeypatch.setattr(seats, "detect_seats", forbidden)
    monkeypatch.setattr(model_discovery, "_spawn_probe_child", forbidden)
    monkeypatch.setattr(scan.urllib.request, "urlopen", forbidden)


def tags(*items):
    return [{"name": name, "digest": digest} for name, digest in items]


def fake_seats(now=10000, **kwargs):
    return seats.Seats(
        detected_at=datetime.fromtimestamp(now, timezone.utc).isoformat(), **kwargs
    )


def run(models, now=10000, **kwargs):
    return scan.rescan(
        force=True, probe=lambda: models, detect=lambda: fake_seats(now),
        now=now, **kwargs,
    )


def test_cache_and_model_diffs(tmp_path):
    first = run(tags(("fake-a:1b", "d1"), ("fake-b:2b", "d2")))
    assert first.ran and first.first_scan and first.changed
    assert first.ollama_added == ["fake-a:1b", "fake-b:2b"]
    cache = json.loads((tmp_path / "discovery.json").read_text())
    assert set(cache["models"]) == {"ollama/fake-a:1b", "ollama/fake-b:2b"}
    changed = run(tags(("fake-a:1b", "d3"), ("fake-c:3b", "d4")), now=10001)
    assert changed.ollama_added == ["fake-c:3b"]
    assert changed.ollama_removed == ["fake-b:2b"]
    assert changed.ollama_modified == ["fake-a:1b"]
    assert model_discovery._read_cache()[0] == ["fake-a:1b", "fake-c:3b"]
    cache = json.loads((tmp_path / "discovery.json").read_text())
    assert cache["cached_at"] > 0


def test_local_model_reads_updated_cache():
    run(tags(("fake-a:1b", "d1")))
    assert model_discovery.local_model(allow_probe=False) == "fake-a:1b"
    run(tags(("fake-c:3b", "d2")), now=10001)
    assert model_discovery.local_model(allow_probe=False) == "fake-c:3b"


def test_key_names_only(tmp_path, monkeypatch, caplog, capsys):
    monkeypatch.setenv("FAKE_PROVIDER_API_KEY", "SECRETVALUE123")
    (tmp_path / ".env").write_text(
        'export FAKE_FILE_API_KEY="file-secret"\nEMPTY_API_KEY=""\n'
    )
    first = run([])
    assert first.keys_added == ["FAKE_FILE_API_KEY", "FAKE_PROVIDER_API_KEY"]
    assert scan.key_names_present() == first.keys_added
    text = (tmp_path / "discovery.rescan.json").read_text()
    assert "SECRETVALUE123" not in text and "file-secret" not in text
    assert cmd_discover(["--json"]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["key_names"] == first.keys_added
    assert "SECRETVALUE123" not in output and "file-secret" not in output
    assert "SECRETVALUE123" not in caplog.text
    monkeypatch.delenv("FAKE_PROVIDER_API_KEY")
    removed = run([], now=10001)
    assert removed.keys_removed == ["FAKE_PROVIDER_API_KEY"]


def test_seat_status_change():
    run([])
    changed = scan.rescan(
        force=True, probe=lambda: [],
        detect=lambda: fake_seats(
            10001, claude=seats.Seat(status="installed_not_connected")
        ), now=10001,
    )
    assert changed.changed and changed.seats_changed == ["claude"]
    assert seats.load_seats().claude.status == "installed_not_connected"
    assert scan.seat_fingerprint(fake_seats(10000)) == scan.seat_fingerprint(
        fake_seats(20000, ollama=seats.Seat(models=("fake-a:1b",), reason="new"))
    )


def test_cadence_and_attempt_throttle(tmp_path, monkeypatch):
    run([])
    assert scan.due(now=10001) == (False, "not_due")
    assert scan.due(now=13600) == (True, "interval")
    clock = SimpleNamespace(time=lambda: 13600.0)
    monkeypatch.setattr(scan, "time", clock)
    monkeypatch.delenv("LLM_ROUTER_NO_DETACHED_SPAWN")
    calls = []
    monkeypatch.setattr(
        model_discovery, "_spawn_probe_child",
        lambda **kwargs: calls.append(kwargs) or True,
    )
    assert scan.maybe_rescan_async()
    assert not scan.maybe_rescan_async()
    assert calls == [{"module": "llm_router.discovery_rescan"}]
    assert float((tmp_path / "discovery.rescan_attempt").read_text()) == 13600

    run([], now=13600)
    monkeypatch.setenv("FAKE_NEW_API_KEY", "present")
    clock.time = lambda: 13630.0
    assert scan.due() == (True, "keys_changed")
    assert not scan.maybe_rescan_async()
    clock.time = lambda: 13660.0
    assert scan.maybe_rescan_async()
    assert not scan.maybe_rescan_async()
    assert len(calls) == 2


def test_not_due_does_no_work():
    run([])
    calls = {"probe": 0, "detect": 0}

    def probe():
        calls["probe"] += 1
        return []

    def detect():
        calls["detect"] += 1
        return fake_seats()

    result = scan.rescan(probe=probe, detect=detect, now=10001)
    assert not result.ran and not result.changed
    assert calls == {"probe": 0, "detect": 0}


def test_unreachable_preserves_cache(tmp_path):
    run(tags(("fake-a:1b", "d1")))
    cache = (tmp_path / "discovery.json").read_bytes()
    old = json.loads((tmp_path / "discovery.rescan.json").read_text())
    result = run(None, now=10001)
    assert result.ran and not result.ollama_reachable and not result.changed
    assert result.ollama_added == result.ollama_removed == result.ollama_modified == []
    assert (tmp_path / "discovery.json").read_bytes() == cache
    new = json.loads((tmp_path / "discovery.rescan.json").read_text())
    assert new["ollama_fp"] == old["ollama_fp"]
    assert new["ollama_models"] == {"fake-a:1b": "d1"}


def test_first_use_and_unreadable_state(tmp_path):
    assert scan.due(now=10000) == (True, "first_use")
    result = scan.rescan(
        probe=lambda: tags(("fake-a:1b", "d1")),
        detect=lambda: fake_seats(), now=10000,
    )
    assert result.ran and result.first_scan and result.ollama_reachable
    assert result.ollama_added == ["fake-a:1b"]
    assert result.seats_changed == []
    (tmp_path / "discovery.rescan.json").write_text("broken json")
    assert scan.due(now=10001) == (True, "first_use")


def test_discover_command(monkeypatch, capsys):
    original = scan.rescan

    def fake_rescan(force=False):
        assert force
        return original(
            force=True, probe=lambda: tags(("fake-a:1b", "d1")),
            detect=lambda: fake_seats(), now=10000,
        )

    monkeypatch.setattr(scan, "rescan", fake_rescan)
    assert cmd_discover(["--rescan"]) == 0
    output = capsys.readouterr().out
    assert "Models added: 1 (fake-a:1b)" in output
    assert cmd_discover(["--rescan", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ran"] and not result["changed"]
    assert cmd_discover(["--rescan"]) == 0
    assert "no changes" in capsys.readouterr().out
    assert cmd_discover(["--bad"]) == 2
    assert "usage:" in capsys.readouterr().err
    assert cmd_discover(["--help"]) == 0
    assert "usage:" in capsys.readouterr().out


@pytest.mark.parametrize("raw, expected", [
    ("bad", 3600), ("nan", 3600), ("inf", 3600), ("1", 300), ("450.5", 450.5),
])
def test_interval_validation(raw, expected):
    assert scan._interval({"LLM_ROUTER_RESCAN_INTERVAL_S": raw}) == expected


# ── late binding of ollama/@local (D-205) ────────────────────────────────────

def _hermetic_local(tmp_path, monkeypatch):
    """Scratch LLM_ROUTER_HOME with an EMPTY cache; returns write_cache(name|None)."""
    import json
    import time

    from llm_router import policy

    for var in ("LLM_ROUTER_OLLAMA_MODEL", "OLLAMA_MODELS", "OLLAMA_BUDGET_MODELS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setattr(policy, "_EPOCH_CHECK_S", 0.0)
    monkeypatch.setattr(policy, "_epoch_primed", False)

    def write_cache(name):
        models = {} if name is None else {
            f"ollama/{name}": {"model_id": f"ollama/{name}",
                               "provider": "ollama", "provider_tier": "local"}}
        (tmp_path / "discovery.json").write_text(
            json.dumps({"cached_at": time.time(), "models": models}))

    return write_cache


def _fresh_table():
    from llm_router import profiles
    return profiles._LateBoundTable(profiles._load_routing_table_from_policy())


def _holders(table, model):
    return [c for c in table.values() if f"ollama/{model}" in c]


def test_late_bind_empty_at_import_then_model_appears(tmp_path, monkeypatch):
    # Fresh install: table built with no local model; the rescan loop writes the cache later.
    write_cache = _hermetic_local(tmp_path, monkeypatch)
    write_cache(None)
    table = _fresh_table()
    assert not _holders(table, "fake-a:1b")
    write_cache("fake-a:1b")
    assert _holders(table, "fake-a:1b"), "table built empty never picked up the new model"


def test_late_bind_model_a_to_b(tmp_path, monkeypatch):
    write_cache = _hermetic_local(tmp_path, monkeypatch)
    write_cache("fake-a:1b")
    table = _fresh_table()
    assert _holders(table, "fake-a:1b"), "standard policy must carry an @local slot"
    write_cache("fake-b:2b")
    assert _holders(table, "fake-b:2b")
    assert not _holders(table, "fake-a:1b")


def test_late_bind_swap_is_one_reference(tmp_path, monkeypatch):
    write_cache = _hermetic_local(tmp_path, monkeypatch)
    write_cache("fake-a:1b")
    table = _fresh_table()
    old = table._data
    snapshot = dict(old)
    write_cache("fake-b:2b")
    table.get(next(iter(old)))
    assert table._data is not old and old == snapshot, "old mapping must stay intact"
    assert table.copy() == table._data and isinstance(table.copy(), dict)


def test_late_bind_mutant_epoch_never_refreshes_is_caught(tmp_path, monkeypatch):
    # Mutant: the probe never sees a change (local_placeholder_seen frozen) -> the table must
    # stay stale, proving the tests above would fail if late binding were disabled.
    from llm_router import policy
    write_cache = _hermetic_local(tmp_path, monkeypatch)
    write_cache(None)
    table = _fresh_table()
    monkeypatch.setattr(policy, "local_placeholder_seen", lambda: None)
    write_cache("fake-a:1b")
    assert not _holders(table, "fake-a:1b")


def test_no_change_means_no_table_rebuild(tmp_path, monkeypatch):
    from llm_router import policy
    from llm_router import profiles

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setattr(policy, "_EPOCH_CHECK_S", 0.0)
    profiles.ROUTING_TABLE.get((profiles.RoutingProfile.BALANCED, profiles.TaskType.QUERY))
    calls = []
    real = profiles._load_routing_table_from_policy
    monkeypatch.setattr(profiles, "_load_routing_table_from_policy",
                        lambda: calls.append(1) or real())
    for _ in range(5):
        profiles.ROUTING_TABLE.get((profiles.RoutingProfile.BALANCED, profiles.TaskType.QUERY))
    assert calls == []
