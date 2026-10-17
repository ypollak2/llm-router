"""PREREG-P32 harness (scripts/release/run_p32.py): loaders, outcomes, stability, refusals, mutants.

Model-free and offline. Every check takes the module under test, so the same assertions judge
the real file (green) and each mutated copy (must go red); the mutant table is at the bottom.
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
RELEASE_DIR = REPO / "scripts" / "release"
SRC = RELEASE_DIR / "run_p32.py"
SHA_A = "a" * 40


def _load(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))   # the copy's sibling release_evals.py
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(path.parent))
    mod.ROOT = REPO
    return mod


def _rel():
    return _load(RELEASE_DIR / "release_evals.py", "_p32_release_evals")


def _git_repo(tmp: pathlib.Path) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", str(tmp)], check=True, env=env)
    (tmp / "f").write_text("x")
    subprocess.run(["git", "-C", str(tmp), "add", "f"], check=True, env=env)
    subprocess.run(["git", "-C", str(tmp), "commit", "-q", "-m", "c"], check=True, env=env)
    return subprocess.run(["git", "-C", str(tmp), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()


# ── loaders ───────────────────────────────────────────────────────────────────

def check_loaders_n_unique_prefix(M):
    prefix = {"rt_hand_60": "cg-", "rt_agt_50": "a1-", "fid_syn_60": "fid-", "stab_syn_500": "st-"}
    assert set(M.SETS) == set(prefix)
    for sid, (kind, n) in M.SETS.items():
        ids = M.item_ids(sid, REPO)
        assert len(ids) == n, (sid, len(ids))
        assert len(set(ids)) == n, f"{sid}: duplicate ids"
        assert all(i.startswith(prefix[sid]) and len(i) > len(prefix[sid]) for i in ids), sid


def check_ids_stable_across_runs_and_order(M):
    for sid in M.SETS:
        assert M.item_ids(sid, REPO) == M.item_ids(sid, REPO), f"{sid}: ids differ between runs"
    # content-addressed hand ids: reversing the lists changes no id -> text pairing
    lists = M.parse_gate_lists((REPO / M.GATE_REL).read_text())
    fx, fl = lists["FIXTURES"][0], lists["FLOOR_FIXTURES"][0]
    fwd = {i["id"]: i["text"] for i in M.hand_items(fx, fl)}
    rev = {i["id"]: i["text"] for i in M.hand_items(fx[::-1], fl[::-1])}
    assert fwd == rev, "hand ids must not depend on position"
    # a1 ids come from each record's own "i", not its line number
    lines = (REPO / M.AGT_REL).read_text().splitlines()
    with tempfile.TemporaryDirectory() as t:
        shuffled = pathlib.Path(t) / "a1.jsonl"
        shuffled.write_text("\n".join(lines[::-1]) + "\n")
        assert {i["id"]: i["text"] for i in M.load_agt(path=shuffled)} == \
               {i["id"]: i["text"] for i in M.load_agt(REPO)}, "a1 ids must not depend on line order"


def check_stab_generator_pinned_and_hashseed_independent(M):
    items = M.stab_items(REPO)
    assert len(items) == M.STAB_N and len({i["id"] for i in items}) == M.STAB_N
    assert M.stab_texts_hash(items) == M.STAB_GEN_TEXTS_SHA256
    fixture_texts = {i["text"] for i in M.load_hand(REPO) + M.load_agt(REPO)}
    assert fixture_texts <= {i["text"] for i in items}
    out = subprocess.run(
        [sys.executable, "-c",
         f"import sys; sys.path.insert(0, {str(pathlib.Path(M.__file__).parent)!r}); import run_p32 as m; "
         f"print(m.stab_texts_hash(m.stab_items(__import__('pathlib').Path({str(REPO)!r}))))"],
        env={**os.environ, "PYTHONHASHSEED": "7"}, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == M.STAB_GEN_TEXTS_SHA256


# ── outcomes ──────────────────────────────────────────────────────────────────

def check_hand_outcome(M):
    ok = {"id": "x1", "text": "implement a function that reverses a doubly linked list in place", "policy": "ROUTER_POLICY",
          "task_type": "code", "complexity": "moderate"}
    wrong_label = dict(ok, id="x2", complexity="simple")
    wrong_type = dict(ok, id="x3", task_type="image")
    got = {r["id"]: r["pass"] for r in M.score_hand([ok, wrong_label, wrong_type])}
    assert got == {"x1": True, "x2": False, "x3": False}
    assert all(type(v) is bool for v in got.values())


def check_agt_outcome(M):
    ok = {"id": "a1-x", "text": "What is the capital of Brazil?", "task_type": "query", "tier": "haiku"}
    bad = dict(ok, id="a1-y", task_type="image")
    tier_ignored = dict(ok, id="a1-z", tier="opus")
    got = {r["id"]: r["pass"] for r in M.score_agt([ok, bad, tier_ignored])}
    assert got == {"a1-x": True, "a1-y": False, "a1-z": True}


def check_fidelity_retention_outcome(M):
    assert M.retained("quokka-ember-123", "port is Quokka-Ember-123.") is True
    assert M.retained("quokka-ember-123", "port is quokka-ember-124") is False
    rows = M.run_fid(REPO, indices=[0, 30, 40, 50])      # recent, early_user, early_tool, early_assistant
    assert [r["id"] for r in rows] == ["fid-0", "fid-30", "fid-40", "fid-50"]
    assert all(r["full"] is True for r in rows), "the token is in the FULL context by construction"
    assert all(type(r["pack"]) is bool for r in rows)


# ── stability ─────────────────────────────────────────────────────────────────

def _stub(items, seed, flip=()):
    dec = {i["id"]: "d" for i in items}
    if seed == 1:
        for k in flip:
            dec[k] = "different"
    return {"pid": 1000 + seed, "hashseed": str(seed), "net_violations": 0, "decisions": dec}


def check_stability_detects_nondeterministic_stub(M):
    items = [{"id": f"st-{i}", "text": f"t{i}"} for i in range(20)]
    clean, _ = M.run_stability(items, spawn=lambda it, s: _stub(it, s))
    assert all(r["a"] == r["b"] for r in clean)
    flip = {"st-3", "st-9", "st-17"}
    rows, meta = M.run_stability(items, spawn=lambda it, s: _stub(it, s, flip))
    assert {r["id"] for r in rows if r["a"] != r["b"]} == flip
    assert meta["hashseeds"] == [0, 1]


def check_stability_refuses_bad_children(M):
    items = [{"id": "st-0", "text": "t"}]
    for bad in (lambda it, s: dict(_stub(it, s), pid=os.getpid()),            # ran in this process
                lambda it, s: dict(_stub(it, s), hashseed="0"),               # wrong PYTHONHASHSEED
                lambda it, s: dict(_stub(it, s), net_violations=1),           # touched the network
                lambda it, s: dict(_stub(it, s), decisions={}),               # decided nothing
                lambda it, s: dict(_stub(it, s), pid=7)):                     # both runs one process
        with pytest.raises(RuntimeError):
            M.run_stability(items, spawn=bad)


def check_stability_real_children_are_separate_processes(M):
    items = (M.load_hand(REPO)[:4] + M.load_agt(REPO)[:2] + M.stab_items(REPO)[-4:])
    rows, meta = M.run_stability([{"id": i["id"], "text": i["text"]} for i in items])
    assert len(rows) == 10 and all(r["a"] == r["b"] for r in rows)
    assert os.getpid() not in meta["pids"] and meta["pids"][0] != meta["pids"][1]
    d = json.loads(rows[0]["a"])
    assert set(d) == {"HOOK_POLICY", "ROUTER_POLICY"} and len(d["HOOK_POLICY"]) == 4


# ── refusals (§5) and the end to end file ─────────────────────────────────────

def check_provenance_refusals(M):
    with tempfile.TemporaryDirectory() as t:
        wt = pathlib.Path(t)
        head = _git_repo(wt)
        pkg = str(wt / "src" / "llm_router" / "__init__.py")
        assert M.check_provenance(wt, head, pkg) == []
        assert M.check_provenance(wt, SHA_A, pkg), "HEAD != --sha must refuse"
        assert M.check_provenance(wt, head, "/elsewhere/llm_router/__init__.py"), "package outside worktree must refuse"
        assert M.check_provenance(wt, "abc", pkg), "a malformed --sha must refuse"


def check_dirty_tree_refusal(M):
    with tempfile.TemporaryDirectory() as t:
        wt = pathlib.Path(t)
        head = _git_repo(wt)
        pkg = str(wt / "src" / "llm_router" / "__init__.py")
        (wt / "f").write_text("changed")                                   # tracked file modified
        assert M.check_provenance(wt, head, pkg), "dirty tree must refuse"
        assert M.check_provenance(wt, head, pkg, "candidate"), "dirty tree must refuse for a candidate"
        (wt / "f").write_text("x")
        (wt / "untracked").write_text("u")                                 # untracked is not dirty
        assert M.check_provenance(wt, head, pkg, "candidate") == []
        (wt / "release_evals").mkdir()
        (wt / "release_evals" / "o.json").write_text("{}")
        subprocess.run(["git", "-C", str(wt), "add", "-f", "release_evals/o.json"], check=True)
        subprocess.run(["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "o"],
                       check=True)
        head = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
        (wt / "release_evals" / "o.json").write_text("[]")                 # output location edited
        assert M.check_provenance(wt, head, pkg, "candidate") == [], "release_evals/ is ignored for a candidate"
        assert M.check_provenance(wt, head, pkg, "baseline"), "but not for a baseline"


def check_candidate_worktree_must_be_root(M):
    with tempfile.TemporaryDirectory() as t:
        other = pathlib.Path(t)
        assert M.check_root("candidate", REPO, REPO) == []
        assert M.check_root("candidate", other, REPO), "candidate worktree != ROOT must refuse"
        assert M.check_root("baseline", other, REPO) == [], "a baseline worktree is intentionally elsewhere"
        head = _git_repo(other)
        out = other / "o.json"
        assert M.main(["--role", "candidate", "--sha", head, "--sets", "rt_hand_60", "--worktree", str(other),
                       "--out", str(out)]) == 1
        assert not out.exists()


@contextlib.contextmanager
def _offline_synthetic(M):
    """Synthetic stand-in sets and a clean-tree answer: a CLI check must neither score a real set nor
    depend on whether the dev tree has uncommitted edits."""
    saved = M.load_set, M.dirty_paths
    M.load_set, M.dirty_paths = _synthetic_load_set, lambda *a, **k: []
    try:
        yield
    finally:
        M.load_set, M.dirty_paths = saved


def check_cli_refuses_and_writes_nothing(M):
    with _offline_synthetic(M):
        with tempfile.TemporaryDirectory() as t:
            out = pathlib.Path(t) / "o.json"
            base = ["--sets", "rt_hand_60", "--worktree", str(REPO), "--out", str(out)]
            assert M.main(["--role", "candidate", "--sha", SHA_A, *base]) == 1       # HEAD is not aaaa...
            # provenance is satisfied here (real HEAD), so only the baseline rule can refuse
            head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True,
                                  check=True).stdout.strip()
            for sets in ("fid_syn_60", "stab_syn_500", "rt_hand_60,fid_syn_60"):
                assert M.main(["--role", "baseline", "--sha", head, "--sets", sets, "--worktree", str(REPO),
                               "--out", str(out)]) == 1
            assert not out.exists()


def _synthetic_load_set(set_id, root=REPO):
    """Invented stand-ins (not the real FIXTURES / a1 items), so the e2e path never scores a real set."""
    if set_id == "rt_hand_60":
        return [{"id": f"cg-syn{i}", "text": t, "policy": "ROUTER_POLICY", "task_type": tt, "complexity": cx}
                for i, (t, tt, cx) in enumerate([("implement a function that reverses a doubly linked list in place", "code", "moderate"),
                                                 ("what is the tallest mountain in Norway", "query", "simple"),
                                                 ("zzz qqq unlabeled nonsense", "code", "complex")])]
    if set_id == "rt_agt_50":
        return [{"id": f"a1-syn{i}", "text": t, "task_type": tt, "tier": "haiku"}
                for i, (t, tt) in enumerate([("What is the capital of Brazil?", "query"),
                                             ("write a haiku about autumn rain", "generate")])]
    raise ValueError(set_id)


def check_end_to_end_file_passes_the_gate_and_manifests_match_freeze(M):
    rel = _rel()
    gate = _load(RELEASE_DIR / "eval_gate.py", "_p32_gate_for_e2e")
    head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()
    sets = ["rt_hand_60", "rt_agt_50"]
    with tempfile.TemporaryDirectory() as t:
        t = pathlib.Path(t)
        out = t / f"{head}.json"
        with _offline_synthetic(M):
            assert M.main(["--role", "candidate", "--sha", head, "--sets", ",".join(sets), "--worktree", str(REPO),
                           "--out", str(out)]) == 0
        doc = json.loads(out.read_text())
        assert doc["sha"] == head and set(doc["sets"]) == set(sets)
        prov = doc["provenance"]
        assert prov["git_sha"] == head and str(REPO) in prov["llm_router_file"] and prov["item_file_sha256"]
        # manifests: same bytes as `release_evals.py freeze`
        mdir = t / "manifests"
        M.write_manifests(REPO, sets, mdir)
        for sid in sets:
            ids_file = t / f"{sid}.ids"
            ids_file.write_text("\n".join(M.item_ids(sid, REPO)) + "\n")
            assert rel.main(["freeze", "--set-id", sid, "--kind", M.SETS[sid][0], "--min-n", str(M.SETS[sid][1]),
                             "--ids-file", str(ids_file), "--out-dir", str(t / "frozen")]) == 0
            assert (mdir / f"{sid}.json").read_bytes() == (t / "frozen" / f"{sid}.json").read_bytes()
        manifests = rel.load_manifests(mdir)
        assert all(rel.manifest_problems(m) == [] and m["n"] == m["min_n"] for m in manifests.values())
        # the gate accepts the file as candidate and as baseline (identical -> PASS). The file holds only
        # synthetic stand-ins, so the gate is judged against manifests frozen over the same stand-in ids.
        manifests = {sid: rel.make_manifest(sid, M.SETS[sid][0], len(doc["sets"][sid]["items"]),
                                            [i["id"] for i in doc["sets"][sid]["items"]]) for sid in sets}
        pin = rel.make_pin(manifests, head, "c" * 64)
        code, lines = gate.run_gate(doc, doc, manifests, expect_sha=head, pin=pin, prereg_sidecar="c" * 64)
        assert code == gate.PASS, lines


def check_dry_run_counts(M):
    ref = "v15.4.0" if subprocess.run(["git", "-C", str(REPO), "rev-parse", "-q", "--verify", "v15.4.0^{commit}"],
                                      capture_output=True).returncode == 0 else "HEAD"   # shallow clone: no tag
    code, lines = M.dry_run(REPO, list(M.SETS), ref)
    text = "\n".join(lines)
    assert code == 0, text
    for sid, (_k, n) in M.SETS.items():
        assert f"{sid} " in text and f"n={n} expected={n} unique_ids={n}" in text
    assert "identical=True" in text and "matches_pin=True" in text
    assert "no item scored" in text


def check_fixtures_identity_detects_an_edit(M):
    src = (REPO / M.GATE_REL).read_text()
    same = M.fixtures_identity(src, src)
    assert all(v["identical"] for v in same.values()) and same["FIXTURES"]["n_old"] == 56
    edited = src.replace('("draft release notes for v15.2.0", "generate", "moderate")',
                         '("draft release notes for v15.2.0", "generate", "complex")')
    assert edited != src
    ident = M.fixtures_identity(src, edited)
    assert ident["FIXTURES"]["identical"] is False and ident["FLOOR_FIXTURES"]["identical"] is True


def _real_texts() -> frozenset:
    m = _load(SRC, "_p32_texts")
    return frozenset(i["text"] for i in m.load_hand(REPO) + m.load_agt(REPO))


_REAL_TEXTS = _real_texts()    # texts only; nothing is scored here

CHECKS = {fn.__name__: fn for fn in (
    check_loaders_n_unique_prefix, check_ids_stable_across_runs_and_order,
    check_stab_generator_pinned_and_hashseed_independent, check_hand_outcome, check_agt_outcome,
    check_fidelity_retention_outcome, check_stability_detects_nondeterministic_stub,
    check_stability_refuses_bad_children, check_stability_real_children_are_separate_processes,
    check_provenance_refusals, check_dirty_tree_refusal, check_candidate_worktree_must_be_root,
    check_cli_refuses_and_writes_nothing,
    check_end_to_end_file_passes_the_gate_and_manifests_match_freeze, check_dry_run_counts,
    check_fixtures_identity_detects_an_edit,
)}


@pytest.fixture(autouse=True)
def forbid_scoring_real_items(monkeypatch):
    """PREREG-P32: no real set may be scored before the freeze. Every test in this file runs with
    classify_signals wrapped to raise on any text that belongs to the real hand/a1 sets, so a test
    cannot score them even through a mutated module copy. Texts only are read here (no outcomes)."""
    lists = _REAL_TEXTS
    from llm_router import classify
    real = classify.classify_signals

    def guarded(text, *a, **k):
        if text in lists:
            raise AssertionError("test attempted to score a REAL P32 item before the freeze")
        return real(text, *a, **k)

    monkeypatch.setattr(classify, "classify_signals", guarded)


def test_guard_blocks_scoring_the_real_sets(M):
    hand, agt = M.load_hand(REPO), M.load_agt(REPO)
    with pytest.raises(AssertionError, match="REAL P32"):
        M.score_hand(hand[:1])
    with pytest.raises(AssertionError, match="REAL P32"):
        M.score_agt(agt[:1])
    ok = {"id": "x", "text": "zzz not a real item", "policy": "ROUTER_POLICY", "task_type": "code",
          "complexity": "moderate"}
    assert len(M.score_hand([ok])) == 1                     # synthetic items still score


@pytest.fixture(scope="module")
def M():
    return _load(SRC, "_p32_real")


@pytest.mark.parametrize("name", sorted(CHECKS))
def test_check_green_on_the_real_harness(name, M):
    CHECKS[name](M)


def test_checks_found_something():
    assert len(CHECKS) >= 16 and _REAL_TEXTS   # an empty table would pass everything


# ── mutants: each must turn its named check red ───────────────────────────────

_SAME_PROCESS = [
    ("    res_a, res_b = spawn(items, STAB_HASHSEEDS[0]), spawn(items, STAB_HASHSEEDS[1])\n",
     '    res_a = res_b = {"pid": os.getpid(), "hashseed": "0", "decisions": decide_all(items)}\n'),
    ('        if res.get("pid") == os.getpid():', "        if False:"),
    ('        if str(res.get("hashseed")) != str(seed):', "        if False:"),
    ('    if res_a["pid"] == res_b["pid"]:', "    if False:"),
]
_POSITIONAL_HAND = [
    ("        for prompt, task, cx in lst:\n", "        for pos, (prompt, task, cx) in enumerate(lst):\n"),
    ('"id": f"cg-{_sha(policy + \'|\' + prompt)[:12]}"', '"id": f"cg-{key}{pos}"'),
]
_POSITIONAL_A1 = [
    ("    for line in (path or Path(root) / AGT_REL).read_text().splitlines():\n",
     "    for pos, line in enumerate((path or Path(root) / AGT_REL).read_text().splitlines()):\n"),
    ('"id": f"a1-{r[\'i\']}"', '"id": f"a1-{pos}"'),
]

MUTANTS = [
    ("MA drop the HEAD/sha check", [("    if head != sha:\n", "    if False:\n")],
     "check_provenance_refusals"),
    ("MA2 drop the package-under-worktree check", [("        Path(pkg_file).resolve().relative_to(Path(worktree).resolve())\n",
                                                    "        pass\n")],
     "check_provenance_refusals"),
    ("MH drop the dirty-tree check", [("    if dirty:\n", "    if False:\n")], "check_dirty_tree_refusal"),
    ("MH2 candidate ignores more than release_evals/", [('if role == "candidate":', "if True:")],
     "check_dirty_tree_refusal"),
    ("MI drop the candidate worktree==ROOT check", [('if role == "candidate" and Path(worktree)', "if False and Path(worktree)")],
     "check_candidate_worktree_must_be_root"),
    ("MB positional hand ids", _POSITIONAL_HAND, "check_ids_stable_across_runs_and_order"),
    ("MB2 positional a1 ids", _POSITIONAL_A1, "check_ids_stable_across_runs_and_order"),
    ("MC stability run in the same process", _SAME_PROCESS, "check_stability_detects_nondeterministic_stub"),
    ("MC2 stability children not given PYTHONHASHSEED", [('"PYTHONHASHSEED": str(hashseed)', '"PYTHONHASHSEED": "0"')],
     "check_stability_real_children_are_separate_processes"),
    ("ME baseline allowed non-performance sets", [('if a.role == "baseline" and any(', 'if False and any(')],
     "check_cli_refuses_and_writes_nothing"),
    ("MF hand outcome ignores complexity", [('and s.complexity.value == it["complexity"])', ")")],
     "check_hand_outcome"),
    ("MG fixtures identity always true", [('"identical": a[name][1].encode() == b[name][1].encode()', '"identical": True')],
     "check_fixtures_identity_detects_an_edit"),
]


def test_unmutated_copy_is_green_on_every_check(tmp_path):
    """Negative control: a verbatim copy passes every check, and the mutant table is non-empty."""
    (tmp_path / "release_evals.py").write_text((RELEASE_DIR / "release_evals.py").read_text())
    (tmp_path / "run_p32.py").write_text(SRC.read_text())
    mod = _load(tmp_path / "run_p32.py", "_p32_clean")
    assert MUTANTS and CHECKS
    for check in CHECKS.values():
        check(mod)


@pytest.mark.parametrize("mid,edits,killer", MUTANTS, ids=[m[0].split()[0] for m in MUTANTS])
def test_mutant_turns_its_check_red(tmp_path, mid, edits, killer):
    src = SRC.read_text()
    for old, new in edits:
        assert src.count(old) == 1, f"{mid}: anchor must match exactly once: {old!r}"
        src = src.replace(old, new)
    (tmp_path / "release_evals.py").write_text((RELEASE_DIR / "release_evals.py").read_text())
    (tmp_path / "run_p32.py").write_text(src)
    mod = _load(tmp_path / "run_p32.py", "_p32_mut")
    failed = []
    for name, check in CHECKS.items():
        try:
            check(mod)
        except (Exception, pytest.fail.Exception):
            failed.append(name)
    assert killer in failed, f"{mid}: killed by {failed}, expected {killer}"
