# The harder strata — 18 September 2026

The first benchmark asked "which file defines `post_entry`?" and the semantic
layer won 58/60 against 41/60. It was a real result on a favourable task: the
query names the symbol, an `ast` index looks it up exactly, and lexical
matching over prose is at its worst.

These are the strata built to break it. All four derived from the repository by
grep and `ast`, never authored — an authored question set is one shaped by
whoever knows what the system does well.

## Results, n=60 each, paired, strict scoring

| stratum | B (OKF baseline) | C (semantic) | BC (what ships) | BC vs B |
|---|---|---|---|---|
| **symbol** — query names the identifier | 41/60 | **60/60** | **60/60** | +19, p=3.8e-06 |
| **decoy** — basename in ≥2 directories | 42/60 | **59/60** | 59/60 | +17, p=1.5e-05 |
| **concept** — docstring, identifier hidden | 2/60 | 1/60 | 3/60 | +1, n.s. |
| **absent** — symbol does not exist | 60/60 | 60/60 | 60/60 | 0, n.s. |

Model `qwen3-coder:30b`, seed 7, 2000-token budget. Source:
`scripts/run_semantic_arms.py`, questions from `scripts/question_strata.py`,
per-question detail in `semantic-strata-*-after-n60.json`.

## What each stratum settled

**symbol and decoy: the win is real and it survives decoys.** The `decoy`
stratum was built on the suspicion that the layer might be naming the right
*file* and getting the *directory* wrong — 69 basenames in this repository live
in two or more directories. It is not: 59/60 under a scorer that rejects a
wrong directory outright.

**concept: everything collapses, and the layer does not rescue it.** Identify a
file from its own docstring with the identifier removed, and the baseline gets
2/60 while the best arm gets 3/60. This is a genuinely hard task that neither
lexical retrieval nor an exact-match index addresses, and the honest reading is
that **the layer's +30 points do not generalise to questions that never name
code.** A user who asks in prose gets no help from it.

**absent: nobody hallucinates.** Every arm abstained on all 60 invented
symbols. The property worth noting is that C **retrieved nothing at all** on
0/60 — the index correctly has no entry, so the pack is empty and the prompt
goes out untouched. Retrieval that invented a plausible file here would be
worse than no retrieval, and it does not.

## The bug this found

Before the fix, arm C retrieved context on **60/60 concept questions** — all of
them the same five irrelevant files.

`seeds_from` matched any word of three or more characters behind a stopword
list, and this repository contains entities named `project`, `implements`,
`routing` and `override`. Ordinary English was seeding symbol lookups.

That was live. Source retrieval had been defaulted on one commit earlier, so
"how does the routing override work?" was retrieving whatever happened to be
named `routing` and attaching it with source spans and content hashes — the
shape of evidence, holding a guess.

The fix requires a seed to *look* like code: backticked, `snake_case`,
`CamelCase` with two or more capitals, or dotted. It improved both directions
at once:

| | before fix | after fix |
|---|---|---|
| symbol, C correct | 58/60 | **60/60** |
| concept, C retrieved noise | 60/60 | **3/60** |

Cleaner seeds meant cleaner retrieval on the task it is good at, and near-total
silence on the task it cannot do — which is the correct behaviour for a query
that names no code.

## What still is not established

Task completion. Every stratum here asks a retrieval question and scores a file
path. None of them shows a better patch, a fixed bug, or work getting done. The
M0/M1/M2 history track — recurrence, lesson applicability, prevention coverage
— remains scaffolded and unrun.

One model, one repository, one seed. The `concept` result in particular is a
floor measurement (2/60 and 3/60) where almost any change is inside the noise.
