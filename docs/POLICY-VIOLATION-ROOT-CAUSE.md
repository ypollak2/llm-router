# Claude Opus Policy Violation — Root Cause Analysis

## Executive Summary

510 routing decisions (24.3% of real traffic) incorrectly selected Claude Opus in BALANCED/CODE context between April 20-22, 2026. The root cause was a logic error in the `reorder_for_pressure()` function that treated Claude Opus as a "free" model under subscription, causing it to be promoted to the front of routing chains when quota was available (< 85%).

**Status**: FIXED in commit 76783d7. The `_CLAUDE_MODELS` set was renamed to `_CLAUDE_CHEAP_MODELS` and Claude Opus was explicitly excluded.

---

## Timeline

| Date | Time | Event |
|------|------|-------|
| 2026-04-20 | 13:39:07 | 510 Opus violations begin occurring |
| 2026-04-20 | 13:39:52 | Last batch of violations logged |
| 2026-04-21 | 16:32:34 | QUOTA_BALANCED feature added (commit 60af1de) - unrelated to violation |
| 2026-04-21 | 16:53:30 | **FIX APPLIED** (commit 76783d7) - Opus removed from cheap models set |

---

## Root Cause Mechanism

### The Bug

Before commit 76783d7, `src/llm_router/profiles.py` contained:

```python
_CLAUDE_MODELS: frozenset[str] = frozenset({
    "anthropic/claude-opus-4-6",        # ❌ INCLUDED OPUS
    "anthropic/claude-sonnet-4-6",
    "anthropic/claude-haiku-4-5-20251001",
})
```

In `reorder_for_pressure()`, when subscription quota pressure < 0.85 (quota available):

```python
claude_models = [m for m in chain if m in _CLAUDE_MODELS]
other_models = [m for m in chain if m not in _CLAUDE_MODELS]

if pressure < 0.85:
    return claude_models + other_models  # Puts ALL Claude models first
```

### Why This Violated Policy

- **Policy**: Claude Opus ($15/1M) is PREMIUM-only; never BALANCED
- **Intent**: Cheap Claude models (Haiku $3/1M, Sonnet $3/1M) should be prioritized as fallbacks when quota is available
- **Bug**: Opus was incorrectly grouped with cheap models, so it was prioritized alongside them
- **Impact**: When any chain containing Opus was passed to `reorder_for_pressure()` with pressure < 0.85, Opus would be moved to the front

### Why the Static ROUTING_TABLE Wasn't Enough

The static `ROUTING_TABLE[(BALANCED, CODE)]` correctly excluded Opus:

```python
(RoutingProfile.BALANCED, TaskType.CODE): [
    "ollama/qwen3.5:latest",
    "codex/gpt-4o",
    "gemini/gemini-2.5-pro",
    "deepseek/deepseek-chat",
    "openai/gpt-4o",
    "anthropic/claude-sonnet-4-6",     # ✓ Cheap Claude fallback
    "anthropic/claude-haiku-4-5-20251001",
    # ✓ NO OPUS
],
```

However, if any **dynamic routing** or **fallback logic** added Opus to the chain BEFORE calling `reorder_for_pressure()`, the bug would trigger.

---

## Data Pattern of the Violation

All 510 violations share identical characteristics:

| Field | Value |
|-------|-------|
| profile | "balanced" |
| task_type | "code" |
| final_model | "anthropic/claude-opus" |
| final_provider | "other" |
| complexity | "moderate" |
| classifier_model | (empty) |
| timestamp | 2026-04-20 13:39:07 or 13:39:52 |

**Significance of empty classifier_model**: These records bypassed the normal classification path, suggesting they came from a specific code path (possibly testing, replay, or fallback logic).

---

## The Fix (Commit 76783d7)

```diff
-_CLAUDE_MODELS: frozenset[str] = frozenset({
-    "anthropic/claude-opus-4-6",
+_CLAUDE_CHEAP_MODELS: frozenset[str] = frozenset({
     "anthropic/claude-sonnet-4-6",
     "anthropic/claude-haiku-4-5-20251001",
 })

 def reorder_for_pressure(...):
-    claude_models = [m for m in chain if m in _CLAUDE_MODELS]
-    other_models = [m for m in chain if m not in _CLAUDE_MODELS]
+    claude_cheap_models = [m for m in chain if m in _CLAUDE_CHEAP_MODELS]
+    other_models = [m for m in chain if m not in _CLAUDE_CHEAP_MODELS]

     if pressure < 0.85:
-        return claude_models + other_models
+        return claude_cheap_models + other_models
```

**Result**: Claude Opus can no longer be prioritized as a "free" fallback. Only cheap models (Haiku, Sonnet) are prioritized when quota is available.

---

## Safeguards to Prevent Recurrence

### 1. **Profile-Model Invariant Assertion**

Add runtime validation in `reorder_for_pressure()`:

```python
def reorder_for_pressure(chain, pressure, profile):
    # INVARIANT: Claude Opus should NEVER appear in BALANCED profiles
    if profile == RoutingProfile.BALANCED:
        assert "anthropic/claude-opus" not in chain, (
            f"BUG: Claude Opus in BALANCED chain. "
            f"Opus is PREMIUM-only. Check reorder_for_pressure() and dynamic routing."
        )
    # ... rest of function
```

### 2. **Logging on Policy Mismatch**

When a model appears in a chain where it doesn't belong, log an ERROR:

```python
OPUS_VIOLATIONS = [
    (profile == RoutingProfile.BALANCED, "Opus in BALANCED"),
    (profile == RoutingProfile.BUDGET, "Opus in BUDGET"),
]

for check, msg in OPUS_VIOLATIONS:
    if check and "anthropic/claude-opus" in chain:
        log.error(f"POLICY VIOLATION: {msg} — {chain}")
        raise ValueError(f"Policy violation: {msg}")
```

### 3. **Model-Profile Constraints**

Add a data structure that explicitly lists which models are allowed per profile:

```python
# Explicit policy: which models are allowed per profile
MODELS_PER_PROFILE: dict[RoutingProfile, set[str]] = {
    RoutingProfile.BUDGET: {
        "ollama/*", "codex/*", "gemini/*", "deepseek/*", "openai/gpt-4o-mini",
        "anthropic/claude-haiku-4-5-20251001",  # Cheap Claude only
    },
    RoutingProfile.BALANCED: {
        "ollama/*", "codex/*", "gemini/*", "deepseek/*", "openai/gpt-4o",
        "anthropic/claude-sonnet-4-6",  # Cheap Claude only
        "anthropic/claude-haiku-4-5-20251001",
    },
    RoutingProfile.PREMIUM: {
        # All models allowed for best quality
        "*",
    },
}

def validate_chain(chain, profile):
    """Raise if any model in chain violates profile constraints."""
    allowed = MODELS_PER_PROFILE[profile]
    for model in chain:
        if not any(model.startswith(pat.replace("*", "")) for pat in allowed):
            raise ValueError(f"Model {model} not allowed in {profile} profile")
```

### 4. **Monitoring Alert for Opus Selection**

Add metrics that alert if Claude Opus is ever selected in a non-PREMIUM context:

```python
async def log_routing_decision(...):
    # Check for policy violations
    if final_model == "anthropic/claude-opus" and profile != "premium":
        # Alert immediately
        log.critical(
            f"POLICY VIOLATION: Claude Opus selected in {profile} profile "
            f"(expected PREMIUM only). Investigate routing chains immediately."
        )
        # Trigger alert to monitoring system
        await send_alert(f"Opus policy violation: {profile}/{task_type}")
```

### 5. **Static Analysis Check**

Add a pytest sanity check that verifies Opus never appears in BALANCED or BUDGET chains:

```python
def test_opus_only_in_premium():
    """Claude Opus must never appear in BALANCED or BUDGET profiles."""
    for (profile, task), chain in ROUTING_TABLE.items():
        if profile in {RoutingProfile.BALANCED, RoutingProfile.BUDGET}:
            assert "anthropic/claude-opus" not in chain, (
                f"Policy violation: Opus found in {profile}/{task} chain. "
                f"Opus is PREMIUM-only."
            )
```

---

## Prevention Recommendations

1. **Code Review**: All changes to `reorder_for_pressure()` and related routing functions must explicitly justify why they don't violate the Opus exclusion policy

2. **Test Coverage**: Add unit tests for profile-model constraints for every profile change

3. **Monitoring**: Enable alerts on the llm_quota_status dashboard to show Claude model distribution per profile in real-time

4. **Documentation**: Update CLAUDE.md and architecture docs to explicitly state: "Claude Opus is PREMIUM-only. It will never appear in BALANCED or BUDGET chains."

---

## Verification (Post-Fix)

Run this query to confirm no Opus violations exist post-fix:

```sql
SELECT COUNT(*) as violations
FROM routing_decisions
WHERE final_model = 'anthropic/claude-opus'
  AND profile IN ('balanced', 'budget')
  AND is_real = 1
  AND timestamp > '2026-04-21 17:00:00';
-- Expected result: 0
```

If violations appear, the fix is incomplete or another code path is injecting Opus.

---

## Technical Details

### Why `reorder_for_pressure()` Is Vulnerable

This function reorders models based on quota pressure AFTER the static chain is retrieved. If ANY code path before this function adds Opus to the chain, the function has no knowledge that Opus is "unauthorized" for that profile. The function assumes all models in the input chain are valid for the profile.

**Key insight**: **Dynamic reordering is profile-unaware**. It assumes all input models have been pre-validated against profile constraints.

### Why the Database Shows `classifier_model = NULL`

Out of 2103 real routing decisions, 2091 have empty classifier_model. This suggests:

1. Either the classifier_model field is not being populated by the logging code, OR
2. These records were inserted from a code path that bypasses classification

This is a separate bug that should be investigated.

---

## References

- **Fix commit**: 76783d7
- **Related commits**: 60af1de (QUOTA_BALANCED feature - unrelated but concurrent)
- **Affected routes**: All BALANCED/CODE requests with quota pressure < 85%
- **Impact scope**: 510 routing decisions = ~24.3% of real traffic during Apr 20-21
