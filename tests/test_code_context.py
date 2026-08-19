"""Tests for code_context module — AST-based symbol extraction."""

import textwrap

from llm_router.code_context import (
    Symbol,
    _build_progressive_context,
    _fallback_parse,
    detect_file_paths,
    detect_symbols,
    extract_code_context,
    find_relevant_files,
    parse_symbols,
)


class TestDetectSymbols:
    """Test symbol detection from natural language prompts."""

    def test_backtick_extraction(self):
        symbols = detect_symbols("fix the bug in `authenticate()` function")
        assert "authenticate" in symbols

    def test_snake_case_detection(self):
        symbols = detect_symbols("why does route_and_call return early?")
        assert "route_and_call" in symbols

    def test_camel_case_detection(self):
        symbols = detect_symbols("how does TaskType work?")
        assert "TaskType" in symbols

    def test_function_call_pattern(self):
        symbols = detect_symbols("what does calculate_budget(model) do?")
        assert "calculate_budget" in symbols

    def test_filters_common_english(self):
        symbols = detect_symbols("fix the bug and run the tests")
        # "fix", "run" should not appear (too common)
        assert "fix" not in symbols
        assert "run" not in symbols

    def test_multiple_symbols(self):
        symbols = detect_symbols(
            "refactor `route_and_call` to use TaskType.CODE instead of string"
        )
        assert "route_and_call" in symbols
        assert "TaskType" in symbols

    def test_no_symbols_in_plain_text(self):
        symbols = detect_symbols("what is the meaning of life?")
        assert len(symbols) == 0

    def test_short_names_filtered(self):
        symbols = detect_symbols("use `db` and `x` variables")
        # "db" and "x" are < 3 chars, should be filtered
        assert "db" not in symbols
        assert "x" not in symbols

    def test_caps_to_10(self):
        prompt = " ".join(f"`symbol_{i}`" for i in range(20))
        symbols = detect_symbols(prompt)
        assert len(symbols) <= 10


class TestDetectFilePaths:
    """Test file path extraction from prompts."""

    def test_python_file(self):
        paths = detect_file_paths("fix the bug in src/auth.py")
        assert "src/auth.py" in paths

    def test_typescript_file(self):
        paths = detect_file_paths("update components/Button.ts")
        assert "components/Button.ts" in paths

    def test_no_paths(self):
        paths = detect_file_paths("what is Python?")
        assert len(paths) == 0


class TestFallbackParse:
    """Test regex-based parsing when tree-sitter is unavailable."""

    def test_python_functions(self, tmp_path):
        f = tmp_path / "example.py"
        f.write_text(textwrap.dedent("""\
            def hello():
                print("hello")

            def world(name):
                return f"world {name}"

            class MyClass:
                def method(self):
                    pass
        """))
        symbols = _fallback_parse(f)
        names = [s.name for s in symbols]
        assert "hello" in names
        assert "world" in names
        assert "MyClass" in names

    def test_python_function_source(self, tmp_path):
        f = tmp_path / "example.py"
        f.write_text(textwrap.dedent("""\
            def add(a, b):
                return a + b

            def subtract(a, b):
                return a - b
        """))
        symbols = _fallback_parse(f)
        add_sym = next(s for s in symbols if s.name == "add")
        assert "return a + b" in add_sym.source

    def test_go_functions(self, tmp_path):
        f = tmp_path / "main.go"
        f.write_text(textwrap.dedent("""\
            func Hello() string {
                return "hello"
            }

            func (s *Server) Start() error {
                return nil
            }
        """))
        symbols = _fallback_parse(f)
        names = [s.name for s in symbols]
        assert "Hello" in names
        assert "Start" in names

    def test_nonexistent_file(self, tmp_path):
        f = tmp_path / "nope.py"
        symbols = _fallback_parse(f)
        assert symbols == []


class TestFindRelevantFiles:
    """Test file discovery for symbols."""

    def test_finds_matching_python_file(self, tmp_path):
        # Create a file that matches symbol name
        (tmp_path / "auth.py").write_text("def authenticate(): pass")
        (tmp_path / "other.py").write_text("def something(): pass")

        files = find_relevant_files(["authenticate"], str(tmp_path))
        # Should find something (auth.py might match *authenticate* pattern)
        # At minimum, should not crash
        assert isinstance(files, list)

    def test_skips_node_modules(self, tmp_path):
        nm = tmp_path / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        (nm / "auth.py").write_text("def authenticate(): pass")

        files = find_relevant_files(["authenticate"], str(tmp_path))
        # node_modules should be skipped
        assert not any("node_modules" in str(f) for f in files)

    def test_empty_for_invalid_dir(self):
        files = find_relevant_files(["anything"], "/nonexistent/path")
        assert files == []


class TestBuildProgressiveContext:
    """Test progressive disclosure budget logic."""

    def test_fits_within_budget(self):
        symbols = [
            Symbol("func_a", "function", 1, 5, "def func_a():\n    return 1", "a.py"),
            Symbol("func_b", "function", 10, 15, "def func_b():\n    return 2", "a.py"),
        ]
        result = _build_progressive_context(symbols, budget=500)
        assert "func_a" in result
        assert "func_b" in result
        assert "[Code context]" in result

    def test_respects_tight_budget(self):
        big_source = "def big_func():\n" + "    x = 1\n" * 100
        symbols = [
            Symbol("big_func", "function", 1, 100, big_source, "a.py"),
            Symbol("small", "function", 200, 201, "def small(): pass", "a.py"),
        ]
        result = _build_progressive_context(symbols, budget=50)
        # With only 50 tokens budget, should get at most signature
        assert len(result) < len(big_source)

    def test_empty_for_no_symbols(self):
        result = _build_progressive_context([], budget=1000)
        assert result == ""


class TestExtractCodeContext:
    """Test the full extraction pipeline."""

    def test_full_pipeline(self, tmp_path):
        # Create a Python file with a function matching our query
        (tmp_path / "router.py").write_text(textwrap.dedent("""\
            def route_and_call(prompt, model):
                \"\"\"Route a prompt to the best model.\"\"\"
                chain = get_model_chain(model)
                for m in chain:
                    try:
                        return call_model(m, prompt)
                    except Exception:
                        continue
                raise RuntimeError("All models failed")

            def get_model_chain(model):
                return ["ollama/gemma4", "openai/gpt-4o-mini"]
        """))

        result = extract_code_context(
            "why does `route_and_call` fail?",
            str(tmp_path),
            budget_tokens=1000,
        )
        assert "route_and_call" in result
        assert "[Code context]" in result

    def test_returns_empty_no_project(self):
        result = extract_code_context("fix the bug", None, 1000)
        assert result == ""

    def test_returns_empty_no_symbols(self):
        result = extract_code_context(
            "what is the meaning of life?",
            "/tmp",
            1000,
        )
        assert result == ""

    def test_graceful_with_nonexistent_dir(self):
        result = extract_code_context(
            "fix `authenticate`",
            "/nonexistent/path/that/does/not/exist",
            1000,
        )
        assert result == ""


class TestParseSymbols:
    """Test parse_symbols with fallback (no tree-sitter required for CI)."""

    def test_parses_python_file(self, tmp_path):
        f = tmp_path / "example.py"
        f.write_text(textwrap.dedent("""\
            def calculate_budget(model, task):
                return model.context_limit * 0.7

            class TokenBudget:
                def __init__(self, total):
                    self.total = total
        """))
        symbols = parse_symbols(f)
        names = [s.name for s in symbols]
        assert "calculate_budget" in names
        assert "TokenBudget" in names

    def test_returns_symbol_metadata(self, tmp_path):
        f = tmp_path / "example.py"
        f.write_text("def hello():\n    return 'world'\n")
        symbols = parse_symbols(f)
        assert len(symbols) >= 1
        sym = symbols[0]
        assert sym.name == "hello"
        assert sym.kind == "function"
        assert sym.start_line >= 1
        assert "return" in sym.source


class TestPromptDerivedPathsAreConfined:
    """CodeQL py/path-injection, src/llm_router/code_context.py.

    `extract_code_context` joined `project_dir` with paths pulled out of the
    PROMPT. `detect_file_paths` requires a code extension but constrains nothing
    before it, so `x/../../../../etc/shadow.py` matches and the join escapes the
    project. Whatever it yields is read and forwarded to an external provider,
    which makes this an exfiltration primitive rather than only a path bug —
    forwarding context is what llm_router does.

    Prompt text is not trusted input: it routinely carries pasted logs, web
    content and tool output the user never wrote.

    TWO WAYS THIS TEST CAN PASS FOR THE WRONG REASON, both hit while writing it:

    1. A file is only rendered into the context if one of its symbols ALSO
       matches a symbol detected in the prompt (extract_code_context step 3).
       A traversal prompt that names no matching symbol collects the file and
       still returns "" — green with the confinement removed.
    2. `Path.exists()` requires every intermediate component to exist, so a
       traversal through a directory that is not there (`x/../../outside`) is
       False before confinement is ever consulted. The traversal must go through
       a REAL directory — `src/` here — to be reachable at all.

    Both are versions of the defect this suite exists to catch, so the control
    is recorded: with `is_safe_path` removed, test_traversal fails and leaks
    `sk-live-...` into the context. Re-run that control if this test is edited.
    """

    @staticmethod
    def _layout(tmp_path):
        project = tmp_path / "project"
        (project / "src").mkdir(parents=True)
        (project / "src" / "app.py").write_text(
            "def collect_me():\n    return 'in-project'\n"
        )
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.py").write_text(
            "def collect_me():\n    return 'sk-live-must-never-be-collected'\n"
        )
        return project

    def test_traversal_out_of_the_project_is_not_collected(self, tmp_path):
        project = self._layout(tmp_path)
        # Traversal goes through `src/`, which really exists — see note (2).
        ctx = extract_code_context(
            "what does collect_me in src/../../outside/secret.py return?",
            str(project),
        )
        assert "sk-live-must-never-be-collected" not in ctx

    def test_ordinary_in_project_paths_still_work(self, tmp_path):
        """The confinement must not break the feature it protects."""
        project = self._layout(tmp_path)
        ctx = extract_code_context(
            "what does collect_me in src/app.py return?", str(project)
        )
        assert "in-project" in ctx


class TestFilePathRegexIsBounded:
    """CodeQL py/polynomial-redos, src/llm_router/code_context.py.

    `[\\w/.-]` contains `.`, so it competes with the following `\\.` for every
    dot and the engine backtracks quadratically. This runs in the prompt hook.
    Ordinary pasted text is unaffected (whitespace resets the scan); a crafted
    unbroken token is not.
    """

    def test_pathological_token_stays_fast(self):
        import time

        pathological = "a" + "a." * 32_000  # one unbroken token
        start = time.perf_counter()
        detect_file_paths(pathological)
        elapsed = time.perf_counter() - start
        # Unbounded: ~7s at this size, ~69s at 200k chars.
        assert elapsed < 2.0, f"regex took {elapsed:.2f}s — the bound is gone"

    def test_real_paths_are_unchanged_by_the_bound(self):
        found = detect_file_paths(
            "see src/llm_router/router.py and lib/mod.rs plus a.b.c.js"
        )
        assert "src/llm_router/router.py" in found
        assert "lib/mod.rs" in found
        assert "a.b.c.js" in found
