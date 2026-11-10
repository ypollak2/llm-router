"""P18 wiring in proxy/server.py, through the existing proxy test harness (mocked backend, no network).

Flag off: no worker, no task, no ledger file, no row key. Flag on: build_compact_worker, lifespan start and
cancel, note_request / note_done around local_step, the row's compact_bg. Each missing flag builds no worker."""
from __future__ import annotations

import asyncio

import pytest

from llm_router.proxy import compact_worker
from llm_router.toolkit import local_gate as g
from tests.test_p9_proxy_mr import MRBackend
from tests.test_proxy_local_mode import _first_call, _post, env  # noqa: F401  (fixture)

FLAGS = {"LLM_ROUTER_COMPACT_BG": "1", "LLM_ROUTER_LOCAL_QUEUE": "on", "LLM_ROUTER_PROXY_MR": "1",
         "LLM_ROUTER_CHUNK_STORE": "1"}


class PBackend(MRBackend):
    base_url = "http://localhost:59996"   # never contacted

    def __init__(self):
        super().__init__()
        self.priorities = []

    async def complete(self, body, timeout_s, priority="interactive"):
        self.priorities.append(priority)
        return await super().complete(body, timeout_s)


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LLM_ROUTER_LOCAL_GATE_DIR", str(tmp_path / "gate"))
    g._FS_CACHE.clear()
    monkeypatch.setattr(g, "_fstype_for", lambda p: "apfs")
    for v in (*FLAGS, "LLM_ROUTER_COMPACT_BG_TOOL_GAPS"):
        monkeypatch.delenv(v, raising=False)
    compact_worker.clear()
    yield tmp_path / "home"
    compact_worker.clear()


def _spy_run(monkeypatch):
    seen = {"started": False, "cancelled": False}

    async def run(self):
        seen["started"] = True
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            seen["cancelled"] = True
            raise

    monkeypatch.setattr(compact_worker.Worker, "run", run)
    return seen


async def _serve(env, monkeypatch, flags):  # noqa: F811
    for k, v in flags.items():
        monkeypatch.setenv(k, v)
    seen = _spy_run(monkeypatch)
    backend = PBackend()
    app = env.app(backend)
    out = {"backend": backend}
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        out["during"] = compact_worker.get()
        await _post(app, _first_call())
        out["counts"] = out["during"].counts() if out["during"] else None
        if out["during"] is not None:   # a real map through the proxy's own map_call
            out["reply"] = await out["during"].map_call("You compress one slice of a session", 450)
    await asyncio.sleep(0)
    out.update(after=compact_worker.get(), seen=seen, row=env.rows()[-1])
    return out


async def test_flag_off_builds_nothing_and_writes_nothing(env, monkeypatch, clean):  # noqa: F811
    out = await _serve(env, monkeypatch, {})
    assert out["during"] is None and out["after"] is None and not out["seen"]["started"]
    assert "compact_bg" not in out["row"] and not (clean / compact_worker.LEDGER_NAME).exists()


async def test_flag_on_builds_starts_notes_turns_and_cancels(env, monkeypatch, clean):  # noqa: F811
    out = await _serve(env, monkeypatch, FLAGS)
    assert isinstance(out["during"], compact_worker.Worker) and out["seen"]["started"] and out["seen"]["cancelled"]
    assert out["counts"]["live_turns"] == 1 and out["counts"]["started"] == 0
    assert out["row"]["compact_bg"] == {"live_maps_paid": 0}
    assert out["after"] is None   # shutdown clears the installed worker
    assert not (clean / compact_worker.LEDGER_NAME).exists()   # no worker event, no ledger row


@pytest.mark.parametrize("missing", sorted(FLAGS))
async def test_each_missing_flag_builds_no_worker(env, monkeypatch, missing):  # noqa: F811
    out = await _serve(env, monkeypatch, {k: v for k, v in FLAGS.items() if k != missing})
    if missing == "LLM_ROUTER_COMPACT_BG":
        assert out["during"] is None and "compact_bg" not in out["row"]
    else:
        assert out["during"] is None and not out["seen"]["started"] and "compact_bg" not in out["row"]


async def test_worker_maps_go_out_at_background_priority(env, monkeypatch):  # noqa: F811
    out = await _serve(env, monkeypatch, FLAGS)
    backend = out["backend"]
    assert out["reply"] and out["reply"].startswith("## Goal")   # the map reached the live backend and came back
    assert backend.priorities[-1] == "background" and backend.maps   # the worker's map, not the live turn
    assert "background" not in backend.priorities[:-1]            # live turns stay interactive
