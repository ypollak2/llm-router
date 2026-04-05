# LLM Router — demo tape
# Generates: docs/images/demo.gif
# Run: vhs demo/demo.tape

Output docs/images/demo.gif

Set Shell "bash"
Set FontSize 13
Set FontFamily "JetBrains Mono"
Set Width 1100
Set Height 560
Set Theme "Dracula"
Set Padding 20
Set PlaybackSpeed 1.0
Set TypingSpeed 50ms
Set CursorBlink false

# ── Demo: routing decisions ───────────────────────────────────────────────────
Type "llm-router demo"
Sleep 300ms
Enter
Sleep 2500ms

# ── Doctor: health check ─────────────────────────────────────────────────────
Type "llm-router doctor"
Sleep 300ms
Enter
Sleep 2500ms

# ── Status: today's savings ──────────────────────────────────────────────────
Type "llm-router status"
Sleep 300ms
Enter
Sleep 2000ms

# end pause
Sleep 1500ms
