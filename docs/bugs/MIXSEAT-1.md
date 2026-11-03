---
id: MIXSEAT-1
status: fixed in `bind/honest-statusline` (deploy: refresh `llm_router-statusline.sh` and `llm_router_statusline_segments.py`)
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
- **Test.** `tests/test_w8_mix_money.py` (the seat table, Codex never in S, a Codex call without a seat stays unclassified, the cache round
  trip and an older cache, a package without the split figures, the script for the split, an older cache, the coverage rounding, a hostile cache value);
  `tests/test_p34a_six_surfaces.py` pins the statusline mix against the hand-classified fixture.
