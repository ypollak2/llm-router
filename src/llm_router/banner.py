"""LLM Router startup banner — painterly ASCII art of the river confluence.

LLM Router (Dzongkha ཆུ་ཛོམ་, also Chhuzom) is the river confluence in
western Bhutan where the **Paro Chhu** and the **Thimphu Chhu** meet to
form the **Wang Chhu** — and the literal gateway to Thimphu. Three
stupas guard the junction: one Bhutanese (chorten), one Tibetan
(square base), and one Nepali (with eyes). Together they ward off the
inauspicious energies of converging roads and rivers.

The banner reproduces that image in 24-bit ANSI. The composition reads
top-to-bottom:

* Snow-capped Himalayan peaks (cool blue-white gradient)
* Mid-range mountains (deeper blue)
* Three stupas standing in front of the confluence
* Two rivers joining into one beneath them
* Wang Chhu flowing out as the routing intelligence "thread" of LLM Router

We expose two surfaces:

* :func:`render_banner` returns the full multi-line string, ready to
  ``print`` to stderr at SessionStart.
* :func:`render_compact_banner` returns a one-line variant for the
  statusline / hook output where vertical space is rationed.

Both are pure functions — no I/O, no global state, no env reads — so
``hooks/session-start.py`` can call them and decide where to render.
"""

from __future__ import annotations

# ── 24-bit ANSI helpers ────────────────────────────────────────────────────
# Truecolor escapes; degrades gracefully on terminals that strip them
# (the underlying block characters still draw a recognisable picture).

_RESET = "\033[0m"
_BOLD = "\033[1m"


#: The wordmark, SPACED FROM A CONSTANT rather than written out letter by
#: letter.
#:
#: It used to be the literal `C  H  U  Z  O  M`. That form contains no
#: substring `llm_router`, so nothing that searches for the brand can see it —
#: not the downstream sync's rewrite, and not the identity gate that exists
#: to catch exactly this. The banner kept printing the upstream brand in a
#: redistribution while every automated check reported clean.
#:
#: Deriving it from the package name means the rewrite reaches it like any
#: other occurrence, and the gate can find it if it ever drifts again.
_BRAND = "llm_router"
_WORDMARK = "  ".join(_BRAND.upper())


def _fg(r: int, g: int, b: int) -> str:
    return f"\033[38;2;{r};{g};{b}m"


# Palette — picked to evoke a Bhutanese landscape painting:
#   snow:   high luminance, slight cool tint
#   peak:   cold blue for distant ridges
#   stone:  warmer slate for mid-mountains
#   stupa:  off-white for the chortens
#   gold:   the harmika / spire (Bhutanese accent)
#   river:  glacial turquoise → indigo for the chhu
#   wang:   deep teal for the unified Wang Chhu thread
_SNOW = _fg(238, 244, 252)
_PEAK = _fg(110, 145, 192)
_RIDGE = _fg(75, 105, 150)
_STONE = _fg(95, 90, 110)
_STUPA = _fg(238, 230, 215)
_STUPA_SHADOW = _fg(170, 160, 145)
_GOLD = _fg(212, 175, 55)
_RIVER_HI = _fg(126, 200, 220)
_RIVER_LO = _fg(70, 130, 175)
_WANG = _fg(40, 95, 130)
_INK = _fg(50, 50, 70)
_TEXT = _fg(220, 220, 230)
_TEXT_DIM = _fg(140, 145, 160)
_ACCENT = _fg(242, 200, 100)


def render_banner() -> str:
    """Return the full painterly ASCII banner.

    Loads the canonical art from ``banner_art.txt`` (a hand-drawn
    confluence stippling delivered by the user) and dresses it with a
    river-blue gradient + the gold wordmark below. The .txt source is
    kept on disk so future edits to the painting don't require touching
    Python — anyone can iterate the composition with a text editor.

    Falls back to the procedurally-rendered painting when the .txt file
    is missing (e.g. stripped sdist installs), so SessionStart still
    gets a banner rather than a stack trace.
    """
    art = _load_painting()
    if art is not None:
        return _frame_painting(art)
    return _render_procedural_painting()


def _load_painting() -> str | None:
    """Read the canonical ASCII source. Returns ``None`` when missing.

    Path resolves relative to this module so the file ships in the
    wheel without needing a manifest entry. importlib.resources would
    be more orthodox, but the direct read is simpler and fine for a
    small text file alongside the source.
    """
    try:
        from pathlib import Path
        return (Path(__file__).resolve().parent / "banner_art.txt").read_text(
            encoding="utf-8"
        )
    except OSError:
        return None


def _frame_painting(art: str) -> str:
    """Apply the river-blue gradient + wordmark trim to the raw painting.

    Trims the uniform left-padding the source uses for centering, then
    runs a depth gradient (glacial turquoise → deep teal) across the
    non-blank lines so the confluence reads as water on a 24-bit
    terminal. Terminals that strip colour still see the painting at
    full fidelity.

    Order matters: trim *before* colouring so the ANSI prefixes don't
    have to be parsed back out. Variable-length escape sequences
    (``\\033[38;2;r;g;bm``) make post-colour string slicing fragile.
    """
    raw_lines = art.splitlines()

    # Strip uniform leading whitespace so the painting hugs column 0
    # in narrow terminals. Computed over non-empty lines only — blank
    # lines are preserved as-is.
    min_pad = min(
        (len(line) - len(line.lstrip(" "))
         for line in raw_lines if line.strip()),
        default=0,
    )
    trimmed = [line[min_pad:] if line.strip() else line for line in raw_lines]

    # Identify the glyph band so the depth gradient flows across the
    # painting itself, not across leading / trailing blank lines.
    first = next(
        (i for i, line in enumerate(trimmed) if line.strip()),
        0,
    )
    last = len(trimmed) - 1 - next(
        (i for i, line in enumerate(reversed(trimmed)) if line.strip()),
        0,
    )
    span = max(1, last - first)

    coloured: list[str] = []
    for idx, line in enumerate(trimmed):
        if not line.strip():
            coloured.append(line)
            continue
        depth = (idx - first) / span
        depth = max(0.0, min(1.0, depth))
        r1, g1, b1 = (126, 200, 220)  # glacial turquoise at the surface
        r2, g2, b2 = (40, 95, 130)    # deep teal at the floor
        r = int(r1 + (r2 - r1) * depth)
        g = int(g1 + (g2 - g1) * depth)
        b = int(b1 + (b2 - b1) * depth)
        coloured.append(f"{_fg(r, g, b)}{line}{_RESET}")

    wordmark = (
        f"\n           {_BOLD}{_ACCENT}⚡ {_WORDMARK} ⚡{_RESET}"
        f"   {_TEXT_DIM}— meeting of rivers, routing intelligence{_RESET}\n"
        f"           {_TEXT_DIM}three stupas guard every confluence  ·  every prompt finds its current{_RESET}"
    )
    return "\n".join(coloured) + wordmark


def _render_procedural_painting() -> str:
    """Fallback painting — used when banner_art.txt is missing.

    Same composition as the original procedural banner: three peaks,
    three stupas, two rivers converging into Wang Chhu, wordmark below.
    """
    # Build line-by-line so the composition is editable without parsing
    # multi-line strings. Each line is constructed left-to-right with
    # palette codes interleaved into the block characters.

    lines: list[str] = []

    # ── Sky + distant Himalayan peaks ────────────────────────────────
    # Three peaks suggest the three rivers' watersheds; the rightmost
    # is Jomolhari, sacred to the Paro valley.
    lines.append(
        f"{_SNOW}            ▲                  ▲                       ▲              {_RESET}"
    )
    lines.append(
        f"{_SNOW}           ▲█▲                ▲█▲                     ▲█▲             {_RESET}"
    )
    lines.append(
        f"{_PEAK}         ▲{_SNOW}███{_PEAK}▲              ▲{_SNOW}███{_PEAK}▲                 ▲{_SNOW}███{_PEAK}▲           {_RESET}"
    )
    lines.append(
        f"{_PEAK}       ▲█████▲            ▲█████▲               ▲█████▲         {_RESET}"
    )
    lines.append(
        f"{_RIDGE}     ▲{_PEAK}███████{_RIDGE}▲▄▄▄▄▄▄▄▄▄{_PEAK}▲█████████▲▄▄▄▄▄▄▄▄▄▄▄▄{_PEAK}▲███████▲       {_RESET}"
    )
    lines.append(
        f"{_RIDGE}   ▄▄███████████████████████████████████████████████████████▄▄   {_RESET}"
    )
    lines.append(
        f"{_STONE} ▄████████████████████████████████████████████████████████████▄ {_RESET}"
    )

    # ── Mid-range valley shadow (gives depth to the stupas)
    lines.append(
        f"{_INK}                                                                          {_RESET}"
    )

    # ── Three stupas: Bhutanese (chorten) · Tibetan (square) · Nepali (eyes) ──
    # Each is centred over its column on the river path below.
    lines.append(
        f"             {_GOLD}╿{_STUPA}      "      # Bhutanese: tall slim spire on a small dome
        f"          {_GOLD}╴┴╴{_STUPA}         "  # Tibetan: square step pyramid with finial
        f"      {_GOLD}╱╲{_STUPA}              {_RESET}"  # Nepali: with stylised eyes
    )
    lines.append(
        f"            {_STUPA}╔╧╗               ╔═╧═╗            ╱┃┃╲             {_RESET}"
    )
    lines.append(
        f"          {_STUPA}▄{_STUPA_SHADOW}█{_STUPA}███{_STUPA_SHADOW}█{_STUPA}▄          "  # Bhutanese dome
        f"   {_STUPA}▄{_STUPA_SHADOW}█{_STUPA}██████{_STUPA_SHADOW}█{_STUPA}▄        "      # Tibetan dome
        f"   {_STUPA}▄{_STUPA_SHADOW}█{_STUPA}{_INK}◕ ◕{_STUPA}{_STUPA_SHADOW}█{_STUPA}▄         {_RESET}"  # Nepali (with eyes)
    )
    lines.append(
        f"         {_STUPA_SHADOW}██{_STUPA}███████{_STUPA_SHADOW}██        "
        f"{_STUPA_SHADOW}██{_STUPA}████████{_STUPA_SHADOW}██     "
        f"  {_STUPA_SHADOW}██{_STUPA}████████{_STUPA_SHADOW}██        {_RESET}"
    )
    lines.append(
        f"       {_STUPA_SHADOW}▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀{_RESET}"
    )

    # ── Two rivers converging into one ───────────────────────────────
    # Top of the river system: two streams diverge to either side, then
    # arc back into the centre channel labelled Wang Chhu.
    lines.append(
        f"   {_TEXT_DIM}Paro Chhu {_RIVER_HI}▒▒▒▓▓▓▓▒▒▒▒▒{_RIVER_LO}▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▓▓▓▓▒▒▒ "
        f"{_TEXT_DIM}Thimphu Chhu{_RESET}"
    )
    lines.append(
        f"           {_RIVER_HI}╲▒▒▒▒▒▒▓▓▓▓▒▒▒▒▒▒▒▒▒{_WANG}▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▓▓▓▓▒▒▒▒▒▒▒▒╱{_RESET}"
    )
    lines.append(
        f"             {_RIVER_LO}╲▒▒▒▒▒▒▒▒▒▒▒▒▒▒▓▓▓{_WANG}▓▓▓▓▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒╱{_RESET}"
    )
    lines.append(
        f"               {_WANG}╲▒▒▒▒▒▒▒▒▒▒▒▓▓▓▓▓▓▓▓▓▓▓▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒╱{_RESET}"
    )
    lines.append(
        f"                  {_WANG}╲▒▒▒▒▒▓▓▓▓▓▓███▓▓▓▓▓▓▒▒▒▒▒▒▒▒▒▒▒▒▒╱{_RESET}"
    )
    lines.append(
        f"                     {_WANG}▼▒▓▓▓▓████{_BOLD}WANG CHHU{_RESET}{_WANG}████▓▓▓▒▼{_RESET}"
    )

    # ── Wordmark + tagline ───────────────────────────────────────────
    lines.append("")
    lines.append(
        f"           {_BOLD}{_ACCENT}⚡ {_WORDMARK} ⚡{_RESET}"
        f"   {_TEXT_DIM}— meeting of rivers, routing intelligence{_RESET}"
    )
    lines.append(
        f"           {_TEXT_DIM}three stupas guard every confluence  ·  every prompt finds its current{_RESET}"
    )

    return "\n".join(lines)


def render_compact_banner() -> str:
    """One-line variant for hooks/statusline contexts.

    Uses a small subset of the painterly palette so it stands out in a
    status bar without bleeding into surrounding text.
    """
    return (
        f"{_BOLD}{_ACCENT}⚡ LLM_ROUTER{_RESET}  "
        f"{_RIVER_HI}╲{_RIVER_LO}╱{_RESET}  "
        f"{_TEXT_DIM}meeting of rivers · routing intelligence{_RESET}"
    )


if __name__ == "__main__":
    # ``python -m llm_router.banner`` prints the full banner — useful for
    # palette/composition iteration without restarting Claude Code.
    print(render_banner())
    print()
    print(render_compact_banner())
