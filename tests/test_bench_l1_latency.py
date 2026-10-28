"""UN-11: scripts/bench/l1_latency.py on an INVENTED fixture corpus (no real prompt text here)."""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bench" / "l1_latency.py"


def _corpus(tmp_path: Path, n: int = 12) -> Path:
    p = tmp_path / "fixture.jsonl"
    lines = ['{"i": "bad-row-no-text"}', ""]
    lines += [json.dumps({"i": i, "text": "invented prompt %d: explain item %d" % (i, i * 7)}) for i in range(n)]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _run(tmp_path, corpus, *extra, env_extra=None, expect_rc=0):
    import os
    env = dict(os.environ, **(env_extra or {}))
    out = tmp_path / "out.json"
    r = subprocess.run([sys.executable, str(SCRIPT), "--corpus", str(corpus), "--out", str(out), *extra],
                       capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == expect_rc, r.stderr[-800:]
    return json.loads(out.read_text()), r.stdout


def _stub(tmp_path: Path, body: str) -> dict:
    (tmp_path / "l1stub.py").write_text(textwrap.dedent(body))
    return {"PYTHONPATH": str(tmp_path)}


def test_real_engine_on_fixture_is_stable_and_shaped(tmp_path):
    corpus = _corpus(tmp_path)
    res, stdout = _run(tmp_path, corpus, "--n", "10", "--stab-n", "10", "--label", "test")
    assert res["n"] == 10 and res["rows_skipped"] == 1 and res["label"] == "test"
    assert res["corpus_sha256"] == __import__("hashlib").sha256(corpus.read_bytes()).hexdigest()
    assert {"p50_ms", "p95_ms", "p99_ms", "max_ms", "cold_first_ms", "warm"} <= set(res["latency"])
    st = res["stability"]
    assert st["runs"] == 2 and st["n"] == 10 and st["agree"] == 10 and st["disagreeing_ids"] == []
    assert len(set(st["hashseeds"])) == 2
    assert res["network"] == {"guarded": True, "attempts": 0}
    assert res["sha"] and res["window"]["date"] and "uncontended" in res["machine"]
    assert "invented prompt" not in stdout  # prompt text is never emitted


def test_positive_control_slow_stub_is_reported_over_the_bar(tmp_path):
    env = _stub(tmp_path, """
        import time
        from llm_router import engine
        def decide(text, door):
            time.sleep(0.06)
            return engine.decide(text, door=door)
    """)
    res, _ = _run(tmp_path, _corpus(tmp_path, 6), "--n", "6", "--engine-spec", "l1stub:decide", env_extra=env, expect_rc=1)
    assert res["ok"] is False  # SHOULD3: a breached bar fails ok and exits 1
    assert res["latency"]["p95_ms"] > 50 and res["latency"]["p95_within_bar"] is False


def test_negative_control_hashseed_dependent_stub_is_flagged_unstable(tmp_path):
    env = _stub(tmp_path, """
        import os
        def decide(text, door):
            return {"tier": os.environ["PYTHONHASHSEED"] if text.endswith("7") else "x"}
    """)
    res, _ = _run(tmp_path, _corpus(tmp_path, 12), "--n", "12", "--stab-n", "12", "--engine-spec", "l1stub:decide",
                  env_extra=env, expect_rc=1)
    st = res["stability"]
    assert st["disagreeing_ids"] == ["1", "11"] and st["agree"] == 10 and res["ok"] is False


def test_network_attempt_is_blocked_and_counted(tmp_path):
    env = _stub(tmp_path, """
        import socket
        def decide(text, door):
            try:
                socket.create_connection(("127.0.0.1", 9))
            except OSError:
                pass
            return {"tier": "x"}
    """)
    res, _ = _run(tmp_path, _corpus(tmp_path, 3), "--n", "3", "--engine-spec", "l1stub:decide", env_extra=env, expect_rc=1)
    assert res["network"]["attempts"] == 6 and res["ok"] is False  # 3 prompts x 2 runs


def test_empty_corpus_fails_loudly(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text('{"i": 1}\n')
    r = subprocess.run([sys.executable, str(SCRIPT), "--corpus", str(p)], capture_output=True, text=True)
    assert r.returncode != 0 and "no usable rows" in r.stderr


def test_fallback_layer_fails_ok_and_is_in_histogram(tmp_path):  # SHOULD2
    env = _stub(tmp_path, """
        from llm_router import engine
        def decide(text, door):
            import dataclasses
            return dataclasses.replace(engine.decide(text, door=door), layer="fallback")
    """)
    res, _ = _run(tmp_path, _corpus(tmp_path, 4), "--n", "4", "--engine-spec", "l1stub:decide", env_extra=env, expect_rc=1)
    assert res["layers"] == {"fallback": 8} and res["ok"] is False


def test_real_engine_layer_histogram_has_no_fallback(tmp_path):
    res, _ = _run(tmp_path, _corpus(tmp_path, 6), "--n", "6")
    assert sum(res["layers"].values()) == 12 and "fallback" not in res["layers"]
    assert res["ok"] is True and res["scope"] == "rules-only" and res["artifacts"] == {"lexical_sha256": None, "calibration_sha256": None}


def test_short_corpus_is_not_ok(tmp_path):  # SHOULD3: n < requested
    res, _ = _run(tmp_path, _corpus(tmp_path, 5), "--n", "50", expect_rc=1)
    assert res["n"] == 5 and res["n_ok"] is False and res["ok"] is False


def test_seeded_sample_is_default_and_recorded(tmp_path):  # SHOULD4
    c = _corpus(tmp_path, 30)
    a, _ = _run(tmp_path, c, "--n", "8", "--seed", "5")
    b, _ = _run(tmp_path, c, "--n", "8", "--seed", "5")
    f, _ = _run(tmp_path, c, "--n", "8", "--first-n")
    assert a["sampling"] == {"method": "seeded-random", "seed": 5} and a["n"] == 8
    assert f["sampling"] == {"method": "first-n", "seed": None}
    assert a["corpus_sha256"] == b["corpus_sha256"]


def test_text_like_ids_are_redacted_and_worker_error_is_type_only(tmp_path):  # SHOULD5
    p = tmp_path / "ids.jsonl"
    p.write_text(json.dumps({"i": "SECRET words here", "text": "invented one"}) + "\n"
                 + json.dumps({"i": "ok-1", "text": "invented two"}) + "\n")
    res, stdout = _run(tmp_path, p, "--n", "2", "--stab-n", "2", "--first-n")
    assert res["ids_redacted"] == 1 and "SECRET" not in stdout
    env = _stub(tmp_path, """
        def decide(text, door):
            raise ValueError("LEAKED " + text)
    """)
    import os
    r = subprocess.run([sys.executable, str(SCRIPT), "--corpus", str(p), "--n", "2", "--engine-spec", "l1stub:decide"],
                       capture_output=True, text=True, env=dict(os.environ, **env))
    assert r.returncode != 0 and "ValueError" in r.stderr and "LEAKED" not in r.stderr and "invented" not in r.stderr


def test_lexical_passthrough_records_sha_and_scope(tmp_path):  # SHOULD1
    import hashlib
    lex = tmp_path / "lex.json"
    lex.write_text("{}")
    out = tmp_path / "o.json"
    subprocess.run([sys.executable, str(SCRIPT), "--corpus", str(_corpus(tmp_path, 4)), "--n", "4", "--lexical", str(lex),
                    "--out", str(out)], capture_output=True, text=True)
    res = json.loads(out.read_text())
    assert res["scope"] == "rules+lexical" and res["artifacts"]["lexical_sha256"] == hashlib.sha256(b"{}").hexdigest()
