#!/usr/bin/env python3
"""Group seed tasks by the verification strategy each one actually admits.

    python3 scripts/groundtruth/classify.py --dataset data/groundtruth/seed-v1

The question this answers is not "what is this task about" but "what evidence
could establish that a model did it". Those are different questions, and the
second one is the only one that decides whether a Ground Truth label is
possible.

Category order below is the order they are tested in, most-disqualifying
first. A task that references a dead session cannot be graded no matter how
crisply it is worded, so that check runs before any look at content.

Categories map onto the repo's existing verification vocabulary
(`dataset.VERIFICATION_PREFERENCE`) rather than introducing a parallel scheme:
each category names the strongest verifier type it could reach, or says that
it reaches none.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from groundtruth import dataset as ds  # noqa: E402

# ── Categories ───────────────────────────────────────────────────────────────
NON_PROMPT = "non-prompt-artifact"
TEMPLATE_AUTOMATION = "template-automation"
SESSION_STATE = "unverifiable-session-state"
EXTERNAL_STATE = "unverifiable-external-state"
MACHINE_STATE = "unverifiable-machine-state"
SUBJECTIVE = "subjective-generative"
CODE_EDIT = "code-edit-verifiable"
FACTUAL = "factual-reference"
STRUCTURED = "structured-output"

# What each category could reach at best. None = no verifier exists for it.
BEST_VERIFIER: dict[str, str | None] = {
    NON_PROMPT: None,
    TEMPLATE_AUTOMATION: None,
    SESSION_STATE: None,
    EXTERNAL_STATE: None,
    MACHINE_STATE: None,
    SUBJECTIVE: ds.V_JUDGE,          # only an opinion is available
    CODE_EDIT: ds.V_SANDBOX,         # needs a fixture tree + assertion
    FACTUAL: ds.V_MECHANICAL,
    STRUCTURED: ds.V_PROGRAMMATIC,
}

# ── Detectors ────────────────────────────────────────────────────────────────

# Harness artefacts that are not user instructions at all. Several announce it
# themselves ("not an instruction to follow") and were still admitted by the
# corpus filters, which keyed on shape rather than on this marker.
_NON_PROMPT = re.compile(
    r"^\s*(\[Background context from this session"
    r"|<bash-std(out|err)"
    r"|\[Image: source:"
    r"|Base directory for this skill:"
    r"|npm notice|npm error"
    r"|[a-z]+@[A-Za-z-]+ npm %"
    r"|macos-13 \(best effort\))",
    re.I,
)

# References to state that existed only inside a finished conversation. "commit
# this" cannot be graded because "this" no longer exists anywhere.
# Deliberately anchored to IMPERATIVE use. An earlier version matched these as
# bare words anywhere, so "Please inspect the ... implementation" was excluded
# because the body happened to contain the word "tag". The reason string is
# shown to a human deciding whether to author a verifier, so a plausible-but-
# wrong reason is worse than no reason.
_SESSION_STATE = re.compile(
    r"(?:^|\band\b|\bthen\b|,|\bnow\b|\bplease\b)\s*"
    r"(commit|push|merge|tag|cut|ship|publish|release|rebase|branch|pull)\b"
    r"|\b(commit|merge|push|tag)\s+(it|this|these|them|the\s+\w+|\d+|#\d+|v?\d)"
    r"|\bkeep going\b|\bonce (the )?(ci|tests?|suite|eval)\b"
    r"|\bPR\s*#?\d+\b|\b#\d{2,4}\b"
    r"|\bv\d+\.\d+\.\d+\b",
    re.I,
)

# Needs a file, repo, URL or attachment that is not part of this dataset.
# `.md`/`.txt` only mean "external" when they arrive as an attachment or a
# path outside the tree. `requirements.txt` is repo state, and classifying it
# as external made every dependency task unverifiable.
_EXTERNAL_STATE = re.compile(
    r"@\"|https?://|\.pdf\b"
    r"|(?:~/|<HOME:|/Users/|/Downloads/|/Desktop/)[\w./-]*\.(?:md|txt|html)\b"
    r"|~/|<HOME:|/Downloads/|/Desktop/|/Calls/|/Projects/"
    r"|\[Image\s*#\d+\]"
    r"|\b(this (codebase|repo|project)|the repo|the codebase)\b",
    re.I,
)

# Answers depend on this machine right now (installed models, running daemons).
_MACHINE_STATE = re.compile(
    r"\b(ollama|installed|running now|operate now|loaded|unloading"
    r"|statusline|quota)\b",
    re.I,
)

# Open-ended production: plans, audits, research, opinions, summaries.
_SUBJECTIVE = re.compile(
    r"\b(plan|planning|audit|research|propose|proposal|describe|explain"
    r"|summar\w+|recommend|suggest|improve\w*|vision|thoughts?|opinion"
    r"|understand\w*|plan how|what (should|would|kind of)|plans?)\b",
    re.I,
)

# A crisp, self-contained code change with a nameable success condition.
#
# Two ways to qualify, because the noun list alone was too strict: "Add a retry
# with exponential backoff to src/client.py for 5xx responses" names no listed
# noun and fell through to `subjective`, which would have rejected a perfectly
# good future candidate. Naming a source file is the stronger signal anyway —
# it says where the change lands, which is what makes it checkable.
_CHANGE_VERB = (r"\b(make|change|fix|add|implement|rename|remove|correct|update"
                r"|refactor|replace|extend|handle|support|convert|migrate"
                r"|set|bump|upgrade|pin|enable|disable|write|create)\b")

_CODE_EDIT_NOUN = re.compile(
    _CHANGE_VERB +
    r".{0,80}\b(function|method|class|filter|flag|field|parameter|default"
    r"|case-insensitive|behaviour|behavior|endpoint|column|slide|retry|timeout"
    r"|schema|validation|handler|config|option)\b",
    re.I,
)

# A change verb plus a named source file: the change has an address.
_CODE_EDIT_FILE = re.compile(
    _CHANGE_VERB +
    # Config and dependency files are edit targets too: `requirements.txt` and
    # lockfiles are where a dependency task lands, and omitting them sent
    # every dependency-upgrade task to `subjective`.
    r".{0,120}\b[\w./-]+\.(py|ts|tsx|js|jsx|go|rs|java|rb|sh|sql|ya?ml|toml|json"
    r"|txt|lock|cfg|ini|env|properties)\b",
    re.I,
)


def _is_code_edit(s: str) -> bool:
    return bool(_CODE_EDIT_NOUN.search(s) or _CODE_EDIT_FILE.search(s))

# Structured output: the answer's SHAPE is the acceptance criterion, which is
# programmatically checkable regardless of the prose around it. This category
# carried a verifier mapping from the start and nothing ever returned it.
_STRUCTURED = re.compile(
    r"\b(return|produce|output|emit|respond with|give me|reply with)\b"
    r".{0,60}\b(json|yaml|csv|schema|object|array|list of|table|dictionary)\b"
    r"|\bas (a )?(json|yaml|csv)\b"
    r"|\bvalid json\b",
    re.I,
)

_QUESTION = re.compile(r"\?\s*$|^\s*(what|which|who|when|where|how many|how much)\b", re.I)


@dataclass
class Classification:
    task_id: str
    category: str
    best_verifier: str | None
    reason: str
    prompt: str

    @property
    def mechanically_verifiable(self) -> bool:
        return self.best_verifier in (ds.V_MECHANICAL, ds.V_PROGRAMMATIC,
                                      ds.V_SANDBOX)

    @property
    def weakly_verifiable(self) -> bool:
        return self.best_verifier in (ds.V_JUDGE, ds.V_HUMAN)

    @property
    def unverifiable(self) -> bool:
        return self.best_verifier is None


def classify(task_id: str, prompt: str, *, duplicate_count: int = 1,
             session_id: str | None = None) -> Classification:
    s = " ".join((prompt or "").split())

    def out(cat: str, reason: str) -> Classification:
        return Classification(task_id, cat, BEST_VERIFIER[cat], reason, s)

    if _NON_PROMPT.match(s):
        return out(NON_PROMPT, "harness artefact, not a user instruction")

    # A prompt repeated verbatim across many DIFFERENT sessions is a template
    # fired by automation, not a task a person set. Two such prompts account
    # for 82 of the corpus's collapsed duplicates, both appearing at the same
    # line offset of 47 and 35 distinct fact-find-agent sessions.
    if duplicate_count >= 10:
        return out(TEMPLATE_AUTOMATION,
                   f"identical prompt in {duplicate_count} sessions — canned template")

    if _SESSION_STATE.search(s):
        m = _SESSION_STATE.search(s)
        return out(SESSION_STATE, f"refers to finished-session state: {m.group(0)!r}")

    if _EXTERNAL_STATE.search(s):
        m = _EXTERNAL_STATE.search(s)
        return out(EXTERNAL_STATE, f"needs an artefact not in the dataset: {m.group(0)!r}")

    if _MACHINE_STATE.search(s):
        m = _MACHINE_STATE.search(s)
        return out(MACHINE_STATE, f"answer depends on live machine state: {m.group(0)!r}")

    if _STRUCTURED.search(s):
        return out(STRUCTURED, "the answer's shape is the acceptance criterion")

    if _is_code_edit(s):
        # A concrete change wins over an incidental subjective word. "Return a
        # JSON object describing the package" is structured output that happens
        # to contain "describing"; vetoing on that word alone sent four of ten
        # realistic coding tasks to `subjective` and blocked authoring.
        return out(CODE_EDIT, "names a concrete change with a checkable condition")

    if _SUBJECTIVE.search(s):
        m = _SUBJECTIVE.search(s)
        return out(SUBJECTIVE, f"open-ended production: {m.group(0)!r}")

    if _QUESTION.search(s):
        return out(FACTUAL, "a question with a potentially checkable answer")

    return out(SUBJECTIVE, "no checkable success condition stated")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", type=Path, default=Path("data/groundtruth/seed-v1"))
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--show", default="", help="print prompts for this category")
    args = ap.parse_args()

    corpus = args.dataset / "corpus.jsonl"
    if not corpus.exists():
        raise SystemExit(f"no corpus at {corpus}")
    rows = [json.loads(ln) for ln in corpus.read_text(encoding="utf-8").splitlines()
            if ln.strip()]

    results = [classify(f"seed-{i:04d}", r["text"],
                        duplicate_count=r.get("duplicate_count", 1),
                        session_id=r.get("session_id"))
               for i, r in enumerate(rows, start=1)]

    counts = Counter(c.category for c in results)
    mech = [c for c in results if c.mechanically_verifiable]
    weak = [c for c in results if c.weakly_verifiable]
    none_ = [c for c in results if c.unverifiable]

    print(f"seed tasks                     {len(results)}\n")
    print(f"  {'category':32s} {'n':>4s}  best verifier")
    for cat, n in counts.most_common():
        print(f"  {cat:32s} {n:4d}  {BEST_VERIFIER[cat] or '— none —'}")
    print()
    print(f"could reach MECHANICAL verification   {len(mech):4d}")
    print(f"could reach WEAK verification only    {len(weak):4d}")
    print(f"NO verifier reachable                 {len(none_):4d}")
    print(f"                                      {'-'*4}")
    print(f"                                      {len(results):4d}")
    assert len(mech) + len(weak) + len(none_) == len(results), "categories must partition"

    if args.show:
        print(f"\n--- {args.show} ---")
        for c in results:
            if c.category == args.show:
                print(f"  {c.task_id}  {c.prompt[:120]}")
                print(f"           -> {c.reason}")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", encoding="utf-8") as fh:
            for c in results:
                fh.write(json.dumps({
                    "task_id": c.task_id, "category": c.category,
                    "best_verifier": c.best_verifier, "reason": c.reason,
                    "prompt": c.prompt,
                }, ensure_ascii=False) + "\n")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
