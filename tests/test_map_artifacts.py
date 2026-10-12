"""P1.8 task 12: model artifacts on disk. Fixtures under tmp_path only; fake clocks."""
from __future__ import annotations

import builtins
import json
import os
import time
from pathlib import Path

from llm_router import map_reader, resource_map as rm
from llm_router.discovery.keys import discover
from llm_router.resolver import inventory as inv
from llm_router.seats import Seat, Seats
from llm_router.types import Complexity, RoutingProfile, TaskType

NOW = 1_790_000_000.0
BLOB = "ab" * 32
ST_HEADER = (8).to_bytes(8, "little") + b'{"a":{}} '[:8]


def _write(p: Path, data: bytes = b"") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def hf_fixture(tmp_path: Path) -> tuple[dict, Path]:
    hub = tmp_path / "home" / ".cache" / "huggingface" / "hub"
    _write(hub / "models--TheBloke--tiny-GGUF" / "blobs" / BLOB, b"GGUF" + b"\0" * 64)
    snap = hub / "models--TheBloke--tiny-GGUF" / "snapshots" / "rev1"
    snap.mkdir(parents=True)
    os.symlink(f"../../blobs/{BLOB}", snap / "tiny.Q4.gguf")
    _write(snap / "fake.gguf", b"NOPE" + b"\0" * 16)
    for repo in ("org--st-model", "mlx-community--m-4bit"):
        s = hub / f"models--{repo}" / "snapshots" / "rev1"
        _write(s / "config.json", b"{}")
        _write(s / "model.safetensors", ST_HEADER + b"\0" * 32)
    _write(hub / "models--org--nocfg" / "snapshots" / "rev1" / "model.safetensors", ST_HEADER)
    return {"HOME": str(tmp_path / "home")}, hub


def scan(env, **kw):
    return inv._inventory_artifacts(env, **kw)


def test_formats_and_state(tmp_path):
    env, _ = hf_fixture(tmp_path)
    rows, status = scan(env)
    assert status.ok
    fmt = {r["artifact"]["repo"]: r["artifact"]["format"] for r in rows}
    assert fmt == {"TheBloke/tiny-GGUF": "gguf", "org/st-model": "safetensors",
                   "mlx-community/m-4bit": "mlx"}
    assert all(r["status"] == r["state"] == "discovered" and r["reason"] == "no_executable_runtime"
               for r in rows)
    gg = next(r for r in rows if r["artifact"]["format"] == "gguf")
    assert gg["artifact"]["blob_sha256"] == BLOB and gg["resource"].endswith("tiny.Q4.gguf")


def test_mlx_from_config_in_other_namespace(tmp_path):
    root = tmp_path / "models"
    _write(root / "acme" / "x" / "config.json", json.dumps({"library_name": "mlx"}).encode())
    _write(root / "acme" / "x" / "w.safetensors", ST_HEADER)
    rows, _ = scan({"HOME": str(tmp_path)}, extra_dirs=[root])
    assert [r["artifact"]["format"] for r in rows] == ["mlx"]


def test_reads_header_bytes_only(tmp_path, monkeypatch):
    env, _ = hf_fixture(tmp_path)
    reads = []
    real_open = builtins.open

    def spy(path, mode="r", *a, **k):
        f = real_open(path, mode, *a, **k)
        if "b" in mode:
            orig = f.read
            f.read = lambda n=-1: (reads.append((str(path), n)), orig(n))[1]
        return f
    monkeypatch.setattr(builtins, "open", spy)
    rows, _ = scan(env)
    assert rows and reads
    for path, n in reads:
        limit = (1 << 20) if path.endswith("config.json") else 9
        assert 0 <= n <= limit, (path, n)


def test_bounds_and_symlink_loop(tmp_path):
    env, hub = hf_fixture(tmp_path)
    rows, st = scan(env, max_files=2)
    assert "truncated: file limit" in st.detail and len(rows) < 3
    t = iter(x * 10.0 for x in range(1000))
    rows, st = scan(env, monotonic=lambda: next(t), max_seconds=5.0)
    assert "truncated: time limit" in st.detail
    os.symlink(hub, hub / "models--org--st-model" / "loop")
    rows, st = scan(env)
    assert len(rows) == 3 and "truncated" not in st.detail


def test_roots(tmp_path):
    h = tmp_path / "home"
    _write(h / ".lmstudio" / "models" / "pub" / "repo" / "m.gguf", b"GGUF")
    _write(h / ".ollama" / "models" / "m.gguf", b"GGUF")
    _write(h / "mine" / "u.gguf", b"GGUF")
    _write(h / "extra" / "e.gguf", b"GGUF")
    _write(h / "hfh" / "hub" / "models--a--b" / "snapshots" / "r" / "h.gguf", b"GGUF")
    env = {"HOME": str(h), "LLM_ROUTER_ARTIFACT_DIRS": str(h / "mine")}
    rows, _ = scan(env, extra_dirs=[h / "extra"])
    assert {r["artifact"]["origin"] for r in rows} == {"lmstudio", "user_dir"}
    assert sorted(Path(r["artifact"]["path"]).name for r in rows) == ["e.gguf", "m.gguf", "u.gguf"]
    rows, _ = scan({"HOME": str(h), "HF_HOME": str(h / "hfh")})
    assert [r["artifact"]["origin"] for r in rows if r["artifact"]["origin"] == "hf_cache"] == ["hf_cache"]


def rt(**limits):
    return rm._row("ollama/x", "local", "ollama/x", status="connected", limits=limits)


def test_merge_by_hash_path_and_no_fuzzy_join(tmp_path):
    env, _ = hf_fixture(tmp_path)
    arts, _ = scan(env)
    gg = next(a for a in arts if a["artifact"]["format"] == "gguf")
    by_hash = inv.merge_artifacts([rt(blob_sha256=BLOB)], [gg])
    assert len(by_hash) == 1 and by_hash[0]["state"] == "accessible"
    assert by_hash[0]["status"] == "connected" and by_hash[0]["artifact"]["format"] == "gguf"
    for key in ("path", "real_path"):
        assert len(inv.merge_artifacts([rt(path=gg["artifact"][key])], [gg])) == 1
    other = rt(blob_sha256="cd" * 32, size_bytes=gg["artifact"]["size_bytes"])
    other["resource"] = "ollama/tiny-GGUF"       # same name and size, different hash: no join
    out = inv.merge_artifacts([other], [gg])
    assert len(out) == 2 and "state" not in out[0] and out[1]["state"] == "discovered"
    assert len(inv.merge_artifacts([rt()], [gg])) == 2
    assert inv.runtime_identity(f"FROM /h/.ollama/models/blobs/sha256-{BLOB}\nX") == {
        "path": f"/h/.ollama/models/blobs/sha256-{BLOB}", "blob_sha256": BLOB}
    assert inv.runtime_identity("FROM llama3") == {} and inv.runtime_identity(None) == {}


def make_inputs(env, *, reachable, modelfile=None, tmp_path, now=NOW):
    return rm.MapInputs(
        seats=Seats(claude=Seat(kind="none"), codex=Seat(kind="none"), gemini=Seat(kind="none"),
                    ollama=Seat(kind="local")),
        env={}, policy="balanced", now=now,
        http_get=lambda u: {"models": [{"name": "tiny:latest", "size": 1}]}
        if reachable and u.endswith("/api/tags") else None,
        http_post=lambda u, b: {"capabilities": ["completion"], "modelfile": modelfile},
        claude_reading=lambda: (None, "unknown", None), codex_counter=lambda: None,
        codex_sessions=tmp_path / "none", gemini_quota=lambda: None,
        keys=lambda: discover(env={}, dotenv_paths=[], keychain=lambda v: False),
        artifacts=lambda: inv._inventory_artifacts(env)[0],
    )


def test_one_refresh_makes_runtime_listed_artifact_accessible(tmp_path):
    env, _ = hf_fixture(tmp_path)
    data = rm.build(make_inputs(env, reachable=True, tmp_path=tmp_path,
                                modelfile=f"FROM /x/blobs/sha256-{BLOB}"))
    res = data["resources"]
    assert [r["resource"] for r in res if "tiny" in r["resource"]] == ["ollama/tiny:latest"]
    row = next(r for r in res if r["resource"] == "ollama/tiny:latest")
    assert row["state"] == "accessible" and row["artifact"]["blob_sha256"] == BLOB
    assert sorted(r["artifact"]["format"] for r in res if r.get("state") == "discovered") == ["mlx", "safetensors"]
    rm.render_table(data)
    # a runtime listing a different name/hash does not join
    data = rm.build(make_inputs(env, reachable=True, tmp_path=tmp_path, modelfile="FROM /x/blobs/sha256-" + "cd" * 32))
    assert sum(1 for r in data["resources"] if r.get("state") == "discovered") == 3
    assert "state" not in next(r for r in data["resources"] if r["resource"] == "ollama/tiny:latest")


async def test_no_runtime_artifacts_never_route_in_300_decisions(tmp_path, monkeypatch):
    import llm_router.dynamic_routing as dr
    from llm_router.config import RouterConfig, get_config
    from llm_router.hooks import chain_builder as hook_cb
    from llm_router.hooks.direct_executor import ModelSpec
    from llm_router.proxy import backends
    from llm_router.router import _build_and_filter_chain

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "rhome"))
    local, gem = "ollama/qwen3:8b", "gemini/gemini-2.5-flash"
    monkeypatch.setattr(dr, "get_dynamic_model_chain", lambda profile, task: [local, gem])
    monkeypatch.setattr(hook_cb, "_ollama_models", lambda *a, **k: ([ModelSpec("ollama", "qwen3:8b")], []))
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setattr(backends, "_chain_cache", {})
    monkeypatch.setattr(RouterConfig, "all_ollama_models", lambda self: [local])
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "qwen3:8b")

    env, _ = hf_fixture(tmp_path)
    data = rm.build(make_inputs(env, reachable=False, tmp_path=tmp_path, now=time.time()))
    data["resources"].append(rm._row("gemini/api", "api", "gemini/api", status="connected",
                                     capabilities=["generate"], effective_priority=1))
    rm.write_map(data)
    p = rm.map_path()
    os.utime(p, ns=(p.stat().st_atime_ns, p.stat().st_mtime_ns + 1_000_000))
    art_ids = {r["chain_id"] for r in data["resources"] if r.get("state") == "discovered"}
    assert len(art_ids) == 3
    view = map_reader.load_view()
    assert view is not None and not (view.connected & {"artifact"})

    decisions = routed = nonempty = 0
    profiles = list(RoutingProfile)[:3]
    tasks = [TaskType.CODE, TaskType.QUERY, TaskType.GENERATE, TaskType.ANALYZE, TaskType.RESEARCH]
    for prof in profiles:
        for task in tasks:
            for i in range(20):
                cx = [Complexity.SIMPLE, Complexity.MODERATE][i % 2]
                chains = [await _build_and_filter_chain(task, prof, None, cx.value, cx, get_config()),
                          [f"{m.provider}/{m.model}" for m in hook_cb.build_chain("simple", "green", "code")],
                          (await backends.policy_chain(f"write a python function number {i}"))[2]]
                decisions += 1
                for c in chains:
                    nonempty += bool(c)
                    routed += sum(1 for m in c if m in art_ids or m.startswith("artifact/") or m == local)
    assert decisions == 300
    assert nonempty > 0          # the check had chains to inspect
    assert routed == 0
