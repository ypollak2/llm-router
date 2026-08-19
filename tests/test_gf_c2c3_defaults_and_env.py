"""G-F classes C2 and C3 — env-var overrides, and `.get()` defaults on unknown keys.

TWO CLASSES, ONE OMISSION
-------------------------
C2 (10 mutants): every `os.environ.get("LLM_ROUTER_…", default)` read survives mutation of
the variable NAME and of the default. Nothing sets these variables and asserts the
override takes effect, so a mutant can read a differently-named variable — or read
nothing at all — and no test notices. These are documented, user-facing knobs.

C3 (12 mutants): every `dict.get(key, default)` survives having its DEFAULT removed.
`get(key)` returns None instead of the fallback and, where the default is a value the
caller depends on, changes behaviour from graceful to broken. The mutants survive because
no test passes a key that is absent — every existing call uses a known task type, a known
tier, a known model.

Both are the same omission from opposite ends: the happy path is exercised and the
declared fallback is not.

WHY THE UNKNOWN-KEY CASES MATTER BEYOND THE SCORE
------------------------------------------------
`MODEL_COST_PER_1K.get(model, 0)` is on the savings path: an unmapped model must price at
zero, not raise. `_TIER_FLOOR.get(tier, "llm_query")` is what makes an unrecognised tier
degrade to a safe floor rather than a KeyError. Those defaults are the contract; a test
that never passes an unknown key never checks it.
"""

from __future__ import annotations

from llm_router import classify, cost, tool_surface


class TestGeminiBaselineOverride:
    """`LLM_ROUTER_GEMINI_BASELINE` — documented in the function's own docstring."""

    def test_a_valid_override_wins_over_the_task_default(self, monkeypatch):
        # "research" would otherwise select gemini-2.5-pro; the override must win.
        target = next(m for m in cost.GEMINI_RATES_PER_M if m != "gemini-2.5-pro")
        monkeypatch.setenv("LLM_ROUTER_GEMINI_BASELINE", target)
        assert cost._get_gemini_baseline_for_task("research", None) == target

    def test_the_override_is_case_insensitive(self, monkeypatch):
        target = next(m for m in cost.GEMINI_RATES_PER_M if m != "gemini-2.5-pro")
        monkeypatch.setenv("LLM_ROUTER_GEMINI_BASELINE", target.upper())
        assert cost._get_gemini_baseline_for_task("research", None) == target

    def test_surrounding_whitespace_is_ignored(self, monkeypatch):
        target = next(m for m in cost.GEMINI_RATES_PER_M if m != "gemini-2.5-pro")
        monkeypatch.setenv("LLM_ROUTER_GEMINI_BASELINE", f"  {target}  ")
        assert cost._get_gemini_baseline_for_task("research", None) == target

    def test_an_unknown_override_is_ignored_not_adopted(self, monkeypatch):
        """A typo must not become the baseline.

        The guard is `if override in GEMINI_RATES_PER_M`. Adopting an unpriced model
        would make every savings figure computed against it meaningless.
        """
        monkeypatch.setenv("LLM_ROUTER_GEMINI_BASELINE", "not-a-real-model")
        assert cost._get_gemini_baseline_for_task("research", None) == "gemini-2.5-pro"

    def test_no_override_uses_the_task_default(self, monkeypatch):
        monkeypatch.delenv("LLM_ROUTER_GEMINI_BASELINE", raising=False)
        assert cost._get_gemini_baseline_for_task("research", None) == "gemini-2.5-pro"


class TestCodexBaselineOverride:
    """`LLM_ROUTER_CODEX_BASELINE` — same contract, separate variable.

    Asserted separately rather than parametrised with the Gemini case: a mutant that
    swapped one variable name for the other would pass a shared test.
    """

    def test_a_valid_override_wins_over_the_task_default(self, monkeypatch):
        """Codex validates its override against OPENAI_RATES_PER_M, not GEMINI's.

        The first version of this class only asserted the fallback was "a non-empty
        string" and killed 0 of 3 mutants — a test that cannot fail is worth nothing
        however sensible it reads. The override path is where the mutants live.
        """
        default = cost._get_codex_baseline_for_task("query", "simple")
        target = next(m for m in cost.OPENAI_RATES_PER_M if m != default)
        monkeypatch.setenv("LLM_ROUTER_CODEX_BASELINE", target)
        assert cost._get_codex_baseline_for_task("query", "simple") == target

    def test_the_override_is_case_and_whitespace_insensitive(self, monkeypatch):
        default = cost._get_codex_baseline_for_task("query", "simple")
        target = next(m for m in cost.OPENAI_RATES_PER_M if m != default)
        monkeypatch.setenv("LLM_ROUTER_CODEX_BASELINE", f"  {target.upper()}  ")
        assert cost._get_codex_baseline_for_task("query", "simple") == target

    def test_an_unpriced_override_is_ignored(self, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_CODEX_BASELINE", "not-a-real-model")
        assert cost._get_codex_baseline_for_task("query", "simple") != "not-a-real-model"

    def test_no_override_falls_back_to_the_task_default(self, monkeypatch):
        monkeypatch.delenv("LLM_ROUTER_CODEX_BASELINE", raising=False)
        baseline = cost._get_codex_baseline_for_task("query", "simple")
        assert baseline in cost.OPENAI_RATES_PER_M

    def test_the_two_baseline_variables_are_independent(self, monkeypatch):
        """Setting the GEMINI variable must not change the CODEX answer."""
        before = cost._get_codex_baseline_for_task("query", "simple")
        monkeypatch.setenv("LLM_ROUTER_GEMINI_BASELINE", "gemini-2.5-flash")
        assert cost._get_codex_baseline_for_task("query", "simple") == before


class TestActiveSlimEnvVar:
    """`(os.environ.get("LLM_ROUTER_SLIM") or "consolidated").strip().lower()`.

    The docstring is explicit that the default matters: "an unset env var means
    `consolidated`, not `off`. Getting this default wrong is the original bug."
    """

    def test_unset_defaults_to_consolidated(self, monkeypatch):
        monkeypatch.delenv("LLM_ROUTER_SLIM", raising=False)
        assert tool_surface.active_slim() == "consolidated"

    def test_empty_string_also_defaults_to_consolidated(self, monkeypatch):
        """`or` rather than a `.get` default: an EMPTY value must fall back too.

        `os.environ.get("LLM_ROUTER_SLIM", "consolidated")` would return "" here. The `or`
        spelling is what makes an exported-but-blank variable behave as unset.
        """
        monkeypatch.setenv("LLM_ROUTER_SLIM", "")
        assert tool_surface.active_slim() == "consolidated"

    def test_an_explicit_value_is_honoured_case_and_space_insensitively(self, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_SLIM", "  OFF  ")
        assert tool_surface.active_slim() == "off"


class TestDictDefaultsOnUnknownKeys:
    """C3 — every one of these passes a key that is NOT in the dict.

    Existing tests use known task types, known tiers and known models, so the declared
    fallback is never reached and removing it changes nothing they observe.
    """

    def test_an_unknown_task_type_falls_back_to_the_default_tool(self):
        assert tool_surface.tool_for_task("no-such-task") == tool_surface.DEFAULT_TASK_TOOL

    def test_a_known_task_type_does_not_use_the_fallback(self):
        """Pins that the fallback is a FALLBACK. A mutant returning the default
        unconditionally would satisfy the test above on its own."""
        known = next(iter(tool_surface.TASK_TOOL_MAP))
        assert tool_surface.tool_for_task(known) == tool_surface.TASK_TOOL_MAP[known]

    def test_an_unknown_tier_registers_everything_rather_than_raising(self):
        """`_TIERS.get(tier, None)` — None means "all tools", the permissive degrade.

        The docstring: "a typo degrades to permissive rather than to a wrong-name hint."
        """
        assert tool_surface.registered_tools("not-a-tier") is None

    def test_an_unknown_model_prices_at_zero_rather_than_raising(self):
        """`MODEL_COST_PER_1K.get(model, 0)` on the savings path.

        Removing the default raises TypeError inside a savings computation; the
        contract is that an unpriced model contributes zero, not an exception.
        """
        result = cost.calc_savings("a-model-nobody-has-priced", 1000)
        assert result is not None

    def test_an_unknown_task_type_has_no_floor_and_passes_through(self):
        """`_TASK_COMPLEXITY_FLOOR.get(key)` returns None for an unknown task type,
        and the complexity is returned unchanged rather than clamped to a guess."""
        from llm_router.classify import Complexity
        assert classify.apply_complexity_floor(
            Complexity.SIMPLE, "not-a-task-type"
        ) == Complexity.SIMPLE

    def test_a_known_task_type_clamps_up_to_its_floor(self):
        """Pins that the floor is a FLOOR: `code` has one, and SIMPLE is raised to it."""
        from llm_router.classify import Complexity
        out = classify.apply_complexity_floor(Complexity.SIMPLE, "code")
        assert out != Complexity.SIMPLE


class TestConfidenceThresholdBoundary:
    """`confident = best_score >= _CONFIDENCE_THRESHOLD` — the boundary is the
    threshold exactly. `classify_signals` takes a PROMPT and derives the score itself,
    so the boundary is reached through real input rather than an injected score map.

    Mutated to `>`, a prompt scoring exactly at the threshold is reported as NOT
    confident and escalates when the heuristic was meant to be trusted.
    """

    def test_a_strongly_signalled_prompt_is_confident(self):
        s = classify.classify_signals("write a python function to sort a list")
        assert s.confident is True
        assert s.score >= classify._CONFIDENCE_THRESHOLD

    def test_a_signal_free_prompt_is_not_confident(self):
        s = classify.classify_signals("hm")
        assert s.confident is False
        assert s.score < classify._CONFIDENCE_THRESHOLD

    def test_confident_is_exactly_score_at_or_above_threshold(self):
        """The boundary itself, asserted as a PROPERTY over many prompts.

        A single prompt cannot be pinned to the exact threshold without depending on
        the scoring weights, which would make this a change-detector. Asserting the
        relationship holds for every prompt kills `>=` -> `>` the moment any prompt
        lands exactly on the threshold, without hardcoding which one does.
        """
        for prompt in ("write a python function", "hm", "analyze this data carefully",
                       "x", "research the latest papers on transformers", ""):
            s = classify.classify_signals(prompt)
            assert s.confident is (s.score >= classify._CONFIDENCE_THRESHOLD), prompt

    def test_an_empty_prompt_does_not_raise(self):
        assert classify.classify_signals("").confident in (True, False)
