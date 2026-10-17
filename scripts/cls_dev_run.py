#!/usr/bin/env python3
"""CLS-v16 B0: the DEV candidate trainer/runner for PREREG-CLS-DEV (sections 1.3, 2, 3.2, 6-10).

    cls_dev_run.py manifest | scrub-check | folds | fit-heads | c4-precheck | c4-run | trigger-mb
                   | fit-modernbert | download-modernbert | reference-arms | latency | stability | assemble

DEV-gated: runs nothing until PREREG-CLS-DEV is frozen; no model call or download happens in tests.
Counts, ids, tiers and hashes only on stdout and in every output file; item text lives only in the 0600 input
items files. Every subcommand: rejects TEST-SYN and discarded paths (exit 10), verifies the pins first
(cls_pins.verify_at_start, exit 9), needs a fresh scrub self-test (exit 6; scrub-check excepted), reads the
manifest / rules_tier only through a matching PINS-RUN entry (exit 9), and appends a start line and an end
line to the append-only cls_dev_log.jsonl. Outputs are guard-free and screening-only (they say so).
Network: 127.0.0.1 only (Ollama). Never reads ~/.codex, never writes ~/.llm-router or ~/.claude.

Exit: 0 done, 3 parked (quiet gate / night window), 5 refused input, 6 scrub self-test, 7 C4 killed (no
logprobs), 8 ModernBERT gate, 9 pins, 10 TEST-SYN custody, 19 a set with two voided judges.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import cls_dev_lib as D  # noqa: E402
import cls_pins as P  # noqa: E402
import cls_send_lib as S  # noqa: E402

VERIFY_PINS = P.verify_at_start  # every subcommand calls it first (tests inject a stub)
OLLAMA_MODELS = {"C2": "nomic-embed-text", "C4": "qwen3.5:latest", D.ARM_KW1: "nimble:9b", D.ARM_KB1: "qwen3-coder:30b"}
EMBED_PREFIX = "classification: "
UNIT_N = 25
CLS_EVAL_TIMEOUT_S = 6 * 3600
PATH_ARGS = ("items", "syn_items", "manifest", "reference_arms", "rules_tier", "prompt_module", "results",
             "trigger", "download_log", "cls_cache", "run_a", "run_b", "fit", "resemblance_label", "model_artifact",
             "result", "selftest_result", "denylist", "env_file", "out_dir", "pins_run", "pins_static")


class Deps:
    """Everything a test replaces: embedder, trainer, clocks, system readers, the engine tier and the L2
    scorer used by latency. The defaults are the real ones (heavy imports stay lazy)."""

    def __init__(self, **kw):
        self.embedder = kw.get("embedder")        # (cand, a) -> object with embed(texts), count_tokens(text), info
        self.trainer = kw.get("trainer") or D.sklearn_trainer
        self.clock = kw.get("clock") or D.local_now
        self.utc_clock = kw.get("utc_clock") or (lambda: D.datetime.datetime.now(D.datetime.timezone.utc))
        self.readers = kw.get("readers")          # (ollama, own_model) -> readers dict
        self.sleep = kw.get("sleep") or time.sleep
        self.perf = kw.get("perf") or time.perf_counter
        self.canonical = kw.get("canonical") or D.canonical_text_default
        self.l2_score = kw.get("l2_score")        # (cand, a) -> callable(text, digest) -> probs
        self.rss_gib = kw.get("rss_gib") or D.peak_rss_gib
        self.monotonic = kw.get("monotonic") or time.monotonic  # the 60 s in-block sampling schedule
        self.engine_tier = kw.get("engine_tier") or D.engine_alone_tier
        self.ollama = kw.get("ollama")            # (url) -> Ollama-like


# ---------------------------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------------------------

def guard_paths(a) -> None:
    for name in PATH_ARGS:
        v = getattr(a, name, None)
        if v:
            D.guard_path(v, a.cls_root, a.test_syn_root)
    for name in ("legacy_labels", "oof", "label_dir"):
        for v in getattr(a, name, None) or []:
            D.guard_path(v.split("=", 1)[-1], a.cls_root, a.test_syn_root)


def verify_pins(a, *, ollama: bool = False) -> dict:
    """cls_pins verify at start: STATIC always, RUN entries once any exist; exit 9 on a mismatch."""
    return VERIFY_PINS(a.pins_static, a.pins_run, check_ollama=ollama)


def require_selftest(a) -> None:
    S.require_fresh_selftest(a.selftest_result)


def begin(a, *, ollama: bool = False, selftest: bool = True) -> None:
    guard_paths(a)
    D.check_local_url(a.ollama_url)
    verify_pins(a, ollama=ollama)
    if selftest:
        require_selftest(a)
    Path(a.out_dir).mkdir(parents=True, exist_ok=True, mode=0o700)


def out_path(a, name) -> Path:
    return Path(a.out_dir) / name


def write_json(a, name, doc) -> Path:
    p = out_path(a, name)
    S.write_private(p, D.canonical_json(dict(doc, **D.LABEL_FLAGS)))
    return p


def write_jsonl(a, name, rows) -> Path:
    p = out_path(a, name)
    S.write_private(p, "".join(D.canonical_json(r) for r in rows))
    return p


def write_sha(p: Path) -> str:
    sha = S.sha256_file(p)
    S.write_private(Path(str(p) + ".sha256"), "%s  %s\n" % (sha, p.name))
    return sha


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def emit(doc: dict) -> None:
    print(json.dumps(dict(doc, **D.LABEL_FLAGS), sort_keys=True))


def logged(a, name, fn, *, config=None):
    """Append-only run log (PREREG 6.2): a start line before any work (a crash still leaves it) and an end
    line with the counts in finally. Stamps come from the system clock only."""
    log = out_path(a, "cls_dev_log.jsonl")
    cfg = {k: v for k, v in sorted(vars(a).items()) if isinstance(v, (str, int, float, bool, type(None), list))}
    D.append_log(log, {"event": "start", "cmd": name, "utc": S.utc_now(),
                       "config_sha": S.sha256_bytes(D.canonical_json(dict(cfg, extra=config)).encode()),
                       "env_sha_at_use": S.env_sha_at_use(getattr(a, "env_file", None))})
    counts: dict = {}
    code = None
    try:
        code = fn(counts)
        return code
    except S.CLSExit as e:
        counts["refused"] = e.code
        code = e.code
        raise
    except BaseException as e:  # noqa: BLE001 (recorded, re-raised)
        counts["crashed"] = type(e).__name__
        raise
    finally:
        D.append_log(log, {"event": "end", "cmd": name, "utc": S.utc_now(), "counts": counts, "exit": code})


def read_items(a, path) -> list[dict]:
    text = S.read_guarded_text(path, a.cls_root)
    return [json.loads(x) for x in text.splitlines() if x.strip()]


def load_manifest(a) -> dict:
    D.require_run_pin(a.manifest, a.pins_run)
    return read_json(a.manifest)


def scrub_context(a) -> S.ScrubContext:
    return S.ScrubContext(S.load_denylist(getattr(a, "denylist", None)), S.load_env_values(getattr(a, "env_file", None))[0])


def get_ollama(a, deps):
    return deps.ollama(a.ollama_url) if deps.ollama else D.Ollama(a.ollama_url)


def get_readers(deps, ollama, own_model):
    return (deps.readers or D.default_readers)(ollama, own_model)


def label_status(a) -> tuple[dict | None, dict | None]:
    """status.json of each cls_label_run dir; its "set" says real_dev or dev_syn."""
    real = syn = None
    for d in getattr(a, "label_dir", None) or []:
        p = Path(d) / "status.json"
        st = read_json(p) if p.exists() else {}
        if st.get("set") == "dev_syn":
            syn = st
        elif st.get("set") == "real_dev":
            real = st
        else:
            raise S.CLSExit(S.EXIT_REFUSE, "label dir has no real_dev/dev_syn status.json")
    return real, syn


def truth_rules(a) -> dict:
    real, syn = label_status(a)
    res = D.resolve_truth_rules(real, syn)
    if res["not_informative"]:
        raise S.CLSExit(S.EXIT_TWO_VOIDS, "NOT_INFORMATIVE (two voided judges) for: %s" % ",".join(res["not_informative"]))
    return res


def load_truths(a, items: list[dict], rules: dict) -> tuple[dict, dict]:
    run = D.load_run_labels(getattr(a, "label_dir", None) or [])
    return D.collect_truths(items, run, rules["voided"])


def load_legacy(a) -> dict:
    legacy = {}
    for pair in getattr(a, "legacy_labels", None) or []:
        judge, _sep, path = pair.partition("=")
        if judge not in ("opus", "sonnet") or not path:
            raise S.CLSExit(S.EXIT_REFUSE, "--legacy-labels wants opus=FILE or sonnet=FILE")
        legacy[judge] = S.read_jsonl(path)
    return legacy


def text_map(a) -> dict:
    """item_id -> (text, capped digest), read from the 0600 items files through the custody guards."""
    out = {}
    for path in (getattr(a, "items", None), getattr(a, "syn_items", None)):
        if path:
            for it in read_items(a, path):
                out[it["item_id"]] = (it["text"], D.cap_digest(it.get("digest")))
    return out


def train_rows(cls_eval, items, truths, rules) -> tuple[list[dict], dict]:
    """Items with a primary truth, with pooled weights (real 1, DEV-SYN min(1, n_real/n_syn))."""
    prim = D.primary_truths(cls_eval, items, truths, rules["rules"])
    kept = [x for x in items if prim[x["item_id"]]]
    n_real = sum(1 for x in kept if x["kind"] == "real")
    n_syn = len(kept) - n_real
    w_real, w_syn = D.pool_weights(n_real, n_syn)
    rows = [{"item_id": x["item_id"], "fold": x["fold"], "group": "%s:%s" % (x["kind"], x["group"]),
             "truth": prim[x["item_id"]], "w_pool": w_syn if x["kind"] == "syn" else w_real, "kind": x["kind"]}
            for x in kept]
    return rows, {"n_real": n_real, "n_syn": n_syn, "n_missing_truth": len(items) - len(kept)}


# ---------------------------------------------------------------------------------------------
# manifest, scrub-check, folds
# ---------------------------------------------------------------------------------------------

def cmd_manifest(a, deps) -> int:
    begin(a)
    if not a.pins_run:
        raise S.CLSExit(S.EXIT_REFUSE, "manifest needs --pins-run (its outputs are registered before first use)")

    def work(counts):
        ctx = scrub_context(a)
        real = read_items(a, a.items)
        syn = read_items(a, a.syn_items) if a.syn_items else []
        legacy = load_legacy(a)
        den, env = a.denylist, a.env_file
        den_info = {"path": str(den) if den else None, "sha256": S.env_sha_at_use(den), "entries": len(S.load_denylist(den))}
        env_sha = S.env_sha_at_use(env)
        env_info = {"path": str(env) if env else None, "sha256": env_sha, "qualifying_values": S.load_env_values(env)[1]}
        manifest, length_counts = D.build_manifest(real, syn, ctx=ctx, t0=D.parse_utc(a.t0), cutoff=D.parse_utc(a.cutoff),
                                                   legacy=legacy, env_info=env_info, denylist_info=den_info)
        kept = {x["text_sha"] for x in manifest["items"] if x["kind"] == "real"}
        shingles = {"text_sha": sorted(kept),
                    "sets": [sorted(S.shingle_hashes(it["text"])) for it in real if S.text_sha(it["text"]) in kept]}
        mpath = write_json(a, "dev_manifest.json", manifest)
        lpath = out_path(a, "length_counts.json")
        S.write_private(lpath, D.canonical_json(length_counts))
        spath = out_path(a, "shingles_real.json")
        S.write_private(spath, D.canonical_json(shingles))
        sha = write_sha(mpath)
        P.append_run_entry(a.pins_run, a.run_entry, [mpath, lpath, spath], extra={"manifest_sha256": sha})
        counts.update(manifest["counts"], manifest_sha256=sha, env_sha_at_use=env_sha)
        emit({"manifest_sha256": sha, "counts": manifest["counts"], "length_counts": length_counts,
              "env_sha_at_use": env_sha, "pins_run_entry": a.run_entry})
        return 0
    return logged(a, "manifest", work)


def cmd_scrub_check(a, deps) -> int:
    begin(a, selftest=False)

    def work(counts):
        ok, rep = S.self_test(a.result, env_path=getattr(a, "env_file", None))
        bad = sorted([k for k, v in rep["shapes"].items() if not v["ok"]]
                     + [k + ":control" for k, v in rep["controls"].items() if not v["ok"]])
        counts.update({"shapes": len(rep["shapes"]), "failed": bad, "env_sha_at_use": rep["env_sha_at_use"]})
        emit({"ok": ok, "shapes": len(rep["shapes"]), "per_shape_ok": {k: v["ok"] for k, v in rep["shapes"].items()},
              "failed": bad, "env_sha_at_use": rep["env_sha_at_use"]})
        return S.EXIT_OK if ok else S.EXIT_SELFTEST
    return logged(a, "scrub-check", work)


def cmd_folds(a, deps) -> int:
    begin(a)

    def work(counts):
        man = load_manifest(a)
        want = D.assign_folds(man["items"])
        bad = [x["item_id"] for x in man["items"] if x["fold"] != want[x["item_id"]]]
        if bad:
            raise S.CLSExit(S.EXIT_REFUSE, "manifest fold ids differ from the recomputed folds (%d items)" % len(bad))
        spans = {}
        for x in man["items"]:
            spans.setdefault((x["kind"], x["group"]), set()).add(x["fold"])
        sizes = D._tally(x["fold"] for x in man["items"])
        res = {"fold_sizes": sizes, "n_items": len(man["items"]), "folds_match": True,
               "groups_spanning_folds": sum(1 for v in spans.values() if len(v) > 1)}
        counts.update(res)
        emit(res)
        return 0
    return logged(a, "folds", work)


# ---------------------------------------------------------------------------------------------
# fit-heads (C1, C2) and the C4 pair
# ---------------------------------------------------------------------------------------------

class OllamaEmbedder:
    """C2: nomic-embed-text through the local Ollama /api/embed, task prefix 'classification: '."""

    def __init__(self, ollama, model: str):
        self.ollama, self.model = ollama, model
        self.info = {"model": model, "model_digest": ollama.tags().get(model), "prefix": EMBED_PREFIX}

    def count_tokens(self, text):  # a word count is an upper-bound proxy; Ollama truncates at the model limit
        return D.count_words(text)

    def embed(self, texts):
        return self.ollama.embed(self.model, [EMBED_PREFIX + t for t in texts])


class BgeEmbedder:  # pragma: no cover (needs sentence_transformers and the HF cache)
    """C1: bge-small-en-v1.5 from the pinned HF cache snapshot, offline."""

    def __init__(self, snapshot_dir):
        self.dir = Path(snapshot_dir)
        self.info = {"revision": self.dir.name, "files": D.hash_dir_files(self.dir)}
        self._model = None

    def _load(self):
        if self._model is None:
            os.environ["HF_HUB_OFFLINE"] = "1"
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(str(self.dir), device="cpu")
            self._model.max_seq_length = D.TOKEN_CAP
        return self._model

    def count_tokens(self, text):
        return len(self._load().tokenizer.tokenize(text)) + 2

    def embed(self, texts):
        return [list(map(float, v)) for v in self._load().encode(list(texts), normalize_embeddings=True, show_progress_bar=False)]


def make_embedder(cand, a, deps, ollama):
    if deps.embedder:
        return deps.embedder(cand, a)
    if cand == "C2":
        return OllamaEmbedder(ollama, OLLAMA_MODELS["C2"])
    return BgeEmbedder(read_json(a.pins_static)["params"]["bge_snapshot_dir"])  # pragma: no cover


def candidate_inputs(a, deps, rows, count_tokens) -> list[str]:
    texts = text_map(a)
    missing = [r["item_id"] for r in rows if r["item_id"] not in texts]
    if missing:
        raise S.CLSExit(S.EXIT_REFUSE, "items file lacks %d manifest items" % len(missing))
    return [D.model_input(texts[r["item_id"]][1], deps.canonical(texts[r["item_id"]][0]), count_tokens=count_tokens)
            for r in rows]


def cmd_fit_heads(a, deps) -> int:
    begin(a, ollama=(a.candidate == "C2"))
    cls_eval = D.load_cls_eval(a.cls_eval)

    def work(counts):
        man = load_manifest(a)
        rules = truth_rules(a)
        truths, lc = load_truths(a, man["items"], rules)
        rows, c = train_rows(cls_eval, man["items"], truths, rules)
        ollama = get_ollama(a, deps) if a.candidate == "C2" else None
        emb = make_embedder(a.candidate, a, deps, ollama)
        inputs = candidate_inputs(a, deps, rows, emb.count_tokens)
        try:
            vecs = [D.l2_normalise(v) for v in emb.embed(inputs)]
        finally:
            if ollama is not None:
                D.unload_own(ollama, OLLAMA_MODELS["C2"])
        res = D.fit_candidate(deps.trainer, vecs, rows)
        write_jsonl(a, "oof_%s.jsonl" % a.candidate, res["oof"])
        art = dict(res["artifact"], candidate=a.candidate, embedder=emb.info, seed=D.SEED, input_cap_tokens=D.TOKEN_CAP,
                   artifact_sha256=res["artifact_sha256"], truth_rules=rules["rules"])
        write_json(a, "model_%s.json" % a.candidate, art)
        counts.update(c, n_oof=len(res["oof"]), artifact_sha256=res["artifact_sha256"], labels=lc)
        emit({"candidate": a.candidate, "n_oof": len(res["oof"]), "artifact_sha256": res["artifact_sha256"],
              "C": res["artifact"]["C"], **c, "label_counts": lc, "truth_rules": rules["rules"],
              "truth_fallback": rules["truth_fallback"]})
        return 0
    return logged(a, "fit-heads", work, config={"candidate": a.candidate})


def heavy_preamble(deps, ollama, own_model) -> dict | None:
    """Quiet gate and night rule before a heavy-model run; None when clear, else the parked record. An
    unreachable Ollama during the gate parks the run (exit 3 by the callers), it never crashes it."""
    try:
        q = D.quiet_gate(get_readers(deps, ollama, own_model), sleep=deps.sleep)
    except D.OllamaError:
        return {"status": "parked", "reason": "quiet:ollama_unreachable", "readings": []}
    if not q["ok"]:
        return {"status": "parked", "reason": "quiet:" + str(q["reason"]), "readings": q["readings"]}
    ok, why = D.can_start_unit(deps.clock(), None)
    if not ok:
        return {"status": "parked", "reason": why, "readings": q["readings"]}
    return None


def cmd_c4_precheck(a, deps) -> int:
    begin(a, ollama=True)

    def work(counts):
        ollama = get_ollama(a, deps)
        model = OLLAMA_MODELS["C4"]
        park = heavy_preamble(deps, ollama, model)
        if park:
            counts.update(park)
            emit(park)
            return D.EXIT_PARK
        ver, resp = None, {}
        try:
            ver = ollama.version()
            resp = ollama.chat(D.c4_payload(model, "(no context)", "rename the variable x to count"))
        except D.OllamaError:
            pass
        finally:
            D.unload_own(ollama, model)
        status = dict(D.c4_precheck_status(resp), ollama_version=ver)
        write_json(a, "c4_status.json", status)
        counts.update(status)
        emit(status)
        return {"ok": S.EXIT_OK, "killed": D.EXIT_C4_KILLED}.get(status["c4"], D.EXIT_PARK)
    return logged(a, "c4-precheck", work)


def cmd_c4_run(a, deps) -> int:
    begin(a, ollama=True)
    cls_eval = D.load_cls_eval(a.cls_eval)

    def work(counts):
        st_path = out_path(a, "c4_status.json")
        status = read_json(st_path) if st_path.exists() else None
        if not status or status.get("c4") != "ok":
            parked = bool(status) and status.get("c4") == "parked_for_coordinator"
            refusal = ({"c4": "parked_for_coordinator", "reason": status.get("reason")} if parked else
                       {"c4": "killed", "criterion": 1, "reason": (status or {}).get("reason", "no_precheck")})
            counts.update(refusal)
            emit(refusal)
            return D.EXIT_PARK if parked else D.EXIT_C4_KILLED
        man = load_manifest(a)
        rules = truth_rules(a)
        truths, _lc = load_truths(a, man["items"], rules)
        rows, c = train_rows(cls_eval, man["items"], truths, rules)
        texts = text_map(a)
        ollama = get_ollama(a, deps)
        model = OLLAMA_MODELS["C4"]
        feats_path = out_path(a, "c4_features.jsonl")
        done = {r["item_id"] for r in S.read_jsonl(feats_path)}
        todo = [r for r in rows if r["item_id"] not in done]
        units = [todo[i:i + UNIT_N] for i in range(0, len(todo), UNIT_N)]

        def do_unit(_i, unit):
            for r in unit:
                text, digest = texts[r["item_id"]]
                try:
                    resp = ollama.chat(D.c4_payload(model, digest, deps.canonical(text)))
                except D.OllamaError:
                    resp = {}
                S.append_jsonl(feats_path, {"item_id": r["item_id"], "features": D.c4_features_from_response(resp)})
        res = D.run_units(units, do_unit, clock=deps.clock, state_path=out_path(a, "c4_state.json"), ollama=ollama,
                          own_model=model, artifact_shas={"manifest": S.sha256_file(a.manifest)},
                          quiet=lambda: D.quiet_gate(get_readers(deps, ollama, model), sleep=deps.sleep))
        counts.update({k: v for k, v in res.items() if k != "readings"}, readings=res.get("readings"))
        if res["status"] != "done":
            emit(res)
            return D.EXIT_PARK
        feats = {r["item_id"]: r["features"] for r in S.read_jsonl(feats_path)}
        fit_rows = [r for r in rows if feats.get(r["item_id"])]
        n_error = len(rows) - len(fit_rows)
        fit = D.fit_candidate(deps.trainer, [feats[r["item_id"]] for r in fit_rows], fit_rows)
        write_jsonl(a, "oof_C4.jsonl", fit["oof"])
        art = dict(fit["artifact"], candidate="C4", model=model, ollama_version=status["ollama_version"],
                   artifact_sha256=fit["artifact_sha256"], n_error=n_error)
        write_json(a, "model_C4.json", art)
        counts.update(c, n_oof=len(fit["oof"]), n_error=n_error, artifact_sha256=fit["artifact_sha256"])
        emit({"candidate": "C4", "n_oof": len(fit["oof"]), "n_error": n_error, "artifact_sha256": fit["artifact_sha256"]})
        return 0
    return logged(a, "c4-run", work)


# ---------------------------------------------------------------------------------------------
# ModernBERT (C3, conditional)
# ---------------------------------------------------------------------------------------------

def cmd_trigger_mb(a, deps) -> int:
    begin(a)

    def work(counts):
        res = D.mb_trigger(read_json(a.results).get("candidates", {}))
        write_json(a, "mb_trigger.json", res)
        counts.update(res)
        print("wait" if res.get("wait") else ("fire" if res["fire"] else "no-fire"))
        return 0
    return logged(a, "trigger-mb", work)


def cmd_fit_modernbert(a, deps) -> int:
    begin(a)

    def work(counts):
        trig = Path(a.trigger)
        if not trig.exists() or not read_json(trig).get("fire"):
            raise S.CLSExit(D.EXIT_MB_GATE, "fit-modernbert needs a trigger file that says fire")
        cache = D.check_mb_cache(a.cls_cache)
        D.check_mb_download_log(a.download_log, cache)
        cls_eval = D.load_cls_eval(a.cls_eval)  # pragma: no cover (below needs torch and the download)
        man = load_manifest(a)
        rules = truth_rules(a)
        truths, _lc = load_truths(a, man["items"], rules)
        rows, c = train_rows(cls_eval, man["items"], truths, rules)
        ollama = get_ollama(a, deps)  # D-160: foreign-resident models are checked via /api/ps
        park = heavy_preamble(deps, ollama, None)
        if park:
            counts.update(park)
            emit(park)
            return D.EXIT_PARK
        inputs = candidate_inputs(a, deps, rows, D.count_words)
        oof = D.modernbert_oof(rows, inputs, cache)
        write_jsonl(a, "oof_C3.jsonl", oof)
        counts.update(c, n_oof=len(oof))
        emit({"candidate": "C3", "n_oof": len(oof), **c})
        return 0
    return logged(a, "fit-modernbert", work)


def cmd_download_modernbert(a, deps) -> int:
    begin(a)

    def work(counts):
        res = D.download_modernbert(confirm_d129=a.confirm_d129, revision=a.revision, cls_cache=a.cls_cache,
                                    log_path=a.download_log)
        counts.update(res)
        emit(res)
        return 0
    return logged(a, "download-modernbert", work)


# ---------------------------------------------------------------------------------------------
# reference arms
# ---------------------------------------------------------------------------------------------

KB1_OPAQUE = "L001"


def kb1_mods(a):
    """ADAPTER, part 1 (B0 touches the B-L judge-prompt contract only here and in kb1_payload/kb1_verdict):
    the pinned rubric.py + gt500.py through cls_label_run.load_judge_mods (md5/sha checked; exit 9 otherwise)."""
    import cls_label_run as L
    return L.load_judge_mods(a.pins_static, a.prompt_module)


def kb1_payload(mods, model: str, context: str, prompt: str) -> dict:
    """ADAPTER, part 2: the K-B1 rubric request = cls_label_run.build_payload for one item (rubric first,
    gt.prompt_view form, SINGLE_TAIL verbatim). Sent to the LOCAL Ollama only."""
    import cls_label_run as L
    body = L.build_payload(mods, [{"opaque": KB1_OPAQUE, "ctx": context or "", "prompt": prompt}], single=True)
    return {"model": model, "stream": False, "think": False, "keep_alive": "30m",
            "messages": [{"role": "user", "content": body.decode("utf-8")}],
            "options": {"temperature": 0, "num_predict": 400, "num_ctx": 8192}}


def kb1_verdict(mods, content) -> str | None:
    """ADAPTER, part 3: the answer through cls_label_run.parse_labels (gt.extract_json + gt.valid_label)."""
    import cls_label_run as L
    lab = L.parse_labels(mods, content if isinstance(content, str) else "", [KB1_OPAQUE], single=True).get(KB1_OPAQUE)
    return lab["tier"] if lab else None


def kw1_payload(model: str, context: str, prompt: str) -> dict:
    from llm_router import local_classifier as lc
    return lc._payload(model, lc.Assembled(context or "(no context)", prompt))


def kw1_verdict(content) -> str | None:
    from llm_router import local_classifier as lc
    v = lc.parse_verdict(content if isinstance(content, str) else "")
    return ("haiku" if v.tier == "local" else v.tier) if v.ok else None


def run_hard_arm(a, deps, ollama, arm, ids, texts, counts, mods=None) -> dict:
    model = OLLAMA_MODELS[arm]
    part = out_path(a, "arm_part_%s.jsonl" % arm)
    done = {r["item_id"] for r in S.read_jsonl(part)}
    todo = [i for i in ids if i not in done]
    units = [todo[i:i + UNIT_N] for i in range(0, len(todo), UNIT_N)]

    def do_unit(_i, unit):
        for i in unit:
            text, digest = texts[i]
            canon = deps.canonical(text)
            payload = kw1_payload(model, digest, canon) if arm == D.ARM_KW1 else kb1_payload(mods, model, digest, canon)
            try:
                content = ollama.chat(payload).get("message", {}).get("content")
                tier = kw1_verdict(content) if arm == D.ARM_KW1 else kb1_verdict(mods, content)
            except D.OllamaError:
                tier = None
            S.append_jsonl(part, {"item_id": i, "tier": tier})
    res = D.run_units(units, do_unit, clock=deps.clock, state_path=out_path(a, "arm_state_%s.json" % arm), ollama=ollama,
                      own_model=model, artifact_shas={"ids": S.sha256_bytes(",".join(ids).encode())},
                      quiet=lambda: D.quiet_gate(get_readers(deps, ollama, model), sleep=deps.sleep))
    counts.setdefault("runs", {})[arm] = {k: v for k, v in res.items() if k != "readings"}
    if res["status"] != "done":
        counts.setdefault("parked", {})[arm] = res["reason"]
        return {}
    got = {r["item_id"]: r["tier"] for r in S.read_jsonl(part)}
    return {i: got.get(i) for i in ids}


def cmd_reference_arms(a, deps) -> int:
    want = set(a.arm or [D.ARM_KW1, D.ARM_KB1, D.ARM_ENGINE])
    begin(a, ollama=bool(want & {D.ARM_KW1, D.ARM_KB1}))
    cls_eval = D.load_cls_eval(a.cls_eval)

    def work(counts):
        man = load_manifest(a)
        items = man["items"]
        ids = [x["item_id"] for x in items]
        rules = truth_rules(a)
        truths, _lc = load_truths(a, items, rules)
        prev_p = out_path(a, "reference_arms.json")
        arms: dict = read_json(prev_p)["arms"] if prev_p.exists() else {}
        arms["always_haiku"] = D.arm_constant(ids, "haiku")
        arms["always_sonnet"] = D.arm_constant(ids, "sonnet")
        arms["always_opus"] = D.arm_constant(ids, "opus")
        arms["random"] = D.arm_random(ids)
        prim = D.primary_truths(cls_eval, items, truths, rules["rules"])
        arms["oracle"] = dict(prim)
        arms["noised_oracle"] = D.arm_noised_oracle(ids, prim)
        header, rrows = D.read_rules_tier(a.rules_tier, a.pins_run)
        arms["rules_eff"], arms["rules_unlifted"] = D.rules_arms(rrows, ids)
        counts["rules_tier_header"] = header
        texts = text_map(a) if want & {D.ARM_KW1, D.ARM_KB1, D.ARM_ENGINE} else {}
        if D.ARM_ENGINE in want:
            D.verify_engine_pin(read_json(a.pins_static), a.engine_module)
            arms[D.ARM_ENGINE] = {i: deps.engine_tier(texts[i][0]) if i in texts else None for i in ids}
        heavy = [x for x in (D.ARM_KW1, D.ARM_KB1) if x in want]
        mods = kb1_mods(a) if D.ARM_KB1 in heavy else None
        ollama = get_ollama(a, deps) if heavy else None
        for arm in heavy:  # one heavy model at a time
            got = run_hard_arm(a, deps, ollama, arm, ids, texts, counts, mods)
            if got:
                arms[arm] = got
            else:
                break
        counts["n_none"] = {k: sum(1 for v in d.values() if v is None) for k, d in sorted(arms.items())}
        write_json(a, "reference_arms.json", {"arms": arms, "rules_tier_sha256": S.sha256_file(a.rules_tier)})
        emit({"arms": sorted(arms), "n_none": counts["n_none"], "parked": counts.get("parked")})
        return D.EXIT_PARK if counts.get("parked") else 0
    return logged(a, "reference-arms", work)


# ---------------------------------------------------------------------------------------------
# latency and stability
# ---------------------------------------------------------------------------------------------

def make_l2_scorer(cand, a, deps, ollama):  # pragma: no cover (real models; tests inject deps.l2_score)
    art = read_json(a.model_artifact)
    head = D.scorer_from_artifact(art)
    if cand == "C4":
        model = OLLAMA_MODELS["C4"]

        def score(text, digest):
            f = D.c4_features_from_response(ollama.chat(D.c4_payload(model, digest, deps.canonical(text))))
            return None if f is None else head(f)
        return score
    emb = make_embedder(cand, a, deps, ollama)

    def score(text, digest):
        x = D.model_input(digest, deps.canonical(text), count_tokens=emb.count_tokens)
        return head(D.l2_normalise(emb.embed([x])[0]))
    return score


def cmd_latency(a, deps) -> int:
    D.refuse_unmeasured_candidate(a.candidate)
    uses_ollama = a.candidate in ("C2", "C4")
    begin(a, ollama=uses_ollama)

    def work(counts):
        blocks_path = out_path(a, "latency_blocks_%s.jsonl" % a.candidate)
        prev = S.read_jsonl(blocks_path)
        if a.summarize:
            return summarize_latency(a, deps, prev, counts)
        valid_prev = [r for r in prev if r.get("valid")]
        if len(valid_prev) >= 3:
            raise S.CLSExit(S.EXIT_REFUSE, "three valid blocks already exist; use --summarize")
        now = deps.utc_clock()
        if not D.blocks_spaced([D.parse_utc(r["start_utc"]) for r in valid_prev], now):
            park = {"status": "parked", "reason": "blocks_less_than_30_min_apart"}
            counts.update(park)
            emit(park)
            return D.EXIT_PARK
        ollama = get_ollama(a, deps)  # every candidate: foreign-resident models are checked via /api/ps (D-160)
        own = OLLAMA_MODELS.get(a.candidate)
        park = heavy_preamble(deps, ollama, own)
        if park:
            counts.update(park)
            emit(park)
            return D.EXIT_PARK
        man = load_manifest(a)
        sample = D.stratified_sample([x for x in man["items"] if x["kind"] == "real"], a.n)
        texts = text_map(a)
        score = deps.l2_score(a.candidate, a) if deps.l2_score else make_l2_scorer(a.candidate, a, deps, ollama)
        readers = get_readers(deps, ollama, own)
        warm = []
        try:
            for k in range(a.warmups):
                text, digest = texts[sample[k % len(sample)]["item_id"]]
                t0 = deps.perf()
                score(text, digest)
                warm.append((deps.perf() - t0) * 1000.0)
            timings, readings, n_none = D.run_timed_block([texts[x["item_id"]] for x in sample], score,
                                                          take=lambda: D.take_reading(readers), perf=deps.perf,
                                                          mono=deps.monotonic)
            model_gib = D.ollama_model_gib(ollama, own) if own else None
        finally:
            if own:
                D.unload_own(ollama, own)
        invalid = D.evaluate_block(readings)
        rec = {"start_utc": D.utc_str(now), "valid": invalid is None, "invalid": invalid, "timings_ms": timings,
               "warmup_ms": warm, "n": len(timings), "n_none": n_none, "rss_gib": deps.rss_gib(), "model_gib": model_gib,
               "readings": readings, "n_readings": len(readings)}
        S.append_jsonl(blocks_path, rec)
        discarded = sum(1 for r in prev if not r.get("valid")) + (0 if rec["valid"] else 1)
        counts.update(valid=rec["valid"], invalid=invalid, n=len(timings), discarded_blocks=discarded)
        emit({"candidate": a.candidate, "valid": rec["valid"], "invalid": invalid, "n_calls": len(timings),
              "n_warmups": len(warm), "discarded_blocks": discarded})
        return 0
    return logged(a, "latency", work, config={"candidate": a.candidate})


def _hw_memsize():
    try:
        return int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, timeout=10, shell=False).stdout.decode().strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def summarize_latency(a, deps, prev, counts) -> int:
    valid = [r for r in prev if r.get("valid")]
    if len(valid) < 3:
        raise S.CLSExit(S.EXIT_REFUSE, "latency needs three valid blocks (have %d)" % len(valid))
    valid = valid[:3]
    starts = [D.parse_utc(r["start_utc"]) for r in valid]
    if any((b - x).total_seconds() < D.BLOCK_GAP_S for i, x in enumerate(starts) for b in starts[i + 1:]):
        raise S.CLSExit(S.EXIT_REFUSE, "the three blocks are not 30 minutes apart")
    w0 = valid[0]["warmup_ms"]
    summ = D.latency_summary([r["timings_ms"] for r in valid], w0, w0[0] if w0 else None)
    summ.update({"candidate": a.candidate, "discarded_blocks": sum(1 for r in prev if not r.get("valid")),
                 "rss_gib": max(r.get("rss_gib") or 0.0 for r in valid),
                 "model_gib": max((r.get("model_gib") or 0.0 for r in valid), default=0.0),
                 "hw_memsize": _hw_memsize(), "block_starts_utc": [r["start_utc"] for r in valid]})
    write_json(a, "latency_%s.json" % a.candidate, summ)
    counts.update(n_calls=summ["n_calls"], n_blocks=summ["n_blocks"], hw_memsize=summ["hw_memsize"])
    emit({k: summ[k] for k in ("candidate", "n_calls", "n_blocks", "p50_ms", "p95_ms", "p99_ms", "p95_ci_ms",
                               "worst_block_p95_ms", "discarded_blocks", "n_warmups", "cold_first_load_ms")})
    return 0


def oof_decisions(path, t_hi, t_lo) -> dict:
    return {r["item_id"]: D.l2_tier({"haiku": r["p_haiku"], "sonnet": r["p_sonnet"], "opus": r["p_opus"]}, t_hi, t_lo)
            for r in S.read_jsonl(path)}


def cmd_stability(a, deps) -> int:
    begin(a)

    def work(counts):
        fr = read_json(a.fit).get("frozen") or {}
        if not fr.get("admissible"):
            raise S.CLSExit(S.EXIT_REFUSE, "stability needs an admissible frozen fit (cls_eval fit --out)")
        share, n = D.stability_share(oof_decisions(a.run_a, fr["t_hi"], fr["t_lo"]),
                                     oof_decisions(a.run_b, fr["t_hi"], fr["t_lo"]))
        res = {"candidate": a.candidate, "identical_share": share, "n": n, "t_hi": fr["t_hi"], "t_lo": fr["t_lo"]}
        write_json(a, "stability_%s.json" % a.candidate, res)
        counts.update(res)
        emit(res)
        return 0
    return logged(a, "stability", work, config={"candidate": a.candidate})


# ---------------------------------------------------------------------------------------------
# assemble: the B1 arms table, cls_eval runs (documented argv only), criteria 0-8, finalists
# ---------------------------------------------------------------------------------------------

def load_oof(a) -> dict:
    out = {}
    for pair in a.oof or []:
        cand, path = pair.split("=", 1)
        if cand not in D.FAMILY:
            raise S.CLSExit(S.EXIT_REFUSE, "--oof wants C1|C2|C3|C4=FILE")
        out[cand] = {r["item_id"]: r for r in S.read_jsonl(path)}
    return out


def shadow_eval_pin(a) -> str | None:
    try:
        return read_json(a.pins_static)["pins"]["engine"]["shadow_eval_py_sha256"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def run_cls_eval(a, argv: list[str], out: Path, ok_codes=(0,)) -> tuple[dict | None, int]:
    """cls_eval as a subprocess through its documented argv only (no import coupling beyond truth_for)."""
    cmd = [sys.executable, str(a.cls_eval)] + argv + ["--seed", str(D.SEED), "--boot-b", str(a.boot_b), "--out", str(out)]
    se_pin = shadow_eval_pin(a)
    if se_pin:  # the shadow_eval sha pinned (by engine commit) in PINS-STATIC; cls_eval exits 3 on a mismatch
        cmd += ["--pin-shadow-eval-sha", se_pin]
    r = subprocess.run(cmd, capture_output=True, timeout=CLS_EVAL_TIMEOUT_S, shell=False)
    if r.returncode not in ok_codes:
        raise S.CLSExit(S.EXIT_REFUSE, "cls_eval %s exited %d" % (argv[0], r.returncode))
    return (read_json(out) if out.exists() else None), r.returncode


def truth_by_source(items: list[dict], rules: dict) -> str:
    srcs = sorted({x["source_set"] for x in items})
    return ",".join("%s=%s" % (s, rules[D.SYN_SOURCE if s == D.SYN_SOURCE else "real"]) for s in srcs)


def _metrics_of(score: dict, col: str) -> dict | None:
    comp = score["primary"]["comparisons"].get(col)
    arm = score["arms"].get(col)
    if not comp or not arm or not comp.get("evaluable"):
        return None
    return {"cost": comp["cost"]["cand"], "under": comp["under"]["cand"], "raw_cost": comp["raw_cost"],
            "opus_share": arm["share"]["opus"] or 0.0, "guard": comp["guard"]["passes"], "bars": comp["bars"],
            "haiku_routes": arm["haiku_precision"]["n"], "haiku_precision": arm["haiku_precision"]["rate"]}


def _armb_ok(m: dict, truth_opus_share: float) -> bool:
    """PREREG 7.2 constraints as far as a fixed decision column allows (guard, bars, opus share, haiku)."""
    if not m["guard"] or not m["bars"] or m["opus_share"] > truth_opus_share + 1e-12:
        return False
    return m["haiku_routes"] == 0 or (m["haiku_routes"] >= 10 and (m["haiku_precision"] or 0.0) >= 0.70)


T_CONF_METHOD = "in-sample screen (no 7.2/7.3)"  # D-160: stated in the finalist file; cls_eval fitting is not reimplemented


def fit_arm_b(a, cand: str, rows: list[dict], tbs: str, truth_opus_share: float) -> dict:
    """Arm A vs arm B for one finalist (PREREG 9.2): t_conf over {0.50..0.95} and the A/B choice are made
    ONLY on arm_choice_items (outside GE3 tune / E2-cal, plus DEV-SYN); both arms are also scored on the
    engine-seen items, reported and flagged."""
    out = {}
    cols = {"B@%.2f" % t: t for t in D.T_CONF_GRID}
    for part, sel in (("choice", D.arm_choice_items(rows)), ("engine_seen", D.engine_seen_items(rows))):
        if not sel:
            out[part] = {"n": 0}
            continue
        recs = []
        for r in sel:
            preds = {"rules_eff": r["preds"].get("rules_eff"), "A": r["preds"].get(cand),
                     D.ARM_ENGINE: r["preds"].get(D.ARM_ENGINE)}
            for col, t in cols.items():
                preds[col] = D.arm_b_preds([r], cand, t)[r["item_id"]]
            recs.append(dict({k: v for k, v in r.items() if k != "scores"}, preds=preds))
        tp = write_jsonl(a, "armchoice_%s_%s.jsonl" % (cand, part), recs)
        sc, _ = run_cls_eval(a, ["score", str(tp), "--truth-by-source", tbs, "--mode", "dev"],
                             out_path(a, "armchoice_%s_%s.score.json" % (cand, part)))
        out[part] = {"n": len(sel), "A": _metrics_of(sc, "A"), "B": {c: _metrics_of(sc, c) for c in cols}}
    ch = out["choice"]
    if not ch.get("n") or ch["A"] is None:
        return {"arm": "A", "t_conf": None, "t_conf_method": T_CONF_METHOD, "detail": out, "note": "no arm-choice items"}
    okb = [(D.objective_key(m) + (-cols[c],), c) for c, m in ch["B"].items() if m and _armb_ok(m, truth_opus_share)]
    best = min(okb)[1] if okb else None
    arm = D.choose_arm(ch["A"], ch["B"][best] if best else None)
    return {"arm": arm, "t_conf": cols[best] if best else None, "t_conf_method": T_CONF_METHOD, "detail": out}


def c4_status(a) -> dict | None:
    p = out_path(a, "c4_status.json")
    return read_json(p) if p.exists() else None


def candidate_criteria(a, cand, *, floor, score, fit, n_error, n_pool, low_cluster, resemblance_failed, voided_real,
                       voided_syn=(), n_syn_pool=0, score_noerr=None):
    lat_p, st_p = out_path(a, "latency_%s.json" % cand), out_path(a, "stability_%s.json" % cand)
    lat = read_json(lat_p) if lat_p.exists() else {}
    st = read_json(st_p) if st_p.exists() else {}
    c4 = c4_status(a) if cand == "C4" else None
    po = (score or {}).get("pooled_oos") or {}
    sr = ((score or {}).get("split_rows") or {}).get("kinds") or {}
    re_raw = ((score or {}).get("arms") or {}).get("rules_eff", {}).get("raw_cost")
    raw = po.get("raw_cost")
    jr = (score or {}).get("judge_robustness") or {}
    rows = [
        D.criterion0(floor),
        D.criterion1({"runtime_unavailable": bool(c4 and c4.get("c4") == "killed"),
                      "runtime_parked": bool(c4 and c4.get("c4") == "parked_for_coordinator"), "reason": (c4 or {}).get("reason"),
                      "p95_ms": lat.get("p95_ms"), "worst_block_p95_ms": lat.get("worst_block_p95_ms"),
                      "rss_gib": lat.get("rss_gib"), "model_gib": lat.get("model_gib"), "n_calls": lat.get("n_calls"),
                      "n_blocks": lat.get("n_blocks")}),
        D.criterion2({"n": n_pool, "n_error": n_error}),
        D.criterion3(((score_noerr if score_noerr is not None else score or {}).get("auroc") or {}).get(cand) or {},
                     with_fallback=((score or {}).get("auroc") or {}).get(cand) or {}),
        D.criterion4(dict(po, admissible=bool(po.get("admissible")) and bool((fit or {}).get("admissible")))),
        D.criterion5({"raw": raw, "raw_re": re_raw, "ratio": (raw / re_raw) if raw is not None and re_raw else None}),
        D.criterion6({"real": sr.get("real"), "syn": sr.get("syn")}, low_cluster=low_cluster,
                     resemblance_failed=resemblance_failed),
        D.criterion7({"identical_share": st.get("identical_share"), "n": st.get("n")}),
        D.criterion8(jr.get("arms", {}).get(cand), voided=voided_real,
                     syn=(jr.get("syn_leg") or {}).get("arms", {}).get(cand), voided_syn=voided_syn,
                     syn_rows_present=bool(n_syn_pool)),
    ]
    tbl = D.criteria_table(rows)
    tbl["candidate"] = cand
    return tbl


def cmd_assemble(a, deps) -> int:
    begin(a)
    cls_eval = D.load_cls_eval(a.cls_eval)

    def work(counts):
        man = load_manifest(a)
        items = man["items"]
        rules = truth_rules(a)
        truths, lc = load_truths(a, items, rules)
        prim = D.primary_truths(cls_eval, items, truths, rules["rules"])
        rl = read_json(a.resemblance_label) if a.resemblance_label and Path(a.resemblance_label).exists() else {}
        resemblance_failed = rl.get("dev_syn") == "RESEMBLANCE_FAILED"
        refs = read_json(a.reference_arms)["arms"]
        if not any(v for v in refs.get("rules_eff", {}).values()):
            raise S.CLSExit(S.EXIT_REFUSE, "reference arms carry no rules_eff (the bar)")
        oofs = load_oof(a)
        pooled = [x for x in items if prim[x["item_id"]]]
        n_real = sum(1 for x in pooled if x["kind"] == "real")
        w_real, w_syn = D.pool_weights(n_real, len(pooled) - n_real)
        n_error = {c: sum(1 for x in pooled if x["item_id"] not in t) for c, t in oofs.items()}

        def rows_with(cand_preds: dict) -> list[dict]:
            rows = []
            for x in items:
                iid = x["item_id"]
                preds = {arm: d.get(iid) for arm, d in refs.items()}
                preds.update({c: cand_preds.get(c, {}).get(iid) for c in oofs})
                scores = {}
                for c, t in oofs.items():
                    r = t.get(iid)
                    scores[c] = ({"p_haiku": r["p_haiku"], "p_sonnet": r["p_sonnet"], "p_opus": r["p_opus"]} if r
                                 else D.fallback_scores(refs.get("rules_eff", {}).get(iid)))
                row = D.build_arms_row(x, truths=truths[iid], preds=preds, scores=scores,
                                       weight=w_syn if x["kind"] == "syn" else w_real)
                D.validate_arms_record(row)
                rows.append(row)
            return rows
        tbs = truth_by_source(items, rules["rules"])
        fit_table = write_jsonl(a, "arms_table_fit.jsonl", rows_with({}))
        fits, decisions = {}, {}
        for c in sorted(oofs):
            dec_p = out_path(a, "decisions_%s.jsonl" % c)
            fits[c], _code = run_cls_eval(a, ["fit", str(fit_table), "--arm", c, "--truth-by-source", tbs,
                                              "--decisions-out", str(dec_p)], out_path(a, "fit_%s.json" % c), ok_codes=(0, 5))
            decisions[c] = {r["item_id"]: r["decision"] for r in S.read_jsonl(dec_p)} if dec_p.exists() and fits[c].get("admissible") else {}
        rows = rows_with(decisions)
        table = write_jsonl(a, "arms_table.jsonl", rows)
        table_sha = write_sha(table)
        scores, scores_noerr = {}, {}
        for c in sorted(oofs):
            scores[c], _ = run_cls_eval(a, ["score", str(table), "--truth-by-source", tbs, "--mode", "dev", "--pooled-oos",
                                            "--arm", c, "--split-rows", "real,syn"], out_path(a, "score_%s.json" % c))
            if n_error[c]:  # criterion 3 on the items the candidate actually scored (D-160); cls_eval computes it
                kept = D.exclude_error_rows(rows, set(oofs[c]))
                tp = write_jsonl(a, "arms_table_noerr_%s.jsonl" % c, kept)
                scores_noerr[c], _ = run_cls_eval(a, ["score", str(tp), "--truth-by-source", tbs, "--mode", "dev"],
                                                  out_path(a, "score_noerr_%s.json" % c))
        base_score = next(iter(scores.values()), None)
        if base_score is None:
            base_score, _ = run_cls_eval(a, ["score", str(table), "--truth-by-source", tbs, "--mode", "dev"],
                                         out_path(a, "score_reference.json"))
        real_items = [x for x in items if x["kind"] == "real"]
        n_prim_real = sum(1 for x in real_items if prim[x["item_id"]])
        floor_counts = dict(man["counts"], n_truth_opus=sum(1 for x in real_items if prim[x["item_id"]] == "opus"),
                            primary_missing_share=(1 - n_prim_real / len(real_items)) if real_items else 1.0)
        floor = D.validity_floor(floor_counts)
        low_cluster = bool(man["counts"].get("low_cluster_fidelity"))
        voided_real = rules["voided"].get("real", [])
        voided_syn = rules["voided"].get(D.SYN_SOURCE, [])
        tables = {}
        for c in sorted(oofs):
            tables[c] = candidate_criteria(a, c, floor=floor, score=scores[c], fit=fits[c], n_error=n_error[c],
                                           n_pool=len(pooled), low_cluster=low_cluster,
                                           resemblance_failed=resemblance_failed, voided_real=voided_real,
                                           voided_syn=voided_syn, n_syn_pool=len(pooled) - n_real,
                                           score_noerr=scores_noerr.get(c))
        c4 = c4_status(a)
        if "C4" not in oofs and c4 and c4.get("c4") in ("killed", "parked_for_coordinator"):
            tables["C4"] = candidate_criteria(a, "C4", floor=floor, score=None, fit=None, n_error=0, n_pool=len(pooled),
                                              low_cluster=low_cluster, resemblance_failed=resemblance_failed,
                                              voided_real=voided_real)
        validity = base_score.get("validity", {})
        validity_ok = bool(validity.get("ok")) and not validity.get("not_run")
        survivors = {c: {"cost": scores[c]["pooled_oos"]["cost"]["cand"], "under": scores[c]["pooled_oos"]["under"]["cand"],
                         "under_re": scores[c]["pooled_oos"]["under"]["base"]}
                     for c, t in tables.items() if t["eligible"] and c in scores}
        sel = D.select_finalists(survivors) if validity_ok and floor["status"] == "OK" else {
            "ranked": [], "finalists": [], "displaced": None}
        w_tot = sum(r["weight"] for r in rows if prim[r["item_id"]])
        truth_opus_share = (sum(r["weight"] for r in rows if prim[r["item_id"]] == "opus") / w_tot) if w_tot else 0.0
        finalists = []
        for c in sel["finalists"]:
            arm = fit_arm_b(a, c, rows, tbs, truth_opus_share)
            mp = out_path(a, "model_%s.json" % c)
            finalists.append({"candidate": c, "family": D.FAMILY[c], "labels": tables[c]["labels"],
                              "sync_authority": "async_only" not in tables[c]["labels"], "arm": arm["arm"], "arm_choice": arm,
                              "artifact_sha256": [read_json(mp)["artifact_sha256"]] if mp.exists() else [],
                              "thresholds": {"t_hi": fits[c]["frozen"].get("t_hi"), "t_lo": fits[c]["frozen"].get("t_lo"),
                                             "t_conf": arm["t_conf"]},
                              "runtime_config": {"model": OLLAMA_MODELS.get(c), "input_cap_tokens": D.TOKEN_CAP,
                                                 "seed": D.SEED},
                              "dev_cv_metrics": scores[c]["pooled_oos"], "criteria": tables[c],
                              "strata_tables": scores[c].get("strata", {}).get(c),
                              "manifest_sha256": S.sha256_file(a.manifest)})
        flags = {}
        if rules["truth_fallback"]:
            flags["truth_fallback"] = rules["truth_fallback"]
            flags["truth_fallback_labels"] = ["TRUTH_FALLBACK:%s" % j for j in sorted(set(rules["truth_fallback"].values()))]
        if resemblance_failed:
            flags["resemblance"] = "RESEMBLANCE_FAILED"
        if low_cluster:
            flags["cluster_fidelity"] = "LOW CLUSTER FIDELITY"
        extra = dict(flags, truth_rules=rules["rules"], truth_by_source=tbs, manifest_sha256=S.sha256_file(a.manifest),
                     arms_table_sha256=table_sha)
        screen = "NOT_INFORMATIVE" if floor["status"] != "OK" or validity.get("reasons") else "SCREENED"
        cand_summary = {c: {"killed_by": t["killed_by"], "eligible": t["eligible"],
                            "survives_1_4": all(r["status"] in D.PASSING for r in t["rows"] if 1 <= r["n"] <= 4),
                            "pending_1_4": any(r["status"] == "pending" for r in t["rows"] if 1 <= r["n"] <= 4),
                            "labels": t["labels"],
                            "auroc": next(r.get("auroc") for r in t["rows"] if r["n"] == 3)} for c, t in tables.items()}
        result = dict(extra, screen=screen, validity_floor=floor, validity_8_1=validity, criteria=tables,
                      candidates=cand_summary, ranked=sel["ranked"], displaced=sel["displaced"], label_counts=lc,
                      n_items=len(items), n_pooled_with_primary=len(pooled), no_finalist_reason=None if validity_ok else
                      "validity_8_1_failed_or_not_run")
        rp = write_json(a, "cls_dev_result.json", result)
        fp = write_json(a, "cls_dev_finalists.json", dict(extra, finalists=finalists[:2], displaced=sel["displaced"]))
        shas = {p.name: write_sha(p) for p in (rp, fp)}
        counts.update(n_rows=len(rows), finalists=[f["candidate"] for f in finalists],
                      killed={c: t["killed_by"] for c, t in tables.items()}, screen=screen)
        emit(dict(flags, arms_table_sha256=table_sha, n_rows=len(rows), finalists=[f["candidate"] for f in finalists[:2]],
                  killed_by={c: t["killed_by"] for c, t in tables.items()}, screen=screen, sha256=shas))
        return 0
    return logged(a, "assemble", work)


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(prog="cls_dev_run")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def base(name):
        p = sub.add_parser(name)
        p.add_argument("--cls-root", required=True)
        p.add_argument("--test-syn-root", help="default: $CLS/test-syn")
        p.add_argument("--pins-static")
        p.add_argument("--pins-run")
        p.add_argument("--selftest-result")
        p.add_argument("--out-dir", required=True)
        p.add_argument("--cls-eval", default=str(HERE / "cls_eval.py"))
        p.add_argument("--ollama-url", default=D.OLLAMA_URL)
        p.add_argument("--env-file")
        p.add_argument("--denylist")
        return p

    def with_inputs(name):
        p = base(name)
        p.add_argument("--manifest", required=True)
        p.add_argument("--items")
        p.add_argument("--syn-items")
        p.add_argument("--label-dir", action="append", help="a cls_label_run run dir (real_dev or dev_syn)")
        return p

    p = base("manifest")
    p.add_argument("--items", required=True)
    p.add_argument("--syn-items")
    p.add_argument("--legacy-labels", action="append", metavar="JUDGE=FILE",
                   help="existing Opus/Sonnet labels {text_sha, session_id, truth}, joined into the manifest")
    p.add_argument("--t0", default=D.T0_DEFAULT)
    p.add_argument("--cutoff", default=D.CUTOFF_DEFAULT)
    p.add_argument("--run-entry", default="dev_manifest", help="PINS-RUN entry name (unique)")
    p = base("scrub-check")
    p.add_argument("--result", required=True)
    p = base("folds")
    p.add_argument("--manifest", required=True)
    p = with_inputs("fit-heads")
    p.add_argument("--candidate", choices=("C1", "C2"), required=True)
    base("c4-precheck")
    with_inputs("c4-run")
    p = base("trigger-mb")
    p.add_argument("--results", required=True)
    p = with_inputs("fit-modernbert")
    p.add_argument("--trigger", required=True)
    p.add_argument("--download-log", required=True)
    p.add_argument("--cls-cache", required=True)
    p = base("download-modernbert")
    p.add_argument("--confirm-d129", action="store_true")
    p.add_argument("--revision")
    p.add_argument("--cls-cache")
    p.add_argument("--download-log", required=True)
    p = with_inputs("reference-arms")
    p.add_argument("--rules-tier", required=True)
    p.add_argument("--prompt-module", help="optional: must name the rubric.py pinned in PINS-STATIC (K-B1)")
    p.add_argument("--arm", action="append", choices=(D.ARM_KW1, D.ARM_KB1, D.ARM_ENGINE))
    p.add_argument("--engine-module", default=str(S.REPO / "src" / "llm_router" / "engine.py"))
    p = with_inputs("latency")
    p.add_argument("--candidate", required=True)
    p.add_argument("--model-artifact")
    p.add_argument("--n", type=int, default=200)
    p.add_argument("--warmups", type=int, default=5)
    p.add_argument("--summarize", action="store_true")
    p = base("stability")
    p.add_argument("--candidate", required=True)
    p.add_argument("--run-a", required=True)
    p.add_argument("--run-b", required=True)
    p.add_argument("--fit", required=True, help="cls_eval fit --out JSON (frozen thresholds)")
    p = with_inputs("assemble")
    p.add_argument("--oof", action="append", metavar="CAND=FILE")
    p.add_argument("--reference-arms", required=True)
    p.add_argument("--resemblance-label")
    p.add_argument("--boot-b", type=int, default=2000)
    return ap


HANDLERS = {"manifest": cmd_manifest, "scrub-check": cmd_scrub_check, "folds": cmd_folds, "fit-heads": cmd_fit_heads,
            "c4-precheck": cmd_c4_precheck, "c4-run": cmd_c4_run, "trigger-mb": cmd_trigger_mb,
            "fit-modernbert": cmd_fit_modernbert, "download-modernbert": cmd_download_modernbert,
            "reference-arms": cmd_reference_arms, "latency": cmd_latency, "stability": cmd_stability,
            "assemble": cmd_assemble}


def main(argv=None, *, deps: Deps | None = None) -> int:
    a = build_parser().parse_args(argv)
    deps = deps or Deps()
    try:
        return HANDLERS[a.cmd](a, deps)
    except S.CLSExit as e:
        print("refused: %s" % e, file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
