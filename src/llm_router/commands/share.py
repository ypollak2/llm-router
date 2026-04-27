"""Share command — generate shareable savings card."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import urllib.parse
import webbrowser


# ── ANSI helpers (respect NO_COLOR / non-tty) ─────────────────────────────────

def _color_enabled() -> bool:
    return sys.stdout.isatty() and not os.getenv("NO_COLOR")


def _green(s: str) -> str:
    return f"\033[32m{s}\033[0m" if _color_enabled() else s


def _yellow(s: str) -> str:
    return f"\033[33m{s}\033[0m" if _color_enabled() else s


def _dim(s: str) -> str:
    return f"\033[2m{s}\033[0m" if _color_enabled() else s


# ── Command entry point ────────────────────────────────────────────────────────

def cmd_share(args: list[str]) -> int:
    """Entry point for share command."""
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


# ── Implementation ─────────────────────────────────────────────────────────────

def _run_share() -> None:
    """Generate a shareable savings card and open a one-click tweet."""
    state_dir = os.path.expanduser("~/.llm-router")
    db_path   = os.path.join(state_dir, "usage.db")

    SONNET_IN, SONNET_OUT = 3.0, 15.0
    FREE_PROVIDERS = {"ollama", "codex"}

    # ── Query all-time stats ──────────────────────────────────────────
    total_calls = paid_calls = free_calls = 0
    total_saved = 0.0
    top_model   = "—"

    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row

            rows = conn.execute(
                "SELECT provider, input_tokens, output_tokens, cost_usd FROM usage "
                "WHERE success=1"
            ).fetchall()
            for r in rows:
                prov   = r["provider"] or ""
                in_tok = r["input_tokens"]  or 0
                out_tok = r["output_tokens"] or 0
                cost    = r["cost_usd"]      or 0.0
                total_calls += 1
                base = (in_tok * SONNET_IN + out_tok * SONNET_OUT) / 1_000_000
                if prov in FREE_PROVIDERS:
                    free_calls  += 1
                    total_saved += base
                elif prov != "subscription":
                    paid_calls  += 1
                    total_saved += max(0.0, base - cost)

            # Top model by call count (paid only)
            top_row = conn.execute(
                "SELECT model, COUNT(*) as n FROM usage "
                "WHERE success=1 AND provider NOT IN ('subscription','ollama','codex') "
                "GROUP BY model ORDER BY n DESC LIMIT 1"
            ).fetchone()
            if top_row:
                m = top_row["model"]
                top_model = m.split("/")[-1] if "/" in m else m
                if len(top_model) > 24:
                    top_model = top_model[:22] + "…"

            conn.close()
        except Exception:
            pass

    savings_pct = 0
    if total_saved > 0 and (paid_calls + free_calls) > 0:
        # rough pct: saved / (saved + actual_cost)
        try:
            conn2 = sqlite3.connect(db_path)
            actual = conn2.execute(
                "SELECT COALESCE(SUM(cost_usd),0) FROM usage WHERE success=1 "
                "AND provider NOT IN ('subscription','ollama','codex')"
            ).fetchone()[0] or 0.0
            conn2.close()
            total_baseline = total_saved + actual
            savings_pct = round(total_saved / total_baseline * 100) if total_baseline > 0 else 0
        except Exception:
            pass

    # ── Build the card ────────────────────────────────────────────────
    WIDTH = 54
    def _box_line(text: str) -> str:
        pad = WIDTH - 2 - len(text)
        return f"│ {text}{' ' * max(0, pad)} │"

    border = "─" * WIDTH
    card_lines = [
        f"┌{border}┐",
        _box_line(""),
        _box_line(f"  🤖 llm-router saved me ${total_saved:.2f} (lifetime)"),
        _box_line(f"     {savings_pct}% cheaper than always-Sonnet"),
        _box_line(""),
        _box_line(f"  {total_calls:,} total calls tracked"),
        _box_line(f"  {free_calls:,} free  (Ollama / Codex)  ·  {paid_calls:,} paid API"),
        _box_line(f"  Top model: {top_model}"),
        _box_line(""),
        _box_line("  ⭐ github.com/ypollak2/llm-router"),
        _box_line(""),
        f"└{border}┘",
    ]

    print()
    for line in card_lines:
        print(f"  {line}")
    print()

    # ── Copy plain text to clipboard ─────────────────────────────────
    plain = (
        f"🤖 llm-router saved me ${total_saved:.2f} (lifetime)\n"
        f"{savings_pct}% cheaper than always-Sonnet\n\n"
        f"{total_calls:,} calls tracked  ·  {free_calls:,} free (Ollama/Codex)  ·  {paid_calls:,} paid API\n"
        f"Top model: {top_model}\n\n"
        f"⭐ github.com/ypollak2/llm-router"
    )
    _copy_to_clipboard(plain)

    # ── Twitter/X intent URL ──────────────────────────────────────────
    tweet = (
        f"🤖 llm-router saved me ${total_saved:.2f} so far "
        f"({savings_pct}% cheaper than always-Sonnet)\n\n"
        f"{free_calls} free calls (Ollama/Codex) · {paid_calls} paid API calls\n\n"
        f"Open-source MCP router for Claude Code 👇\n"
        f"github.com/ypollak2/llm-router ⭐"
    )
    tweet_url = "https://twitter.com/intent/tweet?text=" + urllib.parse.quote(tweet)

    print(f"  {_green('✓')}  Card copied to clipboard")
    print(f"  {_yellow('→')}  Tweet it: {_dim(tweet_url[:72] + '…')}")
    print()

    try:
        webbrowser.open(tweet_url)
        print(f"  {_dim('(opened in browser)')}")
    except Exception:
        pass
    print()
