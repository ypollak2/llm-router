"""T21f (07-ACCEPTANCE section 6, reduced from T21): 20 timing drills for a same-name re-pull.

A model is re-pulled under the same name with a new digest at a seeded random phase of the
30 s poll. Bar (T21f, M1(a)): detected within 60 s `[T]` at p95 and every drill <= 300 s
(P1.8-d), where "detected" is all of: a `digest` event, the map row's digest updated, exactly
one `map_changes.jsonl` line, and a notice naming the model. Then an identical-digest re-pull
writes nothing (no new line, no notice) through a full TTL rebuild.
n = 20 drills (T21 "20 timing drills for the latency p95"); fewer is NOT INFORMATIVE and fails.
Negative control (T21): a names-only `tags_signature` (the pre-P1.8-e behaviour) must miss the
60 s bound. Harness (07 section 0): fake clock, no sleeps, no network (every connect is recorded
and refused), HOME and LLM_ROUTER_HOME are temp dirs asserted not to be the real ones; same
`World` as tests/test_map_rediscovery.py. Not asserted (16.3): profile stale, grade -> `probe`.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import socket
from pathlib import Path

import pytest

from llm_router import map_rediscovery as mr, resource_map as rm
from tests._real_home_guard import TRUE_HOME
from tests.test_map_rediscovery import POLL, T0, World

N_DRILLS = 20
SEED = 20261028
BAR_P95_S = 60.0
BAR_MAX_S = 300.0
MODEL = "qwen3.5:latest"


def _p95(xs):
    s = sorted(xs)
    return s[max(0, math.ceil(0.95 * len(s)) - 1)]  # nearest rank


def _assert_not_real_home(p):
    p = Path(p).resolve()
    real = TRUE_HOME.resolve()
    assert p != real and real / ".llm-router" not in (p, *p.parents) \
        and real / ".claude" not in (p, *p.parents), p


@pytest.fixture
def net_guard(monkeypatch):
    attempts = []

    def refuse(self, address):
        attempts.append(address)
        raise OSError(f"T21f: no network in timing drills ({address!r})")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    yield attempts
    assert attempts == [], f"outbound connections attempted: {attempts}"


def _drill(tmp_path, monkeypatch, i, rng):
    """Seconds from the re-pull to full detection, or None if not detected within 300 s + a poll."""
    home = tmp_path / f"d{i}"
    home.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home / "state"))
    _assert_not_real_home(os.environ["HOME"])
    _assert_not_real_home(os.environ["LLM_ROUTER_HOME"])
    w = World(home)
    w.poll()                                   # baseline map at T0, logs nothing
    assert w.changes() == []
    phase = rng.uniform(0.0, POLL)
    t_change = T0 + phase                      # the re-pull lands mid-interval
    w.models[MODEL] = (f"sha-new-{i}", "2026-10-28T00:00:00Z")
    delay = None
    while w.clock - t_change <= BAR_MAX_S + POLL:
        events = w.tick()
        if any(e["event"] == "digest" for e in events):
            row = next(r for r in rm.load_map(w.map_path)["resources"]
                       if r["resource"] == f"ollama/{MODEL}")
            assert row["digest"] == f"sha-new-{i}"
            lines = w.changes()
            assert len(lines) == 1 and lines[0]["event"] == "digest", lines
            assert w.notice() == f"map: ~ {MODEL} new digest"
            delay = w.clock - t_change
            break
    if delay is None:
        return None
    # Identical digest (a re-pull that changes only modified_at): writes nothing, even across
    # the TTL rebuild.
    w.models[MODEL] = (f"sha-new-{i}", "2026-10-28T01:00:00Z")
    stop = w.clock + rm.REFRESH_TTL_S + POLL
    while w.clock <= stop:
        assert [e for e in w.tick() if e["event"] == "digest"] == []
    assert len(w.changes()) == 1 and w.notice() == ""
    return delay


def _run(tmp_path, monkeypatch):
    rng = random.Random(SEED)
    return [_drill(tmp_path, monkeypatch, i, rng) for i in range(N_DRILLS)]


def test_t21f_20_timing_drills_detect_within_60s_p95(tmp_path, monkeypatch, net_guard):
    delays = _run(tmp_path, monkeypatch)
    assert len(delays) == N_DRILLS, "NOT INFORMATIVE: fewer than 20 drills"
    assert all(d is not None for d in delays), delays
    p95, worst = _p95(delays), max(delays)
    print(f"T21f n={len(delays)} p95={p95:.1f}s max={worst:.1f}s "
          f"bar_p95={BAR_P95_S}s bar_max={BAR_MAX_S}s")
    assert p95 <= BAR_P95_S, f"p95 {p95:.1f}s > {BAR_P95_S}s"
    assert worst <= BAR_MAX_S, f"max {worst:.1f}s > {BAR_MAX_S}s"


def _names_only(tags):
    if not isinstance(tags, dict) or "models" not in tags:
        return "unreachable"
    names = sorted({m.get("name") for m in tags.get("models") or [] if isinstance(m, dict)} - {None})
    return hashlib.sha256(json.dumps(names).encode()).hexdigest()


def test_t21f_negative_control_names_only_signature_misses_the_bar(tmp_path, monkeypatch, net_guard):
    monkeypatch.setattr(mr, "tags_signature", _names_only)
    delays = _run(tmp_path, monkeypatch)
    assert len(delays) == N_DRILLS, "NOT INFORMATIVE: fewer than 20 drills"
    found = [d for d in delays if d is not None]
    p95 = f"{_p95(found):.1f}s" if found else "none"
    print(f"T21f mutant n={len(delays)} detected={len(found)} p95={p95} bar_p95={BAR_P95_S}s")
    # The mutant must turn the bar red: only the 300 s TTL rebuild can find the change.
    assert not found or _p95(found) > BAR_P95_S
