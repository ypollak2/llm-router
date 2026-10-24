#!/usr/bin/env python3
"""CLS-DEV attach step (build B-A; docs/prereg/PREREG-CLS-DEV.md section 1.3, step 2 of section 0a).

    cls_attach.py attach      --cls-root ABS --out ABS/dev/... [source flags] [record flags] [--denylist ABS --env-file ABS]
    cls_attach.py export-real --cls-root ABS --items ABS --manifest ABS --out ABS [--pins-run ABS]

`attach` reads the DEV sets' item files and their EXISTING labels (GE3 tune/held-out, E2-cal/HO, E1-tune/HO,
P17D F/G) and writes, under `$CLS/dev/` only (directory 0700, files 0600, never replaced):

* `items.jsonl` (TEXT): one row per item with exactly the keys `build_manifest` (cls_dev_lib) reads:
  item_id, text, timestamp, session_id, group, source_set, batch, digest, requested;
* `legacy_opus.jsonl` / `legacy_sonnet.jsonl`: `{text_sha, session_id, truth}` (no text), the inputs of
  `cls_dev_run.py manifest --legacy-labels opus=FILE --legacy-labels sonnet=FILE`; `text_sha` is
  `cls_send_lib.text_sha` of the written item text, the key the manifest computes;
* `attach_counts.json`: counts and input sha256s only.

`export-real` writes the manifest-filtered real DEV items `{item_id, text, group}` (the manifest's own group)
for `cls_resemble.py --real-items`, so resemblance compares against all real DEV as the manifest defines it.

Rules. No model call, no network. Every path is an absolute CLI flag; no home path is built in. Sources are
read only. Item text, digests, session ids and label keys are never printed; stdout is one JSON line of
counts. Any existing output file refuses the run (exit 2) before a source is read. Time filter, dedup,
the secret detector (denylist and `.env` values included) and group/fold assignment are the manifest's
(section 1.3 order); this step only counts what it sees. The denylist and `.env` are read read-only for
their sha256 and counts (values stay in memory, never printed or written).
"""
from __future__ import annotations

import argparse
import datetime
import glob
import hashlib
import importlib.util
import itertools
import json
import os
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import cls_dev_lib as D  # noqa: E402
import cls_send_lib as S  # noqa: E402

EXIT_REFUSE = 2
ITEM_KEYS = ("item_id", "text", "timestamp", "session_id", "group", "source_set", "batch", "digest", "requested")
LEGACY_KEYS = ("text_sha", "session_id", "truth")
ITEMS, LEG_OPUS, LEG_SONNET, COUNTS = "items.jsonl", "legacy_opus.jsonl", "legacy_sonnet.jsonl", "attach_counts.json"
OUTPUTS = (ITEMS, LEG_OPUS, LEG_SONNET, COUNTS)
ID_PREFIX = "clsdev:attach:v1:"
E1_TUNE_SPLITS = ("tune",)
E1_HO_SPLITS = ("heldout", "held_out", "ho", "holdout", "test")
SOURCE_FLAGS = ("ge3_items", "ge3_split", "ge3_opus_labels", "ge3_sonnet_labels", "e2_sample", "e2_opus_labels",
                "e1_tasks", "e1_opus_labels", "p17d_items", "p17d_labels", "ledger")
PATH_FLAGS = ("cls_root", "out") + SOURCE_FLAGS + ("claude_root", "gt_scripts", "denylist", "env_file")


class Refuse(Exception):
    """A refusal (exit 2). The message names files or counts, never text."""


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------

def _load_script(name: str):
    """A sibling script as a private module (p17d_attach, p17d_synthetic), loaded only when needed."""
    path = HERE / ("%s.py" % name)
    spec = importlib.util.spec_from_file_location("_cls_attach_%s" % name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    except SystemExit as e:  # p17d_synthetic refuses when p17d_attach's pin differs
        raise Refuse("refusing: %s could not be loaded (exit %s)" % (path.name, e.code)) from None
    return mod


def read_rows(path, flag: str) -> list[dict]:
    """A JSON list or a JSONL file of objects."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise Refuse("refusing: --%s unreadable (%s)" % (flag.replace("_", "-"), type(e).__name__)) from None
    try:
        if raw.lstrip().startswith("["):
            rows = json.loads(raw)
        else:
            rows = [json.loads(ln) for ln in raw.splitlines() if ln.strip()]
    except ValueError:
        raise Refuse("refusing: --%s is not JSON/JSONL" % flag.replace("_", "-")) from None
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise Refuse("refusing: --%s rows are not objects" % flag.replace("_", "-"))
    return rows


def tier_of(value) -> str | None:
    v = value.strip().lower() if isinstance(value, str) else None
    return v if v in D.TIERS else None


def key_of(value) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or value == "":
        return None
    return str(value)


def ts_of(value) -> str | None:
    """The source timestamp unchanged (never re-formatted: truncating fractions would move items across
    the cutoff); epoch numbers become an ISO string with their fraction kept."""
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.datetime.fromtimestamp(float(value), tz=datetime.timezone.utc).isoformat()
    return None


def sid_of(value) -> str | None:
    return value if isinstance(value, str) and value else None


def label_map(path, flag: str, key_field: str, tier_fields: tuple, drops: Counter) -> dict:
    """{key: (tier, batch)} from a label file. A row with no key or no valid tier is counted and skipped;
    a key with two different tiers gets no label (counted)."""
    out, conflict = {}, set()
    for r in read_rows(path, flag):
        k = key_of(r.get(key_field))
        if k is None:
            drops["label_row_key_missing"] += 1
            continue
        t = next((tier_of(r.get(f)) for f in tier_fields if tier_of(r.get(f))), None)
        if t is None:
            drops["label_bad_tier"] += 1
            continue
        b = r.get("batch")
        b = None if b is None or b == "" else str(b)
        if k in out and out[k][0] != t:
            conflict.add(k)
        out.setdefault(k, (t, b))
    for k in conflict:
        out.pop(k, None)
    drops["label_key_conflict"] += len(conflict)
    return out


def _sha(path) -> str:
    return S.sha256_file(path)


# ---------------------------------------------------------------------------------------------
# Source readers: each returns pre-items {source_set, native, text, timestamp, session_id, batch,
# digest, digest_withheld, requested, requested_basis, opus, sonnet}; drops go to drops[family][reason].
# ---------------------------------------------------------------------------------------------

def _emit(out: list, drops: Counter, *, source_set, native, text, timestamp, session_id, digest, withheld,
          opus, sonnet=None, batch=None, requested=None, basis=None) -> None:
    if not isinstance(text, str) or not text.strip():
        drops["empty_text"] += 1
        return
    if opus is None:
        drops["no_opus_label"] += 1
        return
    withheld = bool(withheld)
    out.append({"source_set": source_set, "native": native, "text": text, "timestamp": ts_of(timestamp),
                "session_id": sid_of(session_id), "batch": batch,
                "digest": digest if isinstance(digest, str) and digest and not withheld else None,
                "digest_withheld": withheld, "requested": requested, "requested_basis": basis,
                "opus": opus, "sonnet": sonnet})


def read_ge3(items_path, split_path, opus_path, sonnet_path, drops: Counter) -> list[dict]:
    """GE3: gt500 items.jsonl {id, prompt, session_id, timestamp, context?, context_withheld?}, split.json
    {tune_ids, heldout_ids}, work/labels_opus.jsonl and labels_sonnet.jsonl {id, tier}."""
    try:
        split = json.loads(Path(split_path).read_text(encoding="utf-8"))
        tune, held = set(split["tune_ids"]), set(split["heldout_ids"])
    except (OSError, ValueError, KeyError, TypeError):
        raise Refuse("refusing: --ge3-split is not a {tune_ids, heldout_ids} JSON file") from None
    if tune & held:
        raise Refuse("refusing: --ge3-split tune and heldout ids overlap (%d)" % len(tune & held))
    opus = label_map(opus_path, "ge3_opus_labels", "id", ("tier", "truth"), drops)
    sonnet = label_map(sonnet_path, "ge3_sonnet_labels", "id", ("tier", "truth"), drops) if sonnet_path else {}
    out: list = []
    for r in read_rows(items_path, "ge3_items"):
        rid = key_of(r.get("id"))
        src = "ge3_tune" if rid in tune else "ge3_ho" if rid in held else None
        if src is None:
            drops["ge3_not_in_split"] += 1
            continue
        o = opus.get(rid)
        _emit(out, drops, source_set=src, native=rid, text=r.get("prompt"), timestamp=r.get("timestamp"),
              session_id=r.get("session_id"), digest=r.get("context"), withheld=r.get("context_withheld"),
              opus=o[0] if o else None, sonnet=(sonnet.get(rid) or (None,))[0], batch=o[1] if o else None)
    return out


def _read_keyed(rows, flag, label_path, label_flag, key_field, tier_field, set_of, drops: Counter) -> list[dict]:
    labels = label_map(label_path, label_flag, key_field, (tier_field,), drops)
    out: list = []
    for r in rows:
        src = set_of(r.get("split"))
        if src is None:
            drops["%s_unknown_split" % flag] += 1
            continue
        k = key_of(r.get(key_field))
        if k is None:
            drops["label_key_missing"] += 1
            continue
        lab = labels.get(k)
        _emit(out, drops, source_set=src, native=k, text=r.get("text"), timestamp=r.get("timestamp"),
              session_id=r.get("session_id"), digest=r.get("context"), withheld=r.get("context_withheld"),
              opus=lab[0] if lab else None, batch=lab[1] if lab else None)
    return out


def read_e2(sample_path, label_path, key_field: str, tier_field: str, drops: Counter) -> list[dict]:
    """E2 (p_eval work/sample.jsonl {text, split, session_id, timestamp}): split "calibration" -> e2_cal,
    else e2_ho (the p17d_attach rule). Labels: any file whose rows carry key_field and tier_field (may be
    the sample file itself)."""
    return _read_keyed(read_rows(sample_path, "e2_sample"), "e2", label_path, "e2_opus_labels", key_field,
                       tier_field, lambda s: "e2_cal" if s == "calibration" else "e2_ho", drops)


def _e1_set(split) -> str | None:
    s = split.strip().lower() if isinstance(split, str) else None
    return "e1_tune" if s in E1_TUNE_SPLITS else "e1_ho" if s in E1_HO_SPLITS else None


def read_e1(tasks_path, label_path, key_field: str, tier_field: str, drops: Counter) -> list[dict]:
    """E1 (local-classifier tasks_local.json, a JSON list {text, split}): tune -> e1_tune, held-out names ->
    e1_ho, other splits dropped. E1 rows carry no session or timestamp: record recovery fills them."""
    return _read_keyed(read_rows(tasks_path, "e1_tasks"), "e1", label_path, "e1_opus_labels", key_field,
                       tier_field, _e1_set, drops)


def read_p17d(items_path, labels_path, drops: Counter, item_key=None) -> list[dict]:
    """P17D: p17d_attach items.jsonl (group F/G) and p17d_synthetic labels.jsonl {text_sha, session_id,
    truth}, joined by p17d_synthetic.item_key (the live key the labeller wrote)."""
    if item_key is None:
        item_key = _load_script("p17d_synthetic").item_key
    labels, conflict = {}, set()
    for r in read_rows(labels_path, "p17d_labels"):
        t = tier_of(r.get("truth")) or tier_of(r.get("tier"))
        if not isinstance(r.get("text_sha"), str) or t is None:
            drops["label_bad_tier"] += 1
            continue
        k = (r.get("session_id") or "", r["text_sha"])
        if k in labels and labels[k] != t:
            conflict.add(k)
        labels.setdefault(k, t)
    for k in conflict:
        labels.pop(k, None)
    drops["label_key_conflict"] += len(conflict)
    out: list = []
    for r in read_rows(items_path, "p17d_items"):
        src = {"F": "p17d_f", "G": "p17d_g"}.get(r.get("group"))
        if src is None:
            drops["p17d_bad_group"] += 1
            continue
        native = key_of(r.get("item_id"))
        if native is None:
            drops["p17d_no_item_id"] += 1
            continue
        req = tier_of(r.get("requested_tier"))
        _emit(out, drops, source_set=src, native=native, text=r.get("prompt"), timestamp=r.get("ts"),
              session_id=r.get("session_id"), digest=r.get("assembled_context"),
              withheld=r.get("context_withheld"), opus=labels.get(item_key(r)), requested=req,
              basis="p17d_attach" if req else None)
    return out


# ---------------------------------------------------------------------------------------------
# Record recovery (section 1.3 step 2: GE3 record rules, sha256(text) join, earliest record wins)
# ---------------------------------------------------------------------------------------------

def build_record_index(claude_root, gt, supply, pa) -> tuple[dict, dict, dict]:
    """Top-level <claude_root>/*/*.jsonl transcripts, each read once into memory. A main-thread human
    user record (gt._is_human, as p17d_attach) gives text = REMINDER-stripped supply.text_of(content)."""
    by_sidts, by_sha = {}, {}
    files = sorted(glob.glob(os.path.join(str(claude_root), "*", "*.jsonl")))
    stats = {"transcript_files": len(files), "records": 0, "unreadable_file": 0}
    for path in files:
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError:
            stats["unreadable_file"] += 1
            continue
        for d, _msgs, nxt in pa.session_messages(raw):
            content = (d.get("message") or {}).get("content")
            if pa._is_tool_result_only(content):
                continue
            origin = d.get("origin") if isinstance(d.get("origin"), dict) else {}
            r = {"entrypoint": d.get("entrypoint"), "prompt_source": d.get("promptSource"),
                 "is_meta": bool(d.get("isMeta")), "origin_human": origin.get("kind") == "human"}
            if not gt._is_human(r):
                continue
            ts = d.get("timestamp") or ""
            tsd = pa.parse_ts(ts)
            if tsd is None:
                continue
            text = supply.REMINDER.sub("", supply.text_of(content)).strip()
            if not text:
                continue
            nm = (nxt or {}).get("message") or {}
            rec = {"session_id": d.get("sessionId") or "", "timestamp": ts, "msg_id": nm.get("id"),
                   "served_model": nm.get("model"), "_order": (tsd, d.get("sessionId") or "", ts)}
            stats["records"] += 1
            by_sidts.setdefault((rec["session_id"], ts), rec)
            k = S.text_sha(text)
            if k not in by_sha or rec["_order"] < by_sha[k]["_order"]:
                by_sha[k] = rec
    return by_sidts, by_sha, stats


def ledger_requested(ledger_path, need: set) -> dict:
    """msg_id -> requested_model, streamed once, first row wins (p17d_attach)."""
    joined: dict = {}
    if not need or not ledger_path or not Path(ledger_path).exists():
        return joined
    with open(ledger_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"msg_id"' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            mid = row.get("msg_id") if isinstance(row, dict) else None
            if mid in need and mid not in joined and row.get("requested_model"):
                joined[mid] = row["requested_model"]
    return joined


def recover(items: list[dict], index, ledger_path, tier_name, counts: Counter) -> None:
    """Fill a missing session_id / timestamp from the joined record (the item's own values win) and set
    requested from the ledger (by the record's msg_id), else the served model, else None."""
    recs = []
    for it in items:
        rec = None
        if index is not None:
            by_sidts, by_sha = index
            if it["session_id"] and it["timestamp"]:
                rec = by_sidts.get((it["session_id"], it["timestamp"]))
            else:
                rec = by_sha.get(S.text_sha(it["text"]))
        recs.append(rec)
        if rec is not None:
            counts["record_joined"] += 1
            if not it["session_id"] and rec["session_id"]:
                it["session_id"] = rec["session_id"]
                counts["session_from_record"] += 1
            if not it["timestamp"] and rec["timestamp"]:
                it["timestamp"] = rec["timestamp"]
                counts["timestamp_from_record"] += 1
    joined = ledger_requested(ledger_path, {r["msg_id"] for r in recs if r and r.get("msg_id")})
    for it, rec in zip(items, recs):
        if it["requested_basis"] == "p17d_attach":
            continue
        it["requested"], it["requested_basis"] = None, "none"
        if rec is None:
            continue
        t = tier_name(joined.get(rec.get("msg_id")))
        if t in D.TIERS:
            it["requested"], it["requested_basis"] = t, "ledger"
            continue
        t = tier_name(rec.get("served_model"))
        if t in D.TIERS:
            it["requested"], it["requested_basis"] = t, "transcript_served"


# ---------------------------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------------------------

def item_id(source_set: str, native: str) -> str:
    return hashlib.sha256((ID_PREFIX + source_set + ":" + native).encode("utf-8")).hexdigest()[:16]


def item_row(it: dict) -> dict:
    return {"item_id": it["item_id"], "text": it["text"], "timestamp": it["timestamp"],
            "session_id": it["session_id"], "group": it["session_id"], "source_set": it["source_set"],
            "batch": it["batch"], "digest": it["digest"], "requested": it["requested"]}


def legacy_rows(items: list[dict], judge: str) -> tuple[list[dict], int, set]:
    """One {text_sha, session_id, truth} row per (session_id, text_sha); a key with two truths is
    written for neither (legacy_index would keep the last row silently). Returns (rows, n_conflict, keys)."""
    keyed, conflict = {}, set()
    for it in items:
        t = it.get(judge)
        if t not in D.TIERS:
            continue
        k = (it["session_id"] or "", S.text_sha(it["text"]))
        if k in keyed and keyed[k] != t:
            conflict.add(k)
        keyed.setdefault(k, t)
    rows = [{"text_sha": k[1], "session_id": k[0], "truth": t} for k, t in sorted(keyed.items()) if k not in conflict]
    return rows, len(conflict), {k for k in keyed if k not in conflict}


def write_new(path: Path, data: str) -> None:
    """0600, O_EXCL: an existing file is never replaced."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(str(path), 0o600)


def jsonl(rows) -> str:
    return "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in rows)


def check_abs(a, names) -> None:
    for n in names:
        v = getattr(a, n, None)
        if v is not None and not os.path.isabs(v):
            raise Refuse("refusing: --%s must be an absolute path" % n.replace("_", "-"))


def check_out(out: Path, cls_root: Path) -> None:
    try:
        S.guard_dev_path(out, cls_root)
    except S.CLSExit as e:
        raise Refuse("refusing: --out %s" % e) from None
    if not S.under(out, cls_root / "dev"):
        raise Refuse("refusing: --out must be under <cls-root>/dev")


def _need(a, flags, required) -> bool:
    given = [f for f in flags if getattr(a, f, None)]
    if given and not all(getattr(a, f, None) for f in required):
        raise Refuse("refusing: %s need %s" % (",".join("--" + f.replace("_", "-") for f in given),
                                                ",".join("--" + f.replace("_", "-") for f in required)))
    return bool(given)


def run_attach(a, *, mods: dict | None = None) -> dict:
    """The attach step. `mods` (tests) may supply {"gt", "supply", "pa", "tier_name", "item_key"}."""
    mods = dict(mods or {})
    check_abs(a, PATH_FLAGS)
    cls_root, out = Path(a.cls_root), Path(a.out)
    check_out(out, cls_root)
    for name in OUTPUTS:
        if (out / name).exists():
            raise Refuse("refusing: %s exists (never overwritten)" % name)
    for f in SOURCE_FLAGS + ("claude_root", "gt_scripts", "denylist", "env_file"):
        v = getattr(a, f, None)
        if v:
            try:
                S.guard_dev_path(v, cls_root)
            except S.CLSExit as e:
                raise Refuse("refusing: --%s %s" % (f.replace("_", "-"), e)) from None
    has_ge3 = _need(a, ("ge3_items", "ge3_split", "ge3_opus_labels", "ge3_sonnet_labels"),
                    ("ge3_items", "ge3_split", "ge3_opus_labels"))
    has_e2 = _need(a, ("e2_sample", "e2_opus_labels"), ("e2_sample", "e2_opus_labels"))
    has_e1 = _need(a, ("e1_tasks", "e1_opus_labels"), ("e1_tasks", "e1_opus_labels"))
    has_p17d = _need(a, ("p17d_items", "p17d_labels"), ("p17d_items", "p17d_labels"))
    if not (has_ge3 or has_e2 or has_e1 or has_p17d):
        raise Refuse("refusing: no source given")
    if a.claude_root and not a.gt_scripts:
        raise Refuse("refusing: --claude-root needs --gt-scripts (the pinned GE3 record rules)")
    if (a.gt_scripts or a.ledger) and not a.claude_root:
        raise Refuse("refusing: --gt-scripts / --ledger need --claude-root")

    drops: dict = {}
    items: list = []
    if has_ge3:
        drops["ge3"] = Counter()
        items += read_ge3(a.ge3_items, a.ge3_split, a.ge3_opus_labels, a.ge3_sonnet_labels, drops["ge3"])
    if has_e2:
        drops["e2"] = Counter()
        items += read_e2(a.e2_sample, a.e2_opus_labels, a.e2_label_key, a.e2_label_tier, drops["e2"])
    if has_e1:
        drops["e1"] = Counter()
        items += read_e1(a.e1_tasks, a.e1_opus_labels, a.e1_label_key, a.e1_label_tier, drops["e1"])
    if has_p17d:
        drops["p17d"] = Counter()
        items += read_p17d(a.p17d_items, a.p17d_labels, drops["p17d"], item_key=mods.get("item_key"))

    rec_counts: Counter = Counter()
    index, index_stats = None, None
    tier_name = mods.get("tier_name")
    if a.claude_root:
        pa = mods.get("pa") or _load_script("p17d_attach")
        gt, supply = mods.get("gt"), mods.get("supply")
        if gt is None or supply is None:
            gts = Path(a.gt_scripts)
            try:
                supply = pa.load_pinned("supply", gts / "supply.py", pa.PINS["supply"])
                gt = pa.load_pinned("gt500", gts / "gt500.py", pa.PINS["gt500"])
            except SystemExit as e:
                raise Refuse(getattr(e, "msg", "refusing: pinned GE3 helpers could not be loaded")) from None
            except OSError as e:
                raise Refuse("refusing: --gt-scripts helpers unreadable (%s)" % type(e).__name__) from None
        by_sidts, by_sha, index_stats = build_record_index(a.claude_root, gt, supply, pa)
        index = (by_sidts, by_sha)
    if tier_name is None:
        from llm_router.shadow_eval import tier_name
    recover(items, index, a.ledger, tier_name, rec_counts)

    seen: dict = {}
    for it in items:
        it["item_id"] = item_id(it["source_set"], it["native"])
        if it["item_id"] in seen:
            raise Refuse("refusing: duplicate item key in %s (item_id collision)" % it["source_set"])
        seen[it["item_id"]] = it
    items.sort(key=lambda x: x["item_id"])
    leg_o, conf_o, keys_o = legacy_rows(items, "opus")
    leg_s, conf_s, keys_s = legacy_rows(items, "sonnet")

    per_source: dict = {}
    for src in sorted({x["source_set"] for x in items}):
        rows = [x for x in items if x["source_set"] == src]
        per_source[src] = {
            "n_items": len(rows), "n_sessions": len({x["session_id"] for x in rows if x["session_id"]}),
            "n_no_session": sum(1 for x in rows if not x["session_id"]),
            "n_no_timestamp": sum(1 for x in rows if not x["timestamp"]),
            "n_unparseable_timestamp": sum(1 for x in rows if x["timestamp"] and D.parse_utc(x["timestamp"]) is None),
            "digest_present": sum(1 for x in rows if x["digest"]),
            "digest_withheld": sum(1 for x in rows if x["digest_withheld"]),
            "requested_basis": dict(sorted(Counter(x["requested_basis"] for x in rows).items())),
            "n_legacy_opus": sum(1 for x in rows if ((x["session_id"] or ""), S.text_sha(x["text"])) in keys_o
                                 and x["opus"]),
            "n_legacy_sonnet": sum(1 for x in rows if ((x["session_id"] or ""), S.text_sha(x["text"])) in keys_s
                                   and x["sonnet"]),
        }
    by_sha: dict = {}
    for x in items:
        by_sha.setdefault(S.text_sha(x["text"]), set()).add(x["source_set"])
    overlap: Counter = Counter()
    for srcs in by_sha.values():
        for p, q in itertools.combinations(sorted(srcs), 2):
            overlap["%s|%s" % (p, q)] += 1
    total_drop: Counter = Counter()
    for c in drops.values():
        total_drop.update({k: v for k, v in c.items() if v})
    den = env = None
    if a.denylist:
        den = {"sha256": S.env_sha_at_use(a.denylist), "entries": len(S.load_denylist(a.denylist))}
    if a.env_file:
        env = {"sha256": S.env_sha_at_use(a.env_file), "qualifying_values": S.load_env_values(a.env_file)[1]}
    counts = {
        "what": "CLS-DEV attach (PREREG-CLS-DEV 1.3); counts only, no text",
        "script_sha256": _sha(Path(__file__)), "generated_utc": S.utc_now(),
        "n_items": len(items), "n_sessions": len({x["session_id"] for x in items if x["session_id"]}),
        "per_source": per_source, "dropped": dict(sorted(total_drop.items())),
        "dropped_by_source": {k: dict(sorted((r, n) for r, n in v.items() if n)) for k, v in sorted(drops.items())},
        "record_recovery": dict(sorted(rec_counts.items())), "record_index": index_stats,
        "legacy": {"opus": len(leg_o), "sonnet": len(leg_s), "conflict": {"opus": conf_o, "sonnet": conf_s}},
        "exact_overlap": dict(sorted(overlap.items())),
        "inputs": {f: _sha(getattr(a, f)) for f in SOURCE_FLAGS if getattr(a, f, None) and Path(getattr(a, f)).is_file()},
        "denylist": den, "env": env, "out_dir": str(out),
    }
    os.makedirs(out, mode=0o700, exist_ok=True)
    os.chmod(out, 0o700)
    write_new(out / LEG_OPUS, jsonl(leg_o))
    write_new(out / LEG_SONNET, jsonl(leg_s))
    write_new(out / COUNTS, json.dumps(counts, sort_keys=True, indent=1) + "\n")
    write_new(out / ITEMS, jsonl(item_row(x) for x in items))
    return counts


# ---------------------------------------------------------------------------------------------
# export-real
# ---------------------------------------------------------------------------------------------

def run_export_real(a) -> dict:
    check_abs(a, ("cls_root", "items", "manifest", "out", "pins_run"))
    cls_root, out = Path(a.cls_root), Path(a.out)
    check_out(out, cls_root)
    for f in ("items", "manifest"):
        try:
            S.guard_dev_path(getattr(a, f), cls_root)
        except S.CLSExit as e:
            raise Refuse("refusing: --%s %s" % (f, e)) from None
    if out.exists():
        raise Refuse("refusing: %s exists (never overwritten)" % out.name)
    if a.pins_run:
        try:
            D.require_run_pin(a.manifest, a.pins_run)
        except S.CLSExit as e:
            raise Refuse("refusing: %s" % e) from None
    try:
        man = json.loads(Path(a.manifest).read_text(encoding="utf-8"))
        text = S.read_guarded_text(a.items, cls_root)
        items = {}
        for ln in text.splitlines():
            if ln.strip():
                rec = json.loads(ln)
                items[rec["item_id"]] = rec
    except S.CLSExit as e:
        raise Refuse("refusing: %s" % e) from None
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise Refuse("refusing: manifest or items unreadable (%s)" % type(e).__name__) from None
    real = sorted((x for x in man.get("items", []) if x.get("kind") == "real"), key=lambda x: x["item_id"])
    missing = [x for x in real if x["item_id"] not in items]
    if missing:
        raise Refuse("refusing: items file lacks %d manifest items" % len(missing))
    bad = [x for x in real if S.text_sha(items[x["item_id"]]["text"]) != x.get("text_sha")]
    if bad:
        raise Refuse("refusing: %d items differ from the manifest text_sha" % len(bad))
    rows = [{"item_id": x["item_id"], "text": items[x["item_id"]]["text"], "group": x["group"]} for x in real]
    os.makedirs(out.parent, mode=0o700, exist_ok=True)
    write_new(out, jsonl(rows))
    return {"n_real": len(rows), "n_groups": len({r["group"] for r in rows}), "out": str(out)}


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="cls_attach", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("attach", help="write items.jsonl + legacy label files under $CLS/dev")
    p.add_argument("--cls-root", required=True)
    p.add_argument("--out", required=True, help="output directory under <cls-root>/dev")
    p.add_argument("--ge3-items", help="gt500 items.jsonl")
    p.add_argument("--ge3-split", help="gt500 split.json")
    p.add_argument("--ge3-opus-labels", help="gt500 work/labels_opus.jsonl {id, tier}")
    p.add_argument("--ge3-sonnet-labels", help="gt500 work/labels_sonnet.jsonl {id, tier}")
    p.add_argument("--e2-sample", help="p_eval work/sample.jsonl")
    p.add_argument("--e2-opus-labels", help="E2 Opus labels (may be the sample file itself)")
    p.add_argument("--e2-label-key", default="id", help="field joining E2 rows and label rows (default id)")
    p.add_argument("--e2-label-tier", default="tier", help="tier field of an E2 label row (default tier)")
    p.add_argument("--e1-tasks", help="local-classifier tasks_local.json")
    p.add_argument("--e1-opus-labels", help="E1 Opus labels (may be the tasks file itself)")
    p.add_argument("--e1-label-key", default="id", help="field joining E1 rows and label rows (default id)")
    p.add_argument("--e1-label-tier", default="tier", help="tier field of an E1 label row (default tier)")
    p.add_argument("--p17d-items", help="p17d_attach items.jsonl")
    p.add_argument("--p17d-labels", help="p17d labels.jsonl {text_sha, session_id, truth}")
    p.add_argument("--claude-root", help="transcript root (top-level */*.jsonl) for record recovery")
    p.add_argument("--gt-scripts", help="directory of the pinned gt500.py and supply.py")
    p.add_argument("--ledger", help="proxy_calls.jsonl (requested_model by msg_id)")
    p.add_argument("--denylist", help="exclusion list (sha256 and count recorded; applied by the manifest)")
    p.add_argument("--env-file", help=".env (sha256 and qualifying-value count recorded; applied by the manifest)")
    q = sub.add_parser("export-real", help="manifest-filtered real DEV items for cls_resemble --real-items")
    q.add_argument("--cls-root", required=True)
    q.add_argument("--items", required=True, help="the attach items.jsonl")
    q.add_argument("--manifest", required=True, help="dev_manifest.json from cls_dev_run.py manifest")
    q.add_argument("--out", required=True, help="output file under <cls-root>/dev")
    q.add_argument("--pins-run", help="pins_run.jsonl: the manifest must match its PINS-RUN entry")
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    try:
        if a.cmd == "attach":
            counts = run_attach(a)
            print(json.dumps({k: v for k, v in counts.items() if k != "inputs"}, sort_keys=True))
        else:
            print(json.dumps(run_export_real(a), sort_keys=True))
    except Refuse as e:
        print(str(e), file=sys.stderr)
        return EXIT_REFUSE
    return 0


if __name__ == "__main__":
    sys.exit(main())
