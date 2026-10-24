v16 release notes, `llm-router local-check` (measured per user; pilot evidence and limits). Full command reference: `docs/local-check.md`; routing: `docs/local-check-routing.md`.

**What it does (D-259, D-260)**
- `llm-router local-check` measures **your own local model** on your machine, against a public benchmark bank (two text jobs: `commit_message`, `pr_description`), with a Claude judge that runs on your own subscription or your own API key.
- It records a result per lane. Commit messages and PR descriptions are routed to your local model only on a lane that model passed. Routing is on by default after a pass; switch a lane off with `llm-router local-check disable <lane>`.
- Results are per user and per machine. This release does not say that any particular model qualifies for you; run the check to find out for yours.
- `--no-judge` runs are probes: they record nothing and appear only in `llm-router local-check status`.

**Limits, stated first**
- Not validated: CPU-only machines (D-267, untested). Runtimes other than Ollama: LM Studio, llama.cpp and vLLM adapters exist, but no judged pilot has exercised them. Model families beyond the two below.
- The pilot evidence is from one machine (Apple M5 Pro, 48 GB) and one item draw. It is development evidence, not a guarantee for other hardware or models.
- A run that is neither a pass nor a powered fail is INCONCLUSIVE and records nothing, so no routing is switched on.

**Pilot evidence (bank v1 draw 1, n = 70: 35 commit_message, 35 pr_description; pass bar: k >= 56 and Wilson 95% lower bound >= 0.60)**
- qwen3.6:35b-a3b-coding (Ollama): PASS, pooled 59/70 (Wilson lower bound 0.74). Lanes: commit_message 32/35, pr_description 27/35. All validity gates passed. 95 judge calls. 58 min wall time.
- gemma3:12b (Ollama): INCONCLUSIVE, pooled 51/70 (Wilson lower bound 0.61). Lanes: commit_message 24/35, pr_description 27/35. All validity gates passed. 96 judge calls. No wall time is given: the machine slept during the run.
- Consistency check, different item set: an earlier preregistered evaluation (PREREG-Q37) of the qwen3.6 model gave 60/70. It used different items from the bank draw above, so it is not a replication.

**Where the numbers come from:** the two pilots are development runs on one Apple M5 Pro, 48 GB machine, bank v1 draw 1 (n = 70 each), recorded in the coordinator's PROGRESS log on 2026-10-22 and 2026-10-24. The 60/70 figure is from PREREG-Q37 (different item set). Wilson bounds use the 95% interval for the stated k and n.
