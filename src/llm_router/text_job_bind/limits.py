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
