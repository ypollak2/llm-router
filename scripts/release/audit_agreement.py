#!/usr/bin/env python3
"""Does the audit label the hand-labelled units the way the hand labels say?

The 16.0.0 outcome audit was checked by hand twice (BIND-R1: 35 Codex units followed
through the transcripts; BIND-R2: all 158 routed_mcp rows read). The labels survive
here as text-free fixtures (``tests/fixtures/bind_w3/``: ids are sha256 hashes of the
unit id, there is no prompt or reply text). This script re-runs the audit read-only on
the live transcripts and reports agreement, per unit and per class:

    python3 scripts/release/audit_agreement.py --until 2026-11-03T09:50:20Z
    python3 scripts/release/audit_agreement.py --until 2026-11-03T09:50:20Z \\
        --old-artifact <repo>/release-artifacts/outcome-audit/outcome_audit_16.0.0.jsonl

It needs the transcripts the fixtures were taken from (Claude Code keeps about 30
days), so it is a local check, not a CI one (the test suite may not read the owner's
home): ``tests/test_bind_audit.py::test_agreement_cli_on_a_synthetic_corpus`` runs the
same comparison on fixtures built from a synthetic corpus.

Session-kind overrides are NOT applied here (``--keep-overrides`` applies them): the
hand labels cover sessions the owner later marked non-organic, and a unit that is
absent because its session was excluded is not a disagreement.

Read-only. Imports nothing from ``src/``.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "bind_w3"


def _audit():
    name = "_aa_outcome_audit"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("outcome_audit.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def uid_hash(unit_id: str) -> str:
    return hashlib.sha256(unit_id.encode()).hexdigest()[:16]


def load_fixture(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def r2_agreement(labels_by_id: dict[str, str], fixture: dict) -> dict:
    """Compare label-by-hashed-unit-id with the R2 hand labels. A fixture unit counts as
    agreeing when the audit's label is one of its ``hand_label_any_of`` (a replay
    duplicate agrees when the audit has no row for it). Units the audit has no row for
    and that are not duplicates are reported as absent, not as disagreements."""
    confusion: Counter = Counter()
    agree = disagree = absent = 0
    by_class: dict[str, Counter] = {}
    misses: list[dict] = []
    for u in fixture["units"]:
        got = labels_by_id.get(u["id"])
        want = u["hand_label_any_of"]
        c = by_class.setdefault(u["class"], Counter())
        if u["replay_duplicate"]:
            ok = got is None
            confusion[("deduped", "deduped" if ok else got)] += 1
        elif got is None:
            absent += 1
            c["absent"] += 1
            continue
        else:
            ok = got in want
            confusion[("|".join(want), got)] += 1
        if ok:
            agree += 1
            c["agree"] += 1
        else:
            disagree += 1
            c["disagree"] += 1
            misses.append({"r2_row": u["r2_row"], "class": u["class"], "subclass": u["subclass"],
                           "hand": want, "audit": got})
    used_hand = {u["id"] for u in fixture["units"] if u["hand_label_any_of"] == ["used"]}
    used_audit = {i for i, lab in labels_by_id.items() if lab == "used"} & {u["id"] for u in fixture["units"]}
    na_hand = {u["id"] for u in fixture["units"] if u["hand_label_any_of"] == ["not_applicable"]}
    na_audit = {i for i, lab in labels_by_id.items() if lab == "not_applicable"} & {u["id"] for u in fixture["units"]}
    return {"n_fixture": len(fixture["units"]), "agree": agree, "disagree": disagree, "absent": absent,
            "agreement": agree / (agree + disagree) if agree + disagree else None,
            "used": {"hand": len(used_hand), "audit": len(used_audit & {u["id"] for u in fixture["units"]}),
                     "both": len(used_hand & used_audit), "audit_only": sorted(used_audit - used_hand)},
            "not_applicable": {"hand": len(na_hand), "audit": len(na_audit), "both": len(na_hand & na_audit)},
            "by_class": {k: dict(v) for k, v in sorted(by_class.items())},
            "confusion": {f"{a} -> {b}": n for (a, b), n in sorted(confusion.items())},
            "disagreements": misses}


def r1_agreement(units_by_id: dict[str, object], fixture: dict) -> dict:
    """Compare the audit's Codex units with the 35 hand-followed ones: label, bound, and
    the pairing, checked against the sub-agent transcripts (the paired Agent call's
    subagent_type and the launched agent's resolved model must be the hand table's)."""
    n = label_ok = bound_ok = type_ok = model_ok = found = 0
    bad: list[int] = []
    for u in fixture["units"]:
        unit = units_by_id.get(u["id"])
        if unit is None:
            bad.append(u["r1_row"])
            continue
        found += 1
        e = unit.extra
        n += 1
        label_ok += (unit.label, unit.signal) == (u["hand_label"], u["hand_signal"])
        bound_ok += bool(e.get("bound")) == u["hand_bound"]
        type_ok += e.get("call_subagent_type") == u["spawned_type"]
        model_ok += (e.get("resolved_model") or "").split("[")[0] == u["claude_agent_model"].split("[")[0]
        if not ((unit.label, unit.signal) == (u["hand_label"], u["hand_signal"])
                and e.get("call_subagent_type") == u["spawned_type"]):
            bad.append(u["r1_row"])
    old_ok = sum(1 for u in fixture["units"] if u["old_audit_paired_its_own_call"])
    return {"n_fixture": len(fixture["units"]), "found": found, "label_and_signal": label_ok, "not_bound": bound_ok,
            "paired_call_type_matches": type_ok, "launched_model_matches": model_ok,
            "old_audit_paired_its_own_call": old_ok, "rows_to_inspect": sorted(set(bad))}


def render(r2: dict, r1: dict, old: dict | None) -> str:
    out = ["BIND W3 audit validation (hand-labelled fixtures; ids hashed, no text)", ""]
    out += [f"R1 Codex sample: {r1['found']}/{r1['n_fixture']} units found in the audit",
            f"  labelled redone/claude_subagent_ran as the hand table says: {r1['label_and_signal']}/{r1['found']}",
            f"  not bound (hand: the Codex text never reached the model): {r1['not_bound']}/{r1['found']}",
            f"  paired Agent call has the hand table's subagent_type: {r1['paired_call_type_matches']}/{r1['found']}  "
            f"(the old audit paired its own call for {r1['old_audit_paired_its_own_call']}/{r1['n_fixture']})",
            f"  launched sub-agent has the hand table's model: {r1['launched_model_matches']}/{r1['found']}"]
    if r1["rows_to_inspect"]:
        out.append(f"  inspect R1 rows: {r1['rows_to_inspect']}")
    out += ["", f"R2 routed_mcp rows: {r2['n_fixture']} hand-labelled ({r2['absent']} absent from the audit)"]
    a = r2["agreement"]
    out.append(f"  agreement with the hand label: {r2['agree']}/{r2['agree'] + r2['disagree']}"
               + (f" = {100 * a:.1f}%" if a is not None else ""))
    u, na = r2["used"], r2["not_applicable"]
    out.append(f"  used: hand {u['hand']}, audit {u['audit']}, both {u['both']}; audit-only {len(u['audit_only'])}")
    out.append(f"  not_applicable: hand {na['hand']}, audit {na['audit']}, both {na['both']}")
    out.append("  by hand class (agree/disagree): " + ", ".join(
        f"{k}:{v.get('agree', 0)}/{v.get('disagree', 0)}" for k, v in r2["by_class"].items()))
    out.append("  disagreements (hand -> audit): " + "; ".join(
        f"row {m['r2_row']} {m['class']}:{m['subclass'][:24]} {'|'.join(m['hand'])} -> {m['audit']}" for m in r2["disagreements"]))
    if old is not None:
        out.append(f"  the 16.0.0 audit on the same rows: {old['agree']}/{old['agree'] + old['disagree']}"
                   + (f" = {100 * old['agreement']:.1f}%" if old["agreement"] is not None else "")
                   + f"; used: hand {old['used']['hand']}, audit {old['used']['audit']}, both {old['used']['both']}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--until", default=None, metavar="ISO_UTC", help="end of the window (default: now)")
    ap.add_argument("--fixtures", type=Path, default=FIXTURES)
    ap.add_argument("--projects-dir", type=Path, default=None)
    ap.add_argument("--state-dir", type=Path, default=None)
    ap.add_argument("--keep-overrides", action="store_true", help="apply session_kind_overrides.json")
    ap.add_argument("--old-artifact", type=Path, default=None, help="the 16.0.0 audit rows, for the old audit's agreement")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    audit = _audit()
    until = datetime.fromisoformat(args.until.replace("Z", "+00:00")).timestamp() if args.until else None
    units, _ctx = audit._collect(args.days, args.projects_dir or audit.default_projects_dir(),
                                 args.state_dir or audit.default_state_dir(), until,
                                 overrides=None if args.keep_overrides else {})
    by_id = {uid_hash(u.unit_id): u for u in units}
    r2 = r2_agreement({i: u.label for i, u in by_id.items() if u.kind == "routed_mcp"},
                      load_fixture(args.fixtures / "r2_mcp_hand_labels.json"))
    r1 = r1_agreement({i: u for i, u in by_id.items() if u.kind == "agent_route_codex"},
                      load_fixture(args.fixtures / "r1_codex_sample.json"))
    old = None
    if args.old_artifact is not None:
        old_labels = {uid_hash(r["unit_id"]): r["label"] for r in audit._read_jsonl(args.old_artifact)
                      if r.get("kind") == "routed_mcp"}
        old = r2_agreement(old_labels, load_fixture(args.fixtures / "r2_mcp_hand_labels.json"))
    print(json.dumps({"r1": r1, "r2": r2, "r2_old_audit": old}, indent=2) if args.json else render(r2, r1, old))
    return 0


if __name__ == "__main__":
    sys.exit(main())
