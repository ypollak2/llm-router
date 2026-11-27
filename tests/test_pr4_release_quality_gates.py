"""PR4 — North Star point 7: "a quality regression blocks a release".

Tests `scripts/release/classifier_gate.py` and `scripts/release/quality_gate.py`,
wired into `pre-release-verify.sh` as steps 11 and 12. Both are release scripts
under `scripts/`, not part of the installed package, so they are loaded by path
(same pattern as `tests/test_k4_adversarial_corpus.py`'s `_guard()`).

classifier_gate.py is deterministic and offline (`classify_signals` never calls
a model or the network) — its tests run every time, no skip. quality_gate.py
calls a real local model, so its tests only exercise the availability/skip
machinery via monkeypatch, never a live Ollama call — a CI machine with no
Ollama installed must still be able to run this file.
"""
from __future__ import annotations

import importlib.util
import pathlib
import urllib.request

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _no_real_ollama(monkeypatch, tmp_path):
    """No test in this file may reach a real Ollama: quality_gate's read-only
    peer check (`_peer_memory_conflict`) runs in every `main()` call, so every
    HTTP call fails here unless a test stubs the gate's helpers itself."""
    def _refuse(*_a, **_k):
        raise OSError("network disabled in tests")
    monkeypatch.setattr(urllib.request, "urlopen", _refuse)
    monkeypatch.delenv("LLM_ROUTER_LOCAL_MODEL_URL", raising=False)
    monkeypatch.delenv("QUALITY_GATE_PEER_OLLAMA_URLS", raising=False)
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "router-home"))  # never the real .env


def _load(rel_path: str, name: str):
    path = REPO / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── classifier_gate.py ───────────────────────────────────────────────────────

def test_classifier_gate_passes_on_head(capsys):
    gate = _load("scripts/release/classifier_gate.py", "_classifier_gate_head")
    rc = gate.main()
    out = capsys.readouterr().out
    assert rc == 0, out
    assert f"baseline {gate.BASELINE:.3f}" in out
    assert "classifier regression gate: PASS" in out


def test_classifier_gate_reports_the_low_signal_default_share(capsys):
    """CLAUDE.md S8: a gate can't be trusted to measure the classifier unless
    it also says how much of its own sample was decided by the low-signal
    default rather than an actual score. Just checking accuracy is >= baseline
    would pass even if every fixture hit the default by luck."""
    gate = _load("scripts/release/classifier_gate.py", "_classifier_gate_default")
    gate.main()
    out = capsys.readouterr().out
    assert "decided by low_signal_default" in out


def test_classifier_gate_catches_a_broken_floor_rule(monkeypatch, capsys):
    """Narrowest-mutation red-check (CLAUDE.md): break exactly ONE classify.py
    rule condition -- the research task-type complexity floor -- via
    monkeypatch, never by editing src/. Everything else about classify.py is
    untouched, including every regex and every other floor entry."""
    gate = _load("scripts/release/classifier_gate.py", "_classifier_gate_mut")
    from llm_router import classify as classify_mod

    original_floor = classify_mod._TASK_COMPLEXITY_FLOOR
    assert "research" in original_floor, "premise: research has a floor today"
    mutated_floor = {k: v for k, v in original_floor.items() if k != "research"}
    monkeypatch.setattr(classify_mod, "_TASK_COMPLEXITY_FLOOR", mutated_floor)

    rc = gate.main()
    out = capsys.readouterr().out

    assert rc == 1, out
    assert "FAILED" in out
    # Every research fixture in FIXTURES relies on the floor (none of them
    # carries a _COMPLEXITY_COMPLEX/_COMPLEXITY_DEEP keyword of its own), so
    # removing the floor must mismatch every one of them, not just one.
    research_prompts = [p for p, t, _ in gate.FIXTURES if t == "research"]
    assert len(research_prompts) >= 5, "premise: enough research fixtures to matter"
    for prompt in research_prompts:
        assert prompt in out, f"{prompt!r} missing from the gate's failure output"


def test_classifier_gate_catches_a_broken_code_generate_floor(monkeypatch, capsys):
    """Independent-review finding (round 2, PR #146): under HOOK_POLICY, a
    short code/generate prompt with no keyword already computes MODERATE from
    the plain length fallback -- IDENTICAL to the floor value -- so deleting
    _TASK_COMPLEXITY_FLOOR["code"/"generate"] left FIXTURES's accuracy at
    1.000 (rc=0), a floor mutation the gate could not catch. FLOOR_FIXTURES
    (checked under ROUTER_POLICY, where the fallback is SIMPLE) closes that
    gap. classify.py itself is untouched -- monkeypatch only."""
    gate = _load("scripts/release/classifier_gate.py", "_classifier_gate_mut_cg")
    from llm_router import classify as classify_mod

    original_floor = classify_mod._TASK_COMPLEXITY_FLOOR
    assert {"code", "generate"} <= original_floor.keys(), (
        "premise: code and generate both have a floor today"
    )
    mutated_floor = {k: v for k, v in original_floor.items()
                     if k not in ("code", "generate")}
    monkeypatch.setattr(classify_mod, "_TASK_COMPLEXITY_FLOOR", mutated_floor)

    rc = gate.main()
    out = capsys.readouterr().out

    assert rc == 1, out
    assert "FAILED" in out
    assert len(gate.FLOOR_FIXTURES) >= 4, "premise: enough floor fixtures to matter"
    for prompt, *_ in gate.FLOOR_FIXTURES:
        assert prompt in out, f"{prompt!r} missing from the gate's failure output"


def test_classifier_gate_floor_fixtures_are_invisible_under_hook_policy(capsys):
    """Documents WHY FLOOR_FIXTURES uses ROUTER_POLICY rather than HOOK_POLICY:
    the same prompts, classified under HOOK_POLICY with no floor at all, are
    NOT reliably SIMPLE either (task_aware_query / simple_max=0 differences
    make this policy-shape-dependent) -- the point is narrower: this test
    pins that ROUTER_POLICY is the policy where the floor's effect is
    observable, as a premise check for the mutation test above."""
    gate = _load("scripts/release/classifier_gate.py", "_classifier_gate_premise")
    from llm_router.classify import ROUTER_POLICY, classify_signals

    for prompt, exp_type, exp_complexity in gate.FLOOR_FIXTURES:
        signal = classify_signals(prompt, ROUTER_POLICY)
        assert signal.task_type.value == exp_type
        assert signal.complexity.value == exp_complexity, (
            f"{prompt!r}: FLOOR_FIXTURES premise broke -- expected "
            f"{exp_complexity} under ROUTER_POLICY, got "
            f"{signal.complexity.value}"
        )


# ── quality_gate.py ──────────────────────────────────────────────────────────

def test_quality_gate_unavailable_backend_with_no_flag_fails_loud(monkeypatch, capsys):
    gate = _load("scripts/release/quality_gate.py", "_quality_gate_unavail")
    monkeypatch.setattr(gate, "is_ollama_available", lambda: False)

    rc = gate.main([])
    err = capsys.readouterr().err

    assert rc != 0, err
    assert "unreachable" in err.lower()
    assert "unmeasured quality change" in err


def test_quality_gate_skip_flag_records_reason_and_exits_clean(monkeypatch, tmp_path, capsys):
    gate = _load("scripts/release/quality_gate.py", "_quality_gate_skip")
    monkeypatch.setattr(gate, "is_ollama_available", lambda: False)
    note_path = tmp_path / "dist" / "QUALITY_SKIPPED.txt"
    monkeypatch.setattr(gate, "DIST_DIR", tmp_path / "dist")
    monkeypatch.setattr(gate, "SKIP_NOTE_PATH", note_path)

    reason = "no GPU on this CI runner"
    rc = gate.main(["--skip-quality", reason])
    out = capsys.readouterr().out

    assert rc == 0, out
    assert reason in out
    assert note_path.exists()
    assert reason in note_path.read_text()


def test_quality_gate_skip_requires_a_reason():
    """argparse enforces this: `--skip-quality` with no value is a usage
    error, not a silent skip with an empty reason."""
    gate = _load("scripts/release/quality_gate.py", "_quality_gate_argcheck")
    try:
        gate.main(["--skip-quality"])
    except SystemExit as e:
        assert e.code != 0
    else:
        raise AssertionError("--skip-quality with no value did not error")


# ── quality_gate.py: contention detection (HIGH, independent review) ─────────
#
# Round 2 found the fix over-corrected: `_resident_model_conflict() or
# _warm_up_probe()` treated ANY other resident model as contention by itself,
# a false positive (an idle different model just gets evicted on the next
# request). Round 3 fixes that: `_resident_model_note()` is context only, and
# `_warm_up_probe()` alone decides contention.
#
# Round 2's red-check (removing the old contention line) produced pytest
# TIMEOUTS, not assertion failures, because the mutated code fell through
# into a REAL subprocess call against a REAL Ollama. Every test in this
# section now ALSO monkeypatches `_run_bench` to a canned, instant result, so
# a mutation that removes the contention gate makes the test fail on a
# deterministic assertion (rc/message mismatch) in under a second, never a
# hang -- and no test in this file can touch a real Ollama, contention gate
# intact or not.

def _mock_clean_pass(gate, monkeypatch):
    """Both pinned tasks pass, instantly. Used as the `_run_bench` stand-in
    everywhere a test wants to prove the gate did NOT reach a real bench call."""
    monkeypatch.setattr(gate, "_run_bench", lambda: (gate.N_TASKS, gate.N_TASKS, [], []))


def test_quality_gate_resident_other_model_with_healthy_probe_proceeds(monkeypatch, capsys):
    """The false-positive case round 2 found: a DIFFERENT model is resident,
    but the warm-up probe still answers -- that's "idle, not contended", and
    the gate must proceed rather than refuse the release."""
    gate = _load("scripts/release/quality_gate.py", "_quality_gate_resident_ok")
    monkeypatch.setattr(gate, "is_ollama_available", lambda: True)
    monkeypatch.setattr(gate, "_resident_model_note",
                        lambda: "Ollama's one model slot is currently held by "
                                f"qwen3.8:latest, not {gate.TARGET_MODEL}")
    monkeypatch.setattr(gate, "_warm_up_probe", lambda: None)
    _mock_clean_pass(gate, monkeypatch)

    rc = gate.main([])
    out = capsys.readouterr().out

    assert rc == 0, out
    assert "qwen3.8" in out  # the note is still visible
    assert "not contention by itself" in out
    assert "CONTENDED" not in out
    assert "answer-quality gate: PASS" in out


def test_quality_gate_resident_other_model_with_failing_probe_is_contended(monkeypatch, capsys):
    """The genuine-contention case: a different model is resident AND the
    warm-up probe fails -- the resident note is folded into the message for
    context, but the probe is what makes the call."""
    gate = _load("scripts/release/quality_gate.py", "_quality_gate_resident_bad")
    monkeypatch.setattr(gate, "is_ollama_available", lambda: True)
    monkeypatch.setattr(gate, "_resident_model_note",
                        lambda: "Ollama's one model slot is currently held by "
                                f"qwen3.8:latest, not {gate.TARGET_MODEL}")
    monkeypatch.setattr(gate, "_warm_up_probe",
                        lambda: f"warm-up probe to {gate.TARGET_MODEL} failed within "
                                f"{gate.WARMUP_TIMEOUT_S}s: TimeoutError: timed out")
    _mock_clean_pass(gate, monkeypatch)

    rc = gate.main([])
    err = capsys.readouterr().err

    assert rc != 0, err
    assert "CONTENDED" in err
    assert "qwen3.8" in err
    assert "TimeoutError" in err
    assert "unmeasured quality change" in err


def test_quality_gate_warmup_probe_empty_with_no_resident_note_is_contended(monkeypatch, capsys):
    """Contention without a resident-model explanation -- e.g. the pinned
    model IS resident but stalled -- must still be caught by the probe alone."""
    gate = _load("scripts/release/quality_gate.py", "_quality_gate_warmup_empty")
    monkeypatch.setattr(gate, "is_ollama_available", lambda: True)
    monkeypatch.setattr(gate, "_resident_model_note", lambda: None)
    monkeypatch.setattr(gate, "_warm_up_probe",
                        lambda: f"warm-up probe to {gate.TARGET_MODEL} returned an empty answer")
    _mock_clean_pass(gate, monkeypatch)

    rc = gate.main([])
    err = capsys.readouterr().err

    assert rc != 0, err
    assert "CONTENDED" in err
    assert "empty answer" in err


def test_quality_gate_contention_with_skip_flag_records_reason_and_exits_clean(
    monkeypatch, tmp_path, capsys
):
    gate = _load("scripts/release/quality_gate.py", "_quality_gate_contention_skip")
    monkeypatch.setattr(gate, "is_ollama_available", lambda: True)
    monkeypatch.setattr(gate, "_resident_model_note",
                        lambda: "Ollama's one model slot is currently held by qwen3.8:latest")
    monkeypatch.setattr(gate, "_warm_up_probe",
                        lambda: f"warm-up probe to {gate.TARGET_MODEL} returned an empty answer")
    _mock_clean_pass(gate, monkeypatch)
    note_path = tmp_path / "dist" / "QUALITY_SKIPPED.txt"
    monkeypatch.setattr(gate, "DIST_DIR", tmp_path / "dist")
    monkeypatch.setattr(gate, "SKIP_NOTE_PATH", note_path)

    reason = "shared machine, another session holds the model slot"
    rc = gate.main(["--skip-quality", reason])
    out = capsys.readouterr().out

    assert rc == 0, out
    assert reason in out
    assert note_path.exists()
    note = note_path.read_text()
    assert reason in note
    assert "CONTENDED" in note


def test_quality_gate_prints_the_dated_baseline_note_on_every_run(monkeypatch, capsys):
    """The baseline provenance must be visible on every run, not only in a
    docstring a releaser may never open -- checked on both the unavailable
    path and the passing path. Confirmed 2026-09-25 (n=3/3 clean runs against
    qwen3-coder:30b, see "The threshold" in the module docstring), so this no
    longer says "pending"."""
    gate = _load("scripts/release/quality_gate.py", "_quality_gate_baseline_note")
    assert gate.FRESH_BASELINE_DATE == "2026-09-25"
    assert gate.FRESH_BASELINE_N == 3

    monkeypatch.setattr(gate, "is_ollama_available", lambda: False)
    gate.main([])
    unavailable_out = capsys.readouterr().out
    assert gate.FRESH_BASELINE_DATE in unavailable_out
    assert "fresh baseline pending" not in unavailable_out

    monkeypatch.setattr(gate, "is_ollama_available", lambda: True)
    monkeypatch.setattr(gate, "_resident_model_note", lambda: None)
    monkeypatch.setattr(gate, "_warm_up_probe", lambda: None)
    _mock_clean_pass(gate, monkeypatch)
    gate.main([])
    passing_out = capsys.readouterr().out
    assert gate.FRESH_BASELINE_DATE in passing_out
    assert "fresh baseline pending" not in passing_out


def test_quality_gate_empty_answer_mid_run_is_reported_as_no_answer_not_regression(
    monkeypatch, capsys
):
    """The pre-run checks can pass and contention can still start once the
    real bench is running. An empty answer with no recorded error must be
    called out as a backend failure ("no answer"), not folded silently into
    "quality regression"."""
    gate = _load("scripts/release/quality_gate.py", "_quality_gate_mid_run_empty")
    monkeypatch.setattr(gate, "is_ollama_available", lambda: True)
    monkeypatch.setattr(gate, "_resident_model_note", lambda: None)
    monkeypatch.setattr(gate, "_warm_up_probe", lambda: None)
    monkeypatch.setattr(
        gate, "_run_bench",
        lambda: (1, 2, ["hard/hd-last-page"], ["hard/hd-last-page"]),
    )

    rc = gate.main([])
    out, err = (lambda r: (r.out, r.err))(capsys.readouterr())

    assert rc != 0, out + err
    assert "NO ANSWER" in out
    assert "hard/hd-last-page" in out
    assert "CONTENDED" in err
    assert "quality regression" not in (out + err).lower()


def test_quality_gate_empty_answer_mid_run_with_skip_flag_exits_clean(
    monkeypatch, tmp_path, capsys
):
    gate = _load("scripts/release/quality_gate.py", "_quality_gate_mid_run_empty_skip")
    monkeypatch.setattr(gate, "is_ollama_available", lambda: True)
    monkeypatch.setattr(gate, "_resident_model_note", lambda: None)
    monkeypatch.setattr(gate, "_warm_up_probe", lambda: None)
    monkeypatch.setattr(
        gate, "_run_bench",
        lambda: (1, 2, ["hard/hd-last-page"], ["hard/hd-last-page"]),
    )
    note_path = tmp_path / "dist" / "QUALITY_SKIPPED.txt"
    monkeypatch.setattr(gate, "DIST_DIR", tmp_path / "dist")
    monkeypatch.setattr(gate, "SKIP_NOTE_PATH", note_path)

    reason = "contention discovered mid-run, accepting for this release"
    rc = gate.main(["--skip-quality", reason])

    assert rc == 0
    assert reason in note_path.read_text()


def test_quality_gate_genuine_wrong_answer_still_reports_regression(monkeypatch, capsys):
    """The counterpart to the no-answer tests above: a WRONG (non-empty)
    answer must still be scored as a quality regression, not swept into the
    contention framing."""
    gate = _load("scripts/release/quality_gate.py", "_quality_gate_wrong_answer")
    monkeypatch.setattr(gate, "is_ollama_available", lambda: True)
    monkeypatch.setattr(gate, "_resident_model_note", lambda: None)
    monkeypatch.setattr(gate, "_warm_up_probe", lambda: None)
    monkeypatch.setattr(
        gate, "_run_bench",
        lambda: (1, 2, ["easy/qa-max-value"], []),  # failing, but NOT no_answer
    )

    rc = gate.main([])
    out = capsys.readouterr().out

    assert rc != 0, out
    assert "quality regression" in out.lower()
    assert "NO ANSWER" not in out


# ── quality_gate.py: second Ollama server, GPU memory (2026-11-27 Metal OOM) ──
#
# qwen3.6:35b-a3b-coding (22.4 GB) resident on the router's :11435 plus
# qwen3-coder:30b (18.6 GB) loaded by this gate's probe on :11434 = 40.9 GB of
# 51.5 GB RAM: Metal OOM, empty probe answer, :11435 wedged. The check must
# refuse BEFORE the probe loads anything.

GB = 1_000_000_000
RAM_48 = 51_539_607_552  # sysctl hw.memsize on the 48 GB Mac


def _fake_ollama(gate, monkeypatch, *, here_ps=(), peer_ps=(), target_size=18_556_700_761,
                 ram=RAM_48, peers=("http://127.0.0.1:11435",)):
    peer_set = set(peers)

    def _get_json(url):
        if url.endswith("/api/ps"):
            if url.startswith(gate.OLLAMA_BASE_URL):
                return {"models": [{"name": n} for n in here_ps]}
            if any(url.startswith(p) for p in peer_set):
                return {"models": [{"name": n, "size": s} for n, s in peer_ps]}
        if url == f"{gate.OLLAMA_BASE_URL}/api/tags":
            return {"models": [{"name": gate.TARGET_MODEL, "size": target_size}]}
        return None
    monkeypatch.setattr(gate, "_get_json", _get_json)
    monkeypatch.setattr(gate, "_peer_ollama_urls", lambda: list(peers))
    monkeypatch.setattr(gate, "_physical_memory_bytes", lambda: ram)


def test_peer_memory_conflict_reproduces_the_20261127_oom(monkeypatch):
    gate = _load("scripts/release/quality_gate.py", "_qg_peer_oom")
    _fake_ollama(gate, monkeypatch, peer_ps=[("qwen3.6:35b-a3b-coding", 22_384_570_530)])
    msg = gate._peer_memory_conflict()
    assert msg is not None
    assert "qwen3.6:35b-a3b-coding" in msg and "40.9 GB" in msg and "51.5 GB" in msg


def test_peer_memory_conflict_fits_on_a_bigger_machine(monkeypatch):
    gate = _load("scripts/release/quality_gate.py", "_qg_peer_fits")
    _fake_ollama(gate, monkeypatch, peer_ps=[("qwen3.6:35b-a3b-coding", 22_384_570_530)],
                 ram=128 * GB)
    assert gate._peer_memory_conflict() is None


def test_peer_memory_conflict_none_when_peer_is_empty_or_target_already_loaded(monkeypatch):
    gate = _load("scripts/release/quality_gate.py", "_qg_peer_empty")
    _fake_ollama(gate, monkeypatch, peer_ps=[])
    assert gate._peer_memory_conflict() is None
    _fake_ollama(gate, monkeypatch, peer_ps=[("qwen3.6:35b-a3b-coding", 22_384_570_530)],
                 here_ps=[gate.TARGET_MODEL])
    assert gate._peer_memory_conflict() is None


def test_peer_memory_conflict_undecided_prints_a_note(monkeypatch, capsys):
    gate = _load("scripts/release/quality_gate.py", "_qg_peer_undecided")
    _fake_ollama(gate, monkeypatch, peer_ps=[("qwen3.6:35b-a3b-coding", 22_384_570_530)],
                 target_size=0)
    assert gate._peer_memory_conflict() is None
    assert "GPU-memory check undecided" in capsys.readouterr().out


def test_peer_memory_conflict_none_without_peers(monkeypatch):
    gate = _load("scripts/release/quality_gate.py", "_qg_no_peers")
    _fake_ollama(gate, monkeypatch, peers=(), peer_ps=[("x", 40 * GB)])
    assert gate._peer_memory_conflict() is None


def test_peer_urls_from_env_state_env_and_extra_list(monkeypatch, tmp_path):
    gate = _load("scripts/release/quality_gate.py", "_qg_peer_urls")
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    assert gate._peer_ollama_urls() == []
    (tmp_path / ".env").write_text("FOO=1\nLLM_ROUTER_LOCAL_MODEL_URL='http://127.0.0.1:11435/'\n")
    assert gate._peer_ollama_urls() == ["http://127.0.0.1:11435"]
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL_URL", "http://localhost:11436")
    monkeypatch.setenv("QUALITY_GATE_PEER_OLLAMA_URLS",
                       f"http://localhost:11437, {gate.OLLAMA_BASE_URL}, http://127.0.0.1:11434")
    assert gate._peer_ollama_urls() == ["http://localhost:11436", "http://localhost:11437"]


def test_quality_gate_peer_overload_fails_loud_before_the_probe(monkeypatch, capsys):
    gate = _load("scripts/release/quality_gate.py", "_qg_peer_main")
    monkeypatch.setattr(gate, "is_ollama_available", lambda: True)
    monkeypatch.setattr(gate, "_peer_memory_conflict", lambda: "loading X next to Y needs 41 GB")
    probed = []
    monkeypatch.setattr(gate, "_warm_up_probe", lambda: probed.append(1))
    _mock_clean_pass(gate, monkeypatch)

    rc = gate.main([])
    err = capsys.readouterr().err

    assert rc != 0
    assert probed == []
    assert "OVERRUN GPU memory" in err and "needs 41 GB" in err
    assert "unmeasured quality change" in err


def test_quality_gate_peer_overload_with_skip_flag_records_reason(monkeypatch, tmp_path, capsys):
    gate = _load("scripts/release/quality_gate.py", "_qg_peer_skip")
    monkeypatch.setattr(gate, "DIST_DIR", tmp_path)
    monkeypatch.setattr(gate, "SKIP_NOTE_PATH", tmp_path / "QUALITY_SKIPPED.txt")
    monkeypatch.setattr(gate, "is_ollama_available", lambda: True)
    monkeypatch.setattr(gate, "_peer_memory_conflict", lambda: "needs 41 GB")
    monkeypatch.setattr(gate, "_warm_up_probe", lambda: pytest.fail("probe must not run"))
    _mock_clean_pass(gate, monkeypatch)

    rc = gate.main(["--skip-quality", "runner pinned on :11435"])

    assert rc == 0
    note = (tmp_path / "QUALITY_SKIPPED.txt").read_text()
    assert "runner pinned on :11435" in note and "OVERRUN GPU memory" in note
