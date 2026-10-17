"""The one definition of the injected ``<knowledge_context>`` block pattern.

Lives apart from ``okf`` so the UserPromptSubmit hook can strip injected blocks
before persisting a turn without importing ``okf`` (asyncio, ~9 ms) on every prompt
(D-171 A3). ``okf`` re-exports it as ``_KNOWLEDGE_CTX_RE``; there is one pattern.
"""
from __future__ import annotations

import re

#: Never re-capture an injected knowledge block back into the store (feedback loop).
KNOWLEDGE_CTX_RE = re.compile(r"<knowledge_context>.*?</knowledge_context>", re.DOTALL | re.IGNORECASE)
