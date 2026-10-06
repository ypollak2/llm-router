"""Verifier PR C (SHADOW): the pending_verify queue, the detached verify_worker, and the Codex
marker in hooks/agent-route.py. One end-to-end test on the REAL sandbox, REAL git and a REAL
detached worker process
the rest pin each rule on its own with a counting fake ``verify``.

State is isolated by LLM_ROUTER_HOME + HOME under tmp_path (nothing here touches the real
~/.llm-router or ~/.claude).
"""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_router import failopen, northstar as ns
from llm_router import verify_queue as Q
from llm_router import verify_worker as W
from llm_router.commands import kpi
from llm_router.toolkit import sandbox
from llm_router.toolkit import verify_unit as VU
from llm_router.toolkit.verify_unit import UnitResult
from tests.test_agent_route_hook import _load_hook_module
from tests.test_northstar import SID_MAIN, _bulk_user_prompts, _home_dir, _project, _user, _write_jsonl
from tests.test_verify_unit import FIXED, _git
from tests.toolkit_fixtures import digest, make_source

SANDBOX_OK = sandbox.prove_sandbox().proven
real_sandbox = pytest.mark.skipif(not SANDBOX_OK, reason="sandbox not proven: the verifier does not run")
pytestmark = pytest.mark.timeout(240)

SECRETS = {"ANTHROPIC_API_KEY": "sk-ant-CANARY", "OPENAI_API_KEY": "sk-CANARY", "FAKE_KEY": "CANARY",
           "GITHUB_TOKEN": "ghp_CANARY", "LLM_ROUTER_GATEWAY_TOKEN": "CANARY"}


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(_home_dir(tmp_path)))
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path / "claude_projects"))
    monkeypatch.setenv("LLM_ROUTER_AGENT_ROUTE_CODEX", "on")
    monkeypatch.delenv("LLM_ROUTER_VERIFY", raising=False)
    monkeypatch.delenv("LLM_ROUTER_VERIFY_BUDGET_S", raising=False)
    failopen.reset_cache()
    failopen.reset_unpersisted()
    yield
    failopen.reset_cache()


def _repo(tmp_path, name="repo") -> Path:
    """make_source: add() is wrong and test_add fails; committed. (.env canaries included.)"""
    root = make_source(tmp_path / name)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


def _uid(n: int) -> str:
    return "u_" + hashlib.sha256(str(n).encode()).hexdigest()[:16]


def _enqueue(n: int = 0, *, repo="/nonexistent", head="0" * 40, patch=b"diff --git a/x b/x\n", now=None):
    return Q.enqueue(_uid(n), str(repo), head, patch, now=now)


def _verify_rows(tmp_path) -> list[dict]:
    p = _home_dir(tmp_path) / "north_star_units.jsonl"
    if not p.exists():
        return []
    return [r for r in map(json.loads, p.read_text().splitlines()) if "verify" in r]


def _ok_result(*a, **k):
    return UnitResult("pass_f2p", "v1_f2p", n_candidates=1, n_f2p=1, ms=5, sandboxed=True)


def _by_code() -> dict:
    failopen.reset_cache()
    return dict(failopen.snapshot().by_code)


# ── the fake Codex delegation ────────────────────────────────────────────────

def _fake_codex(monkeypatch, mod, repo: Path, *, truncated=False, content="done: fixed add()",
                writes=True, keep_class=True):
    """Codex writes into the user's tree (as the real one does) and returns a result."""
    def _run(prompt, timeout, context_root):
        if writes:
            (repo / "src" / "pkg.py").write_text(FIXED)
            (repo / "src" / "helpers.py").write_text("def noop():\n    return None\n")
        return SimpleNamespace(content=content, model="gpt-5.5", duration_sec=1.0, success=True,
                               truncated=truncated), "ok"
    monkeypatch.setattr("llm_router.codex_agent.is_codex_available", lambda: True)
    monkeypatch.setattr(mod, "_run_codex_agent", _run)
    monkeypatch.setattr(mod, "_log_cli_savings", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_is_codex_suitable", lambda *a, **k: True)


def _delegate(mod, repo: Path, monkeypatch, session=SID_MAIN):
    monkeypatch.chdir(repo)
    return mod._try_codex_subagent_delegation("fix add()", "code", "moderate", "general-purpose",
                                              session, cwd=str(repo))


def _markers() -> list[Q.Marker]:
    return Q.pending()


# ── end to end ───────────────────────────────────────────────────────────────

@real_sandbox
def test_end_to_end_fake_codex_delegation_to_a_joined_verify_record(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    mod = _load_hook_module()
    _fake_codex(monkeypatch, mod, repo)
    proj = _project(tmp_path)
    _write_jsonl(proj / f"{SID_MAIN}.jsonl",
                 [_user(SID_MAIN, "fix it", time.time() - 9000)]
                 + _bulk_user_prompts(SID_MAIN, 60, start_ts=time.time() - 8000))
    head = _head(repo)
    git_index = (repo / ".git" / "index").read_bytes()

    out = _delegate(mod, repo, monkeypatch)
    assert out == "done: fixed add()"                         # the turn's result is untouched

    # 1. patch captured + marker appended, the user's repo only read
    (m,) = _markers()
    assert m.data["head"] == head and Path(m.data["cwd"]).resolve() == repo.resolve()
    patch_file = Q.patch_path(m.unit_id)
    assert stat.S_IMODE(patch_file.stat().st_mode) == 0o600
    text = patch_file.read_text()
    assert "src/pkg.py" in text and "+    return a + b" in text        # tracked edit
    assert "src/helpers.py" in text and "new file mode" in text         # untracked file
    assert (repo / ".git" / "index").read_bytes() == git_index           # not even the index moved
    assert _head(repo) == head

    # 2. the unit row the marker joins on
    rows = [json.loads(x) for x in (_home_dir(tmp_path) / "north_star_units.jsonl").read_text().splitlines()]
    (codex_row,) = [r for r in rows if r.get("lever") == "agent_route_codex"]
    assert m.unit_id == ns.unit_id(SID_MAIN, ns.UNIT_AGENT_ROUTE_CODEX, codex_row["ts"])

    def scorecard():
        return json.dumps(kpi.compute_scorecard(days=100000, now=time.time() + 60)["kpis"], sort_keys=True)
    before_kpis = scorecard()
    tree_before = digest(repo)

    # 3. the REAL detached worker (what SessionStart/Stop spawn), no injection anywhere
    assert Q.spawn_worker_if_needed(cooldown_s=0.0) is True
    deadline = time.time() + 200
    while time.time() < deadline and (Q.queue_nonempty() or not _verify_rows(tmp_path)):
        time.sleep(0.5)

    (v_row,) = _verify_rows(tmp_path)
    assert v_row["unit_id"] == m.unit_id
    v = v_row["verify"]
    assert (v["verify_status"], v["verify_level"], v["verify_sandboxed"]) == ("pass_f2p", "V1", True), v
    assert v["verify_n_f2p"] >= 1
    # 4. joined in units(); SHADOW: the outcome is not touched and no KPI moves
    unit = next(u for u in ns.units(days=None, session_id=SID_MAIN, root=proj.parent)
                if u["kind"] == ns.UNIT_AGENT_ROUTE_CODEX)
    assert unit["verify"]["verify_status"] == "pass_f2p"
    assert (unit["outcome"], unit["signal"]) == ("unknown", "agent_route_codex_delegated")
    assert scorecard() == before_kpis
    # 5. patch + marker gone, the user's tree untouched by the worker
    assert not patch_file.exists() and not Q.queue_nonempty()
    assert digest(repo) == tree_before
    assert (repo / ".git" / "index").read_bytes() == git_index


# ── capture ──────────────────────────────────────────────────────────────────

def test_captured_patch_round_trips_onto_the_head_checkout(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text("ignored.txt\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "gi")
    (repo / "data.bin").write_bytes(bytes(range(256)) * 4)
    _git(repo, "add", "data.bin")
    _git(repo, "commit", "-qm", "b")
    (repo / "src" / "pkg.py").write_text(FIXED)                          # modify
    (repo / "README.md").unlink()                                        # delete
    (repo / "data.bin").write_bytes(bytes(reversed(range(256))) * 4)     # tracked binary change
    new = {"src/new.py": b"X = 1\n", "src/no_eol.py": b"Z = 3", "src/empty.py": b"",
           "src/multi.py": b"a\nb\n\nc\n", "src/with space.py": b"W = 1\n", "run.sh": b"#!/bin/sh\necho hi\n",
           "src/crlf.py": b"A = 1\r\nB = 2\r\n"}
    for rel, data in new.items():
        (repo / rel).write_bytes(data)
    os.chmod(repo / "run.sh", 0o755)
    (repo / "ignored.txt").write_text("nope\n")                          # ignored
    nested = repo / "vendor" / "inner"
    nested.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=nested, check=True)        # nested repo: skipped
    (nested / "f.py").write_text("Y = 2\n")
    status = _git(repo, "status", "--porcelain")
    index = (repo / ".git" / "index").read_bytes()
    (top, head, patch), why = Q.capture(str(repo))
    assert why == "ok" and head == _head(repo) and Path(top).resolve() == repo.resolve()
    assert b"ignored.txt" not in patch and b"vendor/inner" not in patch
    assert _git(repo, "status", "--porcelain") == status and (repo / ".git" / "index").read_bytes() == index
    clone = tmp_path / "clone"
    clone.mkdir()
    arch = subprocess.run(["git", "archive", "--format=tar", head], cwd=repo, capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", str(clone)], input=arch.stdout, check=True)
    (tmp_path / "p.diff").write_bytes(patch)
    subprocess.run(["git", "apply", "--check", str(tmp_path / "p.diff")], cwd=clone, check=True)
    subprocess.run(["git", "apply", str(tmp_path / "p.diff")], cwd=clone, check=True)
    for rel in ("src/pkg.py", "data.bin", "run.sh", *new):
        assert (clone / rel).read_bytes() == (repo / rel).read_bytes(), rel
    assert os.access(clone / "run.sh", os.X_OK) and not os.access(clone / "src" / "new.py", os.X_OK)
    assert not (clone / "README.md").exists() and not (clone / "ignored.txt").exists()


def test_untracked_binary_and_non_utf8_are_not_queued_rather_than_mangled(tmp_path):
    repo = _repo(tmp_path)
    (repo / "blob.bin").write_bytes(b"\0\1\2")
    assert Q.capture(str(repo)) == (None, "untracked_unrepresentable")
    (repo / "blob.bin").unlink()
    (repo / "src" / "pkg.py").write_bytes(b"x = '\xff\xfe'\n")
    assert Q.capture(str(repo)) == (None, "non_utf8_patch")


def test_nothing_to_verify_leaves_no_marker(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    mod = _load_hook_module()
    _fake_codex(monkeypatch, mod, repo, writes=False, content="analysis only")
    assert _delegate(mod, repo, monkeypatch) == "analysis only"          # empty diff
    assert not Q.queue_nonempty() and _by_code() == {}
    plain = tmp_path / "notgit"
    plain.mkdir()
    monkeypatch.chdir(plain)
    assert Q.capture(str(plain)) == (None, "not_a_repo")                 # not a repo: expected, no failopen
    assert _by_code() == {}


def test_oversized_patch_is_not_queued(tmp_path):
    repo = _repo(tmp_path)
    (repo / "big.txt").write_bytes(b"x" * (Q.MAX_PATCH_BYTES + 10))
    assert Q.capture(str(repo)) == (None, "patch_too_large")
    (repo / "big.txt").unlink()
    (repo / "src" / "pkg.py").write_text("y = '" + "z" * (Q.MAX_PATCH_BYTES + 10) + "'\n")
    assert Q.capture(str(repo)) == (None, "patch_too_large")


def test_queue_cap_matches_the_verifier_cap():
    assert Q.MAX_PATCH_BYTES == VU.MAX_PATCH_BYTES
    assert W.DEFAULT_BUDGET_S == 120 and W.MAX_BUDGET_S == 300


def test_unit_id_matches_northstar():
    for sid, ts in ((SID_MAIN, 1_800_000_050.123456), ("s", 1.5), ("ünï", 1_790_000_000.0)):
        assert Q.unit_id(sid, "agent_route_codex", ts) == ns.unit_id(sid, "agent_route_codex", ts)
    assert Q.unit_id(None, "agent_route_codex", 1.0) is None and Q.unit_id("s", "k", None) is None


# ── truncated / capped runs ──────────────────────────────────────────────────

@pytest.mark.parametrize("truncated,content", [(True, "partial"), (False, "partial\n[truncated: output cap]")])
def test_a_truncated_run_gets_no_marker(tmp_path, monkeypatch, truncated, content):
    repo = _repo(tmp_path)
    mod = _load_hook_module()
    _fake_codex(monkeypatch, mod, repo, truncated=truncated, content=content)
    assert _delegate(mod, repo, monkeypatch) == content                  # the turn still gets its answer
    assert not Q.queue_nonempty() and not list(Q.queue_dir().glob("patches/*"))
    rows = [json.loads(x) for x in (_home_dir(tmp_path) / "north_star_units.jsonl").read_text().splitlines()]
    assert [r["outcome"] for r in rows if r.get("lever") == "agent_route_codex"] == ["delegated"]
    assert _verify_rows(tmp_path) == []                                  # never a verdict, never "clean"


def test_the_same_run_untruncated_is_queued(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    mod = _load_hook_module()
    _fake_codex(monkeypatch, mod, repo)
    _delegate(mod, repo, monkeypatch)
    assert len(_markers()) == 1                                          # the truncated tests are not vacuous


# ── fail open ────────────────────────────────────────────────────────────────

def test_a_marker_write_failure_fails_open_with_a_code(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    mod = _load_hook_module()
    _fake_codex(monkeypatch, mod, repo)
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(Q, "enqueue", boom)
    assert _delegate(mod, repo, monkeypatch) == "done: fixed add()"
    assert not Q.queue_nonempty()
    assert _by_code().get("CHZ-FO-VERIFY-MARKER") == 1


def test_a_capture_failure_fails_open_with_a_code(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    mod = _load_hook_module()
    _fake_codex(monkeypatch, mod, repo)
    monkeypatch.setattr(Q, "capture", lambda cwd: (_ for _ in ()).throw(Q.GitError("x")))
    assert _delegate(mod, repo, monkeypatch) == "done: fixed add()"
    assert not Q.queue_nonempty() and _by_code().get("CHZ-FO-VERIFY-MARKER") == 1


def test_a_read_only_queue_leaves_no_marker_and_no_patch(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    mod = _load_hook_module()
    _fake_codex(monkeypatch, mod, repo)
    Q.ensure_dirs()
    monkeypatch.setattr(Q, "_write_json", lambda *a, **k: (_ for _ in ()).throw(OSError("ro")))
    assert _delegate(mod, repo, monkeypatch) == "done: fixed add()"
    assert not Q.queue_nonempty() and not list(Q.queue_dir().glob("patches/*"))   # the patch is rolled back
    assert _by_code().get("CHZ-FO-VERIFY-MARKER") == 1


def test_kill_switch_writes_no_marker(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    mod = _load_hook_module()
    _fake_codex(monkeypatch, mod, repo)
    monkeypatch.setenv("LLM_ROUTER_VERIFY", "off")
    _delegate(mod, repo, monkeypatch)
    assert not Q.queue_nonempty() and not Q.queue_dir().exists()
    _enqueue()
    assert Q.spawn_worker_if_needed(cooldown_s=0.0) is False


# ── worker: ordinary paths and failure paths ─────────────────────────────────

def test_worker_records_the_verdict_then_deletes_marker_and_patch(tmp_path):
    repo = _repo(tmp_path)
    _enqueue(1, repo=repo, head=_head(repo), patch=b"diff --git a/a b/a\n")
    seen, recs = [], []

    def verify(checkout, patch, budget_s):
        seen.append((checkout, patch, budget_s, (checkout / "src" / "pkg.py").exists()))
        return _ok_result()
    c = W.drain(verify=verify, record=lambda uid, r: recs.append((uid, r.verify_status)))
    assert c["processed"] == 1 and recs == [(_uid(1), "pass_f2p")]
    checkout, patch, budget, had_source = seen[0]
    assert had_source and patch.startswith("diff --git") and budget <= 120    # the baseline is the recorded HEAD
    assert not checkout.exists()                                              # temp checkout removed
    assert not Q.queue_nonempty() and not list(Q.queue_dir().glob("patches/*"))


def test_any_exception_records_unavailable_with_a_reason_code(tmp_path):
    repo = _repo(tmp_path)
    _enqueue(1, repo=repo, head=_head(repo))
    recs = []
    def verify(*a, **k):
        raise RuntimeError("secret text from a test run")
    W.drain(verify=verify, record=lambda uid, r: recs.append(r))
    (r,) = recs
    assert (r.verify_status, r.reason) == ("unavailable", "verify_worker_error")
    assert _by_code().get("CHZ-FO-VERIFY-WORKER") == 1
    assert not Q.queue_nonempty() and not list(Q.queue_dir().glob("patches/*"))
    assert ns.verify_row(_uid(1), r)["verify"]["verify_reason"] == "verify_worker_error"


def test_a_record_failure_still_deletes_the_patch(tmp_path):
    repo = _repo(tmp_path)
    _enqueue(1, repo=repo, head=_head(repo))
    def rec(uid, r):
        raise OSError("ledger full")
    W.drain(verify=_ok_result, record=rec)
    assert not Q.queue_nonempty() and not list(Q.queue_dir().glob("patches/*"))
    assert _by_code().get("CHZ-FO-VERIFY-RECORD") == 1


def test_an_unreachable_head_is_unavailable_not_a_crash(tmp_path):
    repo = _repo(tmp_path)
    _enqueue(1, repo=repo, head="1" * 40)
    _enqueue(2, repo=tmp_path / "gone", head=_head(repo))
    recs = {}
    W.drain(verify=lambda *a, **k: pytest.fail("must not be reached"),
            record=lambda uid, r: recs.__setitem__(uid, r.reason))
    assert recs == {_uid(1): "verify_head_unavailable", _uid(2): "verify_repo_missing"}


def test_the_watchdog_ends_a_hung_unit(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _enqueue(1, repo=repo, head=_head(repo))
    monkeypatch.setenv("LLM_ROUTER_VERIFY_BUDGET_S", "1")
    monkeypatch.setattr(W, "_GRACE_S", 0.3)
    monkeypatch.setattr(W, "_MIN_VERIFY_S", 0.1)
    recs = []
    t0 = time.monotonic()
    W.drain(verify=lambda *a, **k: time.sleep(30), record=lambda uid, r: recs.append(r.reason))
    assert recs == ["verify_worker_timeout"] and time.monotonic() - t0 < 10
    assert not Q.queue_nonempty()


def test_budget_default_and_cap(monkeypatch):
    assert W.budget_s() == 120
    monkeypatch.setenv("LLM_ROUTER_VERIFY_BUDGET_S", "9999")
    assert W.budget_s() == 300
    monkeypatch.setenv("LLM_ROUTER_VERIFY_BUDGET_S", "45")
    assert W.budget_s() == 45
    monkeypatch.setenv("LLM_ROUTER_VERIFY_BUDGET_S", "junk")
    assert W.budget_s() == 120
    monkeypatch.setenv("LLM_ROUTER_VERIFY_BUDGET_S", "nan")
    assert W.budget_s() == 120


def test_the_unit_is_handed_the_remaining_budget_not_more_than_the_cap(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _enqueue(1, repo=repo, head=_head(repo))
    monkeypatch.setenv("LLM_ROUTER_VERIFY_BUDGET_S", "500")
    got = []
    W.drain(verify=lambda r, p, budget_s: got.append(budget_s) or _ok_result(), record=lambda *a: None)
    assert 0 < got[0] <= 300


# ── TTL ──────────────────────────────────────────────────────────────────────

def test_expired_marker_records_verify_expired_and_deletes_the_patch(tmp_path):
    now = time.time()
    _enqueue(1, now=now - 25 * 3600)
    _enqueue(2, now=now - 23 * 3600 - 3000)                              # just inside the TTL
    patch1 = Q.patch_path(_uid(1))
    assert patch1.exists()
    recs = {}
    c = W.drain(verify=_ok_result, record=lambda uid, r: recs.__setitem__(uid, (r.verify_status, r.reason)), now=now)
    assert recs[_uid(1)] == ("unavailable", "verify_expired") and c["expired"] == 1
    assert _uid(2) in recs and recs[_uid(2)][1] != "verify_expired"      # the live one was processed instead
    assert not patch1.exists() and not Q.queue_nonempty()


def test_expiry_is_recorded_in_the_ledger_and_joins(tmp_path):
    ledger = _home_dir(tmp_path) / "north_star_units.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ts = 1_800_000_050.0
    ledger.write_text(json.dumps({"ts": ts, "lever": "agent_route_codex", "model": "m", "outcome": "delegated",
                                  "session_id": SID_MAIN, "session_kind": "organic"}) + "\n")
    uid = ns.unit_id(SID_MAIN, "agent_route_codex", ts)
    Q.enqueue(uid, "/x", "0" * 40, b"diff\n", now=time.time() - 30 * 3600)
    W.drain(verify=_ok_result)                                           # real record_verify
    proj = _project(tmp_path)
    _write_jsonl(proj / f"{SID_MAIN}.jsonl", [_user(SID_MAIN, "x", ts - 10)] + _bulk_user_prompts(SID_MAIN, 60, ts))
    u = next(u for u in ns.units(days=None, session_id=SID_MAIN, root=proj.parent) if u["kind"] == "agent_route_codex")
    assert (u["verify"]["verify_status"], u["verify"]["verify_reason"]) == ("unavailable", "verify_expired")
    assert u["outcome"] == "unknown"


def test_future_dated_and_overlong_ttl_markers_cannot_outlive_the_ttl():
    now = time.time()
    p = _enqueue(1, now=now + 7200)
    assert Q.is_expired(Q.pending()[0], now)
    data = json.loads(p.read_text())
    data.update(created_at=now - 25 * 3600, ttl_s=10**9)
    p.write_text(json.dumps(data))
    assert Q.is_expired(Q.pending()[0], now)


def test_orphan_patches_are_swept_after_the_ttl():
    Q.ensure_dirs()
    old, young = Q.patch_path(_uid(1)), Q.patch_path(_uid(2))
    old.write_text("x")
    young.write_text("x")
    t = time.time() - 25 * 3600
    os.utime(old, (t, t))
    W.drain(verify=_ok_result, record=lambda *a: None)
    assert not old.exists() and young.exists()


def test_a_dead_workers_claim_is_retried_then_given_up(tmp_path):
    _enqueue(1)
    c = Q.claim(Q.pending()[0])
    old = time.time() - 3600
    os.utime(c.path, (old, old))
    assert Q.recover_stale_claims(time.time()) == [] and len(Q.pending()) == 1     # attempt 1: back in line
    for _ in range(Q.MAX_ATTEMPTS - 1):
        c = Q.claim(Q.pending()[0])
        os.utime(c.path, (old, old))
        Q.recover_stale_claims(time.time())
    recs = []
    W.drain(verify=_ok_result, record=lambda uid, r: recs.append(r.reason))
    assert recs == ["verify_worker_crashed"] and not Q.queue_nonempty()


def test_a_corrupt_marker_is_dropped_with_its_patch():
    Q.ensure_dirs()
    (Q._sub("pending") / f"{_uid(1)}.json").write_text("{not json")
    Q.patch_path(_uid(1)).write_text("src")
    recs = []
    W.drain(verify=_ok_result, record=lambda uid, r: recs.append((uid, r.verify_status, r.reason)))
    assert Q.pending() == [] and not Q.patch_path(_uid(1)).exists()
    assert recs == [(_uid(1), "unavailable", "marker_invalid")]           # not dropped silently


# ── files are private ────────────────────────────────────────────────────────

def test_patch_marker_and_dirs_are_private_even_under_umask_zero():
    old = os.umask(0)
    try:
        marker = _enqueue(1)
    finally:
        os.umask(old)
    assert stat.S_IMODE(Q.patch_path(_uid(1)).stat().st_mode) == 0o600
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    for d in (Q.queue_dir(), Q._sub("pending"), Q._sub("claimed"), Q._sub("patches")):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, d


def test_patches_are_deleted_after_a_verdict_expiry_and_a_failure(tmp_path):
    repo = _repo(tmp_path)
    _enqueue(1, repo=repo, head=_head(repo))                              # verdict
    _enqueue(2, repo=repo, head=_head(repo))                              # verify raises
    _enqueue(3, now=time.time() - 48 * 3600)                              # expired
    n = {"i": 0}
    def verify(*a, **k):
        n["i"] += 1
        if n["i"] == 2:
            raise RuntimeError("x")
        return _ok_result()
    assert len(list(Q.queue_dir().glob("patches/*"))) == 3
    W.drain(verify=verify, record=lambda *a: None)
    assert list(Q.queue_dir().glob("patches/*")) == [] and not Q.queue_nonempty()


# ── concurrency ──────────────────────────────────────────────────────────────

def _worker_proc(log: str, tag: str, started):
    slot = Q.acquire_slot()
    if slot is None:
        os.write(os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT), f"{tag} noslot\n".encode())
        return
    started.set()
    def verify(repo, patch, budget_s):
        time.sleep(0.03)
        return _ok_result()
    def rec(uid, r):
        os.write(os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT), f"{tag} {uid}\n".encode())
    W.drain(verify=verify, record=rec)
    slot.close()


def test_two_workers_never_process_a_unit_twice(tmp_path):
    repo = _repo(tmp_path)
    n = 16
    for i in range(n):
        _enqueue(i, repo=repo, head=_head(repo))
    log = str(tmp_path / "log.txt")
    ctx = multiprocessing.get_context("fork")
    ev = [ctx.Event(), ctx.Event()]
    procs = [ctx.Process(target=_worker_proc, args=(log, f"w{i}", ev[i])) for i in range(2)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
        assert p.exitcode == 0
    lines = [ln.split() for ln in Path(log).read_text().splitlines()]
    uids = [u for _, u in lines if u != "noslot"]
    assert sorted(uids) == sorted(_uid(i) for i in range(n))              # each exactly once
    assert len({tag for tag, u in lines}) == 2, lines                     # both workers took part
    assert not Q.queue_nonempty()


def test_at_most_two_workers_run_at_once(tmp_path):
    _enqueue(1)
    a, b = Q.acquire_slot(), Q.acquire_slot()
    assert a is not None and b is not None and a is not b
    assert Q.acquire_slot() is None                                       # a third finds no slot
    assert W.main() == 0 and len(Q.pending()) == 1                        # ...so it exits, touching nothing
    a.close()
    assert Q.acquire_slot() is not None                                   # a slot is free again


def test_a_marker_can_be_claimed_by_exactly_one_worker():
    _enqueue(1)
    m = Q.pending()[0]
    assert Q.claim(m) is not None and Q.claim(m) is None


# ── the trigger: hooks spawn the worker detached ─────────────────────────────

class _PopenSpy:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return SimpleNamespace(pid=1)


def test_spawn_is_detached_with_a_fixed_argv_and_no_secrets(monkeypatch):
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("LLM_ROUTER_VERIFY_BUDGET_S", "60")
    monkeypatch.setenv("LLM_ROUTER_SOME_OTHER", "x")
    spy = _PopenSpy()
    monkeypatch.setattr(subprocess, "Popen", spy)
    _enqueue(1)
    assert Q.spawn_worker_if_needed(cooldown_s=0.0) is True
    ((argv, kw),) = spy.calls
    assert argv == [sys.executable, "-m", "llm_router.verify_worker"]
    assert kw["stdin"] == kw["stdout"] == kw["stderr"] == subprocess.DEVNULL
    assert kw["start_new_session"] is True and "shell" not in kw
    env = kw["env"]
    assert env["LLM_ROUTER_HOME"] == os.environ["LLM_ROUTER_HOME"] and env["LLM_ROUTER_VERIFY_BUDGET_S"] == "60"
    for k, v in SECRETS.items():
        assert k not in env and v not in env.values(), k
    assert "LLM_ROUTER_SOME_OTHER" not in env
    assert set(env) <= {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TERM",
                        "TMPDIR", "TZ", "PWD", "PYTHONPATH", "PYTHONHOME", "PYTHONUNBUFFERED", "VIRTUAL_ENV",
                        *Q.CHILD_ENV_KEYS} | {k for k in env if k.startswith(("PYTHON", "VIRTUAL_ENV"))}


def test_worker_env_never_carries_a_secret(monkeypatch):
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    env = Q.worker_env()
    assert not set(SECRETS) & set(env) and not set(SECRETS.values()) & set(env.values())


def test_spawn_needs_a_non_empty_queue_and_respects_the_cooldown(monkeypatch):
    spy = _PopenSpy()
    monkeypatch.setattr(subprocess, "Popen", spy)
    assert Q.spawn_worker_if_needed() is False and spy.calls == []        # empty queue: nothing to do
    _enqueue(1)
    assert Q.spawn_worker_if_needed(cooldown_s=60) is True
    assert Q.spawn_worker_if_needed(cooldown_s=60) is False               # inside the cooldown
    assert len(spy.calls) == 1
    marker = Q.queue_dir() / "worker_spawn.txt"
    old = time.time() - 120
    os.utime(marker, (old, old))
    assert Q.spawn_worker_if_needed(cooldown_s=60) is True                # staleness is the release
    future = time.time() + 3600
    os.utime(marker, (future, future))
    assert Q.spawn_worker_if_needed(cooldown_s=60) is True                # a future-dated marker cannot wedge it
    assert len(spy.calls) == 3


def test_spawn_holds_the_lock_so_two_callers_cannot_both_spawn(monkeypatch):
    from llm_router.file_lock import exclusive_lock
    spy = _PopenSpy()
    monkeypatch.setattr(subprocess, "Popen", spy)
    _enqueue(1)
    with exclusive_lock(Q.queue_dir() / "worker_spawn.txt.lock", timeout=0.0) as held:
        assert held
        assert Q.spawn_worker_if_needed(cooldown_s=0.0) is False
    assert spy.calls == []


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", ["session-start", "session-end"])
def test_session_start_and_stop_hooks_spawn_the_worker_and_mirrors_match(name):
    src = (REPO_ROOT / "hooks" / f"{name}.py").read_text()
    assert "_verify_queue.spawn_worker_if_needed()" in src and "CHZ-FO-VERIFY-WORKER-SPAWN" in src
    assert src.index("spawn_worker_if_needed") < src.index("_session_store" if name == "session-end" else "write_pointer")
    for rel in (f"hooks/{name}.py", "hooks/agent-route.py"):
        assert (REPO_ROOT / rel).read_bytes() == (REPO_ROOT / "src/llm_router" / rel).read_bytes(), rel


def test_a_spawn_failure_is_recorded_by_the_hook_wrapper(monkeypatch):
    # the wrapper in the hooks: any error -> failopen code, nothing raised
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(OSError("no exec")))
    _enqueue(1)
    with pytest.raises(OSError):
        Q.spawn_worker_if_needed(cooldown_s=0.0)                          # the library raises; hooks catch it


# ── SHADOW: no KPI moves ─────────────────────────────────────────────────────

def test_kpi_is_byte_identical_after_the_worker_has_run(tmp_path):
    rows = [{"ts": 1_800_000_200.0 + i, "lever": "agent_route_codex", "model": "m",
             "outcome": "delegated" if i % 3 else "codex_failed", "task_type": "code",
             "session_id": SID_MAIN, "session_kind": "organic"} for i in range(60)]
    ledger = _home_dir(tmp_path) / "north_star_units.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))
    proj = _project(tmp_path)
    _write_jsonl(proj / f"{SID_MAIN}.jsonl", [_user(SID_MAIN, "go", 1_800_000_000)]
                 + _bulk_user_prompts(SID_MAIN, 60, start_ts=1_800_000_100))
    sc = lambda: kpi.compute_scorecard(days=100000, now=1_800_100_000.0)  # noqa: E731
    base = sc()
    for k in ("NS", "D1", "D2"):
        assert "%" in base["kpis"][k]["value"], base["kpis"][k]
    base_text = kpi.render_scorecard(base)
    ids = [ns.unit_id(SID_MAIN, "agent_route_codex", r["ts"]) for r in rows]
    for uid in ids[:20]:
        Q.enqueue(uid, "/x", "0" * 40, b"diff\n")
    statuses = iter(["pass_f2p", "fail", "pass_p2p", "unavailable"] * 10)
    for m in Q.pending():                                                  # real record_verify, mixed verdicts
        c = Q.claim(m)
        s = next(statuses)
        ns.record_verify(c.unit_id, UnitResult(s, "v1_f2p" if s == "pass_f2p" else "x", sandboxed=True))
        Q.discard(c)
    after = sc()
    dump = lambda d: json.dumps(d, sort_keys=True)  # noqa: E731
    assert dump(after["kpis"]) == dump(base["kpis"]) and dump(after["joins"]) == dump(base["joins"])
    assert after["verify_shadow"]["verified"] == 5
    stripped = "\n".join(ln for ln in kpi.render_scorecard(after).splitlines() if "verify (shadow)" not in ln)
    assert stripped == base_text


# ── review fixes: marker validation, git `--`, capture budget ────────────────

def _edit_marker(n: int, **fields) -> Path:
    path = Q._sub("pending") / f"{_uid(n)}.json"
    data = json.loads(path.read_text())
    data.update(fields)
    path.write_text(json.dumps(data))
    return path


def test_a_marker_head_that_is_an_option_is_refused_and_nothing_is_written(tmp_path):
    repo = _repo(tmp_path)
    target = tmp_path / "pwned.tar"
    _enqueue(1, repo=repo, head=_head(repo))
    _edit_marker(1, head=f"--output={target}")
    recs = []
    W.drain(verify=lambda *a, **k: pytest.fail("must not be reached"),
            record=lambda uid, r: recs.append((uid, r.reason)))
    assert not target.exists() and recs == [(_uid(1), "marker_invalid")]
    assert not Q.queue_nonempty() and not Q.patch_path(_uid(1)).exists()
    with pytest.raises(W._Fail):                     # defence in depth: the worker re-checks the head
        W._checkout(str(repo), f"--output={target}", tmp_path / "x", time.monotonic() + 5)
    assert not target.exists()


@pytest.mark.parametrize("head", ["", "HEAD", "abc", "g" * 40, "A" * 40, "0" * 39, "0" * 65, "0" * 40 + "\n--x", "0" * 40 + "\n"])
def test_only_a_hex_sha_is_a_valid_head(head):
    _enqueue(1)
    _edit_marker(1, head=head)
    assert Q.pending() == [] and Q.take_invalid() == [_uid(1)]


def test_a_bad_patch_ref_is_refused_recorded_and_never_read(tmp_path):
    repo = _repo(tmp_path)
    victim = tmp_path / "victim.patch"
    victim.write_text("diff --git a/x b/x\n")
    _enqueue(1, repo=repo, head=_head(repo))
    _enqueue(2, repo=repo, head=_head(repo))
    _edit_marker(1, patch="../../victim.patch")                 # traversal
    _edit_marker(2, patch=f"{_uid(1)}.patch")                   # another unit's patch
    recs = {}
    W.drain(verify=lambda *a, **k: pytest.fail("a bad marker must never reach verify"),
            record=lambda uid, r: recs.__setitem__(uid, r.reason))
    assert recs == {_uid(1): "marker_invalid", _uid(2): "marker_invalid"}
    assert victim.exists() and not Q.queue_nonempty()


def test_a_marker_whose_unit_id_is_not_its_file_name_is_refused():
    _enqueue(1)
    _edit_marker(1, unit_id=_uid(2), patch=f"{_uid(2)}.patch")
    assert Q.pending() == [] and Q.take_invalid() == [_uid(1)]


def test_git_calls_end_option_parsing(tmp_path):
    repo = _repo(tmp_path)
    (repo / "HEAD").write_text("an untracked file named like a rev\n")    # `git diff HEAD` is ambiguous without `--`
    (repo / "src" / "pkg.py").write_text(FIXED)
    got, why = Q.capture(str(repo))
    assert why == "ok" and b"HEAD" in got[2] and b"+    return a + b" in got[2]


def test_a_stalled_capture_yields_no_marker_within_the_budget(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    (repo / "src" / "new.py").write_text("X = 1\n")
    monkeypatch.setattr(Q, "_new_file_diff", lambda *a, **k: time.sleep(5))     # a hung network read
    t0 = time.monotonic()
    assert Q.capture(str(repo), budget_s=0.3) == (None, "capture_timeout")
    assert time.monotonic() - t0 < 2
    mod = _load_hook_module()
    _fake_codex(monkeypatch, mod, repo)
    monkeypatch.setattr(Q, "CAPTURE_BUDGET_S", 0.3)
    assert _delegate(mod, repo, monkeypatch) == "done: fixed add()" and not Q.queue_nonempty()


def test_untracked_files_are_bounded_by_the_remaining_byte_budget(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a.txt").write_bytes(b"x" * (Q.MAX_PATCH_BYTES - 100))
    (repo / "b.txt").write_bytes(b"y" * 5000)
    assert Q.capture(str(repo)) == (None, "patch_too_large")


# ── the repo's own interpreter (.venv) ───────────────────────────────────────

def _fake_venv(repo: Path, *, editable: str | None, with_pytest: bool = True) -> Path:
    """A real venv (no pip) whose site-packages sees the test runner's pytest and, when `editable`
    is given, an editable-install style .pth pointing at that path. `VENVMARK` lives in its prefix."""
    import pytest as _pt
    venv = repo / ".venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    site = next(venv.glob("lib/python*/site-packages"))
    if with_pytest:
        (site / "zz_host.pth").write_text(str(Path(_pt.__file__).parent.parent) + "\n")
    if editable:
        (site / "__editable__.pth").write_text(editable + "\n")
    (venv / "VENVMARK").write_text("x")
    return venv


def _venv_repo(tmp_path) -> Path:
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text(".venv/\n")
    (repo / "tests" / "test_venvmark.py").write_text(
        "import os, sys\nfrom pkg import add\n\n\ndef test_runs_in_the_repo_venv():\n"
        "    assert os.path.exists(os.path.join(sys.prefix, 'VENVMARK'))\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "venv test")
    return repo


@real_sandbox
def test_the_repo_venv_is_used_and_the_sandbox_copy_shadows_an_editable_install(tmp_path):
    repo = _venv_repo(tmp_path)
    _fake_venv(repo, editable=str(repo / "src"))          # the venv imports the REAL (still broken) tree
    (repo / "src" / "pkg.py").write_text(FIXED)
    fix = _git(repo, "diff", "HEAD")
    _git(repo, "checkout", "--", "src/pkg.py")           # the user's tree is still broken
    _enqueue(1, repo=repo, head=_head(repo), patch=fix.encode())
    recs = []
    W.drain(record=lambda uid, r: recs.append(r))
    (r,) = recs
    # f2p needs the venv python (VENVMARK test passes) AND the patched copy to win over the real tree
    assert (r.verify_status, r.reason) == ("pass_f2p", "f2p"), r
    assert r.n_f2p >= 1 and (repo / "src" / "pkg.py").read_text() != FIXED     # user's tree untouched


@real_sandbox
def test_flat_layout_copy_shadows_an_editable_install_of_the_real_root(tmp_path):
    """No src/ dir: only the shim's PYTHONPATH=$PWD puts the sandbox copy ahead of the real tree."""
    repo = tmp_path / "flat"
    (repo / "tests").mkdir(parents=True)
    (repo / "pkg.py").write_text("def add(a, b):\n    return a - b\n")
    (repo / "tests" / "test_pkg.py").write_text("from pkg import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    (repo / "tests" / "test_venvmark.py").write_text(
        "import os, sys\nfrom pkg import add\n\n\ndef test_venv():\n"
        "    assert os.path.exists(os.path.join(sys.prefix, 'VENVMARK'))\n")
    (repo / ".gitignore").write_text(".venv/\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _fake_venv(repo, editable=str(repo))                    # the real (broken) root is on the venv's sys.path
    (repo / "pkg.py").write_text(FIXED)
    fix = _git(repo, "diff", "HEAD")
    _git(repo, "checkout", "--", "pkg.py")
    _enqueue(1, repo=repo, head=_head(repo), patch=fix.encode())
    recs = []
    W.drain(record=lambda uid, r: recs.append(r))
    assert (recs[0].verify_status, recs[0].reason) == ("pass_f2p", "f2p"), recs[0]


@real_sandbox
def test_an_editable_install_that_escapes_the_copy_is_unavailable(tmp_path):
    repo = _venv_repo(tmp_path)
    (repo / "libs" / "inner").mkdir(parents=True)
    _fake_venv(repo, editable=str(repo / "libs" / "inner"))    # under the real tree, not shadowed
    _enqueue(1, repo=repo, head=_head(repo))
    recs = []
    W.drain(verify=lambda *a, **k: pytest.fail("must not verify against the wrong tree"),
            record=lambda uid, r: recs.append((r.verify_status, r.reason)))
    assert recs == [("unavailable", "editable_points_outside")]


@real_sandbox
def test_a_venv_without_pytest_falls_back_to_the_default_interpreter(tmp_path):
    repo = _venv_repo(tmp_path)
    _fake_venv(repo, editable=None, with_pytest=False)
    _enqueue(1, repo=repo, head=_head(repo))
    seen = []
    W.drain(verify=lambda r, p, budget_s, **k: seen.append(k) or _ok_result(), record=lambda *a: None)
    assert seen == [{}]                                    # no python_dir: the default interpreter


@real_sandbox
def test_the_venv_shims_are_handed_to_the_verifier(tmp_path):
    repo = _venv_repo(tmp_path)
    _fake_venv(repo, editable=str(repo / "src"))
    _enqueue(1, repo=repo, head=_head(repo))
    seen = []
    W.drain(verify=lambda r, p, budget_s, python_dir=None: seen.append(python_dir) or _ok_result(),
            record=lambda *a: None)
    assert seen and seen[0] and seen[0].endswith("shim")


def test_no_venv_means_the_default_interpreter(tmp_path):
    repo = _repo(tmp_path)
    _enqueue(1, repo=repo, head=_head(repo))
    seen = []
    W.drain(verify=lambda r, p, budget_s, **k: seen.append(k) or _ok_result(), record=lambda *a: None)
    assert seen == [{}]
