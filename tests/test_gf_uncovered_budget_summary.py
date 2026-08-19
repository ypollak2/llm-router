"""G-F — `budget.format_budget_summary` had NO test executing it.

The G-F baseline found 18 mutants in this function and every one was 🫥 no-coverage:
nothing in the suite called it. It renders the Budget Oracle block an operator reads to
decide whether spend is under control, so "it is only formatting" is exactly backwards —
a wrong bar or a wrong percentage is a wrong answer to that question, delivered
confidently.

WHAT IS ASSERTED
----------------
Behaviour of the returned string, from inputs chosen here:

  * the bar has a fixed width and its fill tracks pressure (0%, 50%, 100%)
  * the percentage matches the pressure
  * the three spend branches each render their own way — capped, local, uncapped
  * providers come out sorted, not in dict order

The expected strings are written out literally rather than rebuilt with the same
expression the implementation uses. Recomputing `"█" * round(p * 10)` in the test would
assert that the code equals itself, and would survive a mutant that changed both.
"""

from __future__ import annotations

from llm_router.budget import format_budget_summary
from llm_router.types import BudgetState


def _state(provider: str, pressure: float, *, spend: float = 0.0, cap: float = 0.0
           ) -> BudgetState:
    return BudgetState(provider=provider, pressure=pressure, spend_usd=spend, cap_usd=cap)


class TestTheBar:
    """`bar_len = 10`, `filled = round(pressure * bar_len)`, filled + empty = 10."""

    def test_zero_pressure_is_an_empty_bar(self):
        out = format_budget_summary({"openai": _state("openai", 0.0, cap=100.0)})
        assert "[░░░░░░░░░░]" in out

    def test_full_pressure_is_a_full_bar(self):
        out = format_budget_summary({"openai": _state("openai", 1.0, cap=100.0)})
        assert "[██████████]" in out

    def test_half_pressure_is_half_a_bar(self):
        out = format_budget_summary({"openai": _state("openai", 0.5, cap=100.0)})
        assert "[█████░░░░░]" in out

    def test_the_bar_is_always_ten_cells(self):
        """Kills the mutants that change `bar_len` on one side of the expression only.

        `"█" * filled + "░" * (bar_len - filled)` has two references to the width; a
        mutant altering either produces a bar that is no longer 10 wide, which no single
        fill-level assertion above would necessarily catch.
        """
        for pressure in (0.0, 0.14, 0.5, 0.86, 1.0):
            out = format_budget_summary({"p": _state("p", pressure, cap=10.0)})
            bar = out.split("[")[1].split("]")[0]
            assert len(bar) == 10, f"pressure {pressure} produced a {len(bar)}-cell bar"
            assert set(bar) <= {"█", "░"}


class TestThePercentage:
    def test_percentage_renders_the_pressure(self):
        out = format_budget_summary({"openai": _state("openai", 0.42, cap=100.0)})
        assert "42%" in out

    def test_zero_and_one_render_as_0_and_100(self):
        assert "0%" in format_budget_summary({"a": _state("a", 0.0, cap=1.0)})
        assert "100%" in format_budget_summary({"a": _state("a", 1.0, cap=1.0)})


class TestTheThreeSpendBranches:
    """Each branch must be reachable and distinguishable from the other two."""

    def test_a_capped_provider_shows_spend_over_cap(self):
        out = format_budget_summary({"openai": _state("openai", 0.5, spend=12.5, cap=50.0)})
        assert "$12.50 / $50.00" in out

    def test_a_local_provider_with_no_cap_is_free(self):
        """The field is compared EXACTLY, not by substring.

        `assert "free (local)" in out` was the first version and it let a mutant live:
        the mutation wraps the literal as `"XXfree (local)XX"`, and the original string
        is a substring of that, so the assertion held while the rendered output was
        wrong. A superstring satisfies `in`.

        The provider line ends with the spend field, so comparing the tail pins the
        whole field rather than any fragment of it.
        """
        out = format_budget_summary({"ollama": _state("ollama", 0.0)})
        line = next(ln for ln in out.splitlines() if "ollama" in ln)
        assert line.rstrip().endswith("free (local)")
        assert "$" not in line

    def test_an_uncapped_paid_provider_shows_spend_with_no_cap(self):
        out = format_budget_summary({"openai": _state("openai", 0.0, spend=0.0123)})
        assert "$0.0123 (no cap)" in out

    def test_the_cap_branch_wins_over_the_local_branch(self):
        """A local provider that HAS a cap must render as capped.

        The branch order is `cap_usd > 0` first, then the local check. A boundary
        mutant (`> 0` -> `>= 0`) or a reordering would send a capped local provider down
        the "free (local)" path and hide real spend.
        """
        out = format_budget_summary({"ollama": _state("ollama", 0.5, spend=3.0, cap=9.0)})
        assert "$3.00 / $9.00" in out
        assert "free (local)" not in out


class TestOrderingAndHeader:
    def test_providers_are_sorted_not_in_insertion_order(self):
        out = format_budget_summary({
            "zzz": _state("zzz", 0.0, cap=1.0),
            "aaa": _state("aaa", 0.0, cap=1.0),
            "mmm": _state("mmm", 0.0, cap=1.0),
        })
        assert out.index("aaa") < out.index("mmm") < out.index("zzz")

    def test_the_header_is_present_and_first(self):
        out = format_budget_summary({"openai": _state("openai", 0.0, cap=1.0)})
        assert out.splitlines()[0] == "**Budget Oracle**"

    def test_every_provider_gets_its_own_line(self):
        states = {p: _state(p, 0.0, cap=1.0) for p in ("a", "b", "c")}
        out = format_budget_summary(states)
        # header + blank (the header carries a trailing \n) + one line per provider.
        assert len([ln for ln in out.splitlines() if ln.strip().startswith(("a", "b", "c"))]) == 3

    def test_no_providers_yields_the_header_alone(self):
        out = format_budget_summary({})
        assert out.strip() == "**Budget Oracle**"
