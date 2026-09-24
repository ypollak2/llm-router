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

REPO = pathlib.Path(__file__).resolve().parents[1]


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
