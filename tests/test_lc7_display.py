"""LC-7: local-check display surfaces (status/doctor lines, statusline segment, nudge, retention).

Every test uses tmp homes (LLM_ROUTER_HOME -> tmp_path) and a fake inventory cache; no network, no model.
"""
from __future__ import annotations

import calendar
import json
import os
import stat
import time
from pathlib import Path

import pytest

from llm_router import statusline_tick as tick
from llm_router.local_check import attempts as A
from llm_router.local_check import display, retention
from llm_router.resolver import eligibility_store as store

MODEL = "qwen-fx:9b"
DIGEST = "sha256:" + "a" * 16
SHA = "b" * 64
NOW = calendar.timegm(time.strptime("2026-10-20", "%Y-%m-%d")) + 3600
PROV = {"bank_version": "v1", "bank_sha256": SHA, "settings_hash": "s" * 16, "model_digest": DIGEST,
        "runtime": "ollama", "run_id": "r1", "date": "2026-10-20"}
PASS70 = {"k": 60, "n": 70, "verdict": "PASS"}
FAIL70 = {"k": 41, "n": 70, "verdict": "NOT_PASSED"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    return tmp_path


def write_inventory(home: Path, models: dict[str, str | None], age_s: float = 0.0) -> None:
    body = {f"ollama/{m}": ({"model_id": f"ollama/{m}", "digest": d} if d else {"model_id": f"ollama/{m}"})
            for m, d in models.items()}
    (home / "discovery.json").write_text(json.dumps({"models": body, "cached_at": time.time() - age_s}))


def lanes(home: Path, model: str = MODEL, **jobs):
    path = home / "local_eligibility.json"
    store.record_local_check(model, jobs, PROV, path=path)
    return path


def texts(ls):
    return [ln.text for ln in ls]


# ------------------------------------------------------------------ 1. status / doctor lines
def test_passed_line_has_n_bank_date_and_routing_na(home):
    p = lanes(home, commit_message=PASS70, pr_description=PASS70)
    ls = store.local_check_lines(path=p, current_digests={MODEL: DIGEST}, now=NOW)
    assert texts(ls) == [f"local-check: {MODEL} — commit_message, pr_description passed 60/70 "
                         "(bank v1, 2026-10-20) [routing: n/a]"]
    assert ls[0].kind == "passed"


def test_routing_on_off_from_local_routing_json(home):
    p = lanes(home, commit_message=PASS70, pr_description=PASS70)
    (home / "local_routing.json").write_text(json.dumps(
        {MODEL: {"commit_message": {"enabled": True}, "pr_description": False}}))
    ls = display.lines(path=p, now=NOW)
    assert [t for t in texts(ls)] == [
        f"local-check: {MODEL} — commit_message passed 60/70 (bank v1, 2026-10-20) [routing: on]",
        f"local-check: {MODEL} — pr_description passed 60/70 (bank v1, 2026-10-20) [routing: off]"]


def test_not_passed_line_suggests_recommend(home):
    p = lanes(home, commit_message=FAIL70)
    ls = store.local_check_lines(path=p, current_digests={MODEL: DIGEST}, now=NOW)
    assert texts(ls) == [f"local-check: {MODEL} — commit_message not passed (41/70, bank v1, 2026-10-20); "
                         "try: llm-router local-check recommend"]
    assert ls[0].kind == "not_passed"


def test_small_n_is_flagged(home):
    p = lanes(home, commit_message={"k": 40, "n": 40, "verdict": "PASS"})
    (line,) = store.local_check_lines(path=p, current_digests={MODEL: DIGEST}, now=NOW)
    assert "passed 40/40, small n" in line.text


def test_stale_model_changed_and_expired(home):
    p = lanes(home, commit_message=PASS70)
    (changed,) = store.local_check_lines(path=p, current_digests={MODEL: "sha256:zzz"}, now=NOW)
    assert changed.kind == "stale" and f"{MODEL} — commit_message stale — model changed" in changed.text
    assert "passed" not in changed.text
    # No usable current digest: the 30-day window applies.
    (fresh,) = store.local_check_lines(path=p, current_digests={}, now=NOW)
    assert fresh.kind == "passed"
    (expired,) = store.local_check_lines(path=p, current_digests={}, now=NOW + 40 * 86400)
    assert expired.kind == "stale" and "stale — expired" in expired.text


def test_not_measured_for_installed_model_without_lane(home):
    write_inventory(home, {MODEL: DIGEST, "other:3b": None})
    ls = display.lines(path=home / "absent.json", now=NOW)
    assert texts(ls) == ["local-check: other:3b — not measured (llm-router local-check)",
                         f"local-check: {MODEL} — not measured (llm-router local-check)"]
    assert {ln.kind for ln in ls} == {"not_measured"}      # never a failure kind


def test_nothing_installed_nothing_shown(home):
    assert display.lines(path=home / "absent.json", now=NOW) == []


def test_unmeasured_list_is_capped(home):
    write_inventory(home, {f"m{i}:1b": None for i in range(5)})
    ls = display.lines(path=home / "absent.json", now=NOW)
    assert len(ls) == display.MAX_UNMEASURED + 1 and "+2 more local models not measured" in ls[-1].text


def test_digest_comes_from_cache_and_is_ignored_when_old(home, monkeypatch):
    p = lanes(home, commit_message=PASS70)
    write_inventory(home, {MODEL: "a" * 16})                 # bare hex, as /api/tags reports it
    assert display.inventory()[1] == {MODEL: DIGEST}
    assert display.lines(path=p, now=NOW)[0].kind == "passed"
    write_inventory(home, {MODEL: "c" * 16})
    assert display.lines(path=p, now=NOW)[0].kind == "stale"
    write_inventory(home, {MODEL: "c" * 16}, age_s=display.DIGEST_MAX_AGE_S + 60)
    assert display.inventory()[1] == {}                       # an old cache does not say "changed"
    # No HTTP on this path: any socket use fails the test.
    import socket

    def boom(*a, **k):
        raise AssertionError("network used")
    monkeypatch.setattr(socket.socket, "connect", boom)
    display.lines(path=p, now=NOW)


def test_prereg_lines_kept_and_local_lines_not_duplicated(home):
    prereg = {"grade": "passed", "verdict": "eligible", "eligible": True, "source": "prereg", "prereg": "PREREG-Q37",
              "manifest_sha256": "a" * 64, "k": 60, "n": 70, "point": 0.857, "wilson_lb": 0.75,
              "job_k": 32, "job_n": 35, "recorded_at": "2026-10-19"}
    p = lanes(home, commit_message=PASS70)
    data = json.loads(p.read_text())
    data["x:1b"] = {"text_job": {"commit_message": prereg}}
    p.write_text(json.dumps(data))
    ls = display.lines(path=p, now=NOW)
    assert [ln.kind for ln in ls] == ["prereg", "passed"]
    assert ls[0].text.startswith("text_job: commit_message — qualified (PREREG-Q37, 60/70)")
    # The default text_job_lines keeps its old behaviour (local lines included).
    assert any("jobs" not in t and MODEL in t for t in store.text_job_lines(path=p, now=NOW))


def test_status_and_doctor_text_has_no_failure_colour(home, monkeypatch):
    p = lanes(home, commit_message=FAIL70)
    write_inventory(home, {MODEL: DIGEST, "other:3b": None})
    monkeypatch.setattr(store, "eligibility_path", lambda: p)
    from llm_router.ui.status_premium import PremiumStatusCommand
    out = PremiumStatusCommand().render_text_job_qualification()
    assert "not passed (41/70" in str(out) and "other:3b — not measured" in str(out)
    styles = {str(s.style) for s in out.spans}
    assert not any("red" in s or "warn" in s for s in styles)


# ------------------------------------------------------------------ 2. statusline
def test_segment_values(home):
    write_inventory(home, {MODEL: DIGEST})
    assert display.statusline_segment(path=home / "none.json", now=NOW) == ""        # nothing measured
    p = lanes(home, commit_message=FAIL70)
    assert display.statusline_segment(path=p, now=NOW) == "local n/p"
    p = lanes(home, commit_message=PASS70)
    assert display.statusline_segment(path=p, now=NOW) == "local ok"
    write_inventory(home, {MODEL: "sha256:other"})                                   # model changed: nothing measured now
    assert display.statusline_segment(path=p, now=NOW) == ""


def test_tick_renders_segment_only_for_the_closed_set():
    base = {"written_at": NOW, "mode": "smart", "ns": None, "codex": None}
    plain = tick.render(base, now=NOW)
    assert "local" not in plain
    assert tick.render({**base, "local_check": "local ok"}, now=NOW).count("local ok") == 1
    assert "local n/p" in tick.render({**base, "local_check": "local n/p"}, now=NOW)
    assert "evil" not in tick.render({**base, "local_check": "evil\x1b[31m"}, now=NOW)
    assert tick.render({**base, "local_check": None}, now=NOW) == plain
    assert "local ok" not in tick.render({**base, "local_check": "local ok", "written_at": NOW - 10 * 3600}, now=NOW)  # stale cache


def test_refresher_cache_carries_segment(home):
    from llm_router import statusline_refresh as R
    write_inventory(home, {MODEL: DIGEST})
    lanes(home, commit_message=PASS70)
    assert R._local_check() == "local ok"
    store.record_local_check(MODEL, {"commit_message": FAIL70, "pr_description": FAIL70}, PROV,
                             path=home / "local_eligibility.json")
    assert R._local_check() == "local n/p"
    (home / "local_eligibility.json").unlink()
    assert R._local_check() is None


def test_tick_adds_no_io(monkeypatch):
    import builtins
    import io
    calls = []

    def spy(real, name):
        def wrapped(*a, **k):
            calls.append(name)
            return real(*a, **k)
        return wrapped

    monkeypatch.setattr(builtins, "open", spy(builtins.open, "builtins.open"))
    monkeypatch.setattr(io, "open", spy(io.open, "io.open"))
    monkeypatch.setattr(os, "stat", spy(os.stat, "os.stat"))
    monkeypatch.setattr(Path, "read_text", spy(Path.read_text, "Path.read_text"))
    monkeypatch.setattr(Path, "open", spy(Path.open, "Path.open"))
    monkeypatch.setattr(Path, "stat", spy(Path.stat, "Path.stat"))
    tick.render({"written_at": NOW, "mode": "smart", "local_check": "local ok"}, now=NOW)
    assert calls == []
    Path(__file__).read_text()                              # the spies are live: a Path read WOULD be seen
    assert "Path.read_text" in calls




@pytest.mark.timing
def test_tick_segment_costs_nothing_measurable():
    base = {"written_at": NOW, "mode": "smart", "ns": {"pct": 1.0, "n": 5}, "codex": None}
    cached = {**base, "local_check": "local ok"}
    n = 2000
    t0 = time.perf_counter()
    for _ in range(n):
        tick.render(base, now=NOW)
    plain = time.perf_counter() - t0
    t0 = time.perf_counter()
    for _ in range(n):
        tick.render(cached, now=NOW)
    with_seg = time.perf_counter() - t0
    assert (with_seg - plain) / n < 0.0005          # < 0.5 ms per tick added; the tick budget is 100 ms
    assert with_seg / n < 0.005


# ------------------------------------------------------------------ 3. nudge
def test_nudge_once_per_bank_version_0600(home):
    write_inventory(home, {MODEL: DIGEST})
    p = home / "local_eligibility.json"
    line = display.maybe_nudge(path=p, now=NOW)
    assert line and MODEL in line and "llm-router local-check" in line and "?" not in line
    f = home / "local_check" / "nudge.json"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert display.maybe_nudge(path=p, now=NOW) is None                     # once
    f.write_text(json.dumps({"bank_versions": ["0"]}))
    assert display.maybe_nudge(path=p, now=NOW)                              # a new bank version nudges again


def test_nudge_silent_without_model_or_with_a_measurement(home):
    assert display.maybe_nudge(path=home / "x.json", now=NOW) is None        # no local model
    write_inventory(home, {MODEL: DIGEST})
    p = lanes(home, commit_message=FAIL70)
    assert display.maybe_nudge(path=p, now=NOW) is None                      # something is measured
    assert not (home / "local_check" / "nudge.json").exists()


def test_nudge_unwritable_state_stays_silent(home, monkeypatch):
    write_inventory(home, {MODEL: DIGEST})
    (home / "local_check").write_text("a file where the directory should be")
    assert display.maybe_nudge(path=home / "x.json", now=NOW) is None


def test_nudge_creates_parent_0700_and_leaves_no_temp_on_failure(home, monkeypatch):
    write_inventory(home, {MODEL: DIGEST})
    assert not (home / "local_check").exists()
    assert display.maybe_nudge(path=home / "x.json", now=NOW) is not None
    assert stat.S_IMODE((home / "local_check").stat().st_mode) == 0o700
    (home / "local_check" / "nudge.json").unlink()
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    assert display.maybe_nudge(path=home / "x.json", now=NOW) is None
    assert [p.name for p in (home / "local_check").iterdir()] == []


def test_nudge_dir_forced_0700_even_if_preexisting_loose(home):
    write_inventory(home, {MODEL: DIGEST})
    d = home / "local_check"
    d.mkdir()
    d.chmod(0o755)
    assert display.maybe_nudge(path=home / "x.json", now=NOW) is not None
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_install_nudge_prints_one_dim_line_and_never_prompts(home, capsys, monkeypatch):
    write_inventory(home, {MODEL: DIGEST})
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("prompted")))
    from llm_router.commands import install
    install._local_check_nudge()
    first = capsys.readouterr().out
    install._local_check_nudge()
    assert first.count("llm-router local-check") == 1 and capsys.readouterr().out == ""


def test_doctor_section_prints_lines_prunes_and_nudges_once(home, capsys):
    from llm_router.commands import doctor
    write_inventory(home, {MODEL: DIGEST})
    root = home / "local_check" / "runs"
    root.mkdir(parents=True)
    for i, age in enumerate((9, 8, 7, 6, 5)):
        mkrun(root, f"run{i}", age, now=time.time())
    doctor._local_text_jobs_section()
    out = capsys.readouterr().out
    assert f"{MODEL} \u2014 not measured" in out and out.count("tip: local model found") == 1
    assert sorted(p.name for p in root.iterdir()) == ["run2", "run3", "run4"]
    doctor._local_text_jobs_section()
    assert "tip:" not in capsys.readouterr().out
    assert "\x1b[31m" not in out                                   # no red anywhere


# ------------------------------------------------------------------ 4. retention
def mkrun(root: Path, name: str, age_days: float, text="an answer", now=NOW) -> Path:
    d = root / name
    d.mkdir(parents=True, mode=0o700)
    (d / "results.jsonl").write_text(json.dumps({"item_id": "i1", "status": "ok", "text": text, "elapsed_s": 1.5}) + "\n"
                                     + json.dumps({"item_id": "i2", "status": "timeout", "text": ""}) + "\n")
    (d / "judge.jsonl").write_text(json.dumps({"item_id": "i1", "verdict": "pass"}) + "\n")
    (d / "meta.json").write_text("{}")
    for f in d.iterdir():
        os.utime(f, (now - age_days * 86400,) * 2)
    os.utime(d, (now - age_days * 86400,) * 2)
    return d


def test_keep_last_three_runs(home):
    root = home / "local_check" / "runs"
    root.mkdir(parents=True)
    for i, age in enumerate((9, 5, 4, 3, 2, 1)):
        mkrun(root, f"run{i}", age)
    (root / "attempts.jsonl").write_text("{}\n")
    rep = retention.prune(root, now=NOW)
    assert sorted(rep.kept) == ["run3", "run4", "run5"] and sorted(rep.deleted) == ["run0", "run1", "run2"]
    assert sorted(p.name for p in root.iterdir()) == ["attempts.jsonl", "run3", "run4", "run5"]


def test_open_reservation_survives_and_expired_does_not(home):
    root = home / "local_check" / "runs"
    root.mkdir(parents=True)
    for i, age in enumerate((9, 8, 3, 2, 1)):
        mkrun(root, f"run{i}", age)
    rows = [{"config_key": "k", "run_id": "run0", "state": "open", "ts": NOW - 3 * 86400},
            {"config_key": "k", "run_id": "run1", "state": "open", "ts": NOW - 20 * 86400}]
    (root / A.LOG_NAME).write_text("".join(json.dumps(r) + "\n" for r in rows))
    rep = retention.prune(root, now=NOW)
    assert "run0" in rep.kept and rep.deleted == ["run1"]


def test_answers_pruned_after_30_days_counts_stay_0600(home):
    root = home / "local_check" / "runs"
    root.mkdir(parents=True)
    old, new = mkrun(root, "old", 45), mkrun(root, "new", 2)
    before = (old / "results.jsonl").stat().st_mtime
    rep = retention.prune(root, now=NOW)
    assert rep.answers_pruned == ["old"]
    recs = [json.loads(x) for x in (old / "results.jsonl").read_text().splitlines()]
    assert [r["text"] for r in recs] == ["", ""] and recs[0]["answer_pruned"] is True
    assert recs[0]["status"] == "ok" and recs[0]["elapsed_s"] == 1.5 and recs[1]["status"] == "timeout"
    assert "answer_pruned" not in recs[1]
    assert stat.S_IMODE((old / "results.jsonl").stat().st_mode) == 0o600
    assert (old / "results.jsonl").stat().st_mtime == before
    assert "an answer" in (new / "results.jsonl").read_text()
    assert retention.prune(root, now=NOW).answers_pruned == []              # idempotent


def test_prune_is_safe_on_missing_root_and_symlinks(home, tmp_path):
    assert retention.prune(home / "nope", now=NOW).deleted == []
    root = home / "runs"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("x")
    for i in range(4):
        mkrun(root, f"r{i}", 5 - i)
    (root / "link").symlink_to(outside, target_is_directory=True)
    retention.prune(root, now=NOW)
    assert (outside / "keep.txt").exists() and (root / "link").is_symlink()


def test_non_utf8_attempts_log_fails_closed_without_raising(home):
    root = home / "local_check" / "runs"
    root.mkdir(parents=True)
    for i, age in enumerate((9, 8, 7, 3, 2, 1)):
        mkrun(root, f"run{i}", age)
    (root / A.LOG_NAME).write_bytes(b"\xff\xfe{not utf8}\n")
    rep = retention.prune(root, now=NOW)
    assert rep.deleted == [] and len(rep.kept) == 6
    assert any(A.LOG_NAME in e and "UnicodeDecodeError" in e for e in rep.errors)
    assert len([p for p in root.iterdir() if p.is_dir()]) == 6


def test_non_utf8_results_in_one_run_is_collected_and_others_continue(home):
    root = home / "local_check" / "runs"
    root.mkdir(parents=True)
    bad, good = mkrun(root, "bad", 45), mkrun(root, "good", 44)
    (bad / "results.jsonl").write_bytes(b"\xff\xfe\x00 not utf8\n")
    os.utime(bad / "results.jsonl", (NOW - 45 * 86400,) * 2)
    rep = retention.prune(root, now=NOW)
    assert any(e.startswith("bad:") and "UnicodeDecodeError" in e for e in rep.errors)
    assert rep.answers_pruned == ["good"]
    assert "an answer" not in (good / "results.jsonl").read_text()


def test_symlinked_runs_root_is_refused(home, tmp_path):
    real = tmp_path / "real_runs"
    real.mkdir()
    for i in range(5):
        mkrun(real, f"r{i}", 40 - i)
    link = home / "runs_link"
    link.symlink_to(real, target_is_directory=True)
    rep = retention.prune(link, now=NOW)
    assert rep.deleted == [] and rep.answers_pruned == [] and any("symlink" in e for e in rep.errors)
    assert len(list(real.iterdir())) == 5 and "an answer" in (real / "r0" / "results.jsonl").read_text()


def test_doctor_prune_failure_does_not_block_nudge_and_is_recorded(home, capsys, monkeypatch):
    from llm_router import failopen
    from llm_router.commands import doctor
    write_inventory(home, {MODEL: DIGEST})
    codes = []
    monkeypatch.setattr(failopen, "record", lambda code, *a, **k: codes.append(code))
    real_prune = retention.prune
    monkeypatch.setattr(retention, "prune", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    doctor._local_text_jobs_section()
    assert "tip: local model found" in capsys.readouterr().out
    assert codes == ["CHZ-FO-LOCALCHECK-PRUNE"]
    codes.clear()
    monkeypatch.setattr(retention, "prune", real_prune)
    (home / "local_check" / "nudge.json").unlink()
    monkeypatch.setattr(display, "maybe_nudge", lambda: (_ for _ in ()).throw(RuntimeError("y")))
    doctor._local_text_jobs_section()
    assert codes == ["CHZ-FO-LOCALCHECK-NUDGE"]


def test_retention_doc_lists_the_stores():
    doc = (Path(__file__).resolve().parents[1] / "docs" / "RETENTION.md").read_text()
    for needle in ("local_check/runs/", "attempts.jsonl", "text_job_local", "KEEP_RUNS", "ANSWER_DAYS", "nudge.json"):
        assert needle in doc
    assert retention.KEEP_RUNS == 3 and retention.ANSWER_DAYS == 30
