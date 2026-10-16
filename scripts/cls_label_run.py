#!/usr/bin/env python3
"""CLS-v16 relabel runner (build B-L): blinded judge labelling for PREREG-CLS-DEV section 3.

  cls_label_run.py plan    --set {real_dev,dev_syn,test_syn} --judge {gpt,sonnet,opus} ...
  cls_label_run.py run     --set ... --judge ...
  cls_label_run.py status  --run-dir D
  cls_label_run.py collect --run-dir D

Nothing here runs until PREREG-CLS-DEV is frozen. Output: counts only (stdout and a 0600 status.json).
Item text lives only in the 0600 input items file and in the stdin of a judge child (after scrub, PII
replacement and outgoing_check on the exact bytes). It is never printed, logged or put in an exception.

Prompt contract (cls_label_run reads it from the module named by --prompt-module, normally the pinned
rubric.py): RUBRIC (str), ITEM_TEMPLATE, BATCH_TAIL, SINGLE_TAIL (format strings; fields id, ctx, prompt),
PLANTS ({plant_id: {text, digest, expected}}), optional prompt_view(str).
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import random
import re
import sys
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

VERIFY_PINS = P.verify_at_start  # patched in tests; every command calls it first


class Void(Exception):
    def __init__(self, reason: str, code: int):
        super().__init__(reason)
        self.reason = reason
        self.code = code


class _Safe(dict):
    def __missing__(self, key):
        return ""


def sha_order(item_id: str) -> str:
    return hashlib.sha256((ORDER_PREFIX + item_id).encode()).hexdigest()


def load_prompt_module(path):
    spec = importlib.util.spec_from_file_location("cls_prompt_module", str(path))
    if spec is None or spec.loader is None:
        raise S.CLSExit(S.EXIT_REFUSE, "prompt module not loadable")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in ("RUBRIC", "ITEM_TEMPLATE", "BATCH_TAIL", "SINGLE_TAIL", "PLANTS"):
        if not hasattr(mod, name):
            raise S.CLSExit(S.EXIT_REFUSE, "prompt module lacks %s" % name)
    plants = mod.PLANTS
    if sorted(plants) != sorted(PLANT_IDS):
        raise S.CLSExit(S.EXIT_REFUSE, "prompt module must define exactly the 6 planted items")
    return mod


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


def render_item(mod, opaque: str, ctx: str, prompt: str) -> str:
    view = getattr(mod, "prompt_view", None)
    p = view(prompt) if callable(view) else prompt
    return mod.ITEM_TEMPLATE.format_map(_Safe(id=opaque, ctx=ctx, context=ctx, digest=ctx, prompt=p, text=p))


def build_payload(mod, entries: list[dict], *, single: bool) -> bytes:
    """The final bytes: rubric + one item block per entry + the tail. This is what outgoing_check sees."""
    tail = mod.SINGLE_TAIL if single else mod.BATCH_TAIL
    body = "".join(render_item(mod, e["opaque"], e["ctx"], e["prompt"]) for e in entries)
    return (mod.RUBRIC + "\n" + body + tail.format_map(_Safe(n=len(entries)))).encode("utf-8")


_TIER_RE = re.compile(r"\b(L\d+)\b[^A-Za-z0-9]{1,12}(haiku|sonnet|opus)\b", re.IGNORECASE)


def parse_tiers(text: str) -> dict:
    """opaque id -> tier. Tries JSON (dict id->tier, or a list of {id, tier}) first, then id/tier pairs."""
    out = {}
    m = re.search(r"[\[{].*[\]}]", text, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, str) and v.lower() in TIERS:
                    out[str(k)] = v.lower()
        elif isinstance(obj, list):
            for d in obj:
                if isinstance(d, dict):
                    t = d.get("tier") or d.get("label") or d.get("rating")
                    if d.get("id") and isinstance(t, str) and t.lower() in TIERS:
                        out[str(d["id"])] = t.lower()
    if not out:
        for oid, t in _TIER_RE.findall(text):
            out[oid] = t.lower()
    return out


def check_model_id(judge: str, ids: list[str], pinned_id: str | None, recorded_id: str | None) -> tuple[bool, str]:
    """claude: every reported model id is of the judge's family (opus/sonnet pinned) and equals the id seen
    earlier in this run; codex: the CLI-reported id equals the pinned id. A differing family or a mid-run
    id change is not ok."""
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
                                                 "plants": {"asked": 0, "correct": 0, "wrong": 0}, "cwd_leftover": 0})

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
        self.mod = load_prompt_module(a.prompt_module)
        self.ctx = S.ScrubContext(S.load_denylist(a.denylist), S.load_env_values(a.env_file)[0])
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
        return len(S.read_jsonl(self.calls_path))

    def _log_call(self, **rec):
        rec.update({"utc": S.utc_now(), "judge": self.judge})
        S.append_jsonl(self.calls_path, rec)

    def _send(self, payload: bytes, batch_id: str):
        a = self.a
        res, leftover = S.child_call("codex" if self.judge == "gpt" else "claude", self.pinned_id or self.judge,
                                     payload, self.run_dir, claude_bin=a.claude_bin, codex_bin=a.codex_bin,
                                     timeout=a.timeout, batch_id=batch_id)
        self.js["cwd_leftover"] += leftover
        return res

    def ask(self, entries, *, single, batch_no, attempt, kind) -> dict | None:
        """One call. Returns {tiers, call_id, ids, usd} on a complete parse, None on a failed call.
        ScrubAbort propagates (caller counts it). A model-id problem raises Void."""
        if self.judge == "opus":
            self.spend.guard()
        payload = build_payload(self.mod, entries, single=single)
        call_id = "%s-%06d" % (self.judge, self._n_calls() + 1)
        res = self._send(payload, call_id)
        self.js["calls"] += 1
        parsed = (S.parse_codex_jsonl if self.judge == "gpt" else S.parse_claude_json)(res.stdout)
        usd = parsed.get("cost_usd", 0.0) if self.judge == "opus" else 0.0
        if self.judge == "opus" or parsed.get("cost_usd"):
            self.spend.record(self.pinned_id or self.judge, "claude" if self.judge != "gpt" else "codex",
                              parsed.get("tokens", 0), usd, call_id=call_id)
        ok, reason = check_model_id(self.judge, parsed["model_ids"], self.pinned_id, self.js.get("model_id"))
        self._log_call(call_id=call_id, batch=batch_no, attempt=attempt, kind=kind, rc=res.returncode,
                       model_ids=parsed["model_ids"], n_items=len(entries), usd=usd)
        if res.returncode == 0 and not ok:
            raise Void(reason, S.EXIT_MODEL_ID)
        if res.returncode != 0 or not parsed.get("ok"):
            return None
        tiers = parse_tiers(parsed["text"])
        if any(e["opaque"] not in tiers for e in entries):
            return None
        self.js["model_id"] = parsed["model_ids"][0]
        return {"tiers": tiers, "call_id": call_id, "model_id": parsed["model_ids"][0], "usd": usd}

    # -- plants ------------------------------------------------------------------------------
    def _cursor(self) -> int:
        return json.loads(self.state_path.read_text())["plant_cursor"] if self.state_path.exists() else 0

    def take_plants(self, plant_ids: list[str]) -> list[dict]:
        cur = self._cursor()
        pool = self.plan["plant_pool"]
        if cur + len(plant_ids) > len(pool):
            raise S.CLSExit(S.EXIT_REFUSE, "opaque id pool for plants exhausted")
        S.write_private(self.state_path, json.dumps({"plant_cursor": cur + len(plant_ids)}) + "\n")
        out = []
        for k, pid in enumerate(plant_ids):
            pl = self.mod.PLANTS[pid]
            out.append({"opaque": pool[cur + k], "plant_id": pid, "plant": True, "expected": pl["expected"],
                        "ctx": S.pii_replace((pl.get("digest") or "")[:S.DIGEST_CAP]), "prompt": pl["text"]})
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
        def sender(payload: bytes) -> str:
            res = self._send(payload, "canary")
            return S.parse_codex_jsonl(res.stdout)["text"]
        S.codex_canary(sender, self.run_dir / "canary", log=lambda m: self.js.__setitem__("canary", m))


def S_load_model_ids(pins_static) -> dict:
    try:
        return json.loads(Path(pins_static).read_text(encoding="utf-8"))["pins"]["model_ids"]
    except (OSError, ValueError, KeyError):
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
            p.add_argument("--prompt-module", required=True)
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
    S.outgoing_check(r.mod.RUBRIC.encode("utf-8"), batch_id="rubric")  # the template itself must be clean
    r.status.save()
    print(json.dumps({"set": a.set, "judge": a.judge, "n_items": len(plan["order"]),
                      "n_batches": len(plan["batches"]), "excluded_detector": r.js["excluded_detector"],
                      "retro_exclusions": r.js["retro_exclusions"], "env_sha_at_use": env_sha}, sort_keys=True))
    return S.EXIT_OK


def cmd_run(a) -> int:
    env_sha = _start(a)
    r = Runner(a, a.judge)
    items, truth = r.load()
    S.outgoing_check(r.mod.RUBRIC.encode("utf-8"), batch_id="rubric")
    code = r.run(items, truth)
    js = r.js
    print(json.dumps({"set": a.set, "judge": a.judge, "state": js["state"], "calls": js["calls"],
                      "batches": js["batches_done"], "label_errors": js["label_errors"],
                      "scrub_abort": js["scrub_abort"], "retro_exclusions": js["retro_exclusions"],
                      "plants": js["plants"], "env_sha_at_use": env_sha, "exit": code}, sort_keys=True))
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
