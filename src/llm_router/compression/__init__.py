"""Token compression system for llm-router.

Three-layer compression pipeline:
1. RTK: Compress shell command outputs
2. Router: Choose model by complexity
3. Token-Savior: Compress LLM responses (future)
"""

from llm_router.compression.rtk_adapter import RTKAdapter, CompressionResult

__all__ = ["RTKAdapter", "CompressionResult", "compress_command_output"]


def compress_command_output(command: str, output: str, enabled: bool = True) -> CompressionResult:
    """Convenience function to compress command output.
    
    Args:
        command: Full command string (e.g., "git log --oneline")
        output: Command output to compress
        enabled: Whether compression is enabled (default: True)
    
    Returns:
        CompressionResult with compression metrics
    """
    adapter = RTKAdapter(enable=enabled)
    return adapter.compress(command, output)
