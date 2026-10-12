"""P1.8 tasks 8 and 10: re-discovery drills on a fake clock, no network, no real Ollama.

MUST P1.8-d: a change notice within 300 s of each of 4 drills (new key, new model,
Codex login, quota exhausted). MUST P1.8-e: 5 digest drills.
"""
from __future__ import annotations

import dataclasses
import json
import os
import threading
from datetime import datetime, timezone

import pytest

from llm_router import map_rediscovery as mr, resource_map as rm
from llm_router.discovery.keys import discover
from llm_router.resolver import inventory as inv
from llm_router.seats import Seat, Seats

T0 = 1_790_000_000.0
POLL = mr.POLL_INTERVAL_S


class World:
    """The machine the fake probes read; tests mutate it between polls."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.models = {"qwen3.5:latest": ("sha-a", "2026-10-01T00:00:00Z")}
        self.env: dict[str, str] = {}
        self.counter = None
        self.seats = Seats(claude=Seat(kind="claude.ai", plan="max"), codex=Seat(),
                           gemini=Seat(), ollama=Seat(kind="local"))
        self.files = [tmp_path / ".env", tmp_path / "auth.json", tmp_path / "usage.json"]
        self.map_path = tmp_path / "state" / "resource_map.json"
        self.state = tmp_path / "state"
        self.clock = T0
        self.http_calls = 0

    def tags(self, url):
        self.http_calls += 1
        if not url.endswith("/api/tags"):
            return None
        return {"models": [{"name": n, "digest": d, "modified_at": m, "size": 1}
                           for n, (d, m) in self.models.items()]}

    def inputs(self):
        return rm.MapInputs(
            seats=self.seats, env=self.env, http_get=self.tags,
            http_post=lambda url, body: {"capabilities": ["completion"]},
            claude_reading=lambda: (0.1, "ok", self.clock),
            codex_counter=lambda: self.counter, codex_sessions=self.tmp / "no-codex",
            gemini_quota=lambda: None, policy="balanced",
            keys=lambda: discover(env=self.env, dotenv_paths=[], keychain=lambda v: False),
        )

    def login_codex(self):
        self.seats = dataclasses.replace(self.seats, codex=Seat(kind="chatgpt", plan="plus"))

    def touch(self, i):
        p = self.files[i]
        p.write_text(str(self.clock))
        os.utime(p, (self.clock, self.clock))

    def poll(self, **kw):
        return mr.poll(inputs=self.inputs(), now=self.clock, paths=self.files,
                       map_path=self.map_path, state_dir=self.state, **kw)

    def tick(self, seconds=POLL):
        self.clock += seconds
        return self.poll()

    def notice(self, consume=True):
        return mr.pending_notice(now=self.clock, path_dir=self.state, consume=consume)

    def changes(self):
        p = self.state / mr.CHANGES_FILE
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "state"))
    w = World(tmp_path)
    w.poll()                      # baseline map: builds, logs nothing
    assert w.changes() == [] and w.map_path.exists()
    return w


def _first_notice_delay(w, mutate):
    """Seconds from the mutation until a poll (every 30 s) yields a notice."""
    t_change = w.clock
    mutate()
    for _ in range(int(mr_ttl() // POLL) + 1):
        w.tick()
        text = w.notice()
        if text:
            return w.clock - t_change, text
    return None, ""


def mr_ttl():
    return rm.REFRESH_TTL_S


# ── P1.8-d: four drills ─────────────────────────────────────────────────────

def test_drill_new_model(world):
    delay, text = _first_notice_delay(world, lambda: world.models.update({"qwen3.6:latest": ("sha-n", "m")}))
    assert text == "map: + qwen3.6:latest local"
    assert 0 < delay <= 300


def test_drill_new_key(world):
    def mutate():
        world.env["OPENAI_API_KEY"] = "x"
        world.touch(0)
    delay, text = _first_notice_delay(world, mutate)
    assert text == "map: + api/OPENAI_API_KEY api"
    assert 0 < delay <= 300


def test_drill_codex_login(world):
    def mutate():
        world.login_codex()
        world.touch(1)
    delay, text = _first_notice_delay(world, mutate)
    assert "~ codex connected" in text
    assert 0 < delay <= 300


def test_drill_quota_exhausted(world):
    world.login_codex()
    world.touch(1)
    world.tick()
    world.notice()                                  # drain the login notice

    def mutate():
        today = datetime.fromtimestamp(world.clock, tz=timezone.utc).date().isoformat()
        world.counter = {"date": today, "count": 1000, "cached_at": world.clock}
        world.touch(2)
    delay, text = _first_notice_delay(world, mutate)
    assert text == "map: ! codex quota exhausted"
    assert 0 < delay <= 300


def test_input_mtime_triggers_a_rebuild_without_waiting_for_the_ttl(world):
    world.touch(0)
    world.clock += POLL
    world.poll()
    assert rm.load_map(world.map_path)["generated_at"] != rm._iso(T0)
    assert world.changes() == []        # rebuilt, nothing differed: nothing logged


def test_unchanged_inputs_do_not_rebuild(world):
    generated = rm.load_map(world.map_path)["generated_at"]
    for _ in range(5):
        assert world.tick() == []
    assert rm.load_map(world.map_path)["generated_at"] == generated


def test_ttl_rebuild_when_nothing_else_changed(world):
    world.clock += rm.REFRESH_TTL_S + 1
    world.poll()
    assert rm.load_map(world.map_path)["generated_at"] == rm._iso(world.clock)


def test_a_second_process_holding_the_lock_skips(world):
    from llm_router.file_lock import exclusive_lock
    world.models["x:1"] = ("d", "m")
    with exclusive_lock(world.state / mr.LOCK_FILE, timeout=0) as got:
        assert got
        assert world.tick() == []     # another process is the refresher
    assert world.changes() == []


# ── P1.8-e: five digest drills ──────────────────────────────────────────────

def test_digest_drill_1_repull_new_digest_is_detected(world):
    world.models["qwen3.5:latest"] = ("sha-b", "2026-10-12T00:00:00Z")
    events = world.tick()
    assert [e["event"] for e in events] == ["digest"]
    row = next(r for r in rm.load_map(world.map_path)["resources"] if r["resource"] == "ollama/qwen3.5:latest")
    assert row["digest"] == "sha-b"
    lines = world.changes()
    assert len(lines) == 1 and lines[0]["event"] == "digest" and lines[0]["source"] == "ollama_signature"
    assert world.notice() == "map: ~ qwen3.5:latest new digest"


def test_digest_drill_2_identical_digest_writes_nothing(world):
    world.tick()
    world.poll(force=True)               # even a forced rebuild finds no difference
    assert world.changes() == [] and world.notice() == ""


def test_digest_drill_3_modified_at_alone_writes_nothing(world):
    world.models["qwen3.5:latest"] = ("sha-a", "2026-10-12T00:00:00Z")
    generated = rm.load_map(world.map_path)["generated_at"]
    world.tick()
    assert rm.load_map(world.map_path)["generated_at"] != generated   # the signature did change
    assert world.changes() == []                                       # the row did not


def test_digest_drill_4_signature_hashes_digest_and_modified_at_not_names():
    base = {"models": [{"name": "a", "digest": "1", "modified_at": "t"}]}
    same_names = {"models": [{"name": "a", "digest": "2", "modified_at": "t"}]}
    other_time = {"models": [{"name": "a", "digest": "1", "modified_at": "u"}]}
    reordered = {"models": [{"name": "b", "digest": "9", "modified_at": "t"}, *base["models"]]}
    sigs = {mr.tags_signature(x) for x in (base, same_names, other_time, reordered)}
    assert len(sigs) == 4
    assert mr.tags_signature(reordered) == mr.tags_signature({"models": list(reversed(reordered["models"]))})
    assert mr.tags_signature(base) == mr.tags_signature({"models": base["models"] * 2})
    assert mr.tags_signature(None) == "unreachable"


def test_digest_drill_5_inventory_reads_digest_and_modified_at():
    tags = {"models": [{"name": "qwen3.5:latest", "digest": "sha-z", "modified_at": "2026-10-12T00:00:00Z"},
                       {"name": "bare:1b"}]}
    p = inv.Probes(ollama_base=lambda: "http://localhost:11434",
                   http_get=lambda u, t: tags if u.endswith("/api/tags") else None,
                   http_post=lambda u, b, t: {"capabilities": ["completion"]})
    entries, status = inv._inventory_ollama(p)
    by_id = {e.id: e for e in entries}
    assert status.ok
    assert (by_id["ollama/qwen3.5:latest"].digest, by_id["ollama/qwen3.5:latest"].modified_at) == (
        "sha-z", "2026-10-12T00:00:00Z")
    assert (by_id["ollama/bare:1b"].digest, by_id["ollama/bare:1b"].modified_at) == ("", "")


def test_refresh_if_stale_logs_a_repull_with_no_poller_running(world):
    world.models["qwen3.5:latest"] = ("sha-c", "m")
    world.clock += rm.REFRESH_TTL_S + 1
    rm.refresh_if_stale(world.inputs(), path=world.map_path, now=world.clock)
    assert [x["event"] for x in world.changes()] == ["digest"]
    assert world.changes()[0]["source"] == "refresh_if_stale"


def test_map_schema_stays_one(world):
    assert rm.load_map(world.map_path)["schema"] == 1


# ── hook side: read only ────────────────────────────────────────────────────

def test_notice_is_printed_once_then_cleared(world):
    world.models["n:1"] = ("d", "m")
    world.tick()
    assert world.notice(consume=False) == "map: + n:1 local"
    assert world.notice() == "map: + n:1 local"
    assert world.notice() == ""


def test_old_events_are_not_announced(world):
    world.models["n:1"] = ("d", "m")
    world.tick()
    world.clock += mr.NOTICE_MAX_AGE_S + 1
    assert world.notice() == ""


def test_notice_caps_the_number_of_parts():
    events = [{"notice": f"+ m{i} local"} for i in range(7)]
    assert mr.notice_text(events) == "map: + m0 local; + m1 local; + m2 local; + m3 local; +3 more"
    assert mr.notice_text([{"notice": ""}]) == ""


def test_removed_model_is_announced(world):
    del world.models["qwen3.5:latest"]
    world.tick()
    assert world.notice() == "map: - qwen3.5:latest local"


def test_pending_notice_never_touches_the_network(world):
    world.models["n:1"] = ("d", "m")
    world.tick()
    calls = world.http_calls
    world.notice()
    assert world.http_calls == calls


def test_spawn_is_debounced_and_only_when_needed(world, monkeypatch):
    monkeypatch.setenv(mr.ENV_SWITCH, "1")
    spawned = []
    assert mr.needs_refresh(now=world.clock, paths=world.files, state_dir=world.state, map_path=world.map_path) is False
    monkeypatch.setattr(mr, "needs_refresh", lambda **kw: True)
    assert mr.spawn_refresh_if_needed(now=world.clock, spawner=spawned.append) is True
    assert spawned[0][-2:] == ["llm_router.map_rediscovery", "--once"] or spawned[0][-1] == "--once"
    assert mr.spawn_refresh_if_needed(now=world.clock + 10, spawner=spawned.append) is False
    assert mr.spawn_refresh_if_needed(now=world.clock + mr.SPAWN_COOLDOWN_S + 1, spawner=spawned.append) is True
    assert len(spawned) == 2


def test_spawn_respects_the_off_switch_and_swallows_errors(world, monkeypatch):
    monkeypatch.setattr(mr, "needs_refresh", lambda **kw: True)
    monkeypatch.setenv(mr.ENV_SWITCH, "0")
    assert mr.spawn_refresh_if_needed(now=world.clock, spawner=lambda a: 1 / 0) is False
    monkeypatch.setenv(mr.ENV_SWITCH, "1")
    assert mr.spawn_refresh_if_needed(now=world.clock, spawner=lambda a: 1 / 0) is False


def test_needs_refresh_sees_an_mtime_change_and_a_stale_map(world):
    kw = dict(paths=world.files, state_dir=world.state, map_path=world.map_path)
    assert mr.needs_refresh(now=world.clock, **kw) is False
    world.touch(1)
    assert mr.needs_refresh(now=world.clock, **kw) is True
    world.poll()
    assert mr.needs_refresh(now=world.clock, **kw) is False
    assert mr.needs_refresh(now=world.clock + rm.REFRESH_TTL_S + 1, **kw) is True


# ── the 30 s poller ─────────────────────────────────────────────────────────

def test_poller_calls_poll_every_interval_and_survives_errors():
    calls = []
    waits = []

    def poll_fn(**kw):
        calls.append(kw)
        if len(calls) == 1:
            raise RuntimeError("boom")

    third, release = threading.Event(), threading.Event()

    def wait(interval):
        waits.append(interval)
        if len(waits) == 3:
            third.set()
            release.wait(5)

    p = mr.start_poller(30.0, poll_fn=poll_fn, wait=wait, force=False)
    assert third.wait(5)
    p.stop()
    release.set()
    p.join(5)
    assert len(calls) == 3 and waits == [30.0, 30.0, 30.0]


def test_disabled_switch_starts_no_poller(monkeypatch):
    monkeypatch.setenv(mr.ENV_SWITCH, "0")
    assert mr.maybe_start_poller() is None


def test_once_entry_point_never_raises(monkeypatch):
    monkeypatch.setattr(mr, "poll", lambda **kw: 1 / 0)
    assert mr.main(["--once"]) == 0


# ── review fixes ────────────────────────────────────────────────────────────

def test_transient_ollama_outage_announces_nothing(world):
    saved = world.tags
    world.tags = lambda url: None
    world.tick()
    world.tags = saved
    world.tick()
    assert world.changes() == [] and world.notice() == ""


def test_map_without_digest_key_does_not_storm_on_upgrade(world):
    data = rm.load_map(world.map_path)
    for r in data["resources"]:
        r.pop("digest", None)
    rm.write_map(data, world.map_path)
    (world.state / mr.WATCH_FILE).unlink()
    world.tick()
    assert world.changes() == []


def test_partial_trailing_line_is_not_skipped(world):
    p = world.state / mr.CHANGES_FILE
    ev = {"ts": rm._iso(world.clock), "notice": "+ x local"}
    line = json.dumps(ev)
    p.write_text(line[:10])
    assert world.notice() == ""
    p.write_text(line + "\n")
    assert world.notice() == "map: + x local"
