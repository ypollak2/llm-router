"""P1.8 map-core: the resource map builds from fixtures only (no Ollama, no network, no CLI)."""
from __future__ import annotations

import json

import pytest

from llm_router import local_models, resource_map as rm
from llm_router.seats import Seat, Seats

NOW = 1_790_000_000.0  # fixed clock


def _tags(*names):
    return {"models": [{"name": n, "size": 1, "details": {"parameter_size": "9B"}} for n in names]}


SHOW = {
    "qwen3.5:latest": {"capabilities": ["completion", "tools", "thinking"],
                       "model_info": {"qwen35.context_length": 262144}},
    "nimble:9b": {"capabilities": ["completion"], "model_info": {}},   # Ollama says completion; it still 400s (N19)
    "nomic-embed-text:latest": {"capabilities": ["embedding"]},
}


def make_inputs(tmp_path, *, seats=None, models=("qwen3.5:latest", "nimble:9b"), env=None,
                claude=(0.42, "ok", NOW - 10), counter=None, gemini=None, policy="balanced"):
    seats = seats or Seats(
        claude=Seat(kind="claude.ai", plan="max"), codex=Seat(kind="chatgpt", plan="plus"),
        gemini=Seat(kind="google"), ollama=Seat(kind="local"),
    )
    return rm.MapInputs(
        seats=seats, env=env or {},
        http_get=lambda url: _tags(*models) if url.endswith("/api/tags") else None,
        http_post=lambda url, body: SHOW.get(body["model"], {}),
        claude_reading=lambda: claude,
        codex_counter=lambda: counter, codex_sessions=tmp_path / "no-codex",
        gemini_quota=lambda: gemini, policy=policy, now=NOW,
    )


def row(data, resource):
    return next(r for r in data["resources"] if r["resource"] == resource)


def walk_quotas(data):
    for r in data["resources"]:
        for name, q in r["quota"].items():
            yield r["resource"], name, q


def test_every_quota_value_carries_provenance(tmp_path):
    data = rm.build(make_inputs(tmp_path, env={"OPENAI_API_KEY": "x"}))
    quotas = list(walk_quotas(data))
    assert len(quotas) >= 5, "check found almost nothing to check"
    for res, name, q in quotas:
        assert set(q) == {"value", "provenance", "as_of"}, (res, name)
        assert q["provenance"] in rm.PROVENANCE, (res, name)


def test_quota_value_rejects_unlabelled():
    with pytest.raises(ValueError):
        rm.quota_value(0.5, "guess")


def test_claude_measured_and_unknown(tmp_path):
    ok = row(rm.build(make_inputs(tmp_path)), "claude")
    assert ok["quota"]["pressure"]["provenance"] == "measured" and ok["quota"]["pressure"]["value"] == 0.42
    unk = row(rm.build(make_inputs(tmp_path, claude=(None, "stale", None))), "claude")
    assert unk["quota"]["pressure"] == {"value": None, "provenance": "default", "as_of": None}
    assert "stale" in unk["reason"]


def test_codex_counter_default_limit_and_rollout(tmp_path):
    from datetime import datetime, timezone
    day = datetime.fromtimestamp(NOW, tz=timezone.utc).date().isoformat()
    c = row(rm.build(make_inputs(tmp_path, counter={"date": day, "count": 250, "cached_at": NOW})), "codex")
    assert c["quota"]["daily_limit"] == {"value": 1000, "provenance": "default", "as_of": None}
    assert c["quota"]["pressure"]["value"] == 0.25 and c["quota"]["pressure"]["provenance"] == "estimated"
    assert c["reset_at"] is not None
    # a rollout file with rate_limits wins and is measured
    d = tmp_path / "sessions" / "2026" / "10"
    d.mkdir(parents=True)
    (d / "rollout-a.jsonl").write_text(
        json.dumps({"type": "x"}) + "\n" +
        json.dumps({"payload": {"rate_limits": {"primary": {"used_percent": 61.0, "window_minutes": 300, "resets_at": NOW + 99},
                                                 "secondary": {"used_percent": 12.0, "window_minutes": 10080}}}}) + "\n")
    inp = make_inputs(tmp_path)
    inp.codex_sessions = tmp_path / "sessions"
    m = row(rm.build(inp), "codex")
    assert m["quota"]["pressure"]["value"] == pytest.approx(0.61) and m["quota"]["pressure"]["provenance"] == "measured"
    assert m["quota"]["weekly_pressure"]["value"] == pytest.approx(0.12)


def test_absent_seats_have_reason(tmp_path):
    data = rm.build(make_inputs(tmp_path, seats=Seats()))
    for name in ("claude", "codex", "gemini_cli"):
        r = row(data, name)
        assert r["status"] == "absent" and r["reason"] and r["effective_priority"] is None


def test_ollama_rows_facts_and_n19(tmp_path):
    data = rm.build(make_inputs(tmp_path, models=("qwen3.5:latest", "nimble:9b", "nomic-embed-text:latest")))
    q = row(data, "ollama/qwen3.5:latest")
    assert "generate" in q["capabilities"] and "tools" in q["capabilities"]
    assert q["limits"]["num_ctx"] == local_models.num_ctx("qwen3.5:latest") == 131072
    assert q["limits"]["max_context"] == 262144
    n = row(data, "ollama/nimble:9b")
    assert "generate" not in n["capabilities"] and "decision" in n["capabilities"]
    assert n["effective_priority"] is None          # a reader skips it: that is the N19 fix
    e = row(data, "ollama/nomic-embed-text:latest")
    assert "generate" not in e["capabilities"] and "embedding" in e["capabilities"]
    assert q["effective_priority"] is not None


def test_ollama_unreachable_and_empty(tmp_path):
    inp = make_inputs(tmp_path)
    inp.http_get = lambda url: None
    r = row(rm.build(inp), "ollama")
    assert r["status"] == "absent" and "not reachable" in r["reason"]
    inp.http_get = lambda url: {"models": []}
    assert "no models" in row(rm.build(inp), "ollama")["reason"]


def test_api_key_rows_hold_no_value(tmp_path):
    secret = "sk-SECRET-VALUE-123"
    data = rm.build(make_inputs(tmp_path, env={"OPENAI_API_KEY": secret}))
    assert secret not in json.dumps(data)
    k = row(data, "api/OPENAI_API_KEY")
    assert k["status"] == "connected" and k["chain_id"] == "openai/api"


def test_policy_changes_priority(tmp_path):
    bal = rm.build(make_inputs(tmp_path, env={"OPENAI_API_KEY": "x"}, policy="balanced"))
    loc = rm.build(make_inputs(tmp_path, env={"OPENAI_API_KEY": "x"}, policy="local-first"))
    assert row(bal, "claude")["effective_priority"] < row(bal, "ollama/qwen3.5:latest")["effective_priority"]
    assert row(loc, "ollama/qwen3.5:latest")["effective_priority"] < row(loc, "claude")["effective_priority"]


def test_map_mutation_changes_view(tmp_path):
    a = rm.build(make_inputs(tmp_path))
    b = rm.build(make_inputs(tmp_path, models=("qwen3.5:latest",)))
    assert {r["resource"] for r in a["resources"]} - {r["resource"] for r in b["resources"]} == {"ollama/nimble:9b"}


def test_atomic_write_and_refresh_ttl(tmp_path):
    p = tmp_path / "state" / "resource_map.json"
    first = rm.refresh_if_stale(make_inputs(tmp_path), path=p, now=NOW)
    assert p.exists() and not list(p.parent.glob(".resource_map.*"))
    assert rm.load_map(p) == first
    # inside the TTL the file is returned, not rebuilt
    again = rm.refresh_if_stale(make_inputs(tmp_path, models=("qwen3.5:latest",)), path=p, now=NOW + 299)
    assert again == first
    # past the TTL it rebuilds
    third = rm.refresh_if_stale(make_inputs(tmp_path, models=("qwen3.5:latest",)), path=p, now=NOW + 301)
    assert all(r["resource"] != "ollama/nimble:9b" for r in third["resources"])


def test_write_failure_leaves_old_file_and_no_tmp(tmp_path):
    p = tmp_path / "resource_map.json"
    rm.write_map({"schema": 1, "generated_at": rm._iso(NOW), "x": 1}, p)
    with pytest.raises(TypeError):
        rm.write_map({"schema": 1, "bad": object()}, p)
    assert json.loads(p.read_text())["x"] == 1
    assert not list(tmp_path.glob(".resource_map.*"))


def test_load_map_rejects_other_schema_and_garbage(tmp_path):
    p = tmp_path / "m.json"
    p.write_text("{not json")
    assert rm.load_map(p) is None
    p.write_text(json.dumps({"schema": 99}))
    assert rm.load_map(p) is None


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def test_cli_equals_mcp_view_minus_timestamps(home, tmp_path, monkeypatch, capsys):
    import asyncio

    from llm_router.commands.map import cmd_map
    from llm_router.tools.consolidated import llm_router_status

    inp = make_inputs(tmp_path)
    rm.write_map(rm.build(inp), rm.map_path())
    monkeypatch.setattr(rm, "refresh_if_stale", lambda *a, **k: rm.load_map())
    assert cmd_map(["--json"]) == 0
    cli_out = capsys.readouterr().out.strip()
    mcp_out = asyncio.run(llm_router_status(view="map"))
    assert cli_out == mcp_out
    assert json.dumps(rm.strip_timestamps(json.loads(cli_out)), sort_keys=True) == \
        json.dumps(rm.strip_timestamps(json.loads(mcp_out)), sort_keys=True)
    assert json.loads(cli_out)["resources"], "empty map would pass trivially"


def test_cli_table_and_bad_arg(home, tmp_path, monkeypatch, capsys):
    from llm_router.commands.map import cmd_map

    rm.write_map(rm.build(make_inputs(tmp_path)), rm.map_path())
    monkeypatch.setattr(rm, "refresh_if_stale", lambda *a, **k: rm.load_map())
    assert cmd_map([]) == 0
    out = capsys.readouterr().out
    assert "ollama/nimble:9b" in out and "policy=balanced" in out
    assert cmd_map(["--bogus"]) == 2


def test_roles_table():
    assert local_models.roles("ollama/nimble:9b") == frozenset({"decision"})
    assert local_models.roles("qwen3.5:latest") == frozenset({"generate"})


# ── review fixes (#402) ─────────────────────────────────────────────────────

def test_gemini_no_data_is_default_not_estimated(tmp_path):
    g = row(rm.build(make_inputs(tmp_path, gemini=None)), "gemini_cli")
    assert g["quota"]["daily_limit"]["provenance"] == "default"
    assert g["quota"]["pressure"]["provenance"] == "default"


def test_gemini_source_decides_label(tmp_path, monkeypatch):
    from llm_router import gemini_cli_quota as gq

    monkeypatch.setattr(gq, "_load_quota_cache", lambda: {"count": 150, "daily_limit": 1500, "tier": "t"})
    stats = rm._default_gemini_quota()
    assert stats == {"provenance": "measured", "daily_limit": 1500, "pressure": 0.1}
    monkeypatch.setattr(gq, "_load_quota_cache", lambda: None)
    monkeypatch.setattr(gq, "_get_local_quota", lambda: {"date": "d", "count": 15, "daily_limit": 1500})
    assert rm._default_gemini_quota()["provenance"] == "estimated"
    monkeypatch.setattr(gq, "_get_local_quota", lambda: None)
    assert rm._default_gemini_quota() is None
    g = row(rm.build(make_inputs(tmp_path, gemini={"provenance": "measured", "daily_limit": 1500, "pressure": 0.1})),
            "gemini_cli")
    assert g["quota"]["pressure"]["provenance"] == "measured"


def _rollout(tmp_path, *, resets_at=None, as_of_age_s=0, window=300):
    import os
    d = tmp_path / "sess"
    d.mkdir(exist_ok=True)
    f = d / "rollout-s.jsonl"
    prim = {"used_percent": 97.0, "window_minutes": window}
    if resets_at is not None:
        prim["resets_at"] = resets_at
    f.write_text(json.dumps({"payload": {"rate_limits": {"primary": prim}}}) + "\n")
    os.utime(f, (NOW - as_of_age_s, NOW - as_of_age_s))
    inp = make_inputs(tmp_path)
    inp.codex_sessions = d
    return inp


def test_expired_codex_rollout_is_stale_not_current(tmp_path):
    c = row(rm.build(_rollout(tmp_path, resets_at=NOW - 3 * 86400, as_of_age_s=3 * 86400 + 60)), "codex")
    assert c["quota"]["pressure"]["provenance"] != "measured"
    assert c["quota"]["pressure"]["value"] != 0.97
    assert c["quota"]["last_rollout_pressure"] == {"value": 0.97, "provenance": "stale", "as_of": rm._iso(NOW - 3 * 86400 - 60)}


def test_codex_rollout_without_resets_at_expires_by_window(tmp_path):
    old = row(rm.build(_rollout(tmp_path, as_of_age_s=6 * 3600)), "codex")     # 5 h window ended 1 h ago
    assert "last_rollout_pressure" in old["quota"]
    fresh = row(rm.build(_rollout(tmp_path, as_of_age_s=3600)), "codex")
    assert fresh["quota"]["pressure"]["provenance"] == "measured" and fresh["quota"]["pressure"]["value"] == 0.97


def test_ollama_duplicate_names_listed_once_priorities_contiguous(tmp_path):
    data = rm.build(make_inputs(tmp_path, models=("qwen3.5:latest", "qwen3.5:latest", "other:1")))
    names = [r["resource"] for r in data["resources"] if r["resource"].startswith("ollama/")]
    assert names == ["ollama/qwen3.5:latest", "ollama/other:1"]
    prios = sorted(r["effective_priority"] for r in data["resources"] if r["effective_priority"])
    assert prios == list(range(1, len(prios) + 1))


def test_mcp_view_map_runs_off_the_event_loop(home, monkeypatch):
    import asyncio
    import threading

    from llm_router.tools.consolidated import llm_router_status

    seen = {}

    def fake():
        seen["thread"] = threading.current_thread()
        return "{}"
    monkeypatch.setattr(rm, "view_json", fake)

    async def go():
        seen["loop"] = threading.current_thread()
        return await llm_router_status(view="map")
    assert asyncio.run(go()) == "{}"
    assert seen["thread"] is not seen["loop"]


def test_competence_loaded_from_file_when_present(tmp_path):
    inp = make_inputs(tmp_path)
    f = tmp_path / "elig.json"
    f.write_text(json.dumps({"qwen3.5:latest": {"code": {"eligible": True}}}))
    inp.competence_path = f
    data = rm.build(inp)
    assert row(data, "ollama/qwen3.5:latest")["competence"] == {"code": {"eligible": True}}
    assert row(data, "ollama/nimble:9b")["competence"] is None
