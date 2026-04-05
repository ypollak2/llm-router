# LLM Router — demo tape
# Generates: docs/images/demo.gif
# Run: vhs demo/demo.tape

Output docs/images/demo.gif

Set Shell "bash"
Set FontSize 13
Set FontFamily "JetBrains Mono"
Set Width 920
Set Height 520
Set Theme "Dracula"
Set Padding 20
Set PlaybackSpeed 1.0
Set TypingSpeed 50ms
Set CursorBlink false

# ── Demo: routing decisions ───────────────────────────────────────────────────
Type "/Users/yali.pollak/.local/bin/llm-router demo"
Sleep 300ms
Enter
Sleep 2500ms

# ── Doctor: health check ─────────────────────────────────────────────────────
Type "/Users/yali.pollak/.local/bin/llm-router doctor"
Sleep 300ms
Enter
Sleep 2500ms

# ── Status: today's savings ──────────────────────────────────────────────────
Type "/Users/yali.pollak/.local/bin/llm-router status"
Sleep 300ms
Enter
Sleep 2000ms

# end pause
Sleep 1500ms
