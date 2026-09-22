"""Shadow mode must be invisible, and an arm must say which arm it was.

SHADOW IS A MEASUREMENT, NOT A SOFT LAUNCH

The point of shadow is to learn what a feature would have done without letting
it do anything. The moment shadow changes one byte of what reaches the model,
every comparison against `off` is confounded and the mode is worse than
useless — it is a change nobody agreed to, running under a name that says it
is not a change.

So the test is byte equality, not "roughly the same" or "no behaviour change
observed". And it must still record its cost, or shadow measures nothing at
all and you have paid for the overhead twice.

THREE SWITCHES, NOT ONE

Source retrieval, history retrieval and intervention fail differently and are
worth different amounts. One flag over all three means the first problem in
any of them turns off the other two, and the answer to "what did this buy" is
never attributable to a part.

ARMS HAVE TO SELF-IDENTIFY

The research document's whole evaluation rests on comparing B/C/D and M0/M1/M2.
A result that cannot say which arm produced it is a number without a treatment
attached, and offline replay of it is not valid — the first thing to get wrong
here is running an arm and forgetting which one, which the stamp prevents.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from llm_router.semantic import modes


def _repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for rel, body in files.items():
        p = path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True,
                   capture_output=True, timeout=30)
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True,
                   capture_output=True, timeout=30)
    return path


@pytest.fixture
def project(tmp_path: Path):
    from llm_router.semantic import indexer as ix
    repo = _repo(tmp_path / "repo", {
        "ledger.py": "def post_entry(amount):\n    return amount\n",
    })
    base = tmp_path / "store"
    ix.index(root=repo, base=base)
    return repo, base


@pytest.fixture
def store(tmp_path: Path):
    from llm_router.semantic import experience as exp
    s = exp.ExperienceStore(tmp_path / "experience")
    s.put(exp.Lesson(
        lesson_id="ledger-001",
        statement="post_entry must not run twice for one line.",
        affected_paths=["ledger.py"], affected_symbols=["post_entry"],
        known_from="2026-09-18",
    ))
    return s


# ── the three switches ───────────────────────────────────────────────────────

def test_the_three_controls_are_independent(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_SOURCE", "on")
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_HISTORY", "shadow")
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_INTERVENTION", "off")

    cfg = modes.current()
    assert cfg.source is modes.Mode.ON
    assert cfg.history is modes.Mode.SHADOW
    assert cfg.intervention is modes.Mode.OFF


def test_only_the_measured_switch_is_on_by_default(monkeypatch):
    """The defaults follow the evidence, switch by switch.

    Source retrieval was measured at n=60 against the corrected baseline —
    58/60 vs 41/60, 17 discordant pairs all one way, p=1.5e-05 — and the
    configuration measured is the one that ships. So it is on.

    History and intervention have no such number; the M0/M1/M2 track has not
    been run. They stay off. Turning on the measured half while leaving the
    unmeasured half alone is the entire reason these are three switches and not
    one, and a change that flips all three together has lost the distinction.
    """
    for name in ("SOURCE", "HISTORY", "INTERVENTION"):
        monkeypatch.delenv(f"LLM_ROUTER_SEMANTIC_{name}", raising=False)
    monkeypatch.delenv("LLM_ROUTER_SEMANTIC_ARM", raising=False)

    cfg = modes.current()
    assert cfg.source is modes.Mode.ON
    assert cfg.history is modes.Mode.OFF, (
        "history retrieval is on by default and the M-track has never been run"
    )
    assert cfg.intervention is modes.Mode.OFF


def test_an_explicit_off_still_beats_the_default(monkeypatch):
    """A default is not a policy. Anyone can still turn it off."""
    monkeypatch.delenv("LLM_ROUTER_SEMANTIC_ARM", raising=False)
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_SOURCE", "off")
    assert modes.current().source is modes.Mode.OFF


def test_an_unknown_mode_falls_back_to_off_rather_than_guessing(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_SOURCE", "probably")
    assert modes.current().source is modes.Mode.OFF


# ── shadow is invisible ──────────────────────────────────────────────────────

def test_shadow_output_is_byte_identical_to_off(project, store, monkeypatch):
    """The load-bearing assertion of the whole mode."""
    repo, base = project

    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_SOURCE", "off")
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_HISTORY", "off")
    off = modes.apply("fix post_entry in ledger.py", root=repo, base=base,
                      experience=store)

    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_SOURCE", "shadow")
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_HISTORY", "shadow")
    shadow = modes.apply("fix post_entry in ledger.py", root=repo, base=base,
                         experience=store)

    assert shadow.prompt == off.prompt, (
        "shadow changed the prompt. Every comparison against off is now "
        "confounded, and the mode is a change nobody agreed to running under a "
        "name that says it is not one"
    )


def test_on_actually_changes_the_prompt(project, store, monkeypatch):
    """The control. Without it the test above passes when nothing works."""
    repo, base = project
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_SOURCE", "off")
    off = modes.apply("fix post_entry in ledger.py", root=repo, base=base,
                      experience=store)

    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_SOURCE", "on")
    on = modes.apply("fix post_entry in ledger.py", root=repo, base=base,
                     experience=store)

    assert on.prompt != off.prompt
    assert "post_entry" in on.prompt


def test_shadow_records_the_overhead_it_did_not_spend_on_you(project, store,
                                                             monkeypatch):
    """Otherwise shadow measures nothing and you pay for it twice."""
    repo, base = project
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_SOURCE", "shadow")

    result = modes.apply("fix post_entry in ledger.py", root=repo, base=base,
                         experience=store)

    assert result.shadow is not None
    assert result.shadow["would_have_added_tokens"] > 0, (
        "shadow reported adding nothing, so either retrieval found nothing or "
        "the measurement is not happening"
    )
    assert result.shadow["elapsed_ms"] >= 0
    assert result.shadow["retrieval_status"]


def test_off_does_no_work_at_all(project, store, monkeypatch):
    """Off is off, not shadow with the output discarded."""
    repo, base = project
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_SOURCE", "off")
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_HISTORY", "off")

    result = modes.apply("fix post_entry in ledger.py", root=repo, base=base,
                         experience=store)
    assert result.shadow is None
    assert result.pack is None


# ── arms ─────────────────────────────────────────────────────────────────────

def test_every_arm_is_selectable_and_stamps_its_own_name(project, store,
                                                         monkeypatch):
    repo, base = project
    for arm in ("B", "C", "M0", "M1", "M2"):
        monkeypatch.setenv("LLM_ROUTER_SEMANTIC_ARM", arm)
        result = modes.apply("fix post_entry in ledger.py", root=repo,
                             base=base, experience=store)
        assert result.arm == arm, (
            f"arm {arm} produced a result stamped {result.arm!r}; a number with "
            f"no treatment attached cannot be compared with anything"
        )


def test_arm_b_is_the_corrected_baseline_with_no_semantic_layer(project, store,
                                                                monkeypatch):
    repo, base = project
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_ARM", "B")
    result = modes.apply("fix post_entry in ledger.py", root=repo, base=base,
                         experience=store)
    assert result.pack is None, "arm B is the no-graph baseline and got a pack"


def test_arm_c_has_source_evidence_and_arm_m1_has_experience(project, store,
                                                             monkeypatch):
    repo, base = project

    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_ARM", "C")
    c = modes.apply("fix post_entry in ledger.py", root=repo, base=base,
                    experience=store)
    assert c.pack is not None and c.pack.evidence

    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_ARM", "M1")
    m1 = modes.apply("fix post_entry in ledger.py", root=repo, base=base,
                     experience=store)
    assert m1.pack is not None and m1.pack.applicable_lessons, (
        "M1 is the typed-experience arm and retrieved no experience"
    )


def test_an_arm_overrides_the_individual_switches(project, store, monkeypatch):
    """Otherwise an experiment silently inherits whatever was left set."""
    repo, base = project
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_SOURCE", "on")
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_ARM", "B")

    result = modes.apply("fix post_entry in ledger.py", root=repo, base=base,
                         experience=store)
    assert result.pack is None
    assert result.arm == "B"


def test_an_unknown_arm_is_refused_rather_than_silently_ignored(project, store,
                                                               monkeypatch):
    """A typo'd arm name that falls back to default produces a mislabelled run."""
    repo, base = project
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_ARM", "Z9")
    with pytest.raises(ValueError, match="Z9"):
        modes.apply("fix post_entry", root=repo, base=base, experience=store)


def _identifiers_in(module) -> set[str]:
    """Every identifier the code actually USES — names, attributes, args.

    R13/A-10: `forbidden not in inspect.getsource(modes)` scans the WHOLE
    module's text — comments, docstrings, everything — so this line itself
    (which has to name the forbidden identifiers to explain the rule) would
    make the module fail-open in the other direction, and worse, a comment
    quoting one of these names would fail a check that should be about real
    code touching model selection. Collecting identifiers from the parsed AST
    means only actual `Name`/`Attribute`/`arg`/`keyword` references count —
    comments and docstrings cannot contribute one.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(module)))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.arg):
            out.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.keyword) and node.arg:
            out.add(node.arg)
    return out


def test_routing_policy_is_not_something_an_arm_can_change():
    """The document pins it, and this is where someone would forget.

    D vs C measures retrieval; E vs D measures routing. An arm that could do
    both makes neither attributable, which is the one thing the arm structure
    exists to prevent.
    """
    used = _identifiers_in(modes)
    for forbidden in ("models_to_try", "select_model", "routing_profile",
                      "model_chain"):
        assert forbidden not in used, (
            f"modes.py touches {forbidden}: an arm is changing model selection "
            f"in the same experiment as context, so neither result is "
            f"attributable"
        )
