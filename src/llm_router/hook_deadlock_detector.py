"""Hook deadlock detection and prevention system.

Detects and prevents:
- Circular dependencies between hooks
- Missing subprocess timeouts  
- Resource contention patterns
- Excessive timeout values
- Hook execution chain issues
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple


class DeadlockReport(NamedTuple):
    """Result of deadlock detection analysis."""
    has_cycles: bool
    cycles: list[list[str]]
    has_timeout_issues: bool
    timeout_issues: list[str]
    has_resource_contention: bool
    contention_patterns: list[tuple[str, str]]
    all_dependencies: dict[str, set[str]]
    critical_path_length: int


@dataclass
class HookAnalysis:
    """Analysis of a single hook for deadlock risks."""
    name: str
    path: Path
    dependencies: set[str] = field(default_factory=set)
    subprocess_calls: list[str] = field(default_factory=list)
    timeouts: dict[str, int | None] = field(default_factory=dict)
    shared_resources: set[str] = field(default_factory=set)
    has_recursion: bool = False
    error_messages: list[str] = field(default_factory=list)


class HookDeadlockDetector:
    """Detects and reports deadlock risks in hook chains."""

    def __init__(self, hooks_dir: str | Path | None = None):
        """Initialize the deadlock detector."""
        if hooks_dir is None:
            hooks_dir = Path.home() / ".claude" / "hooks"
        self.hooks_dir = Path(hooks_dir).expanduser()
        self._analyses: dict[str, HookAnalysis] = {}
        self._visited_cycles: set[frozenset[str]] = set()

    def analyze(self) -> DeadlockReport:
        """Run full deadlock analysis on all hooks."""
        self._analyze_all_hooks()
        cycles = self._find_cycles()
        timeout_issues = self._check_timeout_coverage()
        contention = self._detect_contention()
        critical_path = self._compute_critical_path()

        return DeadlockReport(
            has_cycles=len(cycles) > 0,
            cycles=cycles,
            has_timeout_issues=len(timeout_issues) > 0,
            timeout_issues=timeout_issues,
            has_resource_contention=len(contention) > 0,
            contention_patterns=contention,
            all_dependencies={
                name: analysis.dependencies
                for name, analysis in self._analyses.items()
            },
            critical_path_length=critical_path,
        )

    def _analyze_all_hooks(self) -> None:
        """Scan and analyze all hook files in hooks_dir."""
        if not self.hooks_dir.exists():
            return

        for hook_file in self.hooks_dir.glob("llm-router-*.py"):
            hook_name = hook_file.stem.replace("llm-router-", "")
            self._analyses[hook_name] = self._analyze_hook(hook_name, hook_file)

    def _analyze_hook(self, name: str, path: Path) -> HookAnalysis:
        """Analyze a single hook file for deadlock risks."""
        analysis = HookAnalysis(name=name, path=path)

        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as e:
            analysis.error_messages.append(f"Cannot read hook: {e}")
            return analysis

        self._extract_subprocess_calls(content, analysis)
        self._extract_timeout_config(content, analysis)
        self._check_recursion(name, content, analysis)
        self._extract_shared_resources(content, analysis)
        self._extract_hook_dependencies(name, content, analysis)

        return analysis

    def _extract_subprocess_calls(self, content: str, analysis: HookAnalysis) -> None:
        """Extract subprocess.run and similar calls."""
        patterns = [
            r"subprocess\.(run|Popen|call|check_call|check_output)\([^)]*\)",
            r"os\.system\([^)]*\)",
            r"subprocess\.TimeoutExpired",
        ]

        for pattern in patterns:
            matches = re.findall(pattern, content)
            analysis.subprocess_calls.extend(matches)

    def _extract_timeout_config(self, content: str, analysis: HookAnalysis) -> None:
        """Extract timeout values from subprocess calls."""
        timeout_patterns = [
            r"timeout=(\d+|\w+\(\))",
            r"LLM_ROUTER_.*TIMEOUT",
            r"_timeout\(\)",
        ]

        for pattern in timeout_patterns:
            matches = re.findall(pattern, content, re.IGNORECASE)
            for match in matches:
                val = match[0] if isinstance(match, tuple) else match
                if val and val.isdigit():
                    analysis.timeouts[pattern] = int(val)
                elif val:
                    analysis.timeouts[pattern] = None

    def _check_recursion(self, name: str, content: str, analysis: HookAnalysis) -> None:
        """Check if hook calls itself (direct recursion)."""
        patterns = [
            rf"from llm_router\.{name}",
            rf"import.*{name}",
            r"exec\(|eval\(",
        ]

        for pattern in patterns:
            if re.search(pattern, content, re.IGNORECASE):
                analysis.has_recursion = True
                analysis.error_messages.append(f"Potential recursion: {pattern}")

    def _extract_shared_resources(self, content: str, analysis: HookAnalysis) -> None:
        """Extract files and resources accessed by the hook."""
        resource_patterns = [
            r"os\.path\.join\([^)]*\.json[^)]*\)",
            r"os\.path\.join\([^)]*\.txt[^)]*\)",
            r"os\.path\.join\([^)]*\.lock[^)]*\)",
            r"\"[^\"]*\.json\"",
            r"\"[^\"]*\.db\"",
            r"\"[^\"]*\.lock\"",
        ]

        for pattern in resource_patterns:
            matches = re.findall(pattern, content)
            analysis.shared_resources.update(matches)

    def _extract_hook_dependencies(self, name: str, content: str, analysis: HookAnalysis) -> None:
        """Extract which other hooks this hook depends on."""
        hook_patterns = [
            r"from llm_router\.hooks\.(\w+) import",
            r"subprocess\.run\([^)]*llm-router-(\w+)",
            r"hook_client\.call\([\'\"](\w+)",
        ]

        for pattern in hook_patterns:
            matches = re.findall(pattern, content)
            for match in matches:
                dep_name = match[0] if isinstance(match, tuple) else match
                if dep_name and dep_name != name:
                    analysis.dependencies.add(dep_name)

    def _find_cycles(self) -> list[list[str]]:
        """Find all circular dependencies using DFS."""
        cycles = []
        for start_hook in self._analyses:
            visited = set()
            rec_stack = set()
            cycle = self._dfs_detect_cycle(start_hook, visited, rec_stack, [start_hook])
            if cycle:
                cycle_set = frozenset(cycle)
                if cycle_set not in self._visited_cycles:
                    cycles.append(cycle)
                    self._visited_cycles.add(cycle_set)
        return cycles

    def _dfs_detect_cycle(self, node: str, visited: set[str], rec_stack: set[str], path: list[str]) -> list[str] | None:
        """DFS helper to detect cycle from a starting node."""
        visited.add(node)
        rec_stack.add(node)
        
        analysis = self._analyses.get(node)
        dependencies = analysis.dependencies if analysis else set()

        for dep in dependencies:
            if dep not in visited:
                result = self._dfs_detect_cycle(dep, visited, rec_stack, path + [dep])
                if result:
                    return result
            elif dep in rec_stack:
                cycle_start_idx = path.index(dep)
                return path[cycle_start_idx:] + [dep]

        rec_stack.discard(node)
        return None

    def _check_timeout_coverage(self) -> list[str]:
        """Check for hooks with subprocess calls but no timeout."""
        issues = []
        for name, analysis in self._analyses.items():
            if analysis.subprocess_calls and not analysis.timeouts:
                issues.append(f"{name}: {len(analysis.subprocess_calls)} subprocess calls without timeout")
            for op, timeout_val in analysis.timeouts.items():
                if timeout_val and timeout_val > 3600:
                    issues.append(f"{name}: excessive timeout {timeout_val}s")
        return issues

    def _detect_contention(self) -> list[tuple[str, str]]:
        """Detect resource contention between hooks."""
        contention = []
        hook_names = list(self._analyses.keys())
        for i, name1 in enumerate(hook_names):
            for name2 in hook_names[i + 1 :]:
                analysis1 = self._analyses[name1]
                analysis2 = self._analyses[name2]
                shared = analysis1.shared_resources & analysis2.shared_resources
                if shared:
                    contention.append((name1, name2))
        return contention

    def _compute_critical_path(self) -> int:
        """Compute longest dependency chain."""
        max_depth = 0
        for start_hook in self._analyses:
            depth = self._compute_depth(start_hook, set())
            max_depth = max(max_depth, depth)
        return max_depth

    def _compute_depth(self, node: str, visited: set[str]) -> int:
        """Compute depth of dependency tree from a node."""
        if node in visited:
            return 0
        analysis = self._analyses.get(node)
        if not analysis or not analysis.dependencies:
            return 1
        visited.add(node)
        max_child_depth = 0
        for dep in analysis.dependencies:
            child_depth = self._compute_depth(dep, visited.copy())
            max_child_depth = max(max_child_depth, child_depth)
        return 1 + max_child_depth

    def get_hook_info(self, hook_name: str) -> dict | None:
        """Get detailed information about a specific hook."""
        analysis = self._analyses.get(hook_name)
        if not analysis:
            return None
        return {
            "name": analysis.name,
            "path": str(analysis.path),
            "dependencies": list(analysis.dependencies),
            "subprocess_calls": analysis.subprocess_calls,
            "timeouts": analysis.timeouts,
            "shared_resources": list(analysis.shared_resources),
            "has_recursion": analysis.has_recursion,
            "errors": analysis.error_messages,
        }

    def format_report(self, report: DeadlockReport) -> str:
        """Format deadlock report as human-readable text."""
        lines = ["╔════════════════════════════════════════════════════════╗"]
        lines.append("║     Hook Deadlock Detection Report                   ║")
        lines.append("╚════════════════════════════════════════════════════════╝")
        lines.append("")

        if report.has_cycles:
            lines.append("🔴 CIRCULAR DEPENDENCIES DETECTED:")
            for i, cycle in enumerate(report.cycles, 1):
                cycle_str = " → ".join(cycle)
                lines.append(f"  {i}. {cycle_str}")
        else:
            lines.append("✅ No circular dependencies")
        lines.append("")

        if report.has_timeout_issues:
            lines.append("⚠️  TIMEOUT ISSUES:")
            for issue in report.timeout_issues:
                lines.append(f"  • {issue}")
        else:
            lines.append("✅ All subprocess calls protected by timeouts")
        lines.append("")

        if report.has_resource_contention:
            lines.append("⚠️  RESOURCE CONTENTION:")
            for hook1, hook2 in report.contention_patterns:
                lines.append(f"  • {hook1} ↔ {hook2}")
        else:
            lines.append("✅ No resource contention detected")
        lines.append("")

        lines.append(f"📊 Critical Path Length: {report.critical_path_length} hooks")
        lines.append("")

        if report.has_cycles or report.has_timeout_issues:
            lines.append("🚨 STATUS: ACTION REQUIRED")
        else:
            lines.append("✅ STATUS: SAFE TO DEPLOY")

        return "\n".join(lines)


def check_hook_deadlock(hooks_dir: str | Path | None = None) -> bool:
    """Quick check for hook deadlock issues."""
    detector = HookDeadlockDetector(hooks_dir)
    report = detector.analyze()
    if report.has_cycles or report.has_timeout_issues:
        print(detector.format_report(report), file=sys.stderr)
        return True
    return False
