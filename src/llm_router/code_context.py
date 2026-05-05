"""AST-based code context extraction for routed prompts.

When routing code tasks to cheap models (Ollama, Haiku, Gemini Flash),
this module extracts precise code context — function bodies, callers,
and related tests — so the model has exactly what it needs.

Uses tree-sitter for AST parsing (optional dependency). Falls back
gracefully when tree-sitter is not installed.

Progressive disclosure strategy:
  Layer 1: Function signatures only (~15 tokens each)
  Layer 2: Function bodies (~50-200 tokens each)
  Layer 3: Callers + related references (~50-100 tokens each)
  Layer 4: Related test functions

Each layer is added only if the token budget allows.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from llm_router.token_budget import estimate_tokens

log = logging.getLogger("llm_router.code_context")

# ── Tree-sitter availability ──────────────────────────────────────────────────
try:
    import tree_sitter_languages  # noqa: F401
    HAS_TREESITTER = True
except ImportError:
    HAS_TREESITTER = False

# ── Language detection ────────────────────────────────────────────────────────
_EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".swift": "swift",
    ".kt": "kotlin",
    ".scala": "scala",
}

# ── Symbol detection patterns ─────────────────────────────────────────────────
# Patterns for extracting symbol names from natural language prompts
_BACKTICK_RE = re.compile(r"`([a-zA-Z_]\w*(?:\(\))?)`")
_SNAKE_CASE_RE = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b")
_CAMEL_CASE_RE = re.compile(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b")
_FUNCTION_CALL_RE = re.compile(r"\b([a-zA-Z_]\w*)\s*\(")
_FILE_PATH_RE = re.compile(r"([a-zA-Z_][\w/.-]*\.(?:py|ts|js|go|rs|java|rb))")


@dataclass(frozen=True)
class Symbol:
    """A code symbol extracted from AST."""

    name: str
    kind: str  # function, class, method
    start_line: int
    end_line: int
    source: str
    file_path: str

    @property
    def signature(self) -> str:
        """First line of the symbol (typically the def/function line)."""
        first_line = self.source.split("\n")[0]
        return first_line

    @property
    def token_estimate(self) -> int:
        return estimate_tokens(self.source)


@dataclass
class CodeContext:
    """Assembled code context ready for prompt injection."""

    symbols: list[Symbol] = field(default_factory=list)
    callers: list[str] = field(default_factory=list)
    total_tokens: int = 0


def detect_symbols(prompt: str) -> list[str]:
    """Extract likely symbol names from a natural language prompt.

    Uses multiple strategies in priority order:
    1. Backtick-quoted names: `authenticate()`
    2. Snake_case identifiers: route_and_call
    3. CamelCase identifiers: TaskType
    4. Function call patterns: authenticate(
    5. File paths: auth.py → look for symbols in that file

    Returns:
        Ranked list of likely symbol names (most confident first).
    """
    symbols: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        clean = name.rstrip("()")
        if clean and clean not in seen and len(clean) >= 3:
            seen.add(clean)
            symbols.append(clean)

    # 1. Backtick-quoted (highest confidence)
    for m in _BACKTICK_RE.finditer(prompt):
        _add(m.group(1))

    # 2. Snake_case
    for m in _SNAKE_CASE_RE.finditer(prompt):
        _add(m.group(1))

    # 3. CamelCase
    for m in _CAMEL_CASE_RE.finditer(prompt):
        _add(m.group(1))

    # 4. Function calls (lower confidence — might be English)
    for m in _FUNCTION_CALL_RE.finditer(prompt):
        name = m.group(1)
        # Skip common English words that match the pattern
        if name.lower() not in {"the", "and", "for", "with", "from", "that", "this",
                                  "what", "how", "why", "when", "where", "which",
                                  "does", "can", "will", "should", "would", "could",
                                  "fix", "add", "use", "get", "set", "run", "make"}:
            _add(name)

    return symbols[:10]  # Cap at 10 symbols


def detect_file_paths(prompt: str) -> list[str]:
    """Extract file paths mentioned in the prompt."""
    return [m.group(1) for m in _FILE_PATH_RE.finditer(prompt)]


def find_relevant_files(
    symbols: list[str],
    project_dir: str,
    max_files: int = 10,
) -> list[Path]:
    """Find files likely containing the given symbols.

    Searches the project directory for files matching symbol patterns.
    """
    project = Path(project_dir)
    if not project.is_dir():
        return []

    found: list[Path] = []
    seen: set[Path] = set()

    for symbol in symbols:
        # Convert symbol to possible file patterns
        snake = _to_snake(symbol)
        patterns = [
            f"**/{snake}.py",
            f"**/*{snake}*.py",
            f"**/test_{snake}*.py",
            f"**/{snake}.ts",
            f"**/{snake}.go",
        ]
        for pattern in patterns:
            for f in project.glob(pattern):
                if f not in seen and not _should_skip(f):
                    seen.add(f)
                    found.append(f)
                    if len(found) >= max_files:
                        return found

    return found


def parse_symbols(file_path: Path) -> list[Symbol]:
    """Parse a file and extract all function/class symbols using tree-sitter.

    Returns empty list if tree-sitter is not available or file can't be parsed.
    """
    if not HAS_TREESITTER:
        return _fallback_parse(file_path)

    lang = _detect_language(file_path)
    if not lang:
        return []

    try:
        import tree_sitter_languages
        parser = tree_sitter_languages.get_parser(lang)
        source = file_path.read_bytes()
        tree = parser.parse(source)
        return _extract_symbols(tree.root_node, source, str(file_path), lang)
    except Exception as e:
        log.debug("Tree-sitter parse failed for %s: %s", file_path, e)
        return _fallback_parse(file_path)


def extract_code_context(
    prompt: str,
    project_dir: str | None,
    budget_tokens: int = 2000,
) -> str:
    """Extract relevant code context for a prompt within token budget.

    This is the main entry point. It:
    1. Detects symbols in the prompt
    2. Finds relevant files
    3. Parses them for matching symbols
    4. Builds progressive context within budget

    Args:
        prompt: User's prompt (for symbol detection).
        project_dir: Project root directory.
        budget_tokens: Maximum tokens for code context.

    Returns:
        Formatted code context string, or empty string if nothing found.
    """
    if not project_dir:
        return ""

    # Step 1: Detect symbols
    symbols = detect_symbols(prompt)
    file_paths = detect_file_paths(prompt)

    if not symbols and not file_paths:
        return ""

    # Step 2: Find relevant files
    files = find_relevant_files(symbols, project_dir)

    # Also add explicitly mentioned files
    project = Path(project_dir)
    for fp in file_paths:
        full = project / fp
        if full.exists():
            files.append(full)

    # If no files found by name, try all Python/TS/Go files in project root
    if not files:
        project = Path(project_dir)
        for ext in (".py", ".ts", ".go"):
            for f in sorted(project.glob(f"*{ext}"))[:5]:
                if not _should_skip(f):
                    files.append(f)

    if not files:
        return ""

    # Step 3: Parse and find matching symbols
    all_symbols: list[Symbol] = []
    for f in files[:10]:
        parsed = parse_symbols(f)
        for sym in parsed:
            if any(s.lower() in sym.name.lower() for s in symbols):
                all_symbols.append(sym)

    if not all_symbols:
        return ""

    # Step 4: Progressive disclosure within budget
    return _build_progressive_context(all_symbols, budget_tokens)


# ── Private helpers ───────────────────────────────────────────────────────────


def _build_progressive_context(symbols: list[Symbol], budget: int) -> str:
    """Build context with progressive disclosure layers."""
    parts: list[str] = []
    tokens_used = 0
    header = "[Code context]\n"
    tokens_used += estimate_tokens(header)

    # Sort: exact matches first, then by size (smaller = more likely to fit)
    symbols.sort(key=lambda s: s.token_estimate)

    for sym in symbols:
        # Layer 1: Try full source
        entry = f"# {sym.file_path}:{sym.start_line} ({sym.kind})\n{sym.source}\n\n"
        entry_tokens = estimate_tokens(entry)

        if tokens_used + entry_tokens <= budget:
            parts.append(entry)
            tokens_used += entry_tokens
        elif tokens_used + estimate_tokens(sym.signature) + 20 <= budget:
            # Layer fallback: signature only
            sig_entry = f"# {sym.file_path}:{sym.start_line} ({sym.kind})\n{sym.signature}\n\n"
            parts.append(sig_entry)
            tokens_used += estimate_tokens(sig_entry)

        if tokens_used >= budget * 0.9:
            break

    if not parts:
        return ""

    return header + "".join(parts).rstrip()


def _extract_symbols(
    node, source: bytes, file_path: str, lang: str,
) -> list[Symbol]:
    """Walk tree-sitter AST and extract function/class definitions."""
    symbols: list[Symbol] = []

    # Node types that represent definitions per language
    def_types = {
        "python": ("function_definition", "class_definition"),
        "typescript": ("function_declaration", "class_declaration", "method_definition",
                       "arrow_function", "export_statement"),
        "tsx": ("function_declaration", "class_declaration", "method_definition"),
        "javascript": ("function_declaration", "class_declaration", "method_definition"),
        "go": ("function_declaration", "method_declaration"),
        "rust": ("function_item", "impl_item"),
        "java": ("method_declaration", "class_declaration"),
    }

    target_types = def_types.get(lang, ("function_definition", "class_definition"))

    def _walk(n):
        if n.type in target_types:
            # Extract name
            name = _get_node_name(n, lang)
            if name:
                start = n.start_point[0]
                end = n.end_point[0]
                src = source[n.start_byte:n.end_byte].decode("utf-8", errors="replace")
                kind = "function" if "function" in n.type or "method" in n.type else "class"
                symbols.append(Symbol(
                    name=name,
                    kind=kind,
                    start_line=start + 1,
                    end_line=end + 1,
                    source=src,
                    file_path=file_path,
                ))
        for child in n.children:
            _walk(child)

    _walk(node)
    return symbols


def _get_node_name(node, lang: str) -> str | None:
    """Extract the name identifier from a definition node."""
    for child in node.children:
        if child.type == "identifier":
            return child.text.decode("utf-8") if isinstance(child.text, bytes) else child.text
        if child.type == "name":  # Python class/function name
            return child.text.decode("utf-8") if isinstance(child.text, bytes) else child.text
        # TypeScript/JS: look for property_identifier or identifier
        if child.type in ("property_identifier", "type_identifier"):
            return child.text.decode("utf-8") if isinstance(child.text, bytes) else child.text
    return None


def _fallback_parse(file_path: Path) -> list[Symbol]:
    """Regex-based fallback when tree-sitter is unavailable.

    Less precise but catches common patterns in Python, TS, Go.
    """
    try:
        source = file_path.read_text(errors="replace")
    except (OSError, UnicodeDecodeError):
        return []

    symbols: list[Symbol] = []
    lines = source.split("\n")
    ext = file_path.suffix

    if ext == ".py":
        pattern = re.compile(r"^(?:def|class)\s+(\w+)")
    elif ext in (".ts", ".js", ".tsx", ".jsx"):
        pattern = re.compile(r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)|^(?:export\s+)?class\s+(\w+)")
    elif ext == ".go":
        pattern = re.compile(r"^func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)")
    else:
        return []

    i = 0
    while i < len(lines):
        m = pattern.match(lines[i])
        if m:
            name = next((g for g in m.groups() if g), None)
            if name:
                # Find end of block (next definition or significant dedent)
                end = _find_block_end(lines, i, ext)
                block_source = "\n".join(lines[i:end])
                kind = "class" if "class" in lines[i][:10] else "function"
                symbols.append(Symbol(
                    name=name,
                    kind=kind,
                    start_line=i + 1,
                    end_line=end,
                    source=block_source,
                    file_path=str(file_path),
                ))
        i += 1

    return symbols


def _find_block_end(lines: list[str], start: int, ext: str) -> int:
    """Find the end of a code block starting at `start`."""
    if ext == ".py":
        # Python: indent-based
        if start + 1 >= len(lines):
            return start + 1
        # Find the indentation of the first line of the body
        body_start = start + 1
        while body_start < len(lines) and not lines[body_start].strip():
            body_start += 1
        if body_start >= len(lines):
            return len(lines)
        body_indent = len(lines[body_start]) - len(lines[body_start].lstrip())
        for i in range(body_start + 1, min(len(lines), start + 200)):
            line = lines[i]
            if line.strip() and (len(line) - len(line.lstrip())) < body_indent:
                return i
        return min(len(lines), start + 200)
    else:
        # Brace languages: count braces
        depth = 0
        for i in range(start, min(len(lines), start + 200)):
            depth += lines[i].count("{") - lines[i].count("}")
            if depth <= 0 and i > start:
                return i + 1
        return min(len(lines), start + 200)


def _detect_language(file_path: Path) -> str | None:
    """Detect tree-sitter language name from file extension."""
    return _EXT_TO_LANG.get(file_path.suffix.lower())


def _to_snake(name: str) -> str:
    """Convert CamelCase to snake_case."""
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def _should_skip(path: Path) -> bool:
    """Skip files that shouldn't be parsed (node_modules, .venv, etc)."""
    skip_dirs = {"node_modules", ".venv", "__pycache__", ".git", "dist", "build"}
    return any(part in skip_dirs for part in path.parts)
