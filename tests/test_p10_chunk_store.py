"""P10: LLM_ROUTER_CHUNK_STORE (default off) caches valid chunk summaries for the P9 summarizer.

Unit tests drive ``mr_summary.shorten`` with a stub map call; the proxy tests reuse the P9 fixtures. Every test
runs against a tmp LLM_ROUTER_HOME, never the real ~/.llm-router. No network.
"""

from __future__ import annotations

import asyncio
import copy
import json
import stat

import pytest

from llm_router import failopen
from llm_router.proxy import chunk_store, deadline, mr_summary
from llm_router.token_estimate import est_tokens
from tests.test_p9_proxy_mr import SUMMARY, MRBackend, _is_map, _post, _session, _warm
from tests.test_proxy_local_mode import MODEL, Backend, Breaker, _call, _first_call, _ollama, _overrides_readable, env  # noqa: F401  (fixtures)

CAP = 40_000


def _est(b: dict) -> int:
    return est_tokens(json.dumps(b))


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LLM_ROUTER_CHUNK_STORE", raising=False)
    return tmp_path / "home"


class Counter:
    def __init__(self, reply=SUMMARY):
        self.reply, self.prompts, self.nps = reply, [], []

    async def __call__(self, prompt, num_predict):
        self.prompts.append(prompt)
        self.nps.append(num_predict)
        return self.reply


def _run(body, call, store, model="ollama/m"):
    return asyncio.run(mr_summary.shorten(copy.deepcopy(body), call, cap=CAP, est=_est, store=store, model=model))


def _body():
    return _session(pairs=40)


def test_second_turn_with_same_chunks_makes_no_map_calls():
    call = Counter()
    r1 = _run(_body(), call, chunk_store.ChunkStore())
    assert r1.changed and r1.row["store_misses"] == r1.row["chunks"] == r1.row["store_writes"] == r1.row["map_calls"] > 0
    assert r1.row["store_hits"] == 0
    n = len(call.prompts)
    r2 = _run(_body(), call, chunk_store.ChunkStore())  # fresh object: read back from the file
    assert len(call.prompts) == n and r2.row["map_calls"] == 0
    assert r2.row["store_hits"] == r2.row["chunks"] and r2.row["store_misses"] == r2.row["store_writes"] == 0
    assert r2.body == r1.body  # the summaries are the same text


def test_changed_chunk_prompt_version_model_or_num_predict_is_a_miss(monkeypatch):
    call = Counter()
    base = _run(_body(), call, chunk_store.ChunkStore())
    chunks = base.row["chunks"]

    def hits(**kw):
        return _run(_body(), call, chunk_store.ChunkStore(), **kw).row["store_hits"]

    assert hits(model="ollama/other") == 0
    with monkeypatch.context() as m:
        m.setattr(mr_summary, "PROMPT_VERSION", "mr-map-v2")
        assert hits() == 0
    with monkeypatch.context() as m:
        m.setenv("LLM_ROUTER_MR_MAP_NUM_PREDICT", "300")
        assert hits() == 0
    assert hits() == chunks  # the original entries are still there
    changed = _body()
    changed["messages"][2]["content"][0]["content"] += "edited"  # first chunk text differs
    row = _run(changed, call, chunk_store.ChunkStore()).row
    assert row["store_misses"] >= 1 and row["store_hits"] < chunks


def test_corrupt_store_is_a_miss_with_failopen_and_the_request_is_served(home, monkeypatch):
    seen = []
    monkeypatch.setattr(failopen, "record", lambda code, exc=None, **kw: seen.append(code))
    home.mkdir(parents=True, exist_ok=True)
    (home / chunk_store.FILE_NAME).write_text("{not json")
    call = Counter()
    r = _run(_body(), call, chunk_store.ChunkStore())
    assert r.changed and r.row["store_hits"] == 0 and r.row["store_misses"] == r.row["chunks"]
    assert "LR-FO-PROXY-CHUNK-STORE" in seen
    assert json.loads((home / chunk_store.FILE_NAME).read_text())["schema"] == chunk_store.SCHEMA  # healed by the next write


def test_unreadable_store_path_never_raises(home, monkeypatch):
    seen = []
    monkeypatch.setattr(failopen, "record", lambda code, exc=None, **kw: seen.append(code))
    home.mkdir(parents=True, exist_ok=True)
    (home / chunk_store.FILE_NAME).mkdir()  # a directory where the file should be
    r = _run(_body(), Counter(), chunk_store.ChunkStore())
    assert r.changed and seen


def test_extractive_results_are_not_stored(home):
    call = Counter(reply="no headings here")
    r = _run(_body(), call, chunk_store.ChunkStore())
    assert r.changed and r.row["extractive_chunks"] == r.row["chunks"]
    assert r.row["store_writes"] == 0 and not (home / chunk_store.FILE_NAME).exists()
    good = Counter()
    r2 = _run(_body(), good, chunk_store.ChunkStore())  # a later model map replaces the extractive one
    assert r2.row["store_hits"] == 0 and r2.row["store_writes"] == r2.row["chunks"] and good.prompts


def test_store_file_is_mode_0600_and_atomic(home):
    _run(_body(), Counter(), chunk_store.ChunkStore())
    p = home / chunk_store.FILE_NAME
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert [f.name for f in home.iterdir() if f.name.startswith("chunk_summaries")] == [chunk_store.FILE_NAME]  # no temp file left behind


def test_size_cap_evicts_least_recently_used(home):
    s = chunk_store.ChunkStore(cap=3)
    for i in range(5):
        s.put(f"k{i}", "body%d" % i)
    s.flush()
    assert list(chunk_store.ChunkStore(cap=3).entries) == ["k2", "k3", "k4"]
    s2 = chunk_store.ChunkStore(cap=3)
    s2.get("k2")  # a hit alone never rewrites the file
    mtime = (home / chunk_store.FILE_NAME).stat().st_mtime_ns
    s2.flush()
    assert (home / chunk_store.FILE_NAME).stat().st_mtime_ns == mtime
    s2.put("k5", "b")
    s2.flush()
    assert list(chunk_store.ChunkStore(cap=3).entries) == ["k3", "k4", "k5"]  # recency from a get is in-memory only


def test_cap_env_var(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_CHUNK_STORE_MAX", "7")
    assert chunk_store.max_entries() == 7
    monkeypatch.setenv("LLM_ROUTER_CHUNK_STORE_MAX", "zero")
    assert chunk_store.max_entries() == chunk_store.DEFAULT_MAX_ENTRIES


def test_ledger_counts_hold_no_text():
    r = _run(_body(), Counter(), chunk_store.ChunkStore())
    assert {"store_hits", "store_misses", "store_writes"} <= set(r.row)
    assert all(isinstance(v, (int, float, str)) for v in r.row.values())
    blob = json.dumps(r.row)
    assert "none-0" not in blob and "line 0 of output" not in blob and "chunk-summary" not in blob


def test_no_store_row_has_no_store_keys():
    r = _run(_body(), Counter(), None)
    assert not any(k.startswith("store_") for k in r.row)


# -- through the proxy ----------------------------------------------------------------------------------

async def test_proxy_second_turn_hits_the_store(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    monkeypatch.setenv("LLM_ROUTER_CHUNK_STORE", "1")
    backend = MRBackend()
    app = env.app(backend)
    await _warm(app)
    await _post(app, copy.deepcopy(_session()))
    first = env.rows()[-1]["mr_summary"]
    n = len(backend.maps)
    assert first["outcome"] == "summarized" and first["store_writes"] == first["chunks"] == n
    await _post(app, copy.deepcopy(_session()))
    second = env.rows()[-1]["mr_summary"]
    assert len(backend.maps) == n and second["map_calls"] == 0 and second["store_hits"] == second["chunks"]
    assert env.up.requests == []


async def test_proxy_flag_off_writes_no_store_and_adds_no_keys(env, home, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    backend = MRBackend()
    app = env.app(backend)
    await _warm(app)
    await _post(app, copy.deepcopy(_session()))
    mr = env.rows()[-1]["mr_summary"]
    assert not any(k.startswith("store_") for k in mr) and not (home / chunk_store.FILE_NAME).exists()


async def test_store_flag_without_proxy_mr_does_nothing(env, home, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_CHUNK_STORE", "1")
    backend = MRBackend()
    app = env.app(backend)
    await _warm(app)
    await _post(app, copy.deepcopy(_session()))
    assert backend.maps == [] and "mr_summary" not in env.rows()[-1] and not (home / chunk_store.FILE_NAME).exists()


async def test_timeout_row_carries_elapsed_and_map_calls_and_is_stamped_wasted(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    monkeypatch.setattr(deadline, "cap_s", lambda keepalive=False: 0.2)

    class Slow(MRBackend):
        async def complete(self, body, timeout_s):
            if _is_map(body):
                await asyncio.sleep(5)
            return await super().complete(body, timeout_s)

    app = env.app(Slow())
    await _warm(app)
    r = await _post(app, copy.deepcopy(_session()))
    mr = env.rows()[-1]["mr_summary"]
    assert r.status_code == 200 and mr["outcome"] == "timeout"
    assert mr["map_calls"] == 1 and 0.1 <= mr["elapsed_s"] < 3
    assert mr["wasted"] is True and mr["wasted_reason"]
    assert len(env.up.requests) == 1  # the turn ended on Claude


async def test_error_row_carries_elapsed_and_map_calls_and_is_stamped_wasted(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    real = mr_summary.shorten

    async def boom(body, map_call, *, progress, **kw):
        await map_call("p", 1)  # one real map call, then the summarizer dies
        progress["map_calls"] = 1
        raise ValueError("estimator broke")

    monkeypatch.setattr(mr_summary, "shorten", boom)
    app = env.app(MRBackend())
    await _warm(app)
    await _post(app, copy.deepcopy(_session()))
    mr = env.rows()[-1]["mr_summary"]
    assert mr["outcome"] == "error" and mr["map_calls"] == 1 and mr["elapsed_s"] >= 0
    assert mr["wasted"] is True and mr["wasted_reason"] and len(env.up.requests) == 1
    assert real is not boom


# -- review SHOULDs (PR 894) ----------------------------------------------------------------------------

def test_lone_surrogate_chunk_still_summarizes_and_is_stored():
    body = _body()
    body["messages"][2]["content"][0]["content"] = "\ud800x" + body["messages"][2]["content"][0]["content"]
    call = Counter()
    r = _run(body, call, chunk_store.ChunkStore())
    assert r.changed and r.row["outcome"] == "summarized" and r.row["store_writes"] == r.row["chunks"]
    assert _run(body, call, chunk_store.ChunkStore()).row["store_hits"] == r.row["chunks"]


def test_key_failure_means_no_store_for_that_chunk_but_the_map_runs(monkeypatch):
    seen = []
    monkeypatch.setattr(failopen, "record", lambda code, exc=None, **kw: seen.append(code))

    def boom(*a, **k):
        raise UnicodeEncodeError("utf-8", "x", 0, 1, "boom")

    monkeypatch.setattr(chunk_store, "chunk_key", boom)
    call = Counter()
    r = _run(_body(), call, chunk_store.ChunkStore())
    assert r.changed and call.prompts and r.row["store_writes"] == 0 and "LR-FO-PROXY-CHUNK-STORE" in seen


@pytest.mark.parametrize("name,value", [("BODY_EST", 260), ("BLOCK_CHARS", 5_000), ("INPUT_EST", 23_000)])
def test_changed_clip_constant_is_a_miss(monkeypatch, name, value):
    call = Counter()
    _run(_body(), call, chunk_store.ChunkStore())
    with monkeypatch.context() as m:
        m.setattr(mr_summary, name, value)
        assert _run(_body(), call, chunk_store.ChunkStore()).row["store_hits"] == 0


def test_changed_mark_is_a_miss(monkeypatch):
    call = Counter()
    _run(_body(), call, chunk_store.ChunkStore())
    for name in ("BLOCK_MARK", "INPUT_MARK"):
        with monkeypatch.context() as m:
            m.setattr(mr_summary, name, getattr(mr_summary, name) + " ")
            assert _run(_body(), call, chunk_store.ChunkStore()).row["store_hits"] == 0


def test_over_length_valid_reply_hits_on_the_second_turn_with_identical_text():
    long_reply = "\n".join(f"{h}\n" + " ".join(["word"] * 40) for h in mr_summary.HEADINGS)
    assert mr_summary.valid_summary(long_reply) and est_tokens(long_reply) > mr_summary.BODY_EST
    call = Counter(long_reply)
    r1 = _run(_body(), call, chunk_store.ChunkStore())
    assert r1.changed and r1.row["map_calls"] > 0
    n = len(call.prompts)
    r2 = _run(_body(), call, chunk_store.ChunkStore())
    assert len(call.prompts) == n and r2.row["map_calls"] == 0 and r2.row["store_hits"] == r2.row["chunks"]
    assert r2.body == r1.body  # injected text byte-identical to what the miss injected


def test_preamble_reply_is_not_stored_and_does_not_churn_the_file(home):
    call = Counter("Sure, here:\n" + SUMMARY)
    assert mr_summary.valid_summary(call.reply)
    r1 = _run(_body(), call, chunk_store.ChunkStore())
    assert r1.changed and r1.row["store_writes"] == 0
    p = home / chunk_store.FILE_NAME
    before = p.read_bytes() if p.exists() else None
    r2 = _run(_body(), call, chunk_store.ChunkStore())
    assert r2.row["store_writes"] == 0 and r2.row["store_hits"] == 0
    assert (p.read_bytes() if p.exists() else None) == before


def test_changed_map_prompt_est_tokens_or_token_constant_is_a_miss(monkeypatch):
    from llm_router import token_estimate as te
    call = Counter()
    _run(_body(), call, chunk_store.ChunkStore())
    orig_prompt, orig_est = mr_summary.map_prompt, mr_summary.est_tokens

    def other_prompt(messages):
        return orig_prompt(messages)  # different source, same text

    def other_est(x):
        return orig_est(x)

    for target, name, val in ((mr_summary, "map_prompt", other_prompt), (mr_summary, "est_tokens", other_est),
                              (te, "DIGIT_TOKENS", te.DIGIT_TOKENS + 0.1),
                              (te, "WIDE_CHAR_TOKENS", te.WIDE_CHAR_TOKENS + 0.1),
                              (te, "OTHER_CHARS_PER_TOKEN", te.OTHER_CHARS_PER_TOKEN + 0.1)):
        with monkeypatch.context() as m:
            m.setattr(mr_summary, "_CODE_HASH", None)  # recomputed from the patched source
            m.setattr(target, name, val)
            assert _run(_body(), call, chunk_store.ChunkStore()).row["store_hits"] == 0, name


def test_changed_render_or_validity_code_hash_is_a_miss(monkeypatch):
    call = Counter()
    _run(_body(), call, chunk_store.ChunkStore())
    with monkeypatch.context() as m:
        m.setattr(mr_summary, "_CODE_HASH", "edited-render-or-valid_summary")
        assert _run(_body(), call, chunk_store.ChunkStore()).row["store_hits"] == 0


def test_poisoned_or_stale_entry_is_a_miss_and_not_served(home):
    call = Counter()
    first = _run(_body(), call, chunk_store.ChunkStore())
    p = home / chunk_store.FILE_NAME
    doc = json.loads(p.read_text())
    doc["entries"][0][1] = "POISON: no headings"
    doc["entries"][1][1] = SUMMARY + "\n" + "word " * 2000  # over BODY_EST
    p.write_text(json.dumps(doc))
    n = len(call.prompts)
    r = _run(_body(), call, chunk_store.ChunkStore())
    assert r.row["store_misses"] == 2 and r.row["store_hits"] == first.row["chunks"] - 2
    assert len(call.prompts) == n + 2 and "POISON" not in json.dumps(r.body)
    assert "POISON" not in p.read_text()  # replaced by the fresh summary


def test_two_instances_writing_different_entries_both_survive(home):
    a, b = chunk_store.ChunkStore(), chunk_store.ChunkStore()  # both loaded before either writes
    a.put("ka", "A")
    a.flush()
    b.put("kb", "B")
    b.flush()
    got = chunk_store.ChunkStore().entries
    assert got == {"ka": "A", "kb": "B"}


def test_concurrent_flush_threads_keep_all_entries(home):
    import threading
    stores = [chunk_store.ChunkStore() for _ in range(4)]
    for i, st in enumerate(stores):
        st.put(f"k{i}", str(i))
    ts = [threading.Thread(target=st.flush) for st in stores]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    # an unlocked read-merge-replace can still lose a race; one final flush from each loser must converge
    for st in stores:
        st._added = {k for k in st.entries if k.startswith("k")}
        st.flush()
    assert set(chunk_store.ChunkStore().entries) >= {f"k{i}" for i in range(4)}


@pytest.mark.parametrize("payload", [
    {"schema": 2, "entries": [["k", "v"]]},
    {"schema": 1, "entries": "nope"},
    {"schema": 1, "entries": [["k"], 5, ["k2", 7]]},
    ["not", "a", "dict"],
])
def test_schema_or_malformed_store_is_a_miss_with_failopen(home, monkeypatch, payload):
    seen = []
    monkeypatch.setattr(failopen, "record", lambda code, exc=None, **kw: seen.append(code))
    home.mkdir(parents=True, exist_ok=True)
    (home / chunk_store.FILE_NAME).write_text(json.dumps(payload))
    st = chunk_store.ChunkStore()
    assert st.entries == {} and st.get("k") is None and seen


def test_symlinked_target_is_replaced_not_followed(home):
    home.mkdir(parents=True, exist_ok=True)
    victim = home / "victim.txt"
    victim.write_text("keep me")
    (home / chunk_store.FILE_NAME).symlink_to(victim)
    st = chunk_store.ChunkStore()
    st.put("k", "v")
    st.flush()
    p = home / chunk_store.FILE_NAME
    assert not p.is_symlink() and victim.read_text() == "keep me"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600 and chunk_store.ChunkStore().entries == {"k": "v"}


# ---- P10 x P11: the store holds the raw summary; the gate runs on every result, hit or fresh ----------------
def _gated_body():
    from tests.test_p11_summary_gate import _session as gated_session
    return gated_session(40)


def _gated_run(call, store=None):
    return _run(_gated_body(), call, store or chunk_store.ChunkStore())


def test_store_hit_still_runs_the_gate_and_pins_a_missing_fact(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SUMMARY_GATE", "1")
    call = Counter()  # SUMMARY names none of the chunk's paths / errors
    r1 = _gated_run(call)
    assert r1.changed and r1.row["map_calls"] > 0 and r1.row["gate_missing_path"] > 0
    n = len(call.prompts)
    r2 = _gated_run(call)
    assert len(call.prompts) == n and r2.row["map_calls"] == 0 and r2.row["store_hits"] == r2.row["chunks"] > 0
    assert r2.row["gate_checked"] == r1.row["gate_checked"] and r2.row["gate_missing_path"] == r1.row["gate_missing_path"] > 0
    served = json.dumps(r2.body["messages"][0])
    assert "pinned:" in served and "src/pkg/mod0.py" in served and "ValueError: boom 0" in served
    assert r2.body == r1.body


def test_stored_entry_is_the_raw_summary_not_the_gated_one(home, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SUMMARY_GATE", "1")
    store = chunk_store.ChunkStore()
    r = _gated_run(Counter(), store)
    assert r.row["gate_missing_path"] > 0 and "pinned:" in json.dumps(r.body["messages"][0])
    assert store.entries and set(store.entries.values()) == {SUMMARY.strip()}
    assert set(chunk_store.ChunkStore().entries.values()) == {SUMMARY.strip()}  # and so is the file


def test_gate_rejection_on_a_hit_behaves_like_a_fresh_rejection(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SUMMARY_GATE", "1")
    call = Counter()
    ok = _gated_run(call)  # populates the store with the raw summaries
    assert ok.changed
    from llm_router.proxy import summary_gate
    monkeypatch.setattr(summary_gate, "pin_block", lambda facts, miss: "")  # facts stay missing: the gate rejects
    fresh = _gated_run(Counter(), chunk_store.ChunkStore(path=chunk_store.store_path().with_name("other.json")))
    n = len(call.prompts)
    hit = _gated_run(call)
    assert len(call.prompts) == n and hit.row["store_hits"] > 0  # the entry was served as a hit...
    for r in (fresh, hit):  # ...and the gate still rejected it exactly as for a fresh summary
        assert not r.changed and r.row["outcome"] == mr_summary.OUTCOME_GATE_FAILED and r.row["gate_failed"] == 1
        assert r.body == _gated_body()


def test_gate_rejected_hit_is_evicted_and_next_run_remaps(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SUMMARY_GATE", "1")
    call = Counter()
    assert _gated_run(call).changed  # populates the store
    from llm_router.proxy import summary_gate
    with monkeypatch.context() as m:
        m.setattr(summary_gate, "pin_block", lambda facts, miss: "")  # the gate rejects the stored summary
        n = len(call.prompts)
        hit = _gated_run(call)
        assert len(call.prompts) == n and hit.row["store_hits"] > 0 and hit.row["gate_failed"] == 1
    healed = _gated_run(call)  # gate works again: the evicted chunk is mapped afresh, not served from the store
    assert len(call.prompts) > n and healed.row["map_calls"] >= 1


def test_gate_rejected_fresh_summary_is_also_evicted(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SUMMARY_GATE", "1")
    from llm_router.proxy import summary_gate
    monkeypatch.setattr(summary_gate, "pin_block", lambda facts, miss: "")
    store = chunk_store.ChunkStore()
    r = _gated_run(Counter(), store)
    assert r.row["gate_failed"] == 1 and r.row["store_writes"] >= 1
    assert not store.entries and not chunk_store.ChunkStore().entries
