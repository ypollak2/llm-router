"""D-372: when Codex cannot take a sub-agent task, could a LOCAL model? This module measures; it does not run one.

Owner decision D-372 (2026-11-04): "stay on Plus; if the quota on Codex ends fast, use more of the local models, they are
free". When agent-route cannot hand a sub-agent task to Codex (bench, spent window, spent daily budget) the task goes to
Claude. The decision asked for a local path in front of Claude for tasks that are read-only, small and suitable, behind a
flag, with a strict gate and a quality guard, and said: if the evidence says local is not good enough for any sub-agent class,
build only the seam and the logging.

The evidence says that (numbers: ``changelog.d/W11-codex-meter-reserve.changed.md``):

* the sub-agent tasks Codex could not take were 1,049 organic decisions in 30 days, 1,048 of them classified ``complex`` and none
  ``simple``; of the 843 whose prompt could be read, 842 are at least 500 characters long and 799 name a write or a run;
* measured on mechanically scored tasks (docs/BACKEND-QUALITY.md), the local model matches the cloud agents only on contained
  single-file work (25 tasks, 24/25 for local, Codex and Claude alike) and drops to 0/3 on the questions of the hard suite and
  1/2 on the brutal one, whose answers have to cross files; on the owner's real development questions local answers were
  acceptable 3-24% of the time against 55-100% for Sonnet (two blind graders; ``hooks/agent-route.py``,
  ``_try_direct_subagent``, owner decision 2026-10-02).

So this module holds the strict gate (the one class with parity evidence: a short, read-only question about exactly one
existing file) and records, for every sub-agent task Codex could not take, whether that task would have passed it and which rules
it failed. No model is started and nothing is bound. ``agent-route.py`` calls :func:`evaluate` at the one point where a local
runner would be put in front of Claude, and :func:`record` writes the verdict to ``agent_route_local.jsonl``. The rows are kept out
of ``north_star_units.jsonl`` on purpose: that ledger is the North Star signal, and a measurement that runs nothing is not a unit.
A runner is worth building when these rows show a share of tasks that passes the gate; until then there is nothing to run.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field

from llm_router import capped_log, failopen, paths

#: One row per sub-agent task Codex declined for capacity (``outcome``: ``local_candidate`` or ``local_ineligible``).
LOG_FILE = "agent_route_local.jsonl"
_LOG_MAX_BYTES = 512 * 1024

#: Codex outcomes (``agent_route_codex`` rows) that mean "Codex had no capacity", as opposed to "this task is not for Codex".
CAPACITY_OUTCOMES = frozenset({"benched", "codex_quota", "window_tight", "window_exhausted", "budget_exhausted"})

#: A prompt this long or longer is not "small". Real sub-agent prompts are review briefs of thousands of characters.
MAX_PROMPT_CHARS = 1200

#: Only the untyped agent: a typed one (task-reviewer, qa-testing, benchmark-designer) carries a system prompt and tools
#: that a local read-only loop does not reproduce.
LOCAL_SUBAGENT_TYPES = frozenset({"general-purpose"})
LOCAL_TASK_TYPES = frozenset({"query", "analyze"})

_WRITES_OR_RUNS = re.compile(
    r"\b(?:write|create|edit|modify|implement|fix|add|remove|delete|rename|refactor|commit|push|merge|deploy|install|run|"
    r"execute|apply|patch|update|migrate|build|tests?|pytest|worktree|branch|pull request|PR)\b", re.IGNORECASE)
_NEEDS_THE_SESSION = re.compile(
    r"\b(?:above|earlier|previous(?:ly)?|as discussed|this conversation|you just|we just|last time|the thread)\b", re.IGNORECASE)
_QUESTION_SHAPE = re.compile(
    r"^\W*(?:what|which|where|why|how|does|do|is|are|can|list|find|explain|summari[sz]e|show|identify|check|read|describe)\b",
    re.IGNORECASE)
_FILE_TOKEN = re.compile(
    r"(?<![\w/.-])(?:[\w.-]+/)*[\w.-]+\.(?:py|md|json|jsonl|toml|ya?ml|txt|sh|js|ts|tsx|cfg|ini|rs|go|java|c|h|cpp)\b")


@dataclass(frozen=True)
class Verdict:
    eligible: bool
    #: Every rule the task failed (empty when eligible). The first is the one a reader sees as "the reason".
    blockers: tuple[str, ...] = field(default_factory=tuple)
    prompt_chars: int = 0
    files: int = 0

    @property
    def reason_code(self) -> str:
        return self.blockers[0] if self.blockers else "eligible"


def _files_named(prompt: str, cwd: str | None) -> tuple[int, int]:
    """``(named, missing)``: distinct file paths the prompt names, and how many of them do not exist under ``cwd``.
    With no ``cwd`` nothing is checked for existence (an upper bound, used when a stored prompt has lost its directory)."""
    names = set(_FILE_TOKEN.findall(prompt))
    if cwd is None:
        return len(names), 0
    root = os.path.realpath(cwd)
    missing = 0
    for name in names:
        path = os.path.realpath(name if os.path.isabs(name) else os.path.join(root, name))
        inside = path == root or path.startswith(root + os.sep)
        if not (inside and os.path.isfile(path)):
            missing += 1
    return len(names), missing


def evaluate(prompt: str, subagent_type: str, task_type: str, complexity: str, cwd: str | None) -> Verdict:
    """Would this sub-agent task pass the strict local gate? Pure: reads only the file system names the prompt mentions.

    The gate is the class with parity evidence: short, read-only, answerable from exactly one existing file, in a session that
    is not needed for context. Every failed rule is reported, not just the first, so the ledger shows what blocks the most."""
    blockers: list[str] = []
    if subagent_type not in LOCAL_SUBAGENT_TYPES:
        blockers.append("typed_agent")
    if complexity != "simple":
        blockers.append("not_simple")
    if task_type not in LOCAL_TASK_TYPES:
        blockers.append("task_type")
    if len(prompt) >= MAX_PROMPT_CHARS:
        blockers.append("long_prompt")
    if _WRITES_OR_RUNS.search(prompt):
        blockers.append("writes_or_runs")
    if _NEEDS_THE_SESSION.search(prompt):
        blockers.append("needs_session")
    if not _QUESTION_SHAPE.search(prompt):
        blockers.append("not_a_question")
    named, missing = _files_named(prompt, cwd)
    if named == 0:
        blockers.append("no_file")
    elif named > 1:
        blockers.append("many_files")
    if missing:
        blockers.append("file_missing")
    return Verdict(not blockers, tuple(blockers), len(prompt), named)


def record(verdict: Verdict, *, declined_by: str, subagent_type: str, task_type: str, complexity: str, session_id: str,
           session_kind: str | None = None, tool_use_id: str = "") -> None:
    """Append the verdict for one declined task to ``agent_route_local.jsonl``. Rule names and counts only: never the prompt.
    Never raises; a lost row is counted."""
    row = {
        "ts": round(time.time(), 3), "outcome": "local_candidate" if verdict.eligible else "local_ineligible",
        "reason_code": verdict.reason_code, "blockers": list(verdict.blockers), "prompt_chars": verdict.prompt_chars,
        "files_named": verdict.files, "declined_by": declined_by, "subagent_type": subagent_type, "task_type": task_type,
        "complexity": complexity, "session_id": session_id, "session_kind": session_kind,
        **({"tool_use_id": tool_use_id} if tool_use_id else {}),
    }
    try:
        capped_log.append(paths.state_path(LOG_FILE), (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8"),
                          _LOG_MAX_BYTES)
    except Exception as exc:  # noqa: BLE001
        failopen.record("CHZ-FO-AGENT-ROUTE-LOCAL-LOG", exc)
