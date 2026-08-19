"""Share command — generate a shareable savings card.

Two output modes:

* ``llm_router share``          — prints an ANSI card, copies plain text to the
                              clipboard, and opens a one-click tweet intent.
* ``llm_router share --svg``    — writes a self-contained SVG card to disk
                              (default ``~/.llm-router/savings-card.svg``) that can
                              be embedded in a README or posted to social. Zero
                              external dependencies — SVG is generated as text.
"""

from __future__ import annotations

import html
import os
import sqlite3
import subprocess
import sys
import urllib.parse
import webbrowser
from dataclasses import dataclass

# Baseline = always-Sonnet (the model a developer's tool would otherwise pick).
#
# WP-03: was a hardcoded 3.0/15.0. This command renders a shareable savings
# card, so a stale rate here is published rather than merely displayed. It also
# would not have tracked Sonnet 5's introductory pricing, which is live right
# now — the card would have overstated the baseline, and therefore the saving,
# by 50%.
from llm_router import pricing as _pricing  # noqa: E402

_sonnet = _pricing.price_for("sonnet")
SONNET_IN, SONNET_OUT = (_sonnet.input, _sonnet.output) if _sonnet else (0.0, 0.0)
FREE_PROVIDERS = {"ollama", "codex", "gemini_cli"}
DEFAULT_SVG_PATH = os.path.expanduser("~/.llm-router/savings-card.svg")
REPO = "github.com/ypollak2/llm_router"


# ── ANSI helpers (respect NO_COLOR / non-tty) ─────────────────────────────────

def _color_enabled() -> bool:
    return sys.stdout.isatty() and not os.getenv("NO_COLOR")


def _green(s: str) -> str:
    return f"\033[32m{s}\033[0m" if _color_enabled() else s


def _yellow(s: str) -> str:
    return f"\033[33m{s}\033[0m" if _color_enabled() else s


def _dim(s: str) -> str:
    return f"\033[2m{s}\033[0m" if _color_enabled() else s


# ── Stats model ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SavingsStats:
    """All-time savings rollup, shared by the ANSI and SVG renderers."""

    total_calls: int = 0
    paid_calls: int = 0
    free_calls: int = 0
    total_saved: float = 0.0
    savings_pct: int = 0
    top_model: str = "—"


def _gather_stats(db_path: str | None = None) -> SavingsStats:
    """Compute all-time savings vs an always-Sonnet baseline.

    Reads ``usage.db`` defensively: a missing or unreadable DB yields an
    all-zero ``SavingsStats`` rather than raising, so the card always renders.
    """
    if db_path is None:
        db_path = os.path.join(os.path.expanduser("~/.llm-router"), "usage.db")

    if not os.path.exists(db_path):
        return SavingsStats()

    total_calls = paid_calls = free_calls = 0
    total_saved = actual_paid = 0.0
    top_model = "—"

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT provider, input_tokens, output_tokens, cost_usd FROM usage "
                "WHERE success=1"
            ).fetchall()
            for r in rows:
                prov = r["provider"] or ""
                in_tok = r["input_tokens"] or 0
                out_tok = r["output_tokens"] or 0
                cost = r["cost_usd"] or 0.0
                total_calls += 1
                base = (in_tok * SONNET_IN + out_tok * SONNET_OUT) / 1_000_000
                if prov in FREE_PROVIDERS:
                    free_calls += 1
                    total_saved += base
                elif prov != "subscription":
                    paid_calls += 1
                    actual_paid += cost
                    total_saved += max(0.0, base - cost)

            top_row = conn.execute(
                "SELECT model, COUNT(*) as n FROM usage "
                "WHERE success=1 AND provider NOT IN ('subscription','ollama','codex') "
                "GROUP BY model ORDER BY n DESC LIMIT 1"
            ).fetchone()
            if top_row and top_row["model"]:
                m = top_row["model"]
                top_model = m.split("/")[-1] if "/" in m else m
                if len(top_model) > 24:
                    top_model = top_model[:22] + "…"
        finally:
            conn.close()
    except Exception:
        # Corrupt/locked DB → render zeros rather than crash the command.
        return SavingsStats()

    savings_pct = 0
    total_baseline = total_saved + actual_paid
    if total_baseline > 0:
        savings_pct = round(total_saved / total_baseline * 100)

    return SavingsStats(
        total_calls=total_calls,
        paid_calls=paid_calls,
        free_calls=free_calls,
        total_saved=total_saved,
        savings_pct=savings_pct,
        top_model=top_model,
    )


# ── Command entry point ────────────────────────────────────────────────────────

def cmd_share(args: list[str]) -> int:
    """Entry point for ``llm_router share`` and ``llm_router share --svg [path]``."""
    if args and args[0] == "--svg":
        out_path = args[1] if len(args) > 1 else DEFAULT_SVG_PATH
        return _write_svg(out_path)
    _run_share()
    return 0


# ── Helpers ────────────────────────────────────────────────────────────────────

def _copy_to_clipboard(text: str) -> None:
    """Copy *text* to the system clipboard. Silent on failure."""
    try:
        if sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text.encode(), check=True)
        elif sys.platform == "win32":
            subprocess.run(["clip"], input=text.encode("utf-16"), check=True)
        else:
            # Linux: try xclip then xsel
            for cmd in (["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
                try:
                    subprocess.run(cmd, input=text.encode(), check=True)
                    break
                except FileNotFoundError:
                    continue
    except Exception:
        pass


# ── SVG renderer ────────────────────────────────────────────────────────────────

def render_svg(stats: SavingsStats) -> str:
    """Return a self-contained SVG savings card as a string.

    No external dependencies, no network fonts — uses a generic monospace
    stack so it renders identically on GitHub, PyPI, and social previews.
    """
    saved = f"${stats.total_saved:,.2f}"
    pct = f"{stats.savings_pct}% cheaper than always-Sonnet"
    calls = (
        f"{stats.total_calls:,} calls  ·  {stats.free_calls:,} free "
        f"(Ollama / Codex)  ·  {stats.paid_calls:,} paid API"
    )
    top = f"Top model: {stats.top_model}"

    def esc(s: str) -> str:
        return html.escape(s, quote=True)

    # 640×360, dark card, accent indigo (#6366F1) matching the PyPI badge.
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360" viewBox="0 0 640 360" role="img" aria-label="llm_router savings card">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#0F172A"/>
      <stop offset="1" stop-color="#111827"/>
    </linearGradient>
    <style>
      .mono {{ font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace; }}
    </style>
  </defs>
  <rect x="0" y="0" width="640" height="360" rx="20" fill="url(#bg)"/>
  <rect x="0.5" y="0.5" width="639" height="359" rx="20" fill="none" stroke="#1F2937" stroke-width="1"/>

  <text x="40" y="58" class="mono" font-size="22" font-weight="700" fill="#E5E7EB">llm_router</text>
  <text x="40" y="80" class="mono" font-size="13" fill="#6B7280">meeting of rivers, routing intelligence</text>

  <text x="40" y="168" class="mono" font-size="56" font-weight="800" fill="#10B981">{esc(saved)}</text>
  <text x="40" y="198" class="mono" font-size="16" fill="#A7F3D0">saved (lifetime)</text>

  <text x="40" y="240" class="mono" font-size="15" fill="#818CF8">{esc(pct)}</text>
  <text x="40" y="272" class="mono" font-size="13" fill="#9CA3AF">{esc(calls)}</text>
  <text x="40" y="294" class="mono" font-size="13" fill="#9CA3AF">{esc(top)}</text>

  <text x="40" y="334" class="mono" font-size="13" fill="#6B7280">⭐ {esc(REPO)}</text>
</svg>
"""


def _write_svg(out_path: str) -> int:
    """Write the SVG savings card to *out_path*. Returns a CLI exit code."""
    stats = _gather_stats()
    svg = render_svg(stats)

    out_path = os.path.expanduser(out_path)
    parent = os.path.dirname(out_path) or "."
    try:
        os.makedirs(parent, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(svg)
    except OSError as exc:
        print(f"  {_yellow('✗')}  Could not write {out_path}: {exc}")
        return 1

    print()
    print(f"  {_green('✓')}  Savings card written to {out_path}")
    print(f"     {_dim(f'{stats.total_saved:,.2f} saved · {stats.savings_pct}% cheaper · {stats.total_calls:,} calls')}")
    print(f"     {_dim('Embed it:')} ![llm_router savings](savings-card.svg)")
    print()
    return 0


# ── ANSI implementation ──────────────────────────────────────────────────────────

def _run_share() -> None:
    """Generate a shareable ANSI savings card and open a one-click tweet."""
    stats = _gather_stats()

    # ── Build the card ────────────────────────────────────────────────
    WIDTH = 54

    def _box_line(text: str) -> str:
        pad = WIDTH - 2 - len(text)
        return f"│ {text}{' ' * max(0, pad)} │"

    border = "─" * WIDTH
    card_lines = [
        f"┌{border}┐",
        _box_line(""),
        _box_line(f"  🤖 llm_router saved me ${stats.total_saved:.2f} (lifetime)"),
        _box_line(f"     {stats.savings_pct}% cheaper than always-Sonnet"),
        _box_line(""),
        _box_line(f"  {stats.total_calls:,} total calls tracked"),
        _box_line(f"  {stats.free_calls:,} free  (Ollama / Codex)  ·  {stats.paid_calls:,} paid API"),
        _box_line(f"  Top model: {stats.top_model}"),
        _box_line(""),
        _box_line(f"  ⭐ {REPO}"),
        _box_line(""),
        f"└{border}┘",
    ]

    print()
    for line in card_lines:
        print(f"  {line}")
    print()

    # ── Copy plain text to clipboard ─────────────────────────────────
    plain = (
        f"🤖 llm_router saved me ${stats.total_saved:.2f} (lifetime)\n"
        f"{stats.savings_pct}% cheaper than always-Sonnet\n\n"
        f"{stats.total_calls:,} calls tracked  ·  {stats.free_calls:,} free (Ollama/Codex)  ·  "
        f"{stats.paid_calls:,} paid API\n"
        f"Top model: {stats.top_model}\n\n"
        f"⭐ {REPO}"
    )
    _copy_to_clipboard(plain)

    # ── Twitter/X intent URL ──────────────────────────────────────────
    tweet = (
        f"🤖 llm_router saved me ${stats.total_saved:.2f} so far "
        f"({stats.savings_pct}% cheaper than always-Sonnet)\n\n"
        f"{stats.free_calls} free calls (Ollama/Codex) · {stats.paid_calls} paid API calls\n\n"
        f"Open-source MCP router for Claude Code 👇\n"
        f"{REPO} ⭐"
    )
    tweet_url = "https://twitter.com/intent/tweet?text=" + urllib.parse.quote(tweet)

    print(f"  {_green('✓')}  Card copied to clipboard")
    print(f"  {_yellow('→')}  Tweet it: {_dim(tweet_url[:72] + '…')}")
    print(f"  {_dim('Tip:')}  llm_router share --svg  → writes an embeddable savings-card.svg")
    print()

    try:
        webbrowser.open(tweet_url)
        print(f"  {_dim('(opened in browser)')}")
    except Exception:
        pass
    print()
