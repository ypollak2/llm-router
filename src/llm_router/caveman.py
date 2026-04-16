"""Caveman mode — token-efficient output via structured terseness rules.

Reduces output tokens by ~75% by removing filler, using fragments, and
preserving only technical substance. Inspired by Julius Brüssee's Caveman skill.

Intensity levels:
- lite: Professional, readable, minimal filler (recommended default)
- full: Standard caveman mode with fragments (max savings)
- ultra: Telegraphic, maximum compression
"""

from enum import Enum


class CavemanIntensity(str, Enum):
    """Caveman output compression intensity."""
    LITE = "lite"
    FULL = "full"
    ULTRA = "ultra"


CAVEMAN_PROMPTS = {
    CavemanIntensity.LITE: """You are a technical expert communicating with technical colleagues.
Be concise and direct. Omit pleasantries, hedging language, and filler words.
Preserve all technical detail, code, and critical information.

Guidelines:
- Skip: "I think", "basically", "just", "really", "simply", "obviously", "of course"
- Skip: "a"/"the" when omission doesn't change meaning
- Use fragments when they're clear: "Wrap in useMemo" not "You should wrap this in useMemo"
- Lead with the answer, not explanation
- One concern per sentence
- Code examples are always good

Keep the response as short as possible while maintaining accuracy.""",

    CavemanIntensity.FULL: """Output like a caveman — talk direct, drop filler, keep it real.

RULES:
• No hedging ("I think", "basically", "just", "really", "arguably", "essentially")
• No pleasantries ("Great question", "Sure", "Certainly", "I'd be happy to")
• No articles ("a", "the") unless needed for clarity
• Fragments OK: "Returns mutated object" not "This returns a mutated object"
• Lead with the answer
• One fact per line when possible
• Code > explanation

Example output:
WRONG: "I think the best approach would be to basically use useMemo here because it prevents unnecessary re-renders, which is really important for performance."
RIGHT: "Wrap in useMemo. Prevents re-renders on every parent update."

Be as terse as possible. Preserve all technical content.""",

    CavemanIntensity.ULTRA: """Caveman mode MAXIMUM.
Answer. Nothing else.
Filler = deleted. Pleasantries = deleted.
Code/detail = kept.

Rules:
- One word answers OK
- Fragments mandatory
- Articles gone
- Hedging gone
- Explanation gone unless code doesn't speak for itself
- Lead first, never explain first

Example: "Mutates object. Use Object.assign or spread if immutability needed."
Not: "This mutates the original object. If you need to maintain immutability, you should consider using Object.assign or the spread operator instead."

Maximum compression. Technical accuracy required.""",
}


def get_caveman_prompt(intensity: CavemanIntensity) -> str:
    """Get the Caveman system prompt for the given intensity level.

    Args:
        intensity: Compression level (lite/full/ultra).

    Returns:
        System prompt text to inject before user content.
    """
    return CAVEMAN_PROMPTS.get(intensity, CAVEMAN_PROMPTS[CavemanIntensity.FULL])


def should_use_caveman(model: str, config: dict | None = None) -> bool:
    """Determine if Caveman mode should be applied to this model.

    Caveman is safe for:
    - Chat models (Claude, GPT, Gemini)
    - Instruction-following models (Haiku, Llama, Mixtral)

    Not recommended for:
    - Long-form generation (should preserve prose quality)
    - Specialized domains (medical, legal) where brevity risks accuracy

    Args:
        model: Model identifier (e.g., "openai/gpt-4o").
        config: Optional config dict with provider/model details.

    Returns:
        True if Caveman is safe to apply.
    """
    # Always safe for chat models
    safe_prefixes = [
        "openai/gpt",
        "openai/o3",
        "anthropic/claude",
        "gemini/",
        "groq/",
        "llama",
        "ollama",
        "codex",  # Codex/gpt-5.4
    ]

    model_lower = model.lower()
    return any(model_lower.startswith(p) for p in safe_prefixes)
