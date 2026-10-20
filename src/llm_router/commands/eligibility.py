"""`llm-router eligibility record` -- write a pre-registered text-job verdict (F124).

Reads a frozen prereg directory (``MANIFEST-*.json``, ``FROZEN.txt``, ``run/USED.json``,
``run/result.json``), refuses unless the provenance holds, and records one lane per qualified
job under ``{model}.text_job.{job}`` in ``local_eligibility.json``. Dry run unless ``--apply``.
"Qualified" grants no routing by itself; the authority switch is separate and owner-gated.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

from llm_router.eligibility import (
    _Z95, TEXT_JOB_BAR_LB, TEXT_JOBS, _wilson, grade_text_job,
)

_HELP = """usage: llm-router eligibility record --prereg DIR [--apply] [--file PATH]

  record        record the verdict of a pre-registered text-job qualification
  --prereg DIR  the frozen prereg directory (MANIFEST-*.json, FROZEN.txt, run/result.json,
                run/USED.json)
  --apply       write the lanes; without it the command prints what it would write
  --file PATH   eligibility file to write (default: the state dir's local_eligibility.json)

Refuses when the MANIFEST sha256 differs from FROZEN.txt or run/USED.json, when the verdict
is not PASS, when the actions name no text job, or when the stored counts do not meet the
D-241 bar (point >= 0.80 and Wilson 95% lower bound >= 0.60). Qualified grants no routing.
"""


class Refused(Exception):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused(f"cannot read {path}: {type(exc).__name__}") from None


def _frozen(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise Refused(f"cannot read {path}: {type(exc).__name__}") from None
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def build_lanes(prereg: Path, *, today: str | None = None) -> tuple[str, dict[str, dict]]:
    """``(model, {job: lane})`` for a prereg directory, or raise ``Refused``."""
    manifests = sorted(prereg.glob("MANIFEST-*.json"))
    if len(manifests) != 1:
        raise Refused(f"need exactly one MANIFEST-*.json in {prereg}, found {len(manifests)}")
    manifest_path = manifests[0]
    digest = _sha256(manifest_path)
    frozen = _frozen(prereg / "FROZEN.txt").get("manifest_sha256")
    used = _json(prereg / "run" / "USED.json")
    used = used.get("manifest_sha256") if isinstance(used, dict) else None
    if digest != frozen or digest != used:
        raise Refused("MANIFEST sha256 does not match FROZEN.txt and run/USED.json "
                      f"(manifest {digest}, FROZEN {frozen}, USED {used})")
    manifest, result = _json(manifest_path), _json(prereg / "run" / "result.json")
    if not isinstance(manifest, dict) or not isinstance(result, dict):
        raise Refused("manifest and result must be JSON objects")
    rules = manifest.get("rules")
    rules = rules if isinstance(rules, dict) else {}
    prereg_id = rules.get("label")
    if not isinstance(prereg_id, str) or not prereg_id:
        raise Refused("manifest rules.label (prereg id) missing")
    if result.get("verdict") != "PASS":
        raise Refused(f"verdict is {result.get('verdict')!r}, not PASS")
    if rules.get("bar_point") != 0.8 or rules.get("bar_lb") != TEXT_JOB_BAR_LB:
        raise Refused("manifest bar is not the D-241 bar (point 0.80, Wilson LB 0.60)")
    actions = [a for a in result.get("local_eligibility_actions") or []
               if isinstance(a, dict) and a.get("task_type") == "text_job"]
    if len(actions) != 1:
        raise Refused(f"need exactly one text_job action, found {len(actions)}")
    action = actions[0]
    model, jobs = action.get("model"), action.get("jobs")
    if action.get("verdict") != "eligible" or not isinstance(model, str) or not model:
        raise Refused("the text_job action is not an eligible verdict for a named model")
    if model != manifest.get("local_model"):
        raise Refused("action model differs from the manifest local_model")
    if not isinstance(jobs, list) or not jobs or any(j not in TEXT_JOBS for j in jobs):
        raise Refused(f"the action lists no valid text jobs (valid: {', '.join(TEXT_JOBS)})")
    arms = result.get("arms")
    qwen = arms.get("qwen") if isinstance(arms, dict) else None
    qwen = qwen if isinstance(qwen, dict) else {}
    overall = qwen.get("overall")
    k, n = (overall.get("k"), overall.get("n")) if isinstance(overall, dict) else (None, None)
    base = {"k": k, "n": n}
    if grade_text_job(base) != "passed":
        raise Refused(f"pooled {k}/{n} does not meet the D-241 bar")
    guard = rules.get("guard_job_min")
    date = today or time.strftime("%Y-%m-%d", time.gmtime())
    lanes: dict[str, dict] = {}
    for job in jobs:
        per = qwen.get(f"job={job}")
        jk, jn = (per.get("k"), per.get("n")) if isinstance(per, dict) else (None, None)
        if grade_text_job({"k": jk, "n": jn}) == "unknown":
            raise Refused(f"no counts for job {job}")
        if isinstance(guard, (int, float)) and jk < guard * jn:
            raise Refused(f"job {job} {jk}/{jn} is under the per-job guard {guard}")
        lb, _ = _wilson(k, n, _Z95)
        lanes[job] = {
            "grade": "passed", "verdict": "eligible", "eligible": True, "source": "prereg",
            "prereg": prereg_id, "manifest_sha256": digest, "k": k, "n": n,
            "point": k / n, "wilson_lb": lb,
            "bar": {"decision": "D-241", "point": 0.8, "wilson_lb": TEXT_JOB_BAR_LB},
            "job_k": jk, "job_n": jn, "recorded_at": date,
        }
    return model, lanes


def cmd_eligibility(args: list[str]) -> int:
    if "-h" in args or "--help" in args:
        print(_HELP)
        return 0
    if not args or args[0] != "record":
        print(_HELP)
        return 2
    prereg = file = None
    apply = False
    rest, i = args[1:], 0
    while i < len(rest):
        if rest[i] == "--apply":
            apply = True
        elif rest[i] in ("--prereg", "--file") and i + 1 < len(rest):
            i += 1
            if rest[i - 1] == "--prereg":
                prereg = Path(rest[i])
            else:
                file = Path(rest[i])
        else:
            print(_HELP)
            return 2
        i += 1
    if prereg is None:
        print(_HELP)
        return 2
    try:
        model, lanes = build_lanes(prereg)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    from llm_router.resolver import eligibility_store as store

    target = file or store.eligibility_path()
    print(f"{'apply' if apply else 'dry run'}: {model} text_job -> {target}")
    print(json.dumps({model: {"text_job": lanes}}, indent=2, sort_keys=True))
    if not apply:
        print("dry run: nothing written; add --apply to write")
        return 0
    store.record_competence(model, "text_job", lanes, path=file)
    print("written (0600). Qualified grants no routing; the authority switch is separate.")
    return 0
