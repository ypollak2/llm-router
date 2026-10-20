"""P3.6-a: the example store and the update gate. Synthetic fixtures only: no owner data."""
import json
import random
import time
from array import array

import pytest

from llm_router import classifier_learning as cl
from llm_router import complexity_knn as ck
from llm_router import examples, prompt_key
from llm_router.commands import learn

DIM = 8
MODEL = "synthetic-embed"


def vec(center: int, rng: random.Random) -> list[float]:
    v = [rng.gauss(0, 0.15) for _ in range(DIM)]
    v[center] += 1.0
    return v


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A base index (30 frontier near axis 0, 30 cheap near axis 1), a 120-row replay set and paths."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    art_path = tmp_path / "knn.json"
    replay_path = tmp_path / "replay.jsonl"
    monkeypatch.setenv("LLM_ROUTER_COMPLEXITY_KNN_ARTIFACT", str(art_path))
    monkeypatch.setenv(cl.REPLAY_ENV, str(replay_path))
    rng = random.Random(7)
    vectors = [vec(0, rng) for _ in range(30)] + [vec(1, rng) for _ in range(30)]
    labels = [1.0] * 30 + [0.0] * 30
    ck.write_artifact(art_path, vectors, labels, embedding_model=MODEL, k=15)
    with replay_path.open("w") as fh:
        for i in range(120):
            frontier = i % 2 == 0
            fh.write(json.dumps({"vec": vec(0 if frontier else 1, rng), "needs_frontier": frontier,
                                 "text_sha": f"replay{i:011d}"}) + "\n")
    ck.clear_cache()
    return {"art": art_path, "replay": replay_path, "rng": rng, "ex": tmp_path / "home" / "examples.jsonl"}


def add_examples(world, n, center, label, source="prefix", sha_prefix="ex", ts0=1e9):
    for i in range(n):
        row = {"text_sha": f"{sha_prefix}{center}{label:.0f}{i:09d}", "source": source, "label": label,
               "ts": ts0 + i, "vec": examples.encode_vec(vec(center, world["rng"])), "model": MODEL}
        examples.append_row(row, path=world["ex"])


def run(world, **kw):
    return cl.apply(examples_path=world["ex"], now=1e9 + 100, **kw)


def test_valid_update_accepted_and_installed(world):
    add_examples(world, 10, 0, 1.0)
    add_examples(world, 10, 1, 0.0)
    before = ck.load_artifact().n
    v = run(world)
    assert v.status == cl.ACCEPTED and v.added == 20 and v.base.n == 120
    ck.clear_cache()
    assert ck.load_artifact().n == before + 20
    assert world["art"].with_suffix(".json.prev").exists()
    log = [json.loads(x) for x in cl.decisions_path().read_text().splitlines()]
    assert log[-1]["status"] == "accepted" and "base" in log[-1]
    assert run(world).status == cl.NOTHING_NEW  # applied shas are remembered


def test_poisoned_flipped_labels_rejected(world):
    add_examples(world, 60, 0, 0.0, sha_prefix="px")  # frontier-looking prompts labelled cheap
    raw = world["art"].read_bytes()
    v = run(world)
    assert v.status == cl.REJECTED and "worse_by_more_than_2_pts" in v.reason
    assert v.candidate.under_route - v.base.under_route > 0.02
    assert world["art"].read_bytes() == raw  # the live index is untouched
    assert not world["art"].with_suffix(".json.prev").exists()


def test_exact_match_regression_alone_is_rejected(world):
    add_examples(world, 60, 1, 1.0, sha_prefix="fx")  # cheap-looking prompts labelled frontier (over-route)
    v = run(world)
    assert v.status == cl.REJECTED and "exact" in v.reason
    assert v.candidate.under_route <= v.base.under_route + 0.02  # under-route did not move: exact is what failed


def test_dry_run_installs_nothing(world):
    add_examples(world, 10, 0, 1.0)
    raw = world["art"].read_bytes()
    assert run(world, dry_run=True).status == cl.ACCEPTED
    assert world["art"].read_bytes() == raw


def test_gate_fails_closed(world, monkeypatch):
    add_examples(world, 10, 0, 1.0)
    monkeypatch.setenv(cl.MIN_REPLAY_ENV, "500")
    assert run(world).reason.startswith("replay_too_small")
    monkeypatch.delenv(cl.REPLAY_ENV)
    assert run(world).status == cl.REJECTED  # no replay set at all
    world["art"].unlink()
    ck.clear_cache()
    assert run(world).reason.startswith("no_base_artifact")


def test_held_out_examples_never_join_the_index(world):
    rows = [{"text_sha": f"replay{i:011d}", "source": "prefix", "label": 0.0, "ts": 1e9,
             "vec": examples.encode_vec(vec(0, world["rng"])), "model": MODEL} for i in range(5)]
    for r in rows:
        examples.append_row(r, path=world["ex"])
    assert run(world).status == cl.NOTHING_NEW


def test_wrong_model_or_dim_and_no_vector_rows_skipped(world):
    examples.append_row({"text_sha": "a" * 16, "source": "redo", "label": 1.0, "ts": 1e9}, path=world["ex"])
    examples.append_row({"text_sha": "b" * 16, "source": "prefix", "label": 0.0, "ts": 1e9, "model": "other",
                         "vec": examples.encode_vec(vec(0, world["rng"]))}, path=world["ex"])
    v = run(world)
    assert v.status == cl.NOTHING_NEW and v.detail["without_vector"] == 1


def test_cli_exit_codes(world, capsys):
    add_examples(world, 60, 0, 0.0, sha_prefix="px", ts0=time.time() - 1000)
    assert learn.main(["--json"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "rejected"


# ── the store ────────────────────────────────────────────────────────────────


def test_store_holds_hashes_and_embeddings_never_text(world, monkeypatch):
    secret = "a private prompt that must not be stored"
    monkeypatch.setenv("LLM_ROUTER_COMPLEXITY_KNN", "on")
    monkeypatch.setattr(ck, "_embed", lambda text, model: [1.0] + [0.0] * (DIM - 1))
    assert examples.record(secret, "prefix", 0.0, path=world["ex"], now=5.0, embed=True)
    raw = world["ex"].read_text()
    assert secret not in raw
    row, = examples.read_examples(world["ex"], now=6.0)
    assert row["text_sha"] == prompt_key.key(secret) and row["model"] == MODEL and len(row["vec"]) == DIM
    assert world["ex"].stat().st_mode & 0o777 == 0o600


def test_no_vector_when_knn_is_off(world, monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_COMPLEXITY_KNN", raising=False)
    monkeypatch.setattr(ck, "_embed", lambda *a: pytest.fail("embedded while the index is off"))
    assert examples.record("x", "prefix", 0.0, path=world["ex"], now=5.0, embed=True)
    assert examples.read_examples(world["ex"], now=6.0)[0]["vec"] is None


def test_legacy_prefix_rows_get_source_and_label(tmp_path):
    p = tmp_path / "examples.jsonl"
    rows = [{"text_sha": "a" * 16, "forced": "local", "decided": "ollama/x", "door": "hook", "ts": 10},
            {"text_sha": "b" * 16, "forced": "opus", "decided": "claude", "door": "mcp", "ts": 10},
            {"text_sha": "c" * 16, "forced": "weird", "decided": "?", "door": "mcp", "ts": 10}, "junk"]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    got = examples.read_examples(p, now=20)
    assert [(r["source"], r["label"]) for r in got] == [("prefix", 0.0), ("prefix", 1.0)]


def test_old_rows_are_ignored_and_file_is_bounded(tmp_path):
    p = tmp_path / "examples.jsonl"
    old = {"text_sha": "a" * 16, "source": "redo", "label": 1.0, "ts": 0}
    p.write_text(json.dumps(old) + "\n")
    assert examples.read_examples(p, now=examples.MAX_AGE_S + 1) == []
    big = json.dumps({"text_sha": "x" * 200, "source": "redo", "label": 1.0, "ts": 1}) + "\n"
    p.write_text(big * 100)
    examples.append_row({"text_sha": "n" * 16, "source": "redo", "label": 0.0, "ts": 2}, path=p, max_bytes=5000)
    assert len(p.read_text().splitlines()) == 51


def test_redo_press_becomes_a_label_one_example_by_hash(tmp_path):
    ledger = tmp_path / "proxy.jsonl"
    ledger.write_text(json.dumps({"ts": time.time(), "msg_id": "msg_1", "text_sha": "d" * 16}) + "\n")
    target = tmp_path / "ex.jsonl"
    assert examples.record_redo("msg_1", now=1e9, path=target, ledger_path=ledger)
    assert not examples.record_redo("msg_missing", now=1e9, path=target, ledger_path=ledger)
    row, = examples.read_examples(target, now=1e9)
    assert (row["source"], row["label"], row["text_sha"], row["vec"]) == ("redo", 1.0, "d" * 16, None)


def test_always_local_rule_is_recorded_hash_only(tmp_path, monkeypatch):
    from llm_router import user_rules

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LLM_ROUTER_COMPLEXITY_KNN", "on")  # even with the kNN on, a rule is never embedded
    monkeypatch.setattr(ck, "_embed", lambda *a: pytest.fail("a rule sentence was embedded"))
    user_rules.add_rules([{"kind": "local_only", "task": "query"}], "always local for queries",
                         path=tmp_path / "routing.yaml")
    row, = examples.read_examples(tmp_path / "home" / "examples.jsonl")
    assert (row["source"], row["label"], row["vec"]) == ("rule", 0.0, None)
    user_rules.add_rules([{"kind": "no_paid_api"}], "no paid api", path=tmp_path / "routing.yaml")
    assert len(examples.read_examples(tmp_path / "home" / "examples.jsonl")) == 1


def test_decode_roundtrip():
    assert list(array("f", [0.6, 0.8])) == pytest.approx(list(examples.decode_vec(examples.encode_vec([3, 4]))))


# ── repair round 1: latency, privacy, retention, held-out, concurrency ───────


def _network_forbidden(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_COMPLEXITY_KNN", "on")
    calls = []

    def boom(text, model):
        calls.append(text)
        raise AssertionError("embedder called")

    monkeypatch.setattr(ck, "_embed", boom)
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("network call"))
    return calls


def test_hook_hot_path_makes_no_embed_or_network_call(world, monkeypatch):
    from llm_router import prefixes

    calls = _network_forbidden(monkeypatch)
    assert prefixes.record_example("write a poem", forced="local", decided="ollama", door="hook", path=world["ex"])
    assert calls == []
    row, = examples.read_examples(world["ex"])
    assert row["vec"] is None and row["label"] == 0.0  # hash-only; learn can never embed it later


async def test_proxy_embeds_in_a_worker_thread_never_on_the_loop(world, monkeypatch):
    import asyncio
    import threading

    from llm_router import prefixes

    monkeypatch.setenv("LLM_ROUTER_COMPLEXITY_KNN", "on")
    seen = []
    release = threading.Event()  # the worker waits here, so "returned first" is not a race

    def embed(text, model):
        release.wait(5)
        seen.append(threading.current_thread() is threading.main_thread())
        return [1.0] + [0.0] * (DIM - 1)

    monkeypatch.setattr(ck, "_embed", embed)
    assert prefixes.record_example("p", forced="claude", decided="x", door="proxy", path=world["ex"], embed=True)
    assert seen == []  # the handler returned before any embedding ran
    release.set()
    for _ in range(200):
        if world["ex"].exists() and "vec" in world["ex"].read_text():
            break
        await asyncio.sleep(0.01)
    assert seen == [False]  # embedded, off the event-loop thread
    assert examples.read_examples(world["ex"])[0]["vec"] is not None


def test_old_rows_are_deleted_from_disk_on_append_and_in_learn(world):
    old = {"text_sha": "o" * 16, "source": "redo", "label": 1.0, "ts": 100.0}
    examples.append_row(old, path=world["ex"])
    now = 100.0 + examples.MAX_AGE_S + 5
    examples.append_row({"text_sha": "n" * 16, "source": "redo", "label": 0.0, "ts": now}, path=world["ex"])
    shas = [json.loads(x)["text_sha"] for x in world["ex"].read_text().splitlines()]
    assert shas == ["n" * 16]  # the 180-day-old row is gone from the file, not just hidden
    examples.append_row({"text_sha": "m" * 16, "source": "redo", "label": 0.0, "ts": 1e9}, path=world["ex"])
    assert examples.prune(world["ex"], now=1e9 + examples.MAX_AGE_S + 1) == 1  # "n" already went with the append of "m"
    assert world["ex"].read_text() == ""
    # learn prunes too, even when nothing is learnable
    examples.append_row({"text_sha": "z" * 16, "source": "redo", "label": 1.0, "ts": 5.0}, path=world["ex"])
    cl.apply(examples_path=world["ex"], now=5.0 + examples.MAX_AGE_S + 1)
    assert world["ex"].read_text() == ""


def test_replay_rows_without_text_sha_reject_the_file(world):
    add_examples(world, 10, 0, 1.0)
    lines = world["replay"].read_text().splitlines()
    row = json.loads(lines[0])
    del row["text_sha"]
    world["replay"].write_text("\n".join([json.dumps(row)] + lines[1:]) + "\n")
    v = run(world)
    assert v.status == cl.REJECTED and v.reason.startswith("replay_missing_text_sha") and "1 replay row" in v.reason
    assert not world["art"].with_suffix(".json.prev").exists()
    with pytest.raises(cl.ReplayError):
        cl.load_replay(world["replay"])


def test_purge_user_examples_restores_the_shipped_rows(world):
    add_examples(world, 10, 0, 1.0)
    add_examples(world, 10, 1, 0.0)
    shipped = ck.load_artifact().n
    assert run(world).status == cl.ACCEPTED
    ck.clear_cache()
    assert ck.load_artifact().n == shipped + 20
    v = cl.purge_user_examples()
    assert v.status == cl.ACCEPTED and v.added == -20
    ck.clear_cache()
    art = ck.load_artifact()
    assert art.n == shipped and "learning" not in art.provenance
    assert cl.purge_user_examples().status == cl.NOTHING_NEW
    assert learn.main(["--purge-user-examples"]) == 0


def test_two_concurrent_learn_runs_lose_nothing(world, tmp_path):
    import threading

    ex_a, ex_b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    for ex, center, label, tag in ((ex_a, 0, 1.0, "aa"), (ex_b, 1, 0.0, "bb")):
        for i in range(10):
            examples.append_row({"text_sha": f"{tag}{i:014d}", "source": "prefix", "label": label, "ts": 1e9 + i,
                                 "vec": examples.encode_vec(vec(center, world["rng"])), "model": MODEL}, path=ex)
    shipped = ck.load_artifact().n
    out, start = {}, threading.Barrier(2)

    def go(name, ex):
        start.wait()
        out[name] = cl.apply(examples_path=ex, now=1e9 + 100)

    threads = [threading.Thread(target=go, args=(n, e)) for n, e in (("a", ex_a), ("b", ex_b))]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert {v.status for v in out.values()} == {cl.ACCEPTED}, out
    ck.clear_cache()
    art = ck.load_artifact()
    applied = set(art.provenance["learning"]["applied_shas"])
    assert art.n == shipped + 20 and len(applied) == 20  # neither run's examples were dropped
    assert not [p for p in world["art"].parent.iterdir() if p.name.endswith(".tmp")]  # no stray temp files
    assert (world["art"].parent / "knn.json.prev").exists()  # the artifact from before the last update


def test_base_changed_mid_run_writes_nothing_and_keeps_the_other_runs_shas(world, monkeypatch):
    add_examples(world, 10, 0, 1.0)
    other = world["art"].read_bytes()
    real_gate = cl.gate

    def gate_then_other_run_wins(base, cand, replay, **kw):
        v = real_gate(base, cand, replay, **kw)
        # another run installs its own artifact between our read and our swap
        art2 = cl.build_candidate(base, [vec(0, world["rng"])], [1.0], ["theirs" + "0" * 10], now=1.0)
        ck.write_artifact(world["art"], [list(r) for r in art2.rows], list(art2.labels),
                          embedding_model=MODEL, k=art2.k, provenance=art2.provenance)
        return v

    monkeypatch.setattr(cl, "gate", gate_then_other_run_wins)
    v = run(world)
    assert v.status == cl.REJECTED and v.reason.startswith("base_changed_during_run")
    ck.clear_cache()
    assert ck.load_artifact().provenance["learning"]["applied_shas"] == ["theirs" + "0" * 10]
    assert not world["art"].with_suffix(".json.prev").exists()  # never overwrite .prev for a base we did not read
    assert other != world["art"].read_bytes()


def test_write_artifact_uses_a_unique_temp_name(tmp_path, monkeypatch):
    seen = []
    real = __import__("os").replace
    monkeypatch.setattr("os.replace", lambda a, b: (seen.append(str(a)), real(a, b))[1])
    for _ in range(2):
        ck.write_artifact(tmp_path / "k.json", [[1.0, 0.0]], [1.0], embedding_model=MODEL)
    assert len(set(seen)) == 2 and all(x != str(tmp_path / "k.json.tmp") for x in seen)


def test_purge_leaves_no_user_embedding_in_prev(world):
    add_examples(world, 10, 0, 1.0)
    assert run(world).status == cl.ACCEPTED
    art_path = ck.artifact_path()
    prev = art_path.with_suffix(art_path.suffix + ".prev")
    ck.clear_cache()
    shipped = ck.load_artifact().n - 10
    assert cl.purge_user_examples().status == cl.ACCEPTED
    assert prev.exists() and prev.read_bytes() == art_path.read_bytes()
    assert "learning" not in json.loads(prev.read_text()).get("provenance", {})
    ck.clear_cache()
    assert ck.load_artifact().n == shipped  # .prev is byte-equal to this purged artifact


def test_lock_times_out_with_a_clear_message_and_exit_2(world, capsys):
    import threading

    held, release = threading.Event(), threading.Event()

    def holder():
        with cl._lock(ck.artifact_path()):
            held.set()
            release.wait(10)

    t = threading.Thread(target=holder)
    t.start()
    assert held.wait(5)
    try:
        with pytest.raises(cl.LockTimeout):
            with cl._lock(ck.artifact_path(), timeout=0.2):
                pass
        cl.LOCK_TIMEOUT_S, old = 0.2, cl.LOCK_TIMEOUT_S
        try:
            assert learn.main(["--dry-run"]) == 2
        finally:
            cl.LOCK_TIMEOUT_S = old
        assert "holds" in capsys.readouterr().err
    finally:
        release.set()
        t.join(5)


def test_lock_degrades_without_fcntl(world, monkeypatch):
    import sys

    from llm_router import file_lock

    monkeypatch.setattr(file_lock, "_IS_WINDOWS", True)
    fake = type(sys)("msvcrt")
    fake.LK_NBLCK, fake.LK_UNLCK = 1, 0
    calls = []
    fake.locking = lambda fd, mode, n: calls.append(mode)
    monkeypatch.setattr(file_lock, "msvcrt", fake, raising=False)
    with cl._lock(ck.artifact_path()):
        pass
    assert calls == [1, 0]


def test_prune_does_not_drop_a_concurrent_append(tmp_path, monkeypatch):
    """Deterministic: a second thread appends in the middle of prune's rewrite (after the read,
    before the replace). With the lock the append waits and lands after the replace; with no
    lock it lands before the replace and is lost."""
    import threading

    path = tmp_path / "ex.jsonl"
    old = time.time() - examples.MAX_AGE_S - 1000
    path.write_text("".join(
        json.dumps({"text_sha": f"old{i:013d}", "source": "redo", "label": 1.0, "ts": old}) + "\n"
        for i in range(5)))
    fresh = {"text_sha": "fresh0000000000a", "source": "redo", "label": 1.0, "ts": time.time()}
    errs: list = []
    started = threading.Event()
    box: dict = {}
    real_rewrite = examples._rewrite

    def appender():
        started.set()
        try:
            examples.append_row(fresh, path=path)
        except Exception as exc:  # noqa: BLE001
            errs.append(exc)

    def rewrite_with_concurrent_append(target, lines):
        if "t" in box:  # the appender's own prune (only when unlocked) must not recurse
            return real_rewrite(target, lines)
        box["t"] = t = threading.Thread(target=appender)
        t.start()
        assert started.wait(2)
        t.join(0.5)  # unlocked: the append finishes here, before the replace
        box["finished_mid_rewrite"] = not t.is_alive()
        real_rewrite(target, lines)

    monkeypatch.setattr(examples, "_rewrite", rewrite_with_concurrent_append)
    assert examples.prune(path) == 5, "the check found nothing to check"
    box["t"].join(10)
    assert not errs
    assert fresh["text_sha"] in {r["text_sha"] for r in examples.read_examples(path)}
    assert box["finished_mid_rewrite"] is False  # the lock held the append back


@pytest.mark.timing
def test_hot_path_example_write_gives_up_fast_when_the_lock_is_held(tmp_path, monkeypatch):
    """The hook and the proxy/MCP loop write inline: a held lock must cost <= ~0.25 s and leave
    a failopen record, not a 5 s stall."""
    import threading

    from llm_router import failopen, file_lock, prefixes

    recorded = []
    monkeypatch.setattr(failopen, "record", lambda code, exc=None, **kw: recorded.append(code))
    path = tmp_path / "ex.jsonl"
    held, release = threading.Event(), threading.Event()

    def holder():
        with file_lock.exclusive_lock(path.with_name(path.name + ".lock"), 5.0) as got:
            assert got
            held.set()
            release.wait(10)

    t = threading.Thread(target=holder)
    t.start()
    assert held.wait(5)
    try:
        for door, embed in (("hook", False), ("proxy", True), ("mcp", True)):
            monkeypatch.setattr(examples, "embedding_wanted", lambda: False)
            t0 = time.monotonic()
            ok = prefixes.record_example("p", "local", "x", door, path=path, embed=embed)
            took = time.monotonic() - t0
            assert ok is False and took < 0.3, (door, took)
    finally:
        release.set()
        t.join(10)
    assert recorded == ["LR-FO-PREFIX-EXAMPLE"] * 3
    assert not path.exists() or path.read_text() == ""


def test_apply_locked_records_failopen_when_prune_is_busy(monkeypatch):
    from llm_router import classifier_learning as cl
    from llm_router import failopen

    recorded = []
    monkeypatch.setattr(failopen, "record", lambda code, exc=None, **kw: recorded.append(code))

    def busy(*a, **k):
        raise examples.LockBusy("held")

    monkeypatch.setattr(examples, "prune", busy)
    monkeypatch.setattr(cl.complexity_knn, "load_artifact", lambda: None)
    monkeypatch.setattr(cl, "_log", lambda *a, **k: None)
    v = cl._apply_locked(dry_run=True, replay=None, examples_path=None, now=1.0)
    assert v.status == cl.REJECTED and recorded == ["LR-FO-EXAMPLE"]


def test_embed_queue_is_bounded_and_drops_with_failopen(monkeypatch):
    import asyncio
    import threading

    from llm_router import failopen

    recorded = []
    monkeypatch.setattr(failopen, "record", lambda code, exc=None, **kw: recorded.append(code))
    monkeypatch.setattr(examples, "MAX_PENDING_EMBEDS", 3)
    gate_ = threading.Event()
    ran = []

    async def go():
        results = [examples.run_off_loop(lambda i=i: (gate_.wait(5), ran.append(i))) for i in range(6)]
        gate_.set()
        for _ in range(100):
            if examples._pending == 0:
                break
            await asyncio.sleep(0.02)
        return results

    results = asyncio.run(go())
    assert results == [True] * 3 + [False] * 3
    assert recorded == ["LR-FO-EXAMPLE-QUEUE"] * 3 and sorted(ran) == [0, 1, 2]
