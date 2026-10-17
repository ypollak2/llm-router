"""Gate 13 (#23) — mutation-coverage tests for gates.py.

These pin behaviours that a mutmut run showed were NOT exercised by the existing
gate tests (survivors in `_check_length`, `run_gates`, `_check_syntax`). Each test
targets a specific surviving mutant so the mutation score on gates.py rises toward
"all non-equivalent mutants killed".

Gates auto-skip under pytest unless LLM_ROUTER_GATES=on (see run_gates), so where a
test needs the *real* env-branching behaviour it manipulates LLM_ROUTER_GATES and
PYTEST_CURRENT_TEST explicitly.
"""
from __future__ import annotations

import pathlib

import pytest

from llm_router.contract import GateType, build_contract
from llm_router.gates import _check_length, _check_syntax, run_gates
from llm_router.types import Complexity, TaskType


def _contract(task_type=TaskType.ANALYZE, complexity=Complexity.COMPLEX):
    return build_contract("m", task_type, complexity, "test/model")


# ── _check_length boundary: kills `actual < min_len` → `actual <= min_len` ────

def test_length_gate_exact_boundary_passes():
    """A response EXACTLY at min_output_length must PASS. `<` vs `<=` differ only
    at the boundary, so this is the one input that kills the mutant."""
    c = _contract(complexity=Complexity.COMPLEX)  # min_output_length == 50
    min_len = c.constraints.min_output_length
    assert _check_length(c, "x" * min_len).passed, "exactly min chars must pass"
    r = _check_length(c, "x" * (min_len - 1))
    assert not r.passed, "one char under min must fail"
    assert "too short" in r.reason


# ── run_gates env handling ────────────────────────────────────────────────────

def test_gates_off_disables_even_without_pytest_marker(monkeypatch):
    """LLM_ROUTER_GATES=off must disable gates via the 'off' branch specifically —
    not incidentally via the pytest-skip path. Removing PYTEST_CURRENT_TEST
    isolates the 'off' branch (kills the "off"→"XXoffXX" mutant)."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("LLM_ROUTER_GATES", "off")
    c = build_contract("g", TaskType.CODE, Complexity.MODERATE, "test/m")
    passed, results = run_gates(c, "x")  # would fail LENGTH if gates ran
    assert passed and results == []


def test_gates_run_when_env_unset_and_not_pytest(monkeypatch):
    """With LLM_ROUTER_GATES unset AND no pytest marker, gates RUN (not skipped, not
    crashed). Kills the `os.environ.get("LLM_ROUTER_GATES", "")` → `, None` mutant,
    which would raise AttributeError on None.lower() when the var is unset."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("LLM_ROUTER_GATES", raising=False)
    c = build_contract("g", TaskType.CODE, Complexity.MODERATE, "test/m")
    passed, results = run_gates(c, "x")  # too short → LENGTH fails
    assert not passed, "gates must actually run (and fail) when env is unset"
    assert results, "gate results must be produced, not skipped/crashed"


def test_gates_on_forces_run_under_pytest(monkeypatch):
    """LLM_ROUTER_GATES=on overrides the pytest auto-skip. Pins the 'on' comparison."""
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x")
    monkeypatch.setenv("LLM_ROUTER_GATES", "on")
    c = build_contract("g", TaskType.CODE, Complexity.MODERATE, "test/m")
    passed, _ = run_gates(c, "x")
    assert not passed, "LLM_ROUTER_GATES=on must force gates to run under pytest"


# ── _check_syntax code detection: case + structure ───────────────────────────

@pytest.fixture(autouse=True)
def _force_gates(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_GATES", "on")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)


def test_syntax_gate_detects_unfenced_lowercase_code_with_error():
    """A non-fenced response whose first lines start with lowercase `def `/`return`
    is treated as code; a syntax error in it must FAIL. Kills the case-folding
    mutants (`"def "`→`"DEF "`, etc.) — with them the lowercase keywords aren't
    detected, the block is treated as prose, and the broken code slips through."""
    c = _contract(task_type=TaskType.CODE)
    broken = "def f(:\n    return 1\nfrom x import y\n"  # 3 code-indicator lines, invalid syntax
    r = _check_syntax(c, broken)
    assert not r.passed, "unfenced lowercase code with a syntax error must fail"
    assert "SyntaxError" in r.reason


def test_syntax_gate_prose_is_not_code():
    """Plain prose (no code-indicator lines) passes the syntax gate — pins the
    'non-code response' branch so a mutant flipping the indicator threshold is
    caught alongside the detection tests."""
    c = _contract(task_type=TaskType.CODE)
    r = _check_syntax(c, "This is a normal sentence. And another one here.")
    assert r.passed


# ── _check_structure (lever-① new code): kill the marker/boundary/field mutants ─

from llm_router.gates import _check_structure  # noqa: E402


def _structure_contract():
    return build_contract("s", TaskType.ANALYZE, Complexity.MODERATE, "test/m")


@pytest.mark.parametrize("marker", ["## ", "### ", "- ", "* ", "1. "])
def test_structure_each_marker_type_counts(marker):
    """A >200-char, single-paragraph, <3-sentence body structured ONLY by exactly
    two of ONE marker type must PASS. Removing that marker must FAIL. This kills
    the per-marker string mutants (`"\\n## "`→`"XX\\n## XX"`), the `+`→`-`
    marker-sum mutants, and the `markers >= 2` boundary mutants — for every
    marker kind — because the marker count is the sole reason the body is legible."""
    c = _structure_contract()
    filler = "x" * 110  # no sentence punctuation, no blank lines
    body = f"lead\n{marker}{filler}\n{marker}{filler}"
    assert len(body) > 200
    assert _check_structure(c, body).passed, f"two '{marker}' markers must structure it"
    # Strip the markers → same length-class body, now genuinely unstructured.
    unstructured = body.replace(f"\n{marker}", "\n")
    assert not _check_structure(c, unstructured).passed, \
        f"without the '{marker}' markers the wall must fail"


def test_structure_exactly_three_sentences_passes():
    """Structured ONLY by exactly 3 sentences (0 markers, 1 paragraph) must PASS —
    kills the `sentences >= 3` → `> 3` / `>= 4` mutants."""
    c = _structure_contract()
    body = (
        "This first sentence is padded with enough words to push the whole body "
        "comfortably past two hundred characters in total length here. This is the "
        "second sentence adding still more filler words for length. And a third."
    )
    assert body.count(".") == 3 and "\n" not in body and len(body) > 200
    assert _check_structure(c, body).passed


def test_structure_length_gate_boundary_exact():
    """The >200 length guard: a 200-char unstructured body PASSES (not gated), a
    201-char one FAILS. Kills `> 200` → `>= 200` and `> 200` → `> 201`."""
    c = _structure_contract()
    assert _check_structure(c, "a" * 200).passed, "exactly 200 chars must not be gated"
    assert not _check_structure(c, "a" * 201).passed, "201 unstructured chars must fail"


def test_structure_result_fields_are_populated():
    """Kill the GateResult field mutants (gate=None/passed=None/reason=None): a
    failing structure check reports gate=STRUCTURE, passed=False, non-empty reason;
    a passing one reports gate=STRUCTURE, passed=True."""
    c = _structure_contract()
    fail = _check_structure(c, "a" * 300)
    assert fail.gate == GateType.STRUCTURE
    assert fail.passed is False
    assert fail.reason and "unstructured wall" in fail.reason
    ok = _check_structure(c, "short prose. two sentences. three sentences here.")
    assert ok.gate == GateType.STRUCTURE
    assert ok.passed is True


# ── _check_citation (pre-existing): kill detection/combo/boundary/field mutants ─

from llm_router.gates import _check_citation  # noqa: E402


def _cite(text):
    return _check_citation(_contract(task_type=TaskType.RESEARCH), text)


def test_citation_url_only_passes():
    """A bare (lowercase) URL is sufficient citation. The url regex is
    case-SENSITIVE (`https?://`, no IGNORECASE), so a lowercase URL matches the
    original but NOT the `HTTPS?://` case-fold mutant → kills it."""
    assert _cite("See https://example.com/paper for details. " + "x" * 300).passed


def test_citation_marker_only_passes():
    """Citation markers alone suffice — pins the has_citation regex (incl. case)."""
    for marker in ("See [1] here.", "As [source] notes.", "According to the study.",
                   "per the report", "ACCORDING TO everyone"):
        assert _cite(marker + " " + "x" * 300).passed, marker


def test_citation_reference_word_only_passes():
    """The word 'reference' or 'source' alone suffices — pins has_reference + case."""
    assert _cite("This reference explains it. " + "x" * 300).passed
    assert _cite("Our SOURCE confirms it. " + "x" * 300).passed


def test_citation_no_signal_short_passes_long_fails():
    """No url/citation/reference: a short body passes (gate n/a); a >300-char body
    fails with the exact reason. Pins the >300 boundary, the reason, and fields."""
    assert _cite("x" * 100).passed, "short uncited body is not gated"
    r = _cite("x" * 400)
    assert r.gate == GateType.CITATION and r.passed is False
    assert r.reason == "no citations found in research response"


def test_citation_length_boundary_exact():
    """Exactly 300 uncited chars pass (not gated); 301 fail. Kills >=300 and >301."""
    assert _cite("x" * 300).passed
    assert not _cite("x" * 301).passed


def test_citation_combination_is_or_not_and():
    """Each signal independently suffices — kills the `or`→`and` boolean mutants:
    citation-only (no url, no reference) and url-only (no citation, no reference)
    must BOTH pass."""
    assert _cite("[1] only " + "x" * 300).passed          # citation only
    assert _cite("http://a.b/c only " + "x" * 300).passed  # url only


# ── _check_format (pre-existing): kill fmt-branch / json / field mutants ───────

from dataclasses import replace as _dc_replace  # noqa: E402

from llm_router.gates import _check_format  # noqa: E402


def _fmt_contract(required_format):
    c = _contract(task_type=TaskType.GENERATE)
    return _dc_replace(c, constraints=_dc_replace(c.constraints, required_format=required_format))


def test_format_none_required_passes():
    """required_format=None → passes with the 'no format required' reason. Kills the
    `fmt is None`/`is not None` flip and the field/reason mutants on that path."""
    r = _check_format(_fmt_contract(None), "anything at all")
    assert r.gate == GateType.FORMAT and r.passed is True
    assert r.reason == "no format required"


def test_format_json_valid_passes():
    """required_format='json' + valid JSON → passes via the final return. Kills the
    `fmt=='json'` string/case flips and json.loads(None) on the happy path."""
    r = _check_format(_fmt_contract("json"), '{"a": 1, "b": [2, 3]}')
    assert r.gate == GateType.FORMAT and r.passed is True


def test_format_json_invalid_fails_with_reason():
    """required_format='json' + invalid JSON → fails with the exact 'invalid JSON'
    reason. Kills the passed/gate/reason field mutants and the reason string/case."""
    r = _check_format(_fmt_contract("json"), "this is not json{")
    assert r.gate == GateType.FORMAT
    assert r.passed is False
    assert r.reason == "invalid JSON"


def test_format_non_json_format_is_not_json_checked():
    """required_format='code' (≠ json) skips the JSON parse and passes — kills the
    `fmt == 'json'` → `fmt != 'json'` mutant (which would JSON-check code)."""
    r = _check_format(_fmt_contract("code"), "def f(): return 1")
    assert r.gate == GateType.FORMAT and r.passed is True


# ── _check_syntax keyword detection + threshold + fields ──────────────────────

# One 2-line, syntax-broken snippet per detected keyword. Two lines start with the
# SAME keyword → 2 code-indicators → the text is parsed → SyntaxError → FAIL.
# Breaking that keyword's detection drops indicators below 2 → "non-code" → the
# broken code would pass. Asserting FAIL kills the per-keyword string+case mutants
# AND the `code_indicators < 2` → `<= 2` / `< 3` threshold mutants.
_SYNTAX_BROKEN = {
    "def ": "def f(:\ndef g(:",
    "class ": "class A(:\nclass B(:",
    "import ": "import (\nimport (",
    "from ": "from (\nfrom (",
    "if ": "if :\nif :",
    "for ": "for :\nfor :",
    "return ": "return (\nreturn (",
}


@pytest.mark.parametrize("keyword,code", list(_SYNTAX_BROKEN.items()))
def test_syntax_each_keyword_is_detected_and_broken_code_fails(keyword, code):
    c = _contract(task_type=TaskType.CODE)
    r = _check_syntax(c, code)
    assert r.gate == GateType.SYNTAX
    assert r.passed is False, f"{keyword!r} code with a syntax error must fail"
    assert "SyntaxError" in r.reason


def test_syntax_non_code_response_fields():
    """Non-code prose returns gate=SYNTAX, passed=True, reason='non-code response'
    — kills the field/reason mutants on that branch."""
    c = _contract(task_type=TaskType.CODE)
    r = _check_syntax(c, "Just one short sentence, nothing code-like about it.")
    assert r.gate == GateType.SYNTAX
    assert r.passed is True
    assert r.reason == "non-code response"


def test_syntax_valid_fenced_code_fields():
    """Valid fenced code returns gate=SYNTAX, passed=True — kills the final
    `gate=None` mutant on the valid path."""
    c = _contract(task_type=TaskType.CODE)
    r = _check_syntax(c, "```python\ndef add(a, b):\n    return a + b\n```")
    assert r.gate == GateType.SYNTAX and r.passed is True


# ── _check_length fields + premium-downshift warning ──────────────────────────

def test_length_gate_result_fields():
    c = _contract(complexity=Complexity.COMPLEX)
    ok = _check_length(c, "x" * c.constraints.min_output_length)
    assert ok.gate == GateType.LENGTH and ok.passed is True
    bad = _check_length(c, "x")
    assert bad.gate == GateType.LENGTH and bad.passed is False


def test_length_premium_warning_names_task_and_phrases(caplog):
    """A short answer on a COMPLEX/premium task logs a WARNING that names the
    task_type/complexity and the downshift phrasing — kills the log-string and
    the None-arg (task_type/complexity) mutants."""
    import logging
    c = _contract(task_type=TaskType.ANALYZE, complexity=Complexity.COMPLEX)
    with caplog.at_level(logging.WARNING, logger="llm_router.gates"):
        _check_length(c, "x")  # far under min → premium warning path
    msgs = [rec.getMessage() for rec in caplog.records]
    joined = " ".join(msgs)
    assert any("premium task" in m for m in msgs), msgs
    assert "analyze" in joined and "complex" in joined
    assert "downshift" in joined
    # Pin the message text EXACTLY enough to kill the log-string mutants: no
    # mutmut XX-wrapping survives, and the exact-case phrases kill the case flips.
    assert "XX" not in joined, "log string must not carry mutmut XX-padding"
    assert "Length gate failed on premium task" in joined      # exact case
    assert "A brief valid answer may be forcing" in joined     # exact case


# ── run_gates pytest-skip branch ──────────────────────────────────────────────

def test_run_gates_skips_under_pytest_when_not_forced_on(monkeypatch):
    """With PYTEST_CURRENT_TEST set and LLM_ROUTER_GATES unset/≠on, run_gates auto-skips
    and returns (True, []) — kills the PYTEST_CURRENT_TEST env-name string/case
    mutants and the `return True, []` → `return False, []` mutant."""
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x::y (call)")
    monkeypatch.delenv("LLM_ROUTER_GATES", raising=False)
    c = build_contract("g", TaskType.CODE, Complexity.MODERATE, "test/m")
    passed, results = run_gates(c, "x")  # would FAIL length if gates ran
    assert passed is True and results == []


# ── _check_citation pass-path fields ──────────────────────────────────────────

def test_citation_pass_paths_report_gate():
    """Kills the `gate=None` mutants on both citation pass paths (cited, and
    short-uncited)."""
    assert _cite("See [1].").gate == GateType.CITATION
    assert _cite("x" * 50).gate == GateType.CITATION


def test_syntax_single_code_indicator_line_is_non_code():
    """A SINGLE code-indicator line is below the ≥2 threshold → treated as
    non-code and passed (the gate isn't confident it's code). Kills the
    `sum(1 …)` → `sum(2 …)` mutant, which would count one line as 2 and parse it."""
    c = _contract(task_type=TaskType.CODE)
    r = _check_syntax(c, "def f(:")   # one broken code line, on its own
    assert r.passed is True, "a lone code-ish line is non-code, not a syntax failure"


def test_syntax_only_first_ten_lines_are_scanned_for_indicators():
    """Indicator scan is bounded to the first 10 lines (`lines[:10]`). With one
    code line at the top and a second only at line 11, the count stays at 1 (<2)
    → non-code → pass. The `[:10]`→`[:11]` mutant would see 2 indicators, parse,
    and fail — so asserting PASS kills it."""
    c = _contract(task_type=TaskType.CODE)
    lines = ["def f(:"] + ["just some prose here"] * 9 + ["class B(:"]  # 11 lines
    r = _check_syntax(c, "\n".join(lines))
    assert r.passed is True


# ── P3.2 release eval gate: real source mutants ───────────────────────────────
#
# Unlike the sections above (which pin behaviours a mutmut run found unexercised),
# these apply a named textual mutation to a COPY of scripts/release/eval_gate.py and
# require the named check from tests/test_release_eval_gate.py to turn red. The
# unmutated copy must be green on every check (negative control), and every
# replacement must match exactly once, so a refactor that moves the line cannot
# turn a mutant into a silent no-op.

_GATE_SRC = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "release" / "eval_gate.py"
_REL_SRC = _GATE_SRC.with_name("release_evals.py")

_FIDELITY_CALL = "code, text = judge_fidelity(m, cand_items)"
_STABILITY_CALL = "code, text = judge_stability(m, cand_items)"
_POSITIONAL_OLD = (
    '    for i in manifest["item_ids"]:\n'
    '        bp, cp = bool(base[i]["pass"]), bool(cand[i]["pass"])\n'
)
_POSITIONAL_NEW = (
    '    for k, i in enumerate(manifest["item_ids"]):\n'
    '        bp, cp = bool(base_items[k]["pass"]), bool(cand_items[k]["pass"])\n'
)

# (id, [(old, new), ...], check that must go red)
_P32_MUTANTS = [
    ("M1 drop > 3.0 becomes >=", [("if drop > DROP_BLOCK_PTS:", "if drop >= DROP_BLOCK_PTS:")],
     "check_perf_exactly_3pt_passes"),
    ("M2 baseline/candidate sign swapped", [("Fraction(b - c, n) * 100", "Fraction(c - b, n) * 100")],
     "check_perf_4pt_drop_blocks"),
    ("M3 unpaired: gains (c) ignored", [("Fraction(b - c, n) * 100", "Fraction(b, n) * 100")],
     "check_perf_improvement_and_offset_pass"),
    ("M3b unpaired: items matched by position, not id", [(_POSITIONAL_OLD, _POSITIONAL_NEW)],
     "check_perf_pairs_by_id_not_position"),
    ("M4 n checks dropped (min_n and manifest n)",
     [('if len(items) < manifest["min_n"]:', "if False:"),
      ('if len(items) != manifest["n"]:', "if False:")],
     "check_n_checks_inside_the_comparator"),
    ("M4b duplicate-id check dropped", [("    if dup:\n", "    if False:\n")],
     "check_duplicate_id_not_informative"),
    ("M4c missing-set / no-manifests tolerated", [("if not manifests:", "if False:")],
     "check_missing_set_and_no_manifests_not_informative"),
    ("M4d candidate sha not checked", [("if expect_sha is not None and", "if False and")],
     "check_wrong_sha_not_informative"),
    ("M5 fidelity step disabled", [(_FIDELITY_CALL, 'code, text = PASS, "skipped"')],
     "check_fidelity_bar"),
    ("M5b fidelity bar > becomes >=", [("if gap > FIDELITY_MAX:", "if gap >= FIDELITY_MAX:")],
     "check_fidelity_bar"),
    ("M6 stability step disabled", [(_STABILITY_CALL, 'code, text = PASS, "skipped"')],
     "check_stability_bar"),
    ("M6b stability bar < becomes <=", [("if share < STABILITY_MIN:", "if share <= STABILITY_MIN:")],
     "check_stability_bar"),
    ("M7 skip refusal off", [("if not on_tag:", "if True:")], "check_skip_policy"),
    ("M7b owner override accepts any value",
     [('env.get("LLM_ROUTER_RELEASE_OWNER_OVERRIDE") == "1"', 'env.get("LLM_ROUTER_RELEASE_OWNER_OVERRIDE")')],
     "check_skip_policy"),
]


def _load_p32(tmp_path, src_text, tag):
    import importlib.util
    d = tmp_path / tag
    d.mkdir()
    (d / "eval_gate.py").write_text(src_text)
    (d / "release_evals.py").write_text(_REL_SRC.read_text())
    spec = importlib.util.spec_from_file_location(f"_p32_{tag}", d / "eval_gate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rel = importlib.util.spec_from_file_location(f"_p32_rel_{tag}", d / "release_evals.py")
    rmod = importlib.util.module_from_spec(rel)
    rel.loader.exec_module(rmod)
    return mod, rmod


def test_p32_unmutated_copy_is_green_on_every_check(tmp_path):
    """Negative control: the harness itself must not fail a clean copy, and the
    table must be non-empty (an empty one passes everything)."""
    from tests.test_release_eval_gate import CHECKS
    mod, rmod = _load_p32(tmp_path, _GATE_SRC.read_text(), "clean")
    assert CHECKS and _P32_MUTANTS
    for name, check in CHECKS.items():
        check(mod, rmod)


@pytest.mark.parametrize("mid,edits,killer", _P32_MUTANTS, ids=[m[0].split()[0] for m in _P32_MUTANTS])
def test_p32_mutant_turns_its_check_red(tmp_path, mid, edits, killer):
    from tests.test_release_eval_gate import CHECKS
    src = _GATE_SRC.read_text()
    for old, new in edits:
        assert src.count(old) == 1, f"{mid}: anchor must match exactly once: {old!r}"
        src = src.replace(old, new)
    mod, rmod = _load_p32(tmp_path, src, "mut")
    # pytest.fail / raises-miss raise Failed, which is not an Exception subclass
    failed = []
    for name, check in CHECKS.items():
        try:
            check(mod, rmod)
        except (Exception, pytest.fail.Exception):
            failed.append(name)
    assert killer in failed, f"{mid}: killed by {failed}, expected {killer}"
