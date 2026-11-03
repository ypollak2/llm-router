---
id: MIXSEAT-1
status: fixed in `bind/honest-statusline` (deploy: refresh `llm_router-statusline.sh`, `llm_router_statusline_segments.py` and the installed package; the status-bar hook `llm_router-status-bar.py` is re-copied (old copy kept as `.bak`) when the MCP server starts)
---
## MIXSEAT-1. The session mix `L/S/P` put the Claude seat and Codex in one bucket

- **Symptom (2026-11-03).** The status line printed `4L/25S/0P`, where S (25 calls) was Claude Opus and Haiku on the Max seat. A Codex
  delegation would have landed in the same S, so the seat the router wants to spend less of and the one it wants to spend more of could
  not be told apart (session 1a45a332, n=29 ledger rows, `~/.rsi/research/primary-plan/v16/coord/NS-STATUSLINE-ANALYSIS-20261103.md`).
- **Cause.** `ledger_view.provider_class` returns `subscription` for any seat (`SEAT_ALIASES`: Claude, Codex, Gemini CLI), and the segment
  printed that one class as S.
- **Fix.** `ledger_view.subscription_seat` and `split_subscription` split the subscription rows into the Claude seat and every other seat;
  `LedgerWindow` carries `subscription_claude_n` and `subscription_other_n`; the segments write `mix_claude` and `mix_other` and keep
  `mix_sub` as every seat, so an older script still prints the old line. The script prints `L/S/C/P` (S the Claude seat, C every
  other seat) and labels its n: `session ledger rows 29 of ~6,900 (0.4%)`, the coverage being the session's calls from the NS refresher. A package that predates the split (a rollback of the installed package, or the segments file deployed first) has no such figure: `mix_segment` then writes the old figures only and the line degrades to `L/S/P`, where it first dropped the whole segment on a `KeyError` (review S3).
- **Other surfaces (review S2).** Two more surfaces lumped the seats: the status-bar hook's full mode, `sub:25 free:4 paid:0 n=29 session`, and the ledger line shared by `llm-router kpi`, `llm-router summary` and the dashboard summary (`ledger_view.format_line`), `mix local 86 / subscription 10127 / paid 0 · est. saved $4.92 (n=634)` (7 d, the review's copy of this machine's data, 2026-11-03). The hook now prints `claude:25 other:0 free:4 paid:0 n=29 ledger rows, session` (the one bucket `sub:N` for a package without the split), its money `n=44 calls`; the ledger line prints `subscription 10127 (Claude seat A + other seats B)` and `(n=634 calls)`.
- **Test.** `tests/test_w8_mix_money.py` (the seat table, Codex never in S, a Codex call without a seat stays unclassified, the cache round
  trip and an older cache, a package without the split figures, the script for the split, an older cache, the coverage rounding, a hostile cache value);
  `tests/test_p34a_six_surfaces.py` pins the statusline mix against the hand-classified fixture; `tests/test_w8_money_surfaces.py` (the ledger line with and without the split, the hook's mix with the split and with a package that predates it, its money unit); `tests/test_p34a_ledger_view_identity.py` pins the ledger line against a hand count (this home has no Claude seat: every subscription row is an other-seat row).
