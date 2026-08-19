"""`llm_router-hook-version` must be bumped whenever the hook's behaviour changes.

WHY THIS EXISTS — a defect the audit introduced and then found

`auto-route.py` carries a hand-maintained integer stamp, and its own self-update check
warns Cursor/Windsurf/Codex users when their installed copy has a LOWER number than the
bundled one. Those hosts never start the MCP server, so `check_and_update_hooks()` never
fires and that warning is their only staleness signal.

On 2026-08-16 the hook's behaviour was changed twice — the directive is now emitted BEFORE
the accounting write, and `CHZ-FO-HOOK-SLOW` self-timing was added — and the stamp stayed
at 32. A Cursor user holding v32 therefore had materially different code, an installed
version equal to the bundled one, and **no warning at all**. They would have kept running
the ordering that discards a fully-computed routing directive whenever an accounting write
is slow.

The mechanism was never broken. It depends on a human remembering, and this is what that
looks like when the human forgets.

THE INVARIANT, ENFORCED RATHER THAN REMEMBERED

A committed content hash. If the hook's bytes change, this test fails until BOTH the stamp
is bumped and the hash re-recorded. That is the same shape as `verify_criteria_hashes` and
the CHZ-SR-01 state-root ratchet, and it fails the way those do: in CI, on the change
itself, rather than silently for a user weeks later.

Deriving the version FROM the hash was considered and rejected: the stamp is compared with
`>` to decide "is the installed copy older", which requires an ordered value. A hash is not
ordered.

WHEN THIS TEST FAILS, the fix is two lines and one command — bump both stamps in
auto-route.py, then re-record with:

    python -c "import hashlib,pathlib; \\
      p=pathlib.Path('src/llm_router/hooks/auto-route.py'); \\
      print(hashlib.sha256(p.read_bytes()).hexdigest())"

Do NOT re-record the hash without bumping the version. That converts an enforced invariant
back into a remembered one, and this docstring is the evidence that the remembered version
does not hold.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_HOOK = _ROOT / "src" / "llm_router" / "hooks" / "auto-route.py"
_RECORD = _ROOT / ".llm-router" / "zero-tolerance-audit" / "hook_version_baseline.json"


def _stamps(text: str) -> list[int]:
    return [int(m) for m in re.findall(r"llm_router-hook-version:\s*(\d+)", text)]


def test_the_version_was_bumped_with_the_content():
    """The load-bearing check. Fails on ANY byte change until the stamp moves."""
    text = _HOOK.read_text()
    digest = hashlib.sha256(_HOOK.read_bytes()).hexdigest()
    rec = json.loads(_RECORD.read_text())

    stamps = _stamps(text)
    assert stamps, "the hook has no llm_router-hook-version stamp at all"
    current = stamps[0]

    if digest == rec["sha256"]:
        assert current == rec["version"], (
            f"content is unchanged but the stamp moved {rec['version']} -> {current}; "
            "re-record the baseline if that was deliberate"
        )
        return

    assert current > rec["version"], (
        f"auto-route.py changed but llm_router-hook-version is still {current}.\n"
        f"Cursor/Windsurf/Codex never start the MCP server, so the version comparison is "
        f"the ONLY staleness signal those users get — an unchanged stamp means they keep "
        f"running the old hook with no warning.\n"
        f"Bump the stamp above {rec['version']}, then re-record the hash in "
        f"{_RECORD.relative_to(_ROOT)}."
    )
    raise AssertionError(
        f"stamp correctly bumped to {current}, but the recorded hash is stale.\n"
        f"Re-record: sha256 = {digest}"
    )


def test_both_stamp_sites_agree():
    """The version appears twice: the file header, and `_THIS_VERSION_LINE`, which is what
    the self-update check actually compares. Bumping only the header would leave the check
    announcing a version the file does not claim."""
    stamps = _stamps(_HOOK.read_text())
    assert len(stamps) >= 2, f"expected header + _THIS_VERSION_LINE, found {len(stamps)}"
    assert len(set(stamps)) == 1, (
        f"the stamp sites disagree: {stamps}. The self-update check reads "
        "_THIS_VERSION_LINE, so a mismatch makes the warning wrong in whichever "
        "direction the lower one sits."
    )


def test_the_baseline_is_not_vacuous():
    """A recorded hash that no longer matches any real file would make the first test
    pass unconditionally on the 'content changed' branch."""
    rec = json.loads(_RECORD.read_text())
    assert len(rec["sha256"]) == 64 and rec["version"] > 0
    assert _HOOK.exists(), "the hook this baseline describes does not exist"
