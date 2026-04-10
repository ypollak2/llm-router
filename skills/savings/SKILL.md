# LLM Router — Savings Tracking Skill

Track and report how much you've saved by routing tasks to cheaper models.

## Viewing Savings

```
llm_savings          — Cross-session savings by period (today / week / month / all time)
llm_usage("all")     — Full dashboard: subscription %, Codex status, savings, providers
llm_dashboard        — Open web dashboard at localhost:7337
```

## Savings Digest (Slack / Discord)

Send a weekly savings summary to your team channel:

```
llm_digest(period="week")                 — format digest, print only
llm_digest(period="week", send=True)      — format + push to LLM_ROUTER_WEBHOOK_URL
```

Set `LLM_ROUTER_WEBHOOK_URL=https://hooks.slack.com/...` in your environment.
Auto-detects Slack, Discord, or generic JSON webhook from the URL.

## Spend Spike Alerts

The digest automatically flags when today's spend is > 2× the 7-day average.
No config needed — it's always on.

## Routing Policy

```
llm_policy           — Show active org/repo routing policy + last 10 audit decisions
```

Set policy in `~/.llm-router/org-policy.yaml`:

```yaml
block_models:
  - "o3*"          # never use o3 series (too expensive)
  - "gpt-4o"
allow_models:
  - "gemini*"       # prefer Gemini (allow overrides block)
task_caps:
  image: 2.00       # max $2/day on image generation
```

## Routing Quality

```
llm_benchmark        — Per-task routing accuracy from your 👍/👎 feedback
llm_rate             — Rate the last response to improve future routing
llm_quality_report   — Full routing stats, classifier accuracy, savings metrics
```
