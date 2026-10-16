"""CLS-v16 build B0: scripts/cls_dev_run.py + scripts/cls_dev_lib.py (the DEV candidate runner).

Behaviour only (no source-text assertions). Fakes only: a fake embedder, an injected pure-Python trainer, a
fake Ollama HTTP server on 127.0.0.1 (records every request body), injected clocks and system readers, an
injected engine tier. No model call, no network, no download, no HF cache; HOME is a tmp dir. cls_eval (B1)
runs as a real subprocess through its documented argv.

Mutants: each MUTANTS entry patches ONE function; its invariant passes on the unmutated code and fails
under the mutant (a vacuous mutant would make the test fail).
"""
from __future__ import annotations

import datetime
import functools
import hashlib
import http.server
import json
import os
import random
import re
import sys
import threading
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import cls_dev_lib as D  # noqa: E402
import cls_dev_run as R  # noqa: E402
import cls_gen_run as G  # noqa: E402
import cls_pins as P  # noqa: E402
import cls_send_lib as S  # noqa: E402

UTC = datetime.timezone.utc
SENTINEL = "sk-" + "ant-" + "-".join(["Zq98Wv76"] * 5)  # engine-shaped fake key built from fragments
VOCAB = ["w%03d" % i for i in range(900)]
TRUTH_TOKEN = {"opus": "zzopus", "sonnet": "zzsonnet", "haiku": "zzhaiku"}
FAST_TRAINER = functools.partial(D.pure_python_trainer, iters=40)


@pytest.fixture(autouse=True)
def _scratch_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home / ".llm-router"))


# ---------------------------------------------------------------------------------------------
# Fixtures and builders
# ---------------------------------------------------------------------------------------------

class Env:
    def __init__(self, tmp: Path, mp):
        self.tmp, self.mp = tmp, mp
        self.cls = tmp / "cls"
        self.dev = self.cls / "dev"
        self.out = self.dev / "out"
        self.dev.mkdir(parents=True)
        (self.cls / "test-syn").mkdir()
        self.pins_static = self.dev / "PINS-STATIC.json"
        self.pins_static.write_text("{}")
        self.pins_run = self.dev / "pins_run.jsonl"
        self.selftest = self.dev / "selftest.json"
        ok, _ = S.self_test(self.selftest)
        assert ok
        self.verify_calls = []
        mp.setattr(R, "VERIFY_PINS", lambda *a, **k: self.verify_calls.append((a, k)) or {"ok": True})

    def base(self, *extra) -> list[str]:
        return ["--cls-root", str(self.cls), "--out-dir", str(self.out), "--pins-static", str(self.pins_static),
                "--pins-run", str(self.pins_run), "--selftest-result", str(self.selftest), *map(str, extra)]

    def run(self, cmd, *extra, deps=None) -> int:
        return R.main([cmd, *self.base(*extra)], deps=deps or quiet_deps())


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def quiet_readers(**over):
    vals = {"free_mem_pct": 80.0, "load1": 0.5, "foreign_models": [], "claude_p": 0, "on_ac": True, "throttled": False}
    vals.update(over)
    return {k: (lambda v=v: v) for k, v in vals.items()}


def local_clock(h=12, m=0, tz=None):
    tz = tz or datetime.timezone(datetime.timedelta(hours=2))
    return lambda: datetime.datetime(2026, 10, 20, h, m, tzinfo=tz)


def quiet_deps(**kw):
    base = {"readers": lambda ollama, own: quiet_readers(), "sleep": lambda s: None, "clock": local_clock(),
            "canonical": lambda t: t, "trainer": FAST_TRAINER, "engine_tier": lambda text: "sonnet"}
    base.update(kw)
    return R.Deps(**base)


def sentence(rng, n):
    return " ".join(rng.choice(VOCAB) for _ in range(n))


def draw_tier(rng):
    x = rng.random()
    return "opus" if x < 0.3 else ("haiku" if x < 0.45 else "sonnet")


SOURCES = ("ge3_tune", "ge3_ho", "e2_cal", "p17d_f")


def make_data(n_real=40, n_groups=20, n_syn=12, n_calls=4, seed=7, words=(8, 30, 80, 160)):
    rng = random.Random(seed)
    base = datetime.datetime(2026, 9, 1, tzinfo=UTC)
    real, syn, truth = [], [], {}
    for i in range(n_real):
        t = draw_tier(rng)
        tok = TRUTH_TOKEN[t] if rng.random() < 0.9 else "zzsonnet"
        iid = "r%04d" % i
        truth[iid] = t
        real.append({"item_id": iid, "text": sentence(rng, words[i % len(words)]) + " " + tok,
                     "session_id": "sess%03d" % (i % n_groups), "source_set": SOURCES[i % len(SOURCES)],
                     "timestamp": D.utc_str(base + datetime.timedelta(minutes=i)), "requested": "opus",
                     "digest": "ctx " + sentence(rng, 5)})
    for i in range(n_syn):
        t = draw_tier(rng)
        iid = "syn-dev-%04d" % i
        truth[iid] = t
        syn.append({"item_id": iid, "text": sentence(rng, 25) + " " + TRUTH_TOKEN[t], "digest": json.dumps({"files": ["a.py"]}),
                    "group": "call-%02d" % (i % n_calls), "generator": "gpt" if i % 3 else "opus",
                    "cell": {"task": "code", "band": "L2", "tool": "none", "project": "p", "difficulty": "med", "ack": False}})
    return real, syn, truth


def write_jsonl(path: Path, rows):
    S.write_private(path, "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    return path


def judge_label(rng, t):
    if rng.random() < 0.92:
        return t
    return rng.choice([x for x in D.TIERS if x != t])


def write_labels(env: Env, real, syn, truth, *, voided_real=(), voided_syn=(), seed=11):
    """Opus labels on real DEV come from a legacy file (text_sha, session_id, truth); everything else from
    cls_label_run-shaped run dirs."""
    rng = random.Random(seed)
    legacy = [{"text_sha": S.text_sha(it["text"]), "session_id": it["session_id"], "truth": judge_label(rng, truth[it["item_id"]])}
              for it in real]
    lp = write_jsonl(env.dev / "legacy_opus.jsonl", legacy)
    dirs = []
    for name, items, judges, voided in (("real_dev", real, ("gpt", "sonnet"), voided_real),
                                        ("dev_syn", syn, D.JUDGES, voided_syn)):
        d = env.dev / ("labels_" + name)
        d.mkdir()
        for j in judges:
            write_jsonl(d / ("labels_%s.jsonl" % j), [{"item_id": it["item_id"], "judge": j, "tier": judge_label(rng, truth[it["item_id"]]),
                                                       "batch": 1, "call_id": "c1", "model_id": "m", "utc": "x", "attempt": 1}
                                                      for it in items])
        st = {"set": name, "judges": {j: {"state": "voided" if j in voided else "done"} for j in judges},
              "voided": sorted(voided)}
        S.write_private(d / "status.json", json.dumps(st))
        dirs.append(d)
    return lp, dirs


def build_manifest(env: Env, real, syn, **kw) -> Path:
    ip = write_jsonl(env.dev / "items.jsonl", real)
    extra = ["--items", ip]
    if syn:
        extra += ["--syn-items", write_jsonl(env.dev / "syn_items.jsonl", syn)]
    for k, v in kw.items():
        extra += ["--" + k.replace("_", "-"), v]
    assert env.run("manifest", *extra) == 0
    return env.out / "dev_manifest.json"


def write_rules_tier(env: Env, man: dict, tier="sonnet") -> Path:
    rows = [{"schema": "rules_tier/1", "resolver_commit": "a" * 40, "produced_utc": "2026-10-16T00:00:00Z"}]
    for it in man["items"]:
        syn = it["kind"] == "syn"
        rows.append({"item_id": it["item_id"], "rules_tier": tier, "policy_id": "p", "resolver_sha": "b" * 40,
                     "resolved_utc": "2026-10-16T00:00:00Z", "record_utc": None if syn else "2026-09-01T00:00:00Z",
                     "haiku_live": syn})
    p = write_jsonl(env.dev / "rules_tier.jsonl", rows)
    P.append_run_entry(env.pins_run, "rules_tier", [p])
    return p


class FakeEmbedder:
    """Features from the truth token planted in the fake texts (signal) plus a hash-derived noise term."""

    info = {"fake": True}

    def __init__(self):
        self.seen = []

    def count_tokens(self, text):
        return len(text.split())

    def embed(self, texts):
        self.seen += list(texts)
        out = []
        for t in texts:
            noise = int(hashlib.sha256(t.encode()).hexdigest()[:4], 16) / 65535.0
            out.append([t.count("zzopus") + 0.01, t.count("zzhaiku") + 0.01, t.count("zzsonnet") + 0.01, noise])
        return out


def jaccard_pair(n_shared, n_extra, seed):
    """Two texts whose 5-shingle Jaccard is exactly n_shared / (n_shared + n_extra)."""
    rng = random.Random(seed)
    toks = ["u%d_%d" % (seed, i) for i in range(n_shared + 4)]
    rng.shuffle(toks)
    a = " ".join(toks)
    return a, a + " " + " ".join("x%d_%d" % (seed, i) for i in range(n_extra))


# ---------------------------------------------------------------------------------------------
# Fake Ollama on 127.0.0.1 (records every request body)
# ---------------------------------------------------------------------------------------------

class FakeOllama:
    def __init__(self, *, logprobs=True, ps_models=(), chat_content=None):
        self.logprobs, self.ps_models, self.chat_content = logprobs, list(ps_models), chat_content
        self.bodies, self.unloaded = [], []
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj):
                b = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                fake.bodies.append(("GET", self.path, ""))
                if self.path == "/api/version":
                    return self._send({"version": "0.99.0-fake"})
                if self.path == "/api/tags":
                    return self._send({"models": [{"name": "nomic-embed-text", "digest": "d" * 64}]})
                if self.path == "/api/ps":
                    return self._send({"models": [{"name": n, "size": 1 << 30} for n in fake.ps_models]})
                self._send({})

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
                fake.bodies.append(("POST", self.path, body))
                req = json.loads(body)
                if self.path == "/api/generate" and req.get("keep_alive") == 0:
                    fake.unloaded.append(req["model"])
                    fake.ps_models = [m for m in fake.ps_models if m != req["model"]]
                    return self._send({"done": True})
                if self.path == "/api/embed":
                    return self._send({"embeddings": FakeEmbedder().embed([x[len(R.EMBED_PREFIX):] for x in req["input"]])})
                if self.path == "/api/chat":
                    if req["model"] not in fake.ps_models:
                        fake.ps_models.append(req["model"])
                    content = fake.chat_content or '{"scope": 2, "tier": "opus"}'
                    out = {"message": {"role": "assistant", "content": content}}
                    if fake.logprobs and req.get("logprobs"):
                        toks = ['{"', 'scope', '": ', '2', ', "', 'tier', '": "', 'op', 'us', '"}']
                        top = [{"token": "op", "logprob": -0.2}, {"token": "son", "logprob": -1.9}, {"token": "ha", "logprob": -3.1}]
                        out["logprobs"] = [{"token": t, "logprob": -0.1, "top_logprobs": top if t == "op" else
                                            [{"token": t, "logprob": -0.1}]} for t in toks]
                    return self._send(out)
                self._send({})

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def ollama():
    servers = []

    def make(**kw):
        s = FakeOllama(**kw)
        servers.append(s)
        return s
    yield make
    for s in servers:
        s.close()


class ObjOllama:
    """In-process Ollama double for the loop/unload invariants."""

    def __init__(self, ps):
        self.models, self.unloaded = list(ps), []

    def unload(self, model):
        self.unloaded.append(model)
        self.models = [m for m in self.models if m != model]

    def ps(self):
        return [{"name": m} for m in self.models]


# ---------------------------------------------------------------------------------------------
# (a) manifest
# ---------------------------------------------------------------------------------------------

def test_manifest_counts_drops_dedup_secret_and_no_text(env, capsys):
    real, syn, _ = make_data(n_real=30, n_groups=10, n_syn=6)
    a, b = jaccard_pair(8, 2, 1)        # J = 0.80 -> dropped
    c, d = jaccard_pair(79, 21, 2)      # J = 0.79 -> kept
    t = datetime.datetime(2026, 9, 2, tzinfo=UTC)
    extra = [
        {"item_id": "dupA", "text": a, "session_id": "sx", "source_set": "ge3_ho", "timestamp": D.utc_str(t)},
        {"item_id": "dupB", "text": b, "session_id": "sx", "source_set": "ge3_ho", "timestamp": D.utc_str(t - datetime.timedelta(hours=1))},
        {"item_id": "nearC", "text": c, "session_id": "sy", "source_set": "ge3_ho", "timestamp": D.utc_str(t)},
        {"item_id": "nearD", "text": d, "session_id": "sy", "source_set": "ge3_ho", "timestamp": D.utc_str(t)},
        {"item_id": "atT0", "text": "after the window opened " + sentence(random.Random(3), 9), "timestamp": D.T0_DEFAULT},
        {"item_id": "late", "text": "after the cutoff " + sentence(random.Random(4), 9), "timestamp": "2026-10-09T00:00:00Z"},
        {"item_id": "secret", "text": "please use " + SENTINEL + " for the call", "session_id": "sz",
         "source_set": "e2_cal", "timestamp": D.utc_str(t)},
        {"item_id": "nogroup", "text": "a lonely prompt " + sentence(random.Random(5), 9), "source_set": "e2_cal",
         "batch": "b7", "timestamp": D.utc_str(t)},
    ]
    legacy = write_jsonl(env.dev / "legacy_opus.jsonl", [{"text_sha": S.text_sha(extra[6]["text"]), "session_id": "sz", "truth": "opus"}])
    man_p = build_manifest(env, real + extra, syn, legacy_labels="opus=%s" % legacy)
    out = capsys.readouterr()
    man = json.loads(man_p.read_text())
    ids = {x["item_id"] for x in man["items"]}
    c_ = man["counts"]
    assert {"dupB", "nearC", "nearD", "nogroup"} <= ids          # earliest of the 0.80 pair kept; 0.79 pair both kept
    assert not {"dupA", "atT0", "late", "secret"} & ids
    assert c_["n_dropped_dup"] == 1 and c_["n_after_T0_dropped"] == 1 and c_["n_after_cutoff_dropped"] == 1
    assert c_["n_excluded_secret"] == 1 and c_["excluded_secret_by_detector"]["engine"] == 1
    assert c_["excluded_secret_by_opus_tier"] == {"opus": 1}
    grp = {x["item_id"]: x["group"] for x in man["items"]}
    assert grp["nogroup"] == "unk:e2_cal:b7" and c_["n_unk_group"] == 1
    assert c_["n_real_dev"] == 30 + 4 and c_["n_syn_dev"] == 6
    assert man["guard_free"] is True and man["screening_only"] is True
    assert man["counts"]["validity_floor"]["status"] == "NOT_INFORMATIVE"   # n_real < 300
    # counts only: no item text anywhere in the outputs, stdout, stderr
    blob = out.out + out.err + "".join(p.read_text() for p in env.out.iterdir() if p.is_file())
    for it in real[:5] + extra:
        assert it["text"] not in blob
    assert SENTINEL not in blob
    assert all(oct(p.stat().st_mode & 0o777) == "0o600" for p in env.out.iterdir() if p.is_file())
    # the manifest, the length counts and the hashed shingles are registered as one PINS-RUN entry
    entry = P.read_run(env.pins_run)[0]
    assert entry["name"] == "dev_manifest" and {Path(f).name for f in entry["files"]} == {
        "dev_manifest.json", "length_counts.json", "shingles_real.json"}


def test_manifest_low_cluster_fidelity_boundary_and_floor(env):
    rng = random.Random(9)
    t = datetime.datetime(2026, 9, 3, tzinfo=UTC)

    def items(n, n_unk):
        return [{"item_id": "i%03d" % i, "text": sentence(rng, 12), "timestamp": D.utc_str(t + datetime.timedelta(minutes=i)),
                 "source_set": "ge3_ho", **({} if i < n_unk else {"session_id": "s%03d" % (i % 40)})} for i in range(n)]
    ctx = S.ScrubContext()
    t0, cut = D.parse_utc(D.T0_DEFAULT), D.parse_utc(D.CUTOFF_DEFAULT)
    m15, _ = D.build_manifest(items(100, 15), [], ctx=ctx, t0=t0, cutoff=cut)
    m16, _ = D.build_manifest(items(100, 16), [], ctx=ctx, t0=t0, cutoff=cut)
    assert m15["counts"]["unk_share"] == 0.15 and m15["counts"]["low_cluster_fidelity"] is False
    assert m16["counts"]["low_cluster_fidelity"] is True
    floor = D.validity_floor({"n_real_dev": 300, "largest_session_share": 0.2, "n_truth_opus": 40, "primary_missing_share": 0.05})
    assert floor["status"] == "OK"
    for bad in ({"n_real_dev": 299}, {"largest_session_share": 0.21}, {"n_truth_opus": 39}, {"primary_missing_share": 0.051}):
        f = D.validity_floor(dict({"n_real_dev": 300, "largest_session_share": 0.2, "n_truth_opus": 40,
                                   "primary_missing_share": 0.0}, **bad))
        assert f["status"] == "NOT_INFORMATIVE"


def test_manifest_secret_exclusion_by_each_detector(env):
    ctx = S.ScrubContext(denylist=frozenset({"zq-deny-77"}), env_values=frozenset({"envVal-0042x"}))
    shapes = {name: secret for name, _exp, secret, _ctl in S._shape_table() if _exp != "independent"}
    shapes["denylist_entry"] = "see zq-deny-77 here"
    shapes["env_value"] = "uses envVal-0042x as given"
    t = "2026-09-05T00:00:00Z"
    items = [{"item_id": "s_%s" % k, "text": "prompt %s with %s" % (k, v), "session_id": "g", "timestamp": t}
             for k, v in shapes.items()]
    man, _ = D.build_manifest(items, [], ctx=ctx, t0=D.parse_utc(D.T0_DEFAULT), cutoff=D.parse_utc(D.CUTOFF_DEFAULT))
    assert man["items"] == [] and man["counts"]["n_excluded_secret"] == len(items)
    det = man["counts"]["excluded_secret_by_detector"]
    assert {"engine", "scrubber", "prose", "denylist", "env_value", "entropy"} <= set(det)


def test_manifest_joins_existing_labels_for_the_label_runner(env):
    import cls_label_run as L
    real, _, truth = make_data(n_real=20, n_groups=10, n_syn=0)
    son = [{"text_sha": S.text_sha(it["text"]), "session_id": it["session_id"], "truth": truth[it["item_id"]]}
           for it in real[:8]]
    son.append({"text_sha": "f" * 64, "session_id": "nobody", "truth": "opus"})       # unmapped, counted
    sp = write_jsonl(env.dev / "legacy_sonnet.jsonl", son)
    man_p = build_manifest(env, real, [], legacy_labels="sonnet=%s" % sp)
    man = json.loads(man_p.read_text())
    assert man["counts"]["n_existing_labels"] == {"sonnet": 8} and man["counts"]["n_unmapped_labels"] == {"sonnet": 1}
    items = {it["item_id"]: it for it in real}
    need = L.select_ids("real_dev", "sonnet", items, L.manifest_truth(man_p))
    assert sorted(need) == sorted(it["item_id"] for it in real[8:])           # existing Sonnet labels are reused
    assert sorted(L.select_ids("real_dev", "gpt", items, L.manifest_truth(man_p))) == sorted(items)


def test_length_counts_are_the_generator_input(env):
    real, syn, _ = make_data(n_real=24, n_groups=8, n_syn=0)
    build_manifest(env, real, [])
    lc = json.loads((env.out / "length_counts.json").read_text())
    man = json.loads((env.out / "dev_manifest.json").read_text())
    assert sum(lc.values()) == man["counts"]["n_real_dev"]
    assert {b: n for b, n in lc.items() if n} == man["counts"]["per_length_band_n"]
    shares = G.load_length_shares(env.out / "length_counts.json")
    assert sum(shares.values()) == 100


# ---------------------------------------------------------------------------------------------
# (b) scrub self-test and staleness
# ---------------------------------------------------------------------------------------------

def test_scrub_check_all_shapes_and_exit_6_on_a_missed_shape(env, monkeypatch, capsys):
    res = env.dev / "st2.json"
    assert env.run("scrub-check", "--result", res) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["ok"] and all(rep["per_shape_ok"].values()) and rep["failed"] == []
    orig = S.scrub_detect
    monkeypatch.setattr(S, "scrub_detect", lambda text, ctx=None: [h for h in orig(text, ctx) if h != "prose"])
    assert env.run("scrub-check", "--result", res) == S.EXIT_SELFTEST


def test_outgoing_check_aborts_on_a_secret():
    with pytest.raises(S.ScrubAbort) as e:
        S.outgoing_check(("batch " + SENTINEL).encode(), batch_id="b1")
    assert SENTINEL not in str(e.value)


def test_selftest_absent_or_stale_refuses(env, monkeypatch):
    real, _, _ = make_data(n_real=10, n_groups=5, n_syn=0)
    build_manifest(env, real, [])
    env.selftest.unlink()
    assert env.run("folds", "--manifest", env.out / "dev_manifest.json") == S.EXIT_SELFTEST
    S.self_test(env.selftest)
    monkeypatch.setattr(S, "scrubber_sha", lambda: "0" * 64)
    assert env.run("folds", "--manifest", env.out / "dev_manifest.json") == S.EXIT_SELFTEST


# ---------------------------------------------------------------------------------------------
# (c) folds
# ---------------------------------------------------------------------------------------------

def test_folds_grouped_deterministic_balanced_and_kinds_never_merged(env, capsys):
    items = [{"item_id": "a%03d" % i, "kind": "real", "group": "g%02d" % (i // 3)} for i in range(60)]
    items += [{"item_id": "b%03d" % i, "kind": "syn", "group": "g%02d" % (i // 3)} for i in range(15)]  # same names, other kind
    f1, f2 = D.assign_folds(items), D.assign_folds(list(reversed(items)))
    assert f1 == f2
    by = {}
    for it in items:
        by.setdefault((it["kind"], it["group"]), set()).add(f1[it["item_id"]])
    assert all(len(v) == 1 for v in by.values())
    sizes = [sum(1 for v in f1.values() if v == k) for k in range(5)]
    assert max(sizes) - min(sizes) <= 3
    real, syn, _ = make_data(n_real=30, n_groups=10, n_syn=6)
    build_manifest(env, real, syn)
    assert env.run("folds", "--manifest", env.out / "dev_manifest.json") == 0
    rep = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rep["folds_match"] and rep["groups_spanning_folds"] == 0


# ---------------------------------------------------------------------------------------------
# (d) heads
# ---------------------------------------------------------------------------------------------

def _rows(n=60, groups=12, seed=3):
    rng = random.Random(seed)
    rows, X = [], []
    for i in range(n):
        t = draw_tier(rng)
        rows.append({"item_id": "i%03d" % i, "fold": (i % groups) % 5, "group": "g%02d" % (i % groups), "truth": t,
                     "w_pool": 1.0 if i % 4 else 0.5})
        X.append([float(t == "opus") + rng.random() * 0.3, float(t == "haiku") + rng.random() * 0.3])
    return rows, X


def test_oof_never_scores_a_row_with_a_model_that_saw_its_group(monkeypatch):
    rows, X = _rows()
    calls = []
    orig = D.fit_two_heads
    monkeypatch.setattr(D, "fit_two_heads", lambda tr, Xs, rs: calls.append({r["group"] for r in rs}) or orig(tr, Xs, rs))
    res = D.fit_candidate(FAST_TRAINER, X, rows)
    fold_groups = {f: {r["group"] for r in rows if r["fold"] == f} for f in range(5)}
    for f in range(5):
        assert calls[f].isdisjoint(fold_groups[f])
    assert len(res["oof"]) == len(rows)
    for r in res["oof"]:
        assert abs(r["p_haiku"] + r["p_sonnet"] + r["p_opus"] - 1.0) < 1e-9
        assert min(r["p_haiku"], r["p_sonnet"], r["p_opus"]) >= 0.0
    assert D.fit_candidate(FAST_TRAINER, X, rows)["oof"] == res["oof"]          # deterministic


def test_ordinal_assembly_monotone_and_clipped():
    p = D.assemble_probs(0.3, 0.9)               # P(>=opus) > P(>=sonnet) is forced down
    assert p["opus"] <= 0.3 + 1e-12 and p["sonnet"] >= 0 and abs(sum(p.values()) - 1) < 1e-12
    q = D.assemble_probs(1.0, 0.0)
    assert q["haiku"] == pytest.approx(0.001) and q["opus"] == pytest.approx(0.001)


def test_weights_real_keeps_half_the_loss_and_m_multiplier(monkeypatch):
    w_real, w_syn = D.pool_weights(100, 300)
    assert w_real * 100 / (w_real * 100 + w_syn * 300) >= 0.5
    seen = []
    rows, X = _rows()

    def rec(Xs, y, w, C):
        seen.append((list(y), list(w)))
        return FAST_TRAINER(Xs, y, w, C)
    D.fit_two_heads(rec, X, rows)
    y, w = seen[-1]  # last call = the opus head's final fit
    for r, yy, ww in zip(rows, y, w):
        assert ww == pytest.approx(r["w_pool"] * (2.0 if yy else 1.0))


def test_fit_heads_c2_via_fake_ollama_end_to_end(env, ollama, capsys):
    real, syn, truth = make_data(n_real=40, n_groups=20, n_syn=12)
    real[0]["digest"] = "d" * 1000
    lp, dirs = write_labels(env, real, syn, truth)
    build_manifest(env, real, syn, legacy_labels="opus=%s" % lp)
    srv = ollama()
    extra = ["--manifest", env.out / "dev_manifest.json", "--items", env.dev / "items.jsonl", "--syn-items",
             env.dev / "syn_items.jsonl", "--label-dir", dirs[0], "--label-dir", dirs[1],
             "--candidate", "C2", "--ollama-url", srv.url]
    assert env.run("fit-heads", *extra) == 0
    rep = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rep["n_oof"] == 52 and rep["truth_rules"] == {"real": "majority3", "dev_syn": "majority3"}
    embeds = [json.loads(b) for m, p, b in srv.bodies if p == "/api/embed"]
    assert embeds and all(x.startswith(R.EMBED_PREFIX) for x in embeds[0]["input"])
    first = [x for x in embeds[0]["input"] if "[ctx] ddd" in x][0]
    assert first.count("d" * 400) == 1 and "d" * 401 not in first          # digest capped at 400
    assert "nomic-embed-text" in srv.unloaded                              # own model unloaded at the end
    model = json.loads((env.out / "model_C2.json").read_text())
    assert model["embedder"]["model_digest"] == "d" * 64 and model["artifact_sha256"]


# ---------------------------------------------------------------------------------------------
# (e) C4 precheck / run
# ---------------------------------------------------------------------------------------------

def test_c4_precheck_ok_and_killed_and_c4_run_refuses(env, ollama, capsys):
    good = ollama(logprobs=True)
    assert env.run("c4-precheck", "--ollama-url", good.url) == 0
    assert json.loads((env.out / "c4_status.json").read_text())["c4"] == "ok"
    chat = [json.loads(b) for m, p, b in good.bodies if p == "/api/chat"][0]
    assert chat["think"] is False and chat["logprobs"] is True and chat["format"]  # the v6 request, thinking off
    assert good.unloaded == ["qwen3.5:latest"]
    bad = ollama(logprobs=False)
    assert env.run("c4-precheck", "--ollama-url", bad.url) == D.EXIT_C4_KILLED
    st = json.loads((env.out / "c4_status.json").read_text())
    assert st["c4"] == "killed" and st["criterion"] == 1 and st["reason"] == "no_logprobs" and st["ollama_version"]
    capsys.readouterr()
    real, syn, truth = make_data(n_real=10, n_groups=5, n_syn=0)
    build_manifest(env, real, [])
    assert env.run("c4-run", "--manifest", env.out / "dev_manifest.json", "--ollama-url", bad.url) == D.EXIT_C4_KILLED
    assert not [b for b in bad.bodies if b[1] == "/api/chat" and "rename" not in b[2]]   # no item was sent


def test_c4_features_never_fall_back_to_argmax():
    assert D.c4_features_from_response({"message": {"content": '{"tier": "opus"}'}}) is None
    assert D.c4_features_from_response({"logprobs": [{"token": "x", "logprob": float("nan")}]}) is None
    top = [{"token": "op", "logprob": -0.2}, {"token": "son", "logprob": -1.9}]
    resp = {"logprobs": [{"token": '{"tier": "', "logprob": -0.1, "top_logprobs": []}, {"token": "op", "logprob": -0.2, "top_logprobs": top}]}
    f = D.c4_features_from_response(resp)
    assert f is not None and abs(sum(pow(2.718281828459045, v) for v in f) - 1) < 1e-6 and f[2] > f[1] > f[0]


# ---------------------------------------------------------------------------------------------
# (f) ModernBERT gates
# ---------------------------------------------------------------------------------------------

def test_modernbert_gates_and_download_refusals(env, monkeypatch):
    real, _, _ = make_data(n_real=10, n_groups=5, n_syn=0)
    man = build_manifest(env, real, [])
    cache = env.cls / "hf"
    trig = env.dev / "trig.json"
    log = env.dev / "dl.jsonl"
    args = ["--manifest", man, "--trigger", trig, "--download-log", log, "--cls-cache", cache]
    assert env.run("fit-modernbert", *args) == D.EXIT_MB_GATE                    # no trigger file
    S.write_private(trig, json.dumps({"fire": True}))
    assert env.run("fit-modernbert", *args) == D.EXIT_MB_GATE                    # no download log
    S.write_private(log, json.dumps({"phase": "pre", "repo_id": D.MB_REPO, "revision": "a" * 40, "licence": "apache-2.0",
                                     "cache_dir": str(cache), "utc": "1"}) + "\n")
    assert env.run("fit-modernbert", *args) == D.EXIT_MB_GATE                    # no post phase
    sys_cache = Path(os.environ["HOME"]) / ".cache" / "huggingface"
    sys_cache.mkdir(parents=True)
    with pytest.raises(S.CLSExit) as e:
        D.check_mb_cache(sys_cache)
    assert e.value.code == D.EXIT_MB_GATE
    called = []
    monkeypatch.setattr(D, "hf_snapshot_download", lambda *a: called.append(a))
    monkeypatch.setattr(D, "hf_model_licence", lambda *a: called.append(a) or "apache-2.0")
    dl = env.dev / "dl2.jsonl"
    assert env.run("download-modernbert", "--download-log", dl, "--cls-cache", cache) == D.EXIT_MB_GATE
    assert env.run("download-modernbert", "--download-log", dl, "--cls-cache", cache, "--confirm-d129") == D.EXIT_MB_GATE
    assert env.run("download-modernbert", "--download-log", dl, "--cls-cache", cache, "--revision", "a" * 40) == D.EXIT_MB_GATE
    assert called == []
    assert env.run("download-modernbert", "--download-log", dl, "--cls-cache", cache, "--revision", "a" * 40, "--confirm-d129") == 0
    phases = [r["phase"] for r in S.read_jsonl(dl)]
    assert phases == ["pre", "post"] and called[0][0] == D.MB_REPO              # licence read before the download


def test_trigger_mb_boundary(env, capsys):
    sets = [
        ({"C1": {"survives_1_4": True, "auroc": 0.75}}, False),
        ({"C1": {"survives_1_4": True, "auroc": 0.7499}}, True),
        ({"C1": {"survives_1_4": False, "auroc": 0.9}, "C2": {"survives_1_4": False, "auroc": 0.8}}, True),
        ({"C2": {"survives_1_4": True, "auroc": 0.6}, "C4": {"survives_1_4": False, "auroc": 0.81}}, True),
    ]
    for cands, fire in sets:
        assert D.mb_trigger(cands)["fire"] is fire
    p = env.dev / "res.json"
    S.write_private(p, json.dumps({"candidates": sets[0][0]}))
    assert env.run("trigger-mb", "--results", p) == 0
    assert capsys.readouterr().out.strip() == "no-fire"


# ---------------------------------------------------------------------------------------------
# (g) quiet gate, (h) night window, unload
# ---------------------------------------------------------------------------------------------

def test_quiet_gate_cases():
    assert D.quiet_gate(quiet_readers(), sleep=lambda s: None)["ok"]
    cases = {"memory_below_40_percent": {"free_mem_pct": 39.9}, "load1_at_or_above_4": {"load1": 4.0},
             "load1_at_start_at_or_above_2": {"load1": 2.0}, "foreign_model_resident": {"foreign_models": ["other:7b"]},
             "claude_p_running": {"claude_p": 1}, "not_on_ac_power": {"on_ac": False}, "thermal_throttling": {"throttled": True}}
    for reason, over in cases.items():
        q = D.quiet_gate(quiet_readers(**over), sleep=lambda s: None)
        assert not q["ok"] and q["reason"] == reason and len(q["readings"]) == 3
    slept = []
    D.quiet_gate(quiet_readers(), sleep=slept.append)
    assert slept == [10.0, 10.0]
    assert D.evaluate_block([0.5, 3.0, 0.5]) == "invalid:load_sample"
    assert D.evaluate_block([0.5, 2.0, 2.0]) == "invalid:load_mean"
    assert D.evaluate_block([0.5, 1.0, 1.2]) is None


def test_count_claude_p_only_foreign_claude_print():
    ps = "\n".join(["101 codex codex exec -s read-only", "102 pytest pytest -q", "103 node node build.js",
                    "104 claude claude --resume", "105 claude claude -p --model x", "106 ruff ruff check"])
    assert D.count_claude_p(ps, own_pids=[105]) == 0
    assert D.count_claude_p(ps) == 1


def test_night_window_local_time_and_measured_duration():
    tz3 = datetime.timezone(datetime.timedelta(hours=3))
    at = lambda h, m=0, tz=tz3: datetime.datetime(2026, 10, 20, h, m, tzinfo=tz)  # noqa: E731
    assert D.can_start_unit(at(23, 55), None)[0] is False
    assert D.can_start_unit(at(6, 59), None)[0] is False
    assert D.can_start_unit(at(7, 0), None)[0] is True
    assert D.can_start_unit(at(23, 34), None)[0] is True       # 23:34 + 15 min < 23:50
    assert D.can_start_unit(at(23, 36), None)[0] is False
    assert D.can_start_unit(at(22, 30), 3600)[0] is False      # 22:30 + 1.5 h >= 23:50
    assert D.can_start_unit(at(2, 0), None)[0] is False        # 02:00 local is 23:00 UTC: still night


def test_run_units_checks_every_unit_resumes_and_unloads(tmp_path):
    clock_vals = iter([local_clock(22, 0)(), local_clock(23, 52)()])
    ol = ObjOllama(["qwen3.5:latest", "other:7b"])
    done = []
    st = tmp_path / "state.json"
    res = D.run_units([1, 2, 3], lambda i, u: done.append(u), clock=lambda: next(clock_vals), state_path=st, ollama=ol,
                      own_model="qwen3.5:latest", artifact_shas={"m": "1"})
    assert res["status"] == "parked" and res["done"] == 1 and done == [1] and ol.unloaded == ["qwen3.5:latest"]
    res2 = D.run_units([1, 2, 3], lambda i, u: done.append(u), clock=local_clock(9, 0), state_path=st, ollama=ol,
                       own_model="qwen3.5:latest", artifact_shas={"m": "1"})
    assert res2["status"] == "done" and done == [1, 2, 3]
    with pytest.raises(S.CLSExit) as e:
        D.run_units([1], lambda i, u: None, clock=local_clock(9, 0), state_path=st, ollama=None, own_model=None,
                    artifact_shas={"m": "2"})
    assert e.value.code == S.EXIT_PINS


def test_heavy_start_refused_at_2355_and_parks_on_foreign_model(env, ollama):
    srv = ollama(ps_models=["foreign:7b"])
    deps = quiet_deps(readers=lambda o, own: quiet_readers(foreign_models=[m["name"] for m in o.ps() if m["name"] != own]))
    assert env.run("c4-precheck", "--ollama-url", srv.url, deps=deps) == D.EXIT_PARK
    assert srv.unloaded == []                                  # never unloads a foreign model
    srv2 = ollama()
    assert env.run("c4-precheck", "--ollama-url", srv2.url, deps=quiet_deps(clock=local_clock(23, 55))) == D.EXIT_PARK
    log = S.read_jsonl(env.out / "cls_dev_log.jsonl")
    assert any(r.get("counts", {}).get("reason") == "would_cross_2350" for r in log)


# ---------------------------------------------------------------------------------------------
# (i) latency
# ---------------------------------------------------------------------------------------------

def test_latency_maths_spacing_and_codex_refused(env):
    s = D.latency_summary([[10.0] * 20, [12.0] * 20, [11.0] * 19 + [50.0]], [900.0] * 5, 900.0, B=200)
    assert s["n_calls"] == 60 and s["n_blocks"] == 3 and s["p50_ms"] == 11.0 and s["p99_ms"] == 50.0
    assert s["p95_ms"] == 12.0 and s["worst_block_p95_ms"] == 12.0 and s["n_warmups"] == 5
    assert s["p95_ci_ms"][0] <= s["p95_ms"] <= s["p95_ci_ms"][1]
    with pytest.raises(S.CLSExit):
        D.refuse_unmeasured_candidate("codex-gpt")
    real, _, _ = make_data(n_real=20, n_groups=10, n_syn=0)
    man = build_manifest(env, real, [])
    t = [0.0]

    def perf():
        t[0] += 0.004
        return t[0]
    now = [datetime.datetime(2026, 10, 20, 10, 0, tzinfo=UTC)]
    deps = quiet_deps(perf=perf, l2_score=lambda c, a: (lambda text, dig: {"haiku": 0.2, "sonnet": 0.5, "opus": 0.3}),
                      utc_clock=lambda: now[0], load_sampler=lambda: 0.5, rss_gib=lambda: 0.4)
    args = ["--manifest", man, "--items", env.dev / "items.jsonl", "--candidate", "C1", "--n", 8, "--warmups", 2]
    assert env.run("latency", *args, deps=deps) == 0
    assert env.run("latency", *args, deps=deps) == D.EXIT_PARK          # second block < 30 min later
    for k in (1, 2):
        now[0] = now[0] + datetime.timedelta(minutes=31)
        assert env.run("latency", *args, deps=deps) == 0
    assert env.run("latency", *args, "--summarize", deps=deps) == 0
    lat = json.loads((env.out / "latency_C1.json").read_text())
    assert lat["n_blocks"] == 3 and lat["n_calls"] == 24 and lat["n_warmups"] == 2
    assert env.run("latency", "--manifest", man, "--candidate", "codex", deps=deps) == S.EXIT_REFUSE


# ---------------------------------------------------------------------------------------------
# (k) append-only log, (l) network guard, custody, pins
# ---------------------------------------------------------------------------------------------

def test_log_is_append_only_and_a_crash_leaves_the_start_line(env, monkeypatch):
    real, _, _ = make_data(n_real=10, n_groups=5, n_syn=0)
    man = build_manifest(env, real, [])
    log = env.out / "cls_dev_log.jsonl"
    before = log.read_text()
    assert env.run("folds", "--manifest", man) == 0
    assert log.read_text().startswith(before)
    monkeypatch.setattr(D, "assign_folds", lambda items, k=5: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        env.run("folds", "--manifest", man)
    rows = S.read_jsonl(log)
    assert rows[-2]["event"] == "start" and rows[-1]["event"] == "end" and rows[-1]["counts"]["crashed"] == "RuntimeError"


def test_non_local_host_refused(env, ollama):
    real, _, _ = make_data(n_real=10, n_groups=5, n_syn=0)
    man = build_manifest(env, real, [])
    for url in ("http://example.com:11434", "http://10.0.0.5:11434", "https://127.0.0.1:11434", "http://u:p@127.0.0.1:1"):
        assert env.run("folds", "--manifest", man, "--ollama-url", url) == S.EXIT_REFUSE
        with pytest.raises(S.CLSExit):
            D.Ollama(url)


def test_test_syn_paths_refused_including_symlink_and_dotdot(env):
    real, _, _ = make_data(n_real=10, n_groups=5, n_syn=0)
    man = build_manifest(env, real, [])
    ts = env.cls / "test-syn" / "items.jsonl"
    ts.write_text("{}")
    link = env.dev / "innocent.jsonl"
    link.symlink_to(ts)
    dotdot = env.dev / ".." / "test-syn" / "items.jsonl"
    for p in (ts, link, dotdot):
        assert env.run("fit-heads", "--manifest", man, "--items", p, "--candidate", "C1") == S.EXIT_CUSTODY
    disc = env.cls / "discarded-20261016" / "x.jsonl"
    disc.parent.mkdir()
    disc.write_text("{}")
    assert env.run("fit-heads", "--manifest", man, "--items", disc, "--candidate", "C1") == S.EXIT_CUSTODY


def test_pins_verified_first_and_unpinned_manifest_refused(env, monkeypatch):
    real, _, _ = make_data(n_real=10, n_groups=5, n_syn=0)
    man = build_manifest(env, real, [])
    assert env.verify_calls
    monkeypatch.setattr(R, "VERIFY_PINS", P.verify_at_start)
    env.pins_static.unlink()
    assert env.run("folds", "--manifest", man) == S.EXIT_PINS        # the real verifier: PINS-STATIC absent
    env.pins_static.write_text("{}")
    monkeypatch.setattr(R, "VERIFY_PINS", lambda *a, **k: {"ok": True})
    man.write_text(man.read_text() + " ")                              # changed after its RUN entry
    assert env.run("folds", "--manifest", man) == S.EXIT_PINS


# ---------------------------------------------------------------------------------------------
# truth rules, rules_tier, reference arms
# ---------------------------------------------------------------------------------------------

def test_truth_rules_fallback_and_two_voids():
    r = D.resolve_truth_rules({"voided": []}, {"voided": []})
    assert r["rules"] == {"real": "majority3", "dev_syn": "majority3"} and not r["truth_fallback"]
    r = D.resolve_truth_rules({"voided": ["gpt"]}, {"voided": ["opus"]})
    assert r["rules"] == {"real": "gpt_excluded", "dev_syn": "other_two_higher:opus"}
    assert r["truth_fallback"] == {"real": "gpt", "dev_syn": "opus"}
    r = D.resolve_truth_rules({"voided": ["gpt", "sonnet"]}, None)
    assert r["not_informative"] == ["real"] and r["rules"]["real"] is None
    ce = D.load_cls_eval(SCRIPTS / "cls_eval.py")
    assert D.truth_tier(ce, "other_two_higher:opus", {"opus": "haiku", "gpt": "sonnet", "sonnet": "opus"}) == "opus"
    assert D.truth_tier(ce, "majority3", {"opus": "haiku", "gpt": "sonnet", "sonnet": "opus"}) == "sonnet"


def test_rules_tier_header_pin_and_lift(env, tmp_path):
    real, _, _ = make_data(n_real=10, n_groups=5, n_syn=0)
    man = json.loads(build_manifest(env, real, []).read_text())
    p = write_rules_tier(env, man, tier="haiku")
    header, rows = D.read_rules_tier(p, env.pins_run)
    eff, unl = D.rules_arms(rows, [x["item_id"] for x in man["items"]])
    assert set(eff.values()) == {"sonnet"} and set(unl.values()) == {"sonnet"}   # pre-10-09 items: never haiku
    rows2 = {"x": {"rules_tier": "haiku", "haiku_live": True}}
    assert D.rules_arms(rows2, ["x"]) == ({"x": "sonnet"}, {"x": "haiku"})
    nohead = tmp_path / "nh.jsonl"
    nohead.write_text(json.dumps({"item_id": "a", "rules_tier": "sonnet"}) + "\n")
    with pytest.raises(S.CLSExit) as e:
        D.read_rules_tier(nohead, env.pins_run)
    assert e.value.code == S.EXIT_PINS
    p.write_text(p.read_text() + "\n")
    with pytest.raises(S.CLSExit) as e:
        D.read_rules_tier(p, env.pins_run)
    assert e.value.code == S.EXIT_PINS


def test_reference_arms_kw1_engine_and_sentinel_never_sent(env, ollama):
    real, syn, truth = make_data(n_real=12, n_groups=6, n_syn=0)
    real.append({"item_id": "zsecret", "text": "deploy with " + SENTINEL, "session_id": "s1", "source_set": "ge3_ho",
                 "timestamp": "2026-09-09T00:00:00Z"})
    lp, dirs = write_labels(env, real[:-1], [], truth)
    man_p = build_manifest(env, real, [], legacy_labels="opus=%s" % lp)
    man = json.loads(man_p.read_text())
    rt = write_rules_tier(env, man)
    content = json.dumps({"scope": 2, "ambiguity": 2, "repo_knowledge": 2, "reasoning_depth": 4, "execution_load": 2, "risk": 2,
                          "needs_tools": False, "tier": "opus", "task_type": "code", "qa": False, "needs_repo_context": False,
                          "local_eligible": False, "reason": "x"})
    srv = ollama(chat_content=content)
    args = ["--manifest", man_p, "--items", env.dev / "items.jsonl", "--label-dir", dirs[0],
            "--rules-tier", rt, "--arm", "nimble", "--arm", "engine_alone", "--ollama-url", srv.url]
    env.mp.setattr(D, "verify_engine_pin", lambda doc, path: None)
    assert env.run("reference-arms", *args) == 0
    arms = json.loads((env.out / "reference_arms.json").read_text())["arms"]
    ids = [x["item_id"] for x in man["items"]]
    assert set(arms["nimble"].values()) == {"opus"} and set(arms["engine_alone"].values()) == {"sonnet"}
    assert set(arms["rules_eff"].values()) == {"sonnet"} and arms["random"] == D.arm_random(ids)
    assert srv.unloaded[-1] == "nimble:9b"
    sent = "".join(b for _m, _p, b in srv.bodies)
    assert SENTINEL not in sent and "zsecret" not in json.dumps(arms)


FAKE_RUBRIC = """
RUBRIC = "RUBRIC fake text: rate each item"
ITEM_TEMPLATE = "### ITEM id={id}\\n{context}\\n{prompt}\\n### END ITEM id={id}"
BATCH_TAIL = "ANSWER labels for all items"
SINGLE_TAIL = "ANSWER one label as json"
DIMS = ["scope", "risk"]
TIERS = ["haiku", "sonnet", "opus"]
PLANTED = [{"id": i, "expect": e, "context": "c", "prompt": "p"} for i, e in (
    ("cal-h1", "haiku"), ("cal-h2", "haiku"), ("cal-s1", "sonnet"), ("cal-s2", "sonnet"), ("cal-o1", "opus"), ("cal-o2", "opus"))]
"""

FAKE_GT500 = """
import importlib.util, json
RUBRIC_MD5 = "__MD5__"
def load_rubric(path):
    spec = importlib.util.spec_from_file_location("fake_rubric_b0", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
def prompt_view(text):
    return "VIEW[" + text + "]VIEW"
def extract_json(text):
    return json.loads(text.strip())
def valid_label(lab, rub):
    return lab.get("tier") in rub.TIERS and all(int(lab[k]) in range(1, 6) for k in rub.DIMS)
"""


def test_reference_arms_kb1_through_the_pinned_rubric_adapter(env, ollama):
    rub = env.dev / "rubric.py"
    rub.write_text(FAKE_RUBRIC)
    md5 = hashlib.md5(rub.read_bytes()).hexdigest()
    gt = env.dev / "gt500.py"
    gt.write_text(FAKE_GT500.replace("__MD5__", md5))
    env.mp.setattr(P, "RUBRIC_MD5", md5)
    env.mp.setattr(P, "GT500_SHA256", S.sha256_file(gt))
    env.pins_static.write_text(json.dumps({"params": {"rubric_py": str(rub), "gt500_py": str(gt)},
                                           "pins": {"rubric": {"md5": md5, "sha256": S.sha256_file(rub)},
                                                    "gt500": {"sha256": S.sha256_file(gt)}}}))
    real, _, truth = make_data(n_real=8, n_groups=4, n_syn=0)
    lp, dirs = write_labels(env, real, [], truth)
    man_p = build_manifest(env, real, [], legacy_labels="opus=%s" % lp)
    rt = write_rules_tier(env, json.loads(man_p.read_text()))
    srv = ollama(chat_content=json.dumps({"id": "L001", "scope": 2, "risk": 4, "tier": "opus"}))
    args = ["--manifest", man_p, "--items", env.dev / "items.jsonl", "--label-dir", dirs[0], "--rules-tier", rt,
            "--arm", "qwen3coder", "--ollama-url", srv.url]
    assert env.run("reference-arms", *args) == 0
    arms = json.loads((env.out / "reference_arms.json").read_text())["arms"]
    assert set(arms["qwen3coder"].values()) == {"opus"} and srv.unloaded[-1] == "qwen3-coder:30b"
    sent = [json.loads(b)["messages"][0]["content"] for _m, p, b in srv.bodies if p == "/api/chat"]
    assert len(sent) == 8 and all(c.startswith("RUBRIC fake text") and "VIEW[" in c and c.endswith("ANSWER one label as json")
                                  for c in sent)
    other = env.dev / "other_rubric.py"
    other.write_text(FAKE_RUBRIC)
    (env.out / "arm_part_qwen3coder.jsonl").unlink()
    assert env.run("reference-arms", *args, "--prompt-module", other) == S.EXIT_PINS   # not the pinned file


# ---------------------------------------------------------------------------------------------
# (j) assemble: the B1 table, criteria, finalists (cls_eval runs as a subprocess)
# ---------------------------------------------------------------------------------------------

def _reference_arms(man, truth, seed=5):
    rng = random.Random(seed)
    ids = [x["item_id"] for x in man["items"]]
    arms = {"always_haiku": D.arm_constant(ids, "haiku"), "always_sonnet": D.arm_constant(ids, "sonnet"),
            "always_opus": D.arm_constant(ids, "opus"), "random": D.arm_random(ids), "rules_eff": D.arm_constant(ids, "sonnet"),
            "rules_unlifted": D.arm_constant(ids, "sonnet"), "engine_alone": D.arm_constant(ids, "sonnet")}
    arms["oracle"] = {i: truth[i] for i in ids}
    arms["noised_oracle"] = D.arm_noised_oracle(ids, arms["oracle"])
    arms["qwen3coder"] = {i: truth[i] if rng.random() < 0.9 else "sonnet" for i in ids}
    arms["nimble"] = {i: "haiku" if rng.random() < 0.7 else truth[i] for i in ids}
    return {"arms": arms}


def _assemble_setup(env, n_real, n_groups, n_syn, n_calls, *, voided_real=(), voided_syn=()):
    real, syn, truth = make_data(n_real=n_real, n_groups=n_groups, n_syn=n_syn, n_calls=n_calls, words=(8, 20, 30))
    lp, dirs = write_labels(env, real, syn, truth, voided_real=voided_real, voided_syn=voided_syn)
    man_p = build_manifest(env, real, syn, legacy_labels="opus=%s" % lp)
    man = json.loads(man_p.read_text())
    ra = env.dev / "reference_arms.json"
    S.write_private(ra, json.dumps(_reference_arms(man, truth)))
    common = ["--manifest", man_p, "--items", env.dev / "items.jsonl", "--syn-items", env.dev / "syn_items.jsonl",
              "--label-dir", dirs[0], "--label-dir", dirs[1]]
    return man, common, ra


def test_assemble_end_to_end_table_criteria_and_finalist(env, capsys):
    man, common, ra = _assemble_setup(env, 320, 80, 40, 8)
    deps = quiet_deps(embedder=lambda c, a: FakeEmbedder())
    assert env.run("fit-heads", *common, "--candidate", "C1", deps=deps) == 0
    S.write_private(env.out / "latency_C1.json", json.dumps({"p95_ms": 40.0, "rss_gib": 0.6, "model_gib": 0.0, "n_calls": 600, "n_blocks": 3}))
    S.write_private(env.out / "stability_C1.json", json.dumps({"identical_share": 1.0, "n": 360}))
    capsys.readouterr()
    rc = env.run("assemble", *common, "--oof", "C1=%s" % (env.out / "oof_C1.jsonl"), "--reference-arms", ra,
                 "--boot-b", 100, deps=deps)
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    ce = D.load_cls_eval(SCRIPTS / "cls_eval.py")
    header, items, raws, sha = ce.load_table(env.out / "arms_table.jsonl")          # passes B1's record validation
    assert sha == out["arms_table_sha256"] == (env.out / "arms_table.jsonl.sha256").read_text().split()[0]
    assert all(D.find_text_key(r) is None for r in raws)
    assert {r["kind"] for r in raws} == {"real", "syn"} and all(r["fold"] in range(5) for r in raws)
    res = json.loads((env.out / "cls_dev_result.json").read_text())
    fin = json.loads((env.out / "cls_dev_finalists.json").read_text())
    rows = res["criteria"]["C1"]["rows"]
    assert [r["n"] for r in rows] == list(range(9)) and res["guard_free"] and res["screening_only"]
    assert res["validity_floor"]["status"] == "OK", res["validity_floor"]
    assert res["criteria"]["C1"]["killed_by"] is None, [(r["n"], r["status"]) for r in rows]
    assert [f["candidate"] for f in fin["finalists"]] == ["C1"] and fin["finalists"][0]["arm"] in ("A", "B")
    assert fin["finalists"][0]["thresholds"]["t_hi"] is not None
    for p in ("cls_dev_result.json", "cls_dev_finalists.json"):
        assert (env.out / (p + ".sha256")).read_text().split()[0] == S.sha256_file(env.out / p)
    blob = "".join(p.read_text() for p in env.out.iterdir() if p.is_file() and p.suffix in (".json", ".jsonl"))
    assert "zzopus" not in blob and "w001 " not in blob


def test_assemble_truth_fallback_labels_and_two_voids_exit_19(env, capsys):
    man, common, ra = _assemble_setup(env, 40, 20, 12, 4, voided_real=("gpt",))
    rc = env.run("assemble", *common, "--reference-arms", ra, "--boot-b", 100)
    assert rc == 0
    res = json.loads((env.out / "cls_dev_result.json").read_text())
    assert res["truth_fallback"] == {"real": "gpt"} and res["truth_rules"]["real"] == "gpt_excluded"
    assert "TRUTH_FALLBACK:gpt" in res["truth_fallback_labels"] and "ge3_tune=gpt_excluded" in res["truth_by_source"]
    assert res["screen"] == "NOT_INFORMATIVE"                                       # n_real < 300
    status = env.dev / "labels_dev_syn" / "status.json"
    st = json.loads(status.read_text())
    st["voided"] = ["opus", "sonnet"]
    S.write_private(status, json.dumps(st))
    assert env.run("assemble", *common, "--reference-arms", ra, "--boot-b", 100) == S.EXIT_TWO_VOIDS


def test_criteria_table_order_all_numbers_after_early_kill_and_flags():
    floor = {"status": "NOT_INFORMATIVE", "failed": ["n_real_dev_below_300"], "pending": []}
    rows = [D.criterion0(floor), D.criterion1({"p95_ms": 2000.0, "rss_gib": 1.0}), D.criterion2({"n": 100, "n_error": 9}),
            D.criterion3({"auroc": 0.6, "ci95": [0.5, 0.7]}),
            D.criterion4({"admissible": True, "meets_bars": True, "guard": {"passes": True}, "non_inf": {"ok": False}}),
            D.criterion5({"raw": 3.0, "raw_re": 2.0, "ratio": 1.5}),
            D.criterion6({"real": {"significantly_worse": True}, "syn": {"meets_criterion4_conditions": True}},
                         low_cluster=False, resemblance_failed=True),
            D.criterion7({"identical_share": 0.97}), D.criterion8(None, voided=["gpt"])]
    t = D.criteria_table(list(reversed(rows)))
    assert [r["n"] for r in t["rows"]] == list(range(9)) and t["killed_by"] == 0 and not t["eligible"]
    st = {r["n"]: r for r in t["rows"]}
    assert st[1]["p95_ms"] == 2000.0 and st[2]["error_rate"] == 0.09 and st[3]["auroc"] == 0.6
    assert st[4]["status"] == "kill" and st[5]["status"] == "report_only" and st[5]["flag"] == "COST_HIGH"
    assert st[6]["status"] == "report_only" and st[6]["would_kill"] is True
    assert st[8]["status"] == "not_evaluable" and st[7]["status"] == "kill"


def test_criterion8_reads_both_cls_eval_legs():
    conc = {"under": {"discordant": False}, "cost": {"discordant": False}}
    disc = {"under": {"discordant": True}, "cost": {"discordant": True}}
    half = {"under": {"discordant": True}, "cost": {"discordant": False}}
    assert D.criterion8(conc, voided=[], syn=conc)["status"] == "pass"
    assert D.criterion8(half, voided=[], syn=half)["status"] == "pass"        # 1 of 2 metrics is not "more than 1"
    assert D.criterion8(disc, voided=[], syn=conc)["status"] == "kill"
    assert D.criterion8(conc, voided=[], syn=disc)["status"] == "kill"        # DEV-SYN leg: T-gpt vs T-sonnet
    assert D.criterion8(disc, voided=["opus"], syn=conc)["status"] == "pass"  # real leg not evaluable
    r = D.criterion8(disc, voided=["gpt"], syn=disc, voided_syn=["sonnet"])
    assert r["status"] == "not_evaluable" and r["real_leg"]["voided"] == ["gpt"]


def test_finalists_diversity_rule_names_the_displaced():
    surv = {"C1": {"cost": 1.0, "under": 0.1, "under_re": 0.2}, "C2": {"cost": 1.1, "under": 0.1, "under_re": 0.2},
            "C4": {"cost": 1.3, "under": 0.1, "under_re": 0.2}, "C3": {"cost": 0.9, "under": 0.3, "under_re": 0.2}}
    sel = D.select_finalists(surv)
    assert sel["ranked"] == ["C1", "C2", "C4"] and sel["finalists"] == ["C1", "C4"]
    assert sel["displaced"]["candidate"] == "C2" and sel["displaced"]["rank"] == 2 and sel["displaced"]["replaced_by_rank"] == 3
    assert D.select_finalists(surv) == sel


def test_arm_choice_only_outside_engine_seen():
    rows = [{"strata": {"source": s}} for s in ("ge3_tune", "e2_cal", "ge3_ho", "dev_syn", "p17d_f")]
    assert [r["strata"]["source"] for r in D.arm_choice_items(rows)] == ["ge3_ho", "dev_syn", "p17d_f"]
    a = {"cost": 1.0, "opus_share": 0.2, "raw_cost": 2.0}
    assert D.choose_arm(a, dict(a)) == "A" and D.choose_arm(a, {"cost": 0.9, "opus_share": 0.3, "raw_cost": 2.5}) == "B"


# ---------------------------------------------------------------------------------------------
# MUTANTS: each patches ONE function; its invariant must then fail
# ---------------------------------------------------------------------------------------------

class Ctx:
    def __init__(self, env, mp):
        self.env, self.mp = env, mp


def inv_folds_group(c):
    items = [{"item_id": "a%03d" % i, "kind": "real", "group": "g%02d" % (i // 3)} for i in range(45)]
    f = D.assign_folds(items)
    for g in {it["group"] for it in items}:
        assert len({f[it["item_id"]] for it in items if it["group"] == g}) == 1


def inv_oof(c):
    rows, X = _rows(40, 10)
    calls = []
    orig = D.fit_two_heads
    c.mp.setattr(D, "fit_two_heads", lambda tr, Xs, rs: calls.append({r["fold"] for r in rs}) or orig(tr, Xs, rs))
    D.fit_candidate(FAST_TRAINER, X, rows)
    assert sum(1 for s in calls if len(s) == 4) == 5


def inv_dedup(c):
    a, b = jaccard_pair(8, 2, 1)
    items = [{"item_id": "x1", "text": a, "timestamp": "2026-09-01T00:00:00Z", "session_id": "s"},
             {"item_id": "x2", "text": b, "timestamp": "2026-09-01T01:00:00Z", "session_id": "s"}]
    m, _ = D.build_manifest(items, [], ctx=S.ScrubContext(), t0=D.parse_utc(D.T0_DEFAULT), cutoff=D.parse_utc(D.CUTOFF_DEFAULT))
    assert [x["item_id"] for x in m["items"]] == ["x1"] and m["counts"]["n_dropped_dup"] == 1


def inv_t0(c):
    items = [{"item_id": "t0", "text": "one two three", "timestamp": D.T0_DEFAULT},
             {"item_id": "late", "text": "four five six", "timestamp": "2026-10-10T00:00:00Z"}]
    m, _ = D.build_manifest(items, [], ctx=S.ScrubContext(), t0=D.parse_utc(D.T0_DEFAULT), cutoff=D.parse_utc(D.CUTOFF_DEFAULT))
    assert m["items"] == [] and m["counts"]["n_after_T0_dropped"] == 1 and m["counts"]["n_after_cutoff_dropped"] == 1


def inv_secret(c):
    items = [{"item_id": "s", "text": "use " + SENTINEL, "timestamp": "2026-09-01T00:00:00Z", "session_id": "g"}]
    m, _ = D.build_manifest(items, [], ctx=S.ScrubContext(), t0=D.parse_utc(D.T0_DEFAULT), cutoff=D.parse_utc(D.CUTOFF_DEFAULT))
    assert m["items"] == [] and m["counts"]["n_excluded_secret"] == 1


def _manifested(c, n=10):
    real, _, _ = make_data(n_real=n, n_groups=5, n_syn=0, seed=99)
    return build_manifest(c.env, real, [])


def inv_selftest(c):
    man = _manifested(c)
    c.env.selftest.unlink()
    assert c.env.run("folds", "--manifest", man) == S.EXIT_SELFTEST


def inv_c4_no_argmax(c):
    assert D.c4_features_from_response({"message": {"content": '{"tier": "opus"}'}, "logprobs": None}) is None


def inv_trigger(c):
    assert D.mb_trigger({"C1": {"survives_1_4": True, "auroc": 0.75}})["fire"] is False
    assert D.mb_trigger({"C1": {"survives_1_4": True, "auroc": 0.74}})["fire"] is True


def inv_quiet_foreign(c):
    assert not D.quiet_gate(quiet_readers(foreign_models=["x:7b"]), sleep=lambda s: None)["ok"]


def inv_night_local(c):
    tz3 = datetime.timezone(datetime.timedelta(hours=3))
    assert D.can_start_unit(datetime.datetime(2026, 10, 20, 23, 55, tzinfo=tz3), None)[0] is False
    assert D.can_start_unit(datetime.datetime(2026, 10, 20, 2, 0, tzinfo=tz3), None)[0] is False


def inv_night_per_unit(c):
    vals = iter([local_clock(22, 0)(), local_clock(23, 52)(), local_clock(23, 53)()])
    done = []
    res = D.run_units([1, 2, 3], lambda i, u: done.append(u), clock=lambda: next(vals), state_path=c.env.dev / "s.json",
                      ollama=None, own_model=None, artifact_shas={})
    assert res["status"] == "parked" and done == [1]


def inv_syn_weight(c):
    w_real, w_syn = D.pool_weights(100, 300)
    assert w_real * 100 / (w_real * 100 + w_syn * 300) >= 0.5


def inv_latency_warmups(c):
    s = D.latency_summary([[10.0] * 30], [1000.0] * 5, 1000.0, B=100)
    assert s["p99_ms"] == 10.0 and s["n_calls"] == 30


def inv_criteria_all_rows(c):
    t = D.criteria_table([D.criterion0({"status": "NOT_INFORMATIVE", "failed": ["x"], "pending": []}),
                          D.criterion1({"p95_ms": 10.0}), D.criterion2({"n": 10, "n_error": 0}),
                          D.criterion3({"auroc": 0.9, "ci95": [0.8, 0.95]}),
                          D.criterion4({"admissible": True, "meets_bars": True, "guard": {"passes": True}, "non_inf": {"ok": True}}),
                          D.criterion5({"ratio": 1.0}), D.criterion6({}, low_cluster=False, resemblance_failed=False),
                          D.criterion7({"identical_share": 1.0}), D.criterion8(None, voided=["gpt"])])
    assert [r["n"] for r in t["rows"]] == list(range(9)) and t["rows"][3]["auroc"] == 0.9


def inv_random_seed(c):
    ids = ["i%02d" % i for i in range(40)]
    rng = random.Random(20261017)
    assert D.arm_random(ids) == {i: rng.choice(D.TIERS) for i in ids}


def inv_local_only(c):
    for url in ("http://example.com:11434", "http://192.168.1.4:11434"):
        with pytest.raises(S.CLSExit):
            D.check_local_url(url)


def inv_log_append(c):
    p = c.env.dev / "log.jsonl"
    D.append_log(p, {"a": 1})
    D.append_log(p, {"b": 2})
    assert [json.loads(x) for x in p.read_text().splitlines()] == [{"a": 1}, {"b": 2}]


def inv_test_syn(c):
    ts = c.env.cls / "test-syn" / "f.jsonl"
    ts.write_text("x")
    link = c.env.dev / "l.jsonl"
    link.symlink_to(ts)
    for p in (ts, link, c.env.dev / ".." / "test-syn" / "f.jsonl"):
        with pytest.raises(S.CLSExit) as e:
            D.guard_path(p, c.env.cls)
        assert e.value.code == S.EXIT_CUSTODY


def inv_night_measured(c):
    assert D.can_start_unit(local_clock(22, 30)(), 3600.0)[0] is False


def inv_claude_p(c):
    ps = "1 codex codex exec\n2 pytest pytest\n3 node node x\n4 ruff ruff check\n5 claude claude --resume"
    assert D.count_claude_p(ps) == 0


def inv_crit5(c):
    rows = [D.criterion0({"status": "OK", "failed": [], "pending": []}), D.criterion1({"p95_ms": 10.0}),
            D.criterion2({"n": 10, "n_error": 0}), D.criterion3({"auroc": 0.9, "ci95": [0.8, 0.95]}),
            D.criterion4({"admissible": True, "meets_bars": True, "guard": {"passes": True}, "non_inf": {"ok": True}}),
            D.criterion5({"raw": 4.0, "raw_re": 2.0, "ratio": 2.0}), D.criterion6({}, low_cluster=False, resemblance_failed=False),
            D.criterion7({"identical_share": 1.0}), D.criterion8({"under": {"discordant": False}, "cost": {"discordant": False}}, voided=[])]
    t = D.criteria_table(rows)
    assert t["killed_by"] is None and t["eligible"] and t["rows"][5]["flag"] == "COST_HIGH"


def inv_crit4(c):
    ok = {"admissible": True, "meets_bars": True, "guard": {"passes": True}, "non_inf": {"ok": True}}
    assert D.criterion4(ok)["status"] == "pass"
    assert D.criterion4(dict(ok, guard={"passes": False}))["status"] == "kill"
    assert D.criterion4(dict(ok, non_inf={"ok": False}))["status"] == "kill"
    assert D.criterion4(dict(ok, admissible=False))["status"] == "kill"


def inv_arm_choice(c):
    rows = [{"strata": {"source": s}} for s in ("ge3_tune", "e2_cal", "dev_syn", "ge3_ho")]
    assert {r["strata"]["source"] for r in D.arm_choice_items(rows)} == {"dev_syn", "ge3_ho"}


def inv_pins(c):
    man = _manifested(c)

    def deny(*a, **k):
        raise S.CLSExit(S.EXIT_PINS, "pins mismatch: scripts")
    c.mp.setattr(R, "VERIFY_PINS", deny)
    assert c.env.run("folds", "--manifest", man) == S.EXIT_PINS


def inv_m_multiplier(c):
    seen = []
    rows, X = _rows(30, 10)
    D.fit_two_heads(lambda Xs, y, w, C: seen.append((y, w)) or FAST_TRAINER(Xs, y, w, C), X, rows)
    y, w = seen[-1]
    assert any(yy for yy in y) and all(ww == pytest.approx(r["w_pool"] * (2.0 if yy else 1.0)) for r, yy, ww in zip(rows, y, w))


def inv_length_counts(c):
    man = json.loads(_manifested(c, 12).read_text())
    lc = json.loads((c.env.out / "length_counts.json").read_text())
    assert sorted(lc) == list(D.BANDS) and all(isinstance(v, int) for v in lc.values())
    assert sum(lc.values()) == man["counts"]["n_real_dev"]
    G.load_length_shares(c.env.out / "length_counts.json")


def inv_voided_rule(c):
    r = D.resolve_truth_rules({"voided": ["gpt"]}, {"voided": ["sonnet"]})
    assert r["rules"] == {"real": "gpt_excluded", "dev_syn": "other_two_higher:sonnet"} and r["truth_fallback"]


def inv_crit6_resemblance(c):
    m = {"real": {"significantly_worse": True}, "syn": {"meets_criterion4_conditions": True}}
    assert D.criterion6(m, low_cluster=False, resemblance_failed=False)["status"] == "kill"
    assert D.criterion6(m, low_cluster=False, resemblance_failed=True)["status"] == "report_only"


def inv_digest_cap(c):
    x = D.model_input("q" * 1000, "the prompt", max_tokens=10 ** 6)
    assert x.count("q") == 400


def inv_run_pin(c):
    man = _manifested(c)
    other = c.env.dev / "copy_manifest.json"
    other.write_text(man.read_text())
    assert c.env.run("folds", "--manifest", other) == S.EXIT_PINS


def inv_two_voids(c):
    r = D.resolve_truth_rules({"voided": ["gpt", "opus"]}, {"voided": []})
    assert r["not_informative"] == ["real"] and r["rules"]["real"] is None and "real" not in r["truth_fallback"]


def inv_unload_on_exception(c):
    ol = ObjOllama(["qwen3.5:latest"])

    def boom(i, u):
        raise RuntimeError("x")
    with pytest.raises(RuntimeError):
        D.run_units([1], boom, clock=local_clock(9), state_path=c.env.dev / "u.json", ollama=ol, own_model="qwen3.5:latest",
                    artifact_shas={})
    assert ol.unloaded == ["qwen3.5:latest"]


def inv_foreign_not_unloaded(c):
    ol = ObjOllama(["qwen3.5:latest", "foreign:7b"])
    assert D.unload_own(ol, "qwen3.5:latest") is True
    assert ol.unloaded == ["qwen3.5:latest"]


# mutant implementations ------------------------------------------------------------------------

def _m_run_units_start_only(units, do_unit, *, clock, state_path, ollama, own_model, artifact_shas, quiet=None, perf=None):
    ok, why = D.can_start_unit(clock(), None)
    if not ok:
        return {"status": "parked", "reason": why, "done": 0}
    for i, u in enumerate(units):
        do_unit(i, u)
    if ollama is not None and own_model:
        D.unload_own(ollama, own_model)
    return {"status": "done", "reason": None, "done": len(units)}


def _m_run_units_no_finally(units, do_unit, *, clock, state_path, ollama, own_model, artifact_shas, quiet=None, perf=None):
    for i, u in enumerate(units):
        ok, why = D.can_start_unit(clock(), None)
        if not ok:
            break
        do_unit(i, u)
    if ollama is not None and own_model:
        D.unload_own(ollama, own_model)
    return {"status": "done", "reason": None, "done": len(units)}


def _m_unload_all(ollama, own_model):
    for m in list(ollama.ps()):
        ollama.unload(m["name"])
    return True


def _m_oof_in_fold(trainer, X, rows, fold):
    te = [i for i, r in enumerate(rows) if r["fold"] == fold]
    fit = D.fit_two_heads(trainer, X, rows)
    return list(zip(te, D.predict_with(fit, [X[i] for i in te])))


def _m_can_start_fixed10(now, longest_unit_s):
    wall = now.replace(tzinfo=None)
    if wall.hour < 7:
        return False, "night_window"
    if wall + datetime.timedelta(minutes=10) >= wall.replace(hour=23, minute=50, second=0, microsecond=0):
        return False, "would_cross_2350"
    return True, None


def _m_c4_argmax(resp):
    content = (resp.get("message") or {}).get("content") or ""
    m = re.search(r'"tier"\s*:\s*"(haiku|sonnet|opus)"', content)
    t = m.group(1) if m else None
    return [0.0 if x == t else -30.0 for x in D.TIERS] if t else None


def _m_criteria_stop(rows):
    out = []
    for r in sorted(rows, key=lambda r: r["n"]):
        out.append(r)
        if r["status"] == "kill":
            break
    return {"rows": out, "killed_by": out[-1]["n"] if out and out[-1]["status"] == "kill" else None,
            "eligible": False, "pending": []}


def _m_crit5_gates(m):
    r = D._row(5, "kill" if (m.get("ratio") or 0) > 1.35 else "pass", ratio=m.get("ratio"))
    r["flag"] = "COST_HIGH" if (m.get("ratio") or 0) > 1.35 else None
    return r


def _m_crit6_ignores_resemblance(m, *, low_cluster, resemblance_failed):
    would = bool((m.get("real") or {}).get("significantly_worse")) and bool((m.get("syn") or {}).get("meets_criterion4_conditions"))
    return D._row(6, "report_only" if low_cluster else ("kill" if would else "pass"), would_kill=would)


def _m_rules_ignore_voids(sr, ss):
    return {"rules": {"real": "majority3", "dev_syn": "majority3"}, "truth_fallback": {}, "voided": {"real": [], "dev_syn": []},
            "not_informative": []}


def _m_rules_two_void_fallback(sr, ss):
    out = {"rules": {}, "truth_fallback": {}, "voided": {}, "not_informative": []}
    for key, st in (("real", sr), ("dev_syn", ss)):
        v = sorted((st or {}).get("voided") or [])
        out["voided"][key] = v
        if v:
            out["rules"][key] = "gpt_excluded" if v[0] == "gpt" else "other_two_higher:%s" % v[0]
            out["truth_fallback"][key] = v[0]
        else:
            out["rules"][key] = "majority3"
    return out


def _m_append_truncate(path, record):
    Path(path).write_text(json.dumps(record) + "\n")


MUTANTS = [
    ("01_fold_ignores_groups", D, "assign_folds", lambda items, k=5: {it["item_id"]: i % k for i, it in enumerate(items)}, inv_folds_group),
    ("02_oof_in_fold_scores", D, "oof_fold", _m_oof_in_fold, inv_oof),
    ("03_dedup_threshold_0_9", D, "is_near_dup", lambda a, b: S.jaccard(a, b) >= 0.9, inv_dedup),
    ("04_t0_filter_drops_nothing", D, "time_filter", lambda ts, t0, cutoff: None, inv_t0),
    ("05_secret_item_kept", D, "secret_hits", lambda text, digest, ctx: [], inv_secret),
    ("06_scrub_selftest_skipped", R, "require_selftest", lambda a: None, inv_selftest),
    ("07_c4_argmax_fallback", D, "c4_features_from_response", _m_c4_argmax, inv_c4_no_argmax),
    ("08_trigger_fires_always", D, "mb_trigger", lambda results: {"fire": True, "reasons": ["x"]}, inv_trigger),
    ("09_quiet_ignores_foreign_model", D, "check_foreign_model", lambda r: True, inv_quiet_foreign),
    ("10_night_window_uses_utc", D, "to_local", lambda now: now.astimezone(UTC).replace(tzinfo=None), inv_night_local),
    ("11_night_check_only_at_start", D, "run_units", _m_run_units_start_only, inv_night_per_unit),
    ("12_syn_weight_always_1", D, "pool_weights", lambda n_real, n_syn: (1.0, 1.0), inv_syn_weight),
    ("13_latency_includes_warmups", D, "latency_summary",
     lambda blocks, warmups, cold, B=2000: {"n_calls": sum(map(len, blocks)) + len(warmups),
                                            "p99_ms": D.percentile([v for b in blocks for v in b] + list(warmups), 99)}, inv_latency_warmups),
    ("14_criteria_stop_at_first_kill", D, "criteria_table", _m_criteria_stop, inv_criteria_all_rows),
    ("15_random_seed_ignored", D, "arm_random", lambda ids, seed=0: {i: random.Random().choice(D.TIERS) for i in ids}, inv_random_seed),
    ("16_non_local_host_allowed", D, "check_local_url", lambda url: url, inv_local_only),
    ("17_log_truncated_on_start", D, "append_log", _m_append_truncate, inv_log_append),
    ("18_test_syn_path_allowed", D, "guard_path", lambda path, cls_root, test_syn_root=None: Path(path), inv_test_syn),
    ("19_night_cutoff_fixed_10_min", D, "can_start_unit", _m_can_start_fixed10, inv_night_measured),
    ("20_quiet_counts_non_claude", D, "count_claude_p", lambda ps_text, own_pids=(): len(ps_text.splitlines()), inv_claude_p),
    ("21_criterion5_gates", D, "criterion5", _m_crit5_gates, inv_crit5),
    ("22_criterion4_point_estimates_only", D, "criterion4",
     lambda m: D._row(4, "pass" if m.get("meets_bars") else "kill"), inv_crit4),
    ("23_arm_choice_uses_engine_seen", D, "arm_choice_items", lambda rows: list(rows), inv_arm_choice),
    ("24_pins_verify_skipped", R, "verify_pins", lambda a, ollama=False: {"ok": True}, inv_pins),
    ("25_sample_weight_m_dropped", D, "head_sample_weight", lambda w_pool, truth, head_rank: w_pool, inv_m_multiplier),
    ("26_length_shares_not_counts", D, "length_counts_from_items",
     lambda items: {b: round(100.0 * sum(1 for x in items if x["band"] == b) / max(1, len(items)), 1) for b in D.BANDS}, inv_length_counts),
    ("27_voided_judge_keeps_majority3", D, "resolve_truth_rules", _m_rules_ignore_voids, inv_voided_rule),
    ("28_criterion6_weights_resemblance_failed", D, "criterion6", _m_crit6_ignores_resemblance, inv_crit6_resemblance),
    ("29_digest_not_capped", D, "cap_digest", lambda d: d or "", inv_digest_cap),
    ("30_file_used_without_run_entry", D, "require_run_pin", lambda path, run_path: None, inv_run_pin),
    ("31_two_void_set_scored_with_fallback", D, "resolve_truth_rules", _m_rules_two_void_fallback, inv_two_voids),
    ("32_unload_skipped_on_exception", D, "run_units", _m_run_units_no_finally, inv_unload_on_exception),
    ("33_foreign_model_unloaded", D, "unload_own", _m_unload_all, inv_foreign_not_unloaded),
]


def test_mutant_list_is_complete():
    assert len(MUTANTS) == 33 and len({m[0] for m in MUTANTS}) == 33


@pytest.mark.parametrize("name,mod,attr,mutant,invariant", MUTANTS, ids=[m[0] for m in MUTANTS])
def test_mutant_killed(name, mod, attr, mutant, invariant, tmp_path, monkeypatch):
    invariant(Ctx(Env(tmp_path / "unmutated", monkeypatch), monkeypatch))     # unmutated: the invariant holds
    env2 = Env(tmp_path / "mutated", monkeypatch)
    monkeypatch.setattr(mod, attr, mutant)
    try:
        invariant(Ctx(env2, monkeypatch))
    except (AssertionError, pytest.fail.Exception):
        return
    pytest.fail("mutant %s survived its invariant" % name)
