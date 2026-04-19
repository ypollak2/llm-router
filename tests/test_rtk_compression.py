"""Tests for RTK-style command output compression."""

from llm_router.compression.rtk_adapter import RTKAdapter, CompressionResult


class TestCompressionResult:
    """Test CompressionResult dataclass."""

    def test_tokens_saved(self):
        """Test tokens_saved calculation."""
        result = CompressionResult(
            original_tokens=100,
            compressed_tokens=20,
            compression_ratio=0.2,
            output="compressed",
            strategy="git:log",
        )
        assert result.tokens_saved() == 80


class TestRTKAdapter:
    """Test RTKAdapter compression engine."""

    def test_initialization(self):
        """Test adapter initialization."""
        adapter = RTKAdapter(enable=True)
        assert adapter.enable is True
        assert adapter.filters is not None

    def test_disabled_adapter(self):
        """Test disabled adapter returns no compression."""
        adapter = RTKAdapter(enable=False)
        result = adapter.compress("git log", "line1\nline2\nline3")
        assert result.compression_ratio == 1.0
        assert result.strategy == "disabled"

    def test_empty_output(self):
        """Test empty output returns no compression."""
        adapter = RTKAdapter(enable=True)
        result = adapter.compress("git log", "")
        assert result.compression_ratio == 1.0
        assert result.strategy == "disabled"

    def test_git_log_compression(self):
        """Test git log compression keeps first 10 + last 5 commits."""
        adapter = RTKAdapter(enable=True)
        lines = [f"commit{i}" for i in range(20)]
        output = "\n".join(lines)

        result = adapter.compress("git log --oneline", output)
        assert result.strategy == "git:log"
        # Should compress significantly (20 lines → ~15 with ellipsis)
        assert result.compression_ratio < 1.0
        assert "..." in result.output

    def test_git_status_compression(self):
        """Test git status extracts branch and file counts."""
        adapter = RTKAdapter(enable=True)
        output = ("On branch main\nmodified: file1.py\nmodified: file2.py\n"
                  "new file: file3.py\nnothing to commit")

        result = adapter.compress("git status", output)
        assert result.strategy == "git:status"
        assert "On branch main" in result.output
        assert "Files changed:" in result.output
        assert "2 modified, 1 new" in result.output

    def test_git_diff_compression(self):
        """Test git diff keeps file headers and summarizes changes."""
        adapter = RTKAdapter(enable=True)
        output = ("diff --git a/file1.py b/file1.py\n--- a/file1.py\n+++ b/file1.py\n"
                  "@@ -1,3 +1,3 @@\n def foo():\n-    return 1\n+    return 2\n"
                  "diff --git a/file2.py b/file2.py\n--- a/file2.py\n+++ b/file2.py\n"
                  "@@ -1,5 +1,5 @@\n line1\n-line2\n+modified line2\n")

        result = adapter.compress("git diff", output)
        assert result.strategy == "git:diff"
        assert "diff --git a/file1.py" in result.output
        assert "Summary:" in result.output

    def test_git_branch_compression(self):
        """Test git branch keeps current and total count."""
        adapter = RTKAdapter(enable=True)
        output = "  feature/auth\n  feature/ui\n* main\n  develop\n  staging"

        result = adapter.compress("git branch", output)
        assert result.strategy == "git:branch"
        assert "* main" in result.output
        assert "Total branches: 5" in result.output

    def test_pytest_compression(self):
        """Test pytest compression keeps pass/fail summary."""
        adapter = RTKAdapter(enable=True)
        output = ("test_module.py::test_func1 PASSED\ntest_module.py::test_func2 PASSED\n"
                  "test_module.py::test_func3 FAILED\ntest_module.py::test_func4 PASSED\n"
                  "===== 3 passed, 1 failed in 0.42s =====")

        result = adapter.compress("pytest", output)
        assert result.strategy == "pytest:*"
        assert "passed" in result.output or "3 passed" in result.output

    def test_cargo_build_compression(self):
        """Test cargo build keeps errors only."""
        adapter = RTKAdapter(enable=True)
        output = ("   Compiling my_crate v0.1.0\nwarning: unused variable: `x`\n"
                  "error: cannot find function `foo` in this scope\n"
                  "warning: unused import\n   Finished release [optimized] target(s)")

        result = adapter.compress("cargo build", output)
        assert result.strategy == "cargo:build"
        # Should focus on errors
        assert "error" in result.output.lower()

    def test_docker_ps_compression(self):
        """Test docker ps summarizes container counts."""
        adapter = RTKAdapter(enable=True)
        output = ("CONTAINER ID   IMAGE     STATUS\nabc123         image:1   Up 2 hours\n"
                  "def456         image:2   Up 1 hour\nghi789         image:3   "
                  "Exited (0) 30 minutes ago\njkl012         image:4   Exited (1) 1 hour ago\n"
                  "mno345         image:5   Exited (0) 2 hours ago")

        result = adapter.compress("docker ps -a", output)
        assert result.strategy == "docker:ps"
        assert "2 running" in result.output
        assert "3 exited" in result.output

    def test_docker_logs_compression(self):
        """Test docker logs keeps recent errors or last 10 lines."""
        adapter = RTKAdapter(enable=True)
        output = "\n".join([f"line{i}" for i in range(50)])

        result = adapter.compress("docker logs container", output)
        assert result.strategy == "docker:logs"
        # Should keep only last 10 lines
        assert "line49" in result.output
        assert "line40" in result.output  # Last 10 lines start at line40

    def test_npm_test_compression(self):
        """Test npm test keeps pass/fail summary."""
        adapter = RTKAdapter(enable=True)
        output = ("FAIL test/unit/bar.test.js\nTest Suites: 1 failed, 1 passed, 2 total\n"
                  "Tests: 4 passed, 1 failed, 5 total\nFAIL")

        result = adapter.compress("npm test", output)
        assert result.strategy == "npm:test"
        # Should capture summary lines with "passed" or "failed"
        assert "FAIL" in result.output or "passed" in result.output.lower()

    def test_uv_run_compression(self):
        """Test uv run keeps errors only."""
        adapter = RTKAdapter(enable=True)
        output = "Resolved 42 packages in 1.23s\nInstalled 25 packages\nerror: ImportError: cannot import name 'foo'\n       from 'module' (module/__init__.py)"

        result = adapter.compress("uv run test", output)
        assert result.strategy == "uv:run"
        assert "error" in result.output.lower()

    def test_generic_compression_short_output(self):
        """Test generic compression skips short outputs."""
        adapter = RTKAdapter(enable=True)
        short_output = "line1\nline2\nline3"

        result = adapter.compress("unknown command", short_output)
        assert "unknown" in result.strategy
        assert result.compression_ratio == 1.0
        assert result.output == short_output

    def test_generic_compression_long_output(self):
        """Test generic compression keeps first and last lines."""
        adapter = RTKAdapter(enable=True)
        lines = [f"line{i}" for i in range(100)]
        output = "\n".join(lines)

        result = adapter.compress("unknown command", output)
        assert "unknown" in result.strategy
        assert result.compression_ratio < 1.0
        assert "line0" in result.output
        assert "line99" in result.output
        assert "omitted" in result.output

    def test_compression_ratio_calculation(self):
        """Test compression ratio is correctly calculated."""
        original = "x" * 400  # 100 tokens
        compressed = "y" * 40  # 10 tokens

        # Mock a filter that returns the compressed output
        class MockAdapter(RTKAdapter):
            def _generic_compress(self, output, max_lines=50):
                return compressed

        mock = MockAdapter()
        result = mock.compress("test", original)

        assert result.original_tokens == 100
        assert result.compressed_tokens == 10
        assert result.compression_ratio == 0.1

    def test_token_estimation(self):
        """Test token estimation (1 token ≈ 4 characters)."""
        adapter = RTKAdapter()
        # 4 characters = 1 token
        assert adapter._estimate_tokens("xxxx") == 1
        # 8 characters = 2 tokens
        assert adapter._estimate_tokens("xxxxxxxx") == 2
        # Empty returns at least 1
        assert adapter._estimate_tokens("") == 1

    def test_command_parsing(self):
        """Test command string parsing."""
        adapter = RTKAdapter()
        
        # Full command
        result = adapter.compress("git log --oneline --all", "line1\nline2" * 20)
        assert result.strategy == "git:log"
        
        # Single word
        result = adapter.compress("git", "content" * 50)
        # Should still recognize git but no subcommand
        assert result.strategy.startswith("git:")

    def test_fallback_to_generic(self):
        """Test fallback when no specific filter exists."""
        adapter = RTKAdapter()
        output = "\n".join([f"line{i}" for i in range(100)])
        
        result = adapter.compress("make build", output)
        # Should fallback to generic compression
        assert "make" in result.strategy or "*" in result.strategy
        assert result.compression_ratio < 1.0
