"""Split surviving prompts by whether they can stand alone as a task.

Why this exists
---------------
The corpus is agentic-session traffic. Measured on 2026-09-20 over the 379
unique llm-router transcript prompts: median length 46 characters, ~8 words,
and roughly four in five are continuations or carry an unresolved referent
("go with the plan", "push the branch", "measure it against the brutal suite
first"). You cannot send those to four models cold and score the answers —
there is nothing for an isolated model to be right or wrong about.

So every prompt gets a `standalone` verdict before it can become a task.

What this is NOT
----------------
This is a *pre-filter for human review*, not a label. The heuristic is
deliberately conservative in the direction that costs least: it would rather
send a usable prompt to review than admit an unusable one to the dataset.
`extract_corpus.py` records the verdict; a prompt only becomes a task when a
human authors its acceptance assertion in `tasks/`, and the manifest reports
both counts so the gap is visible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

STANDALONE = "standalone"
REPO_BOUND = "repo-bound"
DEICTIC = "deictic"
CONTINUATION = "continuation"
SESSION_BOUND = "session-bound"
TOO_THIN = "too-thin"

# Prompts whose referent is the *session* rather than a pronoun. An earlier
# version of this module missed these entirely: it looked for "it"/"that" and
# passed "what's left?", "did you push the npm?" and "what did attempt 9
# score?" as standalone. Reviewing the 67 it admitted on 2026-09-20, only two
# were gradable by a model with no session history — the rest asked about work
# this conversation had done. A model answering them cold cannot be right.
_SESSION_BOUND = re.compile(
    r"\b(did|do|have|has|are|were|was)\s+(you|we|i)\b"          # did you push…
    r"|\byou\s+(already|just|earlier|previously|forgot|missed)\b"
    r"|\bwhat(?:'s| is| are| has|s)?\s+(left|next|missing|the status|up with"
    r"|remain\w*|the current state)\b"
    r"|\b(anything|something|what|how much|how many)\s+(else\s+)?(is |do we |we )?"
    r"(have\s+)?(left|remain\w*|missing|to (do|cover|go))\b"
    r"|\b(next steps?|current state|the status|so far|until now|till now)\b"
    r"|\b(attempt|round|run|phase|step|slice|task|iteration)\s*#?\s*\d+\b"
    r"|\b(the|our|your)\s+(audit|report|plan|presentation|demo|results?|findings?)\b"
    r"|\bwe (at|have|got|did|need)\b"
    r"|\b(proceed|continue|resume|go on)\b",
    re.I,
)

# Opens by continuing a prior turn rather than stating anything.
_CONTINUATION = re.compile(
    r"^\s*(ok(ay)?|yes|yep|no|nope|sure|go|going|continue|cont|next|proceed|do it"
    r"|carry on|keep going|now|then|also|and|but|so|great|perfect|cool|nice|good"
    r"|thanks|thank you|stop|wait|hold on|again|more|another|let'?s|lets)\b"
    r"[\s,.:;!?-]*",
    re.I,
)

# Unresolved referents. "the plan", "it", "that" point at conversation state.
_DEICTIC = re.compile(
    r"(?<![\w-])(it|its|it'?s|that|this|these|those|them|they|there|the plan"
    r"|the branch|the pr|the rest|the same|the above|the below|the issue"
    r"|the file|the test|the run|the error|the last|the first|the other"
    r"|same|above|below|previous|earlier|as discussed)(?![\w-])",
    re.I,
)

# Names a concrete local artifact: the task cannot be graded without the repo.
_REPO_BOUND = re.compile(
    r"(/[\w.@-]+){2,}"                       # a path with two or more segments
    r"|(?<![\w/])[\w-]+\.(py|ts|tsx|js|jsx|md|json|ya?ml|toml|sh|sql|rs|go)\b"
    r"|\b(src|tests?|scripts?|docs?)/[\w./-]+"
    r"|\bPR\s*#\d+|\bissue\s*#\d+|#\d{1,5}\b"
    r"|\b(commit|push|merge|rebase|branch|checkout|git)\b"
    r"|\b(this repo|the repo|this project|this codebase|the codebase)\b",
    re.I,
)

# Self-contained work usually names its own object and its own success shape.
_IMPERATIVE_WITH_OBJECT = re.compile(
    r"\b(write|create|build|implement|generate|summar\w+|translate|classify"
    r"|explain|compare|analy[sz]e|calculate|compute|convert|extract|list"
    r"|rewrite|draft|design|recommend|evaluate|rank|score|solve|prove)\b",
    re.I,
)


@dataclass(frozen=True)
class Verdict:
    kind: str
    reasons: tuple[str, ...]

    @property
    def usable(self) -> bool:
        """Could this become a task without inventing missing context?"""
        return self.kind in (STANDALONE, REPO_BOUND)


def triage(text: str) -> Verdict:
    s = " ".join((text or "").split())
    words = s.split()
    reasons: list[str] = []

    if len(words) < 5:
        return Verdict(TOO_THIN, ("under-5-words",))

    head = s[:40]
    is_continuation = bool(_CONTINUATION.match(s))
    # A continuation opener is only disqualifying while the rest stays thin;
    # "OK, now create a research plan for all the issues" states a real task.
    if is_continuation:
        remainder = _CONTINUATION.sub("", s, count=1)
        if len(remainder.split()) < 8 or not _IMPERATIVE_WITH_OBJECT.search(remainder):
            return Verdict(CONTINUATION, (f"continuation-opener:{head!r}",))
        reasons.append("continuation-opener-but-states-a-task")

    repo_bound = bool(_REPO_BOUND.search(s))
    deictic = bool(_DEICTIC.search(s))

    # Checked before repo-bound: "did you update the readme?" names a file but
    # is really a question about this session's history, not a task.
    if _SESSION_BOUND.search(s):
        hit = _SESSION_BOUND.search(s)
        return Verdict(SESSION_BOUND,
                       tuple(reasons + [f"session-referent:{hit.group(0).lower()!r}"]))

    if repo_bound:
        reasons.append("names-local-artifact")
        # Repo-bound work is gradable, but only inside a sandbox holding that
        # repo — the bench_* harnesses already work this way.
        return Verdict(REPO_BOUND, tuple(reasons))

    if deictic:
        hits = sorted({m.group(0).lower() for m in _DEICTIC.finditer(s)})
        return Verdict(DEICTIC, tuple(reasons + [f"unresolved-referent:{','.join(hits[:4])}"]))

    if not _IMPERATIVE_WITH_OBJECT.search(s) and "?" not in s:
        return Verdict(TOO_THIN, tuple(reasons + ["no-imperative-and-no-question"]))

    return Verdict(STANDALONE, tuple(reasons + ["no-unresolved-referent"]))


def summarise(verdicts: list[Verdict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in verdicts:
        out[v.kind] = out.get(v.kind, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
