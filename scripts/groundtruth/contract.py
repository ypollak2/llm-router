"""The acceptance contract: what must be true for a task to count as done.

Written before any verifier, because a verifier without a stated contract is
just an assertion somebody liked. Three tiers, and the separation is the whole
point:

    required     failing any one of these is a FAIL
    invariants   things the change must not break
    optional     useful signals that must NEVER decide PASS on their own

The optional tier exists to be excluded. Left unnamed, "the implementation is
concise" and "logging improved" quietly leak into the pass condition, and the
dataset starts measuring taste. `Contract.pass_conditions()` returns required +
invariants and nothing else; there is no flag to include optional.

Templates here are shapes, not shortcuts. A template supplies the invariants
that are true of every task of its kind — existing tests still pass, the build
still works — and leaves `required` to be filled per task. A template that
produced a complete contract would be "tests pass = success", which is exactly
the thing that cannot be allowed to stand in for acceptance criteria.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

SCHEMA_VERSION = 1

REQUIRED = "required"
INVARIANT = "invariant"
OPTIONAL = "optional"


@dataclass
class Condition:
    """One checkable statement, with how it would be checked."""

    text: str
    tier: str                       # REQUIRED | INVARIANT | OPTIONAL
    check: str | None = None        # verifier snippet or command, when known
    source: str = "derived"         # derived | template | human
    note: str = ""

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class Contract:
    task_id: str
    task: str
    schema_version: int = SCHEMA_VERSION
    conditions: list[Condition] = field(default_factory=list)
    template: str | None = None
    unclear: list[str] = field(default_factory=list)   # what a human must settle

    def of_tier(self, tier: str) -> list[Condition]:
        return [c for c in self.conditions if c.tier == tier]

    @property
    def required(self) -> list[Condition]:
        return self.of_tier(REQUIRED)

    @property
    def invariants(self) -> list[Condition]:
        return self.of_tier(INVARIANT)

    @property
    def optional(self) -> list[Condition]:
        return self.of_tier(OPTIONAL)

    def pass_conditions(self) -> list[Condition]:
        """What PASS actually depends on. Optional conditions are not here, and
        there is deliberately no parameter to include them."""
        return self.required + self.invariants

    @property
    def is_actionable(self) -> bool:
        """A contract with no required condition cannot decide anything."""
        return bool(self.required) and not self.unclear

    def to_json(self) -> dict:
        return {
            "task_id": self.task_id,
            "task": self.task,
            "schema_version": self.schema_version,
            "template": self.template,
            "unclear": list(self.unclear),
            "required": [c.to_json() for c in self.required],
            "invariants": [c.to_json() for c in self.invariants],
            "optional": [c.to_json() for c in self.optional],
        }


# ── Templates ────────────────────────────────────────────────────────────────
# Each supplies invariants and the shape of the required tier. None of them
# supplies a complete contract: `required` always needs the task's own
# acceptance criteria, which is why every template records `needs_required`.

@dataclass
class Template:
    name: str
    matches: re.Pattern[str]
    invariants: tuple[str, ...]
    required_hint: str
    strategy: str                 # the verifier class this shape implies
    needs_required: bool = True


TEMPLATES: tuple[Template, ...] = (
    Template(
        name="bug-fix-with-existing-tests",
        matches=re.compile(r"\b(fix|correct|repair|resolve)\b.{0,60}\b(bug|error|crash"
                           r"|failure|regression|issue|off-by-one|incorrect)\b", re.I),
        invariants=("the existing test suite for the touched module still passes",
                    "no test file is deleted, skipped or weakened",
                    "behaviour outside the reported fault is unchanged"),
        required_hint="the reported failing input now produces the correct result",
        strategy="task_specific_test",
    ),
    Template(
        name="behaviour-change",
        matches=re.compile(r"\b(make|change|update|convert|migrate|rename|extend"
                           r"|support|handle)\b.{0,80}\b(case-insensitive|so that"
                           r"|to accept|to return|to use|behaviour|behavior)\b", re.I),
        invariants=("previously-specified behaviour not mentioned by the task is unchanged",
                    "existing tests for the touched module still pass"),
        required_hint="the newly requested behaviour is observable",
        strategy="task_specific_test",
    ),
    Template(
        name="add-capability",
        matches=re.compile(r"\b(add|implement|introduce)\b.{0,80}"
                           r"\b(retry|timeout|cache|flag|option|endpoint|field"
                           r"|parameter|validation|handler|backoff)\b", re.I),
        invariants=("the new capability is off or neutral when not requested",
                    "existing callers keep working unchanged"),
        required_hint="the new capability does what the task described",
        strategy="task_specific_test",
    ),
    Template(
        name="structured-output",
        matches=re.compile(r"\b(return|produce|output|emit|respond with)\b.{0,40}"
                           r"\b(json|yaml|csv|schema|object|array|table)\b", re.I),
        invariants=("the output parses",),
        required_hint="every required key is present with the right type and constraint",
        strategy="schema_validation",
    ),
    Template(
        name="config-change",
        matches=re.compile(r"\b(set|change|update|configure)\b.{0,50}"
                           r"\b(config|setting|option|default|env|environment)\b", re.I),
        invariants=("unrelated configuration keys are untouched",
                    "the application still starts"),
        required_hint="the named setting has the requested value and takes effect",
        strategy="file_state_assertion",
    ),
    Template(
        name="test-writing",
        matches=re.compile(r"\b(write|add|create)\b.{0,40}\b(tests?|test case|coverage)\b",
                           re.I),
        invariants=("the new test fails against the un-fixed code",
                    "the new test passes against the correct code"),
        required_hint="a test exists that actually exercises the named behaviour",
        strategy="mutation_validated_test",
    ),
    Template(
        name="dependency-upgrade",
        matches=re.compile(r"\b(upgrade|bump|update|pin)\b.{0,40}"
                           r"\b(dependency|dependencies|package|version|lockfile)\b", re.I),
        invariants=("the project still builds", "the existing test suite still passes"),
        required_hint="the named dependency is at the requested version",
        strategy="build_check",
    ),
)

# Phrases that describe taste rather than outcome. Anything matching lands in
# `optional` and is thereby excluded from PASS.
_OPTIONAL_SIGNAL = re.compile(
    r"\b(concise|clean|readable|idiomatic|elegant|nice|tidy|well[- ]structured"
    r"|maintainable|simple|pretty|style|naming|comment|docstring|log(ging)?"
    r"|performance|faster|efficient)\b",
    re.I,
)

# Words that make an acceptance criterion undecidable. Their presence is not a
# failure — it is a question for a human, recorded in `unclear`.
_VAGUE = re.compile(
    r"\b(better|improve\w*|appropriate|reasonable|sensible|properly|correctly"
    r"|as needed|if necessary|etc\.?|and so on|make it work|handle it)\b",
    re.I,
)


def pick_template(task: str) -> Template | None:
    for t in TEMPLATES:
        if t.matches.search(task):
            return t
    return None


def derive(task_id: str, task: str, *, existing_tests: list[str] | None = None,
           test_command: str | None = None) -> Contract:
    """Build a contract from the task text and what the envelope captured.

    This is a *proposal*. It names the conditions a verifier would have to
    check and flags what it could not pin down; it never asserts that the
    conditions are complete. `unclear` is the honest output when the task does
    not say what success means, and it blocks the contract from being
    actionable rather than guessing.
    """
    s = " ".join((task or "").split())
    tmpl = pick_template(s)
    c = Contract(task_id=task_id, task=s, template=tmpl.name if tmpl else None)

    if tmpl:
        c.conditions.append(Condition(tmpl.required_hint, REQUIRED, source="template",
                                      note="instantiate with the task's own criteria"))
        for inv in tmpl.invariants:
            c.conditions.append(Condition(inv, INVARIANT, source="template"))
    else:
        c.unclear.append("no template matched; the task shape is unrecognised")

    # Anything the task itself states as a hard constraint becomes required.
    for clause in _split_clauses(s):
        if _OPTIONAL_SIGNAL.search(clause):
            c.conditions.append(Condition(clause, OPTIONAL, source="derived",
                                          note="taste, not outcome — excluded from PASS"))
        elif re.search(r"\b(must|has to|should not|must not|never|always|keep\w*"
                       r"|stay\w*|remain\w*|unchanged|preserve\w*)\b", clause, re.I):
            tier = INVARIANT if re.search(
                r"\b(not|never|unchanged|preserve\w*|keep\w*|stay\w*|remain\w*)\b",
                clause, re.I) else REQUIRED
            c.conditions.append(Condition(clause, tier, source="derived"))

    if _VAGUE.search(s):
        hit = _VAGUE.search(s)
        c.unclear.append(f"acceptance criteria are vague: {hit.group(0)!r}")

    if existing_tests:
        c.conditions.append(Condition(
            f"existing tests still pass: {', '.join(existing_tests[:3])}",
            INVARIANT, check=test_command, source="derived"))

    if not c.required:
        c.unclear.append("no required condition could be derived from the task text")
    return c


def _split_clauses(text: str) -> list[str]:
    parts = re.split(r"(?:[.;]|\band\b(?=\s+the\b)|,\s*(?=(?:but|keeping|leaving|while)\b))",
                     text)
    return [p.strip() for p in parts if len(p.strip().split()) >= 3]
