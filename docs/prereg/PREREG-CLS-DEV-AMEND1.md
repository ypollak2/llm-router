# PREREG-CLS-DEV AMEND1: E1 items take the E1 source file's mtime as an upper-bound timestamp (D-280)

This file is frozen only when `PREREG-CLS-DEV-AMEND1.sha256` exists beside it and
`scripts/check_prereg.py docs/prereg/PREREG-CLS-DEV-AMEND1.md` reports a match; until then it is a draft. It
amends `PREREG-CLS-DEV.md` (r5, sha256 `6d173a1c081579f4decc82e8024f2814b2a24477052f2580c8f21dcb38ffc795`, frozen_utc
2026-10-22T16:02:26Z), which stays byte-identical. It changes only the items under "What changes". Every
frozen script (`cls_eval.py`, `cls_dev_run.py`, `cls_dev_lib.py`, `cls_label_run.py`, `cls_gen_run.py`,
`cls_resemble.py`, `cls_send_lib.py`, `cls_pins.py`), every threshold and floor, and the PINS-STATIC file stand.

## Authority

D-280 (owner decision). The rule itself is the owner's. The author chose the details that implement it, each
disclosed below: the session-handling paragraph (it picks what the frozen code already does), rounding the bound
up to the whole second, the flag's shape, and the dedup-order disclosure.

## Deviation and evidence (counts only)

* **What happened.** The real attach (`scripts/cls_attach.py attach`, not frozen, built after the freeze) ran
  on the owner's machine, then the frozen `cls_dev_run.py manifest` ran. It wrote one PINS-RUN entry, `manifest-m1` (files
  `dev_manifest.json`, `length_counts.json`, `shingles_real.json`). The attach output `dev/attach-m1/` has no
  PINS-RUN entry; frozen §15 names only the manifest entry ("the manifest sha256 (one entry, `cls_dev_run.py
  manifest`...)"), so that is not a deviation.
* **Counts.** `n_input_real` 737; `n_real_dev` **293**. Drops: 204 duplicates, 190 excluded as secret, and **50
  with no timestamp: all of E1** (`e1_tune` 30, `e1_ho` 20, read from the local-classifier `tasks_local.json`).
  Record recovery found 0 E1 sessions and 0 E1 timestamps.
* **Result.** `validity_floor` failed with `n_real_dev_below_300`; status NOT_INFORMATIVE.
* **Why it is a contradiction, not a bad draw.** PREREG §1.1 drops "a real record with no recoverable
  timestamp" because it "cannot be shown to precede T0" (2026-10-12T09:18:13Z). The E1 rows are not records
  from the T0 window. They sit in a file that existed before T0. The rule dropped them for a missing field,
  not for evidence that they are late.

## What changes

1. **Rule (E1 sets only).** For source sets `e1_tune` and `e1_ho` only, an item with **no recoverable record
   timestamp** (after record recovery, §1.3 step 2) takes the **last-modified time (mtime) of its source file**
   (`tasks_local.json`) as its timestamp. The mtime is read **at attach time** and recorded in
   `attach_counts.json` together with the file's sha256 (below). The value is an **upper bound**: the item
   existed at or before it.
2. **Format.** The timestamp is UTC, `Z`-suffixed, whole seconds, **rounded up** (`YYYY-MM-DDTHH:MM:SSZ`, the
   format of `cls_dev_lib.utc_str`, which `cls_dev_lib.parse_utc` reads). Rounding up keeps the string from being
   earlier than the true mtime. The exact `st_mtime_ns` is recorded too.
3. **§1.1 consequence.** Because the coordinator reports the mtime as 2026-10-06T20:45:16Z, earlier than the P17D
   cutoff (2026-10-08T20:46:00Z) and T0, those items "can be shown to precede T0", so the no-timestamp drop of
   §1.1 does not apply to them. The frozen `time_filter` then runs unchanged: an E1 item whose bound is at or
   after T0 or after the cutoff is dropped as `n_after_T0_dropped` / `n_after_cutoff_dropped` like any other.
   The mtime value itself is read at the rerun; this amendment does not depend on the coordinator's figure.
4. **Attach flag (not frozen code).** `cls_attach.py attach --timestamp-upper-bound-from-mtime SET`, repeatable.
   Allowed values are exactly `e1_tune` and `e1_ho`; any other value refuses. The flag needs `--e1-tasks`. For
   the same set it is **refused together with** `--allow-missing-timestamps` (the m1 workaround is not needed
   and not allowed with this rule). An item that already has a timestamp keeps it.
5. **Counts recorded.** `attach_counts.json` gains `timestamp_upper_bound_from_mtime`, per set:
   `n_timestamp_from_mtime`, `mtime_utc`, `mtime_ns`, `source_file_sha256`, `source_flag` (`e1_tasks`) and
   `amendment` (`PREREG-CLS-DEV-AMEND1`). The stdout line stays counts only; no text is printed.
6. **Run entries.** The rerun writes a new attach directory `dev/attach-m2/` and a new manifest PINS-RUN entry
   `manifest-m2` (out dir `dev/manifest-m2/`). An `attach-m2` PINS-RUN entry is not required (as for m1); if one is
   added with `pins --run`, it is voluntary. The `manifest-m1` entry and every m1 output stay as they are; the m1
   manifest remains the record of the NOT_INFORMATIVE result.
7. **Rerun preconditions (frozen code).** `manifest --run-entry` defaults to `dev_manifest`, so m2 passes
   `--run-entry manifest-m2` (a duplicate name refuses, exit 2). `manifest --out-dir` must be a new directory:
   reusing `dev/manifest-m1/` would overwrite pinned files and make the next `verify_at_start` fail (exit 9).
   `attach --out` must be a new directory under `<cls-root>/dev` (the writer opens files with O_EXCL). The
   manifest takes `--pins-static`, `--pins-run` and `--selftest-result` as at m1; the existing self-test result
   stays valid because the scrubber is unchanged. The manifest path checks no finalists/thresholds file (only
   `assemble` writes those).

## Why the weaker provenance is acceptable (stated precisely)

A file mtime is weaker provenance than a record timestamp: the file can be touched or rewritten later.

* **Direction of the error.** The rule needs only "the item existed at or before T". A file contains an item's
  text only if the item was written into it at or before the file's last modification, so `existence time <=
  mtime`. A later touch, rewrite or copy that sets mtime to the current time makes the mtime **later**. A later
  bound can only push an item toward the drop side of the precede-T0 / before-cutoff test. The rule can
  therefore drop items it should have kept, and cannot admit items it should have dropped.
* **The one way this fails.** The argument assumes mtime was not moved **backwards**. Operations that preserve or
  set an old mtime (`touch -d`, `cp -p`, `rsync -t`, extracting an archive) could make the bound earlier than
  the truth and admit a late item. The attach reads the file in place on the owner's machine, and the file's
  sha256 and `st_mtime_ns` are recorded so a later check can compare them. This is a residual risk, not a
  proof; it is disclosed here.
* **What the bound is not.** It is not the item's real timestamp. Two effects follow, both disclosed:
  * **Dedup order.** The frozen manifest keeps the earliest-timestamp copy of a duplicate pair. An E1 item with a
    bound earlier than the real record time of its twin in another set will be the copy that is kept, and the
    twin (which has a session) is counted as `n_dropped_dup`. That moves items from a recovered session group to
    an `unk:` group.
  * **Ordering and fold dealing** use the group, not the timestamp, so the bound affects nothing else.

## Session handling (E1 items have no `session_id`)

* **What the frozen code does.** `cls_dev_lib.build_manifest`: `group = item.group or item.session_id or ""`; an
  empty group becomes `unk:<source_set>:<label batch or 0>` and counts toward `n_unk_group`. `assign_folds`
  (§6.1) deals whole groups, so a group never spans folds. `attach` writes `group = session_id` (None for E1), so
  E1 items fall into this rule.
* **Rule chosen: the frozen `unk:` rule, with no attach-side value.** E1 items stay one group per source set
  (and per label batch when the E1 label rows carry a `batch`): `unk:e1_tune:*`, `unk:e1_ho:*`.
* **Leakage justification.** Items that may share an origin must stay in the same fold. E1 rows come from one
  local-classifier task file; their sessions are unknown, so the safe assumption is that items of a set may share
  one. One group per set keeps them together, which is the conservative choice. A per-item pseudo-session would
  let them split across folds and would also **hide them from `n_unk_group`**, which would silence the `LOW
  CLUSTER FIDELITY` label of §1.3 (2) that exists for this exact case. A made-up session id is rejected for that
  second reason as well. Residual risk, unchanged from m1's other `unk:` items: an E1 item may share a real
  session with an item of another set that lands in another fold; the unk-share label is the guard.
* **Expected effect (arithmetic on the stated counts; the real values come from `manifest-m2`).** Let `k <= 50`
  be the E1 items that survive dedup and the secret detector.
  * `n_real_dev = 293 + k` (before any dedup-order shift). The floor term `n_real_dev >= 300` needs `k >= 7`.
  * `n_unk_group` rises by `k`. At `k = 50`, E1 alone gives `unk_share = 50/343 = 14.6 percent`, under the
    15 percent line; `low_cluster_fidelity` turns true once the other `unk:` items number about 2 or more
    (`n_unk > 0.15 * n_real_dev`). If it turns true, kill criterion 6 is reported only (§1.3 (2)); that is
    the frozen consequence and is accepted by D-280.
  * `largest_session_share`: an E1 group holds at most 30 items (`e1_tune`), 30/343 = 8.7 percent at `k = 50`,
    under the 20 percent floor. The existing largest group's share falls slightly because the denominator grows.

## Attach-time deviations already used at m1 (put on record)

These were made with `cls_attach.py` flags, outside the frozen scripts. They are disclosed here; none is changed
by this amendment.

* **(a) Label join.** The legacy label files are keyed by item id, not `text_sha`. `--ge3-label-join key`,
  `--e2-label-join key` and `--e1-label-join key` were used (an exact id join). PREREG §1.3 (4) names
  `text_sha`. The flag defaults were `text_sha` from the b-A2/b-A3 builds on; the id join was explicit.
* **(b) E1 label field.** The E1 inline label field is `truth`: `--e1-label-tier truth`.
* **(c) Missing timestamps.** `--allow-missing-timestamps e1_ho` and `--allow-missing-timestamps e1_tune` were
  used so the attach would write E1 items with no timestamp; the frozen manifest then dropped all 50. With this
  amendment the flag is no longer needed for E1 and is refused with the new flag for the same set.

## What does NOT change

* The frozen scripts and `PINS-STATIC.json`; `PREREG-CLS-DEV.md` and its sidecar.
* Every threshold and floor: `n_real_dev >= 300`, `n_truth_opus >= 40`, largest session 20 percent, 5 percent
  missing primary truth, the 15 percent unk-share label.
* T0, the P17D cutoff, and the time-filter rule for **every non-E1 item** (including a non-E1 item with no
  timestamp, which is still dropped as `n_no_timestamp_dropped`). Dedup, the secret detector, fold dealing and
  every later stage.
* m1: the `manifest-m1` PINS-RUN entry and every m1 output (`dev/attach-m1/`, `dev/manifest-m1/`).

## Freeze record

To freeze: after an independent review, write `PREREG-CLS-DEV-AMEND1.sha256` as `<sha256>  PREREG-CLS-DEV-AMEND1.md`
and `frozen_utc <date -u>` (after 2026-10-22T16:02:26Z), then run `scripts/check_prereg.py
docs/prereg/PREREG-CLS-DEV-AMEND1.md`. Any change to this file changes the hash and needs a new sidecar. The rerun
(`attach-m2`, then `manifest-m2`) starts after the freeze.
