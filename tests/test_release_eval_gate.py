"""PLAN v16 P3.2 (D-192): the release eval gate's CI half.

Everything here is synthetic: opaque ids, no prompt text, no model, no network.
`scripts/release/eval_gate.py` is loaded by path (same pattern as
test_pr4_release_quality_gates.py).

The CHECKS table at the bottom is what tests/test_gates_mutation_coverage.py runs
against mutated copies of eval_gate.py: each check takes the module under test as
its argument, so the same assertions judge the real file (green) and a mutant (red).
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import stat
import subprocess
import sys

import pytest
import yaml

from llm_router.eligibility import newcombe_diff_ci

REPO = pathlib.Path(__file__).resolve().parents[1]
RELEASE_DIR = REPO / "scripts" / "release"
SHA_A = "a" * 40
SHA_B = "b" * 40


def load_gate_from_source(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def G():
    return load_gate_from_source(RELEASE_DIR / "eval_gate.py", "_eval_gate_real")


@pytest.fixture(scope="module")
def R():
    return load_gate_from_source(RELEASE_DIR / "release_evals.py", "_release_evals_real")


# ── synthetic builders ────────────────────────────────────────────────────────

def _ids(prefix: str, n: int) -> list[str]:
    return [f"{prefix}{i:04d}" for i in range(n)]


def _manifest(R, set_id, kind, n=100, min_n=100):
    return R.make_manifest(set_id, kind, min_n, _ids(set_id, n))


def _perf_items(ids, fail=()):
    return [{"id": i, "pass": i not in set(fail)} for i in ids]


def _perf(R, base_fail=(), cand_fail=(), n=100):
    """Manifest + baseline items + candidate items; fail lists are item indexes."""
    m = _manifest(R, "perf", "performance", n=n)
    ids = m["item_ids"]
    bf = {ids[i] for i in base_fail}
    cf = {ids[i] for i in cand_fail}
    return m, _perf_items(ids, bf), _perf_items(ids, cf)


def _doc(sha, sets):
    return {"schema": 1, "sha": sha, "sets": {k: {"items": v} for k, v in sets.items()}}


def _run(G, R, cand, base, manifests, expect_sha=None, pin="auto"):
    """run_gate with a pin freshly derived from the manifests/baseline (a consistent freeze)."""
    if pin == "auto":
        pin = R.make_pin(manifests, (base or {}).get("sha", SHA_B))
    return G.run_gate(cand, base, manifests, expect_sha=expect_sha, pin=pin)


def _gate_perf(G, R, **kw):
    m, base, cand = _perf(R, **kw)
    return _run(G, R, _doc(SHA_A, {"perf": cand}), _doc(SHA_B, {"perf": base}), {"perf": m})[0]


def _fid_items(ids, worse=0, better=0):
    items = []
    for k, i in enumerate(ids):
        pack = full = True
        if k < worse:
            pack = False           # pack failed where full passed
        elif k < worse + better:
            full = False           # pack passed where full failed
        items.append({"id": i, "pack": pack, "full": full})
    return items


def _gate_fid(G, R, worse=0, better=0, n=100):
    m = _manifest(R, "fid", "fidelity", n=n)
    return _run(G, R, _doc(SHA_A, {"fid": _fid_items(m["item_ids"], worse, better)}), None, {"fid": m})[0]


def _stab_items(ids, differ=0):
    return [{"id": i, "a": "x", "b": "y" if k < differ else "x"} for k, i in enumerate(ids)]


def _gate_stab(G, R, differ=0, n=100):
    m = _manifest(R, "stab", "stability", n=n)
    return _run(G, R, _doc(SHA_A, {"stab": _stab_items(m["item_ids"], differ)}), None, {"stab": m})[0]


# ── checks (each takes the module under test) ─────────────────────────────────

def check_perf_4pt_drop_blocks(G, R):
    assert _gate_perf(G, R, cand_fail=range(4)) == G.BLOCK


def check_perf_exactly_3pt_passes(G, R):
    assert _gate_perf(G, R, cand_fail=range(3)) == G.PASS


def check_perf_2pt_drop_passes(G, R):
    assert _gate_perf(G, R, cand_fail=range(2)) == G.PASS


def check_perf_improvement_and_offset_pass(G, R):
    # candidate better by 4: a sign swap would call this a 4-pt drop
    assert _gate_perf(G, R, base_fail=range(4)) == G.PASS
    # 4 lost and 4 gained: net zero, discordant 8. Counting only losses would block.
    assert _gate_perf(G, R, base_fail=range(10, 14), cand_fail=range(4)) == G.PASS
    # 7 lost, 4 gained: net 3.0 passes; 8 lost, 4 gained: net 4 blocks
    assert _gate_perf(G, R, base_fail=range(10, 14), cand_fail=range(7)) == G.PASS
    assert _gate_perf(G, R, base_fail=range(10, 14), cand_fail=range(8)) == G.BLOCK


def check_perf_pairs_by_id_not_position(G, R):
    # the same 10 items fail in both runs but the candidate file lists items in the
    # opposite order: paired by id there are 0 discordant pairs, by position 20
    m, base, cand = _perf(R, base_fail=range(10), cand_fail=range(10))
    code, lines = _run(G, R, _doc(SHA_A, {"perf": list(reversed(cand))}),
                             _doc(SHA_B, {"perf": base}), {"perf": m})
    assert code == G.PASS and "discordant=0" in "\n".join(lines)


def check_perf_clean_control_passes_and_prints_stats(G, R):
    m, base, cand = _perf(R, base_fail=[5], cand_fail=[5])
    code, lines = _run(G, R, _doc(SHA_A, {"perf": cand}), _doc(SHA_B, {"perf": base}), {"perf": m})
    text = "\n".join(lines)
    assert code == G.PASS and "eval gate: PASS" in text
    for needle in ("n=100", "McNemar exact p=", "Newcombe paired 95% CI", "discordant=0", "drop=0.00"):
        assert needle in text, needle


def check_deleted_item_not_informative(G, R):
    m, base, cand = _perf(R)
    code, lines = _run(G, R, _doc(SHA_A, {"perf": cand[:-1]}), _doc(SHA_B, {"perf": base}), {"perf": m})
    assert code == G.NOT_INFORMATIVE and "missing" in "\n".join(lines)


def check_duplicate_id_not_informative(G, R):
    m, base, cand = _perf(R)
    cand[-1] = dict(cand[0])  # same n, an id duplicated and another missing
    code, lines = _run(G, R, _doc(SHA_A, {"perf": cand}), _doc(SHA_B, {"perf": base}), {"perf": m})
    assert code == G.NOT_INFORMATIVE and "duplicate" in "\n".join(lines)


def check_lowered_n_in_manifest_not_informative(G, R):
    # a manifest edited down to a smaller n (ids removed, hash recomputed) but with
    # min_n kept: n < min_n is NOT INFORMATIVE even though the file is self-consistent
    m = R.make_manifest("perf", "performance", 100, _ids("perf", 80))
    ids = m["item_ids"]
    code, lines = _run(G, R, _doc(SHA_A, {"perf": _perf_items(ids)}),
                             _doc(SHA_B, {"perf": _perf_items(ids)}), {"perf": m})
    assert code == G.NOT_INFORMATIVE and "below min_n" in "\n".join(lines)


def check_n_checks_inside_the_comparator(G, R):
    # the comparator must enforce min_n and manifest n itself, not rely on the
    # manifest validator having run first
    m, base, cand = _perf(R)
    lying = dict(m, min_n=101)
    with pytest.raises(G.NotInformative):
        G.judge_performance(lying, cand, base)
    lying = dict(m, n=99)
    with pytest.raises(G.NotInformative):
        G.judge_performance(lying, cand, base)


def check_missing_set_and_no_manifests_not_informative(G, R):
    m, base, cand = _perf(R)
    assert _run(G, R, _doc(SHA_A, {}), _doc(SHA_B, {"perf": base}), {"perf": m})[0] == G.NOT_INFORMATIVE
    assert _run(G, R, _doc(SHA_A, {"perf": cand}), _doc(SHA_B, {"perf": base}), {})[0] == G.NOT_INFORMATIVE
    assert _run(G, R, _doc(SHA_A, {"perf": cand}), None, {"perf": m})[0] == G.NOT_INFORMATIVE


def check_wrong_sha_not_informative(G, R):
    m, base, cand = _perf(R)
    args = (_doc(SHA_A, {"perf": cand}), _doc(SHA_B, {"perf": base}), {"perf": m})
    assert _run(G, R, *args, expect_sha=SHA_A)[0] == G.PASS
    assert _run(G, R, *args, expect_sha=SHA_B)[0] == G.NOT_INFORMATIVE


def check_fidelity_bar(G, R):
    assert _gate_fid(G, R, worse=3) == G.BLOCK
    assert _gate_fid(G, R, worse=2) == G.PASS
    assert _gate_fid(G, R, worse=0) == G.PASS                 # clean control
    assert _gate_fid(G, R, worse=5, better=3) == G.PASS       # net 2
    assert _gate_fid(G, R, worse=0, better=9) == G.PASS       # PACK better is not a failure


def check_stability_bar(G, R):
    assert _gate_stab(G, R, differ=3) == G.BLOCK              # 97% identical
    assert _gate_stab(G, R, differ=2) == G.PASS               # 98% identical
    assert _gate_stab(G, R, differ=0) == G.PASS               # clean control


def check_skip_policy(G, R):
    ok, _ = G.skip_quality_decision({}, head_tagged=False)
    assert ok
    ok, why = G.skip_quality_decision({"GITHUB_REF": "refs/heads/main"}, head_tagged=False)
    assert ok
    ok, why = G.skip_quality_decision({"GITHUB_REF": "refs/tags/v16.0.0"}, head_tagged=False)
    assert not ok and "refused" in why
    ok, _ = G.skip_quality_decision({}, head_tagged=True)
    assert not ok
    assert not G.skip_quality_decision({"LLM_ROUTER_RELEASE_OWNER_OVERRIDE": "0",
                                        "GITHUB_REF": "refs/tags/v1"}, False)[0]
    assert G.skip_quality_decision({"LLM_ROUTER_RELEASE_OWNER_OVERRIDE": "1",
                                    "GITHUB_REF": "refs/tags/v1"}, False)[0]


def _str_false_cand(G, R, bad):
    m, base, cand = _perf(R)
    cand = [dict(i, **{"pass": bad}) for i in cand]
    return _run(G, R, _doc(SHA_A, {"perf": cand}), _doc(SHA_B, {"perf": base}), {"perf": m})


def check_non_bool_values_not_informative(G, R):
    # F46: every candidate pass is the truthy string "false" vs an all-pass baseline: used to
    # coerce to True and PASS. Strings, 0/1 and null are refused, never coerced.
    for bad in ("false", "true", 1, 0, None):
        code, lines = _str_false_cand(G, R, bad)
        assert code == G.NOT_INFORMATIVE and "invalid item values" in "\n".join(lines), bad
    m = _manifest(R, "fid", "fidelity")
    items = _fid_items(m["item_ids"])
    items[0]["pack"] = "false"
    assert _run(G, R, _doc(SHA_A, {"fid": items}), None, {"fid": m})[0] == G.NOT_INFORMATIVE
    ms = _manifest(R, "stab", "stability")
    items = _stab_items(ms["item_ids"])
    items[0]["a"] = 1
    assert _run(G, R, _doc(SHA_A, {"stab": items}), None, {"stab": ms})[0] == G.NOT_INFORMATIVE


def _pinned(G, R, mutate=None, pin_mutate=None, base_sha=SHA_B, with_pin=True):
    m, base, cand = _perf(R)
    manifests = {"perf": m}
    pin = R.make_pin(manifests, SHA_B)
    if mutate:
        mutate(manifests)
    if pin_mutate:
        pin_mutate(pin)
    return G.run_gate(_doc(SHA_A, {"perf": cand}), _doc(base_sha, {"perf": base}), manifests,
                      pin=pin if with_pin else None)


def check_pin_consistent_fixture_still_passes(G, R):
    assert _pinned(G, R)[0] == G.PASS


def check_pin_refusals(G, R):
    def refuses(**kw):
        code, lines = _pinned(G, R, **kw)
        assert code == G.NOT_INFORMATIVE and "provenance" in "\n".join(lines), kw

    refuses(with_pin=False)                                        # manifests, no pin
    refuses(mutate=lambda ms: ms.clear())                          # manifest deleted
    refuses(mutate=lambda ms: ms.update(extra=_manifest(R, "extra", "stability")))   # manifest added
    refuses(base_sha=SHA_A)                                        # baseline is not the pinned release
    refuses(pin_mutate=lambda p: p["sets"]["perf"].update(manifest_sha256="0" * 64))  # sha differs
    # n / min_n edited CONSISTENTLY in the manifest (ids dropped, hash recomputed): pin catches it
    refuses(mutate=lambda ms: ms.update(perf=R.make_manifest("perf", "performance", 80, _ids("perf", 80))))
    # min_n lowered alone, everything else self-consistent
    refuses(mutate=lambda ms: ms["perf"].update(min_n=50))


CHECKS = {fn.__name__: fn for fn in (
    check_perf_4pt_drop_blocks, check_perf_exactly_3pt_passes, check_perf_2pt_drop_passes,
    check_perf_improvement_and_offset_pass, check_perf_pairs_by_id_not_position,
    check_perf_clean_control_passes_and_prints_stats, check_deleted_item_not_informative,
    check_duplicate_id_not_informative, check_lowered_n_in_manifest_not_informative,
    check_n_checks_inside_the_comparator, check_missing_set_and_no_manifests_not_informative,
    check_wrong_sha_not_informative, check_non_bool_values_not_informative,
    check_pin_consistent_fixture_still_passes, check_pin_refusals, check_fidelity_bar, check_stability_bar, check_skip_policy,
)}


@pytest.mark.parametrize("name", sorted(CHECKS))
def test_check_green_on_the_real_gate(name, G, R):
    CHECKS[name](G, R)


def test_checks_found_something():
    # an empty table would pass everything
    assert len(CHECKS) >= 18


# ── statistics ────────────────────────────────────────────────────────────────

def test_mcnemar_exact_known_values(G):
    assert G.mcnemar_exact_p(4, 0) == pytest.approx(0.125)
    assert G.mcnemar_exact_p(3, 3) == 1.0
    assert G.mcnemar_exact_p(0, 0) == 1.0
    assert G.mcnemar_exact_p(10, 0) == pytest.approx(2 / 1024)
    assert G.mcnemar_exact_p(0, 10) == G.mcnemar_exact_p(10, 0)


def test_newcombe_paired_matches_unpaired_when_phi_is_zero(G):
    # a*d == b*c  =>  phi == 0  =>  method 10 reduces to the unpaired interval
    lo, hi = G.newcombe_paired_ci(40, 20, 20, 10)
    ulo, uhi = newcombe_diff_ci(60, 90, 60, 90)
    assert lo == pytest.approx(ulo) and hi == pytest.approx(uhi)


def test_newcombe_paired_is_tighter_with_positive_correlation_and_contains_delta(G):
    a, b, c, d = 80, 4, 0, 16
    lo, hi = G.newcombe_paired_ci(a, b, c, d)
    delta = (a + b) / 100 - (a + c) / 100
    ulo, uhi = newcombe_diff_ci(a + b, 100, a + c, 100)
    assert lo <= delta <= hi
    assert (hi - lo) < (uhi - ulo)


def test_no_float_rounding_at_the_boundary(G, R):
    # n=300, 9 net losses is exactly 3.0 points: float drop*100 can land on 3.0000000000000004
    m = _manifest(R, "perf", "performance", n=300, min_n=60)
    ids = m["item_ids"]
    cand = _perf_items(ids, set(ids[:9]))
    base = _perf_items(ids)
    assert _run(G, R, _doc(SHA_A, {"perf": cand}), _doc(SHA_B, {"perf": base}), {"perf": m})[0] == G.PASS
    cand = _perf_items(ids, set(ids[:10]))
    assert _run(G, R, _doc(SHA_A, {"perf": cand}), _doc(SHA_B, {"perf": base}), {"perf": m})[0] == G.BLOCK


def test_worst_verdict_wins(G, R):
    mp, base, cand = _perf(R, cand_fail=range(4))          # BLOCK
    ms = _manifest(R, "stab", "stability")                 # fine
    docs = _doc(SHA_A, {"perf": cand, "stab": _stab_items(ms["item_ids"])})
    code, _ = _run(G, R, docs, _doc(SHA_B, {"perf": base}), {"perf": mp, "stab": ms})
    assert code == G.BLOCK
    docs["sets"].pop("stab")                               # now also NOT INFORMATIVE
    code, _ = _run(G, R, docs, _doc(SHA_B, {"perf": base}), {"perf": mp, "stab": ms})
    assert code == G.NOT_INFORMATIVE


# ── manifest / eval file format ───────────────────────────────────────────────

def test_manifest_hash_pins_the_id_list(R):
    m = _manifest(R, "perf", "performance")
    assert R.manifest_problems(m) == []
    dropped = dict(m, item_ids=m["item_ids"][:-1], n=99)   # delete an item, keep the hash
    assert any("items_sha256" in p for p in R.manifest_problems(dropped))
    dup = dict(m, item_ids=m["item_ids"][:-1] + [m["item_ids"][0]])
    assert any("duplicate" in p for p in R.manifest_problems(dup))
    assert any("below min_n" in p for p in R.manifest_problems(dict(m, min_n=101)))


def test_manifest_edited_to_hide_a_deleted_item_is_caught_through_the_gate(G, R):
    m = _manifest(R, "perf", "performance")
    ids = m["item_ids"][:-1]
    tampered = dict(m, item_ids=ids, n=99)
    code, lines = _run(G, R, _doc(SHA_A, {"perf": _perf_items(ids)}),
                             _doc(SHA_B, {"perf": _perf_items(ids)}), {"perf": tampered})
    assert code == G.NOT_INFORMATIVE and "manifest invalid" in "\n".join(lines)


def test_eval_file_roundtrip_and_sha_validation(R, tmp_path):
    m, base, cand = _perf(R)
    path = R.write_eval_file(tmp_path, SHA_A, {"perf": cand})
    assert path.name == f"{SHA_A}.json"
    doc = json.loads(path.read_text())
    assert doc["schema"] == 1 and doc["sha"] == SHA_A and len(doc["sets"]["perf"]["items"]) == 100
    with pytest.raises(ValueError):
        R.write_eval_file(tmp_path, "HEAD", {"perf": cand})


def test_freeze_cli_writes_a_valid_manifest(R, tmp_path):
    ids = tmp_path / "ids.txt"
    ids.write_text("\n".join(_ids("x", 60)) + "\n")
    rc = R.main(["freeze", "--set-id", "x", "--kind", "performance", "--min-n", "60",
                 "--ids-file", str(ids), "--out-dir", str(tmp_path / "m")])
    assert rc == 0
    m = json.loads((tmp_path / "m" / "x.json").read_text())
    assert R.manifest_problems(m) == [] and m["n"] == 60
    assert R.main(["freeze", "--set-id", "x", "--kind", "performance", "--min-n", "61",
                   "--ids-file", str(ids), "--out-dir", str(tmp_path / "m2")]) == 1


# ── CLI end to end (files, exit codes) ────────────────────────────────────────

def _write_case(R, tmp_path, cand_fail=(), base_fail=(), manifests=True):
    m, base, cand = _perf(R, base_fail=base_fail, cand_fail=cand_fail)
    mdir = tmp_path / "manifests"
    mdir.mkdir()
    if manifests:
        (mdir / "perf.json").write_text(json.dumps(m))
    (tmp_path / "PIN.json").write_text(json.dumps(R.make_pin({"perf": m}, SHA_B)))
    (tmp_path / "base.json").write_text(json.dumps(_doc(SHA_B, {"perf": base})))
    (tmp_path / f"{SHA_A}.json").write_text(json.dumps(_doc(SHA_A, {"perf": cand})))


def _cli(G, tmp_path, *extra):
    return G.main(["compare", "--candidate", str(tmp_path / f"{SHA_A}.json"),
                   "--baseline", str(tmp_path / "base.json"),
                   "--manifests", str(tmp_path / "manifests"),
                   "--pin", str(tmp_path / "PIN.json"), *extra])


def test_cli_exit_codes(G, R, tmp_path, capsys):
    _write_case(R, tmp_path, cand_fail=range(2))
    assert _cli(G, tmp_path, "--sha", SHA_A) == 0
    assert "eval gate: PASS" in capsys.readouterr().out
    _write_case(R, tmp_path / "b", cand_fail=range(4)) if (tmp_path / "b").mkdir() is None else None
    assert _cli(G, tmp_path / "b") == 1
    capsys.readouterr()
    (tmp_path / "c").mkdir()
    _write_case(R, tmp_path / "c", manifests=False)
    assert _cli(G, tmp_path / "c") == 2
    assert "NOT INFORMATIVE" in capsys.readouterr().out


def test_cli_missing_candidate_file_is_not_informative(G, tmp_path, capsys):
    rc = G.main(["compare", "--candidate", str(tmp_path / f"{SHA_A}.json"),
                 "--manifests", str(tmp_path / "none")])
    assert rc == 2


# ── --skip-quality policy: CLI + shell wiring ─────────────────────────────────

def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                   check=True, capture_output=True)


def test_head_is_tagged_detects_a_tag(G, tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    (tmp_path / "f").write_text("x")
    _git(tmp_path, "add", "f")
    _git(tmp_path, "commit", "-qm", "c")
    monkeypatch.chdir(tmp_path)
    assert G.head_is_tagged() is False
    _git(tmp_path, "tag", "v0.0.1")
    assert G.head_is_tagged() is True


def test_skip_check_cli_refuses_on_tag_logs_off_tag(G, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)  # not a git repo: HEAD cannot be tagged
    log = tmp_path / "skip.log"
    monkeypatch.setenv("GITHUB_REF", "refs/tags/v16.0.0")
    monkeypatch.delenv("LLM_ROUTER_RELEASE_OWNER_OVERRIDE", raising=False)
    assert G.main(["skip-check", "--reason", "ollama down", "--log", str(log)]) == 1
    assert not log.exists()
    assert "REFUSED" in capsys.readouterr().err
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    assert G.main(["skip-check", "--reason", "ollama down", "--log", str(log)]) == 0
    row = log.read_text()
    assert "ollama down" in row and "not a tag" in row
    monkeypatch.setenv("GITHUB_REF", "refs/tags/v16.0.0")
    monkeypatch.setenv("LLM_ROUTER_RELEASE_OWNER_OVERRIDE", "1")
    assert G.main(["skip-check", "--reason", "owner call", "--log", str(log)]) == 0
    assert "OWNER_OVERRIDE=1" in log.read_text()


def _fake_uv(tmp_path):
    """`uv run python X` -> this interpreter, so the shell script runs hermetically."""
    d = tmp_path / "bin"
    d.mkdir()
    uv = d / "uv"
    uv.write_text(f'#!/bin/sh\nshift\nif [ "$1" = python ]; then shift; fi\nexec "{sys.executable}" "$@"\n')
    uv.chmod(uv.stat().st_mode | stat.S_IEXEC)
    return d


def _run_verify(tmp_path, ref, extra_env=None):
    env = {**os.environ, "PATH": f"{_fake_uv(tmp_path)}:{os.environ['PATH']}", "GITHUB_REF": ref,
           "HOME": str(tmp_path)}
    env.pop("LLM_ROUTER_RELEASE_OWNER_OVERRIDE", None)
    env.update(extra_env or {})
    return subprocess.run(["bash", str(RELEASE_DIR / "pre-release-verify.sh"),
                           "--skip-quality", "ollama down"],
                          cwd=REPO, env=env, capture_output=True, text=True, timeout=120)


def test_pre_release_verify_refuses_skip_quality_on_a_tag(tmp_path):
    r = _run_verify(tmp_path, "refs/tags/v99.0.0")
    assert r.returncode == 1
    assert "REFUSED" in r.stderr
    # refused up front: the checklist banner (step 1) never printed
    assert "Pre-Release Verification Checklist" not in r.stdout


def test_pre_release_verify_shell_wiring():
    sh = (RELEASE_DIR / "pre-release-verify.sh").read_text()
    assert sh.index("eval_gate.py skip-check") < sh.index("Pre-Release Verification Checklist")
    assert "eval_gate.py compare" in sh
    assert sh.index("scripts/release/quality_gate.py") < sh.index("eval_gate.py compare") \
        < sh.index("scripts/release/outcome_audit.py")
    gate_block = sh[sh.index("12b."):sh.index("# 13.")]
    assert "--skip" not in gate_block and "SKIP_" not in gate_block  # no skip path for the gate
    assert "exit 1" in gate_block


def test_the_duplicate_pre_release_script_is_gone():
    assert not (REPO / "scripts" / "pre-release-verify.sh").exists()
    assert (RELEASE_DIR / "pre-release-verify.sh").is_file()


def test_override_env_var_is_registered():
    from llm_router import env_registry
    assert env_registry.category_of("LLM_ROUTER_RELEASE_OWNER_OVERRIDE") == "llm_router"


# ── workflows ─────────────────────────────────────────────────────────────────

def _wf(name):
    doc = yaml.safe_load((REPO / ".github" / "workflows" / name).read_text())
    doc["on"] = doc.get("on", doc.get(True))  # PyYAML reads the key `on` as True
    return doc


def test_release_workflow_gates_on_the_sha_file_and_runs_the_gate():
    wf = _wf("release.yml")
    assert "v*" in wf["on"]["push"]["tags"] and "workflow_call" in wf["on"]
    text = (REPO / ".github" / "workflows" / "release.yml").read_text()
    assert "github.sha" in text and ".json" in text
    assert "scripts/release/eval_gate.py compare" in text
    assert "--sha" in text
    assert "secrets." not in text
    assert wf["permissions"] == {"contents": "read"}


def test_publish_needs_the_release_gate():
    wf = _wf("publish.yml")
    assert wf["jobs"]["eval-gate"]["uses"] == "./.github/workflows/release.yml"
    needs = wf["jobs"]["publish"]["needs"]
    assert "eval-gate" in ([needs] if isinstance(needs, str) else needs)


def test_pr_ci_job_runs_comparator_stability_and_pack_goldens():
    wf = _wf("ci.yml")
    job = wf["jobs"]["release-eval-gate"]
    run = "\n".join(s.get("run", "") for s in job["steps"])
    for needle in ("tests/test_release_eval_gate.py", "tests/test_gates_mutation_coverage.py",
                   "tests/test_context_pack.py", "tests/proxy/test_tier_decisions_golden.py"):
        assert needle in run, needle
    for fixture in ("tests/fixtures/context_pack/golden_30.jsonl",
                    "tests/fixtures/proxy/golden_tier_decisions_m05.json"):
        assert (REPO / fixture).is_file(), fixture


def test_release_sh_attaches_the_eval_file_before_tagging():
    sh = (RELEASE_DIR / "release.sh").read_text()
    assert sh.index('eval_asset="release_evals/') < sh.index('git tag "v${current_version}"')
    assert '"$eval_asset"' in sh.split("gh release create")[1].splitlines()[0]


# ── F46/F47 file level: write validates, pin CLI, gate CLI refuses without a pin ──

def test_write_cli_refuses_non_bool_results(R, tmp_path, capsys):
    m, _, cand = _perf(R)
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"perf": cand}))
    assert R.main(["write", "--sha", SHA_A, "--results", str(good), "--out-dir", str(tmp_path / "o1"),
                   "--manifests", str(tmp_path / "none")]) == 0
    for bad in ("false", 1):
        (tmp_path / "bad.json").write_text(json.dumps({"perf": [dict(i, **{"pass": bad}) for i in cand]}))
        assert R.main(["write", "--sha", SHA_A, "--results", str(tmp_path / "bad.json"),
                       "--out-dir", str(tmp_path / "o2"), "--manifests", str(tmp_path / "none")]) == 1
    assert not (tmp_path / "o2").exists()
    assert "invalid item values" in capsys.readouterr().err


def test_pin_cli_roundtrip_and_cli_gate_needs_the_pin(G, R, tmp_path, capsys):
    _write_case(R, tmp_path)
    (tmp_path / "PIN.json").unlink()
    assert _cli(G, tmp_path, "--sha", SHA_A) == 2          # manifests, no pin
    assert "provenance" in capsys.readouterr().out
    assert R.main(["pin", "--manifests", str(tmp_path / "manifests"),
                   "--baseline", str(tmp_path / "base.json"), "--out", str(tmp_path / "PIN.json")]) == 0
    assert _cli(G, tmp_path, "--sha", SHA_A) == 0
    (tmp_path / "manifests" / "perf.json").unlink()        # delete the manifest after the freeze
    capsys.readouterr()
    assert _cli(G, tmp_path, "--sha", SHA_A) == 2
    assert R.main(["pin", "--manifests", str(tmp_path / "manifests"),
                   "--baseline", str(tmp_path / "base.json"), "--out", str(tmp_path / "P2.json")]) == 1


def test_release_workflow_passes_the_pin():
    assert "--pin release_evals/PIN.json" in (REPO / ".github" / "workflows" / "release.yml").read_text()
