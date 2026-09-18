# Corrected grounding baseline — 18 September 2026

The baseline that later work has to beat. Measured **after** the five
prerequisite fixes landed, because the point of measuring here is that a scope
fix or a richer snippet must not be able to collect credit later as a semantic
layer gain.

## The number

```
n = 60 questions · model = qwen3-coder:30b · seed = 7

                                strict         lenient
without OKF context         0     0.0%      0     0.0%
with OKF context           40    66.7%     40    66.7%

lift (strict):  +66.7% (+40 of 60)
lift (lenient): +66.7% (+40 of 60)

material retrieved for 41/60 (68%)
on those 41 alone (strict): 0.0% → 97.6% (+40 of 41)
```

Source: `scripts/bench_grounding.py`, commit `873a101`, branch
`feat/semantic-layer`. Per-question detail:
`docs/measurements/grounding-corrected-baseline.json`.

Questions are derived, not authored: symbols the repo defines exactly once, per
`git ls-files` plus a definition regex, so the ground truth comes from grep. No
model judges another model.

## Strict and lenient agree, which was not expected

`score()` now returns two numbers, because the old rule accepted a bare
basename *and*, through substring matching, a wrong directory. The expectation
going in was that strict scoring would come in below lenient and reveal that
some previously-correct answers had named the wrong path.

It did not. **40 = 40.** On these 60 questions the model either named the exact
repo-relative path or named nothing usable; not one answer was carried by the
basename allowance.

### What that means for the published `+64.0%`

`a535f22`'s commit message publishes `with OKF context 16/25 64.0%, lift
+64.0%`, measured under the old substring rule. The reasonable worry was that
some of those 16 were bare-basename or wrong-directory hits.

On this evidence that worry does not hold up: the two rules do not separate on
this task at all. The old number looks sound, and **+64.0% at n=25 and +66.7%
at n=60 are consistent with each other.**

Two honest caveats. Different n, different question set, and this run is on a
branch with five fixes the old one did not have — so this is agreement, not
replication. And "the rules did not separate here" is a fact about this model
on this task, not a licence to drop strict scoring: the wrong-directory answer
it was written to reject remains wrong, and a different model will produce one.

## What caps the lift

Retrieval found material for 41 of 60 questions. On those 41 the model went
from 0% to 97.6%; on the other 19 it had nothing to work with, and 19/60 is
most of the 33.3% that is still missing.

So the ceiling here is **recall, not reasoning**. That is the number the
structural index exists to move, and it is the one to watch — an improvement
that raises accuracy on the 41 without raising the 41 is improving something
this benchmark cannot distinguish from luck.

## Reading this later

`n = 60`. This repo has four recorded cases of a 21–64 prompt day being read as
a collapse when it was noise, which is why the script now refuses to report a
rate below n = 50 without saying "too few to tell". 60 clears that bar and is
still small: the 95% interval on 40/60 is roughly ±12 points.
