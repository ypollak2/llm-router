"""A compressor that replaces a tool result may not lose data silently.

While compression output was only appended (and, as it turned out, ignored
entirely), a lossy filter was harmless. Now that a PreToolUse deny SUBSTITUTES
the compressed text for the real output, anything the filter drops is something
the model will never learn — and it has no way to know it is missing.

The bug that prompted these: `_git_status` was written for the verbose format
("On branch", "modified:") and matched nothing in `--porcelain`, so it fell
through to `output[:200]` — a blind character cut that dropped 18 of 28 lines
and ended mid-word at "?? prefix_". Two sibling filters had the same fallback.
"""
from __future__ import annotations

import pytest

from llm_router.compression.rtk_adapter import RTKAdapter


@pytest.fixture
def adapter():
    return RTKAdapter(enable=True)


PORCELAIN = "\n".join(
    [" M src/a.py", " M src/b.py", "?? new_one.txt"]
    + [f"?? generated/file_{i}.txt" for i in range(25)]
)


def test_porcelain_status_is_understood(adapter):
    out = adapter.compress("git status --porcelain", PORCELAIN).output
    assert "modified" in out and "untracked" in out


def test_every_file_is_accounted_for(adapter):
    """Naming a few and counting the rest is compression. Dropping the rest
    without saying so is data loss wearing compression's clothes."""
    out = adapter.compress("git status --porcelain", PORCELAIN).output
    assert "(26)" in out or "26" in out, "the untracked count is missing"
    assert "more" in out, "omitted entries are not acknowledged"


def test_output_is_never_cut_mid_token(adapter):
    """The symptom that exposed this: the substituted text ended '?? prefix_'."""
    out = adapter.compress("git status --porcelain", PORCELAIN).output
    assert not out.endswith(("_", "-", "/")), f"cut mid-token: {out[-40:]!r}"


@pytest.mark.parametrize("command", [
    "git status", "git status --porcelain", "git diff", "git branch", "ls -la",
])
def test_an_unrecognised_shape_declines_rather_than_truncating(adapter, command):
    """A filter that did not recognise its input has no basis for choosing
    which 200 characters matter, so it must hand back everything."""
    weird = "\n".join(f"totally unexpected line {i} of output" for i in range(40))
    out = adapter.compress(command, weird).output
    assert len(out) >= len(weird) or "omitted" in out, \
        "content disappeared with no accounting"


def test_compression_never_exceeds_its_input(adapter):
    for command in ("git status --porcelain", "ls -la", "cat x.txt"):
        out = adapter.compress(command, PORCELAIN).output
        assert len(out) <= len(PORCELAIN)


def test_the_generic_filter_announces_what_it_dropped(adapter):
    """The generic path was already honest; the named filters were not. This
    pins the behaviour they were brought up to match."""
    long_output = "\n".join(f"line {i}" for i in range(200))
    out = adapter.compress("somethingunknown --flag", long_output).output
    assert "omitted" in out


def test_a_verbose_status_still_compresses(adapter):
    """The original format must keep working — the porcelain branch is an
    addition, not a replacement."""
    verbose = ("On branch main\n"
               "Changes to be committed:\n"
               "\tmodified:   a.py\n"
               "\tmodified:   b.py\n"
               "\tnew file:   c.py\n")
    out = adapter.compress("git status", verbose).output
    assert "main" in out
