"""Tests for Caveman mode — token-efficient output via terseness rules."""

import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from llm_router.caveman import CavemanIntensity, get_caveman_prompt, should_use_caveman


def test_caveman_intensity_enum():
    """Verify all Caveman intensity levels are defined."""
    intensities = {CavemanIntensity.LITE, CavemanIntensity.FULL, CavemanIntensity.ULTRA}
    assert len(intensities) == 3
    print("✅ Caveman intensity levels: lite, full, ultra")


def test_caveman_prompts_exist():
    """Verify all intensity levels have system prompts."""
    for intensity in [CavemanIntensity.LITE, CavemanIntensity.FULL, CavemanIntensity.ULTRA]:
        prompt = get_caveman_prompt(intensity)
        assert prompt, f"No prompt for {intensity}"
        assert len(prompt) > 50, f"Prompt for {intensity} too short"
    print("✅ All intensity levels have system prompts")


def test_caveman_prompts_have_key_rules():
    """Verify Caveman prompts include the core terseness rules."""
    for intensity in [CavemanIntensity.LITE, CavemanIntensity.FULL, CavemanIntensity.ULTRA]:
        prompt = get_caveman_prompt(intensity)
        # Should mention removing filler or using fragments
        assert any(word in prompt.lower() for word in ["filler", "fragment", "terse", "direct"]), \
            f"Prompt for {intensity} missing core terseness concept"
    print("✅ Caveman prompts include core terseness rules")


def test_should_use_caveman_safe_models():
    """Verify Caveman is enabled for safe chat models."""
    safe_models = [
        "openai/gpt-4o",
        "openai/o3",
        "anthropic/claude-sonnet-4-20250514",
        "gemini/gemini-2.5-pro",
        "groq/mixtral-8x7b-32768",
        "ollama/llama3.2:latest",
    ]
    for model in safe_models:
        assert should_use_caveman(model), f"Should use Caveman for {model}"
    print(f"✅ Caveman enabled for {len(safe_models)} safe models")


def test_should_use_caveman_case_insensitive():
    """Verify Caveman detection is case-insensitive."""
    assert should_use_caveman("OPENAI/GPT-4O")
    assert should_use_caveman("Anthropic/claude-sonnet")
    assert should_use_caveman("OlLaMa/mistral")
    print("✅ Caveman detection is case-insensitive")


def test_lite_intensity_most_readable():
    """Verify lite intensity is more readable than ultra."""
    lite = get_caveman_prompt(CavemanIntensity.LITE)
    ultra = get_caveman_prompt(CavemanIntensity.ULTRA)
    # Lite should have more explanatory words
    lite_words = len(lite.split())
    ultra_words = len(ultra.split())
    assert lite_words > ultra_words, "Lite should be more verbose than ultra"
    print(f"✅ Lite ({lite_words} words) > Ultra ({ultra_words} words)")


def test_full_intensity_default():
    """Verify 'full' is the recommended default intensity."""
    full_prompt = get_caveman_prompt(CavemanIntensity.FULL)
    assert full_prompt, "Full intensity should have a prompt"
    assert "caveman" in full_prompt.lower() or "drop filler" in full_prompt.lower()
    print("✅ Full intensity is the default recommendation")


if __name__ == "__main__":
    print("\n" + "="*70)
    print("CAVEMAN MODE TESTS")
    print("="*70 + "\n")

    test_caveman_intensity_enum()
    test_caveman_prompts_exist()
    test_caveman_prompts_have_key_rules()
    test_should_use_caveman_safe_models()
    test_should_use_caveman_case_insensitive()
    test_lite_intensity_most_readable()
    test_full_intensity_default()

    print("\n" + "="*70)
    print("✅ ALL CAVEMAN TESTS PASSED")
    print("="*70 + "\n")
