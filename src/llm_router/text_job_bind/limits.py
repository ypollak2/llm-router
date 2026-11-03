"""The time limits of the text-job-bind hook, in one place.

``HOOK_TIMEOUT_SEC`` is the ``timeout`` the installer writes for the hook (``install_hooks``
reads it from here), so the host never kills the hook before it has answered. A host kill is
silent: the tool call would go ahead with whatever the model wrote, which here is the
placeholder. The hook therefore keeps ``MARGIN_SEC`` in hand and gives the model call at most
``BUDGET_SEC``: it always has time to say no in words.
"""

from __future__ import annotations

HOOK_TIMEOUT_SEC = 130
MARGIN_SEC = 30
BUDGET_SEC = HOOK_TIMEOUT_SEC - MARGIN_SEC

#: After a model call runs out of its budget the hook stops waiting for the model: for this
#: long a placeholder command is refused at once, without a call, instead of costing the whole
#: budget again.
COOLDOWN_SEC = 300

#: Message files the hook wrote are removed once they are this old.
FILE_TTL_SEC = 6 * 3600

#: Jobs are serialised per machine (a lock file in the state directory): Ollama serves one request at a time,
#: and about nine in ten commits run in parallel sub-agents. A job that cannot get the lock in this many seconds is
#: refused at once ("busy, write it yourself") instead of queueing behind the others past its own budget. The wait
#: counts against the budget. The number is a first value pending a threshold audit, not a measured optimum.
LOCK_WAIT_SEC = 10.0

#: What a bound commit message holds besides the model's subject: ``files`` (default) a list of the changed files
#: with their added/deleted line counts, built from git, with no model text; ``none`` the subject only; ``model``
#: the model's own body. An independent read of real messages found a false statement in about one body in six,
#: so the model's body is opt-in. The environment variable overrides this constant.
COMMIT_BODY_MODE = "files"
BODY_MODE_ENV = "LLM_ROUTER_TEXT_JOB_BODY"
BODY_MODES = ("model", "files", "none")
#: Files listed in the ``files`` body; the rest are counted.
FILES_BODY_MAX = 40
