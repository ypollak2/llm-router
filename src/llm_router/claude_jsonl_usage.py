"""Read per-model token usage from Claude Code's local conversation JSONL files.

Claude Code stores every conversation in ~/.claude/projects/**/*.jsonl.
Each assistant message has message.usage with input_tokens, output_tokens,
cache_creation_input_tokens, and cache_read_input_tokens.
This is the same source the Claude Code Desktop app reads for its Models tab.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

_CC_DIR = Path.home() / ".claude" / "projects"

_SHORT_NAMES: dict[str, str] = {
    "claude-opus-4-8": "Opus 4.8",
    "claude-opus-4-7": "Opus 4.7",
    "claude-opus-4-5": "Opus 4.5",
    "claude-sonnet-4-6": "Sonnet 4.6",
    "claude-sonnet-4-5": "Sonnet 4.5",
    "claude-haiku-4-5-20251001": "Haiku 4.5",
    "claude-haiku-4-5": "Haiku 4.5",
    "claude-haiku-3-5": "Haiku 3.5",
}


def _shorten(model: str) -> str:
    if model in _SHORT_NAMES:
        return _SHORT_NAMES[model]
    # Fallback: strip "claude-" prefix
    return model.replace("claude-", "").replace("-", " ").title()


@dataclass
class ModelUsage:
    model: str
    display_name: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    turns: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class CCUsageSummary:
    models: list[ModelUsage] = field(default_factory=list)
    sessions: int = 0
    total_input: int = 0
    total_output: int = 0
    total_cache_read: int = 0

    @property
    def total_tokens(self) -> int:
        return self.total_input + self.total_output

    def pct_output(self, m: ModelUsage) -> float:
        if self.total_output == 0:
            return 0.0
        return m.output_tokens / self.total_output * 100


def read_cc_usage(cc_dir: Path | None = None) -> CCUsageSummary:
    """Aggregate per-model token counts from all local JSONL conversation files."""
    base = cc_dir or _CC_DIR
    if not base.exists():
        return CCUsageSummary()

    model_data: dict[str, ModelUsage] = {}
    session_ids: set[str] = set()

    for jsonl_file in base.rglob("*.jsonl"):
        session_ids.add(jsonl_file.stem)
        try:
            with open(jsonl_file, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                        msg = d.get("message", {})
                        model = msg.get("model", "")
                        usage = msg.get("usage")
                        if not model or not usage:
                            continue
                        inp = int(usage.get("input_tokens") or 0)
                        out = int(usage.get("output_tokens") or 0)
                        cr  = int(usage.get("cache_read_input_tokens") or 0)
                        cw  = int(usage.get("cache_creation_input_tokens") or 0)
                        if inp == 0 and out == 0:
                            continue
                        if model not in model_data:
                            model_data[model] = ModelUsage(
                                model=model,
                                display_name=_shorten(model),
                            )
                        m = model_data[model]
                        m.input_tokens += inp
                        m.output_tokens += out
                        m.cache_read_tokens += cr
                        m.cache_write_tokens += cw
                        m.turns += 1
                    except (json.JSONDecodeError, KeyError, TypeError):
                        pass
        except OSError:
            pass

    models = sorted(model_data.values(), key=lambda m: -m.output_tokens)
    total_input  = sum(m.input_tokens for m in models)
    total_output = sum(m.output_tokens for m in models)
    total_cache  = sum(m.cache_read_tokens for m in models)

    return CCUsageSummary(
        models=models,
        sessions=len(session_ids),
        total_input=total_input,
        total_output=total_output,
        total_cache_read=total_cache,
    )
