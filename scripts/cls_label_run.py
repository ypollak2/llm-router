#!/usr/bin/env python3
"""CLS-v16 relabel runner (build B-L): blinded judge labelling for PREREG-CLS-DEV section 3.

  cls_label_run.py plan    --set {real_dev,dev_syn,test_syn} --judge {gpt,sonnet,opus} ...
  cls_label_run.py run     --set ... --judge ...
  cls_label_run.py status  --run-dir D
  cls_label_run.py collect --run-dir D

Nothing here runs until PREREG-CLS-DEV is frozen. Output: counts only (stdout and a 0600 status.json).
Item text lives only in the 0600 input items file and in the stdin of a judge child (after scrub, PII
replacement and outgoing_check on the exact bytes). It is never printed, logged or put in an exception.

Judge-prompt contract = the repo's P1.7-d labeller (scripts/p17d_synthetic.py load_label_mods / judge_call /
Labeller), read from PINS-STATIC:
  * gt500.py from params.gt500_py, its sha256 equal to pins.gt500.sha256 and to cls_pins.GT500_SHA256 (as
    p17d's load_pinned); gt.RUBRIC_MD5 must equal the rubric md5. Its sibling supply.py (which gt500 can
    import: its directory goes on sys.path) is verified first against pins.supply.sha256 and
    cls_pins.SUPPLY_SHA256, as p17d_synthetic.load_label_mods does. Required: gt.load_rubric, gt.prompt_view,
    gt.valid_label, gt.extract_json (a gt500 without prompt_view is refused: a raw prompt is never sent).
  * rubric.py from params.rubric_py, its md5 equal to pins.rubric.md5 and cls_pins.RUBRIC_MD5 and its sha256
    equal to pins.rubric.sha256, loaded through gt.load_rubric(path). `--prompt-module`, when given, must name
    that same pinned file (anything else exits 9).
  * rub.PLANTED = list of {id, expect, context, prompt} (the 6 cal-* items); rub.ITEM_TEMPLATE.format(id=,
    context=, prompt=gt.prompt_view(prompt)); items joined by a blank line, then a blank line and
    rub.BATCH_TAIL (or rub.SINGLE_TAIL) VERBATIM (no format_map).
  * RUBRIC placement: P17D passed rub.RUBRIC as `--system-prompt`; the frozen judge argv (PREREG 3.2, pinned
    help lines) has no such flag and codex has none, so rub.RUBRIC is the FIRST block of the stdin payload
    (RUBRIC, a blank line, then the items). It therefore also passes outgoing_check with the items.
  * Answers: gt.extract_json(text); a batch answer is {"labels": [{"id", "tier", ...}]} (ids required); a
    single answer is one label object (or {"labels": [one]}): id-less, it takes the one asked id (the P1.7-d
    rule, D-161); with an id, that id must be the asked one. A label is kept only if gt.valid_label(label,
    rub) and its tier is haiku/sonnet/opus.
Failure classes: a wrong plant or a model change (claude family differs, id changes mid-run) VOIDS the judge
run (PREREG 3.3). A codex non-zero rc (bad -m, timeout, CLI error) and a claude answer with no model id STOP
and PARK the run (D-147): never a void, never a TRUTH_FALLBACK.
Ledger: every call writes a `pending` row (calls_<judge>.jsonl; for Opus also the shared spend log) BEFORE the
child starts and a `done` row after it; a timeout is a recorded failure (Opus: charged at the estimate).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import cls_pins as P  # noqa: E402
import cls_send_lib as S  # noqa: E402

SETS = ("real_dev", "dev_syn", "test_syn")
JUDGES = ("gpt", "sonnet", "opus")
TIERS = ("haiku", "sonnet", "opus")
ORDER_PREFIX = "clsdev:label:20261017:"
BATCH_SIZE = 8
INTERLEAVE_EVERY = 10
MAX_BATCH_TRIES = 3
PLANT_IDS = ["cal-h1", "cal-h2", "cal-s1", "cal-s2", "cal-o1", "cal-o2"]
FIRST_CALL_REAL = 2
PROVIDER = {"gpt": "codex", "sonnet": "claude", "opus": "claude"}

VERIFY_PINS = P.verify_at_start  # patched in tests; every command calls it first


class Void(Exception):
    def __init__(self, reason: str, code: int):
        super().__init__(reason)
        self.reason = reason
        self.code = code


def sha_order(item_id: str) -> str:
    return hashlib.sha256((ORDER_PREFIX + item_id).encode()).hexdigest()


# ---------------------------------------------------------------------------------------------
# The pinned judge-prompt modules (gt500.py + rubric.py)
# ---------------------------------------------------------------------------------------------

class JudgeMods:
    """The pinned gt500 module and the rubric module it loaded, plus their identifying hashes."""

    def __init__(self, gt, rub, rubric_md5: str, gt500_sha256: str):
        self.gt, self.rub, self.rubric_md5, self.gt500_sha256 = gt, rub, rubric_md5, gt500_sha256


def _static_doc(pins_static) -> dict:
    try:
        return json.loads(Path(pins_static).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        raise S.CLSExit(S.EXIT_PINS, "PINS-STATIC not readable") from None


def resolve_rubric(prompt_module, doc: dict) -> tuple[Path, str]:
    """The rubric.py path to load and its md5, after the pin checks. Refuses (exit 9) a --prompt-module that is
    not the pinned file, and a pinned file whose md5/sha256 differ from PINS-STATIC or the prereg md5."""
    params, pins = doc.get("params", {}), doc.get("pins", {})
    want = pins.get("rubric") or {}
    if not params.get("rubric_py") or not want.get("md5") or not want.get("sha256"):
        raise S.CLSExit(S.EXIT_PINS, "PINS-STATIC lacks the rubric path or its md5/sha256")
    pinned = Path(params["rubric_py"])
    if prompt_module and os.path.realpath(str(prompt_module)) != os.path.realpath(str(pinned)):
        raise S.CLSExit(S.EXIT_PINS, "--prompt-module is not the pinned rubric.py")
    try:
        data = pinned.read_bytes()
    except OSError:
        raise S.CLSExit(S.EXIT_PINS, "pinned rubric.py not readable") from None
    md5 = hashlib.md5(data).hexdigest()  # noqa: S324 (identification: the prereg pins the rubric by md5)
    if md5 != want["md5"] or md5 != P.RUBRIC_MD5 or S.sha256_bytes(data) != want["sha256"]:
        raise S.CLSExit(S.EXIT_PINS, "rubric.py md5/sha256 differ from PINS-STATIC")
    return pinned, md5


def check_supply(doc: dict, gt500_path) -> None:
    """supply.py next to gt500.py, pinned by sha256 (PINS-STATIC and the prereg value), verified BEFORE gt500
    is loaded: gt500's directory goes on sys.path, so an unpinned sibling would run unchecked (exit 9)."""
    want = (doc.get("pins", {}).get("supply") or {}).get("sha256")
    if not want:
        raise S.CLSExit(S.EXIT_PINS, "PINS-STATIC lacks the supply.py sha256")
    path = P.supply_path(gt500_path)
    try:
        sha = S.sha256_file(path)
    except OSError:
        raise S.CLSExit(S.EXIT_PINS, "pinned supply.py not readable") from None
    if sha != want or sha != P.SUPPLY_SHA256:
        raise S.CLSExit(S.EXIT_PINS, "supply.py sha256 differs from PINS-STATIC")


def _load_gt500(doc: dict):
    params, pins = doc.get("params", {}), doc.get("pins", {})
    want = (pins.get("gt500") or {}).get("sha256")
    path = params.get("gt500_py")
    if not path or not want:
        raise S.CLSExit(S.EXIT_PINS, "PINS-STATIC lacks the gt500 path or its sha256")
    check_supply(doc, path)
    try:
        data = Path(path).read_bytes()
    except OSError:
        raise S.CLSExit(S.EXIT_PINS, "pinned gt500.py not readable") from None
    sha = S.sha256_bytes(data)
    if sha != want or sha != P.GT500_SHA256:
        raise S.CLSExit(S.EXIT_PINS, "gt500.py sha256 differs from PINS-STATIC")
    # As p17d_attach.load_pinned: no bytecode, the helper's own directory importable for its siblings. The
    # verified bytes are what is executed (no second read of the file).
    sys.dont_write_bytecode = True
    parent = str(Path(path).resolve().parent)
    if parent not in sys.path:
        sys.path.append(parent)
    mod = types.ModuleType("cls_gt500")
    mod.__file__ = str(path)
    sys.modules["cls_gt500"] = mod
    exec(compile(data, str(path), "exec"), mod.__dict__)  # noqa: S102 (pinned by sha256 just above)
    return mod, sha


def load_judge_mods(pins_static, prompt_module=None) -> JudgeMods:
    """gt500.py by its pinned sha, rubric.py from its pinned path with the md5 check, through gt.load_rubric
    (the P1.7-d contract). Any missing name refuses before anything is sent."""
    doc = _static_doc(pins_static)
    rub_path, md5 = resolve_rubric(prompt_module, doc)
    gt, gsha = _load_gt500(doc)
    for name in ("load_rubric", "prompt_view", "valid_label", "extract_json"):
        if not callable(getattr(gt, name, None)):
            raise S.CLSExit(S.EXIT_PINS, "gt500 lacks %s" % name)
    if getattr(gt, "RUBRIC_MD5", None) != md5:
        raise S.CLSExit(S.EXIT_PINS, "gt500 RUBRIC_MD5 differs from the pinned rubric md5")
    rub = gt.load_rubric(rub_path)
    for name in ("RUBRIC", "ITEM_TEMPLATE", "BATCH_TAIL", "SINGLE_TAIL"):
        if not isinstance(getattr(rub, name, None), str):
            raise S.CLSExit(S.EXIT_REFUSE, "rubric lacks %s" % name)
    planted = getattr(rub, "PLANTED", None)
    if not isinstance(planted, list) or not all(isinstance(p, dict) and {"id", "expect", "context", "prompt"} <= set(p)
                                                 for p in planted):
        raise S.CLSExit(S.EXIT_REFUSE, "rubric PLANTED must be a list of {id, expect, context, prompt}")
    if sorted(p["id"] for p in planted) != sorted(PLANT_IDS) or any(p["expect"] not in TIERS for p in planted):
        raise S.CLSExit(S.EXIT_REFUSE, "rubric must define exactly the 6 planted items")
    return JudgeMods(gt, rub, md5, gsha)


# ---------------------------------------------------------------------------------------------
# Items, plan, payload, answers
# ---------------------------------------------------------------------------------------------

def read_items(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if line.strip():
            rec = json.loads(line)
            out[rec["item_id"]] = rec
    return out


def manifest_truth(manifest_path) -> dict:
    """item_id -> {judge: tier|None} from the manifest (key `truth`, or `truth_tiers`)."""
    doc = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    out = {}
    for it in doc.get("items", []):
        out[it["item_id"]] = it.get("truth") or it.get("truth_tiers") or {}
    return out


def select_ids(set_name: str, judge: str, items: dict, truth: dict | None) -> list[str]:
    """PREREG 3.1: real_dev gpt = all manifest items; sonnet = manifest items lacking an existing
    Sonnet label; opus = none. dev_syn / test_syn: every judge labels every item."""
    if set_name == "real_dev":
        if truth is None:
            raise S.CLSExit(S.EXIT_REFUSE, "real_dev needs --manifest")
        ids = [i for i in items if i in truth]
        if judge == "gpt":
            return ids
        if judge == "sonnet":
            return [i for i in ids if not truth[i].get("sonnet")]
        return []
    return list(items)


def prepare(ids, items: dict, ctx) -> tuple[list[dict], dict]:
    """Detector, then digest cap and PII replacement. Items with any detector hit never reach the plan."""
    kept, excluded = [], {}
    for i in ids:
        rec = items[i]
        text = rec.get("text", "")
        digest = (rec.get("digest") or "")[:S.DIGEST_CAP]
        hits = sorted(set(S.scrub_detect(text, ctx) + (S.scrub_detect(digest, ctx) if digest else [])))
        if hits:
            for h in hits:
                excluded[h] = excluded.get(h, 0) + 1
            continue
        kept.append({"item_id": i, "ctx": S.pii_replace(digest), "prompt": S.pii_replace(text)})
    return kept, excluded


def retro_scan(kept: list[dict]) -> tuple[list[dict], list[str]]:
    """Independent scanner over the FULL sent set: a hit excludes the item retroactively (counted)."""
    ok, removed = [], []
    for e in kept:
        if S.independent_scan(e["ctx"] + "\n" + e["prompt"]):
            removed.append(e["item_id"])
        else:
            ok.append(e)
    return ok, removed


def build_plan(set_name: str, judge: str, kept: list[dict]) -> dict:
    """Deterministic: order by sha256, first call = 2 real items, then batches of 8, opaque ids from a
    seeded permutation shared by real items and (later) plants."""
    order = [e["item_id"] for e in sorted(kept, key=lambda e: sha_order(e["item_id"]))]
    n = len(order)
    n_batches = max(0, (max(0, n - FIRST_CALL_REAL) + BATCH_SIZE - 1) // BATCH_SIZE)
    pool_n = n + 6 + 2 * (n_batches // INTERLEAVE_EVERY + 1) + 64
    universe = list(range(1, pool_n + 1))
    random.Random("%d:ids:%s:%s" % (S.SEED, set_name, judge)).shuffle(universe)
    ids = {iid: "L%03d" % universe[k] for k, iid in enumerate(order)}
    plant_pool = ["L%03d" % u for u in universe[n:]]
    first = order[:FIRST_CALL_REAL]
    rest = order[FIRST_CALL_REAL:]
    batches = [rest[k:k + BATCH_SIZE] for k in range(0, len(rest), BATCH_SIZE)]
    return {"set": set_name, "judge": judge, "order": order, "ids": ids, "plant_pool": plant_pool,
            "first_call_real": first, "batches": batches}


def render_item(mods: JudgeMods, opaque: str, ctx: str, prompt: str) -> str:
    """One item block: rub.ITEM_TEMPLATE with the P17D field names; the prompt always in gt.prompt_view form."""
    return mods.rub.ITEM_TEMPLATE.format(id=opaque, context=ctx, prompt=mods.gt.prompt_view(prompt))


def build_payload(mods: JudgeMods, entries: list[dict], *, single: bool) -> bytes:
    """The final bytes: rub.RUBRIC, a blank line, the item blocks joined by a blank line, a blank line and the
    tail verbatim. This is what outgoing_check sees."""
    tail = mods.rub.SINGLE_TAIL if single else mods.rub.BATCH_TAIL
    user = "\n\n".join(render_item(mods, e["opaque"], e["ctx"], e["prompt"]) for e in entries) + "\n\n" + tail
    return (mods.rub.RUBRIC + "\n\n" + user).encode("utf-8")


def parse_labels(mods: JudgeMods, text: str, ids: list[str], *, single: bool) -> dict:
    """opaque id -> label dict, for labels that gt.valid_label accepts and whose tier is a known tier.
    Batch answers must carry an id per label. A single answer follows the P1.7-d rule (p17d_synthetic.py
    judge_call, `labs = {ids[0]: obj}`, D-161): when exactly one id was asked, an id-less answer gets that id
    attached; an answer that does carry an id must carry the asked one."""
    try:
        obj = mods.gt.extract_json(text)
    except Exception:  # noqa: BLE001 - an unparsable answer labels nothing; the batch rules retry
        return {}
    rows = obj.get("labels") if isinstance(obj, dict) else None
    if single and not isinstance(rows, list):
        rows = [obj] if isinstance(obj, dict) else None
    if not isinstance(rows, list):
        return {}
    if single and len(ids) == 1 and len(rows) == 1 and isinstance(rows[0], dict) and rows[0].get("id") is None:
        rows = [dict(rows[0], id=ids[0])]
    out = {}
    for lab in rows:
        if not isinstance(lab, dict) or lab.get("id") is None or str(lab["id"]) not in ids:
            continue
        try:
            ok = bool(mods.gt.valid_label(lab, mods.rub))
        except Exception:  # noqa: BLE001 - a label the validator cannot judge is not valid
            ok = False
        if ok and lab.get("tier") in TIERS:
            out[str(lab["id"])] = lab
    return out


def check_model_id(judge: str, ids: list[str], pinned_id: str | None, recorded_id: str | None) -> tuple[bool, str]:
    """claude: every reported model id is of the judge's family and equals the id seen earlier in this run.
    codex: the served id (the -m pin, see S.served_model_ids) equals the pin. An empty list is
    `no_model_id_reported` (a park, see classify_call); a differing family or a mid-run change is a void."""
    if not ids:
        return False, "no_model_id_reported"
    if judge == "gpt":
        if ids != [pinned_id]:
            return False, "gpt_model_id_differs_from_pin"
    elif any(S.family_of(i) != judge for i in ids):
        return False, "model_family_differs"
    if recorded_id and ids != [recorded_id]:
        return False, "model_id_changed_mid_run"
    return True, ""


def classify_call(judge: str, rc: int, timed_out: bool, id_ok: bool, id_reason: str) -> str:
    """ok | retry | park | void for one finished call (D-147).
    codex: any non-zero rc (an unknown -m id exits 1; a timeout) -> park.
    claude: rc 0 with no model id -> park; rc 0 with a wrong family / mid-run change -> void; a non-zero rc or a
    timeout -> retry under the batch rules (recorded first)."""
    if judge == "gpt":
        if rc != 0 or timed_out:
            return "park"
        return "ok" if id_ok else "park"
    if rc != 0 or timed_out:
        return "retry"
    if id_ok:
        return "ok"
    return "park" if id_reason == "no_model_id_reported" else "void"


class Status:
    """status.json (0600, counts only). One run-dir = one set."""

    def __init__(self, run_dir, set_name):
        self.path = Path(run_dir) / "status.json"
        self.set = set_name
        if self.path.exists():
            self.doc = json.loads(self.path.read_text(encoding="utf-8"))
            if self.doc.get("set") != set_name:
                raise S.CLSExit(S.EXIT_REFUSE, "run-dir belongs to another set")
        else:
            self.doc = {"set": set_name, "judges": {}}

    def judge(self, j) -> dict:
        return self.doc["judges"].setdefault(j, {"state": "new", "calls": 0, "batches_done": 0, "label_errors": 0,
                                                 "scrub_abort": 0, "retro_exclusions": 0, "excluded_detector": {},
                                                 "plants": {"asked": 0, "correct": 0, "wrong": 0}, "cwd_leftover": 0,
                                                 "timeouts": 0})

    def save(self):
        voided = sorted(j for j, v in self.doc["judges"].items() if v.get("state") == "voided")
        self.doc["voided"] = voided
        self.doc.pop("truth_fallback", None)
        self.doc.pop("label", None)
        if len(voided) == 1:
            self.doc["truth_fallback"] = voided[0]
        elif len(voided) >= 2:
            self.doc["label"] = "NOT_INFORMATIVE"
        S.write_private(self.path, json.dumps(self.doc, sort_keys=True, indent=1) + "\n")


class Runner:
    def __init__(self, a, judge: str):
        self.a = a
        self.judge = judge
        self.run_dir = Path(a.run_dir)
        self.status = Status(a.run_dir, a.set)
        self.js = self.status.judge(judge)
        self.labels_path = self.run_dir / ("labels_%s.jsonl" % judge)
        self.plants_path = self.run_dir / ("plants_%s.jsonl" % judge)
        self.calls_path = self.run_dir / ("calls_%s.jsonl" % judge)
        self.excl_path = self.run_dir / ("excluded_%s.jsonl" % judge)
        self.quar_path = self.run_dir / ("quarantine_%s.jsonl" % judge)
        self.state_path = self.run_dir / ("state_%s.json" % judge)
        self.plan_path = self.run_dir / ("plan_%s.json" % judge)
        self.pinned = S_load_model_ids(a.pins_static)
        self.pinned_id = self.pinned.get("judge_" + judge)
        if not self.pinned_id:
            raise S.CLSExit(S.EXIT_PINS, "PINS-STATIC has no model id for judge_%s" % judge)
        self.spend = S.SpendLog(a.spend_log or (Path(a.cls_root) / "spend_log.jsonl"), a.cap_usd)

    # -- inputs ------------------------------------------------------------------------------
    def load(self):
        a = self.a
        if a.set == "test_syn":
            text = S.read_guarded_text(a.items, a.cls_root, release_token=a.release_token, allow_test_syn=True)
        else:
            for p in (a.items, a.manifest, a.run_dir):
                if p:
                    S.guard_dev_path(p, a.cls_root)
            text = S.read_guarded_text(a.items, a.cls_root)
        items = read_items(text)
        truth = manifest_truth(a.manifest) if a.manifest else None
        self.mods = load_judge_mods(a.pins_static, a.prompt_module)
        self.ctx = S.ScrubContext(S.load_denylist(a.denylist), S.load_env_values(a.env_file)[0])
        S.configure(self.ctx)  # detector (d) (denylist + .env values) also applies inside outgoing_check at send
        return items, truth

    def make_plan(self, items, truth, *, write=True):
        ids = select_ids(self.a.set, self.judge, items, truth)
        kept, excluded = prepare(ids, items, self.ctx)
        kept, removed = retro_scan(kept)
        already = {r["item_id"] for r in S.read_jsonl(self.excl_path)}
        for r in removed:
            if r not in already:
                S.append_jsonl(self.excl_path, {"item_id": r, "reason": "independent_scan", "utc": S.utc_now()})
                self.js["retro_exclusions"] += 1
        for k, v in excluded.items():
            self.js["excluded_detector"][k] = v
        plan = build_plan(self.a.set, self.judge, kept)
        self.entries = {e["item_id"]: e for e in kept}
        self.plan = plan
        if write:
            S.write_private(self.plan_path, json.dumps(plan, sort_keys=True) + "\n")
        return plan

    # -- calls -------------------------------------------------------------------------------
    def _n_calls(self) -> int:
        return sum(1 for r in S.read_jsonl(self.calls_path) if r.get("phase") == "pending")

    def _log_call(self, **rec):
        rec.update({"utc": S.utc_now(), "judge": self.judge})
        S.append_jsonl(self.calls_path, rec)

    def _send(self, payload: bytes, batch_id: str):
        a = self.a
        res, leftover = S.child_call(PROVIDER[self.judge], self.pinned_id, payload, self.run_dir, claude_bin=a.claude_bin,
                                     codex_bin=a.codex_bin, timeout=a.timeout, batch_id=batch_id)
        self.js["cwd_leftover"] += leftover
        return res

    def ask(self, entries, *, single, batch_no, attempt, kind) -> dict | None:
        """One call. Returns {labels, tiers, call_id, model_id, usd} on a complete parse, None on a failed call
        (retried by the caller). ScrubAbort propagates (caller counts it). A model change raises Void; a codex
        failure or a missing claude model id raises CLSExit (park)."""
        payload = build_payload(self.mods, entries, single=single)
        call_id = "%s-%06d" % (self.judge, self._n_calls() + 1)
        priced = self.judge == "opus"
        est = 0.0
        if priced:
            self.spend.guard()
            est = self.spend.estimate()
            self.spend.pending(call_id, self.pinned_id, PROVIDER[self.judge], est)
        base = {"call_id": call_id, "batch": batch_no, "attempt": attempt, "kind": kind, "n_items": len(entries)}
        self._log_call(phase="pending", **base)
        try:
            res = self._send(payload, call_id)
        except S.ScrubAbort:
            self._log_call(phase="done", outcome="scrub_abort", rc=None, model_ids=[], usd=0.0, **base)
            if priced:
                self.spend.record(self.pinned_id, PROVIDER[self.judge], 0, 0.0, call_id=call_id, cost_basis="not_sent")
            raise
        except OSError:
            self._log_call(phase="done", outcome="not_started", rc=None, model_ids=[], usd=0.0, **base)
            if priced:
                self.spend.record(self.pinned_id, PROVIDER[self.judge], 0, 0.0, call_id=call_id, cost_basis="not_sent")
            raise S.CLSExit(S.EXIT_REFUSE, "judge child could not be started") from None
        self.js["calls"] += 1
        parsed = (S.parse_codex_jsonl if self.judge == "gpt" else S.parse_claude_json)(res.stdout)
        usd = 0.0
        if priced:
            usd = est if res.timed_out else float(parsed.get("cost_usd", 0.0) or 0.0)
            self.spend.record(self.pinned_id, PROVIDER[self.judge], parsed.get("tokens", 0), usd, call_id=call_id,
                              cost_basis="estimate" if res.timed_out else "reported")
        if res.timed_out:
            self.js["timeouts"] += 1
        ids = S.served_model_ids(PROVIDER[self.judge], parsed, self.pinned_id)
        ok, reason = check_model_id(self.judge, ids, self.pinned_id, self.js.get("model_id"))
        verdict = classify_call(self.judge, res.returncode, res.timed_out, ok, reason)
        self._log_call(phase="done", outcome="timeout" if res.timed_out else verdict, rc=res.returncode,
                       model_ids=ids, usd=usd, **base)
        if verdict == "void":
            raise Void(reason, S.EXIT_MODEL_ID)
        if verdict == "park":
            if self.judge == "gpt":
                raise S.CLSExit(S.EXIT_GPT_PARK, "codex call failed (rc=%d%s); parked, not voided" % (
                    res.returncode, ", timeout" if res.timed_out else ""))
            raise S.CLSExit(S.EXIT_MODEL_ID, "%s; parked, not voided" % reason)
        if verdict == "retry" or not parsed.get("ok"):
            return None
        labels = parse_labels(self.mods, parsed["text"], [e["opaque"] for e in entries], single=single)
        if any(e["opaque"] not in labels for e in entries):
            return None
        self.js["model_id"] = ids[0]
        return {"labels": labels, "tiers": {k: v["tier"] for k, v in labels.items()}, "call_id": call_id,
                "model_id": ids[0], "usd": usd}

    # -- plants ------------------------------------------------------------------------------
    def _cursor(self) -> int:
        return json.loads(self.state_path.read_text())["plant_cursor"] if self.state_path.exists() else 0

    def take_plants(self, plant_ids: list[str]) -> list[dict]:
        cur = self._cursor()
        pool = self.plan["plant_pool"]
        if cur + len(plant_ids) > len(pool):
            raise S.CLSExit(S.EXIT_REFUSE, "opaque id pool for plants exhausted")
        S.write_private(self.state_path, json.dumps({"plant_cursor": cur + len(plant_ids)}) + "\n")
        planted = {p["id"]: p for p in self.mods.rub.PLANTED}
        out = []
        for k, pid in enumerate(plant_ids):
            pl = planted[pid]
            out.append({"opaque": pool[cur + k], "plant_id": pid, "plant": True, "expected": pl["expect"],
                        "ctx": S.pii_replace(pl["context"] or ""), "prompt": S.pii_replace(pl["prompt"] or "")})
        return out

    def interleave_pair(self) -> list[str]:
        k = sum(1 for r in S.read_jsonl(self.plants_path) if r["interleaved"]) // 2
        return [PLANT_IDS[(2 * k) % 6], PLANT_IDS[(2 * k + 1) % 6]]

    def record_plants(self, plants, entries_order, out, interleaved) -> bool:
        wrong = False
        for pl in plants:
            got = out["tiers"].get(pl["opaque"])
            S.append_jsonl(self.plants_path, {
                "plant_id": pl["plant_id"], "opaque_id": pl["opaque"], "expected": pl["expected"], "got": got,
                "call_id": out["call_id"], "position": [e["opaque"] for e in entries_order].index(pl["opaque"]),
                "interleaved": interleaved, "utc": S.utc_now()})
            self.js["plants"]["asked"] += 1
            if got == pl["expected"]:
                self.js["plants"]["correct"] += 1
            else:
                self.js["plants"]["wrong"] += 1
                wrong = True
        return wrong

    def void(self, reason: str, code: int):
        """Terminal record; the judge's labels are quarantined (never joined)."""
        if self.labels_path.exists():
            moved = self.labels_path.read_bytes()
            fd = os.open(str(self.quar_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, moved)
            finally:
                os.close(fd)
            os.unlink(self.labels_path)
        self.js.update({"state": "voided", "reason": reason, "code": code, "utc": S.utc_now()})

    def write_labels(self, entries, out, batch_no, attempt):
        for e in entries:
            if e.get("plant"):
                continue
            S.append_jsonl(self.labels_path, {
                "item_id": e["item_id"], "judge": self.judge, "tier": out["tiers"][e["opaque"]], "batch": batch_no,
                "call_id": out["call_id"], "model_id": out["model_id"], "utc": S.utc_now(), "attempt": attempt})

    def real_entry(self, item_id):
        e = self.entries[item_id]
        return {"item_id": item_id, "opaque": self.plan["ids"][item_id], "ctx": e["ctx"], "prompt": e["prompt"]}

    # -- the run -----------------------------------------------------------------------------
    def calibration(self):
        reals = [self.real_entry(i) for i in self.plan["first_call_real"]]
        plants = self.take_plants(PLANT_IDS)
        call_entries = reals + plants
        random.Random(S.SEED).shuffle(call_entries)
        for attempt in range(1, MAX_BATCH_TRIES + 1):
            try:
                out = self.ask(call_entries, single=False, batch_no=0, attempt=attempt, kind="calibration")
            except S.ScrubAbort:
                self.js["scrub_abort"] += 1
                break
            if out is None:
                continue
            if self.record_plants(plants, call_entries, out, False):
                raise Void("first_call_plant_wrong", S.EXIT_PLANT_VOID)
            self.write_labels(call_entries, out, 0, attempt)
            return
        raise Void("calibration_call_failed", S.EXIT_PLANT_VOID)

    def process_batch(self, bno, todo, inter):
        reals = [self.real_entry(i) for i in todo]
        plants = self.take_plants(self.interleave_pair()) if inter else []
        call_entries = reals + plants
        random.Random("%d:%s:%d" % (S.SEED, self.judge, bno)).shuffle(call_entries)
        for attempt in range(1, MAX_BATCH_TRIES + 1):
            try:
                out = self.ask(call_entries, single=False, batch_no=bno, attempt=attempt, kind="batch")
            except S.ScrubAbort:
                self.js["scrub_abort"] += 1
                break  # the same bytes would abort again; isolate with singles
            if out is None:
                continue
            if plants and self.record_plants(plants, call_entries, out, True):
                raise Void("interleaved_plant_wrong", S.EXIT_PLANT_VOID)
            self.write_labels(call_entries, out, bno, attempt)
            return
        for e in reals:
            try:
                out = self.ask([e], single=True, batch_no=bno, attempt=MAX_BATCH_TRIES + 1, kind="single")
            except S.ScrubAbort:
                self.js["scrub_abort"] += 1
                continue
            if out is None:
                self.js["label_errors"] += 1
                continue
            self.write_labels([e], out, bno, MAX_BATCH_TRIES + 1)

    def run(self, items, truth) -> int:
        if self.js.get("state") == "voided":
            raise S.CLSExit(self.js.get("code", S.EXIT_PLANT_VOID), "judge run is voided (terminal)")
        plan = self.make_plan(items, truth)
        self.js["state"] = "running"
        self.js.pop("stop_reason", None)
        code = S.EXIT_OK
        try:
            if not plan["order"]:
                self.js["state"] = "done"
                return code
            if self.judge == "gpt":
                self.canary()
            labelled = {r["item_id"] for r in S.read_jsonl(self.labels_path)}
            prev = S.read_jsonl(self.plants_path)
            first_done = sum(1 for r in prev if not r["interleaved"]) >= 6
            resumed = bool(prev) or bool(labelled) or self._n_calls() > 0
            if not first_done:
                self.calibration()
            labelled = {r["item_id"] for r in S.read_jsonl(self.labels_path)}
            first_of_run = True
            processed, paused = 0, False
            for bno, batch in enumerate(plan["batches"], start=1):
                todo = [i for i in batch if i not in labelled]
                if not todo:
                    continue
                inter = (bno % INTERLEAVE_EVERY == 0) or (resumed and first_of_run)
                first_of_run = False
                self.process_batch(bno, todo, inter)
                self.js["batches_done"] += 1
                processed += 1
                labelled |= {r["item_id"] for r in S.read_jsonl(self.labels_path)}
                if self.a.max_batches and processed >= self.a.max_batches:
                    paused = True
                    break
            self.js["state"] = "paused" if paused else "done"
        except Void as v:
            self.void(v.reason, v.code)
            code = v.code
        except S.CLSExit as e:
            self.js["state"] = "stopped"
            self.js["stop_reason"] = str(e)[:120]
            code = e.code
        finally:
            self.status.save()
        return code

    def canary(self):
        S.run_codex_canary(self.pinned_id, self.run_dir / "canary", codex_bin=self.a.codex_bin, timeout=self.a.timeout,
                           log=lambda m: self.js.__setitem__("canary", m))


def S_load_model_ids(pins_static) -> dict:
    try:
        return json.loads(Path(pins_static).read_text(encoding="utf-8"))["pins"]["model_ids"]
    except (OSError, ValueError, KeyError, TypeError):
        raise S.CLSExit(S.EXIT_PINS, "model_ids not readable from PINS-STATIC") from None


def build_parser():
    ap = argparse.ArgumentParser(prog="cls_label_run")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "run", "status", "collect"):
        p = sub.add_parser(name)
        p.add_argument("--run-dir", required=True)
        p.add_argument("--cls-root")
        p.add_argument("--pins-static")
        p.add_argument("--pins-run", default=None)
        p.add_argument("--selftest-result")
        if name in ("plan", "run"):
            p.add_argument("--set", required=True, choices=SETS)
            p.add_argument("--judge", required=True, choices=JUDGES)
            p.add_argument("--items", required=True)
            p.add_argument("--manifest")
            p.add_argument("--prompt-module", default=None,
                           help="optional; must be the pinned rubric.py (PINS-STATIC params.rubric_py), else exit 9")
            p.add_argument("--denylist")
            p.add_argument("--env-file")
            p.add_argument("--release-token")
            p.add_argument("--cap-usd", type=float, default=20.0)
            p.add_argument("--spend-log")
            p.add_argument("--claude-bin", default="claude")
            p.add_argument("--codex-bin", default="codex")
            p.add_argument("--timeout", type=int, default=S.CLAUDE_TIMEOUT_S)
            p.add_argument("--max-batches", type=int, default=0,
                           help="process at most N batches, then pause (resumable; 0 = all)")
    return ap


def _start(a):
    VERIFY_PINS(a.pins_static, a.pins_run, check_ollama=False)
    S.require_fresh_selftest(a.selftest_result)
    return S.env_sha_at_use(getattr(a, "env_file", None))


def cmd_plan(a) -> int:
    env_sha = _start(a)
    r = Runner(a, a.judge)
    items, truth = r.load()
    plan = r.make_plan(items, truth)
    S.outgoing_check(r.mods.rub.RUBRIC.encode("utf-8"), batch_id="rubric")  # the template itself must be clean
    r.status.save()
    print(json.dumps({"set": a.set, "judge": a.judge, "n_items": len(plan["order"]),
                      "n_batches": len(plan["batches"]), "excluded_detector": r.js["excluded_detector"],
                      "retro_exclusions": r.js["retro_exclusions"], "env_sha_at_use": env_sha}, sort_keys=True))
    return S.EXIT_OK


def cmd_run(a) -> int:
    env_sha = _start(a)
    r = Runner(a, a.judge)
    items, truth = r.load()
    S.outgoing_check(r.mods.rub.RUBRIC.encode("utf-8"), batch_id="rubric")
    code = r.run(items, truth)
    js = r.js
    print(json.dumps({"set": a.set, "judge": a.judge, "state": js["state"], "calls": js["calls"],
                      "batches": js["batches_done"], "label_errors": js["label_errors"],
                      "scrub_abort": js["scrub_abort"], "retro_exclusions": js["retro_exclusions"],
                      "plants": js["plants"], "timeouts": js.get("timeouts", 0), "env_sha_at_use": env_sha,
                      "exit": code}, sort_keys=True))
    return code


def cmd_status(a) -> int:
    VERIFY_PINS(a.pins_static, a.pins_run, check_ollama=False)
    p = Path(a.run_dir) / "status.json"
    print(p.read_text(encoding="utf-8") if p.exists() else "{}")
    return S.EXIT_OK


def cmd_collect(a) -> int:
    VERIFY_PINS(a.pins_static, a.pins_run, check_ollama=False)
    run_dir = Path(a.run_dir)
    doc = json.loads((run_dir / "status.json").read_text(encoding="utf-8")) if (run_dir / "status.json").exists() else {}
    out = {"set": doc.get("set"), "judges": {}}
    for j in JUDGES:
        js = doc.get("judges", {}).get(j)
        if not js:
            continue
        excl = {r["item_id"] for r in S.read_jsonl(run_dir / ("excluded_%s.jsonl" % j))}
        labels = [r for r in S.read_jsonl(run_dir / ("labels_%s.jsonl" % j)) if r["item_id"] not in excl]
        out["judges"][j] = {"state": js.get("state"), "n_labels": len(labels), "label_errors": js.get("label_errors", 0),
                            "scrub_abort": js.get("scrub_abort", 0), "retro_exclusions": js.get("retro_exclusions", 0),
                            "plants": js.get("plants")}
    out["voided"] = doc.get("voided", [])
    if doc.get("label") == "NOT_INFORMATIVE":
        out["label"] = "NOT_INFORMATIVE"
        print(json.dumps(out, sort_keys=True))
        return S.EXIT_TWO_VOIDS
    if doc.get("truth_fallback"):
        out["truth_fallback"] = doc["truth_fallback"]
    print(json.dumps(out, sort_keys=True))
    return S.EXIT_OK


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    try:
        if a.cmd in ("plan", "run") and not a.cls_root:
            raise S.CLSExit(S.EXIT_REFUSE, "--cls-root is required")
        return {"plan": cmd_plan, "run": cmd_run, "status": cmd_status, "collect": cmd_collect}[a.cmd](a)
    except S.CLSExit as e:
        print("refused: %s" % e, file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
