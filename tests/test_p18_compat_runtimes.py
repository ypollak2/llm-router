"""P1.8 task 11: local OpenAI-compatible runtimes in the inventory and the resource map."""
from __future__ import annotations

import json

import pytest

from llm_router import local_platforms, resource_map as rm
from llm_router.config import RouterConfig as Config
from llm_router.model_discovery import order_for_chain
from llm_router.repo_config import _dict_to_config
from llm_router.resolver import inventory as inv
from llm_router.seats import Seat, Seats

NOW = 1_790_000_000.0


def models_body(*ids, **extra):
    return {"object": "list", "data": [{"id": i, "object": "model", **extra} for i in ids]}


def probes(urls, user=()):
    return inv.Probes(environ={}, http_get=lambda u, t: urls.get(u), user_runtimes=lambda: list(user))


def only(*keys):
    """Restrict the platform table to the given provider keys (keeps their order)."""
    return [p for p in local_platforms.PLATFORMS if p.provider_key in keys]


@pytest.fixture
def state(tmp_path, monkeypatch):
    home = tmp_path / "state"
    home.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    for v in ("LLM_ROUTER_PROMOTE_GATE", "OPENAI_COMPAT_MODELS", "OPENAI_COMPAT_BASE_URL"):
        monkeypatch.delenv(v, raising=False)
    return home


def test_a_no_cap_across_three_runtimes(monkeypatch):
    monkeypatch.setattr(local_platforms, "PLATFORMS", only("jan", "vllm", "lmstudio"))
    urls = {
        "http://localhost:1337/v1/models": models_body("j1", "j2"),
        "http://localhost:8000/v1/models": models_body(*[f"v{i}" for i in range(7)]),
        "http://localhost:1234/v1/models": models_body(),
    }
    entries, status = inv._inventory_openai_compat(probes(urls))
    assert len(entries) == 9
    assert {e.privacy for e in entries} == {"local"}
    assert status.ok and "LM Studio: 0 models" in status.detail
    assert "9 model(s) across 3 runtime(s)" in status.detail


def test_a_nothing_reachable(monkeypatch):
    entries, status = inv._inventory_openai_compat(probes({}))
    assert entries == [] and not status.ok
    assert status.detail == "no OpenAI-compatible runtime reachable"


SHAPES = {
    "llamacpp": {"models": [{"name": "x"}], "data": [{"id": "x", "meta": {"n_ctx_train": 4096}}]},
    "llamafile": models_body("x", owned_by="llamafile"),
    "localai": models_body("x", owned_by="localai"),
    "mlx": models_body("mlx-community/x"),
    "openai_compat": models_body("plain", owned_by="someone"),
}


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("want", list(SHAPES))
def test_b_port_8080_is_decided_by_shape(monkeypatch, want, reverse):
    plats = only("llamacpp", "llamafile", "localai", "mlx")
    monkeypatch.setattr(local_platforms, "PLATFORMS", plats[::-1] if reverse else plats)
    runtimes = inv._openai_compat_runtimes(probes({"http://localhost:8080/v1/models": SHAPES[want]}))
    assert [r[0] for r in runtimes] == [want]


def test_b_empty_data_on_shared_port_is_generic(monkeypatch):
    monkeypatch.setattr(local_platforms, "PLATFORMS", only("llamacpp", "llamafile"))
    runtimes = inv._openai_compat_runtimes(probes({"http://localhost:8080/v1/models": models_body()}))
    assert [(r[0], r[3]) for r in runtimes] == [("openai_compat", [])]


def test_c_user_runtimes_privacy(monkeypatch):
    monkeypatch.setattr(local_platforms, "PLATFORMS", only("jan"))
    seen = []
    urls = {
        "http://192.168.1.50:9000/v1/models": models_body("far"),
        "http://127.0.0.1:9100/v1/models": models_body("near"),
    }

    def get(u, t):
        seen.append(u)
        return urls.get(u)

    p = inv.Probes(environ={}, http_get=get, user_runtimes=lambda: [
        {"name": "Box", "url": "http://192.168.1.50:9000"},
        {"name": "Tunnel", "url": "http://127.0.0.1:9100/v1"},
        {"name": "bad", "url": "ftp://x"}, {"name": "none"}])
    entries, status = inv._inventory_openai_compat(p)
    by_id = {e.id: e for e in entries}
    assert by_id["openai_compat/far"].privacy == "cloud"
    assert by_id["openai_compat/far"].route_kind == "local"
    assert by_id["openai_compat/near"].privacy == "local"
    assert any("non-local" in n for n in by_id["openai_compat/far"].notes)
    builtin = [u for u in seen if u.endswith(":1337/v1/models")]
    assert builtin == ["http://localhost:1337/v1/models"]
    assert all(u.startswith("http://localhost:") or "192.168.1.50" in u or "127.0.0.1" in u for u in seen)


def test_c_duplicate_of_builtin_base_not_probed_twice(monkeypatch):
    monkeypatch.setattr(local_platforms, "PLATFORMS", only("jan"))
    seen = []
    p = inv.Probes(environ={}, http_get=lambda u, t: seen.append(u),
                   user_runtimes=lambda: [{"name": "J", "url": "http://localhost:1337"}])
    inv._inventory_openai_compat(p)
    assert seen == ["http://localhost:1337/v1/models"]


def test_c_same_model_two_runtimes_gets_suffix(monkeypatch):
    monkeypatch.setattr(local_platforms, "PLATFORMS", only("jan", "vllm"))
    urls = {"http://localhost:1337/v1/models": models_body("m"),
            "http://localhost:8000/v1/models": models_body("m")}
    ids = [e.id for e in inv._inventory_openai_compat(probes(urls))[0]]
    assert ids == ["openai_compat/m", "openai_compat/m@vllm"]


def test_d_port_env_moves_the_probe(monkeypatch):
    monkeypatch.setattr(local_platforms, "PLATFORMS", only("jan"))
    seen = []
    p = inv.Probes(environ={"LLM_ROUTER_LOCAL_JAN_PORT": "4242"},
                   http_get=lambda u, t: seen.append(u), user_runtimes=lambda: [])
    inv._inventory_openai_compat(p)
    assert seen == ["http://localhost:4242/v1/models"]


# ── map + config ────────────────────────────────────────────────────────────

URLS = {
    "http://localhost:1337/v1/models": models_body(*[f"j{i}" for i in range(7)]),
    "http://localhost:8000/v1/models": models_body("v1", "v2"),
}
JAN_IDS = [f"openai_compat/j{i}" for i in range(7)]


def build_map(tmp_path, monkeypatch):
    monkeypatch.setattr(local_platforms, "PLATFORMS", only("jan", "vllm"))
    monkeypatch.setattr(inv, "_user_runtimes", lambda: [])
    inp = rm.MapInputs(
        seats=Seats(claude=Seat(kind="claude.ai"), codex=Seat(kind="chatgpt"),
                    gemini=Seat(kind="google"), ollama=Seat(kind="local")),
        env={}, http_get=lambda u: URLS.get(u), http_post=lambda u, b: {},
        claude_reading=lambda: (None, "stale", None), codex_counter=lambda: None,
        codex_sessions=tmp_path / "nocodex", gemini_quota=lambda: None, policy="balanced",
        now=NOW, keys=lambda: [])
    return rm.build(inp)


def test_e_map_rows_and_config_read(state, tmp_path, monkeypatch):
    data = build_map(tmp_path, monkeypatch)
    rows = [r for r in data["resources"] if r["chain_id"].startswith("openai_compat/")]
    assert len(rows) == 9
    assert {r["runtime"] for r in rows} == {"jan", "vllm"}
    assert all(r["privacy"] == "local" and r["endpoint"].startswith("http://localhost:") for r in rows)
    rm.write_map(data)
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    cfg = Config()
    assert cfg.all_openai_compat_models() == JAN_IDS
    assert cfg.openai_compat_base_url == "http://localhost:1337/v1"
    assert len(cfg.legacy_compat_history_ids()) == 5


def test_f_gate_keeps_every_compat_model_off_the_front(state, tmp_path, monkeypatch):
    rm.write_map(build_map(tmp_path, monkeypatch))
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    cfg = Config()
    ids = cfg.all_openai_compat_models()
    assert order_for_chain(ids, "query", critical=False) == ([], ids)
    assert order_for_chain(ids, "query", critical=True) == ([], [])
    (state / "grandfathered_models.json").write_text(json.dumps({"at": 1, "ids": ids[:5]}))
    front, tail = order_for_chain(ids, "query", critical=False)
    assert front == ids[:5] and tail == ids[5:]


def test_g_startup_summary_from_map(state, tmp_path, monkeypatch, capsys):
    rm.write_map(build_map(tmp_path, monkeypatch))
    local_platforms.print_startup_summary_from_map()
    out = capsys.readouterr().out
    assert "Jan" in out and "7 model(s)" in out and "(+4 more)" in out
    assert "vLLM" in out


def test_g_no_map_falls_back(state, monkeypatch, capsys):
    called = []
    monkeypatch.setattr(local_platforms, "print_startup_summary", lambda: called.append(1))
    local_platforms.print_startup_summary_from_map()
    assert called == [1]


def test_h_repo_config_runtimes():
    cfg = _dict_to_config({"runtimes": [
        {"name": "Box", "url": "http://10.0.0.2:8000", "key": "box"},
        {"name": "no url"}, "junk", {"name": 5, "url": "x"}]}, "t")
    assert cfg.runtimes == [{"name": "Box", "url": "http://10.0.0.2:8000", "key": "box"}]
