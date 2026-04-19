# Token Compression System

LLM-Router implements a three-layer token compression pipeline to reduce context pollution and costs.

## Layer 1: RTK Command Output Compression

Automatically compresses shell command outputs before they reach the LLM context (80-90% reduction).

### Supported Commands

The RTKAdapter includes specialized filters for:

| Command | Filter | Behavior |
|---------|--------|----------|
| `git log` | Deduplication | Keep first 10 + last 5 commits |
| `git status` | Summarization | Branch info + file count summary |
| `git diff` | Header extraction | File names + change summary |
| `git branch` | Summarization | Current branch + total count |
| `pytest` | Aggregation | Pass/fail summary + failures only |
| `cargo build` | Error filtering | Errors only, skip warnings |
| `cargo test` | Aggregation | Test results summary |
| `docker ps` | Summarization | Container status summary |
| `docker logs` | Tail + error filter | Last 10 lines + errors |
| `npm test` | Aggregation | Test pass/fail summary |
| `uv run` | Error filtering | Errors only |
| Generic | Smart fallback | First 20 + last 20 lines |

### Usage

```python
from llm_router.compression import compress_command_output

# Compress git log output
result = compress_command_output(
    command="git log --oneline --all",
    output=very_long_git_output
)

print(f"Original: {result.original_tokens} tokens")
print(f"Compressed: {result.compressed_tokens} tokens")
print(f"Saved: {result.tokens_saved()} tokens ({result.compression_ratio:.1%})")
print(f"Strategy: {result.strategy}")
```

### Direct Access

```python
from llm_router.compression import RTKAdapter

adapter = RTKAdapter(enable=True)
result = adapter.compress("git log", output)
```

### Disable Compression

Set `enable=False` when initializing RTKAdapter:

```python
adapter = RTKAdapter(enable=False)  # Returns uncompressed
```

Or use the environment variable:

```bash
export LLM_ROUTER_COMPRESSION_LAYERS=router,token-savior  # Skip RTK
```

## Layer 2: Model Selection Routing

Chooses the optimal model for the task complexity (70-90% cost reduction).

See [routing.md](routing.md) for details.

## Layer 3: Response Compression (Future)

Planned: Compress LLM responses to remove verbosity (60-75% reduction).

Currently under development. Token-Savior integration planned for v6.3.

## Measurement

Token savings are tracked in the database and displayed in the `llm_gain` dashboard:

```bash
llm_gain              # Show savings dashboard
llm_gain --history    # Show command history with compression stats
```

## Example: Real-World Compression

### Before Compression

```bash
$ git log --oneline --all
commit1 (HEAD -> main) feat: add feature X
commit2 docs: update README
commit3 fix: bug Y
commit4 refactor: clean up code
... (495 more commits)
commit500 initial commit

Total: 500 commits, ~500 tokens
```

### After RTK Compression

```
commit1 (HEAD -> main) feat: add feature X
commit2 docs: update README
commit3 fix: bug Y
commit4 refactor: clean up code
commit5 feat: feature Z
commit6 fix: bug A
commit7 docs: API docs
commit8 refactor: module X
commit9 test: coverage
commit10 chore: deps

... (495 lines omitted) ...

commit500 initial commit
commit499 early work
commit498 proto version
commit497 experiment
commit496 WIP branch

Total: ~50 tokens (90% reduction)
```

## Architecture

The compression pipeline is designed to:
1. **Preserve information**: Keep what's important for reasoning
2. **Reduce noise**: Remove repetitive or low-value lines
3. **Maintain clarity**: Output remains human-readable
4. **Track metrics**: All compression is measured and reported

## Future Extensions

Planned additions:
- Custom compression rules per project
- Machine learning-based importance scoring
- Real-time dashboard showing compression effectiveness by command type
- User-configurable compression aggressiveness

## Configuration

Environment variables (future):

```bash
# Disable specific compression layers
export LLM_ROUTER_COMPRESSION_LAYERS=router,token-savior  # Skip RTK

# Set compression aggressiveness (0-100%)
export LLM_ROUTER_COMPRESSION_RATIO=80  # Target 80% reduction

# Enable detailed compression logging
export LLM_ROUTER_COMPRESSION_DEBUG=1
```
