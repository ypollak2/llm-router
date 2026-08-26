"""Regression: _install_kimi_files appended to KIMI.md on EVERY run.

The guard was `if "llm_router" not in content.lower()`, testing the existing
file for a token the block it writes never contains: the rules text says
"LLM Router" (with a space) throughout, and localize() rewrites the tool names
to `llm(task="code")`. So the guard was always true and every install appended
another copy.

This is not theoretical. The committed KIMI.md in this repo carried 53 copies
of the same section, accumulated across runs, because the installer tests
invoke this function with the repo as cwd. The block was also never recorded
to the install manifest, so every copy survived `llm_router uninstall`.
"""
from __future__ import annotations

import llm_router.cli as cli
import llm_router.install_manifest as im


def _run_kimi(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(im, "_manifest_path", lambda: tmp_path / ".llm-router" / "manifest.json")
    return cli._install_kimi_files()


def test_second_install_does_not_append_a_second_copy(tmp_path, monkeypatch):
    _run_kimi(tmp_path, monkeypatch)
    kimi = tmp_path / "KIMI.md"
    first = kimi.read_text()
    header_count = first.count("## LLM Router routing")
    assert header_count == 1, f"first install wrote {header_count} copies"

    _run_kimi(tmp_path, monkeypatch)
    second = kimi.read_text()
    assert second.count("## LLM Router routing") == 1, (
        f"second install appended again — {second.count('## LLM Router routing')} copies now"
    )
    assert second == first, "KIMI.md changed on a repeat install"


def test_ten_installs_still_leave_one_copy(tmp_path, monkeypatch):
    """The committed file reached 53 copies; one run per suite execution."""
    for _ in range(10):
        _run_kimi(tmp_path, monkeypatch)
    body = (tmp_path / "KIMI.md").read_text()
    assert body.count("## LLM Router routing") == 1, (
        f"{body.count('## LLM Router routing')} copies after 10 installs"
    )


def test_user_content_is_preserved(tmp_path, monkeypatch):
    kimi = tmp_path / "KIMI.md"
    kimi.write_text("# My Project\n\nMy own notes.\n")
    _run_kimi(tmp_path, monkeypatch)
    body = kimi.read_text()
    assert "My own notes." in body, "installer clobbered the user's file"
    assert "## LLM Router routing" in body, "installer did not append its block"


def test_appended_block_is_recorded_for_uninstall(tmp_path, monkeypatch):
    """It recorded nothing, so uninstall could not strip what it wrote."""
    kimi = tmp_path / "KIMI.md"
    kimi.write_text("# My Project\n")
    _run_kimi(tmp_path, monkeypatch)
    assert (
        im.find("text_block", kimi) is not None or im.find("created_file", kimi) is not None
    ), "KIMI.md edit was never recorded to the install manifest"
