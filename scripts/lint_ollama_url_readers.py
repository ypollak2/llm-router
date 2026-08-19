#!/usr/bin/env python3
"""Every reader of the Ollama URL env vars must route through the validator.

WHY THIS EXISTS
===============

`config.validate_ollama_url` was written because these variables were an SSRF
sink -- its docstring records that `file://` was accepted (local file read) and
cloud-metadata addresses were attempted. The fix landed in `config.py`.

It was then bypassed by THREE separate copies of the same reader:

    hooks/agent_loop.py        _get_ollama_url()      unvalidated
    hooks/direct_executor.py   _get_ollama_url()      unvalidated
    hooks/auto-route.py        inline in a probe      unvalidated

The first two were found by triaging Bandit B310. The third was found only by
auditing the `# nosec` justification attached to it, which claimed "localhost
Ollama only" -- a statement the code did not guarantee, since the URL comes from
the environment and `_load_dotenv` reads `Path.cwd()/".env"`.

Three copies, two discovery passes, and the last one was hidden behind a
confident and false comment. That is enough recurrence to enforce rather than
remember.

THE RULE
========

Any file that reads `LLM_ROUTER_OLLAMA_URL` or `OLLAMA_BASE_URL` must also reference
`validate_ollama_url` (directly, or via `_validated_ollama_url` which wraps it).
`config.py` is the definition site and is exempt.

WHAT THIS DOES NOT CATCH
========================

Textual co-presence, not dataflow. Known false negatives, stated so nobody reads
a green result as more than it is:

  - a file that imports the validator and then does not call it on some path;
  - validation performed in a different module than the read;
  - a read whose value reaches urlopen before the validation runs.

It is shipped anyway because the failure that actually happened SEVEN times was
none of those. It was a file reading the variable with no mention of validation
anywhere in it -- which this catches exactly, on the day the file is written. A
dataflow check would be strictly better and is not a reason to ship nothing in
the meantime.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "llm_router"

ENV_VARS = ("LLM_ROUTER_OLLAMA_URL", "OLLAMA_BASE_URL")

#: Only an actual environment READ counts. A mention in a comment, docstring or
#: registry table is documentation, and flagging it trains people to ignore this.
_READ_FORMS = (
    'os.environ.get("{v}"', "os.environ.get('{v}'",
    'os.getenv("{v}"',      "os.getenv('{v}'",
    'os.environ["{v}"]',    "os.environ['{v}']",
)
VALIDATORS = ("validate_ollama_url", "_validated_ollama_url")

#: The validator's own home. Exempt because it IS the check.
EXEMPT = {"config.py"}


def main() -> int:
    offenders: list[str] = []
    readers = 0

    for path in sorted(SRC.rglob("*.py")):
        if path.name in EXEMPT:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        reads = [
            form.format(v=var)
            for var in ENV_VARS
            for form in _READ_FORMS
            if form.format(v=var) in text
        ]
        if not reads:
            continue
        readers += 1
        if not any(v in text for v in VALIDATORS):
            rel = path.relative_to(SRC.parent.parent)
            lines = [
                f"      line {i}: {ln.strip()}"
                for i, ln in enumerate(text.splitlines(), 1)
                if any(r in ln for r in reads)
            ]
            offenders.append(f"  {rel}\n" + "\n".join(lines[:4]))

    if readers == 0:
        # Guards the guard. If the variables are renamed and this lint is not
        # updated, it would pass while checking nothing -- the shape
        # 30_CI_GAP_PLAN §8 is about.
        print(
            "FAIL: no reader of "
            f"{'/'.join(ENV_VARS)} found anywhere in src/. Either the variables "
            "were renamed and this lint was not updated, or the Ollama "
            "integration is gone. Both need a human.",
            file=sys.stderr,
        )
        return 1

    if offenders:
        print("OLLAMA URL VALIDATION FAIL: an env reader does not reach the validator.\n")
        print("\n".join(offenders))
        print(
            "\nThese variables were a documented SSRF sink (CHZ-SEC-06): file:// gave a"
            "\nlocal file read and 169.254.169.254 was reachable. `_load_dotenv` reads"
            "\nPath.cwd()/'.env', so a cloned repository can set them."
            "\n\nRoute the value through llm_router.config.validate_ollama_url — imported,"
            "\nnot reimplemented. Three copies of this reader already diverged."
        )
        return 1

    print(f"ollama-url validation OK: {readers} reader(s), all reach the validator")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
