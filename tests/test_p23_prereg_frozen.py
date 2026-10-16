"""PREREG-P23 (P2.3 score weights and replay rule) is frozen: the committed file matches
its recorded sha256 and the sidecar carries a parseable frozen_utc (D-122)."""
from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PREREG = ROOT / "docs" / "prereg" / "PREREG-P23-quota-score.md"


def _check():
    sys.path.insert(0, str(ROOT / "scripts"))
    import check_prereg

    return check_prereg.check(PREREG)


def test_prereg_p23_matches_its_recorded_hash():
    res = _check()
    assert res["match"] is True
    assert res["sha256"] == hashlib.sha256(PREREG.read_bytes()).hexdigest() == res["recorded"]
    # one changed byte would not match
    assert hashlib.sha256(PREREG.read_bytes() + b" ").hexdigest() != res["recorded"]


def test_prereg_p23_frozen_utc_parses_and_is_not_in_the_future():
    frozen = _check()["frozen_utc"]
    ts = datetime.fromisoformat(frozen.replace("Z", "+00:00"))
    assert ts.tzinfo is not None
    assert datetime(2026, 10, 16, tzinfo=timezone.utc) <= ts <= datetime.now(timezone.utc)
