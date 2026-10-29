"""SEC-S6 (UN-06): policy never silently lowers a privacy guarantee.

Interpretation: the class is a property of a resource-map ROW (``local`` or
``cloud``, from ``local_privacy.stays_local``), and a change of that class for a
row present in both maps is announced. Routing-policy diffs are out of scope.
"""
from __future__ import annotations

import json

import pytest

from llm_router import map_rediscovery as mr, resource_map as rm
from llm_router.discovery.keys import discover
from llm_router.seats import Seat, Seats

T0 = 1_790_000_000.0


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("OLLAMA_API_BASE", raising=False)
    monkeypatch.delenv("OLLAMA_URL", raising=False)


def _map(tmp_path, names, base="http://localhost:11434"):
    tags = {"models": [{"name": n, "digest": "d", "modified_at": "m"} for n in names]}
    inp = rm.MapInputs(
        seats=Seats(claude=Seat(), codex=Seat(), gemini=Seat(), ollama=Seat(kind="local")),
        env={}, ollama_url=base, http_get=lambda url: tags if url.endswith("/api/tags") else None,
        http_post=lambda url, body: {"capabilities": ["completion"]},
        claude_reading=lambda: None, codex_counter=lambda: None, codex_sessions=tmp_path / "none",
        gemini_quota=lambda: None, policy="balanced", now=T0,
        keys=lambda: discover(env={}, dotenv_paths=[], keychain=lambda v: False))
    return rm.build(inp)


def _privacy(events):
    return [e for e in events if e["event"] == "privacy"]


def test_rows_carry_the_class_from_stays_local(tmp_path):
    m = _map(tmp_path, ["qwen3:8b", "gpt-oss:120b-cloud", "kimi:cloud"])
    got = {r["resource"]: r["privacy"] for r in m["resources"] if r["resource"].startswith("ollama/")}
    assert got == {"ollama/qwen3:8b": "local", "ollama/gpt-oss:120b-cloud": "cloud", "ollama/kimi:cloud": "cloud"}


def test_a_remote_daemon_makes_every_row_cloud(tmp_path):
    m = _map(tmp_path, ["qwen3:8b"], base="http://203.0.113.9:11434")
    assert [r["privacy"] for r in m["resources"] if r["resource"] == "ollama/qwen3:8b"] == ["cloud"]


def test_remote_base_is_one_event_and_a_notice(tmp_path):
    old = _map(tmp_path, ["qwen3:8b"])
    new = _map(tmp_path, ["qwen3:8b"], base="http://203.0.113.9:11434")
    ev = _privacy(mr.diff_maps(old, new))
    assert len(ev) == 1 and (ev[0]["before"], ev[0]["after"]) == ("local", "cloud")
    assert ev[0]["notice"] == "! qwen3:8b privacy local->cloud"
    assert mr.notice_text(ev) == "map: ! qwen3:8b privacy local->cloud"


def test_a_cloud_tag_replacing_the_row_class_is_one_event(tmp_path):
    old = _map(tmp_path, ["x"])
    new = json.loads(json.dumps(old))
    for r in new["resources"]:
        if r["resource"] == "ollama/x":
            r["privacy"] = "cloud"
    assert len(_privacy(mr.diff_maps(old, new))) == 1


def test_unchanged_class_is_silent(tmp_path):
    a, b = _map(tmp_path, ["qwen3:8b", "k:cloud"]), _map(tmp_path, ["qwen3:8b", "k:cloud"])
    assert _privacy(mr.diff_maps(a, b)) == []


def test_a_map_from_before_the_field_announces_nothing(tmp_path):
    old = _map(tmp_path, ["qwen3:8b"])
    for r in old["resources"]:
        r.pop("privacy")
    new = _map(tmp_path, ["qwen3:8b"], base="http://203.0.113.9:11434")
    assert _privacy(mr.diff_maps(old, new)) == []


def test_the_notice_reaches_pending_notice(tmp_path):
    old = _map(tmp_path, ["qwen3:8b"])
    new = _map(tmp_path, ["qwen3:8b"], base="http://203.0.113.9:11434")
    mr.record_changes(old, new, source="test", now=T0)
    assert "privacy local->cloud" in mr.pending_notice(now=T0 + 1)
