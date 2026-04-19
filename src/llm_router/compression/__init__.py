"""Token compression system for llm-router.

Three-layer compression pipeline:
1. RTK: Compress shell command outputs
2. Router: Choose model by complexity
3. Token-Savior: Compress LLM responses (future)
"""

from llm_router.compression.rtk_adapter import RTKAdapter

__all__ = ["RTKAdapter"]
