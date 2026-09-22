# Verifier Trust audit — Phases 19-20 (2026-09-22, re-run)

Independent red-team of `scripts/groundtruth/mutants.py`, `propose.py`,
`verifier_registry.py`, `verifiers.py`. All probes run directly against the
library functions under an isolated tmp dir; no writes to `~/.llm-router`.

---

## Phase 20 — correlated judge failure

**Mapped every model-invocation call site in the subsystem:**

```
grep -n "litellm\|openai\|anthropic\|XAI\|ollama\|completion(\|judge(" scripts/groundtruth/*.py
```

Exactly **one** hit that is an actual model call: `run_matrix.call_model()`,
which invokes `litellm.completion()` against the tier **under test** (`local`,
`cheap`, `mid`, `premium`). Every other script in the subsystem —
`eligibility.py`, `classify.py`, `propose.py`, `mutants.py`,
`contract.py` — is regex/template logic with no model call anywhere.

**The taxonomy has a slot for a judge (`S_JUDGE`/`V_JUDGE`, "llm_judge") that
is not implemented.** Grep for both symbols across every script: they appear
only in enum/constant definitions, `STRATEGY_CLASS` mapping tables, and one
dead branch in `propose()` (`if strategy == S_JUDGE: p.risks.append(...)`, a
risk-string append, nothing that produces a verdict). `select_strategy()` has
no path that returns `S_JUDGE` before falling through to `S_NONE`, and
`generate_snippet()` has no case for it. **There is no code today that a
correlated-judge failure could occur in**, because there is no judge.

**Verified this claim directly, not just by absence of a grep hit**: traced
`propose()` → `select_strategy()` for both a factual-QA candidate and a
code-fix candidate (live, reproduced in this session) — neither reached
`S_JUDGE`; one reached `S_REFERENCE` (blocked on a missing human reference
answer) and one reached `S_NONE` ("no template matched; the task shape is
unrecognised"). Cross-model verification (the brief's request to test
disagreement between Ollama `qwen3.5`/`qwen3.8`/`qwen3-coder` and
`XAI_API_KEY`) is therefore **not applicable to this subsystem as shipped** —
there is no judge output to compare across models. The only place multiple
models appear at all is `run_matrix.TIERS`, and those are the **subjects being
measured**, not judges measuring each other; conflating the two would be the
mistake this phase exists to catch, and the code does not make it.

**This is a genuine, reproducible independence property — the single
strongest thing in the subsystem** (confirming, not merely trusting, the
2026-09-22 audit's finding of the same). "Ground Truth" here does not mean "a
model agreeing with itself," for the simple reason that no model participates
in producing it except the one being scored.

**Where correlation risk actually lives, unaddressed by the above:**

1. **The human sign-off gate is a name-plausibility filter, not identity
   verification** (`verifier_registry.require_human_actor`,
   `verifier_registry.py:44-70`). The code says so itself: *"nothing offline
   can prove a human typed a string."* An operator — including an unattended
   agent scripting the CLI with a plausible-looking `--by` value — can drive a
   verifier from `PROPOSED` through `VALIDATED`/`APPROVED`/`ACTIVE` without a
   person actually reviewing it. This does not correlate a *judge* with the
   *subject*, but it does mean the one human checkpoint the whole design
   leans on for SOFT-adjacent judgment calls (is this contract complete? is
   this the right reference answer?) is honestly disclosed as spoofable, not
   robust against it.
2. **The mutation library and the verifier templates were both hand-authored
   by the same development process that also writes the router.** This is a
   process-level correlation the code cannot self-detect (see Phase 19: the
   mutation library tests bugs a human predicted a model would make, not bugs
   an adversary — including an adversarial *auditor* — would make).

---

## Phase 19 — verifier authoring red team, mutation kill rate

### What `mutants.py` already defends against (confirmed by reading + reran
tests)

* `MIN_BAD_ANSWERS = 3` with a distinctness check
  (`_snippet_discrimination_floor`) — a lazy operator supplying one trivial
  bad answer caps confidence at `MEDIUM`, not `HIGH` (this is itself a fix
  from the prior remediation round, S-07/H-08 lineage).
* `classify()` refuses `HIGH` unless `baseline_passed` **and**
  `detected == total` **and** the probe set clears the floor **and** the
  acceptance contract is complete — four independent conditions, not one
  ratio.
* The pytest path mutates the **target**, never the test
  (`validate_pytest_verifier`'s own docstring states this explicitly, and the
  code only ever calls `mut.apply(target_source)`).

### Adversarial mutants this audit designed (not the repo's own), and the
measured kill rate against the repo's own validator

**Attack 1 — semantically empty verifier, shape-compliant probe set.**
Built and ran directly against `mutants.validate_snippet_verifier`:

```python
snippet = 'import os\nassert len((os.environ.get("BENCH_ANSWER") or "").strip()) > 5'
good    = "The capital of Portugal is Lisbon."
bad     = ["no", "na", "x"]       # 3 distinct, non-empty, != good — clears MIN_BAD_ANSWERS
```

Result, reproduced live:

```
baseline_passed: True
detected/total:  3 / 3
weak_probe_set:  None
CONFIDENCE:      HIGH — "passes a known-good implementation and detects 3/3 broken ones"
```

Then probed the resulting **HIGH-confidence** verifier with an answer that is
confidently, factually wrong but merely long:

```python
run_verifier(snippet, "The capital of Portugal is Madrid, a beautiful coastal city.")
# -> accepted: True
```

**CONFIRMED, reproduced, kill rate 0%.** A verifier checking nothing but
answer length reaches the system's highest confidence tier and then accepts
a wrong answer with 100% consistency against any sufficiently verbose wrong
answer. This is not a corner case invented for the audit — it is exactly the
shape the *previous* audit's own worked example demonstrated
(`assert len(BENCH_ANSWER) > 5`), which this audit re-derived independently
and confirmed **still reaches HIGH after the H-08/S-07 remediation round**,
because that remediation's floor (`MIN_BAD_ANSWERS`, distinctness) checks the
*shape* of the operator-supplied probe set (count, uniqueness), never its
*semantic* relationship to the property under test. It cannot check the
latter without embedding judgment — which is precisely why authoring is
"irreducibly human" per the module's own docstring. The honest characterization
is therefore: **`mutants.py` proves a verifier discriminates against the probes
it was given; it cannot and does not prove those probes are the right probes.**
A human reviewer looking only at `classify()`'s `HIGH` output, without reading
the snippet, would rubber-stamp this.

**Attack 2 — gaming guards are advisory text, not enforcement.** Traced
`propose.gaming_guards()`: it returns human-readable strings (e.g.
`no_tests_weakened("test_x.py", min_assertions=1)`) printed by
`verifier_cli.py show` for a reviewer to read. **Nothing calls these
functions automatically.** `generate_snippet()` (the S_SCHEMA/S_FILE_STATE
code path) never invokes `no_tests_weakened`, `unchanged`, or
`no_hardcoded_answer` — those are opt-in helpers a human verifier-author must
manually write into a snippet. Grep confirms: `no_tests_weakened` appears only
in `verifiers.py` (definition) and `propose.py` (a string template, not a
call). So "test deletion or fixture mutation" is not structurally prevented
for the schema/file-state strategies — it is prevented only for the pytest
strategy, and only insofar as `generate_test()` deliberately emits
`pytest.fail("UNIMPLEMENTED...")` skeletons that cannot pass until a human
fills in a real assertion (this part is genuinely well designed — a generated
test that passes on arrival is impossible by construction, confirmed by
reading `_TEST_HEADER`/`generate_test`).

**Attack 3 — hardcoded/happy-path output.** Not separately reproduced beyond
Attack 1, which *is* an instance of it: the "verifier" effectively hardcodes
"anything long enough is fine," the happy path. `no_hardcoded_answer()` exists
as an available helper (`verifiers.py`) but, like the gaming guards, is opt-in
and not injected by any auto-generation path.

**Attack 4 — bypass validation entirely.** Checked whether `approve()`/
`activate()` can be reached without ever calling `validate`: `approve()`
explicitly refuses from status `PROPOSED` ("this verifier has not been
validated..." — reproduced live, exit path confirmed). This one is closed:
validation is a mandatory gate on the state machine, not merely a
recommendation.

**Attack 5 — accept an incomplete solution.** Not fully reproducible without
a populated `pytest` strategy target (none exists in the current seed set —
0 authored verifiers). Based on code reading: `contract.py`'s "unclear"
detection plus `classify()`'s `contract_complete` requirement for `HIGH` is
the intended defense; not independently exercised here for lack of a live
`proposed_files` example to mutate. Recorded as **not tested**, not as
passing.

### Kill-rate summary against adversarial (not self-generated) mutants

| Attack | Result against repo's own validator |
|---|---|
| Semantically empty check (length-only), shape-valid probes | **Not caught. Reaches HIGH. 0% kill rate on the actual invariant.** |
| Advisory-only gaming guards (test deletion/weakening on schema/file-state strategies) | **Not enforced automatically** — depends entirely on a human manually inserting the helper call |
| Hardcoded/happy-path output | Same mechanism as above — not caught for auto-generated snippets |
| Bypass validation state transition | **Caught.** `approve()` refuses from `PROPOSED` |
| Incomplete solution via `proposed_files` mutation gate | Not exercised (no live target) — inconclusive |

---

## Bottom line for both phases

* **Phase 20**: no correlated-judge failure exists in the mechanical-verifier
  path, because no judge exists in it — CONFIRMED by exhaustive call-site
  grep plus two live traces through `propose()`. This is real and it is the
  subsystem's best property.
* **Phase 19**: the mutation *validator* is a real, working discrimination
  test against the probes it is given, and closes several previously-real
  gaps (rubber-stamp approval, unvalidated activation, thin probe sets). It
  is **not** a verifier of verifier *correctness* — a verifier that
  discriminates on an irrelevant property of the answer (length, presence of
  any long string, anything correlated by accident with the hand-picked bad
  answers) can still be driven to the system's highest confidence label by an
  operator who is careless rather than malicious, with zero code change
  required. The system's own quality bar (`HIGH` = "passes a known-good
  implementation and detects N/N broken ones") is stated in terms that sound
  like ground truth but are, mechanically, a statement about the specific
  probe set supplied — nothing more.
