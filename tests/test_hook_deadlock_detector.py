"""Tests for hook deadlock detection system."""

from llm_router.hook_deadlock_detector import (
    HookDeadlockDetector,
    check_hook_deadlock,
)


class TestHookAnalysis:
    """Tests for individual hook analysis."""

    def test_analyze_simple_hook(self, tmp_path):
        """Test analyzing a basic hook with no issues."""
        hook_file = tmp_path / "llm-router-simple.py"
        hook_file.write_text("import subprocess\nresult = subprocess.run(['ls'], timeout=10)")
        
        detector = HookDeadlockDetector(tmp_path)
        detector._analyze_all_hooks()
        
        assert "simple" in detector._analyses
        analysis = detector._analyses["simple"]
        assert "run" in analysis.subprocess_calls

    def test_extract_subprocess_calls(self, tmp_path):
        """Test extraction of subprocess patterns."""
        hook_file = tmp_path / "llm-router-test.py"
        hook_file.write_text("import subprocess\nsubprocess.run(['cmd'])\nsubprocess.Popen(['cmd2'])")
        
        detector = HookDeadlockDetector(tmp_path)
        detector._analyze_all_hooks()
        
        analysis = detector._analyses["test"]
        assert len(analysis.subprocess_calls) >= 2

    def test_extract_timeout_config(self, tmp_path):
        """Test extraction of timeout values."""
        hook_file = tmp_path / "llm-router-timeout-test.py"
        hook_file.write_text("import subprocess\nsubprocess.run(['cmd'], timeout=30)")
        
        detector = HookDeadlockDetector(tmp_path)
        detector._analyze_all_hooks()
        
        analysis = detector._analyses["timeout-test"]
        assert len(analysis.timeouts) > 0


class TestCycleDetection:
    """Tests for circular dependency detection."""

    def test_no_cycles(self, tmp_path):
        """Test detection when no cycles exist."""
        (tmp_path / "llm-router-a.py").write_text("")
        (tmp_path / "llm-router-b.py").write_text("from llm_router.hooks.a import func")
        
        detector = HookDeadlockDetector(tmp_path)
        report = detector.analyze()
        
        assert not report.has_cycles

    def test_simple_cycle(self, tmp_path):
        """Test detection of simple 2-node cycle."""
        (tmp_path / "llm-router-a.py").write_text("from llm_router.hooks.b import func")
        (tmp_path / "llm-router-b.py").write_text("from llm_router.hooks.a import func")
        
        detector = HookDeadlockDetector(tmp_path)
        report = detector.analyze()
        
        assert report.has_cycles
        assert len(report.cycles) > 0


class TestTimeoutValidation:
    """Tests for timeout configuration checking."""

    def test_subprocess_without_timeout(self, tmp_path):
        """Test detection of subprocess calls without timeout."""
        (tmp_path / "llm-router-no-timeout.py").write_text("import subprocess\nsubprocess.run(['cmd'])")
        
        detector = HookDeadlockDetector(tmp_path)
        report = detector.analyze()
        
        assert report.has_timeout_issues


class TestReportFormatting:
    """Tests for deadlock report formatting."""

    def test_format_clean_report(self, tmp_path):
        """Test formatting of report with no issues."""
        (tmp_path / "llm-router-clean.py").write_text("subprocess.run(['cmd'], timeout=10)")
        
        detector = HookDeadlockDetector(tmp_path)
        report = detector.analyze()
        
        formatted = detector.format_report(report)
        assert "Hook Deadlock Detection Report" in formatted
        assert "STATUS" in formatted


class TestQuickCheck:
    """Tests for quick deadlock check function."""

    def test_quick_check_safe(self, tmp_path):
        """Test quick check returns False for safe hooks."""
        (tmp_path / "llm-router-safe.py").write_text("subprocess.run(['cmd'], timeout=10)")
        
        result = check_hook_deadlock(tmp_path)
        assert result is False or result is None


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_empty_hooks_dir(self, tmp_path):
        """Test analysis with no hooks."""
        detector = HookDeadlockDetector(tmp_path)
        report = detector.analyze()
        
        assert not report.has_cycles
        assert not report.has_timeout_issues
        assert report.critical_path_length == 0

