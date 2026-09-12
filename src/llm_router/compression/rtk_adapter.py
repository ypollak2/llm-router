"""RTK-style command output compression for llm_router.

Compresses shell command outputs before they reach the LLM context,
reducing context pollution by 80-90%.
"""

from dataclasses import dataclass
from typing import Callable


@dataclass
class CompressionResult:
    """Result of compressing command output."""

    original_tokens: int
    compressed_tokens: int
    compression_ratio: float
    output: str
    strategy: str

    def tokens_saved(self) -> int:
        """Calculate tokens saved."""
        return self.original_tokens - self.compressed_tokens


# Prefixes that position a command without being the command: the interesting
# part is what follows. Kept deliberately small — guessing wrong here silently
# mis-files output, which is the bug this exists to fix.
_POSITIONING_CMDS = frozenset({"cd", "pushd", "env", "time", "nohup", "exec"})

# Separators after which a new command begins. `|` is excluded on purpose: the
# output of `git log | head` is shaped by `git log`, not by `head`.
# A NEWLINE separates commands too, and in agent transcripts it is the common
# case: a multi-line Bash block that opens with `cd /repo` on its own line.
# Omitting it left 377 of 993 real outputs still classified as `cd`.
_SEPARATORS = ("&&", ";", "||", "\n")


def effective_command(command: str) -> str:
    """The command whose OUTPUT we are about to compress.

    `cd /repo && git status` produces git output, not cd output. Walks past
    leading positioning commands and returns the first real one; returns the
    original string when there is nothing to strip.
    """
    if not command:
        return ""
    remaining = command.strip()
    for _ in range(4):  # bounded: `cd a && cd b && env X=1 git status`
        parts = remaining.split()
        if not parts or parts[0] not in _POSITIONING_CMDS:
            break
        # `env`/`time`/`nohup` prefix a command directly rather than via a
        # separator: `env X=1 pytest tests` is a pytest run. Step over the
        # keyword and any VAR=value assignments that follow it.
        if parts[0] in ("env", "time", "nohup", "exec"):
            rest = parts[1:]
            while rest and "=" in rest[0] and not rest[0].startswith("-"):
                rest = rest[1:]
            if rest:
                remaining = " ".join(rest)
                continue
            break

        cut = -1
        for sep in _SEPARATORS:
            idx = remaining.find(sep)
            if idx != -1 and (cut == -1 or idx < cut):
                cut = idx + len(sep)
        if cut == -1:
            break          # `cd /repo` alone really is a cd
        remaining = remaining[cut:].strip()
    return remaining or command.strip()


class RTKAdapter:
    """Compress shell command outputs like RTK does.

    Strategies:
    - Deduplicate repetitive lines
    - Remove noise and metadata
    - Summarize long outputs
    - Keep only errors/changes
    """

    def __init__(self, enable: bool = True):
        self.enable = enable
        self.filters = self._build_filters()

    def _build_filters(self) -> dict[str, dict[str, Callable[[str], str]]]:
        """Build command-specific compression filters."""
        return {
            "git": {
                "log": self._git_log,
                "status": self._git_status,
                "diff": self._git_diff,
                "branch": self._git_branch,
            },
            "pytest": {
                "*": self._pytest,
            },
            "cargo": {
                "build": self._cargo_build,
                "test": self._cargo_test,
            },
            "docker": {
                "ps": self._docker_ps,
                "logs": self._docker_logs,
            },
            "npm": {
                "test": self._npm_test,
            },
            "uv": {
                "run": self._uv_run,
            },
        }

    def compress(
        self, command: str, output: str, max_lines: int = 50
    ) -> CompressionResult:
        """Compress command output.

        Args:
            command: Full command string (e.g., "git log --oneline")
            output: Command output
            max_lines: Maximum lines in compressed output

        Returns:
            CompressionResult with compression stats
        """
        if not self.enable or not output:
            return CompressionResult(
                original_tokens=self._estimate_tokens(output),
                compressed_tokens=self._estimate_tokens(output),
                compression_ratio=1.0,
                output=output,
                strategy="disabled",
            )

        # Parse command — the EFFECTIVE one, not the first word.
        #
        # Measured on 989 real Bash outputs from live transcripts: 710 of them
        # (72%) were classified as `cd`, because an agent's command is routinely
        # `cd /some/repo && git status`. The filter was chosen from the `cd`, so
        # the git/pytest/grep filter that would have compressed the output never
        # ran: those 710 calls saved 4.8%, while a correctly-classified `sed`
        # saved 36%.
        parts = effective_command(command).split()
        if not parts:
            return self._no_compression(output)

        base_cmd = parts[0]  # git, pytest, cargo, etc.
        sub_cmd = parts[1] if len(parts) > 1 else "*"

        # Find matching filter
        if base_cmd in self.filters:
            cmd_filters = self.filters[base_cmd]
            if sub_cmd in cmd_filters:
                compressed = cmd_filters[sub_cmd](output)
            elif "*" in cmd_filters:
                compressed = cmd_filters["*"](output)
            else:
                # Fallback: generic compression
                compressed = self._generic_compress(output, max_lines)
        else:
            # Unknown command: apply generic compression
            compressed = self._generic_compress(output, max_lines)

        return CompressionResult(
            original_tokens=self._estimate_tokens(output),
            compressed_tokens=self._estimate_tokens(compressed),
            compression_ratio=(
                self._estimate_tokens(compressed) / self._estimate_tokens(output)
                if output
                else 1.0
            ),
            output=compressed,
            strategy=f"{base_cmd}:{sub_cmd}",
        )

    # ─────────────────────────────────────────────────────
    # Git filters
    # ─────────────────────────────────────────────────────

    def _git_log(self, output: str) -> str:
        """Compress git log output.

        git log (500 lines) → 10 key commits
        """
        lines = output.split("\n")
        if len(lines) <= 15:
            return output

        # Keep first 10 + last 5
        return "\n".join(lines[:10] + ["..."] + lines[-5:])

    def _git_status(self, output: str) -> str:
        """Compress git status output, in either of its two formats.

        This was written for the VERBOSE format only ("On branch",
        "modified:", "new file:"). Against `--porcelain` — ` M path`, `?? path`
        — nothing matched, `summary` came out empty, and the fallback returned
        `output[:200]`: a blind character cut that dropped 18 of 28 lines and
        ended mid-word, with nothing to say it had happened.

        That was survivable while compression output was merely appended and
        ignored. It is not survivable now that the output REPLACES what the
        model sees: silently discarding two thirds of a file list produces
        confident wrong answers about which files changed.
        """
        lines = output.split("\n")
        summary = []

        # Porcelain: a two-character status code, a space, then a path.
        porcelain = [ln for ln in lines if len(ln) > 3 and ln[2] == " " and ln[:2].strip("? MADRCU!") == ""]
        if porcelain and len(porcelain) >= len([ln for ln in lines if ln.strip()]) // 2:
            buckets: dict[str, list[str]] = {}
            for line in porcelain:
                buckets.setdefault(line[:2].strip() or "??", []).append(line[3:])
            names = {"M": "modified", "A": "added", "D": "deleted",
                     "R": "renamed", "??": "untracked", "!!": "ignored"}
            out = []
            for code, paths in sorted(buckets.items()):
                label = names.get(code, code)
                # Name a few, then COUNT the rest. Every path is accounted for,
                # which is the difference between compression and data loss.
                shown = paths[:8]
                out.append(f"{label} ({len(paths)}): " + ", ".join(shown)
                           + (f", +{len(paths) - len(shown)} more" if len(paths) > len(shown) else ""))
            return "\n".join(out)

        for line in lines:
            # Keep branch info
            if "On branch" in line or "HEAD detached" in line:
                summary.append(line)
            # Count changes
            elif "modified:" in line or "new file:" in line:
                pass  # Skip individual files
            # Keep change summary
            elif "Changes to be committed" in line or "modified:" in line.lower():
                summary.append(line)

        # Add file count summary
        modified_count = output.count("modified:")
        new_count = output.count("new file:")
        if modified_count > 0 or new_count > 0:
            summary.append(f"Files changed: {modified_count} modified, {new_count} new")

        # Decline rather than truncate. A filter that did not recognise its
        # input has no basis for choosing which 200 characters matter.
        return "\n".join(summary) if summary else output

    def _git_diff(self, output: str) -> str:
        """Compress git diff output.

        Keep only: file names, +/- counts, skip actual hunks
        """
        lines = output.split("\n")
        result = []

        for line in lines:
            # Keep file headers
            if line.startswith("diff --git"):
                result.append(line)
            # Skip hunks but count them
            elif line.startswith("@@"):
                pass
            # Skip actual changes, keep only summary
            elif line.startswith("+++") or line.startswith("---"):
                result.append(line)

        if not result:
            return output  # decline rather than cut blindly

        # Add summary
        additions = output.count("\n+")
        deletions = output.count("\n-")
        result.append(f"\nSummary: +{additions} -{deletions} lines")

        return "\n".join(result)

    def _git_branch(self, output: str) -> str:
        """Compress git branch output.

        Keep only: current branch, total count
        """
        lines = output.split("\n")
        current = None
        total = len([line_content for line_content in lines if line_content.strip()])

        for line in lines:
            if line.startswith("*"):
                current = line
                break

        if current:
            return f"{current}\nTotal branches: {total}"
        return output

    # ─────────────────────────────────────────────────────
    # Pytest filter
    # ─────────────────────────────────────────────────────

    def _pytest(self, output: str) -> str:
        """Compress pytest output.

        Keep: test count, pass/fail summary, failures only
        """
        lines = output.split("\n")
        result = []

        for line in lines:
            # Keep summary lines
            if "passed" in line or "failed" in line or "error" in line:
                if any(c.isdigit() for c in line):
                    result.append(line)
            # Keep failure info
            elif "FAILED" in line or "ERROR" in line:
                result.append(line)
            # Skip verbose output
            elif line.startswith("test_"):
                pass

        return "\n".join(result[-20:]) if result else "Tests completed"

    # ─────────────────────────────────────────────────────
    # Cargo filters
    # ─────────────────────────────────────────────────────

    def _cargo_build(self, output: str) -> str:
        """Compress cargo build output.

        Keep: errors only, skip warnings and intermediate steps
        """
        lines = output.split("\n")
        errors = [line_content for line_content in lines if "error" in line_content.lower()]
        summary = [line_content for line_content in lines if "finished" in line_content.lower() or "compiling" in line_content]

        if errors:
            return "ERRORS:\n" + "\n".join(errors[:5])
        elif summary:
            return "\n".join(summary)
        else:
            return output[-200:]

    def _cargo_test(self, output: str) -> str:
        """Compress cargo test output.

        Keep: test count, pass/fail summary
        """
        lines = output.split("\n")
        result = []

        for line in lines:
            if "test result:" in line or "passed" in line or "FAILED" in line:
                result.append(line)

        return "\n".join(result) if result else "Tests completed"

    # ─────────────────────────────────────────────────────
    # Docker filters
    # ─────────────────────────────────────────────────────

    def _docker_ps(self, output: str) -> str:
        """Compress docker ps output.

        Keep: container count, status summary
        """
        lines = output.split("\n")
        if len(lines) <= 5:
            return output

        header = lines[0]
        containers = lines[1:]
        running = [line_content for line_content in containers if "Up" in line_content]
        exited = [line_content for line_content in containers if "Exited" in line_content]

        summary = f"{header}\n{len(running)} running, {len(exited)} exited"
        return summary

    def _docker_logs(self, output: str) -> str:
        """Compress docker logs output.

        Keep: last 10 lines + errors
        """
        lines = output.split("\n")
        errors = [line_content for line_content in lines if "error" in line_content.lower()]

        if errors:
            return "Recent errors:\n" + "\n".join(errors[-5:])
        else:
            # Keep last 10 lines
            return "\n".join(lines[-10:])

    # ─────────────────────────────────────────────────────
    # NPM filter
    # ─────────────────────────────────────────────────────

    def _npm_test(self, output: str) -> str:
        """Compress npm test output.

        Keep: test count, pass/fail summary
        """
        lines = output.split("\n")
        result = []

        for line in lines:
            if "passing" in line or "failing" in line or "pending" in line:
                result.append(line)
            elif "FAIL" in line:
                result.append(line)

        return "\n".join(result) if result else output

    # ─────────────────────────────────────────────────────
    # UV filter
    # ─────────────────────────────────────────────────────

    def _uv_run(self, output: str) -> str:
        """Compress uv run output.

        Keep: errors/failures only, skip compilation details
        """
        lines = output.split("\n")
        errors = [line_content for line_content in lines if "error" in line_content.lower()]

        if errors:
            return "ERRORS:\n" + "\n".join(errors[:5])

        # Keep last 5 lines (usually summary)
        return "\n".join(lines[-5:])

    # ─────────────────────────────────────────────────────
    # Generic fallback
    # ─────────────────────────────────────────────────────

    def _generic_compress(self, output: str, max_lines: int = 50) -> str:
        """Generic compression: keep first + last lines, remove duplicates."""
        lines = output.split("\n")

        if len(lines) <= max_lines:
            return output

        # Keep first 20 and last 20 lines
        keep_start = 20
        keep_end = 20

        result = lines[:keep_start] + [f"... ({len(lines) - keep_start - keep_end} lines omitted) ..."] + lines[-keep_end:]
        return "\n".join(result)

    def _no_compression(self, output: str) -> CompressionResult:
        """Return uncompressed output."""
        tokens = self._estimate_tokens(output)
        return CompressionResult(
            original_tokens=tokens,
            compressed_tokens=tokens,
            compression_ratio=1.0,
            output=output,
            strategy="no_compression",
        )

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Estimate token count (rough: 1 token ≈ 4 characters)."""
        return max(1, len(text) // 4)
