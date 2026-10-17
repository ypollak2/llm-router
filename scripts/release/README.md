# Release eval sets (PREREG-P32)

`run_p32.py` runs the four synthetic regression pins for the v16 gate and writes the file
`eval_gate.py compare` reads. No model, no network, no private data. The gate is
`eval_gate.py`; formats are in `release_evals.py`.

| set | kind | n | outcome |
|---|---|---|---|
| `rt_hand_60` | performance | 60 | `classify_signals` (task_type, complexity) equals the `classifier_gate.py` label |
| `rt_agt_50` | performance | 50 | task_type equals the `a1_prompts_50.jsonl` label (tier not scored; ROUTER_POLICY) |
| `fid_syn_60` | fidelity | 60 | answer token kept in the PACK vs the FULL context (g1 generator, seed 20261014, window 8192) |
| `stab_syn_500` | stability | 500 | decision identical across two processes, PYTHONHASHSEED 0 vs 1 |

`stab_syn_500` = 106 unique fixture texts (the 110 fixtures; the 4 floor prompts repeat a
FIXTURES prompt) + 394 seeded template prompts. Ids: `cg-`, `a1-`, `fid-`, `st-`.

Check without scoring anything (counts, id uniqueness, label cells, FIXTURES byte-identity vs v15.4.0):

    uv run python scripts/release/run_p32.py --dry-run

## Freeze to baseline to pin (PREREG-P32 §10)

Run from the freeze commit (`FREEZE`). Order matters: pin needs the baseline, baseline needs the frozen prereg.

    # 1. manifests (same bytes as `release_evals.py freeze`), then commit them
    uv run python scripts/release/run_p32.py --write-manifests release_evals/manifests

    # 2. prereg sidecar (one line, sha256sum format), then commit it
    (cd <dir of PREREG-P32.md> && sha256sum PREREG-P32.md) > release_evals/PREREG.sha256

    # 3. baseline: a clean v15.4.0 worktree; harness and item files from FREEZE, package from v15.4.0
    git worktree add /tmp/base v15.4.0
    BASE=$(git -C /tmp/base rev-parse HEAD)          # must be 9e8b1460eea9a48f2686eacca184e5a2e7a232e5
    PYTHONPATH=/tmp/base/src LITELLM_LOCAL_MODEL_COST_MAP=True \
      python scripts/release/run_p32.py --role baseline --sha "$BASE" --worktree /tmp/base \
        --sets rt_hand_60,rt_agt_50 --out release_evals/baseline.json

    # 4. pin (records baseline_sha, manifest hashes and the prereg hash), then commit it
    uv run python scripts/release/release_evals.py pin --baseline release_evals/baseline.json \
        --prereg release_evals/PREREG.sha256

    # 5. candidate, at the RC commit (HEAD == --sha, package under the checkout)
    SHA=$(git rev-parse HEAD)
    uv run python scripts/release/run_p32.py --role candidate --sha "$SHA" --out "release_evals/$SHA.json"

    # 6. verdict (what release.yml runs)
    uv run python scripts/release/eval_gate.py compare --candidate "release_evals/$SHA.json" \
        --baseline release_evals/baseline.json --manifests release_evals/manifests \
        --pin release_evals/PIN.json --prereg release_evals/PREREG.sha256 --sha "$SHA"

The harness refuses (exit 1, nothing written) if `--sha` is not 40 hex, the `--worktree` HEAD
is not `--sha`, the imported `llm_router` is not under `--worktree`, or a baseline is asked
for a fidelity or stability set. The comparator returns NOT INFORMATIVE if
`release_evals/PREREG.sha256` is absent or differs from `prereg_sha256` in `PIN.json`.
The output file also carries a `provenance` object (package path, git sha, item-file hashes).
